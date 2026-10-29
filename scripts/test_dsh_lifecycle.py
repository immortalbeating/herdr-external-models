"""Offline lifecycle integration against a stateful Herdr protocol fake."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import dsh
from dsh_processes import FileLock


class Herdr:
    def __init__(self):
        self.workspaces = []
        self.tabs = []
        self.panes = []
        self.agents = []
        self.calls = []
    def __call__(self, state, *args):
        self.calls.append(args)
        kind, action = args[:2]
        if action == 'list':
            return {kind + 's': getattr(self, kind + 's')}
        if kind == 'workspace' and action == 'create':
            ws = {'workspace_id':'w1','label':args[args.index('--label')+1]}
            tab = {'tab_id':'t1','label':'initial'}
            pane = {'pane_id':'p1','terminal_id':'term1','tab_id':'t1','cwd':state['workspace']}
            self.workspaces.append(ws); self.tabs.append(tab); self.panes.append(pane)
            return dict(workspace=ws, tab=tab, root_pane=pane)
        if action == 'rename':
            if kind == 'tab': self.tabs[0]['label'] = args[3]
            return {}
        if kind == 'pane' and action == 'process-info':
            return {'process_info': {'shell_pid': 100, 'foreground_processes':[100]}}
        if action == 'close':
            if kind == 'workspace': self.workspaces.clear(); self.panes.clear(); self.tabs.clear()
            elif kind == 'tab':
                self.tabs[:] = [t for t in self.tabs if t['tab_id'] != args[2]]
                self.panes[:] = [p for p in self.panes if p['tab_id'] != args[2]]
            elif kind == 'pane': self.panes.clear()
            return {}
        if kind == 'pane' and action == 'run': return {}
        raise AssertionError(args)


SHELL = dict(pid=100,parent_pid=0,name='pwsh.exe',image='C:/pwsh.exe',created_at='shell',command_line='pwsh')
DEAD = {'available':True,'processes':{100:SHELL}}
UNKNOWN = {'available':False,'processes':{}}


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.prompt = self.root / 'input.md'; self.prompt.write_text('offline task')
        self.fake = Herdr()
        self.probe = patch('dsh.process_snapshot', return_value=DEAD).start()
        self.addCleanup(patch.stopall)
        patch('dsh.herdr', side_effect=self.fake).start()
        patch('dsh.native', side_effect=lambda value:value).start()
        patch('dsh.dsh_bin', return_value='C:/dsh/bin.js').start()
        patch('dsh.identity', return_value=None).start()
    def args(self):
        return SimpleNamespace(workspace=str(self.root),prompt_file=str(self.prompt),session='named',
            profile='herdr',session_id='native-session',herdr='h.exe',node='node.exe',dsh_bin='bin.js',timeout=0,
            project_root=str(self.root),project_name='Project',task_id='task',task_name='Task')
    def run_job(self):
        result = dsh.run(self.args())
        self.assertEqual(result['state'],'starting', dsh.load(Path(result['job'])))
        return Path(result['job'])
    def settle(self, job, state='succeeded'):
        record=dsh.load(job); record.update(state=state, worker_pid=999, created_at='2020-01-01T00:00:00+00:00')
        dsh.save(job/'job.json', record)
        if state == 'succeeded': (job/'result.md').write_text('answer')
        return record
    def mutations(self):
        return [args for args in self.fake.calls if args[1] in ('create','split','run','close','rename')]
    def test_finished_rounds_reuse_pane(self):
        first=self.run_job(); self.settle(first)
        second=self.run_job()
        self.assertEqual(dsh.load(first)['superseded_by'],str(second))
        self.assertEqual(dsh.load(second)['pane_id'],'p1')
        self.assertEqual(sum(c[:2]==('workspace','create') for c in self.fake.calls),1)
        self.assertEqual(sum(c[:2]==('pane','run') for c in self.fake.calls),2)
        self.assertFalse(any(c[:2]==('pane','split') for c in self.fake.calls))
        before=len(self.mutations())
        with self.assertRaisesRegex(ValueError,'later job'): dsh.close(first)
        self.assertEqual(len(self.mutations()),before)
        review=self.root/'review-old.md'; review.write_text('Reviewed first round')
        result=dsh.finish(first,str(review),'completed')
        self.assertEqual(result['state'],'succeeded')
        self.assertIn('later round',result['close_reason'])
        self.assertEqual(len(self.mutations()),before)
        self.assertEqual(self.fake.panes[0]['pane_id'],dsh.load(second)['pane_id'])
    def test_same_session_live_or_unknown_refuses_dispatch(self):
        job=self.run_job(); record=self.settle(job,'working')
        for probe in (UNKNOWN, {'available':True,'processes':{999:dict(pid=999,parent_pid=0,name='python.exe',image='python.exe',created_at='a',command_line=f'python dsh.py --job "{job}"')}}):
            self.probe.return_value=probe
            before=len(self.mutations())
            with self.assertRaisesRegex(ValueError,'live or unconfirmed'): dsh.run(self.args())
            self.assertEqual(len(self.mutations()),before)
    def test_status_dead_interrupts_unknown_preserves(self):
        job=self.run_job(); self.settle(job,'working')
        self.probe.return_value=UNKNOWN
        self.assertEqual(dsh.status(job)['state'],'working')
        self.assertEqual(dsh.load(job)['state'],'working')
        self.probe.return_value=DEAD
        self.assertEqual(dsh.status(job)['state'],'interrupted')
        self.assertEqual(dsh.load(job)['state'],'interrupted')
    def test_restored_marked_and_legacy_adoption_require_idle_dead_owned(self):
        job=self.run_job(); state=self.settle(job)
        self.fake.panes[0]['terminal_id']='restored'
        self.assertTrue(dsh.owned_pane(job,state,DEAD)['restored'])
        self.fake.workspaces[0]['label']='foreign'
        with self.assertRaisesRegex(ValueError,'ownership changed'): dsh.owned_pane(job,state,DEAD)
        self.fake.workspaces[0]['label']=job.name
        self.assertTrue(dsh.owned_pane(job,state,DEAD)['restored'])
        self.fake.panes[0]['cwd']=str(self.root/'other')
        with self.assertRaisesRegex(ValueError,'ownership changed'): dsh.owned_pane(job,state,DEAD)
        self.fake.panes[0]['cwd']=str(self.root)
        self.fake.agents=[{'terminal_id':'restored','agent_status':'idle'}]
        with self.assertRaisesRegex(ValueError,'still owns'): dsh.owned_pane(job,state,DEAD)
        self.fake.agents=[]
        with self.assertRaisesRegex(ValueError,'unconfirmed'): dsh.owned_pane(job,state,UNKNOWN)
        child=dict(SHELL,pid=101,parent_pid=100,name='node.exe')
        with self.assertRaisesRegex(ValueError,'busy'): dsh.owned_pane(job,state,{'available':True,'processes':{100:SHELL,101:child}})
    def test_finish_reviews_and_closes_only_own_tab_in_shared_workspace(self):
        job=self.run_job(); state=self.settle(job)
        state['owns_workspace']=False; dsh.save(job/'job.json',state)
        self.fake.panes.append({'pane_id':'agy','terminal_id':'agy-term','tab_id':'agy-tab'})
        self.fake.tabs.append({'tab_id':'agy-tab','label':'AGY'})
        review=self.root/'review.md'; review.write_text('Reviewed offline result')
        with self.assertRaises(ValueError): dsh.finish(job,str(self.root/'missing.md'),'completed')
        result=dsh.finish(job,str(review),'completed')
        self.assertEqual(result['state'],'closed')
        self.assertTrue(Path(dsh.load(job)['review_file']).is_file())
        self.assertIn(('tab','close','t1'),self.fake.calls)
        self.assertEqual([p['pane_id'] for p in self.fake.panes], ['agy'])
        self.assertFalse(any(c[:2]==('workspace','close') for c in self.fake.calls))
    def test_finish_completed_requires_success_and_nonempty_result(self):
        job=self.run_job(); self.settle(job,'failed')
        review=self.root/'review.md'; review.write_text('review')
        with self.assertRaises(ValueError): dsh.finish(job,str(review),'completed')
        self.settle(job); (job/'result.md').write_text(' ')
        with self.assertRaises(ValueError): dsh.finish(job,str(review),'completed')
    def test_prune_preview_read_only_apply_closes_dead_only(self):
        job=self.run_job(); self.settle(job,'working')
        before=(job/'job.json').read_bytes(); calls=len(self.mutations())
        result=dsh.prune(str(self.root),'named',str(self.root))
        self.assertEqual(result['candidates'],1)
        self.assertEqual((job/'job.json').read_bytes(),before)
        self.assertEqual(len(self.mutations()),calls)
        self.probe.return_value=UNKNOWN
        self.assertEqual(dsh.prune(str(self.root),'named',str(self.root),True)['closed'],0)
        self.probe.return_value={'available':True,'processes':{999:dict(pid=999,parent_pid=0,
            name='python.exe',image='python.exe',created_at='a',command_line=f'python dsh.py --job "{job}"')}}
        self.assertEqual(dsh.prune(str(self.root),'named',str(self.root),True)['closed'],0)
        self.assertEqual((job/'job.json').read_bytes(),before)
        self.probe.return_value=DEAD
        self.assertEqual(dsh.prune(str(self.root),'named',str(self.root),True)['closed'],1)
        self.assertEqual(dsh.load(job)['state'],'closed')

    def test_expired_unstarted_owned_idle_job_interrupts_and_delayed_worker_refuses(self):
        job=self.run_job()
        record=dsh.load(job); record['created_at']='2020-01-01T00:00:00+00:00'
        dsh.save(job/'job.json',record)
        self.assertNotIn('worker_pid',record)
        self.assertFalse((job/'.worker-started').exists())
        self.assertEqual(dsh.status(job)['state'],'interrupted')
        with patch('dsh.subprocess.Popen') as model, patch('dsh.herdr') as herdr:
            with self.assertRaisesRegex(ValueError,'no longer launchable'):
                dsh.worker(job)
            model.assert_not_called(); herdr.assert_not_called()
        self.assertFalse((job/'.worker-started').exists())

    def test_unstarted_expiry_requires_confirmed_idle_pane(self):
        job=self.run_job()
        record=dsh.load(job); record['created_at']='2020-01-01T00:00:00+00:00'
        dsh.save(job/'job.json',record)
        self.probe.return_value=UNKNOWN
        self.assertEqual(dsh.status(job)['state'],'starting')
        self.probe.return_value={'available':True,'processes':{100:SHELL,101:dict(SHELL,pid=101,parent_pid=100,name='node.exe')}}
        self.assertEqual(dsh.status(job)['state'],'starting')
        self.probe.return_value=DEAD
        self.fake.agents=[{'terminal_id':'term1','agent_status':'idle'}]
        self.assertEqual(dsh.status(job)['state'],'starting')
        self.assertFalse((job/'.worker-started').exists())

    def test_interrupted_unstarted_job_can_close_verified_idle_layout(self):
        job=self.run_job()
        record=dsh.load(job); record['created_at']='2020-01-01T00:00:00+00:00'
        dsh.save(job/'job.json',record)
        self.assertEqual(dsh.status(job)['state'],'interrupted')
        self.assertEqual(dsh.load(job)['interruption_kind'],'unstarted')
        self.assertEqual(dsh.close(job)['state'],'closed')
        self.assertFalse(self.fake.panes)

    def test_interrupted_unstarted_job_can_resume_in_same_pane(self):
        job=self.run_job()
        record=dsh.load(job); record['created_at']='2020-01-01T00:00:00+00:00'
        dsh.save(job/'job.json',record)
        self.assertEqual(dsh.status(job)['state'],'interrupted')
        resumed=self.run_job()
        self.assertEqual(dsh.load(job)['superseded_by'],str(resumed))
        self.assertEqual(dsh.load(resumed)['pane_id'],'p1')
        self.assertEqual(sum(c[:2]==('workspace','create') for c in self.fake.calls),1)
        with patch('dsh.subprocess.Popen') as model:
            with self.assertRaisesRegex(ValueError,'no longer launchable'): dsh.worker(job)
            model.assert_not_called()

    def test_unstarted_interruption_does_not_bypass_fresh_process_checks(self):
        job=self.run_job()
        record=dsh.load(job); record['created_at']='2020-01-01T00:00:00+00:00'
        dsh.save(job/'job.json',record)
        self.assertEqual(dsh.status(job)['state'],'interrupted')
        mutations=len(self.mutations())
        self.probe.return_value=UNKNOWN
        with self.assertRaises(ValueError): dsh.close(job)
        self.probe.return_value={'available':True,'processes':{100:SHELL,101:dict(SHELL,pid=101,parent_pid=100,name='node.exe')}}
        with self.assertRaises(ValueError): dsh.close(job)
        self.probe.return_value=DEAD
        (job/'.worker-started').touch()
        with self.assertRaises(ValueError): dsh.close(job)
        self.assertEqual(len(self.mutations()),mutations)
        self.assertEqual(dsh.load(job)['state'],'interrupted')

    def test_allocate_mutations_hold_same_dispatch_and_project_guards_as_close(self):
        def guarded(state,*args):
            if args[1] in ('create','split','rename'):
                for name in ('.dsh-dispatch.guard','.dsh-layout.guard'):
                    with self.assertRaises(TimeoutError):
                        with FileLock(self.root/'.agent-runs'/name,timeout=0): pass
                self.assertTrue((self.root/'.agent-runs'/'.layout.lock').exists())
            return self.fake(state,*args)
        with patch('dsh.herdr',side_effect=guarded): self.run_job()


if __name__ == '__main__': unittest.main()
