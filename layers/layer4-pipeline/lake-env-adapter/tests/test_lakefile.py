from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lake_env_adapter.lakefile import parse_lakefile, update_explicit_revisions


class LakefileTests(unittest.TestCase):
    def test_toml_parse_and_format_preserving_revision_update(self) -> None:
        text = '''name = "demo"

[[require]]
name = "alpha"
git = "https://github.com/acme/alpha.git"
rev = "old" # keep this comment

[[require]]
name = "local"
path = "../local"
'''
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "lakefile.toml"
            path.write_text(text, encoding="utf-8")
            dependencies = parse_lakefile(path)
            self.assertEqual([dep.name for dep in dependencies], ["alpha", "local"])
            self.assertTrue(dependencies[0].explicit_rev)
            self.assertEqual(dependencies[1].source_type, "path")
            edited = update_explicit_revisions(path, {0: "abc123"})
            self.assertIn('rev = "abc123" # keep this comment', edited)
            self.assertIn('path = "../local"', edited)

    def test_lean_parse_git_and_reservoir_forms(self) -> None:
        text = '''import Lake
open Lake DSL

-- require ignored from git "https://example.invalid/nope" @ "bad"
require alpha from git
  "https://github.com/acme/alpha.git" @ "old-alpha"
require "leanprover-community" / "mathlib" @ git "old-mathlib"
require "leanprover" / verso @ "1.2.3"

/- require alsoIgnored from git "x" @ "y" -/
package demo
'''
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "lakefile.lean"
            path.write_text(text, encoding="utf-8")
            dependencies = parse_lakefile(path)
            self.assertEqual([dep.name for dep in dependencies], ["alpha", "mathlib", "verso"])
            self.assertEqual(dependencies[0].url, "https://github.com/acme/alpha.git")
            self.assertEqual(dependencies[1].scope, "leanprover-community")
            self.assertEqual(dependencies[1].declared_rev, "old-mathlib")
            self.assertFalse(dependencies[2].explicit_rev)
            self.assertEqual(dependencies[2].declared_version, "1.2.3")
            edited = update_explicit_revisions(path, {0: "new-alpha", 1: "new-mathlib"})
            self.assertIn('@ "new-alpha"', edited)
            self.assertIn('@ git "new-mathlib"', edited)
            self.assertIn('@ "1.2.3"', edited)


if __name__ == "__main__":
    unittest.main()
