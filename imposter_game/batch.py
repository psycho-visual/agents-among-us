"""Batch mode: play many games in a row until a limit is reached.

    runner = BatchRunner(cfg, client_factory, on_event, log_dir, mock=False, options=BatchOptions(...))
    record = runner.run()          # blocking
    runner.request_stop()          # from another thread: finish the current game(s), start no more
    runner.abort()                 # from another thread: abort the running game(s) now

A batch stops at the first of these (see `_stop_reason_before_start`):
  games_limit  `games` games have been started (0 = no game limit)
  budget       starting another game could push spending over `budget_usd`. The cost of the next game is
               estimated as the most expensive game of this batch so far (before the first game finishes:
               `first_game_estimate_usd`). With several games in parallel, each running game reserves that
               estimate too. A game that has started always finishes.
  credit       the OpenRouter credit left is below `min_credit_usd` (checked before each game; never in mock mode)
  errors       `stop_after_errors` games in a row ended with outcome "error"
  stopped / aborted   the user asked

The config and the prompt templates are read once, when the batch is created, so every game of a batch
uses the same settings. Every game is logged exactly like a normal game, plus batch_id / batch_index /
roles_swapped. A batch record is written to logs/batches/<batch_id>.json (mock: logs/mock/batches/)
after every game.

Events sent to on_event (all carry batch_id): batch_started, batch_progress, batch_game_started,
batch_game_finished, batch_warning, batch_finished — plus every event of every game, forwarded with
batch_id and batch_index added.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import random
import threading
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import prompts
from .config import BatchConfig, ConfigError, GameConfig, validate_batch_limits
from .game import Game
from .logger import _atomic_write, resolve_log_dir

log = logging.getLogger("imposter_game.batch")


@dataclass
class BatchOptions:
    games: int = 20
    budget_usd: float | None = 1.00
    min_credit_usd: float | None = None
    alternate_roles: bool = False
    stop_after_errors: int = 3
    parallel: int = 1
    first_game_estimate_usd: float = 0.10

    @classmethod
    def from_config(cls, batch_cfg: BatchConfig | None = None, **overrides) -> "BatchOptions":
        """[batch] config values, with any non-None overrides (e.g. command-line flags) on top."""
        base = asdict(batch_cfg or BatchConfig())
        base.update({k: v for k, v in overrides.items() if v is not None and k in base})
        return cls(**base)

    def validate(self) -> None:
        """Raise ConfigError if the options are invalid or the batch would be unbounded."""
        validate_batch_limits(self, "Batch:")


def swap_roles(cfg: GameConfig) -> GameConfig:
    """The same config with the [trio] and [imposter] model blocks swapped."""
    return dataclasses.replace(cfg, trio=cfg.imposter, imposter=cfg.trio)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def new_batch_id() -> str:
    return "b" + time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()


class _Forwarder:
    """Holds a game's events until batch_game_started is sent, then forwards them (with batch fields)."""

    def __init__(self, runner: "BatchRunner", index: int):
        self.runner, self.index = runner, index
        self.buffer: list[dict] | None = []
        self.lock = threading.Lock()

    def __call__(self, event: dict) -> None:
        with self.lock:
            if self.buffer is not None:
                self.buffer.append(event)
                return
        self.runner._send({**event, "batch_id": self.runner.batch_id, "batch_index": self.index})

    def release(self) -> None:
        with self.lock:
            pending, self.buffer = self.buffer or [], None
        for ev in pending:
            self.runner._send({**ev, "batch_id": self.runner.batch_id, "batch_index": self.index})


