"""Throughput features: dependency mirrors, background cleanup, fork reuse, timeout prediction."""

from __future__ import annotations

from argparse import Namespace
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import run_pipeline as pipeline
import resource_management as resources
from result_validation import PREDICTED_TIMEOUT_OUTCOME, RESULT_FORMAT_VERSION
from test_clone_pruning import git, make_repo
from test_parallel_pipeline import insert_pair

OLD = "leanprover/lean4:v4.0.0"
NEW = "leanprover/lean4:v4.1.0"
MATHLIB = "https://github.com/leanprover-community/mathlib4"
PROOFWIDGETS = "https://github.com/leanprover-community/ProofWidgets4"
FAKE_LAKE = '''\
import os, sys
args = sys.argv[1:]
if "--help" in args:
    print("OPTIONS:\\n  --keep-toolchain, -K  do not update toolchain\\n"
          "  --no-build            exit immediately if a build target is not up-to-date")
    sys.exit(0)
if "--no-build" in args:
    sys.exit(int(os.environ.get("FAKE_LAKE_NO_BUILD_EXIT", "3")))
print("[1/1] Built Demo")
print("Build completed successfully.")
sys.exit(0)
'''


def native_log(version: str) -> str:
    return (f"# toolchain: {version}\n[1/1] Built Demo\n# toolchain_after: {version}\n"
            f"# root_olean_count: 1\n# exit_code: 0\n")


class ManifestParsingTests(unittest.TestCase):
    def test_nested_schema_versions_4_to_5(self) -> None:
        manifest = {
            "version": 5, "packagesDir": "lake-packages",
            "packages": [
                {"git": {"url": MATHLIB, "subDir?": None, "rev": "abc", "name": "mathlib", "inputRev?": "master"}},
                {"path": {"name": "local", "dir": "../local"}},
            ],
        }
        self.assertEqual(pipeline.manifest_git_packages(manifest), [("mathlib", MATHLIB, "abc")])
        self.assertEqual(pipeline.manifest_package_names(manifest), {"mathlib", "local"})
        self.assertEqual(pipeline.manifest_packages_dir(Path("C:/wt"), manifest), Path("C:/wt/lake-packages"))

    def test_flat_schema_versions_6_7_and_strings(self) -> None:
        manifest = {
            "version": "1.1.0", "packagesDir": ".lake/packages",
            "packages": [
                {"type": "git", "url": "https://github.com/leanprover/std4/", "rev": "r1", "name": "std"},
                {"type": "path", "name": "p", "dir": "."},
                {"type": "git", "url": PROOFWIDGETS, "rev": "r2", "name": "proofwidgets"},
            ],
        }
        self.assertEqual(pipeline.manifest_git_packages(manifest),
                         [("std", "https://github.com/leanprover/std4/", "r1"), ("proofwidgets", PROOFWIDGETS, "r2")])
        self.assertEqual(pipeline.manifest_packages_dir(Path("C:/wt"), manifest), Path("C:/wt/.lake/packages"))
        for version, expected in ((6, "lake-packages"), (7, ".lake/packages"), ("1.0.0", ".lake/packages")):
            self.assertEqual(pipeline.manifest_packages_dir(Path("C:/wt"), {"version": version}),
                             Path("C:/wt") / expected, version)
        self.assertEqual(pipeline.manifest_packages_dir(Path("C:/wt"), {"version": 7, "packagesDir": ".\\lake-packages"}),
                         Path("C:/wt/lake-packages"))
        self.assertIsNone(pipeline.manifest_packages_dir(Path("C:/wt"), {"version": 7, "packagesDir": "C:/elsewhere"}))


class AllowlistTests(unittest.TestCase):
    def test_normalization_and_exclusions(self) -> None:
        for url in (MATHLIB, MATHLIB + ".git", MATHLIB + "/", "https://github.com/Leanprover-Community/Mathlib4.GIT/",
                    "https://github.com/leanprover/std4", "https://github.com/leanprover-community/LeanSearchClient"):
            self.assertTrue(pipeline.mirror_allowlisted(url), url)
        for url in (PROOFWIDGETS, "https://github.com/EdAyers/ProofWidgets4", "https://github.com/someone/mathlib4",
                    "https://codeberg.org/x/y.git"):
            self.assertFalse(pipeline.mirror_allowlisted(url), url)
        root = Path("C:/mirrors")
        self.assertEqual(pipeline.dependency_mirror_path(root, MATHLIB + ".git"),
                         pipeline.dependency_mirror_path(root, "https://github.com/Leanprover-Community/mathlib4/"))
        self.assertEqual(pipeline.dependency_mirror_path(root, MATHLIB).name,
                         "github.com__leanprover-community__mathlib4.git")

    def test_mirror_root_flag(self) -> None:
        self.assertIsNone(pipeline.dependency_mirror_root(Namespace()))
        self.assertIsNone(pipeline.dependency_mirror_root(Namespace(dep_mirror_root=Path("C:/m"), no_dep_mirror=True)))
        self.assertEqual(pipeline.dependency_mirror_root(Namespace(dep_mirror_root=Path("C:/m"))), Path("C:/m"))


class SeedingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.old, self.new = make_repo(self.root / "upstream")
        self.mirror_root = self.root / "mirrors"
        self.mirror = pipeline.dependency_mirror_path(self.mirror_root, MATHLIB)
        self.mirror.parent.mkdir(parents=True)
        git(self.root, "clone", "-q", "--bare", str(self.root / "upstream"), str(self.mirror))
        self.worktree = self.root / "wt"
        self.worktree.mkdir()
        self.log = self.root / "seed.log"
        watchdog = patch.object(pipeline, "NETWORK_WATCHDOG", pipeline.NetworkWatchdog(0, stop=threading.Event()))
        watchdog.start()
        self.addCleanup(watchdog.stop)

    def write_manifest(self, url: str, rev: str, name: str = "mathlib") -> None:
        (self.worktree / pipeline.LAKE_MANIFEST).write_text(json.dumps({
            "version": 7, "packagesDir": ".lake/packages",
            "packages": [{"type": "git", "url": url, "rev": rev, "name": name},
                         {"type": "git", "url": PROOFWIDGETS, "rev": self.new, "name": "proofwidgets"}],
        }), encoding="utf8")

    def test_seeds_clone_with_manifest_url_and_revision(self) -> None:
        url = MATHLIB + ".git"
        self.write_manifest(url, self.new)
        self.assertEqual(pipeline.seed_dependency_clones(self.worktree, self.log, self.mirror_root), 1)
        package = self.worktree / ".lake" / "packages" / "mathlib"
        self.assertEqual(git(package, "remote", "get-url", "origin"), url)
        self.assertEqual(git(package, "rev-parse", "HEAD"), self.new)
        self.assertEqual((package / "Demo.lean").read_text(encoding="utf8"), "example := bar\n")
        self.assertEqual(git(package, "status", "--porcelain"), "")
        self.assertIn(f"# seeded: mathlib {self.new} from mirror", self.log.read_text(encoding="utf8"))
        self.assertFalse((self.worktree / ".lake" / "packages" / "proofwidgets").exists())
        # Existing package directories are left to Lake.
        self.assertEqual(pipeline.seed_dependency_clones(self.worktree, self.log, self.mirror_root), 0)

    def test_missing_revision_and_unreachable_remote_fall_back_to_lake(self) -> None:
        git(self.mirror, "remote", "set-url", "origin", str(self.root / "does-not-exist"))
        self.write_manifest(MATHLIB, "0" * 40)
        self.assertEqual(pipeline.seed_dependency_clones(self.worktree, self.log, self.mirror_root), 0)
        self.assertFalse((self.worktree / ".lake").exists())
        self.assertIn("# not seeded: mathlib", self.log.read_text(encoding="utf8"))
        self.assertIsNone(pipeline.dependency_mirror_root(Namespace(no_dep_mirror=True, dep_mirror_root=self.mirror_root)))

    def test_mirror_is_created_on_first_use_and_refreshed_on_demand(self) -> None:
        upstream = self.root / "upstream"
        url = upstream.as_posix()
        allow = pipeline.DEPENDENCY_MIRROR_URLS | {pipeline.normalize_dependency_url(url)}
        with patch.object(pipeline, "DEPENDENCY_MIRROR_URLS", allow):
            self.write_manifest(url, self.old, name="Cli")
            self.assertEqual(pipeline.seed_dependency_clones(self.worktree, self.log, self.mirror_root), 1)
            mirror = pipeline.dependency_mirror_path(self.mirror_root, url)
            self.assertTrue((mirror / "HEAD").is_file())
            self.assertEqual(git(mirror, "config", "--get-all", "remote.origin.fetch"),
                             "+refs/heads/*:refs/heads/*\n+refs/tags/*:refs/tags/*")
            self.assertEqual(git(self.worktree / ".lake" / "packages" / "Cli", "rev-parse", "HEAD"), self.old)
            # A revision added upstream later is fetched on demand.
            (upstream / "Demo.lean").write_text("example := baz\n", encoding="utf8")
            git(upstream, "commit", "-q", "-am", "newer")
            newer = git(upstream, "rev-parse", "HEAD")
            self.assertFalse(pipeline.mirror_has_rev(mirror, newer))
            found, error = pipeline.ensure_dependency_mirror(self.mirror_root, url, newer, 60)
            self.assertEqual((found, error), (mirror, None))
            self.assertTrue(pipeline.mirror_has_rev(mirror, newer))
            self.assertEqual(pipeline.refresh_dependency_mirrors(self.mirror_root, 60), 2)

    def test_run_start_refresh_invoked_once(self) -> None:
        connection = pipeline.connect_database(self.root / "state" / "progress.sqlite3")
        self.addCleanup(connection.close)
        insert_pair(connection, "a0", "owner/a", 0, "o", "n")
        connection.commit()
        args = Namespace(acknowledge_untrusted_builds=True, lakeva="fake-lakeva", lake="fake-lake",
                         state_root=self.root / "state", keep_worktrees=False, retry_failed=False,
                         max_pairs=None, workers=1, dep_mirror_root=self.mirror_root, cache_timeout=42,
                         no_async_cleanup=True)
        with (
            patch.object(pipeline, "command_prefix", side_effect=lambda value: [value]),
            patch.object(pipeline, "cleanup_stale_worktrees"),
            patch.object(pipeline, "process_pair"),
            patch.object(pipeline, "refresh_dependency_mirrors") as refresh,
        ):
            pipeline.run_pairs(args, connection, [{"name": "owner/a", "path": self.root / "a"}])
        refresh.assert_called_once_with(self.mirror_root, 42)
        self.assertIsNone(pipeline.WORKTREE_TRASH)


class BackgroundCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.worktrees_root = self.root / "wt"
        self.trash = resources.WorktreeTrash(self.worktrees_root / ".trash", 10 * 2**30,
                                             onerror=pipeline._make_writable_and_retry, log=lambda _m: None)
        self.git_calls: list[tuple] = []
        patcher = patch.object(pipeline, "git", side_effect=lambda repo, *a, **k: self.git_calls.append(a))
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_worktree(self, pair_id: str = "pair0", name: str = "target") -> Path:
        worktree = self.worktrees_root / pair_id / name
        (worktree / ".lake" / "build").mkdir(parents=True)
        (worktree / ".lake" / "build" / "Demo.olean").write_bytes(b"x" * 100)
        (worktree / "Demo.lean").write_bytes(b"example := foo\n")
        return worktree

    def test_rename_then_background_delete(self) -> None:
        worktree = self.make_worktree()
        with patch.object(pipeline, "WORKTREE_TRASH", self.trash):
            pipeline.remove_worktree(self.root / "repo", worktree, self.worktrees_root)
        self.assertFalse(worktree.exists())
        entries = list((self.worktrees_root / ".trash").iterdir())
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].name.startswith("pair0-target-"))
        self.assertEqual(self.git_calls, [("worktree", "remove", "--force", str(worktree))])
        self.assertEqual(self.trash.queued_bytes(), 100 + len("example := foo\n"))
        self.trash.start()
        self.assertTrue(self.trash.drain(deadline=20))
        self.assertEqual(list((self.worktrees_root / ".trash").iterdir()), [])
        self.assertEqual(self.trash.deleted, 1)

    def test_failed_rename_deletes_inline(self) -> None:
        worktree = self.make_worktree()
        with (patch.object(pipeline, "WORKTREE_TRASH", self.trash),
              patch.object(self.trash, "discard", return_value=False)):
            pipeline.remove_worktree(self.root / "repo", worktree, self.worktrees_root)
        self.assertFalse(worktree.exists())
        self.assertFalse((self.worktrees_root / ".trash").exists())

    def test_gate_waits_for_janitor(self) -> None:
        self.trash.max_bytes = 0
        self.trash.discard(self.make_worktree(), "pair0-target")
        self.assertFalse(self.trash.wait_below(poll=0.05, deadline=0.3))
        self.trash.start()
        self.assertTrue(self.trash.wait_below(poll=0.05, deadline=20))
        self.assertEqual(self.trash.pending(), 0)

    def test_startup_sweep(self) -> None:
        leftover = self.worktrees_root / ".trash" / "pair9-old-20260101T000000000000"
        leftover.mkdir(parents=True)
        (leftover / "Demo.lean").write_text("x", encoding="utf8")
        connection = pipeline.connect_database(self.root / "state" / "progress.sqlite3")
        self.addCleanup(connection.close)
        args = Namespace(state_root=self.root / "state", worktrees_root=self.worktrees_root, keep_worktrees=False)
        with patch.object(pipeline, "WORKTREE_TRASH", self.trash):
            pipeline.cleanup_stale_worktrees(args, connection, {})
        self.assertEqual(self.trash.pending(), 1)
        self.assertTrue(leftover.exists())
        # Without the janitor (--no-async-cleanup) the sweep deletes inline.
        with patch.object(pipeline, "WORKTREE_TRASH", None):
            pipeline.cleanup_stale_worktrees(args, connection, {})
        self.assertFalse(leftover.exists())


class ForkReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        pipeline._LAKE_HELP.clear()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.primary_repo = self.root / "repos" / "primary"
        self.old, self.new = make_repo(self.primary_repo)
        self.fork_repo = self.root / "repos" / "fork"
        git(self.root, "clone", "-q", str(self.primary_repo), str(self.fork_repo))
        self.args = Namespace(
            state_root=self.root / "state", errors_root=self.root / "errors",
            repair_diffs_root=self.root / "diffs", worktrees_root=self.root / "wt",
            keep_worktrees=False, skip_cache=True, cache_timeout=10, adapt_timeout=10, build_timeout=10,
            native_first=False, validate_clean_pairs=False,
        )
        self.args.errors_root.mkdir()
        self.connection = pipeline.connect_database(self.args.state_root / "progress.sqlite3")
        self.addCleanup(self.connection.close)
        insert_pair(self.connection, "prim", "owner/primary", 0, self.old, self.new)
        insert_pair(self.connection, "fork", "other/fork", 3, self.old, self.new)
        self.connection.commit()
        self.primary_run = pipeline.pair_run_dir(self.args, "owner/primary", "prim")
        self.primary_run.mkdir(parents=True)

    def finish_primary(self, status="completed", outcome="build-errors", build_exit_code=1) -> None:
        body = "Demo.lean:1:11: error: unknown identifier 'foo'\nerror: build failed\n" if outcome == "build-errors" else "[1/1] Built Demo\n"
        (self.primary_run / "build.log").write_text(
            f"# started: x\n# cwd: {self.root / 'gone' / 'target'}\n# toolchain: {NEW}\n{body}"
            f"# toolchain_after: {NEW}\n# root_olean_count: 1\n# exit_code: {build_exit_code}\n", encoding="utf8")
        (self.primary_run / "cache.log").write_text("# exit_code: 0\n", encoding="utf8")
        (self.primary_run / "diagnostic-sources.json").write_text(json.dumps({"Demo.lean": "example := foo\n"}), encoding="utf8")
        primary = self.connection.execute("SELECT * FROM pairs WHERE pair_id='prim'").fetchone()
        outputs = pipeline.write_pair_error_files(self.args, primary, pipeline.parse_log(self.primary_run / "build.log"),
                                                  self.root / "gone" / "target",
                                                  source_fallback=pipeline.saved_source_reader(self.primary_run))
        self.connection.execute(
            "UPDATE pairs SET status=?,outcome=?,result_version=?,build_exit_code=?,cache_exit_code=-1,"
            "last_stage='parsed',error_file=? WHERE pair_id='prim'",
            (status, outcome, RESULT_FORMAT_VERSION, build_exit_code, str(outputs[0]) if outputs else None))
        self.connection.execute("INSERT INTO error_exports VALUES('prim',?,?,?)",
                                (RESULT_FORMAT_VERSION, len(outputs), pipeline.now()))
        for commit, version in ((self.old, OLD), (self.new, NEW)):
            log_dir = pipeline.native_log_dir(self.args, "owner/primary", commit)
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "build.log").write_text(native_log(version), encoding="utf8")
            (log_dir / pipeline.LAKE_MANIFEST).write_text('{"version": 7, "packages": []}', encoding="utf8")
            self.connection.execute(
                "INSERT INTO native_checks VALUES(?,?,?,'passed',-1,0,?,?)",
                ("owner/primary", commit, version, str(log_dir), pipeline.now()))
        self.connection.commit()

    def run_fork(self, add_worktree_error=AssertionError("the fork must not create a worktree")) -> dict:
        pair = dict(self.connection.execute("SELECT * FROM pairs WHERE pair_id='fork'").fetchone())
        with patch.object(pipeline, "add_worktree", side_effect=add_worktree_error):
            pipeline.process_pair(self.args, self.connection, pair, self.fork_repo, ["lakeva"], ["lake"])
        return dict(self.connection.execute("SELECT * FROM pairs WHERE pair_id='fork'").fetchone())

    def test_fork_is_completed_by_reuse_with_its_own_identity(self) -> None:
        self.finish_primary()
        row = self.run_fork()
        self.assertEqual((row["status"], row["outcome"], row["last_stage"]), ("completed", "build-errors", "reused-from-fork"))
        self.assertEqual((row["build_exit_code"], row["cache_exit_code"], row["result_version"]), (1, -1, RESULT_FORMAT_VERSION))
        self.assertIsNone(row["error"])
        fork_files = sorted(self.args.errors_root.glob("*__fork__error-*.json"))
        self.assertEqual(len(fork_files), 1)
        self.assertEqual(row["error_file"], str(fork_files[0]))
        self.assertTrue(fork_files[0].name.startswith("other__fork__pair-0003__"))
        report = json.loads(fork_files[0].read_text(encoding="utf8"))
        self.assertEqual((report["repo"], report["pair_id"], report["pair_index"]), ("other/fork", "fork", 3))
        self.assertEqual((report["old_hash"], report["new_hash"], report["old_version"], report["new_version"]),
                         (self.old, self.new, OLD, NEW))
        self.assertEqual((report["file"], report["error_line"], report["source_found"]), ("Demo.lean", "example := foo", True))
        primary_report = json.loads(next(self.args.errors_root.glob("*__prim__error-*.json")).read_text(encoding="utf8"))
        self.assertEqual(primary_report["repo"], "owner/primary")  # untouched
        self.assertEqual(primary_report["message"], report["message"])
        fork_run = pipeline.pair_run_dir(self.args, "other/fork", "fork")
        provenance = json.loads((fork_run / "provenance.json").read_text(encoding="utf8"))
        self.assertEqual((provenance["source_pair_id"], provenance["source_repo"]), ("prim", "owner/primary"))
        self.assertTrue((fork_run / "build.log").is_file() and (fork_run / "diagnostic-sources.json").is_file())
        export = self.connection.execute("SELECT format_version,error_count FROM error_exports WHERE pair_id='fork'").fetchone()
        self.assertEqual(tuple(export), (RESULT_FORMAT_VERSION, 1))
        with gzip.open(self.args.repair_diffs_root / "other__fork__fork.diff.gz", "rt", encoding="utf8") as handle:
            diff = handle.read()
        self.assertIn("# repo: other/fork", diff)
        self.assertIn("+example := bar", diff)
        for commit, version in ((self.old, OLD), (self.new, NEW)):
            native = self.connection.execute("SELECT * FROM native_checks WHERE repo='other/fork' AND commit_hash=?",
                                             (commit,)).fetchone()
            self.assertEqual((native["status"], native["version"]), ("passed", version))
            self.assertTrue(pipeline.native_pass_is_valid(native, version))
            log_dir = Path(native["log_dir"])
            self.assertEqual(log_dir, pipeline.native_log_dir(self.args, "other/fork", commit))
            self.assertTrue((log_dir / pipeline.LAKE_MANIFEST).is_file())
            self.assertEqual(json.loads((log_dir / "provenance.json").read_text(encoding="utf8"))["source_repo"], "owner/primary")
        self.assertEqual(pipeline.export_pairs_without_lean_errors(self.connection, self.root / "clean.jsonl"), 0)

    def test_clean_pair_reuse_counts_in_no_error_export(self) -> None:
        self.finish_primary(outcome="build-succeeded", build_exit_code=0)
        row = self.run_fork()
        self.assertEqual((row["status"], row["outcome"], row["last_stage"]), ("completed", "build-succeeded", "reused-from-fork"))
        self.assertEqual(list(self.args.errors_root.glob("*__fork__*")), [])
        output = self.root / "clean.jsonl"
        self.assertEqual(pipeline.export_pairs_without_lean_errors(self.connection, output), 2)
        self.assertIn("other/fork", output.read_text(encoding="utf8"))

    def test_inconclusive_primary_is_not_reused(self) -> None:
        self.finish_primary(status="build_inconclusive", outcome="adapted-build-infrastructure-failure")
        row = self.run_fork(RuntimeError("normal processing started"))
        self.assertEqual(row["status"], "pipeline_failed")
        self.assertIn("normal processing started", row["error"])
        self.assertFalse((pipeline.pair_run_dir(self.args, "other/fork", "fork") / "provenance.json").exists())
        # The passing native checks are commit-level evidence and are still reused.
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM native_checks WHERE repo='other/fork' AND status='passed'").fetchone()[0], 2)

    def test_reuse_is_opt_out(self) -> None:
        self.finish_primary()
        self.args.no_fork_reuse = True
        row = self.run_fork(RuntimeError("normal processing started"))
        self.assertEqual(row["status"], "pipeline_failed")

    def test_natives_that_cannot_be_reused_fall_through(self) -> None:
        self.finish_primary()
        self.connection.execute("DELETE FROM native_checks WHERE commit_hash=?", (self.new,))
        self.connection.commit()
        row = self.run_fork(RuntimeError("normal processing started"))
        self.assertEqual(row["status"], "pipeline_failed")
        self.assertEqual(list(self.args.errors_root.glob("*__fork__*")), [])
        self.assertFalse((pipeline.pair_run_dir(self.args, "other/fork", "fork") / "provenance.json").exists())

    def test_duplicate_adapted_timeout_is_not_rebuilt(self) -> None:
        self.finish_primary(status="build_inconclusive", outcome="adapted-build-incomplete", build_exit_code=124)
        row = self.run_fork()
        self.assertEqual((row["status"], row["outcome"], row["last_stage"]),
                         ("build_inconclusive", "adapted-build-incomplete", "skipped-duplicate-timeout"))
        self.assertEqual(row["build_exit_code"], 124)
        self.assertIn("# skipped: duplicate of owner/primary/prim timed out", row["error"])
        fork_run = pipeline.pair_run_dir(self.args, "other/fork", "fork")
        self.assertTrue((fork_run / "provenance.json").is_file())
        self.assertIn("# exit_code: 124", (fork_run / "build.log").read_text(encoding="utf8"))
        self.args.no_timeout_prediction = True
        self.connection.execute("UPDATE pairs SET status='pending' WHERE pair_id='fork'")
        self.connection.commit()
        self.assertEqual(self.run_fork(RuntimeError("normal processing started"))["status"], "pipeline_failed")

    def test_duplicate_native_timeout_is_copied(self) -> None:
        log_dir = self.root / "native-prim"
        log_dir.mkdir()
        (log_dir / "cache.log").write_text("# exit_code: 0\n", encoding="utf8")
        self.connection.execute("INSERT INTO native_checks VALUES(?,?,?,'inconclusive',0,124,?,?)",
                                ("owner/primary", self.old, OLD, str(log_dir), pipeline.now()))
        self.connection.commit()
        status = pipeline.cached_native_status(self.connection, "other/fork", self.old, args=self.args, version=OLD)
        self.assertEqual(status, "inconclusive")
        row = self.connection.execute("SELECT * FROM native_checks WHERE repo='other/fork'").fetchone()
        self.assertEqual((row["build_exit_code"], row["cache_exit_code"]), (124, 0))
        copied = Path(row["log_dir"]) / "build.log"
        self.assertIn(f"# skipped: duplicate of owner/primary/{self.old} timed out", copied.read_text(encoding="utf8"))
        self.assertTrue((Path(row["log_dir"]) / "provenance.json").is_file())
        # Other inconclusive results are never copied; a different toolchain never matches.
        self.assertIsNone(pipeline.cached_native_status(self.connection, "third/repo", self.old, args=self.args, version=NEW))
        self.args.no_timeout_prediction = True
        self.assertIsNone(pipeline.cached_native_status(self.connection, "third/repo", self.old, args=self.args, version=OLD))

    def test_scheduler_defers_identical_pair_running_elsewhere(self) -> None:
        def pair(pair_id, repo, old, new):
            return {"pair_id": pair_id, "repo": repo, "old_hash": old, "new_hash": new,
                    "old_version": OLD, "new_version": NEW, "pair_index": 0}
        scheduler = resources.PairScheduler(
            [pair("a0", "a", "h1", "h2"), pair("b0", "b", "h1", "h2"), pair("c0", "c", "h3", "h4")],
            stop=threading.Event())
        first = scheduler.next()
        second = scheduler.next()
        self.assertEqual((first["pair_id"], second["pair_id"]), ("a0", "c0"))
        scheduler.done(first)
        self.assertEqual(scheduler.next()["pair_id"], "b0")


