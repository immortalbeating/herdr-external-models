"""Offline lifecycle invariants for native OMP/Claude Herdr jobs."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import layout
import run
from test_layout import FakeHerdr


class NativeJobs(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.topology = FakeHerdr()
        self.calls = []
        self.spec = {'mode': 'herdr', 'workspace': str(self.root), 'project_root': str(self.root),
                     'task_id': 'test-task', 'kind': 'omp', 'model': 'exact-model',
                     'cli_args': ['--model', 'exact-model', '--thinking', 'high'],
                     'permission_note': 'isolated fixture only'}
        self.location = layout.allocate(self.spec, 'job-1', self.topology)
        self.job = self.root / '.agent-runs/job-1'
        self.job.mkdir()
        (self.job / 'request.md').write_text('task', encoding='utf-8')
        self.source = self.root / 'native.jsonl'
        self.native_ref = {'kind': 'path', 'value': str(self.source), 'agent': 'omp', 'source': 'herdr:omp'}
        self.agent = {**self.location, 'agent': 'omp', 'name': 'omp-job-1',
                      'cwd': str(self.root), 'agent_status': 'idle', 'interactive_ready': True,
                      'agent_session': self.native_ref}
        self.state = {'id': 'job-1', 'spec': self.spec, **self.location,
                      'agent_name': self.agent['name'], 'native_session': self.native_ref,
                      'status': 'submitted', 'agent_status': 'idle', 'result_current': False,
                      'last_request': str(self.job / 'request.md')}
        run.save(self.job / 'job.json', self.state)
        (self.job / 'events.jsonl').write_text('', encoding='utf-8')
        self.rows = [
            {'type': 'session', 'id': 'native-id', 'cwd': str(self.root)},
            {'type': 'message', 'id': 'u', 'parentId': None,
             'message': {'role': 'user', 'content': 'Task job-1. Read request.md'}},
            {'type': 'message', 'id': 'a', 'parentId': 'u',
             'message': {'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': 'private'},
                                                          {'type': 'text', 'text': 'OK'}],
                         'model': 'exact-model', 'stopReason': 'stop', 'duration': 1000,
                         'usage': {'output': 2}}},
        ]
        self.write_rows()
        self.actual_args = ['omp', *self.spec['cli_args'], '--resume=' + str(self.source)]
        self.gone = False
        self.remove_workspace_on_close = False
        self.list_error = None
        self.extra_agents = []
        self.patcher = patch.object(run, 'herdr', side_effect=self.fake)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def write_rows(self):
        self.source.write_text(''.join(json.dumps(row) + '\n' for row in self.rows), encoding='utf-8')

    def saved(self):
        return run.load(self.job / 'job.json')

    def fake(self, spec, *args, raw=False, rpc_timeout=45):
        self.calls.append(args)
        if args[:2] == ('agent', 'get'):
            if self.gone:
                raise run.HerdrError('agent gone', 'agent_not_found')
            return {'agent': copy.deepcopy(self.agent)}
        if args == ('api', 'snapshot'):
            return {'snapshot': {'agents': [copy.deepcopy(self.agent), *copy.deepcopy(self.extra_agents)],
                                 'panes': copy.deepcopy(self.topology.panes)}}
        if args[:2] == ('pane', 'process-info'):
            return {'process_info': {'foreground_processes': [{'argv': self.actual_args}]}}
        if args[:2] in [('pane', 'list'), ('tab', 'list')] and self.list_error:
            raise run.HerdrError('workspace_not_found in message', self.list_error)
        result = self.topology(spec, *args)
        if args[:2] == ('pane', 'close') and self.remove_workspace_on_close:
            self.topology.tabs.clear()
            self.topology.workspaces.clear()
            self.list_error = 'workspace_not_found'
        return result

    def restart(self):
        self.agent['terminal_id'] = 'restored-terminal'
        self.topology.panes[0]['terminal_id'] = 'restored-terminal'

    def test_short_native_answer_is_collected_without_thinking_or_api_model_claim(self):
        result = run.collect(self.job)
        self.assertEqual(result['collect'], 'collected')
        self.assertEqual((self.job / 'result.md').read_text(), 'OK\n')
        receipt = run.load(self.job / 'result-receipt.json')
        self.assertEqual(receipt['model_observed'], 'exact-model')
        self.assertNotIn('private', (self.job / 'result-receipt.json').read_text())
        self.assertIsNone(self.saved().get('model_reported'))
        self.assertTrue(self.saved()['result_current'])

    def test_collect_busy_or_replaced_agent_is_refused(self):
        self.agent['agent_status'] = 'working'
        with self.assertRaisesRegex(ValueError, 'working'):
            run.collect(self.job)
        self.agent['agent_status'] = 'idle'
        self.restart()
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            run.collect(self.job)
        self.assertFalse((self.job / 'result.md').exists())

    def test_collect_failed_tail_does_not_reuse_old_answer(self):
        (self.job / 'result.md').write_text('previous answer', encoding='utf-8')
        self.rows[-1]['message']['stopReason'] = 'aborted'
        self.write_rows()
        self.assertEqual(run.collect(self.job)['collect'], 'none')
        self.assertEqual((self.job / 'result.md').read_text(), 'previous answer')
        self.assertFalse(self.saved()['result_current'])

    def test_collect_cannot_select_an_older_request_as_current(self):
        with self.assertRaisesRegex(ValueError, 'current round'):
            run.collect(self.job, request='request-old.md')

    def test_failed_recollection_revokes_previously_current_result(self):
        run.collect(self.job)
        self.assertTrue(self.saved()['result_current'])
        self.rows[-1]['message']['stopReason'] = 'aborted'
        self.write_rows()
        self.assertEqual(run.collect(self.job)['collect'], 'none')
        self.assertFalse(self.saved()['result_current'])
        self.assertIsNone(run.summary(self.job, self.saved())['result'])
        self.assertEqual((self.job / 'result.md').read_text(), 'OK\n')

    def test_collection_exception_also_revokes_previously_current_result(self):
        run.collect(self.job)
        self.source.unlink()
        with self.assertRaises(FileNotFoundError):
            run.collect(self.job)
        self.assertFalse(self.saved()['result_current'])
        self.assertEqual((self.job / 'result.md').read_text(), 'OK\n')

    def test_recovery_preview_does_not_write_or_submit_and_apply_rebinds(self):
        self.restart()
        before = (self.job / 'job.json').read_bytes()
        preview = run.recover(self.job)
        self.assertEqual(preview['recovery'], 'preview')
        self.assertTrue(preview['launch_arguments_preserved'])
        self.assertEqual((self.job / 'job.json').read_bytes(), before)
        result = run.recover(self.job, apply=True)
        self.assertEqual(result['recovery'], 'associated')
        self.assertEqual(self.saved()['terminal_id'], 'restored-terminal')
        self.assertTrue(self.saved()['recovery_launch_args_verified'])
        self.assertTrue(self.saved()['last_request'].endswith('request.md'))
        registry = run.load(self.root / '.agent-runs/herdr-layout.json')
        self.assertIn('restored-terminal', str(registry))
        self.assertNotIn(self.location['terminal_id'], str(registry))
        self.assertFalse(any(call[:2] in [('agent', 'prompt'), ('agent', 'start'), ('pane', 'close')] for call in self.calls))
        self.assertEqual(run.collect(self.job)['collect'], 'collected')

    def test_recovery_policy_drift_requires_note_and_disables_implicit_reuse(self):
        self.restart()
        self.actual_args = ['omp', '--resume=' + str(self.source)]
        self.assertTrue(run.recover(self.job)['runtime_note_required'])
        with self.assertRaisesRegex(ValueError, 'runtime-note'):
            run.recover(self.job, apply=True)
        run.recover(self.job, apply=True, runtime_note='Inspected default policy; fixture contract remains applicable')
        self.assertFalse(self.saved()['recovery_launch_args_verified'])
        self.assertIsNone(run.reusable_job(self.spec, self.state['task_key']))

    def test_recovery_unknown_environment_also_requires_review(self):
        self.restart()
        state = self.saved()
        state['spec']['env'] = {'APPDATA': str(self.root / 'isolated-data')}
        run.save(self.job / 'job.json', state)
        self.assertTrue(run.recover(self.job)['environment_needs_review'])
        with self.assertRaisesRegex(ValueError, 'runtime-note'):
            run.recover(self.job, apply=True)

    def test_repeated_recovery_cannot_certify_an_unverified_environment(self):
        self.restart()
        state = self.saved()
        state['spec']['env'] = {'APPDATA': str(self.root / 'isolated-data')}
        run.save(self.job / 'job.json', state)
        run.recover(self.job, apply=True, runtime_note='Environment inspected for this bounded continuation')
        self.assertFalse(self.saved()['recovery_launch_args_verified'])
        self.assertTrue(run.recover(self.job)['environment_needs_review'])
        with self.assertRaisesRegex(ValueError, 'runtime-note'):
            run.recover(self.job, apply=True)
        self.assertFalse(self.saved()['recovery_launch_args_verified'])
        self.assertIsNone(run.reusable_job(self.saved()['spec'], self.state['task_key']))

    def test_recovery_refuses_ambiguous_busy_and_other_owned_terminal(self):
        self.restart()
        self.extra_agents = [{**self.agent, 'pane_id': 'other-pane', 'terminal_id': 'other-terminal'}]
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            run.recover(self.job, apply=True, pane=self.agent['pane_id'])
        self.extra_agents = []
        self.agent['agent_status'] = 'working'
        with self.assertRaisesRegex(ValueError, 'idle/done'):
            run.recover(self.job, apply=True)
        self.agent['agent_status'] = 'idle'
        other = self.root / '.agent-runs/other-job'
        other.mkdir()
        run.save(other / 'job.json', {**self.saved(), 'id': 'other-job', 'terminal_id': 'restored-terminal'})
        with self.assertRaisesRegex(ValueError, 'owned by another job'):
            run.recover(self.job, apply=True)

    def test_recovery_rechecks_identity_after_preview(self):
        self.restart()
        original = self.fake

        def switch(spec, *args, raw=False):
            if args[:2] == ('agent', 'get'):
                self.agent['terminal_id'] = 'replacement-terminal'
            return original(spec, *args, raw=raw)
        with patch.object(run, 'herdr', side_effect=switch):
            with self.assertRaisesRegex(ValueError, 'changed during inspection'):
                run.recover(self.job, apply=True)
        self.assertEqual(self.saved()['terminal_id'], self.location['terminal_id'])

    def test_recovery_refuses_wrong_pane_and_wrong_native_workspace(self):
        self.restart()
        with self.assertRaisesRegex(ValueError, 'Selected pane'):
            run.recover(self.job, apply=True, pane='another-pane')
        self.agent['cwd'] = str(self.root / 'other')
        with self.assertRaisesRegex(ValueError, 'found 0'):
            run.recover(self.job, apply=True)

    def test_last_pane_removing_workspace_is_a_successful_close(self):
        self.remove_workspace_on_close = True
        result = run.close(self.job)
        self.assertEqual(result['close'], 'closed')
        self.assertTrue(self.saved()['pane_closed'])
        self.assertEqual(self.topology.panes, [])
        self.assertEqual(run.close(self.job)['close'], 'closed')

    def test_missing_workspace_after_manual_close_is_reconciled_without_mutation(self):
        self.gone = True
        self.list_error = 'workspace_not_found'
        self.topology.panes.clear()
        self.assertEqual(run.close(self.job)['close'], 'closed')
        self.assertFalse(any(call[:2] == ('pane', 'close') for call in self.calls))

    def test_workspace_absence_text_cannot_hide_a_permission_or_server_error(self):
        self.gone = True
        self.list_error = 'permission_denied'
        with self.assertRaises(run.HerdrError):
            run.close(self.job)
        self.assertFalse(self.saved().get('pane_closed'))


if __name__ == '__main__':
    unittest.main()
