from argparse import Namespace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import run_pipeline as pipeline
from checkpoint_safety import state_lock
from commit_to_version import discover_pairs
from extract_lean_errors import parse_lean_errors
from result_validation import classify_adapted_build, infrastructure_failure, RESULT_FORMAT_VERSION
from test_parallel_pipeline import insert_pair


class RegressionTests(unittest.TestCase):
    def test_optional_release_failures_are_not_required_build_errors(self):
        text = ("[2/8] (Optional) Fetching batteries:optBarrel\n"
                "error: external command 'curl' exited with code 22\n"
                "[3/8] Ran batteries:extraDep\n"
                "trace: building from source; failed to fetch Reservoir build (see 'batteries:optBarrel' for details)\n"
                "[4/8] (Optional) Fetching proofwidgets:optRelease\n"
                "error: no release tag found for revision 'abc'\n"
                "[5/8] Ran proofwidgets:extraDep\n"
                "warning: building from source; failed to fetch GitHub release (see 'proofwidgets:optRelease' for details)\n"
                "[8/8] Built Demo\nBuild completed successfully.\n")
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory)/'build.log'
            log.write_text(text)
            self.assertEqual(parse_lean_errors(text), [])
            self.assertFalse(infrastructure_failure(log))
            self.assertEqual(classify_adapted_build(0, [], log), ('completed', 'build-succeeded'))
        failed = text + 'Demo.lean:2:0: error: unknown identifier\nerror: build failed\n'
        self.assertEqual(parse_lean_errors(failed)[0]['kind'], 'lean-diagnostic')

    def test_linter_information_quotes_are_not_errors(self):
        self.assertEqual(parse_lean_errors(
            'info: Demo.lean:4:0: linter.style.header:298:35: error: expected token\n\n'
            'linter.style.header:424:35: error: expected token\n'
            '[3/3] Built Demo\nBuild completed successfully.\n'), [])

    def test_status_does_not_reset_an_active_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = pipeline.connect_database(root/'progress.sqlite3')
            insert_pair(connection, 'active', 'owner/repo', 0, 'old', 'new')
            connection.execute("UPDATE pairs SET status='running'")
            connection.commit()
            try:
                with state_lock(root), patch.object(pipeline, 'parse_args', return_value=Namespace(state_root=root,status=True)):
                    self.assertEqual(pipeline.main(), 0)
                self.assertEqual(connection.execute('SELECT status FROM pairs').fetchone()[0], 'running')
            finally:
                connection.close()

    def test_nested_lake_error_and_progress(self):
        diagnostics = parse_lean_errors(
            'error: .\\lakefile.lean:4:7: error: unexpected token\n'
            '[?/?] Computing build jobs\n'
            'error: error: Demo.lean:8:0: error: unsolved goals\n'
        )
        self.assertEqual(diagnostics[0]['file'], 'lakefile.lean')
        self.assertEqual(diagnostics[0]['message'], 'unexpected token')
        self.assertNotIn('details', diagnostics[0])
        self.assertEqual(diagnostics[1]['file'], 'Demo.lean')

    def test_json_diagnostic_is_self_contained(self):
        value = {'severity':'error','fileName':'Demo.lean','pos':{'line':1,'column':0},'data':'bad type'}
        self.assertNotIn('details', parse_lean_errors(json.dumps(value) + '\nprogram output\n')[0])

    def test_zero_jobs_and_unverified_old_success_are_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'build.log'
            for content in ('Build completed successfully.\n', '[?/?] Computing build jobs\n'):
                log.write_text(content)
                self.assertEqual(classify_adapted_build(0, [], log)[0], 'build_inconclusive')

    def test_toolchain_switch_even_with_exit_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'build.log'
            log.write_text("# toolchain: old\n# toolchain_after: new\n# root_olean_count: 1\n")
            self.assertEqual(classify_adapted_build(0, [], log, expected_toolchain='old'),
                             ('build_inconclusive', 'wrong-toolchain'))

    def test_real_compiler_message_is_not_infrastructure(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'build.log'
            log.write_text("Demo.lean:2:0: error: failed to create instance for Foo\n")
            self.assertFalse(infrastructure_failure(log))

    def test_same_toolchain_fallback_skips_to_distinct_boundary(self):
        transitions = [dict(commit='new', version='v2'), dict(commit='middle', version='v1'),
                       dict(commit='same', version='v2'), dict(commit='old', version='v0')]
        def paths(repo, new, old):
            return ['lean-toolchain'] if old == 'middle' else ['Demo.lean']
        with patch('commit_to_version.version_change_commits', return_value=transitions), patch('commit_to_version.changed_paths', side_effect=paths):
            pairs = discover_pairs(Path('.'))
        self.assertEqual(pairs[0]['old_hash'], 'old')
        self.assertTrue(all(p['new_version'] != p['old_version'] for p in pairs))

    def test_state_lock_rejects_second_writer_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with state_lock(root):
                with self.assertRaises(RuntimeError):
                    with state_lock(root):
                        pass
            with state_lock(root):
                pass

    def test_repair_reclassifies_old_failed_zero_error_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = Namespace(state_root=root/'state', errors_root=root/'errors')
            args.errors_root.mkdir()
            connection = pipeline.connect_database(args.state_root/'progress.sqlite3')
            try:
                for pid in ('missed', 'no-targets', 'process-failure'):
                    insert_pair(connection, pid, 'owner/repo', 0, 'old-'+pid, 'new-'+pid)
                    connection.execute("UPDATE pairs SET status='completed',outcome='build-failed-without-lean-errors' WHERE pair_id=?", (pid,))
                    log = args.state_root/'runs/owner__repo'/pid/'build.log'
                    log.parent.mkdir(parents=True)
                    content = {'missed':'error: Demo.lean:1:0: unknown identifier\n',
                               'no-targets':'Build completed successfully (0 jobs).\n',
                               'process-failure':"error: external command 'git' exited with code 128\n"}[pid]
                    log.write_text('# toolchain: leanprover/lean4:v4.1.0\n'+content+
                                   '# toolchain_after: leanprover/lean4:v4.1.0\n# exit_code: '+('0' if pid=='no-targets' else '1')+'\n')
                    for side, version in (('old', 'v4.0.0'), ('new', 'v4.1.0')):
                        native_dir = root / (side + '-' + pid)
                        native_dir.mkdir()
                        toolchain = 'leanprover/lean4:' + version
                        (native_dir/'build.log').write_text(
                            f'# toolchain: {toolchain}\n# toolchain_after: {toolchain}\n# root_olean_count: 1\n# exit_code: 0\n')
                        connection.execute('INSERT INTO native_checks(repo,commit_hash,version,status,log_dir,updated_at) VALUES(?,?,?,?,?,?)',
                                           ('owner/repo', side+'-'+pid, toolchain, 'passed', str(native_dir), pipeline.now()))
                connection.commit()
                with patch.object(pipeline, 'git', return_value=Namespace(returncode=0, stdout='example := x\n')):
                    pipeline.migrate_legacy_error_reports(args, connection, [{'name':'owner/repo','path':root}])
                    pipeline.migrate_legacy_error_reports(args, connection, [{'name':'owner/repo','path':root}])
                rows = {r['pair_id']:r for r in connection.execute('SELECT * FROM pairs')}
                self.assertEqual(rows['missed']['outcome'], 'build-errors')
                self.assertEqual(rows['no-targets']['status'], 'build_inconclusive')
                self.assertEqual(rows['process-failure']['status'], 'build_inconclusive')
                self.assertEqual(len(list(args.errors_root.glob('*.json'))), 1)
                self.assertEqual(connection.execute('SELECT count(*) FROM error_exports WHERE format_version=?', (RESULT_FORMAT_VERSION,)).fetchone()[0], 3)
                self.assertFalse(connection.in_transaction)
            finally:
                connection.close()


if __name__ == '__main__':
    unittest.main()
