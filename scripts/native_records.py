"""Conservative, read-only collection of native CLI transcript records."""
import json
import os
import re
from pathlib import Path

_UUID = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')


def _read(path):
    data = Path(path).read_bytes()
    if data and not data.endswith(b'\n'):
        raise ValueError('incomplete transcript tail')
    records = []
    for line in data.splitlines():
        try:
            record = json.loads(line)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError('invalid transcript record') from exc
        if not isinstance(record, dict):
            raise ValueError('invalid transcript record')
        records.append(record)
    return records


def _same_path(a, b):
    return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))


def _validate(kind, records, workspace, path, expected_id=None):
    if kind == 'codex':
        headers = [r.get('payload') for r in records if r.get('type') == 'session_meta']
        if len(headers) != 1 or not isinstance(headers[0], dict):
            raise ValueError('Codex transcript missing or ambiguous header')
        header = headers[0]
        session_id = header.get('id')
        if not isinstance(session_id, str) or not _UUID.fullmatch(session_id):
            raise ValueError('invalid Codex session ID')
        if expected_id and session_id.lower() != expected_id.lower():
            raise ValueError('Codex session ID mismatch')
        if not Path(path).stem.lower().endswith('-' + session_id.lower()):
            raise ValueError('Codex filename identity mismatch')
        if not header.get('cwd') or not _same_path(header['cwd'], workspace):
            raise ValueError('Codex transcript cwd mismatch')
        return session_id
    if kind == 'omp':
        headers = [r for r in records if r.get('type') == 'session']
        if len(headers) != 1 or not headers[0].get('cwd') or not _same_path(headers[0]['cwd'], workspace):
            raise ValueError('OMP transcript cwd mismatch or missing header')
        return headers[0].get('id')
    session_id = expected_id or Path(path).stem
    if not _UUID.fullmatch(session_id):
        raise ValueError('invalid Claude session ID')
    relevant = [r for r in records if r.get('type') in ('user', 'assistant', 'attachment') and not r.get('isSidechain')]
    if not relevant or not any(r.get('cwd') for r in relevant):
        raise ValueError('Claude transcript lacks project identity')
    if any(r.get('sessionId') != session_id or not r.get('cwd') or not _same_path(r['cwd'], workspace) for r in relevant):
        raise ValueError('Claude transcript identity or cwd mismatch')
    return session_id


def codex_home(env=None):
    config = (env or {}).get('CODEX_HOME') or os.environ.get('CODEX_HOME')
    return (Path(config).expanduser() if config else Path.home() / '.codex').resolve()


def resolve_transcript(kind, native_session, workspace, env=None, cli_args=None):
    if kind not in ('omp', 'claude', 'codex'):
        raise ValueError('unsupported native transcript kind')
    refkind = None
    if isinstance(native_session, dict):
        refkind, value = native_session.get('kind'), native_session.get('value')
        if refkind not in ('path', 'id'):
            raise ValueError('invalid native reference')
    else:
        value = native_session
    if not isinstance(value, (str, os.PathLike)) or not str(value):
        raise ValueError('missing native reference')
    value = str(value)
    if refkind == 'path' or (refkind is None and (kind == 'omp' or not _UUID.fullmatch(value))):
        path = Path(value).expanduser()
        if not path.is_absolute() or path.suffix.lower() != '.jsonl':
            raise ValueError('native path must be absolute JSONL')
    else:
        if kind not in ('claude', 'codex') or not _UUID.fullmatch(value):
            raise ValueError('invalid native session ID')
        if kind == 'codex':
            home = codex_home(env)
            matches = list((home / 'sessions').glob('*/*/*/rollout-*-' + value + '.jsonl'))
            matches += list((home / 'archived_sessions').glob('rollout-*-' + value + '.jsonl'))
        else:
            config = (env or {}).get('CLAUDE_CONFIG_DIR') or os.environ.get('CLAUDE_CONFIG_DIR')
            projects = Path(config).expanduser() / 'projects' if config else Path.home() / '.claude' / 'projects'
            matches = list(projects.glob('*/' + value + '.jsonl'))
        if not matches:
            raise FileNotFoundError('native transcript not found')
        if len(matches) != 1:
            raise ValueError('ambiguous native transcript ID')
        path = matches[0]
    if not path.is_file():
        if kind == 'codex':
            home = codex_home(env)
            old = path.resolve()
            sid = path.stem[-36:]
            if _UUID.fullmatch(sid) and (old.is_relative_to(home / 'sessions')
                                         or old.parent == home / 'archived_sessions'):
                # Native archive/unarchive moves the file; retain the exact UUID identity.
                return resolve_transcript(kind, {'kind': 'id', 'value': sid}, workspace, env, cli_args)
        raise FileNotFoundError(str(path))
    _validate(kind, _read(path), workspace, path, value if refkind == 'id' or (kind in ('claude', 'codex') and _UUID.fullmatch(value)) else None)
    return path.resolve()


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ''.join(c.get('text', '') for c in content if isinstance(c, dict) and c.get('type') == 'text' and isinstance(c.get('text'), str))
    return ''


