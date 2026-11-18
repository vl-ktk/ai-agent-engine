"""Stage 6.6: adversarial file-tool confinement inside one task workspace."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_agent_engine import Action, Agent, ModelTurn, ScriptedModel
from ai_agent_engine.task_api import OwnerScope, TaskInbox, parse_submission
from ai_agent_engine.task_assignment import WorkspaceRoot
from ai_agent_engine.task_permissions import (
    ActionGrant, PermissionDenied, ProtectedAction, TaskPermissionGate,
)
from ai_agent_engine.task_state import TaskState
from ai_agent_engine.task_storage import SQLiteTaskStore
from ai_agent_engine.task_workspace_files import TaskWorkspaceFiles
from ai_agent_engine.tools import SandboxFiles, ToolError


OWNER = OwnerScope("owner-a", frozenset({"project-a"}))
FOREIGN = OwnerScope("owner-b", frozenset({"project-a"}))
READ = ProtectedAction.EXECUTE_TOOL
WRITE = ProtectedAction.WRITE_WORKSPACE


class SandboxFileBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-66-sandbox-")
        self.base = Path(self.temp.name)
        self.root = self.base / "root"
        self.root.mkdir()
        self.outside = self.base / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("private", encoding="utf-8")
        self.files = SandboxFiles(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_rejects_absolute_traversal_windows_url_and_nul_paths(self):
        paths = ("../outside/secret.txt", "nested/../../outside/secret.txt",
                 "/etc/passwd", "./local.txt", "nested//bad.txt",
                 "C:\\Windows\\win.ini", "file:///tmp/secret", "..\\outside",
                 "nested/./local.txt", "bad\x00.txt", "\ud800", "", ":oops")
        for path in paths:
            with self.subTest(path=path):
                with self.assertRaises(ToolError):
                    self.files.read_text({"path": path})
                with self.assertRaises(ToolError):
                    self.files.write_new({"path": path, "content": "attack"})
        self.assertEqual((self.outside / "secret.txt").read_text(), "private")

    def test_blocks_symlink_directories_and_leaf_symlinks(self):
        (self.root / "outdir").symlink_to(self.outside, target_is_directory=True)
        (self.root / "outlink.txt").symlink_to(self.outside / "secret.txt")
        (self.root / "dangling.txt").symlink_to(self.outside / "missing.txt")
        for path in ("outdir/secret.txt", "outlink.txt", "dangling.txt"):
            with self.subTest(path=path):
                with self.assertRaises(ToolError):
                    self.files.read_text({"path": path})
                with self.assertRaises(ToolError):
                    self.files.write_new({"path": path, "content": "attack"})
        self.assertEqual((self.outside / "secret.txt").read_text(), "private")
        self.assertFalse((self.outside / "missing.txt").exists())

    def test_rejects_hardlinks_and_special_files_for_read(self):
        os.link(self.outside / "secret.txt", self.root / "hardlink.txt")
        os.mkfifo(self.root / "named-pipe")
        for path in ("hardlink.txt", "named-pipe"):
            with self.subTest(path=path), self.assertRaises(ToolError):
                self.files.read_text({"path": path})

    def test_resists_nested_directory_swap_between_validation_and_open(self):
        (self.root / "nested").mkdir()
        original = self.files._target
        calls = 0

        def swap_after_second_validation(path):
            nonlocal calls
            resolved = original(path)
            calls += 1
            if calls == 2:
                (self.root / "nested").rename(self.root / "nested_saved")
                (self.root / "nested").symlink_to(self.outside, target_is_directory=True)
            return resolved

        with patch.object(self.files, "_target", side_effect=swap_after_second_validation):
            with self.assertRaises((ToolError, OSError)):
                self.files.write_new({"path": "nested/escaped.txt", "content": "attack"})
        self.assertEqual(calls, 2)
        self.assertFalse((self.outside / "escaped.txt").exists())

    def test_resists_root_swap_between_validation_and_open(self):
        (self.root / "allowed.txt").write_text("internal", encoding="utf-8")
        original = self.files._target

        def replace_root(path):
            resolved = original(path)
            self.root.rename(self.base / "root_saved")
            self.root.symlink_to(self.outside, target_is_directory=True)
            return resolved

        with patch.object(self.files, "_target", side_effect=replace_root):
            with self.assertRaises((ToolError, OSError)):
                self.files.read_text({"path": "allowed.txt"})
        self.assertEqual((self.outside / "secret.txt").read_text(), "private")

    def test_rejects_root_inode_replacement_with_another_regular_directory(self):
        (self.root / "allowed.txt").write_text("internal", encoding="utf-8")
        original = self.files._target

        def replace_root(path):
            resolved = original(path)
            self.root.rename(self.base / "root_saved")
            self.root.mkdir()
            (self.root / "allowed.txt").write_text("injected", encoding="utf-8")
            return resolved

        with patch.object(self.files, "_target", side_effect=replace_root):
            with self.assertRaises(ToolError):
                self.files.read_text({"path": "allowed.txt"})

    def test_rejects_initial_symlink_root_and_bool_size_limit(self):
        (self.base / "linked-root").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            SandboxFiles(self.base / "linked-root")
        with self.assertRaises(ValueError):
            SandboxFiles(self.root, max_bytes=True)

    def test_rejects_invalid_utf8_content_without_creating_file(self):
        with self.assertRaises(ToolError):
            self.files.write_new({"path": "bad.txt", "content": "\ud800"})
        self.assertFalse((self.root / "bad.txt").exists())


class TaskWorkspaceFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-66-tasks-")
        base = Path(self.temp.name)
        self.root = base / "workspaces"
        self.root.mkdir(mode=0o700)
        self.a = self.root / "slot-a"
        self.b = self.root / "slot-b"
        self.a.mkdir(mode=0o700)
        self.b.mkdir(mode=0o700)
        self.inbox = TaskInbox(store=SQLiteTaskStore(base / "tasks.sqlite3"),
                               workspace_root=WorkspaceRoot(self.root))
        self.task_id = self.inbox.accept(parse_submission(
            b'{"project_id":"project-a","title":"Sandbox task","prompt":"Safe file job"}'
        ), OWNER).task_id
        self.inbox.claim_task(self.task_id, "worker-a", "slot-a", OWNER)

    def tearDown(self):
        self.inbox.close()
        self.temp.cleanup()

    def gate(self, *actions):
        return TaskPermissionGate(self.inbox, grants=tuple(
            ActionGrant(self.task_id, "worker-a", action) for action in actions))

    def tools(self, gate):
        return TaskWorkspaceFiles(self.inbox, OWNER, task_id=self.task_id,
                                  worker_id="worker-a", gate=gate)

    def start(self):
        self.inbox.transition_state(self.task_id, TaskState.RUNNING,
                                    OWNER, actor_id="worker-a")

    def test_granted_working_folder_roundtrip_and_private_audit(self):
        gate = self.gate(READ, WRITE)
        tools = self.tools(gate)
        self.start()
        self.assertEqual(tools.write_new({"path": "notes.txt", "content": "hello"}),
                         "Created notes.txt (5 bytes)")
        self.assertEqual(tools.read_text({"path": "notes.txt"}), "hello")
        self.assertFalse((self.b / "notes.txt").exists())
        self.assertEqual((self.a / "notes.txt").read_text(), "hello")
        events = gate.audit_history(self.task_id, OWNER)
        self.assertEqual([e.action for e in events], [READ, WRITE, READ])
        self.assertEqual([e.allowed for e in events], [True, True, True])
        self.assertNotIn("hello", repr(events))

    def test_default_deny_and_missing_second_write_grant(self):
        no_grants = self.gate()
        tools = self.tools(no_grants)
        self.start()
        with self.assertRaises(PermissionDenied):
            tools.write_new({"path": "denied.txt", "content": "private"})
        self.assertFalse((self.a / "denied.txt").exists())
        self.assertEqual([e.allowed for e in no_grants.audit_history(self.task_id, OWNER)],
                         [False])
        execute_only = self.gate(READ)
        tools2 = self.tools(execute_only)
        with self.assertRaises(PermissionDenied):
            tools2.write_new({"path": "also-denied.txt", "content": "private"})
        self.assertFalse((self.a / "also-denied.txt").exists())
        self.assertEqual([(e.action, e.allowed) for e in
                          execute_only.audit_history(self.task_id, OWNER)],
                         [(READ, False), (READ, True), (WRITE, False)])

    def test_read_requires_execute_grant_even_for_existing_file(self):
        (self.a / "existing.txt").write_text("private", encoding="utf-8")
        gate = self.gate(WRITE)
        tools = self.tools(gate)
        self.start()
        with self.assertRaises(PermissionDenied):
            tools.read_text({"path": "existing.txt"})
        self.assertEqual([e.action for e in gate.audit_history(self.task_id, OWNER)],
                         [READ])

    def test_assignment_and_scope_cannot_be_supplied_by_model_arguments(self):
        gate = self.gate(READ, WRITE)
        with self.assertRaises(ToolError):
            TaskWorkspaceFiles(self.inbox, FOREIGN, task_id=self.task_id,
                               worker_id="worker-a", gate=gate)
        with self.assertRaises(ToolError):
            TaskWorkspaceFiles(self.inbox, OWNER, task_id=self.task_id,
                               worker_id="worker-b", gate=gate)
        tools = self.tools(gate)
        self.start()
        with self.assertRaises(ToolError):
            tools.write_new({"path": "fake.txt", "content": "x", "workspace_name": "slot-b"})
        with self.assertRaises(ToolError):
            tools.read_text({"path": "fake.txt", "owner_id": "owner-b"})
        self.assertFalse((self.a / "fake.txt").exists())

    def test_real_tools_cannot_read_or_write_sibling_workspace(self):
        (self.b / "other.txt").write_text("other user", encoding="utf-8")
        tools = self.tools(self.gate(READ, WRITE))
        self.start()
        for path in ("../slot-b/other.txt", "../slot-b/overwrite.txt",
                     str(self.b / "other.txt")):
            with self.subTest(path=path):
                with self.assertRaises(ToolError):
                    tools.read_text({"path": path})
                with self.assertRaises(ToolError):
                    tools.write_new({"path": path, "content": "attack"})
        self.assertEqual((self.b / "other.txt").read_text(), "other user")
        self.assertFalse((self.b / "overwrite.txt").exists())

    def test_non_running_state_denies_access_before_io(self):
        gate = self.gate(READ, WRITE)
        tools = self.tools(gate)
        (self.a / "existing.txt").write_text("safe", encoding="utf-8")
        with self.assertRaises(PermissionDenied):
            tools.read_text({"path": "existing.txt"})
        with self.assertRaises(PermissionDenied):
            tools.write_new({"path": "no.txt", "content": "no"})
        self.assertFalse((self.a / "no.txt").exists())
        self.assertEqual([e.reason_code for e in gate.audit_history(self.task_id, OWNER)],
                         ["task_not_running", "task_not_running"])

    def test_agent_tool_registry_uses_same_task_bound_permission_checks(self):
        gate = self.gate(READ, WRITE)
        tools = self.tools(gate)
        self.start()
        model = ScriptedModel((
            ModelTurn(action=Action("write_new_file", {"path": "model.txt", "content": "ok"})),
            ModelTurn(action=Action("read_file", {"path": "model.txt"})),
            ModelTurn(final_text="done"),
        ))
        result = Agent(model=model, tools=tools.allowed_tools()).run("work")
        self.assertEqual(result.status, "done")
        self.assertEqual((self.a / "model.txt").read_text(), "ok")
        self.assertFalse((self.b / "model.txt").exists())

    def test_workspace_files_are_new_only_and_bounded(self):
        gate = self.gate(READ, WRITE)
        tools = TaskWorkspaceFiles(self.inbox, OWNER, task_id=self.task_id,
                                   worker_id="worker-a", gate=gate, max_bytes=4)
        self.start()
        tools.write_new({"path": "new.txt", "content": "one"})
        with self.assertRaises(ToolError):
            tools.write_new({"path": "new.txt", "content": "two"})
        with self.assertRaises(ToolError):
            tools.write_new({"path": "large.txt", "content": "12345"})
        self.assertEqual(tools.read_text({"path": "new.txt"}), "one")


if __name__ == "__main__":
    unittest.main()
