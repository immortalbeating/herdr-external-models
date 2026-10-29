#!/usr/bin/env python3
"""External-model tasks with local records; direct API, Codex exec, or Herdr."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

import api
import windows_support
import layout
import native_records
import record_redaction
import review_inputs
import completion_checks
import codex_archive

ACTIVE = "HERDR_EXTERNAL_MODELS_ACTIVE"


def now():
    return datetime.now(timezone.utc).isoformat()


def redact(text):
    return record_redaction.redact(text, min_length=8)


def redact_value(value):
    return record_redaction.redact_value(value, min_length=8, redact_keys=True)

def write(path, text):
    Path(path).write_text(redact(text), encoding="utf-8")


def save(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(redact_value(value), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def run_job_spec(state):
    if isinstance(state, dict) and "state" in state and "status" not in state:
        raise ValueError("DSH jobs use dsh.py for their own lifecycle")
    spec = state.get("spec") if isinstance(state, dict) else None
    if not isinstance(spec, dict) or spec.get("mode") not in ("direct", "exec", "herdr"):
        raise ValueError("Invalid run.py job spec; inspect the record before controlling its pane")
    return spec


def read_run_jobs(paths, problems=None):
    """Mixed .agent-runs contains DSH too; isolate unreadable records during discovery."""
    for path in paths:
        try:
            state = load(path)
            if isinstance(state, dict) and "state" in state and "status" not in state:
                continue  # DSH's separate lifecycle owns this record and its pane.
            run_job_spec(state)
        except (OSError, UnicodeError, ValueError) as exc:
            if problems is not None:
                problems.append({"job": str(path.parent), "close": "refused",
                                 "error": "Cannot inspect job record: " + type(exc).__name__ + ": " + str(exc)})
            continue
        yield path.parent, state


def update(job, state, event, **fields):
    state.update(fields, updated_at=now())
    save(job / "job.json", state)
    with (job / "events.jsonl").open("a", encoding="utf-8") as out:
        out.write(json.dumps(redact_value({"at": now(), "event": event, **fields}), ensure_ascii=False) + "\n")


@contextmanager
def locked(job):
    path = job / ".lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ValueError("Job has an active/stale .lock; inspect its owner before retrying") from None
    try:
        with os.fdopen(fd, "w") as out:
            out.write(str(os.getpid()))
        yield
    finally:
        path.unlink(missing_ok=True)


def executable(name):
    override = os.environ.get("CODEX_EXECUTABLE") if name == "codex" else None
    found = override or shutil.which(name)
    if not found:
        raise ValueError("Executable not found: " + name)
    if name == "codex" and Path(found).suffix.lower() in (".cmd", ".bat", ".ps1"):
        # Avoid cmd.exe interpreting model/provider values as shell syntax.
        package = Path(found).parent / "node_modules" / "@openai" / "codex"
        candidates = list(package.glob("node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe"))
        candidates += list(package.glob("vendor/*/codex/codex.exe"))
        if len(candidates) != 1:
            raise ValueError("Set CODEX_EXECUTABLE to the native codex.exe path")
        found = str(candidates[0])
    if Path(found).suffix.lower() in (".cmd", ".bat", ".ps1") or not Path(found).is_file():
        raise ValueError("Use a native executable; shell wrappers are not executed")
    return found


def codex_args(spec):
    args = ["--model", spec["model"], "--sandbox", spec.get("access", "read-only"),
            "--disable", "multi_agent", "--disable", "multi_agent_v2", "-c", "approval_policy=\"never\"",
            "-c", "model_provider=" + json.dumps(spec["provider"])]
    if spec.get("effort"):
        args += ["-c", "model_reasoning_effort=" + json.dumps(spec["effort"])]
    for image in spec.get("images", []):
        args += ["--image", image]
    for extra in spec.get("add_dirs", []):  # extra writable dirs for workspace-write (e.g. an isolated test user dir)
        args += ["--add-dir", extra]
    return args


def validate(spec):
    mode = spec.get("mode")
    if mode not in ("direct", "exec", "herdr"):
        raise ValueError("mode must be direct, exec, or herdr")
    if not isinstance(spec.get("model"), str) or not spec["model"].strip():
        raise ValueError("Specify the exact requested model; there is no automatic fallback")
    work = Path(spec["workspace"]).expanduser()
    if not work.is_absolute() or not work.is_dir():
        raise ValueError("workspace must be an existing absolute directory")
    spec["workspace"] = str(work.resolve())
    if spec.get("access", "read-only") not in ("read-only", "workspace-write", "danger-full-access"):
        raise ValueError("access must be read-only, workspace-write or danger-full-access")
    if spec.get("access") in ("workspace-write", "danger-full-access") and not spec.get("write_scope"):
        raise ValueError(spec["access"] + " requires write_scope")
    if spec.get("access") == "danger-full-access" and not spec.get("sandbox_bypass_reason"):
        raise ValueError("danger-full-access (no sandbox) requires sandbox_bypass_reason")
    if spec.get("write_scope") and (not isinstance(spec["write_scope"], list)
                                   or not all(isinstance(x, str) and x.strip() for x in spec["write_scope"])):
        raise ValueError("write_scope must be a list of nonempty strings")
    if not isinstance(spec.get("timeout", 180), (float, int)) or spec.get("timeout", 180) <= 0:
        raise ValueError("timeout must be positive")
    if "images" in spec and not isinstance(spec["images"], list):
        raise ValueError("images must be an array")
    if "add_dirs" in spec and (not isinstance(spec["add_dirs"], list)
                               or not all(isinstance(x, str) and Path(x).is_absolute() and Path(x).is_dir() for x in spec["add_dirs"])):
        raise ValueError("add_dirs must be a list of existing absolute directories")
    if "env" in spec and (not isinstance(spec["env"], dict) or not all(
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) and isinstance(value, str)
            and "\x00" not in value for key, value in spec["env"].items())):
        raise ValueError("env must map variable names to plain strings; no secrets")
    if ACTIVE in spec.get("env", {}) or "EXTERNAL_MODEL_CONSULT_ACTIVE" in spec.get("env", {}):
        raise ValueError("Reserved recursion guard environment variable")
    for field in ("project_name", "parent_thread_id", "task_id", "task_name", "checkpoint_id"):  # recorded in every mode
        if field in spec and (not isinstance(spec[field], str) or not spec[field].strip()):
            raise ValueError(field + " must be a nonempty string")
    if not isinstance(spec.get("reuse", False), bool):
        raise ValueError("reuse must be true or false")
    if "review_inputs" in spec:
        spec["review_inputs"] = review_inputs.validate_paths(spec["review_inputs"])
        if spec.get("access", "read-only") != "read-only":
            raise ValueError("review_inputs binds read-only review tasks, not implementation output")
    for path in spec.get("images", []):
        if not Path(path).is_absolute():
            raise ValueError("Use absolute image paths")
        api.image_url(path)
    if mode in ("direct", "exec") or (mode == "herdr" and spec.get("kind") == "codex" and "cli_args" not in spec):
        if not spec.get("provider"):
            raise ValueError("Specify provider explicitly")
    if mode == "direct":
        api.provider(spec)
    elif mode == "exec":
        if "config" in spec:
            raise ValueError("exec uses CODEX_HOME; config file override is only supported in direct mode")
        executable("codex")
    else:
        executable("herdr")
        spec["project_root"] = layout.project_root(spec["workspace"], spec.get("project_root"))
        for field in ("project_name", "parent_thread_id", "task_id", "task_name", "agent_label", "role"):
            if field in spec and (not isinstance(spec[field], str) or not spec[field].strip()):
                raise ValueError(field + " must be a nonempty string")
        if spec.get("split_direction", "right") not in ("right", "down"):
            raise ValueError("split_direction must be right or down")
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", spec.get("kind", "")):
            raise ValueError("Specify a Herdr-supported kind")
        if "cli_args" in spec:
            if not isinstance(spec["cli_args"], list) or not all(isinstance(x, str) for x in spec["cli_args"]):
                raise ValueError("cli_args must be a string array, never a shell command")
            if not spec.get("permission_note", "").strip():
                raise ValueError("Custom cli_args require permission_note describing verified CLI permissions")
        elif spec["kind"] != "codex":
            raise ValueError("Non-Codex Herdr kinds require cli_args and permission_note")
        if spec.get("images") and (spec["kind"] != "codex" or "cli_args" in spec):
            raise ValueError("For this CLI use its native image arguments; images is only automatic for Codex")
    if redact(json.dumps(spec, ensure_ascii=False)) != json.dumps(spec, ensure_ascii=False):
        raise ValueError("Spec contains a credential environment value; reference existing auth instead")
    return spec


class HerdrError(ValueError):
    """A Herdr CLI failure; `code` is Herdr's structured error code when it returned one."""
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def error_code(*streams):
    """Herdr's structured error code from the first output holding `{"error": {"code": "<text>"}}`
    (a failed command prints it on stderr with exit 1), else None."""
    for stream in streams:
        try:
            error = json.loads(stream).get("error")
        except (ValueError, AttributeError, TypeError):
            continue
        if isinstance(error, dict) and isinstance(error.get("code"), str) and error["code"]:
            return error["code"]
    return None


