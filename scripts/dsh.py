#!/usr/bin/env python3
"""Run DSH headless in an owned, unfocused Herdr workspace (no shell wrappers)."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from contextlib import contextmanager, nullcontext

import layout
from record_redaction import redact, redact_value, secret_variants
import completion_checks
from dsh_processes import FileLock, job_liveness, pane_idle, process_identity, process_snapshot

ACTIVE = ('starting', 'working', 'dispatch_uncertain', 'monitor_lost')
SETTLED = ('succeeded', 'failed', 'interrupted')
LAUNCH_GRACE = 60


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return os.path.normcase(str(Path(value).resolve()))


class RedactedSink:
    """Bounded cross-chunk/line secret recognition for non-JSON streams."""
    def __init__(self, target):
        self.target = target
        patterns = list(secret_variants())
        self.keep = max(map(len, patterns), default=1)
        self.pending = ''

    def feed(self, chunk='', final=False):
        self.pending = redact(self.pending + chunk)
        if final:
            self.target.write(self.pending)
            self.pending = ''
        elif len(self.pending) > self.keep:
            self.target.write(self.pending[:-self.keep])
            self.pending = self.pending[-self.keep:]
        self.target.flush()


def redact_stream(source, target, errors=None):
    sink = RedactedSink(target) if target is not None else None
    while True:
        chunk = source.read(4096)
        if sink is not None:
            try:
                sink.feed(chunk, final=not chunk)
            except Exception as exc:
                if errors is None:
                    raise
                errors.append(exc)
                sink = None  # Keep draining even when the output file is broken.
        if not chunk:
            return


def save(path, obj):
    # OS locks are released after a crash; an old lock file is not a live owner.
    with FileLock(path.with_suffix('.guard')):
        store_locked(path, obj)


def store_locked(path, obj):
    if path.exists():
        previous = json.loads(path.read_text(encoding='utf-8'))
        if obj.get('state') == 'dispatch_uncertain' and previous.get('state') != 'starting':
            obj = {**previous, 'dispatch_error': obj['dispatch_error'], 'recovery': obj['recovery']}
        elif 'dispatch_error' in previous:
            obj = {**obj, 'dispatch_error': previous['dispatch_error'], 'recovery': previous['recovery']}
        for key in ('superseded_by', 'review_outcome', 'review_file', 'reviewed_at'):
            if key in previous and key not in obj:
                obj[key] = previous[key]
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(redact_value(obj), ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def load(job):
    return json.loads((job / 'job.json').read_text(encoding='utf-8'))


def native(value):
    found = shutil.which(value) or value
    if not Path(found).is_file() or Path(found).suffix.lower() in ('.cmd', '.bat', '.ps1'):
        raise ValueError('A native executable is required: ' + value)
    return str(Path(found).resolve())


def dsh_bin(value=None):
    candidates = [Path(value)] if value else []
    if not value:
        for item in os.environ.get('PATH', '').split(os.pathsep):
            candidates.append(Path(item) / 'node_modules/@deepseek-ai/dsh/lib/bin.js')
        candidates.append(Path(os.environ.get('APPDATA', str(Path.home()))) / 'npm/node_modules/@deepseek-ai/dsh/lib/bin.js')
    for path in candidates:
        if path.is_file() and path.suffix == '.js':
            return str(path.resolve())
    raise ValueError('DSH JavaScript entry not found; pass --dsh-bin')


def herdr(state, *args):
    proc = subprocess.run([state['herdr'], '--session', state['session'], *args],
                          capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=45)
    if proc.returncode:
        raise RuntimeError(redact(proc.stderr or proc.stdout))
    # Herdr mutation commands can succeed silently (including pane run/report).
    if not proc.stdout.strip():
        return {}
    data = json.loads(proc.stdout)
    if data.get('error'):
        raise RuntimeError(redact(json.dumps(data['error'])))
    return data.get('result', data)


def report(state, status):
    args = ['pane', 'report-agent', state['pane_id'], '--source', 'custom:dsh-headless',
            '--agent', 'dsh', '--state', status]
    if state.get('dsh_session_id'):
        args += ['--agent-session-id', state['dsh_session_id']]
    herdr(state, *args)


def records(workspace, project_root=None):
    found = {}
    for root in {Path(workspace).resolve(), Path(project_root or workspace).resolve()}:
        for path in (root / '.agent-runs').glob('dsh-*/job.json'):
            try:
                found[str(path.parent.resolve())] = (path.parent, load(path.parent))
            except (OSError, ValueError):
                # A corrupt record cannot authorize closing a terminal.
                continue
    return list(found.values())


def record_time(job, state):
    if state.get('created_at'):
        return datetime.fromisoformat(state['created_at']).timestamp()
    stat = job.stat()
    return getattr(stat, 'st_birthtime', stat.st_mtime)


def effective_state(job, state, snapshot=None, persist=False):
    state = dict(state)
    if state['state'] not in ACTIVE:
        return state
    if not state.get('worker_pid') and time.time() - record_time(job, state) < LAUNCH_GRACE:
        state['liveness'] = 'launching'
        return state
    probe = job_liveness(job, state, snapshot)
    state['liveness'] = probe['state']
    abandoned = False
    if (probe['state'] == 'unknown' and not state.get('worker_pid') and not state.get('dsh_pid')
            and not (job / '.worker-started').exists() and time.time() - record_time(job, state) >= LAUNCH_GRACE):
        try:
            abandoned = snapshot is not None and snapshot.get('available') and owned_pane(job, state, snapshot, allow_unstarted=True) is not None
        except (ValueError, RuntimeError, OSError):
            pass
    if probe['state'] == 'dead' or abandoned:
        original = dict(state)
        state.update(state='interrupted', interrupted_at=now(), interruption_reason=probe['reason'],
                     recovery='Recorded processes have stopped. Inspect saved events, then explicitly resume the native session; do not resend automatically.')
        if abandoned:
            state.update(interruption_kind='unstarted', interruption_reason='Launch expired without a worker claiming the verified idle owned pane')
        if persist:
            path = job / 'job.json'
            with FileLock(path.with_suffix('.guard')):
                current = load(job)
                fields = ('state', 'worker_pid', 'dsh_pid', 'worker_identity', 'dsh_identity', 'superseded_by')
                if any(current.get(key) != original.get(key) for key in fields):
                    return current
                current.update({key: state[key] for key in ('state', 'interrupted_at', 'interruption_reason', 'recovery', 'liveness', 'interruption_kind') if key in state})
                store_locked(path, current)
                state = current
    return state


def identity(pid):
    info = process_identity(pid)
    if not info or info.get('unknown'):
        return None
    return {key: info.get(key) for key in ('pid', 'created_at', 'image', 'name')}


def progress(job, state, phase, event=None):
    """Expose lifecycle/tool names, never raw prompts, arguments or token streams."""
    phase = re.sub(r'[^\w .:/=;-]', '', str(phase).replace('\\', '/'))[:240]
    view = dict(job=job.name, task=state.get('task_name', job.name), phase=phase,
                updated_at=now(), dsh_session_id=state.get('dsh_session_id'))
    save(job / 'progress.json', view)
    try:
        print('[DSH] ' + redact(view['task']) + ' | ' + phase + ' | ' + view['updated_at'], flush=True)
    except (OSError, UnicodeError):
        pass


def event_phase(event):
    kind = event.get('type', event.get('event', ''))
    data = event.get('data') or event
    if kind == 'status':
        return data.get('phase', 'status')
    if kind in ('tool_call', 'tool_start', 'tool_result', 'tool_end'):
        name = data.get('toolName') or data.get('name') or data.get('tool') or data.get('status') or ''
        if isinstance(name, dict):
            name = name.get('name', '')
        return kind + ' ' + str(name)
    if kind == 'thinking':
        return 'thinking'
    return None


def summary(job, state, timed_out=False):
    return {'job': str(job), 'state': state['state'], 'timed_out': timed_out,
            'result': str(job / 'result.md') if (job / 'result.md').exists() else None,
            'dsh_session_id': state.get('dsh_session_id'), 'exit_code': state.get('exit_code'),
            **{key: state[key] for key in ('workspace_id', 'tab_id', 'pane_id', 'terminal_id',
               'task_name', 'liveness', 'review_outcome', 'close_reason', 'recovery') if key in state}}


def status(job, wait=0):
    deadline = time.monotonic() + wait
    last_probe, snapshot = 0, None
    while True:
        state = load(job)
        if state['state'] in ACTIVE and (state.get('worker_pid') or time.time() - record_time(job, state) >= LAUNCH_GRACE):
            if time.monotonic() - last_probe >= 5 or snapshot is None:
                snapshot, last_probe = process_snapshot(), time.monotonic()
            state = effective_state(job, state, snapshot, persist=True)
        if state['state'] not in ('starting', 'working') or time.monotonic() >= deadline:
            return summary(job, state, state['state'] in ('starting', 'working') and wait > 0)
        time.sleep(min(.2, max(0, deadline - time.monotonic())))


def event_outcome(event, outcome):
    """Only explicit final success is authoritative; unknown events never imply success."""
    kind = event.get('type', event.get('event'))
    data = event.get('data', event)
    sid = event.get('sessionId') or event.get('session_id') or data.get('sessionId') or data.get('session_id')
    if sid:
        outcome['session_id'] = sid
    if kind == 'status' and data.get('phase') == 'turn_end':
        outcome['success'] = data.get('reason', {}).get('kind') == 'completed'
    if kind == 'final':
        outcome['terminal'] = True
        outcome['text'] = data.get('text', data.get('result', ''))


def worker(job):
    # Exclusive launch claim prevents accidentally repeating a model request.
    with FileLock((job / 'job.json').with_suffix('.guard')):
        if (job / '.worker-started').exists():
            raise FileExistsError('This job already claimed a worker')
        state = load(job)
        if state['state'] not in ('starting', 'dispatch_uncertain'):
            raise ValueError('Job is no longer launchable; refusing a delayed request')
        state.update(state='working', worker_pid=os.getpid(), worker_identity=identity(os.getpid()), started_at=now())
        store_locked(job / 'job.json', state)
        fd = os.open(job / '.worker-started', os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    outcome = {}
    proc = None
    heartbeat_stop = None
    try:
        report(state, 'working')
        progress(job, state, 'starting model')
        env = os.environ.copy()
        if env.get('ALL_PROXY', '').lower().startswith('socks5'):
            replacement = env.get('HTTP_PROXY') or env.get('HTTPS_PROXY')
            if replacement:
                env['ALL_PROXY'] = replacement
            else:
                env.pop('ALL_PROXY', None)
        command = [state['node'], state['dsh_bin'], '--profile', state['profile'], '--json']
        if state.get('dsh_session_id'):
            command += ['--session-id', state['dsh_session_id']]
        # Temporary raw stderr is avoided: drain both streams concurrently and redact before writing.
        import threading
        proc = subprocess.Popen(command, cwd=state['workspace'], env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
        state['dsh_pid'] = proc.pid
        state['dsh_identity'] = identity(proc.pid)
        save(job / 'job.json', state)
        heartbeat_stop = threading.Event()
        def heartbeat():
            while not heartbeat_stop.wait(5):
                try:
                    progress(job, state, 'working; waiting for model/tools')
                except (OSError, ValueError, TimeoutError):
                    pass
        heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
        heartbeat_thread.start()
        stream_errors = []
        def stderr():
            out = None
            try:
                try:
                    out = (job / 'stderr.log').open('a', encoding='utf-8')
                except Exception as exc:
                    stream_errors.append(exc)
                redact_stream(proc.stderr, out, stream_errors)
            except Exception as exc:
                stream_errors.append(exc)
            finally:
                if out is not None:
                    try:
                        out.close()
                    except Exception as exc:
                        stream_errors.append(exc)
        thread = threading.Thread(target=stderr, daemon=True)
        thread.start()
        def feed():
            try:
                proc.stdin.write((job / 'prompt.md').read_text(encoding='utf-8'))
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
        last_phase, last_progress = None, 0
        with (job / 'events.jsonl').open('a', encoding='utf-8') as events:
            non_json = RedactedSink(events)
            for line in proc.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    non_json.feed(line)
                    continue
                non_json.feed(final=True)
                events.write(json.dumps(redact_value(event), ensure_ascii=False) + '\n'); events.flush()
                try:
                    event_outcome(event, outcome)
                except (ValueError, TypeError, AttributeError):
                    continue
                phase = event_phase(event)
                if phase and (phase != last_phase or time.monotonic() - last_progress >= 2):
                    try:
                        progress(job, state, phase)
                        last_phase, last_progress = phase, time.monotonic()
                    except (OSError, ValueError, TimeoutError):
                        pass
                if outcome.get('session_id') and outcome['session_id'] != state.get('dsh_session_id'):
                    state['dsh_session_id'] = outcome['session_id']
                    save(job / 'job.json', state)
                    try:
                        report(state, 'working')
                    except Exception as exc:
                        state['lifecycle_error'] = redact(str(exc))
            non_json.feed(final=True)
        code = proc.wait()
        thread.join(); feeder.join()
        if stream_errors:
            raise RuntimeError('stderr monitor failed: ' + str(stream_errors[0]))
        state.update(exit_code=code, finished_at=now(), state='succeeded' if code == 0 and outcome.get('terminal') and outcome.get('success') else 'failed')
        if state['state'] == 'succeeded':
            (job / 'result.md').write_text(redact(str(outcome.get('text', ''))), encoding='utf-8')
    except Exception as exc:
        alive = proc is not None and proc.poll() is None
        state.update(state='monitor_lost' if alive else 'failed', error=redact(str(exc)))
        if alive:
            state['recovery'] = 'DSH process may still be running (pid %s). Inspect the process and pane; never resend automatically.' % proc.pid
    finally:
        if heartbeat_stop is not None:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=6)
        save(job / 'job.json', state)
        try:
            progress(job, state, state['state'] + '; result=' + str(job / 'result.md'))
        except (OSError, ValueError, TimeoutError):
            pass
        try:
            if state['state'] in ('succeeded', 'failed'):
                report(state, 'idle')
                herdr(state, 'pane', 'release-agent', state['pane_id'], '--source', 'custom:dsh-headless', '--agent', 'dsh')
        except Exception as exc:
            state['lifecycle_error'] = redact(str(exc))
            save(job / 'job.json', state)
    return summary(job, state)


def server_key(state):
    config = os.environ.get('HERDR_CONFIG_PATH') or str(Path(os.environ.get('APPDATA', str(Path.home() / '.config'))) / 'herdr' / 'config.toml')
    return layout.key(canonical(config) + '|' + state['session'])


def layout_path(state):
    return Path(state['project_root']) / '.agent-runs' / 'dsh-layout.json'


def read_layout(state):
    path = layout_path(state)
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def slot_for(state, cache):
    return cache.get(server_key(state), {}).get('tasks', {}).get(state.get('task_key'), {}).get('panes', {}).get(state.get('layout_slot'))


@contextmanager
def project_lock(root):
    """Cooperate with the normal adapter while recovering only provably abandoned legacy locks."""
    root = Path(root)
    with FileLock(root / '.dsh-layout.guard', timeout=45):
        legacy = root / '.layout.lock'
        if legacy.exists():
            before = legacy.stat()
            try:
                pid = int(legacy.read_text(encoding='ascii').strip())
                owner = process_identity(pid)
                stale = owner is None
                if owner and not owner.get('unknown') and owner.get('created_at'):
                    stale = datetime.fromisoformat(owner['created_at']).timestamp() > before.st_mtime + .001
                if stale and legacy.exists() and legacy.stat().st_mtime_ns == before.st_mtime_ns:
                    legacy.unlink()
            except (ValueError, OSError):
                pass
        with layout.layout_lock(root):
            yield


@contextmanager
def control_lock(job):
    state = load(job)
    guard = Path(state['workspace']) / '.agent-runs' / '.dsh-dispatch.guard'
    with FileLock(guard, timeout=45):
        state = load(job)
        with project_lock(layout_path(state).parent) if state.get('project_root') else nullcontext():
            yield


def owned_pane(job, state, snapshot=None, allow_unstarted=False):
    """Verify saved identity, or conservatively recognize a restored empty owned shell."""
    workspaces = herdr(state, 'workspace', 'list')['workspaces']
    ws = next((item for item in workspaces if item.get('workspace_id') == state.get('workspace_id')), None)
    if ws is None:
        return None
    panes = herdr(state, 'pane', 'list', '--workspace', state['workspace_id'])['panes']
    pane = next((item for item in panes if item.get('pane_id') == state.get('pane_id')), None)
    if pane is None:
        return None
    # A previous round must never close or reuse its successor's terminal.
    if state.get('superseded_by'):
        raise ValueError('Pane was reassigned to a later job')
    if state.get('project_root'):
        slot = slot_for(state, read_layout(state))
        if slot and canonical(slot['job']) != canonical(job):
            raise ValueError('Layout binding belongs to a later job')
    same_terminal = pane.get('terminal_id') == state.get('terminal_id')
    if not same_terminal:
        legacy = ws.get('label') == job.name and len(panes) == 1
        marked = False
        if state.get('project_root'):
            tabs = herdr(state, 'tab', 'list', '--workspace', state['workspace_id'])['tabs']
            tab = next((item for item in tabs if item.get('tab_id') == pane.get('tab_id')), {})
            marked = (ws.get('label', '').endswith('[p-' + state['project_key'] + ']') and
                      tab.get('label', '').endswith('[t-' + state['task_key'] + ']'))
        if not (legacy or marked) or canonical(pane.get('cwd', '')) != canonical(state['workspace']):
            raise ValueError('Workspace ownership changed; refusing to adopt restored pane')
    abandoned = (state.get('state') == 'interrupted' and state.get('interruption_kind') == 'unstarted'
                 and not state.get('worker_pid') and not state.get('dsh_pid')
                 and not (job / '.worker-started').exists() and time.time() - record_time(job, state) >= LAUNCH_GRACE
                 and snapshot is not None and snapshot.get('available'))
    if not (allow_unstarted or abandoned) and job_liveness(job, state, snapshot)['state'] != 'dead':
        raise ValueError('Job processes are live or unconfirmed')
    agents = herdr(state, 'agent', 'list')['agents']
    if pane.get('agent_status') in ('working', 'blocked') or any(item.get('terminal_id') == pane.get('terminal_id') for item in agents):
        raise ValueError('An agent still owns this terminal; wait for its release before closing')
    process_info = herdr(state, 'pane', 'process-info', '--pane', state['pane_id'])['process_info']
    if pane_idle(process_info, snapshot) is not True:
        raise ValueError('Pane is busy or its processes cannot be confirmed idle')
    return dict(pane, restored=not same_terminal, workspace_label=ws.get('label'), workspace_pane_count=len(panes))


def allocate(job, state, previous=None):
    """Project workspace -> task tab -> one reusable pane per conversation."""
    path = layout_path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    with project_lock(path.parent):
        cache = read_layout(state)
        binding = cache.setdefault(server_key(state), {})
        workspaces = herdr(state, 'workspace', 'list')['workspaces']
        matches = [item for item in workspaces if item.get('label', '').endswith('[p-' + state['project_key'] + ']')]
        if len(matches) > 1:
            raise ValueError('Multiple project workspaces match; inspect layout before dispatch')
        ws = matches[0] if matches else None
        created_pane, created_tab = None, None
        if ws is None:
            created = herdr(state, 'workspace', 'create', '--cwd', state['workspace'], '--label',
                            state['project_name'] + ' [p-' + state['project_key'] + ']', '--no-focus')
            ws, created_tab, created_pane = created['workspace'], created['tab'], created['root_pane']
            binding.clear()
            binding.update(workspace_id=ws['workspace_id'], owns_workspace=True, tasks={})
        elif binding.get('workspace_id') != ws['workspace_id']:
            binding.clear()
            binding.update(workspace_id=ws['workspace_id'], owns_workspace=False, tasks={})
        state.update(workspace_id=ws['workspace_id'], owns_workspace=binding.get('owns_workspace', False))
        tasks = binding.setdefault('tasks', {})
        task = tasks.setdefault(state['task_key'], {'panes': {}})
        if created_tab is not None:
            state.update(tab_id=created_tab['tab_id'], owns_tab=True)
            task['owns_tab'] = True
            herdr(state, 'tab', 'rename', created_tab['tab_id'], state['task_name'] + ' [t-' + state['task_key'] + ']')
        old_slot = task.setdefault('panes', {}).get(state['layout_slot'])
        if old_slot:
            old_job = Path(old_slot['job'])
            old_state = effective_state(old_job, load(old_job), persist=True)
            pane = owned_pane(old_job, old_state, process_snapshot())
            if pane is not None:
                old_state['superseded_by'] = str(job)
                save(old_job / 'job.json', old_state)
                created_pane = pane
                state.update(tab_id=pane['tab_id'], owns_tab=task.get('owns_tab', False))
        if created_pane is None:
            tabs = herdr(state, 'tab', 'list', '--workspace', state['workspace_id'])['tabs'] if created_tab is None else []
            tab_matches = [item for item in tabs if item.get('label', '').endswith('[t-' + state['task_key'] + ']')]
            if len(tab_matches) > 1:
                raise ValueError('Multiple task tabs match; inspect before dispatch')
            tab = created_tab or (tab_matches[0] if tab_matches else None)
            if tab is None:
                created = herdr(state, 'tab', 'create', '--workspace', state['workspace_id'], '--cwd',
                                state['workspace'], '--label', state['task_name'] + ' [t-' + state['task_key'] + ']', '--no-focus')
                tab, created_pane = created['tab'], created['root_pane']
                task['owns_tab'] = True
            if created_pane is None:
                panes = herdr(state, 'pane', 'list', '--workspace', state['workspace_id'])['panes']
                base = next(item for item in panes if item.get('tab_id') == tab['tab_id'])
                created = herdr(state, 'pane', 'split', base['pane_id'], '--direction', 'right', '--cwd', state['workspace'], '--no-focus')
                created_pane = created.get('pane', created.get('root_pane'))
            state.update(tab_id=tab['tab_id'], owns_tab=task.get('owns_tab', False))
        state.update(pane_id=created_pane['pane_id'], terminal_id=created_pane['terminal_id'])
        task.update(tab_id=state['tab_id'])
        task['panes'][state['layout_slot']] = dict(job=str(job), pane_id=state['pane_id'], terminal_id=state['terminal_id'])
        save(path, cache)
        herdr(state, 'pane', 'rename', state['pane_id'], 'DSH · ' + state['task_name'])
        save(job / 'job.json', state)


def run(args):
    workspace, prompt = Path(args.workspace), Path(args.prompt_file)
    if not workspace.is_absolute() or not workspace.is_dir() or not prompt.is_absolute() or not prompt.is_file():
        raise ValueError('workspace and prompt-file must be existing absolute paths')
    project_root = layout.project_root(str(workspace.resolve()), getattr(args, 'project_root', None))
    state = dict(adapter_version=2, state='starting', created_at=now(), workspace=str(workspace.resolve()), session=args.session,
                 profile=args.profile, dsh_session_id=args.session_id, herdr=native(args.herdr),
                 node=native(args.node), dsh_bin=dsh_bin(args.dsh_bin), project_root=project_root)
    job = Path(project_root) / '.agent-runs' / ('dsh-' + uuid.uuid4().hex[:12])
    # All callers for this execution directory share the short dispatch reservation.
    guard = workspace / '.agent-runs' / '.dsh-dispatch.guard'
    with FileLock(guard, timeout=45):
        existing = records(workspace, project_root)
        snapshot = process_snapshot()
        task_id = getattr(args, 'task_id', None)
        matching = [(path, item) for path, item in existing
                    if canonical(item.get('workspace', path.parent)) == canonical(workspace)
                    and item.get('profile') == args.profile
                    and ((args.session_id and item.get('dsh_session_id') == args.session_id)
                         or (task_id and item.get('task_id') == task_id))]
        for path, item in matching:
            checked = effective_state(path, item, snapshot, persist=True)
            live = job_liveness(path, checked, snapshot)['state']
            if live == 'alive' or (checked['state'] in ACTIVE and live != 'dead'):
                raise ValueError('Conversation/task already has a live or unconfirmed request: ' + str(path))
        previous = max(matching, key=lambda pair: record_time(*pair), default=(None, {}))[1]
        state['task_id'] = task_id or previous.get('task_id') or ('dsh-' + args.session_id if args.session_id else job.name)
        state['task_name'] = getattr(args, 'task_name', None) or previous.get('task_name') or prompt.stem
        state['project_name'] = getattr(args, 'project_name', None) or previous.get('project_name') or Path(project_root).name
        state['project_key'] = layout.key(os.path.normcase(project_root))
        state['task_key'] = layout.key(state['task_id'])
        state['layout_slot'] = previous.get('layout_slot') or layout.key(canonical(workspace) + '|' + (args.session_id or state['task_id']))
        job.mkdir(parents=True)
        (job / 'prompt.md').write_text(redact(prompt.read_text(encoding='utf-8-sig')), encoding='utf-8')
        for name in ('events.jsonl', 'stderr.log'):
            (job / name).touch()
        save(job / 'job.json', state)
        try:
            allocate(job, state, previous)
        except Exception as exc:
            state.update(state='create_failed', error=redact(str(exc)), recovery='Inspect the project/task layout; creation may have reached the server. Do not retry automatically.')
            save(job / 'job.json', state)
            return summary(job, state)
        def quote(text):
            return "'" + str(text).replace("'", "''") + "'"
        script = '& ' + ' '.join(quote(x) for x in [sys.executable, str(Path(__file__).resolve()), 'worker', '--job', str(job)])
        encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
        try:
            herdr(state, 'pane', 'run', state['pane_id'], 'powershell.exe -NoProfile -EncodedCommand ' + encoded)
        except Exception as exc:
            current = load(job)
            current['state'] = 'dispatch_uncertain'
            current.update(dispatch_error=redact(str(exc)), recovery='Inspect this pane/job; never resend automatically.')
            save(job / 'job.json', current)
            return summary(job, load(job))
    return status(job, args.timeout)


def close(job):
    with control_lock(job):
        return close_locked(job)


def close_locked(job):
    snapshot = process_snapshot()
    state = effective_state(job, load(job), snapshot, persist=True)
    if state['state'] == 'closed':
        return summary(job, state)
    if state['state'] not in SETTLED:
        raise ValueError('Refusing to close a working/unconfirmed job')
    pane = owned_pane(job, state, snapshot)
    if pane is not None:
        if (pane['workspace_label'] == job.name or state.get('owns_workspace')) and pane['workspace_pane_count'] == 1:
            herdr(state, 'workspace', 'close', state['workspace_id'])
        else:
            panes = herdr(state, 'pane', 'list', '--workspace', state['workspace_id'])['panes']
            tab_panes = [item for item in panes if item.get('tab_id') == pane['tab_id']]
            if state.get('owns_tab') and len(tab_panes) == 1:
                herdr(state, 'tab', 'close', pane['tab_id'])
            else:
                herdr(state, 'pane', 'close', state['pane_id'])
    state.update(execution_state=state['state'], state='closed', closed_at=now(),
                 close_reason='verified idle owned layout' if pane else 'layout already absent')
    save(job / 'job.json', state)
    if state.get('project_root'):
        path = layout_path(state)
        cache = read_layout(state)
        task = cache.get(server_key(state), {}).get('tasks', {}).get(state['task_key'], {})
        slot = task.get('panes', {}).get(state['layout_slot'])
        if slot and canonical(slot['job']) == canonical(job):
            task['panes'].pop(state['layout_slot'])
            save(path, cache)
    return summary(job, state)


def finish(job, review_file, outcome, keep_pane=False, related_jobs=()):
    with control_lock(job):
        result = finish_locked(job, review_file, outcome, keep_pane)
        report = completion_checks.finish_report(job, related_jobs, expect_closed=not keep_pane)
        save(job / 'finish-check.json', report)
        result.update(finish_check=report['status'], finish_check_file=str(job / 'finish-check.json'),
                      checked_jobs=report['checked_jobs'])
        return result


def finish_locked(job, review_file, outcome, keep_pane=False):
    status(job)
    execution = load(job)
    if execution['state'] not in (*SETTLED, 'closed'):
        raise ValueError('Review requires an ended or interrupted request')
    if outcome == 'completed' and (execution.get('execution_state', execution['state']) != 'succeeded'
                                   or not (job / 'result.md').read_text(encoding='utf-8').strip()):
        raise ValueError('Completed review requires a successful nonempty result')
    review = Path(review_file)
    if not review.is_absolute() or not review.is_file():
        raise ValueError('review-file must be an existing absolute file')
    target = job / ('review-' + uuid.uuid4().hex[:10] + '.md')
    target.write_text(redact(review.read_text(encoding='utf-8-sig')), encoding='utf-8')
    execution.update(review_outcome=outcome, review_file=str(target), reviewed_at=now())
    save(job / 'job.json', execution)
    if not keep_pane and execution['state'] != 'closed':
        if execution.get('superseded_by'):
            execution['close_reason'] = 'kept: pane belongs to a later round'
            save(job / 'job.json', execution)
        else:
            return close_locked(job)
    return summary(job, execution)


def prune(workspace, session='default', project_root=None, apply=False):
    """Default is a read-only preview. Never resend or stop a model request."""
    rows = []
    snapshot = process_snapshot()
    for job, original in records(workspace, project_root):
        if original.get('session') != session or original['state'] == 'closed':
            continue
        state = effective_state(job, original, snapshot, persist=False)
        probe = job_liveness(job, state, snapshot)['state']
        row = dict(job=str(job), state=original['state'], effective_state=state['state'],
                   liveness=probe, pane_id=state.get('pane_id'), action='keep')
        try:
            if state['state'] not in SETTLED:
                raise ValueError('Request is live, launching or unconfirmed')
            pane = owned_pane(job, state, snapshot)
            row.update(action='close' if pane else 'record_closed', reason='verified idle owned layout' if pane else 'layout absent')
            if apply:
                result = close(job)  # Recheck current identity and process tree before mutation.
                row.update(action='closed', effective_state=result['state'])
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            row['reason'] = redact(str(exc))
        rows.append(row)
    return dict(apply=apply, jobs=rows, closed=sum(row['action'] == 'closed' for row in rows),
                candidates=sum(row['action'] in ('close', 'record_closed') for row in rows))


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    start = sub.add_parser('run')
    for name in ('workspace', 'prompt-file', 'session'):
        start.add_argument('--' + name, required=True)
    start.add_argument('--session-id')
    start.add_argument('--profile', default='herdr')
    start.add_argument('--timeout', type=float, default=300)
    start.add_argument('--dsh-bin')
    start.add_argument('--node', default='node')
    start.add_argument('--herdr', default='herdr')
    for name in ('project-root', 'project-name', 'task-id', 'task-name'):
        start.add_argument('--' + name)
    for name in ('status', 'close', 'worker'):
        p = sub.add_parser(name)
        p.add_argument('--job', required=True, type=Path)
        if name == 'status':
            p.add_argument('--wait', type=float, default=0)
    review = sub.add_parser('finish')
    review.add_argument('--job', required=True, type=Path)
    review.add_argument('--review-file', required=True)
    review.add_argument('--outcome', required=True, choices=('completed', 'needs_work'))
    review.add_argument('--keep-pane', action='store_true')
    review.add_argument('--related-job', action='append', default=[], help='also check an explicit related job; no directory scan')
    cleanup = sub.add_parser('prune')
    cleanup.add_argument('--workspace', required=True)
    cleanup.add_argument('--session', default='default')
    cleanup.add_argument('--project-root')
    cleanup.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'run':
            result = run(args)
        elif args.command == 'status':
            result = status(args.job, args.wait)
        elif args.command == 'close':
            result = close(args.job)
        elif args.command == 'finish':
            result = finish(args.job, args.review_file, args.outcome, args.keep_pane, args.related_job)
        elif args.command == 'prune':
            result = prune(args.workspace, args.session, args.project_root, args.apply)
        else:
            result = worker(args.job)
        print(json.dumps(redact_value(result), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'error': redact(str(exc))}), file=sys.stderr)
        return 1
    return exit_code(result)


def exit_code(result):
    if result.get('finish_check') == 'attention':
        return 2
    if result.get('state') in ('starting', 'working'):
        return 2
    if result.get('state') in ('failed', 'create_failed', 'dispatch_uncertain', 'monitor_lost', 'interrupted'):
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
