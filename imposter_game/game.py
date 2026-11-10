"""The game engine.

`Game` runs one game step by step: `game.step()` performs one atomic piece of
work (all introductions, one question turn, or one vote) and returns. The GUI
server calls step() in a loop, which lets it pause between steps.

The rules that someone may want to tweak live in small functions near the top
of this file:

    vote_due(rounds_completed, rules)       when is a scheduled vote held?
    vote_call_triggered(requesters, rules)  have enough players called a vote?
    vote_trigger(called, scheduled)         which kind of vote (if any) happens next?
    parse_vote_call(text)                   reading "CALL VOTE: YES/NO" from an answer
    resolve_vote(ballots, cfg)              who (if anyone) is voted out?
    decide_after_vote(...)                  does the game end, and who won?
    parse_vote(text, me, names)             reading "VOTE: <Name>" from a reply
    find_name_violations / redact_names     the "no_names" rule
    normalize_typography                    straight quotes, plain dashes, no markdown
    clean_reply / limit_words               tidying model output

All the words the models see come from editable templates (see prompts.py).
"""

from __future__ import annotations

import inspect
import os
import random
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import prompts
from .config import GameConfig
from .prompts import NOBODY, PromptSet
from .logger import GameLogger, render_markdown

SCHEMA_VERSION = 2


# =============================================================================
# Rules (small, isolated, easy to change)
# =============================================================================

@dataclass
class VoteOutcome:
    tally: dict[str, int]            # every player -> votes received (0 included)
    eliminated: str | None           # who is voted out, or None
    top: list[str] = field(default_factory=list)   # player(s) with the most votes


def vote_due(rounds_completed: int, rules) -> bool:
    """Should a vote be held now that `rounds_completed` full rounds are done?

    Votes happen automatically every `rounds_before_vote` rounds. In "continue"
    mode a final vote is also held when max_rounds is reached.
    (Future variant: let players call votes — change only this function.)
    """
    if rounds_completed <= 0:
        return False
    if rounds_completed % rules.rounds_before_vote == 0:
        return True
    return rules.on_no_decision == "continue" and rounds_completed >= rules.max_rounds


def resolve_vote(ballots: dict[str, str | None], cfg) -> VoteOutcome:
    """Count a vote. `ballots` maps voter -> target (None = abstained).

    A player is voted out if they are the ONLY player with the most votes and
    have at least `vote_threshold` votes. (A ranked-choice tiebreak could be
    added here later.)
    """
    rules = getattr(cfg, "rules", cfg)
    tally = {name: 0 for name in rules.player_names}
    for voter, target in ballots.items():
        if target is not None and target != voter:
            tally[target] = tally.get(target, 0) + 1
    most = max(tally.values()) if tally else 0
    top = [n for n, c in tally.items() if c == most and c > 0]
    eliminated = top[0] if len(top) == 1 and most >= rules.vote_threshold else None
    return VoteOutcome(tally=tally, eliminated=eliminated, top=top)


def vote_call_triggered(requesters: list[str], rules) -> bool:
    """Players call a vote when at least `vote_call_threshold` of them asked in the same turn."""
    return bool(getattr(rules, "player_vote_calls", False)) and len(requesters) >= rules.vote_call_threshold


def vote_trigger(called: bool, scheduled: bool) -> str | None:
    """What kind of vote happens next: 'scheduled', 'called', 'called+scheduled' (one merged vote), or None."""
    if called and scheduled:
        return "called+scheduled"
    if called:
        return "called"
    if scheduled:
        return "scheduled"
    return None


def vote_label(number: int, trigger: str) -> str:
    return {"called": f"Vote {number} (called by players)",
            "called+scheduled": f"Vote {number} (scheduled + called by players)"}.get(trigger, f"Vote {number}")


def decide_after_vote(eliminated_role: str | None, rounds_completed: int, rules,
                      trigger: str = "scheduled", has_imposter: bool = True) -> str:
    """What happens after a vote.

    Returns 'continue' (play on) or a final outcome:
      imposter game:    'trio_win' | 'imposter_win' | 'no_decision'
      no-imposter game: 'trio_loss_no_imposter' (a copy was voted out) |
                        'trio_win_no_imposter' (the game ended with nobody voted out)
    A failed CALLED vote always continues. Scheduled (and merged called+scheduled)
    votes follow `on_no_decision`.
    """
    if not has_imposter and eliminated_role is not None:
        return "trio_loss_no_imposter"
    if eliminated_role == "imposter":
        return "trio_win"
    if eliminated_role == "trio":
        return "imposter_win"
    if trigger == "called":
        return "continue"
    if rules.on_no_decision == "continue" and rounds_completed < rules.max_rounds:
        return "continue"
    return "no_decision" if has_imposter else "trio_win_no_imposter"


WINNER_OF = {"trio_win": "trio", "imposter_win": "imposter", "trio_win_no_imposter": "trio"}


def decide_has_imposter(rng: random.Random, rules) -> bool:
    """Roll whether this game has an imposter (no_imposter_chance). Uses no randomness when the chance is 0."""
    chance = float(getattr(rules, "no_imposter_chance", 0.0) or 0.0)
    if chance <= 0:
        return True
    return not (rng.random() < chance)


_VOTE_RE = re.compile(r"^[\s>*_`#-]*vote\s*[:：]\s*(.*)$", re.IGNORECASE)


_NOBODY_WORDS = {"nobody", "none", "no one", "no-one", "noone"}


