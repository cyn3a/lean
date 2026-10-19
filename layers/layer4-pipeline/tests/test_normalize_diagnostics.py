from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import normalize_diagnostics as nd
from normalize_diagnostics import (
    Drain,
    MVAR_TOKEN,
    PATH_TOKEN,
    PAYLOAD_TOKEN,
    UNIQ_TOKEN,
    normalize_message,
    split_message,
)

# Messages copied verbatim from parsed-errors/ (see the file names in comments).

# 0stellensatz__MassFormula__pair-0000__...__error-0001.json
FUNCTION_EXPECTED = (
    "Function expected at\n"
    "  Ideal.ramificationIdx ?m.25 ↥𝓂[K]\n"
    "but this term has type\n"
    "  ℕ\n"
    "\n"
    "Note: Expected a function because this term is being applied to the argument\n"
    "  (maximalIdealAbove L)"
)

# AntoineChambert-Loir ... error-0001.json
TYPE_MISMATCH = (
    "type mismatch\n"
    "  n.succ\n"
    "has type\n"
    "  ℕ : Type\n"
    "but is expected to have type\n"
    "  PartENat : Sort ?u.208217"
)

# 32805433__Adian-Rabin ... error-0003.json (hypothesis `hphi` wrapped for the test)
UNSOLVED_GOALS = (
    "unsolved goals\n"
    "case left\n"
    "G : Type u_1\n"
    "inst✝ : Group G\n"
    "A B : Subgroup G\n"
    "hphi :\n"
    "    ∀ (a : ↥A), ↑a ∈ H ↔ ↑(phi a) ∈ H\n"
    "a : ↥(restrictedA H)\n"
    "⊢ ↑(phi.symm ⟨↑(phi ⟨↑↑a, ⋯⟩), ⋯⟩) = ↑↑a"
)

# AlexKontorovich ... error-0007.json
FAILED_TO_SYNTHESIZE = (
    "failed to synthesize\n"
    "  OfNat { x // x ∈ n.primeFactors } 1\n"
    "numerals are polymorphic in Lean, but the numeral `1` cannot be used in a context where the expected type is\n"
    "  { x // x ∈ n.primeFactors }\n"
    "due to the absence of the instance above\n"
    "Additional diagnostic information may be available using the `set_option diagnostics true` command."
)

# Alektronnik ... error-0002.json
HINT_WITH_BULLETS = (
    "Invalid field notation: Type of\n"
    "  row\n"
    "is not known; cannot resolve field `get?`\n"
    "\n"
    "Hint: Consider replacing the field projection with a call to one of the following:\n"
    "  • `Dynamic.get?`\n"
    "  • `FloatArray.get?`"
)

# cruhland ... error-0001.json
DEPRECATED = (
    "class ... :=' has been deprecated in favor of 'class ... where'.\n"
    "note: this linter can be disabled with `set_option linter.deprecated false`"
)


class NormalizeMessageTests(unittest.TestCase):
    def test_windows_path(self) -> None:
        msg = (
            "object file 'c:\\Users\\Cynthia\\.elan\\toolchains\\leanprover--lean4---v4.11.0-rc1"
            "\\lib\\lean\\Std\\Lean\\Position.olean' of module Std.Lean.Position does not exist"
        )
        self.assertEqual(
            normalize_message(msg),
            f"object file '{PATH_TOKEN}' of module Std.Lean.Position does not exist",
        )
        self.assertEqual(normalize_message("at C:/Users/me/Foo.lean:3:1"), f"at {PATH_TOKEN}")

    def test_posix_and_lake_paths(self) -> None:
        self.assertEqual(
            normalize_message("error in /home/user/proj/Foo/Bar.lean here"),
            f"error in {PATH_TOKEN} here",
        )
        self.assertEqual(
            normalize_message("see /tmp/build/x.olean"), f"see {PATH_TOKEN}"
        )
        self.assertEqual(
            normalize_message("file ./.lake/packages/mathlib/Mathlib/Foo.lean:1:0"),
            f"file {PATH_TOKEN}",
        )
        self.assertEqual(
            normalize_message("file .lake\\packages\\batteries\\Batteries.lean"),
            f"file {PATH_TOKEN}",
        )

    def test_metavariables(self) -> None:
        self.assertEqual(
            normalize_message("Ideal ?m.11 and Sort ?u.208217 and ?_uniq.77"),
            f"Ideal {MVAR_TOKEN} and Sort {MVAR_TOKEN} and {MVAR_TOKEN}",
        )
        # projections off a metavariable keep the projection
        self.assertEqual(normalize_message("?m.5.toFun"), f"{MVAR_TOKEN}.toFun")

    def test_uniq_ids(self) -> None:
        self.assertEqual(normalize_message("fvar _uniq.1234 here"), f"fvar {UNIQ_TOKEN} here")

    def test_hygienic_scopes(self) -> None:
        self.assertEqual(
            normalize_message("unknown identifier 'x._@.Mathlib.Foo.Bar._hyg.123'"),
            "unknown identifier 'x'",
        )
        self.assertEqual(normalize_message("y._@.Mod._hyg.1.2 z._hyg.9"), "y z")

    def test_inaccessible_name_markers(self) -> None:
        self.assertEqual(
            normalize_message("inst✝ : Group G\nx✝¹ x✝ : ℝ\n_auto✝¹ →"),
            "inst : Group G\nx x : ℝ\n_auto →",
        )

    def test_line_endings(self) -> None:
        self.assertEqual(normalize_message("a\r\nb\rc"), "a\nb\nc")

    def test_is_conservative(self) -> None:
        untouched = [
            "maximum number of heartbeats (200000) has been reached",
            "G : Type u_1",
            "(stage₁F ?F ?E ?i).length",
            "⊢ (↑d)⁻¹ * C₃ / 2 ≤ a / b",
            "unknown identifier 'inv_antitoneOn_Icc'",
            "Nat.succ_le_of_lt h₂",
        ]
        for text in untouched:
            with self.subTest(text=text):
                self.assertEqual(normalize_message(text), text)


