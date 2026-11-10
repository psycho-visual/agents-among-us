"""Round 5: private role reminders above every task, and role-first system prompts."""

from __future__ import annotations

import dataclasses
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from imposter_game import prompts  # noqa: E402
from imposter_game.config import GameConfig, ModelConfig, RulesConfig  # noqa: E402
from imposter_game.game import Game  # noqa: E402
from imposter_game.openrouter import MockClient  # noqa: E402

TRIO_R = "(Private reminder: you are a trio member: you are Claude Haiku 5.5. Two of "
MAYBE_R = "(Private reminder: you are a trio member: you are Claude Haiku 5.5. Either two of "
IMP_R = "(Private reminder: you are the imposter, a different AI model from the other three. "
NEW_IMP_SENTENCE = ("You already know everyone's role: the other three players are the trio. You don't need to work "
                    "out who is who. The only open question in this game is the one the trio is trying to answer: "
                    "which player is not their model?")
# prompt_version of the default templates just before round 5
OLD_DEFAULT_VERSION = "216b921d"


def make_cfg(**rules) -> GameConfig:
    return GameConfig(trio=ModelConfig("anthropic/claude-haiku-5.5", "Claude Haiku 5.5", "high"),
                      imposter=ModelConfig("openai/gpt-6-luna", "GPT-6 Luna", "max"),
                      rules=RulesConfig(**rules))


class Flaky(MockClient):
    """First vote and first answer of each player are invalid/empty, so both retry paths run."""

    def __init__(self):
        super().__init__(seed=4, delay=0)
        self.lock = threading.Lock()
        self.seen: set = set()

    def chat(self, model, messages, effort, max_tokens, session_id=None, purpose=None, context=None):
        res = super().chat(model, messages, effort, max_tokens, session_id, purpose, context)
        key = (context or {}).get("player"), purpose
        with self.lock:
            first = key not in self.seen
            self.seen.add(key)
        if first and purpose == "vote":
            return dataclasses.replace(res, content="Hmm.\nVOTE: Purple")
        if first and purpose == "answer":
            return dataclasses.replace(res, content="")
        return res


class TestReminders(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def play(self, cfg, client=None, seed=3):
        g = Game(cfg, client or MockClient(seed=seed, delay=0), log_dir=self.tmp / "logs", mock=True, seed=seed,
                 prompts_dir=self.tmp / "prompts")
        while not g.is_finished():
            g.step()
        return g

    def check_every_message(self, g, trio_prefix):
        purposes = set()
        for c in g.calls:
            if c["purpose"] == "paraphrase":
                continue
            msgs = [c["user_message"]] + [m["content"] for m in (c["extra_messages"] or []) if m["role"] == "user"]
            player = g.by_name[c["player"]]
            expected = IMP_R if player.role == "imposter" else trio_prefix
            for m in msgs:
                self.assertEqual(m.count("(Private reminder:"), 1, (c["purpose"], m[-400:]))
                self.assertIn(expected, m)
                if player.role == "imposter":
                    line = m[m.index("(Private reminder:"):].split("\n", 1)[0]
                    self.assertNotIn("GPT-6", line)
            # The reminder sits directly above the task block, after the transcript.
            last = msgs[-1]
            r = last.index("(Private reminder:")
            self.assertGreater(r, last.find(prompts.TRANSCRIPT_END))
            if "=== YOUR TASK" in last:
                self.assertTrue(last[r:].split("\n\n", 1)[1].startswith("=== YOUR TASK"))
            purposes.add(c["purpose"])
        return purposes

    def test_every_task_message_has_one_reminder(self):
        g = self.play(make_cfg(player_vote_calls=True), Flaky())
        purposes = self.check_every_message(g, TRIO_R)
        self.assertTrue({"intro", "question", "answer", "answer_retry_empty", "vote", "vote_retry"} <= purposes,
                        purposes)
        self.assertTrue(any(c["purpose"] == "intro" and g.by_name[c["player"]].role == "imposter" for c in g.calls))

    def test_maybe_no_imposter_variant(self):
        for seed in range(6):
            g = self.play(make_cfg(no_imposter_chance=0.5, reveal_votes=False), seed=seed)
            self.check_every_message(g, MAYBE_R)
            for c in g.calls:
                self.assertNotIn(TRIO_R, c["user_message"])

    def test_imposter_never_named_in_reminder(self):
        for knows in (True, False):
            g = self.play(make_cfg(imposter_knows_own_model=knows))
            imp = g.imposter
            self.assertNotIn("GPT-6", imp.reminder)
            self.assertIn(("You are GPT-6 Luna." in imp.system_prompt), (knows,))

    def test_system_prompt_role_first(self):
        g = self.play(make_cfg())
        for p in g.players:
            self.assertTrue(p.system_prompt.startswith("YOUR SECRET ROLE\n"))
            role_end = p.system_prompt.index('You are playing "Agents Among Us"')
            self.assertLess(role_end, p.system_prompt.index("OUTPUT RULES"))
        self.assertIn(NEW_IMP_SENTENCE, g.imposter.system_prompt)
        for p in g.players:
            if p.role == "trio":
                self.assertNotIn(NEW_IMP_SENTENCE, p.system_prompt)
        text = prompts.default_text("role_imposter").split("\n")
        i = next(n for n, line in enumerate(text) if "are all copies of one other AI model" in line)
        self.assertEqual(text[i + 1], NEW_IMP_SENTENCE)

    def test_templates_registered_and_version_changed(self):
        for tid in ("reminder_trio", "reminder_trio_maybe_no_imposter", "reminder_imposter"):
            self.assertIn(tid, prompts.TEMPLATES)
            self.assertEqual(prompts.validate_template(tid, prompts.default_text(tid)), {"errors": [], "warnings": []})
        self.assertTrue(prompts.validate_template("reminder_imposter", "({own_model}) {others}")["errors"])
        v = prompts.prompt_version(prompts_dir=self.tmp / "prompts")
        self.assertNotEqual(v, OLD_DEFAULT_VERSION)

    def test_edited_reminder_is_used(self):
        prompts.save_template("reminder_imposter", "(You are the imposter. Others: {others}.)",
                              prompts_dir=self.tmp / "prompts")
        g = self.play(make_cfg())
        call = next(c for c in g.calls if c["player"] == g.imposter.name and c["purpose"] == "answer")
        self.assertIn("(You are the imposter. Others: ", call["user_message"])


if __name__ == "__main__":
    unittest.main()