def parse_vote(text: str, me: str, names: list[str], allow_nobody: bool = False) -> tuple[str | None, str | None]:
    """Read the LAST 'VOTE: <Name>' line. Returns (target, None) or (None, problem).

    With allow_nobody, 'VOTE: NOBODY' returns (prompts.NOBODY, None).
    """
    lines = (text or "").splitlines()
    for line in reversed(lines):
        m = _VOTE_RE.match(line)
        if not m:
            continue
        raw = m.group(1).strip().strip("*_`\"'[]()<>.,!;: ").strip()
        word = raw.split()[0].strip("*_`\"'[]()<>.,!;:") if raw else ""
        if allow_nobody and (word.lower() in _NOBODY_WORDS or raw.lower() in _NOBODY_WORDS):
            return NOBODY, None
        for n in names:
            if word.lower() == n.lower() or raw.lower() == n.lower():
                if n == me:
                    return None, f"you voted for yourself ({me}), which is not allowed."
                return n, None
        return None, f'"{raw}" is not one of the players you can vote for.'
    return None, 'your reply did not end with a line of the form "VOTE: <Name>".'


_CALL_RE = re.compile(
    r"[*_`]*\bcall[ \t]*(?:a[ \t]+)?vote\b[*_`]*[ \t]*[:：=\-–—][ \t]*[*_`]*[ \t]*([A-Za-z]*)[*_`.!]*",
    re.IGNORECASE)


def parse_vote_call(text: str) -> tuple[bool | None, str]:
    """Read the LAST 'CALL VOTE: YES/NO' and remove every such line from the text.

    Returns (wants_vote, text_without_the_line). wants_vote is None if the line
    is missing or unreadable (which counts as NO).
    """
    text = text or ""
    matches = list(_CALL_RE.finditer(text))
    if not matches:
        return None, text.strip()
    word = matches[-1].group(1).lower()
    wants = True if word in ("yes", "y", "true") else False if word in ("no", "n", "false") else None
    stripped = _CALL_RE.sub("", text)
    stripped = "\n".join(line.rstrip() for line in stripped.splitlines()).strip()
    return wants, stripped


def strip_vote_line(text: str) -> str:
    """The private reasoning part of a vote reply (everything except VOTE lines)."""
    kept = [line for line in (text or "").splitlines() if not _VOTE_RE.match(line)]
    return "\n".join(kept).strip()


def build_name_terms(cfg: GameConfig) -> list[str]:
    """Words that break the no_names rule: config list + both models' names/ids."""
    terms = list(cfg.rules.banned_names)
    for m in (cfg.trio, cfg.imposter):
        terms += [m.display_name, m.model, m.model.split("/")[-1]]
    seen, out = set(), []
    for t in terms:
        t = t.strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def _names_regex(terms: list[str]) -> re.Pattern | None:
    if not terms:
        return None
    alts = sorted((re.escape(t).replace(r"\ ", r"[\s_-]*") for t in terms), key=len, reverse=True)
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(alts) + r")(?![A-Za-z0-9])", re.IGNORECASE)


def find_name_violations(text: str, terms: list[str]) -> list[str]:
    """All forbidden names found in text (whole words, case-insensitive)."""
    rx = _names_regex(terms)
    return [m.group(0) for m in rx.finditer(text or "")] if rx else []


def redact_names(text: str, terms: list[str]) -> str:
    rx = _names_regex(terms)
    return rx.sub("[REDACTED]", text) if rx else text


def clean_reply(text: str, names: list[str]) -> str:
    """Strip accidental 'Red:' / '**Blue:**' / 'Answer:' prefixes and wrapping quotes."""
    text = (text or "").strip()
    labels = [re.escape(n) for n in names] + ["answer", "question", "introduction", "intro", "my answer",
                                              "my question", "my introduction"]
    prefix = re.compile(r"^\s*[*_]*(?:" + "|".join(labels) + r")[*_]*\s*[:\-—–][*_]*\s*", re.IGNORECASE)
    for _ in range(3):
        new = prefix.sub("", text, count=1).strip()
        if new == text:
            break
        text = new
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'“”":
        text = text[1:-1].strip()
    elif len(text) >= 2 and text[0] == "“" and text[-1] == "”":
        text = text[1:-1].strip()
    return text


_TYPO_TABLE = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u2033": '"',
    "\u00ab": '"', "\u00bb": '"',
    "\u2026": "...",
    "\u00a0": " ", "\u2002": " ", "\u2003": " ", "\u2004": " ", "\u2005": " ", "\u2006": " ",
    "\u2007": " ", "\u2008": " ", "\u2009": " ", "\u200a": " ", "\u202f": " ", "\u205f": " ",
    "\u200b": None, "\u200c": None, "\u200d": None, "\u2060": None, "\ufeff": None,
})
_DASHES = "\u2012\u2013\u2014\u2015\u2212"


def normalize_typography(text: str) -> str:
    """Make typography uniform so it can't give a model away.

    Curly quotes -> straight, long dashes -> " - " (but "1–10" -> "1-10"), "…" -> "...",
    odd spaces -> normal/removed, markdown emphasis/headings/bullets stripped. Line breaks kept.
    """
    t = (text or "").translate(_TYPO_TABLE)
    t = re.sub(rf"(?<=\d)[ \t]*[{_DASHES}][ \t]*(?=\d)", "-", t)
    t = re.sub(rf"[ \t]*[{_DASHES}][ \t]*", " - ", t)
    out = []
    for line in t.split("\n"):
        line = re.sub(r"^[ \t]*#{1,6}[ \t]+", "", line)                 # markdown headings
        line = re.sub(r"^[ \t]*(?:[-*\u2022])[ \t]+", "", line)          # bullet markers
        line = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"\1", line)          # **bold**
        line = re.sub(r"__(?=\S)(.+?)(?<=\S)__", r"\1", line)              # __bold__
        line = re.sub(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])", r"\1", line)   # *italic*
        line = re.sub(r"(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])", r"\1", line)      # _italic_
        line = re.sub(r"[ \t]{2,}", " ", line).strip()
        out.append(line)
    return "\n".join(out).strip()


def limit_words(text: str, limit: int, factor: float = 2.0) -> str:
    """Cut text that is far over its word limit (more than factor x limit)."""
    words = text.split()
    hard = int(limit * factor)
    if len(words) <= hard:
        return text
    return " ".join(words[:hard]) + " […]"