class SplitMessageTests(unittest.TestCase):
    def test_function_expected_with_note(self) -> None:
        parts = split_message(normalize_message(FUNCTION_EXPECTED))
        self.assertEqual(
            parts["main_text"],
            f"Function expected at {PAYLOAD_TOKEN} but this term has type {PAYLOAD_TOKEN}",
        )
        self.assertEqual(parts["payloads"], [f"  Ideal.ramificationIdx {MVAR_TOKEN} ↥𝓂[K]", "  ℕ"])
        self.assertEqual(
            parts["notes"],
            [
                "Note: Expected a function because this term is being applied to the argument\n"
                "  (maximalIdealAbove L)"
            ],
        )

    def test_type_mismatch(self) -> None:
        parts = split_message(normalize_message(TYPE_MISMATCH))
        self.assertEqual(
            parts["main_text"],
            f"type mismatch {PAYLOAD_TOKEN} has type {PAYLOAD_TOKEN} "
            f"but is expected to have type {PAYLOAD_TOKEN}",
        )
        self.assertEqual(
            parts["payloads"], ["  n.succ", "  ℕ : Type", f"  PartENat : Sort {MVAR_TOKEN}"]
        )
        self.assertEqual(parts["notes"], [])

    def test_unsolved_goals_context_is_payload(self) -> None:
        parts = split_message(normalize_message(UNSOLVED_GOALS))
        self.assertEqual(parts["main_text"], f"unsolved goals {PAYLOAD_TOKEN}")
        self.assertEqual(len(parts["payloads"]), 1)
        payload = parts["payloads"][0]
        self.assertTrue(payload.startswith("case left\nG : Type u_1\ninst : Group G\n"))
        self.assertIn("hphi :\n    ∀ (a : ↥A)", payload)
        self.assertTrue(payload.endswith("= ↑↑a"))
        self.assertEqual(parts["notes"], [])

    def test_failed_to_synthesize_trailing_diagnostic_is_note(self) -> None:
        parts = split_message(normalize_message(FAILED_TO_SYNTHESIZE))
        self.assertEqual(
            parts["main_text"],
            f"failed to synthesize {PAYLOAD_TOKEN} numerals are polymorphic in Lean, but the "
            f"numeral `1` cannot be used in a context where the expected type is {PAYLOAD_TOKEN} "
            "due to the absence of the instance above",
        )
        self.assertEqual(len(parts["payloads"]), 2)
        self.assertEqual(
            parts["notes"],
            ["Additional diagnostic information may be available using the "
             "`set_option diagnostics true` command."],
        )

    def test_hint_absorbs_indented_bullets(self) -> None:
        parts = split_message(normalize_message(HINT_WITH_BULLETS))
        self.assertEqual(
            parts["main_text"],
            f"Invalid field notation: Type of {PAYLOAD_TOKEN} is not known; cannot resolve field `get?`",
        )
        self.assertEqual(parts["payloads"], ["  row"])
        self.assertEqual(len(parts["notes"]), 1)
        self.assertTrue(parts["notes"][0].startswith("Hint: Consider replacing"))
        self.assertIn("  • `FloatArray.get?`", parts["notes"][0])

    def test_lowercase_linter_note(self) -> None:
        parts = split_message(DEPRECATED)
        self.assertEqual(
            parts["main_text"], "class ... :=' has been deprecated in favor of 'class ... where'."
        )
        self.assertEqual(parts["notes"], [DEPRECATED.split("\n")[1]])

    def test_blank_separated_goal_blocks_collapse_to_one_payload_token(self) -> None:
        msg = "unsolved goals\ncase pos\nn : ℕ\n⊢ n = n\n\ncase neg\nn : ℕ\n⊢ n ≠ 0"
        parts = split_message(msg)
        self.assertEqual(parts["main_text"], f"unsolved goals {PAYLOAD_TOKEN}")
        self.assertEqual(len(parts["payloads"]), 2)

    def test_sentence_after_blank_line_is_note(self) -> None:
        msg = "simp made no progress\n\nTry `simp?` to see which lemmas apply."
        parts = split_message(msg)
        self.assertEqual(parts["main_text"], "simp made no progress")
        self.assertEqual(parts["notes"], ["Try `simp?` to see which lemmas apply."])

    def test_empty_message(self) -> None:
        self.assertEqual(split_message(""), {"main_text": "", "payloads": [], "notes": []})


