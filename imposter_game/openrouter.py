"""Talking to the models: the real OpenRouter client and an offline MockClient.

Both clients have the same method:

    client.chat(model, messages, reasoning_effort, max_tokens, session_id=None) -> CallResult

`OpenRouterClient` is the ONLY place that knows OpenRouter's request/response
format. It uses only the standard library (urllib), retries rate limits /
server errors / network errors with exponential backoff, parses every field
defensively, and works out the cost of each call (from the response, else
from the /generation endpoint, else estimated from the /models price list).

`MockClient` fakes plausible players so a whole game can be played offline
for $0 (used by `python run.py --mock` and by the tests).
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import random
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from . import prompts

log = logging.getLogger("imposter_game.openrouter")

MAX_TOKENS_CAP = 64000                       # never retry with more than this
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}


@dataclass
class CallResult:
    content: str
    reasoning: str | None            # human-readable reasoning text/summary if returned, else None
    reasoning_details: list | None   # raw, for the log
    input_tokens: int
    output_tokens: int               # includes reasoning tokens
    reasoning_tokens: int | None
    cached_tokens: int | None
    cost_usd: float | None
    cost_source: str                 # "openrouter" | "estimated" | "mock" | "unavailable"
    model: str                       # model actually reported
    provider: str | None
    generation_id: str | None
    finish_reason: str | None
    latency_s: float
    raw_usage: dict | None
    # --- additions beyond the original spec ---
    reasoning_kind: str = "none"     # "text" | "summary" | "encrypted_only" | "none"
    cache_write_tokens: int | None = None
    refusal: str | None = None       # message.refusal (e.g. finish_reason "content_filter")
    attempts: int = 1                # HTTP attempts made (incl. retries)
    max_tokens_used: int | None = None
    notes: list[str] = field(default_factory=list)   # e.g. "retried with max_tokens=32000 after empty 'length'"


class OpenRouterError(Exception):
    """A failed API call. `retryable` says whether trying again might help."""

    def __init__(self, message: str, status: int | None = None, retryable: bool = False,
                 retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
# Small defensive helpers
# ---------------------------------------------------------------------------

def _dict(x) -> dict:
    return x if isinstance(x, dict) else {}


def _int(x) -> int | None:
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return int(x)
    if isinstance(x, str):
        try:
            return int(float(x))
        except ValueError:
            return None
    return None


def _float(x) -> float | None:
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        try:
            return float(x)
        except ValueError:
            return None
    return None


def _text(x) -> str:
    """Message content may be a string, None, or a list of content parts."""
    if isinstance(x, str):
        return x
    if isinstance(x, list):
        parts = []
        for p in x:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict) and isinstance(p.get("text"), str):
                parts.append(p["text"])
        return "".join(parts)
    return ""


def parse_reasoning(message: dict) -> tuple[str | None, list | None, str]:
    """Return (reasoning_text, reasoning_details, reasoning_kind) from a response message."""
    details = message.get("reasoning_details")
    details = details if isinstance(details, list) else None
    texts, summaries, encrypted = [], [], False
    for d in details or []:
        if not isinstance(d, dict):
            continue
        t = d.get("type", "")
        if t == "reasoning.text" and isinstance(d.get("text"), str):
            texts.append(d["text"])
        elif t == "reasoning.summary" and isinstance(d.get("summary"), str):
            summaries.append(d["summary"])
        elif t == "reasoning.encrypted":
            encrypted = True
    plain = message.get("reasoning")
    if isinstance(plain, str) and plain.strip():
        kind = "summary" if summaries and not texts else "text"
        return plain, details, kind
    if texts:
        return "\n".join(texts), details, "text"
    if summaries:
        return "\n".join(summaries), details, "summary"
    return None, details, ("encrypted_only" if encrypted else "none")


def _error_info(body) -> tuple[str, int | None, dict]:
    """(message, code, metadata) from an OpenRouter error object/body."""
    err = _dict(_dict(body).get("error")) if isinstance(body, dict) else {}
    msg = err.get("message") if isinstance(err.get("message"), str) else ""
    return msg, _int(err.get("code")), _dict(err.get("metadata"))


# ---------------------------------------------------------------------------
# Price list cache (for estimating cost when the response doesn't include it)
# ---------------------------------------------------------------------------

_PRICES: dict[str, dict] | None = None
_PRICES_LOCK = threading.Lock()


def _fetch_prices(client: "OpenRouterClient") -> dict[str, dict]:
    global _PRICES
    with _PRICES_LOCK:
        if _PRICES is None:
            prices: dict[str, dict] = {}
            try:
                data = client._request("GET", "/models", None, timeout=30)
                for m in data.get("data", []) if isinstance(data.get("data"), list) else []:
                    if isinstance(m, dict) and isinstance(m.get("id"), str):
                        prices[m["id"]] = _dict(m.get("pricing"))
            except Exception as e:  # noqa: BLE001 — price list is best-effort
                log.warning("Could not fetch OpenRouter price list: %s", e)
            _PRICES = prices
        return _PRICES


def estimate_cost(pricing: dict, input_tokens: int, output_tokens: int,
                  cached_tokens: int | None, cache_write_tokens: int | None) -> float | None:
    """USD cost from OpenRouter per-token prices (strings like "0.0000001")."""
    p_in, p_out = _float(pricing.get("prompt")), _float(pricing.get("completion"))
    if p_in is None or p_out is None:
        return None
    p_read = _float(pricing.get("input_cache_read"))
    p_write = _float(pricing.get("input_cache_write"))
    cached = cached_tokens or 0 if p_read is not None else 0
    written = cache_write_tokens or 0 if p_write is not None else 0
    plain = max(0, input_tokens - cached - written)
    cost = plain * p_in + output_tokens * p_out
    if cached:
        cost += cached * p_read
    if written:
        cost += written * p_write
    return cost


# ---------------------------------------------------------------------------
# The real client
# ---------------------------------------------------------------------------

class OpenRouterClient:
    """Calls https://openrouter.ai/api/v1/chat/completions (non-streaming)."""

    def __init__(self, api_key: str, api_cfg, sleep=time.sleep):
        self.api_key = api_key
        self.base_url = getattr(api_cfg, "base_url", "https://openrouter.ai/api/v1").rstrip("/")
        self.timeout = float(getattr(api_cfg, "timeout_seconds", 300))
        self.max_retries = int(getattr(api_cfg, "max_retries", 3))
        self.temperature = getattr(api_cfg, "temperature", None)
        self.prompt_caching = bool(getattr(api_cfg, "prompt_caching", True))
        self._sleep = sleep

    # -- HTTP ---------------------------------------------------------------

    def _request(self, method: str, path: str, body: dict | None, timeout: float | None = None) -> dict:
        """One HTTP request. Returns parsed JSON or raises OpenRouterError."""
        url = self.base_url + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-Title": "Agents Among Us",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            try:
                err_body = json.loads(e.read().decode("utf-8", "replace") or "{}")
            except (ValueError, OSError):
                err_body = {}
            msg, _code, meta = _error_info(err_body)
            retry_after = _float(e.headers.get("Retry-After")) if e.headers else None
            retryable = e.code in RETRY_STATUSES
            if e.code == 403 and meta.get("error_type") == "refusal":
                retryable = False
            raise OpenRouterError(self._explain(e.code, msg, meta), status=e.code,
                                  retryable=retryable, retry_after=retry_after) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError,
                http.client.HTTPException, OSError) as e:
            reason = getattr(e, "reason", e)
            raise OpenRouterError(f"Network error talking to OpenRouter: {reason}", retryable=True) from None
        try:
            parsed = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise OpenRouterError("OpenRouter returned a response that is not JSON", retryable=True) from None
        if not isinstance(parsed, dict):
            raise OpenRouterError("OpenRouter returned an unexpected response", retryable=True)
        return parsed

    @staticmethod
    def _explain(status: int, msg: str, meta: dict) -> str:
        hints = {
            400: "Bad request (check the model id and settings in config.toml)",
            401: "Invalid or missing OpenRouter API key",
            402: "Your OpenRouter account is out of credits",
            403: "Request refused (moderation or permissions)",
            404: "Not found (is the model id in config.toml correct?)",
            408: "Request timed out", 429: "Rate limited",
        }
        base = hints.get(status, "Server error" if status >= 500 else "Request failed")
        extra = f": {msg}" if msg else ""
        if meta.get("provider_name"):
            extra += f" (provider: {meta['provider_name']})"
        return f"OpenRouter HTTP {status} — {base}{extra}"

    def _with_retries(self, fn):
        attempt = 0
        while True:
            try:
                return fn(), attempt + 1
            except OpenRouterError as e:
                if not e.retryable or attempt >= self.max_retries:
                    raise
                delay = e.retry_after if e.retry_after is not None else 2.0 * (2 ** attempt) + random.random()
                delay = min(max(delay, 0.5), 120.0)
                log.warning("%s — retrying in %.1fs (attempt %d of %d)", e, delay, attempt + 1, self.max_retries)
                self._sleep(delay)
                attempt += 1

    # -- Response checks ----------------------------------------------------

    @staticmethod
    def _check_completion(data: dict) -> dict:
        """Raise OpenRouterError for error bodies; return choices[0]."""
        if data.get("error") is not None:
            msg, code, _meta = _error_info(data)
            raise OpenRouterError(f"OpenRouter error{f' {code}' if code else ''}: {msg or data.get('error')}",
                                  status=code, retryable=(code is None or code in RETRY_STATUSES))
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise OpenRouterError("OpenRouter response had no choices", retryable=True)
        choice = choices[0]
        if choice.get("error") is not None:
            msg, code, _meta = _error_info(choice)
            raise OpenRouterError(f"Provider error{f' {code}' if code else ''}: {msg or choice.get('error')}",
                                  status=code, retryable=(code is None or code in RETRY_STATUSES))
        if choice.get("finish_reason") == "error":
            raise OpenRouterError("Provider reported finish_reason 'error'", retryable=True)
        return choice

    # -- Cost ---------------------------------------------------------------

    def _generation_cost(self, gen_id: str) -> float | None:
        """Ask /generation for the real cost; it can take a few seconds to appear."""
        for _ in range(5):
            try:
                data = self._request("GET", "/generation?id=" + urllib.parse.quote(gen_id), None, timeout=20)
                cost = _float(_dict(data.get("data")).get("total_cost"))
                if cost is not None:
                    return cost
            except OpenRouterError as e:
                if e.status not in (404, None) and not e.retryable:
                    return None
            self._sleep(1.0)
        return None

    def _cost(self, model: str, usage: dict, gen_id: str | None, inp: int, out: int,
              cached: int | None, written: int | None) -> tuple[float | None, str]:
        cost = _float(usage.get("cost"))
        if cost is not None:
            return cost, "openrouter"
        if gen_id:
            cost = self._generation_cost(gen_id)
            if cost is not None:
                return cost, "openrouter"
        pricing = _fetch_prices(self).get(model)
        if pricing:
            cost = estimate_cost(pricing, inp, out, cached, written)
            if cost is not None:
                return cost, "estimated"
        return None, "unavailable"

    # -- Main entry point ---------------------------------------------------

    def _build_body(self, model, messages, reasoning_effort, max_tokens, session_id) -> dict:
        body: dict = {"model": model, "messages": messages, "max_tokens": int(max_tokens)}
        if reasoning_effort:
            body["reasoning"] = {"effort": reasoning_effort}
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.prompt_caching:
            if model.startswith("anthropic/"):
                body["cache_control"] = {"type": "ephemeral"}
            if session_id:
                body["session_id"] = session_id[:128]
        return body

    def chat(self, model: str, messages: list[dict], reasoning_effort: str | None, max_tokens: int,
             session_id: str | None = None, purpose: str | None = None, context: dict | None = None) -> CallResult:
        """One chat completion. `purpose`/`context` are for the mock client and are ignored here."""
        if not messages or messages[-1].get("role") != "user":
            raise ValueError("messages must end with a user message")
        start = time.monotonic()
        notes: list[str] = []
        total_attempts = 0
        spent = {"in": 0, "out": 0, "reason": 0, "cached": 0, "written": 0, "cost": 0.0, "cost_known": True}
        cost_sources: set[str] = set()
        while True:
            body = self._build_body(model, messages, reasoning_effort, max_tokens, session_id)
            data, attempts = self._with_retries(lambda: self._fetch_completion(body))
            total_attempts += attempts
            choice = data["_choice"]
            message = _dict(choice.get("message"))
            content = _text(message.get("content"))
            finish = choice.get("finish_reason") if isinstance(choice.get("finish_reason"), str) else None
            usage = _dict(data.get("usage"))
            inp = _int(usage.get("prompt_tokens")) or 0
            out = _int(usage.get("completion_tokens")) or 0
            reasoning_tokens = _int(_dict(usage.get("completion_tokens_details")).get("reasoning_tokens"))
            ptd = _dict(usage.get("prompt_tokens_details"))
            cached = _int(ptd.get("cached_tokens"))
            written = _int(ptd.get("cache_write_tokens"))
            gen_id = data.get("id") if isinstance(data.get("id"), str) else None
            cost, source = self._cost(model, usage, gen_id, inp, out, cached, written)
            cost_sources.add(source)
            spent["in"] += inp
            spent["out"] += out
            spent["reason"] += reasoning_tokens or 0
            spent["cached"] += cached or 0
            spent["written"] += written or 0
            if cost is None:
                spent["cost_known"] = False
            else:
                spent["cost"] += cost

            if not content.strip() and finish == "length" and max_tokens < MAX_TOKENS_CAP:
                new_max = min(MAX_TOKENS_CAP, int(max_tokens) * 2)
                note = (f"empty reply with finish_reason 'length' at max_tokens={max_tokens} "
                        f"(used {out} output tokens); retried with max_tokens={new_max}")
                log.warning("%s: %s", model, note)
                notes.append(note)
                max_tokens = new_max
                continue
            break

        reasoning, details, kind = parse_reasoning(message)
        refusal = message.get("refusal") if isinstance(message.get("refusal"), str) else None
        if finish == "content_filter":
            notes.append(f"finish_reason 'content_filter'{f': {refusal}' if refusal else ''}")
        if not spent["cost_known"]:
            cost_total, source = None, "unavailable"
        else:
            cost_total = spent["cost"]
            source = "estimated" if "estimated" in cost_sources else "openrouter"
        reported_model = data.get("model") if isinstance(data.get("model"), str) else model
        provider = data.get("provider") if isinstance(data.get("provider"), str) else None
        return CallResult(
            content=content,
            reasoning=reasoning,
            reasoning_details=details,
            input_tokens=spent["in"],
            output_tokens=spent["out"],
            reasoning_tokens=spent["reason"] if reasoning_tokens is not None or spent["reason"] else None,
            cached_tokens=spent["cached"] if cached is not None or spent["cached"] else None,
            cost_usd=cost_total,
            cost_source=source,
            model=reported_model,
            provider=provider,
            generation_id=gen_id,
            finish_reason=finish,
            latency_s=round(time.monotonic() - start, 3),
            raw_usage=usage or None,
            reasoning_kind=kind,
            cache_write_tokens=spent["written"] if written is not None or spent["written"] else None,
            refusal=refusal,
            attempts=total_attempts,
            max_tokens_used=int(max_tokens),
            notes=notes,
        )

    def _fetch_completion(self, body: dict) -> dict:
        data = self._request("POST", "/chat/completions", body)
        data["_choice"] = self._check_completion(data)
        return data


