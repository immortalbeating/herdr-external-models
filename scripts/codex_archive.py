"""Archive a closed Herdr job's exact local Codex conversation using the native CLI."""
import os
from pathlib import Path
import re
import subprocess

import native_records


def _has_token(text, value):
    return bool(value) and re.search(r'(?<![\w.-])' + re.escape(value) + r'(?![\w.-])', text) is not None


def archive(state, executable):
    spec = state['spec']
    if spec.get('mode') != 'herdr' or spec.get('kind') != 'codex':
        raise ValueError('Codex archive requires a Herdr Codex job')
    if state.get('pane_closed') is not True:
        raise ValueError('Close the owned pane before archiving its conversation')
    native = state.get('native_session')
    if not native:
        return {'status': 'skipped', 'reason': 'no_native_session'}
    if isinstance(native, dict) and native.get('agent', 'codex') != 'codex':
        raise ValueError('Native session is not Codex')
    path = native_records.resolve_transcript('codex', native, spec['workspace'], spec.get('env'))
    records = native_records._read(path)
    sid = native_records._validate('codex', records, spec['workspace'], path)
    home = native_records.codex_home(spec.get('env'))
    archived_root = (home / 'archived_sessions').resolve()
    if not path.is_relative_to(home / 'sessions') and path.parent != archived_root:
        raise ValueError('Native transcript is outside this job\'s Codex home')
    identified = native_records.resolve_transcript('codex', {'kind': 'id', 'value': sid}, spec['workspace'], spec.get('env'))
    if identified != path:
        raise ValueError('Codex UUID does not resolve to the bound transcript')
    if sid in (state.get('parent_thread_id'), os.environ.get('CODEX_THREAD_ID')):
        raise ValueError('Refusing to archive the controlling Codex conversation')
    # A replacement/resumed conversation must not inherit an old job's cleanup.
    users = []
    for row in records:
        item = row.get('payload') or {}
        if row.get('type') == 'response_item' and item.get('type') == 'message' and item.get('role') == 'user':
            users.append(''.join(c.get('text', '') for c in item.get('content', [])
                                 if isinstance(c, dict) and c.get('type') == 'input_text'
                                 and isinstance(c.get('text'), str)))
    request = state.get('last_request') or 'request.md'
    if not users or not _has_token(users[-1], state['id']) or not _has_token(users[-1], Path(request).name):
        raise ValueError('Codex conversation is not bound to this job and its latest request')
    events = [r.get('payload') or {} for r in records if r.get('type') == 'event_msg']
    starts = [i for i, event in enumerate(events) if event.get('type') == 'task_started']
    if starts and not any(e.get('type') in ('task_complete', 'turn_aborted', 'task_failed')
                          for e in events[starts[-1] + 1:]):
        raise ValueError('Codex conversation has an unfinished turn; inspect before archiving')
    if path.parent == archived_root:
        return {'status': 'archived', 'session_id': sid, 'transcript': str(path), 'already_archived': True}
    # Keep the same home/config environment as this job. No shell, no latest/name lookup.
    env = dict(os.environ, **spec.get('env', {}))
    result = subprocess.run([executable, 'archive', sid], cwd=spec['workspace'], env=env,
                            capture_output=True, text=True, encoding='utf-8', errors='replace',
                            timeout=45, check=False,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ValueError('Codex archive exited ' + str(result.returncode) + '; retry close --job after inspection')
    target = native_records.resolve_transcript('codex', {'kind': 'id', 'value': sid},
                                               spec['workspace'], spec.get('env'))
    if target.parent != archived_root:
        raise ValueError('Codex archive returned success but the archived transcript was not found')
    return {'status': 'archived', 'session_id': sid, 'transcript': str(target), 'already_archived': False}
