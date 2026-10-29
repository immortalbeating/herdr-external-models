"""Agy collection must not retain a stale successful answer after a failed new attempt."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import run

SID = '7f7ea492-d52e-453f-990e-02b81558350a'


class AgyCollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.job = self.root / 'job-1'
        self.job.mkdir()
        self.transcript = self.root / SID / '.system_generated/logs/transcript_full.jsonl'
        self.transcript.parent.mkdir(parents=True)
        self.state = {'id': 'job-1', 'spec': {'mode': 'herdr', 'kind': 'agy'},
                      'native_session': {'kind': 'id', 'value': SID, 'agent': 'agy'},
                      'pane_closed': True, 'last_request': str(self.job / 'request.md'),
                      'status': 'returned', 'result_current': True}
        run.save(self.job / 'job.json', self.state)
        (self.job / 'result.md').write_text('Old answer retained for audit', encoding='utf-8')
        self.rows = [{'type': 'USER_INPUT', 'content': 'Task job-1. Read request.md', 'step_index': 0},
                     {'type': 'PLANNER_RESPONSE', 'status': 'DONE', 'content': 'new answer', 'step_index': 1}]
        self.write()
        patcher = patch.object(run, 'AGY_BRAIN', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self):
        self.transcript.write_text(''.join(json.dumps(r) + '\n' for r in self.rows), encoding='utf-8')

    def current(self):
        return run.load(self.job / 'job.json')['result_current']

    def test_success_has_a_native_receipt_and_failed_recollect_revokes_it(self):
        self.assertEqual(run.collect(self.job, 1)['collect'], 'collected')
        self.assertTrue(self.current())
        receipt = run.load(self.job / 'result-receipt.json')
        self.assertEqual((receipt['native_session_id'], receipt['finality']), (SID, 'planner_done'))
        self.rows[-1]['content'] = ''
        self.write()
        self.assertEqual(run.collect(self.job, 1)['collect'], 'none')
        self.assertFalse(self.current())
        self.assertEqual((self.job / 'result.md').read_text(encoding='utf-8').strip(), 'new answer')

    def test_malformed_or_incomplete_stream_cannot_keep_old_result_current(self):
        for suffix in ('{', '{}\nnot-json\n'):
            with self.subTest(suffix=suffix):
                self.write()
                with self.transcript.open('a', encoding='utf-8') as out:
                    out.write(suffix)
                with self.assertRaises(ValueError):
                    run.collect(self.job, 1)
                self.assertFalse(self.current())

    def test_tool_phase_and_error_or_running_tail_are_not_final_answers(self):
        for modification in ('tool', 'running', 'error', 'explicit-error', 'missing-status'):
            with self.subTest(modification=modification):
                self.rows = self.rows[:2]
                self.rows[1] = {'type': 'PLANNER_RESPONSE', 'status': 'DONE', 'content': 'text', 'step_index': 1}
                if modification == 'tool':
                    self.rows[-1]['tool_calls'] = [{'name': 'read_file', 'args': {}}]
                elif modification == 'missing-status':
                    del self.rows[-1]['status']
                elif modification == 'explicit-error':
                    self.rows.append({'type': 'GENERIC', 'status': 'DONE', 'error': 'failure', 'step_index': 2})
                else:
                    self.rows.append({'type': 'GENERIC', 'status': modification.upper(), 'step_index': 2})
                self.write()
                self.assertEqual(run.collect(self.job, 1)['collect'], 'none')
                self.assertFalse(self.current())

    def test_blocked_agent_and_explicit_older_request_are_refused(self):
        with self.assertRaisesRegex(ValueError, 'current round'):
            run.collect(self.job, 1, request='older.md')
        self.assertFalse(self.current())
        self.state.update(pane_closed=False, agent_name='agy-job-1', result_current=True)
        run.save(self.job / 'job.json', self.state)
        with patch.object(run, 'observe', return_value={'agent_status': 'blocked'}):
            with self.assertRaisesRegex(ValueError, 'blocked'):
                run.collect(self.job, 1)
        self.assertFalse(self.current())

    def test_invalid_session_reference_never_reads_an_arbitrary_path(self):
        self.state['native_session']['value'] = '../elsewhere'
        run.save(self.job / 'job.json', self.state)
        with self.assertRaisesRegex(ValueError, 'Invalid native'):
            run.collect(self.job, 1)
        self.assertFalse(self.current())

    def test_collect_observes_identity_before_requiring_a_lazy_session_id(self):
        self.state.update(pane_closed=False, agent_name='agy-job-1', native_session=None)
        run.save(self.job / 'job.json', self.state)
        def observed(job, state, rpc_timeout=45):
            state['native_session'] = {'kind': 'id', 'agent': 'agy', 'value': SID}
            return {'agent_status': 'idle'}
        with patch.object(run, 'observe', side_effect=observed) as query:
            self.assertEqual(run.collect(self.job, 1)['collect'], 'collected')
        self.assertEqual(query.call_count, 1)


if __name__ == '__main__':
    unittest.main()
