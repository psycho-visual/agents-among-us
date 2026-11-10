"""Play a long series of games from the terminal ("batch mode").

    python batch.py                     use the [batch] settings in config.toml
    python batch.py --mock              try it with fake players ($0)
    python batch.py --games 50 --budget 2.50 --alternate-roles
    python batch.py --budget 5 --games 0 --min-credit 3   no game limit; stop at $5 spent or $3 credit left

Flags override [batch] in config.toml. Press Ctrl+C once to stop after the
game(s) in progress, twice to abort immediately. Every game is logged as usual
(logs/games/, logs/summary.jsonl), and the batch itself in logs/batches/.
Afterwards: python analyze.py --list-batches   or   python analyze.py --batch <id>
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from imposter_game.batch import STOP_REASON_TEXT, BatchOptions, BatchRunner, plan_text  # noqa: E402
from imposter_game.config import ConfigError, load_config, require_api_key  # noqa: E402
from imposter_game.openrouter import MockClient, OpenRouterClient  # noqa: E402


def _money(x) -> str:
    return "n/a" if x is None else f"${x:.4f}"


def game_line(ev: dict, max_games: int | None) -> str:
    """One line per finished game."""
    g = ev["game"]
    total = f"/{max_games}" if max_games else ""
    width = len(str(max_games)) if max_games else 3
    if g.get("eliminated"):
        out = f"{g['eliminated']} ({g.get('eliminated_role')}) out"
    else:
        out = "nobody out"
    swapped = " [roles swapped]" if g.get("roles_swapped") else ""
    oc = ev.get("outcomes") or {}
    decisive = oc.get("trio_win", 0) + oc.get("imposter_win", 0)
    caught = (f"caught {oc.get('trio_win', 0)}/{decisive} ({100 * oc.get('trio_win', 0) / decisive:.0f}%)"
              if decisive else "caught -/-")
    return (f"[{g['batch_index']:>{width}}{total}] {str(g.get('outcome')):<22} {out:<26} "
            f"cost {_money(g.get('cost_usd'))}  total {_money(ev.get('spent_usd'))}  {caught}{swapped}")


def summary_text(record: dict) -> str:
    t = record["totals"]
    oc = t.get("outcomes") or {}
    lines = [
        "",
        f"Batch {record['batch_id']} finished: {STOP_REASON_TEXT.get(record['stop_reason'], record['stop_reason'])}.",
        f"  Games played: {t['games_finished']}   spent: {_money(t['spent_usd'])}"
        f"   mean per game: {_money(t.get('mean_cost_usd'))}",
        "  Outcomes: " + (", ".join(f"{k} {v}" for k, v in sorted(oc.items())) or "none"),
    ]
    rate = t.get("imposter_caught_rate")
    if rate is not None:
        lines.append(f"  Imposter caught in {100 * rate:.0f}% of decisive games (random voting: 25%)")
    lines.append(f"  Prompt version: {record.get('prompt_version')}")
    lines.append(f"  Batch record: {record.get('record_file', '')}".rstrip())
    lines.append(f"  More: python analyze.py --batch {record['batch_id']}" + (" --mock-only" if record.get("mock")
                                                                           else ""))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Play many games in a row until a limit is reached.")
    ap.add_argument("--games", type=int, help="maximum number of games (0 = no game limit; needs --budget)")
    ap.add_argument("--budget", type=float, help="stop before spending more than this many US dollars")
    ap.add_argument("--min-credit", type=float, help="stop if OpenRouter credit falls below this many dollars")
    ap.add_argument("--parallel", type=int, help="games to run at the same time (1-8)")
    ap.add_argument("--alternate-roles", action="store_true", default=None,
                    help="swap the trio and imposter models every second game")
    ap.add_argument("--stop-after-errors", type=int, help="stop after this many failed games in a row")
    ap.add_argument("--mock", action="store_true", help="fake players, no API key, $0")
    ap.add_argument("--config", default="config.toml", help="config file (default: config.toml)")
    ap.add_argument("--log-dir", default="logs", help="log folder (default: logs)")
    ap.add_argument("--prompts-dir", default=None, help="prompt overrides folder (default: prompts)")
    ap.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    ap.add_argument("--mock-delay", type=float, default=0.0, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    try:
        cfg = load_config(args.config, read_key=not args.mock)
        if not args.mock:
            require_api_key(cfg)
        options = BatchOptions.from_config(cfg.batch, games=args.games, budget_usd=args.budget,
                                           min_credit_usd=args.min_credit, parallel=args.parallel,
                                           alternate_roles=args.alternate_roles,
                                           stop_after_errors=args.stop_after_errors)
        options.validate()
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    print(plan_text(options, cfg, mock=args.mock))
    if not args.yes:
        try:
            answer = input("Start? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("Not started.")
            return 1

    if args.mock:
        def client_factory():
            return MockClient(delay=args.mock_delay)
    else:
        def client_factory():
            return OpenRouterClient(cfg.api_key, cfg.api)

    max_games = options.games or None

    def on_event(ev: dict) -> None:
        t = ev.get("type")
        if t == "batch_game_finished":
            print(game_line(ev, max_games), flush=True)
        elif t == "batch_warning":
            print(f"Warning: {ev.get('message')}", flush=True)
        elif t == "batch_started":
            print(f"Batch {ev['batch_id']} started (prompt version {ev.get('prompt_version')}). "
                  "Ctrl+C: stop after the current game; twice: abort.", flush=True)

    runner = BatchRunner(cfg, client_factory, on_event, log_dir=args.log_dir, mock=args.mock, options=options,
                         prompts_dir=args.prompts_dir)
    presses = {"n": 0}

    def on_sigint(signum, frame):
        presses["n"] += 1
        if presses["n"] == 1:
            print("\nStopping after the current game(s)... (Ctrl+C again to abort now)", flush=True)
            runner.request_stop()
        else:
            print("\nAborting...", flush=True)
            runner.abort()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, on_sigint)
    record = runner.run()
    record["record_file"] = str(runner.record_path)
    print(summary_text(record))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
