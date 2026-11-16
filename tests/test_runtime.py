"""Only offline tests. No credentials, network or external services."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ai_agent_engine import Action, Agent, ModelTurn, SandboxFiles, ScriptedModel, Tool
from ai_agent_engine.tools import ToolError


class AgentTests(unittest.TestCase):
    def test_safe_tool_roundtrip(self):
        with TemporaryDirectory() as directory:
            sandbox = SandboxFiles(Path(directory))
            model = ScriptedModel((
                ModelTurn(action=Action("write_new_file", {"path": "a.txt", "content": "hello"})),
                ModelTurn(action=Action("read_file", {"path": "a.txt"})),
                ModelTurn(final_text="ok"),
            ))
            result = Agent(model=model, tools=sandbox.allowed_tools()).run("test")
            self.assertEqual(result.status, "done")
            self.assertEqual(result.answer, "ok")
            self.assertEqual((Path(directory) / "a.txt").read_text(), "hello")
            self.assertEqual([event.event for event in result.audit], ["tool_ok", "tool_ok", "completed"])

    def test_unknown_tool_blocked(self):
        result = Agent(model=ScriptedModel((ModelTurn(action=Action("shell", {"command": "whoami"})),)), tools=()).run("test")
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.audit[0].event, "unauthorized_tool")

    def test_step_limit_stops_loop(self):
        tool = Tool("repeat", lambda args: "ok")
        turns = (ModelTurn(action=Action("repeat", {})),) * 5
        result = Agent(model=ScriptedModel(turns), tools=(tool,), max_steps=2).run("test")
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.steps, 2)
        self.assertEqual(result.audit[-1].event, "step_limit")

    def test_tool_failure_does_not_leak_secret_in_audit(self):
        def failed(args):
            raise RuntimeError("super-secret-token")
        tool = Tool("fail", failed)
        result = Agent(model=ScriptedModel((ModelTurn(action=Action("fail", {"token": "super-secret-token"})),)), tools=(tool,)).run("test")
        self.assertEqual(result.status, "failed")
        self.assertNotIn("super-secret-token", repr(result.audit))

    def test_model_failure_sanitized(self):
        result = Agent(model=ScriptedModel(()), tools=()).run("test")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.audit[0].event, "model_error")

    def test_invalid_turn_rejected(self):
        with self.assertRaises(ValueError):
            ModelTurn()
        with self.assertRaises(ValueError):
            ModelTurn(final_text="done", action=Action("a", {}))


class SandboxTests(unittest.TestCase):
    def test_cannot_escape_root(self):
        with TemporaryDirectory() as directory:
            sandbox = SandboxFiles(Path(directory))
            for path in ("../outside.txt", "/etc/passwd", "./test.txt"):
                with self.subTest(path=path), self.assertRaises(ToolError):
                    sandbox.write_new({"path": path, "content": "wrong"})

    def test_cannot_overwrite(self):
        with TemporaryDirectory() as directory:
            sandbox = SandboxFiles(Path(directory))
            sandbox.write_new({"path": "data.txt", "content": "original"})
            with self.assertRaises(ToolError):
                sandbox.write_new({"path": "data.txt", "content": "replacement"})
            self.assertEqual((Path(directory) / "data.txt").read_text(), "original")

    def test_cannot_follow_symlink_outside_sandbox(self):
        with TemporaryDirectory() as directory, TemporaryDirectory() as external:
            (Path(directory) / "redirect").symlink_to(Path(external), target_is_directory=True)
            sandbox = SandboxFiles(Path(directory))
            with self.assertRaises(ToolError):
                sandbox.write_new({"path": "redirect/hijack.txt", "content": "wrong"})

    def test_reject_large_payload(self):
        with TemporaryDirectory() as directory:
            sandbox = SandboxFiles(Path(directory), max_bytes=4)
            with self.assertRaises(ToolError):
                sandbox.write_new({"path": "large.txt", "content": "12345"})


if __name__ == "__main__":
    unittest.main()
