"""Offline public-command checks; fake models/Herdr, real local records."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import run
import test_run


class ReviewWorkflow(unittest.TestCase):
    @staticmethod
    def no_rpc(*args, **kwargs):
        raise AssertionError('unexpected RPC')

    def setUp(self):
        self.fixture = test_run.Jobs()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.work
        self.source = self.root / 'source.txt'
        self.source.write_text('version one', encoding='utf-8')
        self.review = self.root / 'review.md'
        self.review.write_text('Checked the supplied version.', encoding='utf-8')

    def bound_direct(self, changed=False):
        spec = {**self.fixture.spec, 'review_inputs': [str(self.source)], 'checkpoint_id': 'T-1/cp1'}
        def answer(*args):
            if changed:
                self.source.write_text('version two', encoding='utf-8')
            return 'Visible final result', {'model_reported': 'test-model'}
        with patch.object(run.api, 'call', side_effect=answer) as call:
            result = run.run(spec, 'Review only the supplied file.')
        self.assertEqual(call.call_count, 1)
        return Path(result['job']), result

    def test_ordinary_calls_do_not_snapshot_or_add_model_calls(self):
        with patch.object(run.review_inputs, 'snapshot', side_effect=AssertionError('unexpected hash')):
            output = self.fixture.direct()
        job = Path(output['job'])
        self.assertEqual(output['status'], 'returned')
        self.assertFalse((job / 'input-check.json').exists())
        self.assertEqual(len(self.fixture.server.received), 1)

    def test_matching_input_is_bound_to_request_and_finish_has_one_local_report(self):
        job, output = self.bound_direct()
        receipt = run.load(output['review_binding']['receipt'])
        self.assertEqual(receipt['request'], 'request.md')
        self.assertEqual({Path(x['path']).name for x in receipt['files']}, {'source.txt', 'request.md'})
        with patch.object(Path, 'glob', side_effect=AssertionError('unexpected scan')), \
             patch.object(run, 'herdr', side_effect=AssertionError('unexpected RPC')):
            code, result = self.fixture.cli(self.no_rpc, 'finish', '--job', str(job), '--review-file', str(self.review),
                                            '--outcome', 'completed', '--close')
        self.assertEqual((code, result['finish_check'], result['checked_jobs']), (0, 'ok', 1))
        self.assertTrue((job / 'finish-check.json').is_file())

    def test_changed_during_execution_keeps_result_and_blocks_completed_even_with_manual_file(self):
        job, output = self.bound_direct(changed=True)
        self.assertEqual(output['status'], 'attention')
        self.assertIsNone(output['result'])
        self.assertEqual((job / 'result.md').read_text(encoding='utf-8').strip(), 'Visible final result')
        code, result = self.fixture.cli(self.no_rpc, 'finish', '--job', str(job), '--review-file', str(self.review),
                                       '--outcome', 'completed', '--result-file', str(job / 'result.md'))
        self.assertEqual(code, 2)
        self.assertIn('inputs changed', result['error'])
        self.assertNotEqual(run.load(job / 'job.json')['status'], 'completed')
        code, result = self.fixture.cli(self.no_rpc, 'finish', '--job', str(job), '--review-file', str(self.review),
                                       '--outcome', 'needs_work', '--result-file', str(job / 'result.md'))
        self.assertEqual((code, result['status']), (0, 'needs_work'))
        self.assertFalse(run.load(job / 'job.json')['result_current'])

    def test_native_collect_refuses_drift_before_reading_a_transcript_or_pane(self):
        output, agent, calls, fake = self.fixture.herdr_job()
        job = Path(output['job'])
        state = run.load(job / 'job.json')
        state['spec']['review_inputs'] = [str(self.source)]
        run.bind_review_inputs(job, state, job / 'request.md')
        (job / 'result.md').write_text('old result', encoding='utf-8')
        self.source.write_text('changed', encoding='utf-8')
        with patch.object(run, 'herdr', side_effect=AssertionError('unexpected RPC')):
            result = run.collect(job)
        self.assertEqual(result['collect'], 'stale')
        self.assertIsNone(result['result'])
        self.assertEqual((job / 'result.md').read_text(encoding='utf-8'), 'old result')

    def test_followup_updates_only_this_round_binding_and_preserves_previous_receipt(self):
        output, agent, calls, fake = self.fixture.herdr_job()
        job = Path(output['job'])
        state = run.load(job / 'job.json')
        state['spec']['review_inputs'] = [str(self.source)]
        run.bind_review_inputs(job, state, job / 'request.md')
        previous = Path(state['review_binding']['receipt'])
        original_bytes = previous.read_bytes()
        next_file = self.root / 'next.txt'
        next_file.write_text('next input', encoding='utf-8')
        prompt = self.root / 'followup.md'
        prompt.write_text('Review the new selected input.', encoding='utf-8')
        code, result = self.fixture.cli(fake, 'followup', '--job', str(job), '--prompt-file', str(prompt),
                                       '--review-input', str(next_file), '--checkpoint-id', 'T-1/cp2')
        self.assertEqual(code, 0)
        current = run.load(job / 'job.json')
        receipt = run.load(current['review_binding']['receipt'])
        self.assertNotEqual(Path(current['review_binding']['receipt']), previous)
        self.assertEqual(previous.read_bytes(), original_bytes)
        self.assertEqual(receipt['request'], Path(current['last_request']).name)
        self.assertEqual(receipt['checkpoint_id'], 'T-1/cp2')
        self.assertEqual({Path(x['path']).name for x in receipt['files']}, {next_file.name, receipt['request']})

    def test_related_unreviewed_job_is_reported_without_mutating_it_or_scanning(self):
        job, output = self.bound_direct()
        related = self.root / '.agent-runs' / 'known-related'
        related.mkdir()
        run.save(related / 'job.json', {'status': 'submitted', 'spec': {'mode': 'herdr'}, 'terminal_id': 'term-other'})
        before = (related / 'job.json').read_bytes()
        with patch.object(Path, 'glob', side_effect=AssertionError('unexpected scan')):
            code, result = self.fixture.cli(self.no_rpc, 'finish', '--job', str(job), '--review-file', str(self.review),
                                            '--outcome', 'completed', '--close', '--related-job', str(related))
        self.assertEqual((code, result['status'], result['finish_check']), (2, 'completed', 'attention'))
        report = run.load(job / 'finish-check.json')
        self.assertEqual(report['checked_jobs'], 2)
        self.assertEqual(set(report['jobs'][1]['issues']), {'review_pending', 'close_pending'})
        self.assertEqual((related / 'job.json').read_bytes(), before)

    def test_dry_run_validates_scope_without_hashing_or_creating_a_job(self):
        spec = {**self.fixture.spec, 'review_inputs': [str(self.source)]}
        with patch.object(run.review_inputs, 'snapshot', side_effect=AssertionError('unexpected hash')):
            self.assertEqual(run.run(spec, 'review', dry=True)['status'], 'dry_run')
        with self.assertRaises(ValueError):
            run.run({**spec, 'access': 'workspace-write', 'write_scope': ['allowed']}, 'implementation')
        self.assertFalse((self.root / '.agent-runs').exists())

    def test_reuse_with_new_review_scope_keeps_one_pane_and_binds_new_round(self):
        output, agent, calls, fake = self.fixture.herdr_job(task_id='T-1')
        job = Path(output['job'])
        state = run.load(job / 'job.json')
        state['spec']['review_inputs'] = [str(self.source)]
        run.bind_review_inputs(job, state, job / 'request.md')
        next_file = self.root / 'new-scope.txt'
        next_file.write_text('new scope', encoding='utf-8')
        spec = {**state['spec'], 'reuse': True, 'review_inputs': [str(next_file)], 'checkpoint_id': 'cp2'}
        with patch.object(run, 'executable', return_value='unused'), patch.object(run, 'herdr', side_effect=fake):
            result = run.run(spec, 'Review this next candidate.')
        self.assertTrue(result['reused'])
        self.assertEqual(result['job'], output['job'])
        self.assertEqual(len(fake.topology.panes), 1)
        current = run.load(job / 'job.json')
        receipt = run.load(current['review_binding']['receipt'])
        self.assertEqual(receipt['checkpoint_id'], 'cp2')
        self.assertIn(str(next_file), [x['path'] for x in receipt['files']])

    def test_failed_followup_snapshot_cannot_complete_with_the_previous_round(self):
        output, agent, calls, fake = self.fixture.herdr_job()
        job = Path(output['job'])
        state = run.load(job / 'job.json')
        state['spec']['review_inputs'] = [str(self.source)]
        run.bind_review_inputs(job, state, job / 'request.md')
        state.update(status='completed', result_current=True)
        run.save(job / 'job.json', state)
        (job / 'result.md').write_text('previous answer', encoding='utf-8')
        prompt = self.root / 'followup.md'
        prompt.write_text('Review the next version.', encoding='utf-8')
        previous_prompts = len([c for c in calls if c[:2] == ('agent', 'prompt')])
        with patch.object(run.review_inputs, 'snapshot', side_effect=PermissionError('synthetic denied')):
            code, result = self.fixture.cli(fake, 'followup', '--job', str(job), '--prompt-file', str(prompt),
                                           '--review-input', str(self.source))
        self.assertEqual(code, 2)
        current = run.load(job / 'job.json')
        self.assertFalse(current['result_current'])
        self.assertIsNone(current['review_binding'])
        self.assertNotEqual(Path(current['last_request']).name, 'request.md')
        self.assertEqual(len([c for c in calls if c[:2] == ('agent', 'prompt')]), previous_prompts)
        code, result = self.fixture.cli(self.no_rpc, 'finish', '--job', str(job), '--review-file', str(self.review),
                                       '--outcome', 'completed', '--result-file', str(job / 'result.md'))
        self.assertEqual(code, 2)
        self.assertNotEqual(run.load(job / 'job.json')['status'], 'completed')

    def test_reuse_ordinary_spec_clears_previous_review_scope_without_new_pane(self):
        output, agent, calls, fake = self.fixture.herdr_job(task_id='T-1')
        job = Path(output['job'])
        state = run.load(job / 'job.json')
        ordinary = {**state['spec'], 'reuse': True}
        state['spec']['review_inputs'] = [str(self.source)]
        run.bind_review_inputs(job, state, job / 'request.md')
        with patch.object(run, 'executable', return_value='unused'), patch.object(run, 'herdr', side_effect=fake), \
             patch.object(run.review_inputs, 'snapshot', side_effect=AssertionError('unexpected hash')):
            result = run.run(ordinary, 'Ordinary consultation on supplied text.')
        self.assertTrue(result['reused'])
        self.assertEqual(len(fake.topology.panes), 1)
        current = run.load(job / 'job.json')
        self.assertNotIn('review_inputs', current['spec'])
        self.assertIsNone(current['review_binding'])
        self.assertEqual(run.check_review_inputs(job, current)['status'], 'not_recorded')

    def test_receipt_must_cover_current_spec_scope(self):
        job, output = self.bound_direct()
        state = run.load(job / 'job.json')
        other = self.root / 'other.txt'
        other.write_text('other version', encoding='utf-8')
        state['spec']['review_inputs'] = [str(other)]
        self.assertEqual(run.check_review_inputs(job, state)['status'], 'unavailable')
        state['spec']['review_inputs'] = [str(self.source)]
        run.save(Path(state['review_binding']['receipt']), [])
        self.assertEqual(run.check_review_inputs(job, state)['status'], 'unavailable')
        self.assertFalse(state['result_current'])


if __name__ == '__main__':
    unittest.main()
