from __future__ import annotations

from argparse import Namespace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import run_pipeline as pipeline
from test_parallel_pipeline import insert_pair

DNS_ERROR = (
    "git -c core.longpaths=true worktree add --detach C:\\t\\old abc failed in C:\\repo: "
    "Preparing worktree (detached HEAD abc)\n"
    "fatal: unable to access 'https://github.com/o/r.git/': Could not resolve host: github.com\n"
    "fatal: could not fetch 1234 from promisor remote"
)
INVALID_PATH_ERROR = (
    "git -c core.longpaths=true worktree add --detach C:\\t\\old abc failed in C:\\repo: "
    "Preparing worktree (detached HEAD abc)\n"
    "error: invalid path 'Leanwuzla/Aux.lean'\n"
    "fatal: Could not reset index file to revision 'HEAD'."
)


class ClassificationTests(unittest.TestCase):
    def test_network_text(self) -> None:
        self.assertTrue(pipeline.network_failure_text(DNS_ERROR))
        self.assertTrue(pipeline.network_failure_text(
            "fatal: unable to access 'https://github.com/x/y.git/': Failed to connect to "
            "github.com port 443 after 21057 ms: Could not connect to server"))
        self.assertFalse(pipeline.network_failure_text(INVALID_PATH_ERROR))
        self.assertFalse(pipeline.network_failure_text("error: no lakefile.lean or lakefile.toml found"))
        self.assertFalse(pipeline.network_failure_text(None))

    def test_invalid_path_text(self) -> None:
        self.assertTrue(pipeline.invalid_path_text(INVALID_PATH_ERROR))
        self.assertFalse(pipeline.invalid_path_text(DNS_ERROR))

    def test_clone_command_reproduces_collection_modes(self) -> None:
        path = Path("C:/repos/o/r")
        blobless = pipeline.clone_command("https://github.com/o/r", path, "blobless-no-checkout")
        self.assertIn("--filter=blob:none", blobless)
        self.assertIn("--no-checkout", blobless)
        full = pipeline.clone_command("https://github.com/o/r", path, "full-or-recovered")
        self.assertNotIn("--filter=blob:none", full)
        self.assertNotIn("--no-checkout", full)
        self.assertEqual(full[-2:], ["https://github.com/o/r", str(path)])
        forced = pipeline.clone_command("https://github.com/o/r", path, "full-or-recovered", no_checkout=True)
        self.assertIn("--no-checkout", forced)


class WatchdogTests(unittest.TestCase):
    def test_trips_only_when_probe_fails(self) -> None:
        stop = threading.Event()
        watchdog = pipeline.NetworkWatchdog(2, stop=stop, probe=lambda: False)
        self.assertFalse(watchdog.failure("one"))
        self.assertFalse(stop.is_set())
        self.assertTrue(watchdog.failure("two"))
        self.assertTrue(stop.is_set())
        self.assertTrue(watchdog.tripped)

    def test_reachable_host_resets_the_count(self) -> None:
        stop = threading.Event()
        probes = []
        watchdog = pipeline.NetworkWatchdog(2, stop=stop, probe=lambda: probes.append(1) or True)
        watchdog.failure("one")
        self.assertFalse(watchdog.failure("two"))
        self.assertEqual(len(probes), 1)
        self.assertEqual(watchdog.consecutive, 0)
        self.assertFalse(stop.is_set())

    def test_success_resets_and_zero_disables(self) -> None:
        stop = threading.Event()
        watchdog = pipeline.NetworkWatchdog(2, stop=stop, probe=lambda: False)
        watchdog.failure("one")
        watchdog.success()
        self.assertFalse(watchdog.failure("two"))
        disabled = pipeline.NetworkWatchdog(0, stop=stop, probe=lambda: False)
        for _ in range(10):
            self.assertFalse(disabled.failure("x"))
        self.assertFalse(stop.is_set())


