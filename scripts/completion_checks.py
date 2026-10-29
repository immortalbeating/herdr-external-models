import json
import os
import time
from pathlib import Path


def _nonempty(path):
    try:
        with open(path, 'r', encoding='utf-8-sig') as stream:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    return False
                if chunk.strip():
                    return True
    except (OSError, UnicodeError, ValueError):
        return False


def _inside_job(path, job):
    try:
        return os.path.commonpath((os.path.realpath(path), os.path.realpath(job))) == os.path.realpath(job)
    except (ValueError, OSError):
        return False


def _job_report(job, expect_closed):
    row = {'job': job, 'execution': 'unknown', 'review': 'pending', 'pane': 'unknown', 'issues': []}
    issues = row['issues']
    try:
        record_path = Path(job) / 'job.json'
        if not _inside_job(record_path, job):
            raise ValueError('record outside job')
        state = json.loads(record_path.read_text(encoding='utf-8-sig'))
        if not isinstance(state, dict):
            raise ValueError('record must be an object')
        dsh = 'state' in state and 'status' not in state
        execution = state.get('state' if dsh else 'status')
        if not isinstance(execution, str) or not execution:
            raise ValueError('missing execution status')
        row['execution'] = execution
        if dsh:
            outcome, review = state.get('review_outcome'), state.get('review_file')
            row['pane'] = 'closed' if execution == 'closed' else ('superseded' if state.get('superseded_by') else ('open' if state.get('terminal_id') else 'none'))
        else:
            spec = state.get('spec')
            if not isinstance(spec, dict) or spec.get('mode') not in ('direct', 'exec', 'herdr'):
                raise ValueError('invalid spec')
            outcome, review = execution, state.get('review')
            if spec['mode'] == 'herdr':
                row['pane'] = 'closed' if state.get('pane_closed') is True else ('open' if state.get('terminal_id') else 'none')
            else:
                row['pane'] = 'none'
        if outcome in ('completed', 'needs_work'):
            if not isinstance(review, str) or not os.path.isabs(review) or not _inside_job(review, job) or not _nonempty(review):
                issues.append('review_unreadable')
            else:
                row['review'] = outcome
            if outcome == 'completed' and (not _inside_job(Path(job) / 'result.md', job) or not _nonempty(Path(job) / 'result.md')):
                issues.append('result_unreadable')
            if outcome == 'completed' and not dsh:
                if state.get('result_current') is False:
                    issues.append('result_not_current')
                input_check = state.get('input_check')
                if isinstance(input_check, dict) and input_check.get('status') in ('changed', 'unavailable'):
                    issues.append('input_not_current')
        else:
            issues.append('review_pending')
        if expect_closed and row['pane'] == 'open':
            issues.append('close_pending')
        archive = state.get('codex_archive')
        if expect_closed and isinstance(archive, dict) and archive.get('status') in ('pending', 'failed'):
            issues.append('codex_archive_' + archive['status'])
    except (OSError, UnicodeError, ValueError, TypeError):
        issues.append('record_unreadable')
    return row


def finish_report(job, related_jobs=(), expect_closed=False):
    start = time.monotonic()
    jobs, seen = [], set()
    try:
        candidates = [job, *related_jobs]
        for item in candidates:
            path = os.fspath(item)
            if not isinstance(path, str) or not path.strip():
                raise ValueError('invalid job path')
            normalized = os.path.abspath(os.path.normpath(path))
            key = os.path.normcase(normalized)
            if key not in seen:
                jobs.append(normalized)
                seen.add(key)
        rows = [_job_report(path, expect_closed) for path in jobs]
    except (TypeError, ValueError, OSError):
        rows = [{'job': '', 'execution': 'unknown', 'review': 'pending', 'pane': 'unknown', 'issues': ['record_unreadable']}]
    return {'status': 'attention' if any(row['issues'] for row in rows) else 'ok',
            'checked_jobs': len(rows), 'jobs': rows,
            'elapsed_ms': round((time.monotonic() - start) * 1000, 3)}
