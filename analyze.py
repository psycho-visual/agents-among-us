"""Summarise all finished games.

    python analyze.py                  real games only (logs/summary.jsonl)
    python analyze.py --include-mock   real + mock games
    python analyze.py --mock-only      mock games only (logs/mock/summary.jsonl)
    python analyze.py --csv games.csv  also write one row per game to a CSV file

Prints win/loss per model (as trio and as imposter), results per pairing, how
often the imposter is caught compared with random voting, rounds survived,
and cost/token usage.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from imposter_game.logger import resolve_log_dir  # noqa: E402
from imposter_game.stats import compute_stats, read_summary  # noqa: E402


def pct(x) -> str:
    return "  n/a" if x is None else f"{100 * x:5.1f}%"


def num(x, digits: int = 1) -> str:
    return "n/a" if x is None else f"{x:.{digits}f}"


def money(x) -> str:
    return "n/a" if x is None else f"${x:.4f}"


def table(headers: list[str], rows: list[list], out) -> None:
    cells = [[str(c) for c in headers]] + [[str(c) for c in r] for r in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    for i, r in enumerate(cells):
        print("  " + "  ".join(c.ljust(w) if j == 0 else c.rjust(w) for j, (c, w) in enumerate(zip(r, widths))),
              file=out)
        if i == 0:
            print("  " + "  ".join("-" * w for w in widths), file=out)


def print_report(rows: list[dict], label: str, out=None) -> None:
    out = out or sys.stdout
    s = compute_stats(rows)
    print(f"Agents Among Us — {label}", file=out)
    print("=" * 60, file=out)
    print(f"Games: {s['num_games']}" + (f"  (of which mock: {s['num_mock_games']})" if s["num_mock_games"] else ""),
          file=out)
    print("Outcomes: " + ", ".join(f"{k} {v}" for k, v in sorted(s["outcomes"].items())), file=out)
    print(file=out)

    print("Win-loss per model (W-L-ND, errors/aborted excluded from W/L)", file=out)
    trows = []
    for model, rec in sorted(s["by_model"].items()):
        for side in ("as_trio", "as_imposter"):
            r = rec[side]
            if r["games"]:
                trows.append([model, side.replace("as_", ""), r["games"],
                              f"{r['wins']}-{r['losses']}-{r['no_decision']}", r["errors"],
                              pct(r["win_rate_decisive"])])
    table(["model", "role", "games", "W-L-ND", "errors", "win% (decisive)"], trows, out)
    print(file=out)

    print("Per pairing (trio vs imposter)", file=out)
    table(["trio", "imposter", "games", "trio wins", "imposter wins", "no decision", "errors"],
          [[p["trio_model"], p["imposter_model"], p["games"], p["trio_wins"], p["imposter_wins"],
            p["no_decision"], p["errors"]] for p in s["by_pairing"]], out)
    print(file=out)

    c = s["imposter_caught"]
    print(f"Imposter caught: {c['caught']} of {c['decisive_games']} decisive games = {pct(c['rate']).strip()}"
          f"  (random voting baseline: {pct(c['random_baseline']).strip()})", file=out)
    b = s["trio_ballots"]
    print(f"Trio ballots on the imposter: {b['on_imposter']} of {b['valid_trio_ballots']} = {pct(b['rate']).strip()}"
          f"  (random baseline: {pct(b['random_baseline']).strip()})", file=out)
    rs, irs = s["rounds_survived"], s["imposter_rounds_survived"]
    print(f"Rounds survived by the voted-out player: mean {num(rs['mean'])}, median {num(rs['median'])} "
          f"(n={rs['count']})", file=out)
    print(f"Rounds the imposter survived: mean {num(irs['mean'])}, median {num(irs['median'])} "
          f"(n={irs['count']})", file=out)
    print(file=out)
    cost, tok = s["cost_usd"], s["tokens"]
    print(f"Cost per game: mean {money(cost['mean'])}, min {money(cost['min'])}, max {money(cost['max'])};"
          f" total {money(cost['total'])}", file=out)
    print(f"Tokens per game: mean input {num(tok['mean_input'], 0)}, mean output {num(tok['mean_output'], 0)};"
          f" total input {tok['total_input']}, total output {tok['total_output']}", file=out)


CSV_FIELDS = ["game_id", "timestamp", "mock", "trio_model", "imposter_model", "trio_effort", "imposter_effort",
              "name_rule", "answer_mode", "rounds_before_vote", "vote_threshold", "on_no_decision",
              "imposter_seat", "outcome", "winner", "winner_model", "eliminated", "eliminated_role",
              "eliminated_model", "rounds_survived", "imposter_rounds_survived", "rounds_completed",
              "imposter_votes_received_by_trio", "total_cost_usd", "total_input_tokens", "total_output_tokens",
              "num_api_calls", "cost_by_model", "reasoning_returned", "log_file"]


def write_csv(rows: list[dict], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in r.items()})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Summarise finished Agents Among Us games.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--include-mock", action="store_true", help="include mock games")
    g.add_argument("--mock-only", action="store_true", help="only mock games")
    ap.add_argument("--csv", metavar="PATH", help="write one row per game to this CSV file")
    ap.add_argument("--log-dir", default="logs", help="log folder (default: logs in the repo folder)")
    args = ap.parse_args(argv)

    base = resolve_log_dir(args.log_dir)
    real = [] if args.mock_only else read_summary(base / "summary.jsonl")
    mock = read_summary(base / "mock" / "summary.jsonl") if (args.include_mock or args.mock_only) else []
    rows = real + mock
    label = "mock games" if args.mock_only else ("real + mock games" if args.include_mock else "real games")

    if not rows:
        print(f"No finished games found ({label}) in {base}.")
        if not args.mock_only and not args.include_mock and read_summary(base / "mock" / "summary.jsonl"):
            print("There are mock games — run with --include-mock or --mock-only to see them.")
        else:
            print("Play a game first: python run.py   (or python run.py --mock for a free test game)")
        return 0
    print_report(rows, label)
    if args.csv:
        write_csv(rows, Path(args.csv))
        print(f"\nWrote {len(rows)} rows to {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
