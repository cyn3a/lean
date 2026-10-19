from __future__ import annotations

from argparse import Namespace
from collections import defaultdict
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import run_pipeline as pipeline


def insert_pair(
    connection,
    pair_id: str,
    repo: str,
    pair_index: int,
    old_hash: str,
    new_hash: str,
) -> None:
    connection.execute(
        """
        INSERT INTO pairs(
            pair_id,repo,pair_index,old_hash,new_hash,old_version,new_version,
            status,active,updated_at
        ) VALUES(?,?,?,?,?,?,?,'pending',1,?)
        """,
        (
            pair_id,
            repo,
            pair_index,
            old_hash,
            new_hash,
            "leanprover/lean4:v4.0.0",
            "leanprover/lean4:v4.1.0",
            pipeline.now(),
        ),
    )


class ParallelPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        pipeline._LAKE_HELP.clear()
        probe = patch.object(pipeline, 'lake_supports_keep_toolchain', return_value=False)
        probe.start()
        self.addCleanup(probe.stop)

    def tearDown(self) -> None:
        pipeline.STOP_EVENT.clear()

    def test_parallelizes_repositories_but_serializes_each_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary)
            connection = pipeline.connect_database(state_root / "progress.sqlite3")
            insert_pair(connection, "a0", "owner/a", 0, "a-old-0", "a-new-0")
            insert_pair(connection, "a1", "owner/a", 1, "a-old-1", "a-new-1")
            insert_pair(connection, "b0", "owner/b", 0, "b-old-0", "b-new-0")
            insert_pair(connection, "c0", "owner/c", 0, "c-old-0", "c-new-0")
            connection.commit()

            args = Namespace(
                acknowledge_untrusted_builds=True,
                lakeva="fake-lakeva",
                lake="fake-lake",
                state_root=state_root,
                keep_worktrees=False,
                retry_failed=False,
                max_pairs=None,
                workers=3,
            )
            repositories = [
                {"name": name, "path": state_root / name.replace("/", "-")}
                for name in ("owner/a", "owner/b", "owner/c")
            ]
            lock = threading.Lock()
            active = 0
            maximum_active = 0
            active_by_repo: defaultdict[str, int] = defaultdict(int)
            maximum_by_repo: defaultdict[str, int] = defaultdict(int)
            order_by_repo: defaultdict[str, list[int]] = defaultdict(list)

            def fake_process(_args, _connection, pair, _repo_path, _lakeva, _lake):
                nonlocal active, maximum_active
                repo = pair["repo"]
                with lock:
                    active += 1
                    active_by_repo[repo] += 1
                    maximum_active = max(maximum_active, active)
                    maximum_by_repo[repo] = max(
                        maximum_by_repo[repo], active_by_repo[repo]
                    )
                    order_by_repo[repo].append(pair["pair_index"])
                time.sleep(0.08)
                with lock:
                    active_by_repo[repo] -= 1
                    active -= 1

            with (
                patch.object(pipeline, "command_prefix", side_effect=lambda value: [value]),
                patch.object(pipeline, "cleanup_stale_worktrees"),
                patch.object(pipeline, "process_pair", side_effect=fake_process),
            ):
                pipeline.run_pairs(args, connection, repositories)

            self.assertGreaterEqual(maximum_active, 2)
            self.assertTrue(all(value == 1 for value in maximum_by_repo.values()))
            self.assertEqual(order_by_repo["owner/a"], [0, 1])
            connection.close()

    def test_exports_only_analyzed_pairs_without_lean_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection = pipeline.connect_database(root / "progress.sqlite3")
            outcomes = (
                ("success", "completed", "build-succeeded"),
                ("process", "completed", "build-failed-without-lean-errors"),
                ("lean", "completed", "build-errors"),
                ("native", "discarded_native", "old-commit-does-not-build-natively"),
            )
            for index, (pair_id, status, outcome) in enumerate(outcomes):
                insert_pair(
                    connection,
                    pair_id,
                    f"owner/{pair_id}",
                    index,
                    f"{pair_id}-old",
                    f"{pair_id}-new",
                )
                connection.execute(
                    "UPDATE pairs SET status=?,outcome=?,result_version=?,build_exit_code=0 WHERE pair_id=?",
                    (status, outcome, pipeline.RESULT_FORMAT_VERSION, pair_id),
                )
                for side, version in (('old', 'v4.0.0'), ('new', 'v4.1.0')):
                    connection.execute('INSERT INTO native_checks(repo,commit_hash,version,status,log_dir,updated_at) VALUES(?,?,?,?,?,?)',
                                       (f'owner/{pair_id}', f'{pair_id}-{side}', f'leanprover/lean4:{version}', 'passed', str(root), pipeline.now()))
                connection.execute('INSERT INTO error_exports VALUES(?,?,0,?)', (pair_id, pipeline.RESULT_FORMAT_VERSION, pipeline.now()))
            connection.commit()

            output = root / "pairs-without-lean-errors.jsonl"
            count = pipeline.export_pairs_without_lean_errors(connection, output)
            records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]

            self.assertEqual(count, 1)
            self.assertEqual({record["pair_id"] for record in records}, {"success"})
            self.assertTrue(all(record["lean_error_count"] == 0 for record in records))
            connection.close()

    def test_checkpoint_write_waits_for_temporary_database_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "progress.sqlite3"
            locker = pipeline.connect_database(database)
            insert_pair(locker, "locked-pair", "owner/repo", 0, "old", "new")
            locker.commit()
            locker.execute("BEGIN IMMEDIATE")
            locker.execute(
                "UPDATE pairs SET last_stage='held-lock' WHERE pair_id='locked-pair'"
            )

            failures: list[BaseException] = []

            def update_from_worker() -> None:
                connection = pipeline.open_database(database)
                try:
                    pipeline.update_pair(
                        connection,
                        "locked-pair",
                        last_stage="worker-checkpoint",
                    )
                except BaseException as exc:
                    failures.append(exc)
                finally:
                    connection.close()

            worker = threading.Thread(target=update_from_worker)
            worker.start()
            time.sleep(0.2)
            locker.commit()
            worker.join(timeout=5)

            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
            stage = locker.execute(
                "SELECT last_stage FROM pairs WHERE pair_id='locked-pair'"
            ).fetchone()[0]
            self.assertEqual(stage, "worker-checkpoint")
            locker.close()

    def test_empty_error_migration_releases_main_write_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            errors_root = root / "parsed-errors"
            errors_root.mkdir()
            database = root / "progress.sqlite3"
            connection = pipeline.connect_database(database)
            insert_pair(connection, "failed-pair", "owner/repo", 0, "old", "new")
            connection.execute(
                "UPDATE pairs SET status='pipeline_failed',outcome='stale' "
                "WHERE pair_id='failed-pair'"
            )
            connection.commit()
            args = Namespace(errors_root=errors_root, state_root=root / "state")

            pipeline.migrate_legacy_error_reports(
                args,
                connection,
                [{"name": "owner/repo", "path": root / "repo"}],
            )

            self.assertFalse(connection.in_transaction)
            worker = pipeline.open_database(database)
            pipeline.update_pair(worker, "failed-pair", last_stage="worker-can-write")
            self.assertEqual(
                connection.execute(
                    "SELECT outcome FROM pairs WHERE pair_id='failed-pair'"
                ).fetchone()[0],
                "pipeline-error",
            )
            worker.close()
            connection.close()

    def test_cached_native_failure_skips_worktree_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection = pipeline.connect_database(root / "progress.sqlite3")
            insert_pair(connection, "pair0", "owner/repo", 0, "old", "new")
            connection.execute(
                """
                INSERT INTO native_checks(
                    repo,commit_hash,version,status,cache_exit_code,build_exit_code,
                    log_dir,updated_at
                ) VALUES(?,?,?,'failed',0,1,?,?)
                """,
                (
                    "owner/repo",
                    "new",
                    "leanprover/lean4:v4.1.0",
                    str(root / "native"),
                    pipeline.now(),
                ),
            )
            connection.commit()
            pair = dict(connection.execute("SELECT * FROM pairs WHERE pair_id='pair0'").fetchone())
            args = Namespace(
                state_root=root / "state",
                errors_root=root / "errors",
                keep_worktrees=True,
            )

            with patch.object(
                pipeline,
                "add_worktree",
                side_effect=AssertionError("a cached failure must not create a worktree"),
            ):
                pipeline.process_pair(
                    args,
                    connection,
                    pair,
                    root / "repo",
                    ["fake-lakeva"],
                    ["fake-lake"],
                )

            result = connection.execute(
                "SELECT status,outcome FROM pairs WHERE pair_id='pair0'"
            ).fetchone()
            self.assertEqual(result["status"], "discarded_native")
            self.assertEqual(result["outcome"], "new-commit-does-not-build-natively")
            connection.close()

    def test_cached_inconclusive_native_check_pauses_without_adapting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection = pipeline.connect_database(root / "progress.sqlite3")
            insert_pair(connection, "pair0", "owner/repo", 0, "old", "new")
            connection.execute(
                """
                INSERT INTO native_checks(
                    repo,commit_hash,version,status,cache_exit_code,build_exit_code,
                    log_dir,updated_at
                ) VALUES(?,?,?,'inconclusive',1,124,?,?)
                """,
                (
                    "owner/repo",
                    "new",
                    "leanprover/lean4:v4.1.0",
                    str(root / "native"),
                    pipeline.now(),
                ),
            )
            connection.commit()
            pair = dict(connection.execute("SELECT * FROM pairs WHERE pair_id='pair0'").fetchone())
            args = Namespace(
                state_root=root / "state",
                errors_root=root / "errors",
                keep_worktrees=True,
            )

            with patch.object(
                pipeline,
                "add_worktree",
                side_effect=AssertionError("an inconclusive cache must not adapt"),
            ):
                pipeline.process_pair(
                    args,
                    connection,
                    pair,
                    root / "repo",
                    ["fake-lakeva"],
                    ["fake-lake"],
                )

            result = connection.execute(
                "SELECT status,outcome FROM pairs WHERE pair_id='pair0'"
            ).fetchone()
            self.assertEqual(result["status"], "native_inconclusive")
            self.assertEqual(result["outcome"], "new-native-check-inconclusive")
            connection.close()

    def test_git_environment_appends_without_overwriting_existing_config(self) -> None:
        environment = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": "C:/existing",
        }
        pipeline.append_git_config(environment, "core.longpaths", "true")
        self.assertEqual(environment["GIT_CONFIG_COUNT"], "2")
        self.assertEqual(environment["GIT_CONFIG_KEY_0"], "safe.directory")
        self.assertEqual(environment["GIT_CONFIG_VALUE_0"], "C:/existing")
        self.assertEqual(environment["GIT_CONFIG_KEY_1"], "core.longpaths")
        self.assertEqual(environment["GIT_CONFIG_VALUE_1"], "true")

    def test_native_classifier_separates_infrastructure_from_lean_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache_log = root / "cache.log"
            build_log = root / "build.log"
            cache_log.write_text(
                "error: RPC failed; curl 18 transfer closed with outstanding data\n",
                encoding="utf-8",
            )
            build_log.write_text(
                "fatal: invalid index-pack output\n",
                encoding="utf-8",
            )
            self.assertTrue(
                pipeline.native_failure_is_inconclusive(
                    cache_log, build_log, 1, 1
                )
            )
            self.assertTrue(
                pipeline.native_failure_is_inconclusive(
                    cache_log, build_log, 0, 124
                )
            )

            build_log.write_text(
                "Project/Main.lean:12:4: error: unknown identifier 'x'\n",
                encoding="utf-8",
            )
            self.assertFalse(
                pipeline.native_failure_is_inconclusive(
                    cache_log, build_log, 1, 1
                )
            )

    def test_native_check_uses_plain_build_for_old_lake_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worktree = root / "worktree"
            worktree.mkdir()
            connection = pipeline.connect_database(root / "progress.sqlite3")
            args = Namespace(
                state_root=root / "state",
                skip_cache=True,
                native_attempts=2,
                cache_timeout=10,
                native_timeout=10,
            )
            commands: list[list[str]] = []

            def fake_run(command, _cwd, _log, _timeout, *, toolchain=None):
                commands.append(command)
                (_cwd / 'lean-toolchain').write_text(toolchain, encoding='utf8')
                _log.write_text(f'# toolchain: {toolchain}\n# toolchain_after: {toolchain}\n# root_olean_count: 1\n# exit_code: 0\n', encoding='utf8')
                return 0

            with patch.object(pipeline, "run_logged", side_effect=fake_run):
                status = pipeline.native_check(
                    args,
                    connection,
                    "owner/repo",
                    "deadbeef",
                    "leanprover/lean4:nightly-2022-06-16",
                    worktree,
                    ["lake"],
                )

            self.assertEqual(status, "passed")
            self.assertEqual(commands, [["lake", "build"]])
            connection.close()

    def test_native_check_retries_cleanly_and_snapshots_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worktree = root / "worktree"
            worktree.mkdir()
            connection = pipeline.connect_database(root / "progress.sqlite3")
            args = Namespace(
                state_root=root / "state",
                skip_cache=True,
                native_attempts=2,
                cache_timeout=10,
                native_timeout=10,
            )
            calls = 0

            def fake_run(_command, _cwd, log_path, _timeout, *, toolchain=None):
                nonlocal calls
                calls += 1
                log_path.parent.mkdir(parents=True, exist_ok=True)
                (_cwd / 'lean-toolchain').write_text(toolchain, encoding='utf8')
                if calls == 1:
                    log_path.write_text(
                        "error: RPC failed; curl 18 early EOF\n",
                        encoding="utf-8",
                    )
                    return 1
                log_path.write_text(f'# toolchain: {toolchain}\n# toolchain_after: {toolchain}\n# root_olean_count: 1\n# exit_code: 0\n', encoding='utf8')
                (worktree / pipeline.LAKE_MANIFEST).write_text(
                    '{"version": "1.0.0"}\n', encoding="utf-8"
                )
                return 0

            with (
                patch.object(pipeline, "run_logged", side_effect=fake_run),
                patch.object(
                    pipeline, "restore_pristine_worktree_with_retries"
                ) as restore,
                patch.object(pipeline.STOP_EVENT, "wait", return_value=False),
            ):
                status = pipeline.native_check(
                    args,
                    connection,
                    "owner/repo",
                    "deadbeef",
                    "leanprover/lean4:v4.1.0",
                    worktree,
                    ["lake"],
                )

            self.assertEqual(status, "passed")
            self.assertEqual(calls, 2)
            restore.assert_called_once_with(worktree, "deadbeef")
            snapshot = pipeline.native_manifest_snapshot(
                args, "owner/repo", "deadbeef"
            )
            self.assertTrue(snapshot.is_file())
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM native_checks WHERE repo='owner/repo'"
                ).fetchone()[0],
                "passed",
            )
            connection.close()

    def test_stale_manifest_runs_lake_update_then_rebuilds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worktree = root / "worktree"
            worktree.mkdir()
            connection = pipeline.connect_database(root / "progress.sqlite3")
            args = Namespace(
                state_root=root / "state",
                skip_cache=True,
                native_attempts=1,
                cache_timeout=10,
                native_timeout=10,
            )
            commands: list[list[str]] = []

            def fake_run(command, _cwd, log_path, _timeout, *, toolchain=None):
                commands.append(command)
                log_path.parent.mkdir(parents=True, exist_ok=True)
                (_cwd / 'lean-toolchain').write_text(toolchain, encoding='utf8')
                if command == ["lake", "update"]:
                    log_path.write_text("Manifest updated.\n", encoding="utf-8")
                    return 0
                if len([item for item in commands if item == ["lake", "build"]]) == 1:
                    log_path.write_text(
                        "error: dependency Cli not in manifest, use `lake update` to update\n",
                        encoding="utf-8",
                    )
                    return 1
                log_path.write_text(f'# toolchain: {toolchain}\n# toolchain_after: {toolchain}\n# root_olean_count: 1\n# exit_code: 0\n', encoding='utf8')
                return 0

            with patch.object(pipeline, "run_logged", side_effect=fake_run):
                status = pipeline.native_check(
                    args,
                    connection,
                    "owner/repo",
                    "deadbeef",
                    "leanprover/lean4:v4.1.0",
                    worktree,
                    ["lake"],
                )

            self.assertEqual(status, "passed")
            self.assertEqual(
                commands,
                [["lake", "build"], ["lake", "update"], ["lake", "build"]],
            )
            connection.close()

    def test_stale_cleanup_removes_empty_pair_directory_without_git_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_root = root / "state"
            worktrees_root = root / "short"
            pair_dir = worktrees_root / "pair0"
            pair_dir.mkdir(parents=True)
            connection = pipeline.connect_database(state_root / "progress.sqlite3")
            insert_pair(connection, "pair0", "owner/repo", 0, "old", "new")
            connection.commit()
            args = Namespace(
                state_root=state_root,
                worktrees_root=worktrees_root,
                keep_worktrees=False,
            )

            with (
                patch.object(pipeline, "remove_worktree") as remove,
                patch.object(pipeline, "git"),
            ):
                pipeline.cleanup_stale_worktrees(
                    args, connection, {"owner/repo": root / "repo"}
                )

            remove.assert_not_called()
            self.assertFalse(pair_dir.exists())
            connection.close()

    def test_native_protocol_migration_requeues_binary_failures_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection = pipeline.connect_database(root / "progress.sqlite3")
            insert_pair(connection, "pair0", "owner/repo", 0, "old", "new")
            connection.execute(
                "UPDATE pairs SET status='discarded_native',outcome=? WHERE pair_id='pair0'",
                ("new-commit-does-not-build-natively",),
            )
            connection.execute(
                """
                INSERT INTO native_checks(
                    repo,commit_hash,version,status,cache_exit_code,build_exit_code,
                    log_dir,updated_at
                ) VALUES(?,?,?,'failed',1,1,?,?)
                """,
                (
                    "owner/repo",
                    "new",
                    "leanprover/lean4:v4.1.0",
                    str(root / "native"),
                    pipeline.now(),
                ),
            )
            connection.commit()

            pipeline.migrate_native_check_protocol(connection)
            result = connection.execute(
                "SELECT status,outcome FROM pairs WHERE pair_id='pair0'"
            ).fetchone()
            self.assertEqual(result["status"], "pending")
            self.assertIsNone(result["outcome"])
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM native_checks").fetchone()[0],
                0,
            )

            # The version marker prevents later genuine v2 failures from being
            # invalidated every time the program starts.
            connection.execute(
                """
                INSERT INTO native_checks(
                    repo,commit_hash,version,status,cache_exit_code,build_exit_code,
                    log_dir,updated_at
                ) VALUES(?,?,?,'failed',0,1,?,?)
                """,
                (
                    "owner/repo",
                    "new",
                    "leanprover/lean4:v4.1.0",
                    str(root / "native"),
                    pipeline.now(),
                ),
            )
            connection.commit()
            pipeline.migrate_native_check_protocol(connection)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM native_checks").fetchone()[0],
                1,
            )
            connection.close()


if __name__ == "__main__":
    unittest.main()
