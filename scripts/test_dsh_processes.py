"""Offline process contracts; no desktop input or live DSH jobs."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from dsh_processes import FileLock, job_liveness, pane_idle, process_identity


def snapshot(*rows):
    return {'available': True, 'processes': {row['pid']: row for row in rows}}


def proc(pid, name='python.exe', command='', parent=0, created='time1'):
    return dict(pid=pid, name=name, image='C:/' + name, command_line=command,
                parent_pid=parent, created_at=created)


class ProcessTests(unittest.TestCase):
    def test_legacy_worker_exact_job(self):
        with tempfile.TemporaryDirectory(prefix='job space ') as folder:
            job = Path(folder).resolve()
            command = f'python "C:/scripts/dsh.py" worker --job "{job}"'
            state = {'worker_pid': 42}
            self.assertEqual(job_liveness(job, state, snapshot(proc(42, command=command)))['state'], 'alive')
            self.assertEqual(job_liveness(job, state, snapshot(proc(42, command=command.replace(str(job), str(job) + '-other'))))['state'], 'dead')

    def test_reused_and_unavailable(self):
        self.assertEqual(job_liveness(Path('.'), {'worker_pid': 42}, snapshot(proc(42, 'other.exe')))['state'], 'unknown')
        self.assertEqual(job_liveness(Path('.'), {'worker_pid': 42}, snapshot(proc(42, 'other.exe', 'unrelated')))['state'], 'dead')
        unavailable = {'available': False, 'processes': {}}
        self.assertTrue(process_identity(42, unavailable)['unknown'])
        self.assertEqual(job_liveness(Path('.'), {'worker_pid': 42}, unavailable)['state'], 'unknown')

    def test_new_identity_reuse_and_image(self):
        expected = proc(42)
        state = {'worker_pid': 42, 'worker_identity': expected}
        self.assertEqual(job_liveness(Path('.'), state, snapshot(proc(42)))['state'], 'alive')
        self.assertEqual(job_liveness(Path('.'), state, snapshot(proc(42, created='time2')))['state'], 'dead')
        self.assertEqual(job_liveness(Path('.'), state, snapshot(proc(42, name='other.exe')))['state'], 'dead')
        self.assertEqual(job_liveness(Path('.'), {}, snapshot())['state'], 'unknown')
        self.assertIsNone(process_identity(42, snapshot()))

    def test_dsh_parent_required(self):
        state = {'worker_pid': 42, 'dsh_pid': 43, 'dsh_bin': 'C:/dsh/bin.js'}
        self.assertEqual(job_liveness(Path('.'), state, snapshot(proc(43, 'node.exe', 'node C:/dsh/bin.js', 42)))['state'], 'alive')
        self.assertEqual(job_liveness(Path('.'), state, snapshot(proc(43, 'node.exe', 'node C:/dsh/bin.js', 999)))['state'], 'dead')

    def test_pane_idle(self):
        shell = proc(1, 'pwsh.exe')
        self.assertTrue(pane_idle({'shell_pid': 1}, snapshot(shell)))
        self.assertFalse(pane_idle({'shell_pid': 1}, snapshot(shell, proc(2, parent=1))))
        self.assertFalse(pane_idle({'shell_pid': 1, 'foreground_processes': [{'pid': 3}]}, snapshot(shell)))
        self.assertFalse(pane_idle({'shell_pid': 2}, snapshot(proc(2, 'node.exe'))))
        self.assertIsNone(pane_idle({'shell_pid': 1}, {'available': False}))

    def test_lock_exclusion_and_release(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'nested' / 'meta.lock'
            script = ('from dsh_processes import FileLock\nimport sys\n'
                      'try:\n with FileLock(sys.argv[1], timeout=.15): pass\n'
                      'except TimeoutError: sys.exit(7)\n')
            with FileLock(path):
                result = subprocess.run([sys.executable, '-c', script, str(path)], cwd=Path(__file__).parent, capture_output=True)
                self.assertEqual(result.returncode, 7, result.stderr)
            with FileLock(path, timeout=.1):
                self.assertTrue(path.exists())

    def test_lock_os_releases_after_abrupt_exit(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'meta.lock'
            script = ('from dsh_processes import FileLock\nimport os, sys\n'
                      'with FileLock(sys.argv[1]):\n os._exit(3)\n')
            result = subprocess.run([sys.executable, '-c', script, str(path)], cwd=Path(__file__).parent, capture_output=True)
            self.assertEqual(result.returncode, 3, result.stderr)
            with FileLock(path, timeout=.1):
                self.assertTrue(path.exists())


if __name__ == '__main__':
    unittest.main()
