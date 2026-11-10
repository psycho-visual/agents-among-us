"""Everything the players (the AI models) read is built here, from editable templates.

Where the words live
--------------------
* Default texts: `imposter_game/default_prompts/<id>.txt` (plain text, shipped with the game).
* Your edits:    `prompts/<id>.txt` in the repo root. A missing file means "use the default".
  Edit them in the browser (Prompts button) or with any text editor. Changes apply
  to the NEXT game (templates are loaded when a Game is created, never mid-game).

Templates use `{placeholder}` fields (Python str.format style); write `{{` and `}}`
for literal braces. Which placeholders each template may use is listed in
TEMPLATES below (and shown in the GUI). `phrases.txt` is special: it holds short
one-line phrases (`name = text`) that are slotted into the bigger templates.

How a call is built
-------------------
Each model call is stateless and has two messages:
1. system: role section (differs per player) + rules (same for every player) + output rules.
   The role section comes first so each player reads who it is before the rules.
2. user:   the public transcript so far (append-only, so prompt caching works), then a one-line
           private role reminder (reminder_* templates), then the task.

Fairness notes for anyone editing the texts:
* Trio and imposter receive the same rules text; only the role section differs.
* Never reveal anyone's role or model in public text.
* Nothing should hint to the trio who the imposter is or which model it is.

The transcript layout (headers, "Red answered: ...", vote results) is built in
code below and is not editable; the engine never parses any editable text.

Public API used by the GUI server:
    list_templates(), save_template(id, text), reset_template(id), validate_template(id, text),
    preview(cfg), prompt_version(templates)
All accept `prompts_dir=` to use another overrides folder (tests use a temp dir).
"""

from __future__ import annotations

import hashlib
import json
import string
from dataclasses import dataclass, field
from pathlib import Path

from .config import REPO_ROOT

DEFAULTS_DIR = Path(__file__).resolve().parent / "default_prompts"
_prompts_dir: Path = REPO_ROOT / "prompts"

# Structural transcript markers (not editable).
TRANSCRIPT_START = "=== PUBLIC TRANSCRIPT (everything all players have seen so far) ==="
TRANSCRIPT_END = "=== END OF TRANSCRIPT ==="

# The exact lines the answer/vote parsers look for (parsing itself lives in game.py).
CALL_VOTE_YES = "CALL VOTE: YES"
CALL_VOTE_NO = "CALL VOTE: NO"
NOBODY = "NOBODY"


def set_prompts_dir(path: str | Path | None) -> None:
    """Change where user overrides are read/written (None = <repo>/prompts)."""
    global _prompts_dir
    _prompts_dir = Path(path) if path else REPO_ROOT / "prompts"


def get_prompts_dir() -> Path:
    return _prompts_dir


# =============================================================================
# Registry: what each template is for and which placeholders it may use
# =============================================================================

@dataclass
class TemplateInfo:
    id: str
    title: str
    group: str            # "Rules" | "Roles" | "Tasks" | "Other"
    description: str
    placeholders: dict[str, str] = field(default_factory=dict)
    required_markers: list[str] = field(default_factory=list)


_P_RULES = {
    "seat_names": "The four seat names, e.g. 'Red, Blue, Green and Yellow'.",
    "answering": "How answers are given (phrase answering_simultaneous / answering_sequential).",
    "votes_intro": "How votes are held (phrase votes_intro_scheduled_only / votes_intro_with_calls).",
    "vote_threshold": "Votes needed to be voted out, e.g. 3.",
    "reveal_votes": "Whether ballots are shown (phrase reveal_votes_on / reveal_votes_off).",
    "no_decision": "What happens when nobody is voted out (phrase no_decision_...).",
    "vote_calls_section": "The VOTE CALLS section, or empty when players cannot call votes.",
    "no_imposter_section": "The MAYBE NO IMPOSTER section, or empty when no_imposter_chance is 0.",
    "name_rule": "The name rule (template name_rule_lie_allowed or name_rule_no_names).",
}
_P_ROLE = {
    "name": "This player's seat name.",
    "others": "The other three seat names, e.g. 'Blue, Green and Yellow'.",
    "model_name": "The model's display name, e.g. 'Claude Haiku 5.5'.",
}
_P_ANSWER = {
    "who": "Who asked (phrase answer_who_self / answer_who_other).",
    "asker": "Seat name of the player who asked.",
    "question": "The question text.",
    "timing": "How answering works (phrase answer_timing_...).",
    "earlier_answers": "Sequential mode only: the answers already given (with surrounding line breaks), else empty.",
    "word_limit": "Maximum words for the answer.",
}

