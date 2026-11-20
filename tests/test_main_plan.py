"""Stage 7.2: offline, provider-free and Netcup-free plan contract checks."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ai_agent_engine.main_orchestrator import OwnerDecision
from ai_agent_engine.main_plan import (
    MAX_REPAIRS, InvalidPlan, approve_plan, decompose_for_review,
    load_approved_plan, parse_plan,
)
from ai_agent_engine.task_api import (OwnerApprovalProof, OwnerScope,
                                      ProjectForbidden, TaskInbox, TaskSubmission)
from ai_agent_engine.task_storage import SQLiteTaskStore, StorageUnavailable


SCOPE = OwnerScope("vlady", frozenset({"engine", "private"}))
OUTSIDER = OwnerScope("other", frozenset({"engine"}))
SOURCE = TaskSubmission("engine", "Create dashboards", "Discuss requirements first")


def step(key, deps=(), *, parent=None, project="engine"):
    return {
        "step_id": key, "project_id": project, "title": "Build " + key,
        "goal": "Produce the planned component", "instructions": "Implement and verify",
        "boundaries": "Do not deploy or spend funds",
        "expected_result": "Reviewed changes", "acceptance_checks": ["Unit tests PASS"],
        "expected_artifacts": ["Git diff"], "depends_on": list(deps),
        "parent_step_id": parent,
    }


def plan_json(*steps):
    return json.dumps({"schema_version": 1, "steps": list(steps)},
                      ensure_ascii=False, separators=(",", ":")).encode("utf-8")


GOOD = plan_json(
    step("contract"), step("api", ("contract",)),
    step("ui", ("contract",)), step("integrate", ("api", "ui")),
    step("tests", ("integrate",), parent="integrate"),
)


class FakeGPT:
    def __init__(self, proposals, *, rejects=0, wrong_review=False):
        self.proposals = list(proposals)
        self.rejects = rejects
        self.wrong_review = wrong_review
        self.feedback = []
        self.reviews = 0

    def propose(self, source, feedback, detail_hint):
        self.feedback.append((feedback, detail_hint))
        if not self.proposals:
            raise RuntimeError("model exhausted")
        return self.proposals.pop(0)

    def review(self, source, plan_json):
        self.reviews += 1
        digest = sha256(plan_json).hexdigest()
        if self.wrong_review:
            digest = "0" * 64
        rejected = self.reviews <= self.rejects
        return json.dumps({
            "plan_sha256": digest, "passed": not rejected,
            "summary": "Reviewed the actual dependencies and requirements",
            "findings": ["Missing edge"] if rejected else [],
        }).encode("utf-8")


class PlanParsingTests(unittest.TestCase):
    def test_valid_dag_and_independent_parallel_batches(self):
        spec = parse_plan(GOOD, SCOPE, SOURCE)
        self.assertEqual(spec.batches(), (
            ("contract",), ("api", "ui"), ("integrate",), ("tests",)))
        self.assertEqual(spec.steps[4].parent_step_id, "integrate")
        self.assertEqual(spec.steps[0].expected_artifacts, ("Git diff",))

    def test_one_step_compact_plan(self):
        self.assertEqual(parse_plan(plan_json(step("single")), SCOPE, SOURCE).batches(),
                         (("single",),))

    def test_reject_missing_oversized_spoofed_and_wrong_types(self):
        good = json.loads(GOOD)
        changes = [
            lambda p: p["steps"][0].pop("goal"),
            lambda p: p["steps"][0].update({"owner_id": "vlady"}),
            lambda p: p["steps"][0].update({"action_grant": "yes"}),
            lambda p: p["steps"][0].update({"acceptance_checks": []}),
            lambda p: p["steps"][0].update({"expected_artifacts": []}),
            lambda p: p["steps"][0].update({"expected_result": " "}),
            lambda p: p["steps"][0].update({"step_id": "../bad"}),
            lambda p: p["steps"][0].update({"depends_on": ["api", "api"]}),
            lambda p: p["steps"][0].update({"project_id": "private"}),
            lambda p: p.update({"schema_version": True}),
            lambda p: p.update({"schema_version": 2}),
            lambda p: p.update({"owner_id": "vlady"}),
            lambda p: p.update({"steps": []}),
            lambda p: p.update({"steps": p["steps"] * 7}),
        ]
        for change in changes:
            with self.subTest(change=change):
                payload = json.loads(json.dumps(good))
                change(payload)
                with self.assertRaises(InvalidPlan):
                    parse_plan(json.dumps(payload).encode(), SCOPE, SOURCE)

    def test_reject_graph_cycles_and_unknown_references(self):
        bad_plans = (
            plan_json(step("a", ("b",)), step("b", ("a",))),
            plan_json(step("a", ("a",))),
            plan_json(step("a", ("missing",))),
            plan_json(step("a"), step("a")),
            plan_json(step("a", parent="b"), step("b", parent="a")),
            plan_json(step("a", parent="missing")),
            plan_json(step("a", parent="a")),
        )
        for raw in bad_plans:
            with self.subTest(raw=raw), self.assertRaises(InvalidPlan):
                parse_plan(raw, SCOPE, SOURCE)

    def test_reject_excessive_depth(self):
        names = ["a", "b", "c", "d", "e"]
        raw = plan_json(*(step(name, parent=names[i - 1] if i else None)
                          for i, name in enumerate(names)))
        with self.assertRaisesRegex(InvalidPlan, "deep"):
            parse_plan(raw, SCOPE, SOURCE)

    def test_strict_json_and_scope(self):
        for raw in (b'\xff', b'{}', b'[]', b'null', b'{"steps":[],"steps":[]}',
                    b'{"schema_version":NaN,"steps":[]}', b'x' * 131073):
            with self.subTest(raw=raw[:30]), self.assertRaises(InvalidPlan):
                parse_plan(raw, SCOPE, SOURCE)
        with self.assertRaises(ProjectForbidden):
            parse_plan(GOOD, OwnerScope("vlady", frozenset({"private"})), SOURCE)


class PlanningFlowTests(unittest.TestCase):
    def test_model_review_and_deterministic_validation_both_required(self):
        model = FakeGPT([GOOD])
        outcome = decompose_for_review(model, SCOPE, SOURCE)
        self.assertEqual(outcome.status, "ready")
        self.assertEqual(outcome.attempts, 1)
        self.assertEqual(model.reviews, 1)
        self.assertTrue(outcome.proposal.sha256)

    def test_bounded_repairs_and_feedback_to_model(self):
        model = FakeGPT([plan_json(step("a", ("missing",))), GOOD])
        result = decompose_for_review(model, SCOPE, SOURCE, detail_hint="more")
        self.assertEqual((result.status, result.attempts), ("ready", 2))
        self.assertEqual(model.reviews, 1)
        self.assertEqual(model.feedback[1][0][0], "plan_validation_failed")
        self.assertIn("unknown", model.feedback[1][0][1])
        self.assertEqual(model.feedback[1][1], "more")

    def test_semantic_review_veto_is_repaired(self):
        model = FakeGPT([GOOD, GOOD], rejects=1)
        result = decompose_for_review(model, SCOPE, SOURCE)
        self.assertEqual((result.status, result.attempts), ("ready", 2))
        self.assertEqual(model.feedback[1][0], ("semantic_rejected", "Missing edge"))

    def test_exhaustion_returns_owner_escalation_no_approvable_draft(self):
        model = FakeGPT([plan_json(step("a", ("other",)))] * 5)
        result = decompose_for_review(model, SCOPE, SOURCE)
        self.assertEqual((result.status, result.attempts), ("needs_owner", MAX_REPAIRS + 1))
        self.assertIsNone(result.proposal)
        self.assertEqual(len(model.feedback), MAX_REPAIRS + 1)
        self.assertEqual(model.reviews, 0)

    def test_unavailable_model_fails_immediately(self):
        result = decompose_for_review(FakeGPT([]), SCOPE, SOURCE)
        self.assertEqual((result.status, result.attempts, result.issue_codes),
                         ("needs_owner", 1, ("model_unavailable",)))

    def test_wrong_review_hash_never_passes(self):
        model = FakeGPT([GOOD] * 3, wrong_review=True)
        result = decompose_for_review(model, SCOPE, SOURCE)
        self.assertEqual(result.status, "needs_owner")
        self.assertIsNone(result.proposal)

    def test_invalid_preferences_and_scope_fail_before_model_call(self):
        model = FakeGPT([GOOD])
        with self.assertRaises(InvalidPlan):
            decompose_for_review(model, SCOPE, SOURCE, max_repairs=9)
        with self.assertRaises(InvalidPlan):
            decompose_for_review(model, SCOPE, SOURCE, detail_hint="grant")
        with self.assertRaises(ProjectForbidden):
            decompose_for_review(model, OUTSIDER, TaskSubmission("private", "A", "B"))
        self.assertFalse(model.feedback)


class PlanApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        os.chmod(self.temp.name, 0o700)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "tasks.sqlite3"
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.path))
        self.addCleanup(self._close)
        self.proposal = decompose_for_review(FakeGPT([GOOD]), SCOPE, SOURCE).proposal
        self.decision = self._decision(self.proposal)

    def _close(self):
        if self.inbox is not None:
            self.inbox.close()

    @staticmethod
    def _decision(proposal, channel="button"):
        return OwnerDecision(proposal.owner_id, proposal.proposal_id,
                             proposal.sha256, channel)

    def test_approval_immutable_version_replay_restart_and_no_execution(self):
        receipt = approve_plan(self.inbox, SCOPE, self.proposal, self.decision)
        self.assertEqual(receipt.proposal.sha256, self.proposal.sha256)
        self.assertEqual(approve_plan(self.inbox, SCOPE, self.proposal, self.decision), receipt)
        self.assertEqual(self.inbox.snapshot(SCOPE), ())
        self.assertEqual(load_approved_plan(self.inbox, SCOPE, self.proposal.plan_id), receipt)
        self.inbox.close()
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.path))
        self.assertEqual(load_approved_plan(self.inbox, SCOPE, self.proposal.plan_id), receipt)
        self.assertEqual(self.inbox.snapshot(SCOPE), ())

    def test_no_unauthenticated_or_stale_or_edited_approval(self):
        for tampered in (
            replace(self.decision, owner_id="someone_else"),
            replace(self.decision, proposal_sha256="0" * 64),
            replace(self.decision, proposal_id="proposal_" + "0" * 32),
            replace(self.decision, channel="text"),
        ):
            with self.subTest(tampered=tampered), self.assertRaises(InvalidPlan):
                approve_plan(self.inbox, SCOPE, self.proposal, tampered)
        with self.assertRaises(InvalidPlan):
            approve_plan(self.inbox, SCOPE, replace(self.proposal, revision=2),
                         self.decision)
        with self.assertRaises(InvalidPlan):
            approve_plan(self.inbox, SCOPE, replace(self.proposal, sha256="0" * 64),
                         replace(self.decision, proposal_sha256="0" * 64))
        with self.assertRaises(ProjectForbidden):
            approve_plan(self.inbox, OwnerScope("vlady", frozenset({"private"})),
                         self.proposal, self.decision)
        self.assertIsNone(load_approved_plan(self.inbox, SCOPE, self.proposal.plan_id))

    def test_revision_must_follow_prior_and_old_approval_becomes_stale(self):
        approve_plan(self.inbox, SCOPE, self.proposal, self.decision)
        revised = decompose_for_review(FakeGPT([plan_json(step("fresh"))]),
                                       SCOPE, SOURCE, plan_id=self.proposal.plan_id,
                                       revision=2).proposal
        receipt = approve_plan(self.inbox, SCOPE, revised, self._decision(revised, "message"))
        self.assertEqual(receipt.proposal.revision, 2)
        self.assertEqual(load_approved_plan(self.inbox, SCOPE, revised.plan_id), receipt)
        with self.assertRaises(StorageUnavailable):
            approve_plan(self.inbox, SCOPE, self.proposal, self.decision)
        with self.assertRaises(StorageUnavailable):
            approve_plan(self.inbox, SCOPE, revised,
                         self._decision(revised, "button"))
        skipped = decompose_for_review(FakeGPT([GOOD]), SCOPE, SOURCE,
                                       plan_id=revised.plan_id, revision=4).proposal
        with self.assertRaises(StorageUnavailable):
            approve_plan(self.inbox, SCOPE, skipped, self._decision(skipped))
        self.assertEqual(load_approved_plan(self.inbox, SCOPE, revised.plan_id), receipt)

    def test_out_of_order_initial_revision_denied(self):
        revised = decompose_for_review(FakeGPT([GOOD]), SCOPE, SOURCE, revision=2).proposal
        with self.assertRaises(StorageUnavailable):
            approve_plan(self.inbox, SCOPE, revised, self._decision(revised))
        self.assertEqual(self.inbox.snapshot(SCOPE), ())

    def test_foreign_owner_cannot_read_approved_plan(self):
        approve_plan(self.inbox, SCOPE, self.proposal, self.decision)
        self.assertIsNone(load_approved_plan(self.inbox, OUTSIDER, self.proposal.plan_id))

    def test_saved_plan_tampering_detected_on_scoped_read(self):
        approve_plan(self.inbox, SCOPE, self.proposal, self.decision)
        with self.inbox._store._db() as db:
            db.execute("""UPDATE approved_task_plans SET canonical_json = '{}'
                          WHERE plan_id=?""", (self.proposal.plan_id,))
        with self.assertRaises(StorageUnavailable):
            load_approved_plan(self.inbox, SCOPE, self.proposal.plan_id)

    def test_same_proposal_id_cannot_approve_different_plan(self):
        approve_plan(self.inbox, SCOPE, self.proposal, self.decision)
        other = decompose_for_review(FakeGPT([plan_json(step("other"))]),
                                     SCOPE, SOURCE).proposal
        other = replace(other, proposal_id=self.proposal.proposal_id)
        with self.assertRaises(StorageUnavailable):
            approve_plan(self.inbox, SCOPE, other, self._decision(other))
        self.assertIsNone(load_approved_plan(self.inbox, SCOPE, other.plan_id))

    def test_sqlite_failure_rolls_back_and_does_not_admit_tasks(self):
        db = self.inbox._store._db()
        db.execute("""CREATE TRIGGER reject_plans BEFORE INSERT ON approved_task_plans
                      BEGIN SELECT RAISE(ABORT, 'test-only denial'); END""")
        with self.assertRaises(StorageUnavailable):
            approve_plan(self.inbox, SCOPE, self.proposal, self.decision)
        self.assertIsNone(load_approved_plan(self.inbox, SCOPE, self.proposal.plan_id))
        self.assertEqual(self.inbox.snapshot(SCOPE), ())
        db.execute("DROP TRIGGER reject_plans")
        approve_plan(self.inbox, SCOPE, self.proposal, self.decision)

    def test_existing_v11_database_migrates_without_forged_plan(self):
        approved_task = self.inbox.accept(
            SOURCE, SCOPE, idempotency_key="proposal_" + "1" * 32,
            owner_approval=OwnerApprovalProof("proposal_" + "1" * 32,
                                             "button", "f" * 64))
        self.inbox.close()
        self.inbox = None
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TABLE approved_task_plans")
            db.execute("PRAGMA user_version=11")
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.path))
        self.assertEqual(self.inbox._store._db().execute(
            "PRAGMA user_version").fetchone(), (17,))
        self.assertIsNone(load_approved_plan(self.inbox, SCOPE, self.proposal.plan_id))
        self.assertEqual(self.inbox._store.owner_approval(approved_task.task_id),
                         ("vlady", "engine", "proposal_" + "1" * 32,
                          "button", "f" * 64))
        approve_plan(self.inbox, SCOPE, self.proposal, self.decision)


if __name__ == "__main__":
    unittest.main()
