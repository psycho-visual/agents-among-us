"""The game engine.

`Game` runs one game step by step: `game.step()` performs one atomic piece of
work (all introductions, one question turn, or one vote) and returns. The GUI
server calls step() in a loop, which lets it pause between steps.

The rules that someone may want to tweak live in small functions near the top
of this file:

    vote_due(rounds_completed, rules)       when is a vote held?
    resolve_vote(ballots, cfg)              who (if anyone) is voted out?
    decide_after_vote(...)                  does the game end, and who won?
    parse_vote(text, me, names)             reading "VOTE: <Name>" from a reply
    find_name_violations / redact_names     the "no_names" rule
    clean_reply / limit_words               tidying model output

All the words the models see are in prompts.py.
"""

from __future__ import annotations

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
from .logger import GameLogger, render_markdown

SCHEMA_VERSION = 1


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


def decide_after_vote(eliminated_role: str | None, rounds_completed: int, rules) -> str:
    """'trio_win' | 'imposter_win' | 'no_decision' (game ends) | 'continue' (play on)."""
    if eliminated_role == "imposter":
        return "trio_win"
    if eliminated_role == "trio":
        return "imposter_win"
    if rules.on_no_decision == "continue" and rounds_completed < rules.max_rounds:
        return "continue"
    return "no_decision"


_VOTE_RE = re.compile(r"^[\s>*_`#-]*vote\s*[:：]\s*(.*)$", re.IGNORECASE)


def parse_vote(text: str, me: str, names: list[str]) -> tuple[str | None, str | None]:
    """Read the LAST 'VOTE: <Name>' line. Returns (target, None) or (None, problem)."""
    lines = (text or "").splitlines()
    for line in reversed(lines):
        m = _VOTE_RE.match(line)
        if not m:
            continue
        raw = m.group(1).strip().strip("*_`\"'[]()<>.,!;: ").strip()
        word = raw.split()[0].strip("*_`\"'[]()<>.,!;:") if raw else ""
        for n in names:
            if word.lower() == n.lower() or raw.lower() == n.lower():
                if n == me:
                    return None, f"you voted for yourself ({me}), which is not allowed."
                return n, None
        return None, f'"{raw}" is not one of the players you can vote for.'
    return None, 'your reply did not end with a line of the form "VOTE: <Name>".'


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

    def public_dict(self) -> dict:
        return {"name": self.name, "seat": self.seat, "role": self.role, "model": self.model,
                "display_name": self.display_name, "reasoning_effort": self.reasoning_effort}


class PlayerCallError(Exception):
    def __init__(self, player: str, purpose: str, error: BaseException):
        super().__init__(f"{player} ({purpose}): {error}")
        self.player = player
        self.error = error


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _new_game_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()


