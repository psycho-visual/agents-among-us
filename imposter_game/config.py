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
    """A problem with config.toml or the API key, explained for a human."""


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
class GameConfig:
    trio: ModelConfig
    imposter: ModelConfig
    rules: RulesConfig = field(default_factory=RulesConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    paraphrase: ParaphraseConfig = field(default_factory=ParaphraseConfig)
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
