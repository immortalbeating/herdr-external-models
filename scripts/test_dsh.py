"""Offline contracts for the DSH adapter; no models or Herdr servers are called."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import dsh


class Process:
    pid = 123

    def __init__(self, events, code):
        self.stdout = io.StringIO(''.join(json.dumps(e) + '\n' for e in events))
        self.stderr = io.StringIO('diagnostic\n')
        self.stdin = io.StringIO()
        self.code = code

    def wait(self):
        return self.code

    def poll(self):
        return self.code


class AdapterTests(unittest.TestCase):
    def setUp(self):
        for name, value in [('identity', None), ('process_snapshot', {'available': False, 'processes': {}})]:
            stub = patch('dsh.' + name, return_value=value)
            stub.start()
            self.addCleanup(stub.stop)

    def make_job(self, root, **extra):
        job = Path(root)
        state = dict(state='starting', herdr='herdr.exe', session='named', pane_id='p1',
                     terminal_id='t1', workspace_id='w1', node='node.exe', dsh_bin='bin.js',
                     workspace=root, profile='herdr', dsh_session_id='session-old', **extra)
        dsh.save(job / 'job.json', state)
        (job / 'prompt.md').write_text('task', encoding='utf-8')
        return job

    def execute(self, reason='completed', code=0, final=True):
        events = [dict(type='session', sessionId='session-new'),
                  dict(type='status', phase='turn_end', reason=dict(kind=reason))]
        if final:
            events.append(dict(type='final', text='answer'))
        with tempfile.TemporaryDirectory() as root:
            job = self.make_job(root)
            with patch('dsh.herdr', return_value={}) as herdr, patch('dsh.subprocess.Popen', return_value=Process(events, code)) as popen:
                result = dsh.worker(job)
            state = dsh.load(job)
            text = (job / 'result.md').read_text() if (job / 'result.md').exists() else None
            self.assertIn('--session-id', popen.call_args.args[0])
            self.assertIn('session-old', popen.call_args.args[0])
            self.assertEqual(state['dsh_session_id'], 'session-new')
            self.assertTrue(any('release-agent' in call.args for call in herdr.call_args_list))
            with self.assertRaises(FileExistsError):
                dsh.worker(job)
            return result, text

    def test_success_requires_final_completed_and_exit_zero(self):
        result, text = self.execute()
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(text, 'answer')
        for reason, code, final in [('error', 0, True), ('completed', 1, True), ('completed', 0, False)]:
            with self.subTest(reason=reason, code=code, final=final):
                result, text = self.execute(reason, code, final)
                self.assertEqual(result['state'], 'failed')
                self.assertIsNone(text)

    def test_timeout_is_read_only_and_close_refuses_working(self):
        with tempfile.TemporaryDirectory() as root:
            job = self.make_job(root)
            with patch('dsh.subprocess.Popen') as popen, patch('dsh.herdr') as herdr:
                result = dsh.status(job, .01)
                self.assertTrue(result['timed_out'])
                self.assertEqual(result['state'], 'starting')
                with self.assertRaises(ValueError):
                    dsh.close(job)
                popen.assert_not_called(); herdr.assert_not_called()

    def test_herdr_always_passes_session(self):
        proc = type('Result', (), dict(returncode=0, stdout='{"id":1,"result":{"workspace":{"id":"w"}}}'))()
        with patch('dsh.subprocess.run', return_value=proc) as call:
            result = dsh.herdr(dict(herdr='h.exe', session='named'), 'workspace', 'list')
        self.assertEqual(call.call_args.args[0][:3], ['h.exe', '--session', 'named'])
        self.assertEqual(result['workspace']['id'], 'w')

    def test_herdr_empty_success_and_empty_failure(self):
        for command in [('pane', 'run', 'p1', 'command'), ('pane', 'report-agent', 'p1')]:
            proc = SimpleNamespace(returncode=0, stdout=' \n', stderr='')
            with patch('dsh.subprocess.run', return_value=proc):
                self.assertEqual(dsh.herdr({'herdr':'h.exe','session':'named'}, *command), {})
        proc = SimpleNamespace(returncode=1, stdout='', stderr='mutation rejected')
        with patch('dsh.subprocess.run', return_value=proc):
            with self.assertRaisesRegex(RuntimeError, 'mutation rejected'):
                dsh.herdr({'herdr':'h.exe','session':'named'}, 'pane', 'run', 'p1', 'command')

    def test_close_refuses_foreign_pane(self):
        with tempfile.TemporaryDirectory() as root:
            job = self.make_job(root)
            state = dsh.load(job); state['state'] = 'failed'; dsh.save(job / 'job.json', state)
            with patch('dsh.herdr', side_effect=[{'workspaces':[{'workspace_id':'w1','label':'foreign'}]}, {'panes':[{'pane_id':'p1','terminal_id':'other'}]}]) as call:
                with self.assertRaises(ValueError):
                    dsh.close(job)
                self.assertEqual(call.call_count, 2)

    def test_redaction_and_shell_wrapper_rejection(self):
        with patch.dict(os.environ, {'CPA_API_KEY':'abc123'}):
            self.assertEqual(dsh.redact('abc123'), '[REDACTED]')
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'bad.cmd'; path.touch()
            with self.assertRaises(ValueError):
                dsh.native(str(path))

    def test_run_creates_unfocused_workspace_and_dispatches_once(self):
        with tempfile.TemporaryDirectory() as root:
            prompt = Path(root) / 'input.md'; prompt.write_text('TASK_NOT_IN_SHELL')
            args = SimpleNamespace(workspace=root, prompt_file=str(prompt), session='named',
                                   profile='herdr', session_id='resume-id', herdr='h.exe',
                                   node='node.exe', dsh_bin='bin.js', timeout=.01)
            with patch('dsh.native', side_effect=lambda x:x), patch('dsh.dsh_bin', return_value='bin.js'), patch('dsh.herdr', side_effect=[
                {'workspaces':[]}, {'workspace':{'workspace_id':'w1'}, 'tab':{'tab_id':'tab1'}, 'root_pane':{'pane_id':'p1','terminal_id':'t1'}}, {}, {}, {}]) as call:
                result = dsh.run(args)
            self.assertTrue(result['timed_out'])
            self.assertEqual(call.call_count, 5)
            self.assertIn('--no-focus', call.call_args_list[1].args)
            command = call.call_args_list[-1].args[-1]
            self.assertIn('-EncodedCommand', command)
            self.assertNotIn('TASK_NOT_IN_SHELL', command)
            state = dsh.load(Path(result['job']))
            self.assertEqual(state['workspace_id'], 'w1')
            self.assertEqual(state['session'], 'named')

    def test_socks_proxy_replaced_only_for_child(self):
        events = [dict(type='status', phase='turn_end', reason={'kind':'completed'}), dict(type='final',text='ok')]
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'ALL_PROXY':'socks5://local:1', 'HTTP_PROXY':'http://local:2'}):
            job = self.make_job(root)
            with patch('dsh.herdr', return_value={}), patch('dsh.subprocess.Popen', return_value=Process(events,0)) as call:
                dsh.worker(job)
            self.assertEqual(call.call_args.kwargs['env']['ALL_PROXY'], 'http://local:2')
            self.assertEqual(os.environ['ALL_PROXY'], 'socks5://local:1')

    def test_dispatch_failures_are_recorded_without_retry(self):
        for stage in ('create', 'dispatch'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as root:
                prompt = Path(root) / 'input.md'; prompt.write_text('task')
                args = SimpleNamespace(workspace=root, prompt_file=str(prompt), session='named',
                                       profile='herdr', session_id=None, herdr='h.exe', node='node.exe',
                                       dsh_bin='bin.js', timeout=.01)
                effects = [{'workspaces':[]}, RuntimeError('create failed')] if stage == 'create' else [
                    {'workspaces':[]}, {'workspace':{'workspace_id':'w1'}, 'tab':{'tab_id':'tab1'}, 'root_pane':{'pane_id':'p1','terminal_id':'t1'}}, {}, {}, RuntimeError('send uncertain')]
                with patch('dsh.native', side_effect=lambda x:x), patch('dsh.dsh_bin', return_value='bin.js'), patch('dsh.herdr', side_effect=effects) as call:
                    result = dsh.run(args)
                self.assertEqual(result['state'], 'create_failed' if stage == 'create' else 'dispatch_uncertain')
                self.assertEqual(call.call_count, 2 if stage == 'create' else 5)
                self.assertIn('recovery', dsh.load(Path(result['job'])))
                self.assertEqual(dsh.exit_code(result), 1)

    def test_close_rejects_another_active_agent(self):
        for agent_status in ('working', 'blocked', 'idle', 'done', 'unknown'):
            with self.subTest(status=agent_status), tempfile.TemporaryDirectory() as root:
                job = self.make_job(root)
                state = dsh.load(job); state['state'] = 'succeeded'; state['worker_pid'] = 999; dsh.save(job / 'job.json', state)
                responses = [{'workspaces':[{'workspace_id':'w1'}]}, {'panes':[{'pane_id':'p1', 'terminal_id':'t1'}]},
                             {'agents':[{'terminal_id':'t1', 'agent_status':agent_status}]}]
                with patch('dsh.process_snapshot', return_value={'available':True,'processes':{}}), patch('dsh.herdr', side_effect=responses) as call:
                    with self.assertRaisesRegex(ValueError, 'still owns'):
                        dsh.close(job)
                    self.assertEqual(call.call_count, 3)

    def test_exit_codes(self):
        for state, code in [('succeeded',0), ('failed',1), ('working',2), ('starting',2), ('dispatch_uncertain',1)]:
            self.assertEqual(dsh.exit_code({'state':state}), code)

    def test_json_redaction_before_serialization(self):
        secret = 'synthetic"credential\nvalue'
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'TEST_API_KEY':secret}):
            job = self.make_job(root)
            state = dsh.load(job)
            state['nested'] = {'list':[secret]}
            dsh.save(job / 'job.json', state)
            self.assertEqual(dsh.load(job)['nested']['list'], ['[REDACTED]'])
            self.assertEqual(dsh.redact(json.dumps(secret)[1:-1]), '[REDACTED]')
            events = [dict(type='status', phase='turn_end', reason={'kind':'completed'}),
                      dict(type='final', text=secret)]
            proc = Process(events,0)
            proc.stderr = io.StringIO(secret + '\n' + json.dumps(secret)[1:-1] + '\n')
            with patch('dsh.herdr', return_value={}), patch('dsh.subprocess.Popen', return_value=proc):
                result = dsh.worker(job)
            self.assertEqual(result['state'], 'succeeded')
            logged = [json.loads(line) for line in (job / 'events.jsonl').read_text().splitlines()]
            self.assertEqual(logged[-1]['text'], '[REDACTED]')
            self.assertEqual((job / 'result.md').read_text(), '[REDACTED]')
            # stderr is a streaming, non-JSON sink; an escaped secret must also be removed.
            self.assertNotIn(json.dumps(secret)[1:-1], (job / 'stderr.log').read_text())
            self.assertNotIn(secret, (job / 'stderr.log').read_text())

    def test_monitor_loss_is_uncertain_and_never_released_or_closed(self):
        class BrokenStream:
            def __iter__(self):
                raise OSError('monitor stream failed')
        with tempfile.TemporaryDirectory() as root:
            job = self.make_job(root)
            proc = Process([],None); proc.stdout = BrokenStream()
            with patch('dsh.herdr', return_value={}) as herdr, patch('dsh.subprocess.Popen', return_value=proc) as popen:
                result = dsh.worker(job)
                self.assertEqual(result['state'], 'monitor_lost')
                self.assertEqual(dsh.exit_code(result), 1)
                self.assertEqual(dsh.load(job)['dsh_pid'], 123)
                self.assertIn('never resend', dsh.load(job)['recovery'])
                with self.assertRaises(ValueError):
                    dsh.close(job)
                self.assertEqual(popen.call_count, 1)
                self.assertFalse(any('release-agent' in call.args for call in herdr.call_args_list))

    def test_non_json_window_is_bounded_and_flushes_continuously(self):
        secret = 'synthetic\ncredential'
        with patch.dict(os.environ, {'TEST_API_KEY':secret}):
            target = io.StringIO(); sink = dsh.RedactedSink(target)
            sink.feed('synthetic\n'); sink.feed('credential\n')
            for _ in range(100):
                sink.feed('ordinary diagnostic line\n' * 100)
                self.assertLessEqual(len(sink.pending), sink.keep)
            self.assertGreater(len(target.getvalue()), 1000)
            sink.feed(final=True)
            self.assertNotIn(secret, target.getvalue())
            self.assertIn('[REDACTED]', target.getvalue())

    def test_broken_stderr_sink_is_discarded_but_source_is_drained(self):
        class BrokenSink:
            def write(self, text):
                raise OSError('disk unavailable')
            def flush(self):
                pass
        source = io.StringIO('diagnostic\n' * 10000)
        errors = []
        dsh.redact_stream(source, BrokenSink(), errors)
        self.assertEqual(source.tell(), len(source.getvalue()))
        self.assertEqual(len(errors), 1)

    def test_stderr_open_failure_still_drains_and_fails_job(self):
        with tempfile.TemporaryDirectory() as root:
            job = self.make_job(root)
            events = [dict(type='status',phase='turn_end',reason={'kind':'completed'}),dict(type='final',text='answer')]
            proc = Process(events,0)
            proc.stderr = io.StringIO('diagnostic\n' * 10000)
            original_open = Path.open
            def fail_log(path, *args, **kwargs):
                if path.name == 'stderr.log':
                    raise OSError('log cannot open')
                return original_open(path, *args, **kwargs)
            with patch('dsh.herdr',return_value={}), patch('dsh.subprocess.Popen',return_value=proc), patch.object(Path,'open',fail_log):
                result = dsh.worker(job)
            self.assertEqual(result['state'], 'failed')
            self.assertEqual(proc.stderr.tell(), len(proc.stderr.getvalue()))
            self.assertIn('stderr monitor failed',dsh.load(job)['error'])
            self.assertFalse((job / 'result.md').exists())


if __name__ == '__main__':
    unittest.main()
