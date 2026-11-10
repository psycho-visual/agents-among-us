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
GET  /api/settings            -> settings schema + current values of config.toml
POST /api/settings/validate   {values} -> {errors: {id: msg}, warnings: {id: msg}}
POST /api/settings/save       {values, start_new_game} -> writes config.toml (comments kept, backup made)
GET  /api/models              -> OpenRouter's public model list (cached 1 hour), for the model picker
GET  /api/batch               -> current/last batch: progress, defaults from [batch], plan text
POST /api/batch/plan  {options} -> plain-English plan for these options (+ validation error)
POST /api/batch/start {options} -> run many games in a row (one at a time) in the background
POST /api/batch/stop          -> finish the current game, start no more
POST /api/batch/abort         -> abort the current game now
GET  /api/credit              -> OpenRouter credit left (null in mock mode or when unknown)
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import os
import queue
import sys
import threading
import time
import traceback
import urllib.request
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
    try:
        from imposter_game import batch as batch_mod
    except Exception:  # an older engine without batch mode
        batch_mod = None

    return SimpleNamespace(
        config=config_mod,
        prompts=prompts_mod,
        batch=batch_mod,
        openrouter=or_mod,
        load_config=config_mod.load_config,
        Game=game_mod.Game,
        OpenRouterClient=or_mod.OpenRouterClient,
        MockClient=or_mod.MockClient,
        compute_stats=stats_mod.compute_stats,
    )


OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
MODELS_CACHE_SECONDS = 3600
SETTINGS_NOTE = "Saved to config.toml. Applies when you click New game."


