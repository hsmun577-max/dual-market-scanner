"""Aggregate quote-based 30-minute replays from retained intraday Actions artifacts."""
import json
import os
import subprocess
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path


WORKFLOWS = ("kis-intraday.yml", "kis-us-intraday.yml")
ARTIFACT_NAME = "intraday-replay"


def gh(*args):
    result = subprocess.run(["gh", *args], check=True, capture_output=True, text=True)
    return result.stdout


def aggregate(start: date, end: date):
    repository = os.environ["GITHUB_REPOSITORY"]
    totals = defaultdict(lambda: {"closed_trades": 0, "winning_trades": 0,
                                  "counts": defaultdict(int), "runs": set()})
    runs_seen, artifacts_seen, issues = [], 0, []
    for workflow in WORKFLOWS:
        runs = json.loads(gh("run", "list", "--workflow", workflow, "--branch", "main",
                             "--limit", "100", "--json",
                             "databaseId,createdAt,conclusion,displayTitle"))
        for run in runs:
            created = date.fromisoformat(run["createdAt"][:10])
            if not start <= created <= end or run.get("conclusion") != "success":
                continue
            run_id = str(run["databaseId"])
            runs_seen.append({"run_id": run_id, "workflow": workflow,
                              "created_at": run["createdAt"], "title": run.get("displayTitle")})
            metadata = json.loads(gh("api", f"repos/{repository}/actions/runs/{run_id}/artifacts"))
            artifact = next((item for item in metadata.get("artifacts", [])
                             if item.get("name") == ARTIFACT_NAME and not item.get("expired")), None)
            if artifact is None:
                continue
            folder = Path("replay-artifacts") / run_id
            folder.mkdir(parents=True, exist_ok=True)
            try:
                gh("run", "download", run_id, "--name", ARTIFACT_NAME, "--dir", str(folder))
            except subprocess.CalledProcessError as exc:
                issues.append({"run_id": run_id, "error": "artifact_download_failed",
                               "detail": (exc.stderr or "")[-300:]})
                continue
            reports = list(folder.rglob("*-replay.json"))
            if not reports:
                issues.append({"run_id": run_id, "error": "replay_summary_missing"})
                continue
            artifacts_seen += 1
            for path in reports:
                try:
                    document = json.loads(path.read_text(encoding="utf-8-sig"))
                except (OSError, json.JSONDecodeError) as exc:
                    issues.append({"run_id": run_id, "error": "invalid_replay_summary",
                                   "detail": type(exc).__name__})
                    continue
                for market, strategies in document.items():
                    if not isinstance(strategies, dict):
                        continue
                    for strategy, metrics in strategies.items():
                        if not isinstance(metrics, dict):
                            continue
                        bucket = totals[(market, strategy)]
                        bucket["runs"].add(run_id)
                        bucket["closed_trades"] += int(metrics.get("closed_trades") or 0)
                        bucket["winning_trades"] += int(metrics.get("winning_trades") or 0)
                        for status, count in (metrics.get("counts") or {}).items():
                            bucket["counts"][status] += int(count)
                        mean = metrics.get("mean_net_return_pct")
                        n = int(metrics.get("closed_trades") or 0)
                        if isinstance(mean, (int, float)) and n:
                            bucket.setdefault("weighted_return_sum", 0.0)
                            bucket["weighted_return_sum"] += mean * n
    results = []
    for (market, strategy), bucket in sorted(totals.items()):
        n = bucket["closed_trades"]
        wins = bucket["winning_trades"]
        results.append({
            "market": market, "strategy": strategy, "runs_with_samples": len(bucket["runs"]),
            "closed_trades": n, "winning_trades": wins, "losing_trades": n - wins,
            "win_rate_pct": round(100 * wins / n, 2) if n else None,
            "mean_net_return_pct": round(bucket.get("weighted_return_sum", 0.0) / n, 4) if n else None,
            "sample_status_counts": dict(sorted(bucket["counts"].items())),
        })
    return {
        "period": [start.isoformat(), end.isoformat()],
        "method": ("Archived five-minute bars and timestamped bid/ask quotes; baseline entries are closed by the "
                   "legacy +2% target/30-minute rule, policy entries by exit_policy.py's instrument stop "
                   "(−2% fund / −3% single stock), +6% take profit and surge trailing exit; estimated fees "
                   "from each source replay."),
        "scope": "Only successful intraday runs with a retained intraday-replay artifact; daily/preopen candidate alerts are not represented unless they produced a quote replay sample.",
        "workflow_runs_found": len(runs_seen), "artifacts_with_replays": artifacts_seen,
        "runs": runs_seen, "results": results, "collection_issues": issues,
        "verdict": ("no_evaluated_trades" if not any(row["closed_trades"] for row in results)
                    else "measured_but_not_statistically_validated"),
        "limitations": [
            "Overlapping candidate samples are individual hypothetical trades, not portfolio-level returns.",
            "A two-week date range does not imply a two-week sample; report the actual run and closed-trade counts.",
            "No out-of-sample win-rate claim is made from a small sample.",
            "Policy-mode positions still open when the 30-minute quote horizon ends are counted as "
            "held_past_horizon and marked to market, not closed as trades.",
            "The surge trailing exit needs five-minute volume after the signal (post_bars); samples without it "
            "fall back to the +6% take profit and cannot show a trailing exit.",
        ],
    }


def main():
    # Default to the last ten sessions instead of a fixed week that goes stale.
    today = date.today()
    start = date.fromisoformat(os.environ.get("START_DATE", (today - timedelta(days=13)).isoformat()))
    end = date.fromisoformat(os.environ.get("END_DATE", today.isoformat()))
    if start > end:
        raise SystemExit("START_DATE must be on or before END_DATE")
    result = aggregate(start, end)
    Path("intraday-two-week-replay.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as out:
            out.write("## Intraday replay\n\n")
            out.write(json.dumps({"period": result["period"], "scope": result["scope"],
                                  "workflow_runs_found": result["workflow_runs_found"],
                                  "artifacts_with_replays": result["artifacts_with_replays"],
                                  "results": result["results"], "verdict": result["verdict"],
                                  "collection_issues": result["collection_issues"]},
                                 ensure_ascii=False, indent=2))
            out.write("\n")


if __name__ == "__main__":
    main()