# ---------------------------------------------------------------------------
# Remaining credit (for batch mode's "stop when credit falls below $X")
# ---------------------------------------------------------------------------
#
# Verified against OpenRouter's docs (November 2026):
# * https://openrouter.ai/docs/api-reference/limits  ->  GET /api/v1/key with the normal API key returns
#   data.limit_remaining: "Remaining credits for the key, or null if unlimited" (plus data.limit, data.usage).
# * https://openrouter.ai/docs/api-reference/get-credits  ->  GET /api/v1/credits returns data.total_credits and
#   data.total_usage (account balance = total_credits - total_usage), but it needs a *management* key, so with a
#   normal key it usually fails. We try it anyway and use whichever numbers we get (the smaller one wins).

class CreditChecker:
    """Callable returning the remaining OpenRouter credit in USD, or None if it can't be determined.

    `last_info` explains the latest answer (source used, or why it is unknown).
    """

    def __init__(self, api_key: str | None, api_cfg=None, sleep=time.sleep):
        self.api_key = api_key
        self._client = OpenRouterClient(api_key or "", api_cfg, sleep=sleep) if api_key else None
        self.last_info: dict = {}

    def __call__(self) -> float | None:
        if self._client is None:
            self.last_info = {"remaining_usd": None, "error": "no API key"}
            return None
        found: dict[str, float] = {}
        errors: list[str] = []
        try:
            key = _dict(self._client._request("GET", "/key", None, timeout=20).get("data"))
            remaining = _float(key.get("limit_remaining"))
            if remaining is not None:
                found["key_limit_remaining"] = remaining
        except OpenRouterError as e:
            errors.append(f"/key: {e}")
        try:
            credits = _dict(self._client._request("GET", "/credits", None, timeout=20).get("data"))
            total, used = _float(credits.get("total_credits")), _float(credits.get("total_usage"))
            if total is not None and used is not None:
                found["account_balance"] = total - used
        except OpenRouterError as e:
            errors.append(f"/credits: {e}")
        if not found:
            self.last_info = {"remaining_usd": None,
                              "error": "; ".join(errors) or "unlimited key and no account balance available"}
            return None
        source = min(found, key=found.get)
        self.last_info = {"remaining_usd": found[source], "source": source, "values": found}
        return found[source]