TEMPLATES: dict[str, TemplateInfo] = {t.id: t for t in [
    TemplateInfo("rules", "Game rules", "Rules",
                 "The rules every player gets at the start of its system prompt (identical for all players).",
                 _P_RULES),
    TemplateInfo("vote_calls_section", "Rules: vote calls", "Rules",
                 "Explains player-called votes. Only used when player_vote_calls = true.",
                 {"vote_call_threshold": "Requests needed in one turn to call a vote.",
                  "vote_threshold": "Votes needed to be voted out.",
                  "vote_requests_visibility": "Phrase vote_requests_public / vote_requests_secret.",
                  "called_vote_fail_note": "Phrase called_vote_fail_end_note (with a leading space) or empty."}),
    TemplateInfo("no_imposter_section", "Rules: maybe no imposter", "Rules",
                 "Explains that a game may have no imposter. Only used when no_imposter_chance > 0.",
                 {"no_imposter_percent": "The chance as a percentage, e.g. '10'."}),
    TemplateInfo("name_rule_lie_allowed", "Name rule: lying allowed", "Rules",
                 "Used when name_rule = \"lie_allowed\".", {}),
    TemplateInfo("name_rule_no_names", "Name rule: no names", "Rules",
                 "Used when name_rule = \"no_names\".",
                 {"redaction": "Phrase redaction_on / redaction_off."}),
    TemplateInfo("output_rules", "Output rules", "Rules",
                 "Formatting rules at the end of every system prompt.", {}),
    TemplateInfo("role_trio", "Role: trio member", "Roles",
                 "Private role text for a trio member (used when no_imposter_chance is 0).", _P_ROLE),
    TemplateInfo("role_trio_maybe_no_imposter", "Role: trio member (maybe no imposter)", "Roles",
                 "Private role text for a trio member when the game might have no imposter.", _P_ROLE),
    TemplateInfo("role_imposter", "Role: imposter", "Roles",
                 "Private role text for the imposter.",
                 {"name": _P_ROLE["name"], "others": _P_ROLE["others"],
                  "own_model_sentence": "Phrase imposter_own_model plus a space, or empty when "
                                        "imposter_knows_own_model = false."}),
    TemplateInfo("reminder_trio", "Reminder: trio member", "Roles",
                 "A one-line private reminder of a trio member's role, placed just above every task.",
                 {"own_model": "The trio model's display name, e.g. 'Claude Haiku 5.5'.",
                  "others": _P_ROLE["others"]}),
    TemplateInfo("reminder_trio_maybe_no_imposter", "Reminder: trio member (maybe no imposter)", "Roles",
                 "The trio reminder used when the game might have no imposter (no_imposter_chance > 0).",
                 {"own_model": "The trio model's display name, e.g. 'Claude Haiku 5.5'.",
                  "others": _P_ROLE["others"]}),
    TemplateInfo("reminder_imposter", "Reminder: imposter", "Roles",
                 "A one-line private reminder of the imposter's role, placed just above every task. "
                 "(It never names the imposter's own model.)",
                 {"others": _P_ROLE["others"]}),
    TemplateInfo("task_intro", "Task: introduction", "Tasks",
                 "Asks a player to write its introduction blind (trio members; everyone in a no-imposter game).",
                 {"intro_timing": "Phrase intro_timing / intro_timing_maybe_no_imposter.",
                  "word_limit": "Maximum words."}),
    TemplateInfo("task_imposter_intro", "Task: imposter introduction", "Tasks",
                 "Asks the imposter for its introduction after showing it the trio introductions.",
                 {"trio_intros": "The three trio introductions, one per line ('Red: ...').",
                  "word_limit": "Maximum words."}),
    TemplateInfo("task_question", "Task: ask a question", "Tasks",
                 "Asks a player to ask the question for its turn.",
                 {"round": "Round number.", "turn": "Question number within the round (1-4).",
                  "word_limit": "Maximum words."}),
    TemplateInfo("task_answer", "Task: answer a question", "Tasks",
                 "Asks a player to answer a question (when players cannot call votes).", _P_ANSWER),
    TemplateInfo("task_answer_vote_calls", "Task: answer a question (vote calls on)", "Tasks",
                 "Asks a player to answer and to add a CALL VOTE: YES/NO line (when player_vote_calls = true).",
                 _P_ANSWER, ["CALL VOTE:"]),
    TemplateInfo("task_vote", "Task: vote", "Tasks",
                 "Asks a player for its secret vote. The reply must end with 'VOTE: <Name>'.",
                 {"trigger_note": "Vote calls on: why this vote happens plus a line break (phrases vote_trigger_*, "
                                  "vote_fail_*); otherwise empty.",
                  "vote_number": "Vote number.", "vote_threshold": "Votes needed to be voted out.",
                  "name": "This player's seat name.",
                  "valid_choices": "Names this player may vote for, e.g. 'Blue, Green, Yellow'.",
                  "nobody_note": "Phrase vote_nobody_option plus a line break when NOBODY is allowed, else empty."},
                 ["VOTE:"]),
    TemplateInfo("task_vote_retry", "Task: vote again (invalid vote)", "Tasks",
                 "Sent once when a vote could not be read. The reply must end with 'VOTE: <Name>'.",
                 {"problem": "What was wrong with the vote.",
                  "valid_choices": "Names this player may vote for.",
                  "nobody_note": "Phrase vote_nobody_option plus a line break when NOBODY is allowed, else empty."},
                 ["VOTE:"]),
    TemplateInfo("paraphrase", "Paraphrase instruction", "Other",
                 "Sent to the [paraphrase] model to rewrite each public message (only when paraphrase is enabled).",
                 {"text": "The message to rewrite."}),
    TemplateInfo("phrases", "Short phrases", "Other",
                 "Short one-line phrases slotted into the templates above. Format: one 'name = text' per line; "
                 "lines starting with # are comments. A missing name uses its default text.", {}),
]}