class TimeoutPredictionTests(unittest.TestCase):
    def setUp(self) -> None:
        pipeline._LAKE_HELP.clear()
        pipeline._KEEP_TOOLCHAIN_SUPPORT.clear()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        script = self.root / "fake_lake.py"
        script.write_text(FAKE_LAKE, encoding="utf8")
        self.lake = [sys.executable, str(script)]
        self.worktree = self.root / "wt"
        self.worktree.mkdir()
        self.set_manifest(["mathlib", "proofwidgets", "aesop"])
        (self.worktree / "lean-toolchain").write_text(NEW + "\n", encoding="utf8")
        self.args = Namespace(state_root=self.root / "state", skip_cache=False, native_attempts=1,
                              cache_timeout=30, native_timeout=30)
        self.log = self.root / "predict.log"

    def set_manifest(self, names: list[str]) -> None:
        (self.worktree / pipeline.LAKE_MANIFEST).write_text(json.dumps({
            "version": 7, "packagesDir": ".lake/packages",
            "packages": [{"type": "git", "url": f"https://example.invalid/{n}", "rev": "r", "name": n} for n in names],
        }), encoding="utf8")

    def predict(self, version=NEW, cache_rc=0, exit_code="3") -> bool:
        (self.worktree / "lean-toolchain").write_text(version + "\n", encoding="utf8")
        with patch.dict(os.environ, {"FAKE_LAKE_NO_BUILD_EXIT": exit_code}):
            return pipeline.proofwidgets_cascade_predicted(self.args, self.lake, self.worktree, version, self.log, cache_rc)

    def test_cascade_predicted_from_no_build_exit(self) -> None:
        self.assertTrue(self.predict())
        text = self.log.read_text(encoding="utf8")
        self.assertIn("+ProofWidgets.Compat --no-build", text)
        self.assertIn("--keep-toolchain", text)
        self.assertIn("# exit_code: 3", text)
        self.assertIn("# predicted: ProofWidgets is out of date", text)

    def test_up_to_date_proofwidgets_builds_normally(self) -> None:
        self.assertFalse(self.predict(exit_code="0"))
        self.assertIn("# predicted: no cascade (exit 0)", self.log.read_text(encoding="utf8"))
        # Other exit codes (an unknown module, a timeout) are not a prediction either.
        self.assertFalse(self.predict(exit_code="1"))

    def test_prediction_skipped_when_not_applicable(self) -> None:
        self.assertFalse(self.predict(version="leanprover/lean4:v4.16.0"))
        self.assertFalse(self.predict(version="leanprover/lean4:v4.20.0-rc1"))
        self.assertFalse(self.predict(version="leanprover/lean4:nightly-2024-05-01"))
        self.assertFalse(self.predict(cache_rc=-1))
        self.assertFalse(self.predict(cache_rc=1))
        self.set_manifest(["mathlib", "aesop"])
        self.assertFalse(self.predict())
        self.set_manifest(["proofwidgets"])
        self.assertFalse(self.predict())
        self.set_manifest(["mathlib", "proofwidgets"])
        self.args.no_timeout_prediction = True
        self.assertFalse(self.predict())
        del self.args.no_timeout_prediction
        with patch.object(pipeline, "lake_help_text", return_value="OPTIONS: --keep-toolchain"):
            self.assertFalse(self.predict())
        self.assertFalse(self.log.exists())
        self.assertTrue(self.predict(version="leanprover/lean4:v4.15.0-rc2"))

    def test_native_check_skips_build_and_pair_is_retryable(self) -> None:
        connection = pipeline.connect_database(self.root / "state" / "progress.sqlite3")
        self.addCleanup(connection.close)
        insert_pair(connection, "pair0", "owner/repo", 0, "old", "deadbeef")
        connection.commit()
        with patch.dict(os.environ, {"FAKE_LAKE_NO_BUILD_EXIT": "3"}):
            status = pipeline.native_check(self.args, connection, "owner/repo", "deadbeef", NEW, self.worktree, self.lake)
        self.assertEqual(status, PREDICTED_TIMEOUT_OUTCOME)
        log_dir = pipeline.native_log_dir(self.args, "owner/repo", "deadbeef")
        self.assertTrue((log_dir / "cache.log").is_file() and (log_dir / "predict.log").is_file())
        self.assertFalse((log_dir / "build.log").exists())
        row = connection.execute("SELECT * FROM native_checks WHERE repo='owner/repo'").fetchone()
        self.assertEqual((row["status"], row["cache_exit_code"], row["build_exit_code"]), ("inconclusive", 0, -1))
        pair = connection.execute("SELECT * FROM pairs WHERE pair_id='pair0'").fetchone()
        pipeline.record_native_rejection(connection, pair, "new", status)
        pair = connection.execute("SELECT * FROM pairs WHERE pair_id='pair0'").fetchone()
        self.assertEqual((pair["status"], pair["outcome"]), ("native_inconclusive", PREDICTED_TIMEOUT_OUTCOME))
        self.assertIn("predict.log", pair["error"])
        connection.execute("DELETE FROM native_checks WHERE status IN ('failed','inconclusive')")
        connection.commit()
        with patch.dict(os.environ, {"FAKE_LAKE_NO_BUILD_EXIT": "0"}):
            status = pipeline.native_check(self.args, connection, "owner/repo", "deadbeef", NEW, self.worktree, self.lake)
        self.assertNotEqual(status, PREDICTED_TIMEOUT_OUTCOME)
        self.assertTrue((log_dir / "build.log").is_file())

    def test_adapted_build_is_skipped_and_recorded(self) -> None:
        connection = pipeline.connect_database(self.root / "state" / "progress.sqlite3")
        self.addCleanup(connection.close)
        insert_pair(connection, "pair0", "owner/repo", 0, "old", "new")
        connection.commit()
        args = Namespace(state_root=self.root / "state", errors_root=self.root / "errors", worktrees_root=self.root / "w",
                         keep_worktrees=False, skip_cache=False, adapt_timeout=10, build_timeout=10, cache_timeout=10,
                         native_first=False, validate_clean_pairs=False)
        args.errors_root.mkdir()
        manifest = (self.worktree / pipeline.LAKE_MANIFEST).read_text(encoding="utf8")
        events: list[str] = []

        def fake_add_worktree(_repo, tree, commit, _root):
            tree.mkdir(parents=True, exist_ok=True)
            (tree / "lean-toolchain").write_text(OLD if commit == "old" else NEW, encoding="utf8")
            (tree / pipeline.LAKE_MANIFEST).write_text(manifest, encoding="utf8")

        def fake_run_logged(command, cwd, log_path, _timeout, *, toolchain=None):
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if "adapt" in command:
                (Path(command[-1]) / "lean-toolchain").write_text(NEW, encoding="utf8")
                log_path.write_text("# exit_code: 0\n", encoding="utf8")
                return 0
            if "--no-build" in command:
                events.append("predict")
                log_path.write_text("# exit_code: 3\n", encoding="utf8")
                return 3
            events.append("cache" if "cache" in command else "build")
            log_path.write_text(f"# toolchain: {toolchain}\n# toolchain_after: {toolchain}\n# root_olean_count: 1\n# exit_code: 0\n",
                                encoding="utf8")
            return 0

        pair = dict(connection.execute("SELECT * FROM pairs WHERE pair_id='pair0'").fetchone())
        with (
            patch.object(pipeline, "git", return_value=Namespace(returncode=0, stdout="")),
            patch.object(pipeline, "lake_supports_keep_toolchain", return_value=False),
            patch.object(pipeline, "lake_help_text", return_value="--no-build"),
            patch.object(pipeline, "add_worktree", side_effect=fake_add_worktree),
            patch.object(pipeline, "restore_pristine_worktree_with_retries"),
            patch.object(pipeline, "run_logged", side_effect=fake_run_logged),
        ):
            pipeline.process_pair(args, connection, pair, self.root / "repo", ["lakeva"], ["lake"])
        row = connection.execute("SELECT * FROM pairs WHERE pair_id='pair0'").fetchone()
        self.assertEqual(events, ["cache", "predict"])
        self.assertEqual((row["status"], row["outcome"], row["build_exit_code"], row["result_version"]),
                         ("build_inconclusive", PREDICTED_TIMEOUT_OUTCOME, -1, RESULT_FORMAT_VERSION))
        self.assertIn("# predicted: ProofWidgets is out of date", row["error"])
        run_dir = pipeline.pair_run_dir(args, "owner/repo", "pair0")
        self.assertTrue((run_dir / "predict.log").is_file() and (run_dir / "lean-toolchain").is_file())
        self.assertFalse((run_dir / "build.log").exists())
        # --repair-results keeps the record instead of filing it as a missing build log.
        connection.execute("UPDATE pairs SET result_version=NULL WHERE pair_id='pair0'")
        for commit, version in (("old", OLD), ("new", NEW)):
            log_dir = self.root / ("native-" + commit)
            log_dir.mkdir()
            (log_dir / "build.log").write_text(native_log(version), encoding="utf8")
            connection.execute("INSERT INTO native_checks VALUES(?,?,?,'passed',-1,0,?,?)",
                               ("owner/repo", commit, version, str(log_dir), pipeline.now()))
        connection.commit()
        pipeline.migrate_legacy_error_reports(args, connection, [{"name": "owner/repo", "path": self.root / "repo"}])
        row = connection.execute("SELECT * FROM pairs WHERE pair_id='pair0'").fetchone()
        self.assertEqual((row["status"], row["outcome"], row["result_version"]),
                         ("build_inconclusive", PREDICTED_TIMEOUT_OUTCOME, RESULT_FORMAT_VERSION))


if __name__ == "__main__":
    unittest.main()
