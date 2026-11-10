"""Tests for batch mode (imposter_game/batch.py and batch.py). Mock games, zero delay, temp log dirs."""

from __future__ import annotations

import dataclasses
import io
import json
import re
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from imposter_game.batch import BatchOptions, BatchRunner, plan_text  # noqa: E402
from imposter_game.config import ConfigError, GameConfig, ModelConfig, RulesConfig  # noqa: E402
from imposter_game.openrouter import CreditChecker, MockClient, OpenRouterError  # noqa: E402

TRIO, IMP = "anthropic/claude-haiku-5.5", "openai/gpt-6-luna"


def make_cfg() -> GameConfig:
    return GameConfig(trio=ModelConfig(TRIO, "Claude Haiku 5.5", "high"),
                      imposter=ModelConfig(IMP, "GPT-6 Luna", "max", max_output_tokens=32000),
                      rules=RulesConfig(player_vote_calls=False), api_key="sk-test")


class CostClient(MockClient):
    """Mock players where every call costs exactly `per_call` dollars."""

    def __init__(self, per_call=0.001):
        super().__init__(delay=0)
        self.per_call = per_call

    def chat(self, *a, **kw):
        return dataclasses.replace(super().chat(*a, **kw), cost_usd=self.per_call)


class Broken(MockClient):
    def __init__(self):
        super().__init__(delay=0)

    def chat(self, *a, **kw):
        raise RuntimeError("provider down")


class BatchTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.events: list[dict] = []
        self.lock = threading.Lock()

    def tearDown(self):
        self._tmp.cleanup()

    def on_event(self, ev):
        with self.lock:
            self.events.append(ev)

    def runner(self, factory=lambda: MockClient(delay=0), mock=True, on_event=None, **opts):
        return BatchRunner(make_cfg(), factory, on_event or self.on_event, log_dir=self.tmp / "logs", mock=mock,
                           options=BatchOptions(**opts), prompts_dir=self.tmp / "prompts", seed=1)

    def of(self, type_):
        return [e for e in self.events if e["type"] == type_]

    def summary_rows(self, mock=True):
        p = self.tmp / "logs" / ("mock" if mock else "") / "summary.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