class DrainTests(unittest.TestCase):
    def test_identifier_only_differences_share_a_template(self) -> None:
        drain = Drain()
        g1 = drain.add("unknown identifier 'inv_antitoneOn_Icc'", 0)
        g2 = drain.add("unknown identifier 'finrank'", 1)
        g3 = drain.add("maximum number of heartbeats (200000) has been reached", 2)
        g4 = drain.add("maximum number of heartbeats (400000) has been reached", 3)
        self.assertIs(g1, g2)
        self.assertEqual(" ".join(g1.template), "unknown identifier <*>")
        self.assertIs(g3, g4)
        self.assertEqual(
            " ".join(g3.template), "maximum number of heartbeats <*> has been reached"
        )
        self.assertEqual(len(drain.groups), 2)

    def test_structurally_different_messages_do_not_share(self) -> None:
        drain = Drain()
        a = drain.add("unknown identifier 'foo'", 0)
        b = drain.add("unknown constant 'foo'", 1)
        c = drain.add("simp made no progress", 2)
        d = drain.add(f"type mismatch {PAYLOAD_TOKEN} has type {PAYLOAD_TOKEN} "
                      f"but is expected to have type {PAYLOAD_TOKEN}", 3)
        e = drain.add(f"unsolved goals {PAYLOAD_TOKEN}", 4)
        self.assertEqual(len({id(g) for g in (a, b, c, d, e)}), 5)
        # a and b differ in one of three tokens (sim 0.67 >= 0.4) but are split
        # by the token-prefix layer of the tree (depth 4 -> first two tokens).
        self.assertEqual(" ".join(a.template), "unknown identifier 'foo'")
        self.assertEqual(" ".join(b.template), "unknown constant 'foo'")

    def test_threshold_controls_merging(self) -> None:
        strict = Drain(sim_threshold=0.9)
        s1 = strict.add("foo bar 'a' x y", 0)
        s2 = strict.add("foo bar 'b' x z", 1)
        self.assertIsNot(s1, s2)
        loose = Drain(sim_threshold=0.5)
        l1 = loose.add("foo bar 'a' x y", 0)
        l2 = loose.add("foo bar 'b' x z", 1)
        self.assertIs(l1, l2)
        self.assertEqual(" ".join(l1.template), "foo bar <*> x <*>")

    def test_wildcard_tokens(self) -> None:
        self.assertTrue(nd.is_wildcard_token("?u.1"))
        self.assertTrue(nd.is_wildcard_token("`lemma`"))
        self.assertTrue(nd.is_wildcard_token("⊢"))
        self.assertTrue(nd.is_wildcard_token("⟨...⟩,"))
        self.assertFalse(nd.is_wildcard_token("unknown"))
        self.assertFalse(nd.is_wildcard_token("α"))
        self.assertFalse(nd.is_wildcard_token("ℕ"))

    def test_invalid_parameters(self) -> None:
        with self.assertRaises(ValueError):
            Drain(depth=2)
        with self.assertRaises(ValueError):
            Drain(sim_threshold=1.5)


