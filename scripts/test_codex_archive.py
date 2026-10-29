"""Archive behavior: exact ownership, ordering, retry, and native record preservation."""
import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import codex_archive
import completion_checks
import native_records
import run
from test_layout import FakeHerdr

SID = '80d20633-9f7f-4e35-b2d8-3c9028106850'
OTHER = '11111111-1111-4111-8111-111111111111'


class CodexArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'codex-home'
        self.job = self.root / '.agent-runs/job-1'
        self.job.mkdir(parents=True)
        self.path = self.home / 'sessions/2026/10/29' / ('rollout-2026-10-29T00-00-00-' + SID + '.jsonl')
        self.path.parent.mkdir(parents=True)
        self.rows = [
            {'type': 'session_meta', 'payload': {'id': SID, 'cwd': str(self.root)}},
            {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'turn-1'}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [
                {'type': 'input_text', 'text': 'Task job-1. Read "job-1/request.md"'}]}},
            {'type': 'turn_context', 'payload': {'turn_id': 'turn-1', 'cwd': str(self.root), 'model': 'test'}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'phase': 'final_answer',
                'content': [{'type': 'output_text', 'text': 'answer'}]}},
            {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'turn-1', 'last_agent_message': 'answer'}},
        ]
        self.write_rows()
        self.topology = FakeHerdr()
        layout = self.topology({'workspace': str(self.root)}, 'workspace', 'create', '--cwd', str(self.root))
        pane = layout['root_pane']
        self.state = {
            'id': 'job-1', 'status': 'returned', 'result_current': True, 'last_request': 'request.md',
            'spec': {'mode': 'herdr', 'kind': 'codex', 'workspace': str(self.root), 'project_root': str(self.root),
                     'env': {'CODEX_HOME': str(self.home)}},
            'project_root': str(self.root), 'task_key': 'task-1', 'native_session': {'agent': 'codex', 'kind': 'id', 'value': SID},
            **{k: pane[k] for k in ('workspace_id', 'tab_id', 'pane_id', 'terminal_id')},
        }
        run.save(self.job / 'job.json', self.state)
        (self.job / 'request.md').write_text('prompt', encoding='utf-8')
        (self.job / 'result.md').write_text('answer', encoding='utf-8')
        self.calls = []
        self.env = patch.dict(os.environ, {run.ACTIVE: '', 'EXTERNAL_MODEL_CONSULT_ACTIVE': '', 'CODEX_THREAD_ID': ''})
        self.env.start()
        self.addCleanup(self.env.stop)

    def write_rows(self):
        self.path.write_text(''.join(json.dumps(row) + '\n' for row in self.rows), encoding='utf-8')

    def state_on_disk(self):
        return run.load(self.job / 'job.json')

    def native_cli(self, argv, **kwargs):
        self.assertTrue(self.state_on_disk()['pane_closed'])
        self.assertEqual(self.topology.panes, [])
        self.assertEqual(argv, ['native-codex', 'archive', SID])
        self.assertEqual(kwargs['env']['CODEX_HOME'], str(self.home))
        self.assertEqual(kwargs['cwd'], str(self.root))
        self.assertNotIn('shell', kwargs)
        self.calls.append(argv)
        target = self.home / 'archived_sessions' / self.path.name
        target.parent.mkdir(exist_ok=True)
        self.path.replace(target)
        return subprocess.CompletedProcess(argv, 0)

    def close(self, native=None):
        with patch.object(run, 'herdr', side_effect=self.topology), \
                patch.object(run, 'executable', return_value='native-codex'), \
                patch.object(codex_archive.subprocess, 'run', side_effect=native or self.native_cli):
            return run.close(self.job)

    def test_close_then_archive_and_repeat_without_second_cli(self):
        raw = self.path.read_bytes()
        result = self.close()
        self.assertEqual(result['close'], 'closed')
        self.assertEqual(result['codex_archive']['status'], 'archived')
        target = Path(result['codex_archive']['transcript'])
        self.assertEqual(target.read_bytes(), raw)
        self.assertEqual(self.close()['codex_archive']['already_archived'], True)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(native_records.collect_round('codex', target, self.root, 'job-1', 'request.md')['text'], 'answer')

    def test_close_failure_does_not_archive(self):
        def refuse(spec, *args):
            if args[:2] == ('pane', 'close'):
                raise run.HerdrError('permission denied', 'permission_denied')
            return self.topology(spec, *args)
        with patch.object(run, 'herdr', side_effect=refuse), patch.object(run.codex_archive, 'archive') as archive:
            with self.assertRaises(run.HerdrError):
                run.close(self.job)
        archive.assert_not_called()
        self.assertFalse(self.state_on_disk().get('pane_closed'))

    def test_busy_or_replaced_agent_never_archives(self):
        for change in ({'agent_status': 'working'}, {'agent_status': 'blocked'}, {'terminal_id': 'replacement'},
                       {'agent_session': {'kind': 'id', 'value': OTHER}}):
            state = copy.deepcopy(self.state)
            state['agent_name'] = 'owned-agent'
            run.save(self.job / 'job.json', state)
            agent = {'name': 'owned-agent', 'terminal_id': state['terminal_id'], 'pane_id': state['pane_id'],
                     'workspace_id': state['workspace_id'], 'tab_id': state['tab_id'], 'agent_status': 'done',
                     'agent_session': state['native_session'], **change}
            with patch.object(run, 'herdr', return_value={'agent': agent}), patch.object(run.codex_archive, 'archive') as archive:
                with self.assertRaises(ValueError):
                    run.close(self.job)
                archive.assert_not_called()
        run.save(self.job / 'job.json', self.state)

    def test_archive_failure_preserves_close_and_can_retry_by_job_or_task(self):
        failed = self.close(lambda *a, **k: subprocess.CompletedProcess(a[0], 1))
        self.assertEqual(failed['close'], 'closed')
        self.assertEqual(failed['codex_archive']['status'], 'failed')
        self.assertTrue(self.state_on_disk()['pane_closed'])
        self.assertEqual(run.jobs_of_task(str(self.root), 'task-1'), [self.job])
        self.assertEqual(self.close()['codex_archive']['status'], 'archived')
        self.assertEqual(run.jobs_of_task(str(self.root), 'task-1'), [])

    def test_native_timeout_is_visible_and_retry_observes_moved_file(self):
        def timeout(argv, **kwargs):
            self.native_cli(argv, **kwargs)
            raise subprocess.TimeoutExpired(argv, 45)
        failed = self.close(timeout)
        self.assertEqual(failed['codex_archive']['status'], 'failed')
        self.assertEqual(self.close()['codex_archive']['already_archived'], True)
        self.assertEqual(len(self.calls), 1)

    def test_zero_exit_without_native_archive_is_not_success(self):
        result = self.close(lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0))
        self.assertEqual(result['codex_archive']['status'], 'failed')
        self.assertTrue(self.path.exists())

    def test_close_acknowledgement_without_terminal_removal_does_not_archive(self):
        def acknowledge(spec, *args):
            if args[:2] == ('pane', 'close'):
                return {'type': 'ok'}
            return self.topology(spec, *args)
        with patch.object(run, 'herdr', side_effect=acknowledge), patch.object(run.codex_archive, 'archive') as archive:
            with self.assertRaisesRegex(ValueError, 'still reports'):
                run.close(self.job)
        archive.assert_not_called()
        self.assertFalse(self.state_on_disk().get('pane_closed'))

    def test_manual_pane_closure_is_reconciled_then_archived(self):
        self.topology.panes.clear()
        result = self.close()
        self.assertEqual(result['codex_archive']['status'], 'archived')
        self.assertFalse(self.state_on_disk()['closed_live'])
        self.assertFalse(any(call[:2] == ('pane', 'close') for call in self.topology.calls))

    def test_path_retry_after_native_move_and_timeout_or_nonzero(self):
        self.state['native_session'] = {'kind': 'path', 'value': str(self.path)}
        run.save(self.job / 'job.json', self.state)
        def move_and_fail(argv, **kwargs):
            self.native_cli(argv, **kwargs)
            raise subprocess.TimeoutExpired(argv, 45)
        self.assertEqual(self.close(move_and_fail)['codex_archive']['status'], 'failed')
        self.assertEqual(self.close()['codex_archive']['already_archived'], True)
        target = Path(self.state_on_disk()['codex_archive']['transcript'])
        target.replace(self.path)
        run.save(self.job / 'job.json', {**self.state, 'pane_closed': True})
        def move_and_nonzero(argv, **kwargs):
            self.native_cli(argv, **kwargs)
            return subprocess.CompletedProcess(argv, 1)
        self.assertEqual(self.close(move_and_nonzero)['codex_archive']['status'], 'failed')
        self.assertEqual(self.close()['codex_archive']['already_archived'], True)

    def test_explicit_path_with_duplicate_uuid_does_not_invoke_native_archive(self):
        state = {**self.state, 'pane_closed': True, 'native_session': {'kind': 'path', 'value': str(self.path)}}
        duplicate = self.home / 'archived_sessions' / self.path.name
        duplicate.parent.mkdir()
        duplicate.write_bytes(self.path.read_bytes())
        with patch.object(codex_archive.subprocess, 'run') as command:
            with self.assertRaisesRegex(ValueError, 'ambiguous'):
                codex_archive.archive(state, 'native-codex')
            command.assert_not_called()

    def test_latest_request_or_workspace_mismatch_does_not_touch_cli(self):
        for mismatch in ('user', 'cwd', 'unfinished', 'controller'):
            rows = copy.deepcopy(self.rows)
            if mismatch == 'user':
                rows[2]['payload']['content'][0]['text'] = 'Task another-job request.md'
            if mismatch == 'cwd':
                rows[0]['payload']['cwd'] = str(self.root / 'another-workspace')
            if mismatch == 'unfinished':
                rows.append({'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'other'}})
            self.path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
            state = {**self.state, 'pane_closed': True}
            if mismatch == 'controller':
                state['parent_thread_id'] = SID
            with patch.object(codex_archive.subprocess, 'run') as command:
                with self.assertRaises(ValueError):
                    codex_archive.archive(state, 'native-codex')
                command.assert_not_called()

    def test_no_native_session_does_not_require_codex_or_fabricate_archive(self):
        state = {**self.state, 'native_session': None}
        run.save(self.job / 'job.json', state)
        with patch.object(run, 'herdr', side_effect=self.topology), patch.object(run, 'executable', side_effect=AssertionError):
            result = run.close(self.job)
        self.assertEqual(result['codex_archive'], {'status': 'skipped', 'reason': 'no_native_session'})

    def test_other_agents_close_without_archive(self):
        for kind in ('agy', 'omp', 'claude'):
            state = {**self.state, 'pane_closed': True, 'spec': {**self.state['spec'], 'kind': kind}}
            run.save(self.job / 'job.json', state)
            with patch.object(run.codex_archive, 'archive', side_effect=AssertionError), patch.object(run, 'executable', side_effect=AssertionError):
                self.assertEqual(run.close(self.job)['close'], 'closed')
            self.assertNotIn('codex_archive', self.state_on_disk())

    def test_path_reference_is_updated_to_the_archived_file(self):
        self.state['native_session'] = {'kind': 'path', 'agent': 'codex', 'value': str(self.path)}
        run.save(self.job / 'job.json', self.state)
        archived = self.close()['codex_archive']
        self.assertEqual(self.state_on_disk()['native_session']['value'], archived['transcript'])
        self.assertEqual(self.close()['codex_archive']['already_archived'], True)

    def test_finish_without_close_keeps_conversation_available_for_followup(self):
        review = self.root / 'review.md'
        review.write_text('Checked answer and evidence', encoding='utf-8')
        with patch.object(run, 'git_state', return_value=None), patch.object(run.codex_archive, 'archive') as archive:
            run.manage(argparse.Namespace(command='finish', job=str(self.job), outcome='completed',
                                          review_file=str(review), result_file=None))
        archive.assert_not_called()
        self.assertTrue(self.path.exists())
        self.assertFalse(self.state_on_disk().get('pane_closed'))

    def test_close_cli_exit_reports_archive_failure_and_finish_check(self):
        failed = self.close(lambda argv, **kw: subprocess.CompletedProcess(argv, 1))
        review = self.job / 'review.md'
        review.write_text('Evidence', encoding='utf-8')
        state = self.state_on_disk()
        state.update(status='completed', review=str(review))
        run.save(self.job / 'job.json', state)
        self.assertIn('codex_archive_failed', completion_checks.finish_report(self.job, expect_closed=True)['jobs'][0]['issues'])
        with patch.object(sys, 'argv', ['run.py', 'close', '--job', str(self.job)]), patch.object(run, 'close', return_value=failed), patch('builtins.print'):
            self.assertEqual(run.main(), 2)

    def test_finish_close_propagates_archive_result_and_preserves_acceptance_on_failure(self):
        review = self.root / 'review.md'
        review.write_text('Evidence checked', encoding='utf-8')
        argv = ['run.py', 'finish', '--job', str(self.job), '--review-file', str(review),
                '--outcome', 'completed', '--close']
        failed = lambda argv, **kw: subprocess.CompletedProcess(argv, 1)
        with patch.object(sys, 'argv', argv), patch.object(run, 'herdr', side_effect=self.topology), \
                patch.object(run, 'executable', return_value='native-codex'), \
                patch.object(run, 'git_state', return_value=None), \
                patch.object(codex_archive.subprocess, 'run', side_effect=failed), patch('builtins.print') as output:
            self.assertEqual(run.main(), 2)
        result = json.loads(output.call_args.args[0])
        self.assertEqual(result['close'], 'closed')
        self.assertEqual(result['codex_archive']['status'], 'failed')
        self.assertEqual(result['finish_check'], 'attention')
        self.assertEqual(self.state_on_disk()['status'], 'completed')
        self.assertTrue(Path(self.state_on_disk()['review']).is_file())

    def test_explicit_path_outside_the_selected_home_cannot_target_another_store(self):
        self.state.update(pane_closed=True, native_session={'kind': 'path', 'value': str(self.path)})
        self.state['spec']['env'] = {'CODEX_HOME': str(self.root / 'another-home')}
        with patch.object(codex_archive.subprocess, 'run') as command:
            with self.assertRaisesRegex(ValueError, 'outside'):
                codex_archive.archive(self.state, 'native-codex')
            command.assert_not_called()


if __name__ == '__main__':
    unittest.main()
