"""Offline security regression tests for Stage 3.4 tool policy and audit."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ai_agent_engine import (
    Action, Agent, AgentRole, AgentSession, ModelTurn, SandboxFiles,
    ScriptedModel, SessionContext, Tool, ToolRegistry,
)
from ai_agent_engine.tools import ToolError


def action(name, args):
    return ScriptedModel((ModelTurn(action=Action(name, args)),))


class RegistryTests(unittest.TestCase):
    def test_explicit_grant_hides_other_registered_tools_from_model(self):
        ran = []
        read = Tool("read_file", lambda args: ran.append("read") or "ok")
        delete = Tool("delete_file", lambda args: ran.append("delete") or "no")
        registry = ToolRegistry((read, delete), allowed=("read_file",))

        class AskingModel:
            def complete(self, history, allowed_tools):
                self.assertion = allowed_tools
                return ModelTurn(action=Action("delete_file", {}))

        model = AskingModel()
        result = Agent(model=model, registry=registry).run("test")
        self.assertEqual(model.assertion, ("read_file",))
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.audit[0].event, "unauthorized_tool")
        self.assertEqual(ran, [])

    def test_bad_grants_and_duplicate_or_invalid_tool_names_rejected(self):
        read = Tool("read_file", lambda args: "ok")
        for grants in (("read_file", "read_file"), ("shell",)):
            with self.subTest(grants=grants), self.assertRaises(ValueError):
                ToolRegistry((read,), allowed=grants)
        with self.assertRaises(ValueError):
            ToolRegistry((read, read))
        for name in ("", "../../../secret", "shell;echo", "Tool", "-tool"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                Tool(name, lambda args: "ok")
        with self.assertRaises(ValueError):
            Agent(model=action("read_file", {}), tools=(read,), registry=ToolRegistry((read,)))


class ArgumentValidationTests(unittest.TestCase):
    def test_reject_missing_extra_wrong_type_and_oversized_arguments_before_execution(self):
        executed = []
        tool = Tool("write_new_file", lambda args: executed.append(args) or "ok",
                    required_args=("path", "content"), max_argument_bytes=18)
        bad = (
            {"path": "a.txt"},
            {"path": "a.txt", "content": "x", "extra": "x"},
            {"path": "a.txt", "content": 42},
            {"path": "a.txt", "content": "☃" * 10},
            ["path", "content"],
        )
        for args in bad:
            with self.subTest(args=args):
                result = Agent(model=action("write_new_file", args), tools=(tool,)).run("test")
                self.assertEqual(result.status, "blocked")
                self.assertEqual(result.audit[0].event, "invalid_tool_arguments")
        self.assertEqual(executed, [])

    def test_known_optional_arguments_and_defensive_copy(self):
        args = {"path": "a", "encoding": "utf-8"}
        observed = []
        tool = Tool("read_file", lambda payload: observed.append(payload) or "ok",
                    required_args=("path",), optional_args=("encoding",))
        result = Agent(model=ScriptedModel((
            ModelTurn(action=Action("read_file", args)), ModelTurn(final_text="done")
        )), tools=(tool,)).run("test")
        self.assertEqual(result.status, "done")
        self.assertEqual(observed, [args])
        self.assertIsNot(observed[0], args)

    def test_invalid_schema_is_rejected_at_registration(self):
        for kwargs in (
            {"required_args": ("path", "path")},
            {"required_args": ("path",), "optional_args": ("path",)},
            {"required_args": ("../../file",)},
            {"max_argument_bytes": 0},
            {"max_output_chars": 0},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Tool("read_file", lambda args: "ok", **kwargs)

    def test_tool_output_size_is_bounded_and_never_replayed_to_model(self):
        class RecordingModel:
            calls = 0

            def complete(self, history, allowed_tools):
                self.calls += 1
                return ModelTurn(action=Action("read_file", {}))

        model = RecordingModel()
        result = Agent(model=model, tools=(Tool("read_file", lambda args: "secret" * 10,
                                                 max_output_chars=10),)).run("read")
        self.assertEqual(result.status, "failed")
        self.assertEqual([event.event for event in result.audit], ["tool_output_limit"])
        self.assertNotIn("secret", repr(result.audit))
        self.assertEqual(model.calls, 1)


class AuditTests(unittest.TestCase):
    def test_audit_has_actor_time_safe_tool_name_and_no_argument_values(self):
        tool = Tool("read_file", lambda args: "ok", required_args=("token",))
        context = SessionContext("project-1", "worker-1", AgentRole.EXECUTOR)
        result = AgentSession(context=context, model=ScriptedModel((
            ModelTurn(action=Action("read_file", {"token": "secret-value"})),
            ModelTurn(final_text="done"),
        )), tools=(tool,)).run("read")
        self.assertEqual([event.event for event in result.audit], ["tool_ok", "completed"])
        first = result.audit[0]
        self.assertEqual(first.tool, "read_file")
        self.assertEqual(first.project_id, "project-1")
        self.assertEqual(first.agent_id, "worker-1")
        self.assertIn("+00:00", first.timestamp_utc)
        self.assertNotIn("secret-value", repr(result.audit))

    def test_unknown_tool_name_is_not_reflected_into_audit(self):
        result = Agent(model=action("private-access-secret", {}), tools=()).run("test")
        self.assertEqual(result.status, "blocked")
        self.assertNotIn("private-access-secret", repr(result.audit))

    def test_step_limit_rejects_unbounded_runs(self):
        for invalid in (0, 21):
            with self.assertRaises(ValueError):
                Agent(model=action("read_file", {}), max_steps=invalid)
        tool = Tool("read_file", lambda args: "ok")
        result = Agent(model=ScriptedModel((ModelTurn(action=Action("read_file", {})),) * 3),
                       tools=(tool,), max_steps=2).run("test")
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.steps, 2)
        self.assertEqual(result.audit[-1].event, "step_limit")

    def test_bad_model_action_is_blocked_with_sanitized_audit(self):
        result = Agent(model=ScriptedModel((ModelTurn(action="a private string"),)),
                       tools=()).run("test")
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.audit[0].event, "invalid_model_action")
        self.assertNotIn("private string", repr(result.audit))


class SandboxIsolationTests(unittest.TestCase):
    def test_two_sandbox_roots_are_independent(self):
        with TemporaryDirectory() as first, TemporaryDirectory() as second:
            a, b = SandboxFiles(Path(first)), SandboxFiles(Path(second))
            a.write_new({"path": "only-a.txt", "content": "A"})
            b.write_new({"path": "only-b.txt", "content": "B"})
            with self.assertRaises(ToolError):
                b.read_text({"path": "only-a.txt"})
            self.assertEqual(a.read_text({"path": "only-a.txt"}), "A")
            self.assertEqual(b.read_text({"path": "only-b.txt"}), "B")

    def test_reject_internal_symlinks_and_windows_style_paths(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "real.txt").write_text("inside", encoding="utf-8")
            (root / "link.txt").symlink_to(root / "real.txt")
            sandbox = SandboxFiles(root)
            for path in ("link.txt", "dir\\file.txt", "C:\\secrets.txt", "a//b", "a/../b", "bad\x00.txt"):
                with self.subTest(path=path), self.assertRaises(ToolError):
                    sandbox.read_text({"path": path})

    def test_nested_paths_work_only_under_preexisting_directories(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sub").mkdir()
            sandbox = SandboxFiles(root)
            self.assertEqual(
                sandbox.write_new({"path": "sub/data.txt", "content": "inside"}),
                "Created data.txt (6 bytes)",
            )
            self.assertEqual(sandbox.read_text({"path": "sub/data.txt"}), "inside")
            with self.assertRaises(ToolError):
                sandbox.write_new({"path": "absent/data.txt", "content": "no"})

    def test_bounded_read_does_not_return_large_or_invalid_utf8_data(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "large.txt").write_bytes(b"A" * 100)
            (root / "invalid.txt").write_bytes(b"\xff")
            sandbox = SandboxFiles(root, max_bytes=16)
            for path in ("large.txt", "invalid.txt"):
                with self.subTest(path=path), self.assertRaises(ToolError):
                    sandbox.read_text({"path": path})


if __name__ == "__main__":
    unittest.main()
