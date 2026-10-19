"""Read-only consistency audit of pipeline reports and their build evidence."""

import collections
import json
from pathlib import Path
import sqlite3

from extract_lean_errors import parse_log
from result_validation import (
    RESULT_FORMAT_VERSION, classify_adapted_build, infrastructure_failure,
    log_exit_code, toolchain_mismatch,
)
from run_pipeline import native_pass_is_valid, slug


def audit(root: Path) -> dict:
    connection = sqlite3.connect((root / 'state/progress.sqlite3').as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    pairs = {r['pair_id']: dict(r) for r in connection.execute('SELECT * FROM pairs')}
    natives = {(r['repo'], r['commit_hash']): dict(r) for r in connection.execute('SELECT * FROM native_checks')}
    exports = {r['pair_id']: dict(r) for r in connection.execute('SELECT * FROM error_exports')}
    connection.close()
    issues = collections.defaultdict(list)
    messages = collections.Counter()
    files_by_pair = collections.Counter()
    reports_by_pair = collections.defaultdict(list)
    for path in (root / 'parsed-errors').glob('*.json'):
        try:
            report = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, OSError) as exc:
            issues['invalid_json'].append([path.name, str(exc)])
            continue
        if not isinstance(report, dict):
            issues['invalid_schema'].append(path.name)
            continue
        pair = pairs.get(report.get('pair_id'))
        if not pair or not pair['active'] or pair['status'] != 'completed' or pair['outcome'] != 'build-errors':
            issues['stale_error_report'].append(path.name)
        files_by_pair[report.get('pair_id')] += 1
        reports_by_pair[report.get('pair_id')].append(report)
        messages[report.get('message', '').split('\n')[0]] += 1
        if not report.get('source_found'):
            issues['source_unavailable'].append(path.name)
        if report.get('file', '').startswith('error:'):
            issues['bad_filename'].append(path.name)
        if '\ntrace:' in report.get('message', '') or '\n# exit_code:' in report.get('message', ''):
            issues['polluted_message'].append(path.name)
        if pair and any(report.get(key) != pair[key] for key in ('repo', 'old_hash', 'new_hash', 'old_version', 'new_version')):
            issues['wrong_report_identity'].append(path.name)
        if report.get('source_found'):
            code = report.get('code', '').split('\n')
            offset = report['line'] - report['context_start_line']
            if not 0 <= offset < len(code) or code[offset] != report.get('error_line'):
                issues['wrong_source_context'].append(path.name)
            if len(code) != report['context_end_line'] - report['context_start_line'] + 1:
                issues['wrong_context_range'].append(path.name)
    export = root / 'pairs-without-lean-errors.jsonl'
    no_errors = [json.loads(line) for line in export.read_text(encoding='utf-8').splitlines() if line] if export.exists() else []
    for report in no_errors:
        pair = pairs.get(report['pair_id'])
        if not pair or pair['status'] != 'completed' or pair['outcome'] != 'build-succeeded':
            issues['stale_no_error_record'].append(report['pair_id'])
        if pair and any(report.get(key) != pair[key] for key in ('repo', 'old_hash', 'new_hash', 'old_version', 'new_version')):
            issues['wrong_no_error_identity'].append(report['pair_id'])
    actual_no_errors = collections.Counter(r['pair_id'] for r in no_errors)
    expected_no_errors = {p['pair_id'] for p in pairs.values()
                          if p['active'] and p['status'] == 'completed' and p['outcome'] == 'build-succeeded'}
    for pid in expected_no_errors - actual_no_errors.keys():
        issues['missing_no_error_record'].append(pid)
    for pid, count in actual_no_errors.items():
        if count != 1:
            issues['duplicate_no_error_record'].append(pid)
    for pair in pairs.values():
        if not pair['active'] or pair['status'] != 'completed':
            continue
        log_dir = root / 'state/runs' / slug(pair['repo']) / pair['pair_id']
        build = log_dir / 'build.log'
        if not build.exists():
            issues['missing_build_log'].append(pair['pair_id'])
            continue
        parsed = parse_log(build)
        diagnostics = [e for e in parsed if e['kind'] == 'lean-diagnostic']
        classification = classify_adapted_build(log_exit_code(build), parsed, build,
            infrastructure_failure=infrastructure_failure(build),
            expected_toolchain=pair['new_version'], cache_log=log_dir/'cache.log')
        if classification != (pair['status'], pair['outcome']):
            issues['wrong_build_classification'].append([pair['pair_id'], classification])
        export = exports.get(pair['pair_id'], {})
        if pair['result_version'] != RESULT_FORMAT_VERSION or export.get('format_version') != RESULT_FORMAT_VERSION:
            issues['old_result_format'].append(pair['pair_id'])
        if export.get('error_count') != len(diagnostics):
            issues['checkpoint_error_count_mismatch'].append(pair['pair_id'])
        if pair['outcome'] == 'build-succeeded' and (log_exit_code(build) != 0 or diagnostics):
            issues['false_build_success'].append(pair['pair_id'])
        if pair['outcome'] == 'build-errors' and files_by_pair[pair['pair_id']] != len(diagnostics):
            issues['error_count_mismatch'].append(pair['pair_id'])
        expected_messages = collections.Counter('\n'.join(x for x in (e['message'].strip(), e.get('details', '').strip('\n')) if x) for e in diagnostics)
        reported_messages = collections.Counter(r['message'] for r in reports_by_pair[pair['pair_id']])
        if expected_messages != reported_messages:
            issues['diagnostic_message_mismatch'].append(pair['pair_id'])
        if pair['old_version'] == pair['new_version']:
            issues['same_version_pair'].append(pair['pair_id'])
        if any(toolchain_mismatch(log_dir / name, pair['new_version']) for name in ('build.log', 'cache.log', 'lakeva-adapt.log')):
            issues['wrong_adapted_toolchain'].append(pair['pair_id'])
        for side in ('old', 'new'):
            native = natives.get((pair['repo'], pair[side + '_hash']))
            if not native or native['status'] != 'passed':
                issues['native_not_passed'].append([pair['pair_id'], side])
                continue
            if not native_pass_is_valid(native, pair[side + '_version']):
                issues['native_pass_invalid'].append([pair['pair_id'], side])
    counts = collections.Counter((p['status'], p['outcome']) for p in pairs.values() if p['active'])
    return {
        'pair_statuses': [{'status': k[0], 'outcome': k[1], 'count': v} for k, v in counts.items()],
        'error_files': sum(files_by_pair.values()), 'error_pairs': len(files_by_pair),
        'no_error_records': len(no_errors), 'issue_counts': {k: len(v) for k, v in issues.items()},
        'issue_examples': {k: v[:15] for k, v in issues.items()},
        'top_messages': messages.most_common(20),
    }


if __name__ == '__main__':
    print(json.dumps(audit(Path(__file__).resolve().parent), indent=2, ensure_ascii=True))
