import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import completion_checks as cc
import review_inputs as ri


class Checks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_snapshot_scope_and_drift(self):
        a, b, outside = (self.root / n for n in ('a', 'b', 'outside'))
        a.write_text('\u4e2d', encoding='utf-8')
        snap = ri.snapshot([str(a), str(a), str(b)])
        self.assertEqual(len(snap['files']), 2)
        outside.write_text('unrelated')
        self.assertEqual(ri.check(snap)['status'], 'match')
        b.write_bytes(a.read_bytes())
        self.assertEqual(ri.check(snap)['changed'], [str(b)])
        b.unlink()
        a.rename(b)
        self.assertEqual(ri.check(snap)['changed'], [str(a), str(b)])
        b.rename(a)
        a.write_text('changed')
        self.assertEqual(ri.check(snap)['changed'], [str(a)])
        a.unlink()
        self.assertEqual(ri.check(snap)['changed'], [str(a)])

    def test_invalid_snapshot_and_unreadable(self):
        a = self.root / 'a'
        a.write_text('secret')
        snap = ri.snapshot([str(a)])
        snap['files'][0]['size'] = 3
        self.assertEqual(ri.check(snap)['status'], 'unavailable')
        snap = ri.snapshot([str(a)])
        with patch('builtins.open', side_effect=PermissionError('private content')):
            result = ri.check(snap)
        self.assertEqual(result['status'], 'unavailable')
        self.assertNotIn('private content', str(result))
        for paths in ([], [''], ['relative'], [str(self.root)]):
            with self.assertRaises(ValueError):
                ri.validate_paths(paths)

    def test_wildcard_rejected_and_explicit_missing_accepted(self):
        import os
        missing = self.root / 'later.txt'
        snapshot = ri.snapshot([str(missing)])
        missing.write_text('new')
        self.assertEqual(ri.check(snapshot)['changed'], [str(missing)])
        for wildcard in ('*.gd', '**/*.gd', 'file?.txt'):
            with self.assertRaises(ValueError):
                ri.validate_paths([str(self.root / wildcard)])
        if os.name == 'nt':
            prefix = chr(92) * 2 + '?' + chr(92)
            self.assertEqual(len(ri.validate_paths([prefix + str(missing)])), 1)

    def test_completed_current_state(self):
        a = self._job('stale', {'status': 'completed', 'spec': {'mode': 'exec'},
                                'result_current': False,
                                'input_check': {'status': 'changed'}})
        b = self._job('unavailable', {'status': 'completed', 'spec': {'mode': 'direct'},
                                      'input_check': {'status': 'unavailable'}})
        c = self._job('needs', {'status': 'needs_work', 'spec': {'mode': 'exec'},
                                'result_current': False,
                                'input_check': {'status': 'changed'}}, result=False)
        d = self._job('legacy', {'status': 'completed', 'spec': {'mode': 'exec'}})
        rows = cc.finish_report(a, [b, c, d])['jobs']
        self.assertEqual(rows[0]['issues'], ['result_not_current', 'input_not_current'])
        self.assertEqual(rows[1]['issues'], ['input_not_current'])
        self.assertEqual(rows[2]['issues'], [])
        self.assertEqual(rows[3]['issues'], [])

    def test_reparse_component_rejected(self):
        a = self.root / 'junction' / 'file'
        with patch.object(ri.os.path, 'isjunction', create=True,
                          side_effect=lambda path: path == str(a.parent)):
            with self.assertRaises(ValueError):
                ri.validate_paths([str(a)])

    def test_nonempty_is_streamed(self):
        class Stream:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, size):
                self_size = size
                if self_size != 4096:
                    raise AssertionError('unexpected unbounded read')
                return ' evidence '
        with patch('builtins.open', return_value=Stream()):
            self.assertTrue(cc._nonempty('unused'))

    def test_superseded_and_invalid_mode(self):
        a = self._job('old', {'state': 'succeeded', 'terminal_id': 't',
                              'superseded_by': 'new', 'review_outcome': 'needs_work'})
        b = self._job('bad', {'status': 'completed', 'spec': {'mode': 'unknown'}})
        report = cc.finish_report(a, [b], expect_closed=True)
        self.assertEqual(report['jobs'][0]['pane'], 'superseded')
        self.assertEqual(report['jobs'][0]['issues'], [])
        self.assertIn('record_unreadable', report['jobs'][1]['issues'])

    def _job(self, name, record, review=True, result=True):
        job = self.root / name
        job.mkdir()
        review_path = job / 'review.md'
        if review:
            review_path.write_text('evidence')
        if result:
            (job / 'result.md').write_text('result')
        if 'state' in record:
            record['review_file'] = str(review_path)
        else:
            record['review'] = str(review_path)
        (job / 'job.json').write_text(json.dumps(record), encoding='utf-8-sig')
        return job

    def test_finish_normal_and_related(self):
        a = self._job('a', {'status': 'completed', 'spec': {'mode': 'herdr'}, 'terminal_id': 't'})
        b = self._job('b', {'status': 'needs_work', 'spec': {'mode': 'exec'}}, result=False)
        with patch('pathlib.Path.glob', side_effect=AssertionError), patch('pathlib.Path.rglob', side_effect=AssertionError), patch('subprocess.run', side_effect=AssertionError):
            report = cc.finish_report(a, [b, b], expect_closed=True)
        self.assertEqual(report['checked_jobs'], 2)
        self.assertEqual(report['jobs'][0]['issues'], ['close_pending'])
        self.assertEqual(report['jobs'][1]['issues'], [])
        self.assertEqual(report['status'], 'attention')

    def test_dsh_and_pending(self):
        a = self._job('a', {'state': 'closed', 'review_outcome': 'completed'})
        b = self._job('b', {'state': 'succeeded'}, review=False)
        c = self._job('c', {'status': 'completed', 'spec': {'mode': 'direct'}}, result=False)
        report = cc.finish_report(a, [b, c], expect_closed=True)
        self.assertEqual(report['jobs'][0]['issues'], [])
        self.assertEqual(report['jobs'][0]['pane'], 'closed')
        self.assertEqual(report['jobs'][1]['issues'], ['review_pending'])
        self.assertEqual(report['jobs'][1]['pane'], 'none')
        self.assertEqual(report['jobs'][2]['issues'], ['result_unreadable'])
        (a / 'review.md').unlink()
        self.assertIn('review_unreadable', cc.finish_report(a)['jobs'][0]['issues'])
        self.assertIn('record_unreadable', cc.finish_report(self.root / 'missing')['jobs'][0]['issues'])


if __name__ == '__main__':
    unittest.main()
