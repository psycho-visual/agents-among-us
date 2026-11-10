"""Tests for the web server (imposter_game/server.py) using the real engine in mock mode.

Run with:  python -m unittest discover -s tests -v
Logs go to a temporary directory, never to the repo's logs/.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from imposter_game.server import GameController, make_server  # noqa: E402

CONFIG = ROOT / "config.toml"


class SSEReader(threading.Thread):
    """Collects Server-Sent Events from /api/events in a background thread."""

    def __init__(self, url: str):
        super().__init__(daemon=True)
        self.url = url
        self.events: list[dict] = []
        self.cond = threading.Condition()
        self.closed = False
        self.raw_lines: list[str] = []

    def run(self) -> None:
        try:
            with urllib.request.urlopen(self.url, timeout=30) as resp:
                self.content_type = resp.headers.get("Content-Type", "")
                for raw in resp:
                    line = raw.decode("utf-8").rstrip("\r\n")
                    self.raw_lines.append(line)
                    if line.startswith("data: "):
                        ev = json.loads(line[6:])
                        with self.cond:
                            self.events.append(ev)
                            self.cond.notify_all()
        except Exception:
            pass
        finally:
            with self.cond:
                self.closed = True
                self.cond.notify_all()

    def wait_for(self, pred, timeout: float = 60.0) -> dict | None:
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                for ev in self.events:
                    if pred(ev):
                        return ev
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self.closed:
                    return None
                self.cond.wait(remaining)

    def types(self) -> list[str]:
        with self.cond:
            return [e.get("type") for e in self.events]


class ServerTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="aau-test-logs-"))
        cls.prompts_dir = cls.tmp / "prompts"  # never the repo's real prompts/ folder
        cls.controller = GameController(CONFIG, mock=True, log_dir=cls.tmp, mock_options={"delay": 0},
                                        seed=7, keepalive_seconds=1.0, prompts_dir=cls.prompts_dir)
        cls.server = make_server(cls.controller, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.controller.wait_idle(30)
        cls.server.close_all()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def tearDown(self) -> None:
        # Never leave a runner thread going into the next test.
        self.controller.pause()
        self.assertTrue(self.controller.wait_idle(30))

    # -- helpers
    def get(self, path: str, headers: dict | None = None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def get_json(self, path: str):
        status, _, body = self.get(path)
        return status, json.loads(body)

    def post(self, path: str, headers: dict | None = None, body: object = None):
        data = b"{}" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode("utf-8"))
        req = urllib.request.Request(self.base + path, data=data, method="POST",
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def sse(self) -> SSEReader:
        r = SSEReader(self.base + "/api/events")
        r.start()
        return r

    def wait_state(self, states, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _, st = self.get_json("/api/state")
            if st["state"] in states:
                return st
            time.sleep(0.02)
        self.fail(f"state never became {states}; last={st}")


class StaticAndInfoTests(ServerTestBase):
    def test_index_and_static_files(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"Agents Among Us", body)
        for name, ctype in (("app.js", "text/javascript"), ("style.css", "text/css"), ("index.html", "text/html")):
            status, headers, _ = self.get("/static/" + name)
            self.assertEqual(status, 200, name)
            self.assertIn(ctype, headers["Content-Type"])

    def test_no_directory_traversal(self):
        for path in ("/static/../run.py", "/static/%2e%2e/run.py", "/static/..%2frun.py",
                     "/static/%2e%2e%2fimposter_game%2fserver.py", "/static/", "/static/nope.js", "/api/nope"):
            status, _, body = self.get(path)
            self.assertEqual(status, 404, path)
            self.assertNotIn(b"import", body)

    def test_config_never_leaks_key(self):
        status, cfg = self.get_json("/api/config")
        self.assertEqual(status, 200)
        self.assertTrue(cfg["mock"])
        self.assertIn("api_key_present", cfg)
        self.assertIsNone(cfg["config_error"])
        self.assertIn("trio", cfg["config"])
        self.assertNotIn("api_key", json.dumps(cfg["config"]))

    def test_foreign_origin_rejected(self):
        status, _ = self.post("/api/pause", headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, _, _ = self.get("/api/state", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)

    def test_stats_endpoint_shape(self):
        status, data = self.get_json("/api/stats")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertTrue(data["mock"])
        self.assertIn("num_games", data)


class GameFlowTests(ServerTestBase):
    def test_full_mock_game_via_run_all(self):
        status, res = self.post("/api/new_game")
        self.assertEqual(status, 200, res)
        game_id = res["status"]["game_id"]
        self.assertTrue(game_id)
        self.assertEqual(res["status"]["state"], "idle")
        self.assertTrue(res["status"]["next_label"])

        reader = self.sse()
        first_status = reader.wait_for(lambda e: e.get("type") == "status", 10)
        self.assertIsNotNone(first_status)
        self.assertEqual(first_status["game_id"], game_id)

        status, res = self.post("/api/run_all")
        self.assertEqual(status, 200, res)
        over = reader.wait_for(lambda e: e.get("type") == "game_over", 120)
        self.assertIsNotNone(over, f"no game_over; got {reader.types()[-20:]}")
        self.assertIn(over["outcome"], ("trio_win", "imposter_win", "no_decision"))
        st = self.wait_state({"finished"})
        self.assertEqual(st["next_label"], "")
        self.assertIsNotNone(reader.wait_for(lambda e: e.get("type") == "status" and e.get("state") == "finished", 10))

        types = reader.types()
        for t in ("game_started", "phase", "thinking", "message", "api_call", "vote_cast", "vote_result", "game_over"):
            self.assertIn(t, types)
        engine_events = [e for e in reader.events if e.get("type") != "status"]
        self.assertTrue(all(e.get("game_id") == game_id for e in engine_events))
        seqs = [e["seq"] for e in engine_events if isinstance(e.get("seq"), int)]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)), "duplicate events")

        # Running a finished game is refused politely.
        status, res = self.post("/api/run_all")
        self.assertEqual(status, 409)
        self.assertIn("over", res["message"])

        # Log endpoint points at real files in the temp log dir.
        _, log = self.get_json("/api/log")
        self.assertEqual(log["game_id"], game_id)
        self.assertTrue(log["log_file"] and Path(log["log_file"]).exists(), log)
        self.assertTrue(str(log["log_file"]).startswith(str(self.tmp)))

        # Stats now include this mock game.
        _, stats = self.get_json("/api/stats")
        self.assertTrue(stats["ok"], stats)
        self.assertGreaterEqual(stats["num_games"], 1)
        self.assertIsNotNone(stats["stats"])

        # A late client gets the whole game replayed, then a status event.
        late = self.sse()
        last = late.wait_for(lambda e: e.get("type") == "status", 10)
        self.assertIsNotNone(last)
        late_types = late.types()
        self.assertEqual(late_types[-1], "status")
        self.assertEqual(late_types[:-1], [e["type"] for e in engine_events])

    def test_pause_and_next_round(self):
        self.controller.mock_options = {"delay": 0.05}
        try:
            status, res = self.post("/api/new_game")
            self.assertEqual(status, 200, res)
            reader = self.sse()

            status, _ = self.post("/api/run_all")
            self.assertEqual(status, 200)
            # New game is refused while running.
            status, res = self.post("/api/new_game")
            self.assertEqual(status, 409)
            # Only one runner at a time.
            status, _ = self.post("/api/next_round")
            self.assertEqual(status, 409)

            status, res = self.post("/api/pause")
            self.assertEqual(status, 200, res)
            self.assertIn(res["status"]["state"], ("pausing", "paused"))
            st = self.wait_state({"paused"})
            self.assertFalse(st["finished"])
            self.assertTrue(st["next_label"])
            self.assertIsNotNone(reader.wait_for(lambda e: e.get("type") == "status" and e.get("state") == "pausing", 5))
            self.assertIsNotNone(reader.wait_for(lambda e: e.get("type") == "status" and e.get("state") == "paused", 5))
            n_events = len(reader.events)
            time.sleep(0.4)
            self.assertEqual(len(reader.events), n_events, "events kept arriving while paused")
            status, _ = self.post("/api/pause")
            self.assertEqual(status, 409)  # nothing to pause

            # Next round runs to the next boundary and stops by itself.
            status, _ = self.post("/api/next_round")
            self.assertEqual(status, 200)
            st = self.wait_state({"paused", "finished"})

            # Then finish the game.
            if st["state"] != "finished":
                status, _ = self.post("/api/run_all")
                self.assertEqual(status, 200)
            self.assertIsNotNone(reader.wait_for(lambda e: e.get("type") == "game_over", 120))
            self.wait_state({"finished"})
        finally:
            self.controller.mock_options = {"delay": 0}

    def test_next_round_stops_at_boundaries(self):
        status, res = self.post("/api/new_game")
        self.assertEqual(status, 200, res)
        labels = []
        for _ in range(20):
            _, st = self.get_json("/api/state")
            if st["finished"]:
                break
            labels.append(st["next_label"])
            status, _ = self.post("/api/next_round")
            self.assertEqual(status, 200)
            self.wait_state({"paused", "finished"})
        self.assertTrue(self.get_json("/api/state")[1]["finished"])
        self.assertGreaterEqual(len(labels), 3, labels)  # intros, round(s), vote

    def test_upgrade_round_to_run_all(self):
        self.controller.mock_options = {"delay": 0.05}
        try:
            self.post("/api/new_game")
            reader = self.sse()
            self.assertEqual(self.post("/api/next_round")[0], 200)
            status, res = self.post("/api/run_all")  # upgrade while running
            self.assertEqual(status, 200, res)
            self.assertIsNotNone(reader.wait_for(lambda e: e.get("type") == "game_over", 120))
        finally:
            self.controller.mock_options = {"delay": 0}


class ConfigErrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aau-test-cfg-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_config_file(self):
        c = GameController(self.tmp / "nope.toml", mock=True, log_dir=self.tmp)
        ok, msg = c.new_game()
        self.assertFalse(ok)
        self.assertIn("nope.toml", msg)
        self.assertEqual(c.status()["config_error"], msg)
        self.assertFalse(c.status()["has_game"])

    def test_invalid_config_reported_and_recovers(self):
        cfg = self.tmp / "config.toml"
        cfg.write_text(CONFIG.read_text(encoding="utf-8").replace("vote_threshold = 3", "vote_threshold = 9"),
                       encoding="utf-8")
        c = GameController(cfg, mock=True, log_dir=self.tmp, mock_options={"delay": 0})
        ok, msg = c.new_game()
        self.assertFalse(ok)
        self.assertIn("vote_threshold", msg)
        # Fix the file: the next "New game" re-reads it from disk.
        shutil.copy(CONFIG, cfg)
        ok, msg = c.new_game()
        self.assertTrue(ok, msg)
        self.assertIsNone(c.status()["config_error"])


def _engine_supports_vote_calls() -> bool:
    try:
        from imposter_game.config import RulesConfig
        return hasattr(RulesConfig(), "player_vote_calls")
    except Exception:
        return False


def _config_with_rules(extra: dict) -> str:
    """config.toml text with some [rules] keys overridden."""
    lines = CONFIG.read_text(encoding="utf-8").splitlines()
    out, in_rules = [], False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            in_rules = stripped == "[rules]"
            out.append(line)
            if in_rules:
                out.extend(f"{k} = {json.dumps(v)}" for k, v in extra.items())
            continue
        if in_rules and stripped.split("=")[0].strip() in extra:
            continue  # drop the original value of an overridden key
        out.append(line)
    return "\n".join(out) + "\n"


@unittest.skipUnless(_engine_supports_vote_calls(), "engine has no player_vote_calls yet")
class VoteCallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aau-test-votecall-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _play(self, rules: dict, seed: int) -> list[dict]:
        cfg = self.tmp / f"config-{seed}.toml"
        cfg.write_text(_config_with_rules(rules), encoding="utf-8")
        c = GameController(cfg, mock=True, log_dir=self.tmp, mock_options={"delay": 0}, seed=seed)
        ok, msg = c.new_game()
        self.assertTrue(ok, msg)
        q, _ = c.subscribe()
        self.assertTrue(c.start("all")[0])
        self.assertTrue(c.wait_idle(120))
        self.assertEqual(c.status()["state"], "finished")
        events = []
        while not q.empty():
            ev = q.get()
            if ev and ev.get("type") != "status":
                events.append(ev)
        self.assertEqual(events[-1]["type"], "game_over")
        return events

    def test_called_votes_flow_through_server(self):
        events = self._play({"player_vote_calls": True, "vote_call_threshold": 1, "on_no_decision": "continue",
                             "max_rounds": 4}, seed=3)
        answers = [e for e in events if e["type"] == "message" and e.get("kind") == "answer"]
        self.assertTrue(answers and all("wants_vote" in e for e in answers))
        checks = [e for e in events if e["type"] == "vote_call_check"]
        self.assertTrue(checks)
        for k in ("round", "turn", "requesters", "count", "threshold", "triggered"):
            self.assertIn(k, checks[0])
        vote_phases = [e for e in events if e["type"] == "phase" and e.get("phase") == "vote"]
        self.assertTrue(all(e.get("trigger") for e in vote_phases))
        if any(c["triggered"] for c in checks):
            self.assertTrue(any(str(e.get("trigger", "")).startswith("called") for e in vote_phases))
        self.assertIn("turns_completed", events[-1])

    def test_feature_off_emits_no_vote_call_events(self):
        events = self._play({"player_vote_calls": False}, seed=4)
        self.assertFalse([e for e in events if e["type"] == "vote_call_check"])


def _engine_has_prompt_api() -> bool:
    try:
        from imposter_game import prompts
        return callable(getattr(prompts, "list_templates", None))
    except Exception:
        return False


@unittest.skipUnless(_engine_has_prompt_api(), "engine has no editable prompts yet")
class PromptEndpointTests(ServerTestBase):
    REPO_PROMPTS = ROOT / "prompts"

    def setUp(self):
        self.repo_prompts_existed = self.REPO_PROMPTS.exists()

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.prompts_dir, ignore_errors=True)
        # The tests must never create the real prompts/ folder.
        self.assertEqual(self.REPO_PROMPTS.exists(), self.repo_prompts_existed)

    def list_prompts(self) -> dict:
        status, data = self.get_json("/api/prompts")
        self.assertEqual(status, 200, data)
        return data

    def pick(self, data: dict) -> dict:
        """A template with at least one placeholder (so we can test unknown/unused placeholders)."""
        for t in data["templates"]:
            if t["placeholders"]:
                return t
        self.skipTest("no template with placeholders")

    def test_list(self):
        data = self.list_prompts()
        self.assertTrue(data["ok"])
        self.assertGreater(len(data["templates"]), 5)
        for t in data["templates"]:
            for key in ("id", "title", "description", "group", "placeholders", "required_markers",
                        "text", "default_text", "modified"):
                self.assertIn(key, t)
            self.assertFalse(t["modified"])
        self.assertRegex(data["prompt_version"], r"^[0-9a-f]{8}$")
        self.assertFalse(data["modified"])
        self.assertIn("New game", data["note"])

    def test_validate_save_reset_round_trip(self):
        data = self.list_prompts()
        t = self.pick(data)
        version0 = data["prompt_version"]

        # Unknown placeholder -> error, and saving is refused.
        bad = t["text"] + "\n{definitely_not_a_placeholder}"
        status, res = self.post("/api/prompts/validate", body={"id": t["id"], "text": bad})
        self.assertEqual(status, 200, res)
        self.assertTrue(res["errors"])
        status, res = self.post("/api/prompts/save", body={"id": t["id"], "text": bad})
        self.assertFalse(res["ok"])
        self.assertTrue(res["errors"])
        self.assertFalse((self.prompts_dir / f"{t['id']}.txt").exists())

        # Malformed braces -> error.
        status, res = self.post("/api/prompts/validate", body={"id": t["id"], "text": t["text"] + " {oops"})
        self.assertTrue(res["errors"])

        # A valid edit saves to the override folder and changes the version.
        good = t["text"] + "\nBe brief."
        status, res = self.post("/api/prompts/validate", body={"id": t["id"], "text": good})
        self.assertEqual(res["errors"], [])
        status, res = self.post("/api/prompts/save", body={"id": t["id"], "text": good})
        self.assertEqual(status, 200, res)
        self.assertTrue(res["ok"], res)
        self.assertTrue((self.prompts_dir / f"{t['id']}.txt").exists())
        data = self.list_prompts()
        saved = {x["id"]: x for x in data["templates"]}[t["id"]]
        self.assertTrue(saved["modified"])
        self.assertEqual(saved["text"].strip(), good.strip())
        self.assertTrue(data["modified"])
        self.assertNotEqual(data["prompt_version"], version0)

        # A new game picks up the edit (prompt version recorded on game_started).
        self.assertEqual(self.post("/api/new_game")[0], 200)
        data = self.list_prompts()
        if data["current_game"]["prompt_version"] is not None:
            self.assertEqual(data["current_game"]["prompt_version"], data["prompt_version"])

        # Reset deletes the override and restores the default version.
        status, res = self.post("/api/prompts/reset", body={"id": t["id"]})
        self.assertEqual(status, 200, res)
        self.assertTrue(res["ok"])
        self.assertFalse((self.prompts_dir / f"{t['id']}.txt").exists())
        data = self.list_prompts()
        self.assertFalse({x["id"]: x for x in data["templates"]}[t["id"]]["modified"])
        self.assertEqual(data["prompt_version"], version0)

    def test_warning_for_missing_marker(self):
        data = self.list_prompts()
        with_marker = [t for t in data["templates"] if t["required_markers"]]
        if not with_marker:
            self.skipTest("no template with required markers")
        t = with_marker[0]
        text = t["text"]
        for m in t["required_markers"]:
            text = text.replace(m, "")
        status, res = self.post("/api/prompts/validate", body={"id": t["id"], "text": text})
        self.assertEqual(status, 200)
        self.assertTrue(res["warnings"], res)

    def test_bad_requests(self):
        status, res = self.post("/api/prompts/save", body={"id": "no_such_template", "text": "x"})
        self.assertEqual(status, 404)
        status, res = self.post("/api/prompts/validate", body={"text": "x"})
        self.assertEqual(status, 400)
        status, res = self.post("/api/prompts/save", body=b"not json")
        self.assertEqual(status, 400)
        status, res = self.post("/api/prompts/nope", body={})
        self.assertEqual(status, 404)

    def test_origin_protection(self):
        t = self.list_prompts()["templates"][0]
        for path, body in (("/api/prompts/save", {"id": t["id"], "text": t["text"] + " x"}),
                           ("/api/prompts/reset", {"id": t["id"]}),
                           ("/api/prompts/validate", {"id": t["id"], "text": "x"})):
            status, _ = self.post(path, headers={"Origin": "http://evil.example"}, body=body)
            self.assertEqual(status, 403, path)
        self.assertFalse((self.prompts_dir / f"{t['id']}.txt").exists())
        status, _, _ = self.get("/api/prompts", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)

    def test_preview(self):
        status, res = self.get_json("/api/prompts/preview")
        self.assertEqual(status, 200, res)
        pv = res["preview"]
        self.assertTrue(pv["trio_system"].strip())
        self.assertTrue(pv["imposter_system"].strip())
        for key in ("intro", "question", "answer", "vote"):
            self.assertIn(key, pv["tasks"])


def _engine_supports_no_imposter() -> bool:
    try:
        from imposter_game.config import RulesConfig
        return hasattr(RulesConfig(), "no_imposter_chance")
    except Exception:
        return False


@unittest.skipUnless(_engine_supports_no_imposter(), "engine has no no_imposter_chance yet")
class NoImposterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aau-test-noimp-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_imposter_game(self):
        cfg = self.tmp / "config.toml"
        cfg.write_text(_config_with_rules({"no_imposter_chance": 1.0}), encoding="utf-8")
        c = GameController(cfg, mock=True, log_dir=self.tmp, mock_options={"delay": 0}, seed=5,
                           prompts_dir=self.tmp / "prompts")
        ok, msg = c.new_game()
        self.assertTrue(ok, msg)
        q, backlog = c.subscribe()
        self.assertTrue(c.start("all")[0])
        self.assertTrue(c.wait_idle(120))
        events = list(backlog)
        while not q.empty():
            ev = q.get()
            if ev and ev.get("type") != "status":
                events.append(ev)
        started = next(e for e in events if e["type"] == "game_started")
        self.assertIs(started.get("has_imposter"), False)
        self.assertTrue(all(p["role"] == "trio" for p in started["players"]))
        over = events[-1]
        self.assertEqual(over["type"], "game_over")
        self.assertIn(over["outcome"], ("trio_win_no_imposter", "trio_loss_no_imposter"))
        for v in (e for e in events if e["type"] == "vote_cast" and e.get("abstain_reason") == "nobody"):
            self.assertIsNone(v["target"])
        self.assertTrue(c.stats()["ok"])


def _engine_has_settings_api() -> bool:
    try:
        from imposter_game import config
        return all(callable(getattr(config, n, None))
                   for n in ("settings_schema", "read_settings", "validate_settings", "save_settings"))
    except Exception:
        return False


FAKE_MODELS = {"data": [
    {"id": "openai/gpt-6-luna", "name": "OpenAI: GPT-6 Luna", "context_length": 1050000,
     "architecture": {"output_modalities": ["text"]},
     "pricing": {"prompt": "0.0000001", "completion": "0.0000005"},
     "reasoning": {"supported_efforts": ["max", "high", "low", "none"], "default_effort": "medium"}},
    {"id": "acme/painter", "name": "Acme: Painter", "architecture": {"output_modalities": ["image"]},
     "pricing": {"prompt": "0.000001", "completion": "0.000002"}},
    {"id": "anthropic/claude-haiku-5.5", "name": "Anthropic: Claude Haiku 5.5", "context_length": 1000000,
     "pricing": {"prompt": "0.0000001", "completion": "0.0000005"}},
    {"id": "openrouter/auto", "name": "Auto Router", "pricing": {"prompt": "-1", "completion": "-1"}},
]}


@unittest.skipUnless(_engine_has_settings_api(), "engine has no settings API yet")
class SettingsTests(unittest.TestCase):
    """Settings endpoints against a temp COPY of config.toml; the real file and the network are never touched."""

    @classmethod
    def setUpClass(cls):
        cls.real_config_bytes = CONFIG.read_bytes()
        cls.real_bak_existed = (ROOT / "config.toml.bak").exists()
        cls.tmp = Path(tempfile.mkdtemp(prefix="aau-test-settings-"))
        cls.cfg_path = cls.tmp / "config.toml"
        cls.fetch_calls = 0

        def fake_fetcher():
            cls.fetch_calls += 1
            return FAKE_MODELS

        shutil.copy(CONFIG, cls.cfg_path)
        cls.controller = GameController(cls.cfg_path, mock=True, log_dir=cls.tmp / "logs", mock_options={"delay": 0},
                                        seed=11, keepalive_seconds=1.0, prompts_dir=cls.tmp / "prompts",
                                        models_fetcher=fake_fetcher)
        cls.server = make_server(cls.controller, "127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.controller.wait_idle(30)
        cls.server.close_all()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def tearDown(self):
        self.controller.pause()
        self.controller.wait_idle(30)
        # Never touch the real config.toml (or leave a backup next to it).
        self.assertEqual(CONFIG.read_bytes(), self.real_config_bytes)
        self.assertEqual((ROOT / "config.toml.bak").exists(), self.real_bak_existed)

    get = ServerTestBase.get
    get_json = ServerTestBase.get_json
    post = ServerTestBase.post

    def settings(self) -> dict:
        status, data = self.get_json("/api/settings")
        self.assertEqual(status, 200, data)
        return data

    def test_get_settings(self):
        data = self.settings()
        self.assertTrue(data["ok"])
        ids = {f["id"] for f in data["schema"]}
        self.assertIn("rules.vote_threshold", ids)
        self.assertIn("trio.model", ids)
        self.assertEqual(set(data["values"]) >= {"rules.vote_threshold", "trio.model"}, True)
        self.assertEqual(Path(data["config_path"]), self.cfg_path.resolve())
        self.assertFalse(data["running"])
        self.assertIn("New game", data["note"])
        self.assertNotIn("api_key", json.dumps(data))

    def test_validate(self):
        values = self.settings()["values"]
        status, res = self.post("/api/settings/validate", body={"values": values})
        self.assertEqual(status, 200, res)
        self.assertEqual(res["errors"], {})
        bad = dict(values, **{"rules.vote_threshold": 9})
        status, res = self.post("/api/settings/validate", body={"values": bad})
        self.assertIn("rules.vote_threshold", res["errors"])
        status, res = self.post("/api/settings/validate", body={"values": "nope"})
        self.assertEqual(status, 400)

    def test_save_round_trip_and_invalid_save(self):
        values = self.settings()["values"]
        before = self.cfg_path.read_bytes()
        # Invalid: nothing written.
        status, res = self.post("/api/settings/save", body={"values": dict(values, **{"rules.vote_threshold": 0})})
        self.assertFalse(res["ok"])
        self.assertIn("rules.vote_threshold", res["errors"])
        self.assertEqual(self.cfg_path.read_bytes(), before)
        # Valid: only that value changes, comments survive, a backup is made.
        new_limit = values["rules.answer_word_limit"] + 7
        status, res = self.post("/api/settings/save", body={"values": dict(values, **{"rules.answer_word_limit": new_limit})})
        self.assertEqual(status, 200, res)
        self.assertTrue(res["ok"], res)
        self.assertIn("Saved", res["message"])
        text = self.cfg_path.read_text(encoding="utf-8")
        self.assertIn(f"answer_word_limit = {new_limit}", text)
        self.assertEqual(text.count("#"), before.decode("utf-8").count("#"))
        if res.get("backup"):
            self.assertTrue(str(res["backup"]).startswith(str(self.tmp)))
        self.assertEqual(self.settings()["values"]["rules.answer_word_limit"], new_limit)
        # Put it back.
        status, res = self.post("/api/settings/save", body={"values": values})
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.cfg_path.read_bytes(), before)

    def test_save_and_start_new_game(self):
        values = self.settings()["values"]
        new_q = values["rules.question_word_limit"] + 3
        status, res = self.post("/api/settings/save",
                                body={"values": dict(values, **{"rules.question_word_limit": new_q}), "start_new_game": True})
        self.assertEqual(status, 200, res)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["new_game"]["ok"], res)
        self.assertTrue(res["status"]["has_game"])
        self.assertEqual(self.controller.cfg.rules.question_word_limit, new_q)
        # While a game is running, save works but no new game is started.
        self.controller.mock_options = {"delay": 0.05}
        try:
            self.assertEqual(self.post("/api/run_all")[0], 200)
            status, res = self.post("/api/settings/save", body={"values": values, "start_new_game": True})
            self.assertTrue(res["ok"], res)
            self.assertFalse(res["new_game"]["ok"])
            self.assertIn("running", res["new_game"]["message"])
        finally:
            self.controller.pause()
            self.controller.wait_idle(30)
            self.controller.mock_options = {"delay": 0}

    def test_origin_protection(self):
        values = self.settings()["values"]
        before = self.cfg_path.read_bytes()
        for path in ("/api/settings/save", "/api/settings/validate"):
            status, _ = self.post(path, headers={"Origin": "http://evil.example"},
                                  body={"values": dict(values, **{"rules.answer_word_limit": 77})})
            self.assertEqual(status, 403, path)
        status, _, _ = self.get("/api/settings", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(self.cfg_path.read_bytes(), before)

    def test_models_with_fake_fetcher_and_cache(self):
        self.controller._models_cache = None
        type(self).fetch_calls = 0
        status, res = self.get_json("/api/models")
        self.assertEqual(status, 200)
        self.assertTrue(res["ok"])
        ids = [m["id"] for m in res["models"]]
        self.assertNotIn("acme/painter", ids)           # image-only models are left out
        luna = next(m for m in res["models"] if m["id"] == "openai/gpt-6-luna")
        self.assertAlmostEqual(luna["prompt_per_m"], 0.1)
        self.assertAlmostEqual(luna["completion_per_m"], 0.5)
        self.assertEqual(luna["supported_efforts"], ["max", "high", "low", "none"])
        self.assertEqual(luna["default_effort"], "medium")
        self.assertEqual(luna["context_length"], 1050000)
        auto = next(m for m in res["models"] if m["id"] == "openrouter/auto")
        self.assertIsNone(auto["prompt_per_m"])          # negative "variable" prices -> unknown
        haiku = next(m for m in res["models"] if m["id"] == "anthropic/claude-haiku-5.5")
        self.assertIsNone(haiku["supported_efforts"])
        self.get_json("/api/models")
        self.assertEqual(self.fetch_calls, 1)            # cached

    def test_models_offline(self):
        def boom():
            raise OSError("network is unreachable")
        c = GameController(self.cfg_path, mock=True, log_dir=self.tmp / "logs", models_fetcher=boom)
        res = c.models()
        self.assertFalse(res["ok"])
        self.assertEqual(res["models"], [])
        self.assertIn("unreachable", res["error"])


def _engine_has_batch() -> bool:
    try:
        from imposter_game import batch
        return hasattr(batch, "BatchRunner")
    except Exception:
        return False


@unittest.skipUnless(_engine_has_batch(), "engine has no batch mode yet")
class BatchTests(ServerTestBase):
    def tearDown(self):
        c = self.controller
        if c.batch_active:
            c.batch.abort()
        self.assertTrue(c.wait_idle(60))
        c.mock_options = {"delay": 0}
        super().tearDown()

    def wait_batch_done(self, reader: SSEReader, timeout: float = 120) -> dict:
        batch_id = self.controller.batch.batch_id  # ignore a previous batch replayed on connect
        ev = reader.wait_for(lambda e: e.get("type") == "batch_finished" and e.get("batch_id") == batch_id, timeout)
        self.assertIsNotNone(ev, f"no batch_finished; last events: {reader.types()[-10:]}")
        self.assertTrue(self.controller.wait_idle(30))
        return ev

    def test_info_and_plan(self):
        status, info = self.get_json("/api/batch")
        self.assertEqual(status, 200, info)
        self.assertFalse(info["active"])
        self.assertIn("games", info["defaults"])
        self.assertTrue(info["plan"])
        status, res = self.post("/api/batch/plan", body={"options": {"games": 5, "budget_usd": None}})
        self.assertTrue(res["ok"], res)
        self.assertIn("5 games", res["plan"])
        # Never unbounded: no game limit and no budget is refused.
        status, res = self.post("/api/batch/plan", body={"options": {"games": 0, "budget_usd": None}})
        self.assertFalse(res["ok"])
        self.assertTrue(res["message"])
        status, res = self.post("/api/batch/plan", body={"options": {"games": "lots"}})
        self.assertFalse(res["ok"])

    def test_batch_runs_to_games_limit_and_replays(self):
        reader = self.sse()
        status, res = self.post("/api/batch/start", body={"options": {"games": 3, "budget_usd": None,
                                                                      "alternate_roles": True}})
        self.assertEqual(status, 200, res)
        self.assertTrue(res["status"]["batch"]["active"])
        done = self.wait_batch_done(reader)
        self.assertEqual(done["stop_reason"], "games_limit")
        mine = [e for e in reader.events if e.get("batch_id") == done["batch_id"]]
        types = [e.get("type") for e in mine]
        for t in ("batch_started", "batch_progress", "batch_game_started", "game_started", "game_over"):
            self.assertIn(t, types)
        started = [e for e in mine if e.get("type") == "batch_game_started"]
        self.assertEqual([e["batch_index"] for e in started], [1, 2, 3])
        self.assertEqual([e["roles_swapped"] for e in started], [False, True, False])
        self.assertEqual(len([e for e in mine if e.get("type") == "game_over"]), 3)
        st = self.wait_state({"finished", "idle"})
        self.assertFalse(st["batch"]["active"])
        # The batch is over: normal play works again.
        self.assertEqual(self.post("/api/new_game")[0], 200)
        # A late client gets the batch events + only the latest game's events.
        late = self.sse()
        self.assertIsNotNone(late.wait_for(lambda e: e.get("type") == "status", 10))
        late_types = late.types()
        self.assertIn("batch_finished", late_types)
        game_ids = {e.get("game_id") for e in late.events
                    if e.get("type") not in ("status",) and not str(e.get("type")).startswith("batch_")}
        self.assertLessEqual(len(game_ids), 1)
        # Stats saw the batch's games.
        self.assertGreaterEqual(self.get_json("/api/stats")[1]["num_games"], 3)

    def test_refused_while_running_and_graceful_stop(self):
        self.controller.mock_options = {"delay": 0.03}
        reader = self.sse()
        status, res = self.post("/api/batch/start", body={"options": {"games": 6, "budget_usd": None}})
        self.assertEqual(status, 200, res)
        for path in ("/api/new_game", "/api/run_all", "/api/next_round", "/api/pause", "/api/batch/start"):
            status, _ = self.post(path, body={"options": {"games": 1}})
            self.assertEqual(status, 409, path)
        bid = self.controller.batch.batch_id
        self.assertIsNotNone(reader.wait_for(lambda e: e.get("type") == "batch_game_started" and e.get("batch_id") == bid, 30))
        status, res = self.post("/api/batch/stop")
        self.assertEqual(status, 200, res)
        done = self.wait_batch_done(reader)
        self.assertEqual(done["stop_reason"], "stopped")
        self.assertLess(done["totals"]["games_started"], 6)
        overs = [e for e in reader.events if e.get("type") == "game_over" and e.get("batch_id") == done["batch_id"]]
        self.assertTrue(overs and all(e["outcome"] != "aborted" for e in overs))  # the running game finished
        self.assertEqual(self.post("/api/batch/stop")[0], 409)

    def test_abort(self):
        self.controller.mock_options = {"delay": 0.03}
        reader = self.sse()
        self.assertEqual(self.post("/api/batch/start", body={"options": {"games": 5, "budget_usd": None}})[0], 200)
        bid = self.controller.batch.batch_id
        self.assertIsNotNone(reader.wait_for(lambda e: e.get("type") == "message" and e.get("batch_id") == bid, 30))
        status, res = self.post("/api/batch/abort")
        self.assertEqual(status, 200, res)
        done = self.wait_batch_done(reader)
        self.assertEqual(done["stop_reason"], "aborted")
        self.assertIsNotNone(reader.wait_for(lambda e: e.get("type") == "game_over" and e.get("outcome") == "aborted"
                                             and e.get("batch_id") == done["batch_id"], 5))

    def test_refused_while_a_game_runs(self):
        self.controller.mock_options = {"delay": 0.05}
        self.assertEqual(self.post("/api/new_game")[0], 200)
        self.assertEqual(self.post("/api/run_all")[0], 200)
        status, res = self.post("/api/batch/start", body={"options": {"games": 2}})
        self.assertEqual(status, 409)
        self.assertIn("running", res["message"])

    def test_origin_protection(self):
        for path in ("/api/batch/start", "/api/batch/stop", "/api/batch/abort", "/api/batch/plan"):
            status, _ = self.post(path, headers={"Origin": "http://evil.example"}, body={"options": {"games": 1}})
            self.assertEqual(status, 403, path)
        self.assertFalse(self.controller.batch_active)
        status, _, _ = self.get("/api/batch", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)

    def test_credit(self):
        status, res = self.get_json("/api/credit")
        self.assertEqual(status, 200)
        self.assertIsNone(res["credit_usd"])  # mock mode
        real = GameController(CONFIG, mock=False, log_dir=self.tmp)
        real.credit_checker = lambda: 12.5     # injected: never the network
        self.assertEqual(real.credit()["credit_usd"], 12.5)
        real.credit_checker = lambda: None
        self.assertIsNone(real.credit(force=True)["credit_usd"])


if __name__ == "__main__":
    unittest.main()