class ProcessPairTests(unittest.TestCase):
    def setUp(self) -> None:
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
            keep_worktrees=True,
            skip_cache=True,
            adapt_timeout=10,
            build_timeout=10,
            cache_timeout=10,
            native_first=False,
            validate_clean_pairs=False,
        )
        self.args.errors_root.mkdir()
        pipeline.NETWORK_WATCHDOG.configure(0)
        self.addCleanup(pipeline.NETWORK_WATCHDOG.configure, 5)

    def run_pair_with_worktree_error(self, message: str) -> dict:
        pair = dict(self.connection.execute("SELECT * FROM pairs WHERE pair_id='pair0'").fetchone())
        with (
            patch.object(pipeline, "git", return_value=Namespace(returncode=0, stdout="")),
            patch.object(pipeline, "add_worktree", side_effect=RuntimeError(message)),
            patch.object(pipeline, "restore_pristine_worktree_with_retries"),
        ):
            pipeline.process_pair(self.args, self.connection, pair, self.root / "repo", ["lakeva"], ["lake"])
        return dict(self.connection.execute("SELECT * FROM pairs WHERE pair_id='pair0'").fetchone())

    def test_network_failure_keeps_the_pair_pending(self) -> None:
        row = self.run_pair_with_worktree_error(DNS_ERROR)
        self.assertEqual((row["status"], row["outcome"]), ("pending", pipeline.NETWORK_FAILURE_OUTCOME))
        self.assertEqual(row["last_stage"], "network-failure")
        self.assertIn("Could not resolve host", row["error"])

    def test_invalid_path_is_ineligible_not_a_pipeline_error(self) -> None:
        row = self.run_pair_with_worktree_error(INVALID_PATH_ERROR)
        self.assertEqual((row["status"], row["outcome"]), ("ineligible_pair", pipeline.INVALID_PATH_OUTCOME))

    def test_other_errors_remain_pipeline_failures(self) -> None:
        row = self.run_pair_with_worktree_error("fatal: 'C:/x/old' already exists")
        self.assertEqual((row["status"], row["outcome"]), ("pipeline_failed", "pipeline-error"))

    def test_adapter_network_failure_keeps_the_pair_pending(self) -> None:
        pair = dict(self.connection.execute("SELECT * FROM pairs WHERE pair_id='pair0'").fetchone())

        def fake_add_worktree(_repo, tree: Path, commit: str, _root) -> None:
            tree.mkdir(parents=True, exist_ok=True)
            (tree / "lean-toolchain").write_text("leanprover/lean4:v4.0.0" if commit == "old" else "leanprover/lean4:v4.1.0")
            (tree / pipeline.LAKE_MANIFEST).write_text('{"packages": []}')

        def fake_run_logged(command, cwd, log_path, _timeout, *, toolchain=None):
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                "error: external command 'git' exited with code 128\n"
                "fatal: unable to access 'https://github.com/d/e.git/': Could not resolve host: github.com\n"
                "# exit_code: 1\n")
            return 1

        with (
            patch.object(pipeline, "git", return_value=Namespace(returncode=0, stdout="")),
            patch.object(pipeline, "add_worktree", side_effect=fake_add_worktree),
            patch.object(pipeline, "restore_pristine_worktree_with_retries"),
            patch.object(pipeline, "run_logged", side_effect=fake_run_logged),
            patch.object(pipeline, "lake_supports_keep_toolchain", return_value=False),
        ):
            pipeline.process_pair(self.args, self.connection, pair, self.root / "repo", ["lakeva"], ["lake"])
        row = dict(self.connection.execute("SELECT * FROM pairs WHERE pair_id='pair0'").fetchone())
        self.assertEqual((row["status"], row["outcome"]), ("pending", pipeline.NETWORK_FAILURE_OUTCOME))


