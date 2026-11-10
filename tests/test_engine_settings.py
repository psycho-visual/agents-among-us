"""Tests for the settings API in imposter_game/config.py (used by the GUI settings drawer).

All writes go to a temporary COPY of the real config.toml; the real file is never written.
"""

from __future__ import annotations

import dataclasses
import difflib
import hashlib
import shutil
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from imposter_game import config as C  # noqa: E402
from imposter_game import toml_edit  # noqa: E402

REAL = ROOT / "config.toml"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class SettingsBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.real_hash = _sha(REAL)

    @classmethod
    def tearDownClass(cls):
        assert _sha(REAL) == cls.real_hash, "the real config.toml was modified by a test!"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.toml"
        shutil.copyfile(REAL, self.path)
        self.original = self.path.read_text(encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def values(self):
        r = C.read_settings(self.path)
        self.assertEqual(r["errors"], [])
        return r["values"]

    def save(self, **changes):
        v = self.values()
        v.update({k.replace("__", "."): val for k, val in changes.items()})
        return C.save_settings(self.path, v)

    def changed_lines(self):
        a = self.original.splitlines()
        b = self.path.read_text(encoding="utf-8").splitlines()
        return [line for line in difflib.unified_diff(a, b, lineterm="", n=0)
                if line[:1] in "+-" and not line.startswith(("+++", "---"))]


class TestRoundTrip(SettingsBase):
    def test_unchanged_values_byte_identical(self):
        res = C.save_settings(self.path, self.values())
        self.assertTrue(res["ok"])
        self.assertEqual(res["changed"], [])
        self.assertIsNone(res["backup"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.original)
        self.assertFalse(self.path.with_name("config.toml.bak").exists())

    def test_change_one_value_changes_one_line(self):
        res = self.save(rules__vote_threshold=2)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["changed"], ["rules.vote_threshold"])
        self.assertEqual(self.changed_lines(), ["-vote_threshold = 3", "+vote_threshold = 2"])
        self.assertEqual(C.load_config(self.path, read_key=False).rules.vote_threshold, 2)
        backup = Path(res["backup"])
        self.assertEqual(backup.name, "config.toml.bak")
        self.assertEqual(backup.read_text(encoding="utf-8"), self.original)

    def test_comments_preserved(self):
        self.save(rules__vote_threshold=4, trio__model="meta-llama/llama-4-scout", rules__no_imposter_chance=0.25,
                  paraphrase__enabled=True, api__timeout_seconds=120)
        new = self.path.read_text(encoding="utf-8")
        old_comments = [x for x in self.original.splitlines() if x.lstrip().startswith("#")]
        new_comments = [x for x in new.splitlines() if x.lstrip().startswith("#")]
        self.assertEqual(old_comments, new_comments)
        self.assertEqual(len(self.changed_lines()), 10)
        self.assertIn("timeout_seconds = 120.0", new)
        self.assertIn('model = "meta-llama/llama-4-scout"', new)

    def test_trailing_comment_and_indentation_kept(self):
        text = self.original.replace("vote_threshold = 3\n", "  vote_threshold   =  3   # three of four\n")
        self.path.write_text(text, encoding="utf-8")
        self.original = text
        self.assertTrue(self.save(rules__vote_threshold=2)["ok"])
        self.assertEqual(self.changed_lines(), ["-  vote_threshold   =  3   # three of four",
                                                "+  vote_threshold   =  2   # three of four"])

    def test_multiline_banned_names(self):
        names = ["claude", "gpt", 'odd "quoted" word', "back\\slash", "ünïcödé ✓"]
        self.assertTrue(self.save(rules__banned_names=names)["ok"])
        self.assertEqual(self.values()["rules.banned_names"], names)
        new = self.path.read_text(encoding="utf-8")
        start = new.index("banned_names = [")
        self.assertIn("\n]", new[start:start + 300])                 # still a multi-line array
        # Everything outside the array is unchanged.
        before, after = self.original.split("banned_names = [", 1)
        self.assertTrue(new.startswith(before))
        self.assertTrue(new.endswith(after.split("]\n", 1)[1]))
        self.assertEqual(tomllib.loads(new)["rules"]["banned_names"], names)

    def test_missing_key_inserted_at_end_of_section(self):
        text = self.original.replace("reveal_vote_calls = false\n", "")
        self.path.write_text(text, encoding="utf-8")
        self.assertFalse(self.values()["rules.reveal_vote_calls"])
        self.assertTrue(self.save(rules__reveal_vote_calls=True)["ok"])
        new = self.path.read_text(encoding="utf-8")
        rules_part = new.split("[rules]", 1)[1].split("\n[", 1)[0]
        self.assertIn("reveal_vote_calls = true", rules_part)
        self.assertTrue(C.load_config(self.path, read_key=False).rules.reveal_vote_calls)
        # A key that's missing and stays at its default is not added.
        self.path.write_text(text, encoding="utf-8")
        self.assertTrue(C.save_settings(self.path, self.values())["ok"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), text)

    def test_missing_section_created(self):
        start = self.original.index("[paraphrase]")
        end = self.original.index("max_output_tokens = 1000\n", start) + len("max_output_tokens = 1000\n")
        text = self.original[:start] + self.original[end:]
        self.path.write_text(text, encoding="utf-8")
        self.assertTrue(self.save(paraphrase__enabled=True)["ok"])
        data = tomllib.loads(self.path.read_text(encoding="utf-8"))
        self.assertTrue(data["paraphrase"]["enabled"])
        self.assertTrue(C.load_config(self.path, read_key=False).paraphrase.enabled)

    def test_string_escaping(self):
        name = 'He said "hi" \\ C:\\path\tünïcödé ✓ 🙂'
        self.assertTrue(self.save(trio__display_name=name)["ok"])
        self.assertEqual(C.load_config(self.path, read_key=False).trio.display_name, name)
        self.assertEqual(self.values()["trio.display_name"], name)

    def test_nullable_values(self):
        self.assertTrue(self.save(trio__max_output_tokens=None, api__temperature=0.5)["ok"])
        cfg = C.load_config(self.path, read_key=False)
        self.assertIsNone(cfg.trio.max_output_tokens)
        self.assertEqual(cfg.api.temperature, 0.5)
        self.assertIsNone(self.values()["trio.max_output_tokens"])
        self.assertTrue(self.save(api__temperature=None, trio__max_output_tokens=8000)["ok"])
        self.assertIn('temperature = ""', self.path.read_text(encoding="utf-8"))
        self.assertEqual(C.load_config(self.path, read_key=False).trio.max_output_tokens, 8000)

    def test_invalid_value_writes_nothing(self):
        for bad in ({"rules.vote_threshold": 9}, {"rules.vote_threshold": "three"}, {"trio.model": ""},
                    {"rules.player_names": ["A", "B"]}, {"rules.name_rule": "whatever"},
                    {"rules.no_imposter_chance": 1.5}, {"bogus.key": 1}):
            with self.subTest(bad=bad):
                v = self.values()
                v.update(bad)
                res = C.save_settings(self.path, v)
                self.assertFalse(res["ok"])
                self.assertIn(next(iter(bad)), res["errors"])
                self.assertEqual(self.path.read_text(encoding="utf-8"), self.original)
                self.assertFalse(self.path.with_name("config.toml.bak").exists())

    def test_crlf_file(self):
        text = self.original.replace("\n", "\r\n")
        self.path.write_bytes(text.encode("utf-8"))
        self.assertTrue(self.save(rules__vote_threshold=2, rules__banned_names=["a", "b"])["ok"])
        new = self.path.read_bytes().decode("utf-8")
        self.assertNotIn("\n", new.replace("\r\n", ""))
        self.assertEqual(C.load_config(self.path, read_key=False).rules.vote_threshold, 2)

    def test_partial_values_keep_the_rest(self):
        res = C.save_settings(self.path, {"rules.vote_threshold": 2})
        self.assertTrue(res["ok"])
        self.assertEqual(len(self.changed_lines()), 2)


class TestSchema(unittest.TestCase):
    SECTIONS = {"trio": C.ModelConfig, "imposter": C.ModelConfig, "rules": C.RulesConfig,
                "paraphrase": C.ParaphraseConfig, "api": C.ApiConfig, "batch": C.BatchConfig}

    def test_schema_matches_config_keys(self):
        ids = [e["id"] for e in C.settings_schema()]
        self.assertEqual(len(ids), len(set(ids)))
        expected = {f"{sec}.{f.name}" for sec, cls in self.SECTIONS.items() for f in dataclasses.fields(cls)}
        self.assertEqual(set(ids), expected)

    def test_entries_well_formed(self):
        schema = C.settings_schema()
        by_id = {e["id"]: e for e in schema}
        groups = {"Players & models", "Game flow", "Voting", "Fairness", "Text limits", "Paraphrase", "API", "Batch"}
        for e in schema:
            self.assertIn(e["group"], groups)
            self.assertIn(e["type"], ("string", "int", "float", "bool", "enum", "string_list", "model"))
            self.assertTrue(e["label"] and e["help"])
            if e["type"] == "enum":
                self.assertIn(e["default"], [c["value"] for c in e["choices"]])
            if e["visible_if"]:
                target = by_id[e["visible_if"]["id"]]
                if target["type"] == "enum":
                    self.assertIn(e["visible_if"]["equals"], [c["value"] for c in target["choices"]])
                else:
                    self.assertIsInstance(e["visible_if"]["equals"], bool)
        efforts = [c["value"] for c in by_id["trio.reasoning_effort"]["choices"]]
        self.assertEqual(efforts, ["", "none", "low", "medium", "high", "xhigh", "max"])
        self.assertTrue(by_id["imposter.max_output_tokens"]["nullable"])
        # Schema defaults pass validation.
        self.assertEqual(C.validate_settings({})["errors"], {})


class TestEverySettingIsUnderstood(SettingsBase):
    def alternative(self, e, current):
        t = e["type"]
        if t == "bool":
            return not current
        if t == "enum":
            return next(c["value"] for c in e["choices"] if c["value"] != current)
        if t == "int":
            return (current or 1000) + 1
        if t == "float":
            return 0.5 if e["id"] != "api.timeout_seconds" else 123.5
        if t == "string_list":
            return ["Ann", "Bob", "Cy", "Di"] if e["key"] == "player_names" else ["foo", "bar"]
        if e["id"] == "api.base_url":
            return "https://example.com/api/v1"
        return "test/model-x" if t == "model" else "Some Name"

    def attr(self, cfg, e):
        return getattr(getattr(cfg, e["section"]), e["key"])

    def test_each_setting_reaches_load_config(self):
        for e in C.settings_schema():
            with self.subTest(id=e["id"]):
                shutil.copyfile(REAL, self.path)
                current = self.values()[e["id"]]
                new = self.alternative(e, current)
                res = C.save_settings(self.path, {e["id"]: new})
                self.assertTrue(res["ok"], res)
                self.assertEqual(res["changed"], [e["id"]])
                got = self.attr(C.load_config(self.path, read_key=False), e)
                if e["type"] == "enum" and new == "":
                    new = None
                self.assertEqual(got, new)


class TestValidation(unittest.TestCase):
    def test_errors_per_field(self):
        res = C.validate_settings({"rules.vote_threshold": 9, "rules.on_no_decision": "continue",
                                   "rules.max_rounds": 1, "api.timeout_seconds": "abc", "trio.model": ""})
        self.assertEqual(set(res["errors"]), {"rules.vote_threshold", "rules.max_rounds", "api.timeout_seconds",
                                              "trio.model"})

    def test_same_checks_as_load_config(self):
        for key, bad in (("rules.vote_call_threshold", 0), ("rules.intro_word_limit", 2),
                         ("api.max_retries", -1), ("rules.player_names", ["A", "A", "B", "C"])):
            with self.subTest(key=key):
                self.assertIn(key, C.validate_settings({key: bad})["errors"])
                sec, k = key.split(".")
                data = {"trio": {"model": "a/b"}, "imposter": {"model": "c/d"}, sec: {k: bad}}
                with self.assertRaises(C.ConfigError):
                    C.config_from_dict(data)

    def test_coercion_from_json(self):
        res = C.validate_settings({"rules.vote_threshold": 2.0, "api.timeout_seconds": "90",
                                   "rules.banned_names": "a, b\nc", "trio.max_output_tokens": ""})
        self.assertEqual(res["errors"], {})
        self.assertEqual(res["values"]["rules.banned_names"], ["a", "b", "c"])
        self.assertIsNone(res["values"]["trio.max_output_tokens"])
        self.assertIn("rules.shuffle_seats", C.validate_settings({"rules.shuffle_seats": "maybe"})["errors"])

    def test_warnings(self):
        w = C.validate_settings({"imposter.model": "anthropic/claude-haiku-5.5"})["warnings"]
        self.assertIn("imposter.model", w)
        w = C.validate_settings({"paraphrase.enabled": True, "paraphrase.model": "openai/gpt-tiny"})["warnings"]
        self.assertIn("paraphrase.model", w)
        w = C.validate_settings({"imposter.max_output_tokens": 4000, "imposter.reasoning_effort": "max"})["warnings"]
        self.assertIn("imposter.max_output_tokens", w)
        self.assertEqual(C.validate_settings({})["warnings"], {})

    def test_read_settings_missing_or_broken(self):
        with tempfile.TemporaryDirectory() as d:
            r = C.read_settings(Path(d) / "nope.toml")
            self.assertTrue(r["errors"])
            self.assertEqual(r["values"]["rules.vote_threshold"], 3)
            bad = Path(d) / "bad.toml"
            bad.write_text("[rules\nvote_threshold = ", encoding="utf-8")
            self.assertTrue(C.read_settings(bad)["errors"])
            res = C.save_settings(bad, {"rules.vote_threshold": 2})
            self.assertFalse(res["ok"])
            self.assertEqual(bad.read_text(encoding="utf-8"), "[rules\nvote_threshold = ")


class TestTomlEdit(unittest.TestCase):
    def test_format_value(self):
        f = toml_edit.format_value
        self.assertEqual(f(True), "true")
        self.assertEqual(f(300.0), "300.0")
        self.assertEqual(f(0.25), "0.25")
        self.assertEqual(f(3), "3")
        self.assertEqual(f(["a", "b"]), '["a", "b"]')
        for s in ['q"uote', "back\\slash", "tab\tnew\nline", "ctrl\x01", "ünï ✓"]:
            self.assertEqual(tomllib.loads("v = " + f(s))["v"], s)
        with self.assertRaises(ValueError):
            f(float("nan"))

    def test_edit_keeps_other_text(self):
        text = '# top\n[a]\nx = 1 # c\ny = "s # not a comment"\nz = [\n  1, # one\n  2,\n] # tail\n\n[b]\nq = 2\n'
        out = toml_edit.set_values(text, {("a", "x"): 5, ("a", "y"): "t", ("a", "z"): [3], ("b", "new"): True,
                                          ("c", "k"): "v"})
        self.assertEqual(tomllib.loads(out), {"a": {"x": 5, "y": "t", "z": [3]}, "b": {"q": 2, "new": True},
                                              "c": {"k": "v"}})
        self.assertIn("x = 5 # c\n", out)
        self.assertIn("] # tail\n", out)
        self.assertTrue(out.startswith("# top\n[a]\n"))
        self.assertEqual(toml_edit.set_values(text, {("a", "x"): 1, ("a", "z"): [1, 2]}), text)


if __name__ == "__main__":
    unittest.main()