# =============================================================================
# The game
# =============================================================================

@dataclass(eq=False)
class Player:
    name: str
    seat: int                   # 1-based seat number
    role: str                   # "trio" | "imposter"
    model: str
    display_name: str
    reasoning_effort: str | None
    max_tokens: int
    system_prompt: str = ""
    reminder: str = ""          # private one-line role reminder put above every task

    def public_dict(self) -> dict:
        return {"name": self.name, "seat": self.seat, "role": self.role, "model": self.model,
                "display_name": self.display_name, "reasoning_effort": self.reasoning_effort}


class PlayerCallError(Exception):
    def __init__(self, player: str, purpose: str, error: BaseException):
        super().__init__(f"{player} ({purpose}): {error}")
        self.player = player
        self.error = error


def _accepts_kwarg(client, name: str) -> bool:
    """Does client.chat accept `name=` (or **kwargs)? Old/fake clients may not."""
    try:
        params = inspect.signature(client.chat).parameters
    except (TypeError, ValueError, AttributeError):
        return False
    return name in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _new_game_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()


class Game:
    """One game, advanced one step at a time. See module docstring."""

    def __init__(self, cfg: GameConfig, client, on_event: Callable[[dict], None] | None = None,
                 log_dir: Path = Path("logs"), mock: bool = False, seed: int | None = None,
                 prompts_dir: str | Path | None = None, templates: dict[str, str] | None = None):
        self.cfg = cfg
        self.rules = cfg.rules
        self.client = client
        self.on_event = on_event
        self.mock = mock
        self.seed = seed
        self.game_id = _new_game_id()
        self.logger = GameLogger(log_dir, self.game_id, mock)
        while self.logger.json_path.exists():
            self.game_id = _new_game_id()
            self.logger = GameLogger(log_dir, self.game_id, mock)

        self.events: list[dict] = []
        self.calls: list[dict] = []
        self._lock = threading.RLock()        # events, counters, totals
        self._step_lock = threading.Lock()    # one step at a time
        self._seq = 0
        self._call_counter = 0
        self._closed = False                  # game_over emitted
        self._started = False                 # True once step() has run; no files are written before
        self.started_at = _now_iso()
        self.finished_at: str | None = None

        # Totals
        self.total_cost_usd = 0.0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.cost_by_model: dict[str, float] = {}
        self.reasoning_returned: dict[str, bool] = {}
        self.cost_sources: set[str] = set()

        # Prompt templates: loaded once, here (edits apply to the next game).
        self.prompts = PromptSet(templates, prompts_dir=prompts_dir)
        self._client_takes_context = _accepts_kwarg(client, "purpose")

        # Seats (and whether there is an imposter at all)
        names = list(self.rules.player_names)
        rng = random.Random(seed)
        imp_index = rng.randrange(len(names)) if self.rules.shuffle_seats else len(names) - 1
        self.has_imposter = decide_has_imposter(rng, self.rules)
        self.allow_nobody = float(self.rules.no_imposter_chance or 0) > 0
        self.players: list[Player] = []
        for i, name in enumerate(names):
            is_imp = self.has_imposter and i == imp_index
            m = cfg.imposter if is_imp else cfg.trio
            role = "imposter" if is_imp else "trio"
            p = Player(name=name, seat=i + 1, role=role, model=m.model, display_name=m.display_name,
                       reasoning_effort=m.reasoning_effort, max_tokens=cfg.max_tokens_for(role))
            p.system_prompt = self.prompts.system_prompt(name, role, m.display_name, self.rules, names)
            p.reminder = self.prompts.reminder(name, role, m.display_name, names, self.rules)
            self.players.append(p)
        self.by_name = {p.name: p for p in self.players}
        self.imposter: Player | None = self.players[imp_index] if self.has_imposter else None
        self.name_terms = build_name_terms(cfg)

        # Progress state
        self._state = "intro"                 # intro | turn | vote | done
        self.round = 1                        # round currently being played
        self.turn = 1                         # next turn within the round (1..4)
        self.rounds_completed = 0
        self.vote_number = 0
        self.outcome: str | None = None
        self.winner: str | None = None
        self.eliminated: str | None = None
        self.eliminated_role: str | None = None
        self.rounds_survived: int | None = None
        self.end_reason: str | None = None
        self.votes: list[dict] = []           # per-vote summary for the logs
        self.turns_completed = 0              # question turns fully played
        self._vote_trigger: str | None = None # kind of the pending vote (state "vote")
        self._vote_call_count: int | None = None   # how many asked, if players called it
        self.eliminated_by: str | None = None
        self.vote_requests: dict[str, dict[str, int]] = {}  # model -> {yes, no, missing}

        # What players have publicly seen
        self._intros: list[tuple[str, str]] = []
        self._entries: list[dict] = []
        self._own_votes: dict[str, list[tuple[int, str | None]]] = {p.name: [] for p in self.players}

        self._emit("game_started", mock=mock, config=self._config_summary(),
                   players=[p.public_dict() for p in self.players], has_imposter=self.has_imposter,
                   prompt_version=self.prompts.version, prompts_modified=self.prompts.modified)
        for w in self.prompts.warnings:
            self._emit("error", message=w, fatal=False, player=None)
        # Log files are only created once the first step() runs, so a game that
        # is created but never played leaves nothing on disk.

    # ------------------------------------------------------------------ API

    def is_finished(self) -> bool:
        return self._state == "done"

    def next_label(self) -> str:
        if self._state == "intro":
            return "Introductions"
        if self._state == "turn":
            return f"Round {self.round} · {self.players[self.turn - 1].name} asks"
        if self._state == "vote":
            return vote_label(self.vote_number + 1, self._vote_trigger or "scheduled")
        return ""

    def step(self) -> dict:
        """Run one atomic step. Never raises on API failure."""
        with self._step_lock:
            if self.is_finished():
                return {"boundary": True, "finished": True}
            self._started = True
            boundary = True
            try:
                if self._state == "intro":
                    self._do_intros()
                elif self._state == "turn":
                    boundary = self._do_turn()
                elif self._state == "vote":
                    self._do_vote()
            except Exception as e:  # noqa: BLE001 — a game must never crash the server
                self._fail(e)
                boundary = True
            self._write_logs()
            return {"boundary": boundary, "finished": self.is_finished()}

    def abort(self, reason: str) -> None:
        """Stop the game now (e.g. the user pressed Stop)."""
        self._finish("aborted", reason=reason or "aborted")

    # ------------------------------------------------------------ Events

    def _emit(self, type_: str, **fields) -> dict | None:
        with self._lock:
            if self._closed:
                return None
            self._seq += 1
            event = {"type": type_, "seq": self._seq, "ts": _now_iso(), "game_id": self.game_id, **fields}
            self.events.append(event)
            if type_ == "game_over":
                self._closed = True
            if self.on_event is not None:
                try:
                    self.on_event(event)
                except Exception:  # noqa: BLE001 — a GUI bug must not kill the game
                    traceback.print_exc()
            return event

    def _config_summary(self) -> dict:
        s = self.cfg.summary()
        s["prompt_version"] = self.prompts.version
        s["prompts_modified"] = self.prompts.modified
        return s

    def _set_state(self, state: str) -> None:
        """Move to the next phase, unless the game was ended meanwhile (e.g. aborted)."""
        with self._lock:
            if self._state != "done":
                self._state = state

    # ------------------------------------------------------------ Model calls

    def _messages(self, player: Player, task: str) -> list[dict]:
        transcript = prompts.render_transcript(self._intros, self._entries)
        notes = "" if self.rules.reveal_votes else prompts.private_notes(self._own_votes[player.name])
        return [{"role": "system", "content": player.system_prompt},
                {"role": "user", "content": prompts.user_message(transcript, task, notes, player.reminder)}]

    @staticmethod
    def _with_reminder(player: Player, text: str) -> str:
        """A follow-up user message (retry), also starting with the role reminder."""
        return f"{player.reminder}\n\n{text}" if player.reminder else text

    def _context(self, player: Player, **extra) -> dict:
        """Facts about a call for the mock client (so it never has to parse prompt text)."""
        ctx = {"player": player.name, "is_imposter": player.role == "imposter",
               "seat_names": [p.name for p in self.players], "no_names": self.rules.name_rule == "no_names",
               "vote_calls": self.rules.player_vote_calls, "allow_nobody": self.allow_nobody,
               "round": self.round, "rounds_completed": self.rounds_completed,
               "public_key": f"{len(self._intros)}|{len(self._entries)}|"
                             f"{sum(len(e.get('answers', [])) for e in self._entries)}|{self.vote_number}"}
        ctx.update(extra)
        return ctx

    def _call(self, player: Player, purpose: str, messages: list[dict], round_no: int | None,
              context: dict | None = None, model: str | None = None, effort: str | None = None,
              max_tokens: int | None = None, session: str | None = None, is_paraphrase: bool = False):
        """One model call: run it, record it, emit api_call. Returns (CallResult, call_id)."""
        model = model or player.model
        effort = effort if is_paraphrase else player.reasoning_effort
        max_tokens = max_tokens or player.max_tokens
        with self._lock:
            self._call_counter += 1
            call_id = f"c{self._call_counter:04d}"
        kwargs = {"session_id": session or f"{self.game_id}-{player.name}"}
        if self._client_takes_context:
            kwargs["purpose"] = purpose
            kwargs["context"] = context if context is not None else self._context(player)
        try:
            res = self.client.chat(model, messages, effort, max_tokens, **kwargs)
        except Exception as e:
            with self._lock:
                self.calls.append({"call_id": call_id, "player": player.name, "purpose": purpose,
                                   "round": round_no, "model_requested": model,
                                   "user_message": messages[-1]["content"], "error": str(e)})
            raise PlayerCallError(player.name, purpose, e) from e

        reasoning_available = bool(res.reasoning and res.reasoning.strip())
        user_idx = 0 if is_paraphrase else 1
        with self._lock:
            self.calls.append({
                "call_id": call_id, "player": player.name, "role": "paraphraser" if is_paraphrase else player.role,
                "purpose": purpose,
                "round": round_no, "model_requested": model, "model": res.model,
                "provider": res.provider, "reasoning_effort": effort,
                "max_tokens": max_tokens, "max_tokens_used": res.max_tokens_used,
                "user_message": messages[user_idx]["content"] if len(messages) > user_idx else None,
                "extra_messages": messages[user_idx + 1:] if len(messages) > user_idx + 1 else None,
                "content": res.content, "reasoning": res.reasoning, "reasoning_kind": res.reasoning_kind,
                "reasoning_details": res.reasoning_details, "refusal": res.refusal,
                "input_tokens": res.input_tokens, "output_tokens": res.output_tokens,
                "reasoning_tokens": res.reasoning_tokens, "cached_tokens": res.cached_tokens,
                "cache_write_tokens": res.cache_write_tokens, "cost_usd": res.cost_usd,
                "cost_source": res.cost_source, "latency_s": res.latency_s, "generation_id": res.generation_id,
                "finish_reason": res.finish_reason, "attempts": res.attempts, "notes": res.notes,
                "raw_usage": res.raw_usage,
            })
            self.total_cost_usd += res.cost_usd or 0.0
            self.total_input_tokens += res.input_tokens or 0
            self.total_output_tokens += res.output_tokens or 0
            self.cost_by_model[model] = self.cost_by_model.get(model, 0.0) + (res.cost_usd or 0.0)
            if not is_paraphrase:
                self.reasoning_returned[model] = self.reasoning_returned.get(model, False) or reasoning_available
            self.cost_sources.add(res.cost_source)
            self._emit("api_call", call_id=call_id, player=player.name, model=res.model, purpose=purpose,
                       input_tokens=res.input_tokens, output_tokens=res.output_tokens,
                       reasoning_tokens=res.reasoning_tokens, cached_tokens=res.cached_tokens,
                       cost_usd=res.cost_usd, cost_source=res.cost_source, latency_s=res.latency_s,
                       total_cost_usd=round(self.total_cost_usd, 8), total_input_tokens=self.total_input_tokens,
                       total_output_tokens=self.total_output_tokens, reasoning_available=reasoning_available,
                       reasoning_kind=res.reasoning_kind, cache_write_tokens=res.cache_write_tokens,
                       finish_reason=res.finish_reason, round=round_no, notes=list(res.notes))
        return res, call_id

    def _call_text(self, player: Player, purpose: str, task: str, round_no: int | None, **ctx):
        """A call whose reply is public text. Retries once if the reply is empty."""
        messages = self._messages(player, task)
        context = self._context(player, **ctx)
        res, call_id = self._call(player, purpose, messages, round_no, context=context)
        if not (res.content or "").strip():
            retry = messages + [{"role": "assistant", "content": "(no reply)"},
                                {"role": "user", "content": self._with_reminder(
                                    player, self.prompts.empty_reply_retry_message())}]
            res, call_id = self._call(player, purpose + "_retry_empty", retry, round_no, context=context)
        return res, call_id

    def _annotate_call(self, call_id: str, **fields) -> None:
        """Add extra fields to an already-recorded call in the log."""
        with self._lock:
            for c in reversed(self.calls):
                if c.get("call_id") == call_id:
                    c.update(fields)
                    return

    def _paraphrase(self, player: Player, text: str, round_no: int | None) -> tuple[str | None, str | None]:
        """Rewrite text with the [paraphrase] model. Returns (new_text, call_id) or (None, None) on failure."""
        pcfg = self.cfg.paraphrase
        messages = [{"role": "user", "content": self.prompts.paraphrase_request(text)}]
        try:
            res, call_id = self._call(player, "paraphrase", messages, round_no,
                                      context=self._context(player, text=text), model=pcfg.model,
                                      effort=pcfg.reasoning_effort, max_tokens=pcfg.max_output_tokens,
                                      session=f"{self.game_id}-paraphrase", is_paraphrase=True)
        except PlayerCallError as e:
            self._emit("error", message=f"Paraphrase failed, showing the original text instead: {e.error}",
                       fatal=False, player=player.name)
            return None, None
        new = (res.content or "").strip()
        if not new:
            self._emit("error", message="Paraphrase returned nothing, showing the original text instead.",
                       fatal=False, player=player.name)
            self._annotate_call(call_id, paraphrase_failed=True)
            return None, call_id
        return new, call_id

    def _public_text(self, player: Player, raw: str, call_id: str, word_limit: int,
                     round_no: int | None = None) -> tuple[str, str, list[str], str | None]:
        """Turn a reply into what other players see.

        Pipeline: tidy ("Red:" prefixes, quotes) -> paraphrase (optional) -> normalize typography
        -> word limit -> name rule (checked on the text before AND after paraphrasing).
        Returns (shown_text, original_text, transforms, paraphrase_call_id).
        """
        original = (raw or "").strip()
        transforms: list[str] = []
        text = clean_reply(original, self.rules.player_names)
        before_paraphrase = text
        para_id = None
        if self.cfg.paraphrase.enabled and text:
            new, para_id = self._paraphrase(player, text, round_no)
            if new is not None:
                text = clean_reply(new, self.rules.player_names)
                transforms.append("paraphrased")
        if self.rules.normalize_typography:
            tidy = normalize_typography(text)
            if tidy != text:
                transforms.append("normalized")
            text = tidy
        text = limit_words(text, word_limit) or "(no reply)"
        if self.rules.name_rule == "no_names":
            matches = find_name_violations(text, self.name_terms)
            if "paraphrased" in transforms:
                matches += [m for m in find_name_violations(before_paraphrase, self.name_terms) if m not in matches]
            if matches:
                redact = self.rules.redact_violations
                self._emit("rule_violation", player=player.name, call_id=call_id, matches=matches, redacted=redact)
                if redact:
                    redacted = redact_names(text, self.name_terms)
                    if redacted != text:
                        transforms.append("redacted")
                    text = redacted
        return text, original, transforms, para_id

    def _parallel(self, fn, players: list[Player]) -> dict[str, object]:
        """Run fn(player) for several players at once; results keyed by name."""
        if not players:
            return {}
        pool = ThreadPoolExecutor(max_workers=len(players), thread_name_prefix=f"game-{self.game_id}")
        try:
            futures = {pool.submit(fn, p): p.name for p in players}
            results = {}
            for fut in as_completed(futures):
                results[futures[fut]] = fut.result()   # re-raises a worker's exception
            return results
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _answer_order(self, asker: str) -> list[Player]:
        """Seat order starting after the asker; the asker answers last."""
        i = self.by_name[asker].seat - 1
        return self.players[i + 1:] + self.players[:i + 1]

    # ------------------------------------------------------------ Steps

    def _do_intros(self) -> None:
        self._emit("phase", phase="intro", round=None, label="Introductions")
        limit = self.rules.intro_word_limit
        trio = [p for p in self.players if p.role == "trio"]   # all four in a no-imposter game
        for p in trio:
            self._emit("thinking", player=p.name, action="intro", round=None)

        def write_intro(p: Player):
            res, call_id = self._call_text(p, "intro", self.prompts.intro_task(limit, self.allow_nobody), None)
            return self._public_text(p, res.content, call_id, limit), res, call_id

        done = self._parallel(write_intro, trio)
        imp = self.imposter
        if imp is not None:
            # The imposter writes last, after reading the trio intros (as players will see them) in seat order.
            seen = [(p.name, done[p.name][0][0]) for p in trio]
            self._emit("thinking", player=imp.name, action="intro", round=None)
            res, call_id = self._call_text(imp, "intro", self.prompts.imposter_intro_task(seen, limit), None,
                                           trio_intros=seen)
            done[imp.name] = (self._public_text(imp, res.content, call_id, limit), res, call_id)

        # Reveal all four in SEAT order.
        for p in self.players:
            (text, original, transforms, para_id), res, call_id = done[p.name]
            self._emit("message", player=p.name, kind="intro", round=None, turn=None, question_id=None,
                       asker=None, text=text, original_text=original, reasoning=res.reasoning, call_id=call_id,
                       transforms=transforms, paraphrase_call_id=para_id)
        self._intros = [(p.name, done[p.name][0][0]) for p in self.players]
        self._set_state("turn")

    def _do_turn(self) -> bool:
        r, t = self.round, self.turn
        asker = self.players[t - 1]
        qid = f"r{r}t{t}"
        if t == 1:
            self._emit("phase", phase="round", round=r, label=f"Round {r}")

        # 1. The question
        self._emit("thinking", player=asker.name, action="question", round=r)
        res, call_id = self._call_text(asker, "question",
                                       self.prompts.question_task(r, t, self.rules.question_word_limit), r, turn=t)
        question, original, transforms, para_id = self._public_text(asker, res.content, call_id,
                                                                    self.rules.question_word_limit, r)
        self._emit("message", player=asker.name, kind="question", round=r, turn=t, question_id=qid,
                   asker=asker.name, text=question, original_text=original, reasoning=res.reasoning, call_id=call_id,
                   transforms=transforms, paraphrase_call_id=para_id)

        # 2. Everyone answers
        order = self._answer_order(asker.name)
        limit = self.rules.answer_word_limit
        answers: list[tuple[str, str]] = []

        calls_on = self.rules.player_vote_calls
        wants: dict[str, bool | None] = {}

        def answer(p: Player, earlier: list[tuple[str, str]] | None):
            sequential = self.rules.answer_mode == "sequential"
            task = self.prompts.answer_task(asker.name, question, p.name, limit, earlier, sequential, calls_on)
            res, call_id = self._call_text(p, "answer", task, r, turn=t, question=question, asker=asker.name)
            wants_vote, content = None, res.content
            if calls_on:
                wants_vote, content = parse_vote_call(res.content)
                self._annotate_call(call_id, wants_vote=wants_vote, vote_call_note=None if wants_vote is not None
                                    else "no readable 'CALL VOTE: YES/NO' line; counted as NO")
            text, _, transforms, para_id = self._public_text(p, content, call_id, limit, r)
            return text, (res.content or "").strip(), res, call_id, wants_vote, transforms, para_id

        def show(p: Player, result) -> None:
            text, original, res, call_id, wants_vote, transforms, para_id = result
            extra = {"wants_vote": wants_vote} if calls_on else {}
            self._emit("message", player=p.name, kind="answer", round=r, turn=t, question_id=qid,
                       asker=asker.name, text=text, original_text=original, reasoning=res.reasoning,
                       call_id=call_id, transforms=transforms, paraphrase_call_id=para_id, **extra)
            answers.append((p.name, text))
            wants[p.name] = wants_vote
            if calls_on:
                rec = self.vote_requests.setdefault(p.model, {"yes": 0, "no": 0, "missing": 0})
                rec["yes" if wants_vote else "no" if wants_vote is False else "missing"] += 1

        if self.rules.answer_mode == "sequential":
            for p in order:
                self._emit("thinking", player=p.name, action="answer", round=r)
                show(p, answer(p, list(answers)))
        else:
            for p in order:
                self._emit("thinking", player=p.name, action="answer", round=r)
            results = self._parallel(lambda p: answer(p, None), order)
            for p in order:
                show(p, results[p.name])

        # 3. Did the players call a vote?
        requesters = [p.name for p in self.players if wants.get(p.name)]
        called = vote_call_triggered(requesters, self.rules)
        if calls_on:
            self._emit("vote_call_check", round=r, turn=t, requesters=requesters, count=len(requesters),
                       threshold=self.rules.vote_call_threshold, triggered=called)
        show_requests = calls_on and self.rules.reveal_vote_calls
        self._entries.append({"kind": "turn", "round": r, "turn": t, "asker": asker.name,
                              "question": question, "answers": answers,
                              "requesters": requesters if show_requests else None})
        self.turns_completed += 1

        # 4. Advance
        self.turn += 1
        end_of_round = self.turn > len(self.players)
        if end_of_round:
            self.rounds_completed = r
            self.round += 1
            self.turn = 1
        trigger = vote_trigger(called, end_of_round and vote_due(self.rounds_completed, self.rules))
        if trigger:
            self._vote_trigger = trigger
            self._vote_call_count = len(requesters) if called else None
            self._set_state("vote")
        return end_of_round or trigger is not None

    def _cast_vote(self, p: Player, number: int, note: str = "") -> dict:
        valid_names = [x.name for x in self.players if x is not p]
        threshold = self.rules.vote_threshold
        nobody = self.allow_nobody
        messages = self._messages(p, self.prompts.vote_task(number, p.name, valid_names, threshold, note, nobody))
        ctx = self._context(p, valid_choices=valid_names, vote_number=number)
        res, call_id = self._call(p, "vote", messages, self.rounds_completed, context=ctx)
        target, problem = parse_vote(res.content, p.name, self.rules.player_names, nobody)
        first_problem = problem
        if target is None:
            retry = messages + [{"role": "assistant", "content": res.content or "(no reply)"},
                                {"role": "user", "content": self._with_reminder(
                                    p, self.prompts.vote_retry_message(problem, valid_names, nobody))}]
            res, call_id = self._call(p, "vote_retry", retry, self.rounds_completed, context={**ctx, "is_retry": True})
            target, problem = parse_vote(res.content, p.name, self.rules.player_names, nobody)
        voted_nobody = target == NOBODY
        abstain_reason = "nobody" if voted_nobody else ("invalid" if target is None else None)
        return {"target": None if voted_nobody else target, "valid": target is not None,
                "abstain_reason": abstain_reason, "raw_text": res.content,
                "reasoning": strip_vote_line(res.content) or None, "model_reasoning": res.reasoning,
                "call_id": call_id, "retried": first_problem is not None,
                "problem": problem if target is None else first_problem}

    def _do_vote(self) -> None:
        self.vote_number += 1
        n = self.vote_number
        trigger = self._vote_trigger or "scheduled"
        call_count = self._vote_call_count
        self._emit("phase", phase="vote", round=self.rounds_completed, label=vote_label(n, trigger), trigger=trigger)
        note = ""
        if self.rules.player_vote_calls:
            continues = decide_after_vote(None, self.rounds_completed, self.rules, trigger) == "continue"
            note = self.prompts.vote_trigger_note(trigger, call_count, continues, self.allow_nobody)
        for p in self.players:
            self._emit("thinking", player=p.name, action="vote", round=self.rounds_completed)
        results = self._parallel(lambda p: self._cast_vote(p, n, note), self.players)

        ballots: dict[str, str | None] = {}         # NOBODY and invalid votes are None (abstain)
        shown_ballots: dict[str, str | None] = {}   # for the public transcript: NOBODY spelled out
        for p in self.players:
            v = results[p.name]
            ballots[p.name] = v["target"]
            shown_ballots[p.name] = NOBODY if v["abstain_reason"] == "nobody" else v["target"]
            self._own_votes[p.name].append((n, shown_ballots[p.name]))
            self._emit("vote_cast", player=p.name, target=v["target"], reasoning=v["reasoning"],
                       raw_text=v["raw_text"], valid=v["valid"], call_id=v["call_id"], vote_number=n,
                       retried=v["retried"], problem=v["problem"], model_reasoning=v["model_reasoning"],
                       abstain_reason=v["abstain_reason"])

        outcome = resolve_vote(ballots, self.cfg)
        elim_role = self.by_name[outcome.eliminated].role if outcome.eliminated else None
        decision = decide_after_vote(elim_role, self.rounds_completed, self.rules, trigger, self.has_imposter)
        nobody_votes = sum(1 for v in results.values() if v["abstain_reason"] == "nobody")
        self._emit("vote_result", vote_number=n, tally=outcome.tally, ballots=ballots,
                   eliminated=outcome.eliminated, eliminated_role=elim_role, decision=decision,
                   rounds_completed=self.rounds_completed, trigger=trigger, call_count=call_count,
                   turns_completed=self.turns_completed, has_imposter=self.has_imposter, nobody_votes=nobody_votes)

        trio_names = {p.name for p in self.players if p.role == "trio"}
        imp_name = self.imposter.name if self.imposter else None
        self.votes.append({
            "vote_number": n, "rounds_completed": self.rounds_completed, "tally": outcome.tally,
            "ballots": ballots, "eliminated": outcome.eliminated, "eliminated_role": elim_role,
            "decision": decision, "trigger": trigger, "call_count": call_count,
            "turns_completed": self.turns_completed, "nobody_votes": nobody_votes,
            "imposter_votes_from_trio": sum(1 for v, t in ballots.items()
                                            if v in trio_names and imp_name and t == imp_name),
            "trio_valid_ballots": sum(1 for v, t in ballots.items() if v in trio_names and t is not None),
        })
        self._entries.append({
            "kind": "vote", "vote_number": n, "tally": outcome.tally,
            "called_count": call_count if "called" in trigger else None, "num_players": len(self.players),
            "ballots": shown_ballots if self.rules.reveal_votes else None,
            "result_text": prompts.vote_result_text(outcome.eliminated, outcome.tally,
                                                    self.rules.vote_threshold, decision == "continue"),
        })

        self._vote_trigger, self._vote_call_count = None, None
        if decision == "continue":
            self._set_state("turn")
            return
        if outcome.eliminated:
            self.eliminated_by = trigger
        winner = WINNER_OF.get(decision)
        self._finish(decision, winner=winner, eliminated=outcome.eliminated, eliminated_role=elim_role,
                     rounds_survived=self.rounds_completed if outcome.eliminated else None)

    # ------------------------------------------------------------ Ending

    def _fail(self, e: BaseException) -> None:
        player = e.player if isinstance(e, PlayerCallError) else None
        message = str(e) if isinstance(e, PlayerCallError) else f"{type(e).__name__}: {e}"
        if not isinstance(e, PlayerCallError):
            traceback.print_exc()
        self._emit("error", message=message, fatal=True, player=player)
        self._finish("error", reason=message)

    def _finish(self, outcome: str, winner: str | None = None, eliminated: str | None = None,
                eliminated_role: str | None = None, rounds_survived: int | None = None,
                reason: str | None = None) -> None:
        with self._lock:
            if self._state == "done":
                return
            self._state = "done"
            self.outcome, self.winner = outcome, winner
            self.eliminated, self.eliminated_role = eliminated, eliminated_role
            self.rounds_survived = rounds_survived
            self.end_reason = reason
            self.finished_at = _now_iso()
            self._emit("game_over", outcome=outcome, winner=winner, eliminated=eliminated,
                       eliminated_role=eliminated_role, eliminated_by=self.eliminated_by,
                       rounds_survived=rounds_survived,
                       rounds_completed=self.rounds_completed, turns_completed=self.turns_completed,
                       total_cost_usd=round(self.total_cost_usd, 8),
                       total_input_tokens=self.total_input_tokens, total_output_tokens=self.total_output_tokens,
                       num_api_calls=len(self.calls),
                       log_file=str(self.logger.json_path) if self._started else None,
                       transcript_file=str(self.logger.md_path) if self._started else None, reason=reason,
                       imposter=self.imposter.name if self.imposter else None, has_imposter=self.has_imposter)
        if not self._started:
            return          # never played: leave no files behind
        self._write_logs()
        try:
            self.logger.append_summary(self._summary_row())
        except OSError:
            traceback.print_exc()

    # ------------------------------------------------------------ Logs

    def _totals(self) -> dict:
        return {"total_cost_usd": round(self.total_cost_usd, 8), "total_input_tokens": self.total_input_tokens,
                "total_output_tokens": self.total_output_tokens, "num_api_calls": len(self.calls),
                "cost_by_model": {k: round(v, 8) for k, v in self.cost_by_model.items()},
                "cost_sources": sorted(self.cost_sources)}

    def _record(self) -> dict:
        with self._lock:
            return {
                "schema_version": SCHEMA_VERSION, "game_id": self.game_id, "mock": self.mock, "seed": self.seed,
                "started_at": self.started_at, "finished_at": self.finished_at,
                "config": self._config_summary(),
                "players": [p.public_dict() for p in self.players],
                "seat_order": [p.name for p in self.players],
                "system_prompts": {p.name: p.system_prompt for p in self.players},
                "has_imposter": self.has_imposter,
                "prompt_version": self.prompts.version, "prompts_modified": self.prompts.modified,
                "prompt_warnings": list(self.prompts.warnings),
                "templates": dict(self.prompts.templates),
                "state": {"state": self._state, "round": self.round, "turn": self.turn,
                          "rounds_completed": self.rounds_completed, "vote_number": self.vote_number,
                          "turns_completed": self.turns_completed, "pending_vote_trigger": self._vote_trigger},
                "outcome": {"outcome": self.outcome, "winner": self.winner, "eliminated": self.eliminated,
                            "eliminated_role": self.eliminated_role, "rounds_survived": self.rounds_survived,
                            "rounds_completed": self.rounds_completed, "turns_completed": self.turns_completed,
                            "eliminated_by": self.eliminated_by, "reason": self.end_reason},
                "votes": list(self.votes),
                "totals": self._totals(),
                "calls": list(self.calls),
                "events": list(self.events),
            }

    def _write_logs(self) -> None:
        if not self._started:
            return
        try:
            record = self._record()
            self.logger.write_record(record)
            self.logger.write_transcript(render_markdown(record))
        except Exception:  # noqa: BLE001 — logging problems must not kill the game
            traceback.print_exc()

    def _summary_row(self) -> dict:
        cfg = self.cfg
        winner_model = {"trio": cfg.trio.model, "imposter": cfg.imposter.model}.get(self.winner or "")
        elim_model = self.by_name[self.eliminated].model if self.eliminated else None
        if not self.has_imposter:
            imposter_rounds = None
        else:
            imposter_rounds = (self.rounds_survived if self.eliminated_role == "imposter" else self.rounds_completed)
        return {
            "game_id": self.game_id, "timestamp": self.finished_at, "mock": self.mock, "seed": self.seed,
            "trio_model": cfg.trio.model, "imposter_model": cfg.imposter.model,
            "trio_display_name": cfg.trio.display_name, "imposter_display_name": cfg.imposter.display_name,
            "trio_effort": cfg.trio.reasoning_effort, "imposter_effort": cfg.imposter.reasoning_effort,
            "name_rule": self.rules.name_rule, "answer_mode": self.rules.answer_mode,
            "rounds_before_vote": self.rules.rounds_before_vote, "vote_threshold": self.rules.vote_threshold,
            "on_no_decision": self.rules.on_no_decision,
            "imposter_seat": self.imposter.name if self.imposter else None,
            "has_imposter": self.has_imposter,
            "no_imposter_chance": self.rules.no_imposter_chance,
            "prompt_version": self.prompts.version, "prompts_modified": self.prompts.modified,
            "normalize_typography": self.rules.normalize_typography,
            "paraphrase_model": self.cfg.paraphrase.model if self.cfg.paraphrase.enabled else None,
            "imposter_knows_own_model": self.rules.imposter_knows_own_model,
            "intro_word_limit": self.rules.intro_word_limit,
            "outcome": self.outcome, "winner": self.winner, "winner_model": winner_model,
            "eliminated": self.eliminated, "eliminated_role": self.eliminated_role,
            "eliminated_model": elim_model, "rounds_survived": self.rounds_survived,
            "imposter_rounds_survived": imposter_rounds,
            "rounds_completed": self.rounds_completed,
            "turns_completed": self.turns_completed,
            "player_vote_calls": self.rules.player_vote_calls,
            "vote_call_threshold": self.rules.vote_call_threshold,
            "called_votes": sum(1 for v in self.votes if "called" in v["trigger"]),
            "scheduled_votes": sum(1 for v in self.votes if "scheduled" in v["trigger"]),
            "eliminated_by": self.eliminated_by,
            "vote_requests_by_model": {m: dict(c) for m, c in self.vote_requests.items()},
            "votes": [{"vote_number": v["vote_number"], "rounds_completed": v["rounds_completed"],
                       "turns_completed": v["turns_completed"], "trigger": v["trigger"],
                       "call_count": v["call_count"], "tally": v["tally"], "ballots": v["ballots"],
                       "nobody_votes": v["nobody_votes"], "eliminated": v["eliminated"]}
                      for v in self.votes],
            "imposter_votes_received_by_trio": [v["imposter_votes_from_trio"] for v in self.votes],
            "trio_valid_ballots": [v["trio_valid_ballots"] for v in self.votes],
            "total_cost_usd": round(self.total_cost_usd, 8), "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens, "num_api_calls": len(self.calls),
            "cost_by_model": {k: round(v, 8) for k, v in self.cost_by_model.items()},
            "reasoning_returned": dict(self.reasoning_returned),
            "cost_sources": sorted(self.cost_sources),
            "log_file": str(self.logger.json_path),
            "end_reason": self.end_reason,
        }