def pane_tail(spec, pane, lines=12):
    """The pane's last lines as plain text, or "" when they cannot be read (a diagnostic extra only)."""
    for source in ("recent-unwrapped", "visible"):
        try:
            text = herdr(spec, "pane", "read", pane, "--source", source, "--lines", str(lines),
                         "--format", "text", raw=True).strip()
            if text:
                return text
        except Exception:
            pass
    return ""


def herdr(spec, *args, raw=False, rpc_timeout=45):
    command = [executable("herdr")]
    if spec.get("session"):
        command += ["--session", spec["session"]]
    command += list(args)
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=rpc_timeout, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout)[-3000:]
        code = error_code(result.stderr, result.stdout)  # first: nothing below may lose it
        if "PermissionDenied" in detail or "拒绝访问" in detail:
            try:
                detail += "\n" + json.dumps(windows_support.diagnose(spec.get("session")), ensure_ascii=False)
            except Exception as exc:  # the diagnosis is an extra; it never hides the error itself
                detail += "\n(diagnosis failed: %s)" % exc
        raise HerdrError("Herdr: " + detail, code)
    if raw:
        return result.stdout
    data = json.loads(result.stdout)
    if data.get("error"):
        error = data["error"]
        raise HerdrError("Herdr: " + json.dumps(error), error.get("code") if isinstance(error, dict) else None)
    return data["result"]


def observe(job, state):
    info = herdr(state["spec"], "agent", "get", state["agent_name"])["agent"]
    if info.get("terminal_id") != state.get("terminal_id") or info.get("name") != state["agent_name"]:
        raise ValueError("Agent identity changed; inspect/recover the native session before rebinding")
    native = info.get("agent_session")
    if state.get("native_session") and native and native != state["native_session"]:
        raise ValueError("Native agent session changed; refusing to control a replacement conversation")
    update(job, state, "observed", agent_status=info["agent_status"],
           native_session=native or state.get("native_session"), pane_id=info["pane_id"],
           **{k: info[k] for k in ("workspace_id", "tab_id") if k in info})
    return info


def git_state(work):
    if not shutil.which("git"):
        return None
    records = {}
    for name, args in (("head", ["rev-parse", "HEAD"]), ("status", ["status", "--short"])):
        proc = subprocess.run(["git", "-C", work, *args], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=15)
        if proc.returncode == 0:
            records[name] = proc.stdout.strip()
    return records or None


def wait_native_ready(job, state, info, timeout=30):
    """TUI idle may precede the native identity hook; do not submit during that gap."""
    kind = state['spec']['kind']
    label = {'claude': 'Claude', 'codex': 'Codex', 'agy': 'Agy'}[kind]
    native_required = kind == 'claude'  # Codex/Agy may allocate their conversation on the first prompt.
    deadline = time.monotonic() + timeout
    native_seen = state.get("native_session")
    waited = False
    while True:
        if time.monotonic() >= deadline:
            raise ValueError(label + " initialization timed out: native session and interactive_ready required; "
                             "no task was submitted. Inspect the pane and integration before continuing")
        if (info.get("name") != state["agent_name"]
                or any(info.get(key) != state.get(key) for key in ("terminal_id", "pane_id"))):
            raise ValueError(label + " identity changed during initialization; no task was submitted")
        if info.get("agent_status") == "blocked" or (info.get("agent_status") == "working"
                and info.get("agent_session") and info.get("interactive_ready") is True):
            raise ValueError(label + " is " + info["agent_status"] + "; inspect before submitting task")
        native = info.get("agent_session")
        if native:
            try:
                valid = (native.get("agent") == kind and native.get("kind") == "id"
                         and isinstance(native.get("value"), str))
                if not valid:
                    raise ValueError
                if str(uuid.UUID(native["value"])) != native["value"].lower():
                    raise ValueError
            except (ValueError, AttributeError, TypeError):
                raise ValueError("Invalid " + label + " native session identity; no task was submitted") from None
            if native_seen and native != native_seen:
                raise ValueError(label + " native session changed during initialization; no task was submitted")
            native_seen = native
        if ((native or (not native_required and native_seen is None)) and info.get("interactive_ready") is True
                and info.get("agent_status") in ("idle", "done")):
            update(job, state, "agent_ready", native_session=native,
                   agent_status=info["agent_status"], interactive_ready=True,
                   native_identity_pending_at_start=native is None)
            return info
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError(label + " initialization timed out: native session and interactive_ready required; "
                             "no task was submitted. Inspect the pane and integration before continuing")
        if not waited:
            update(job, state, "agent_readiness_wait", readiness_timeout_seconds=timeout)
            waited = True
        time.sleep(min(0.25, remaining))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            continue
        info = herdr(state["spec"], "agent", "get", state["agent_name"],
                     rpc_timeout=remaining)["agent"]


def bind_review_inputs(job, state, request):
    """Optional per-round binding; ordinary calls do no extra file reads."""
    paths = state["spec"].get("review_inputs")
    if not paths:
        return
    saved = review_inputs.snapshot([*paths, str(request)])
    saved.update(request=request.name, checkpoint_id=state["spec"].get("checkpoint_id"), captured_at=now())
    receipt = job / ("input-receipt-" + request.stem + ".json")
    save(receipt, saved)
    update(job, state, "inputs_bound", review_binding={"request": request.name, "receipt": str(receipt),
           "digest": saved["digest"]}, last_request=str(request))


