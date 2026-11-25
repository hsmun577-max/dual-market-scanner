"""Entry score floors and a cost-adjusted reward-to-risk gate."""

import math

from exit_policy import FUND_STOP_PCT, STOCK_STOP_PCT, classify_asset, exit_policy_for

MIN_COMPOSITE_SCORE = 56.0
FUND_SCORE_FLOOR = 53.0
EARLY_ENTRY_SCORE_FLOOR = 53.0
MIN_CATALYST_COMPOSITE_SCORE = 50.0
MIN_NET_REWARD_RISK = 1.5
ROUND_TRIP_COST_FLOOR = .003
NON_SPREAD_COST = .001


def score_floor(row, strategy='trend'):
    if strategy == 'earnings_catalyst':
        return MIN_CATALYST_COMPOSITE_SCORE
    if strategy == 'early_ma_alignment':
        return EARLY_ENTRY_SCORE_FLOOR
    return FUND_SCORE_FLOOR if classify_asset(row)[0] == 'fund' else MIN_COMPOSITE_SCORE


def minimum_gross_reward(stop_pct, cost=ROUND_TRIP_COST_FLOOR):
    return MIN_NET_REWARD_RISK * (stop_pct + cost) + cost


def _positive_price(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def reward_check(row, entry, base_target, bid=None):
    """Use the base scenario, capped at default take-profit, and actual spread if known."""
    policy = exit_policy_for(row)
    cost = ROUND_TRIP_COST_FLOOR
    if _positive_price(entry) and _positive_price(bid) and bid <= entry:
        cost = max(cost, (entry - bid) / entry + NON_SPREAD_COST)
    required = minimum_gross_reward(policy.stop_pct, cost)
    reward = (min(base_target / entry - 1, policy.take_profit_pct)
              if _positive_price(entry) and _positive_price(base_target) else None)
    net_ratio = (reward - cost) / (policy.stop_pct + cost) if reward is not None else None
    return dict(
        allowed=net_ratio is not None and net_ratio >= MIN_NET_REWARD_RISK - 1e-9,
        minimum_gross_reward_pct=round(required * 100, 4),
        gross_reward_pct=round(reward * 100, 4) if reward is not None else None,
        net_reward_risk=round(net_ratio, 4) if net_ratio is not None else None,
        assumed_round_trip_cost_pct=round(cost * 100, 4),
    )


def entry_policy_snapshot():
    return dict(
        score_floors=dict(stock_trend=MIN_COMPOSITE_SCORE, fund_trend=FUND_SCORE_FLOOR,
                          early_entry=EARLY_ENTRY_SCORE_FLOOR,
                          confirmed_disclosure=MIN_CATALYST_COMPOSITE_SCORE),
        min_net_reward_risk=MIN_NET_REWARD_RISK,
        round_trip_cost_floor_pct=ROUND_TRIP_COST_FLOOR * 100,
        non_spread_cost_pct=NON_SPREAD_COST * 100,
        minimum_gross_reward_pct=dict(
            stock=round(minimum_gross_reward(STOCK_STOP_PCT) * 100, 2),
            fund=round(minimum_gross_reward(FUND_STOP_PCT) * 100, 2),
        ),
    )