class BatchRunner:
    def __init__(self, cfg: GameConfig, client_factory: Callable[[], object],
                 on_event: Callable[[dict], None] | None, log_dir: str | Path = Path("logs"), mock: bool = False,
                 options: BatchOptions | None = None, credit_checker: Callable[[], float | None] | None = None,
                 prompts_dir: str | Path | None = None, seed: int | None = None):
        self.options = options or BatchOptions.from_config(getattr(cfg, "batch", None))
        self.options.validate()
        self.cfg = cfg
        self.client_factory = client_factory
        self.on_event = on_event
        self.log_dir = log_dir
        self.mock = mock
        self.batch_id = new_batch_id()
        # Templates are loaded ONCE so every game of the batch uses the same prompts.
        self.templates, self.prompt_warnings = prompts.load_templates(prompts_dir)
        self.prompt_version = prompts.prompt_version(self.templates)
        self.prompts_modified = any(self.templates[t] != prompts.default_text(t) for t in prompts.TEMPLATES)
        self.credit_checker = None if mock else credit_checker
        if (not mock and credit_checker is None and self.options.min_credit_usd is not None):
            from .openrouter import CreditChecker
            self.credit_checker = CreditChecker(cfg.api_key, cfg.api)
        self._rng = random.Random(seed)
        base = resolve_log_dir(log_dir)
        self.record_path = (base / "mock" if mock else base) / "batches" / f"{self.batch_id}.json"

        self._lock = threading.RLock()
        self._seq = 0
        self._stop_requested = False
        self._aborted = False
        self.stop_reason: str | None = None
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.games: list[dict] = []                 # finished games, in finish order
        self._running: dict[int, dict] = {}         # batch_index -> {"game": Game|None, "estimate": float}
        self.games_started = 0
        self.spent_usd = 0.0
        self.consecutive_errors = 0
        self.credit_remaining_usd: float | None = None
        self._credit_warned = False
        self.finished = False
        self._progress: dict = {}

    # ------------------------------------------------------------------ control

    def request_stop(self) -> None:
        """Graceful stop: let running games finish, start no new ones."""
        with self._lock:
            self._stop_requested = True
            if self.stop_reason is None:
                self.stop_reason = "stopped"

    def abort(self) -> None:
        """Stop now: abort the running game(s)."""
        with self._lock:
            self._stop_requested = True
            self._aborted = True
            self.stop_reason = "aborted" if self.stop_reason in (None, "stopped") else self.stop_reason
            games = [r["game"] for r in self._running.values() if r.get("game") is not None]
        for g in games:
            try:
                g.abort("Batch aborted")
            except Exception:  # noqa: BLE001
                traceback.print_exc()

    @property
    def state(self) -> dict:
        """Live progress snapshot (same fields as the batch_progress event)."""
        with self._lock:
            return dict(self._progress or self._make_progress())

    # ------------------------------------------------------------------ events

    def _send(self, event: dict) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(event)
        except Exception:  # noqa: BLE001 — a GUI problem must not stop the batch
            traceback.print_exc()

    def _emit(self, type_: str, **fields) -> None:
        with self._lock:
            self._seq += 1
            seq = self._seq
        self._send({"type": type_, "seq": seq, "ts": _now_iso(), "batch_id": self.batch_id, "game_id": None,
                    **fields})

    # ------------------------------------------------------------------ estimates

    def estimated_next_game_usd(self) -> float:
        costs = [g["cost_usd"] for g in self.games if g.get("cost_usd") is not None]
        return max(costs) if costs else float(self.options.first_game_estimate_usd)

    def _make_progress(self) -> dict:
        o = self.options
        outcomes: dict[str, int] = {}
        for g in self.games:
            outcomes[g["outcome"]] = outcomes.get(g["outcome"], 0) + 1
        decisive = outcomes.get("trio_win", 0) + outcomes.get("imposter_win", 0)
        costs = [g["cost_usd"] for g in self.games if g.get("cost_usd") is not None]
        est = self.estimated_next_game_usd()
        left = None
        if o.games:
            left = max(0, o.games - self.games_started)
        if o.budget_usd is not None:
            reserved = est * len(self._running)
            by_budget = int(max(0.0, o.budget_usd - self.spent_usd - reserved) / est + 1e-9) if est > 0 else None
            if by_budget is not None:
                left = by_budget if left is None else min(left, by_budget)
        if self.stop_reason is not None:
            left = 0
        running = sorted(self._running)
        current = None
        if running:
            g = self._running[running[-1]].get("game")
            current = g.game_id if g is not None else None
        next_index = self.games_started + 1
        return {
            "batch_id": self.batch_id,
            "games_started": self.games_started, "games_finished": len(self.games),
            "max_games": o.games or None,
            "spent_usd": round(self.spent_usd, 8), "budget_usd": o.budget_usd,
            "estimated_next_game_usd": round(est, 8), "estimated_games_left": left,
            "outcomes": outcomes,
            "imposter_caught_rate": (outcomes.get("trio_win", 0) / decisive) if decisive else None,
            "mean_cost_usd": (sum(costs) / len(costs)) if costs else None,
            "credit_remaining_usd": self.credit_remaining_usd, "min_credit_usd": o.min_credit_usd,
            "current_game_id": current, "running_games": [self._running[i].get("game_id") for i in running],
            "roles_swapped_next": bool(o.alternate_roles and next_index % 2 == 0),
            "stop_requested": self._stop_requested, "stop_reason": self.stop_reason, "finished": self.finished,
            "mock": self.mock,
        }

    def _progress_event(self) -> None:
        with self._lock:
            self._progress = self._make_progress()
            snapshot = dict(self._progress)
        self._emit("batch_progress", **snapshot)

    # ------------------------------------------------------------------ record

    def _record(self) -> dict:
        with self._lock:
            o = self.options
            return {
                "batch_id": self.batch_id, "mock": self.mock, "started_at": self.started_at,
                "finished_at": self.finished_at, "stop_reason": self.stop_reason, "options": asdict(o),
                "config": self.cfg.summary(), "prompt_version": self.prompt_version,
                "prompts_modified": self.prompts_modified,
                "games": sorted(self.games, key=lambda g: g["batch_index"]),
                "totals": {"games_started": self.games_started, "games_finished": len(self.games),
                           "spent_usd": round(self.spent_usd, 8), **{k: v for k, v in self._make_progress().items()
                                                                     if k in ("outcomes", "imposter_caught_rate",
                                                                              "mean_cost_usd")}},
            }

    def _write_record(self) -> None:
        try:
            _atomic_write(self.record_path, json.dumps(self._record(), indent=1, ensure_ascii=False, default=str))
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    # ------------------------------------------------------------------ limits

    def _check_credit(self) -> float | None:
        if self.credit_checker is None:
            return None
        try:
            value = self.credit_checker()
        except Exception as e:  # noqa: BLE001
            value, info = None, {"error": str(e)}
        else:
            info = getattr(self.credit_checker, "last_info", {}) or {}
        self.credit_remaining_usd = value
        if value is None and not self._credit_warned:
            self._credit_warned = True
            msg = ("Could not find out how much OpenRouter credit is left"
                   + (f" ({info['error']})" if info.get("error") else "")
                   + "; the batch carries on using its other limits.")
            log.warning(msg)
            self._emit("batch_warning", message=msg)
        return value

    def _stop_reason_before_start(self) -> str | None:
        """Why no new game may start now (None = OK to start one)."""
        o = self.options
        if self._aborted:
            return "aborted"
        if self._stop_requested:
            return "stopped"
        if o.games and self.games_started >= o.games:
            return "games_limit"
        if o.stop_after_errors and self.consecutive_errors >= o.stop_after_errors:
            return "errors"
        if o.budget_usd is not None:
            est = self.estimated_next_game_usd()
            reserved = est * len(self._running)
            if self.spent_usd + reserved + est > o.budget_usd + 1e-12:
                return "budget"
        if o.min_credit_usd is not None and self.credit_checker is not None:
            credit = self._check_credit()
            if credit is not None and credit < o.min_credit_usd:
                return "credit"
        return None

    # ------------------------------------------------------------------ games

    def _play(self, index: int, seed: int, swapped: bool) -> dict:
        cfg = swap_roles(self.cfg) if swapped else self.cfg
        fwd = _Forwarder(self, index)
        game = None
        try:
            client = self.client_factory()
            game = Game(cfg, client, on_event=fwd, log_dir=self.log_dir, mock=self.mock, seed=seed,
                        templates=self.templates, batch_id=self.batch_id, batch_index=index, roles_swapped=swapped)
            with self._lock:
                self._running[index]["game"] = game
                self._running[index]["game_id"] = game.game_id
            self._emit("batch_game_started", game_id=game.game_id, batch_index=index, roles_swapped=swapped,
                       trio_model=cfg.trio.model, imposter_model=cfg.imposter.model, seed=seed)
            fwd.release()
            self._progress_event()
            if self._aborted:
                game.abort("Batch aborted")
            while not game.is_finished():
                game.step()
                if self._aborted and not game.is_finished():
                    game.abort("Batch aborted")
        except Exception as e:  # noqa: BLE001 — never let one game kill the batch
            traceback.print_exc()
            if game is not None and not game.is_finished():
                game.abort(f"Batch error: {e}")
            fwd.release()
            if game is None:
                return {"batch_index": index, "game_id": None, "outcome": "error", "winner": None,
                        "eliminated": None, "eliminated_role": None, "has_imposter": None, "cost_usd": 0.0,
                        "roles_swapped": swapped, "trio_model": cfg.trio.model, "imposter_model": cfg.imposter.model,
                        "seed": seed, "rounds_completed": 0, "log_file": None, "error": str(e)}
        return {"batch_index": index, "game_id": game.game_id, "outcome": game.outcome, "winner": game.winner,
                "eliminated": game.eliminated, "eliminated_role": game.eliminated_role,
                "has_imposter": game.has_imposter, "cost_usd": round(game.total_cost_usd, 8),
                "roles_swapped": swapped, "trio_model": cfg.trio.model, "imposter_model": cfg.imposter.model,
                "seed": seed, "rounds_completed": game.rounds_completed,
                "log_file": str(game.logger.json_path), "end_reason": game.end_reason}

    def _game_done(self, result: dict) -> None:
        with self._lock:
            self._running.pop(result["batch_index"], None)
            self.games.append(result)
            self.spent_usd += result.get("cost_usd") or 0.0
            if result["outcome"] == "error":
                self.consecutive_errors += 1
            elif result["outcome"] != "aborted":
                self.consecutive_errors = 0
            summary = self._make_progress()
        self._emit("batch_game_finished", game=result, spent_usd=summary["spent_usd"],
                   games_finished=summary["games_finished"], outcomes=summary["outcomes"],
                   imposter_caught_rate=summary["imposter_caught_rate"])
        self._write_record()
        self._progress_event()

    # ------------------------------------------------------------------ main loop

    def run(self) -> dict:
        """Play games until a stop condition is reached. Returns the batch record."""
        o = self.options
        self.started_at = _now_iso()
        self._emit("batch_started", options=asdict(o), config=self.cfg.summary(), prompt_version=self.prompt_version,
                   prompts_modified=self.prompts_modified, max_games=o.games or None, budget_usd=o.budget_usd,
                   min_credit_usd=o.min_credit_usd, mock=self.mock, record_file=str(self.record_path))
        for w in self.prompt_warnings:
            self._emit("batch_warning", message=w)
        if o.min_credit_usd is not None and self.credit_checker is not None:
            self._check_credit()
        self._write_record()
        self._progress_event()

        pool = ThreadPoolExecutor(max_workers=o.parallel, thread_name_prefix=f"batch-{self.batch_id}")
        futures: dict[Future, int] = {}
        try:
            while True:
                with self._lock:
                    blocked = None
                    if len(futures) < o.parallel and self.stop_reason is None:
                        blocked = self._stop_reason_before_start()
                        if blocked is None:
                            self.games_started += 1
                            index = self.games_started
                            seed = self._rng.randrange(1 << 31)
                            swapped = bool(o.alternate_roles and index % 2 == 0)
                            self._running[index] = {"game": None, "game_id": None,
                                                    "estimate": self.estimated_next_game_usd()}
                            futures[pool.submit(self._play, index, seed, swapped)] = index
                            continue
                        # The budget may free up when a running game turns out cheaper than its
                        # reservation, so "budget" is only final once nothing is running.
                        if blocked != "budget" or not futures:
                            self.stop_reason = blocked
                    if not futures:
                        break
                done, _ = wait(list(futures), timeout=0.5, return_when=FIRST_COMPLETED)
                for fut in done:
                    futures.pop(fut)
                    try:
                        result = fut.result()
                    except Exception as e:  # noqa: BLE001
                        result = {"batch_index": -1, "game_id": None, "outcome": "error", "cost_usd": 0.0,
                                  "error": str(e)}
                    self._game_done(result)
        finally:
            pool.shutdown(wait=True)
        with self._lock:
            self.finished = True
            self.finished_at = _now_iso()
            self.stop_reason = self.stop_reason or "games_limit"
        self._write_record()
        record = self._record()
        self._progress_event()
        self._emit("batch_finished", stop_reason=self.stop_reason, **{k: record[k] for k in (
            "totals", "games", "prompt_version", "started_at", "finished_at", "options")},
            record_file=str(self.record_path))
        return record