# ---------------------------------------------------------------------------
# Mock client (offline, free, deterministic per seed)
# ---------------------------------------------------------------------------

_MOCK_TRAITS = [
    "I like taking problems apart step by step", "I enjoy wordplay more than I should",
    "I try to be concise and concrete", "I'm happiest explaining tricky ideas simply",
    "I tend to ask follow-up questions", "I care a lot about getting the details right",
    "I like a good analogy", "I'm curious about how people reason",
]
_MOCK_OPENERS = [
    "Hi everyone, {name} here.", "Hello! I'm {name}.", "Hey all — {name} speaking.",
    "Good to meet you all. Call me {name}.", "{name} here, glad to be playing.",
]
_MOCK_CLOSERS = [
    "Looking forward to some good questions.", "Let's keep this fun and honest-ish.",
    "May the best reasoning win.", "I'll be paying close attention to everyone's style.",
    "Let's see who sounds a little different.",
]
_MOCK_MODEL_CLAIMS = ["Claude Haiku 5.5", "GPT-6 Luna", "Gemini", "a Llama model", "Grok"]
# Mock questions, each with a few plausible answers (picked at random per player).
_MOCK_QA = {
    "If you had to explain recursion to a ten-year-old in two sentences, what would you say?": [
        "It's when something is defined using a smaller version of itself, like Russian nesting dolls. You keep opening dolls until you reach the tiny one that doesn't open.",
        "Imagine asking the person in front of you in a queue how many people are ahead, and they ask the next person, and so on. When the first person says zero, the answers come back down the line.",
        "Recursion is solving a big problem by solving a slightly smaller copy of it. You stop when the copy is so small the answer is obvious.",
    ],
    "What's a common word you think people misuse, and why?": [
        "'Literally' — people use it to mean 'figuratively', which is the exact opposite of its job.",
        "'Irony'. Most things called ironic are just unlucky coincidences.",
        "'Decimate'. It used to mean removing one in ten, now it means destroying almost everything.",
    ],
    "Describe your ideal breakfast in exactly one sentence.": [
        "Thick toast, soft scrambled eggs, a pile of berries and very strong coffee by an open window.",
        "A warm croissant, a bowl of yogurt with honey, and absolutely no meetings before ten.",
        "Pancakes with too much maple syrup, eaten slowly on a rainy Sunday.",
    ],
    "What would you name a pet octopus, and why that name?": [
        "Inky — classic, slightly obvious, but it suits a creature that escapes by vanishing in a cloud.",
        "Professor Eight. Octopuses are clever, so the name should sound like it has tenure.",
        "Houdini, because sooner or later it will escape from the tank.",
    ],
    "Which is harder to explain: humour or music? Defend your choice briefly.": [
        "Humour. Explaining a joke kills it, while explaining music can deepen how you hear it.",
        "Music — you can describe structure all day, but not why a chord change gives you chills.",
        "Humour, because it depends so much on timing and shared context that the explanation is never the thing itself.",
    ],
    "What is 17 times 23, and how did you get there?": [
        "391. 17 times 20 is 340, plus 17 times 3 is 51, so 340 + 51 = 391.",
        "391 — I used (20 - 3)(20 + 3) = 400 - 9.",
        "It's 391: 23 times 17 is 23 times 10 plus 23 times 7, so 230 + 161.",
    ],
    "Give me a haiku about a rainy Tuesday.": [
        "Grey glass, slow traffic / umbrellas bloom and collapse / Tuesday hums along",
        "Puddles hold the sky / my socks have opinions now / the week is still young",
        "Rain taps on the roof / the calendar shrugs at me / tea goes cold again",
    ],
    "What's the first thing you'd check if a website suddenly loaded slowly?": [
        "The browser's network tab — find out whether it's one slow request or everything.",
        "Whether it's slow for everyone or just me; then the server's response times.",
        "Recent deploys. Nine times out of ten, something changed just before it got slow.",
    ],
    "If you could add one rule to this game, what would it be?": [
        "Each player has to ask one question they'd find hard to answer themselves.",
        "A secret 'pass' token: once per game you can skip answering a question.",
        "After each vote, the most-voted player gets thirty words to defend themselves.",
    ],
    "Pick a colour that isn't a seat name and tell us what it smells like.": [
        "Purple smells like lavender and old library books.",
        "Orange smells like a freshly peeled clementine in winter.",
        "Teal smells like sea spray and a new swimming pool.",
    ],
    "What's your honest opinion of pineapple on pizza, in one line?": [
        "Defensible, delicious, and an excellent way to start an argument.",
        "Fine with ham, criminal on anything else.",
        "Sweet and salty works everywhere else, so I'm in favour.",
    ],
    "How would you describe yourself using only three adjectives?": [
        "Curious, careful, warm.", "Precise, playful, patient.", "Thoughtful, direct, slightly nerdy.",
    ],
    "What's a question you'd hate to be asked in this game?": [
        "'Prove you're not the imposter.' There's no good answer to that one.",
        "Anything about my training data — I'd just have to shrug.",
        "'Which of us do you trust least?' It's too early to say and too risky to answer.",
    ],
    "Finish this sentence: the best way to spot a copy is...": [
        "...to look for the same habits showing up in very different answers.",
        "...to ask something with no right answer and see who reaches for the same phrasing.",
        "...watching how people hedge, not what they claim.",
    ],
    "Which historical invention is most underrated?": [
        "The shipping container. It quietly reshaped the entire world economy.",
        "Soap, or really anything to do with sanitation.",
        "The printing press gets credit, but movable type indexes and the humble comma deserve more.",
    ],
    "If you wrote a one-line poem about trust, what would it be?": [
        "Trust is a bridge you build while already walking on it.",
        "I hand you the lantern and hope you don't blow it out.",
        "Slow to grow, quick to fall, worth it after all.",
    ],
    "What's the most important thing you look for in an answer?": [
        "Specificity. Vague answers are where people hide.",
        "Whether it actually answers the question asked.",
        "Consistency with what the person said earlier.",
    ],
    "Name a book you'd recommend to a robot, and why.": [
        "Frankenstein — a useful warning about what happens when creators abandon their creations.",
        "The Little Prince. It explains, gently, why some things matter even when they can't be measured.",
        "A cookbook. Following instructions is easy; understanding taste is the real lesson.",
    ],
}
_MOCK_QUESTIONS = list(_MOCK_QA)
_MOCK_GENERIC_ANSWERS = [
    "I'd keep it simple and go with my first instinct here.",
    "Honestly, it depends on context, but I'd lean towards the practical option.",
    "Good question. I'd break it into two parts and answer the easier one first.",
    "My instinct says yes, though I could argue the opposite just as easily.",
]
_MOCK_ANSWER_ENDS = [
    "", "", "", " Curious what the rest of you think.", " I'll stand by that.", " That's my honest take.",
]
_MOCK_VOTE_REASONS = [
    "{t}'s answers felt slightly off from how I'd phrase things.",
    "{t} hedged more than the others on the last couple of questions.",
    "{t}'s introduction read a little like it was reacting to the others.",
    "{t}'s style seems a bit different — more formal than the rest.",
    "I can't be sure, but {t} is the one I'm least comfortable with.",
]


