"""Behavioral topology tests; no CLI/model/network required."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import tempfile
import unittest

import layout


class FakeHerdr:
    def __init__(self):
        self.workspaces, self.tabs, self.panes, self.calls = [], [], [], []
        self.created_panes = 0

    def new_tab(self, wid, label, cwd):
        tid = wid + ":t" + str(sum(t["workspace_id"] == wid for t in self.tabs) + 1)
        tab = {"workspace_id": wid, "tab_id": tid, "label": label}
        self.tabs.append(tab)
        return tab, self.new_pane(wid, tid, cwd)

    def new_pane(self, wid, tid, cwd):
        self.created_panes += 1
        pane = {"workspace_id": wid, "tab_id": tid, "pane_id": wid + ":p" + str(self.created_panes),
                "terminal_id": "term-" + str(self.created_panes), "cwd": cwd}
        self.panes.append(pane)
        return pane

    def __call__(self, spec, *args, raw=False):
        self.calls.append(args)
        def opt(name, default=None):
            return args[args.index(name) + 1] if name in args else default
        action = args[:2]
        if action == ("workspace", "list"):
            result = {"workspaces": self.workspaces}
        elif action == ("workspace", "create"):
            ws = {"workspace_id": "w" + str(len(self.workspaces) + 1), "label": opt("--label")}
            self.workspaces.append(ws)
            tab, pane = self.new_tab(ws["workspace_id"], "1", opt("--cwd"))
            result = {"workspace": ws, "tab": tab, "root_pane": pane}
        elif action == ("tab", "list"):
            result = {"tabs": [t for t in self.tabs if t["workspace_id"] == opt("--workspace")]}
        elif action == ("tab", "create"):
            tab, pane = self.new_tab(opt("--workspace"), opt("--label"), opt("--cwd"))
            result = {"tab": tab, "root_pane": pane}
        elif action == ("tab", "rename"):
            next(t for t in self.tabs if t["tab_id"] == args[2])["label"] = args[3]
            result = {"type": "ok"}
        elif action == ("pane", "list"):
            result = {"panes": [p for p in self.panes if opt("--workspace") in (None, p["workspace_id"])]}
        elif action == ("pane", "close"):
            self.panes = [p for p in self.panes if p["pane_id"] != args[2]]
            result = {"type": "ok"}
        elif action == ("tab", "close"):
            self.tabs = [t for t in self.tabs if t["tab_id"] != args[2]]
            self.panes = [p for p in self.panes if p["tab_id"] != args[2]]
            result = {"type": "ok"}
        elif action == ("pane", "split"):
            parent = next(p for p in self.panes if p["pane_id"] == args[2])
            result = {"pane": self.new_pane(parent["workspace_id"], parent["tab_id"], opt("--cwd"))}
        elif action == ("pane", "rename"):
            next(p for p in self.panes if p["pane_id"] == args[2])["label"] = args[3]
            result = {"type": "ok"}
        else:
            raise AssertionError(args)
        return copy.deepcopy(result)


class Layout(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.spec = {"project_root": str(self.root), "workspace": str(self.root), "project_name": "项目",
                     "parent_thread_id": "thread-a", "task_name": "修复", "kind": "agy", "model": "gemini",
                     "env": {"HTTPS_PROXY": "http://127.0.0.1:7897"}}
        self.fake = FakeHerdr()

    def tearDown(self):
        self.temp.cleanup()

    def allocate(self, number, **fields):
        return layout.allocate({**self.spec, **fields}, "job-" + str(number), self.fake)

    def test_same_task_different_cli_reuses_workspace_and_tab_only(self):
        first = self.allocate(1)
        second = self.allocate(2, kind="codex", model="sol")
        self.assertEqual(first["workspace_id"], second["workspace_id"])
        self.assertEqual(first["tab_id"], second["tab_id"])
        self.assertNotEqual(first["pane_id"], second["pane_id"])
        self.assertEqual(len(self.fake.workspaces), 1)
        self.assertEqual(len(self.fake.tabs), 1)
        self.assertEqual(len(self.fake.panes), 2)
        self.assertIn("agy · gemini", first["pane_label"])
        split = next(c for c in self.fake.calls if c[:2] == ("pane", "split"))
        self.assertIn("HTTPS_PROXY=http://127.0.0.1:7897", split)

    def test_new_thread_creates_tab_in_same_project(self):
        first = self.allocate(1)
        second = self.allocate(2, parent_thread_id="thread-b", task_name="复核")
        self.assertEqual(first["workspace_id"], second["workspace_id"])
        self.assertNotEqual(first["tab_id"], second["tab_id"])

    def test_same_name_other_checkout_is_a_separate_workspace(self):
        first = self.allocate(1)
        other = self.root / "other-checkout"
        other.mkdir()
        second = self.allocate(2, workspace=str(other), project_root=str(other))
        self.assertNotEqual(first["workspace_id"], second["workspace_id"])
        self.assertNotEqual(first["project_key"], second["project_key"])

    def test_subdirectories_use_explicit_common_project_root(self):
        subdir = self.root / "src"
        subdir.mkdir()
        first = self.allocate(1)
        second = self.allocate(2, workspace=str(subdir))
        self.assertEqual(first["tab_id"], second["tab_id"])
        self.assertEqual(self.fake.panes[-1]["cwd"], str(subdir))

    def test_parallel_creations_do_not_duplicate_workspace_or_tab(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(self.allocate, range(4)))
        self.assertEqual(len({x["workspace_id"] for x in results}), 1)
        self.assertEqual(len({x["tab_id"] for x in results}), 1)
        self.assertEqual(len({x["pane_id"] for x in results}), 4)

    def test_user_renamed_live_layout_keeps_identity(self):
        first = self.allocate(1)
        self.fake.workspaces[0]["label"] = "手动项目名"
        self.fake.tabs[0]["label"] = "手动任务名"
        second = self.allocate(2)
        self.assertEqual(first["tab_id"], second["tab_id"])
        self.assertEqual(self.fake.workspaces[0]["label"], "手动项目名")

    def test_missing_tab_is_recreated_but_existing_workspace_is_preserved(self):
        first = self.allocate(1)
        self.fake.tabs.clear()
        self.fake.panes.clear()
        second = self.allocate(2)
        self.assertEqual(first["workspace_id"], second["workspace_id"])
        self.assertNotEqual(first["terminal_id"], second["terminal_id"])

    def test_registry_loss_recovers_tagged_layout_without_duplicates(self):
        first = self.allocate(1)
        (self.root / ".agent-runs" / "herdr-layout.json").unlink()
        second = self.allocate(2)
        self.assertEqual(first["tab_id"], second["tab_id"])
        self.assertEqual(len(self.fake.panes), 2)

    def test_reused_workspace_id_cannot_hijack_unrelated_layout(self):
        self.allocate(1)
        self.fake.workspaces[0]["label"] = "unrelated workspace"
        self.fake.panes[0]["terminal_id"] = "unrelated terminal"
        with self.assertRaisesRegex(ValueError, "identity"):
            self.allocate(2)
        self.assertEqual(len(self.fake.panes), 1)

    def test_no_thread_or_group_id_does_not_merge_unrelated_tasks(self):
        first = self.allocate(1, parent_thread_id=None)
        second = self.allocate(2, parent_thread_id=None)
        self.assertNotEqual(first["tab_id"], second["tab_id"])

    def test_session_bindings_are_isolated(self):
        servers = {"a": FakeHerdr(), "b": FakeHerdr()}
        def call(spec, *args):
            return servers[spec["session"]](spec, *args)
        for session in ("a", "b", "a"):
            layout.allocate({**self.spec, "session": session}, "job-" + session, call)
        self.assertEqual(len(servers["a"].workspaces), 1)
        self.assertEqual(len(servers["b"].workspaces), 1)
        self.assertEqual(len(servers["a"].panes), 2)
        self.assertEqual(len(servers["b"].panes), 1)
        registry = json.loads((self.root / ".agent-runs" / "herdr-layout.json").read_text(encoding="utf-8"))
        self.assertEqual(len(registry), 2)


if __name__ == "__main__":
    unittest.main()
