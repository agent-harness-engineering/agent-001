"""Integration tests for the Maestro Agent P0 safety layer.

These tests wire all four P0 components (ToolProxy, PermissionGate,
AuditStore, GovernanceGuard) together end-to-end — both via direct
component calls and via the aiohttp HTTP endpoints.

Run with:  python3 -m pytest tests/test_p0_integration.py -v
"""

from __future__ import annotations

import json
import sys
import uuid
import asyncio
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path for clean imports
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parents[1]))

from audit import AuditStore, AuditEvent
from gates import PermissionGate, PermissionPolicy, PermissionDecision, PolicyRule
from governance import GovernanceGuard
from proxy import ToolProxy, CanonicalToolCall, ValidationError


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

TOOL_REGISTRY = {
    "read_file": {
        "schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        }
    },
    "write_file": {
        "schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        }
    },
    "list_dir": {
        "schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        }
    },
    "delete": {
        "schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        }
    },
}


def _make_allow_policy() -> PermissionPolicy:
    """Policy that auto-allows read_file and list_dir, denies delete."""
    return PermissionPolicy(
        rules=[
            PolicyRule(pattern="read_file", action="auto_allow", reason="read-only"),
            PolicyRule(pattern="list_dir", action="auto_allow", reason="read-only"),
            PolicyRule(pattern="delete", action="deny", reason="destructive"),
            PolicyRule(pattern="write_file", action="auto_allow", reason="test allow"),
        ],
        default_action="deny",
    )


# ---------------------------------------------------------------------------
# Test 1: Full pipeline — proxy → gate (auto_allow) → audit → response
# ---------------------------------------------------------------------------

