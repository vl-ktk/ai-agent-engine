"""Stage 6.5: offline structural validation and GPT/Claude outcome adapters."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_agent_engine.task_api import OwnerScope, TaskInbox, parse_submission
from ai_agent_engine.task_assignment import WorkspaceRoot
from ai_agent_engine.task_claude import ClaudeTaskExecutor
from ai_agent_engine.task_gpt import GPTTaskExecutor, GPTTaskOutcome
from ai_agent_engine.task_permissions import ActionGrant, ProtectedAction, TaskPermissionGate
from ai_agent_engine.task_response import (
    InvalidTaskResponse, MAX_RESPONSE_BYTES, MAX_TEXT_CHARS,
    adapt_task_outcome, parse_task_response,
)
from ai_agent_engine.task_state import TaskState
from ai_agent_engine.task_storage import SQLiteTaskStore


ROOT = Path(__file__).parent.parent
FIXTURE_DIR = ROOT / "tests/fixtures"
SCHEMA = ROOT / "schemas/task_execution_response.v1.schema.json"
OWNER = OwnerScope("owner-65", frozenset({"private-65"}))
ECHO = ("import sys,json;d=json.loads(sys.stdin.buffer.read());"
        "print(json.dumps({'ok':True,'text':'offline:'+d['prompt']}))")
QUOTA = ("import sys;sys.stdin.buffer.read();"
         "print('{\"ok\":false,\"error\":\"quota\"}');raise SystemExit(2)")
HANG = "import sys,time;sys.stdin.buffer.read();time.sleep(60)"


def encoded(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


class ResponseFixtureTests(unittest.TestCase):
    def setUp(self):
        self.success = json.loads((FIXTURE_DIR / "task_execution_response.success.json")
                                  .read_text(encoding="utf-8"))
        self.failure = json.loads((FIXTURE_DIR / "task_execution_response.failure.json")
                                  .read_text(encoding="utf-8"))
        self.artifact = json.loads((FIXTURE_DIR / "task_execution_response.artifact.json")
                                   .read_text(encoding="utf-8"))

    def test_fixtures_roundtrip_and_schema_fields(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(schema["properties"]["schema_version"]["const"], 1)
        for item in (self.success, self.failure, self.artifact):
            with self.subTest(state=item["state"]):
                self.assertEqual(set(item), set(schema["required"]))
                self.assertEqual(set(item["limits"]),
                                 set(schema["properties"]["limits"]["required"]))
                parsed = parse_task_response(encoded(item))
                self.assertEqual(parsed.to_payload(), item)
                self.assertEqual(parse_task_response(parsed.to_bytes()), parsed)
        self.assertEqual(self.success["result"]["artifacts"], [])
        self.assertEqual(self.failure["error"], {"code": "quota"})
        self.assertEqual(self.artifact["result"]["artifacts"][0]["path"],
                         "reports/synthetic.json")

    def test_unknown_missing_and_duplicate_fields_fail_closed(self):
        for group in (None, "result", "error", "limits"):
            with self.subTest(group=group):
                original = self.failure if group == "error" else self.success
                target = original if group is None else original[group]
                unknown = deepcopy(original)
                (unknown if group is None else unknown[group])["raw_provider_output"] = "secret"
                with self.assertRaises(InvalidTaskResponse):
                    parse_task_response(encoded(unknown))
                missing = deepcopy(original)
                (missing if group is None else missing[group]).pop(next(iter(target)))
                with self.assertRaises(InvalidTaskResponse):
                    parse_task_response(encoded(missing))
        with self.assertRaises(InvalidTaskResponse):
            parse_task_response(encoded(self.success).replace(
                b'"schema_version":1', b'"schema_version":1,"schema_version":1', 1))
        with self.assertRaises(InvalidTaskResponse):
            parse_task_response(encoded(self.success).replace(
                b'"text_chars":8', b'"text_chars":8,"text_chars":8', 1))

    def test_invalid_cross_field_states_errors_and_limits(self):
        cases = [
            ("schema_version", True), ("schema_version", 2),
            ("task_id", "task_/escape"), ("provider", "other"),
            ("model_id", "a b"), ("state", "running"),
            ("limits.timeout_ms", 0), ("limits.timeout_ms", True),
            ("limits.max_text_chars", 20001), ("limits.text_chars", 7),
            ("limits.text_chars", True), ("limits.token_usage", {"input_tokens": 12}),
            ("result.text", " "), ("result.text", "hidden\x00control"),
            ("result.text", "a" * (MAX_TEXT_CHARS + 1)),
            ("result.artifacts", "report.txt"),
            ("error", {"code": "auth"}),
        ]
        for path, value in cases:
            with self.subTest(path=path):
                original = deepcopy(self.success)
                parts = path.split(".")
                target = original
                for part in parts[:-1]:
                    target = target[part]
                target[parts[-1]] = value
                with self.assertRaises(InvalidTaskResponse):
                    parse_task_response(encoded(original))
        bad = deepcopy(self.failure)
        for state, code in (("failed", "timed_out"), ("failed", "cancelled"),
                            ("cancelled", "quota"), ("timed_out", "auth")):
            bad["state"], bad["error"]["code"] = state, code
            with self.subTest(state=state, code=code):
                with self.assertRaises(InvalidTaskResponse):
                    parse_task_response(encoded(bad))
        for mismatch in ({"result": self.success["result"]},
                         {"error": None}, {"limits": self.success["limits"]}):
            invalid = deepcopy(self.failure)
            invalid.update(mismatch)
            with self.assertRaises(InvalidTaskResponse):
                parse_task_response(encoded(invalid))

    def test_artifacts_validate_manifest_only_not_file_paths_from_worker(self):
        base = self.artifact["result"]["artifacts"][0]
        cases = [
            {"path": "../secret"}, {"path": "/etc/passwd"},
            {"path": "reports/../secret"}, {"path": "reports\\a.txt"},
            {"path": "reports//a.txt"}, {"path": "reports/.hidden"},
            {"sha256": "FAKE"}, {"size_bytes": -1},
            {"size_bytes": True}, {"size_bytes": 100_000_001},
            {"media_type": "text/html; charset=utf-8"},
            {"media_type": "../../secret"},
        ]
        for change in cases:
            with self.subTest(change=change):
                payload = deepcopy(self.artifact)
                payload["result"]["artifacts"][0].update(change)
                with self.assertRaises(InvalidTaskResponse):
                    parse_task_response(encoded(payload))
        duplicate = deepcopy(self.artifact)
        duplicate["result"]["artifacts"].append(deepcopy(base))
        with self.assertRaises(InvalidTaskResponse):
            parse_task_response(encoded(duplicate))
        empty = deepcopy(self.artifact)
        empty["result"]["artifacts"] = []
        with self.assertRaises(InvalidTaskResponse):
            parse_task_response(encoded(empty))

    def test_bad_json_encoding_numbers_length_and_surrogate(self):
        cases = (b"", b"null", b"[]", b"{", b"\xff",
                 b" " * (MAX_RESPONSE_BYTES + 1),
                 encoded(self.success).replace(b'"text_chars":8', b'"text_chars":NaN'))
        for raw in cases:
            with self.assertRaises(InvalidTaskResponse):
                parse_task_response(raw)
        p = deepcopy(self.success)
        p["result"]["text"] = "\ud800"
        with self.assertRaises(InvalidTaskResponse):
            parse_task_response(json.dumps(p).encode("ascii"))


class OfflineProviderAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-65-")
        self.root = Path(self.temp.name)
        workspaces = self.root / "workspaces"
        workspaces.mkdir(mode=0o700)
        (workspaces / "slot-a").mkdir(mode=0o700)
        (workspaces / "slot-b").mkdir(mode=0o700)
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.root / "tasks.sqlite3"),
                               workspace_root=WorkspaceRoot(workspaces))
        self.runners = []

    def tearDown(self):
        for runner in self.runners:
            runner.close()
        self.inbox.close()
        self.temp.cleanup()

    def task(self):
        return self.inbox.accept(parse_submission(encoded({
            "project_id": "private-65", "title": "Structural adapter test",
            "prompt": "AE-65-OK",
        })), OWNER).task_id

    def executor(self, provider: str, task_id: str, command: str = ECHO,
                 timeout_seconds: float = 3):
        worker_id = f"{provider}-worker"
        gate = TaskPermissionGate(self.inbox, grants=(
            ActionGrant(task_id, worker_id, ProtectedAction.EXTERNAL_REQUEST),))
        kw = dict(worker_id=worker_id, permission_gate=gate,
                  timeout_seconds=timeout_seconds,
                  worker_argv=(sys.executable, "-c", command))
        if provider == "claude":
            runner = ClaudeTaskExecutor(self.inbox, OWNER,
                                        authorized_owner_id=OWNER.owner_id,
                                        model_id="default", **kw)
        else:
            runner = GPTTaskExecutor(self.inbox, OWNER,
                                     model_id="gpt-test-model", **kw)
        self.runners.append(runner)
        return runner

    def test_both_providers_share_one_structural_success_response(self):
        fields = []
        for provider, slot in (("gpt", "slot-a"), ("claude", "slot-b")):
            with self.subTest(provider=provider):
                tid = self.task()
                runner = self.executor(provider, tid)
                raw = runner.execute(tid, slot)
                response = adapt_task_outcome(runner, raw)
                payload = response.to_payload()
                fields.append(set(payload))
                self.assertEqual(payload["provider"], provider)
                self.assertEqual(payload["state"], "succeeded")
                self.assertEqual(payload["result"]["text"], "offline:AE-65-OK")
                self.assertEqual(payload["result"]["artifacts"], [])
                self.assertIsNone(payload["error"])
                self.assertEqual(payload["limits"]["token_usage"], None)
                self.assertEqual(payload["limits"]["text_chars"], len("offline:AE-65-OK"))
                self.assertEqual(parse_task_response(response.to_bytes()), response)
                self.assertEqual([(e.provider, e.outcome) for e in runner.provider_history(tid)],
                                 [(provider, "completed")])
        self.assertEqual(fields[0], fields[1])

    def test_both_providers_share_sanitized_quota_failure(self):
        for provider, slot in (("gpt", "slot-a"), ("claude", "slot-b")):
            with self.subTest(provider=provider):
                tid = self.task()
                runner = self.executor(provider, tid, QUOTA)
                outcome = runner.execute(tid, slot)
                response = adapt_task_outcome(runner, outcome)
                payload = response.to_payload()
                self.assertEqual(payload["state"], "failed")
                self.assertIsNone(payload["result"])
                self.assertEqual(payload["error"], {"code": "quota"})
                self.assertEqual(payload["limits"]["text_chars"], 0)
                self.assertNotIn("AE-65-OK", response.to_bytes().decode())

    def test_timeout_is_terminal_and_not_a_fake_success(self):
        tid = self.task()
        runner = self.executor("gpt", tid, HANG, timeout_seconds=0.12)
        outcome = runner.execute(tid, "slot-a")
        response = adapt_task_outcome(runner, outcome)
        self.assertEqual(response.state, TaskState.TIMED_OUT)
        self.assertEqual(response.error_code, "timed_out")
        self.assertEqual(response.timeout_ms, 120)
        self.assertEqual(response.artifacts, ())
        self.assertIsNone(response.text)

    def test_duplicate_unexecuted_result_cannot_claim_another_success(self):
        tid = self.task()
        runner = self.executor("gpt", tid)
        outcome = runner.execute(tid, "slot-a")
        adapt_task_outcome(runner, outcome)
        duplicate = runner.execute(tid, "slot-a")
        self.assertFalse(duplicate.executed)
        with self.assertRaises(InvalidTaskResponse):
            adapt_task_outcome(runner, duplicate)

    def test_forged_result_identity_state_or_audit_fails_closed(self):
        tid = self.task()
        runner = self.executor("gpt", tid)
        outcome = runner.execute(tid, "slot-a")
        for forged in (
            GPTTaskOutcome("task_" + "1" * 32, outcome.state, outcome.model_id,
                           True, outcome.text),
            GPTTaskOutcome(tid, TaskState.FAILED, outcome.model_id, True),
            GPTTaskOutcome(tid, outcome.state, "other-model", True, outcome.text),
        ):
            with self.assertRaises(InvalidTaskResponse):
                adapt_task_outcome(runner, forged)
        with patch.object(runner, "provider_history", return_value=()):
            with self.assertRaises(InvalidTaskResponse):
                adapt_task_outcome(runner, outcome)


if __name__ == "__main__":
    unittest.main()
