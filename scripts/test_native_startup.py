"""First Claude submission waits for its hook identity and interactive readiness."""
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import run
from test_layout import FakeHerdr

SID = '0e035f6a-0edf-42a3-871a-5bde3d524355'
REF = {'kind': 'id', 'agent': 'claude', 'value': SID, 'source': 'herdr:claude'}


class NativeStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = 0.0

    def sleep(self, seconds):
        self.clock += seconds

    def launch(self, initial=None, changes=(), kind='claude', prompt_error=False, rpc_error=False, rpc_elapsed=0):
        calls = []
        topology = FakeHerdr()
        agent = {'agent': kind, 'name': None, 'terminal_id': None, 'pane_id': None,
                 'agent_status': 'idle', 'interactive_ready': False, 'agent_session': None,
                 'cwd': str(self.root)}
        agent.update(initial or {})
        pending = iter(changes)

        def fake(spec, *args, raw=False, rpc_timeout=45):
            calls.append((args, rpc_timeout))
            if args[0] in ('workspace', 'tab', 'pane'):
                value = topology(spec, *args)
                if 'root_pane' in value:
                    agent.update(terminal_id=value['root_pane']['terminal_id'],
                                 pane_id=value['root_pane']['pane_id'])
                return value
            if args[:2] == ('agent', 'start'):
                agent['name'] = args[2]
                return {'agent': dict(agent)}
            if args[:2] == ('agent', 'get'):
                self.clock += rpc_elapsed
                if rpc_error:
                    raise subprocess.TimeoutExpired('herdr', rpc_timeout)
                agent.update(next(pending, {}))
                return {'agent': dict(agent)}
            if args[:2] == ('agent', 'prompt'):
                if prompt_error:
                    raise run.HerdrError('agent_prompt_stalled', 'agent_prompt_stalled')
                return {'agent': dict(agent)}
            raise AssertionError(args)

        spec = {'mode': 'herdr', 'kind': kind, 'workspace': str(self.root), 'model': 'cli-default',
                'cli_args': ['--tools', 'Read'], 'permission_note': 'Offline test fixture only'}
        with patch.object(run, 'herdr', side_effect=fake), patch.object(run, 'executable', return_value='unused'), \
                patch.object(run.time, 'monotonic', side_effect=lambda: self.clock), \
                patch.object(run.time, 'sleep', side_effect=self.sleep):
            output = run.run(spec, 'Read only this fixture')
        folder = Path(output['job'])
        state = run.load(folder / 'job.json')
        events = [json.loads(line) for line in (folder / 'events.jsonl').read_text(encoding='utf-8').splitlines()]
        return output, state, events, calls

    def assert_unsent(self, state, events, calls):
        self.assertNotIn('last_request', state)
        self.assertNotIn('last_submitted_at', state)
        self.assertNotIn('submission_started', [e['event'] for e in events])
        self.assertFalse(any(args[:2] == ('agent', 'prompt') for args, _ in calls))
        self.assertEqual(sum(args[:2] == ('agent', 'start') for args, _ in calls), 1)
        self.assertIn('agent_readiness_failed', [e['event'] for e in events])

    def test_delayed_identity_precedes_exactly_one_submission(self):
        output, state, events, calls = self.launch(changes=[{'agent_session': REF}, {'interactive_ready': True}])
        self.assertEqual(output['status'], 'submitted')
        sequence = [e['event'] for e in events]
        self.assertLess(sequence.index('agent_ready'), sequence.index('submission_started'))
        self.assertEqual(state['native_session'], REF)
        self.assertEqual(sum(args[:2] == ('agent', 'get') for args, _ in calls), 2)
        self.assertEqual(sum(args[:2] == ('agent', 'prompt') for args, _ in calls), 1)

    def test_already_ready_needs_no_poll(self):
        output, _, _, calls = self.launch(initial={'agent_session': REF, 'interactive_ready': True})
        self.assertEqual(output['status'], 'submitted')
        self.assertFalse(any(args[:2] == ('agent', 'get') for args, _ in calls))

    def test_ready_without_native_identity_times_out_unsent(self):
        output, state, events, calls = self.launch(initial={'interactive_ready': True})
        self.assertIn('initialization timed out', output['error'])
        self.assert_unsent(state, events, calls)
        self.assertLessEqual(self.clock, 30)
        self.assertTrue(all(0 < timeout <= 30 for args, timeout in calls if args[:2] == ('agent', 'get')))

    def test_native_identity_without_explicit_ready_stays_unsent(self):
        for value in (False, None):
            with self.subTest(ready=value):
                output, state, events, calls = self.launch(initial={'agent_session': REF, 'interactive_ready': value})
                self.assertIn('initialization timed out', output['error'])
                self.assert_unsent(state, events, calls)

    def test_invalid_or_wrong_cli_native_identity_is_refused(self):
        for ref in ({**REF, 'value': 'not-a-uuid'}, {**REF, 'agent': 'omp'},
                    {**REF, 'value': SID.replace('-', '')}, {**REF, 'value': '{' + SID + '}'}):
            with self.subTest(ref=ref):
                output, state, events, calls = self.launch(initial={'agent_session': ref, 'interactive_ready': True})
                self.assertIn('Invalid Claude', output['error'])
                self.assert_unsent(state, events, calls)

    def test_identity_replacement_is_never_rebound(self):
        for mutation in ({'terminal_id': 'other'}, {'pane_id': 'w9:p9'}, {'name': 'other'},
                         {'agent_session': {**REF, 'value': 'ca83d8ac-4fae-4f06-b176-8d81a1e582ec'}}):
            with self.subTest(mutation=mutation):
                output, state, events, calls = self.launch(initial={'agent_session': REF}, changes=[mutation])
                self.assertIn('changed during initialization', output['error'])
                self.assert_unsent(state, events, calls)
                self.assertEqual(state['native_session'], REF)

    def test_blocked_or_working_stops_without_automatic_input(self):
        for status in ('blocked', 'working'):
            with self.subTest(status=status):
                _, state, events, calls = self.launch(initial={'agent_status': status,
                    'agent_session': REF, 'interactive_ready': True})
                self.assert_unsent(state, events, calls)

    def test_rpc_failure_is_not_retried_or_submitted(self):
        _, state, events, calls = self.launch(rpc_error=True)
        self.assert_unsent(state, events, calls)
        self.assertEqual(sum(args[:2] == ('agent', 'get') for args, _ in calls), 1)

    def test_ready_snapshot_returned_after_deadline_is_not_submitted(self):
        output, state, events, calls = self.launch(initial={'agent_session': REF},
            changes=[{'interactive_ready': True}], rpc_elapsed=30)
        self.assertIn('initialization timed out', output['error'])
        self.assert_unsent(state, events, calls)

    def test_prompt_failure_after_ready_is_not_replayed(self):
        output, _, events, calls = self.launch(initial={'agent_session': REF, 'interactive_ready': True}, prompt_error=True)
        self.assertEqual(output['status'], 'attention')
        self.assertEqual(sum(args[:2] == ('agent', 'prompt') for args, _ in calls), 1)
        self.assertEqual(sum(e['event'] == 'submission_started' for e in events), 1)

    def test_codex_and_agy_also_wait_for_their_own_identity(self):
        for kind in ('codex', 'agy'):
            with self.subTest(kind=kind):
                output, _, events, calls = self.launch(kind=kind, changes=[
                    {'agent_session': {**REF, 'agent': kind}, 'interactive_ready': True}])
                self.assertEqual(output['status'], 'submitted')
                self.assertIn('agent_ready', [e['event'] for e in events])
                self.assertEqual(sum(args[:2] == ('agent', 'prompt') for args, _ in calls), 1)

    def test_lazy_identity_clis_can_submit_once_after_explicit_ready(self):
        for kind in ('codex', 'agy'):
            with self.subTest(kind=kind):
                output, state, events, calls = self.launch(kind=kind, initial={'interactive_ready': True})
                self.assertEqual(output['status'], 'submitted')
                self.assertTrue(state['native_identity_pending_at_start'])
                self.assertEqual(sum(args[:2] == ('agent', 'prompt') for args, _ in calls), 1)

    def test_initial_working_detection_waits_for_readiness_without_input(self):
        output, _, events, calls = self.launch(kind='codex', initial={'agent_status': 'working'},
            changes=[{'agent_status': 'idle', 'interactive_ready': True}])
        self.assertEqual(output['status'], 'submitted')
        sequence = [e['event'] for e in events]
        self.assertLess(sequence.index('agent_ready'), sequence.index('submission_started'))
        self.assertEqual(sum(args[:2] == ('agent', 'prompt') for args, _ in calls), 1)

    def test_lazy_identity_exception_cannot_clear_a_previously_bound_session(self):
        for kind in ('codex', 'agy'):
            with self.subTest(kind=kind):
                native = {**REF, 'agent': kind}
                output, state, events, calls = self.launch(kind=kind, initial={'agent_session': native},
                    changes=[{'agent_session': None, 'interactive_ready': True}])
                self.assertEqual(output['status'], 'attention')
                self.assert_unsent(state, events, calls)
                self.assertEqual(state['native_session'], native)

    def test_unadapted_kinds_keep_the_existing_start_path(self):
        for kind in ('qwen', 'omp'):
            with self.subTest(kind=kind):
                output, _, events, calls = self.launch(kind=kind)
                self.assertEqual(output['status'], 'submitted')
                self.assertNotIn('agent_readiness_wait', [e['event'] for e in events])
                self.assertFalse(any(args[:2] == ('agent', 'get') for args, _ in calls))

    def test_rpc_timeout_is_passed_to_the_process(self):
        response = SimpleNamespace(returncode=0, stdout='{"result":{"agent":{}}}', stderr='')
        with patch.object(run, 'executable', return_value='unused'), patch.object(run.subprocess, 'run', return_value=response) as process:
            run.herdr({}, 'agent', 'get', 'mine', rpc_timeout=0.75)
        self.assertEqual(process.call_args.kwargs['timeout'], 0.75)


if __name__ == '__main__':
    unittest.main()