def check_review_inputs(job, state):
    binding = state.get("review_binding")
    if not binding and not state["spec"].get("review_inputs"):
        return {"status": "not_recorded"}  # legacy and ordinary jobs remain compatible
    try:
        if not isinstance(binding, dict):
            raise ValueError("No per-round input receipt")
        receipt = Path(binding["receipt"]).resolve()
        if receipt.parent != job.resolve() or not receipt.name.startswith("input-receipt-"):
            raise ValueError("Input receipt must belong to this job")
        saved = load(receipt)
        if not isinstance(saved, dict) or not isinstance(saved.get("files"), list) or not all(isinstance(item, dict) for item in saved["files"]):
            raise ValueError("Invalid input receipt")
        marker = Path(state.get("last_request") or "request.md").name
        if saved.get("request") != marker or binding.get("request") != marker or saved.get("digest") != binding.get("digest"):
            raise ValueError("Input receipt does not identify the current request")
        expected = review_inputs.validate_paths([*state["spec"]["review_inputs"], str(job / marker)])
        if [item.get("path") for item in saved.get("files", [])] != expected:
            raise ValueError("Input receipt does not cover this round's selected files")
        checked = review_inputs.check(saved)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        checked = {"status": "unavailable", "errors": [{"reason": type(exc).__name__}], "changed": []}
    checked.update(request=Path(state.get("last_request") or "request.md").name)
    save(job / "input-check.json", checked)
    fields = {"input_check": checked}
    if checked["status"] != "match":
        fields.update(status="attention", result_current=False)
    update(job, state, "inputs_checked", **fields)
    return checked


def execute(job, state, prompt):
    spec = state["spec"]
    mode = spec["mode"]
    if mode != "herdr":
        bind_review_inputs(job, state, job / "request.md")
    if mode == "direct":
        update(job, state, "started", status="running")
        content, receipt = api.call(spec, prompt)
        write(job / "result.md", content + "\n")
        update(job, state, "returned", status="returned", result_current=True, **receipt)
    elif mode == "exec":
        command = [executable("codex"), "exec", *codex_args(spec), "--json", "--color", "never",
                   "-C", spec["workspace"], "-o", str(job / "result.md")]
        if spec.get("skip_git_repo_check"):
            command += ["--skip-git-repo-check"]
        command += ["-"]
        update(job, state, "started", status="running")
        env = dict(os.environ, **spec.get("env", {}), **{ACTIVE: "1", "EXTERNAL_MODEL_CONSULT_ACTIVE": "1"})
        try:
            with (job / "cli-events.jsonl").open("w", encoding="utf-8") as out, (job / "stderr.log").open("w", encoding="utf-8") as err:
                proc = subprocess.run(command, input=prompt, stdout=out, stderr=err, text=True,
                                      encoding="utf-8", cwd=spec["workspace"], env=env,
                                      timeout=spec.get("timeout", 600), check=False)
        finally:
            for name in ("cli-events.jsonl", "stderr.log", "result.md"):
                path = job / name
                if path.exists():
                    write(path, path.read_text(encoding="utf-8", errors="replace"))
            for line in (job / "cli-events.jsonl").read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "thread.started" and event.get("thread_id"):
                    update(job, state, "session", native_session={"kind": "id", "value": event["thread_id"], "agent": "codex"})
        if proc.returncode:
            raise ValueError(f"Codex exited {proc.returncode}; inspect stderr.log and cli-events.jsonl")
        if not (job / "result.md").exists() or not (job / "result.md").read_text(encoding="utf-8").strip():
            raise ValueError("Codex returned without a final result")
        update(job, state, "returned", status="returned", result_current=True, exit_code=proc.returncode)
    else:
        spec_args = spec.get("cli_args")
        args = spec_args if spec_args is not None else codex_args(spec)
        name = spec["kind"][:12] + "-" + state["id"].split("-")[-1]
        # Task identity first, so `close --task-key` finds the job even if the layout fails midway.
        ident = layout.identity(spec, state["id"])
        update(job, state, "task_identified", **{k: ident[k] for k in ("project_root", "task_key", "task_name")})
        try:
            location = layout.allocate(spec, state["id"], herdr)
        except (Exception, KeyboardInterrupt) as exc:
            if getattr(exc, "partial", None):  # a pane was created before the failure: keep its IDs for close
                update(job, state, "layout_partial", **exc.partial)
            raise
        pane = location["pane_id"]
        update(job, state, "layout_assigned", agent_name=name, **location)
        try:
            info = herdr(spec, "agent", "start", name, "--kind", spec["kind"], "--pane", pane, "--", *args)["agent"]
        except HerdrError as exc:
            # A CLI that exits at launch (unknown flag, a tool unavailable in this install) reaches Herdr only
            # as a readiness timeout; its own message stays in the pane, so the error and the record carry it.
            tail = pane_tail(spec, pane)
            if not tail:
                raise
            update(job, state, "agent_start_failed", pane_tail=tail)
            raise HerdrError("%s\nPane %s ends with:\n%s" % (exc, pane, tail), exc.code) from exc
        update(job, state, "agent_started", terminal_id=info["terminal_id"],
               native_session=info.get("agent_session"), agent_status=info["agent_status"])
        if spec["kind"] in ("claude", "codex", "agy"):
            try:
                info = wait_native_ready(job, state, info)
            except (Exception, KeyboardInterrupt) as exc:
                update(job, state, "agent_readiness_failed", initialization_failed_before_submission=True,
                       readiness_error=redact(str(exc)))
                raise
        if info["agent_status"] not in ("idle", "done"):
            raise ValueError("Agent is not idle; inspect before submitting task")
        submit(job, state, job / "request.md")


def submit(job, state, path):
    # Pass a bounded instruction; the CLI reads the full prompt inside its workspace.
    message = (f"Task {state['id']}. Read the task file {json.dumps(str(path), ensure_ascii=False)} "
               "and complete that task within its stated scope. Follow its requested output format; "
               "if none is specified, return concise findings, artifacts, verification and gaps. "
               "Do not invoke external collaboration recursively.")
    # last_request names this round's request file: `collect` finds the round's answer by it.
    update(job, state, "submission_started", status="submitting", error=None, result_current=False,
           review_binding=None, input_check=None, last_request=str(path), last_submitted_at=now())
    bind_review_inputs(job, state, path)
    response = herdr(state["spec"], "agent", "prompt", state["agent_name"], message,
                     "--wait", "--until", "working", "--until", "blocked", "--until", "done", "--until", "idle",
                     "--timeout", "15000")
    info = response["agent"]
    update(job, state, "submitted", status="attention" if info["agent_status"] == "blocked" else "submitted",
           agent_status=info["agent_status"], native_session=info.get("agent_session") or state.get("native_session"))


