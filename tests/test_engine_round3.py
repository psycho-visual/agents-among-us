"""Tests for round 3: editable prompt templates, typography normalisation, no-imposter games,
imposter_knows_own_model, and paraphrasing.

Run from the repo root:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import dataclasses
import io
import json
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from imposter_game import prompts  # noqa: E402
from imposter_game.config import (GameConfig, ModelConfig, ParaphraseConfig, RulesConfig,  # noqa: E402
                                  load_config)
from imposter_game.game import Game, decide_after_vote, normalize_typography, parse_vote  # noqa: E402
from imposter_game.openrouter import MockClient  # noqa: E402

NAMES = ["Red", "Blue", "Green", "Yellow"]


def make_cfg(paraphrase: bool = False, **rules) -> GameConfig:
    rules.setdefault("player_vote_calls", False)
    return GameConfig(
        trio=ModelConfig("anthropic/claude-haiku-5.5", "Claude Haiku 5.5", "high"),
        imposter=ModelConfig("openai/gpt-6-luna", "GPT-6 Luna", "max"),
        rules=RulesConfig(**rules),
        paraphrase=ParaphraseConfig(enabled=paraphrase, model="mistralai/mistral-small-3.2-24b-instruct"),
    )


def play(game: Game, max_steps: int = 500):
    for _ in range(max_steps):
        if game.is_finished():
            break
        game.step()


class Scripted(MockClient):
    """MockClient with scripted votes / intros, and optional failure on one purpose."""

    def __init__(self, vote_fn=None, intro_text=None, fail_purpose=None, seed=0):
        super().__init__(seed=seed, delay=0)
        self.vote_fn = vote_fn
        self.intro_text = intro_text
        self.fail_purpose = fail_purpose
        self.lock = threading.Lock()
        self.purposes: list[str] = []

    def chat(self, model, messages, reasoning_effort, max_tokens, session_id=None, purpose=None, context=None):
        with self.lock:
            self.purposes.append(purpose)
        if purpose == self.fail_purpose:
            raise RuntimeError(f"simulated {purpose} outage")
        res = super().chat(model, messages, reasoning_effort, max_tokens, session_id, purpose, context)
        ctx = context or {}
        if purpose in ("vote", "vote_retry") and self.vote_fn:
            return dataclasses.replace(res, content=f"Thinking.\nVOTE: {self.vote_fn(ctx['player'])}")
        if purpose == "intro" and self.intro_text:
            return dataclasses.replace(res, content=self.intro_text)
        return res


class TempDirs(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.log_dir = self.tmp / "logs"
        self.prompts_dir = self.tmp / "prompts"

    def tearDown(self):
        self._tmp.cleanup()

    def game(self, client, cfg=None, seed=1, **kw):
        events = []
        g = Game(cfg or make_cfg(), client, on_event=events.append, log_dir=self.log_dir, mock=True, seed=seed,
                 prompts_dir=self.prompts_dir, **kw)
        return g, events

    def summary(self):
        p = self.log_dir / "mock" / "summary.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


# =============================================================================
# Typography
# =============================================================================

class TestTypography(unittest.TestCase):
    def test_cases(self):
        n = normalize_typography
        self.assertEqual(n("I’m “sure”"), 'I\'m "sure"')
        self.assertEqual(n("‘quoted’ ‚low’ 5′ 6″"), "'quoted' 'low' 5' 6\"")
        self.assertEqual(n("„low”"), '"low"')
        self.assertEqual(n("wait—really"), "wait - really")
        self.assertEqual(n("wait — really"), "wait - really")
        self.assertEqual(n("wait – really ― yes"), "wait - really - yes")
        self.assertEqual(n("pick 1–10"), "pick 1-10")
        self.assertEqual(n("pick 1 – 10"), "pick 1-10")
        self.assertEqual(n("hmm…"), "hmm...")
        self.assertEqual(n("a b c d"), "a b c d")
        self.assertEqual(n("zero​width﻿"), "zerowidth")
        self.assertEqual(n("**bold** and __strong__"), "bold and strong")
        self.assertEqual(n("an *italic* and _under_ word"), "an italic and under word")
        self.assertEqual(n("snake_case_name and 2*3*4"), "snake_case_name and 2*3*4")
        self.assertEqual(n("# Heading\n## Sub"), "Heading\nSub")
        self.assertEqual(n("- one\n* two\n• three"), "one\ntwo\nthree")
        self.assertEqual(n("line one\nline two"), "line one\nline two")
        self.assertEqual(n("well-known  spaces"), "well-known spaces")

    def test_luna_style_equals_straight(self):
        curly = "I’d say “seven” — it’s the classic pick… between 1–10, **really**."
        straight = "I'd say \"seven\" - it's the classic pick... between 1-10, really."
        self.assertEqual(normalize_typography(curly), normalize_typography(straight))
        self.assertEqual(normalize_typography(curly), straight)


class TestTypographyInGame(TempDirs):
    def test_normalized_before_anyone_sees_it(self):
        curly = "Hi, I’m here — “ready”."
        g, events = self.game(Scripted(vote_fn=lambda me: "Purple", intro_text=curly))
        play(g)
        intros = [e for e in events if e["type"] == "message" and e["kind"] == "intro"]
        for e in intros:
            self.assertEqual(e["text"], 'Hi, I\'m here - "ready".')
            self.assertEqual(e["original_text"], curly)
            self.assertIn("normalized", e["transforms"])
        imp_call = next(c for c in g.calls if c["purpose"] == "intro" and c["player"] == g.imposter.name)
        self.assertNotIn("’", imp_call["user_message"])          # imposter sees normalised trio intros
        self.assertIn('Hi, I\'m here - "ready".', imp_call["user_message"])

    def test_off(self):
        curly = "Hi, I’m here."
        g, events = self.game(Scripted(vote_fn=lambda me: "Purple", intro_text=curly),
                              cfg=make_cfg(normalize_typography=False))
        play(g)
        intro = next(e for e in events if e["type"] == "message" and e["kind"] == "intro")
        self.assertEqual(intro["text"], curly)
        self.assertEqual(intro["transforms"], [])


# =============================================================================
# Default prompt wording
# =============================================================================

class TestDefaultPromptText(unittest.TestCase):
    def setUp(self):
        self.ps = prompts.PromptSet(prompts_dir=tempfile.gettempdir() + "/no-such-prompts-dir")

    def test_key_sentences(self):
        rules = RulesConfig(player_vote_calls=False)
        trio = self.ps.system_prompt("Red", "trio", "Claude Haiku 5.5", rules, NAMES)
        imp = self.ps.system_prompt("Blue", "imposter", "GPT-6 Luna", rules, NAMES)
        for sp in (trio, imp):
            self.assertTrue(sp.startswith("YOUR SECRET ROLE\n"))
            self.assertIn('\n\nYou are playing "Agents Among Us", a game for four players.\n', sp)
            self.assertNotIn("social-deduction", sp)
            self.assertIn("This is not a game about hidden actions, sabotage, or lying about what you did. "
                          "The only secret is which AI model each player is. The trio is trying to work out "
                          "which player is not their model; the imposter is trying to keep that unclear.", sp)
            self.assertIn("- The seats are Red, Blue, Green and Yellow.", sp)
            self.assertIn("3. Votes: after every 2 rounds a vote is held automatically (players cannot call votes).",
                          sp)
            self.assertIn("NAME RULE: Players may claim to be any AI model", sp)
            self.assertIn("OUTPUT RULES\n- Your replies are spoken aloud", sp)
            self.assertNotIn("MAYBE NO IMPOSTER", sp)
            self.assertNotIn("VOTE CALLS", sp)
            for coaching in ("punctuation", "write alike", "fourth copy", "Blend in"):
                self.assertNotIn(coaching, sp)
        self.assertIn("Your name in this game is Blue. You are the IMPOSTER.\nYou are GPT-6 Luna. Red, Green and "
                      "Yellow are all copies of one other AI model", imp)
        self.assertIn("Your goal: keep it unclear which player is the imposter, so the trio cannot tell which "
                      "player is not their model, and get one of them voted out.", imp)
        self.assertIn("Your name in this game is Red. You are a member of the trio.\nYou are Claude Haiku 5.5. "
                      "Two of Blue, Green and Yellow are exact copies of you", trio)
        self.assertNotIn("GPT-6 Luna", trio)
        # Trio and imposter share the exact same rules text.
        rules_part = lambda sp: sp[sp.index("You are playing"):]  # noqa: E731
        self.assertEqual(rules_part(trio), rules_part(imp))

    def test_tasks(self):
        self.assertEqual(self.ps.intro_task(40), (
            "=== YOUR TASK: INTRODUCTION ===\nWrite your introduction for the other players. The other two trio "
            "members are writing theirs at the same moment; you have not seen any introductions.\nIntroduce "
            "yourself however you think best serves your goal.\nAt most 40 words. Reply with the introduction only."))
        self.assertEqual(self.ps.vote_task(1, "Red", ["Blue", "Green", "Yellow"], 3), (
            "=== YOUR TASK: VOTE ===\nVote 1. This is a secret ballot; your reasoning here is private and will not "
            "be shown to the other players.\nChoose the one player you want voted out. A player with 3 or more "
            "votes is voted out. You cannot vote for yourself (Red).\nValid choices: Blue, Green, Yellow\nThink it "
            "through, then end your reply with exactly one line in this form:\nVOTE: <Name>"))
        ans = self.ps.answer_task("Blue", "Why?", "Red", 100, None, False, vote_calls=True)
        self.assertIn("Blue asked this question.\nQuestion from Blue: Why?\nAll players answer at the same time", ans)
        self.assertIn("\nCALL VOTE: YES\nor\nCALL VOTE: NO\n", ans)
        self.assertNotIn("CALL VOTE", self.ps.answer_task("Blue", "Why?", "Red", 100, None, False))

    def test_config_defaults(self):
        cfg = load_config(ROOT / "config.toml", read_key=False)
        self.assertEqual(cfg.rules.intro_word_limit, 40)
        self.assertTrue(cfg.rules.normalize_typography)
        self.assertTrue(cfg.rules.imposter_knows_own_model)
        self.assertEqual(cfg.rules.no_imposter_chance, 0.0)
        self.assertFalse(cfg.paraphrase.enabled)
        self.assertTrue(cfg.paraphrase.model)
        self.assertFalse(cfg.paraphrase.model.startswith(("anthropic/", "openai/")))
        self.assertEqual(cfg.paraphrase.max_output_tokens, 1000)

    def test_imposter_not_told_own_model(self):
        rules = RulesConfig(imposter_knows_own_model=False)
        imp = self.ps.system_prompt("Blue", "imposter", "GPT-6 Luna", rules, NAMES)
        self.assertNotIn("GPT-6 Luna", imp)
        self.assertIn("You are the IMPOSTER.\nRed, Green and Yellow are all copies", imp)
        trio = self.ps.system_prompt("Red", "trio", "Claude Haiku 5.5", rules, NAMES)
        self.assertIn("You are Claude Haiku 5.5.", trio)


# =============================================================================
# No-imposter games
# =============================================================================

class TestNoImposter(TempDirs):
    def test_rules_notice_only_when_chance_positive(self):
        notice = "There is a 25% chance that this game has no imposter at all"
        g0, _ = self.game(MockClient(seed=1, delay=0), cfg=make_cfg(no_imposter_chance=0.0))
        g1, _ = self.game(MockClient(seed=1, delay=0), cfg=make_cfg(no_imposter_chance=0.25))
        for p in g0.players:
            self.assertNotIn("no imposter", p.system_prompt.lower())
        for p in g1.players:
            self.assertIn(notice, p.system_prompt)
        trio = next(p for p in g1.players if p.role == "trio")
        self.assertIn("or there is no imposter and all three are copies of you", trio.system_prompt)

    def test_seeded_and_both_kinds_occur(self):
        kinds = {}
        for seed in range(30):
            g, _ = self.game(MockClient(seed=seed, delay=0), cfg=make_cfg(no_imposter_chance=0.5), seed=seed)
            g2, _ = self.game(MockClient(seed=seed, delay=0), cfg=make_cfg(no_imposter_chance=0.5), seed=seed)
            self.assertEqual(g.has_imposter, g2.has_imposter)
            self.assertEqual([p.role for p in g.players], [p.role for p in g2.players])
            kinds[g.has_imposter] = kinds.get(g.has_imposter, 0) + 1
        self.assertTrue(kinds.get(True) and kinds.get(False))
        # chance 0 keeps the old seat assignment for a given seed
        a, _ = self.game(MockClient(seed=1, delay=0), cfg=make_cfg(), seed=5)
        self.assertTrue(a.has_imposter)

    def test_trio_wins_by_voting_nobody(self):
        g, events = self.game(Scripted(vote_fn=lambda me: "NOBODY"), cfg=make_cfg(no_imposter_chance=1.0))
        self.assertFalse(g.has_imposter)
        self.assertIsNone(g.imposter)
        self.assertTrue(all(p.role == "trio" for p in g.players))
        self.assertFalse(events[0]["has_imposter"])
        play(g)
        go = events[-1]
        self.assertEqual((go["outcome"], go["winner"], go["has_imposter"]), ("trio_win_no_imposter", "trio", False))
        casts = [e for e in events if e["type"] == "vote_cast"]
        self.assertTrue(all(c["target"] is None and c["valid"] and c["abstain_reason"] == "nobody" for c in casts))
        vote_call = next(c for c in g.calls if c["purpose"] == "vote")
        self.assertIn("Valid choices: Blue, Green, Yellow, NOBODY", vote_call["user_message"]) \
            if vote_call["player"] == "Red" else self.assertIn(", NOBODY", vote_call["user_message"])
        self.assertIn("VOTE: NOBODY", vote_call["user_message"])
        # All four wrote intros blind (none of them saw another intro).
        intro_calls = [c for c in g.calls if c["purpose"] == "intro"]
        self.assertEqual(len(intro_calls), 4)
        for c in intro_calls:
            self.assertIn("(Nothing has happened yet.)", c["user_message"])
            self.assertNotIn("have already written", c["user_message"])
        row = self.summary()[-1]
        self.assertEqual((row["has_imposter"], row["no_imposter_chance"], row["imposter_seat"]), (False, 1.0, None))
        self.assertEqual(row["winner_model"], "anthropic/claude-haiku-5.5")
        self.assertEqual(row["votes"][0]["nobody_votes"], 4)

    def test_trio_loss_when_copy_voted_out(self):
        g, events = self.game(Scripted(vote_fn=lambda me: "Red" if me != "Red" else "Blue"),
                              cfg=make_cfg(no_imposter_chance=1.0))
        play(g)
        go = events[-1]
        self.assertEqual((go["outcome"], go["winner"], go["eliminated"]), ("trio_loss_no_imposter", None, "Red"))

    def test_failed_called_vote_continues(self):
        self.assertEqual(decide_after_vote(None, 0, RulesConfig(), "called", has_imposter=False), "continue")
        self.assertEqual(decide_after_vote(None, 2, RulesConfig(), "scheduled", has_imposter=False),
                         "trio_win_no_imposter")
        self.assertEqual(decide_after_vote(None, 2, RulesConfig(on_no_decision="continue", max_rounds=6),
                                           "scheduled", has_imposter=False), "continue")
        self.assertEqual(decide_after_vote("trio", 2, RulesConfig(), "called", has_imposter=False),
                         "trio_loss_no_imposter")

    def test_nobody_vote_parsing(self):
        self.assertEqual(parse_vote("VOTE: NOBODY", "Red", NAMES, allow_nobody=True), (prompts.NOBODY, None))
        self.assertEqual(parse_vote("vote: nobody.", "Red", NAMES, allow_nobody=True)[0], prompts.NOBODY)
        self.assertIsNone(parse_vote("VOTE: NOBODY", "Red", NAMES)[0])

    def test_nobody_option_hidden_when_chance_zero(self):
        g, _ = self.game(Scripted(vote_fn=lambda me: "NOBODY"))
        play(g)
        for c in g.calls:
            if c["purpose"] == "vote":
                self.assertNotIn("NOBODY", c["user_message"])
        casts = [e for e in g.events if e["type"] == "vote_cast"]
        self.assertTrue(all(not c["valid"] and c["abstain_reason"] == "invalid" for c in casts))


# =============================================================================
# Paraphrase
# =============================================================================

class TestParaphrase(TempDirs):
    def test_paraphrase_with_mock(self):
        g, events = self.game(Scripted(vote_fn=lambda me: "Purple", intro_text="I'm here, and I don't bite!"),
                              cfg=make_cfg(paraphrase=True))
        play(g)
        para = [e for e in events if e["type"] == "api_call" and e["purpose"] == "paraphrase"]
        public = [e for e in events if e["type"] == "message"]
        self.assertEqual(len(para), len(public))
        self.assertTrue(all(e["model"] == "mistralai/mistral-small-3.2-24b-instruct" for e in para))
        intro = next(e for e in public if e["kind"] == "intro")
        self.assertEqual(intro["text"], "I am here, and I do not bite.")
        self.assertIn("paraphrased", intro["transforms"])
        self.assertIn(intro["paraphrase_call_id"], {e["call_id"] for e in para})
        self.assertEqual({e["player"] for e in para}, set(NAMES))
        self.assertIn("mistralai/mistral-small-3.2-24b-instruct", g.cost_by_model)
        self.assertGreater(g.cost_by_model["mistralai/mistral-small-3.2-24b-instruct"], 0)
        call = next(c for c in g.calls if c["purpose"] == "paraphrase")
        self.assertIn("I'm here, and I don't bite!", call["user_message"])
        row = self.summary()[-1]
        self.assertEqual(row["paraphrase_model"], "mistralai/mistral-small-3.2-24b-instruct")

    def test_paraphrase_failure_falls_back(self):
        g, events = self.game(Scripted(vote_fn=lambda me: "Purple", intro_text="I'm here!",
                                       fail_purpose="paraphrase"), cfg=make_cfg(paraphrase=True))
        play(g)
        self.assertEqual(g.outcome, "no_decision")            # game carried on
        errs = [e for e in events if e["type"] == "error"]
        self.assertTrue(errs and all(not e["fatal"] for e in errs))
        intro = next(e for e in events if e["type"] == "message" and e["kind"] == "intro")
        self.assertEqual(intro["text"], "I'm here!")
        self.assertNotIn("paraphrased", intro["transforms"])
        self.assertIsNone(intro["paraphrase_call_id"])

    def test_name_rule_checks_original_too(self):
        g, events = self.game(Scripted(vote_fn=lambda me: "Purple", intro_text="I'm Claude, nice!"),
                              cfg=make_cfg(paraphrase=True, name_rule="no_names"))
        play(g)
        intro = next(e for e in events if e["type"] == "message" and e["kind"] == "intro")
        self.assertEqual(intro["text"], "I am [REDACTED], nice.")
        self.assertEqual(intro["transforms"], ["paraphrased", "redacted"])

    def test_off_by_default(self):
        g, events = self.game(Scripted(vote_fn=lambda me: "Purple"))
        play(g)
        self.assertNotIn("paraphrase", [e.get("purpose") for e in events if e["type"] == "api_call"])
        self.assertIsNone(self.summary()[-1]["paraphrase_model"])


# =============================================================================
# Templates: validation, save/reset, version, game integration
# =============================================================================

class TestTemplates(TempDirs):
    def test_registry_matches_files(self):
        files = {p.stem for p in prompts.DEFAULTS_DIR.glob("*.txt")}
        self.assertEqual(files, set(prompts.TEMPLATES))
        for tid in prompts.TEMPLATES:
            self.assertEqual(prompts.validate_template(tid, prompts.default_text(tid)),
                             {"errors": [], "warnings": []}, tid)

    def test_validation(self):
        v = prompts.validate_template("task_vote", "Vote {vote_number} {bogus}\nVOTE: <Name>")
        self.assertTrue(any("{bogus}" in e for e in v["errors"]))
        v = prompts.validate_template("task_vote", prompts.default_text("task_vote").replace("VOTE: <Name>", "x"))
        self.assertEqual(v["errors"], [])
        self.assertTrue(any("VOTE:" in w for w in v["warnings"]))
        v = prompts.validate_template("task_answer_vote_calls", "Answer {question}")
        self.assertTrue(any("CALL VOTE:" in w for w in v["warnings"]))
        self.assertTrue(any("{asker}" in w for w in v["warnings"]))          # unused placeholder
        self.assertTrue(prompts.validate_template("task_question", "Ask {round")["errors"])   # malformed
        self.assertEqual(prompts.validate_template("task_question", "Literal {{braces}} {round}")["errors"], [])
        self.assertTrue(prompts.validate_template("nope", "x")["errors"])
        self.assertTrue(prompts.validate_template("rules", "   ")["errors"])
        v = prompts.validate_template("phrases", "unknown_phrase = hi\nanswer_who_other = {asker} {oops}\nbad line")
        self.assertEqual(len(v["errors"]), 3)
        self.assertTrue(v["warnings"])                                        # missing phrases

    def test_save_reset_round_trip(self):
        d = self.prompts_dir
        tid = "task_question"
        original = prompts.default_text(tid)
        res = prompts.save_template(tid, "Ask {round} {bogus}", prompts_dir=d)
        self.assertFalse(res["ok"])
        self.assertFalse((d / f"{tid}.txt").exists())
        edited = original.replace("Ask ONE question", "Ask exactly ONE question")
        res = prompts.save_template(tid, edited + "\r\n", prompts_dir=d)
        self.assertTrue(res["ok"])
        self.assertTrue((d / f"{tid}.txt").exists())
        t = {x["id"]: x for x in prompts.list_templates(prompts_dir=d)}
        self.assertTrue(t[tid]["modified"])
        self.assertEqual(t[tid]["text"], edited)
        self.assertEqual(t[tid]["default_text"], original)
        self.assertEqual(t[tid]["group"], "Tasks")
        self.assertIn({"name": "round", "description": "Round number."}, t[tid]["placeholders"])
        self.assertTrue(prompts.prompts_modified(prompts_dir=d))
        prompts.reset_template(tid, prompts_dir=d)
        self.assertFalse((d / f"{tid}.txt").exists())
        self.assertFalse(prompts.prompts_modified(prompts_dir=d))
        # Saving the default text removes the override instead of writing it.
        prompts.save_template(tid, edited, prompts_dir=d)
        prompts.save_template(tid, original, prompts_dir=d)
        self.assertFalse((d / f"{tid}.txt").exists())
        self.assertFalse(prompts.reset_template("nope", prompts_dir=d)["ok"])

    def test_set_prompts_dir(self):
        old = prompts.get_prompts_dir()
        try:
            prompts.set_prompts_dir(self.prompts_dir)
            prompts.save_template("output_rules", "OUTPUT RULES\n- Be brief.")
            self.assertTrue((self.prompts_dir / "output_rules.txt").exists())
        finally:
            prompts.set_prompts_dir(old)
        self.assertEqual(prompts.get_prompts_dir(), old)

    def test_prompt_version(self):
        d = self.prompts_dir
        v1 = prompts.prompt_version(prompts_dir=d)
        self.assertRegex(v1, r"^[0-9a-f]{8}$")
        self.assertEqual(v1, prompts.prompt_version(prompts_dir=d))
        listed = prompts.list_templates(prompts_dir=d)
        self.assertEqual(v1, prompts.prompt_version(listed))
        self.assertEqual(v1, prompts.prompt_version({t["id"]: t["text"] for t in listed}))
        prompts.save_template("output_rules", "OUTPUT RULES\n- Be brief.", prompts_dir=d)
        v2 = prompts.prompt_version(prompts_dir=d)
        self.assertNotEqual(v1, v2)
        prompts.reset_template("output_rules", prompts_dir=d)
        self.assertEqual(v1, prompts.prompt_version(prompts_dir=d))

    def test_game_uses_overrides_and_records_version(self):
        default_version = prompts.prompt_version(prompts_dir=self.prompts_dir)
        prompts.save_template("output_rules", "OUTPUT RULES\n- Be brief and kind.", prompts_dir=self.prompts_dir)
        g, events = self.game(Scripted(vote_fn=lambda me: "Purple"))
        self.assertTrue(g.players[0].system_prompt.endswith("OUTPUT RULES\n- Be brief and kind."))
        started = events[0]
        self.assertNotEqual(started["prompt_version"], default_version)
        self.assertTrue(started["prompts_modified"])
        self.assertEqual(started["config"]["prompt_version"], started["prompt_version"])
        # A later edit does not affect a game already created.
        prompts.save_template("output_rules", "OUTPUT RULES\n- Changed again.", prompts_dir=self.prompts_dir)
        play(g)
        self.assertTrue(g.calls[0]["user_message"])
        record = json.loads(Path(events[-1]["log_file"]).read_text())
        self.assertEqual(record["prompt_version"], started["prompt_version"])
        self.assertEqual(record["templates"]["output_rules"], "OUTPUT RULES\n- Be brief and kind.")
        row = self.summary()[-1]
        self.assertEqual((row["prompt_version"], row["prompts_modified"]), (started["prompt_version"], True))

    def test_invalid_override_file_falls_back(self):
        self.prompts_dir.mkdir(parents=True)
        (self.prompts_dir / "task_question.txt").write_text("Ask {nonsense} {round")
        g, events = self.game(Scripted(vote_fn=lambda me: "Purple"))
        warn = [e for e in events if e["type"] == "error"]
        self.assertTrue(warn and not warn[0]["fatal"])
        self.assertEqual(g.prompts.templates["task_question"], prompts.default_text("task_question"))

    def test_engine_does_not_parse_editable_text(self):
        # Rewrite every task header: the engine and mock must not care.
        for tid in ("task_intro", "task_imposter_intro", "task_question", "task_answer", "task_vote"):
            text = prompts.default_text(tid).split("\n", 1)[1]
            prompts.save_template(tid, "### " + tid.upper() + "\n" + text, prompts_dir=self.prompts_dir)
        prompts.save_template("role_imposter", prompts.default_text("role_imposter").replace(
            "You are the IMPOSTER.", "You are the odd one out."), prompts_dir=self.prompts_dir)
        for seed in range(3):
            g, events = self.game(MockClient(seed=seed, delay=0), seed=seed)
            play(g)
            self.assertIn(g.outcome, ("trio_win", "imposter_win", "no_decision"))
            casts = [e for e in events if e["type"] == "vote_cast"]
            self.assertTrue(any(c["valid"] for c in casts))

    def test_preview(self):
        cfg = make_cfg(no_imposter_chance=0.1, player_vote_calls=True)
        p = prompts.preview(cfg, prompts_dir=self.prompts_dir)
        self.assertEqual(set(p["tasks"]) >= {"intro", "imposter_intro", "question", "answer", "vote"}, True)
        self.assertIn("YOUR SECRET ROLE", p["trio_system"])
        self.assertIn("You are the IMPOSTER.", p["imposter_system"])
        self.assertIn("There is a 10% chance", p["trio_system"])
        self.assertIn("CALL VOTE: YES", p["tasks"]["answer"])
        self.assertIn("NOBODY", p["tasks"]["vote"])
        self.assertRegex(p["prompt_version"], r"^[0-9a-f]{8}$")


# =============================================================================
# Summary + analyze
# =============================================================================

class TestSummaryAndAnalyze(TempDirs):
    def test_summary_fields(self):
        g, _ = self.game(MockClient(seed=3, delay=0),
                         cfg=make_cfg(no_imposter_chance=0.3, imposter_knows_own_model=False))
        play(g)
        row = self.summary()[-1]
        for key in ("prompt_version", "prompts_modified", "has_imposter", "no_imposter_chance",
                    "normalize_typography", "paraphrase_model", "imposter_knows_own_model", "intro_word_limit"):
            self.assertIn(key, row)
        self.assertEqual(row["no_imposter_chance"], 0.3)
        self.assertFalse(row["imposter_knows_own_model"])
        self.assertTrue(row["normalize_typography"])
        self.assertEqual(row["intro_word_limit"], 40)
        self.assertFalse(row["prompts_modified"])

    def test_stats_and_analyze_prompt_version(self):
        import analyze
        from imposter_game.stats import compute_stats
        for seed in range(2):
            g, _ = self.game(Scripted(vote_fn=lambda me: "NOBODY"), cfg=make_cfg(no_imposter_chance=1.0), seed=seed)
            play(g)
        g, _ = self.game(MockClient(seed=9, delay=0))
        play(g)
        version = g.prompts.version
        # a legacy row (no prompt_version, no has_imposter)
        legacy = dict(self.summary()[-1])
        for k in ("prompt_version", "has_imposter", "prompts_modified"):
            legacy.pop(k)
        legacy["game_id"] = "legacy-1"
        with open(self.log_dir / "mock" / "summary.jsonl", "a") as f:
            f.write(json.dumps(legacy) + "\n")

        s = compute_stats(self.summary())
        self.assertEqual(s["no_imposter"]["games"], 2)
        self.assertEqual(s["no_imposter"]["trio_wins"], 2)
        self.assertEqual(s["no_imposter"]["false_elimination_rate"], 0.0)
        self.assertEqual(s["num_imposter_games"], 2)
        self.assertEqual(s["imposter_caught"]["decisive_games"],
                         sum(1 for r in self.summary() if r["outcome"] in ("trio_win", "imposter_win")))
        versions = {v["prompt_version"]: v for v in s["by_prompt_version"]}
        self.assertEqual(versions[version]["games"], 3)
        self.assertEqual(versions["legacy"]["games"], 1)

        out = io.StringIO()
        with redirect_stdout(out):
            analyze.main(["--log-dir", str(self.log_dir), "--mock-only"])
        text = out.getvalue()
        self.assertIn("By prompt version", text)
        self.assertIn("No-imposter games: 2", text)
        out = io.StringIO()
        with redirect_stdout(out):
            analyze.main(["--log-dir", str(self.log_dir), "--mock-only", "--prompt-version", version])
        self.assertIn("Games: 3", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            analyze.main(["--log-dir", str(self.log_dir), "--mock-only", "--prompt-version", "legacy"])
        self.assertIn("Games: 1", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            analyze.main(["--log-dir", str(self.log_dir), "--mock-only", "--prompt-version", "deadbeef"])
        self.assertIn("Versions found", out.getvalue())


if __name__ == "__main__":
    unittest.main()
