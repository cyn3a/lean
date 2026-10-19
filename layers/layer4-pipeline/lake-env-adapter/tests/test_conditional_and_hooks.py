from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from lake_env_adapter.apply import apply_plan
from lake_env_adapter.lakefile import parse_lakefile
from lake_env_adapter.planner import Planner


TOOLCHAIN = "leanprover/lean4:v4.10.0"
OLD_TOOLCHAIN = "leanprover/lean4:v4.8.0-rc1"
MATHLIB_TOOLCHAIN = "leanprover/lean4:v4.7.0"


class NoReservoir:
    def select_exact_build(self, owner: str, name: str, target: str):
        raise AssertionError(f"Reservoir should not be consulted for @{owner}/{name}")

    def package_source(self, owner: str, name: str):
        raise AssertionError("Reservoir should not be consulted")


class NoHistory:
    def find_exact_toolchain(self, repository_url: str, target: str, subdir=None):
        raise AssertionError("history should not be needed")


def _package(name, rev, inherited=False):
    return {"url": f"https://github.com/acme/{name}", "type": "git", "subDir": None,
            "scope": "", "rev": rev, "name": name, "inputRev": None, "inherited": inherited}


def _manifest(*packages):
    return {"version": "1.1.0", "packagesDir": ".lake/packages", "name": "demo",
            "lakeDir": ".lake", "packages": list(packages)}


def _project(root: Path, lakefile: str, old_manifest: dict, new_manifest: dict):
    old, new, target = root / "old", root / "new", root / "target"
    for directory, toolchain, manifest in (
        (old, OLD_TOOLCHAIN, old_manifest),
        (new, TOOLCHAIN, new_manifest),
        (target, OLD_TOOLCHAIN, old_manifest),
    ):
        directory.mkdir()
        (directory / "lean-toolchain").write_text(toolchain + "\n", encoding="utf-8")
        (directory / "lakefile.lean").write_text(lakefile, encoding="utf-8")
        (directory / "lake-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return old, new, target


CONDITIONAL_LAKEFILE = '''import Lake
open Lake DSL
package demo
require mathlib from git
  "https://github.com/leanprover-community/mathlib4.git"

meta if get_config? env = some "dev" then
require «doc-gen4» from git
  "https://github.com/leanprover/doc-gen4" @ "main"
'''


class ConditionalRequireTests(unittest.TestCase):
    def test_lean_meta_if_require_is_conditional(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "lakefile.lean"
            path.write_text(CONDITIONAL_LAKEFILE, encoding="utf-8")
            deps = parse_lakefile(path)
            self.assertEqual([d.name for d in deps], ["mathlib", "doc-gen4"])
            self.assertFalse(deps[0].conditional)
            self.assertTrue(deps[1].conditional)
            self.assertTrue(deps[1].explicit_rev)


class PostUpdateHookTests(unittest.TestCase):
    def test_post_update_hook_toolchain_copy_is_restored_and_conditional_is_not_drift(self) -> None:
        old_manifest = _manifest(_package("batteries", "b-old", True), _package("mathlib", "m-old"),
                                 _package("doc-gen4", "d-old"))
        new_manifest = _manifest(_package("batteries", "b-new", True), _package("mathlib", "m-new"),
                                 _package("doc-gen4", "d-new"))
        with tempfile.TemporaryDirectory() as temporary:
            old, new, target = _project(Path(temporary), CONDITIONAL_LAKEFILE, old_manifest, new_manifest)
            # Fake Lake predating --keep-toolchain whose "post-update hook" copies
            # Mathlib's toolchain over the root's (as Mathlib's 2024 lakefile does)
            # and, like Lake, drops the inactive `meta if` dependency's record.
            # The outer Lake must see the ELAN_TOOLCHAIN pin.
            (target / "update").write_text(
                f'''import json, os, sys
from pathlib import Path
assert os.environ.get("ELAN_TOOLCHAIN") == "{TOOLCHAIN}", os.environ.get("ELAN_TOOLCHAIN")
if "--keep-toolchain" in sys.argv:
    print("error: unknown long option '--keep-toolchain'", file=sys.stderr)
    raise SystemExit(1)
assert sys.argv[1:] == ["batteries"], sys.argv[1:]
path = Path("lake-manifest.json")
manifest = json.loads(path.read_text(encoding="utf-8"))
manifest["packages"] = [p for p in manifest["packages"] if p["name"] != "doc-gen4"]
for p in manifest["packages"]:
    if p["name"] == "batteries":
        p["rev"] = "b-resolved"
path.write_text(json.dumps(manifest), encoding="utf-8")
Path("lean-toolchain").write_text("{MATHLIB_TOOLCHAIN}\\n", encoding="utf-8")
''',
                encoding="utf-8",
            )
            plan = Planner(NoReservoir(), NoHistory()).build(old, new, target)
            self.assertTrue(plan.decisions[1].dependency.conditional)
            result = apply_plan(plan, run_update=True, lake_command=sys.executable, backup=False)
            self.assertTrue(result.lake_updated)
            self.assertEqual(
                (target / "lean-toolchain").read_text(encoding="utf-8").strip(), TOOLCHAIN
            )
            resolved = json.loads((target / "lake-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual({p["name"]: p["rev"] for p in resolved["packages"]},
                             {"batteries": "b-resolved", "mathlib": "m-new"})

    def test_unconditional_dependency_missing_after_update_is_still_drift(self) -> None:
        lakefile = '''import Lake
open Lake DSL
package demo
require alpha from git "https://github.com/acme/alpha" @ "main"
'''
        alpha_old = dict(_package("alpha", "a-old"), inputRev="main")
        alpha_new = dict(alpha_old, rev="a-new")
        with tempfile.TemporaryDirectory() as temporary:
            old, new, target = _project(Path(temporary), lakefile, _manifest(alpha_old), _manifest(alpha_new))
            (target / "update").write_text(
                '''import json
from pathlib import Path
path = Path("lake-manifest.json")
manifest = json.loads(path.read_text(encoding="utf-8"))
manifest["packages"] = []
path.write_text(json.dumps(manifest), encoding="utf-8")
''',
                encoding="utf-8",
            )
            plan = Planner(NoReservoir(), NoHistory()).build(old, new, target)
            with self.assertRaises(Exception) as raised:
                apply_plan(plan, run_update=True, lake_command=sys.executable, backup=False)
            self.assertIn("got missing", str(raised.exception))
            # Rolled back: the old toolchain is intact.
            self.assertEqual(
                (target / "lean-toolchain").read_text(encoding="utf-8").strip(), OLD_TOOLCHAIN
            )


if __name__ == "__main__":
    unittest.main()
