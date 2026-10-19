import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import build_repair_corpus as b  # noqa: E402

DIFF = """# repo: x/y
# pair_id: abc
# old: 1 leanprover/lean4:v4.14.0
# new: 2 leanprover/lean4:v4.15.0
diff --git a/A.lean b/A.lean
index 1..2 100644
--- a/A.lean
+++ b/A.lean
@@ -1,3 +1,3 @@
 import Std
-def a := 1
+def a := 2
 def b := a
@@ -10,2 +10,2 @@ theorem t
-  simp
+  simp only [a]
 end
diff --git a/lean-toolchain b/lean-toolchain
--- a/lean-toolchain
+++ b/lean-toolchain
@@ -1 +1 @@
-leanprover/lean4:v4.14.0
+leanprover/lean4:v4.15.0
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-old
+new
diff --git a/New.lean b/New.lean
new file mode 100644
--- /dev/null
+++ b/New.lean
@@ -0,0 +1,2 @@
+def n := 1
+def m := n
diff --git a/img.png b/img.png
Binary files a/img.png and b/img.png differ
"""


class ParseTests(unittest.TestCase):
    def test_units_and_exclusions(self):
        patches = b.parse_diff(DIFF)
        self.assertEqual([p.path for p in patches], ["A.lean", "lean-toolchain", "README.md", "New.lean", "img.png"])
        units, excluded = b.candidate_units(patches)
        self.assertEqual(sorted(units), ["A.lean#0", "A.lean#1", "New.lean#file"])
        self.assertEqual(excluded, {"protocol-config": 1, "non-lean": 1, "binary": 1})
        self.assertEqual(units["A.lean#0"][0].added, 1)
        self.assertEqual(units["A.lean#0"][0].removed, 1)

    def test_render_subset(self):
        patches = b.parse_diff(DIFF)
        text = b.render_patch(patches, {"A.lean#1"})
        self.assertIn("@@ -10,2 +10,2 @@", text)
        self.assertNotIn("@@ -1,3 +1,3 @@", text)
        self.assertNotIn("lean-toolchain", text)
        self.assertTrue(text.startswith("diff --git a/A.lean b/A.lean\n"))
        self.assertTrue(text.endswith("\n"))
        whole = b.render_patch(patches, {"New.lean#file"})
        self.assertIn("new file mode", whole)
        self.assertIn("+def m := n", whole)

    def test_render_all_preserves_hunk_text(self):
        patches = b.parse_diff(DIFF)
        units, _ = b.candidate_units(patches)
        text = b.render_patch(patches, set(units))
        for h in units["A.lean#0"] + units["A.lean#1"]:
            self.assertIn(h.text, text)


class DdminTests(unittest.TestCase):
    def test_finds_minimal_pair(self):
        items = [f"h{i}" for i in range(12)]
        needed = {"h3", "h9"}
        calls = []

        def test(subset):
            calls.append(len(subset))
            return needed <= set(subset)

        result, done = b.ddmin(items, test, lambda: 10**6)
        self.assertTrue(done)
        self.assertEqual(set(result), needed)

    def test_budget_exhaustion_returns_best_so_far(self):
        items = [f"h{i}" for i in range(16)]
        budget = {"n": 3}

        def left():
            return budget["n"]

        def test(subset):
            budget["n"] -= 1
            return "h0" in subset

        result, done = b.ddmin(items, test, left)
        self.assertFalse(done)
        self.assertIn("h0", result)
        self.assertLess(len(result), 16)

    def test_single_item(self):
        result, done = b.ddmin(["only"], lambda s: True, lambda: 100)
        self.assertEqual(result, ["only"])
        self.assertTrue(done)


if __name__ == "__main__":
    unittest.main()