def run(spec, prompt, dry=False):
    if os.environ.get(ACTIVE) == "1" or os.environ.get("EXTERNAL_MODEL_CONSULT_ACTIVE") == "1":
        raise ValueError("Recursive external collaboration refused")
    spec = validate(dict(spec))
    if not prompt.strip():
        raise ValueError("Prompt is empty")
    if redact(prompt) != prompt:
        raise ValueError("Prompt contains a credential environment value")
    if dry:
        return {"status": "dry_run", "spec": spec, "network_called": False}
    owned = bool(spec.get("task_id") or spec.get("parent_thread_id"))
    if spec["mode"] == "herdr" and spec.get("reuse") and owned:
        reused = reuse(spec, prompt)
        if reused is not None:
            return reused
    root = Path(spec["project_root"] if spec["mode"] == "herdr" else spec["workspace"]) / ".agent-runs"
    root.mkdir(exist_ok=True)
    ignore = root / ".gitignore"
    if not ignore.exists():
        write(ignore, "*\n")
    ident = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12]
    job = root / ident
    job.mkdir()
    state = {"id": ident, "created_at": now(), "status": "prepared", "spec": spec,
             "model_requested": spec["model"], "model_reported": None,
             "native_session": None, "git_before": git_state(spec["workspace"])}
    if owned:  # every mode records its task, so `list --task-key` finds exec and direct jobs too
        task = layout.task_identity(spec, ident, str(root.parent))
        state.update({k: task[k] for k in ("project_name", "parent_thread_id", "task_key", "task_name")})
    contract = ("\n\n## Execution boundary\nAccess: " + spec.get("access", "read-only")
                + "\nWrite scope: " + json.dumps(spec.get("write_scope", []), ensure_ascii=False)
                + "\nPreserve existing changes. Do not delegate further or invoke this skill recursively.\n")
    prompt += contract
    write(job / "request.md", prompt)
    with locked(job):
        update(job, state, "created")
        try:
            execute(job, state, prompt)
            if spec["mode"] != "herdr":
                check_review_inputs(job, state)
        except (Exception, KeyboardInterrupt) as exc:
            update(job, state, "attention", status="interrupted" if isinstance(exc, (subprocess.TimeoutExpired, KeyboardInterrupt)) else "attention",
                   error=redact(type(exc).__name__ + ": " + str(exc)))
    return summary(job, state)


def summary(job, state):
    return {"job": str(job), **{key: state.get(key) for key in
            ("id", "status", "agent_status", "model_requested", "model_reported", "cli_model_observed", "cli_effort_observed", "native_session", "error",
              "project_root", "project_name", "parent_thread_id", "task_key", "task_name", "workspace_id", "tab_id", "pane_id", "pane_label",
              "review_binding", "input_check", "codex_archive")},
            "result": str(job / "result.md") if state.get("result_current") and (job / "result.md").exists() else None}


def archive_closed_codex(job, state):
    """The caller holds the job lock; a native archive failure never undoes pane closure."""
    if state['spec'].get('kind') != 'codex':
        return
    if not state.get('native_session'):
        update(job, state, 'codex_archive_skipped', codex_archive={'status': 'skipped', 'reason': 'no_native_session'})
        return
    update(job, state, 'codex_archive_started', codex_archive={'status': 'pending'})
    try:
        result = codex_archive.archive(state, executable('codex'))
    except Exception as exc:
        result = {'status': 'failed', 'error': type(exc).__name__ + ': ' + str(exc)}
    fields = {'codex_archive': result}
    native = state.get('native_session')
    if result['status'] == 'archived' and isinstance(native, dict) and native.get('kind') == 'path':
        fields['native_session'] = {**native, 'value': result['transcript']}
    update(job, state, 'codex_archive_' + result['status'], **fields)


def close(job, force=False):
    """Release a Herdr job's pane: refuse a working/blocked agent unless forced, close only the
    pane that still hosts this job's terminal, close its tab when no pane is left, and drop the
    terminal from the layout registry. Records and results stay on disk."""
    job = Path(job).resolve()
    with locked(job):
        state = load(job / "job.json")
        spec = run_job_spec(state)
        if spec["mode"] != "herdr":
            raise ValueError("close requires a Herdr job")
        if state.get("pane_closed"):
            archive_closed_codex(job, state)
            return {**summary(job, state), "close": "closed"}
        if not state.get("terminal_id") or not state.get("workspace_id"):
            # No pane was ever recorded (allocate records a created pane even when it fails later).
            update(job, state, "pane_closed", pane_closed=True, closed_live=False, tab_closed=False)
            archive_closed_codex(job, state)
            return {**summary(job, state), "close": "closed"}
        info = None
        if state.get("agent_name"):
            try:
                info = observe(job, state)
            except HerdrError as exc:
                # Only Herdr's own agent_not_found code means it exited; any other failure refuses, so a
                # failed query cannot bypass the working/blocked check. Identity changes raise ValueError.
                if exc.code != "agent_not_found":
                    raise
        if info and info["agent_status"] in ("working", "blocked") and not force:
            raise ValueError("Agent is " + info["agent_status"] + "; wait, cancel, or pass --force before closing")
        project = state.get("project_root") or spec["project_root"]
        # Hold the layout lock so no job can split a new pane into this tab between the checks and the closes.
        with layout.layout_lock(Path(project) / ".agent-runs"):
            wid = state["workspace_id"]
            panes = workspace_items(spec, "pane", wid)
            mine = next((p for p in panes if p["terminal_id"] == state.get("terminal_id")), None)
            if mine and info is None and mine.get("agent_status") in ("working", "blocked") and not force:
                raise ValueError("Another agent is " + mine["agent_status"] + " in this terminal; inspect before closing")
            tab_id = mine["tab_id"] if mine else state.get("tab_id")
            if mine:
                herdr(spec, "pane", "close", mine["pane_id"])
                update(job, state, "pane_close_sent", pane_close_sent=True)
            # Re-query: close the tab only when it is live and empty (also finishes a retried partial close).
            remaining = workspace_items(spec, "pane", wid)
            if any(p.get('terminal_id') == state.get('terminal_id') for p in remaining):
                raise ValueError('Herdr still reports the owned terminal after close; inspect and retry')
            left = [p for p in remaining if p["tab_id"] == tab_id]
            tabs = workspace_items(spec, "tab", wid)
            tab_closed = False
            if tab_id and not left and any(t["tab_id"] == tab_id for t in tabs):
                herdr(spec, "tab", "close", tab_id)
                tab_closed = True
            tab_live = any(t["tab_id"] == tab_id for t in tabs) and not tab_closed
            dropped = layout.forget_locked(project, spec, [state.get("terminal_id")], drop_empty=not tab_live)
        update(job, state, "pane_closed", pane_closed=True, agent_status="closed",
               closed_live=bool(mine), tab_closed=tab_closed, registry_tabs_dropped=dropped)
        archive_closed_codex(job, state)
        return {**summary(job, state), "close": "closed"}


def workspace_items(spec, kind, workspace_id):
    """The last pane may remove its workspace; only Herdr's exact absence code is empty."""
    try:
        return herdr(spec, kind, "list", "--workspace", workspace_id)[kind + "s"]
    except HerdrError as exc:
        if exc.code != "workspace_not_found":
            raise
        return []


def same_path(first, second):
    return os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second))


