"""Tests for layer4. Run: python -m pytest tests -q  (or python tests/test_layer4.py)"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from layer4 import discover, emit, leanlex, mine, rules, taxonomy  # noqa: E402
from layer4.diffparse import parse_diff  # noqa: E402
from layer4.gitio import Git  # noqa: E402
from layer4.taxonomy import RepairLabel  # noqa: E402


def _hunk(old: str, new: str, path="Mathlib/T.lean"):
    minus = "".join(f"-{l}\n" for l in old.split("\n")) if old else ""
    plus = "".join(f"+{l}\n" for l in new.split("\n")) if new else ""
    diff = (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1,3 +1,3 @@\n ctx\n{minus}{plus} ctx2\n")
    return parse_diff(diff)[0].hunks[0]


class TestDiffParse(unittest.TestCase):
    def test_roundtrip_sides(self):
        h = _hunk("theorem a : p := by simp", "theorem a : p := by simpa")
        self.assertEqual(h.old_lines, ["theorem a : p := by simp"])
        self.assertEqual(h.new_lines, ["theorem a : p := by simpa"])
        self.assertIn("ctx", h.window("new"))

    def test_independent_edits_split_by_default(self):
        diff = ("diff --git a/A.lean b/A.lean\n--- a/A.lean\n+++ b/A.lean\n"
                "@@ -1,6 +1,6 @@\n ctx\n-old1\n+new1\n keep\n-old2\n+new2\n tail\n")
        hs = parse_diff(diff)[0].hunks
        self.assertEqual(len(hs), 2)          # gap=0: two unrelated edits
        self.assertEqual(hs[0].old_lines, ["old1"])
        self.assertEqual(hs[1].new_lines, ["new2"])

    def test_gap_can_rejoin_edits(self):
        diff = ("diff --git a/A.lean b/A.lean\n--- a/A.lean\n+++ b/A.lean\n"
                "@@ -1,6 +1,6 @@\n ctx\n-old1\n+new1\n keep\n-old2\n+new2\n tail\n")
        h = parse_diff(diff, gap=1)[0].hunks[0]
        self.assertEqual(h.old_lines, ["old1", "old2"])
        self.assertIn("keep", h.window("old"))   # interior context survives
        self.assertIn("keep", h.window("new"))

    def test_window_excludes_other_sides_context(self):
        diff = ("diff --git a/A.lean b/A.lean\n--- a/A.lean\n+++ b/A.lean\n"
                "@@ -1,5 +1,5 @@\n-gone\n+added\n ctx\n-old\n+new\n")
        h = parse_diff(diff)[0].hunks[1]
        self.assertNotIn("gone", h.window("new"))
        self.assertNotIn("added", h.window("old"))

    def test_new_and_deleted_files(self):
        d = ("diff --git a/A.lean b/A.lean\nnew file mode 100644\n"
             "--- /dev/null\n+++ b/A.lean\n@@ -0,0 +1,1 @@\n+x\n")
        self.assertTrue(parse_diff(d)[0].is_new)

    def test_multiple_runs_split(self):
        diff = ("diff --git a/A.lean b/A.lean\n--- a/A.lean\n+++ b/A.lean\n"
                "@@ -1,12 +1,12 @@\n a\n-b\n+B\n c\n d\n e\n f\n g\n-h\n+H\n i\n")
        self.assertEqual(len(parse_diff(diff)[0].hunks), 2)


class TestLeanLex(unittest.TestCase):
    def test_dotted_identifier_is_one_token(self):
        self.assertIn("Nat.succ_le_succ", leanlex.tokenize("exact Nat.succ_le_succ h"))

    def test_unicode_identifiers(self):
        toks = leanlex.tokenize("theorem foo (α : Type*) (h₁ : ℕ) : α := h₁")
        self.assertIn("α", toks)
        self.assertIn("h₁", toks)

    def test_normalize_ignores_reflow_and_comments(self):
        a = "theorem x : p :=\n  rfl"
        b = "theorem x : p := rfl  -- note"
        self.assertEqual(leanlex.normalize(a), leanlex.normalize(b))

    def test_truncated_attribute_block(self):
        # hunks routinely cut mid-attribute; extraction must still work
        self.assertIn("implicit_reducible",
                      leanlex.attributes("@[to_additive (attr := implicit_reducible, simps)"))

    def test_truncated_simp_list(self):
        calls = leanlex.simp_calls("  simp only [Foo.bar, Baz.qux,")
        self.assertEqual(calls[0][2], ["Foo.bar", "Baz.qux"])

    def test_attribute_name_vs_argument(self):
        self.assertEqual(leanlex.in_attribute_position("@[deprecated mk]", "deprecated"), "name")
        self.assertEqual(leanlex.in_attribute_position("@[deprecated mk]", "mk"), "argument")

    def test_scan_context(self):
        src = ("import Mathlib.Init\n\nnamespace Foo\nnamespace Bar\n\n"
               "@[simp]\ntheorem baz (n : Nat) : n = n := by\n  rfl\n\nend Bar\nend Foo\n")
        ctx = leanlex.scan_context(src, 8)
        self.assertEqual(ctx.name, "Foo.Bar.baz")
        self.assertEqual(ctx.kind, "theorem")
        self.assertEqual(ctx.namespace, "Foo.Bar")
        self.assertIn("@[simp]", ctx.attributes)
        self.assertEqual(ctx.imports, ["Mathlib.Init"])

    def test_adaptation_note(self):
        notes = leanlex.extract_adaptation_notes(
            "#adaptation_note\n/-- `foo` now unfolds differently -/\nset_option x in")
        self.assertEqual(notes, ["`foo` now unfolds differently"])


class TestTaxonomy(unittest.TestCase):
    def assertLabel(self, old, new, label):
        v = taxonomy.classify(_hunk(old, new))
        self.assertIn(label, v.labels, f"{v.labels} for {old!r} -> {new!r}")

    def test_formatting_is_noise(self):
        v = taxonomy.classify(_hunk("theorem a : p :=\n  rfl", "theorem a : p := rfl"))
        self.assertTrue(v.noise)
        self.assertEqual(v.primary, RepairLabel.FORMATTING)

    def test_rename(self):
        self.assertLabel("exact Nat.succ_le_succ h", "exact Nat.succ_le_succ' h",
                         RepairLabel.RENAME_DECL)

    def test_namespace_move(self):
        self.assertLabel("open DirectLimit", "open Module.DirectLimit",
                         RepairLabel.NAMESPACE_MOVE)

    def test_rename_with_projection_is_not_namespace_move(self):
        v = taxonomy.classify(_hunk("exact eqRec_heq_iff_heq.mp h", "exact eqRec_heq_iff.mp h"))
        self.assertEqual(v.primary, RepairLabel.RENAME_DECL)

    def test_simp_added_removed_reoriented(self):
        self.assertLabel("  simp only [a, b]", "  simp only [a, b, c]",
                         RepairLabel.SIMP_LEMMA_ADDED)
        self.assertLabel("  simp only [a, b]", "  simp only [a]",
                         RepairLabel.SIMP_LEMMA_REMOVED)
        self.assertLabel("  simp only [a, b]", "  simp only [a, ← b]",
                         RepairLabel.SIMP_LEMMA_REORIENTED)

    def test_set_option_classes(self):
        self.assertLabel("theorem a", "set_option maxHeartbeats 400000 in\ntheorem a",
                         RepairLabel.HEARTBEAT_BUMP)
        self.assertLabel("theorem a",
                         "set_option backward.isDefEq.respectTransparency false in\ntheorem a",
                         RepairLabel.DEFEQ_TRANSPARENCY)
        self.assertLabel("theorem a", "set_option linter.unusedVariables false in\ntheorem a",
                         RepairLabel.LINTER_DISABLED)

    def test_attribute_reducible_migration(self):
        self.assertLabel("@[implicit_reducible]", "@[instance_reducible]",
                         RepairLabel.INSTANCE_REDUCIBILITY)

    def test_adaptation_note_only(self):
        v = taxonomy.classify(_hunk("", "#adaptation_note\n/-- `x` changed -/"))
        self.assertEqual(v.primary, RepairLabel.ADAPTATION_NOTE_ONLY)
        self.assertEqual(v.evidence["notes"], ["`x` changed"])

    def test_expected_errors_populated(self):
        v = taxonomy.classify(_hunk("exact foo h", "exact bar h"))
        self.assertIn(taxonomy.ErrorClass.UNKNOWN_IDENTIFIER, v.expected_errors)

    def test_token_substitution_rejects_insertions(self):
        # an insertion is not a reversible 1-for-1 substitution
        self.assertEqual(taxonomy.token_substitutions("f x", "f (g x)"), [])

    def test_token_substitution_requires_consistency(self):
        self.assertEqual(taxonomy.token_substitutions("f a a", "f b c"), [])

    def test_diagnostic_parsing(self):
        out = ("Mathlib/A.lean:12:4: error: unknown identifier 'foo'\n"
               "Mathlib/A.lean:20:2: warning: unused variable `h`\n")
        diags = taxonomy.parse_diagnostics(out)
        self.assertEqual(diags[0]["error_class"], taxonomy.ErrorClass.UNKNOWN_IDENTIFIER)
        self.assertEqual(diags[1]["error_class"], taxonomy.ErrorClass.LINTER_WARNING)


class TestRules(unittest.TestCase):
    def _pairs(self, n=3):
        return [{
            "label": RepairLabel.RENAME_DECL, "path": f"Mathlib/F{i}.lean",
            "commit": "abc", "toolchain_after": "v4.33.0",
            "broken": "exact Nat.old h", "fixed": "exact Nat.new h",
            "evidence": {"substitutions": [["Nat.old", "Nat.new"]]},
        } for i in range(n)]

    def test_induce_counts_support(self):
        rs = rules.induce(self._pairs(3), min_support=1)
        sub = [r for r in rs if r.kind == "substitution"][0]
        self.assertEqual(sub.support, 3)
        self.assertEqual(sub.files, 3)

    def test_reverse_then_forward_is_identity(self):
        r = [x for x in rules.induce(self._pairs(), min_support=1)
             if x.kind == "substitution"][0]
        clean = "theorem t : p := by\n  exact Nat.new h\n"
        broken, n = r.apply_reverse(clean)
        self.assertEqual(n, 1)
        self.assertIn("Nat.old", broken)
        restored, _ = r.apply_forward(broken)
        self.assertEqual(restored, clean)

    def test_reverse_respects_token_boundaries(self):
        r = [x for x in rules.induce(self._pairs(), min_support=1)
             if x.kind == "substitution"][0]
        # must not rewrite `Nat.newer` or `Foo.Nat.new`
        out, n = r.apply_reverse("Nat.newer\n")
        self.assertEqual(n, 0)
        self.assertEqual(out, "Nat.newer\n")

    def test_reverse_does_not_match_dotted_prefix(self):
        """A rule for `Mathlib.Tactic` must not fire inside
        `Mathlib.Tactic.Common` -- that silently corrupts unrelated imports."""
        p = [{"label": RepairLabel.NAMESPACE_MOVE, "path": "a", "commit": "c",
              "broken": "Tactic", "fixed": "Mathlib.Tactic",
              "evidence": {"substitutions": [["Tactic", "Mathlib.Tactic"]]}}]
        r = [x for x in rules.induce(p, 1) if x.kind == "substitution"][0]
        out, n = r.apply_reverse("import Mathlib.Tactic.Common\n")
        self.assertEqual((out, n), ("import Mathlib.Tactic.Common\n", 0))
        out, n = r.apply_reverse("import Mathlib.Tactic\n")
        self.assertEqual((out, n), ("import Tactic\n", 1))

    def test_reverse_still_matches_projection_suffix(self):
        p = [{"label": RepairLabel.RENAME_DECL, "path": "a", "commit": "c",
              "broken": "old_iff", "fixed": "new_iff",
              "evidence": {"substitutions": [["old_iff", "new_iff"]]}}]
        r = [x for x in rules.induce(p, 1) if x.kind == "substitution"][0]
        out, n = r.apply_reverse("exact new_iff.mp h\n")
        self.assertEqual((out, n), ("exact old_iff.mp h\n", 1))

    def test_unsafe_tokens_not_reversed(self):
        p = [{"label": RepairLabel.RENAME_DECL, "path": "a", "commit": "c",
              "broken": "rfl", "fixed": "trivial",
              "evidence": {"substitutions": [["rfl", "trivial"]]}}]
        self.assertFalse([r for r in rules.induce(p, 1) if r.kind == "substitution"])

    def test_synthesize_roundtrip_validated(self):
        rs = [r for r in rules.induce(self._pairs(), min_support=1)
              if r.kind == "substitution"]
        files = {"Mathlib/X.lean": "a\nexact Nat.new h\nb\n",
                 "Mathlib/Y.lean": "nothing here\n"}
        samples = rules.synthesize(rs, files, n=10)
        self.assertEqual(len(samples), 1)
        self.assertIn("Nat.old", samples[0].broken)
        self.assertNotIn("Nat.old", samples[0].fixed)


class TestEmit(unittest.TestCase):
    def _rows(self):
        return [
            {"label": "a", "broken": "x", "fixed": "y", "path": "p1",
             "toolchain_after": "v1", "date": "2024-01-01", "confidence": 1.0},
            {"label": "a", "broken": "x", "fixed": "y  ", "path": "p2",
             "toolchain_after": "v1", "date": "2024-01-01", "confidence": 1.0},
            {"label": "b", "broken": "q", "fixed": "r", "path": "p3",
             "toolchain_after": "v2", "date": "2025-01-01", "confidence": 1.0},
        ]

    def test_dedup_is_whitespace_insensitive(self):
        rows, _ = emit.dedup(self._rows())
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["duplicate_count"], 2)

    def test_cap_per_label(self):
        rows = [{"label": "a", "broken": f"x{i}", "fixed": "y", "confidence": 1.0}
                for i in range(50)]
        rows, _ = emit.dedup(rows)
        self.assertEqual(len(emit.rebalance(rows, cap_per_label=5)), 5)

    def test_split_falls_back_when_too_few_groups(self):
        rows = [{"label": "a", "broken": f"x{i}", "fixed": "y", "path": f"p{i}",
                 "toolchain_after": "v1", "date": "2024-01-01", "confidence": 1.0}
                for i in range(9)]
        rows, _ = emit.dedup(rows)
        buckets, used = emit.split(rows, by="toolchain", ratios=(0.6, 0.2, 0.2))
        self.assertNotEqual(used, "toolchain")     # one group cannot fill three
        self.assertTrue(all(buckets[k] for k in ("train", "val", "test")))

    def test_split_is_group_disjoint_and_chronological(self):
        rows, _ = emit.dedup(self._rows() * 30)
        buckets, _ = emit.split(rows, by="toolchain", ratios=(0.5, 0.25, 0.25))
        groups = {k: {r["toolchain_after"] for r in v} for k, v in buckets.items() if v}
        seen = set()
        for g in groups.values():
            self.assertFalse(seen & g, "toolchain leaked across splits")
            seen |= g


class TestEndToEnd(unittest.TestCase):
    """Build a real git repo shaped like mathlib and run discover -> mine -> rules."""

    OLD = """import Mathlib.Init

