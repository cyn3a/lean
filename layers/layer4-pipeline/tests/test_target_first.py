from __future__ import annotations

from argparse import Namespace
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import run_pipeline as pipeline
import resource_management as resources
from test_parallel_pipeline import insert_pair

OLD = "leanprover/lean4:v4.0.0"
NEW = "leanprover/lean4:v4.1.0"
SOURCE = "theorem demo : True := by\n  exact foo\n"


class TargetFirstTests(unittest.TestCase):
    def setUp(self) -> None:
        pipeline._LAKE_HELP.clear()
        probe = patch.object(pipeline, "lake_supports_keep_toolchain", return_value=False)
        probe.start()
        self.addCleanup(probe.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.connection = pipeline.connect_database(self.root / "progress.sqlite3")
        self.addCleanup(self.connection.close)
        insert_pair(self.connection, "pair0", "owner/repo", 0, "old", "new")
        self.connection.commit()
        self.args = Namespace(
            state_root=self.root / "state",
            errors_root=self.root / "errors",
            worktrees_root=self.root / "wt",
            keep_worktrees=False,
            skip_cache=True,
            adapt_timeout=10,
            build_timeout=10,
            cache_timeout=10,
            native_first=False,
            validate_clean_pairs=False,
        )
        self.args.errors_root.mkdir()
        self.with_manifest = {"old": True, "new": True}
        self.adapted_errors = True
        self.native_result = "passed"
        self.native_calls: list[str] = []
        self.events: list[str] = []

    def fake_add_worktree(self, _repo, tree: Path, commit: str, _root) -> None:
        tree.mkdir(parents=True, exist_ok=True)
        (tree / "lean-toolchain").write_text(OLD if commit == "old" else NEW, encoding="utf8")
        (tree / "Demo.lean").write_text(SOURCE, encoding="utf8")
        if self.with_manifest[commit]:
            (tree / pipeline.LAKE_MANIFEST).write_text('{"packages": []}', encoding="utf8")
        self.events.append(f"add:{tree.name}")

    def fake_run_logged(self, command, cwd, log_path, _timeout, *, toolchain=None):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if "adapt" in command:
            target = Path(command[-1])
            (target / "lean-toolchain").write_text(NEW, encoding="utf8")
            log_path.write_text("adapted\n# exit_code: 0\n", encoding="utf8")
            self.events.append("adapt")
            return 0
        self.events.append("target-build")
        body = "Demo.lean:2:8: error: unknown identifier 'foo'\nerror: build failed\n" if self.adapted_errors else ""
        exit_code = 1 if self.adapted_errors else 0
        log_path.write_text(
            f"# toolchain: {toolchain}\n{body}# toolchain_after: {toolchain}\n"
            f"# root_olean_count: 1\n# exit_code: {exit_code}\n",
            encoding="utf8",
        )
        return exit_code

    def fake_native_check(self, _args, _connection, _repo, commit_hash, _version, worktree, _lake):
        self.native_calls.append(commit_hash)
        self.events.append(f"native:{commit_hash}")
        if not (worktree / pipeline.LAKE_MANIFEST).is_file():
            (worktree / pipeline.LAKE_MANIFEST).write_text('{"packages": []}', encoding="utf8")
        return self.native_result

    def run_pair(self) -> dict:
        pair = dict(self.connection.execute("SELECT * FROM pairs WHERE pair_id='pair0'").fetchone())
        with (
            patch.object(pipeline, "git", return_value=Namespace(returncode=0, stdout="")),
            patch.object(pipeline, "add_worktree", side_effect=self.fake_add_worktree),
            patch.object(pipeline, "restore_pristine_worktree_with_retries"),
            patch.object(pipeline, "run_logged", side_effect=self.fake_run_logged),
            patch.object(pipeline, "native_check", side_effect=self.fake_native_check),
        ):
            pipeline.process_pair(self.args, self.connection, pair, self.root / "repo",
                                  ["lakeva"], ["lake"])
        return dict(self.connection.execute("SELECT * FROM pairs WHERE pair_id='pair0'").fetchone())

    def test_clean_adapted_build_skips_native_builds(self) -> None:
        self.adapted_errors = False
        row = self.run_pair()
        self.assertEqual(self.native_calls, [])
        self.assertEqual((row["status"], row["outcome"]), ("unvalidated", "build-succeeded"))
        self.assertEqual(list(self.args.errors_root.iterdir()), [])

    def test_clean_pairs_can_still_be_validated(self) -> None:
        self.adapted_errors = False
        self.args.validate_clean_pairs = True
        row = self.run_pair()
        self.assertEqual(self.native_calls, ["old", "new"])
        self.assertEqual((row["status"], row["outcome"]), ("completed", "build-succeeded"))

    def test_lean_errors_trigger_native_validation_after_target_removal(self) -> None:
        row = self.run_pair()
        self.assertEqual(self.native_calls, ["old", "new"])
        self.assertLess(self.events.index("target-build"), self.events.index("native:old"))
        self.assertEqual((row["status"], row["outcome"]), ("completed", "build-errors"))
        reports = [json.loads(path.read_text(encoding="utf8")) for path in self.args.errors_root.iterdir()]
        self.assertEqual(len(reports), 1)
        # The target checkout was removed before the native builds, so the
        # context must come from the saved diagnostic-source snapshot.
        self.assertTrue(reports[0]["source_found"])
        self.assertEqual(reports[0]["error_line"], "  exact foo")

    def test_native_failure_discards_errors_found_first(self) -> None:
        self.native_result = "failed"
        row = self.run_pair()
        self.assertEqual(self.native_calls, ["old"])
        self.assertEqual((row["status"], row["outcome"]),
                         ("discarded_native", "old-commit-does-not-build-natively"))
        self.assertEqual(list(self.args.errors_root.iterdir()), [])

    def test_missing_manifest_falls_back_to_native_first(self) -> None:
        self.with_manifest["new"] = False
        self.adapted_errors = False
        row = self.run_pair()
        self.assertEqual(self.native_calls, ["old", "new"])
        self.assertLess(self.events.index("native:new"), self.events.index("adapt"))
        self.assertEqual(row["status"], "completed")

    def test_native_timeout_is_not_retried(self) -> None:
        worktree = self.root / "native-tree"
        worktree.mkdir()
        args = Namespace(state_root=self.root / "state", skip_cache=True, native_attempts=3,
                         cache_timeout=10, native_timeout=10)
        calls = 0

        def timed_out(_command, cwd, log_path, _timeout, *, toolchain=None):
            nonlocal calls
            calls += 1
            log_path.parent.mkdir(parents=True, exist_ok=True)
            (cwd / "lean-toolchain").write_text(toolchain, encoding="utf8")
            log_path.write_text("error: command timed out after 10 seconds\n# exit_code: 124\n", encoding="utf8")
            return 124

        with patch.object(pipeline, "run_logged", side_effect=timed_out):
            status = pipeline.native_check(args, self.connection, "owner/repo", "deadbeef", NEW, worktree, ["lake"])
        self.assertEqual(status, "inconclusive")
        self.assertEqual(calls, 1)
        log_dir = pipeline.native_log_dir(args, "owner/repo", "deadbeef")
        self.assertTrue((log_dir / "build.log").is_file())
        self.assertFalse((log_dir / "build-attempt-01.log").exists())

    def test_cache_get_is_skipped_without_mathlib(self) -> None:
        worktree = self.root / "plain"
        worktree.mkdir()
        (worktree / pipeline.LAKE_MANIFEST).write_text('{"packages": [{"name": "batteries"}]}', encoding="utf8")
        args = Namespace(skip_cache=False, cache_timeout=10)
        with patch.object(pipeline, "run_logged", side_effect=AssertionError("must not run")):
            self.assertEqual(pipeline.run_cache_get(args, ["lake"], worktree, self.root / "c.log", NEW), -1)
        (worktree / pipeline.LAKE_MANIFEST).write_text('{"packages": [{"name": "mathlib"}]}', encoding="utf8")
        with patch.object(pipeline, "run_logged", return_value=0) as run:
            self.assertEqual(pipeline.run_cache_get(args, ["lake"], worktree, self.root / "c.log", NEW), 0)
            run.assert_called_once()


class SchedulingTests(unittest.TestCase):
    @staticmethod
    def pair(pair_id, repo, old, new, index=0):
        return {"pair_id": pair_id, "repo": repo, "old_version": old, "new_version": new,
                "pair_index": index, "old_hash": pair_id + "o", "new_hash": pair_id + "n"}

    def test_priority_buckets(self) -> None:
        lean_change = {"meaningful_paths_sample": ["A.lean"], "meaningful_path_count": 1}
        no_lean = {"meaningful_paths_sample": ["README.md"], "meaningful_path_count": 1}
        p = self.pair
        self.assertEqual(resources.pair_priority(p("a", "r", "leanprover/lean4:v4.20.0", "leanprover/lean4:v4.21.0"), lean_change), 0)
        self.assertEqual(resources.pair_priority(p("a", "r", "leanprover/lean4:v4.9.0", "leanprover/lean4:v4.20.0"), lean_change), 1)
        self.assertEqual(resources.pair_priority(p("a", "r", "leanprover/lean4:nightly-2023-01-01", "leanprover/lean4:v4.1.0"), lean_change), 2)
        self.assertEqual(resources.pair_priority(p("a", "r", "leanprover/lean4:v4.20.0", "leanprover/lean4:v4.20.1"), lean_change), 3)
        self.assertEqual(resources.pair_priority(p("a", "r", "leanprover/lean4:v4.20.0", "leanprover/lean4:v4.21.0"), no_lean), 3)
        self.assertEqual(resources.pair_priority(p("a", "r", "v4.20.0", "v4.21.0"), None), 0)

    def test_scheduler_serializes_repositories_and_drains(self) -> None:
        stop = threading.Event()
        pairs = [self.pair("a0", "a", "x", "y"), self.pair("a1", "a", "x", "y", 1), self.pair("b0", "b", "x", "y")]
        drained = False
        scheduler = resources.PairScheduler(pairs, stop=stop, should_drain=lambda: drained)
        first = scheduler.next()
        second = scheduler.next()
        self.assertEqual((first["pair_id"], second["pair_id"]), ("a0", "b0"))
        self.assertEqual(scheduler.versions_in_use(0), {"x", "y"})
        scheduler.done(first)
        drained = True
        self.assertIsNone(scheduler.next())


class DiskHygieneTests(unittest.TestCase):
    def test_toolchain_janitor_evicts_least_recent_unprotected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / "settings.toml").write_text('default_toolchain = "leanprover/lean4:v4.1.0"\n', encoding="utf8")
            versions = [f"leanprover/lean4:v4.{n}.0" for n in range(1, 6)] + ["leanprover/lean4:nightly-2023-02-02"]
            for version in versions + ["leanprover/lean4:v4.99.0"]:
                directory = home / "toolchains" / resources.elan_toolchain_dirname(version)
                (directory / "bin").mkdir(parents=True)
            self.assertTrue((home / "toolchains" / "leanprover--lean4-nightly---nightly-2023-02-02").is_dir())
            janitor = resources.ToolchainJanitor(versions, 2, home=home, log=lambda _m: None)
            janitor.touch(["leanprover/lean4:v4.2.0"])
            time.sleep(0.01)
            janitor.touch(["leanprover/lean4:v4.3.0"])
            removed = janitor.evict(keep_versions=["leanprover/lean4:v4.4.0"])
            remaining = {entry.name for entry in (home / "toolchains").iterdir()}
            self.assertEqual(removed, 3)
            self.assertEqual(remaining, {
                "leanprover--lean4---v4.1.0",   # elan default
                "leanprover--lean4---v4.3.0",   # most recently used
                "leanprover--lean4---v4.4.0",   # needed by an upcoming pair
                "leanprover--lean4---v4.99.0",  # not a pipeline toolchain
            })

    def test_ltar_cache_pruned_oldest_first(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for index in range(10):
                path = directory / f"{index:02d}.ltar"
                path.write_bytes(b"x" * 100)
                os.utime(path, (1000 + index, 1000 + index))
            (directory / "keep.txt").write_bytes(b"y" * 1000)
            files, size = resources.prune_ltar_cache(directory, 500)
            self.assertEqual((files, size), (6, 600))
            self.assertEqual(sorted(p.name for p in directory.glob("*.ltar")), ["06.ltar", "07.ltar", "08.ltar", "09.ltar"])
            self.assertTrue((directory / "keep.txt").exists())

    def test_cache_gate_blocks_pruning_while_cache_get_runs(self) -> None:
        gate = resources.CacheGate()
        operation = MagicMock()
        with gate:
            self.assertFalse(gate.try_exclusive(operation))
        self.assertTrue(gate.try_exclusive(operation))
        operation.assert_called_once()


if __name__ == "__main__":
    unittest.main()
