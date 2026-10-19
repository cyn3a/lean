from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from lake_env_adapter.apply import apply_plan
from lake_env_adapter.planner import Planner
from lake_env_adapter.reservoir import ReservoirSelection


TOOLCHAIN = "leanprover/lean4:v4.20.0"
OLD_TOOLCHAIN = "leanprover/lean4:v4.19.0"


class FakeReservoir:
    def select_exact_build(self, owner: str, name: str, target: str):
        if name == "removed" and target == TOOLCHAIN:
            return ReservoirSelection(
                revision="removed-new-sha",
                repository_url="https://github.com/acme/removed.git",
                subdir=None,
                identity="@acme/removed",
                run_at="2026-01-01T00:00:00Z",
            )
        return None

    def package_source(self, owner: str, name: str):
        return None, None


class NeverHistory:
    def find_exact_toolchain(self, repository_url: str, target: str, subdir=None):
        raise AssertionError("history should not be needed")


class WorkflowTests(unittest.TestCase):
    def test_planner_matches_lake_quoted_manifest_dependency_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old, new, target = root / "old", root / "new", root / "target"
            lakefile = '''import Lake
open Lake DSL
package Demo
require "0art0" / "lean-slides"
'''
            old_package = _package("«lean-slides»", "old-sha")
            old_package["scope"] = "0art0"
            new_package = _package("«lean-slides»", "new-sha")
            new_package["scope"] = "0art0"
            for directory, toolchain, manifest in (
                (old, OLD_TOOLCHAIN, _manifest(old_package)),
                (new, TOOLCHAIN, _manifest(new_package)),
                (target, OLD_TOOLCHAIN, _manifest(old_package)),
            ):
                directory.mkdir()
                (directory / "lean-toolchain").write_text(toolchain + "\n", encoding="utf-8")
                (directory / "lakefile.lean").write_text(lakefile, encoding="utf-8")
                (directory / "lake-manifest.json").write_text(
                    json.dumps(manifest), encoding="utf-8"
                )

            plan = Planner(FakeReservoir(), NeverHistory()).build(old, new, target)
            self.assertFalse(plan.unresolved)
            self.assertEqual(plan.decisions[0].source, "new-manifest")
            self.assertEqual(plan.decisions[0].revision, "new-sha")

    def test_plan_and_apply_use_new_manifest_and_recover_removed_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old, new, target = root / "old", root / "new", root / "target"
            for directory in (old, new, target):
                directory.mkdir()
                (directory / "lean-toolchain").write_text(TOOLCHAIN + "\n", encoding="utf-8")
            (old / "lean-toolchain").write_text(OLD_TOOLCHAIN + "\n", encoding="utf-8")
            (target / "lean-toolchain").write_text(OLD_TOOLCHAIN + "\n", encoding="utf-8")
            lakefile = '''name = "demo"

[[require]]
name = "alpha"
git = "https://github.com/acme/alpha.git"
rev = "old-alpha"

[[require]]
name = "removed"
git = "https://github.com/acme/removed.git"
rev = "old-removed"
'''
            for directory in (old, new, target):
                (directory / "lakefile.toml").write_text(lakefile, encoding="utf-8")
            old_manifest = _manifest(
                _package("alpha", "old-alpha-sha"),
                _package("removed", "old-removed-sha"),
            )
            inherited_alpha = _package("alpha", "alpha-new-sha")
            inherited_alpha["inherited"] = True
            new_manifest = _manifest(inherited_alpha)
            (old / "lake-manifest.json").write_text(json.dumps(old_manifest), encoding="utf-8")
            (new / "lake-manifest.json").write_text(json.dumps(new_manifest), encoding="utf-8")
            (target / "lake-manifest.json").write_text(json.dumps(old_manifest), encoding="utf-8")

            plan = Planner(FakeReservoir(), NeverHistory()).build(old, new, target)
            self.assertEqual(plan.target_toolchain, TOOLCHAIN)
            self.assertEqual([d.source for d in plan.decisions], ["new-manifest", "reservoir"])
            self.assertFalse(plan.decisions[0].package["inherited"])
            self.assertFalse(plan.unresolved)
            result = apply_plan(plan, run_update=False, backup=False)
            self.assertEqual(result.selected_count, 2)
            self.assertEqual(
                (target / "lean-toolchain").read_text(encoding="utf-8").strip(), TOOLCHAIN
            )

            updated_lakefile = (target / "lakefile.toml").read_text(encoding="utf-8")
            self.assertIn('rev = "alpha-new-sha"', updated_lakefile)
            self.assertIn('rev = "removed-new-sha"', updated_lakefile)
            updated_manifest = json.loads((target / "lake-manifest.json").read_text(encoding="utf-8"))
            revisions = {package["name"]: package["rev"] for package in updated_manifest["packages"]}
            self.assertEqual(
                revisions,
                {"alpha": "alpha-new-sha", "removed": "removed-new-sha"},
            )

    def test_apply_runs_lake_for_transitives_and_checks_direct_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old, new, target = root / "old", root / "new", root / "target"
            for directory in (old, new, target):
                directory.mkdir()
                (directory / "lean-toolchain").write_text(TOOLCHAIN + "\n", encoding="utf-8")
                (directory / "lakefile.toml").write_text(
                    '''name = "demo"
[[require]]
name = "alpha"
git = "https://github.com/acme/alpha.git"
rev = "old"
''',
                    encoding="utf-8",
                )
            (old / "lean-toolchain").write_text(OLD_TOOLCHAIN + "\n", encoding="utf-8")
            (target / "lean-toolchain").write_text(OLD_TOOLCHAIN + "\n", encoding="utf-8")
            old_manifest = _manifest(_package("alpha", "old-sha"))
            transitive = _package("transitive", "t-old")
            transitive["inherited"] = True
            new_manifest = _manifest(_package("alpha", "new-sha"), transitive)
            (old / "lake-manifest.json").write_text(json.dumps(old_manifest), encoding="utf-8")
            (new / "lake-manifest.json").write_text(json.dumps(new_manifest), encoding="utf-8")
            (target / "lake-manifest.json").write_text(json.dumps(old_manifest), encoding="utf-8")
            # Using Python as lake_command makes this local script a hermetic fake Lake process.
            # A bare `lake update` would re-resolve every direct dependency from the lakefile,
            # so Lake must be asked to update only a transitive package.
            (target / "update").write_text(
                '''import json
import sys
from pathlib import Path
assert sys.argv[1:] == ["--keep-toolchain", "transitive"], sys.argv[1:]
path = Path("lake-manifest.json")
manifest = json.loads(path.read_text(encoding="utf-8"))
assert manifest["packages"][0]["rev"] == "new-sha"
assert [p["name"] for p in manifest["packages"]] == ["alpha", "transitive"]
manifest["packages"][1]["rev"] = "t-sha"
path.write_text(json.dumps(manifest), encoding="utf-8")
''',
                encoding="utf-8",
            )

            plan = Planner(FakeReservoir(), NeverHistory()).build(old, new, target)
            result = apply_plan(plan, run_update=True, lake_command=sys.executable, backup=False)
            self.assertTrue(result.lake_updated)
            self.assertEqual(
                (target / "lean-toolchain").read_text(encoding="utf-8").strip(), TOOLCHAIN
            )
            resolved = json.loads((target / "lake-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual([item["name"] for item in resolved["packages"]], ["alpha", "transitive"])
            self.assertEqual(resolved["packages"][1]["rev"], "t-sha")

    def test_apply_names_explicitly_pinned_direct_dependency_without_transitives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old, new, target = root / "old", root / "new", root / "target"
            for directory in (old, new, target):
                directory.mkdir()
                (directory / "lean-toolchain").write_text(TOOLCHAIN + "\n", encoding="utf-8")
                (directory / "lakefile.toml").write_text(
                    '''name = "demo"
[[require]]
name = "alpha"
git = "https://github.com/acme/alpha.git"
rev = "old"
''',
                    encoding="utf-8",
                )
            (old / "lean-toolchain").write_text(OLD_TOOLCHAIN + "\n", encoding="utf-8")
            (target / "lean-toolchain").write_text(OLD_TOOLCHAIN + "\n", encoding="utf-8")
            old_manifest = _manifest(_package("alpha", "old-sha"))
            new_manifest = _manifest(_package("alpha", "new-sha"))
            (old / "lake-manifest.json").write_text(json.dumps(old_manifest), encoding="utf-8")
            (new / "lake-manifest.json").write_text(json.dumps(new_manifest), encoding="utf-8")
            (target / "lake-manifest.json").write_text(json.dumps(old_manifest), encoding="utf-8")
            (target / "update").write_text(
                '''import sys
assert sys.argv[1:] == ["--keep-toolchain", "alpha"], sys.argv[1:]
''',
                encoding="utf-8",
            )
            plan = Planner(FakeReservoir(), NeverHistory()).build(old, new, target)
            apply_plan(plan, run_update=True, lake_command=sys.executable, backup=False)
            self.assertIn('rev = "new-sha"', (target / "lakefile.toml").read_text(encoding="utf-8"))

    def test_apply_writes_target_manifest_in_new_layout(self) -> None:
        """Old Lean v4.0 projects use nested version-5 records; the new toolchain's Lake
        reads only the new manifest's layout (Lake v4.5: `no inductive constructor
        matched` when version-7 records appear under `"version": 5`)."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old, new, target = root / "old", root / "new", root / "target"
            lakefile = '''import Lake
open Lake DSL
package demo
require std from git "https://github.com/leanprover/std4/" @ "main"
require proofwidgets from git "https://github.com/EdAyers/ProofWidgets4" @ "v0.0.14"
'''
            legacy = {
                "version": 5,
                "packagesDir": "lake-packages",
                "packages": [
                    {"git": {"url": "https://github.com/EdAyers/ProofWidgets4", "subDir?": None,
                             "rev": "pw-old", "opts": {}, "name": "proofwidgets",
                             "inputRev?": "v0.0.14", "inherited": False}},
                    {"git": {"url": "https://github.com/leanprover/std4/", "subDir?": None,
                             "rev": "std-old", "opts": {}, "name": "std",
                             "inputRev?": "main", "inherited": False}},
                ],
            }
            new_std = _package("std", "std-new")
            new_std["inputRev"] = "main"
            new_pw = _package("proofwidgets", "pw-new")
            new_pw["inputRev"] = "v0.0.25"
            new_manifest = {
                "version": 7,
                "packagesDir": ".lake/packages",
                "packages": [new_std, new_pw],
                "name": "demo",
                "lakeDir": ".lake",
            }
            for directory, toolchain, manifest in (
                (old, OLD_TOOLCHAIN, legacy),
                (new, TOOLCHAIN, new_manifest),
                (target, OLD_TOOLCHAIN, legacy),
            ):
                directory.mkdir()
                (directory / "lean-toolchain").write_text(toolchain + "\n", encoding="utf-8")
                (directory / "lakefile.lean").write_text(lakefile, encoding="utf-8")
                (directory / "lake-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            plan = Planner(FakeReservoir(), NeverHistory()).build(old, new, target)
            self.assertEqual(plan.manifest_template["version"], 7)
            apply_plan(plan, run_update=False, backup=False)
            written = json.loads((target / "lake-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(written["version"], 7)
            self.assertEqual(written["lakeDir"], ".lake")
            self.assertEqual(written["packagesDir"], ".lake/packages")
            self.assertEqual(
                {p["name"]: (p["type"], p["rev"], p["inherited"]) for p in written["packages"]},
                {"std": ("git", "std-new", False), "proofwidgets": ("git", "pw-new", False)},
            )
            self.assertTrue(all("git" not in p for p in written["packages"]))

    def test_apply_falls_back_for_lake_without_keep_toolchain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old, new, target = root / "old", root / "new", root / "target"
            for directory in (old, new, target):
                directory.mkdir()
                (directory / "lean-toolchain").write_text(TOOLCHAIN + "\n", encoding="utf-8")
                (directory / "lakefile.toml").write_text('name = "demo"\n', encoding="utf-8")
                (directory / "lake-manifest.json").write_text(
                    json.dumps(_manifest()), encoding="utf-8"
                )
            (old / "lean-toolchain").write_text(OLD_TOOLCHAIN + "\n", encoding="utf-8")
            (target / "lean-toolchain").write_text(OLD_TOOLCHAIN + "\n", encoding="utf-8")
            (target / "update").write_text(
                '''import sys
if "--keep-toolchain" in sys.argv:
    print("error: unknown long option '--keep-toolchain'", file=sys.stderr)
    raise SystemExit(1)
''',
                encoding="utf-8",
            )

            plan = Planner(FakeReservoir(), NeverHistory()).build(old, new, target)
            result = apply_plan(plan, run_update=True, lake_command=sys.executable, backup=False)
            self.assertTrue(result.lake_updated)
            self.assertEqual(
                (target / "lean-toolchain").read_text(encoding="utf-8").strip(), TOOLCHAIN
            )


def _package(name: str, revision: str) -> dict:
    return {
        "url": f"https://github.com/acme/{name}.git",
        "type": "git",
        "subDir": None,
        "scope": "",
        "rev": revision,
        "name": name,
        "inputRev": revision,
        "inherited": False,
        "configFile": "lakefile.toml",
    }


def _manifest(*packages: dict) -> dict:
    return {
        "version": "1.1.0",
        "packagesDir": ".lake/packages",
        "packages": list(packages),
        "name": "demo",
        "lakeDir": ".lake",
    }


if __name__ == "__main__":
    unittest.main()
