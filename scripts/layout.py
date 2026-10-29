"""Project -> workspace, task -> tab, invocation -> pane. No model calls."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time


def key(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def project_root(workspace, explicit=None):
    if explicit:
        root = Path(explicit).expanduser()
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("project_root must be an existing absolute directory")
        return str(root.resolve())
    if shutil.which("git"):
        proc = subprocess.run(["git", "-C", workspace, "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, encoding="utf-8", timeout=10)
        if proc.returncode == 0:
            return str(Path(proc.stdout.strip()).resolve())
    return str(Path(workspace).resolve())


def task_identity(spec, job_id, root):
    """The owning task of a job in any mode: a thread identifies the owning Codex task; task_id can
    name a subgroup; without either the job is its own task."""
    owner = spec.get("parent_thread_id") or spec.get("task_id") or job_id
    group = owner + (":" + spec["task_id"] if spec.get("parent_thread_id") and spec.get("task_id") else "")
    return {"project_name": spec.get("project_name") or Path(root).name or root,
            "parent_thread_id": spec.get("parent_thread_id"), "task_key": key(group), "task_group": group,
            "task_name": spec.get("task_name") or ("Codex " + owner[:8] if spec.get("parent_thread_id") else "外部任务 " + owner[-8:])}


def identity(spec, job_id):
    root = spec["project_root"]
    project_key = key(os.path.normcase(root))
    task = task_identity(spec, job_id, root)
    project_name, task_key, task_name = task["project_name"], task["task_key"], task["task_name"]
    label = spec.get("agent_label") or " · ".join(filter(None, (spec["kind"], spec["model"], spec.get("role"))))
    return {"project_root": root, "project_key": project_key, **task,
            "workspace_label": f"{project_name} [p-{project_key}]",
            "tab_label": f"{task_name} [t-{task_key}]",
            "pane_label": f"{label} [{job_id[-6:]}]"}


@contextmanager
def layout_lock(root, timeout=15):
    path = root / ".layout.lock"
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise ValueError("Project layout is locked; inspect .layout.lock before retrying") from None
            time.sleep(0.05)
    try:
        with os.fdopen(fd, "w") as out:
            out.write(str(os.getpid()))
        yield
    finally:
        path.unlink(missing_ok=True)


def save(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def only_match(items, marker):
    matches = [x for x in items if (x.get("label") or "").endswith(marker)]
    if len(matches) > 1:
        raise ValueError("Multiple Herdr layouts have the same identity marker; resolve before dispatch")
    return matches[0] if matches else None


def server_key(spec):
    # Scope cached runtime IDs to the Herdr config location and named session.
    config = os.environ.get("HERDR_CONFIG_PATH") or str(Path(os.environ.get("APPDATA", str(Path.home() / ".config"))) / "herdr" / "config.toml")
    return key(os.path.normcase(str(Path(config).resolve())) + "|" + (spec.get("session") or "default"))


def forget_locked(project, spec, terminal_ids, drop_empty=True):
    """Drop closed terminals from the registry; a tab left without terminals is dropped too unless
    drop_empty is false (the live tab still holds panes). The caller holds layout_lock."""
    path = Path(project) / ".agent-runs" / "herdr-layout.json"
    if not path.exists():
        return []
    gone = set(terminal_ids)
    removed = []
    cache = json.loads(path.read_text(encoding="utf-8"))
    tabs = cache.get(server_key(spec), {}).get("tabs", {})
    for task_key, tab in list(tabs.items()):
        if not gone & set(tab.get("terminals", [])):
            continue
        tab["terminals"] = [t for t in tab["terminals"] if t not in gone]
        if not tab["terminals"] and drop_empty:
            removed.append(tabs.pop(task_key).get("label"))
    save(path, cache)
    return removed


def rebind_locked(project, spec, task_key, old_terminal, location):
    """Replace one verified runtime binding; caller holds the project layout lock."""
    path = Path(project) / ".agent-runs" / "herdr-layout.json"
    cache = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    binding = cache.setdefault(server_key(spec), {"workspace_id": location["workspace_id"], "tabs": {}})
    tabs = binding.setdefault("tabs", {})
    if binding.get("workspace_id") != location["workspace_id"]:
        if any(key_ != task_key for key_ in tabs):
            raise ValueError("Layout workspace changed; prune stale mappings before recovery")
        binding["workspace_id"] = location["workspace_id"]
    tab = tabs.get(task_key, {})
    others = [terminal for terminal in tab.get("terminals", []) if terminal != old_terminal]
    if tab.get("tab_id") not in (None, location["tab_id"]) and others:
        raise ValueError("Task tab changed while other terminals remain; prune/inspect its mapping")
    tabs[task_key] = {**tab, "tab_id": location["tab_id"],
                      "terminals": list(dict.fromkeys([*others, location["terminal_id"]]))}
    save(path, cache)


def prune(project, spec, call):
    """Drop registry entries whose workspace, tab or terminal no longer exists in the running server."""
    root = Path(project) / ".agent-runs"
    path = root / "herdr-layout.json"
    report = {"workspace_dropped": False, "tabs_dropped": [], "terminals_dropped": 0}
    if not path.exists():
        return report
    with layout_lock(root):
        cache = json.loads(path.read_text(encoding="utf-8"))
        binding = cache.get(server_key(spec))
        if not binding:
            return report
        live_ws = {w["workspace_id"] for w in call(spec, "workspace", "list")["workspaces"]}
        if binding.get("workspace_id") not in live_ws:
            report["workspace_dropped"] = True
            report["tabs_dropped"] = [t.get("label") for t in binding.get("tabs", {}).values()]
            cache.pop(server_key(spec))
        else:
            wid = binding["workspace_id"]
            live_tabs = {t["tab_id"] for t in call(spec, "tab", "list", "--workspace", wid)["tabs"]}
            live_terms = {p["terminal_id"] for p in call(spec, "pane", "list", "--workspace", wid)["panes"]}
            tabs = binding.get("tabs", {})
            for task_key, tab in list(tabs.items()):
                kept = [t for t in tab.get("terminals", []) if t in live_terms]
                report["terminals_dropped"] += len(tab.get("terminals", [])) - len(kept)
                tab["terminals"] = kept
                if tab.get("tab_id") not in live_tabs or not kept:
                    report["tabs_dropped"].append(tabs.pop(task_key).get("label"))
        save(path, cache)
    return report


def allocate(spec, job_id, call):
    """Find or create the task tab and split a new pane for the job. If a pane was created and a
    later step fails, the exception carries `partial` (its workspace/tab/pane/terminal IDs) so the
    job can record and later close it."""
    made = {}
    try:
        return _allocate(spec, job_id, call, made)
    except (Exception, KeyboardInterrupt) as exc:
        if made:
            exc.partial = dict(made)
        raise


def _made(made, ws, tab, pane):
    made.update(workspace_id=ws["workspace_id"], tab_id=tab["tab_id"], pane_id=pane["pane_id"],
                terminal_id=pane["terminal_id"])


def _allocate(spec, job_id, call, made):
    result = identity(spec, job_id)
    root = Path(result["project_root"]) / ".agent-runs"
    root.mkdir(exist_ok=True)
    server_key_ = server_key(spec)
    path = root / "herdr-layout.json"
    focus = "--focus" if spec.get("visible") else "--no-focus"
    env = {**spec.get("env", {}), "HERDR_EXTERNAL_MODELS_ACTIVE": "1"}
    environment = [part for name, value in env.items() for part in ("--env", name + "=" + value)]
    with layout_lock(root):
        cache = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        binding = cache.get(server_key_, {})
        workspaces = call(spec, "workspace", "list")["workspaces"]
        ws = next((x for x in workspaces if x["workspace_id"] == binding.get("workspace_id")), None)
        panes = call(spec, "pane", "list", "--workspace", ws["workspace_id"])["panes"] if ws else []
        anchors = {t for tab in binding.get("tabs", {}).values() for t in tab.get("terminals", [])}
        if ws and not ((ws.get("label") or "").endswith("[p-" + result["project_key"] + "]")
                       or any(p["terminal_id"] in anchors for p in panes)):
            raise ValueError("Cached workspace identity no longer matches; inspect the layout binding")
        if not ws:
            ws = only_match(workspaces, "[p-" + result["project_key"] + "]")
            binding = {"workspace_id": ws["workspace_id"], "tabs": {}} if ws else {}
            panes = call(spec, "pane", "list", "--workspace", ws["workspace_id"])["panes"] if ws else []
        created_pane = None
        if not ws:
            created = call(spec, "workspace", "create", "--cwd", spec["workspace"],
                           "--label", result["workspace_label"], *environment, focus)
            ws, tab, created_pane = created["workspace"], created["tab"], created["root_pane"]
            _made(made, ws, tab, created_pane)
            binding = {"workspace_id": ws["workspace_id"], "tabs": {}}
            # Persist IDs before the next mutation so partial failure can be reconciled.
            cache[server_key_] = binding
            save(path, cache)
            call(spec, "tab", "rename", tab["tab_id"], result["tab_label"])
        else:
            tabs = call(spec, "tab", "list", "--workspace", ws["workspace_id"])["tabs"]
            saved_tab = binding.get("tabs", {}).get(result["task_key"], {})
            tab = next((t for t in tabs if t["tab_id"] == saved_tab.get("tab_id")), None)
            if tab and not ((tab.get("label") or "").endswith("[t-" + result["task_key"] + "]")
                            or any(p["terminal_id"] in saved_tab.get("terminals", []) and p["tab_id"] == tab["tab_id"] for p in panes)):
                raise ValueError("Cached tab identity no longer matches; inspect before dispatch")
            if not tab:
                tab = only_match(tabs, "[t-" + result["task_key"] + "]")
            if not tab:
                created = call(spec, "tab", "create", "--workspace", ws["workspace_id"], "--cwd", spec["workspace"],
                               "--label", result["tab_label"], *environment, focus)
                tab, created_pane = created["tab"], created["root_pane"]
                _made(made, ws, tab, created_pane)
        tab_binding = binding.setdefault("tabs", {}).setdefault(result["task_key"], {})
        if tab_binding.get("tab_id") != tab["tab_id"]:
            tab_binding.clear()
        tab_binding.update(tab_id=tab["tab_id"], label=tab.get("label") or result["tab_label"])
        cache[server_key_] = binding
        save(path, cache)
        if created_pane is None:
            candidates = [p for p in panes if p["tab_id"] == tab["tab_id"]]
            if not candidates:
                raise ValueError("Task tab has no pane to split; refresh its layout before dispatch")
            base = candidates[-1]
            direction = spec.get("split_direction") or ("right" if len(candidates) % 2 else "down")
            created_pane = call(spec, "pane", "split", base["pane_id"], "--direction", direction,
                                "--cwd", spec["workspace"], *environment, focus)["pane"]
            _made(made, ws, tab, created_pane)
        # Never reuse an occupied or apparently idle agent's pane for a new job.
        terminals = tab_binding.setdefault("terminals", [])
        terminals.append(created_pane["terminal_id"])
        save(path, cache)
        call(spec, "pane", "rename", created_pane["pane_id"], result["pane_label"])
        result.update(workspace_id=ws["workspace_id"], tab_id=tab["tab_id"],
                      pane_id=created_pane["pane_id"], terminal_id=created_pane["terminal_id"],
                      layout_registry=str(path))
        return result