class CloneRuleTests(unittest.TestCase):
    def test_paused_and_failed_pairs_keep_their_clone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = pipeline.connect_database(Path(directory) / "progress.sqlite3")
            try:
                insert_pair(connection, "p0", "owner/repo", 0, "a", "b")
                for status in ("native_inconclusive", "build_inconclusive", "pipeline_failed", "pending"):
                    connection.execute("UPDATE pairs SET status=? WHERE pair_id='p0'", (status,))
                    connection.commit()
                    self.assertFalse(pipeline.repository_finished(connection, "owner/repo"), status)
                for status in ("completed", "adapt_failed", "discarded_native", "ineligible_pair", "unvalidated"):
                    connection.execute("UPDATE pairs SET status=? WHERE pair_id='p0'", (status,))
                    connection.commit()
                    self.assertTrue(pipeline.repository_finished(connection, "owner/repo"), status)
            finally:
                connection.close()


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.args = Namespace(state_root=self.root / "state")
        self.connection = pipeline.connect_database(self.root / "state" / "progress.sqlite3")
        self.addCleanup(self.connection.close)
        rows = {
            "dns": ("pipeline_failed", "pipeline-error", DNS_ERROR),
            "path": ("pipeline_failed", "pipeline-error", INVALID_PATH_ERROR),
            "bug": ("pipeline_failed", "pipeline-error", "fatal: 'C:/x/old' already exists"),
            "adapt-dns": ("adapt_failed", "lakeva-adapt-failed", "exit 1"),
            "adapt-real": ("adapt_failed", "lakeva-adapt-failed", "error: no lakefile.lean found"),
            "native": ("native_inconclusive", "old-native-check-inconclusive", None),
            "native-timeout": ("native_inconclusive", "old-native-check-inconclusive", None),
        }
        for index, (pair_id, (status, outcome, error)) in enumerate(rows.items()):
            insert_pair(self.connection, pair_id, "owner/repo", index, f"old-{pair_id}", f"new-{pair_id}")
            self.connection.execute("UPDATE pairs SET status=?,outcome=?,error=? WHERE pair_id=?",
                                    (status, outcome, error, pair_id))
        self.connection.execute(
            "INSERT INTO error_exports(pair_id,format_version,error_count,updated_at) VALUES('dns',5,0,'t')")
        runs = self.root / "state" / "runs" / "owner__repo"
        (runs / "adapt-dns").mkdir(parents=True)
        (runs / "adapt-dns" / "lakeva-adapt.log").write_text(
            "fatal: unable to access 'https://github.com/d/e.git/': Could not resolve host: github.com\n")
        (runs / "adapt-real").mkdir(parents=True)
        (runs / "adapt-real" / "lakeva-adapt.log").write_text("error: no lakefile.lean found\n")
        for pair_id, exit_code, body in (
            ("native", 128, "fatal: unable to access 'https://github.com/o/r.git/': Could not resolve host: github.com\n"),
            ("native-timeout", 124, "[1/3] (Optional) Fetching mathlib:optRelease\nerror: Could not resolve host: github.com\n"
                                    "[2/3] Building Mathlib\nerror: command timed out after 3600 seconds\n"),
        ):
            log_dir = self.root / "state" / "native-checks" / "owner__repo" / pair_id
            log_dir.mkdir(parents=True)
            (log_dir / "build.log").write_text(body)
            self.connection.execute(
                "INSERT INTO native_checks(repo,commit_hash,version,status,build_exit_code,log_dir,updated_at) "
                "VALUES('owner/repo',?,?,'inconclusive',?,?,'t')",
                (f"old-{pair_id}", "leanprover/lean4:v4.0.0", exit_code, str(log_dir)))
        self.connection.commit()

    def status_of(self, pair_id: str) -> tuple[str, str | None]:
        row = self.connection.execute("SELECT status,outcome FROM pairs WHERE pair_id=?", (pair_id,)).fetchone()
        return row["status"], row["outcome"]

    def test_requeues_network_results_and_files_invalid_paths(self) -> None:
        pipeline.migrate_network_failures(self.args, self.connection)
        pending = ("pending", pipeline.NETWORK_FAILURE_OUTCOME)
        self.assertEqual(self.status_of("dns"), pending)
        self.assertEqual(self.status_of("adapt-dns"), pending)
        self.assertEqual(self.status_of("native"), pending)
        self.assertEqual(self.status_of("path"), ("ineligible_pair", pipeline.INVALID_PATH_OUTCOME))
        self.assertEqual(self.status_of("bug"), ("pipeline_failed", "pipeline-error"))
        self.assertEqual(self.status_of("adapt-real"), ("adapt_failed", "lakeva-adapt-failed"))
        # An optional download failure before a compile timeout is not a network result.
        self.assertEqual(self.status_of("native-timeout"), ("native_inconclusive", "old-native-check-inconclusive"))
        remaining = {row[0] for row in self.connection.execute("SELECT commit_hash FROM native_checks")}
        self.assertEqual(remaining, {"old-native-timeout"})
        self.assertIsNone(self.connection.execute("SELECT 1 FROM error_exports WHERE pair_id='dns'").fetchone())

    def test_is_idempotent(self) -> None:
        pipeline.migrate_network_failures(self.args, self.connection)
        self.connection.execute("UPDATE pairs SET status='pipeline_failed',outcome='pipeline-error' WHERE pair_id='dns'")
        self.connection.commit()
        pipeline.migrate_network_failures(self.args, self.connection)
        self.assertEqual(self.status_of("dns"), ("pipeline_failed", "pipeline-error"))


if __name__ == "__main__":
    unittest.main()