def without_resume(args):
    """Ignore only native resume selectors when comparing a restored command's launch policy."""
    result, i = [], 0
    while i < len(args):
        arg = args[i]
        if arg in ("--resume", "-r"):
            i += 2
        elif arg.startswith("--resume="):
            i += 1
        else:
            result.append(arg)
            i += 1
    return result


def recover(job, apply=False, pane=None, runtime_note=None):
    """Explicitly associate an idle OMP/Claude native conversation after a terminal restart.

    Never starts a CLI, submits a prompt, or guesses a latest session. Launch-policy
    drift must be recorded explicitly and is never eligible for automatic spec reuse.
    """
    job = Path(job).resolve()
    with locked(job):
        state = load(job / "job.json")
        spec, kind = state["spec"], state["spec"].get("kind")
        if spec["mode"] != "herdr" or kind not in ("omp", "claude"):
            raise ValueError("recover supports Herdr OMP/Claude jobs only")
        if state.get("pane_closed"):
            raise ValueError("Closed jobs cannot take over a new pane")
        transcript = native_records.resolve_transcript(kind, state.get("native_session"), spec["workspace"],
                                                        spec.get("env"), spec.get("cli_args"))
        project = Path(state.get("project_root") or spec["project_root"])
        with layout.layout_lock(project / ".agent-runs"):
            snapshot = herdr(spec, "api", "snapshot")["snapshot"]
            candidates = []
            for candidate in snapshot["agents"]:
                if candidate.get("agent") != kind or not candidate.get("cwd") or not same_path(candidate["cwd"], spec["workspace"]):
                    continue
                if not candidate.get("agent_session"):
                    continue
                try:
                    source = native_records.resolve_transcript(kind, candidate["agent_session"], spec["workspace"],
                                                               spec.get("env"), spec.get("cli_args"))
                except (ValueError, OSError):
                    continue
                if same_path(source, transcript):
                    candidates.append(candidate)
            if len(candidates) != 1:
                raise ValueError("Recovery needs exactly one matching native conversation; found " + str(len(candidates)))
            candidate = candidates[0]
            if pane and candidate["pane_id"] != pane:
                raise ValueError("Selected pane does not match the recorded native conversation")
            if candidate.get("agent_status") not in ("idle", "done") or candidate.get("interactive_ready") is False:
                raise ValueError("Recovered agent must be idle/done and ready before association")
            if not candidate.get("name") or not candidate.get("terminal_id"):
                raise ValueError("Recovered agent lacks a stable alias/terminal identity")
            # An existing job's ownership wins, even when native sessions happen to match.
            for path in (project / ".agent-runs").glob("*/job.json"):
                if path.parent == job:
                    continue
                other = load(path)
                if other.get("pane_closed") or other.get("spec", {}).get("mode") != "herdr":
                    continue
                if layout.server_key(other["spec"]) == layout.server_key(spec) and other.get("terminal_id") == candidate["terminal_id"]:
                    raise ValueError("Recovered terminal is owned by another job: " + path.parent.name)
            process = herdr(spec, "pane", "process-info", "--pane", candidate["pane_id"])["process_info"]
            commands = [entry.get("argv") for entry in process.get("foreground_processes", []) if entry.get("argv")]
            if len(commands) != 1:
                raise ValueError("Cannot uniquely inspect the recovered CLI launch arguments")
            actual = without_resume(commands[0][1:])
            expected = without_resume(launch_args(spec))
            same_launch = actual == expected
            changed_terminal = candidate["terminal_id"] != state.get("terminal_id")
            env_unverified = bool(spec.get("env")) and (changed_terminal or state.get("recovery_environment_unverified", False))
            needs_note = not same_launch or env_unverified
            plan = {"recovery": "preview", "job": str(job), "native_transcript": str(transcript),
                    "previous_terminal_id": state.get("terminal_id"),
                    "candidate": {key: candidate.get(key) for key in ("name", "workspace_id", "tab_id", "pane_id", "terminal_id", "agent_status")},
                    "launch_arguments_preserved": same_launch, "environment_needs_review": env_unverified,
                    "expected_flags": [arg.split("=", 1)[0] for arg in expected if arg.startswith("-")],
                    "observed_flags": [arg.split("=", 1)[0] for arg in actual if arg.startswith("-")],
                    "runtime_note_required": needs_note, "prompt_sent": False}
            if not apply:
                return plan
            if runtime_note is not None and (not runtime_note.strip() or redact(runtime_note) != runtime_note):
                raise ValueError("runtime-note must be nonempty and contain no credential values")
            if needs_note and not runtime_note:
                raise ValueError("Restored launch arguments/environment differ; inspect policy and pass --runtime-note, or restart the CLI with the required flags before recovery")
            # Re-read immediately before the write so a replacement cannot inherit this job.
            fresh = herdr(spec, "agent", "get", candidate["name"])["agent"]
            if any(fresh.get(key) != candidate.get(key) for key in ("terminal_id", "pane_id", "agent_session", "agent_status", "name")):
                raise ValueError("Recovered agent changed during inspection; retry from preview")
            location = {key: fresh[key] for key in ("workspace_id", "tab_id", "pane_id", "terminal_id")}
            layout.rebind_locked(project, spec, state["task_key"], state.get("terminal_id"), location)
            previous = {key: state.get(key) for key in ("agent_name", "workspace_id", "tab_id", "pane_id", "terminal_id", "native_session")}
            update(job, state, "native_recovered", **location, agent_name=fresh["name"],
                   native_session=fresh["agent_session"], agent_status=fresh["agent_status"],
                   recovery_previous=previous, recovery_launch_args_verified=same_launch and not env_unverified,
                   recovery_environment_unverified=env_unverified,
                   runtime_policy_note=runtime_note or "Original launch arguments preserved; same native conversation and workspace verified")
            return {**summary(job, state), **plan, "recovery": "associated"}


def send_followup(job, state, info, text, inputs=None, checkpoint=None):
    """Send `text` as the next round of this job (the caller holds its lock and observed `info`)."""
    if info["agent_status"] not in ("idle", "done"):
        raise ValueError("Agent must be idle/done before follow-up; inspect working/blocked state")
    text = text.strip()
    if not text or redact(text) != text:
        raise ValueError("Follow-up is empty or contains a credential environment value")
    if inputs is not None:
        state["spec"]["review_inputs"] = review_inputs.validate_paths(inputs)
        if state["spec"].get("access", "read-only") != "read-only":
            raise ValueError("review_inputs binds read-only review tasks")
    if checkpoint is not None:
        if not checkpoint.strip():
            raise ValueError("checkpoint-id must not be empty")
        state["spec"]["checkpoint_id"] = checkpoint
    path = job / ("request-" + uuid.uuid4().hex[:8] + ".md")
    text += ("\n\nOriginal access: " + state["spec"].get("access", "read-only")
             + "\nOriginal write scope: " + json.dumps(state["spec"].get("write_scope", []), ensure_ascii=False)
             + "\nScope expansion requires a new authorized task.\n")
    write(path, text)
    try:
        submit(job, state, path)
    except (Exception, KeyboardInterrupt) as exc:
        update(job, state, "submission_uncertain", status="attention", error=redact(str(exc)))
        raise