def _branch(kind, records):
    key, parent = ('id', 'parentId') if kind == 'omp' else ('uuid', 'parentUuid')
    nodes = [r for r in records if r.get(key) and (kind == 'omp' and r.get('type') != 'session' or kind == 'claude' and not r.get('isSidechain'))]
    if not nodes:
        return []
    index = {}
    for r in nodes:
        if r[key] in index:
            raise ValueError('duplicate transcript node')
        index[r[key]] = r
    chain, seen, current = [], set(), nodes[-1]
    while current:
        ident = current[key]
        if ident in seen:
            raise ValueError('transcript cycle')
        seen.add(ident)
        chain.append(current)
        parent_id = current.get(parent)
        if parent_id and parent_id not in index:
            raise ValueError('missing transcript parent')
        current = index.get(parent_id)
    return list(reversed(chain))


def _collect_codex(records, session_id, workspace, job_id, request_name, min_chars):
    # Associate records only within the latest explicitly started turn.
    starts = [i for i, r in enumerate(records) if r.get('type') == 'event_msg'
              and (r.get('payload') or {}).get('type') == 'task_started']
    if not starts:
        return None
    turn = records[starts[-1]:]
    start = turn[0].get('payload') or {}
    turn_id = start.get('turn_id')
    if not turn_id:
        return None
    contexts = [r.get('payload') for r in turn if r.get('type') == 'turn_context']
    if not contexts or any(not isinstance(c, dict) or c.get('turn_id') != turn_id or
                           not c.get('cwd') or not _same_path(c['cwd'], workspace) for c in contexts):
        return None
    users = []
    for r in turn:
        p = r.get('payload') or {}
        if r.get('type') == 'response_item' and p.get('type') == 'message' and p.get('role') == 'user':
            content = p.get('content')
            if not isinstance(content, list):
                return None
            users.append(''.join(c.get('text', '') for c in content if isinstance(c, dict)
                                 and c.get('type') == 'input_text' and isinstance(c.get('text'), str)))
    def has_token(text, value):
        return re.search(r'(?<![\w.-])' + re.escape(value) + r'(?![\w.-])', text) is not None
    if not users or not has_token(users[-1], job_id) or not has_token(users[-1], request_name):
        return None
    completed = [(i, r.get('payload') or {}) for i, r in enumerate(turn)
                 if r.get('type') == 'event_msg' and (r.get('payload') or {}).get('type') == 'task_complete']
    if len(completed) != 1 or completed[0][1].get('turn_id') != turn_id:
        return None
    end, event = completed[0]
    # A completion marker must remain the end of the visible turn. Only a
    # known non-output token counter may follow it; unfamiliar record types
    # are not evidence that the final is still current.
    if any(r.get('type') != 'event_msg' or not isinstance(r.get('payload'), dict) or
           r['payload'].get('type') != 'token_count' for r in turn[end + 1:]):
        return None
    if any(r.get('type') == 'event_msg' and (r.get('payload') or {}).get('type') in
           ('turn_aborted', 'task_failed', 'error') for r in turn):
        return None
    finals = [(i, r.get('payload') or {}) for i, r in enumerate(turn)
              if r.get('type') == 'response_item' and (r.get('payload') or {}).get('type') == 'message'
              and (r.get('payload') or {}).get('role') == 'assistant'
              and ((r.get('payload') or {}).get('phase') == 'final_answer' or
                   (r.get('payload') or {}).get('channel') == 'final')]
    if len(finals) != 1 or finals[0][0] >= end:
        return None
    pos, msg = finals[0]
    content = msg.get('content')
    if not isinstance(content, list) or not content or any(not isinstance(c, dict) or
            c.get('type') != 'output_text' or not isinstance(c.get('text'), str) for c in content):
        return None
    text = ''.join(c['text'] for c in content).strip()
    completed_text = event.get('last_agent_message')
    if len(text) < min_chars or not isinstance(completed_text, str) or completed_text.strip() != text:
        return None
    # Any new user message, tool activity, or assistant output after final makes
    # the proposed final ambiguous even if a completion marker follows.
    for r in turn[pos + 1:end]:
        p = r.get('payload') or {}
        if r.get('type') == 'response_item' and p.get('type') != 'reasoning':
            return None
    context = contexts[-1]
    return {'text': text, 'message_id': msg.get('id'), 'turn_id': turn_id, 'native_session_id': session_id,
            'model_observed': context.get('model'), 'effort_observed': context.get('effort'),
            'usage': None, 'duration_ms': event.get('duration_ms'),
            'ttft_ms': event.get('time_to_first_token_ms'), 'finality': 'task_complete'}