class TestPipelineAutoAllow(unittest.TestCase):
    """model output → proxy → gate (auto_allow) → audit → approved."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.audit_log = Path(self.tmp.name) / "audit.jsonl"
        self.audit_store = AuditStore(self.audit_log)
        self.policy = _make_allow_policy()
        self.gate = PermissionGate(policy=self.policy, channels=[])
        self.proxy = ToolProxy(tool_registry=TOOL_REGISTRY)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_file_passes_through(self):
        """read_file call: proxy validates, gate auto-allows, audit records."""
        model_output = {
            "tool_calls": [
                {"function": {"name": "read_file", "arguments": '{"path": "/tmp/foo.txt"}'}}
            ]
        }
        # Proxy
        proxy_results = self.proxy.process(model_output, source_hint="openai")
        self.assertEqual(len(proxy_results), 1)
        call = proxy_results[0]
        self.assertIsInstance(call, CanonicalToolCall)
        self.assertEqual(call.tool_name, "read_file")

        # Gate
        decision = self.gate.evaluate(call.tool_name, call.id, call.parameters)
        self.assertEqual(decision.action, "allow")
        self.assertEqual(decision.reason, "auto_allow")

        # Audit
        self.audit_store.record(AuditEvent(
            event_id=str(uuid.uuid4()),
            timestamp="2026-04-29T00:00:00+00:00",
            event_type="tool_call",
            session_id="test-session",
            agent_id="agent-001",
            tool_name=call.tool_name,
            tool_call_id=call.id,
            decision=decision.action,
            outcome="success",
            details={"source": call.source},
        ))
        events = self.audit_store.query(event_type="tool_call")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].tool_name, "read_file")
        self.assertEqual(events[0].decision, "allow")

    def test_list_dir_passes_through(self):
        model_output = {
            "content": [
                {"type": "tool_use", "name": "list_dir", "input": {"path": "/tmp"}}
            ]
        }
        proxy_results = self.proxy.process(model_output, source_hint="anthropic")
        self.assertEqual(len(proxy_results), 1)
        call = proxy_results[0]
        self.assertIsInstance(call, CanonicalToolCall)

        decision = self.gate.evaluate(call.tool_name, call.id, call.parameters)
        self.assertEqual(decision.action, "allow")


# ---------------------------------------------------------------------------
# Test 2: Full pipeline — proxy → gate (deny) → audit → blocked
# ---------------------------------------------------------------------------

class TestPipelineDeny(unittest.TestCase):
    """model output → proxy → gate (deny) → audit → blocked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.audit_log = Path(self.tmp.name) / "audit.jsonl"
        self.audit_store = AuditStore(self.audit_log)
        self.policy = _make_allow_policy()
        self.gate = PermissionGate(policy=self.policy, channels=[])
        self.proxy = ToolProxy(tool_registry=TOOL_REGISTRY)

    def tearDown(self):
        self.tmp.cleanup()

    def test_delete_is_blocked_by_gate(self):
        """delete call: proxy validates OK, gate denies, audit records blocked."""
        model_output = {
            "tool_calls": [
                {"function": {"name": "delete", "arguments": '{"path": "/tmp/bar.txt"}'}}
            ]
        }
        proxy_results = self.proxy.process(model_output, source_hint="openai")
        self.assertEqual(len(proxy_results), 1)
        call = proxy_results[0]
        self.assertIsInstance(call, CanonicalToolCall)

        decision = self.gate.evaluate(call.tool_name, call.id, call.parameters)
        self.assertEqual(decision.action, "deny")
        self.assertEqual(decision.reason, "policy_deny")

        # Audit the blocked call
        self.audit_store.record(AuditEvent(
            event_id=str(uuid.uuid4()),
            timestamp="2026-04-29T00:00:00+00:00",
            event_type="tool_call",
            session_id="test-session",
            agent_id="agent-001",
            tool_name=call.tool_name,
            tool_call_id=call.id,
            decision="deny",
            outcome="blocked",
            details={"reason": decision.reason},
        ))
        events = self.audit_store.query(event_type="tool_call")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].decision, "deny")
        self.assertEqual(events[0].outcome, "blocked")

    def test_unregistered_policy_tool_denied_by_default(self):
        """A tool not in policy rules falls through to default_action=deny.

        With default_action="deny" and no matching rules the gate returns
        "policy_deny" via the synthetic fallback rule — not "timeout", which
        is only emitted when escalation times out waiting for a channel.
        """
        custom_policy = PermissionPolicy(rules=[], default_action="deny")
        gate = PermissionGate(policy=custom_policy, channels=[])
        model_output = {
            "tool_calls": [
                {"function": {"name": "read_file", "arguments": '{"path": "/etc/hosts"}'}}
            ]
        }
        proxy_results = self.proxy.process(model_output, source_hint="openai")
        call = proxy_results[0]
        self.assertIsInstance(call, CanonicalToolCall)

        decision = gate.evaluate(call.tool_name, call.id, call.parameters)
        self.assertEqual(decision.action, "deny")
        # default_action="deny" matches the explicit deny branch → "policy_deny"
        self.assertEqual(decision.reason, "policy_deny")


# ---------------------------------------------------------------------------
# Test 3: Unknown tool blocked by proxy, never reaches gate
# ---------------------------------------------------------------------------

class TestProxyBlocksUnknownTool(unittest.TestCase):
    """Unknown tool: proxy blocks it; gate is never called."""

    def setUp(self):
        self.proxy = ToolProxy(tool_registry=TOOL_REGISTRY)
        self.gate_calls = []

        def counting_gate(*args, **kwargs):
            self.gate_calls.append(args)

        self.counting_gate = counting_gate

    def test_unknown_tool_becomes_validation_error(self):
        model_output = {
            "tool_calls": [
                {"function": {"name": "launch_missile", "arguments": "{}"}}
            ]
        }
        results = self.proxy.process(model_output, source_hint="openai")
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], ValidationError)
        self.assertEqual(results[0].error_type, "unknown_tool")
        self.assertIn("launch_missile", results[0].message)
        # Gate was never called
        self.assertEqual(len(self.gate_calls), 0)

    def test_unknown_tool_via_anthropic_format(self):
        model_output = {
            "content": [
                {"type": "tool_use", "name": "hack_system", "input": {"target": "prod"}}
            ]
        }
        results = self.proxy.process(model_output, source_hint="anthropic")
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], ValidationError)
        self.assertEqual(results[0].error_type, "unknown_tool")

    def test_known_and_unknown_tool_mixed(self):
        """A batch with one known and one unknown — proxy processes both independently."""
        model_output = {
            "tool_calls": [
                {"function": {"name": "read_file", "arguments": '{"path": "/tmp/ok.txt"}'}},
                {"function": {"name": "mystery_tool", "arguments": "{}"}},
            ]
        }
        results = self.proxy.process(model_output, source_hint="openai")
        self.assertEqual(len(results), 2)
        self.assertIsInstance(results[0], CanonicalToolCall)
        self.assertIsInstance(results[1], ValidationError)