## What must match for `run` to reuse a job's pane instead of opening a new one (spec `reuse`), besides
## the launch arguments the CLI really got (which carry provider, effort, images and extra dirs).
REUSE_FIELDS = ("mode", "workspace", "session", "kind", "model", "access", "write_scope", "env")


def launch_args(spec):
    """The arguments the agent is started with: the spec's own cli_args, else the generated Codex ones."""
    return spec["cli_args"] if spec.get("cli_args") is not None else codex_args(spec)


def reusable_job(spec, task_key):
    """The newest open Herdr job (by creation time) of this task with the same CLI, launch arguments
    and permissions, or None."""
    root = Path(spec["project_root"]) / ".agent-runs"
    states = []
    for path in root.glob("*/job.json"):
        try:
            states.append((path.parent, load(path)))
        except ValueError:
            continue  # a broken record never matches
    want = launch_args(spec)
    for job, state in sorted(states, key=lambda js: (js[1].get("created_at", ""), js[0].name), reverse=True):
        old = state.get("spec", {})
        if state.get("task_key") == task_key and state.get("agent_name") and not state.get("pane_closed"):
            if state.get("recovery_launch_args_verified") is False:
                continue  # its original spec no longer proves the restored process's launch policy
            try:
                same = all(old.get(k) == spec.get(k) for k in REUSE_FIELDS) and launch_args(old) == want
            except KeyError:
                same = False
            if same:
                return job
            # the newest open job of the task with another setup does not hide an older matching one
    return None


def reuse(spec, prompt):
    """Send `prompt` as a follow-up to an idle job of the same task and CLI. Returns its summary
    (with `reused`), or None when there is none or it is busy, gone or locked: then `run` opens a new pane."""
    task_key = layout.task_identity(spec, "", spec["project_root"])["task_key"]
    job = reusable_job(spec, task_key)
    if job is None:
        return None
    sending = False
    try:
        with locked(job):
            state = load(job / "job.json")
            info = observe(job, state)
            if info["agent_status"] not in ("idle", "done"):
                return None
            sending = True
            # A fresh run spec controls this round; direct followup deliberately inherits its scope.
            state["spec"].pop("review_inputs", None)
            state["spec"].pop("checkpoint_id", None)
            send_followup(job, state, info, prompt, spec.get("review_inputs"), spec.get("checkpoint_id"))
            return {**summary(job, state), "reused": True}
    except ValueError:
        if sending:
            raise  # the follow-up may have reached the agent: never send the task again in a new pane
        return None  # locked, agent gone or replaced, or a Herdr failure before sending


AGY_BRAIN = Path(os.environ.get("AGY_BRAIN_DIR") or Path.home() / ".gemini" / "antigravity-cli" / "brain")


def collect_native(job, state, min_chars, request):
    kind, spec = state["spec"]["kind"], state["spec"]
    # A fresh attempt must establish a fresh result. Preserve previous files for
    # inspection, but errors/identity changes/incomplete streams cannot keep them current.
    update(job, state, "collection_started", result_current=False)
    idle_observed = False
    if not state.get("pane_closed") and state.get("agent_name"):
        try:
            info = observe(job, state)
        except HerdrError as exc:
            if exc.code != "agent_not_found":
                raise
        else:
            if info["agent_status"] not in ("idle", "done"):
                raise ValueError("Agent is still working/blocked; collect after it is idle or done")
            idle_observed = True
    marker = request or Path(state.get("last_request") or "").name
    if not marker:
        if any(job.glob("request-*.md")):
            raise ValueError("Older job has no round marker; pass --request explicitly")
        marker = "request.md"
    marker = Path(marker).name
    if request and state.get("last_request") and marker != Path(state["last_request"]).name:
        raise ValueError("--request must identify the current round; refusing an older result")
    path = native_records.resolve_transcript(kind, state.get("native_session"), spec["workspace"],
                                             spec.get("env"), spec.get("cli_args"))
    receipt = native_records.collect_round(kind, path, spec["workspace"], state["id"], marker, min_chars)
    if receipt and receipt.get("finality", "").startswith("idle-") and not idle_observed:
        receipt = None  # a missing stop marker needs actual idle, not an exited/interrupted CLI
    if not receipt:
        update(job, state, "collect_empty", result_current=False, collected_request=marker)
        return {**summary(job, state), "collect": "none"}
    text = receipt.pop("text")
    receipt.update(kind=kind, transcript=str(path), request=marker, collected_at=now())
    write(job / "result.md", text + "\n")
    save(job / "result-receipt.json", receipt)
    update(job, state, "collected", status="returned", result_current=True, collected_from=str(path),
           collected_request=marker, collected_message_id=receipt.get("message_id"),
           cli_model_observed=receipt.get("model_observed"), cli_effort_observed=receipt.get("effort_observed"))
    return {**summary(job, state), "collect": "collected", "receipt": str(job / "result-receipt.json")}


