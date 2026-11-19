"""7.1: owner-bound GPT context, explicit approval and durable isolation."""

from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ai_agent_engine.main_orchestrator import (
    ContextQuery, InvalidOrchestratorRequest, OwnerDecision,
    admit_approved_main_task, build_main_context, draft_main_task,
    parse_context_query,
)
from ai_agent_engine.task_api import (
    IdempotencyConflict, InvalidTask, OwnerScope, ProjectForbidden,
    TaskInbox, TaskSubmission, parse_submission,
)
from ai_agent_engine.task_assignment import WorkspaceRoot
from ai_agent_engine.task_permissions import (
    PermissionDenied, ProtectedAction, TaskPermissionGate,
)
from ai_agent_engine.task_state import TaskState
from ai_agent_engine.task_storage import SQLiteTaskStore, StorageUnavailable


OWNER = OwnerScope("vl", frozenset({"ktk-social", "ai-engine"}))
OTHER = OwnerScope("other-owner", frozenset({"ktk-social"}))


def request(project: str, title: str, prompt: str) -> TaskSubmission:
    return parse_submission(json.dumps({
        "project_id": project, "title": title, "prompt": prompt,
    }).encode())


class MainOrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-stage7-1-")
        self.path = Path(self.temp.name) / "tasks.sqlite3"
        self.workspaces = Path(self.temp.name) / "workspaces"
        self.workspaces.mkdir(mode=0o700)
        (self.workspaces / "slot-7-1").mkdir(mode=0o700)
        self.root = WorkspaceRoot(self.workspaces)
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.path),
                               workspace_root=self.root)
        self.task = self.inbox.accept(request(
            "ktk-social", "Fix dashboard", "private-project-prompt SECRET_TOKEN"), OWNER)
        self.inbox.accept(request("ai-engine", "Update core", "hidden-engine-prompt"), OWNER)
        self.foreign = self.inbox.accept(request(
            "ktk-social", "Other owner's dashboard", "foreign-SECRET"), OTHER)

    def tearDown(self):
        self.inbox.close()
        self.temp.cleanup()

    def test_overview_counts_only_owner_projects_and_never_raw_prompts(self):
        context = build_main_context(self.inbox, OWNER, ContextQuery(None, None))
        payload = context.to_payload()
        self.assertEqual([p["project_id"] for p in payload["projects"]],
                         ["ai-engine", "ktk-social"])
        self.assertEqual(payload["projects"][0]["states"]["queued"], 1)
        self.assertEqual(payload["projects"][1]["states"]["queued"], 1)
        self.assertIsNone(payload["focus"])
        encoded = json.dumps(payload)
        for forbidden in ("Fix dashboard", "Other owner's dashboard",
                          "private-project-prompt", "foreign-SECRET"):
            self.assertNotIn(forbidden, encoded)

    def test_focus_details_from_exact_owner_project_without_prompt(self):
        payload = build_main_context(self.inbox, OWNER,
                       ContextQuery("ktk-social", None)).to_payload()
        self.assertEqual(len(payload["focus"]["tasks"]), 1)
        detail = payload["focus"]["tasks"][0]
        self.assertEqual(detail["task_id"], self.task.task_id)
        self.assertNotIn("title", detail)
        self.assertNotIn("prompt", detail)
        self.assertNotIn("SECRET", json.dumps(payload))

    def test_foreign_project_and_owner_task_cannot_be_selected(self):
        with self.assertRaises(ProjectForbidden):
            build_main_context(self.inbox, OWNER, ContextQuery("unknown", None))
        with self.assertRaises(InvalidOrchestratorRequest):
            build_main_context(self.inbox, OWNER,
                               ContextQuery("ktk-social", self.foreign.task_id))
        with self.assertRaises(InvalidOrchestratorRequest):
            build_main_context(self.inbox, OWNER,
                               ContextQuery("ai-engine", self.task.task_id))
        foreign_payload = build_main_context(self.inbox, OTHER,
                            ContextQuery("ktk-social", None)).to_payload()
        self.assertEqual(len(foreign_payload["focus"]["tasks"]), 1)
        self.assertEqual(foreign_payload["focus"]["tasks"][0]["task_id"],
                         self.foreign.task_id)

    def test_context_query_only_selects_project_and_task_not_identity_or_grant(self):
        valid = {"schema_version": 1, "focus_project_id": "ktk-social",
                 "focus_task_id": self.task.task_id}
        self.assertEqual(parse_context_query(json.dumps(valid).encode()),
                         ContextQuery("ktk-social", self.task.task_id))
        for key in ("owner_id", "role", "permissions", "grant_all", "prompt"):
            with self.subTest(key=key):
                data = dict(valid, **{key: "admin"})
                with self.assertRaises(InvalidOrchestratorRequest):
                    parse_context_query(json.dumps(data).encode())
        for raw in (b"", b"null", b"[1]", b"\xff", b"{" + b" " * 4096,
                    b'{"schema_version":1,"schema_version":1,'
                    b'"focus_project_id":null,"focus_task_id":null}',
                    b'{"schema_version":true,"focus_project_id":null,"focus_task_id":null}',
                    b'{"schema_version":1,"focus_project_id":null,'
                    b'"focus_task_id":"task_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'):
            with self.subTest(raw=raw[:40]):
                with self.assertRaises(InvalidOrchestratorRequest):
                    parse_context_query(raw)

    def test_draft_is_not_a_task_or_permission(self):
        initial = len(self.inbox.snapshot(OWNER))
        proposal = draft_main_task(OWNER, request(
            "ktk-social", "Plan only", "Discuss requirements, do not execute"))
        self.assertEqual(len(self.inbox.snapshot(OWNER)), initial)
        self.assertEqual(self.inbox.get(proposal.proposal_id, OWNER), None)
        self.assertEqual(self.inbox._store.owner_approval(self.task.task_id), None)

    def test_button_approval_is_atomic_durable_and_idempotent(self):
        proposal = draft_main_task(OWNER, request("ktk-social", "Approved", "Do work"))
        decision = OwnerDecision(OWNER.owner_id, proposal.proposal_id,
                                 proposal.sha256, "button")
        first = admit_approved_main_task(self.inbox, OWNER, proposal, decision)
        self.assertEqual(admit_approved_main_task(self.inbox, OWNER,
                                                  proposal, decision), first)
        self.assertEqual(self.inbox._store.owner_approval(first.task_id),
                         ("vl", "ktk-social", proposal.proposal_id,
                          "button", proposal.sha256))
        self.inbox.close()
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.path),
                               workspace_root=self.root)
        self.assertEqual(self.inbox.get(first.task_id, OWNER), first)
        self.assertEqual(admit_approved_main_task(self.inbox, OWNER,
                                                  proposal, decision), first)

    def test_message_approval_is_explicit_and_durable(self):
        proposal = draft_main_task(OWNER, request("ai-engine", "Review", "Run approved review"))
        decision = OwnerDecision(OWNER.owner_id, proposal.proposal_id,
                                 proposal.sha256, "message")
        record = admit_approved_main_task(self.inbox, OWNER, proposal, decision)
        self.assertEqual(self.inbox._store.owner_approval(record.task_id)[3], "message")

    def test_approval_cannot_expand_missing_execution_grants(self):
        proposal = draft_main_task(OWNER, request("ktk-social", "Bounded", "Do"))
        decision = OwnerDecision(OWNER.owner_id, proposal.proposal_id,
                                 proposal.sha256, "button")
        record = admit_approved_main_task(self.inbox, OWNER, proposal, decision)
        self.inbox.claim_task(record.task_id, "worker-a", "slot-7-1", OWNER)
        self.inbox.transition_state(record.task_id, TaskState.RUNNING,
                                    OWNER, actor_id="worker-a")
        performed = []
        gate = TaskPermissionGate(self.inbox)
        with self.assertRaises(PermissionDenied):
            gate.run(record.task_id, OWNER, actor_id="worker-a",
                     action=ProtectedAction.DELETE_ARTIFACT,
                     operation=lambda: performed.append("deleted"))
        self.assertEqual(performed, [])
        self.assertEqual(gate.audit_history(record.task_id, OWNER)[0].reason_code,
                         "grant_missing")

    def test_forged_owner_scope_channel_or_changed_proposal_is_denied(self):
        proposal = draft_main_task(OWNER, request("ktk-social", "Original", "Original"))
        valid = OwnerDecision(OWNER.owner_id, proposal.proposal_id,
                              proposal.sha256, "message")
        invalid = [replace(valid, owner_id="other-owner"),
                   replace(valid, proposal_id="proposal_" + "b" * 32),
                   replace(valid, proposal_sha256="0" * 64),
                   replace(valid, channel="automatic"),
                   replace(proposal, submission=request(
                       "ktk-social", "Tampered", "Different"))]
        for bad in invalid:
            with self.subTest(bad=repr(bad)[:60]):
                with self.assertRaises(InvalidOrchestratorRequest):
                    admit_approved_main_task(self.inbox, OWNER,
                        bad if isinstance(bad, type(proposal)) else proposal,
                        valid if isinstance(bad, type(proposal)) else bad)

    def test_approval_without_durable_store_never_launches(self):
        transient = TaskInbox()
        proposal = draft_main_task(OWNER, request("ktk-social", "Draft", "Do"))
        decision = OwnerDecision(OWNER.owner_id, proposal.proposal_id,
                                 proposal.sha256, "button")
        with self.assertRaises(InvalidTask):
            admit_approved_main_task(transient, OWNER, proposal, decision)
        self.assertEqual(transient.snapshot(OWNER), ())

    def test_storage_failure_rolls_back_both_task_and_approval(self):
        proposal = draft_main_task(OWNER, request("ktk-social", "Atomic", "Do"))
        decision = OwnerDecision(OWNER.owner_id, proposal.proposal_id,
                                 proposal.sha256, "button")
        self.inbox._store._db().execute("""CREATE TRIGGER reject_owner_approval
            BEFORE INSERT ON task_owner_approvals BEGIN
                SELECT RAISE(ABORT, 'test failure'); END""")
        old_count = len(self.inbox.snapshot(OWNER))
        with self.assertRaises(StorageUnavailable):
            admit_approved_main_task(self.inbox, OWNER, proposal, decision)
        self.assertEqual(len(self.inbox.snapshot(OWNER)), old_count)
        self.assertEqual(self.inbox._store._db().execute(
            "SELECT count(*) FROM tasks").fetchone()[0], 3)

    def test_approval_replay_cannot_change_channel(self):
        proposal = draft_main_task(OWNER, request("ktk-social", "Once", "Do"))
        first = OwnerDecision(OWNER.owner_id, proposal.proposal_id,
                              proposal.sha256, "message")
        admit_approved_main_task(self.inbox, OWNER, proposal, first)
        with self.assertRaises(IdempotencyConflict):
            admit_approved_main_task(self.inbox, OWNER, proposal,
                                     replace(first, channel="button"))

    def test_existing_v10_database_migrates_without_forged_approvals(self):
        before = self.task.task_id
        self.inbox.close()
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TABLE task_owner_approvals")
            db.execute("PRAGMA user_version=10")
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.path),
                               workspace_root=self.root)
        self.assertIsNotNone(self.inbox.get(before, OWNER))
        self.assertIsNone(self.inbox._store.owner_approval(before))
        self.assertEqual(self.inbox._store._db().execute(
            "PRAGMA user_version").fetchone()[0], 13)

    def test_context_truncates_oversized_focus_without_escaping_scope(self):
        for index in range(20):
            self.inbox.accept(request("ktk-social", "Item " + str(index), "Prompt"), OWNER)
        context = build_main_context(self.inbox, OWNER,
                                     ContextQuery("ktk-social", None))
        self.assertEqual(len(context.focus_tasks), 16)
        self.assertTrue(context.focus_truncated)
        self.assertEqual(dict(context.projects)["ktk-social"][0][0], "assigned")

    def test_confidential_task_title_is_not_exported_as_model_context(self):
        self.inbox.accept(request("ktk-social", "PRIVATE_API_KEY_VALUE",
                                  "Do not leak"), OWNER)
        encoded = json.dumps(build_main_context(
            self.inbox, OWNER, ContextQuery("ktk-social", None)).to_payload())
        self.assertNotIn("PRIVATE_API_KEY_VALUE", encoded)


if __name__ == "__main__":
    unittest.main()