class TestLimits(BatchTestBase):
    def test_games_limit_and_records(self):
        r = self.runner(games=3, budget_usd=None)
        record = r.run()
        self.assertEqual(record["stop_reason"], "games_limit")
        self.assertEqual(len(record["games"]), 3)
        self.assertEqual(self.events[0]["type"], "batch_started")
        self.assertEqual(self.events[-1]["type"], "batch_finished")
        self.assertEqual(self.events[-1]["stop_reason"], "games_limit")
        self.assertTrue(all(e["batch_id"] == r.batch_id for e in self.events))
        # batch_game_started comes before that game's own events
        for bs in self.of("batch_game_started"):
            gid = bs["game_id"]
            first = next(i for i, e in enumerate(self.events) if e.get("game_id") == gid)
            self.assertIs(self.events[first], bs)
            started = next(e for e in self.events if e["type"] == "game_started" and e["game_id"] == gid)
            self.assertEqual(started["batch_index"], bs["batch_index"])
        prog = self.of("batch_progress")[-1]
        self.assertEqual((prog["games_finished"], prog["max_games"], prog["finished"]), (3, 3, True))
        self.assertEqual(sum(prog["outcomes"].values()), 3)
        # record file + summary rows
        path = self.tmp / "logs" / "mock" / "batches" / f"{r.batch_id}.json"
        saved = json.loads(path.read_text())
        self.assertEqual(saved["stop_reason"], "games_limit")
        self.assertEqual(saved["prompt_version"], r.prompt_version)
        self.assertEqual([g["batch_index"] for g in saved["games"]], [1, 2, 3])
        rows = self.summary_rows()
        self.assertEqual(sorted(x["batch_index"] for x in rows), [1, 2, 3])
        self.assertTrue(all(x["batch_id"] == r.batch_id and x["roles_swapped"] is False for x in rows))
        game_json = json.loads(Path(rows[0]["log_file"]).read_text())
        self.assertEqual(game_json["batch_id"], r.batch_id)
        self.assertEqual(len({g["seed"] for g in saved["games"]}), 3)
        self.assertFalse((ROOT / "logs" / "batches").exists() and any(
            p.name.startswith(r.batch_id) for p in (ROOT / "logs" / "batches").iterdir()))

    def test_budget_guard_never_overshoots(self):
        r = self.runner(factory=lambda: CostClient(0.001), games=0, budget_usd=0.2, first_game_estimate_usd=0.06)
        record = r.run()
        self.assertEqual(record["stop_reason"], "budget")
        spent = 0.0
        max_cost = None
        for e in self.events:
            if e["type"] == "batch_game_started":
                est = max_cost if max_cost is not None else 0.06
                self.assertLessEqual(spent + est, 0.2 + 1e-9)
            elif e["type"] == "batch_game_finished":
                spent += e["game"]["cost_usd"]
                max_cost = max(max_cost or 0, e["game"]["cost_usd"])
        self.assertLessEqual(record["totals"]["spent_usd"], 0.2)
        self.assertGreater(spent + max_cost, 0.2)        # the next game really would not have fit
        self.assertGreaterEqual(len(record["games"]), 2)

    def test_parallel_with_budget_reservation(self):
        r = self.runner(factory=lambda: CostClient(0.001), games=0, budget_usd=0.35, parallel=3,
                        first_game_estimate_usd=0.06)
        record = r.run()
        self.assertEqual(record["stop_reason"], "budget")
        running, spent, max_cost, peak = 0, 0.0, None, 0
        for e in self.events:
            if e["type"] == "batch_game_started":
                est = max_cost if max_cost is not None else 0.06
                self.assertLessEqual(spent + est * (running + 1), 0.35 + 1e-9)
                running += 1
                peak = max(peak, running)
            elif e["type"] == "batch_game_finished":
                running -= 1
                spent += e["game"]["cost_usd"]
                max_cost = max(max_cost or 0, e["game"]["cost_usd"])
        self.assertGreater(peak, 1)
        self.assertLessEqual(record["totals"]["spent_usd"], 0.35)

    def test_parallel_games_limit(self):
        r = self.runner(games=7, budget_usd=None, parallel=3)
        record = r.run()
        self.assertEqual(record["stop_reason"], "games_limit")
        self.assertEqual(sorted(g["batch_index"] for g in record["games"]), list(range(1, 8)))
        self.assertEqual(len({g["game_id"] for g in record["games"]}), 7)
        self.assertEqual(len(self.summary_rows()), 7)

    def test_consecutive_errors(self):
        r = self.runner(factory=Broken, games=10, budget_usd=None, stop_after_errors=3)
        record = r.run()
        self.assertEqual(record["stop_reason"], "errors")
        self.assertEqual([g["outcome"] for g in record["games"]], ["error"] * 3)

    def test_min_credit_stop(self):
        calls = {"n": 0}

        def checker():
            calls["n"] += 1
            return 10.0 - 3 * calls["n"]          # 7, 4, 1, ...

        r = BatchRunner(make_cfg(), lambda: MockClient(delay=0), self.on_event, log_dir=self.tmp / "logs",
                        mock=False, options=BatchOptions(games=10, budget_usd=None, min_credit_usd=3.0),
                        credit_checker=checker, prompts_dir=self.tmp / "prompts", seed=1)
        record = r.run()
        self.assertEqual(record["stop_reason"], "credit")
        self.assertEqual(len(record["games"]), 1)
        self.assertEqual(self.of("batch_progress")[-1]["credit_remaining_usd"], 1.0)
        self.assertTrue(self.summary_rows(mock=False))

    def test_unknown_credit_warns_once_and_continues(self):
        r = BatchRunner(make_cfg(), lambda: MockClient(delay=0), self.on_event, log_dir=self.tmp / "logs",
                        mock=False, options=BatchOptions(games=3, budget_usd=None, min_credit_usd=3.0),
                        credit_checker=lambda: None, prompts_dir=self.tmp / "prompts", seed=1)
        self.assertEqual(r.run()["stop_reason"], "games_limit")
        self.assertEqual(len(self.of("batch_warning")), 1)

    def test_credit_never_checked_in_mock(self):
        def checker():
            raise AssertionError("must not be called in mock mode")
        r = BatchRunner(make_cfg(), lambda: MockClient(delay=0), self.on_event, log_dir=self.tmp / "logs",
                        mock=True, options=BatchOptions(games=2, budget_usd=None, min_credit_usd=3.0),
                        credit_checker=checker, prompts_dir=self.tmp / "prompts")
        self.assertEqual(r.run()["stop_reason"], "games_limit")

    def test_options_must_be_bounded(self):
        with self.assertRaises(ConfigError):
            BatchOptions(games=0, budget_usd=None).validate()
        with self.assertRaises(ConfigError):
            BatchOptions(games=5, budget_usd=0).validate()
        with self.assertRaises(ConfigError):
            BatchOptions(parallel=0).validate()
        BatchOptions(games=0, budget_usd=2.0).validate()
        o = BatchOptions.from_config(make_cfg().batch, games=5, budget_usd=None, parallel=None)
        self.assertEqual((o.games, o.budget_usd, o.parallel), (5, 1.0, 1))
        text = plan_text(BatchOptions(games=20, budget_usd=1.0, alternate_roles=True), make_cfg())
        self.assertIn("Up to 20 games, stopping before $1.00 is spent", text)
        self.assertIn("roughly 10 games", text)
        self.assertIn("swaps", text)