def collect(job, min_chars=None, request=None):
    """An Antigravity (agy) answer from the CLI's own session transcript, for when the pane shows
    no result (plan mode, a stalled prompt, a cut-off screen): the last PLANNER_RESPONSE with text
    after the USER_INPUT that carried this job's latest request, before any later input. Writes
    result.md as the current result; plan artifacts of the session are listed, never used."""
    job = Path(job).resolve()
    if min_chars is not None and (not isinstance(min_chars, int) or min_chars < 1):
        raise ValueError("min-chars must be a positive integer")
    with locked(job):
        state = load(job / "job.json")
        checked = check_review_inputs(job, state)
        if checked["status"] not in ("match", "not_recorded"):
            return {**summary(job, state), "collect": "stale" if checked["status"] == "changed" else "unavailable"}
        if state["spec"].get("mode") == "herdr" and state["spec"].get("kind") in ("omp", "claude", "codex"):
            return collect_native(job, state, 1 if min_chars is None else min_chars, request)
        min_chars = 200 if min_chars is None else min_chars
        if state["spec"].get("kind") != "agy":
            raise ValueError("collect supports agy, omp, claude, and codex native transcripts only")
        update(job, state, "collection_started", result_current=False)
        if not state.get("pane_closed") and state.get("agent_name"):
            try:
                if observe(job, state)["agent_status"] not in ("idle", "done"):
                    raise ValueError("Agent is still working/blocked; collect after it is idle or done")
            except HerdrError as exc:
                if exc.code != "agent_not_found":
                    raise
        session = (state.get("native_session") or {}).get("value")
        if not session:
            raise ValueError("The job has no native agy session id")
        if not isinstance(session, str) or not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', session):
            raise ValueError("Invalid native agy session id")
        folder = Path(state['spec'].get('env', {}).get('AGY_BRAIN_DIR') or AGY_BRAIN) / session
        transcript = folder / ".system_generated" / "logs" / "transcript_full.jsonl"
        if not transcript.is_file():
            raise ValueError("No transcript at " + str(transcript))
        entries = native_records._read(transcript)
        if request:
            marker = Path(request).name
            if state.get("last_request") and marker != Path(state["last_request"]).name:
                raise ValueError("--request must identify the current round; refusing an older result")
        elif state.get("last_request"):
            marker = Path(state["last_request"]).name
        elif not list(job.glob("request-*.md")):
            marker = "request.md"  # a job from before last_request was recorded, with one round only
        else:
            raise ValueError("This job predates last_request and has follow-ups: pass --request <request file> "
                             "to name the round to collect")
        asked = [i for i, e in enumerate(entries) if e.get("type") == "USER_INPUT"
                 and marker in str(e.get("content", "")) and state["id"] in str(e.get("content", ""))]
        if not asked:
            raise ValueError("The transcript has no input carrying " + marker)
        start = asked[-1]
        end = next((i for i in range(start + 1, len(entries)) if entries[i].get("type") == "USER_INPUT"), len(entries))
        planners = [e for e in entries[start + 1:end] if e.get("type") == "PLANNER_RESPONSE"]
        final = planners[-1] if planners else None
        pending_tail = final and any(e.get("status") in ("RUNNING", "PENDING", "ERROR")
                                    or any(e.get(k) for k in ("error", "isError", "refusal"))
                                    for e in entries[entries.index(final) + 1:end])
        answers = [final] if (final and not pending_tail and final.get("status") == "DONE" and not final.get("tool_calls")
                   and not any(final.get(k) for k in ("error", "isError", "refusal"))
                   and isinstance(final.get("content"), str) and len(final["content"].strip()) >= min_chars) else []
        artifacts = sorted(str(p) for p in folder.glob("*.md"))
        if not answers:
            update(job, state, "collect_empty", artifacts_seen=artifacts)
            return {**summary(job, state), "collect": "none", "artifacts": artifacts}
        write(job / "result.md", answers[-1]["content"].strip() + "\n")
        receipt = {"kind": "agy", "native_session_id": session, "message_id": final.get("step_index"),
                   "request": marker, "transcript": str(transcript), "finality": "planner_done",
                   "model_observed": final.get("model"), "effort_observed": final.get("effort"),
                   "collected_at": now()}
        save(job / "result-receipt.json", receipt)
        update(job, state, "collected", status="returned", result_current=True, collected_from=str(transcript),
               collected_step=answers[-1].get("step_index"), cli_model_observed=receipt["model_observed"],
               cli_effort_observed=receipt["effort_observed"])
        return {**summary(job, state), "collect": "collected", "artifacts": artifacts}


def jobs_of_task(workspace, task_key, problems=None):
    roots = {Path(workspace).resolve(), Path(layout.project_root(workspace))}
    found = []
    paths = sorted({p for root in roots for p in (root / ".agent-runs").glob("*/job.json")})
    for job, state in read_run_jobs(paths, problems):
        archive = state.get('codex_archive')
        archive_pending = isinstance(archive, dict) and archive.get('status') in ('failed', 'pending')
        if state["spec"].get("mode") == "herdr" and state.get("task_key") == task_key and (not state.get("pane_closed") or archive_pending):
            found.append(job)
    return found