def collect_round(kind, transcript, workspace, job_id, request_file, min_chars=1):
    if kind not in ('omp', 'claude', 'codex') or not job_id or not request_file or min_chars < 1:
        raise ValueError('invalid collection arguments')
    try:
        records = _read(transcript)
        session_id = _validate(kind, records, workspace, transcript)
        chain = _branch(kind, records) if kind != 'codex' else None
    except (ValueError, OSError):
        return None
    request_name = str(request_file).replace('\\', '/').rsplit('/', 1)[-1]
    if kind == 'codex':
        return _collect_codex(records, session_id, workspace, job_id, request_name, min_chars)
    start = None
    for i, r in enumerate(chain):
        m = r.get('message') or {}
        role = m.get('role') if kind == 'omp' else r.get('type')
        if role == 'user':
            content = m.get('content')
            # Claude tool-result records carry user role but are not new prompts.
            if kind == 'claude' and isinstance(content, list) and any(isinstance(c, dict) and c.get('type') == 'tool_result' for c in content):
                continue
            text = _text(content)
            if job_id in text and request_name in text:
                start = i
            elif start is not None:
                start = None
    if start is None:
        return None
    after = chain[start + 1:]
    assistants = [r for r in after if (r.get('message') or {}).get('role') == 'assistant' and (kind == 'omp' or r.get('type') == 'assistant')]
    if not assistants:
        return None
    last = assistants[-1]
    msg = last['message']
    # A tool result after the final assistant means a response is still pending.
    tail = after[after.index(last) + 1:]
    if any((r.get('message') or {}).get('role') == 'toolResult' or
           (r.get('type') == 'user' and any(isinstance(c, dict) and c.get('type') == 'tool_result' for c in ((r.get('message') or {}).get('content') or []) if isinstance((r.get('message') or {}).get('content'), list))) for r in tail):
        return None
    content = msg.get('content')
    if not isinstance(content, list) or any(isinstance(c, dict) and c.get('type') in ('toolCall', 'tool_use', 'refusal') for c in content):
        return None
    reason = msg.get('stopReason') if kind == 'omp' else msg.get('stop_reason')
    if kind == 'omp':
        if reason != 'stop':
            return None
        finality = 'stop'
    elif reason in ('end_turn', 'stop_sequence'):
        finality = reason
    elif reason is None and after and after[-1] is last and msg.get('id'):
        # Only a complete persisted assistant tail can be treated as idle-tail.
        finality = 'idle-tail-text'
    else:
        return None
    if msg.get('error') or last.get('isApiErrorMessage') or last.get('isError') or msg.get('isError') or msg.get('refusal'):
        return None
    # Claude can persist invisible thinking and visible text as separate records
    # sharing a message ID. Accept only the unambiguous single-text case; never
    # read or combine the contents of the invisible blocks.
    if kind == 'claude' and msg.get('id'):
        same = [r for r in assistants if (r.get('message') or {}).get('id') == msg['id']]
        if len(same) > 1:
            if reason not in ('end_turn', 'stop_sequence') or same[-1] is not last:
                return None
            # Require the group to be adjacent on the selected branch and at
            # the end of the assistant sequence, with explicit parent links.
            first_index = chain.index(same[0])
            if chain[first_index:first_index + len(same)] != same or assistants[-len(same):] != same:
                return None
            if any(b.get('parentUuid') != a.get('uuid') for a, b in zip(same, same[1:])):
                return None
            request_ids = {r.get('requestId') for r in same if r.get('requestId')}
            models = {(r.get('message') or {}).get('model') for r in same if (r.get('message') or {}).get('model')}
            if len(request_ids) > 1 or len(models) > 1:
                return None
            for r in same:
                m = r['message']
                if (m.get('stop_reason') not in ('end_turn', 'stop_sequence') or
                    m.get('error') or r.get('isApiErrorMessage') or r.get('isError') or
                    m.get('isError') or m.get('refusal')):
                    return None
            for r in same[:-1]:
                blocks = r['message'].get('content')
                if not isinstance(blocks, list) or not blocks or any(
                    not isinstance(c, dict) or c.get('type') not in ('thinking', 'redacted_thinking') for c in blocks
                ):
                    return None
            if len(content) != 1 or not isinstance(content[0], dict) or content[0].get('type') != 'text' or not isinstance(content[0].get('text'), str):
                return None
    text = _text(content).strip()
    if len(text) < min_chars:
        return None
    effort = next((r.get('thinkingLevel') for r in reversed(chain[:chain.index(last) + 1]) if r.get('type') == 'thinking_level_change'), None) if kind == 'omp' else msg.get('effort')
    return {'text': text, 'message_id': msg.get('id') or last.get('id') or last.get('uuid'), 'native_session_id': session_id,
            'model_observed': msg.get('model'), 'effort_observed': effort, 'usage': msg.get('usage'),
            'duration_ms': msg.get('duration'), 'ttft_ms': msg.get('ttft'), 'finality': finality}
