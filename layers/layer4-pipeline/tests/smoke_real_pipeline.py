"""Opt-in real Git/Lake/lakeva integration test using local dependency-free repos.

Run from the pipeline directory: python tests/smoke_real_pipeline.py
Requires the already-installed Lean v4.0.0 and v4.31.0 toolchains.
"""
from argparse import Namespace
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_pipeline as p
from commit_to_version import discover_pairs


def main():
    sys.stdout.reconfigure(errors='backslashreplace')
    # Keep the test independent of unrelated user Git configuration.
    os.environ['GIT_CONFIG_GLOBAL'] = os.devnull
    os.environ['GIT_CONFIG_SYSTEM'] = os.devnull
    with tempfile.TemporaryDirectory(prefix='lean-e2e-') as directory:
        root = Path(directory)
        args = Namespace(state_root=root/'state', errors_root=root/'errors',
            worktrees_root=root/'trees', no_lean_errors_output=root/'no-errors.jsonl',
            acknowledge_untrusted_builds=True, keep_worktrees=False, skip_cache=False,
            native_attempts=1, cache_timeout=120, native_timeout=120, adapt_timeout=120,
            build_timeout=120, retry_failed=False, max_pairs=None, workers=2,
            lake=str(Path.home()/'.elan/bin/lake.exe'),
            lakeva=str(Path('C:/Users/Cynthia/Documents/Codex/2026-09-17/i-x20/outputs/lake-env-adapter/lakeva.cmd')))
        args.errors_root.mkdir()
        connection = p.connect_database(args.state_root/'progress.sqlite3')
        try:
            repos = []
            cases = {
                'compatible': ('def answer : Nat := 1\n', 'def answer : Nat := 2\n', 'build-succeeded'),
                'api-change': ('def xs : List Nat := (#[1, 2] : Array Nat).data\n',
                               'def xs : List Nat := (#[1, 2] : Array Nat).toList\n', 'build-errors'),
                'broken-native': ('def answer : Nat := 1\n', 'def answer : Nat := "broken"\n',
                                  'new-commit-does-not-build-natively'),
            }
            for name, (old_source, new_source, outcome) in cases.items():
                repo = root/name
                repo.mkdir()
                p.git(repo, 'init', '-q')
                p.git(repo, 'config', 'user.name', 'Pipeline test')
                p.git(repo, 'config', 'user.email', 'test@example.invalid')
                (repo/'lakefile.lean').write_text('import Lake\nopen Lake DSL\npackage fixture\n@[default_target]\nlean_lib Fixture\n', encoding='utf8')
                for version, source in [('v4.0.0', old_source), ('v4.31.0', new_source)]:
                    (repo/'lean-toolchain').write_text('leanprover/lean4:'+version+'\n', encoding='utf8')
                    (repo/'Fixture.lean').write_text(source, encoding='utf8')
                    p.git(repo, 'add', '.')
                    p.git(repo, 'commit', '-qm', version)
                repo_name = 'smoke/'+name
                repos.append(dict(name=repo_name,path=repo))
                pair = discover_pairs(repo)[0]
                connection.execute('INSERT INTO pairs(pair_id,repo,pair_index,old_hash,new_hash,old_version,new_version,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                    (p.pair_identifier(repo_name, pair['new_hash'],pair['old_hash']), repo_name, 0,
                     pair['old_hash'],pair['new_hash'],pair['old_version'],pair['new_version'],p.now()))
            connection.commit()
            p.run_pairs(args, connection, repos)
            results = list(connection.execute('SELECT * FROM pairs ORDER BY repo'))
            for result in results:
                expected = cases[result['repo'].split('/')[1]][2]
                print(result['repo'], result['status'], result['outcome'], flush=True)
                if result['outcome'] != expected:
                    for log in args.state_root.rglob('*.log'):
                        print(log, log.read_text(encoding='utf8',errors='replace')[-3500:], flush=True)
                    raise AssertionError(f"Expected {expected}, got {result['outcome']}: {result['error']}")
            reports = [json.loads(path.read_text(encoding='utf8')) for path in args.errors_root.glob('*.json')]
            assert reports and all(r['repo']=='smoke/api-change' and r['source_found'] for r in reports), reports
            assert all('.data' in r['error_line'] for r in reports)
            no_errors = [json.loads(line) for line in args.no_lean_errors_output.read_text(encoding='utf8').splitlines()]
            assert [r['repo'] for r in no_errors] == ['smoke/compatible'], no_errors
            # A resumed run must not execute completed/discarded cases again.
            attempts = [r['attempts'] for r in results]
            p.run_pairs(args, connection, repos)
            assert attempts == [r[0] for r in connection.execute('SELECT attempts FROM pairs ORDER BY repo')]
            print('PASS: native checks, adaptation, error/context export, no-error export, two-worker checkpoints, and resume.', flush=True)
        finally:
            connection.close()


if __name__ == '__main__':
    main()
