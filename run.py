#!/usr/bin/env python3
"""Start Agents Among Us: a local web page where LLMs play a 4-player imposter game.

    python run.py              # real models via OpenRouter (needs OPENROUTER_API_KEY)
    python run.py --mock       # fake models: no API key, no cost
    python run.py --port 9000 --no-browser --config my_config.toml
"""
import sys

if sys.version_info < (3, 11):
    sys.stderr.write(
        "\nAgents Among Us needs Python 3.11 or newer, but this is Python %d.%d.\n"
        "Install a newer Python from https://www.python.org/downloads/ and run it with that,\n"
        "e.g.  python3.12 run.py\n\n" % sys.version_info[:2]
    )
    sys.exit(1)

import argparse
import errno
import threading
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from imposter_game.server import GameController, make_server  # noqa: E402


def _box(lines, char="="):
    width = max(len(line) for line in lines) + 4
    bar = "  " + char * width
    return "\n".join([bar] + ["    " + line for line in lines] + [bar])


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Agents Among Us - watch LLMs play a social-deduction imposter game in your browser.")
    p.add_argument("--mock", action="store_true",
                   help="use fake models (no API key needed, costs nothing)")
    p.add_argument("--port", type=int, default=8765, help="port for the local web page (default 8765)")
    p.add_argument("--config", default=str(HERE / "config.toml"),
                   help="path to the config file (default: config.toml next to run.py)")
    p.add_argument("--no-browser", action="store_true", help="don't open the web browser automatically")
    p.add_argument("--log-dir", default=str(HERE / "logs"),
                   help="where game logs are written (default: logs/ next to run.py)")
    p.add_argument("--prompts-dir", default=None,
                   help="where edited prompt templates are saved (default: prompts/ next to run.py)")
    p.add_argument("--verbose", action="store_true", help="log every HTTP request to the terminal")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    config_path = Path(args.config).expanduser()
    if not config_path.is_absolute() and not config_path.exists() and (HERE / config_path).exists():
        config_path = HERE / config_path

    controller = GameController(config_path, mock=args.mock, log_dir=Path(args.log_dir).expanduser().resolve(),
                                prompts_dir=Path(args.prompts_dir).expanduser().resolve() if args.prompts_dir else None)
    controller.reload_config()  # surface config / API-key problems right away

    try:
        server = make_server(controller, "127.0.0.1", args.port, verbose=args.verbose)
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1), 48, 98, 10048):
            print(f"\nPort {args.port} is already in use - maybe Agents Among Us is already running "
                  f"in another terminal window?\nEither open http://127.0.0.1:{args.port}/ in your browser, "
                  f"or start on another port:\n\n    python run.py --port {args.port + 1}\n", file=sys.stderr)
        else:
            print(f"\nCould not start the web server on port {args.port}: {exc}\n"
                  f"Try another port, e.g.  python run.py --port {args.port + 1}\n", file=sys.stderr)
        return 1

    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    mode = "MOCK - fake models, no API calls, $0" if args.mock else "REAL - models via OpenRouter (costs money)"
    print()
    print(_box([
        "Agents Among Us is running!",
        "",
        f"Open:    {url}",
        f"Mode:    {mode}",
        f"Config:  {controller.config_path}",
        f"Logs:    {controller.log_dir}",
        "",
        "Press Ctrl+C to stop.",
    ]))
    if controller.config_error:
        print()
        print("  ! Heads up - the page will show this too:")
        for line in controller.config_error.splitlines():
            print("  ! " + line)
    print(flush=True)

    if not args.no_browser:
        def _open():
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Timer(0.6, _open).start()

    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        unfinished = controller.state in ("running", "pausing", "paused")
        print("\nStopping..." + (" (the unfinished game is saved to the logs as 'aborted')" if unfinished else ""))
    finally:
        controller.shutdown()
        server.server_close()
    print("Bye!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
