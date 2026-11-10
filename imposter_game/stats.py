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
NO_IMPOSTER_OUTCOMES = ("trio_win_no_imposter", "trio_loss_no_imposter")


def has_imposter(row: dict) -> bool:
    """Old rows have no has_imposter field: they all had an imposter."""
    return row.get("has_imposter", True) is not False and row.get("outcome") not in NO_IMPOSTER_OUTCOMES


def prompt_version_of(row: dict) -> str:
    return row.get("prompt_version") or "legacy"


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


def _no_imposter_stats(rows: list[dict]) -> dict:
    """Games with no imposter: did the trio correctly vote nobody out?"""
    games = [r for r in rows if not has_imposter(r) and r.get("outcome") not in FAILED]
    false_elims = sum(1 for r in games if r.get("outcome") == "trio_loss_no_imposter")
    by_model: dict[str, dict] = {}
    for r in games:
        m = by_model.setdefault(r.get("trio_model") or "?", {"games": 0, "false_eliminations": 0})
        m["games"] += 1
        m["false_eliminations"] += r.get("outcome") == "trio_loss_no_imposter"
    for m in by_model.values():
        m["false_elimination_rate"] = _rate(m["false_eliminations"], m["games"])
    return {"games": len(games), "trio_wins": len(games) - false_elims, "false_eliminations": false_elims,
            "false_elimination_rate": _rate(false_elims, len(games)), "by_trio_model": by_model,
            "errors": sum(1 for r in rows if not has_imposter(r) and r.get("outcome") in FAILED)}


def _by_prompt_version(rows: list[dict]) -> list[dict]:
    out: dict[str, dict] = {}
    for r in rows:
        v = prompt_version_of(r)
        d = out.setdefault(v, {"prompt_version": v, "games": 0, "modified": False, "decisive_games": 0,
                               "caught": 0, "no_imposter_games": 0, "errors": 0, "first_seen": r.get("timestamp")})
        d["games"] += 1
        d["modified"] = d["modified"] or bool(r.get("prompts_modified"))
        if r.get("outcome") in FAILED:
            d["errors"] += 1
        elif not has_imposter(r):
            d["no_imposter_games"] += 1
        elif r.get("outcome") in ("trio_win", "imposter_win"):
            d["decisive_games"] += 1
            d["caught"] += r.get("outcome") == "trio_win"
    for d in out.values():
        d["catch_rate"] = _rate(d["caught"], d["decisive_games"])
    return sorted(out.values(), key=lambda d: str(d.get("first_seen") or ""))


def compute_stats(rows: list[dict]) -> dict:
    """Stats over summary rows. Win/loss, pairings, catch rates and ballots use imposter games only;
    no-imposter games are reported in the "no_imposter" section."""
    all_rows = rows
    rows = [r for r in all_rows if has_imposter(r)]
    by_model: dict[str, dict] = {}
    pairings: dict[tuple[str, str], dict] = {}
    outcomes: dict[str, int] = {}
    for row in all_rows:
        o = row.get("outcome") or "unknown"
        outcomes[o] = outcomes.get(o, 0) + 1
    for row in rows:
        trio, imp = row.get("trio_model") or "?", row.get("imposter_model") or "?"
        outcome = row.get("outcome") or "unknown"
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
    votes_by_trigger: dict[str, int] = {}
    elim_by_trigger: dict[str, int] = {}
    requests: dict[str, dict] = {}
    for r in played:
        for v in r.get("votes") or []:
            trig = v.get("trigger") or "scheduled"
            votes_by_trigger[trig] = votes_by_trigger.get(trig, 0) + 1
        if r.get("eliminated"):
            trig = r.get("eliminated_by") or "scheduled"
            elim_by_trigger[trig] = elim_by_trigger.get(trig, 0) + 1
        for model, c in (r.get("vote_requests_by_model") or {}).items():
            rec = requests.setdefault(model, {"yes": 0, "no": 0, "missing": 0})
            for k in rec:
                rec[k] += int((c or {}).get(k) or 0)
    for rec in requests.values():
        rec["yes_rate"] = _rate(rec["yes"], rec["yes"] + rec["no"] + rec["missing"])

    costs = [float(r["total_cost_usd"]) for r in all_rows if isinstance(r.get("total_cost_usd"), (int, float))]
    tin = [r.get("total_input_tokens") or 0 for r in all_rows]
    tout = [r.get("total_output_tokens") or 0 for r in all_rows]

    return {
        "num_games": len(all_rows),
        "num_imposter_games": len(rows),
        "num_mock_games": sum(1 for r in all_rows if r.get("mock")),
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
        "turns_completed": _num_summary([r.get("turns_completed") or 0 for r in played
                                         if r.get("turns_completed") is not None]),
        "vote_calls": {
            "games_with_vote_calls": sum(1 for r in rows if r.get("player_vote_calls")),
            "called_votes": sum(int(r.get("called_votes") or 0) for r in played),
            "scheduled_votes": sum(int(r.get("scheduled_votes") or 0) for r in played),
            "votes_by_trigger": votes_by_trigger,
            "eliminations_by_trigger": elim_by_trigger,
            "vote_requests_by_model": requests,
        },
        "no_imposter": _no_imposter_stats(all_rows),
        "by_prompt_version": _by_prompt_version(all_rows),
        "cost_usd": _num_summary(costs),
        "tokens": {"mean_input": mean(tin) if tin else None, "mean_output": mean(tout) if tout else None,
                   "total_input": sum(tin), "total_output": sum(tout)},
    }