class TestRolesAndControl(BatchTestBase):
    def test_alternate_roles(self):
        r = self.runner(games=4, budget_usd=None, alternate_roles=True)
        record = r.run()
        games = sorted(record["games"], key=lambda g: g["batch_index"])
        self.assertEqual([g["roles_swapped"] for g in games], [False, True, False, True])
        self.assertEqual([g["trio_model"] for g in games], [TRIO, IMP, TRIO, IMP])
        rows = {x["batch_index"]: x for x in self.summary_rows()}
        self.assertEqual(rows[2]["trio_model"], IMP)
        self.assertEqual(rows[2]["imposter_model"], TRIO)
        self.assertTrue(rows[2]["roles_swapped"])
        started = [e for e in self.events if e["type"] == "game_started"]
        imp2 = next(p for p in started[1]["players"] if p["role"] == "imposter")
        self.assertEqual(imp2["model"], TRIO)
        self.assertEqual([e["roles_swapped"] for e in self.of("batch_game_started")], [False, True, False, True])

    def test_graceful_stop(self):
        holder = {}

        def on_event(ev):
            self.on_event(ev)
            if ev["type"] == "batch_game_finished":
                holder["r"].request_stop()

        r = self.runner(games=10, budget_usd=None, on_event=on_event)
        holder["r"] = r
        record = r.run()
        self.assertEqual(record["stop_reason"], "stopped")
        self.assertEqual(len(record["games"]), 1)
        self.assertNotEqual(record["games"][0]["outcome"], "aborted")

    def test_abort(self):
        holder = {}

        def on_event(ev):
            self.on_event(ev)
            if ev["type"] == "message" and ev.get("kind") == "question" and not holder.get("done"):
                holder["done"] = True
                threading.Thread(target=holder["r"].abort).start()

        r = self.runner(games=10, budget_usd=None, on_event=on_event)
        holder["r"] = r
        record = r.run()
        self.assertEqual(record["stop_reason"], "aborted")
        self.assertEqual(len(record["games"]), 1)
        self.assertEqual(record["games"][0]["outcome"], "aborted")
        self.assertEqual(self.of("batch_finished")[0]["stop_reason"], "aborted")


class TestCliAndAnalyze(BatchTestBase):
    def test_cli_mock_and_analyze_filter(self):
        import analyze
        import batch as batch_cli
        out = io.StringIO()
        with redirect_stdout(out):
            code = batch_cli.main(["--mock", "--yes", "--games", "3", "--log-dir", str(self.tmp / "logs"),
                                   "--prompts-dir", str(self.tmp / "prompts")])
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("Up to 3 games", text)
        for i in (1, 2, 3):
            self.assertIn(f"[{i}/3] ", text)
        self.assertIn("finished: reached the game limit", text)
        bid = self.summary_rows()[0]["batch_id"]
        # a second batch, so the filter has something to filter out
        with redirect_stdout(io.StringIO()):
            batch_cli.main(["--mock", "--yes", "--games", "2", "--log-dir", str(self.tmp / "logs"),
                            "--prompts-dir", str(self.tmp / "prompts")])
        out = io.StringIO()
        with redirect_stdout(out):
            analyze.main(["--log-dir", str(self.tmp / "logs"), "--mock-only", "--batch", bid])
        self.assertIn("Games: 3", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            analyze.main(["--log-dir", str(self.tmp / "logs"), "--mock-only", "--list-batches"])
        self.assertIn(bid, out.getvalue())
        self.assertEqual(len(re.findall(r"^  b\d", out.getvalue(), re.M)), 2)

    def test_cli_refuses_unbounded_and_missing_key(self):
        import batch as batch_cli
        err = io.StringIO()
        from contextlib import redirect_stderr
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            self.assertEqual(batch_cli.main(["--mock", "--yes", "--games", "0", "--log-dir", str(self.tmp),
                                             "--budget", "-1"]), 2)
        self.assertIn("budget_usd", err.getvalue())


class TestCreditChecker(unittest.TestCase):
    def fake(self, responses):
        cc = CreditChecker("sk-test")

        def request(method, path, body, timeout=None):
            r = responses[path]
            if isinstance(r, Exception):
                raise r
            return r
        cc._client._request = request
        return cc

    def test_key_limit(self):
        cc = self.fake({"/key": {"data": {"limit_remaining": 4.5, "usage": 1}},
                        "/credits": OpenRouterError("needs management key", status=403)})
        self.assertEqual(cc(), 4.5)
        self.assertEqual(cc.last_info["source"], "key_limit_remaining")

    def test_account_balance_and_min(self):
        cc = self.fake({"/key": {"data": {"limit_remaining": None}},
                        "/credits": {"data": {"total_credits": 10, "total_usage": 7.5}}})
        self.assertEqual(cc(), 2.5)
        cc = self.fake({"/key": {"data": {"limit_remaining": 1.0}},
                        "/credits": {"data": {"total_credits": 10, "total_usage": 2}}})
        self.assertEqual(cc(), 1.0)

    def test_unknown(self):
        cc = self.fake({"/key": {"data": {"limit_remaining": None}},
                        "/credits": OpenRouterError("forbidden", status=403)})
        self.assertIsNone(cc())
        self.assertIn("error", cc.last_info)
        self.assertIsNone(CreditChecker(None)())


if __name__ == "__main__":
    unittest.main()