def manage(args):
    job = Path(args.job).resolve()
    if args.command == "status" and not args.refresh:
        return summary(job, load(job / "job.json"))
    with locked(job):
        state = load(job / "job.json")
        cmd = args.command
        if (cmd == "status" and args.refresh) or cmd in ("read", "followup", "cancel"):
            if state["spec"]["mode"] != "herdr":
                raise ValueError("This live operation requires a Herdr job")
            info = observe(job, state)
        if cmd == "read":
            content = herdr(state["spec"], "agent", "read", state["agent_name"],
                            "--source", getattr(args, "source", "visible"), "--lines", str(args.lines), raw=True)
            dest = job / ("snapshot-" + uuid.uuid4().hex[:8] + ".txt")
            write(dest, content)
            update(job, state, "snapshot", last_snapshot=str(dest))
        elif cmd == "followup":
            send_followup(job, state, info, Path(args.prompt_file).read_text(encoding="utf-8-sig"),
                          getattr(args, "review_input", None), getattr(args, "checkpoint_id", None))
        elif cmd == "cancel":
            herdr(state["spec"], "agent", "send-keys", state["agent_name"], "ctrl+c")
            update(job, state, "cancel_requested", status="cancel_requested")
        elif cmd == "attach":
            source = Path(args.file).resolve()
            if not source.is_file():
                raise ValueError("Artifact must be an existing file")
            folder = job / "artifacts"
            folder.mkdir(exist_ok=True)
            dest = folder / (uuid.uuid4().hex[:8] + "-" + source.name)
            shutil.copy2(source, dest)
            artifacts = list(state.get("artifacts", []))
            artifacts.append({"label": args.label, "path": str(dest), "source": str(source)})
            update(job, state, "artifact", artifacts=artifacts)
        elif cmd == "finish":
            checked = check_review_inputs(job, state)
            if args.outcome == "completed" and checked["status"] not in ("match", "not_recorded"):
                raise ValueError("Review inputs changed or are unavailable; preserve the result and record needs_work, or review the new version")
            if not args.result_file and not state.get("result_current"):
                raise ValueError("This round needs a newly collected --result-file; an older result cannot be reused")
            review = Path(args.review_file).read_text(encoding="utf-8-sig").strip()
            if not review:
                raise ValueError("Review must contain actual validation evidence")
            result = Path(args.result_file) if args.result_file else job / "result.md"
            content = result.read_text(encoding="utf-8-sig").strip()
            if not content:
                raise ValueError("Result is empty")
            write(job / "result.md", content + "\n")
            review_path = job / ("review-" + uuid.uuid4().hex[:8] + ".md")
            write(review_path, review + "\n")
            update(job, state, "reviewed", status=args.outcome, review=str(review_path),
                   result_current=checked["status"] in ("match", "not_recorded"),
                   git_after=git_state(state["spec"]["workspace"]))
        result = summary(job, state)
        if state.get("last_snapshot"):
            result["snapshot"] = state["last_snapshot"]
        return result


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("run")
    start.add_argument("--spec", required=True)
    start.add_argument("--prompt-file", required=True)
    start.add_argument("--dry-run", action="store_true")
    start.add_argument("--task-id", help="overrides the spec's task_id (one shared spec, many tasks)")
    start.add_argument("--task-name", help="overrides the spec's task_name")
    start.add_argument("--review-input", action="append", help="bind an explicit review file; repeatable")
    start.add_argument("--checkpoint-id")
    listing = sub.add_parser("list")
    listing.add_argument("--workspace", required=True)
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--task-key", help="only this task's jobs")
    listing.add_argument("--open", action="store_true", help="only Herdr jobs whose pane was not closed")
    gathering = sub.add_parser("collect")
    gathering.add_argument("--job", required=True)
    gathering.add_argument("--min-chars", type=int, help="default: 200 for agy; 1 for omp/claude")
    gathering.add_argument("--request", help="the round's request file (older jobs without last_request)")
    recovery = sub.add_parser("recover", help="preview or explicitly associate a restored OMP/Claude native session")
    recovery.add_argument("--job", required=True)
    recovery.add_argument("--pane", help="must match the uniquely verified native conversation")
    recovery.add_argument("--apply", action="store_true", help="write verified identity bindings; never resubmit a task")
    recovery.add_argument("--runtime-note", help="record inspected runtime policy if restore lost launch flags/environment")
    closing = sub.add_parser("close")
    closing.add_argument("--job", action="append", help="repeatable")
    closing.add_argument("--task-key", help="every open job of this task (needs --workspace)")
    closing.add_argument("--workspace")
    closing.add_argument("--force", action="store_true", help="also close a working/blocked agent")
    pruning = sub.add_parser("prune")
    pruning.add_argument("--workspace", required=True)
    pruning.add_argument("--session")
    for name in ("doctor", "show"):
        item = sub.add_parser(name)
        target = item.add_mutually_exclusive_group()
        target.add_argument("--session")
        target.add_argument("--job")
    for command in ("status", "read", "followup", "cancel", "attach", "finish"):
        item = sub.add_parser(command)
        item.add_argument("--job", required=True)
        if command == "status":
            item.add_argument("--refresh", action="store_true")
        if command == "read":
            item.add_argument("--lines", type=int, default=120)
            item.add_argument("--source", choices=("visible", "recent", "recent-unwrapped"), default="visible")
        if command == "followup":
            item.add_argument("--prompt-file", required=True)
            item.add_argument("--review-input", action="append", help="replace this round's review input list")
            item.add_argument("--checkpoint-id")
        if command == "attach":
            item.add_argument("--file", required=True)
            item.add_argument("--label", required=True)
        if command == "finish":
            item.add_argument("--result-file")
            item.add_argument("--review-file", required=True)
            item.add_argument("--outcome", choices=("completed", "needs_work"), required=True)
            item.add_argument("--close", action="store_true", help="then release the job's pane (close)")
            item.add_argument("--related-job", action="append", default=[], help="also check this explicit related job; never scan history")
    args = parser.parse_args()
    try:
        if (os.environ.get(ACTIVE) == "1" or os.environ.get("EXTERNAL_MODEL_CONSULT_ACTIVE") == "1") and args.command not in ("list", "status"):
            raise ValueError("External workers cannot dispatch or control external jobs recursively")
        if args.command == "run":
            spec = load(args.spec)
            spec.update({k: v for k, v in (("task_id", args.task_id), ("task_name", args.task_name)) if v is not None})
            if args.review_input is not None:
                spec["review_inputs"] = args.review_input
            if args.checkpoint_id is not None:
                spec["checkpoint_id"] = args.checkpoint_id
            result = run(spec, Path(args.prompt_file).read_text(encoding="utf-8-sig"), args.dry_run)
        elif args.command == "list":
            roots = {Path(args.workspace).resolve(), Path(layout.project_root(args.workspace))}
            paths = sorted({p for root in roots for p in (root / ".agent-runs").glob("*/job.json")},
                           key=lambda p: p.parent.name, reverse=True)
            if not (args.task_key or args.open):  # unchanged: the newest `limit` records as they are
                states = [(p.parent, load(p)) for p in paths[:max(0, args.limit)]]
                result = [summary(j, s) for j, s in states]
            else:  # filtered run.py jobs skip DSH and unreadable records
                states = []
                for job, s in read_run_jobs(paths):
                    if len(states) >= max(0, args.limit):
                        break
                    if args.task_key and s.get("task_key") != args.task_key:
                        continue
                    if args.open and not (s["spec"].get("mode") == "herdr" and s.get("terminal_id") and not s.get("pane_closed")):
                        continue  # panes still hanging: Herdr jobs that got a pane and were never closed
                    states.append((job, s))
                result = [{**summary(j, s), "updated_at": s.get("updated_at")} for j, s in states]
            if args.open:  # one live look: a pane closed by hand still counts as open in the records
                live = {}  # per Herdr session: its terminals, or None when it cannot be asked
                for item, (j, s) in zip(result, states):
                    session = s["spec"].get("session")
                    if session not in live:
                        try:
                            live[session] = {p["terminal_id"] for p in herdr({"session": session}, "pane", "list")["panes"]}
                        except Exception:
                            live[session] = None
                    item["pane_live"] = None if live[session] is None else s.get("terminal_id") in live[session]
        elif args.command == "collect":
            result = collect(args.job, args.min_chars, args.request)
        elif args.command == "recover":
            result = recover(args.job, args.apply, args.pane, args.runtime_note)
        elif args.command == "close":
            if bool(args.job) == bool(args.task_key) or (args.task_key and not args.workspace):
                raise ValueError("Give --job (repeatable) or --task-key with --workspace")
            result = []
            targets = args.job or jobs_of_task(args.workspace, args.task_key, problems=result)
            for job in targets:
                try:
                    result.append(close(job, args.force))
                except Exception as exc:  # one refusal or timeout does not stop the others; Ctrl+C still does
                    result.append({"job": str(job), "close": "refused", "error": str(exc)})
        elif args.command == "prune":
            spec = {"session": args.session} if args.session else {}
            result = layout.prune(layout.project_root(args.workspace), spec, herdr)
        elif args.command in ("doctor", "show"):
            if args.job:
                stored = load(Path(args.job) / "job.json")["spec"]
                if stored["mode"] != "herdr":
                    raise ValueError("This command requires a Herdr job")
                session = stored.get("session")
            else:
                session = args.session
            if args.command == "show":
                result = windows_support.show(executable("herdr"), session)
            else:
                result = windows_support.diagnose(session)
                try:
                    result["server_status"] = herdr({"session": session}, "status", "server", raw=True).strip()
                    result["status"] = "checked"
                except Exception as exc:
                    result.update(status="attention", connection_error=str(exc))
        else:
            result = manage(args)
            if args.command == "finish" and args.close:  # accepted: release the pane in the same step
                if load(Path(args.job) / "job.json")["spec"]["mode"] != "herdr":
                    result["close"] = "no_pane"  # direct and exec jobs never hold a pane
                else:
                    try:
                        released = close(args.job)
                        result.update(close=released['close'], codex_archive=released.get('codex_archive'))
                    except Exception as exc:
                        result.update(close="refused", close_error=str(exc))
            if args.command == "finish":
                job = Path(args.job).resolve()
                with locked(job):
                    report = completion_checks.finish_report(job, args.related_job, expect_closed=args.close)
                    save(job / "finish-check.json", report)
                result.update(finish_check=report["status"], finish_check_file=str(job / "finish-check.json"),
                              checked_jobs=report["checked_jobs"])
        print(json.dumps(redact_value(result), ensure_ascii=False))
        if args.command == "close":  # the close outcome, not the job's own history, decides
            return 2 if any(r.get("close") != "closed" or (r.get('codex_archive') or {}).get('status') in ('failed', 'pending')
                            for r in result) else 0
        if args.command == "finish" and args.close and result.get("close") == "refused":
            return 2
        if args.command == "finish" and result.get("finish_check") == "attention":
            return 2  # acceptance stays recorded; related-job warnings do not trigger retries or cleanup
        if args.command == "collect" and result.get("collect") != "collected":
            return 2
        return 2 if isinstance(result, dict) and result.get("status") in ("attention", "interrupted") else 0
    except (Exception, KeyboardInterrupt) as exc:
        print(json.dumps(redact_value({"status": "attention", "error": type(exc).__name__ + ": " + str(exc)}), ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