def _row(**overrides: object) -> dict:
    base = {
        "file": "Foo/Bar.lean",
        "line": 1,
        "column": 2,
        "message": "unknown identifier 'x'",
        "source_found": True,
        "error_line": "  x",
        "code": "x",
        "context_start_line": 1,
        "context_end_line": 1,
        "repo": "o/r",
        "old_version": "leanprover/lean4:v4.1.0",
        "new_version": "leanprover/lean4:v4.2.0",
        "old_hash": "a" * 40,
        "new_hash": "b" * 40,
        "pair_index": 0,
        "pair_id": "p0",
    }
    base.update(overrides)
    return base


class PipelineTests(unittest.TestCase):
    MESSAGES = [
        FUNCTION_EXPECTED,
        TYPE_MISMATCH,
        UNSOLVED_GOALS,
        FAILED_TO_SYNTHESIZE,
        HINT_WITH_BULLETS,
        DEPRECATED,
        "unknown identifier 'inv_antitoneOn_Icc'",
        "unknown identifier 'inv_sub_antitoneOn_Icc'",
        "unknown constant 'Nat.coprime'",
        "simp made no progress",
    ]

    def _write_corpus(self, root: Path) -> None:
        # Write in reverse order so sorted() must do the work.
        for i, msg in reversed(list(enumerate(self.MESSAGES))):
            row = _row(message=msg, pair_id=f"p{i % 3}", repo=f"o/r{i % 2}",
                       new_version=f"leanprover/lean4:v4.{2 + i % 2}.0")
            (root / f"repo__pair-{i % 3}__error-{i:04d}.json").write_text(
                json.dumps(row, ensure_ascii=False), encoding="utf-8"
            )

    def test_cli_outputs_and_determinism(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            errors = root / "parsed-errors"
            errors.mkdir()
            self._write_corpus(errors)
            out1, out2 = root / "out1", root / "out2"
            for out in (out1, out2):
                with contextlib.redirect_stdout(io.StringIO()) as captured:
                    rc = nd.main(["--errors-root", str(errors), "--out-dir", str(out)])
                self.assertEqual(rc, 0)
                self.assertIn("diagnostics: 10", captured.getvalue())
            for name in ("normalized-diagnostics.jsonl", "templates.jsonl", "summary.json"):
                with self.subTest(name=name):
                    self.assertEqual((out1 / name).read_bytes(), (out2 / name).read_bytes())

            rows = [json.loads(l) for l in (out1 / "normalized-diagnostics.jsonl")
                    .read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), len(self.MESSAGES))
            self.assertEqual([r["source_file"] for r in rows], sorted(r["source_file"] for r in rows))
            for key in nd.ORIGINAL_FIELDS + ("source_file", "normalized_message", "main_text",
                                              "payloads", "notes", "template_id", "template"):
                self.assertIn(key, rows[0])
            by_msg = {r["message"]: r for r in rows}
            self.assertEqual(
                by_msg["unknown identifier 'inv_antitoneOn_Icc'"]["template_id"],
                by_msg["unknown identifier 'inv_sub_antitoneOn_Icc'"]["template_id"],
            )
            self.assertNotEqual(
                by_msg["unknown identifier 'inv_antitoneOn_Icc'"]["template_id"],
                by_msg["unknown constant 'Nat.coprime'"]["template_id"],
            )

            templates = [json.loads(l) for l in (out1 / "templates.jsonl")
                         .read_text(encoding="utf-8").splitlines()]
            self.assertEqual(sum(t["count"] for t in templates), len(self.MESSAGES))
            ids = [t["template_id"] for t in templates]
            self.assertEqual(ids, sorted(ids))
            self.assertEqual(templates[0]["template"], "unknown identifier <*>")
            self.assertEqual(templates[0]["count"], 2)
            self.assertEqual(templates[0]["n_pairs"], 2)
            for t in templates:
                self.assertIn(t["example_message"], self.MESSAGES)
                self.assertTrue(t["example_file"].endswith(".json"))

            summary = json.loads((out1 / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["counts"]["diagnostics"], len(self.MESSAGES))
            self.assertEqual(summary["counts"]["templates"], len(templates))
            self.assertEqual(summary["counts"]["singletons"],
                             sum(1 for t in templates if t["count"] == 1))
            self.assertEqual(len(summary["top_templates"]), min(30, len(templates)))
            self.assertEqual(
                sum(v["diagnostics"] for v in summary["per_transition"].values()),
                len(self.MESSAGES),
            )
            self.assertEqual(summary["parameters"], {"depth": 4, "sim_threshold": 0.4,
                                                     "max_children": 100})

    def test_process_is_deterministic_in_memory(self) -> None:
        rows = [_row(message=m, pair_id=f"p{i}") for i, m in enumerate(self.MESSAGES)]
        first = nd.process(rows)
        second = nd.process([dict(r) for r in rows])
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