# phrase name -> (description, allowed placeholders)
PHRASES: dict[str, tuple[str, tuple[str, ...]]] = {
    "answering_simultaneous": ("Rules, simultaneous answers.", ()),
    "answering_sequential": ("Rules, sequential answers.", ()),
    "votes_intro_scheduled_only": ("Rules, votes when players cannot call votes.", ("rounds_word",)),
    "votes_intro_with_calls": ("Rules, votes when players can call votes.", ("rounds_word",)),
    "reveal_votes_on": ("Rules, reveal_votes = true.", ()),
    "reveal_votes_off": ("Rules, reveal_votes = false.", ()),
    "no_decision_end": ("Rules, on_no_decision = end.", ("vote_threshold",)),
    "no_decision_end_with_calls": ("Rules, on_no_decision = end with vote calls.", ("vote_threshold",)),
    "no_decision_continue": ("Rules, on_no_decision = continue.", ("vote_threshold", "rounds_word", "max_rounds")),
    "redaction_on": ("No-names rule, redact_violations = true.", ()),
    "redaction_off": ("No-names rule, redact_violations = false.", ()),
    "vote_requests_public": ("Vote calls, reveal_vote_calls = true.", ()),
    "vote_requests_secret": ("Vote calls, reveal_vote_calls = false.", ()),
    "called_vote_fail_end_note": ("Vote calls with on_no_decision = end.", ()),
    "imposter_own_model": ("Imposter role, imposter_knows_own_model = true.", ("model_name",)),
    "intro_timing": ("Intro task, normal games.", ()),
    "intro_timing_maybe_no_imposter": ("Intro task when the game may have no imposter.", ()),
    "answer_who_self": ("Answer task, the asker answering its own question.", ()),
    "answer_who_other": ("Answer task, someone else's question.", ("asker",)),
    "answer_timing_simultaneous": ("Answer task, simultaneous mode.", ()),
    "answer_timing_sequential_first": ("Answer task, sequential mode, first to answer.", ()),
    "answer_timing_sequential_later": ("Answer task, sequential mode, answers already given.", ()),
    "vote_trigger_called": ("Vote task, a vote called by the players.", ("count",)),
    "vote_trigger_called_and_scheduled": ("Vote task, called and scheduled at once.", ("count",)),
    "vote_trigger_scheduled": ("Vote task, a scheduled vote.", ()),
    "vote_fail_continues": ("Vote task, a failed vote continues the game.", ()),
    "vote_fail_ends": ("Vote task, a failed vote ends the game.", ()),
    "vote_fail_ends_maybe_no_imposter": ("Vote task, a failed vote ends the game (maybe no imposter).", ()),
    "vote_nobody_option": ("Vote task, when voting NOBODY is allowed.", ()),
    "empty_reply_retry": ("Sent when a model's reply was empty.", ()),
}


