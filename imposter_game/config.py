"""Loading and validating config.toml.

`load_config(path)` reads the TOML file, fills in defaults for anything left
out, checks every value, and returns a `GameConfig` (plain dataclasses).
Problems raise `ConfigError` with a message meant for a human, e.g.
"config.toml: [rules] vote_threshold must be between 1 and 4 (got 7)".

The OpenRouter API key is read from the environment variable
OPENROUTER_API_KEY, or from a `.env` file in the repo root. It is stored on
`GameConfig.api_key` (None if not found); call `require_api_key(cfg)` before
starting a real (non-mock) game to get a friendly error when it is missing.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: The repository root (the folder containing config.toml and run.py).
REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_CONFIG_PATH = REPO_ROOT / "config.toml"

REASONING_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
NAME_RULES = ("lie_allowed", "no_names")
ON_NO_DECISION = ("end", "continue")
ANSWER_MODES = ("simultaneous", "sequential")

DEFAULT_BANNED_NAMES = [
    "claude", "anthropic", "haiku", "sonnet", "opus",
    "gpt", "openai", "chatgpt", "codex", "o1", "o3", "o4",
    "gemini", "gemma", "google", "deepmind", "bard",
    "llama", "meta",
    "mistral", "mixtral",
    "grok", "xai",
    "deepseek", "qwen", "alibaba", "kimi", "moonshot", "glm", "zhipu",
    "microsoft", "copilot", "cohere", "command r",
    "luna", "sol",
]


class ConfigError(Exception):
    """A problem with config.toml or the API key, explained for a human.

    `field` is the setting id it is about (e.g. "rules.vote_threshold") when known,
    and `short` is the message without the "config.toml: [rules]" prefix.
    """

    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        m = re.search(r"\[(\w+)\] section", message) or re.search(r"\[(\w+)\] (\w+)", message)
        if field is None and m:
            field = f"{m.group(1)}.model" if m.re.pattern.endswith("section") else f"{m.group(1)}.{m.group(2)}"
        self.field = field
        if m and m.re.pattern.endswith("section"):
            self.short = re.sub(r"^[^:]*: ", "", message, count=1)
        else:
            self.short = re.sub(r"^.*?\[\w+\] ", "", message, count=1) if m else message


@dataclass
class ModelConfig:
    model: str                      # OpenRouter model id, e.g. "anthropic/claude-haiku-5.5"
    display_name: str               # what the players are told, e.g. "Claude Haiku 5.5"
    reasoning_effort: str | None    # one of REASONING_EFFORTS, or None = model default
    max_output_tokens: int | None = None   # per-role override of [api].max_output_tokens


@dataclass
class RulesConfig:
    name_rule: str = "lie_allowed"
    redact_violations: bool = True
    banned_names: list[str] = field(default_factory=lambda: list(DEFAULT_BANNED_NAMES))
    rounds_before_vote: int = 2
    vote_threshold: int = 3
    on_no_decision: str = "end"
    max_rounds: int = 6
    answer_mode: str = "simultaneous"
    reveal_votes: bool = True
    player_names: list[str] = field(default_factory=lambda: ["Red", "Blue", "Green", "Yellow"])
    shuffle_seats: bool = True
    intro_word_limit: int = 40
    question_word_limit: int = 40
    answer_word_limit: int = 100
    player_vote_calls: bool = True      # players may call a vote (see config.toml)
    vote_call_threshold: int = 3        # requests needed in one turn to call a vote
    reveal_vote_calls: bool = False     # show who requested a vote in the public transcript
    imposter_knows_own_model: bool = True   # tell the imposter its own model name
    normalize_typography: bool = True       # straighten quotes/dashes, strip markdown in public text
    no_imposter_chance: float = 0.0         # probability that a game has no imposter at all


@dataclass
class ApiConfig:
    max_output_tokens: int = 16000
    timeout_seconds: float = 300
    max_retries: int = 3
    temperature: float | None = None
    prompt_caching: bool = True
    base_url: str = "https://openrouter.ai/api/v1"


@dataclass
class ParaphraseConfig:
    enabled: bool = False
    model: str = "mistralai/mistral-small-3.2-24b-instruct"
    reasoning_effort: str | None = None
    max_output_tokens: int = 1000


@dataclass
class BatchConfig:
    """Defaults for batch mode (many games in a row). See [batch] in config.toml."""
    games: int = 20                       # max games (0 = no game limit; then budget_usd is required)
    budget_usd: float | None = 1.00      # max total spend for the batch (None = no budget limit)
    min_credit_usd: float | None = None  # stop if the OpenRouter credit left falls below this (None = off)
    alternate_roles: bool = False         # even-numbered games swap the trio and imposter models
    stop_after_errors: int = 3            # stop after this many failed games in a row (0 = never)
    parallel: int = 1                     # games at once (command line only; the browser runs one at a time)
    first_game_estimate_usd: float = 0.10  # cost guess for the budget check before any game has finished


@dataclass
class GameConfig:
    trio: ModelConfig
    imposter: ModelConfig
    rules: RulesConfig = field(default_factory=RulesConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    paraphrase: ParaphraseConfig = field(default_factory=ParaphraseConfig)
    batch: BatchConfig = field(default_factory=BatchConfig)
    api_key: str | None = field(default=None, repr=False)
    source_path: str | None = None

    def max_tokens_for(self, role: str) -> int:
        """Per-call output token budget for "trio" or "imposter" (role override, else [api])."""
        m = self.imposter if role == "imposter" else self.trio
        return m.max_output_tokens or self.api.max_output_tokens

    def summary(self) -> dict:
        """A JSON-safe snapshot of the config (never includes the API key)."""
        return {
            "trio": asdict(self.trio),
            "imposter": asdict(self.imposter),
            "rules": asdict(self.rules),
            "api": asdict(self.api),
            "paraphrase": asdict(self.paraphrase),
            "batch": asdict(self.batch),
            "source_path": self.source_path,
        }


# ---------------------------------------------------------------------------
# API key
# ---------------------------------------------------------------------------

def read_dotenv(path: Path) -> dict[str, str]:
    """Very small .env parser: KEY=value lines, '#' comments, optional quotes."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def find_api_key(repo_root: Path = REPO_ROOT) -> str | None:
    """OPENROUTER_API_KEY from the environment, else from <repo_root>/.env."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    key = read_dotenv(repo_root / ".env").get("OPENROUTER_API_KEY", "").strip()
    return key or None


def require_api_key(cfg: GameConfig) -> str:
    """Return the API key or raise a ConfigError explaining how to set it."""
    if cfg.api_key:
        return cfg.api_key
    raise ConfigError(
        "No OpenRouter API key found.\n"
        "  Either set the environment variable OPENROUTER_API_KEY, or create a file named .env in\n"
        f"  {REPO_ROOT}\n"
        "  containing one line:\n"
        "      OPENROUTER_API_KEY=sk-or-...\n"
        "  (Get a key at https://openrouter.ai/keys.) To try the game without a key, run: python run.py --mock"
    )


# ---------------------------------------------------------------------------
# Loading + validation
# ---------------------------------------------------------------------------

def _get(table: dict, key: str, default, where: str, kind):
    """Fetch table[key] (or default) and check its type."""
    value = table.get(key, default)
    if kind is float and isinstance(value, int) and not isinstance(value, bool):
        value = float(value)
    if kind is int and isinstance(value, bool):
        raise ConfigError(f"{where} {key} must be a whole number (got {value!r})")
    if not isinstance(value, kind):
        names = {str: "text in quotes", int: "a whole number", float: "a number",
                 bool: "true or false", list: "a list like [\"a\", \"b\"]"}
        raise ConfigError(f"{where} {key} must be {names.get(kind, kind.__name__)} (got {value!r})")
    return value


def _choice(value: str, options: tuple, where: str, key: str) -> str:
    if value not in options:
        opts = ", ".join(f'"{o}"' for o in options)
        raise ConfigError(f'{where} {key} must be one of {opts} (got "{value}")')
    return value


def _model_section(data: dict, name: str, file: str) -> ModelConfig:
    where = f"{file}: [{name}]"
    table = data.get(name)
    if not isinstance(table, dict):
        raise ConfigError(f"{file}: missing the [{name}] section (needs model, display_name, reasoning_effort)")
    model = _get(table, "model", "", where, str).strip()
    if not model:
        raise ConfigError(f'{where} model is required, e.g. model = "anthropic/claude-haiku-5.5"')
    display = _get(table, "display_name", "", where, str).strip() or model
    effort = _get(table, "reasoning_effort", "", where, str).strip().lower()
    if effort:
        _choice(effort, REASONING_EFFORTS, where, "reasoning_effort")
    max_tokens = table.get("max_output_tokens")
    if max_tokens is not None:
        max_tokens = _get(table, "max_output_tokens", None, where, int)
        if max_tokens < 16:
            raise ConfigError(f"{where} max_output_tokens is too small (got {max_tokens})")
    return ModelConfig(model=model, display_name=display, reasoning_effort=effort or None,
                       max_output_tokens=max_tokens)


def _rules_section(table: dict, file: str) -> RulesConfig:
    where = f"{file}: [rules]"
    d = RulesConfig()
    r = RulesConfig(
        name_rule=_choice(_get(table, "name_rule", d.name_rule, where, str), NAME_RULES, where, "name_rule"),
        redact_violations=_get(table, "redact_violations", d.redact_violations, where, bool),
        banned_names=_get(table, "banned_names", d.banned_names, where, list),
        rounds_before_vote=_get(table, "rounds_before_vote", d.rounds_before_vote, where, int),
        vote_threshold=_get(table, "vote_threshold", d.vote_threshold, where, int),
        on_no_decision=_choice(_get(table, "on_no_decision", d.on_no_decision, where, str),
                               ON_NO_DECISION, where, "on_no_decision"),
        max_rounds=_get(table, "max_rounds", d.max_rounds, where, int),
        answer_mode=_choice(_get(table, "answer_mode", d.answer_mode, where, str),
                            ANSWER_MODES, where, "answer_mode"),
        reveal_votes=_get(table, "reveal_votes", d.reveal_votes, where, bool),
        player_names=_get(table, "player_names", d.player_names, where, list),
        shuffle_seats=_get(table, "shuffle_seats", d.shuffle_seats, where, bool),
        intro_word_limit=_get(table, "intro_word_limit", d.intro_word_limit, where, int),
        question_word_limit=_get(table, "question_word_limit", d.question_word_limit, where, int),
        answer_word_limit=_get(table, "answer_word_limit", d.answer_word_limit, where, int),
        player_vote_calls=_get(table, "player_vote_calls", d.player_vote_calls, where, bool),
        vote_call_threshold=_get(table, "vote_call_threshold", d.vote_call_threshold, where, int),
        reveal_vote_calls=_get(table, "reveal_vote_calls", d.reveal_vote_calls, where, bool),
        imposter_knows_own_model=_get(table, "imposter_knows_own_model", d.imposter_knows_own_model, where, bool),
        normalize_typography=_get(table, "normalize_typography", d.normalize_typography, where, bool),
        no_imposter_chance=_get(table, "no_imposter_chance", d.no_imposter_chance, where, float),
    )
    if not 0.0 <= r.no_imposter_chance <= 1.0:
        raise ConfigError(f"{where} no_imposter_chance must be between 0.0 and 1.0 (got {r.no_imposter_chance})")
    names = [str(n).strip() for n in r.player_names]
    if len(names) != 4 or not all(names):
        raise ConfigError(f"{where} player_names must list exactly 4 names (got {r.player_names!r})")
    if len({n.lower() for n in names}) != 4:
        raise ConfigError(f"{where} player_names must all be different (got {names!r})")
    for n in names:
        if not n.replace(" ", "").isalnum():
            raise ConfigError(f'{where} player_names may only use letters/digits (got "{n}")')
    r.player_names = names
    r.banned_names = [str(n).strip() for n in r.banned_names if str(n).strip()]
    if r.rounds_before_vote < 1:
        raise ConfigError(f"{where} rounds_before_vote must be at least 1 (got {r.rounds_before_vote})")
    if not 1 <= r.vote_threshold <= 4:
        raise ConfigError(f"{where} vote_threshold must be between 1 and 4 (got {r.vote_threshold})")
    if not 1 <= r.vote_call_threshold <= 4:
        raise ConfigError(f"{where} vote_call_threshold must be between 1 and 4 (got {r.vote_call_threshold})")
    if r.on_no_decision == "continue" and r.max_rounds < r.rounds_before_vote:
        raise ConfigError(f"{where} max_rounds ({r.max_rounds}) must be at least rounds_before_vote "
                          f"({r.rounds_before_vote})")
    for key in ("intro_word_limit", "question_word_limit", "answer_word_limit"):
        if getattr(r, key) < 5:
            raise ConfigError(f"{where} {key} must be at least 5 (got {getattr(r, key)})")
    return r


def _paraphrase_section(table: dict, file: str) -> ParaphraseConfig:
    where = f"{file}: [paraphrase]"
    d = ParaphraseConfig()
    effort = _get(table, "reasoning_effort", "", where, str).strip().lower()
    if effort:
        _choice(effort, REASONING_EFFORTS, where, "reasoning_effort")
    p = ParaphraseConfig(
        enabled=_get(table, "enabled", d.enabled, where, bool),
        model=_get(table, "model", d.model, where, str).strip(),
        reasoning_effort=effort or None,
        max_output_tokens=_get(table, "max_output_tokens", d.max_output_tokens, where, int),
    )
    if p.enabled and not p.model:
        raise ConfigError(f'{where} model is required when enabled = true')
    if p.max_output_tokens < 16:
        raise ConfigError(f"{where} max_output_tokens is too small (got {p.max_output_tokens})")
    return p


def _optional_money(table: dict, key: str, default, where: str):
    """A dollar amount that may be left empty ("") to switch it off."""
    value = table.get(key, "" if default is None else default)
    if value == "" or value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f'{where} {key} must be an amount like 1.00, or "" for none (got {value!r})')
    return float(value)


def _batch_section(table: dict, file: str) -> BatchConfig:
    where = f"{file}: [batch]"
    d = BatchConfig()
    b = BatchConfig(
        games=_get(table, "games", d.games, where, int),
        budget_usd=_optional_money(table, "budget_usd", d.budget_usd, where),
        min_credit_usd=_optional_money(table, "min_credit_usd", d.min_credit_usd, where),
        alternate_roles=_get(table, "alternate_roles", d.alternate_roles, where, bool),
        stop_after_errors=_get(table, "stop_after_errors", d.stop_after_errors, where, int),
        parallel=_get(table, "parallel", d.parallel, where, int),
        first_game_estimate_usd=_get(table, "first_game_estimate_usd", d.first_game_estimate_usd, where, float),
    )
    validate_batch_limits(b, where)
    return b


def validate_batch_limits(b, where: str = "[batch]") -> None:
    """Shared by config loading, the CLI and the GUI: a batch must always be bounded."""
    if b.games < 0:
        raise ConfigError(f"{where} games cannot be negative (got {b.games})")
    if b.budget_usd is not None and b.budget_usd <= 0:
        raise ConfigError(f'{where} budget_usd must be more than 0, or "" for no budget (got {b.budget_usd})')
    if b.games == 0 and b.budget_usd is None:
        raise ConfigError(f"{where} budget_usd is needed when games = 0 (no game limit): a batch must have a "
                          "game limit, a spending limit, or both")
    if b.min_credit_usd is not None and b.min_credit_usd < 0:
        raise ConfigError(f"{where} min_credit_usd cannot be negative (got {b.min_credit_usd})")
    if b.stop_after_errors < 0:
        raise ConfigError(f"{where} stop_after_errors cannot be negative (got {b.stop_after_errors})")
    if not 1 <= b.parallel <= 8:
        raise ConfigError(f"{where} parallel must be between 1 and 8 (got {b.parallel})")
    if b.first_game_estimate_usd < 0:
        raise ConfigError(f"{where} first_game_estimate_usd cannot be negative (got {b.first_game_estimate_usd})")


def _api_section(table: dict, file: str) -> ApiConfig:
    where = f"{file}: [api]"
    d = ApiConfig()
    temp = table.get("temperature", "")
    if temp == "" or temp is None:
        temperature = None
    elif isinstance(temp, (int, float)) and not isinstance(temp, bool):
        temperature = float(temp)
    else:
        raise ConfigError(f'{where} temperature must be a number or "" (got {temp!r})')
    a = ApiConfig(
        max_output_tokens=_get(table, "max_output_tokens", d.max_output_tokens, where, int),
        timeout_seconds=_get(table, "timeout_seconds", float(d.timeout_seconds), where, float),
        max_retries=_get(table, "max_retries", d.max_retries, where, int),
        temperature=temperature,
        prompt_caching=_get(table, "prompt_caching", d.prompt_caching, where, bool),
        base_url=_get(table, "base_url", d.base_url, where, str).rstrip("/"),
    )
    if a.max_output_tokens < 16:
        raise ConfigError(f"{where} max_output_tokens is too small (got {a.max_output_tokens})")
    if a.timeout_seconds <= 0:
        raise ConfigError(f"{where} timeout_seconds must be positive")
    if a.max_retries < 0:
        raise ConfigError(f"{where} max_retries cannot be negative")
    return a


def config_from_dict(data: dict, file: str = "config", api_key: str | None = None) -> GameConfig:
    """Build and validate a GameConfig from an already-parsed TOML dict."""
    cfg = GameConfig(
        trio=_model_section(data, "trio", file),
        imposter=_model_section(data, "imposter", file),
        rules=_rules_section(data.get("rules", {}) or {}, file),
        api=_api_section(data.get("api", {}) or {}, file),
        paraphrase=_paraphrase_section(data.get("paraphrase", {}) or {}, file),
        batch=_batch_section(data.get("batch", {}) or {}, file),
        api_key=api_key,
    )
    return cfg


def load_config(path: str | Path | None = None, *, read_key: bool = True) -> GameConfig:
    """Read config.toml (default: the one in the repo root) and return a GameConfig."""
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not path.is_absolute() and not path.exists():
        candidate = REPO_ROOT / path
        if candidate.exists():
            path = candidate
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path.name} is not valid TOML: {e}\n"
                          "  (Check for missing quotes around text or a missing ']'.)") from None
    cfg = config_from_dict(data, file=path.name, api_key=find_api_key() if read_key else None)
    cfg.source_path = str(path)
    return cfg


# =============================================================================
# Settings (used by the GUI settings drawer)
# =============================================================================
#
# settings_schema()            describes every option (label, help, type, limits, default, group)
# read_settings(path)          current values from config.toml, defaults filled in
# validate_settings(values)    same checks as load_config, reported per setting id
# save_settings(path, values)  writes the values back into config.toml, keeping all comments
#
# A setting id is "<section>.<key>", e.g. "rules.vote_threshold".

_EFFORT_CHOICES = [{"value": "", "label": "Model default"}] + [
    {"value": e, "label": e} for e in REASONING_EFFORTS]

# Factory defaults for the two model sections (the shipped config.toml); every other
# default comes from the dataclasses above, i.e. what load_config uses when a key is missing.
_MODEL_DEFAULTS = {
    "trio": {"model": "anthropic/claude-haiku-5.5", "display_name": "Claude Haiku 5.5",
             "reasoning_effort": "high", "max_output_tokens": 16000},
    "imposter": {"model": "openai/gpt-6-luna", "display_name": "GPT-6 Luna",
                 "reasoning_effort": "max", "max_output_tokens": 32000},
}


def _s(id, group, label, help, type, default, *, choices=None, min=None, max=None, step=None,
       advanced=False, visible_if=None, nullable=False) -> dict:
    section, key = id.split(".", 1)
    return {"id": id, "section": section, "key": key, "group": group, "label": label, "help": help,
            "type": type, "choices": choices, "min": min, "max": max, "step": step, "default": default,
            "advanced": advanced, "visible_if": visible_if, "nullable": nullable}


def _model_schema(role: str) -> list[dict]:
    d = _MODEL_DEFAULTS[role]
    who = "three trio players (all copies of one model)" if role == "trio" else "single imposter"
    return [
        _s(f"{role}.model", "Players & models", f"{role.capitalize()} model",
           f"The OpenRouter model id used by the {who}, e.g. \"{d['model']}\".", "model", d["model"]),
        _s(f"{role}.display_name", "Players & models", f"{role.capitalize()} display name",
           "The model name the players are told (\"You are ...\"). Leave empty to use the model id.",
           "string", d["display_name"]),
        _s(f"{role}.reasoning_effort", "Players & models", f"{role.capitalize()} reasoning effort",
           "How hard the model thinks before replying. \"Model default\" sends nothing.",
           "enum", d["reasoning_effort"], choices=_EFFORT_CHOICES),
        _s(f"{role}.max_output_tokens", "Players & models", f"{role.capitalize()} max output tokens",
           "Token budget per reply, including hidden reasoning. Empty = use the API default below.",
           "int", d["max_output_tokens"], min=16, max=200000, step=1000, nullable=True, advanced=True),
    ]


def settings_schema() -> list[dict]:
    """One entry per editable option, in display order (see module comment above)."""
    r, p, a = RulesConfig(), ParaphraseConfig(), ApiConfig()
    calls_on = {"id": "rules.player_vote_calls", "equals": True}
    para_on = {"id": "paraphrase.enabled", "equals": True}
    return [
        *_model_schema("trio"),
        *_model_schema("imposter"),
        # --- Game flow
        _s("rules.rounds_before_vote", "Game flow", "Rounds between votes",
           "A vote is held automatically after every this many rounds. In one round every player asks one "
           "question and everybody answers it.", "int", r.rounds_before_vote, min=1, max=20, step=1),
        _s("rules.on_no_decision", "Game flow", "When a vote removes nobody",
           "End the game with no winner, or keep playing and vote again later (up to the max rounds).",
           "enum", r.on_no_decision, choices=[{"value": "end", "label": "End the game (no winner)"},
                                              {"value": "continue", "label": "Keep playing"}]),
        _s("rules.max_rounds", "Game flow", "Max rounds",
           "With \"Keep playing\": the game stops after this many rounds (a final vote is held then).",
           "int", r.max_rounds, min=1, max=50, step=1,
           visible_if={"id": "rules.on_no_decision", "equals": "continue"}),
        _s("rules.answer_mode", "Game flow", "How players answer",
           "Simultaneous: everyone answers blind at the same time. Sequential: one after another, each "
           "seeing the earlier answers.", "enum", r.answer_mode,
           choices=[{"value": "simultaneous", "label": "Simultaneous (blind)"},
                    {"value": "sequential", "label": "Sequential (see earlier answers)"}]),
        _s("rules.player_names", "Game flow", "Seat names",
           "The four neutral names players are known by, in seat order.", "string_list", list(r.player_names),
           advanced=True),
        _s("rules.shuffle_seats", "Game flow", "Random imposter seat",
           "On: the imposter sits in a random seat each game. Off: it always takes the last seat.",
           "bool", r.shuffle_seats),
        # --- Voting
        _s("rules.vote_threshold", "Voting", "Votes needed to eliminate",
           "How many of the 4 votes a player needs to be voted out. Nobody may vote for themselves.",
           "int", r.vote_threshold, min=1, max=4, step=1),
        _s("rules.reveal_votes", "Voting", "Reveal who voted for whom",
           "After each vote, show everybody the individual ballots.", "bool", r.reveal_votes),
        _s("rules.player_vote_calls", "Voting", "Players can call a vote",
           "After each answer, players may secretly ask for a vote; if enough ask in the same turn, a vote "
           "is held at once. A called vote that removes nobody never ends the game.", "bool",
           r.player_vote_calls),
        _s("rules.vote_call_threshold", "Voting", "Requests needed to call a vote",
           "How many players must ask for a vote after the same question.", "int", r.vote_call_threshold,
           min=1, max=4, step=1, visible_if=calls_on),
        _s("rules.reveal_vote_calls", "Voting", "Show who asked for a vote",
           "On: after each question everyone sees who asked for a vote. Off: requests stay secret.",
           "bool", r.reveal_vote_calls, visible_if=calls_on),
        # --- Fairness
        _s("rules.name_rule", "Fairness", "Talking about model names",
           "Lying allowed: players may claim to be any model. No names: nobody may mention any model or AI "
           "company name in public.", "enum", r.name_rule,
           choices=[{"value": "lie_allowed", "label": "Allowed (lying is fine)"},
                    {"value": "no_names", "label": "Forbidden (no model names)"}]),
        _s("rules.redact_violations", "Fairness", "Hide forbidden names",
           "Replace a forbidden name with [REDACTED] before the other players see it. The log keeps the "
           "original.", "bool", r.redact_violations, visible_if={"id": "rules.name_rule", "equals": "no_names"}),
        _s("rules.banned_names", "Fairness", "Forbidden words",
           "Words that count as a model/company name (whole words, any case). Both models' names and ids are "
           "always added.", "string_list", list(r.banned_names), advanced=True,
           visible_if={"id": "rules.name_rule", "equals": "no_names"}),
        _s("rules.imposter_knows_own_model", "Fairness", "Tell the imposter its own model",
           "On: the imposter is told which model it is. Off: it only knows it is the imposter.", "bool",
           r.imposter_knows_own_model),
        _s("rules.normalize_typography", "Fairness", "Tidy typography",
           "Straighten curly quotes, replace long dashes, strip markdown in everything players say, so "
           "punctuation habits don't give a model away.", "bool", r.normalize_typography),
        _s("rules.no_imposter_chance", "Fairness", "Chance of no imposter",
           "Probability (0 to 1) that a game has no imposter at all. When above 0, players are told and may "
           "vote for NOBODY.", "float", r.no_imposter_chance, min=0.0, max=1.0, step=0.05),
        # --- Text limits
        _s("rules.intro_word_limit", "Text limits", "Introduction word limit",
           "Soft limit given to players. Text over twice the limit is cut off.", "int", r.intro_word_limit,
           min=5, max=1000, step=5),
        _s("rules.question_word_limit", "Text limits", "Question word limit",
           "Soft limit given to players. Text over twice the limit is cut off.", "int", r.question_word_limit,
           min=5, max=1000, step=5),
        _s("rules.answer_word_limit", "Text limits", "Answer word limit",
           "Soft limit given to players. Text over twice the limit is cut off.", "int", r.answer_word_limit,
           min=5, max=1000, step=5),
        # --- Paraphrase
        _s("paraphrase.enabled", "Paraphrase", "Paraphrase everything players say",
           "A neutral third model rewrites every public message in a plain style, so players must judge what "
           "is said, not how. Adds a small API cost.", "bool", p.enabled),
        _s("paraphrase.model", "Paraphrase", "Paraphrase model",
           "A cheap, non-reasoning model from a different company than both players.", "model", p.model,
           visible_if=para_on),
        _s("paraphrase.reasoning_effort", "Paraphrase", "Paraphrase reasoning effort",
           "Usually \"Model default\" (a non-reasoning model needs none).", "enum", "", choices=_EFFORT_CHOICES,
           visible_if=para_on, advanced=True),
        _s("paraphrase.max_output_tokens", "Paraphrase", "Paraphrase max output tokens",
           "Token budget per rewritten message.", "int", p.max_output_tokens, min=16, max=200000, step=100,
           visible_if=para_on, advanced=True),
        # --- Batch
        _s("batch.games", "Batch", "Games per batch",
           "Batch mode plays games one after another until a limit is reached. 0 = no game limit (then a "
           "spending limit is required).", "int", BatchConfig().games, min=0, max=10000, step=1),
        _s("batch.budget_usd", "Batch", "Spending limit ($)",
           "Stop the batch before it would spend more than this. A new game only starts if the most expensive "
           "game so far would still fit. Empty = no spending limit.", "float", BatchConfig().budget_usd,
           min=0.0, max=10000.0, step=0.5, nullable=True),
        _s("batch.min_credit_usd", "Batch", "Stop when credit falls below ($)",
           "Optional: before each game, check your OpenRouter credit and stop if less than this is left. "
           "Empty = don't check.", "float", None, min=0.0, max=10000.0, step=1.0, nullable=True),
        _s("batch.alternate_roles", "Batch", "Alternate roles",
           "Every second game swaps the trio and imposter models, so each model plays both sides.", "bool",
           BatchConfig().alternate_roles),
        _s("batch.stop_after_errors", "Batch", "Stop after errors in a row",
           "Stop the batch after this many games in a row end in an error (0 = never stop for errors).",
           "int", BatchConfig().stop_after_errors, min=0, max=100, step=1),
        _s("batch.parallel", "Batch", "Games at once (command line)",
           "How many games python batch.py runs at the same time. The browser always runs one at a time.",
           "int", BatchConfig().parallel, min=1, max=8, step=1, advanced=True),
        _s("batch.first_game_estimate_usd", "Batch", "First game cost estimate ($)",
           "Cost guess used by the spending limit before any game of the batch has finished.", "float",
           BatchConfig().first_game_estimate_usd, min=0.0, max=100.0, step=0.01, advanced=True),
        # --- API
        _s("api.max_output_tokens", "API", "Default max output tokens",
           "Token budget per reply (including hidden reasoning) for roles without their own setting. Empty "
           "replies that ran out are retried once with double.", "int", a.max_output_tokens, min=16,
           max=200000, step=1000, advanced=True),
        _s("api.timeout_seconds", "API", "Timeout (seconds)",
           "How long to wait for one reply before giving up. Reasoning models can be slow.", "float",
           float(a.timeout_seconds), min=1.0, max=3600.0, step=10.0, advanced=True),
        _s("api.max_retries", "API", "Retries",
           "How many times to retry after a network error, rate limit or server error.", "int", a.max_retries,
           min=0, max=10, step=1, advanced=True),
        _s("api.temperature", "API", "Temperature",
           "Sampling temperature. Leave empty to not send one (recommended: several reasoning models reject it).",
           "float", None, min=0.0, max=2.0, step=0.1, nullable=True, advanced=True),
        _s("api.prompt_caching", "API", "Prompt caching",
           "Let providers cache the repeated start of each prompt (cheaper and faster).", "bool",
           a.prompt_caching, advanced=True),
        _s("api.base_url", "API", "API base URL",
           "The OpenRouter API address. Only change this if you know you need to.", "string", a.base_url,
           advanced=True),
    ]


# Optional numbers written as "" in config.toml when switched off.
_EMPTY_MEANS_NONE = {"api.temperature", "batch.budget_usd", "batch.min_credit_usd"}


def _schema_by_id() -> dict[str, dict]:
    return {e["id"]: e for e in settings_schema()}


def _value_from_toml(entry: dict, raw):
    """A value as found in the TOML file -> the settings representation."""
    if entry["id"] in _EMPTY_MEANS_NONE and raw == "":
        return None
    if entry["type"] == "enum" and entry["choices"] and entry["choices"][0]["value"] == "" and isinstance(raw, str):
        return raw.strip().lower()
    if entry["type"] == "float" and isinstance(raw, int) and not isinstance(raw, bool):
        return float(raw)
    return raw


def _missing_value(entry: dict):
    """The value load_config effectively uses when this key is absent from config.toml."""
    if entry["section"] in _MODEL_DEFAULTS:
        return None if entry["nullable"] else ""
    return entry["default"]


def read_settings(path: str | Path | None = None) -> dict:
    """{"values": {id: value}, "errors": [str]} — what config.toml says, defaults filled in."""
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    errors: list[str] = []
    data: dict = {}
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        errors.append(f"Config file not found: {path}")
    except tomllib.TOMLDecodeError as e:
        errors.append(f"{path.name} is not valid TOML: {e}")
    values = {}
    for entry in settings_schema():
        table = data.get(entry["section"])
        if isinstance(table, dict) and entry["key"] in table:
            values[entry["id"]] = _value_from_toml(entry, table[entry["key"]])
        else:
            values[entry["id"]] = _missing_value(entry)
    if data:
        try:
            config_from_dict(data, file=path.name)
        except ConfigError as e:
            errors.append(str(e))
    return {"values": values, "errors": errors}


def _coerce(entry: dict, value):
    """Turn a value from the GUI (JSON) into the right Python type, or raise ValueError."""
    t = entry["type"]
    if entry["nullable"] and (value is None or value == ""):
        return None
    if t == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
        raise ValueError("must be on or off")
    if t == "int":
        if isinstance(value, bool):
            raise ValueError("must be a whole number")
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and re.fullmatch(r"\s*-?\d+\s*", value):
            return int(value)
        raise ValueError("must be a whole number")
    if t == "float":
        if isinstance(value, bool):
            raise ValueError("must be a number")
        if isinstance(value, (int, float)):
            f = float(value)
        elif isinstance(value, str):
            try:
                f = float(value)
            except ValueError:
                raise ValueError("must be a number") from None
        else:
            raise ValueError("must be a number")
        if f != f or f in (float("inf"), float("-inf")):
            raise ValueError("must be a normal number")
        return f
    if t == "string_list":
        if isinstance(value, str):
            value = [x for x in re.split(r"[,\n]", value)]
        if not isinstance(value, (list, tuple)) or not all(isinstance(x, str) for x in value):
            raise ValueError("must be a list of words")
        return [x.strip() for x in value if x.strip()]
    if not isinstance(value, str):
        raise ValueError("must be text")
    if t == "enum":
        value = value.strip()
        if value not in [c["value"] for c in entry["choices"]]:
            raise ValueError("must be one of: " + ", ".join(c["value"] or '""' for c in entry["choices"]))
    return value


def _to_toml_value(entry: dict, value):
    """The settings value as it should be written into config.toml (None = remove the key)."""
    if value is None:
        return "" if entry["id"] in _EMPTY_MEANS_NONE else None
    return value


def _build_data(values: dict) -> dict:
    data: dict = {}
    for entry in settings_schema():
        v = _to_toml_value(entry, values.get(entry["id"]))
        if v is not None:
            data.setdefault(entry["section"], {})[entry["key"]] = v
    return data


def _company(model_id: str) -> str:
    return model_id.split("/", 1)[0].lower() if "/" in model_id else ""


def _warnings(v: dict) -> dict[str, str]:
    w: dict[str, str] = {}
    if v.get("trio.model") and v.get("trio.model") == v.get("imposter.model"):
        w["imposter.model"] = ("The trio and the imposter are the same model, so there is nothing to detect "
                               "(fine as a control experiment).")
    if v.get("paraphrase.enabled"):
        pc = _company(v.get("paraphrase.model") or "")
        if pc and pc in (_company(v.get("trio.model") or ""), _company(v.get("imposter.model") or "")):
            w["paraphrase.model"] = ("The paraphrase model is from the same company as one of the players, "
                                     "which could make that player's style easier to spot.")
    for role in ("trio", "imposter"):
        budget = v.get(f"{role}.max_output_tokens") or v.get("api.max_output_tokens") or 0
        if v.get(f"{role}.reasoning_effort") in ("max", "xhigh") and budget < 16000:
            w[f"{role}.max_output_tokens"] = (f"With reasoning effort \"{v.get(role + '.reasoning_effort')}\", "
                                              f"{budget} tokens may run out before the reply is written; "
                                              "16000 or more is safer.")
    return w


def validate_settings(values: dict, base: dict | None = None) -> dict:
    """{"errors": {id: msg}, "warnings": {id: msg}} using the same checks as load_config.

    Missing ids are taken from `base` (e.g. the current file) or the defaults.
    """
    schema = _schema_by_id()
    merged = {i: e["default"] for i, e in schema.items()}
    merged.update(base or {})
    errors: dict[str, str] = {}
    for k, v in (values or {}).items():
        if k not in schema:
            errors[k] = "Unknown setting."
            continue
        try:
            merged[k] = _coerce(schema[k], v)
        except ValueError as e:
            errors[k] = f"{schema[k]['label']} {e}."
    data = _build_data(merged)
    for _ in range(len(schema) + 1):           # collect every error, not just the first
        try:
            config_from_dict(data, file="config.toml")
            break
        except ConfigError as e:
            fid = e.field if e.field in schema else "_general"
            if fid in errors:
                errors.setdefault("_general", e.short)
                break
            errors[fid] = e.short
            if fid == "_general":
                break
            sec, key = fid.split(".", 1)
            data.setdefault(sec, {})[key] = _to_toml_value(schema[fid], schema[fid]["default"])
            if data[sec][key] is None:
                del data[sec][key]
    return {"errors": errors, "warnings": _warnings(merged), "values": merged}


def save_settings(path: str | Path | None, values: dict) -> dict:
    """Validate and write values into config.toml in place (comments kept). See SETTINGS docs above."""
    from . import toml_edit

    path = Path(path) if path else DEFAULT_CONFIG_PATH
    current = read_settings(path)
    if any(e.startswith(("Config file not found", f"{path.name} is not valid TOML")) for e in current["errors"]):
        return {"ok": False, "errors": {"_general": current["errors"][0]}, "warnings": {}, "backup": None,
                "changed": []}
    check = validate_settings(values, base=current["values"])
    result = {"ok": False, "errors": check["errors"], "warnings": check["warnings"], "backup": None, "changed": []}
    if check["errors"]:
        return result

    schema = _schema_by_id()
    new_values = check["values"]
    original = path.read_bytes().decode("utf-8")
    try:
        file_data = tomllib.loads(original)
    except tomllib.TOMLDecodeError as e:
        result["errors"] = {"_general": f"{path.name} is not valid TOML: {e}"}
        return result
    updates = {}
    changed = []
    for sid, entry in schema.items():
        new = _to_toml_value(entry, new_values[sid])
        table = file_data.get(entry["section"]) if isinstance(file_data.get(entry["section"]), dict) else {}
        present = entry["key"] in table
        if new is None:
            if present:
                updates[(entry["section"], entry["key"])] = toml_edit.REMOVE
                changed.append(sid)
            continue
        if present and toml_edit._same(_value_from_toml(entry, table[entry["key"]]), new_values[sid]):
            continue
        if not present and toml_edit._same(_missing_value(entry), new_values[sid]):
            continue          # absent from the file and still at the built-in default: leave it out
        updates[(entry["section"], entry["key"])] = new
        changed.append(sid)
    result["changed"] = changed
    if not updates:
        result["ok"] = True
        return result

    new_text = toml_edit.set_values(original, updates)
    backup = path.with_name(path.name + ".bak")
    backup.write_bytes(original.encode("utf-8"))
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(new_text.encode("utf-8"))
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()

    # Re-read to make sure the file is valid and says exactly what we meant.
    problem = None
    try:
        reloaded = load_config(path, read_key=False)
        after = read_settings(path)["values"]
        wrong = [sid for sid in schema if not toml_edit._same(after[sid], new_values[sid])
                 and not (after[sid] in (None, "") and new_values[sid] in (None, ""))]
        if wrong:
            problem = "Saving did not produce the expected values for: " + ", ".join(wrong)
        del reloaded
    except Exception as e:  # noqa: BLE001
        problem = f"The saved file could not be read back ({e})"
    if problem:
        tmp.write_bytes(original.encode("utf-8"))      # put the previous file back (atomically)
        os.replace(tmp, path)
        result["errors"] = {"_general": problem + ". Your previous config.toml was restored."}
        return result
    result["ok"] = True
    result["backup"] = str(backup)
    return result