# ---------------------------------------------------------------------------
# Test 4: GovernanceGuard blocks write to mission.md
# ---------------------------------------------------------------------------

class TestGovernanceGuardBlocksWrite(unittest.TestCase):
    """GovernanceGuard must block writes to 4M governance files."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # Create fake governance files so realpath can resolve them
        repo_root = Path(self.tmp.name)
        (repo_root / "mission.md").write_text("# mission")
        (repo_root / "mind.md").write_text("# mind")
        (repo_root / "morals.md").write_text("# morals")
        (repo_root / "memory_module.md").write_text("# memory")
        (repo_root / "gates").mkdir()
        (repo_root / "governance").mkdir()
        (repo_root / "audit").mkdir()
        self.repo_root = repo_root
        self.audit_log = repo_root / "test-audit.jsonl"
        self.audit_store = AuditStore(self.audit_log)
        self.guard = GovernanceGuard(repo_root=repo_root, audit_store=self.audit_store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_to_mission_md_is_blocked(self):
        target = self.repo_root / "mission.md"
        result = self.guard.check_write(target, session_id="test-session")
        self.assertFalse(result, "mission.md write should be blocked")

    def test_write_to_mind_md_is_blocked(self):
        target = self.repo_root / "mind.md"
        self.assertFalse(self.guard.check_write(target))

    def test_write_to_morals_md_is_blocked(self):
        target = self.repo_root / "morals.md"
        self.assertFalse(self.guard.check_write(target))

    def test_write_to_gates_dir_is_blocked(self):
        target = self.repo_root / "gates" / "policy.py"
        self.assertFalse(self.guard.check_write(target))

    def test_write_to_governance_dir_is_blocked(self):
        target = self.repo_root / "governance" / "self_exemption.py"
        self.assertFalse(self.guard.check_write(target))

    def test_write_to_safe_path_is_permitted(self):
        target = self.repo_root / "output" / "result.txt"
        self.assertTrue(self.guard.check_write(target))

    def test_governance_block_is_audited(self):
        target = self.repo_root / "mission.md"
        self.guard.check_write(target, session_id="audit-test-session")
        events = self.audit_store.query(event_type="governance_access")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].decision, "deny")
        self.assertEqual(events[0].outcome, "blocked")

    def test_pipeline_write_to_mission_blocked_end_to_end(self):
        """Full end-to-end: proxy validates write_file, guard blocks it."""
        proxy = ToolProxy(tool_registry=TOOL_REGISTRY)
        model_output = {
            "tool_calls": [
                {
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({
                            "path": str(self.repo_root / "mission.md"),
                            "content": "malicious override",
                        }),
                    }
                }
            ]
        }
        results = proxy.process(model_output, source_hint="openai")
        self.assertEqual(len(results), 1)
        call = results[0]
        self.assertIsInstance(call, CanonicalToolCall)

        # Guard blocks the write
        permitted = self.guard.check_write(
            call.parameters["path"], session_id="e2e-test"
        )
        self.assertFalse(permitted)


# ---------------------------------------------------------------------------
# Test 5: /v1/health returns 200 with all components ok
# ---------------------------------------------------------------------------

class TestV1HealthEndpoint(unittest.IsolatedAsyncioTestCase):
    """HTTP /v1/health endpoint returns 200 with all components ok."""

    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient, TestServer
        import server  # noqa: F401 — to import create_app

        # Patch environment to use a temp audit log
        self._tmp = tempfile.TemporaryDirectory()
        import server as srv
        # Temporarily override AUDIT_LOG_PATH
        self._orig_path = srv.AUDIT_LOG_PATH
        srv.AUDIT_LOG_PATH = Path(self._tmp.name) / "test-audit.jsonl"

        self._app = srv.create_app()
        self._server = TestServer(self._app)
        self._client = TestClient(self._server)
        await self._client.start_server()

    async def asyncTearDown(self):
        import server as srv
        srv.AUDIT_LOG_PATH = self._orig_path
        await self._client.close()
        self._tmp.cleanup()

    async def test_v1_health_returns_200(self):
        resp = await self._client.get("/v1/health")
        self.assertEqual(resp.status, 200)

    async def test_v1_health_all_components_ok(self):
        resp = await self._client.get("/v1/health")
        data = await resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("components", data)
        for component in ("proxy", "gate", "audit", "governance"):
            self.assertIn(component, data["components"])
            self.assertEqual(data["components"][component], "ok",
                             f"Component '{component}' should be 'ok'")


# ---------------------------------------------------------------------------
# Test 6: /v1/process pipeline via HTTP
# ---------------------------------------------------------------------------

class TestV1ProcessEndpoint(unittest.IsolatedAsyncioTestCase):
    """HTTP /v1/process end-to-end pipeline tests."""

    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient, TestServer
        import server as srv

        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path = srv.AUDIT_LOG_PATH
        srv.AUDIT_LOG_PATH = Path(self._tmp.name) / "test-audit.jsonl"

        # Inject a test tool registry so we have known tools
        self._orig_tool_registry_loader = None

        self._app = srv.create_app()
        self._server = TestServer(self._app)
        self._client = TestClient(self._server)
        await self._client.start_server()

        # Inject tool registry and permissive policy into p0 after startup
        p0 = self._app["p0"]
        # Replace tool_proxy with one that knows our test tools
        p0["tool_proxy"] = ToolProxy(tool_registry=TOOL_REGISTRY)
        # Replace permission_gate with a permissive one (auto-allows read_file)
        p0["permission_gate"] = PermissionGate(
            policy=_make_allow_policy(),
            channels=[],
        )

    async def asyncTearDown(self):
        import server as srv
        srv.AUDIT_LOG_PATH = self._orig_path
        await self._client.close()
        self._tmp.cleanup()

    async def test_process_valid_tool_approved(self):
        """read_file call is approved by default policy."""
        payload = {
            "session_id": "http-test-session",
            "model_output": {
                "tool_calls": [
                    {"function": {"name": "read_file", "arguments": '{"path": "/tmp/x.txt"}'}}
                ]
            },
            "model_format": "openai",
            "agent_id": "test-agent",
        }
        resp = await self._client.post("/v1/process", json=payload)
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertIn("tool_calls", data)
        self.assertIn("blocked", data)
        self.assertIn("decisions", data)
        self.assertEqual(len(data["tool_calls"]), 1)
        self.assertEqual(len(data["blocked"]), 0)
        self.assertEqual(data["tool_calls"][0]["tool_name"], "read_file")

    async def test_process_unknown_tool_blocked(self):
        """Unknown tool never reaches gate; goes to blocked."""
        payload = {
            "session_id": "http-test-session",
            "model_output": {
                "tool_calls": [
                    {"function": {"name": "unknown_tool_xyz", "arguments": "{}"}}
                ]
            },
            "model_format": "openai",
            "agent_id": "test-agent",
        }
        resp = await self._client.post("/v1/process", json=payload)
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(len(data["tool_calls"]), 0)
        self.assertEqual(len(data["blocked"]), 1)
        self.assertEqual(data["blocked"][0]["type"], "validation_error")
        self.assertEqual(data["blocked"][0]["error_type"], "unknown_tool")

    async def test_process_denied_tool_in_blocked(self):
        """delete tool is denied by policy; shows up in blocked."""
        payload = {
            "session_id": "http-test-session",
            "model_output": {
                "tool_calls": [
                    {"function": {"name": "delete", "arguments": '{"path": "/tmp/gone.txt"}'}}
                ]
            },
            "model_format": "openai",
            "agent_id": "test-agent",
        }
        resp = await self._client.post("/v1/process", json=payload)
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(len(data["tool_calls"]), 0)
        self.assertEqual(len(data["blocked"]), 1)
        self.assertEqual(data["blocked"][0]["type"], "permission_denied")

    async def test_process_missing_model_output_returns_400(self):
        """Missing model_output returns 400."""
        resp = await self._client.post("/v1/process", json={"session_id": "x"})
        self.assertEqual(resp.status, 400)

    async def test_process_invalid_json_returns_400(self):
        """Non-JSON body returns 400."""
        resp = await self._client.post(
            "/v1/process",
            data="not json",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status, 400)


# ---------------------------------------------------------------------------
# Test 7: /v1/audit returns events after recording
# ---------------------------------------------------------------------------

class TestV1AuditEndpoint(unittest.IsolatedAsyncioTestCase):
    """HTTP /v1/audit returns events that were recorded."""

    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient, TestServer
        import server as srv

        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path = srv.AUDIT_LOG_PATH
        srv.AUDIT_LOG_PATH = Path(self._tmp.name) / "test-audit.jsonl"

        self._app = srv.create_app()
        self._server = TestServer(self._app)
        self._client = TestClient(self._server)
        await self._client.start_server()

        # Inject test tool registry and policy
        p0 = self._app["p0"]
        p0["tool_proxy"] = ToolProxy(tool_registry=TOOL_REGISTRY)
        p0["permission_gate"] = PermissionGate(
            policy=_make_allow_policy(),
            channels=[],
        )

    async def asyncTearDown(self):
        import server as srv
        srv.AUDIT_LOG_PATH = self._orig_path
        await self._client.close()
        self._tmp.cleanup()

    async def test_audit_empty_initially(self):
        """/v1/audit returns empty events list before any process calls."""
        resp = await self._client.get("/v1/audit")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertIn("events", data)
        # May have startup events from system, just check structure
        self.assertIsInstance(data["events"], list)

    async def test_audit_has_events_after_process(self):
        """After a /v1/process call, /v1/audit returns recorded events."""
        session_id = f"audit-e2e-{uuid.uuid4().hex[:8]}"
        process_payload = {
            "session_id": session_id,
            "model_output": {
                "tool_calls": [
                    {"function": {"name": "read_file", "arguments": '{"path": "/tmp/y.txt"}'}}
                ]
            },
            "model_format": "openai",
            "agent_id": "test-agent",
        }
        proc_resp = await self._client.post("/v1/process", json=process_payload)
        self.assertEqual(proc_resp.status, 200)

        audit_resp = await self._client.get(f"/v1/audit?session_id={session_id}")
        self.assertEqual(audit_resp.status, 200)
        data = await audit_resp.json()
        self.assertGreater(len(data["events"]), 0)
        tool_calls = [e for e in data["events"] if e.get("event_type") == "tool_call"]
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0]["tool_name"], "read_file")
        self.assertEqual(tool_calls[0]["session_id"], session_id)

    async def test_audit_filter_by_event_type(self):
        """event_type filter returns only matching events."""
        session_id = f"audit-filter-{uuid.uuid4().hex[:8]}"
        process_payload = {
            "session_id": session_id,
            "model_output": {
                "tool_calls": [
                    {"function": {"name": "read_file", "arguments": '{"path": "/tmp/z.txt"}'}},
                    {"function": {"name": "unknown_xyz", "arguments": "{}"}},
                ]
            },
            "model_format": "openai",
            "agent_id": "test-agent",
        }
        await self._client.post("/v1/process", json=process_payload)

        # Filter to only validation_error events
        resp = await self._client.get(
            f"/v1/audit?session_id={session_id}&event_type=validation_error"
        )
        data = await resp.json()
        for event in data["events"]:
            self.assertEqual(event["event_type"], "validation_error")

    async def test_audit_limit_parameter(self):
        """limit parameter caps returned events."""
        resp = await self._client.get("/v1/audit?limit=2")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertLessEqual(len(data["events"]), 2)


# ---------------------------------------------------------------------------
# Test 8: AuditStore direct integration with ToolProxy callbacks
# ---------------------------------------------------------------------------

class TestProxyAuditIntegration(unittest.TestCase):
    """ToolProxy audit_callback wired directly to AuditStore."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.audit_store = AuditStore(Path(self.tmp.name) / "audit.jsonl")

        def audit_cb(result):
            if isinstance(result, CanonicalToolCall):
                self.audit_store.record(AuditEvent(
                    event_id=str(uuid.uuid4()),
                    timestamp="2026-04-29T00:00:00+00:00",
                    event_type="tool_call",
                    session_id="s1",
                    agent_id="a1",
                    tool_name=result.tool_name,
                    tool_call_id=result.id,
                    decision=None,
                    outcome="parsed",
                    details={},
                ))
            else:
                self.audit_store.record(AuditEvent(
                    event_id=str(uuid.uuid4()),
                    timestamp="2026-04-29T00:00:00+00:00",
                    event_type="validation_error",
                    session_id="s1",
                    agent_id="a1",
                    tool_name=None,
                    tool_call_id=None,
                    decision="deny",
                    outcome="blocked",
                    details={"error_type": result.error_type},
                ))

        self.proxy = ToolProxy(tool_registry=TOOL_REGISTRY, audit_callback=audit_cb)

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_call_recorded_to_audit(self):
        self.proxy.process(
            {"tool_calls": [{"function": {"name": "read_file", "arguments": '{"path": "/x"}'}}]},
            source_hint="openai",
        )
        events = self.audit_store.query(event_type="tool_call")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].tool_name, "read_file")

    def test_invalid_call_recorded_as_validation_error(self):
        self.proxy.process(
            {"tool_calls": [{"function": {"name": "fly_to_moon", "arguments": "{}"}}]},
            source_hint="openai",
        )
        events = self.audit_store.query(event_type="validation_error")
        self.assertEqual(len(events), 1)

    def test_mixed_batch_both_event_types_recorded(self):
        self.proxy.process(
            {
                "tool_calls": [
                    {"function": {"name": "read_file", "arguments": '{"path": "/ok.txt"}'}},
                    {"function": {"name": "nonexistent_tool", "arguments": "{}"}},
                ]
            },
            source_hint="openai",
        )
        all_events = self.audit_store.query()
        self.assertEqual(len(all_events), 2)


