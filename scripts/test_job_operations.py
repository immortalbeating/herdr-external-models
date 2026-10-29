import contextlib
import io
import json
from pathlib import Path
import sys
import subprocess
import unittest
from unittest.mock import patch

import run
import test_native_jobs


class Operations(unittest.TestCase):
    def setUp(self):
        self.fixture = test_native_jobs.NativeJobs('test_short_native_answer_is_collected_without_thinking_or_api_model_claim')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture
        self.job = self.f.job
        self.clock = 0

    def advance(self, seconds):
        self.assertFalse((self.job / '.lock').exists(), 'lock must be released while sleeping')
        self.clock += seconds

    def wait(self, **kwargs):
        with patch.object(run.time, 'monotonic', side_effect=lambda: self.clock), patch.object(run.time, 'sleep', side_effect=self.advance):
            return run.wait_jobs([str(self.job)], timeout=3, interval=1, **kwargs)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['run.py', *args]), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = run.main()
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_fourth_round_stale_review_preserves_canonical_pair(self):
        run.collect(self.job)
        canonical = self.job / 'result.md'; receipt = self.job / 'result-receipt.json'
        old = canonical.read_bytes(), receipt.read_bytes()
        request = self.job / 'request-4.md'; request.write_text('fourth', encoding='utf-8')
        state = self.f.saved(); state['last_request'] = str(request); run.save(self.job / 'job.json', state)
        self.f.rows[1]['message']['content'] = 'Task job-1. Read request-4.md'
        self.f.rows[-1]['message']['content'] = [{'type': 'text', 'text': 'fourth stale answer'}]
        self.f.write_rows()
        review = self.f.root / 'review.md'; review.write_text('Reviewed changed input', encoding='utf-8')
        with patch.object(run, 'check_review_inputs', return_value={'status': 'changed'}):
            result = run.collect(self.job, accept_changed=True)
            code, output, err = self.cli('finish', '--job', str(self.job), '--review-file', str(review), '--outcome', 'needs_work')
        self.assertEqual(code, 0, (output, err))
        self.assertEqual((canonical.read_bytes(), receipt.read_bytes()), old)
        saved = self.f.saved()
        self.assertEqual(saved['review_result'], result['saved_result'])
        self.assertEqual(Path(saved['review_result']).read_text(), 'fourth stale answer\n')
        self.assertEqual(saved['review_result_receipt'], result['receipt'])

    def test_wait_identity_change_between_outer_read_and_locked_status(self):
        for change in ({'id': 'new-id'}, {'native_session': {'kind': 'id', 'agent': 'omp', 'value': 'new-native'}}):
            with self.subTest(change=change):
                run.save(self.job / 'job.json', self.f.state)
                original_lock = run.locked
                @contextlib.contextmanager
                def injecting_lock(job):
                    state = self.f.saved(); state.update(change); run.save(self.job / 'job.json', state)
                    with original_lock(job): yield
                with patch.object(run, 'locked', side_effect=injecting_lock):
                    result = self.wait(collect_results=True)
                self.assertEqual(result['wait'], 'attention')
                self.assertFalse((self.job / 'result.md').exists())

    def test_wait_request_change_after_status_before_collect(self):
        original_status = run.job_status
        def changing_status(job, **kwargs):
            result = original_status(job, **kwargs)
            state = self.f.saved(); state['last_request'] = str(self.job / 'request-new.md'); run.save(self.job / 'job.json', state)
            return result
        with patch.object(run, 'job_status', side_effect=changing_status):
            result = self.wait(collect_results=True)
        self.assertEqual(result['wait'], 'attention')
        self.assertFalse((self.job / 'result.md').exists())

    def test_reuse_rejects_changed_artifact_output_scope(self):
        narrow = str(self.f.root / 'narrow'); wide = str(self.f.root)
        state = self.f.saved(); state['task_key'] = 'same'; state['spec']['artifact_output_dirs'] = [narrow]
        run.save(self.job / 'job.json', state)
        self.assertEqual(run.reusable_job(state['spec'], 'same'), self.job)
        requested = {**state['spec'], 'artifact_output_dirs': [wide]}
        self.assertIsNone(run.reusable_job(requested, 'same'))

    def test_cli_partial_artifact_failure_persists_receipt(self):
        sid = '12345678-1234-1234-1234-123456789abc'
        home = self.f.root / 'codex-home'; source = home / 'generated_images' / sid
        source.mkdir(parents=True); a = source / 'a.png'; b = source / 'b.png'
        a.write_bytes(b'first'); b.write_bytes(b'second')
        state = self.f.saved(); state['spec'] = {'mode': 'exec', 'env': {'CODEX_HOME': str(home)}}
        state['native_session'] = {'kind': 'id', 'agent': 'codex', 'value': sid}; run.save(self.job / 'job.json', state)
        original_copy = run.codex_artifacts.shutil.copyfileobj; count = 0
        def fail_second(src, dst):
            nonlocal count
            count += 1
            if count == 2: raise OSError('simulated second failure')
            return original_copy(src, dst)
        with patch.object(run.codex_artifacts.shutil, 'copyfileobj', side_effect=fail_second):
            code, output, error = self.cli('collect-artifacts', '--job', str(self.job), '--source', str(a), '--source', str(b))
        self.assertEqual(code, 2, (output, error))
        saved = self.f.saved(); self.assertEqual(len(saved['artifacts']), 1)
        self.assertEqual(saved['artifacts'][0]['source'], str(a))
        self.assertEqual(Path(saved['artifacts'][0]['path']).read_bytes(), b'first')
        self.assertFalse((Path(saved['artifacts'][0]['path']).parent / 'b.png').exists())

    def test_cli_status_id_workspace(self):
        code, output, _ = self.cli('status', '--job', 'job-1', '--workspace', str(self.f.root), '--cached')
        self.assertEqual(code, 0, output)
        self.assertEqual(Path(json.loads(output)['job']).resolve(), self.job.resolve())

    def test_wait_rejects_empty_and_nonfinite(self):
        for values, timeout, interval in [([], 1, 1), ([str(self.job)], float('nan'), 1),
                                           ([str(self.job)], float('inf'), 1), ([str(self.job)], 1, float('inf'))]:
            with self.subTest(values=values, timeout=timeout, interval=interval), self.assertRaises(ValueError):
                run.wait_jobs(values, timeout, interval)

    def test_wait_detects_job_and_native_identity_changes(self):
        for change in ({'id': 'replacement'}, {'native_session': {'kind': 'id', 'agent': 'omp', 'value': 'different'}}):
            with self.subTest(change=change):
                state = dict(self.f.state); run.save(self.job / 'job.json', state)
                self.f.agent['agent_status'] = 'working'; self.clock = 0
                def advance(seconds):
                    self.advance(seconds)
                    state = self.f.saved(); state.update(change); run.save(self.job / 'job.json', state)
                with patch.object(run.time, 'monotonic', side_effect=lambda: self.clock), patch.object(run.time, 'sleep', side_effect=advance):
                    result = run.wait_jobs([str(self.job)], 2, 1)
                self.assertEqual(result['wait'], 'attention')
                self.assertEqual(result['jobs'][0]['wait_state'], 'error')

    def test_prune_does_not_reconcile_other_named_session(self):
        other = self.f.root / '.agent-runs' / 'other'; other.mkdir()
        state = self.f.saved(); state['id'] = 'other'; state['spec']['session'] = 'other-session'
        state['terminal_id'] = 'absent-other'; run.save(other / 'job.json', state)
        with patch.object(run.layout, 'prune', return_value={}):
            result = run.prune_jobs(str(self.f.root), self.f.spec)
        self.assertFalse(run.load(other / 'job.json').get('pane_closed', False))
        self.assertFalse(any(row.get('job') == str(other) for row in result['jobs_reconciled']))

    def test_prompt_stall_never_resends_and_unconfirmed_not_submitted(self):
        for outcome in ('working', 'idle', 'done', 'identity_error', 'query_error'):
            with self.subTest(outcome=outcome):
                state = self.f.saved(); calls = []
                def fake(spec, *args, **kwargs):
                    calls.append(args)
                    raise run.HerdrError('stalled', 'agent_prompt_stalled')
                if outcome.endswith('error'):
                    observation = ValueError('identity changed') if outcome == 'identity_error' else run.HerdrError('query failed', 'other')
                    observer = patch.object(run, 'observe', side_effect=observation)
                else:
                    observer = patch.object(run, 'observe', return_value={'agent_status': outcome})
                with patch.object(run, 'herdr', side_effect=fake), observer:
                    run.submit(self.job, state, self.job / 'request.md')
                self.assertEqual(len(calls), 1)
                saved = self.f.saved()
                self.assertEqual(saved['status'], 'submitted' if outcome == 'working' else 'attention')
                self.assertTrue(saved['submission_uncertain'])

    def test_omp_find_warnings_and_regular_tools(self):
        for args in (['--tools', 'read,find'], ['--tools=find,read'], ['--tools', 'read', 'find']):
            with self.subTest(args=args):
                spec = {**self.f.spec, 'cli_args': args}
                self.assertTrue(any('find' in w for w in run.configuration_warnings(spec)))
                with patch.object(run, 'executable', return_value='herdr'):
                    validated = run.validate(spec)
                self.assertTrue(any('find' in w for w in validated['configuration_warnings']))
        self.assertFalse(run.configuration_warnings({**self.f.spec, 'cli_args': ['--tools', 'glob,grep']}))

    def test_pwsh_store_warning_conditional_on_resolved_first_shell(self):
        spec = {'mode': 'exec', 'kind': 'codex', 'access': 'read-only'}
        with patch.object(run.shutil, 'which', return_value=str(self.f.root / 'WindowsApps' / 'pwsh.exe')):
            self.assertTrue(any('PowerShell' in w for w in run.configuration_warnings(spec)))
        with patch.object(run.shutil, 'which', return_value=str(self.f.root / 'PowerShell' / 'pwsh.exe')):
            self.assertFalse(run.configuration_warnings(spec))
        spec['access'] = 'danger-full-access'
        with patch.object(run.shutil, 'which', return_value=str(self.f.root / 'WindowsApps' / 'pwsh.exe')):
            self.assertFalse(run.configuration_warnings(spec))

    def test_artifact_cli_deduplicates_and_keeps_status(self):
        sid = '12345678-1234-1234-1234-123456789abc'
        home = self.f.root / 'codex-home'; source = home / 'generated_images' / sid / 'a.png'
        source.parent.mkdir(parents=True); source.write_bytes(b'png bytes')
        state = self.f.saved(); state['spec'] = {'mode': 'exec', 'env': {'CODEX_HOME': str(home)}}
        state['native_session'] = {'kind': 'id', 'agent': 'codex', 'value': sid}; run.save(self.job / 'job.json', state)
        for i in range(2):
            code, output, error = self.cli('collect-artifacts', '--job', str(self.job), '--source', str(source))
            self.assertEqual(code, 0, (output, error))
            self.assertEqual(json.loads(output)['artifacts'][0]['already_present'], bool(i))
        saved = self.f.saved(); self.assertEqual(len(saved['artifacts']), 1)
        self.assertNotEqual(saved['status'], 'completed')

    def test_wait_rpc_timeout_at_deadline_reports_timeout(self):
        def timeout(job, refresh=False, **kwargs):
            self.clock = 2
            raise subprocess.TimeoutExpired('herdr', kwargs.get('rpc_timeout', 2))
        with patch.object(run, 'job_status', side_effect=timeout), patch.object(run.time, 'monotonic', side_effect=lambda: self.clock), patch.object(run.time, 'sleep', side_effect=self.advance):
            result = run.wait_jobs([str(self.job)], 2, 1)
        self.assertEqual(result['wait'], 'timeout')
        self.assertNotEqual(run.command_exit_code('wait', result), 0)

    def test_wait_rpc_timeout_respects_remaining_budget(self):
        budgets = []
        def status(job, refresh=False, **kwargs):
            budgets.append(kwargs['rpc_timeout'])
            return {'job': str(job), 'agent_status': 'working'}
        with patch.object(run, 'job_status', side_effect=status), patch.object(run.time, 'monotonic', side_effect=lambda: self.clock), patch.object(run.time, 'sleep', side_effect=self.advance):
            result = run.wait_jobs([str(self.job)], 2, 1)
        self.assertEqual(result['wait'], 'timeout')
        self.assertTrue(budgets)
        self.assertTrue(all(0 < value <= 2 for value in budgets))

    def test_status_live_cached_closed_and_idle_is_not_completed(self):
        live = run.job_status(self.job)
        self.assertEqual(live['status_source'], 'live')
        self.assertNotEqual(live['status'], 'completed')
        self.f.calls.clear()
        self.assertEqual(run.job_status(self.job, cached=True)['status_source'], 'cached')
        self.assertFalse(self.f.calls)
        state = self.f.saved(); state['pane_closed'] = True; run.save(self.job / 'job.json', state)
        self.assertEqual(run.job_status(self.job)['status_source'], 'local')
        self.assertFalse(self.f.calls)
        state['spec']['mode'] = 'direct'; run.save(self.job / 'job.json', state)
        self.assertEqual(run.job_status(self.job)['status_source'], 'local')

    def test_resolve_full_id_missing_and_ambiguous(self):
        self.assertEqual(run.resolve_job(str(self.job)), self.job.resolve())
        self.assertEqual(run.resolve_job('job-1', self.f.root), self.job.resolve())
        with self.assertRaises(ValueError): run.resolve_job('missing', self.f.root)
        child = self.f.root / 'child'; child.mkdir()
        duplicate = child / '.agent-runs' / 'job-1'; duplicate.mkdir(parents=True)
        (duplicate / 'job.json').write_text('{}')
        with patch.object(run.layout, 'project_root', return_value=str(self.f.root)):
            with self.assertRaises(ValueError): run.resolve_job('job-1', child)
        with self.assertRaises(ValueError): run.resolve_job('job-1', child / '.agent-runs')

    def test_wait_working_then_real_omp_collect_and_releases_lock(self):
        self.f.agent['agent_status'] = 'working'
        def advance(seconds):
            self.advance(seconds)
            self.f.agent['agent_status'] = 'idle'
        with patch.object(run.time, 'monotonic', side_effect=lambda: self.clock), patch.object(run.time, 'sleep', side_effect=advance):
            result = run.wait_jobs([str(self.job)], 3, 1, collect_results=True)
        self.assertEqual(result['wait'], 'collected')
        self.assertEqual((self.job / 'result.md').read_text(), 'OK\n')
        self.assertFalse(any(c[:2] == ('agent', 'cancel') for c in self.f.calls))

    def test_wait_timeout_ignores_old_result(self):
        (self.job / 'result.md').write_text('old')
        self.f.rows[-1]['message']['stopReason'] = 'aborted'; self.f.write_rows()
        result = self.wait(collect_results=True)
        self.assertEqual(result['wait'], 'timeout')
        self.assertNotEqual(run.command_exit_code('wait', result), 0)
        self.assertEqual((self.job / 'result.md').read_text(), 'old')
        self.assertFalse(any(c[:2] == ('agent', 'cancel') for c in self.f.calls))

    def test_wait_blocked_and_identity_replacement(self):
        self.f.agent['agent_status'] = 'blocked'
        result = self.wait()
        self.assertEqual(result['wait'], 'attention')
        self.assertNotEqual(run.command_exit_code('wait', result), 0)
        self.f.agent['agent_status'] = 'idle'; self.f.restart()
        self.assertEqual(self.wait()['wait'], 'attention')

    def test_wait_request_change_is_rejected(self):
        self.f.agent['agent_status'] = 'working'
        def advance(seconds):
            self.advance(seconds)
            state = self.f.saved(); state['last_request'] = str(self.job / 'request-new.md'); run.save(self.job / 'job.json', state)
        with patch.object(run.time, 'monotonic', side_effect=lambda: self.clock), patch.object(run.time, 'sleep', side_effect=advance):
            result = run.wait_jobs([str(self.job)], 3, 1, collect_results=True)
        self.assertEqual(result['wait'], 'attention')
        self.assertIn('request changed', result['jobs'][0]['error'])

    def test_wait_all_vs_any(self):
        second = self.f.root / '.agent-runs' / 'job-2'; second.mkdir()
        state = self.f.saved(); state['id'] = 'job-2'; run.save(second / 'job.json', state)
        def status(job, refresh=False, **kwargs):
            return {'job': str(job), 'agent_status': 'idle' if job == self.job.resolve() else 'working'}
        with patch.object(run, 'job_status', side_effect=status), patch.object(run.time, 'monotonic', side_effect=lambda: self.clock), patch.object(run.time, 'sleep', side_effect=self.advance):
            any_result = run.wait_jobs([str(self.job), str(second)], 2, 1)
            all_result = run.wait_jobs([str(self.job), str(second)], 2, 1, all_jobs=True)
        self.assertEqual(any_result['wait'], 'settled')
        self.assertEqual(all_result['wait'], 'timeout')

    def test_cli_json_stderr_and_argparse(self):
        self.f.agent['agent_status'] = 'blocked'
        code, output, error = self.cli('wait', '--job', str(self.job), '--timeout', '1')
        self.assertNotEqual(code, 0); self.assertEqual(json.loads(output)['wait'], 'attention'); self.assertTrue(error.strip())
        code, output, error = self.cli('status', '--job', str(self.job), '--cached')
        self.assertEqual(code, 0); self.assertEqual(json.loads(output)['status_source'], 'cached')
        code, output, error = self.cli('wait', '--unknown')
        self.assertEqual(code, 2); self.assertTrue(error.strip())

    def test_peer_hints_are_read_only(self):
        state = self.f.saved(); state['task_key'] = 'same'; run.save(self.job / 'job.json', state)
        peer = self.f.root / '.agent-runs' / 'peer'; peer.mkdir()
        other = {**state, 'id': 'peer', 'terminal_id': 'peer-terminal'}; run.save(peer / 'job.json', other)
        before = (peer / 'job.json').read_bytes()
        hints = run.peer_open_jobs(self.job, state)
        self.assertEqual(len(hints), 1); self.assertEqual(hints[0]['status_source'], 'local_record')
        self.assertEqual((peer / 'job.json').read_bytes(), before)
        self.assertFalse(any(c[:2] == ('pane', 'close') for c in self.f.calls))

    def test_prune_absent_and_live_jobs(self):
        with patch.object(run.layout, 'prune', return_value={}):
            result = run.prune_jobs(str(self.f.root), self.f.spec)
            self.assertEqual(result['jobs_reconciled'], [])
            self.f.gone = True
            self.f.topology.panes.clear()
            result = run.prune_jobs(str(self.f.root), self.f.spec)
        self.assertEqual(len(result['jobs_reconciled']), 1)
        self.assertTrue(self.f.saved()['pane_closed'])
        self.assertFalse(any(c[:2] == ('pane', 'close') for c in self.f.calls))

    def test_finish_retained_stale_needs_work_but_not_completed(self):
        review = self.f.root / 'review.md'; review.write_text('Checked the findings', encoding='utf-8')
        with patch.object(run, 'check_review_inputs', return_value={'status': 'changed'}):
            run.collect(self.job, accept_changed=True)
            code, output, _ = self.cli('finish', '--job', str(self.job), '--review-file', str(review), '--outcome', 'completed')
            self.assertNotEqual(code, 0)
            code, output, _ = self.cli('finish', '--job', str(self.job), '--review-file', str(review), '--outcome', 'needs_work')
        self.assertEqual(code, 0, output)
        self.assertEqual(self.f.saved()['status'], 'needs_work')
        self.assertFalse(self.f.saved()['result_current'])

    def test_prune_failed_query_never_marks_closed(self):
        with patch.object(run.layout, 'prune', return_value={}), patch.object(run, 'herdr', side_effect=run.HerdrError('network error', 'other')):
            with self.assertRaises(run.HerdrError): run.prune_jobs(str(self.f.root), self.f.spec)
        self.assertFalse(self.f.saved().get('pane_closed', False))

    def test_absent_only_rechecks_reappeared_terminal(self):
        with self.assertRaisesRegex(ValueError, 'present again'):
            run.close(self.job, absent_only=True)
        self.assertFalse(self.f.saved().get('pane_closed', False))
        self.assertFalse(any(c[:2] == ('pane', 'close') for c in self.f.calls))

    def test_accept_changed_collects_stale_but_unavailable_is_blocked(self):
        with patch.object(run, 'check_review_inputs', return_value={'status': 'changed'}):
            self.assertEqual(run.collect(self.job)['collect'], 'stale')
            result = run.collect(self.job, accept_changed=True)
            self.assertEqual(result['collect'], 'collected_stale')
            self.assertFalse(self.f.saved()['result_current'])
        with patch.object(run, 'check_review_inputs', return_value={'status': 'unavailable'}):
            self.assertEqual(run.collect(self.job, accept_changed=True)['collect'], 'unavailable')


if __name__ == '__main__': unittest.main()
