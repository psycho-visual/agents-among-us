"""Local web server for Agents Among Us.

Serves the single-page GUI from ``static/``, a small JSON API, and a
Server-Sent Events stream of game events. Everything is Python stdlib.

Endpoints
---------
GET  /                 -> static/index.html
GET  /static/<file>    -> static files
GET  /api/events       -> Server-Sent Events: replay of the current game, a `status` event, then live events
GET  /api/state        -> current run state (idle/running/pausing/paused/finished), next step, errors
GET  /api/config       -> summary of the loaded config (never the API key)
GET  /api/stats        -> aggregate stats over logs/summary.jsonl (via imposter_game.stats)
GET  /api/log          -> paths of the current game's log files
POST /api/new_game     -> reload config.toml from disk and create a new (not started) game
POST /api/next_round   -> play until the end of the current round / vote (in a background thread)
POST /api/run_all      -> play until the game is over (in a background thread)
POST /api/pause        -> pause after the current turn finishes
GET  /api/prompts          -> editable prompt templates + current prompt version
GET  /api/prompts/preview  -> prompts rendered with the current config.toml and sample data
POST /api/prompts/validate {id, text} -> {errors, warnings}
POST /api/prompts/save     {id, text} -> {ok, errors, warnings}   (writes prompts/<id>.txt)
POST /api/prompts/reset    {id}       -> back to the default text  (deletes prompts/<id>.txt)
Prompt edits apply to the next New game, never to a game in progress.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import os
import queue
import sys
import threading
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}

NO_KEY_MESSAGE = (
    "No OpenRouter API key found. Create a file named .env next to run.py containing "
    "OPENROUTER_API_KEY=sk-or-... (or set the OPENROUTER_API_KEY environment variable), "
    "then click \"New game\". To try it for free with fake models, restart with: python run.py --mock"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _jsonable(obj: Any) -> Any:
    """Make an arbitrary engine object JSON-safe (str dict keys, dataclasses, paths, tuples...)."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        obj = dataclasses.asdict(obj)
    if isinstance(obj, dict):
        return {str(k) if not isinstance(k, (tuple, list)) else " vs ".join(map(str, k)): _jsonable(v)
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in obj]
    if obj is None or isinstance(obj, (bool, int, float, str)):
        if isinstance(obj, float) and obj != obj:  # NaN is not valid JSON
            return None
        return obj
    return str(obj)


def _dumps(obj: Any) -> str:
    return json.dumps(_jsonable(obj), ensure_ascii=False)


def _redact_secrets(obj: Any) -> Any:
    """Drop anything that looks like a secret from a config summary."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if "key" in lk and "api" in lk or "secret" in lk or "token" == lk or "password" in lk:
                continue
            out[k] = _redact_secrets(v)
        return out
    if isinstance(obj, list):
        return [_redact_secrets(v) for v in obj]
    return obj


def _call_with_supported_kwargs(fn: Callable, *args, **kwargs):
    """Call fn, passing only the keyword arguments its signature accepts."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn(*args, **kwargs)
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(*args, **kwargs)
    return fn(*args, **{k: v for k, v in kwargs.items() if k in params})


def _load_engine() -> SimpleNamespace:
    """Import the game engine lazily so the server module imports even if the engine is broken."""
    from imposter_game import config as config_mod
    from imposter_game import game as game_mod
    from imposter_game import openrouter as or_mod
    from imposter_game import stats as stats_mod

    try:
        from imposter_game import prompts as prompts_mod
    except Exception:  # an older engine without editable prompts
        prompts_mod = None

    return SimpleNamespace(
        config=config_mod,
        prompts=prompts_mod,
        load_config=config_mod.load_config,
        Game=game_mod.Game,
        OpenRouterClient=or_mod.OpenRouterClient,
        MockClient=or_mod.MockClient,
        compute_stats=stats_mod.compute_stats,
    )


