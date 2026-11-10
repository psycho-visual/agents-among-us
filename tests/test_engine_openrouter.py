"""Tests for OpenRouterClient response handling, using a fake transport (no network)."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from imposter_game import openrouter  # noqa: E402
from imposter_game.config import ApiConfig  # noqa: E402
from imposter_game.openrouter import OpenRouterClient, OpenRouterError, parse_reasoning  # noqa: E402

logging.getLogger("imposter_game.openrouter").setLevel(logging.ERROR)

MSGS = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]


def completion(content="Hello", finish="stop", usage=None, reasoning=None, details=None, **extra):
    msg = {"role": "assistant", "content": content}
    if reasoning is not None:
        msg["reasoning"] = reasoning
    if details is not None:
        msg["reasoning_details"] = details
    body = {"id": "gen-1", "model": "x/y", "provider": "Prov",
            "choices": [{"message": msg, "finish_reason": finish}],
            "usage": usage if usage is not None else {
                "prompt_tokens": 100, "completion_tokens": 50, "cost": 0.001,
                "completion_tokens_details": {"reasoning_tokens": 30},
                "prompt_tokens_details": {"cached_tokens": 40, "cache_write_tokens": 10}}}
    body.update(extra)
    return body


class FakeClient(OpenRouterClient):
    """Replays a scripted list of responses (dicts) or exceptions for each POST."""

    def __init__(self, script, gets=None, max_retries=3):
        super().__init__("sk-test", ApiConfig(max_retries=max_retries), sleep=lambda s: self.sleeps.append(s))
        self.sleeps: list[float] = []
        self.script = list(script)
        self.gets = dict(gets or {})
        self.bodies: list[dict] = []

    def _request(self, method, path, body, timeout=None):
        if method == "GET":
            for prefix, value in self.gets.items():
                if path.startswith(prefix):
                    if isinstance(value, Exception):
                        raise value
                    return value
            raise OpenRouterError("not found", status=404)
        self.bodies.append(body)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return dict(item)


class TestOpenRouterClient(unittest.TestCase):
    def setUp(self):
        openrouter._PRICES = None

    def test_parses_everything(self):
        c = FakeClient([completion(details=[{"type": "reasoning.summary", "summary": "I thought."}])])
        r = c.chat("anthropic/claude-haiku-5.5", MSGS, "high", 1000, session_id="g-Red")
        self.assertEqual(r.content, "Hello")
        self.assertEqual(r.reasoning, "I thought.")
        self.assertEqual(r.reasoning_kind, "summary")
        self.assertEqual((r.input_tokens, r.output_tokens, r.reasoning_tokens, r.cached_tokens, r.cache_write_tokens),
                         (100, 50, 30, 40, 10))
        self.assertEqual((r.cost_usd, r.cost_source), (0.001, "openrouter"))
        self.assertEqual((r.model, r.provider, r.generation_id, r.finish_reason), ("x/y", "Prov", "gen-1", "stop"))
        body = c.bodies[0]
        self.assertEqual(body["reasoning"], {"effort": "high"})
        self.assertEqual(body["cache_control"], {"type": "ephemeral"})
        self.assertEqual(body["session_id"], "g-Red")
        for k in ("usage", "temperature", "reasoning_effort", "include_reasoning"):
            self.assertNotIn(k, body)

    def test_non_anthropic_no_cache_control(self):
        c = FakeClient([completion()])
        c.chat("openai/gpt-6-luna", MSGS, None, 1000)
        self.assertNotIn("cache_control", c.bodies[0])
        self.assertNotIn("reasoning", c.bodies[0])

    def test_error_in_200_body_is_retried(self):
        c = FakeClient([{"error": {"code": 502, "message": "upstream"}}, completion()])
        r = c.chat("x/y", MSGS, None, 100)
        self.assertEqual(r.content, "Hello")
        self.assertEqual(r.attempts, 2)
        self.assertEqual(len(c.sleeps), 1)

    def test_non_retryable_error_raises(self):
        c = FakeClient([{"error": {"code": 400, "message": "bad model"}}])
        with self.assertRaises(OpenRouterError):
            c.chat("x/y", MSGS, None, 100)
        c = FakeClient([OpenRouterError("HTTP 401", status=401, retryable=False)])
        with self.assertRaises(OpenRouterError):
            c.chat("x/y", MSGS, None, 100)

    def test_retries_exhausted(self):
        c = FakeClient([OpenRouterError("429", status=429, retryable=True, retry_after=3)] * 3, max_retries=2)
        with self.assertRaises(OpenRouterError):
            c.chat("x/y", MSGS, None, 100)
        self.assertEqual(c.sleeps, [3, 3])

    def test_missing_choices_and_choice_error(self):
        c = FakeClient([{"id": "x"}, {"choices": [{"error": {"code": 503, "message": "x"}}]}, completion()])
        self.assertEqual(c.chat("x/y", MSGS, None, 100).content, "Hello")

    def test_length_retry_doubles_max_tokens(self):
        c = FakeClient([completion(content="", finish="length"), completion()])
        r = c.chat("x/y", MSGS, "max", 20000)
        self.assertEqual([b["max_tokens"] for b in c.bodies], [20000, 40000])
        self.assertEqual(r.content, "Hello")
        self.assertEqual(r.input_tokens, 200)          # both attempts are billed and counted
        self.assertAlmostEqual(r.cost_usd, 0.002)
        self.assertTrue(r.notes)

    def test_length_retry_capped(self):
        c = FakeClient([completion(content="", finish="length"), completion(content="", finish="length")])
        r = c.chat("x/y", MSGS, "max", 40000)
        self.assertEqual([b["max_tokens"] for b in c.bodies], [40000, 64000])
        self.assertEqual(r.content, "")

    def test_cost_from_generation_endpoint(self):
        usage = {"prompt_tokens": 10, "completion_tokens": 5}
        c = FakeClient([completion(usage=usage)], gets={"/generation": {"data": {"total_cost": 0.5}}})
        r = c.chat("x/y", MSGS, None, 100)
        self.assertEqual((r.cost_usd, r.cost_source), (0.5, "openrouter"))

    def test_cost_estimated_from_prices(self):
        usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
        c = FakeClient([completion(usage=usage)],
                       gets={"/models": {"data": [{"id": "x/y", "pricing": {"prompt": "0.0000001",
                                                                            "completion": "0.0000005"}}]}})
        r = c.chat("x/y", MSGS, None, 100)
        self.assertEqual(r.cost_source, "estimated")
        self.assertAlmostEqual(r.cost_usd, 0.6)

    def test_cost_unavailable(self):
        c = FakeClient([completion(usage={})], gets={"/models": {"data": []}})
        r = c.chat("x/y", MSGS, None, 100)
        self.assertEqual((r.cost_usd, r.cost_source, r.input_tokens), (None, "unavailable", 0))

    def test_garbage_fields(self):
        body = {"choices": [{"message": {"content": None, "reasoning": 5, "reasoning_details": "x"},
                             "finish_reason": None}], "usage": {"prompt_tokens": "12", "cost": "bad"},
                "model": None}
        c = FakeClient([body], gets={"/models": {"data": "nope"}})
        r = c.chat("x/y", MSGS, None, 100)
        self.assertEqual(r.content, "")
        self.assertIsNone(r.reasoning)
        self.assertEqual(r.input_tokens, 12)
        self.assertEqual(r.model, "x/y")

    def test_content_filter(self):
        body = completion(content="", finish="content_filter")
        body["choices"][0]["message"]["refusal"] = "nope"
        c = FakeClient([body])
        r = c.chat("x/y", MSGS, None, 100)
        self.assertEqual(r.refusal, "nope")
        self.assertTrue(any("content_filter" in n for n in r.notes))

    def test_must_end_with_user(self):
        c = FakeClient([])
        with self.assertRaises(ValueError):
            c.chat("x/y", MSGS + [{"role": "assistant", "content": "x"}], None, 100)


class TestRealHttp(unittest.TestCase):
    """Exercise the urllib code path against a local HTTP server."""

    def test_http_429_then_success_and_401(self):
        responses = [(429, {"error": {"code": 429, "message": "slow down"}}, {"Retry-After": "0"}),
                     (200, completion(), {}),
                     (401, {"error": {"code": 401, "message": "No auth credentials found"}}, {})]
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.path, self.headers.get("Authorization"),
                             json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                status, body, headers = responses.pop(0)
                data = json.dumps(body).encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            cfg = ApiConfig(base_url=f"http://127.0.0.1:{server.server_port}/api/v1", timeout_seconds=10)
            sleeps = []
            client = OpenRouterClient("sk-test", cfg, sleep=sleeps.append)
            with mock.patch.dict(os.environ, {"no_proxy": "*", "NO_PROXY": "*"}):
                r = client.chat("x/y", MSGS, "low", 100)
                self.assertEqual(r.content, "Hello")
                self.assertEqual(sleeps, [0.5])     # Retry-After 0, floored to 0.5s
                self.assertEqual(seen[0][0], "/api/v1/chat/completions")
                self.assertEqual(seen[0][1], "Bearer sk-test")
                with self.assertRaises(OpenRouterError) as ctx:
                    client.chat("x/y", MSGS, "low", 100)
                self.assertEqual(ctx.exception.status, 401)
                self.assertIn("API key", str(ctx.exception))
        finally:
            server.shutdown()
            server.server_close()


class TestParseReasoning(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(parse_reasoning({"reasoning": "abc"})[2], "text")
        self.assertEqual(parse_reasoning({"reasoning_details": [{"type": "reasoning.text", "text": "t"}]})[:1], ("t",))
        self.assertEqual(parse_reasoning({"reasoning_details": [{"type": "reasoning.encrypted", "data": "x"}]})[2],
                         "encrypted_only")
        self.assertEqual(parse_reasoning({})[2], "none")
        self.assertEqual(parse_reasoning({"reasoning": "s", "reasoning_details": [
            {"type": "reasoning.summary", "summary": "s"}]})[2], "summary")


if __name__ == "__main__":
    unittest.main()