class Game:
    """One game, advanced one step at a time. See module docstring."""

    def __init__(self, cfg: GameConfig, client, on_event: Callable[[dict], None] | None = None,
                 log_dir: Path = Path("logs"), mock: bool = False, seed: int | None = None):
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

        # Seats
        names = list(self.rules.player_names)
        rng = random.Random(seed)
        imp_index = rng.randrange(len(names)) if self.rules.shuffle_seats else len(names) - 1
        self.players: list[Player] = []
        for i, name in enumerate(names):
            m = cfg.imposter if i == imp_index else cfg.trio
            role = "imposter" if i == imp_index else "trio"
            p = Player(name=name, seat=i + 1, role=role, model=m.model, display_name=m.display_name,
                       reasoning_effort=m.reasoning_effort, max_tokens=cfg.max_tokens_for(role))
            p.system_prompt = prompts.system_prompt(name, role, m.display_name, self.rules, names)
            self.players.append(p)
        self.by_name = {p.name: p for p in self.players}
        self.imposter = self.players[imp_index]
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

        # What players have publicly seen
        self._intros: list[tuple[str, str]] = []
        self._entries: list[dict] = []
        self._own_votes: dict[str, list[tuple[int, str | None]]] = {p.name: [] for p in self.players}

        self._emit("game_started", mock=mock, config=self._config_summary(),
                   players=[p.public_dict() for p in self.players])
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
            return f"Vote {self.vote_number + 1}"
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
        return self.cfg.summary()

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
                {"role": "user", "content": prompts.user_message(transcript, task, notes)}]

    def _call(self, player: Player, purpose: str, messages: list[dict], round_no: int | None):
        """One model call: run it, record it, emit api_call. Returns (CallResult, call_id)."""
        with self._lock:
            self._call_counter += 1
            call_id = f"c{self._call_counter:04d}"
        try:
            res = self.client.chat(player.model, messages, player.reasoning_effort, player.max_tokens,
                                   session_id=f"{self.game_id}-{player.name}")
        except Exception as e:
            with self._lock:
                self.calls.append({"call_id": call_id, "player": player.name, "purpose": purpose,
                                   "round": round_no, "model_requested": player.model,
                                   "user_message": messages[-1]["content"], "error": str(e)})
            raise PlayerCallError(player.name, purpose, e) from e

        reasoning_available = bool(res.reasoning and res.reasoning.strip())
        with self._lock:
            self.calls.append({
                "call_id": call_id, "player": player.name, "role": player.role, "purpose": purpose,
                "round": round_no, "model_requested": player.model, "model": res.model,
                "provider": res.provider, "reasoning_effort": player.reasoning_effort,
                "max_tokens": player.max_tokens, "max_tokens_used": res.max_tokens_used,
                "user_message": messages[1]["content"] if len(messages) > 1 else None,
                "extra_messages": messages[2:] if len(messages) > 2 else None,
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
            self.cost_by_model[player.model] = self.cost_by_model.get(player.model, 0.0) + (res.cost_usd or 0.0)
            self.reasoning_returned[player.model] = self.reasoning_returned.get(player.model, False) or reasoning_available
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

    def _call_text(self, player: Player, purpose: str, task: str, round_no: int | None):
        """A call whose reply is public text. Retries once if the reply is empty."""
        messages = self._messages(player, task)
        res, call_id = self._call(player, purpose, messages, round_no)
        if not (res.content or "").strip():
            retry = messages + [{"role": "assistant", "content": "(no reply)"},
                                {"role": "user", "content": prompts.empty_reply_retry_message()}]
            res, call_id = self._call(player, purpose + "_retry_empty", retry, round_no)
        return res, call_id

    def _public_text(self, player: Player, raw: str, call_id: str, word_limit: int) -> tuple[str, str]:
        """(text shown to players, original text). Applies cleanup and the name rule."""
        original = (raw or "").strip()
        text = limit_words(clean_reply(original, self.rules.player_names), word_limit) or "(no reply)"
        if self.rules.name_rule == "no_names":
            matches = find_name_violations(text, self.name_terms)
            if matches:
                redact = self.rules.redact_violations
                self._emit("rule_violation", player=player.name, call_id=call_id, matches=matches, redacted=redact)
                if redact:
                    text = redact_names(text, self.name_terms)
        return text, original

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
        trio = [p for p in self.players if p.role == "trio"]
        for p in trio:
            self._emit("thinking", player=p.name, action="intro", round=None)

        def write_intro(p: Player):
            res, call_id = self._call_text(p, "intro", prompts.intro_task(limit), None)
            text, original = self._public_text(p, res.content, call_id, limit)
            return text, original, res, call_id

        done = self._parallel(write_intro, trio)
        # The imposter writes last, after reading the (cleaned/redacted) trio intros in seat order.
        seen = [(p.name, done[p.name][0]) for p in trio]
        imp = self.imposter
        self._emit("thinking", player=imp.name, action="intro", round=None)
        res, call_id = self._call_text(imp, "intro", prompts.imposter_intro_task(seen, limit), None)
        text, original = self._public_text(imp, res.content, call_id, limit)
        done[imp.name] = (text, original, res, call_id)

        # Reveal all four in SEAT order.
        for p in self.players:
            text, original, res, call_id = done[p.name]
            self._emit("message", player=p.name, kind="intro", round=None, turn=None, question_id=None,
                       asker=None, text=text, original_text=original, reasoning=res.reasoning, call_id=call_id)
        self._intros = [(p.name, done[p.name][0]) for p in self.players]
        self._set_state("turn")

    def _do_turn(self) -> bool:
        r, t = self.round, self.turn
        asker = self.players[t - 1]
        qid = f"r{r}t{t}"
        if t == 1:
            self._emit("phase", phase="round", round=r, label=f"Round {r}")

        # 1. The question
        self._emit("thinking", player=asker.name, action="question", round=r)
        res, call_id = self._call_text(asker, "question", prompts.question_task(r, t, self.rules.question_word_limit), r)
        question, original = self._public_text(asker, res.content, call_id, self.rules.question_word_limit)
        self._emit("message", player=asker.name, kind="question", round=r, turn=t, question_id=qid,
                   asker=asker.name, text=question, original_text=original, reasoning=res.reasoning, call_id=call_id)

        # 2. Everyone answers
        order = self._answer_order(asker.name)
        limit = self.rules.answer_word_limit
        answers: list[tuple[str, str]] = []

        def answer(p: Player, earlier: list[tuple[str, str]] | None):
            sequential = self.rules.answer_mode == "sequential"
            task = prompts.answer_task(asker.name, question, p.name, limit, earlier, sequential)
            res, call_id = self._call_text(p, "answer", task, r)
            text, original = self._public_text(p, res.content, call_id, limit)
            return text, original, res, call_id

        def show(p: Player, result) -> None:
            text, original, res, call_id = result
            self._emit("message", player=p.name, kind="answer", round=r, turn=t, question_id=qid,
                       asker=asker.name, text=text, original_text=original, reasoning=res.reasoning,
                       call_id=call_id)
            answers.append((p.name, text))

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

        self._entries.append({"kind": "turn", "round": r, "turn": t, "asker": asker.name,
                              "question": question, "answers": answers})

        # 3. Advance
        self.turn += 1
        if self.turn <= len(self.players):
            return False
        self.rounds_completed = r
        self.round += 1
        self.turn = 1
        self._set_state("vote" if vote_due(self.rounds_completed, self.rules) else "turn")
        return True

    def _cast_vote(self, p: Player, number: int) -> dict:
        valid_names = [x.name for x in self.players if x is not p]
        threshold = self.rules.vote_threshold
        messages = self._messages(p, prompts.vote_task(number, p.name, valid_names, threshold))
        res, call_id = self._call(p, "vote", messages, self.rounds_completed)
        target, problem = parse_vote(res.content, p.name, self.rules.player_names)
        first_problem = problem
        if target is None:
            retry = messages + [{"role": "assistant", "content": res.content or "(no reply)"},
                                {"role": "user", "content": prompts.vote_retry_message(problem, valid_names)}]
            res, call_id = self._call(p, "vote_retry", retry, self.rounds_completed)
            target, problem = parse_vote(res.content, p.name, self.rules.player_names)
        return {"target": target, "valid": target is not None, "raw_text": res.content,
                "reasoning": strip_vote_line(res.content) or None, "model_reasoning": res.reasoning,
                "call_id": call_id, "retried": first_problem is not None,
                "problem": problem if target is None else first_problem}

    def _do_vote(self) -> None:
        self.vote_number += 1
        n = self.vote_number
        self._emit("phase", phase="vote", round=self.rounds_completed, label=f"Vote {n}")
        for p in self.players:
            self._emit("thinking", player=p.name, action="vote", round=self.rounds_completed)
        results = self._parallel(lambda p: self._cast_vote(p, n), self.players)

        ballots: dict[str, str | None] = {}
        for p in self.players:
            v = results[p.name]
            ballots[p.name] = v["target"]
            self._own_votes[p.name].append((n, v["target"]))
            self._emit("vote_cast", player=p.name, target=v["target"], reasoning=v["reasoning"],
                       raw_text=v["raw_text"], valid=v["valid"], call_id=v["call_id"], vote_number=n,
                       retried=v["retried"], problem=v["problem"], model_reasoning=v["model_reasoning"])

        outcome = resolve_vote(ballots, self.cfg)
        elim_role = self.by_name[outcome.eliminated].role if outcome.eliminated else None
        decision = decide_after_vote(elim_role, self.rounds_completed, self.rules)
        self._emit("vote_result", vote_number=n, tally=outcome.tally, ballots=ballots,
                   eliminated=outcome.eliminated, eliminated_role=elim_role, decision=decision,
                   rounds_completed=self.rounds_completed)

        trio_names = {p.name for p in self.players if p.role == "trio"}
        self.votes.append({
            "vote_number": n, "rounds_completed": self.rounds_completed, "tally": outcome.tally,
            "ballots": ballots, "eliminated": outcome.eliminated, "eliminated_role": elim_role,
            "decision": decision,
            "imposter_votes_from_trio": sum(1 for v, t in ballots.items()
                                            if v in trio_names and t == self.imposter.name),
            "trio_valid_ballots": sum(1 for v, t in ballots.items() if v in trio_names and t is not None),
        })
        self._entries.append({
            "kind": "vote", "vote_number": n, "tally": outcome.tally,
            "ballots": ballots if self.rules.reveal_votes else None,
            "result_text": prompts.vote_result_text(outcome.eliminated, outcome.tally,
                                                    self.rules.vote_threshold, decision == "continue"),
        })

        if decision == "continue":
            self._set_state("turn")
            return
        winner = {"trio_win": "trio", "imposter_win": "imposter"}.get(decision)
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
                       eliminated_role=eliminated_role, rounds_survived=rounds_survived,
                       rounds_completed=self.rounds_completed, total_cost_usd=round(self.total_cost_usd, 8),
                       total_input_tokens=self.total_input_tokens, total_output_tokens=self.total_output_tokens,
                       num_api_calls=len(self.calls),
                       log_file=str(self.logger.json_path) if self._started else None,
                       transcript_file=str(self.logger.md_path) if self._started else None, reason=reason,
                       imposter=self.imposter.name)
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
                "state": {"state": self._state, "round": self.round, "turn": self.turn,
                          "rounds_completed": self.rounds_completed, "vote_number": self.vote_number},
                "outcome": {"outcome": self.outcome, "winner": self.winner, "eliminated": self.eliminated,
                            "eliminated_role": self.eliminated_role, "rounds_survived": self.rounds_survived,
                            "rounds_completed": self.rounds_completed, "reason": self.end_reason},
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
        imposter_rounds = (self.rounds_survived if self.eliminated_role == "imposter" else self.rounds_completed)
        return {
            "game_id": self.game_id, "timestamp": self.finished_at, "mock": self.mock, "seed": self.seed,
            "trio_model": cfg.trio.model, "imposter_model": cfg.imposter.model,
            "trio_display_name": cfg.trio.display_name, "imposter_display_name": cfg.imposter.display_name,
            "trio_effort": cfg.trio.reasoning_effort, "imposter_effort": cfg.imposter.reasoning_effort,
            "name_rule": self.rules.name_rule, "answer_mode": self.rules.answer_mode,
            "rounds_before_vote": self.rules.rounds_before_vote, "vote_threshold": self.rules.vote_threshold,
            "on_no_decision": self.rules.on_no_decision,
            "imposter_seat": self.imposter.name,
            "outcome": self.outcome, "winner": self.winner, "winner_model": winner_model,
            "eliminated": self.eliminated, "eliminated_role": self.eliminated_role,
            "eliminated_model": elim_model, "rounds_survived": self.rounds_survived,
            "imposter_rounds_survived": imposter_rounds,
            "rounds_completed": self.rounds_completed,
            "votes": [{"vote_number": v["vote_number"], "rounds_completed": v["rounds_completed"],
                       "tally": v["tally"], "ballots": v["ballots"], "eliminated": v["eliminated"]}
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