def fetch_openrouter_models(timeout: float = 20.0) -> dict:
    """Download OpenRouter's public model list (no API key needed)."""
    req = urllib.request.Request(OPENROUTER_MODELS_URL, headers={
        "Accept": "application/json", "User-Agent": "AgentsAmongUs/1.0", "X-Title": "Agents Among Us"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _per_million(price: Any) -> float | None:
    try:
        v = float(price)
    except (TypeError, ValueError):
        return None
    return None if v < 0 else round(v * 1_000_000, 6)


def reduce_models(payload: Any) -> list[dict]:
    """OpenRouter /models response -> the short list the model picker needs."""
    data = payload.get("data") if isinstance(payload, dict) else payload
    out = []
    for m in data or []:
        if not isinstance(m, dict) or not isinstance(m.get("id"), str):
            continue
        arch = m.get("architecture") if isinstance(m.get("architecture"), dict) else {}
        outputs = arch.get("output_modalities")
        if isinstance(outputs, list) and outputs and "text" not in outputs:
            continue  # image/audio generators can't play
        pricing = m.get("pricing") if isinstance(m.get("pricing"), dict) else {}
        reasoning = m.get("reasoning") if isinstance(m.get("reasoning"), dict) else {}
        efforts = reasoning.get("supported_efforts")
        out.append({
            "id": m["id"],
            "name": str(m.get("name") or m["id"]),
            "prompt_per_m": _per_million(pricing.get("prompt")),
            "completion_per_m": _per_million(pricing.get("completion")),
            "context_length": m.get("context_length") if isinstance(m.get("context_length"), int) else None,
            "supported_efforts": [str(e) for e in efforts] if isinstance(efforts, list) else None,
            "default_effort": reasoning.get("default_effort") if isinstance(reasoning.get("default_effort"), str) else None,
        })
    out.sort(key=lambda x: (x["name"].lower(), x["id"]))
    return out


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
        models_fetcher: Callable[[], Any] | None = None,
    ):
        self.config_path = Path(config_path).resolve()
        self.mock = bool(mock)
        self.log_dir = Path(log_dir).resolve() if log_dir else ROOT / "logs"
        self.mock_options = dict(mock_options or {})
        self.seed = seed
        self.keepalive_seconds = keepalive_seconds
        # Where user prompt overrides live (None = the engine's default, prompts/ in the repo root).
        self.prompts_dir = Path(prompts_dir).resolve() if prompts_dir else None
        # Model list for the settings picker; tests inject a fake fetcher (never the network).
        self.models_fetcher = models_fetcher or fetch_openrouter_models
        self._models_lock = threading.Lock()
        self._models_cache: tuple[float, list[dict]] | None = None
        # Batch mode
        self.batch: Any = None                     # the current/last BatchRunner
        self._batch_thread: threading.Thread | None = None
        self._batch_history: list[dict] = []       # batch_* events of the current/last batch (for SSE replay)
        self._batch_game_id: str | None = None     # game currently shown from the batch
        self._credit_cache: tuple[float, dict] | None = None
        self.credit_checker: Callable[[], float | None] | None = None  # tests inject a fake
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
            backlog = list(self._batch_history) + list(self._history)
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
                "batch": self._batch_status(),
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
            if self.batch_active:
                return False, "A batch is running. Stop it first (in the batch panel)."
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
            if self.batch_active:
                return False, "A batch is running. Stop it first (in the batch panel)."
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
            if self.batch_active:
                return False, "A batch is running. Use “Stop after this game” or “Abort now” in the batch panel."
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
        """Block until the runner thread (and any batch thread) exits (used by tests and shutdown)."""
        for t in (self._runner, self._batch_thread):
            if t is not None:
                t.join(timeout)
                if t.is_alive():
                    return False
        return True

    def shutdown(self) -> None:
        """Called on Ctrl+C: stop streams and mark an unfinished, started game as aborted."""
        if self.batch_active:
            try:
                self.batch.abort()
            except Exception:
                traceback.print_exc()
            if self._batch_thread is not None:
                self._batch_thread.join(10)
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

    # ------------------------------------------------------------------ batch mode
    BATCH_OPTION_KEYS = ("games", "budget_usd", "min_credit_usd", "alternate_roles", "stop_after_errors")

    @property
    def batch_active(self) -> bool:
        t = self._batch_thread
        return t is not None and t.is_alive()

    def _batch_status(self) -> dict | None:
        r = self.batch
        if r is None:
            return None
        return {"active": self.batch_active, "batch_id": getattr(r, "batch_id", None),
                "stopping": bool(getattr(r, "_stop_requested", False)),
                "game_id": self._batch_game_id}

    def _batch_mod(self):
        mod = getattr(self.engine, "batch", None)
        if mod is None or not hasattr(mod, "BatchRunner"):
            raise PromptError("This version of the game engine has no batch mode.", 501)
        return mod

    def _batch_options(self, overrides: Any):
        """[batch] defaults from config.toml with the dialog's values on top (empty = off). GUI: 1 game at a time."""
        mod = self._batch_mod()
        if overrides is None:
            overrides = {}
        if not isinstance(overrides, dict):
            raise PromptError("'options' must be an object.", 400)
        base = dataclasses.asdict(getattr(self.cfg, "batch", None) or mod.BatchOptions())
        base = {k: v for k, v in base.items() if k in {f.name for f in dataclasses.fields(mod.BatchOptions)}}

        def num(key: str, value: Any, kind: type, nullable: bool):
            if value is None or (isinstance(value, str) and not value.strip()):
                return None if nullable else 0
            try:
                n = float(value)
            except (TypeError, ValueError):
                raise PromptError(f"{key.replace('_', ' ')} must be a number (got {value!r}).", 400)
            if kind is int:
                if n != int(n):
                    raise PromptError(f"{key.replace('_', ' ')} must be a whole number.", 400)
                return int(n)
            return n

        for key in self.BATCH_OPTION_KEYS:
            if key not in overrides:
                continue
            v = overrides[key]
            if key == "alternate_roles":
                base[key] = bool(v)
            elif key in ("games", "stop_after_errors"):
                base[key] = num(key, v, int, False)
            else:
                base[key] = num(key, v, float, True)
        base["parallel"] = 1  # the browser shows one game at a time
        opts = mod.BatchOptions(**base)
        try:
            opts.validate()
        except Exception as exc:
            raise PromptError(str(exc), 400)
        return opts

    def _ensure_config(self) -> None:
        if self.cfg is None or self.config_error:
            with self._lock:
                self.reload_config()

    def batch_plan(self, overrides: Any) -> dict:
        self._ensure_config()
        if self.cfg is None:
            return {"ok": False, "plan": "", "message": self.config_error or "No config loaded."}
        try:
            opts = self._batch_options(overrides)
        except PromptError as exc:
            if exc.status == 501:
                raise
            return {"ok": False, "plan": "", "message": str(exc)}
        plan_fn = getattr(self._batch_mod(), "plan_text", None)
        plan = plan_fn(opts, self.cfg, mock=self.mock) if callable(plan_fn) else ""
        return {"ok": True, "plan": plan, "options": dataclasses.asdict(opts), "message": None}

    def batch_info(self) -> dict:
        self._ensure_config()
        mod = self._batch_mod()
        r = self.batch
        defaults = None
        if self.cfg is not None:
            defaults = dataclasses.asdict(getattr(self.cfg, "batch", None) or mod.BatchOptions())
        plan = self.batch_plan({}) if self.cfg is not None else {"plan": "", "message": self.config_error}
        finished = next((e for e in reversed(self._batch_history) if e.get("type") == "batch_finished"), None)
        return {
            "ok": True,
            "active": self.batch_active,
            "batch_id": getattr(r, "batch_id", None),
            "progress": r.state if r is not None else None,
            "finished": finished,
            "defaults": defaults,
            "plan": plan.get("plan"),
            "plan_error": plan.get("message"),
            "mock": self.mock,
            "running": self.running or self.batch_active,
            "config_error": self.config_error,
            "stop_reasons": dict(getattr(mod, "STOP_REASON_TEXT", {})),
        }

    def _on_batch_event(self, event: dict, generation: int) -> None:
        if not isinstance(event, dict):
            return
        etype = str(event.get("type") or "")
        if etype.startswith("batch_"):
            with self._clients_lock:
                if generation != self._generation:
                    return
                self._batch_history.append(event)
                for q in self._clients:
                    q.put(event)
            if etype in ("batch_game_started", "batch_finished", "batch_progress"):
                self.broadcast_status()
            return
        gid = event.get("game_id")
        new_game = False
        with self._clients_lock:
            if generation != self._generation:
                return
            if gid and gid != self._batch_game_id:
                # A new game of the batch: the live view (and SSE replay) now follows it.
                self._batch_game_id = gid
                self._history = []
                new_game = True
        if new_game:
            running = dict(getattr(self.batch, "_running", {}) or {})
            for slot in running.values():
                g = slot.get("game") if isinstance(slot, dict) else None
                if g is not None and getattr(g, "game_id", None) == gid:
                    with self._lock:
                        self.game = g
        if etype == "game_over":
            self._game_over = event
        self._publish(event, store=True, generation=generation)
        if new_game or etype in ("phase", "game_over", "vote_result") or (
                etype == "thinking" and event.get("action") == "question"):
            self.broadcast_status()  # keeps "Batch: game 4 of 20 — Round 2 · Blue asks" current

    def batch_start(self, overrides: Any) -> dict:
        mod = self._batch_mod()
        with self._lock:
            if self.batch_active:
                raise PromptError("A batch is already running.", 409)
            if self.state in ("running", "pausing"):
                raise PromptError("A game is running. Pause it (or let it finish) before starting a batch.", 409)
            if not self.reload_config():
                raise PromptError(self.config_error or "Could not load the config.", 409)
            opts = self._batch_options(overrides)
            with self._clients_lock:
                self._generation += 1
                generation = self._generation
                self._history = []
                self._batch_history = []
                self._batch_game_id = None
            self._game_over = None
            cfg = self.cfg
            kwargs = {"options": opts, "prompts_dir": self.prompts_dir, "seed": self.seed}
            if self.credit_checker is not None:
                kwargs["credit_checker"] = self.credit_checker
            try:
                runner = _call_with_supported_kwargs(
                    mod.BatchRunner, cfg, lambda: self._make_client(cfg),
                    lambda ev, g=generation: self._on_batch_event(ev, g),
                    self.log_dir, mock=self.mock, **kwargs)
            except Exception as exc:
                traceback.print_exc()
                raise PromptError(f"Could not start the batch: {exc}", 400)
            self.batch = runner
            self.game = None
            self.state = "running"
            self._mode = "batch"
            self._batch_thread = threading.Thread(target=self._batch_loop, args=(runner, generation),
                                                  name="batch-runner", daemon=True)
            self._batch_thread.start()
        self.broadcast_status()
        return {"ok": True, "message": "Batch started.", "batch_id": runner.batch_id, "status": self.status()}

    def _batch_loop(self, runner: Any, generation: int) -> None:
        try:
            runner.run()
        except Exception as exc:
            traceback.print_exc()
            self._server_error(f"The batch stopped because of an error: {exc!r}", generation)
        finally:
            with self._lock:
                if self.batch is runner:
                    game = self.game
                    try:
                        finished = game is not None and bool(game.is_finished())
                    except Exception:
                        finished = True
                    self.state = "finished" if finished else "idle"
                    self._mode = "round"
            # status() reads batch_active from the thread, which is still alive here: publish after it ends.
            threading.Thread(target=self._status_after_batch, args=(threading.current_thread(),), daemon=True).start()

    def _status_after_batch(self, t: threading.Thread) -> None:
        t.join(5)
        self.broadcast_status()

    def batch_stop(self) -> dict:
        if not self.batch_active:
            raise PromptError("No batch is running.", 409)
        self.batch.request_stop()
        self.broadcast_status()
        return {"ok": True, "message": "The batch will stop after the current game.", "status": self.status()}

    def batch_abort(self) -> dict:
        if not self.batch_active:
            raise PromptError("No batch is running.", 409)
        self.batch.abort()
        self.broadcast_status()
        return {"ok": True, "message": "Aborting the batch.", "status": self.status()}

    def credit(self, force: bool = False) -> dict:
        """OpenRouter credit left in USD (None in mock mode or if it can't be found out). Cached for a minute."""
        if self.mock:
            return {"ok": True, "credit_usd": None, "mock": True, "message": "Mock mode: no real credit is used."}
        cached = self._credit_cache
        if cached and not force and time.monotonic() - cached[0] < 60:
            return cached[1]
        checker = self.credit_checker
        if checker is None:
            self._ensure_config()
            key = self._find_api_key(self.cfg) if self.cfg is not None else None
            if not key:
                return {"ok": True, "credit_usd": None, "mock": False, "message": "No API key, so the credit is unknown."}
            cls = getattr(getattr(self.engine, "openrouter", None), "CreditChecker", None)
            if cls is None:
                return {"ok": True, "credit_usd": None, "mock": False, "message": "This engine can't check credit."}
            checker = cls(key, getattr(self.cfg, "api", None))
        try:
            value = checker()
            msg = None if value is not None else "OpenRouter didn't say how much credit is left (e.g. a key without a limit)."
        except Exception as exc:
            value, msg = None, f"Could not check the credit: {exc}"
        res = {"ok": True, "credit_usd": value, "mock": False, "message": msg}
        self._credit_cache = (time.monotonic(), res)
        return res

    # ------------------------------------------------------------------ settings (config.toml editor)
    def _config_fn(self, name: str):
        fn = getattr(getattr(self.engine, "config", None), name, None)
        if not callable(fn):
            raise PromptError("This version of the game engine has no settings editor.", 501)
        return fn

    @property
    def running(self) -> bool:
        return self.state in ("running", "pausing")

    def settings_get(self) -> dict:
        schema = self._config_fn("settings_schema")()
        read = self._config_fn("read_settings")(self.config_path) or {}
        return {
            "ok": True,
            "schema": schema,
            "values": read.get("values") or {},
            "errors": list(read.get("errors") or []),
            "config_path": str(self.config_path),
            "running": self.running,
            "note": SETTINGS_NOTE,
        }

    @staticmethod
    def _check_values(values: Any) -> dict:
        if not isinstance(values, dict):
            raise PromptError("'values' must be an object of {setting id: value}.", 400)
        return values

    def settings_validate(self, values: Any) -> dict:
        res = self._config_fn("validate_settings")(self._check_values(values)) or {}
        return {"ok": True, "errors": dict(res.get("errors") or {}), "warnings": dict(res.get("warnings") or {})}

    def settings_save(self, values: Any, start_new_game: bool = False) -> dict:
        values = self._check_values(values)
        res = self._config_fn("save_settings")(self.config_path, values) or {}
        out = {
            "ok": bool(res.get("ok")),
            "errors": dict(res.get("errors") or {}),
            "warnings": dict(res.get("warnings") or {}),
            "backup": res.get("backup"),
            "changed": res.get("changed"),
            "new_game": None,
        }
        if not out["ok"]:
            out["message"] = res.get("message") or "Not saved: fix the errors first."
            return out
        out["message"] = "Saved to config.toml." + (f" Backup of the old file: {out['backup']}" if out["backup"] else "")
        if start_new_game:
            if self.running:
                out["new_game"] = {"ok": False, "message": "A game is running, so no new game was started. "
                                                            "Pause it, then click New game."}
            else:
                ok, msg = self.new_game()
                out["new_game"] = {"ok": ok, "message": msg}
        else:
            with self._lock:
                if not self.running:
                    self.reload_config()  # refresh config errors (e.g. the API-key banner) right away
            self.broadcast_status()
        out["status"] = self.status()
        return out

    def models(self, force: bool = False) -> dict:
        with self._models_lock:
            cached = self._models_cache
            if cached and not force and time.monotonic() - cached[0] < MODELS_CACHE_SECONDS:
                return {"ok": True, "models": cached[1], "cached": True, "error": None}
            try:
                models = reduce_models(self.models_fetcher())
            except Exception as exc:
                if cached:  # stale is better than nothing
                    return {"ok": True, "models": cached[1], "cached": True,
                            "error": f"Could not refresh the model list ({exc}); showing an older copy."}
                return {"ok": False, "models": [], "cached": False,
                        "error": f"Could not download the model list from OpenRouter ({exc}). "
                                 "You can still type a model id by hand."}
            self._models_cache = (time.monotonic(), models)
            return {"ok": True, "models": models, "cached": False, "error": None}

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
            if path == "/api/settings":
                return self._prompt_action(self.controller.settings_get)
            if path == "/api/models":
                return self._send_json(self.controller.models())
            if path == "/api/batch":
                return self._prompt_action(self.controller.batch_info)
            if path == "/api/credit":
                return self._prompt_action(self.controller.credit)
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
        if path.startswith(("/api/prompts/", "/api/settings/", "/api/batch/")):
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
                "/api/settings/validate": lambda: c.settings_validate(body.get("values")),
                "/api/settings/save": lambda: c.settings_save(body.get("values"), bool(body.get("start_new_game"))),
                "/api/batch/plan": lambda: c.batch_plan(body.get("options")),
                "/api/batch/start": lambda: c.batch_start(body.get("options")),
                "/api/batch/stop": c.batch_stop,
                "/api/batch/abort": c.batch_abort,
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
