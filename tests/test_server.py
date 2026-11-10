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
        cls.controller = GameController(CONFIG, mock=True, log_dir=cls.tmp, mock_options={"delay": 0},
                                        seed=7, keepalive_seconds=1.0)
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

    def post(self, path: str, headers: dict | None = None):
        req = urllib.request.Request(self.base + path, data=b"{}", method="POST",
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


if __name__ == "__main__":
    unittest.main()
