"""Offline tests for shared executor/supervisor sessions; no provider credentials."""

import unittest

from ai_agent_engine import (
    Action, AgentRole, AgentSession, ModelTurn, SessionContext, StopToken, Tool,
)


class RecordingModel:
    def __init__(self, *turns):
        self.turns = iter(turns)
        self.histories = []
        self.allowed = []

    def complete(self, history, allowed_tools):
        self.histories.append(history)
        self.allowed.append(allowed_tools)
        return next(self.turns)


class ContextTests(unittest.TestCase):
    def test_executor_and_supervisor_share_one_runtime_with_separate_contexts(self):
        cases = (
            ("alpha", "worker-1", AgentRole.EXECUTOR, "Implement one task"),
            ("beta", "lead-1", AgentRole.SUPERVISOR, "Review the plan"),
        )
        for project, name, role, instructions in cases:
            with self.subTest(role=role):
                model = RecordingModel(ModelTurn(final_text="ok"))
                context = SessionContext(project, name, role, instructions)
                result = AgentSession(context=context, model=model, tools=()).run("work")
                self.assertEqual(result.status, "done")
                self.assertEqual(result.answer, "ok")
                self.assertEqual(len(model.histories), 1)
                history = model.histories[0]
                self.assertEqual([m.role for m in history], ["system", "user"])
                self.assertIn("Project: " + project, history[0].content)
                self.assertIn("Agent role: " + role.value, history[0].content)
                self.assertIn(instructions, history[0].content)
                self.assertEqual(history[1].content, "work")
                self.assertEqual(model.allowed, [()])

    def test_context_isolation_does_not_reuse_previous_task_history(self):
        first = RecordingModel(ModelTurn(final_text="first"))
        second = RecordingModel(ModelTurn(final_text="second"))
        AgentSession(context=SessionContext("project-one", "agent-one", AgentRole.EXECUTOR),
                     model=first, tools=()).run("secret task")
        AgentSession(context=SessionContext("project-two", "agent-two", AgentRole.SUPERVISOR),
                     model=second, tools=()).run("independent task")
        self.assertNotIn("project-one", repr(second.histories))
        self.assertNotIn("secret task", repr(second.histories))

    def test_role_never_grants_additional_tools(self):
        model = RecordingModel(ModelTurn(action=Action("shell", {"command": "echo no"})))
        supervisor = AgentSession(
            context=SessionContext("project", "lead", AgentRole.SUPERVISOR),
            model=model, tools=(),
        )
        result = supervisor.run("attempt shell")
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.audit[0].event, "unauthorized_tool")

    def test_invalid_owner_context_rejected(self):
        for project in ("", "../other", "with spaces", "a\nchange", "x" * 129):
            with self.subTest(project=project), self.assertRaises(ValueError):
                SessionContext(project, "agent", AgentRole.EXECUTOR)
        with self.assertRaises(ValueError):
            SessionContext("project", "agent", "administrator")
        with self.assertRaises(ValueError):
            SessionContext("project", "agent", AgentRole.EXECUTOR, "x" * 8001)

    def test_session_runs_one_task_only(self):
        model = RecordingModel(ModelTurn(final_text="done"))
        session = AgentSession(context=SessionContext("project", "worker", AgentRole.EXECUTOR),
                               model=model, tools=())
        self.assertEqual(session.run("first").status, "done")
        with self.assertRaises(RuntimeError):
            session.run("second")
        self.assertEqual(len(model.histories), 1)


class CancellationTests(unittest.TestCase):
    def test_stop_before_start_never_calls_model(self):
        model = RecordingModel(ModelTurn(final_text="should not run"))
        stop = StopToken()
        stop.request_stop()
        session = AgentSession(context=SessionContext("project", "worker", AgentRole.EXECUTOR),
                               model=model, tools=())
        result = session.run("task", stop=stop)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.steps, 0)
        self.assertEqual(model.histories, [])

    def test_stop_during_model_call_blocks_unsafe_followup_tool(self):
        stop = StopToken()
        executed = []

        class StoppingModel:
            def complete(self, history, allowed_tools):
                stop.request_stop()
                return ModelTurn(action=Action("write", {}))

        session = AgentSession(context=SessionContext("project", "worker", AgentRole.EXECUTOR),
                               model=StoppingModel(),
                               tools=(Tool("write", lambda args: executed.append(True) or "ok"),))
        result = session.run("task", stop=stop)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(executed, [])

    def test_stop_after_tool_prevents_next_model_turn(self):
        stop = StopToken()
        model = RecordingModel(
            ModelTurn(action=Action("step", {})),
            ModelTurn(final_text="should not be called"),
        )

        def step(args):
            stop.request_stop()
            return "ok"

        session = AgentSession(context=SessionContext("project", "worker", AgentRole.EXECUTOR),
                               model=model, tools=(Tool("step", step),))
        result = session.run("task", stop=stop)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual([e.event for e in result.audit], ["tool_ok", "cancelled"])
        self.assertEqual(len(model.histories), 1)

    def test_tool_failure_remains_redacted_with_role_context(self):
        def fail(args):
            raise RuntimeError("secret-session-token")

        model = RecordingModel(ModelTurn(action=Action("fail", {})))
        session = AgentSession(context=SessionContext("project", "worker", AgentRole.EXECUTOR),
                               model=model, tools=(Tool("fail", fail),))
        result = session.run("task")
        self.assertEqual(result.status, "failed")
        self.assertNotIn("secret-session-token", repr(result.audit))


if __name__ == "__main__":
    unittest.main()
