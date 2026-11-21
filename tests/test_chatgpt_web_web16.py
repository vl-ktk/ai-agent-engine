"""WEB-16 offline acceptance: shared two-project core and mock Web stack.

Exercises the production APIs together, not any real ChatGPT page or account.
The mock ports are trusted test doubles; no live policy claim is inferred.
"""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import sys
import unittest

if sys.platform.startswith("linux"):
    import test_chatgpt_web_web07 as web07
    import test_chatgpt_web_web11 as web11
    import test_chatgpt_web_web12 as web12
    import test_main_specialists as specialists
    from ai_agent_engine import (
        AgentRole, ChatGPTWebConfig, ProjectChatManager,
        SQLiteWebRotationJournal, TrustedAgentContextPolicy,
        TrustedAgentInstructionPolicy, TrustedAgentModelPolicy,
        TrustedAgentReasoningPolicy, TrustedChatGPTProjectBinding,
        TrustedChatGPTProjectRegistry, TrustedHandoffPolicy,
        TrustedMemoryPin, TrustedModelReasoningChoices,
        TrustedProjectModelCatalog, TrustedProjectReasoningCatalog,
        WebAgentIdentity, WebAgentModelRegistry, WebAgentSessionRegistry,
        WebAutomaticChatRotator, WebChatHandoff, WebContextMonitor,
        WebContextPressureError, WebInstructionBootstrap,
        checkpoint_sha256,
    )
    from ai_agent_engine.chatgpt_web_core import WebCoreTaskAdapter
    from ai_agent_engine.chatgpt_web_delivery import (
        SQLiteWebDeliveryJournal, WebIdempotentDispatcher,
    )
    from ai_agent_engine.chatgpt_web_failures import (
        GuardedBrowserPort, WebFaultCode, WebFaultGuard,
    )
    from ai_agent_engine.chatgpt_web_recovery import (
        SQLiteWebRecoveryQueue, WebRecoveryCoordinator, WebRecoveryHold,
    )
    from ai_agent_engine.main_plan import approve_plan, decompose_for_review
    from ai_agent_engine.main_shared_capacity import (
        CapacityPolicy, ProviderBudget, SharedCapacity,
    )
    from ai_agent_engine.main_specialists import (
        TrustedStepRequirements, approve_team, assign_specialist, draft_team,
    )
    from ai_agent_engine.task_api import TaskSubmission
    from ai_agent_engine.task_delivery import TaskDeliveryManager
    from ai_agent_engine.task_permissions import (
        ActionGrant, ProtectedAction, TaskPermissionGate,
    )
    from ai_agent_engine.task_request import build_task_request
    from ai_agent_engine.task_state import TaskState


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux private SQLite integration")
class Web16AcceptanceTests(unittest.TestCase):
    """One SQLite TaskInbox, two approved teams/projects, two mock ports."""

    def setUp(self):
        self.fx = specialists.SpecialistSQLiteTests(
            "test_catalog_team_approval_exact_replay_no_activation")
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.inbox = self.fx.inbox
        self.scope = specialists.OWNER
        self.calls = []
        catalog = self.fx.new_catalog((specialists.role(
            actions=["external_request"], primary_provider="chatgpt_web",
            fallback_providers=["gpt"]),), revision=2)
        self.fx.new_team(catalog, revision=2, agents=[{
            "agent_id": "agent_a", "role_id": "frontend",
            "workspace_name": "slot-one", "actions": ["external_request"],
        }])
        team_other = draft_team(self.scope, specialists.team(
            catalog.proposal, project="other", agents=[{
                "agent_id": "agent_b", "role_id": "frontend",
                "workspace_name": "slot-two", "actions": ["external_request"],
            }]), catalog, team_id="team_other")
        approve_team(self.inbox, self.scope, team_other,
                     specialists.decision(team_other))

        class OtherPlan(specialists.OnePlanModel):
            def propose(self, source, feedback, detail_hint):
                return specialists.encoded({"schema_version": 1,
                    "steps": [dict(specialists.step(), project_id=source.project_id)]})

        proposal = decompose_for_review(OtherPlan(), self.scope,
            TaskSubmission("other", "Other project", "Approved mock plan")
        ).proposal
        self.other_plan = approve_plan(self.inbox, self.scope, proposal,
                                       specialists.decision(proposal))
        self.tasks = {}
        for agent, project, team_id, plan_id in (
            ("agent_a", "engine", "team_engine", self.fx.plan.proposal.plan_id),
            ("agent_b", "other", "team_other", self.other_plan.proposal.plan_id),
        ):
            task = self.fx.task(project)
            assign_specialist(self.inbox, self.scope,
                TrustedStepRequirements(task, plan_id, "ui", ("frontend",),
                    (ProtectedAction.EXTERNAL_REQUEST,)), team_id,
                self.fx.candidate(agent, "chatgpt_web"),
                grants=(ActionGrant(task, agent, ProtectedAction.EXTERNAL_REQUEST),))
            self.tasks[agent] = task

        self.instructions = web11.Instructions(self.calls)
        self.grants = web11.Grants(self.calls)
        self.checkpoints = web11.Checkpoints(self.calls)
        self.memory = web11.Memory(self.calls)
        self.usage = web11.Usage(self.calls)
        self.efforts = web07.EffortSelector()
        self.rotation_db = str(Path(self.fx.temp.name) / "rotation.sqlite")
        self.delivery_db = Path(self.fx.temp.name) / "web13.sqlite"
        self.recovery_db = Path(self.fx.temp.name) / "web15.sqlite"
        self.ports = {}
        self.guards = {}
        self.build_runtime()

    def coords(self, agent):
        return dict(owner_id="vlady", project_id=(
            "engine" if agent == "agent_a" else "other"), agent_id=agent)

    def binding(self, agent):
        return "gpt-engine" if agent == "agent_a" else "gpt-other"

    def build_runtime(self, *, restore_rotation=False):
        self.ports.clear()
        self.guards.clear()

        def browser_factory(profile):
            browser = web12.Browser(profile)
            guard = WebFaultGuard(profile)
            self.ports[profile.agent_id] = browser
            self.guards[profile.agent_id] = guard
            return GuardedBrowserPort(profile, browser, guard)

        self.sessions = WebAgentSessionRegistry(
            ChatGPTWebConfig(enabled=True, models=("instant", "thinking")),
            browser_factory=browser_factory,
            project_registry=TrustedChatGPTProjectRegistry((
                TrustedChatGPTProjectBinding("vlady", "engine", "gpt-engine"),
                TrustedChatGPTProjectBinding("vlady", "other", "gpt-other"))),
            project_verifier=web11.Member(self.calls))
        self.chats = ProjectChatManager(self.sessions,
            chat_port_factory=lambda p: web11.ChatPort(p))
        self.monitor = WebContextMonitor(self.sessions, self.chats,
                                         usage_source=self.usage)
        self.rotation = SQLiteWebRotationJournal(self.rotation_db)
        self.handoff = WebChatHandoff(self.sessions, self.chats, self.rotation,
                        checkpoints=self.checkpoints, memory=self.memory)
        self.models = WebAgentModelRegistry(self.sessions,
            project_catalogs=(
                TrustedProjectModelCatalog("gpt-engine", ("thinking",)),
                TrustedProjectModelCatalog("gpt-other", ("instant",))),
            selector=web11.ModelSelector(self.calls),
            reasoning_catalogs=(
                TrustedProjectReasoningCatalog("gpt-engine", (
                    TrustedModelReasoningChoices("thinking", ("High",)),)),
                TrustedProjectReasoningCatalog("gpt-other", (
                    TrustedModelReasoningChoices("instant", ("Instant",)),))),
            reasoning_selector=self.efforts,
            instruction_bootstrap=WebInstructionBootstrap(
                source=self.instructions, grant_verifier=self.grants),
            context_monitor=self.monitor, handoff=self.handoff)
        self.rotator = WebAutomaticChatRotator(self.sessions, self.chats,
                                              self.rotation, self.monitor)
        for agent, model, effort in (("agent_a", "thinking", "High"),
                                     ("agent_b", "instant", "Instant")):
            coords = self.coords(agent)
            gpt_project = self.binding(agent)
            self.sessions.register(WebAgentIdentity(**coords, role=AgentRole.EXECUTOR,
                                declared_actions=("EXTERNAL_REQUEST",)))
            self.models.register(TrustedAgentModelPolicy(
                **coords, default_model_id=model, allowed_model_ids=(model,)))
            self.models.register_reasoning(TrustedAgentReasoningPolicy(
                **coords, default_reasoning_effort=effort,
                allowed_reasoning_efforts=(effort,)))
            self.models.register_instructions(TrustedAgentInstructionPolicy(
                **coords, chatgpt_project_id=gpt_project, role=AgentRole.EXECUTOR,
                revision=2, instructions_sha256=web11.digest(
                    self.instructions.content), required_actions=("EXTERNAL_REQUEST",)))
            self.models.register_context(TrustedAgentContextPolicy(
                **coords, chatgpt_project_id=gpt_project,
                context_window_tokens=10000, response_reserve_tokens=0))
            if restore_rotation:
                self.rotator.restore_agent(**coords)
            else:
                self.chats.create_initial(**coords)
                self.rotator.track_initial(**coords)
        self.gate = TaskPermissionGate(self.inbox, grants=tuple(
            ActionGrant(task, agent, ProtectedAction.EXTERNAL_REQUEST)
            for agent, task in self.tasks.items()))
        self.capacity = SharedCapacity(self.inbox, CapacityPolicy(2,
            (ProviderBudget("gpt", 2, 5, 60),)))
        self.core = WebCoreTaskAdapter(self.inbox, self.scope,
            self.sessions, self.models, self.gate, self.capacity)
        self.journal = SQLiteWebDeliveryJournal(self.delivery_db)
        self.addCleanup(self.journal.close)
        self.queue = SQLiteWebRecoveryQueue(self.recovery_db)
        self.addCleanup(self.queue.close)
        self.coordinators = {}
        for agent in self.tasks:
            dispatcher = WebIdempotentDispatcher(self.core,
                TaskDeliveryManager(self.inbox, retry_delay_seconds=0),
                self.journal, fault_guard=self.guards[agent])
            self.coordinators[agent] = WebRecoveryCoordinator(
                dispatcher, self.queue, self.checkpoints)

    def enqueue(self, agent):
        self.coordinators[agent].enqueue(task_id=self.tasks[agent],
            worker_id=agent, checkpoint_id="cp-7",
            revision=self.checkpoints.revision,
            checkpoint_sha=checkpoint_sha256(self.checkpoints.status,
                self.checkpoints.summary, self.checkpoints.next_action))

    def test_both_projects_succeed_with_independent_effort_delivery_and_ack(self):
        self.usage.used = 0
        for agent in self.tasks:
            self.enqueue(agent)
        for agent in self.tasks:
            result = self.coordinators[agent].dispatch_next(
                task_id=self.tasks[agent], worker_id=agent)
            self.assertTrue(result.executed)
            self.assertEqual(result.receipt.state, "ACKED")
            self.assertEqual(self.inbox.state(self.tasks[agent], self.scope),
                             TaskState.SUCCEEDED)
            self.assertEqual(self.coordinators[agent].inventory()[0].state,
                             "COMPLETED")
            self.assertEqual(len(self.ports[agent].turns), 1)
        self.assertEqual(self.efforts.calls,
            [("agent_a", "thinking", "High"),
             ("agent_b", "instant", "Instant")])
        self.assertNotEqual(self.sessions.profile(**self.coords("agent_a")).profile_key,
                            self.sessions.profile(**self.coords("agent_b")).profile_key)
        with sqlite3.connect(self.delivery_db) as db:
            rows = db.execute("SELECT project,agent,state FROM web13_delivery ORDER BY project").fetchall()
        self.assertEqual(rows, [("engine", "agent_a", "ACKED"),
                                ("other", "agent_b", "ACKED")])

    def rotate_and_pin(self, agent="agent_a"):
        task = self.tasks[agent]
        with self.assertRaises(WebContextPressureError):
            self.models.run(**self.coords(agent), prompt="trigger rotation")
        self.assertFalse(self.ports[agent].turns)
        receipt = self.rotator.rotate_on_pressure(**self.coords(agent),
                         task_id=task, checkpoint_id="cp-7")
        self.pin_handoff(agent)
        return receipt

    def pin_handoff(self, agent):
        task = self.tasks[agent]
        policy = TrustedHandoffPolicy(
            **self.coords(agent), chatgpt_project_id=self.binding(agent),
            task_id=task, checkpoint_id="cp-7", role=AgentRole.EXECUTOR,
            instruction_revision=2,
            instruction_sha256=web11.digest(self.instructions.content),
            state_revision=self.checkpoints.revision,
            state_sha256=checkpoint_sha256(self.checkpoints.status,
                self.checkpoints.summary, self.checkpoints.next_action),
            memory_pins=tuple(TrustedMemoryPin(i.memory_id, i.revision,
                i.body_sha256) for i in self.memory.items))
        self.models.register_handoff(policy)

    def test_rotated_chat_handoff_is_verified_before_durable_delivery(self):
        agent = "agent_a"
        task = self.tasks[agent]
        receipt = self.rotate_and_pin(agent)
        self.usage.used = 0
        self.enqueue(agent)
        with self.assertRaises(WebRecoveryHold):
            self.coordinators[agent].dispatch_next(task_id=task, worker_id=agent)
        self.assertEqual(self.coordinators[agent].inventory()[0].state, "READY")
        self.assertIsNone(self.coordinators[agent].dispatcher.delivery.snapshot(
            task, self.scope))
        result = self.coordinators[agent].dispatch_next(task_id=task,
            worker_id=agent, handoff_receipt=receipt)
        self.assertTrue(result.executed)
        self.assertEqual(result.receipt.state, "ACKED")
        self.assertEqual(self.chats.active(**self.coords(agent)).conversation_id,
                         receipt.conversation_id)
        self.assertEqual(len(self.ports[agent].turns), 1)
        payload = json.loads(self.ports[agent].turns[0][1].content)
        self.assertEqual(payload["conversation_id"], receipt.conversation_id)
        self.assertEqual(payload["task_id"], task)
        self.assertEqual(payload["agent_id"], agent)
        self.assertEqual(len(payload["memory"]), 1)
        self.assertEqual(self.ports["agent_b"].turns, [])
        with sqlite3.connect(self.rotation_db) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM web10_operations").fetchone()[0], 1)

    def test_rotated_task_recovers_with_verified_history_and_explicit_receipt(self):
        agent = "agent_a"
        task = self.tasks[agent]
        receipt = self.rotate_and_pin(agent)
        self.enqueue(agent)
        self.queue.close()
        self.journal.close()
        self.inbox.close()
        self.fx.restart()
        self.inbox = self.fx.inbox
        self.build_runtime(restore_rotation=True)
        self.pin_handoff(agent)
        self.usage.used = 0
        self.assertEqual(self.coordinators[agent].inventory()[0].state, "READY")
        with self.assertRaises(WebRecoveryHold):
            self.coordinators[agent].dispatch_next(task_id=task, worker_id=agent)
        result = self.coordinators[agent].dispatch_next(task_id=task,
            worker_id=agent, handoff_receipt=receipt)
        self.assertTrue(result.executed)
        self.assertEqual(result.receipt.state, "ACKED")
        payload = json.loads(self.ports[agent].turns[0][1].content)
        self.assertEqual(payload["conversation_id"], receipt.conversation_id)
        self.assertEqual(payload["task"]["summary"], self.checkpoints.summary)
        self.assertEqual(len(self.ports[agent].turns), 1)

    def test_parallel_agents_do_not_share_fifo_or_browser_side_effect(self):
        from threading import Event
        self.usage.used = 0
        for agent in self.tasks:
            self.enqueue(agent)
        entered, release = Event(), Event()
        self.ports["agent_a"].entered = entered
        self.ports["agent_a"].release = release
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(self.coordinators["agent_a"].dispatch_next,
                task_id=self.tasks["agent_a"], worker_id="agent_a")
            self.assertTrue(entered.wait(5), "agent_a BrowserPort did not start")
            b = self.coordinators["agent_b"].dispatch_next(
                task_id=self.tasks["agent_b"], worker_id="agent_b")
            self.assertTrue(b.executed, "agent_b stalled behind other agent")
            release.set()
            self.assertTrue(a.result(timeout=5).executed)
        self.assertEqual((len(self.ports["agent_a"].turns),
                          len(self.ports["agent_b"].turns)), (1, 1))

    def test_foreign_rotation_receipt_denied_before_delivery_ticket(self):
        self.usage.used = 7700
        with self.assertRaises(WebContextPressureError):
            self.models.run(**self.coords("agent_a"), prompt="rotate")
        receipt = self.rotator.rotate_on_pressure(**self.coords("agent_a"),
                    task_id=self.tasks["agent_a"], checkpoint_id="cp-7")
        raw = build_task_request(self.inbox, self.scope,
            self.tasks["agent_b"], role=AgentRole.EXECUTOR,
            required_actions=(ProtectedAction.EXTERNAL_REQUEST,)).to_bytes()
        with self.assertRaises(Exception):
            self.core.verify_binding(raw, task_id=self.tasks["agent_b"],
                worker_id="agent_b", handoff_receipt=receipt)
        self.assertIsNone(self.coordinators["agent_b"].dispatcher.delivery.snapshot(
            self.tasks["agent_b"], self.scope))
        self.assertEqual(self.ports["agent_a"].turns, [])
        self.assertEqual(self.ports["agent_b"].turns, [])

    def test_captcha_hold_is_durable_and_other_project_remains_runnable(self):
        from ai_agent_engine.chatgpt_web_failures import WebPortFault
        self.usage.used = 0
        for agent in self.tasks:
            self.enqueue(agent)
        original = self.ports["agent_a"].complete
        def captcha(*, model_id, messages):
            self.ports["agent_a"].turns.append(messages)
            raise WebPortFault(WebFaultCode.CAPTCHA)
        self.ports["agent_a"].complete = captcha
        with self.assertRaises(WebRecoveryHold):
            self.coordinators["agent_a"].dispatch_next(
                task_id=self.tasks["agent_a"], worker_id="agent_a")
        self.ports["agent_a"].complete = original
        self.assertEqual(self.guards["agent_a"].snapshot().code,
                         WebFaultCode.CAPTCHA)
        self.assertEqual(self.coordinators["agent_a"].inventory()[0].state,
                         "HOLD")
        result = self.coordinators["agent_b"].dispatch_next(
            task_id=self.tasks["agent_b"], worker_id="agent_b")
        self.assertTrue(result.executed)
        self.assertEqual(len(self.ports["agent_a"].turns), 1)
        self.assertEqual(len(self.ports["agent_b"].turns), 1)
        with sqlite3.connect(self.recovery_db) as db:
            self.assertEqual(db.execute("SELECT fault FROM agents WHERE agent=?",
                ("agent_a",)).fetchone(), (WebFaultCode.CAPTCHA.value,))

    def test_foreign_project_and_effort_denied_without_side_effect(self):
        self.usage.used = 0
        a, b = self.tasks["agent_a"], self.tasks["agent_b"]
        self.enqueue("agent_a")
        with self.assertRaises(WebRecoveryHold):
            self.coordinators["agent_a"].dispatch_next(
                task_id=b, worker_id="agent_a")
        with self.assertRaises(Exception):
            self.models.run(**self.coords("agent_b"), prompt="cross effort",
                            requested_reasoning_effort="High")
        with self.assertRaises(Exception):
            self.sessions.profile(owner_id="vlady", project_id="other",
                                  agent_id="agent_a")
        with self.assertRaises(Exception):
            self.models.run(**self.coords("agent_b"), prompt="cross model",
                            requested_model_id="thinking")
        self.assertEqual(self.ports["agent_a"].turns, [])
        self.assertEqual(self.ports["agent_b"].turns, [])
        self.assertIsNone(self.coordinators["agent_a"].dispatcher.delivery.snapshot(
            a, self.scope))

    def test_ambiguous_agent_hold_does_not_block_other_project_after_restart(self):
        self.usage.used = 0
        self.enqueue("agent_a")
        self.enqueue("agent_b")
        self.queue.transition(self.tasks["agent_a"], "PENDING", "IN_FLIGHT")
        self.queue.close()
        self.journal.close()
        self.inbox.close()
        self.fx.restart()
        self.inbox = self.fx.inbox
        self.build_runtime()
        a = self.coordinators["agent_a"].inventory()
        b = self.coordinators["agent_b"].inventory()
        self.assertEqual((a[0].state, b[0].state), ("HOLD", "READY"))
        self.assertEqual(self.guards["agent_a"].snapshot().code,
                         WebFaultCode.UNKNOWN_SEND)
        with self.assertRaises(WebRecoveryHold):
            self.coordinators["agent_a"].dispatch_next(
                task_id=self.tasks["agent_a"], worker_id="agent_a")
        result = self.coordinators["agent_b"].dispatch_next(
            task_id=self.tasks["agent_b"], worker_id="agent_b")
        self.assertTrue(result.executed)
        self.assertEqual(self.coordinators["agent_b"].inventory()[0].state,
                         "COMPLETED")
        self.assertEqual(self.ports["agent_a"].turns, [])
        self.assertEqual(len(self.ports["agent_b"].turns), 1)