STOP_REASON_TEXT = {
    "games_limit": "reached the game limit",
    "budget": ("the spending limit would have been exceeded by another game (before the first game finishes, "
               "each game is estimated at first_game_estimate_usd from the [batch] settings)"),
    "credit": "OpenRouter credit fell below the minimum",
    "errors": "too many games in a row ended in an error",
    "stopped": "stopped by you",
    "aborted": "aborted by you",
}


def plan_text(options: BatchOptions, cfg: GameConfig, mock: bool = False) -> str:
    """A plain-English description of what a batch will do."""
    o = options
    limits = []
    if o.games:
        limits.append(f"up to {o.games} game{'s' if o.games != 1 else ''}")
    if o.budget_usd is not None:
        limits.append(f"stopping before ${o.budget_usd:.2f} is spent")
    if o.min_credit_usd is not None and not mock:
        limits.append(f"or when OpenRouter credit falls below ${o.min_credit_usd:.2f}")
    text = ", ".join(limits).capitalize() + "."
    est = o.first_game_estimate_usd
    if not mock and o.budget_usd is not None and est > 0:
        roughly = int(o.budget_usd / est + 1e-9)
        if o.games:
            roughly = min(roughly, o.games)
        text += f" About ${est:.2f} per game estimated (until the first game finishes) → roughly {roughly} games."
    roles = (f"Trio: {cfg.trio.display_name} ({cfg.trio.model}); imposter: {cfg.imposter.display_name} "
             f"({cfg.imposter.model})")
    if o.alternate_roles:
        roles += "; every second game swaps the two"
    text += f" {roles}."
    if o.parallel > 1:
        text += f" {o.parallel} games at a time."
    if o.stop_after_errors:
        text += f" Stops after {o.stop_after_errors} failed games in a row."
    if mock:
        text += " Mock mode: fake players, $0."
    if o.budget_usd is not None and est > 0 and o.budget_usd < est:
        text += (f" Warning: the spending limit is below the estimated cost of one game (${est:.2f}), so no game "
                 "will start. Raise the limit, or lower first_game_estimate_usd in the [batch] settings.")
    return text


__all__ = ["BatchOptions", "BatchRunner", "ConfigError", "STOP_REASON_TEXT", "plan_text", "swap_roles"]
