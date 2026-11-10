"""Aggregate statistics over finished games.

`compute_stats(rows)` takes rows from summary.jsonl (one dict per game) and
returns a plain dict (JSON-safe) used by analyze.py and the GUI server.

Baselines worth knowing:
* If everyone voted at random, then among games that end with somebody voted
  out, the imposter would be the one removed 1 time in 4 (25%).
* A single trio ballot cast at random hits the imposter 1 time in 3.
"""

from __future__ import annotations

import json
from pathlib import Path
from statistics import mean, median

RANDOM_CATCH_BASELINE = 0.25
RANDOM_BALLOT_BASELINE = 1 / 3
FAILED = ("error", "aborted")


def read_summary(path: str | Path) -> list[dict]:
    """Read a summary.jsonl file, skipping blank or broken lines."""
    rows: list[dict] = []
    p = Path(path)
    if not p.exists():
        return rows
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _record() -> dict:
    return {"games": 0, "wins": 0, "losses": 0, "no_decision": 0, "errors": 0}


def _tally(rec: dict, row: dict, role: str) -> None:
    rec["games"] += 1
    outcome, winner = row.get("outcome"), row.get("winner")
    if outcome in FAILED:
        rec["errors"] += 1
    elif winner == role:
        rec["wins"] += 1
    elif winner in ("trio", "imposter"):
        rec["losses"] += 1
    else:
        rec["no_decision"] += 1


def _num_summary(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "mean": None, "median": None, "min": None, "max": None, "total": 0}
    return {"count": len(values), "mean": mean(values), "median": median(values),
            "min": min(values), "max": max(values), "total": sum(values)}


def _rate(k: int, n: int) -> float | None:
    return k / n if n else None


def compute_stats(rows: list[dict]) -> dict:
    by_model: dict[str, dict] = {}
    pairings: dict[tuple[str, str], dict] = {}
    outcomes: dict[str, int] = {}
    for row in rows:
        trio, imp = row.get("trio_model") or "?", row.get("imposter_model") or "?"
        outcome = row.get("outcome") or "unknown"
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        _tally(by_model.setdefault(trio, {"as_trio": _record(), "as_imposter": _record()})["as_trio"], row, "trio")
        _tally(by_model.setdefault(imp, {"as_trio": _record(), "as_imposter": _record()})["as_imposter"], row,
               "imposter")
        pr = pairings.setdefault((trio, imp), {"trio_model": trio, "imposter_model": imp, "games": 0,
                                               "trio_wins": 0, "imposter_wins": 0, "no_decision": 0, "errors": 0})
        pr["games"] += 1
        if outcome in FAILED:
            pr["errors"] += 1
        elif outcome == "trio_win":
            pr["trio_wins"] += 1
        elif outcome == "imposter_win":
            pr["imposter_wins"] += 1
        else:
            pr["no_decision"] += 1

    for rec in by_model.values():
        for side in rec.values():
            side["win_rate_decisive"] = _rate(side["wins"], side["wins"] + side["losses"])

    decisive = [r for r in rows if r.get("outcome") in ("trio_win", "imposter_win")]
    caught = sum(1 for r in decisive if r.get("outcome") == "trio_win")

    trio_ballots = sum(sum(r.get("trio_valid_ballots") or []) for r in rows if r.get("outcome") not in FAILED)
    on_imposter = sum(sum(r.get("imposter_votes_received_by_trio") or []) for r in rows
                      if r.get("outcome") not in FAILED)

    played = [r for r in rows if r.get("outcome") not in FAILED]
    survived = [r["rounds_survived"] for r in played if isinstance(r.get("rounds_survived"), (int, float))]
    imp_survived = [r["imposter_rounds_survived"] for r in played
                    if isinstance(r.get("imposter_rounds_survived"), (int, float))]
    costs = [float(r["total_cost_usd"]) for r in rows if isinstance(r.get("total_cost_usd"), (int, float))]
    tin = [r.get("total_input_tokens") or 0 for r in rows]
    tout = [r.get("total_output_tokens") or 0 for r in rows]

    return {
        "num_games": len(rows),
        "num_mock_games": sum(1 for r in rows if r.get("mock")),
        "outcomes": outcomes,
        "by_model": by_model,
        "by_pairing": sorted(pairings.values(), key=lambda p: (-p["games"], p["trio_model"], p["imposter_model"])),
        "imposter_caught": {"decisive_games": len(decisive), "caught": caught,
                            "rate": _rate(caught, len(decisive)), "random_baseline": RANDOM_CATCH_BASELINE},
        "trio_ballots": {"valid_trio_ballots": trio_ballots, "on_imposter": on_imposter,
                         "rate": _rate(on_imposter, trio_ballots), "random_baseline": RANDOM_BALLOT_BASELINE},
        "rounds_survived": _num_summary(survived),
        "imposter_rounds_survived": _num_summary(imp_survived),
        "rounds_completed": _num_summary([r.get("rounds_completed") or 0 for r in played]),
        "cost_usd": _num_summary(costs),
        "tokens": {"mean_input": mean(tin) if tin else None, "mean_output": mean(tout) if tout else None,
                   "total_input": sum(tin), "total_output": sum(tout)},
    }