class PromptError(Exception):
    """A prompt-editor request that can't be served; carries the HTTP status to use."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class GameController:
    """Owns the current Game, the runner thread and the SSE subscribers. Thread-safe."""

    def __init__(
        self,
        config_path: str | os.PathLike,
        mock: bool = False,
        log_dir: str | os.PathLike | None = None,
        mock_options: dict | None = None,
        seed: int | None = None,
        engine: Any = None,
        keepalive_seconds: float = 15.0,
        prompts_dir: str | os.PathLike | None = None,
    ):
        self.config_path = Path(config_path).resolve()
        self.mock = bool(mock)
        self.log_dir = Path(log_dir).resolve() if log_dir else ROOT / "logs"
        self.mock_options = dict(mock_options or {})
        self.seed = seed
        self.keepalive_seconds = keepalive_seconds
        # Where user prompt overrides live (None = the engine's default, prompts/ in the repo root).
        self.prompts_dir = Path(prompts_dir).resolve() if prompts_dir else None
        self._engine = engine
        self.engine_error: str | None = None

        self._lock = threading.RLock()           # guards game / state / runner
        self._clients_lock = threading.Lock()    # guards subscribers / history / generation
        self._clients: list[queue.Queue] = []
        self._history: list[dict] = []
        self._generation = 0
        self._pause = threading.Event()
        self._mode = "round"
        self._runner: threading.Thread | None = None

        self.cfg: Any = None
        self.config_error: str | None = None
        self.api_key_present = False
        self.game: Any = None
        self.state = "idle"
        self._game_over: dict | None = None

    # ------------------------------------------------------------------ engine / config
    @property
    def engine(self) -> SimpleNamespace:
        if self._engine is None:
            self._engine = _load_engine()
        return self._engine

    def _find_api_key(self, cfg: Any) -> str | None:
        for holder in (cfg, getattr(cfg, "api", None)):
            if holder is None:
                continue
            for attr in ("api_key", "openrouter_api_key", "key"):
                val = getattr(holder, attr, None)
                if isinstance(val, str) and val.strip():
                    return val.strip()
        cfg_mod = getattr(self.engine, "config", None)
        for fname in ("load_api_key", "read_api_key", "get_api_key", "find_api_key"):
            fn = getattr(cfg_mod, fname, None)
            if callable(fn):
                try:
                    try:
                        val = fn(self.config_path.parent)
                    except TypeError:
                        val = fn()
                except Exception:
                    val = None
                if isinstance(val, str) and val.strip():
                    return val.strip()
        val = os.environ.get("OPENROUTER_API_KEY", "").strip()
        return val or None

    def reload_config(self) -> bool:
        """(Re)load the config file from disk. Returns True on success; sets self.config_error otherwise."""
        try:
            engine = self.engine
        except Exception as exc:  # engine missing or has a syntax error
            self.engine_error = f"Could not load the game engine: {exc}"
            self.config_error = self.engine_error
            return False
        if not self.config_path.exists():
            self.config_error = (f"Couldn't find the config file at {self.config_path}. "
                                 "Use --config to point at your config.toml.")
            return False
        try:
            cfg = engine.load_config(self.config_path)
        except Exception as exc:
            msg = str(exc).strip() or repr(exc)
            if type(exc).__name__ != "ConfigError" and self.config_path.name not in msg:
                msg = f"Problem in {self.config_path.name}: {msg}"
            self.config_error = msg
            return False
        self.cfg = cfg
        key = self._find_api_key(cfg)
        self.api_key_present = bool(key)
        if not self.mock and not key:
            msg = NO_KEY_MESSAGE
            require = getattr(getattr(engine, "config", None), "require_api_key", None)
            if callable(require):
                try:
                    require(cfg)
                except Exception as exc:
                    msg = str(exc).strip() or msg
            self.config_error = msg
            return False
        self.config_error = None
        return True

    def _make_client(self, cfg: Any):
        engine = self.engine
        if self.mock:
            kwargs = dict(self.mock_options)
            if self.seed is not None:
                kwargs.setdefault("seed", self.seed)
            return _call_with_supported_kwargs(engine.MockClient, **kwargs)
        key = self._find_api_key(cfg)
        api_cfg = getattr(cfg, "api", None)
        return engine.OpenRouterClient(key, api_cfg)

    def config_summary(self) -> dict:
        if self.cfg is None and self.config_error is None:
            with self._lock:
                self.reload_config()
        cfg_dict: Any = None
        if self.cfg is not None:
            try:
                summary = getattr(self.cfg, "summary", None)
                raw = summary() if callable(summary) else self.cfg
                cfg_dict = _redact_secrets(_jsonable(raw))
            except Exception as exc:
                cfg_dict = {"error": f"could not summarise config: {exc}"}
        return {
            "config_path": str(self.config_path),
            "mock": self.mock,
            "api_key_present": self.api_key_present,
            "config_error": self.config_error,
            "log_dir": str(self.log_dir),
            "config": cfg_dict,
        }

    # ------------------------------------------------------------------ SSE plumbing
    def subscribe(self) -> tuple[queue.Queue, list[dict]]:
        q: queue.Queue = queue.Queue()
        with self._clients_lock:
            self._clients.append(q)
            backlog = list(self._history)
        return q, backlog

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._clients_lock:
            if q in self._clients:
                self._clients.remove(q)

    @property
    def num_clients(self) -> int:
        with self._clients_lock:
            return len(self._clients)

    def _publish(self, event: dict, store: bool, generation: int | None = None) -> None:
        with self._clients_lock:
            if generation is not None and generation != self._generation:
                return  # event from a game that has been replaced
            if store:
                self._history.append(event)
            for q in self._clients:
                q.put(event)

    def _on_engine_event(self, event: dict, generation: int) -> None:
        if not isinstance(event, dict):
            return
        if event.get("type") == "game_over":
            self._game_over = event
        self._publish(event, store=True, generation=generation)

    def _server_error(self, message: str, generation: int | None = None) -> None:
        game_id = getattr(self.game, "game_id", None)
        self._publish({"type": "error", "seq": None, "ts": _now_iso(), "game_id": game_id,
                       "message": message, "fatal": True, "player": None, "source": "server"},
                      store=True, generation=generation)

    def status(self) -> dict:
        with self._lock:
            game = self.game
            label = ""
            finished = False
            if game is not None:
                try:
                    finished = bool(game.is_finished())
                    label = "" if finished else (game.next_label() or "")
                except Exception:
                    label = ""
            return {
                "type": "status",
                "seq": None,
                "ts": _now_iso(),
                "game_id": getattr(game, "game_id", None),
                "state": self.state,
                "next_label": label,
                "mock": self.mock,
                "has_game": game is not None,
                "finished": finished,
                "mode": self._mode,
                "config_error": self.config_error,
            }

    def broadcast_status(self) -> None:
        self._publish(self.status(), store=False)

    def log_info(self) -> dict:
        game = self.game
        game_id = getattr(game, "game_id", None)
        info: dict = {"game_id": game_id, "log_file": None, "transcript_file": None, "finished": False}
        if game is None:
            return info
        go = self._game_over
        if go and go.get("game_id") == game_id:
            info["log_file"] = go.get("log_file")
            info["transcript_file"] = go.get("transcript_file")
            info["finished"] = True
        logger = getattr(game, "logger", None)
        for key, attrs in (("log_file", ("log_file", "json_path", "log_path")),
                           ("transcript_file", ("transcript_file", "md_path", "transcript_path"))):
            if info[key]:
                continue
            for holder in (game, logger):
                for attr in attrs:
                    val = getattr(holder, attr, None) if holder is not None else None
                    if val:
                        info[key] = str(val)
                        break
                if info[key]:
                    break
        return info

    # ------------------------------------------------------------------ commands
    def new_game(self) -> tuple[bool, str]:
        with self._lock:
            if self.state in ("running", "pausing"):
                return False, "A game is running. Pause it (and wait for the current turn to finish) first."
            if not self.reload_config():
                msg = self.config_error or "Could not load the config."
                ok, result = False, msg
            else:
                with self._clients_lock:
                    self._generation += 1
                    generation = self._generation
                    self._history = []
                self._game_over = None
                try:
                    client = self._make_client(self.cfg)
                    game = _call_with_supported_kwargs(
                        self.engine.Game, self.cfg, client,
                        lambda ev, g=generation: self._on_engine_event(ev, g),
                        log_dir=self.log_dir, mock=self.mock, seed=self.seed,
                        **({"prompts_dir": self.prompts_dir} if self.prompts_dir else {}),
                    )
                except Exception as exc:
                    traceback.print_exc()
                    self.config_error = f"Could not create a new game: {exc}"
                    self.game = None
                    self.state = "idle"
                    ok, result = False, self.config_error
                else:
                    self.game = game
                    self.state = "finished" if game.is_finished() else "idle"
                    self._mode = "round"
                    ok, result = True, f"New game {getattr(game, 'game_id', '')} ready."
        self.broadcast_status()
        return ok, result

    def start(self, mode: str) -> tuple[bool, str]:
        assert mode in ("round", "all")
        with self._lock:
            game = self.game
            if game is None:
                return False, self.config_error or "There is no game yet. Click \"New game\"."
            if self.state == "running":
                if mode == "all" and self._mode != "all":
                    self._mode = "all"
                    result = (True, "Will keep going until the game is over.")
                else:
                    return False, "Already running."
            elif self.state == "pausing":
                return False, "Pausing - wait for the current turn to finish."
            elif game.is_finished():
                self.state = "finished"
                return False, "This game is over. Click \"New game\" to play again."
            else:
                self._mode = mode
                self._pause.clear()
                self.state = "running"
                generation = self._generation
                self._runner = threading.Thread(target=self._run_loop, args=(game, generation),
                                                name="game-runner", daemon=True)
                self._runner.start()
                result = (True, "Started.")
        self.broadcast_status()
        return result

    def pause(self) -> tuple[bool, str]:
        with self._lock:
            if self.state == "running":
                self.state = "pausing"
                self._pause.set()
                result = (True, "Pausing after the current turn.")
            elif self.state == "pausing":
                return True, "Already pausing."
            else:
                return False, "Nothing is running."
        self.broadcast_status()
        return result

    def _run_loop(self, game: Any, generation: int) -> None:
        try:
            while not self._pause.is_set():
                try:
                    res = game.step() or {}
                except Exception as exc:  # the engine should never raise, but be safe
                    traceback.print_exc()
                    self._server_error(f"The game crashed: {exc!r}", generation)
                    try:
                        game.abort(f"Unexpected error: {exc!r}")
                    except Exception:
                        traceback.print_exc()
                    break
                if bool(res.get("finished")) or game.is_finished():
                    break
                self.broadcast_status()
                if res.get("boundary") and self._mode == "round":
                    break
        finally:
            with self._lock:
                if self.game is game:
                    try:
                        finished = bool(game.is_finished())
                    except Exception:
                        finished = True
                    self.state = "finished" if finished else "paused"
                    self._runner = None
                    self._pause.clear()
            self.broadcast_status()

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until the runner thread exits (used by tests and shutdown)."""
        t = self._runner
        if t is not None:
            t.join(timeout)
            return not t.is_alive()
        return True

    def shutdown(self) -> None:
        """Called on Ctrl+C: stop streams and mark an unfinished, started game as aborted."""
        with self._lock:
            game = self.game
            started = self.state in ("running", "pausing", "paused")
            self._pause.set()
        if game is not None and started:
            try:
                if not game.is_finished():
                    game.abort("Stopped: the server was shut down mid-game.")
            except Exception:
                traceback.print_exc()
        with self._clients_lock:
            for q in self._clients:
                q.put(None)

    # ------------------------------------------------------------------ prompt templates
    PROMPTS_NOTE = "Changes apply when you click New game. Each game records its prompt version."

    def _prompts_mod(self):
        mod = getattr(self.engine, "prompts", None)
        if mod is None or not callable(getattr(mod, "list_templates", None)):
            raise PromptError("This version of the game engine has no editable prompts.", 501)
        return mod

    def _pcall(self, name: str, *args):
        """Call a prompts-module function, pointing it at self.prompts_dir when one is configured."""
        fn = getattr(self._prompts_mod(), name)
        if self.prompts_dir is None:
            return fn(*args)
        return _call_with_supported_kwargs(fn, *args, prompts_dir=self.prompts_dir)

    def _check_template_id(self, template_id: Any) -> str:
        if not isinstance(template_id, str) or not template_id:
            raise PromptError("Missing template id.", 400)
        ids = {t.get("id") for t in self._pcall("list_templates")}
        if template_id not in ids:
            raise PromptError(f"Unknown prompt template: {template_id!r}", 404)
        return template_id

    def _current_game_prompt_info(self) -> dict:
        with self._clients_lock:
            for ev in self._history:
                if ev.get("type") == "game_started":
                    cfg = ev.get("config") if isinstance(ev.get("config"), dict) else {}
                    return {"prompt_version": ev.get("prompt_version", cfg.get("prompt_version")),
                            "prompts_modified": ev.get("prompts_modified", cfg.get("prompts_modified"))}
        return {"prompt_version": None, "prompts_modified": None}

    def prompts_list(self) -> dict:
        templates = self._pcall("list_templates")
        version = None
        fn = getattr(self._prompts_mod(), "prompt_version", None)
        if callable(fn):
            for arg in ({t["id"]: t.get("text", "") for t in templates}, templates):
                try:
                    version = fn(arg)
                    break
                except Exception:
                    continue
        return {
            "ok": True,
            "templates": templates,
            "prompt_version": version,
            "modified": any(bool(t.get("modified")) for t in templates),
            "current_game": self._current_game_prompt_info(),
            "note": self.PROMPTS_NOTE,
        }

    def prompts_validate(self, template_id: Any, text: Any) -> dict:
        template_id = self._check_template_id(template_id)
        if not isinstance(text, str):
            raise PromptError("Template text must be a string.", 400)
        res = self._pcall("validate_template", template_id, text) or {}
        return {"ok": True, "errors": list(res.get("errors") or []), "warnings": list(res.get("warnings") or [])}

    def prompts_save(self, template_id: Any, text: Any) -> dict:
        template_id = self._check_template_id(template_id)
        if not isinstance(text, str):
            raise PromptError("Template text must be a string.", 400)
        res = self._pcall("save_template", template_id, text) or {}
        out = {"ok": bool(res.get("ok", not res.get("errors"))), "errors": list(res.get("errors") or []),
               "warnings": list(res.get("warnings") or [])}
        out["message"] = ("Saved. It will be used from the next New game." if out["ok"]
                          else "Not saved: fix the errors first.")
        return out

    def prompts_reset(self, template_id: Any) -> dict:
        template_id = self._check_template_id(template_id)
        res = self._pcall("reset_template", template_id)
        out = dict(res) if isinstance(res, dict) else {}
        out.setdefault("ok", True)
        out["message"] = "Reset to the default text. It will be used from the next New game."
        return out

    def prompts_preview(self) -> dict:
        mod = self._prompts_mod()
        if not callable(getattr(mod, "preview", None)):
            raise PromptError("This version of the game engine cannot preview prompts.", 501)
        try:
            cfg = _call_with_supported_kwargs(self.engine.load_config, self.config_path, read_key=False)
        except Exception as exc:
            raise PromptError(f"Can't preview: {exc}", 409)
        return {"ok": True, "preview": _jsonable(self._pcall("preview", cfg)), "note": self.PROMPTS_NOTE}

    # ------------------------------------------------------------------ stats
    def stats(self) -> dict:
        paths = [self.log_dir / "summary.jsonl", self.log_dir / "mock" / "summary.jsonl"]
        rows: list[dict] = []
        seen: set = set()
        for p in paths:
            if not p.exists():
                continue
            try:
                lines = p.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                if bool(row.get("mock")) != self.mock:
                    continue
                gid = row.get("game_id")
                if gid is not None and gid in seen:
                    continue
                seen.add(gid)
                rows.append(row)
        out: dict = {"ok": True, "mock": self.mock, "num_games": len(rows),
                     "sources": [str(p) for p in paths if p.exists()], "stats": None}
        if not rows:
            return out
        try:
            out["stats"] = _jsonable(self.engine.compute_stats(rows))
        except Exception as exc:
            traceback.print_exc()
            out["ok"] = False
            out["error"] = f"Could not compute stats: {exc}"
        return out


