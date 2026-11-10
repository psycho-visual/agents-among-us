"""Agents Among Us — a 4-player social-deduction game played by LLMs.

Modules:
    config      load_config(path) -> GameConfig
    openrouter  OpenRouterClient, MockClient, CallResult
    prompts     every piece of text the models see
    game        Game (step-by-step engine) and the rule functions
    logger      writes logs/games/<id>.json, <id>.md and logs/summary.jsonl
    stats       compute_stats(summary_rows)
"""

from .config import ConfigError, GameConfig, load_config, require_api_key
from .game import Game, VoteOutcome, resolve_vote
from .openrouter import CallResult, MockClient, OpenRouterClient, OpenRouterError
from .stats import compute_stats, read_summary

__all__ = [
    "CallResult", "ConfigError", "Game", "GameConfig", "MockClient", "OpenRouterClient", "OpenRouterError",
    "VoteOutcome", "compute_stats", "load_config", "read_summary", "require_api_key", "resolve_vote",
]
__version__ = "0.1.0"