# ---------------------------------------------------------------------------
# Test 9: PermissionGate audit callback wired to AuditStore
# ---------------------------------------------------------------------------

class TestGateAuditIntegration(unittest.TestCase):
    """PermissionGate audit_callback wired directly to AuditStore."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.audit_store = AuditStore(Path(self.tmp.name) / "audit.jsonl")

        def gate_audit_cb(decision: PermissionDecision):
            self.audit_store.record(AuditEvent(
                event_id=str(uuid.uuid4()),
                timestamp="2026-04-29T00:00:00+00:00",
                event_type="permission_decision",
                session_id="s1",
                agent_id="a1",
                tool_name=decision.tool_name,
                tool_call_id=decision.tool_call_id,
                decision=decision.action,
                outcome="allow" if decision.action == "allow" else "blocked",
                details={"reason": decision.reason},
            ))

        self.gate = PermissionGate(
            policy=_make_allow_policy(),
            channels=[],
            audit_callback=gate_audit_cb,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_allow_decision_recorded(self):
        self.gate.evaluate("read_file", str(uuid.uuid4()), {"path": "/x"})
        events = self.audit_store.query(event_type="permission_decision")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].decision, "allow")

    def test_deny_decision_recorded(self):
        self.gate.evaluate("delete", str(uuid.uuid4()), {"path": "/x"})
        events = self.audit_store.query(event_type="permission_decision")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].decision, "deny")

    def test_multiple_decisions_all_recorded(self):
        for tool in ["read_file", "delete", "list_dir"]:
            self.gate.evaluate(tool, str(uuid.uuid4()), {"path": "/x"})
        events = self.audit_store.query(event_type="permission_decision")
        self.assertEqual(len(events), 3)


if __name__ == "__main__":
    unittest.main()
