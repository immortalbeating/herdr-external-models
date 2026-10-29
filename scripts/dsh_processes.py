"""Conservative process inspection and OS-owned metadata locks (stdlib only)."""
import base64
import json
import os
from pathlib import Path
import subprocess
import time


class FileLock:
    """Lock a persistent file; callers must keep model work outside this scope."""
    def __init__(self, path, timeout=5):
        self.path, self.timeout, self.file = Path(path), timeout, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a+b')
        self.file.seek(0, 2)
        if not self.file.tell():
            self.file.write(b'\0')
            self.file.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.file.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self.file.close()
                    self.file = None
                    raise TimeoutError('Timed out acquiring metadata lock: ' + str(self.path))
                time.sleep(min(.05, max(0, deadline - time.monotonic())))

    def __exit__(self, *_):
        try:
            self.file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()
            self.file = None


def process_snapshot():
    """Return a complete snapshot, or unavailable; partial reads never imply death."""
    try:
        if os.name != 'nt':
            return {'available': False, 'processes': {}, 'error': 'Process inspection unavailable on this platform'}
        script = """$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); @(Get-CimInstance Win32_Process | ForEach-Object { [pscustomobject]@{pid=[int]$_.ProcessId; parent_pid=[int]$_.ParentProcessId; name=[string]$_.Name; image=[string]$_.ExecutablePath; command_line=[string]$_.CommandLine; created_at=$(if ($_.CreationDate) {$_.CreationDate.ToUniversalTime().ToString('o')} else {''})} }) | ConvertTo-Json -Compress"""
        encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
        executable = str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe')
        result = subprocess.run([executable, '-NoProfile', '-EncodedCommand', encoded], capture_output=True,
                                text=True, encoding='utf-8', timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError('Windows process query failed')
        rows = json.loads(result.stdout.lstrip('\ufeff') or '[]')
        if isinstance(rows, dict):
            rows = [rows]
        return {'available': True, 'processes': {int(row['pid']): row for row in rows}}
    except Exception as exc:
        return {'available': False, 'processes': {}, 'error': type(exc).__name__ + ': process query unavailable'}


def process_identity(pid, snapshot=None):
    snapshot = process_snapshot() if snapshot is None else snapshot
    if not snapshot.get('available'):
        return {'pid': int(pid), 'unknown': True}
    return snapshot['processes'].get(int(pid))


def _norm(value):
    return str(value or '').replace('\\', '/').replace('"', '').replace("'", '').casefold()


def _name(info):
    return _norm(info.get('name') or info.get('image')).rsplit('/', 1)[-1].removesuffix('.exe')


def _matches(info, expected):
    if expected.get('pid') is not None and int(expected['pid']) != int(info['pid']):
        return False
    if not expected.get('created_at') or not info.get('created_at'):
        return None
    if info['created_at'] != expected['created_at']:
        return False
    if expected.get('image'):
        if not info.get('image'):
            return None
        return _norm(info['image']) == _norm(expected['image'])
    if expected.get('name'):
        if not info.get('name'):
            return None
        return _name(info) == _name(expected)
    return None


def _contains_command_path(command, path):
    return bool(path) and any(command[start:start + len(path)] == path and
                             (start == 0 or command[start - 1].isspace() or command[start - 1] == '=') and
                             (start + len(path) == len(command) or command[start + len(path)].isspace())
                             for start in range(len(command)))


def job_liveness(job, state, snapshot=None):
    snapshot = process_snapshot() if snapshot is None else snapshot
    outcomes = []
    for kind in ('worker', 'dsh'):
        expected = state.get(kind + '_identity') or {}
        pid = state.get(kind + '_pid') or expected.get('pid')
        if not pid:
            continue
        info = process_identity(pid, snapshot)
        if info is None:
            outcomes.append(False)
        elif info.get('unknown'):
            outcomes.append(None)
        elif expected:
            outcomes.append(_matches(info, expected))
        elif not info.get('command_line') or not (info.get('name') or info.get('image')):
            outcomes.append(None)
        elif kind == 'worker':
            command = _norm(info['command_line'])
            path = _norm(Path(job).resolve())
            # Token boundary prevents matching a sibling job with a shared prefix.
            matched_path = _contains_command_path(command, path)
            script = any(token.rsplit('/', 1)[-1] == 'dsh.py' for token in command.split())
            outcomes.append(_name(info) in ('python', 'python3', 'py', 'pythonw') and matched_path and script)
        else:
            binary = _norm(state.get('dsh_bin'))
            outcomes.append(_contains_command_path(_norm(info['command_line']), binary) and
                            _name(info) == 'node' and info.get('parent_pid') == state.get('worker_pid'))
    if True in outcomes:
        return {'state': 'alive', 'reason': 'Verified recorded process identity'}
    if not outcomes or None in outcomes:
        return {'state': 'unknown', 'reason': 'No inspectable recorded process identity'}
    return {'state': 'dead', 'reason': 'Recorded processes absent or unrelated'}


def pane_idle(process_info, snapshot=None):
    snapshot = process_snapshot() if snapshot is None else snapshot
    pid = process_info.get('shell_pid')
    if not pid or not snapshot.get('available'):
        return None
    shell = process_identity(pid, snapshot)
    if shell is None or shell.get('unknown'):
        return None
    if _name(shell) not in ('pwsh', 'powershell', 'cmd', 'sh', 'bash', 'zsh', 'fish', 'nu'):
        return False
    foreground = process_info.get('foreground_processes')
    if foreground is not None:
        for item in foreground:
            foreground_pid = item.get('pid') if isinstance(item, dict) else item
            if foreground_pid != pid:
                return False
    if any(info.get('parent_pid') == pid for info in snapshot['processes'].values()):
        return False
    return True
