"""Tests for the game engine: rules, parsing, name rule, and full mock games.

Run from the repo root:  python -m unittest discover -s tests -v
"""

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

from imposter_game import prompts  # noqa: E402
from imposter_game.config import (ConfigError, ModelConfig, RulesConfig, GameConfig,  # noqa: E402
                                  config_from_dict, load_config)
from imposter_game.game import (Game, build_name_terms, clean_reply, decide_after_vote,  # noqa: E402
                                find_name_violations, limit_words, parse_vote, parse_vote_call,
                                redact_names, resolve_vote, vote_call_triggered, vote_due, vote_trigger)
from imposter_game.openrouter import MockClient  # noqa: E402

NAMES = ["Red", "Blue", "Green", "Yellow"]


def make_cfg(**rules) -> GameConfig:
    """Test config. Player vote calls are OFF unless a test turns them on."""
    rules.setdefault("player_vote_calls", False)
    return GameConfig(
        trio=ModelConfig("anthropic/claude-haiku-5.5", "Claude Haiku 5.5", "high"),
        imposter=ModelConfig("openai/gpt-6-luna", "GPT-6 Luna", "max", max_output_tokens=32000),
        rules=RulesConfig(**rules),
    )


# =============================================================================
# Rule functions
# =============================================================================

class TestResolveVote(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()

    def test_three_votes_eliminates(self):
        out = resolve_vote({"Red": "Blue", "Blue": "Red", "Green": "Blue", "Yellow": "Blue"}, self.cfg)
        self.assertEqual(out.eliminated, "Blue")
        self.assertEqual(out.tally, {"Red": 1, "Blue": 3, "Green": 0, "Yellow": 0})

    def test_four_way_tally_includes_everyone(self):
        out = resolve_vote({"Red": "Blue", "Blue": "Green", "Green": "Yellow", "Yellow": "Red"}, self.cfg)
        self.assertIsNone(out.eliminated)
        self.assertEqual(set(out.tally), set(NAMES))

    def test_two_two_split_is_no_decision(self):
        out = resolve_vote({"Red": "Blue", "Blue": "Red", "Green": "Blue", "Yellow": "Red"}, self.cfg)
        self.assertIsNone(out.eliminated)
        self.assertEqual(sorted(out.top), ["Blue", "Red"])

    def test_two_one_one_is_no_decision(self):
        out = resolve_vote({"Red": "Blue", "Blue": "Red", "Green": "Blue", "Yellow": "Green"}, self.cfg)
        self.assertIsNone(out.eliminated)
        self.assertEqual(out.tally["Blue"], 2)

    def test_abstentions(self):
        out = resolve_vote({"Red": "Blue", "Blue": None, "Green": "Blue", "Yellow": None}, self.cfg)
        self.assertIsNone(out.eliminated)
        out = resolve_vote({"Red": "Blue", "Blue": None, "Green": "Blue", "Yellow": "Blue"}, self.cfg)
        self.assertEqual(out.eliminated, "Blue")
        out = resolve_vote({n: None for n in NAMES}, self.cfg)
        self.assertIsNone(out.eliminated)

    def test_self_votes_ignored(self):
        out = resolve_vote({"Red": "Red", "Blue": "Red", "Green": "Red", "Yellow": "Blue"}, self.cfg)
        self.assertIsNone(out.eliminated)

    def test_tie_at_lower_threshold_is_no_decision(self):
        cfg = make_cfg(vote_threshold=2)
        out = resolve_vote({"Red": "Blue", "Blue": "Red", "Green": "Blue", "Yellow": "Red"}, cfg)
        self.assertIsNone(out.eliminated)
        out = resolve_vote({"Red": "Blue", "Blue": "Red", "Green": "Blue", "Yellow": None}, cfg)
        self.assertEqual(out.eliminated, "Blue")

    def test_accepts_rules_directly(self):
        out = resolve_vote({"Red": "Blue", "Green": "Blue", "Yellow": "Blue", "Blue": "Red"}, self.cfg.rules)
        self.assertEqual(out.eliminated, "Blue")


class TestDecisions(unittest.TestCase):
    def test_decide(self):
        end = RulesConfig(on_no_decision="end")
        cont = RulesConfig(on_no_decision="continue", max_rounds=6)
        self.assertEqual(decide_after_vote("imposter", 2, end), "trio_win")
        self.assertEqual(decide_after_vote("trio", 2, cont), "imposter_win")
        self.assertEqual(decide_after_vote(None, 2, end), "no_decision")
        self.assertEqual(decide_after_vote(None, 2, cont), "continue")
        self.assertEqual(decide_after_vote(None, 6, cont), "no_decision")

    def test_vote_due(self):
        r = RulesConfig(rounds_before_vote=2, on_no_decision="continue", max_rounds=5)
        self.assertEqual([n for n in range(0, 7) if vote_due(n, r)], [2, 4, 5, 6])
        r = RulesConfig(rounds_before_vote=3, on_no_decision="end")
        self.assertEqual([n for n in range(0, 7) if vote_due(n, r)], [3, 6])


class TestParseVote(unittest.TestCase):
    def test_basic_and_case_insensitive(self):
        self.assertEqual(parse_vote("I think so.\nVOTE: Blue", "Red", NAMES), ("Blue", None))
        self.assertEqual(parse_vote("vote: blue", "Red", NAMES), ("Blue", None))
        self.assertEqual(parse_vote("Vote:GREEN", "Red", NAMES), ("Green", None))

    def test_punctuation_and_markdown(self):
        self.assertEqual(parse_vote("VOTE: blue.", "Red", NAMES)[0], "Blue")
        self.assertEqual(parse_vote("**VOTE: Yellow**", "Red", NAMES)[0], "Yellow")
        self.assertEqual(parse_vote("VOTE: **Green**", "Red", NAMES)[0], "Green")
        self.assertEqual(parse_vote("VOTE: <Blue>", "Red", NAMES)[0], "Blue")

    def test_self_vote_invalid(self):
        target, problem = parse_vote("VOTE: Red", "Red", NAMES)
        self.assertIsNone(target)
        self.assertIn("yourself", problem)

    def test_unknown_or_missing(self):
        self.assertIsNone(parse_vote("VOTE: Purple", "Red", NAMES)[0])
        self.assertIsNone(parse_vote("VOTE: nobody", "Red", NAMES)[0])
        self.assertIsNone(parse_vote("I vote for Blue", "Red", NAMES)[0])
        self.assertIsNone(parse_vote("", "Red", NAMES)[0])

    def test_last_vote_line_wins(self):
        text = "VOTE: Blue\nOn reflection, no.\nVOTE: Green"
        self.assertEqual(parse_vote(text, "Red", NAMES)[0], "Green")


class TestTextRules(unittest.TestCase):
    def setUp(self):
        self.terms = build_name_terms(make_cfg())

    def test_detects_names(self):
        m = find_name_violations("Hi, I'm Claude Haiku 5.5 and I love OpenAI.", self.terms)
        self.assertIn("Claude Haiku 5.5", m)
        self.assertTrue(any(x.lower() == "openai" for x in m))
        self.assertTrue(find_name_violations("I'm gpt-6 luna", self.terms))
        self.assertTrue(find_name_violations("model anthropic/claude-haiku-5.5 here", self.terms))

    def test_whole_words_only(self):
        self.assertEqual(find_name_violations("That metaphor is solid and lunar.", self.terms), [])
        self.assertEqual(find_name_violations("A totally normal sentence.", self.terms), [])

    def test_redact(self):
        self.assertEqual(redact_names("I am GPT, made by OpenAI.", self.terms), "I am [REDACTED], made by [REDACTED].")

    def test_clean_reply(self):
        self.assertEqual(clean_reply("Red: Hello there", NAMES), "Hello there")
        self.assertEqual(clean_reply("**Blue:** Hello", NAMES), "Hello")
        self.assertEqual(clean_reply('"Quoted answer"', NAMES), "Quoted answer")
        self.assertEqual(clean_reply("Answer: 391", NAMES), "391")
        self.assertEqual(clean_reply("Redwood trees are tall.", NAMES), "Redwood trees are tall.")

    def test_limit_words(self):
        self.assertEqual(limit_words("a b c", 5), "a b c")
        self.assertTrue(limit_words(" ".join(["w"] * 30), 10).endswith("[…]"))


# =============================================================================
# Full games
# =============================================================================

class ScriptedClient:
    """Wraps MockClient; votes are decided by `vote_fn(vote_number, me, is_imposter) -> target text`."""

    def __init__(self, vote_fn=None, seed=0, intro_text=None, call_fn=None):
        self.base = MockClient(seed=seed, delay=0)
        self.vote_fn = vote_fn
        self.call_fn = call_fn      # call_fn(turn_index (1-based, whole game), me) -> extra last line or None
        self.intro_text = intro_text
        self.lock = threading.Lock()
        self.log: list[tuple[str, str]] = []    # (player, task) in call order

    def chat(self, model, messages, reasoning_effort, max_tokens, session_id=None, purpose=None, context=None):
        res = self.base.chat(model, messages, reasoning_effort, max_tokens, session_id=session_id,
                             purpose=purpose, context=context)
        ctx = context or {}
        me = ctx.get("player")
        with self.lock:
            self.log.append((me, purpose))
        if purpose in ("vote", "vote_retry") and self.vote_fn:
            target = self.vote_fn(ctx["vote_number"], me, ctx["is_imposter"])
            return dataclasses.replace(res, content=f"Some private reasoning.\nVOTE: {target}")
        if purpose == "intro" and self.intro_text:
            return dataclasses.replace(res, content=self.intro_text)
        if purpose == "answer" and self.call_fn:
            turn_index = (ctx["round"] - 1) * 4 + ctx["turn"]
            body = parse_vote_call(res.content)[1]
            line = self.call_fn(turn_index, me)
            return dataclasses.replace(res, content=body + (f"\n{line}" if line else ""))
        return res


class FailingClient(MockClient):
    def __init__(self, fail_after: int):
        super().__init__(seed=1, delay=0)
        self.n = 0
        self.lock = threading.Lock()
        self.fail_after = fail_after

    def chat(self, *a, **kw):
        with self.lock:
            self.n += 1
            n = self.n
        if n > self.fail_after:
            raise RuntimeError("simulated API outage")
        return super().chat(*a, **kw)


def play(game: Game, max_steps: int = 500) -> list[dict]:
    results = []
    for _ in range(max_steps):
        if game.is_finished():
            break
        results.append(game.step())
    return results


class GameTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def summary_rows(self, mock=True):
        p = self.log_dir / ("mock" if mock else "") / "summary.jsonl"
        return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]

    def new_game(self, client, seed=7, mock=True, **rules):
        events = []
        g = Game(make_cfg(**rules), client, on_event=events.append, log_dir=self.log_dir, mock=mock, seed=seed)
        return g, events

    def check_common(self, g: Game, events: list[dict]):
        # seq strictly increasing, delivered in order, same as g.events
        seqs = [e["seq"] for e in events]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))
        self.assertEqual(events, g.events)
        for e in events:
            for k in ("type", "seq", "ts", "game_id"):
                self.assertIn(k, e)
        self.assertEqual(events[0]["type"], "game_started")
        self.assertEqual(events[-1]["type"], "game_over")
        self.assertEqual(sum(e["type"] == "game_over" for e in events), 1)
        self.assertTrue(g.is_finished())
        self.assertEqual(g.next_label(), "")

        names = [p.name for p in g.players]
        imp = g.imposter.name
        # Intros: imposter's call happens after all trio intro calls; displayed in seat order.
        intro_calls = [e for e in events if e["type"] == "api_call" and e["purpose"] == "intro"]
        if intro_calls:
            self.assertEqual(intro_calls[-1]["player"], imp)
            self.assertEqual(len(intro_calls), 4)
            intro_msgs = [e for e in events if e["type"] == "message" and e["kind"] == "intro"]
            self.assertEqual([m["player"] for m in intro_msgs], names)
            self.assertGreater(intro_msgs[0]["seq"], intro_calls[-1]["seq"])

        # Every question answered by everyone, in seat order starting after the asker.
        questions = [e for e in events if e["type"] == "message" and e["kind"] == "question"]
        for q in questions:
            ans = [e for e in events if e["type"] == "message" and e["kind"] == "answer"
                   and e["question_id"] == q["question_id"]]
            i = names.index(q["player"])
            self.assertEqual([a["player"] for a in ans], names[i + 1:] + names[:i + 1])
            for a in ans:
                self.assertGreater(a["seq"], q["seq"])
                self.assertEqual(a["asker"], q["player"])
        # Questions asked in seat order each round.
        by_round = {}
        for q in questions:
            by_round.setdefault(q["round"], []).append(q["player"])
        for r, askers in by_round.items():
            if r <= g.rounds_completed:
                self.assertEqual(askers, names)

        # Scheduled votes happen only after every rounds_before_vote rounds, after the last answer of that
        # round; called votes right after a turn whose vote_call_check triggered.
        rules = g.rules
        checks = [e for e in events if e["type"] == "vote_call_check"]
        self.assertEqual(len(checks), g.turns_completed if rules.player_vote_calls else 0)
        for vr in [e for e in events if e["type"] == "vote_result"]:
            rc = vr["rounds_completed"]
            prior_q = [q for q in questions if q["seq"] < vr["seq"]]
            self.assertEqual(len(prior_q), vr["turns_completed"])
            if vr["trigger"] == "scheduled":
                self.assertTrue(rc % rules.rounds_before_vote == 0 or rc == rules.max_rounds)
                self.assertEqual(len(prior_q), 4 * rc)
            else:
                self.assertTrue(rules.player_vote_calls)
                last_check = [c for c in checks if c["seq"] < vr["seq"]][-1]
                self.assertTrue(last_check["triggered"])
                if vr["trigger"] == "called+scheduled":
                    self.assertEqual(len(prior_q), 4 * rc)
            casts = [e for e in events if e["type"] == "vote_cast" and e["vote_number"] == vr["vote_number"]]
            self.assertEqual(len(casts), 4)
            for c in casts:
                self.assertNotEqual(c["target"], c["player"])

        # Summary row
        rows = self.summary_rows(mock=g.mock)
        row = [r for r in rows if r["game_id"] == g.game_id]
        self.assertEqual(len(row), 1)
        row = row[0]
        go = events[-1]
        self.assertEqual(row["outcome"], go["outcome"])
        self.assertEqual(row["rounds_completed"], go["rounds_completed"])
        self.assertEqual(row["turns_completed"], go["turns_completed"])
        self.assertEqual(row["turns_completed"], len(questions) if g.outcome != "error" else row["turns_completed"])
        self.assertEqual(row["called_votes"] + row["scheduled_votes"]
                         - sum(1 for v in row["votes"] if v["trigger"] == "called+scheduled"), len(row["votes"]))
        self.assertEqual(row["imposter_seat"], imp)
        self.assertTrue(Path(go["log_file"]).exists())
        self.assertTrue(Path(go["transcript_file"]).exists())
        record = json.loads(Path(go["log_file"]).read_text())
        self.assertEqual(len(record["events"]), len(events))
        self.assertEqual(len(record["calls"]), go["num_api_calls"])
        return row