namespace Foo

@[implicit_reducible]
theorem bar (n : Nat) : n + 0 = n := by
  simp only [Nat.add_zero, Nat.old_lemma]

theorem baz (n : Nat) : n = n := by
  exact Nat.old_lemma n

end Foo
"""
    NEW = """import Mathlib.Init

namespace Foo

@[instance_reducible]
theorem bar (n : Nat) : n + 0 = n := by
  simp only [Nat.add_zero]

set_option maxHeartbeats 400000 in
theorem baz (n : Nat) : n = n := by
  exact Nat.new_lemma n

end Foo
"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Path(self.tmp) / "repo"
        self.repo.mkdir()
        run = lambda *a: subprocess.run(["git", "-C", str(self.repo), *a],
                                        capture_output=True, check=True)
        run("init", "-q", "-b", "master")
        run("config", "user.email", "t@t.t")
        run("config", "user.name", "t")
        (self.repo / "Mathlib").mkdir()
        (self.repo / "lean-toolchain").write_text("leanprover/lean4:v4.32.0\n")
        (self.repo / "Mathlib" / "Foo.lean").write_text(self.OLD)
        run("add", "-A")
        run("commit", "-qm", "feat: initial")
        (self.repo / "lean-toolchain").write_text("leanprover/lean4:v4.33.0-rc1\n")
        (self.repo / "Mathlib" / "Foo.lean").write_text(self.NEW)
        run("add", "-A")
        run("commit", "-qm", "chore: bump toolchain to v4.33.0-rc1 (#41779)")

    def test_discover_finds_squashed_bump(self):
        git = Git(self.repo)
        ws = discover.discover(git, "master", modes=("squash",))
        self.assertEqual(len(ws), 1)
        w = ws[0]
        self.assertEqual(w.kind, "bump")
        self.assertEqual(w.key, "v4.33.0-rc1")
        self.assertEqual(w.pr, "41779")
        self.assertIn("v4.32.0", w.toolchain_before)
        self.assertIn("v4.33.0-rc1", w.toolchain_after)

    def test_toolchain_mode_agrees_with_message_mode(self):
        git = Git(self.repo)
        a = discover.discover(git, "master", modes=("squash",))
        b = discover.discover(git, "master", modes=("toolchain",))
        self.assertEqual([(w.base, w.tip) for w in a], [(w.base, w.tip) for w in b])

    def test_mine_produces_labelled_pairs(self):
        git = Git(self.repo)
        w = discover.discover(git, "master", modes=("squash",))[0]
        pairs = mine.mine_window(git, w)
        labels = {p.label for p in pairs}
        self.assertIn(RepairLabel.INSTANCE_REDUCIBILITY, labels)
        self.assertIn(RepairLabel.SIMP_LEMMA_REMOVED, labels)
        self.assertTrue({RepairLabel.HEARTBEAT_BUMP, RepairLabel.RENAME_DECL} & labels)
        for p in pairs:
            self.assertEqual(p.toolchain_after, "leanprover/lean4:v4.33.0-rc1")
            self.assertEqual(p.path, "Mathlib/Foo.lean")
            self.assertTrue(p.expected_errors)

    def test_mined_windows_are_contiguous_file_slices(self):
        """The reversal step string-matches windows against a checkout, so a
        mined `fixed_window` must appear verbatim in the post-adaptation file."""
        git = Git(self.repo)
        w = discover.discover(git, "master", modes=("squash",))[0]
        post = git.file_at(w.tip, "Mathlib/Foo.lean")
        for p in mine.mine_window(git, w):
            self.assertIn(p.fixed_window, post, f"window not verbatim: {p.label}")

    def test_full_pipeline_to_rules_and_synthesis(self):
        git = Git(self.repo)
        w = discover.discover(git, "master", modes=("squash",))[0]
        rows = [p.to_json() for p in mine.mine_window(git, w)]
        rows, _ = emit.dedup(rows)
        rs = rules.induce(rows, min_support=1)
        self.assertTrue(rs)
        subs = {(r.forward["from"], r.forward["to"])
                for r in rs if r.kind == "substitution"}
        self.assertIn(("Nat.old_lemma", "Nat.new_lemma"), subs)
        clean = {"Mathlib/Other.lean": "theorem q : p := Nat.new_lemma 3\n"}
        samples = rules.synthesize(rs, clean, n=5)
        self.assertTrue(any("Nat.old_lemma" in s.broken for s in samples))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
