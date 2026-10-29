"""Offline tests using synthetic secrets only."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import dsh
import run

class RedactionTests(unittest.TestCase):
    def test_structured_state_events_and_text(self):
        secret = 'synthetic-"quoted"-\\path-\u5bc6\u94a5'
        with patch.dict(os.environ, {'SYNTHETIC_TEST_SECRET': secret}, clear=True):
            value = {'nested': [{'message': 'before ' + secret + ' after'}], 'tuple': ('plain', {'inner': (secret, 3)}), 'count': 7}
            with tempfile.TemporaryDirectory() as root:
                job = Path(root)
                run.update(job, value, 'updated', detail={'value': secret, 'tuple': ('plain', (secret, 3))})
                for path in (job / 'job.json', job / 'events.jsonl'):
                    text = path.read_text(encoding='utf-8')
                    self.assertNotIn(secret, text)
                    self.assertNotIn(json.dumps(secret, ensure_ascii=False)[1:-1], text)
                    parsed = json.loads(text)
                    self.assertIn('[REDACTED]', json.dumps(parsed, ensure_ascii=False))
                state = json.loads((job / 'job.json').read_text(encoding='utf-8'))
                self.assertEqual(state['count'], 7)
                self.assertEqual(state['tuple'], ['plain', {'inner': ['[REDACTED]', 3]}])
                event = json.loads((job / 'events.jsonl').read_text(encoding='utf-8'))
                self.assertEqual(event['detail']['tuple'], ['plain', ['[REDACTED]', 3]])
            for ascii_mode in (False, True):
                text = json.dumps({'value': secret}, ensure_ascii=ascii_mode)
                cleaned = run.redact(text)
                self.assertEqual(json.loads(cleaned)['value'], '[REDACTED]')
                self.assertNotIn(json.dumps(secret, ensure_ascii=ascii_mode)[1:-1], cleaned)

    def test_main_error_is_valid_redacted_json(self):
        secret = 'synthetic-"quoted"-\\path-\u5bc6\u94a5'
        output = io.StringIO()
        with patch.dict(os.environ, {'SYNTHETIC_TEST_SECRET': secret}, clear=True), \
             patch.object(sys, 'argv', ['run.py', 'show']), \
             patch.object(run, 'executable', side_effect=ValueError(secret)), \
             contextlib.redirect_stdout(output):
            self.assertEqual(run.main(), 2)
        self.assertEqual(json.loads(output.getvalue())['error'], 'ValueError: [REDACTED]')

    def test_length_thresholds_preserved(self):
        with patch.dict(os.environ, {'SYNTHETIC_TEST_TOKEN': 'tiny'}, clear=True):
            self.assertEqual(run.redact('tiny'), 'tiny')
            self.assertEqual(dsh.redact('tiny'), '[REDACTED]')

if __name__ == '__main__':
    unittest.main()