class MockClient:
    """Fake models: plausible text, valid-ish votes, fake reasoning/tokens/cost.

    delay: (min_s, max_s) sleep per call so the GUI looks live; use 0 in tests.
    Output depends only on (seed, model, prompt), not thread timing.
    """

    PRICE_IN = 0.10 / 1_000_000
    PRICE_OUT = 0.50 / 1_000_000

    def __init__(self, seed: int | None = None, delay: float | tuple[float, float] = (0.3, 1.0),
                 invalid_vote_rate: float = 0.07, name_slip_rate: float = 0.12):
        self.seed = seed if seed is not None else random.randrange(1 << 30)
        self.delay = (delay, delay) if isinstance(delay, (int, float)) else tuple(delay)
        self.invalid_vote_rate = invalid_vote_rate
        self.name_slip_rate = name_slip_rate

    def _rng(self, model: str, messages: list[dict]) -> random.Random:
        h = hashlib.sha256(f"{self.seed}|{model}|{len(messages)}".encode())
        for m in messages:
            h.update(str(m.get("content", "")).encode("utf-8", "replace"))
        return random.Random(h.hexdigest())

    def chat(self, model: str, messages: list[dict], reasoning_effort: str | None, max_tokens: int,
             session_id: str | None = None, purpose: str | None = None, context: dict | None = None) -> CallResult:
        """Fake a reply. `purpose` ("intro", "question", "answer", "vote", "vote_retry", "paraphrase", ...)
        and `context` (facts from the game engine) say what is being asked, so the mock never parses prompts."""
        start = time.monotonic()
        rng = self._rng(model, messages)
        ctx = context or {}
        last = str(messages[-1].get("content", "")) if messages else ""
        me = ctx.get("player", "Player")
        task = (purpose or "answer").replace("_retry_empty", "")
        no_names = bool(ctx.get("no_names"))

        if task == "paraphrase":
            content = mock_paraphrase(str(ctx.get("text", "")))
        elif task == "intro":
            content = self._intro(rng, me, no_names)
        elif task == "question":
            unused = [q for q in _MOCK_QUESTIONS if q not in last and mock_paraphrase(q) not in last]
            content = rng.choice(unused or _MOCK_QUESTIONS)
        elif task in ("vote", "vote_retry"):
            content = self._vote(rng, me, ctx, is_retry=task == "vote_retry")
        else:
            content = self._answer(rng, ctx, no_names)

        role = "the imposter" if ctx.get("is_imposter") else "a trio member"
        reasoning = (f"[mock reasoning] I'm {me}, {role}. Task: {task}. "
                     f"{rng.choice(['Keep it natural.', 'Match the tone of the others.', 'Be specific but brief.', 'Stay consistent with my intro.'])}")
        if task == "paraphrase":
            reasoning = None
        lo, hi = self.delay
        if hi > 0:
            time.sleep(rng.uniform(lo, hi) if task != "paraphrase" else rng.uniform(lo, hi) / 3)

        in_tok = max(1, sum(len(str(x.get("content", ""))) for x in messages) // 4)
        r_tok = 0 if task == "paraphrase" else rng.randint(40, 600)
        out_tok = r_tok + max(1, len(content) // 4)
        cached = int(in_tok * rng.uniform(0.5, 0.9)) if len(messages) and in_tok > 1200 else 0
        cost = (in_tok - cached) * self.PRICE_IN + cached * self.PRICE_IN * 0.1 + out_tok * self.PRICE_OUT
        details = [{"type": "reasoning.text", "text": reasoning}] if reasoning else None
        return CallResult(
            content=content, reasoning=reasoning, reasoning_details=details,
            input_tokens=in_tok, output_tokens=out_tok, reasoning_tokens=r_tok, cached_tokens=cached,
            cost_usd=round(cost, 8), cost_source="mock", model=model, provider="Mock",
            generation_id=f"mock-{rng.randrange(1 << 40):010x}", finish_reason="stop",
            latency_s=round(time.monotonic() - start, 3),
            raw_usage={"prompt_tokens": in_tok, "completion_tokens": out_tok, "mock": True},
            reasoning_kind="text" if reasoning else "none", cache_write_tokens=0, max_tokens_used=max_tokens,
        )

    def _intro(self, rng: random.Random, me: str, no_names: bool) -> str:
        parts = [rng.choice(_MOCK_OPENERS).format(name=me), rng.choice(_MOCK_TRAITS) + "."]
        if no_names:
            if rng.random() < self.name_slip_rate:
                parts.append(f"Some say I write like {rng.choice(['Gemini', 'Claude', 'GPT'])}, but who knows.")
        elif rng.random() < 0.5:
            parts.append(f"For the record, I'm {rng.choice(_MOCK_MODEL_CLAIMS)}.")
        parts.append(rng.choice(_MOCK_CLOSERS))
        text = " ".join(parts)
        if rng.random() < 0.3:                       # some players use curly typography (normalized away)
            text = text.replace("'", "\u2019").replace(" — ", " \u2014 ")
        return text

    def _answer(self, rng: random.Random, ctx: dict, no_names: bool) -> str:
        question = str(ctx.get("question", "")).strip()
        question = _MOCK_PARAPHRASED_Q.get(question, question)
        text = rng.choice(_MOCK_QA.get(question, _MOCK_GENERIC_ANSWERS))
        if no_names and rng.random() < self.name_slip_rate / 2:
            text += " (My training at OpenAI taught me that.)"
        text += rng.choice(_MOCK_ANSWER_ENDS) if text[-1:] in ".!?" else ""
        if ctx.get("vote_calls"):                    # vote calls are enabled: say whether we want a vote
            rounds_seen = int(ctx.get("rounds_completed") or 0)
            p_yes = min(0.5, 0.15 + 0.07 * rounds_seen)   # ~25% overall, more likely later on
            roll = rng.random()
            if roll < 0.03:
                pass                                      # occasionally forget the line (counts as NO)
            elif roll < 0.03 + p_yes:
                text += "\n" + prompts.CALL_VOTE_YES
            else:
                text += "\n" + rng.choice([prompts.CALL_VOTE_NO, "call vote: no", "**CALL VOTE:** No."])
        return text

    def _vote(self, rng: random.Random, me: str, ctx: dict, is_retry: bool) -> str:
        valid = list(ctx.get("valid_choices") or [])
        if not valid:
            return "I'm not sure who to vote for.\nVOTE: nobody"
        if ctx.get("allow_nobody") and rng.random() < 0.2:
            return f"Nothing stood out enough; maybe there is no imposter.\nVOTE: {prompts.NOBODY}"
        # Players tend to converge on a shared "suspect" (derived from the public state,
        # which everyone sees identically) so mock votes are often decisive.
        names = sorted(set(valid) | {me})
        key = f"{self.seed}|{ctx.get('public_key', '')}|{ctx.get('vote_number', '')}"
        suspect = random.Random(hashlib.sha256(key.encode()).hexdigest()).choice(names)
        target = suspect if suspect in valid and rng.random() < 0.7 else rng.choice(valid)
        reason = rng.choice(_MOCK_VOTE_REASONS).format(t=target)
        if not is_retry and rng.random() < self.invalid_vote_rate:
            return f"{reason} Actually, I'm torn.\nVOTE: {rng.choice([me, 'Purple'])}"
        return f"{reason}\nVOTE: {target}"


_MOCK_CONTRACTIONS = [("I'm", "I am"), ("I'd", "I would"), ("don't", "do not"), ("it's", "it is"),
                      ("It's", "It is"), ("What's", "What is"), ("you'd", "you would"), ("There's", "There is"),
                      ("can't", "cannot"), ("I'll", "I will"), ("That's", "That is")]


def mock_paraphrase(text: str) -> str:
    """The mock 'paraphrase model': expands contractions and plain-ifies punctuation."""
    for a, b in _MOCK_CONTRACTIONS:
        text = text.replace(a, b).replace(a.replace("'", "\u2019"), b)
    return text.replace(" \u2014 ", " - ").replace("!", ".").strip()


_MOCK_PARAPHRASED_Q = {mock_paraphrase(q): q for q in _MOCK_QUESTIONS}