class TestFullGames(GameTestBase):
    def test_trio_wins_and_rounds_survived(self):
        holder = {}

        def vote(n, me, is_imp):
            if is_imp:
                return next(x for x in NAMES if x != me)
            return holder["imp"]

        g, events = self.new_game(ScriptedClient(vote))
        holder["imp"] = g.imposter.name
        self.assertEqual(g.next_label(), "Introductions")
        steps = play(g)
        self.assertEqual(g.outcome, "trio_win")
        # intro + 8 turns + 1 vote
        self.assertEqual(len(steps), 10)
        self.assertEqual([s["boundary"] for s in steps],
                         [True, False, False, False, True, False, False, False, True, True])
        go = events[-1]
        self.assertEqual(go["winner"], "trio")
        self.assertEqual(go["eliminated"], g.imposter.name)
        self.assertEqual(go["eliminated_role"], "imposter")
        self.assertEqual(go["rounds_survived"], 2)
        row = self.check_common(g, events)
        self.assertEqual(row["winner_model"], "openai/gpt-6-luna".replace("openai/gpt-6-luna", make_cfg().trio.model))
        self.assertEqual(row["imposter_votes_received_by_trio"], [3])

    def test_imposter_wins(self):
        holder = {}

        def vote(n, me, is_imp):
            victim = holder["victim"]
            return victim if me != victim else holder["imp"]

        g, events = self.new_game(ScriptedClient(vote), seed=3)
        holder["imp"] = g.imposter.name
        holder["victim"] = next(p.name for p in g.players if p.role == "trio")
        play(g)
        self.assertEqual(g.outcome, "imposter_win")
        self.assertEqual(events[-1]["eliminated"], holder["victim"])
        self.assertEqual(events[-1]["eliminated_role"], "trio")
        row = self.check_common(g, events)
        self.assertEqual(row["winner_model"], "openai/gpt-6-luna")

    def split_vote(self, n, me, is_imp):
        # Red & Blue vote Green; Green & Yellow vote Red  -> 2-2 split
        return {"Red": "Green", "Blue": "Green", "Green": "Red", "Yellow": "Red"}[me]

    def test_no_decision_end_mode(self):
        g, events = self.new_game(ScriptedClient(self.split_vote), on_no_decision="end")
        play(g)
        self.assertEqual(g.outcome, "no_decision")
        self.assertEqual(g.rounds_completed, 2)
        self.assertIsNone(events[-1]["rounds_survived"])
        vr = [e for e in events if e["type"] == "vote_result"]
        self.assertEqual(len(vr), 1)
        self.assertEqual(vr[0]["decision"], "no_decision")
        self.check_common(g, events)

    def test_no_decision_continue_mode_until_max_rounds(self):
        g, events = self.new_game(ScriptedClient(self.split_vote), on_no_decision="continue", max_rounds=6)
        play(g)
        self.assertEqual(g.outcome, "no_decision")
        self.assertEqual(g.rounds_completed, 6)
        vr = [e for e in events if e["type"] == "vote_result"]
        self.assertEqual([v["rounds_completed"] for v in vr], [2, 4, 6])
        self.assertEqual([v["decision"] for v in vr], ["continue", "continue", "no_decision"])
        self.check_common(g, events)

    def test_continue_mode_then_elimination(self):
        holder = {}

        def vote(n, me, is_imp):
            if n == 1:
                return self.split_vote(n, me, is_imp)
            return holder["imp"] if me != holder["imp"] else next(x for x in NAMES if x != me)

        g, events = self.new_game(ScriptedClient(vote), on_no_decision="continue", max_rounds=6,
                                  answer_mode="sequential")
        holder["imp"] = g.imposter.name
        play(g)
        self.assertEqual(g.outcome, "trio_win")
        self.assertEqual(events[-1]["rounds_survived"], 4)
        self.assertEqual(events[-1]["rounds_completed"], 4)
        self.check_common(g, events)

    def test_sequential_answers_see_earlier_answers(self):
        client = ScriptedClient(self.split_vote)
        g, events = self.new_game(client, answer_mode="sequential")
        play(g)
        self.check_common(g, events)
        # The 2nd answerer of the first question saw the 1st answer.
        q = next(e for e in events if e["type"] == "message" and e["kind"] == "question")
        answers = [e for e in events if e["type"] == "message" and e["question_id"] == q["question_id"]
                   and e["kind"] == "answer"]
        second_call = next(c for c in g.calls if c["call_id"] == answers[1]["call_id"])
        self.assertIn(f"{answers[0]['player']} answered: {answers[0]['text']}", second_call["user_message"])
        # In sequential mode, thinking/api_call/message alternate per answerer.
        a_events = [e["type"] for e in events if e.get("round") == 1 and e.get("player") == answers[1]["player"]
                    and e["type"] in ("thinking", "message")]
        self.assertIn("thinking", a_events)

    def test_simultaneous_answers_are_blind(self):
        g, events = self.new_game(ScriptedClient(self.split_vote), answer_mode="simultaneous")
        play(g)
        self.check_common(g, events)
        q = next(e for e in events if e["type"] == "message" and e["kind"] == "question")
        answers = [e for e in events if e["type"] == "message" and e["question_id"] == q["question_id"]
                   and e["kind"] == "answer"]
        for a in answers[1:]:
            call = next(c for c in g.calls if c["call_id"] == a["call_id"])
            self.assertNotIn(answers[0]["text"], call["user_message"])
        # All four answer "thinking" events come before the first answer message.
        first_answer_seq = answers[0]["seq"]
        thinking = [e for e in events if e["type"] == "thinking" and e["action"] == "answer" and e["round"] == 1]
        self.assertTrue(all(t["seq"] < first_answer_seq for t in thinking[:4]))

    def test_imposter_sees_trio_intros_trio_does_not(self):
        g, events = self.new_game(ScriptedClient(self.split_vote))
        play(g)
        intros = {e["player"]: e["text"] for e in events if e["type"] == "message" and e["kind"] == "intro"}
        for c in g.calls:
            if c["purpose"] != "intro":
                continue
            others = [intros[n] for n in intros if n != c["player"] and g.by_name[n].role == "trio"]
            if c["player"] == g.imposter.name:
                for t in others:
                    self.assertIn(t, c["user_message"])
            else:
                self.assertIn("(Nothing has happened yet.)", c["user_message"])

    def test_roles_never_in_public_prompts(self):
        g, events = self.new_game(ScriptedClient(self.split_vote))
        play(g)
        imp = g.imposter.name
        for c in g.calls:
            transcript = c["user_message"].split(prompts.TRANSCRIPT_END)[0]
            self.assertIsNone(re.search(r"\b(Red|Blue|Green|Yellow)\s*\((imposter|trio)\)", transcript))
            self.assertNotIn(f"{imp} is the imposter", c["user_message"])
            self.assertNotIn("openai/gpt-6-luna", c["user_message"])
        # The trio's system prompt never names the imposter's model.
        for p in g.players:
            if p.role == "trio":
                self.assertNotIn("GPT-6 Luna", p.system_prompt)
            else:
                self.assertNotIn("Claude Haiku 5.5", p.system_prompt)
                self.assertIn("You are the IMPOSTER.", p.system_prompt)

    def test_random_mock_games_all_modes(self):
        for i, (ond, mode, rule, calls) in enumerate([("end", "simultaneous", "lie_allowed", False),
                                                      ("end", "sequential", "no_names", True),
                                                      ("continue", "simultaneous", "no_names", False),
                                                      ("continue", "sequential", "lie_allowed", True)]):
            for seed in range(3):
                with self.subTest(ond=ond, mode=mode, seed=seed, calls=calls):
                    g, events = self.new_game(MockClient(seed=seed * 10 + i, delay=0), seed=seed,
                                              on_no_decision=ond, answer_mode=mode, name_rule=rule,
                                              player_vote_calls=calls)
                    play(g)
                    self.assertIn(g.outcome, ("trio_win", "imposter_win", "no_decision"))
                    row = self.check_common(g, events)
                    if g.eliminated:
                        self.assertEqual(row["rounds_survived"], g.rounds_completed)
        rows = self.summary_rows()
        self.assertEqual(len(rows), 12)

    def test_real_games_logged_outside_mock(self):
        g, events = self.new_game(ScriptedClient(self.split_vote), mock=False)
        play(g)
        self.assertTrue((self.log_dir / "summary.jsonl").exists())
        self.assertFalse((self.log_dir / "mock" / "summary.jsonl").exists())
        self.check_common(g, events)