# =============================================================================
# Loading, saving, validating
# =============================================================================

def _norm(text: str) -> str:
    """Normalise line endings and drop trailing newlines (editors add them)."""
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


def default_text(template_id: str) -> str:
    return _norm((DEFAULTS_DIR / f"{template_id}.txt").read_text(encoding="utf-8"))


def _override_path(template_id: str, prompts_dir: str | Path | None) -> Path:
    return Path(prompts_dir or _prompts_dir) / f"{template_id}.txt"


def _override_text(template_id: str, prompts_dir) -> str | None:
    p = _override_path(template_id, prompts_dir)
    try:
        return _norm(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None


def _placeholders(text: str) -> list[str]:
    """Field names used in a template (raises ValueError on malformed braces)."""
    names = []
    for _literal, fname, _spec, _conv in string.Formatter().parse(text):
        if fname is not None:
            names.append(fname)
    return names


def parse_phrases(text: str) -> tuple[dict[str, str], list[str]]:
    """Parse phrases.txt -> ({name: text}, [problems])."""
    out: dict[str, str] = {}
    problems: list[str] = []
    for i, raw in enumerate(_norm(text).split("\n"), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            problems.append(f"line {i}: expected 'name = text', got {line[:40]!r}")
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out, problems


def validate_template(template_id: str, text: str) -> dict:
    """{"errors": [...], "warnings": [...]}. Errors block saving."""
    errors: list[str] = []
    warnings: list[str] = []
    info = TEMPLATES.get(template_id)
    if info is None:
        return {"errors": [f"Unknown template {template_id!r}."], "warnings": []}
    text = _norm(text if isinstance(text, str) else "")
    if not text.strip():
        return {"errors": ["The template is empty. Use 'Reset to default' to restore it."], "warnings": []}

    if template_id == "phrases":
        phrases, problems = parse_phrases(text)
        errors += problems
        for key, value in phrases.items():
            if key not in PHRASES:
                errors.append(f"Unknown phrase name {key!r}.")
                continue
            try:
                used = _placeholders(value)
            except ValueError as e:
                errors.append(f"Phrase {key!r}: malformed braces ({e}). Write {{{{ and }}}} for literal braces.")
                continue
            for u in used:
                if u not in PHRASES[key][1]:
                    allowed = ", ".join("{" + a + "}" for a in PHRASES[key][1]) or "none"
                    errors.append(f"Phrase {key!r}: unknown placeholder {{{u}}} (allowed: {allowed}).")
        missing = [k for k in PHRASES if k not in phrases]
        if missing:
            warnings.append("These phrases are missing and will use their default text: " + ", ".join(missing))
        return {"errors": errors, "warnings": warnings}

    try:
        used = _placeholders(text)
    except ValueError as e:
        return {"errors": [f"Malformed braces: {e}. Write {{{{ and }}}} for literal braces."], "warnings": []}
    allowed = info.placeholders
    for u in dict.fromkeys(used):
        if u not in allowed:
            names = ", ".join("{" + a + "}" for a in allowed) or "none"
            errors.append(f"Unknown placeholder {{{u}}}. Allowed here: {names}.")
    for name in allowed:
        if name not in used:
            warnings.append(f"Placeholder {{{name}}} is not used.")
    for marker in info.required_markers:
        if marker not in text:
            warnings.append(f"The text should contain {marker!r}: the game reads the model's reply by looking "
                            f"for it, so players need to be asked to write it.")
    return {"errors": errors, "warnings": warnings}


def load_templates(prompts_dir: str | Path | None = None) -> tuple[dict[str, str], list[str]]:
    """Effective templates {id: text} + warnings (invalid overrides fall back to the default)."""
    texts: dict[str, str] = {}
    warnings: list[str] = []
    for tid in TEMPLATES:
        default = default_text(tid)
        override = _override_text(tid, prompts_dir)
        if override is not None and override != default:
            check = validate_template(tid, override)
            if check["errors"]:
                warnings.append(f"Prompt override {tid!r} has errors, using the default: " + "; ".join(check["errors"]))
                override = None
        texts[tid] = override if override is not None else default
    return texts, warnings


def list_templates(prompts_dir: str | Path | None = None) -> list[dict]:
    effective, _ = load_templates(prompts_dir)
    out = []
    for tid, info in TEMPLATES.items():
        default = default_text(tid)
        out.append({
            "id": tid, "title": info.title, "description": info.description, "group": info.group,
            "placeholders": [{"name": k, "description": v} for k, v in info.placeholders.items()],
            "required_markers": list(info.required_markers),
            "text": effective[tid], "default_text": default, "modified": effective[tid] != default,
        })
    return out


def save_template(template_id: str, text: str, prompts_dir: str | Path | None = None) -> dict:
    """Validate and save an override (or delete it if the text equals the default)."""
    check = validate_template(template_id, text)
    if check["errors"]:
        return {"ok": False, "errors": check["errors"], "warnings": check["warnings"]}
    text = _norm(text)
    path = _override_path(template_id, prompts_dir)
    if text == default_text(template_id):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        modified = False
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
        modified = True
    return {"ok": True, "errors": [], "warnings": check["warnings"], "modified": modified}


def reset_template(template_id: str, prompts_dir: str | Path | None = None) -> dict:
    if template_id not in TEMPLATES:
        return {"ok": False, "errors": [f"Unknown template {template_id!r}."], "warnings": []}
    try:
        _override_path(template_id, prompts_dir).unlink()
    except FileNotFoundError:
        pass
    return {"ok": True, "errors": [], "warnings": [], "modified": False}


def prompts_modified(prompts_dir: str | Path | None = None) -> bool:
    return any(t["modified"] for t in list_templates(prompts_dir))


def prompt_version(templates=None, prompts_dir: str | Path | None = None) -> str:
    """8-hex hash of all effective template texts. Accepts {id: text}, list_templates() output, or None."""
    if templates is None:
        templates, _ = load_templates(prompts_dir)
    elif isinstance(templates, list):
        templates = {t["id"]: t.get("text", "") for t in templates}
    blob = json.dumps({k: _norm(v) for k, v in sorted(templates.items())}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:8]


# =============================================================================
# Rendering
# =============================================================================

class _Safe(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _join(names: list[str]) -> str:
    """'A', 'A and B', 'A, B and C'."""
    names = list(names)
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


class PromptSet:
    """A fixed set of templates (loaded once per game) plus the render functions."""

    def __init__(self, templates: dict[str, str] | None = None, prompts_dir: str | Path | None = None):
        self.warnings: list[str] = []
        if templates is None:
            templates, self.warnings = load_templates(prompts_dir)
        self.templates = dict(templates)
        self.version = prompt_version(self.templates)
        self.modified = any(self.templates.get(t) != default_text(t) for t in TEMPLATES)
        defaults, _ = parse_phrases(default_text("phrases"))
        mine, _ = parse_phrases(self.templates.get("phrases", ""))
        self.phrases = {**defaults, **{k: v for k, v in mine.items() if k in PHRASES}}

    # -- low level -------------------------------------------------------------
    def render(self, template_id: str, **values) -> str:
        text = self.templates.get(template_id) or default_text(template_id)
        try:
            return text.format_map(_Safe(values))
        except (ValueError, KeyError, AttributeError, IndexError):
            return default_text(template_id).format_map(_Safe(values))

    def phrase(self, name: str, **values) -> str:
        text = self.phrases.get(name, "")
        try:
            return text.format_map(_Safe(values))
        except (ValueError, KeyError, AttributeError, IndexError):
            defaults, _ = parse_phrases(default_text("phrases"))
            return defaults.get(name, "").format_map(_Safe(values))

    # -- system prompt -----------------------------------------------------------
    def rules_text(self, rules, seat_names: list[str]) -> str:
        n = rules.rounds_before_vote
        rounds_word = "round" if n == 1 else f"{n} rounds"
        calls_on = bool(getattr(rules, "player_vote_calls", False))
        chance = float(getattr(rules, "no_imposter_chance", 0.0) or 0.0)
        if rules.on_no_decision == "end":
            no_decision = self.phrase("no_decision_end_with_calls" if calls_on else "no_decision_end",
                                      vote_threshold=rules.vote_threshold)
        else:
            no_decision = self.phrase("no_decision_continue", vote_threshold=rules.vote_threshold,
                                      rounds_word=rounds_word, max_rounds=rules.max_rounds)
        vote_calls_section = ""
        if calls_on:
            note = (" " + self.phrase("called_vote_fail_end_note")) if rules.on_no_decision == "end" else ""
            vote_calls_section = "\n" + self.render(
                "vote_calls_section", vote_call_threshold=rules.vote_call_threshold,
                vote_threshold=rules.vote_threshold, called_vote_fail_note=note,
                vote_requests_visibility=self.phrase("vote_requests_public" if rules.reveal_vote_calls
                                                     else "vote_requests_secret")) + "\n"
        no_imposter_section = ""
        if chance > 0:
            no_imposter_section = "\n" + self.render("no_imposter_section",
                                                     no_imposter_percent=f"{chance * 100:g}") + "\n"
        if rules.name_rule == "no_names":
            name_rule = self.render("name_rule_no_names", redaction=self.phrase(
                "redaction_on" if rules.redact_violations else "redaction_off"))
        else:
            name_rule = self.render("name_rule_lie_allowed")
        return self.render(
            "rules", seat_names=_join(seat_names),
            answering=self.phrase("answering_simultaneous" if rules.answer_mode == "simultaneous"
                                  else "answering_sequential"),
            votes_intro=self.phrase("votes_intro_with_calls" if calls_on else "votes_intro_scheduled_only",
                                    rounds_word=rounds_word),
            vote_threshold=rules.vote_threshold,
            reveal_votes=self.phrase("reveal_votes_on" if rules.reveal_votes else "reveal_votes_off"),
            no_decision=no_decision, vote_calls_section=vote_calls_section,
            no_imposter_section=no_imposter_section, name_rule=name_rule)

    def role_text(self, name: str, role: str, model_display_name: str, other_names: list[str], rules) -> str:
        others = _join(other_names)
        if role == "imposter":
            knows = bool(getattr(rules, "imposter_knows_own_model", True))
            own = (self.phrase("imposter_own_model", model_name=model_display_name) + " ") if knows else ""
            return self.render("role_imposter", name=name, others=others, own_model_sentence=own)
        maybe = float(getattr(rules, "no_imposter_chance", 0.0) or 0.0) > 0
        return self.render("role_trio_maybe_no_imposter" if maybe else "role_trio",
                           name=name, others=others, model_name=model_display_name)

    def system_prompt(self, name: str, role: str, model_display_name: str, rules, seat_names: list[str]) -> str:
        """Role section first (who you are), then the shared rules, then output rules."""
        others = [n for n in seat_names if n != name]
        return "\n\n".join([self.role_text(name, role, model_display_name, others, rules),
                            self.rules_text(rules, seat_names),
                            self.render("output_rules")])

    def reminder(self, name: str, role: str, model_display_name: str, seat_names: list[str], rules) -> str:
        """The private one-line role reminder placed just above every task."""
        others = _join([n for n in seat_names if n != name])
        if role == "imposter":
            return self.render("reminder_imposter", others=others)
        maybe = float(getattr(rules, "no_imposter_chance", 0.0) or 0.0) > 0
        return self.render("reminder_trio_maybe_no_imposter" if maybe else "reminder_trio",
                           own_model=model_display_name, others=others)

    # -- tasks -------------------------------------------------------------------
    def intro_task(self, word_limit: int, maybe_no_imposter: bool = False) -> str:
        timing = self.phrase("intro_timing_maybe_no_imposter" if maybe_no_imposter else "intro_timing")
        return self.render("task_intro", intro_timing=timing, word_limit=word_limit)

    def imposter_intro_task(self, trio_intros: list[tuple[str, str]], word_limit: int) -> str:
        shown = "\n".join(f"{name}: {text}" for name, text in trio_intros)
        return self.render("task_imposter_intro", trio_intros=shown, word_limit=word_limit)

    def question_task(self, round_no: int, turn_no: int, word_limit: int) -> str:
        return self.render("task_question", round=round_no, turn=turn_no, word_limit=word_limit)

    def answer_task(self, asker: str, question: str, me: str, word_limit: int,
                    earlier_answers: list[tuple[str, str]] | None, sequential: bool,
                    vote_calls: bool = False) -> str:
        who = self.phrase("answer_who_self") if asker == me else self.phrase("answer_who_other", asker=asker)
        before = ""
        if not sequential:
            timing = self.phrase("answer_timing_simultaneous")
        elif earlier_answers:
            timing = self.phrase("answer_timing_sequential_later")
            before = "\n" + "\n".join(f"  {n} answered: {t}" for n, t in earlier_answers) + "\n"
        else:
            timing = self.phrase("answer_timing_sequential_first")
        return self.render("task_answer_vote_calls" if vote_calls else "task_answer", who=who, asker=asker,
                           question=question, timing=timing, earlier_answers=before, word_limit=word_limit)

    def vote_trigger_note(self, trigger: str, count: int | None, no_decision_continues: bool,
                          maybe_no_imposter: bool = False) -> str:
        if trigger == "called":
            why = self.phrase("vote_trigger_called", count=count)
        elif trigger == "called+scheduled":
            why = self.phrase("vote_trigger_called_and_scheduled", count=count)
        else:
            why = self.phrase("vote_trigger_scheduled")
        if no_decision_continues:
            then = self.phrase("vote_fail_continues")
        else:
            then = self.phrase("vote_fail_ends_maybe_no_imposter" if maybe_no_imposter else "vote_fail_ends")
        return f"{why} {then}\n"

    def _nobody_note(self, allow_nobody: bool) -> str:
        return (self.phrase("vote_nobody_option") + "\n") if allow_nobody else ""

    @staticmethod
    def _choices(valid: list[str], allow_nobody: bool) -> str:
        return ", ".join(list(valid) + ([NOBODY] if allow_nobody else []))

    def vote_task(self, vote_number: int, me: str, valid: list[str], threshold: int, note: str = "",
                  allow_nobody: bool = False) -> str:
        return self.render("task_vote", trigger_note=note, vote_number=vote_number, vote_threshold=threshold,
                           name=me, valid_choices=self._choices(valid, allow_nobody),
                           nobody_note=self._nobody_note(allow_nobody))

    def vote_retry_message(self, problem: str, valid: list[str], allow_nobody: bool = False) -> str:
        return self.render("task_vote_retry", problem=problem, valid_choices=self._choices(valid, allow_nobody),
                           nobody_note=self._nobody_note(allow_nobody))

    def empty_reply_retry_message(self) -> str:
        return self.phrase("empty_reply_retry")

    def paraphrase_request(self, text: str) -> str:
        return self.render("paraphrase", text=text)


# =============================================================================
# Transcript (structural, not editable)
# =============================================================================

def render_transcript(intros: list[tuple[str, str]], entries: list[dict]) -> str:
    """Render what all players have publicly seen.

    intros:  [(name, text)] in seat order (empty before introductions are revealed).
    entries: chronological list of dicts, each either
             {"kind": "turn", "round", "turn", "asker", "question", "answers": [(name, text)],
              "requesters": [names] | None (None = vote requests not shown)}
             {"kind": "vote", "vote_number", "ballots": {voter: target|None|"NOBODY"} | None,
              "tally": {name: n}, "result_text": str,
              "called_count": int | None (set when players called this vote), "num_players": int}
    """
    lines = [TRANSCRIPT_START]
    if not intros and not entries:
        lines.append("(Nothing has happened yet.)")
    if intros:
        lines.append("")
        lines.append("--- Introductions (shown in seat order) ---")
        for name, text in intros:
            lines.append(f"{name}: {text}")
    current_round = None
    for e in entries:
        if e["kind"] == "turn":
            if e["round"] != current_round:
                current_round = e["round"]
                lines.append("")
                lines.append(f"--- Round {current_round} ---")
            lines.append("")
            lines.append(f"Question {e['turn']} of round {e['round']}, asked by {e['asker']}: {e['question']}")
            for name, text in e["answers"]:
                lines.append(f"  {name} answered: {text}")
            if e.get("requesters") is not None:
                lines.append(f"  Vote requests: {', '.join(e['requesters']) if e['requesters'] else 'none'}")
        elif e["kind"] == "vote":
            lines.append("")
            if e.get("called_count") is not None:
                lines.append(f"The players called a vote ({e['called_count']} of {e.get('num_players', 4)} "
                             "requested one).")
            lines.append(f"--- Vote {e['vote_number']} ---")
            if e.get("ballots") is not None:
                for voter, target in e["ballots"].items():
                    shown = target if target else "nobody (abstained)"
                    lines.append(f"  {voter} voted for {shown}")
            lines.append(f"  Result: {e['result_text']}")
    lines.append(TRANSCRIPT_END)
    return "\n".join(lines)


def vote_result_text(eliminated: str | None, tally: dict, threshold: int, continues: bool) -> str:
    """Public one-line announcement of a vote result (never reveals roles of non-eliminated players)."""
    counts = ", ".join(f"{n} {c}" for n, c in tally.items())
    if eliminated:
        return f"{eliminated} was voted out ({counts})."
    tail = " The game continues." if continues else ""
    return f"Nobody reached {threshold} votes ({counts}). Nobody was voted out.{tail}"


def private_notes(own_votes: list[tuple[int, str | None]]) -> str:
    """Things only this player knows (used when votes are not revealed)."""
    if not own_votes:
        return ""
    parts = [f"in vote {n} you voted for {t or 'nobody (abstained)'}" for n, t in own_votes]
    return "Your private notes: " + "; ".join(parts) + "."


def user_message(transcript: str, task: str, notes: str = "", reminder: str = "") -> str:
    """Transcript first (append-only, cache friendly), then private notes, the role reminder, and the task."""
    parts = [transcript]
    if notes:
        parts.append(notes)
    if reminder:
        parts.append(reminder)
    parts.append(task)
    return "\n\n".join(parts)


# =============================================================================
# Preview (for the GUI)
# =============================================================================

def preview(cfg, prompts_dir: str | Path | None = None) -> dict:
    """Render the prompts for sample players/data with the given GameConfig."""
    ps = PromptSet(prompts_dir=prompts_dir)
    rules = cfg.rules
    names = list(rules.player_names)
    trio_name, imp_name = names[0], names[1]
    calls_on = bool(getattr(rules, "player_vote_calls", False))
    maybe = float(getattr(rules, "no_imposter_chance", 0.0) or 0.0) > 0
    sample_intros = [(n, f"(sample introduction by {n})") for n in names if n != imp_name]
    valid = [n for n in names if n != trio_name]
    note = ""
    if calls_on:
        continues = rules.on_no_decision == "continue"
        note = ps.vote_trigger_note("scheduled", None, continues, maybe)
    trio_reminder = ps.reminder(trio_name, "trio", cfg.trio.display_name, names, rules)
    imp_reminder = ps.reminder(imp_name, "imposter", cfg.imposter.display_name, names, rules)
    with_r = lambda r, t: r + "\n\n" + t  # noqa: E731
    return {
        "trio_system": ps.system_prompt(trio_name, "trio", cfg.trio.display_name, rules, names),
        "imposter_system": ps.system_prompt(imp_name, "imposter", cfg.imposter.display_name, rules, names),
        "tasks": {
            "intro": with_r(trio_reminder, ps.intro_task(rules.intro_word_limit, maybe)),
            "imposter_intro": with_r(imp_reminder, ps.imposter_intro_task(sample_intros, rules.intro_word_limit)),
            "question": with_r(trio_reminder, ps.question_task(1, 1, rules.question_word_limit)),
            "answer": with_r(trio_reminder, ps.answer_task(names[2], "(sample question)", trio_name,
                                                           rules.answer_word_limit, None,
                                                           rules.answer_mode == "sequential", calls_on)),
            "vote": with_r(trio_reminder, ps.vote_task(1, trio_name, valid, rules.vote_threshold, note,
                                                       allow_nobody=maybe)),
            "paraphrase": ps.paraphrase_request("(sample message)"),
        },
        "prompt_version": ps.version,
        "prompts_modified": ps.modified,
        "warnings": ps.warnings,
    }
