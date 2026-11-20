"""Sell rules: −2% stop, +6% default take profit, and a surge trailing hold.

The owner's rule, in their words:

  기본은 +6% 익절이다. 그런데 단기 급등으로 +6%를 넘어서는 것이라면 +6%에서
  팔지 말고 기다린다. 그러다 거래량이 확 빠지면서 고점에서 급락하는 신호가
  나오면, 급락하기 전 가격에 팔아 버린다.

So a position has two modes:

* ``fixed`` — the default. Take profit at +6% (a resting +6% limit sell), stop
  at −2%.
* ``trailing`` — entered only when the +6% level was crossed by a short-term
  surge (within ``surge_window`` of entry and on expanded volume). While in this
  mode the +6% target is not sold into; instead the position keeps riding, and
  it is closed at the price observed when the collapse signal appears: volume
  falling away (≤ ``volume_collapse_ratio`` of the surge leg's strongest bar)
  together with a rollover from the peak (≥ ``peak_pullback_pct`` below it).
  The close is the observed price at that moment — before the plunge, not at
  its bottom — and the +6% gain is kept as a floor so a fast round trip cannot
  turn a surged position into a loss.

Missing data never invents a hold: without a pre-entry volume baseline the
surge cannot be confirmed, so the default +6% take profit applies. Those
decisions are marked in the returned dict so a replay can report them.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
import math

DEFAULT_STOP_PCT = 0.02
DEFAULT_TAKE_PROFIT_PCT = 0.06
DEFAULT_SURGE_WINDOW = timedelta(minutes=30)
DEFAULT_SURGE_VOLUME_RATIO = 1.5
DEFAULT_VOLUME_COLLAPSE_RATIO = 0.5
DEFAULT_PEAK_PULLBACK_PCT = 0.015
DEFAULT_HOLD_FLOOR_PCT = 0.06


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


@dataclass(frozen=True)
class ExitPolicy:
    """Numbers of the sell rule. Percentages are fractions of the entry price."""

    stop_pct: float = DEFAULT_STOP_PCT
    take_profit_pct: float = DEFAULT_TAKE_PROFIT_PCT
    surge_window: timedelta = DEFAULT_SURGE_WINDOW
    surge_volume_ratio: float = DEFAULT_SURGE_VOLUME_RATIO
    volume_collapse_ratio: float = DEFAULT_VOLUME_COLLAPSE_RATIO
    peak_pullback_pct: float = DEFAULT_PEAK_PULLBACK_PCT
    hold_floor_pct: float = DEFAULT_HOLD_FLOOR_PCT
    take_profit_mode: str = "surge_trailing"

    def __post_init__(self):
        for name in ("stop_pct", "take_profit_pct", "peak_pullback_pct", "hold_floor_pct"):
            value = getattr(self, name)
            if not _finite(value) or not 0 < value < 1:
                raise ValueError(f"{name} must be a fraction between 0 and 1")
        for name in ("surge_volume_ratio", "volume_collapse_ratio"):
            value = getattr(self, name)
            if not _finite(value) or value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.volume_collapse_ratio >= 1:
            raise ValueError("volume_collapse_ratio must be below 1 to mean a collapse")
        if not isinstance(self.surge_window, timedelta) or self.surge_window <= timedelta(0):
            raise ValueError("surge_window must be a positive timedelta")
        if self.hold_floor_pct > self.take_profit_pct:
            raise ValueError("hold_floor_pct cannot exceed the take profit level")

    def snapshot(self):
        """JSON-friendly form embedded in risk plans and alert payloads."""
        return {
            "stop_pct": self.stop_pct,
            "take_profit_pct": self.take_profit_pct,
            "take_profit_mode": self.take_profit_mode,
            "surge_window_minutes": int(self.surge_window.total_seconds() // 60),
            "surge_volume_ratio": self.surge_volume_ratio,
            "volume_collapse_ratio": self.volume_collapse_ratio,
            "peak_pullback_pct": self.peak_pullback_pct,
            "hold_floor_pct": self.hold_floor_pct,
            "exit_price_rule": "signal_price_before_plunge",
        }

    def describe(self):
        """One line for alerts, using the same numbers as this policy."""
        return (f"손절 −{self.stop_pct:.0%} · 기본 익절 +{self.take_profit_pct:.0%} · "
                f"급등 시 +{self.take_profit_pct:.0%} 매도 대신 트레일링 보유, "
                f"거래량 급감·고점 대비 {self.peak_pullback_pct:.1%} 하락 시 그 가격에 청산")


DEFAULT_EXIT_POLICY = ExitPolicy()


def policy_snapshot(policy=DEFAULT_EXIT_POLICY):
    return policy.snapshot()


class ExitTracker:
    """One position under the sell rule; feed it quotes or bars in time order.

    ``observe`` returns a decision dict whose ``action`` is ``hold`` or
    ``exit``. ``exit`` carries ``exit_price`` (the price the rule sells at) and
    a ``reason``:

    * ``stop_loss`` — bid fell to the −2% stop; filled at the observed price.
    * ``take_profit`` — slow rise to +6%; a resting +6% limit fills at +6%.
    * ``surge_trailing_exit`` — collapse signal after a surge; filled at the
      price observed at the signal, which is the price before the plunge.
    * ``trailing_floor`` — a surged position fell back to the +6% floor; filled
      at +6%, so the surge hold can never do worse than the default rule.

    Holds carry ``reason`` ``surge_hold`` (the crossing observation that turned
    the position into a trailing hold) or ``trailing_hold``. ``mode`` and
    ``volume_observed`` show what evidence the decision had.
    """

    def __init__(self, entry, policy=DEFAULT_EXIT_POLICY, *, entry_at=None, baseline_volume=None):
        if not _finite(entry) or entry <= 0:
            raise ValueError("entry must be a positive finite price")
        if entry_at is not None and (not isinstance(entry_at, datetime) or entry_at.utcoffset() is None):
            raise ValueError("entry_at must be a timezone-aware datetime")
        if baseline_volume is not None and (not _finite(baseline_volume) or baseline_volume <= 0):
            raise ValueError("baseline_volume must be a positive finite volume")
        self.policy = policy
        self.entry = float(entry)
        self.entry_at = entry_at
        self.baseline_volume = baseline_volume
        self.mode = "fixed"
        self.peak = float(entry)
        self.stop_price = self.entry * (1 - policy.stop_pct)
        self.target_price = self.entry * (1 + policy.take_profit_pct)
        self.floor_price = self.entry * (1 + policy.hold_floor_pct)
        self.surge_reference_volume = 0.0
        self.peak_volume = 0.0
        self.surge_at = None
        self.closed = None
        self.observations = 0

    # -- helpers ---------------------------------------------------------
    def _decision(self, action, reason, at, price, **extra):
        decision = {
            "action": action,
            "reason": reason,
            "mode": self.mode,
            "at": at.isoformat(),
            "price": float(price),
            "peak": self.peak,
            "entry": self.entry,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "floor_price": self.floor_price,
        }
        decision.update(extra)
        return decision

    def _close(self, at, price, reason, **extra):
        self.closed = {"at": at.isoformat(), "price": float(price), "reason": reason, "mode": self.mode}
        return self._decision("exit", reason, at, price, exit_price=float(price), **extra)

    def _surge_confirmed(self, at, volume):
        """A short-term surge: fast move to the target on expanded volume."""
        if not _finite(volume) or volume <= 0:
            return False, "volume_missing"
        if self.entry_at is None:
            return False, "entry_time_missing"
        if at - self.entry_at > self.policy.surge_window:
            return False, "outside_surge_window"
        if self.baseline_volume is None:
            return False, "baseline_volume_missing"
        if volume < self.policy.surge_volume_ratio * self.baseline_volume:
            return False, "volume_not_expanded"
        return True, "short_term_surge"

    # -- main ------------------------------------------------------------
    def observe(self, at, price, *, volume=None, high=None, low=None):
        if not isinstance(at, datetime) or at.utcoffset() is None:
            raise ValueError("observation time must be timezone-aware")
        if not _finite(price) or price <= 0:
            raise ValueError("observation price must be positive and finite")
        if volume is not None and (not _finite(volume) or volume < 0):
            raise ValueError("volume must be a non-negative finite number")
        if self.closed is not None:
            raise RuntimeError("position already closed")
        self.observations += 1

        top = float(price)
        for candidate in (high, low):
            if _finite(candidate) and candidate > top:
                top = float(candidate)
        self.peak = max(self.peak, top)
        if _finite(volume):
            self.peak_volume = max(self.peak_volume, float(volume))

        if self.mode == "fixed":
            if price <= self.stop_price:
                return self._close(at, price, "stop_loss")
            if price >= self.target_price:
                surge, detail = self._surge_confirmed(at, volume)
                if not surge:
                    return self._close(at, self.target_price, "take_profit", surge_check=detail)
                self.mode = "trailing"
                self.surge_at = at
                self.surge_reference_volume = float(volume)
                return self._decision("hold", "surge_hold", at, price,
                                      surge_check=detail, volume_observed=True)
            return self._decision("hold", "fixed_hold", at, price)

        # trailing: the +6% target is no longer a sell level
        if _finite(volume) and volume > 0:
            self.surge_reference_volume = max(self.surge_reference_volume, float(volume))
        if price <= self.floor_price:
            return self._close(at, self.floor_price, "trailing_floor")
        if not _finite(volume):
            return self._decision("hold", "trailing_hold", at, price, volume_observed=False)
        collapsed = volume <= self.policy.volume_collapse_ratio * self.surge_reference_volume
        rolled_over = price <= self.peak * (1 - self.policy.peak_pullback_pct)
        if collapsed and rolled_over:
            return self._close(at, price, "surge_trailing_exit", volume_observed=True)
        return self._decision("hold", "trailing_hold", at, price, volume_observed=True,
                              volume_collapsed=collapsed, rolled_over=rolled_over)


def evaluate_series(entry, observations, policy=DEFAULT_EXIT_POLICY, *, entry_at=None,
                    baseline_volume=None):
    """Run a whole observation series and return the last decision.

    ``observations`` items are dicts with ``at``, ``price`` and optional
    ``volume``/``high``/``low``. The series stops at the first exit, which is
    returned as the final decision.
    """
    tracker = ExitTracker(entry, policy, entry_at=entry_at, baseline_volume=baseline_volume)
    decision = None
    for item in observations:
        decision = tracker.observe(item["at"], item["price"], volume=item.get("volume"),
                                   high=item.get("high"), low=item.get("low"))
        if decision["action"] == "exit":
            break
    return decision
