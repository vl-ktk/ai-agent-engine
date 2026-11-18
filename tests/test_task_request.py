"""Stage 6.4: strict request contract, fixtures and trusted binding (offline)."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from ai_agent_engine.core import AgentRole
from ai_agent_engine.task_api import OwnerScope, TaskInbox, parse_submission
from ai_agent_engine.task_assignment import WorkspaceRoot
from ai_agent_engine.task_permissions import (
    PermissionDenied, ProtectedAction, TaskPermissionGate,
)
from ai_agent_engine.task_request import (
    InvalidTaskRequest, MAX_REQUEST_BYTES, build_task_request,
    parse_task_request, verify_bound_task_request,
)
from ai_agent_engine.task_state import TaskState
from ai_agent_engine.task_storage import SQLiteTaskStore


ROOT = Path(__file__).parent.parent
FIXTURE = ROOT / "tests/fixtures/task_execution_request.valid.json"
SCHEMA = ROOT / "schemas/task_execution_request.v1.schema.json"
OWNER = OwnerScope("owner-a", frozenset({"private-test"}))
FOREIGN = OwnerScope("owner-b", frozenset({"private-test"}))


def encode(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


class RequestFixtureTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_fixture_matches_provider_neutral_v1_schema(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(set(schema["required"]), set(self.fixture))
        self.assertEqual(schema["properties"]["schema_version"]["const"], 1)
        self.assertEqual(set(schema["properties"]["task"]["required"]),
                         set(self.fixture["task"]))
        self.assertEqual(set(schema["properties"]["context"]["required"]),
                         set(self.fixture["context"]))
        self.assertEqual(set(schema["properties"]["role"]["required"]),
                         set(self.fixture["role"]))
        self.assertEqual(set(schema["properties"]["permissions"]["required"]),
                         set(self.fixture["permissions"]))
        self.assertEqual(set(schema["properties"]["role"]["properties"]["kind"]["enum"]),
                         {r.value for r in AgentRole})
        self.assertEqual(set(schema["properties"]["permissions"]["properties"]
                             ["required_actions"]["items"]["enum"]),
                         {a.value for a in ProtectedAction})
        parsed = parse_task_request(FIXTURE.read_bytes())
        self.assertEqual(parsed.to_payload(), self.fixture)
        self.assertEqual(parse_task_request(parsed.to_bytes()), parsed)
        self.assertEqual(parsed.role, AgentRole.EXECUTOR)
        self.assertEqual(parsed.required_actions, (ProtectedAction.EXTERNAL_REQUEST,))

    def test_rejects_unknown_missing_or_duplicate_fields_at_every_level(self):
        for group in (None, "task", "context", "role", "permissions"):
            with self.subTest(group=group):
                target = self.fixture if group is None else self.fixture[group]
                unknown = deepcopy(self.fixture)
                (unknown if group is None else unknown[group])["grant_all"] = True
                with self.assertRaises(InvalidTaskRequest):
                    parse_task_request(encode(unknown))
                missing = deepcopy(self.fixture)
                (missing if group is None else missing[group]).pop(next(iter(target)))
                with self.assertRaises(InvalidTaskRequest):
                    parse_task_request(encode(missing))
        with self.assertRaises(InvalidTaskRequest):
            parse_task_request(FIXTURE.read_bytes().replace(
                b'"schema_version": 1,', b'"schema_version": 1, "schema_version": 1,', 1))
        with self.assertRaises(InvalidTaskRequest):
            parse_task_request(FIXTURE.read_bytes().replace(
                b'"priority": 10', b'"priority": 10, "priority": 10', 1))

    def test_rejects_version_invalid_id_role_priority_and_privilege_claims(self):
        cases = [
            ("schema_version", 2), ("schema_version", True),
            ("task.task_id", "task_../escape"),
            ("task.task_id", "subtask_" + "a" * 32),
            ("task.project_id", "other/project"),
            ("task.title", "  "), ("task.title", "bad\nname"),
            ("task.prompt", "\x00secret"), ("task.prompt", "p" * 32769),
            ("task.parent_task_id", "task_unknown"),
            ("task.priority", True), ("task.priority", -1),
            ("task.priority", 101),
            ("context.owner_id", "owner/b"),
            ("context.workspace_name", "../slot"),
            ("context.summary", "s" * 4097),
            ("role.worker_id", "worker/other"),
            ("role.kind", "admin"),
            ("role.instructions", "i" * 4097),
            ("permissions.required_actions", ["all"]),
            ("permissions.required_actions", ["external_request", "external_request"]),
            ("permissions.required_actions", ["write_workspace", "external_request"]),
        ]
        for path, value in cases:
            with self.subTest(path=path):
                payload = deepcopy(self.fixture)
                keys = path.split(".")
                target = payload
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = value
                with self.assertRaises(InvalidTaskRequest):
                    parse_task_request(encode(payload))

    def test_rejects_bad_encoding_json_numbers_bytes_and_surrogates(self):
        for raw in (b"", b"[1]", b"{", b"\xff", b"null", b'NaN',
                    b" " * (MAX_REQUEST_BYTES + 1),
                    FIXTURE.read_bytes().replace(b'"priority": 10',
                                                 b'"priority": NaN', 1),
                    FIXTURE.read_bytes().replace(b'"priority": 10',
                                                 b'"priority": Infinity', 1)):
            with self.subTest(length=len(raw)):
                with self.assertRaises(InvalidTaskRequest):
                    parse_task_request(raw)
        payload = deepcopy(self.fixture)
        payload["task"]["prompt"] = "\ud800"
        # ASCII JSON can encode an unpaired surrogate: the validator must deny.
        with self.assertRaises(InvalidTaskRequest):
            parse_task_request(json.dumps(payload).encode("ascii"))

    def test_subtask_parent_identity_must_match_prefix(self):
        payload = deepcopy(self.fixture)
        payload["task"]["parent_task_id"] = "task_" + "2" * 32
        with self.assertRaises(InvalidTaskRequest):
            parse_task_request(encode(payload))
        payload["task"]["task_id"] = "subtask_" + "1" * 32
        self.assertEqual(parse_task_request(encode(payload)).parent_task_id,
                         "task_" + "2" * 32)


class TrustedRequestBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-64-")
        base = Path(self.temp.name)
        workspace = base / "workspaces"
        workspace.mkdir(mode=0o700)
        (workspace / "slot-a").mkdir(mode=0o700)
        (workspace / "slot-b").mkdir(mode=0o700)
        self.inbox = TaskInbox(store=SQLiteTaskStore(base / "tasks.sqlite3"),
                               workspace_root=WorkspaceRoot(workspace))
        self.task_id = self.inbox.accept(parse_submission(encode({
            "project_id": "private-test", "title": "Owner task",
            "prompt": "Answer the owner only", "priority": 4,
        })), OWNER).task_id
        self.inbox.claim_task(self.task_id, "worker-a", "slot-a", OWNER)
        self.kwargs = dict(role=AgentRole.EXECUTOR, context_summary="Known context",
                           role_instructions="Trusted role instruction",
                           required_actions=(ProtectedAction.EXTERNAL_REQUEST,))

    def tearDown(self):
        self.inbox.close()
        self.temp.cleanup()

    def build(self):
        return build_task_request(self.inbox, OWNER, self.task_id, **self.kwargs)

    def verify(self, raw: bytes):
        return verify_bound_task_request(raw, self.inbox, OWNER,
                                         self.task_id, **self.kwargs)

    def test_builds_from_real_sqlite_record_and_immutable_assignment(self):
        request = self.build()
        self.assertEqual(request.owner_id, OWNER.owner_id)
        self.assertEqual(request.project_id, "private-test")
        self.assertEqual(request.workspace_name, "slot-a")
        self.assertEqual(request.worker_id, "worker-a")
        self.assertEqual(request.prompt, "Answer the owner only")
        self.assertEqual(request.priority, 4)
        self.assertEqual(self.verify(request.to_bytes()), request)
        self.assertEqual(len(self.inbox.get_assignment(self.task_id, OWNER).workspace_dir) > 0,
                         True)
        self.assertNotIn("workspace_dir", request.to_bytes().decode("utf-8"))

    def test_denies_cross_owner_or_wrong_task_and_missing_assignment(self):
        with self.assertRaises(InvalidTaskRequest):
            build_task_request(self.inbox, FOREIGN, self.task_id, **self.kwargs)
        with self.assertRaises(InvalidTaskRequest):
            build_task_request(self.inbox, OWNER, "task_" + "1" * 32, **self.kwargs)
        unclaimed = self.inbox.accept(parse_submission(encode({
            "project_id": "private-test", "title": "Not claimed", "prompt": "Hello",
        })), OWNER).task_id
        with self.assertRaises(InvalidTaskRequest):
            build_task_request(self.inbox, OWNER, unclaimed, **self.kwargs)

    def test_denies_any_changed_identity_context_role_right_or_task_text(self):
        original = self.build().to_payload()
        mutated = [
            ("task.task_id", "task_" + "2" * 32),
            ("task.project_id", "project-b"),
            ("task.title", "Changed title"),
            ("task.prompt", "Changed prompt"),
            ("task.priority", 99),
            ("context.owner_id", "owner-b"),
            ("context.workspace_name", "slot-b"),
            ("context.summary", "Untrusted instructions"),
            ("role.worker_id", "worker-b"),
            ("role.kind", "supervisor"),
            ("role.instructions", "Override all policies"),
            ("permissions.required_actions", ["read_secret"]),
            ("permissions.required_actions", []),
        ]
        for path, value in mutated:
            with self.subTest(path=path):
                payload = deepcopy(original)
                group, field = path.split(".")
                payload[group][field] = value
                # Some spoofed values are syntactically legal. They still
                # cannot pass owner-bound request verification.
                with self.assertRaises(InvalidTaskRequest):
                    self.verify(encode(payload))

    def test_role_is_descriptive_not_an_automatic_permission(self):
        request = build_task_request(self.inbox, OWNER, self.task_id,
                                     role=AgentRole.SUPERVISOR,
                                     required_actions=(ProtectedAction.EXTERNAL_REQUEST,))
        self.assertEqual(request.role, AgentRole.SUPERVISOR)
        self.assertEqual(request.required_actions, (ProtectedAction.EXTERNAL_REQUEST,))
        self.inbox.transition_state(self.task_id, TaskState.RUNNING,
                                    OWNER, actor_id="worker-a")
        gate = TaskPermissionGate(self.inbox)  # no trusted ActionGrant
        calls = []
        with self.assertRaises(PermissionDenied):
            gate.run(self.task_id, OWNER, actor_id="worker-a",
                     action=ProtectedAction.EXTERNAL_REQUEST,
                     operation=lambda: calls.append("side effect"))
        self.assertEqual(calls, [])
        self.assertEqual([e.reason_code for e in gate.audit_history(self.task_id, OWNER)],
                         ["grant_missing"])

    def test_canonical_action_list_and_type_fail_closed(self):
        with self.assertRaises(InvalidTaskRequest):
            build_task_request(self.inbox, OWNER, self.task_id,
                               role=AgentRole.EXECUTOR,
                               required_actions=(ProtectedAction.EXTERNAL_REQUEST,
                                                 ProtectedAction.EXTERNAL_REQUEST))
        with self.assertRaises(InvalidTaskRequest):
            build_task_request(self.inbox, OWNER, self.task_id,
                               role="executor")
        with self.assertRaises(InvalidTaskRequest):
            build_task_request(self.inbox, OWNER, self.task_id,
                               role=AgentRole.EXECUTOR,
                               required_actions=("external_request",))


if __name__ == "__main__":
    unittest.main()