# ---------------------------------------------------------------------- HTTP layer
class _Handler(BaseHTTPRequestHandler):
    server_version = "AgentsAmongUs/1.0"
    protocol_version = "HTTP/1.0"

    @property
    def controller(self) -> GameController:
        return self.server.controller  # type: ignore[attr-defined]

    def log_message(self, format: str, *args) -> None:  # keep the terminal quiet
        if getattr(self.server, "verbose", False):
            super().log_message(format, *args)

    # -- helpers
    def _allowed_hosts(self) -> set[str]:
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}", "127.0.0.1", "localhost"}

    def _host_ok(self) -> bool:
        host = self.headers.get("Host")
        if host and host.lower() not in self._allowed_hosts():
            return False
        origin = self.headers.get("Origin")
        if origin and origin != "null":
            if urlsplit(origin).netloc.lower() not in self._allowed_hosts():
                return False
        return True

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200, cache: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, obj: Any, status: int = 200) -> None:
        self._send_bytes(_dumps(obj).encode("utf-8"), "application/json; charset=utf-8", status)

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json({"ok": False, "message": message}, status)

    # -- routing
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._send_error_json(403, "Forbidden host")
        path = urlsplit(self.path).path
        try:
            if path in ("/", "/index.html"):
                return self._serve_static("index.html")
            if path.startswith("/static/"):
                return self._serve_static(path[len("/static/"):])
            if path == "/favicon.ico":
                return self._send_bytes(b"", "image/x-icon", 204)
            if path == "/api/events":
                return self._serve_events()
            if path == "/api/state":
                return self._send_json(self.controller.status())
            if path == "/api/config":
                return self._send_json(self.controller.config_summary())
            if path == "/api/stats":
                return self._send_json(self.controller.stats())
            if path == "/api/log":
                return self._send_json(self.controller.log_info())
            if path == "/api/prompts":
                return self._prompt_action(self.controller.prompts_list)
            if path == "/api/prompts/preview":
                return self._prompt_action(self.controller.prompts_preview)
            return self._send_error_json(404, "Not found")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_POST(self) -> None:
        if not self._host_ok():
            return self._send_error_json(403, "Forbidden origin")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 2_000_000:
            return self._send_error_json(413, "Request too large")
        raw = self.rfile.read(length) if length > 0 else b""
        path = urlsplit(self.path).path
        c = self.controller
        if path.startswith("/api/prompts/"):
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                return self._send_error_json(400, "Request body must be JSON")
            if not isinstance(body, dict):
                return self._send_error_json(400, "Request body must be a JSON object")
            prompt_actions = {
                "/api/prompts/validate": lambda: c.prompts_validate(body.get("id"), body.get("text")),
                "/api/prompts/save": lambda: c.prompts_save(body.get("id"), body.get("text")),
                "/api/prompts/reset": lambda: c.prompts_reset(body.get("id")),
            }
            action = prompt_actions.get(path)
            if action is None:
                return self._send_error_json(404, "Not found")
            return self._prompt_action(action)
        actions = {
            "/api/new_game": c.new_game,
            "/api/next_round": lambda: c.start("round"),
            "/api/run_all": lambda: c.start("all"),
            "/api/pause": c.pause,
        }
        action = actions.get(path)
        if action is None:
            return self._send_error_json(404, "Not found")
        try:
            ok, message = action()
        except Exception as exc:
            traceback.print_exc()
            ok, message = False, f"Server error: {exc}"
        status_code = 200 if ok else 409
        try:
            self._send_json({"ok": ok, "message": message, "status": c.status()}, status_code)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _prompt_action(self, action: Callable[[], dict]) -> None:
        try:
            result = action()
            status = 200
        except PromptError as exc:
            result, status = {"ok": False, "message": str(exc)}, exc.status
        except Exception as exc:
            traceback.print_exc()
            result, status = {"ok": False, "message": f"Prompt templates error: {exc}"}, 500
        try:
            self._send_json(result, status)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    # -- static files
    def _serve_static(self, rel: str) -> None:
        rel = unquote(rel)
        if "\x00" in rel or "\\" in rel:
            return self._send_error_json(404, "Not found")
        base = STATIC_DIR.resolve()
        try:
            target = (base / rel).resolve()
            target.relative_to(base)
        except (ValueError, OSError):
            return self._send_error_json(404, "Not found")
        if not target.is_file():
            return self._send_error_json(404, "Not found")
        ctype = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        self._send_bytes(target.read_bytes(), ctype, cache=True)

    # -- server-sent events
    def _sse_send(self, event: dict) -> None:
        payload = "data: " + _dumps(event).replace("\n", "\\n") + "\n\n"
        self.wfile.write(payload.encode("utf-8"))
        self.wfile.flush()

    def _serve_events(self) -> None:
        c = self.controller
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        q, backlog = c.subscribe()
        try:
            self.wfile.write(b"retry: 2000\n: connected\n\n")
            for ev in backlog:
                self._sse_send(ev)
            self._sse_send(c.status())
            while True:
                try:
                    ev = q.get(timeout=c.keepalive_seconds)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                if ev is None:
                    break
                self._sse_send(ev)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError, ValueError):
            pass
        finally:
            c.unsubscribe(q)


class GameServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets two servers share a port; we want "port in use" errors instead.
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, address: tuple[str, int], controller: GameController, verbose: bool = False):
        self.controller = controller
        self.verbose = verbose
        super().__init__(address, _Handler)

    def close_all(self) -> None:
        self.controller.shutdown()
        self.shutdown()
        self.server_close()


def make_server(controller: GameController, host: str = "127.0.0.1", port: int = 8765,
                verbose: bool = False) -> GameServer:
    """Create (bind) the server. Raises OSError if the port is unavailable."""
    return GameServer((host, port), controller, verbose=verbose)
