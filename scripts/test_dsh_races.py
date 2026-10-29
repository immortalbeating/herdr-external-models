"""Deterministic races and legacy lock recovery; no external processes."""
from contextlib import contextmanager
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch
import dsh
from dsh_processes import FileLock


class RaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.job = self.root / '.agent-runs' / 'dsh-race'; self.job.mkdir(parents=True)
        self.state = dict(state='working', worker_pid=321, workspace=str(self.root), project_root=str(self.root))
        dsh.save(self.job / 'job.json', self.state)

    def test_interruption_compare_and_commit_preserves_newer_state(self):
        for changed in ({'state':'succeeded','result_tag':'completed concurrently'},
                        {'worker_pid':654,'worker_identity':{'pid':654,'created_at':'new'}},
                        {'superseded_by':'successor'}):
            with self.subTest(changed=changed):
                dsh.save(self.job/'job.json',self.state)
                newer = dict(self.state, **changed)
                def probe(*args):
                    dsh.save(self.job/'job.json',newer)
                    return {'state':'dead','reason':'old snapshot'}
                with patch('dsh.job_liveness',side_effect=probe):
                    result=dsh.effective_state(self.job,self.state,{'available':True,'processes':{}},persist=True)
                self.assertEqual(result,newer)
                self.assertEqual(dsh.load(self.job),newer)

    def test_interruption_commits_under_job_guard_and_keeps_unrelated_metadata(self):
        newer=dict(self.state,review_note='concurrent metadata')
        def probe(*args):
            dsh.save(self.job/'job.json',newer)
            return {'state':'dead','reason':'confirmed absent'}
        original=dsh.store_locked
        def store(path,value):
            with self.assertRaises(TimeoutError):
                with FileLock(path.with_suffix('.guard'),timeout=0): pass
            return original(path,value)
        with patch('dsh.job_liveness',side_effect=probe), patch('dsh.store_locked',side_effect=store):
            result=dsh.effective_state(self.job,self.state,{'available':True,'processes':{}},persist=True)
        self.assertEqual(result['state'],'interrupted')
        self.assertEqual(result['review_note'],'concurrent metadata')

    def test_close_and_finish_hold_dispatch_then_project_locks(self):
        expected=[self.root/'.agent-runs'/'.dsh-dispatch.guard', self.root/'.agent-runs'/'.dsh-layout.guard']
        acquired=[]
        real=dsh.FileLock
        @contextmanager
        def observed(path,timeout=5):
            with real(path,timeout):
                acquired.append(Path(path))
                try: yield
                finally: acquired.pop()
        def critical(*args):
            self.assertEqual(acquired,expected)
            self.assertTrue((self.root/'.agent-runs'/'.layout.lock').exists())
            for path in expected:
                with self.assertRaises(TimeoutError):
                    with real(path,timeout=0): pass
            return {'state':'closed'}
        with patch('dsh.FileLock',side_effect=observed), patch('dsh.close_locked',side_effect=critical), patch('dsh.finish_locked',side_effect=critical):
            self.assertEqual(dsh.close(self.job)['state'],'closed')
            self.assertEqual(dsh.finish(self.job,'unused','completed')['state'],'closed')

    def test_project_lock_recovers_only_confirmed_dead_legacy_pid(self):
        root=self.root/'.agent-runs'; legacy=root/'.layout.lock'
        legacy.write_text('7654321',encoding='ascii')
        with patch('dsh.process_identity',return_value=None) as identity:
            with dsh.project_lock(root):
                self.assertEqual(legacy.read_text(),str(os.getpid()))
            identity.assert_called_once_with(7654321)
        self.assertFalse(legacy.exists())
        self.assertTrue((root/'.dsh-layout.guard').exists())

    def test_project_lock_does_not_remove_live_or_unknown_legacy_pid(self):
        root=self.root/'.agent-runs'; legacy=root/'.layout.lock'
        for owner in ({'pid':7654321,'unknown':True}, {'pid':7654321,'created_at':'2000-01-01T00:00:00+00:00'}):
            with self.subTest(owner=owner):
                legacy.write_text('7654321',encoding='ascii')
                @contextmanager
                def reject_existing(path):
                    self.assertEqual(legacy.read_text(),'7654321')
                    raise ValueError('existing owner retained')
                    yield
                with patch('dsh.process_identity',return_value=owner), patch('dsh.layout.layout_lock',side_effect=reject_existing):
                    with self.assertRaisesRegex(ValueError,'retained'):
                        with dsh.project_lock(root): pass
                self.assertEqual(legacy.read_text(),'7654321')


if __name__ == '__main__': unittest.main()
