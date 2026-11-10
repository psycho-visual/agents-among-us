"""Summarise all finished games.

    python analyze.py                  real games only (logs/summary.jsonl)
    python analyze.py --include-mock   real + mock games
    python analyze.py --mock-only      mock games only (logs/mock/summary.jsonl)
    python analyze.py --csv games.csv  also write one row per game to a CSV file
    python analyze.py --list-batches   one line per batch (python batch.py / the Batch button)
    python analyze.py --batch b20261110-181500-ab12   only the games of that batch
    python analyze.py --prompt-version 1a2b3c4d   only games played with that prompt version
                                                  ("legacy" = games from before prompt versions)

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
from imposter_game.stats import compute_stats, prompt_version_of, read_summary  # noqa: E402


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
    print(f"Games: {s['num_games']}" + (f"  (of which mock: {s['num_mock_games']})" if s["num_mock_games"] else "")
          + (f"  (with an imposter: {s['num_imposter_games']})" if s["no_imposter"]["games"] else ""), file=out)
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
    ni = s["no_imposter"]
    if ni["games"] or ni["errors"]:
        print(file=out)
        print(f"No-imposter games: {ni['games']} — trio correctly voted nobody out {ni['trio_wins']}, "
              f"voted out one of its own copies {ni['false_eliminations']} "
              f"(false-elimination rate {pct(ni['false_elimination_rate']).strip()})", file=out)
        print("  (these games are left out of all the win/loss and catch-rate numbers above)", file=out)

    if len(s["by_prompt_version"]) > 1 or any(v["prompt_version"] != "legacy" for v in s["by_prompt_version"]):
        print(file=out)
        print("By prompt version (oldest first)", file=out)
        table(["version", "games", "edited", "decisive", "imposter caught", "catch rate", "no-imposter", "errors"],
              [[v["prompt_version"], v["games"], "yes" if v["modified"] else "no", v["decisive_games"],
                v["caught"], pct(v["catch_rate"]), v["no_imposter_games"], v["errors"]]
               for v in s["by_prompt_version"]], out)

    vc = s["vote_calls"]
    if vc["games_with_vote_calls"] or vc["votes_by_trigger"].get("called"):
        print(file=out)
        print(f"Player-called votes (feature on in {vc['games_with_vote_calls']} game(s))", file=out)
        trig = vc["votes_by_trigger"]
        print("  Votes held: " + (", ".join(f"{k} {v}" for k, v in sorted(trig.items())) or "none")
              + f"  (called total {vc['called_votes']}, scheduled total {vc['scheduled_votes']};"
              " a merged called+scheduled vote counts in both)", file=out)
        elim = vc["eliminations_by_trigger"]
        print("  Eliminations by vote type: " + (", ".join(f"{k} {v}" for k, v in sorted(elim.items())) or "none"),
              file=out)
        if vc["vote_requests_by_model"]:
            table(["model", "asked YES", "said NO", "no answer", "YES rate"],
                  [[m, r["yes"], r["no"], r["missing"], pct(r["yes_rate"])]
                   for m, r in sorted(vc["vote_requests_by_model"].items())], out)
    print(file=out)
    cost, tok = s["cost_usd"], s["tokens"]
    print(f"Cost per game: mean {money(cost['mean'])}, min {money(cost['min'])}, max {money(cost['max'])};"
          f" total {money(cost['total'])}", file=out)
    print(f"Tokens per game: mean input {num(tok['mean_input'], 0)}, mean output {num(tok['mean_output'], 0)};"
          f" total input {tok['total_input']}, total output {tok['total_output']}", file=out)


CSV_FIELDS = ["game_id", "timestamp", "mock", "trio_model", "imposter_model", "trio_effort", "imposter_effort",
              "name_rule", "answer_mode", "rounds_before_vote", "vote_threshold", "on_no_decision",
              "imposter_seat", "has_imposter", "no_imposter_chance", "prompt_version", "prompts_modified",
              "normalize_typography", "paraphrase_model", "imposter_knows_own_model", "intro_word_limit",
              "player_vote_calls", "vote_call_threshold", "outcome", "winner", "winner_model", "eliminated", "eliminated_role",
              "eliminated_model", "eliminated_by", "rounds_survived", "imposter_rounds_survived", "rounds_completed",
              "turns_completed", "called_votes", "scheduled_votes", "vote_requests_by_model",
              "batch_id", "batch_index", "roles_swapped",
              "imposter_votes_received_by_trio", "total_cost_usd", "total_input_tokens", "total_output_tokens",
              "num_api_calls", "cost_by_model", "reasoning_returned", "log_file"]


def write_csv(rows: list[dict], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in r.items()})


def print_batches(rows: list[dict], label: str, base: Path, out=None) -> None:
    """One line per batch_id: games, outcomes, caught rate, cost, models."""
    out = out or sys.stdout
    batches: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("batch_id"):
            batches.setdefault(r["batch_id"], []).append(r)
    if not batches:
        print(f"No batches found ({label}) in {base}. Start one with: python batch.py", file=out)
        return
    print(f"Batches ({label})", file=out)
    table_rows = []
    for bid, games in sorted(batches.items(), key=lambda kv: min(str(g.get("timestamp")) for g in kv[1])):
        s = compute_stats(games)
        caught = s["imposter_caught"]
        models = sorted({f"{g.get('trio_model')} vs {g.get('imposter_model')}" for g in games})
        table_rows.append([bid, len(games), sum(1 for g in games if g.get("roles_swapped")),
                           caught["caught"], caught["decisive_games"], pct(caught["rate"]),
                           s["outcomes"].get("no_decision", 0), s["outcomes"].get("error", 0),
                           money(s["cost_usd"]["total"]), "; ".join(models)])
    table(["batch", "games", "swapped", "caught", "decisive", "catch rate", "no decision", "errors", "cost",
           "models (trio vs imposter)"], table_rows, out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Summarise finished Agents Among Us games.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--include-mock", action="store_true", help="include mock games")
    g.add_argument("--mock-only", action="store_true", help="only mock games")
    ap.add_argument("--csv", metavar="PATH", help="write one row per game to this CSV file")
    ap.add_argument("--prompt-version", metavar="X",
                    help='only games played with this prompt version ("legacy" = before prompt versions)')
    ap.add_argument("--batch", metavar="BATCH_ID", help="only games from this batch")
    ap.add_argument("--list-batches", action="store_true", help="list batches (one line each) and exit")
    ap.add_argument("--log-dir", default="logs", help="log folder (default: logs in the repo folder)")
    args = ap.parse_args(argv)

    base = resolve_log_dir(args.log_dir)
    real = [] if args.mock_only else read_summary(base / "summary.jsonl")
    mock = read_summary(base / "mock" / "summary.jsonl") if (args.include_mock or args.mock_only) else []
    rows = real + mock
    label = "mock games" if args.mock_only else ("real + mock games" if args.include_mock else "real games")
    if args.list_batches:
        print_batches(rows, label, base)
        return 0
    if args.batch:
        total = len(rows)
        rows = [r for r in rows if r.get("batch_id") == args.batch]
        label += f", batch {args.batch}"
        if total and not rows:
            ids = sorted({r["batch_id"] for r in real + mock if r.get("batch_id")})
            print(f"No games from batch {args.batch}." + (f" Batches found: {', '.join(ids)}" if ids else
                                                           " No batches found (see --list-batches)."))
            return 0
    if args.prompt_version:
        total = len(rows)
        rows = [r for r in rows if prompt_version_of(r) == args.prompt_version]
        label += f", prompt version {args.prompt_version}"
        if total and not rows:
            versions = sorted({prompt_version_of(r) for r in real + mock})
            print(f"No games with prompt version {args.prompt_version}. Versions found: {', '.join(versions)}")
            return 0

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