SPLIT = {"Red": "Green", "Blue": "Green", "Green": "Red", "Yellow": "Red"}   # 2-2 split: nobody out


class TestVoteCallParsing(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(parse_vote_call("My answer.\nCALL VOTE: YES"), (True, "My answer."))
        self.assertEqual(parse_vote_call("My answer.\ncall vote: no"), (False, "My answer."))
        self.assertEqual(parse_vote_call("Answer\n**CALL VOTE:** yes."), (True, "Answer"))
        self.assertEqual(parse_vote_call("Answer\n**CALL VOTE**: No!"), (False, "Answer"))
        self.assertEqual(parse_vote_call("Answer\nCall Vote:YES"), (True, "Answer"))
        self.assertEqual(parse_vote_call("Answer\n  `CALL VOTE: no`  "), (False, "Answer"))
        self.assertEqual(parse_vote_call("Short answer. CALL VOTE: YES"), (True, "Short answer."))

    def test_last_line_wins_and_all_stripped(self):
        wants, text = parse_vote_call("CALL VOTE: YES\nActually...\nCALL VOTE: NO")
        self.assertFalse(wants)
        self.assertEqual(text, "Actually...")

    def test_missing_or_unreadable(self):
        self.assertEqual(parse_vote_call("Just an answer."), (None, "Just an answer."))
        self.assertEqual(parse_vote_call("Answer\nCALL VOTE: maybe"), (None, "Answer"))
        self.assertEqual(parse_vote_call(""), (None, ""))
        # Ordinary words are left alone.
        self.assertEqual(parse_vote_call("I would call a vote soon."), (None, "I would call a vote soon."))

    def test_rule_helpers(self):
        on = RulesConfig(player_vote_calls=True, vote_call_threshold=3)
        self.assertTrue(vote_call_triggered(["Red", "Blue", "Green"], on))
        self.assertFalse(vote_call_triggered(["Red", "Blue"], on))
        self.assertFalse(vote_call_triggered(NAMES, RulesConfig(player_vote_calls=False)))
        self.assertEqual(vote_trigger(True, True), "called+scheduled")
        self.assertEqual(vote_trigger(True, False), "called")
        self.assertEqual(vote_trigger(False, True), "scheduled")
        self.assertIsNone(vote_trigger(False, False))
        end = RulesConfig(on_no_decision="end")
        self.assertEqual(decide_after_vote(None, 0, end, "called"), "continue")
        self.assertEqual(decide_after_vote(None, 2, end, "called+scheduled"), "no_decision")
        self.assertEqual(decide_after_vote("imposter", 0, end, "called"), "trio_win")
        self.assertEqual(decide_after_vote("trio", 1, end, "called"), "imposter_win")


class TestVoteCalls(GameTestBase):
    def calls_game(self, call_fn, vote_fn=lambda n, me, imp: SPLIT[me], **rules):
        rules.setdefault("player_vote_calls", True)
        client = ScriptedClient(vote_fn, call_fn=call_fn)
        return self.new_game(client, **rules)

    def test_three_yes_calls_a_vote_next_step(self):
        # Red, Blue, Green ask for a vote after the very first question.
        def call_fn(turn, me):
            return "CALL VOTE: YES" if turn == 1 and me != "Yellow" else "CALL VOTE: NO"

        g, events = self.calls_game(call_fn, on_no_decision="end")
        self.assertTrue(g.step()["boundary"])                     # intros
        self.assertEqual(g.next_label(), "Round 1 · Red asks")
        self.assertEqual(g.step(), {"boundary": True, "finished": False})   # turn 1 triggers a vote
        self.assertEqual(g.next_label(), "Vote 1 (called by players)")
        check = [e for e in events if e["type"] == "vote_call_check"]
        self.assertEqual(len(check), 1)
        self.assertEqual((check[0]["requesters"], check[0]["count"], check[0]["threshold"], check[0]["triggered"]),
                         (["Red", "Blue", "Green"], 3, 3, True))
        answers = [e for e in events if e["type"] == "message" and e["kind"] == "answer"]
        self.assertEqual({a["player"]: a["wants_vote"] for a in answers},
                         {"Red": True, "Blue": True, "Green": True, "Yellow": False})
        for a in answers:
            self.assertNotIn("CALL VOTE", a["text"])
            self.assertIn("CALL VOTE", a["original_text"])
        self.assertEqual(g.step(), {"boundary": True, "finished": False})   # the called vote: 2-2, continues
        phase = [e for e in events if e["type"] == "phase" and e["phase"] == "vote"][0]
        self.assertEqual((phase["trigger"], phase["label"]), ("called", "Vote 1 (called by players)"))
        vr = [e for e in events if e["type"] == "vote_result"][0]
        self.assertEqual((vr["trigger"], vr["decision"], vr["rounds_completed"]), ("called", "continue", 0))
        self.assertEqual(g.next_label(), "Round 1 · Blue asks")    # resumes at the next turn
        play(g)
        self.assertEqual(g.outcome, "no_decision")               # the scheduled vote after round 2 ends it
        vrs = [e for e in events if e["type"] == "vote_result"]
        self.assertEqual([v["trigger"] for v in vrs], ["called", "scheduled"])
        self.assertEqual([v["vote_number"] for v in vrs], [1, 2])
        row = self.check_common(g, events)
        self.assertEqual((row["called_votes"], row["scheduled_votes"], row["turns_completed"]), (1, 1, 8))
        # Later prompts announce the called vote; requests stay secret (reveal_vote_calls off).
        later = [c for c in g.calls if c["purpose"] == "question"][-1]["user_message"]
        self.assertIn("The players called a vote (3 of 4 requested one).", later)
        self.assertNotIn("Vote requests:", later)
        vote_prompt = next(c for c in g.calls if c["purpose"] == "vote")["user_message"]
        self.assertIn("This vote was called by the players (3 asked for it).", vote_prompt)
        self.assertIn("the game continues", vote_prompt)

    def test_two_yes_is_not_enough(self):
        def call_fn(turn, me):
            return "CALL VOTE: YES" if me in ("Red", "Blue") else "CALL VOTE: NO"

        g, events = self.calls_game(call_fn)
        play(g)
        checks = [e for e in events if e["type"] == "vote_call_check"]
        self.assertEqual(len(checks), 8)
        self.assertTrue(all(c["count"] == 2 and not c["triggered"] for c in checks))
        vrs = [e for e in events if e["type"] == "vote_result"]
        self.assertEqual([v["trigger"] for v in vrs], ["scheduled"])
        self.check_common(g, events)

    def test_called_and_scheduled_merge(self):
        # Everyone asks after the last question of round 2, exactly when the scheduled vote is due.
        def call_fn(turn, me):
            return "CALL VOTE: YES" if turn == 8 else "CALL VOTE: NO"

        g, events = self.calls_game(call_fn, on_no_decision="end")
        results = play(g)
        vrs = [e for e in events if e["type"] == "vote_result"]
        self.assertEqual(len(vrs), 1)
        self.assertEqual(vrs[0]["trigger"], "called+scheduled")
        self.assertEqual(vrs[0]["decision"], "no_decision")      # scheduled rules win
        self.assertEqual(g.outcome, "no_decision")
        self.assertEqual(len(results), 10)
        label = [e for e in events if e["type"] == "phase" and e["phase"] == "vote"][0]["label"]
        self.assertEqual(label, "Vote 1 (scheduled + called by players)")
        row = self.check_common(g, events)
        self.assertEqual((row["called_votes"], row["scheduled_votes"]), (1, 1))

    def test_called_vote_elimination_and_summary(self):
        holder = {}

        def call_fn(turn, me):
            if me == "Yellow":
                return None                                      # forgets the line -> counts as NO
            return "CALL VOTE: YES" if turn == 3 else "CALL VOTE: NO"

        def vote_fn(n, me, is_imp):
            return holder["imp"] if me != holder["imp"] else next(x for x in NAMES if x != me)

        g, events = self.calls_game(call_fn, vote_fn, reveal_vote_calls=True)
        holder["imp"] = g.imposter.name
        play(g)
        self.assertEqual(g.outcome, "trio_win")
        go = events[-1]
        self.assertEqual((go["rounds_survived"], go["rounds_completed"], go["turns_completed"]), (0, 0, 3))
        self.assertEqual(go["eliminated_by"], "called")
        row = self.check_common(g, events)
        self.assertTrue(row["player_vote_calls"])
        self.assertEqual(row["vote_call_threshold"], 3)
        self.assertEqual(row["eliminated_by"], "called")
        self.assertEqual(row["votes"][0]["trigger"], "called")
        self.assertEqual(row["votes"][0]["call_count"], 3)
        req = row["vote_requests_by_model"]
        total = {k: sum(m[k] for m in req.values()) for k in ("yes", "no", "missing")}
        self.assertEqual(total, {"yes": 3, "no": 6, "missing": 3})
        yellow = [e for e in events if e["type"] == "message" and e["player"] == "Yellow" and e["kind"] == "answer"]
        self.assertTrue(all(e["wants_vote"] is None for e in yellow))
        note_calls = [c for c in g.calls if c.get("player") == "Yellow" and c["purpose"] == "answer"]
        self.assertTrue(all(c["vote_call_note"] for c in note_calls))
        # reveal_vote_calls: requests are public, per turn
        vote_prompt = next(c for c in g.calls if c["purpose"] == "vote")["user_message"]
        self.assertIn("Vote requests: none", vote_prompt)
        self.assertIn("Vote requests: Red, Blue, Green", vote_prompt)
        self.assertIn("VOTE CALLS", g.players[0].system_prompt)
        self.assertIn("everyone is shown which players asked for a vote", g.players[0].system_prompt)
        md = Path(go["transcript_file"]).read_text()
        self.assertIn("a vote is called", md)

    def test_feature_off_unchanged(self):
        # Even a client that says YES every time cannot call a vote when the feature is off.
        g, events = self.calls_game(lambda turn, me: "CALL VOTE: YES", player_vote_calls=False)
        play(g)
        self.assertFalse([e for e in events if e["type"] == "vote_call_check"])
        self.assertEqual([e["trigger"] for e in events if e["type"] == "vote_result"], ["scheduled"])
        self.assertEqual([e["label"] for e in events if e["type"] == "phase" and e["phase"] == "vote"], ["Vote 1"])
        for e in events:
            if e["type"] == "message":
                self.assertNotIn("wants_vote", e)
        for p in g.players:
            self.assertNotIn("CALL VOTE", p.system_prompt)
            self.assertNotIn("VOTE CALLS", p.system_prompt)
            self.assertIn("players cannot call votes", p.system_prompt)
        for c in g.calls:
            self.assertNotIn("CALL VOTE", (c["user_message"] or "").split(prompts.TRANSCRIPT_END)[-1])
            self.assertNotIn("scheduled vote", c["user_message"] or "")
        row = self.check_common(g, events)
        self.assertFalse(row["player_vote_calls"])
        self.assertEqual(row["vote_requests_by_model"], {})

    def test_mock_client_emits_vote_calls(self):
        g, events = self.new_game(MockClient(seed=5, delay=0), player_vote_calls=True)
        play(g)
        answers = [e for e in events if e["type"] == "message" and e["kind"] == "answer"]
        self.assertTrue(answers)
        self.assertTrue(all("wants_vote" in a for a in answers))
        self.assertTrue(all("CALL VOTE" not in a["text"].upper() for a in answers))
        self.check_common(g, events)


class TestNameRule(GameTestBase):
    def test_violations_redacted(self):
        client = ScriptedClient(lambda n, me, imp: "Purple", intro_text="Hello, I am Claude, made by Anthropic.")
        g, events = self.new_game(client, name_rule="no_names", redact_violations=True)
        play(g)
        viol = [e for e in events if e["type"] == "rule_violation"]
        self.assertGreaterEqual(len(viol), 4)
        self.assertTrue(all(v["redacted"] for v in viol))
        intro = next(e for e in events if e["type"] == "message" and e["kind"] == "intro")
        self.assertEqual(intro["text"], "Hello, I am [REDACTED], made by [REDACTED].")
        self.assertEqual(intro["original_text"], "Hello, I am Claude, made by Anthropic.")
        # Other players only ever see the redacted text.
        for c in g.calls:
            self.assertNotIn("I am Claude", c["user_message"] or "")

    def test_violations_not_redacted_and_lie_allowed_ignored(self):
        client = ScriptedClient(lambda n, me, imp: "Purple", intro_text="I am Claude.")
        g, events = self.new_game(client, name_rule="no_names", redact_violations=False)
        play(g)
        viol = [e for e in events if e["type"] == "rule_violation"]
        self.assertTrue(viol and not any(v["redacted"] for v in viol))
        g2, events2 = self.new_game(ScriptedClient(lambda n, me, imp: "Purple", intro_text="I am Claude."))
        play(g2)
        self.assertFalse([e for e in events2 if e["type"] == "rule_violation"])


class TestVotingEdgeCases(GameTestBase):
    def test_invalid_votes_retry_then_abstain(self):
        # Every vote is invalid ("Purple") -> retry -> still invalid -> abstain.
        g, events = self.new_game(ScriptedClient(lambda n, me, imp: "Purple"))
        play(g)
        casts = [e for e in events if e["type"] == "vote_cast"]
        self.assertEqual(len(casts), 4)
        self.assertTrue(all(c["target"] is None and not c["valid"] and c["retried"] for c in casts))
        purposes = [c["purpose"] for c in g.calls]
        self.assertEqual(purposes.count("vote"), 4)
        self.assertEqual(purposes.count("vote_retry"), 4)
        self.assertEqual(g.outcome, "no_decision")
        # retry conversation ends with a user message
        retry = next(c for c in g.calls if c["purpose"] == "vote_retry")
        self.assertEqual(retry["extra_messages"][-1]["role"], "user")

    def test_self_vote_then_valid_retry(self):
        holder = {}
        seen = {}

        def vote(n, me, is_imp):
            seen[me] = seen.get(me, 0) + 1
            if seen[me] == 1:
                return me                       # self vote: invalid
            return holder["imp"] if me != holder["imp"] else next(x for x in NAMES if x != me)

        g, events = self.new_game(ScriptedClient(vote))
        holder["imp"] = g.imposter.name
        play(g)
        casts = [e for e in events if e["type"] == "vote_cast"]
        self.assertTrue(all(c["valid"] and c["retried"] for c in casts))
        self.assertEqual(g.outcome, "trio_win")


class TestErrors(GameTestBase):
    def test_client_error_ends_game_with_error(self):
        for fail_after in (0, 2, 9, 30):
            with self.subTest(fail_after=fail_after):
                g, events = self.new_game(FailingClient(fail_after))
                play(g)   # must not raise
                self.assertEqual(g.outcome, "error")
                errs = [e for e in events if e["type"] == "error"]
                self.assertEqual(len(errs), 1)
                self.assertTrue(errs[0]["fatal"])
                self.assertIn("simulated API outage", errs[0]["message"])
                self.assertIsNotNone(errs[0]["player"])
                self.assertEqual(events[-1]["type"], "game_over")
                self.assertEqual(events[-1]["outcome"], "error")
                self.assertEqual(self.summary_rows()[-1]["outcome"], "error")
                self.assertEqual(g.step(), {"boundary": True, "finished": True})

    def test_abort(self):
        g, events = self.new_game(MockClient(seed=1, delay=0))
        g.step()
        g.step()
        g.abort("user pressed stop")
        self.assertTrue(g.is_finished())
        self.assertEqual(events[-1]["outcome"], "aborted")
        self.assertEqual(events[-1]["reason"], "user pressed stop")
        g.abort("again")      # idempotent
        self.assertEqual(sum(e["type"] == "game_over" for e in events), 1)
        self.assertEqual(g.step()["finished"], True)

    def test_no_files_until_first_step(self):
        g, events = self.new_game(MockClient(seed=1, delay=0))
        self.assertEqual(events[0]["type"], "game_started")
        self.assertFalse(g.logger.json_path.exists())
        self.assertFalse(any(self.log_dir.rglob("*")))
        g.abort("never played")
        self.assertEqual(events[-1]["outcome"], "aborted")
        self.assertIsNone(events[-1]["log_file"])
        self.assertFalse(any(self.log_dir.rglob("*")))
        g2, _ = self.new_game(MockClient(seed=1, delay=0))
        g2.step()
        self.assertTrue(g2.logger.json_path.exists())
        self.assertTrue(g2.logger.md_path.exists())

    def test_on_event_exception_does_not_break_game(self):
        def bad(_e):
            raise ValueError("gui bug")
        with redirect_stdout(io.StringIO()), unittest.mock.patch("sys.stderr", io.StringIO()):
            g = Game(make_cfg(), MockClient(seed=2, delay=0), on_event=bad, log_dir=self.log_dir, mock=True, seed=1)
            play(g)
        self.assertIn(g.outcome, ("trio_win", "imposter_win", "no_decision"))


# =============================================================================
# Config + analyze
# =============================================================================

class TestConfig(unittest.TestCase):
    def test_repo_config_loads(self):
        cfg = load_config(ROOT / "config.toml", read_key=False)
        self.assertEqual(cfg.rules.player_names, NAMES)
        self.assertEqual(cfg.max_tokens_for("trio"), 16000)
        self.assertEqual(cfg.max_tokens_for("imposter"), 32000)
        self.assertIsNone(cfg.api.temperature)
        self.assertTrue(cfg.api.prompt_caching)
        self.assertNotIn("api_key", json.dumps(cfg.summary()))

    def test_friendly_errors(self):
        base = {"trio": {"model": "a/b"}, "imposter": {"model": "c/d"}}
        with self.assertRaises(ConfigError):
            config_from_dict({**base, "rules": {"name_rule": "whatever"}})
        with self.assertRaises(ConfigError):
            config_from_dict({**base, "rules": {"vote_threshold": 7}})
        with self.assertRaises(ConfigError):
            config_from_dict({**base, "rules": {"player_names": ["A", "B", "C"]}})
        with self.assertRaises(ConfigError):
            config_from_dict({"trio": {"model": "a/b"}})
        with self.assertRaises(ConfigError):
            config_from_dict({**base, "api": {"temperature": "hot"}})
        cfg = config_from_dict(base)
        self.assertEqual(cfg.trio.display_name, "a/b")


class TestAnalyze(GameTestBase):
    def test_analyze_output(self):
        import analyze
        out = io.StringIO()
        with redirect_stdout(out):
            analyze.main(["--log-dir", str(self.log_dir)])
        self.assertIn("No finished games", out.getvalue())
        for seed in range(3):
            g, _ = self.new_game(MockClient(seed=seed, delay=0), seed=seed)
            play(g)
        out = io.StringIO()
        csv_path = self.log_dir / "out.csv"
        with redirect_stdout(out):
            analyze.main(["--log-dir", str(self.log_dir), "--include-mock", "--csv", str(csv_path)])
        text = out.getvalue()
        self.assertIn("Games: 3", text)
        self.assertIn("random voting baseline", text)
        self.assertEqual(len(csv_path.read_text().strip().splitlines()), 4)
        out = io.StringIO()
        with redirect_stdout(out):
            analyze.main(["--log-dir", str(self.log_dir)])
        self.assertIn("--include-mock", out.getvalue())


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
