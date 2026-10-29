"""Offline behavioral checks: localhost fake API and mocked CLI/Herdr processes."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import api
import layout
import run
import windows_support
from test_layout import FakeHerdr


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.server.received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        code, body = self.server.reply
        self.send_response(code)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode("utf-8"))

    def log_message(self, *args):
        pass


class Jobs(unittest.TestCase):
    def setUp(self):
        # Existing cases exercise Herdr/API lifecycle; native archiving has its own fixtures.
        archive = patch.object(run.codex_archive, 'archive', return_value={'status': 'archived'})
        archive.start()
        self.addCleanup(archive.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"TEST_EXTERNAL_API_KEY": "test-secret-12345",
                                          run.ACTIVE: "", "EXTERNAL_MODEL_CONSULT_ACTIVE": ""})
        self.env.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.reply = (200, {"status": "completed", "model": "reported-alias",
                                   "output_text": "结论：已核对", "usage": {"input_tokens": 4}})
        self.server.received = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.config = self.work / "config.toml"
        self.config.write_text('[model_providers.local]\nwire_api="responses"\n'
                               'env_key="TEST_EXTERNAL_API_KEY"\n'
                               f'base_url="http://127.0.0.1:{self.server.server_port}/v1"\n', encoding="utf-8")
        self.spec = {"mode": "direct", "provider": "local", "model": "requested-model",
                     "workspace": str(self.work), "config": str(self.config)}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.env.stop()
        self.temp.cleanup()

    def direct(self):
        return run.run(self.spec, "核验给定材料")

    def test_direct_records_reported_model_without_claiming_acceptance(self):
        output = self.direct()
        self.assertEqual(output["status"], "returned")
        self.assertEqual(output["model_requested"], "requested-model")
        self.assertEqual(output["model_reported"], "reported-alias")
        job = Path(output["job"])
        self.assertIn("结论", (job / "result.md").read_text(encoding="utf-8"))
        self.assertEqual(len(self.server.received), 1)
        self.assertNotIn("test-secret-12345", (job / "job.json").read_text(encoding="utf-8"))

    def test_dry_run_has_no_network_or_job(self):
        self.assertEqual(run.run(self.spec, "检查", True)["status"], "dry_run")
        self.assertFalse((self.work / ".agent-runs").exists())
        self.assertEqual(self.server.received, [])

    def test_incomplete_response_is_not_success_and_not_retried(self):
        self.server.reply = (200, {"status": "incomplete", "output_text": "partial"})
        output = self.direct()
        self.assertEqual(output["status"], "attention")
        self.assertEqual(len(self.server.received), 1)
        self.assertIsNone(output["result"])

    def test_http_failure_retains_job_without_echoing_response_secret(self):
        self.server.reply = (401, {"error": "sensitive-key-from-error"})
        output = self.direct()
        self.assertEqual(output["status"], "attention")
        self.assertIn("401", output["error"])
        self.assertNotIn("sensitive-key", output["error"])
        self.assertEqual(len(self.server.received), 1)

    def test_chat_and_responses_extract_only_final_content(self):
        self.assertEqual(api.decode({"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}, "chat"), "ok")
        with self.assertRaises(ValueError):
            api.decode({"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]}, "chat")

    def test_scope_and_recursive_calls_rejected_before_creation(self):
        with self.assertRaisesRegex(ValueError, "write_scope"):
            run.run({**self.spec, "access": "workspace-write"}, "修改")
        with patch.dict(os.environ, {run.ACTIVE: "1"}):
            with self.assertRaisesRegex(ValueError, "Recursive"):
                self.direct()
        self.assertFalse((self.work / ".agent-runs").exists())

    def herdr_job(self, environment=None, task_id=None, start_failure=None):
        """start_failure: the CLI exits at launch; Herdr times out and the pane shows this text ("" = unreadable)."""
        spec = {"mode": "herdr", "workspace": str(self.work), "kind": "codex",
                "provider": "local", "model": "requested-model"}
        if task_id:
            spec["task_id"] = task_id
        if environment is not None:
            spec["env"] = environment
        calls = []
        topology = FakeHerdr()
        agent = {"name": None, "terminal_id": "t1", "pane_id": "w1:p1", "agent_status": "idle", "interactive_ready": True,
                 "agent_session": {"kind": "id", "value": "030be614-4095-457b-bfa5-965d6a853009", "agent": "codex", "source": "hook"}}

        def fake(spec, *args, raw=False):
            calls.append(args)
            if args[:2] in fake.interrupt_once:
                fake.interrupt_once.remove(args[:2])
                raise KeyboardInterrupt
            if args[:2] in fake.timeout_once:
                fake.timeout_once.remove(args[:2])
                raise subprocess.TimeoutExpired("herdr", 45)
            if args[:2] in fake.fail_once:
                fake.fail_once.remove(args[:2])
                raise run.HerdrError('Herdr: {"code": "timeout", "message": "transient"}', "timeout")
            if start_failure is not None and args[:2] == ("agent", "start"):
                raise run.HerdrError('Herdr: {"error":{"code":"timeout","message":"timed out waiting for agent startup"}}',
                                     "timeout")
            if start_failure is not None and args[:2] == ("pane", "read"):
                if isinstance(start_failure, dict):
                    return start_failure.get(args[args.index('--source') + 1], '')
                if not start_failure:
                    raise run.HerdrError("Herdr: pane unreadable")
                return start_failure
            if args[:2] == ("pane", "close"):
                fake.locked_on_close.append((self.work / ".agent-runs" / ".layout.lock").exists())
            if args[0] in ("workspace", "tab", "pane"):
                value = topology(spec, *args)
                if "root_pane" in value:
                    agent.update(terminal_id=value["root_pane"]["terminal_id"])
                return value
            if args[:2] == ("agent", "start"):
                agent["name"] = args[2]
                pane = next(p for p in topology.panes if p['pane_id'] == args[args.index('--pane') + 1])
                agent.update(pane_id=pane['pane_id'], terminal_id=pane['terminal_id'])
                return {"agent": dict(agent)}
            if args[:2] == ("agent", "get") and agent.get("gone"):
                raise run.HerdrError('Herdr: {"code": "' + agent["gone"] + '", "message": "agent_not_found in text"}', agent["gone"])
            if args[:2] in (("agent", "get"), ("agent", "prompt"), ("agent", "send-keys")):
                return {"agent": {k: v for k, v in agent.items() if k != "gone"}}
            if args[:2] == ("agent", "read"):
                return "Captured external output"
            raise AssertionError(args)

        fake.topology, fake.fail_once, fake.locked_on_close = topology, [], []
        fake.interrupt_once, fake.timeout_once = [], []
        with patch.object(run, "executable", return_value="unused"), patch.object(run, "herdr", side_effect=fake):
            output = run.run(spec, "读取文件并报告")
        return output, agent, calls, fake

    def registry_tabs(self):
        cache = json.loads((self.work / ".agent-runs" / "herdr-layout.json").read_text(encoding="utf-8"))
        return [tab for binding in cache.values() for tab in binding.get("tabs", {}).values()]

    def test_start_failure_reports_the_cli_message_left_in_the_pane(self):
        # e.g. OMP 18.13 with --tools ...,find exits at once; Herdr only reports its readiness timeout
        message = "Error: Built-in tool unavailable in this session: find.\nRun `omp --help` for available flags."
        output, agent, calls, fake = self.herdr_job(start_failure=message)
        self.assertEqual(output["status"], "attention")
        self.assertIn("timed out waiting for agent startup", output["error"])
        self.assertIn("unavailable in this session: find", output["error"])
        events = (Path(output["job"]) / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn('"agent_start_failed"', events)
        read = [c for c in calls if c[:2] == ("pane", "read")]
        self.assertEqual(len(read), 1)
        start = next(c for c in calls if c[:2] == ("agent", "start"))
        self.assertEqual(read[0][2], start[start.index("--pane") + 1])  # the pane the CLI was launched in
        self.assertFalse(any(c[:2] == ("agent", "prompt") for c in calls))  # nothing was submitted

    def test_start_failure_with_an_unreadable_pane_keeps_the_herdr_error(self):
        output, agent, calls, fake = self.herdr_job(start_failure="")
        self.assertEqual(output["status"], "attention")
        self.assertIn("timed out waiting for agent startup", output["error"])
        self.assertNotIn("ends with", output["error"])
        events = (Path(output["job"]) / "events.jsonl").read_text(encoding="utf-8")
        self.assertNotIn('"agent_start_failed"', events)

    def test_start_failure_falls_back_to_visible_modal_when_scrollback_is_blank(self):
        output, _, calls, _ = self.herdr_job(start_failure={'visible': 'Do you trust this folder?'})
        self.assertIn('Do you trust this folder?', output['error'])
        sources = [c[c.index('--source') + 1] for c in calls if c[:2] == ('pane', 'read')]
        self.assertEqual(sources, ['recent-unwrapped', 'visible'])
        self.assertFalse(any(c[:2] == ('agent', 'prompt') for c in calls))

    def test_close_releases_pane_tab_and_registry_once(self):
        output, agent, calls, fake = self.herdr_job()
        self.assertEqual(len(self.registry_tabs()), 1)
        with patch.object(run, "herdr", side_effect=fake):
            closed = run.close(output["job"])
            again = run.close(output["job"])
        self.assertEqual(fake.topology.panes, [])
        self.assertEqual(fake.topology.tabs, [])
        self.assertEqual(self.registry_tabs(), [])
        self.assertEqual(closed["agent_status"], "closed")
        self.assertEqual(again["agent_status"], "closed")
        self.assertEqual(sum(call[:2] == ("pane", "close") for call in calls), 1)
        self.assertEqual(fake.locked_on_close, [True])  # no job can split into the tab meanwhile
        self.assertTrue((Path(output["job"]) / "request.md").exists())
        state = run.load(Path(output["job"]) / "job.json")
        self.assertTrue(state["tab_closed"])

    def test_close_retry_finishes_a_partial_close(self):
        output, agent, calls, fake = self.herdr_job()
        fake.fail_once.append(("tab", "close"))
        with patch.object(run, "herdr", side_effect=fake):
            with self.assertRaisesRegex(ValueError, "transient"):
                run.close(output["job"])
            self.assertEqual(fake.topology.panes, [])
            self.assertEqual(len(fake.topology.tabs), 1)  # pane closed, empty tab left behind
            self.assertFalse(run.load(Path(output["job"]) / "job.json").get("pane_closed"))
            agent["gone"] = "agent_not_found"
            retried = run.close(output["job"])
        self.assertEqual(fake.topology.tabs, [])
        self.assertEqual(retried["agent_status"], "closed")
        self.assertTrue(run.load(Path(output["job"]) / "job.json")["tab_closed"])

    def test_close_refuses_when_the_agent_query_fails_for_another_reason(self):
        output, agent, calls, fake = self.herdr_job()
        agent["gone"] = "timeout"
        with patch.object(run, "herdr", side_effect=fake):
            with self.assertRaisesRegex(ValueError, "timeout"):
                run.close(output["job"])
        self.assertEqual(len(fake.topology.panes), 1)

    def test_close_refuses_a_busy_agent_left_in_the_terminal(self):
        output, agent, calls, fake = self.herdr_job()
        agent["gone"] = "agent_not_found"
        fake.topology.panes[0]["agent_status"] = "working"
        with patch.object(run, "herdr", side_effect=fake):
            with self.assertRaisesRegex(ValueError, "Another agent"):
                run.close(output["job"])
        self.assertEqual(len(fake.topology.panes), 1)

    def second_job(self, fake, task_id="T-1"):
        spec = {"mode": "herdr", "workspace": str(self.work), "kind": "codex",
                "provider": "local", "model": "requested-model", "task_id": task_id}
        with patch.object(run, "executable", return_value="unused"), patch.object(run, "herdr", side_effect=fake):
            return run.run(spec, "second job")

    def test_layout_failure_after_split_is_recorded_and_closable_by_task(self):
        output, agent, calls, fake = self.herdr_job(task_id="T-1")
        fake.fail_once.append(("pane", "rename"))
        failed = self.second_job(fake)
        self.assertEqual(failed["status"], "attention")
        self.assertTrue(any(call[:2] == ("pane", "split") for call in calls))
        state = run.load(Path(failed["job"]) / "job.json")
        self.assertTrue(state["terminal_id"] and state["workspace_id"] and state["task_key"])
        self.assertNotIn("agent_name", state)
        self.assertEqual(len(fake.topology.panes), 2)
        argv = ["run.py", "close", "--task-key", state["task_key"], "--workspace", str(self.work)]
        with patch.object(run, "herdr", side_effect=fake), patch.object(sys, "argv", argv), patch("builtins.print"):
            self.assertEqual(run.main(), 0)  # both jobs; the failed one's attention status does not count
            self.assertEqual(run.main(), 0)  # nothing left to close
        self.assertEqual(fake.topology.panes, [])
        self.assertEqual(fake.topology.tabs, [])
        self.assertEqual(run.load(Path(failed["job"]) / "job.json")["status"], "attention")

    def test_task_close_skips_dsh_records_and_keeps_other_tasks(self):
        output, agent, calls, fake = self.herdr_job(task_id="T-1")
        first_agent = dict(agent)  # the fixture exposes one agent at a time
        other = self.second_job(fake, task_id="T-2")
        agent.update(first_agent)
        dsh = self.work / ".agent-runs" / "000-dsh"
        dsh.mkdir()
        run.save(dsh / "job.json", {"state": "closed", "task_key": output["task_key"],
                                    "terminal_id": "dsh-owned-terminal"})
        before = (dsh / "job.json").read_bytes()
        code, rows = self.cli(fake, "close", "--task-key", output["task_key"], "--workspace", str(self.work))
        self.assertEqual(code, 0)
        self.assertEqual([(r["job"], r["close"]) for r in rows], [(output["job"], "closed")])
        self.assertEqual((dsh / "job.json").read_bytes(), before)
        self.assertEqual([p["terminal_id"] for p in fake.topology.panes],
                         [run.load(Path(other["job"]) / "job.json")["terminal_id"]])

    def test_task_close_reports_unreadable_records_and_still_releases_valid_jobs(self):
        output, agent, calls, fake = self.herdr_job(task_id="T-1")
        bad = {"000-json": "{", "001-object": "[]", "002-spec": '{"spec": null}'}
        before = {}
        for name, content in bad.items():
            folder = self.work / ".agent-runs" / name
            folder.mkdir()
            record = folder / "job.json"
            record.write_text(content, encoding="utf-8")
            before[record] = record.read_bytes()
        state = run.load(Path(output["job"]) / "job.json")
        state["codex_archive"] = None  # optional fields from older records
        run.save(Path(output["job"]) / "job.json", state)
        code, rows = self.cli(fake, "close", "--task-key", output["task_key"], "--workspace", str(self.work))
        self.assertEqual(code, 2)
        self.assertEqual([r["job"] for r in rows if r["close"] == "closed"], [output["job"]])
        refused = [r for r in rows if r["close"] == "refused"]
        self.assertEqual({Path(r["job"]).name for r in refused}, set(bad))
        self.assertTrue(all(r.get("error") for r in refused))
        self.assertEqual(fake.topology.panes, [])
        for record, content in before.items():
            self.assertEqual(record.read_bytes(), content)

    def test_close_dsh_record_explains_its_own_lifecycle_without_controlling_herdr(self):
        dsh = self.work / "dsh-record"
        dsh.mkdir()
        run.save(dsh / "job.json", {"state": "running", "terminal_id": "dsh-owned-terminal"})
        with patch.object(run, "herdr") as live:
            with self.assertRaisesRegex(ValueError, "dsh.py"):
                run.close(dsh)
        live.assert_not_called()

    def test_interrupt_after_split_still_records_the_pane(self):
        output, agent, calls, fake = self.herdr_job(task_id="T-1")
        fake.interrupt_once.append(("pane", "rename"))
        failed = self.second_job(fake)
        self.assertEqual(failed["status"], "interrupted")
        state = run.load(Path(failed["job"]) / "job.json")
        self.assertTrue(state["terminal_id"])
        with patch.object(run, "herdr", side_effect=fake):
            run.close(failed["job"])
        self.assertEqual(len(fake.topology.panes), 1)  # the first job's pane only

    def test_a_timeout_on_one_job_does_not_stop_the_batch(self):
        output, agent, calls, fake = self.herdr_job(task_id="T-1")
        fake.fail_once.append(("pane", "rename"))
        failed = self.second_job(fake)
        fake.timeout_once.append(("agent", "get"))  # the first job's observe
        argv = ["run.py", "close", "--job", output["job"], "--job", failed["job"]]
        with patch.object(run, "herdr", side_effect=fake), patch.object(sys, "argv", argv), \
                patch("builtins.print") as printed:
            self.assertEqual(run.main(), 2)
        results = json.loads(printed.call_args[0][0])
        self.assertEqual([r["close"] for r in results], ["refused", "closed"])
        self.assertEqual([p["terminal_id"] for p in fake.topology.panes],
                         [run.load(Path(output["job"]) / "job.json")["terminal_id"]])

    def test_only_the_structured_not_found_code_counts_as_gone(self):
        output, agent, calls, fake = self.herdr_job()
        agent["gone"] = "internal"  # its message still mentions agent_not_found
        with patch.object(run, "herdr", side_effect=fake):
            with self.assertRaisesRegex(ValueError, "internal"):
                run.close(output["job"])
        self.assertEqual(len(fake.topology.panes), 1)

    def test_close_cli_exits_nonzero_when_any_job_is_refused(self):
        idle, _, _, fake_idle = self.herdr_job()
        busy, agent, _, fake = self.herdr_job()
        agent["agent_status"] = "working"
        argv = ["run.py", "close", "--job", busy["job"]]
        with patch.object(run, "herdr", side_effect=fake), patch.object(sys, "argv", argv), \
                patch("builtins.print"):
            self.assertEqual(run.main(), 2)
        with patch.object(run, "herdr", side_effect=fake_idle), \
                patch.object(sys, "argv", ["run.py", "close", "--job", idle["job"]]), patch("builtins.print"):
            self.assertEqual(run.main(), 0)

    def test_close_refuses_working_agent_unless_forced(self):
        output, agent, calls, fake = self.herdr_job()
        agent["agent_status"] = "working"
        with patch.object(run, "herdr", side_effect=fake):
            with self.assertRaisesRegex(ValueError, "working"):
                run.close(output["job"])
            self.assertEqual(len(fake.topology.panes), 1)
            run.close(output["job"], force=True)
        self.assertEqual(fake.topology.panes, [])

    def test_close_after_agent_exit_still_closes_its_pane_but_not_a_replacement(self):
        output, agent, calls, fake = self.herdr_job()
        agent["gone"] = "agent_not_found"
        with patch.object(run, "herdr", side_effect=fake):
            run.close(output["job"])
        self.assertEqual(fake.topology.panes, [])
        output, agent, calls, fake = self.herdr_job()
        agent["terminal_id"] = "replacement"
        with patch.object(run, "herdr", side_effect=fake):
            with self.assertRaisesRegex(ValueError, "identity changed"):
                run.close(output["job"])
        self.assertEqual(len(fake.topology.panes), 1)

    def test_close_keeps_the_tab_while_another_pane_is_open(self):
        output, agent, calls, fake = self.herdr_job()
        tab = fake.topology.tabs[0]
        fake.topology.new_pane(tab["workspace_id"], tab["tab_id"], str(self.work))
        with patch.object(run, "herdr", side_effect=fake):
            run.close(output["job"])
        self.assertEqual(len(fake.topology.tabs), 1)
        self.assertFalse(any(call[:2] == ("tab", "close") for call in calls))
        self.assertFalse(run.load(Path(output["job"]) / "job.json")["tab_closed"])
        self.assertEqual(len(self.registry_tabs()), 1)  # the other pane's tab stays registered

    def test_prune_drops_tabs_closed_outside_the_script(self):
        output, agent, calls, fake = self.herdr_job()
        fake.topology.tabs.clear()
        fake.topology.panes.clear()
        report = layout.prune(str(self.work), {}, fake)
        self.assertEqual(len(report["tabs_dropped"]), 1)
        self.assertEqual(self.registry_tabs(), [])

    def cli(self, fake, *argv):
        out = {}
        with patch.object(run, "herdr", side_effect=fake), patch.object(sys, "argv", ["run.py", *argv]), \
                patch("builtins.print", side_effect=lambda text: out.update(value=json.loads(text))):
            code = run.main()
        return code, out.get("value")

    def test_herdr_errors_carry_their_code_with_any_exit_status(self):
        missing = '{"error":{"code":"agent_not_found","message":"agent target x not found"},"id":"cli:agent:get"}'
        # the real CLI: exit 1 with the JSON on stderr (checked live); also stdout and exit 0
        for code, stdout, stderr in ((1, "", missing), (1, missing, ""), (0, missing, ""), (1, "", "not json"), (1, "[1]", "")):
            done = subprocess.CompletedProcess(["herdr"], code, stdout=stdout, stderr=stderr)
            with patch.object(run, "executable", return_value="herdr"), patch.object(run.subprocess, "run", return_value=done):
                with self.assertRaises(run.HerdrError) as caught:
                    run.herdr({}, "agent", "get", "x")
            self.assertEqual(caught.exception.code, "agent_not_found" if missing in (stdout, stderr) else None,
                             (code, stdout, stderr))
        # an error without a code on one stream does not hide the coded one on the other
        done = subprocess.CompletedProcess(["herdr"], 1, stdout=missing, stderr='{"error": "diagnostic"}')
        with patch.object(run, "executable", return_value="herdr"), patch.object(run.subprocess, "run", return_value=done):
            with self.assertRaises(run.HerdrError) as caught:
                run.herdr({}, "agent", "get", "x")
        self.assertEqual(caught.exception.code, "agent_not_found")
        # a failing permission diagnosis keeps the structured error
        denied = '{"error":{"code":"agent_not_found","message":"PermissionDenied"}}'
        done = subprocess.CompletedProcess(["herdr"], 1, stdout="", stderr=denied)
        with patch.object(run, "executable", return_value="herdr"), patch.object(run.subprocess, "run", return_value=done), \
                patch.object(run.windows_support, "diagnose", side_effect=OSError("herdr.sock")):
            with self.assertRaises(run.HerdrError) as caught:
                run.herdr({}, "agent", "get", "x")
        self.assertEqual(caught.exception.code, "agent_not_found")
        self.assertIn("diagnosis failed", str(caught.exception))
        # a normal result envelope is a result, not an error
        done = subprocess.CompletedProcess(["herdr"], 0, stdout='{"result": {"panes": []}, "id": "cli"}', stderr="")
        with patch.object(run, "executable", return_value="herdr"), patch.object(run.subprocess, "run", return_value=done):
            self.assertEqual(run.herdr({}, "pane", "list"), {"panes": []})

    def test_close_and_collect_through_the_real_cli_error_format(self):
        """The fake below answers like the real CLI (exit 1, JSON on stderr) and goes through herdr()."""
        job = self.work / ".agent-runs" / "20260101T000000Z-bbbbbbbbbbbb"
        job.mkdir(parents=True)
        run.save(job / "job.json", {"id": job.name, "status": "completed", "agent_name": "agy-bbbbbbbbbbbb",
                                    "terminal_id": "term-9", "workspace_id": "w1", "tab_id": "w1:t1",
                                    "project_root": str(self.work), "spec": {"mode": "herdr", "kind": "agy"}})
        missing = '{"error":{"code":"agent_not_found","message":"agent target agy-bbbbbbbbbbbb not found"},"id":"cli:agent:get"}'

        def cli(command, **kwargs):
            args = command[1:]
            if args[:2] == ["agent", "get"]:
                return subprocess.CompletedProcess(command, 1, stdout="", stderr=missing)
            if args[:2] in (["pane", "list"], ["tab", "list"]):
                key = "panes" if args[0] == "pane" else "tabs"
                return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"result": {key: []}}), stderr="")
            raise AssertionError(args)

        state = run.load(job / "job.json")
        state.update(native_session={"kind": "id", "value": "ce50fc80-d333-40af-814e-ae5326dd50d1", "agent": "agy"}, last_request=str(job / "request.md"))
        run.save(job / "job.json", state)
        logs = self.work / "brain" / "ce50fc80-d333-40af-814e-ae5326dd50d1" / ".system_generated" / "logs"
        logs.mkdir(parents=True)
        (logs / "transcript_full.jsonl").write_text("\n".join(json.dumps(e) for e in (
            {"type": "USER_INPUT", "content": "Task %s. Read request.md" % job.name},
            {"type": "PLANNER_RESPONSE", "status": "DONE", "content": "答案" + "。" * 220})) + "\n", encoding="utf-8")
        with patch.object(run, "executable", return_value="herdr"), patch.object(run.subprocess, "run", side_effect=cli), \
                patch.object(run, "AGY_BRAIN", self.work / "brain"):
            collected = run.collect(job)  # the agent is gone, the job not yet closed
            closed = run.close(job)
        self.assertEqual(collected["collect"], "collected")
        self.assertEqual(closed["close"], "closed")
        state = run.load(job / "job.json")
        self.assertTrue(state["pane_closed"])
        self.assertFalse(state["closed_live"])

    def test_finish_close_releases_the_pane_after_acceptance(self):
        output, agent, calls, fake = self.herdr_job()
        result = self.work / "answer.md"
        result.write_text("结论", encoding="utf-8")
        review = self.work / "review.md"
        review.write_text("checked", encoding="utf-8")
        code, out = self.cli(fake, "finish", "--job", output["job"], "--result-file", str(result),
                             "--review-file", str(review), "--outcome", "completed", "--close")
        self.assertEqual((code, out["status"], out["close"]), (0, "completed", "closed"))
        self.assertEqual(fake.topology.panes, [])

    def test_finish_close_keeps_the_acceptance_when_the_close_is_refused(self):
        output, agent, calls, fake = self.herdr_job()
        agent["agent_status"] = "working"
        result = self.work / "answer.md"
        result.write_text("结论", encoding="utf-8")
        review = self.work / "review.md"
        review.write_text("checked", encoding="utf-8")
        code, out = self.cli(fake, "finish", "--job", output["job"], "--result-file", str(result),
                             "--review-file", str(review), "--outcome", "completed", "--close")
        self.assertEqual((code, out["status"], out["close"]), (2, "completed", "refused"))
        self.assertEqual(run.load(Path(output["job"]) / "job.json")["status"], "completed")
        self.assertEqual(len(fake.topology.panes), 1)
        direct = self.direct()
        code, out = self.cli(fake, "finish", "--job", direct["job"], "--review-file", str(review),
                             "--outcome", "completed", "--close")
        self.assertEqual((code, out["close"]), (0, "no_pane"))

    def test_plain_list_is_unchanged_by_older_broken_records(self):
        older = self.work / ".agent-runs" / "20200101T000000Z-000000000000"
        older.mkdir(parents=True)
        (older / "job.json").write_text("{", encoding="utf-8")
        newest = self.direct()
        with patch.object(sys, "argv", ["run.py", "list", "--workspace", str(self.work), "--limit", "1"]), \
                patch("builtins.print") as printed:
            self.assertEqual(run.main(), 0)
        listed = json.loads(printed.call_args[0][0])
        self.assertEqual([r["id"] for r in listed], [newest["id"]])
        self.assertNotIn("updated_at", listed[0])

    def test_run_reuses_an_idle_pane_of_the_same_task(self):
        output, agent, calls, fake = self.herdr_job(task_id="T-1")
        spec = {"mode": "herdr", "workspace": str(self.work), "kind": "codex", "provider": "local",
                "model": "requested-model", "task_id": "T-1", "reuse": True}
        with patch.object(run, "executable", return_value="unused"), patch.object(run, "herdr", side_effect=fake):
            again = run.run(spec, "第二轮")
            other = run.run({**spec, "model": "another-model"}, "换了模型")  # a different CLI setup: new pane
            agent["agent_status"] = "working"
            busy = run.run(spec, "第三轮")  # the matching pane is busy: new pane
        self.assertTrue(again["reused"])
        self.assertEqual(again["job"], output["job"])
        self.assertNotIn("reused", other)
        self.assertNotIn("reused", busy)
        self.assertEqual(len(fake.topology.panes), 3)
        state = run.load(Path(output["job"]) / "job.json")
        self.assertIn("第二轮", Path(state["last_request"]).read_text(encoding="utf-8"))
        # first run, the reused round, the other model; the fake has one agent for every pane, so the
        # busy case's new agent also reports working and stops before its prompt
        self.assertEqual(sum(call[:2] == ("agent", "prompt") for call in calls), 3)
        self.assertEqual(busy["status"], "attention")

    def test_reuse_compares_the_real_launch_arguments(self):
        output, agent, calls, fake = self.herdr_job(task_id="T-1")
        spec = {"mode": "herdr", "workspace": str(self.work), "kind": "codex", "provider": "local",
                "model": "requested-model", "task_id": "T-1", "reuse": True}
        with patch.object(run, "executable", return_value="unused"), patch.object(run, "herdr", side_effect=fake):
            dirs = run.run({**spec, "add_dirs": [str(self.work)]}, "多一个可写目录")
            provider = run.run({**spec, "provider": "elsewhere"}, "换了渠道")
            effort = run.run({**spec, "effort": "high"}, "换了强度")
        self.assertFalse(any(r.get("reused") for r in (dirs, provider, effort)))
        self.assertEqual(len(fake.topology.panes), 4)

    def test_reuse_picks_the_newest_by_creation_time(self):
        spec = {"mode": "herdr", "workspace": str(self.work), "kind": "codex", "provider": "local",
                "model": "requested-model", "task_id": "T-1", "project_root": str(self.work)}
        key = layout.key("T-1")
        for name, created in (("20260101T000000Z-aaaaaaaaaaaa", "2026-01-01T00:00:00.900000+00:00"),
                              ("20260101T000000Z-ffffffffffff", "2026-01-01T00:00:00.100000+00:00")):
            job = self.work / ".agent-runs" / name
            job.mkdir(parents=True)
            run.save(job / "job.json", {"id": name, "created_at": created, "spec": spec, "task_key": key,
                                        "agent_name": "codex-" + name[-4:]})
        (self.work / ".agent-runs" / "broken").mkdir()
        (self.work / ".agent-runs" / "broken" / "job.json").write_text("{", encoding="utf-8")
        self.assertEqual(run.reusable_job(spec, key).name, "20260101T000000Z-aaaaaaaaaaaa")

    def test_reuse_never_resends_after_a_failed_send(self):
        output, agent, calls, fake = self.herdr_job(task_id="T-1")
        fake.fail_once.append(("agent", "prompt"))
        spec = {"mode": "herdr", "workspace": str(self.work), "kind": "codex", "provider": "local",
                "model": "requested-model", "task_id": "T-1", "reuse": True}
        with patch.object(run, "executable", return_value="unused"), patch.object(run, "herdr", side_effect=fake):
            with self.assertRaisesRegex(ValueError, "transient"):
                run.run(spec, "第二轮")
        self.assertEqual(len(fake.topology.panes), 1)

    def test_every_mode_records_its_task(self):
        self.spec["task_id"] = "T-9"
        self.spec["task_name"] = "复核"
        output = self.direct()
        self.assertEqual((output["task_key"], output["task_name"]), (layout.key("T-9"), "复核"))
        with patch.object(sys, "argv", ["run.py", "list", "--workspace", str(self.work), "--task-key", layout.key("T-9")]), \
                patch("builtins.print") as printed:
            run.main()
        self.assertEqual([r["id"] for r in json.loads(printed.call_args[0][0])], [output["id"]])

    def test_list_open_marks_panes_closed_by_hand(self):
        output, agent, calls, fake = self.herdr_job()
        code, listed = self.cli(fake, "list", "--workspace", str(self.work), "--open")
        self.assertEqual([r["pane_live"] for r in listed], [True])
        fake.topology.panes.clear()
        code, listed = self.cli(fake, "list", "--workspace", str(self.work), "--open")
        self.assertEqual([r["pane_live"] for r in listed], [False])
        with patch.object(run, "herdr", side_effect=fake):
            run.close(output["job"])
        code, listed = self.cli(fake, "list", "--workspace", str(self.work), "--open")
        self.assertEqual(listed, [])

    def test_list_open_skips_dsh_and_malformed_records(self):
        output, agent, calls, fake = self.herdr_job(task_id="T-1")
        for name, content in (("000-dsh", {"state": "running", "task_key": output["task_key"]}),
                              ("001-object", []), ("002-spec", {"spec": None})):
            folder = self.work / ".agent-runs" / name
            folder.mkdir()
            run.save(folder / "job.json", content)
        code, rows = self.cli(fake, "list", "--workspace", str(self.work), "--open", "--task-key", output["task_key"])
        self.assertEqual(code, 0)
        self.assertEqual([(r["job"], r["pane_live"]) for r in rows], [(output["job"], True)])

    def agy_job(self, entries, last_request="request-abc.md"):
        job = self.work / ".agent-runs" / "20260101T000000Z-aaaaaaaaaaaa"
        job.mkdir(parents=True)
        (job / "request.md").write_text("first", encoding="utf-8")
        run.save(job / "job.json", {"id": job.name, "status": "submitted", "spec": {"mode": "herdr", "kind": "agy"},
                                    "native_session": {"kind": "id", "value": "bca38f5d-2306-4a90-af24-78ff8415825b", "agent": "agy"},
                                    "pane_closed": True, "last_request": str(job / last_request)})
        logs = self.work / "brain" / "bca38f5d-2306-4a90-af24-78ff8415825b" / ".system_generated" / "logs"
        logs.mkdir(parents=True)
        for entry in entries:
            if entry.get('type') == 'PLANNER_RESPONSE':
                entry.setdefault('status', 'DONE')
        (logs / "transcript_full.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
        (self.work / "brain" / "bca38f5d-2306-4a90-af24-78ff8415825b" / "plan.md").write_text("plan", encoding="utf-8")
        return job

    def test_collect_takes_this_rounds_answer_from_the_agy_transcript(self):
        long = lambda text: text + "。" * 220
        jid = "20260101T000000Z-aaaaaaaaaaaa"
        job = self.agy_job([
            {"type": "USER_INPUT", "content": "Task %s. Read request.md" % jid},
            {"type": "PLANNER_RESPONSE", "content": long("第一轮答案")},
            {"type": "USER_INPUT", "content": "Task %s. Read request-abc.md" % jid},
            {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "view_file"}]},
            {"type": "GENERIC", "content": long("文件内容不是答案")},
            {"type": "PLANNER_RESPONSE", "content": long("第二轮答案"), "step_index": 5},
            {"type": "USER_INPUT", "content": "Task another-job. Read request.md"},
            {"type": "PLANNER_RESPONSE", "content": long("别的任务")}])
        with patch.object(run, "AGY_BRAIN", self.work / "brain"):
            out = run.collect(job)
        self.assertEqual(out["collect"], "collected")
        self.assertTrue((job / "result.md").read_text(encoding="utf-8").startswith("第二轮答案"))
        self.assertEqual(run.load(job / "job.json")["collected_step"], 5)
        self.assertEqual(len(out["artifacts"]), 1)

    def test_collect_reports_no_answer_and_keeps_old_results(self):
        jid = "20260101T000000Z-aaaaaaaaaaaa"
        job = self.agy_job([{"type": "USER_INPUT", "content": "Task %s. Read request-abc.md" % jid},
                            {"type": "PLANNER_RESPONSE", "content": "短"}])
        with patch.object(run, "AGY_BRAIN", self.work / "brain"):
            out = run.collect(job, min_chars=200)
            with self.assertRaisesRegex(ValueError, "request-zzz.md"):
                state = run.load(job / "job.json")
                state["last_request"] = str(job / "request-zzz.md")
                run.save(job / "job.json", state)
                run.collect(job)
        self.assertEqual((out["collect"], out["result"]), ("none", None))
        self.assertFalse((job / "result.md").exists())

    def test_short_agy_final_is_collected_by_default_and_explicit_length_is_explained(self):
        jid = "20260101T000000Z-aaaaaaaaaaaa"
        job = self.agy_job([{"type": "USER_INPUT", "content": "Task %s. Read request-abc.md" % jid},
                            {"type": "PLANNER_RESPONSE", "content": "OK"}])
        with patch.object(run, "AGY_BRAIN", self.work / "brain"):
            accepted = run.collect(job)
            self.assertEqual(accepted["collect"], "collected")
            self.assertEqual((job / "result.md").read_text(encoding="utf-8").strip(), "OK")
            short = run.collect(job, min_chars=200)
        self.assertEqual((short["collect"], short["reason"], short["answer_chars"], short["min_chars"]),
                         ("none", "answer_too_short", 2, 200))
        self.assertIsNone(short["result"])
        self.assertFalse(run.load(job / "job.json")["result_current"])

    def test_collect_refuses_to_guess_the_round_of_an_older_job(self):
        long = "第二轮答案" + "。" * 220
        jid = "20260101T000000Z-aaaaaaaaaaaa"
        job = self.agy_job([{"type": "USER_INPUT", "content": "Task %s. Read request.md" % jid},
                            {"type": "PLANNER_RESPONSE", "content": "第一轮" + "。" * 220},
                            {"type": "USER_INPUT", "content": "Task %s. Read request-abc.md" % jid},
                            {"type": "PLANNER_RESPONSE", "content": long}])
        state = run.load(job / "job.json")
        del state["last_request"]  # recorded only since this change
        run.save(job / "job.json", state)
        (job / "request-abc.md").write_text("follow-up", encoding="utf-8")
        with patch.object(run, "AGY_BRAIN", self.work / "brain"):
            with self.assertRaisesRegex(ValueError, "--request"):
                run.collect(job)
            out = run.collect(job, request="request-abc.md")
        self.assertEqual(out["collect"], "collected")
        self.assertEqual((job / "result.md").read_text(encoding="utf-8").strip(), long)

    def test_herdr_idle_is_not_completed_and_busy_followup_is_not_sent(self):
        output, agent, calls, fake = self.herdr_job()
        self.assertEqual(output["status"], "submitted")
        agent["agent_status"] = "working"
        prompt = self.work / "followup.md"
        prompt.write_text("进一步检查", encoding="utf-8")
        before = len(calls)
        with patch.object(run, "herdr", side_effect=fake):
            with self.assertRaisesRegex(ValueError, "idle/done"):
                run.manage(argparse.Namespace(command="followup", job=output["job"], prompt_file=str(prompt)))
        self.assertEqual(len(calls), before + 1)

    def test_replaced_agent_is_not_controlled(self):
        output, agent, calls, fake = self.herdr_job()
        agent["terminal_id"] = "replacement"
        before = len(calls)
        with patch.object(run, "herdr", side_effect=fake):
            with self.assertRaisesRegex(ValueError, "identity changed"):
                run.manage(argparse.Namespace(command="cancel", job=output["job"]))
        self.assertEqual(len(calls), before + 1)

    def test_followup_invalidates_old_result_and_cancel_needs_confirmation(self):
        output, agent, calls, fake = self.herdr_job()
        job = Path(output["job"])
        run.write(job / "result.md", "OLD result")
        review = self.work / "review.md"
        review.write_text("checked", encoding="utf-8")
        first = run.manage(argparse.Namespace(command="finish", job=str(job), result_file=str(job / "result.md"),
                                             review_file=str(review), outcome="completed"))
        self.assertIsNotNone(first["result"])
        followup = self.work / "followup.md"
        followup.write_text("检查另一个边界条件", encoding="utf-8")
        with patch.object(run, "herdr", side_effect=fake):
            second = run.manage(argparse.Namespace(command="followup", job=str(job), prompt_file=str(followup)))
        self.assertIsNone(second["result"])
        self.assertEqual(second["status"], "submitted")
        finish = argparse.Namespace(command="finish", job=str(job), result_file=None,
                                    review_file=str(review), outcome="completed")
        with self.assertRaisesRegex(ValueError, "newly collected"):
            run.manage(finish)
        with patch.object(run, "herdr", side_effect=fake):
            canceled = run.manage(argparse.Namespace(command="cancel", job=str(job)))
        self.assertEqual(canceled["status"], "cancel_requested")

    def test_changed_native_session_is_rejected_even_in_same_terminal(self):
        output, agent, calls, fake = self.herdr_job()
        agent["agent_session"] = {**agent["agent_session"], "value": "another-conversation"}
        before = len(calls)
        with patch.object(run, "herdr", side_effect=fake):
            with self.assertRaisesRegex(ValueError, "session changed"):
                run.manage(argparse.Namespace(command="cancel", job=output["job"]))
        self.assertEqual(len(calls), before + 1)

    def test_model_arguments_are_applied_and_child_delegation_disabled(self):
        args = run.codex_args({"model": "exact-model", "provider": "provider-a", "access": "workspace-write"})
        self.assertEqual(args[args.index("--model") + 1], "exact-model")
        self.assertEqual(args[args.index("--sandbox") + 1], "workspace-write")
        self.assertIn('model_provider="provider-a"', args)
        self.assertIn("multi_agent", args)
        self.assertIn("multi_agent_v2", args)

    def test_dispatch_requires_activity_acknowledgement(self):
        output, agent, calls, fake = self.herdr_job()
        prompt_calls = [call for call in calls if call[:2] == ("agent", "prompt")]
        self.assertEqual(len(prompt_calls), 1)
        self.assertIn("--wait", prompt_calls[0])
        self.assertIn("working", prompt_calls[0])
        self.assertEqual(output["status"], "submitted")

    def test_herdr_proxy_environment_is_scoped_to_created_workspace(self):
        previous = os.environ.get("HTTPS_PROXY")
        output, agent, calls, fake = self.herdr_job({"HTTPS_PROXY": "http://127.0.0.1:9876"})
        created = next(call for call in calls if call[:2] == ("workspace", "create"))
        self.assertIn("HTTPS_PROXY=http://127.0.0.1:9876", created)
        self.assertEqual(os.environ.get("HTTPS_PROXY"), previous)
        with self.assertRaisesRegex(ValueError, "Reserved"):
            run.run({**self.spec, "env": {run.ACTIVE: "0"}}, "inspect", dry=True)

    def test_visible_launcher_refuses_known_integrity_mismatch(self):
        with patch.object(windows_support, "diagnose", return_value={"cause": "integrity mismatch", "remedy": "restart normally"}), patch.object(windows_support.subprocess, "Popen") as start:
            with self.assertRaisesRegex(ValueError, "integrity mismatch"):
                windows_support.show("herdr.exe", "test")
            start.assert_not_called()

    def test_visible_launcher_requests_separate_terminal(self):
        with patch.object(windows_support, "diagnose", return_value={"current_process": {"elevated": False}}), patch.object(windows_support.shutil, "which", return_value="wt.exe"), patch.object(windows_support.subprocess, "Popen") as start:
            start.return_value.pid = 123
            result = windows_support.show("herdr.exe", "test")
            self.assertEqual(result["status"], "launch_requested")
            self.assertEqual(start.call_args.args[0][:4], ["wt.exe", "-w", "new", "new-tab"])

    def test_explicit_review_and_local_status_while_locked(self):
        output = self.direct()
        job = Path(output["job"])
        review = self.work / "review.md"
        review.write_text("已对照原始材料，结论成立", encoding="utf-8")
        result = run.manage(argparse.Namespace(command="finish", job=str(job), result_file=None,
                                               review_file=str(review), outcome="completed"))
        self.assertEqual(result["status"], "completed")
        with run.locked(job):
            result = run.manage(argparse.Namespace(command="status", job=str(job), refresh=False))
            self.assertEqual(result["status"], "completed")
            with self.assertRaisesRegex(ValueError, "lock"):
                with run.locked(job):
                    pass

    def test_exec_persists_session_events_and_final_answer(self):
        spec = {"mode": "exec", "provider": "local", "model": "requested-model", "workspace": str(self.work)}

        def fake(command, **kwargs):
            self.assertNotIn("--ephemeral", command)
            self.assertIn("--json", command)
            self.assertIn("read-only", command)
            self.assertEqual(kwargs["env"][run.ACTIVE], "1")
            kwargs["stdout"].write(json.dumps({"type": "thread.started", "thread_id": "saved-session"}) + "\n")
            Path(command[command.index("-o") + 1]).write_text("finished", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0)

        with patch.object(run, "executable", return_value="codex-native"), patch.object(run, "git_state", return_value=None), patch.object(run.subprocess, "run", side_effect=fake):
            result = run.run(spec, "inspect")
        self.assertEqual(result["status"], "returned")
        self.assertEqual(result["native_session"]["value"], "saved-session")
        self.assertIsNone(result["model_reported"])

    def test_exec_timeout_preserves_partial_events(self):
        spec = {"mode": "exec", "provider": "local", "model": "requested-model", "workspace": str(self.work)}

        def fake(command, **kwargs):
            kwargs["stdout"].write('{"type":"thread.started","thread_id":"partial-session"}\n')
            raise subprocess.TimeoutExpired("codex", 1)

        with patch.object(run, "executable", return_value="codex-native"), patch.object(run, "git_state", return_value=None), patch.object(run.subprocess, "run", side_effect=fake):
            result = run.run(spec, "inspect")
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["native_session"]["value"], "partial-session")


if __name__ == "__main__":
    unittest.main()
