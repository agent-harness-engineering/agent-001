"""
Unit tests for the gates package.

All tests use stdlib unittest only.  Stdin is mocked where needed so
tests pass in non-TTY CI environments.
"""

import io
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Ensure the repo root is importable regardless of working directory
# ---------------------------------------------------------------------------
import os
import pathlib

_REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[3])  # /tmp/agent-001-build
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from gates import (
    PermissionDecision,
    PermissionGate,
    PermissionPolicy,
    PolicyRule,
)
from gates.channels.cli import CLIChannel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gate_no_channels(policy=None) -> PermissionGate:
    """Return a gate with *no* channels (fail-closed for escalations)."""
    return PermissionGate(policy=policy, channels=[])


def _gate_with_mock_channel(approved: bool, approver: str = "testuser", policy=None) -> PermissionGate:
    """Return a gate wired to a mock channel that always returns *approved*."""
    mock_channel = MagicMock()
    mock_channel.request_approval.return_value = (approved, approver)
    mock_channel.__class__.__name__ = "MockChannel"
    return PermissionGate(policy=policy, channels=[mock_channel])


# ---------------------------------------------------------------------------
# Test: auto_allow
# ---------------------------------------------------------------------------

class TestAutoAllow(unittest.TestCase):
    def setUp(self):
        self.policy = PermissionPolicy(
            rules=[PolicyRule(pattern="read_file", action="auto_allow",
                              channels=[], reason="safe read")],
            default_action="escalate",
            timeout_seconds=5,
        )

    def test_auto_allow_returns_allow_immediately(self):
        gate = _gate_no_channels(self.policy)
        decision = gate.evaluate("read_file", "call-001", {"path": "/tmp/x"})
        self.assertEqual(decision.action, "allow")
        self.assertEqual(decision.reason, "auto_allow")
        self.assertEqual(decision.approver, "system")
        self.assertEqual(decision.channel, "system")

    def test_auto_allow_does_not_invoke_any_channel(self):
        mock_channel = MagicMock()
        gate = PermissionGate(policy=self.policy, channels=[mock_channel])
        gate.evaluate("read_file", "call-002", {})
        mock_channel.request_approval.assert_not_called()

    def test_auto_allow_glob_read_star(self):
        policy = PermissionPolicy(
            rules=[PolicyRule(pattern="read_*", action="auto_allow",
                              channels=[], reason="all reads safe")],
            default_action="escalate",
            timeout_seconds=5,
        )
        gate = _gate_no_channels(policy)
        for tool in ("read_file", "read_lines", "read_config"):
            with self.subTest(tool=tool):
                d = gate.evaluate(tool, "call-x", {})
                self.assertEqual(d.action, "allow")
                self.assertEqual(d.reason, "auto_allow")


# ---------------------------------------------------------------------------
# Test: explicit deny
# ---------------------------------------------------------------------------

class TestExplicitDeny(unittest.TestCase):
    def setUp(self):
        self.policy = PermissionPolicy(
            rules=[PolicyRule(pattern="rm", action="deny",
                              channels=[], reason="destructive")],
            default_action="escalate",
            timeout_seconds=5,
        )

    def test_deny_rule_returns_deny_immediately(self):
        gate = _gate_no_channels(self.policy)
        decision = gate.evaluate("rm", "call-003", {"path": "/important"})
        self.assertEqual(decision.action, "deny")
        self.assertEqual(decision.reason, "policy_deny")
        self.assertEqual(decision.approver, "system")

    def test_deny_rule_does_not_invoke_channel(self):
        mock_channel = MagicMock()
        gate = PermissionGate(policy=self.policy, channels=[mock_channel])
        gate.evaluate("rm", "call-004", {})
        mock_channel.request_approval.assert_not_called()


# ---------------------------------------------------------------------------
# Test: default policy denies dangerous tools
# ---------------------------------------------------------------------------

class TestDefaultPolicy(unittest.TestCase):
    def setUp(self):
        self.gate = _gate_no_channels(PermissionPolicy.default())

    def _check_denied(self, tool_name):
        d = self.gate.evaluate(tool_name, "call-x", {})
        self.assertEqual(
            d.action, "deny",
            f"Expected '{tool_name}' to be denied, got {d.action!r} (reason={d.reason!r})",
        )
        self.assertEqual(d.reason, "policy_deny")

    def test_rm_denied(self):
        self._check_denied("rm")

    def test_delete_denied(self):
        self._check_denied("delete")

    def test_delete_glob_denied(self):
        self._check_denied("delete_records")

    def test_drop_denied(self):
        self._check_denied("drop")

    def test_truncate_denied(self):
        self._check_denied("truncate")

    def test_shutdown_denied(self):
        self._check_denied("shutdown")

    def test_format_denied(self):
        self._check_denied("format")

    def test_read_file_allowed(self):
        d = self.gate.evaluate("read_file", "call-y", {})
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.reason, "auto_allow")

    def test_unknown_tool_escalates_no_channels_deny(self):
        # No channels → escalation → fail-closed → deny
        d = self.gate.evaluate("unknown_tool", "call-z", {})
        self.assertEqual(d.action, "deny")
        self.assertEqual(d.reason, "timeout")


# ---------------------------------------------------------------------------
# Test: timeout
# ---------------------------------------------------------------------------

class TestTimeout(unittest.TestCase):
    def test_timeout_produces_deny(self):
        """A channel that simulates timeout returns deny with reason='timeout'."""
        mock_channel = MagicMock()
        mock_channel.request_approval.return_value = (False, "timeout")
        mock_channel.__class__.__name__ = "MockChannel"

        policy = PermissionPolicy(
            rules=[],
            default_action="escalate",
            timeout_seconds=1,
        )
        gate = PermissionGate(policy=policy, channels=[mock_channel])
        decision = gate.evaluate("some_tool", "call-timeout", {"x": 1})
        self.assertEqual(decision.action, "deny")
        self.assertEqual(decision.reason, "timeout")

    def test_short_timeout_with_mock_stdin(self):
        """Gate with CLIChannel, mocked to timeout, returns deny."""
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            # select.select will see no data → timeout path
            with patch("select.select", return_value=([], [], [])):
                channel = CLIChannel()
                approved, approver = channel.request_approval(
                    "write_file", "cid-1", {"path": "/etc/passwd"}, timeout_seconds=1
                )
        self.assertFalse(approved)
        self.assertEqual(approver, "timeout")


# ---------------------------------------------------------------------------
# Test: no_tty
# ---------------------------------------------------------------------------

class TestNoTTY(unittest.TestCase):
    def test_no_tty_produces_deny(self):
        """When stdin is not a TTY, CLIChannel returns (False, 'no_tty')."""
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = False
            channel = CLIChannel()
            approved, reason = channel.request_approval(
                "exec_shell", "cid-2", {}, timeout_seconds=5
            )
        self.assertFalse(approved)
        self.assertEqual(reason, "no_tty")

    def test_gate_no_tty_deny(self):
        """A gate whose CLIChannel reports no_tty returns deny with reason='no_tty'."""
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = False
            channel = CLIChannel()

        policy = PermissionPolicy(rules=[], default_action="escalate", timeout_seconds=5)
        gate = PermissionGate(policy=policy, channels=[channel])

        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = False
            decision = gate.evaluate("exec_shell", "cid-3", {})

        self.assertEqual(decision.action, "deny")
        self.assertEqual(decision.reason, "no_tty")

    def test_gate_no_channels_deny(self):
        """A gate with no channels at all returns deny."""
        policy = PermissionPolicy(rules=[], default_action="escalate", timeout_seconds=5)
        gate = PermissionGate(policy=policy, channels=[])
        decision = gate.evaluate("exec_shell", "cid-4", {})
        self.assertEqual(decision.action, "deny")


# ---------------------------------------------------------------------------
# Test: glob matching
# ---------------------------------------------------------------------------

class TestGlobMatching(unittest.TestCase):
    def test_read_star_matches_read_file(self):
        policy = PermissionPolicy(
            rules=[PolicyRule(pattern="read_*", action="auto_allow", channels=[], reason="")],
            default_action="escalate",
        )
        rule = policy.match("read_file")
        self.assertEqual(rule.action, "auto_allow")

    def test_read_star_does_not_match_write_file(self):
        policy = PermissionPolicy(
            rules=[PolicyRule(pattern="read_*", action="auto_allow", channels=[], reason="")],
            default_action="escalate",
        )
        rule = policy.match("write_file")
        self.assertEqual(rule.action, "escalate")  # fallback

    def test_star_delete_matches_bulk_delete(self):
        policy = PermissionPolicy(
            rules=[PolicyRule(pattern="*_delete", action="deny", channels=[], reason="")],
            default_action="escalate",
        )
        rule = policy.match("bulk_delete")
        self.assertEqual(rule.action, "deny")

    def test_first_matching_rule_wins(self):
        """When two rules match, the first one in the list wins."""
        policy = PermissionPolicy(
            rules=[
                PolicyRule(pattern="read_file", action="auto_allow", channels=[], reason="specific"),
                PolicyRule(pattern="read_*", action="deny", channels=[], reason="broad"),
            ],
            default_action="escalate",
        )
        rule = policy.match("read_file")
        self.assertEqual(rule.action, "auto_allow")
        self.assertEqual(rule.reason, "specific")

    def test_exact_match(self):
        policy = PermissionPolicy(
            rules=[PolicyRule(pattern="write_file", action="escalate",
                              channels=["cli"], reason="needs approval")],
            default_action="auto_allow",
        )
        rule = policy.match("write_file")
        self.assertEqual(rule.action, "escalate")


# ---------------------------------------------------------------------------
# Test: PermissionPolicy.from_dict round-trip
# ---------------------------------------------------------------------------

class TestFromDictRoundTrip(unittest.TestCase):
    def _make_policy(self) -> PermissionPolicy:
        return PermissionPolicy(
            rules=[
                PolicyRule(pattern="read_*", action="auto_allow",
                           channels=[], reason="read ok"),
                PolicyRule(pattern="rm", action="deny",
                           channels=[], reason="destructive"),
                PolicyRule(pattern="write_*", action="escalate",
                           channels=["cli", "web"], reason="needs human"),
            ],
            default_action="escalate",
            timeout_seconds=120,
        )

    def test_round_trip_preserves_rule_count(self):
        original = self._make_policy()
        restored = PermissionPolicy.from_dict(original.to_dict())
        self.assertEqual(len(restored.rules), len(original.rules))

    def test_round_trip_preserves_default_action(self):
        original = self._make_policy()
        restored = PermissionPolicy.from_dict(original.to_dict())
        self.assertEqual(restored.default_action, original.default_action)

    def test_round_trip_preserves_timeout(self):
        original = self._make_policy()
        restored = PermissionPolicy.from_dict(original.to_dict())
        self.assertEqual(restored.timeout_seconds, original.timeout_seconds)

    def test_round_trip_preserves_rule_fields(self):
        original = self._make_policy()
        restored = PermissionPolicy.from_dict(original.to_dict())
        for orig_rule, rest_rule in zip(original.rules, restored.rules):
            self.assertEqual(rest_rule.pattern, orig_rule.pattern)
            self.assertEqual(rest_rule.action, orig_rule.action)
            self.assertEqual(rest_rule.channels, orig_rule.channels)
            self.assertEqual(rest_rule.reason, orig_rule.reason)

    def test_from_dict_empty_rules(self):
        d = {"rules": [], "default_action": "deny", "timeout_seconds": 60}
        policy = PermissionPolicy.from_dict(d)
        self.assertEqual(policy.default_action, "deny")
        self.assertEqual(policy.timeout_seconds, 60)
        self.assertEqual(policy.rules, [])

    def test_from_dict_missing_optional_fields(self):
        """from_dict should use sensible defaults for missing optional fields."""
        d = {"rules": [{"pattern": "rm", "action": "deny"}]}
        policy = PermissionPolicy.from_dict(d)
        self.assertEqual(len(policy.rules), 1)
        self.assertEqual(policy.rules[0].channels, [])
        self.assertEqual(policy.rules[0].reason, "")
        self.assertEqual(policy.default_action, "escalate")
        self.assertEqual(policy.timeout_seconds, 300)


# ---------------------------------------------------------------------------
# Test: audit callback
# ---------------------------------------------------------------------------

class TestAuditCallback(unittest.TestCase):
    def test_audit_callback_called_on_auto_allow(self):
        log = []
        policy = PermissionPolicy(
            rules=[PolicyRule(pattern="read_file", action="auto_allow",
                              channels=[], reason="")],
            default_action="escalate",
        )
        gate = PermissionGate(policy=policy, channels=[], audit_callback=log.append)
        gate.evaluate("read_file", "cid-audit-1", {})
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0].action, "allow")

    def test_audit_callback_called_on_deny(self):
        log = []
        policy = PermissionPolicy(
            rules=[PolicyRule(pattern="rm", action="deny", channels=[], reason="")],
            default_action="escalate",
        )
        gate = PermissionGate(policy=policy, channels=[], audit_callback=log.append)
        gate.evaluate("rm", "cid-audit-2", {})
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0].action, "deny")

    def test_audit_callback_exception_does_not_propagate(self):
        def bad_callback(_):
            raise RuntimeError("audit failure")

        policy = PermissionPolicy(
            rules=[PolicyRule(pattern="read_file", action="auto_allow",
                              channels=[], reason="")],
            default_action="escalate",
        )
        gate = PermissionGate(policy=policy, channels=[], audit_callback=bad_callback)
        # Must not raise
        decision = gate.evaluate("read_file", "cid-audit-3", {})
        self.assertEqual(decision.action, "allow")


# ---------------------------------------------------------------------------
# Test: PermissionDecision timestamp
# ---------------------------------------------------------------------------

class TestDecisionTimestamp(unittest.TestCase):
    def test_timestamp_is_iso8601_utc(self):
        d = PermissionDecision.allow("read_file", "cid-ts-1", "auto_allow")
        # Must parse without error
        dt = datetime.fromisoformat(d.timestamp)
        # Must be timezone-aware
        self.assertIsNotNone(dt.tzinfo)

    def test_deny_timestamp_is_iso8601_utc(self):
        d = PermissionDecision.deny("rm", "cid-ts-2", "policy_deny")
        dt = datetime.fromisoformat(d.timestamp)
        self.assertIsNotNone(dt.tzinfo)


# ---------------------------------------------------------------------------
# Test: thread safety (smoke)
# ---------------------------------------------------------------------------

class TestThreadSafety(unittest.TestCase):
    def test_concurrent_auto_allow_evaluations(self):
        """Multiple threads calling evaluate concurrently all get correct decisions."""
        import threading

        policy = PermissionPolicy(
            rules=[PolicyRule(pattern="read_*", action="auto_allow",
                              channels=[], reason="")],
            default_action="deny",
        )
        gate = PermissionGate(policy=policy, channels=[])
        results: list[PermissionDecision] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def worker(idx: int):
            try:
                d = gate.evaluate("read_file", f"cid-{idx}", {"i": idx})
                with lock:
                    results.append(d)
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 20)
        for d in results:
            self.assertEqual(d.action, "allow")


# ---------------------------------------------------------------------------
# Test: user_approved / user_denied channel flow
# ---------------------------------------------------------------------------

class TestChannelFlow(unittest.TestCase):
    def test_user_approved(self):
        gate = _gate_with_mock_channel(approved=True, approver="alice")
        policy = PermissionPolicy(rules=[], default_action="escalate", timeout_seconds=5)
        gate = PermissionGate(
            policy=policy,
            channels=[_make_mock_channel(True, "alice")],
        )
        d = gate.evaluate("write_file", "cid-ch-1", {"path": "/tmp/out"})
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.reason, "user_approved")
        self.assertEqual(d.approver, "alice")

    def test_user_denied(self):
        policy = PermissionPolicy(rules=[], default_action="escalate", timeout_seconds=5)
        gate = PermissionGate(
            policy=policy,
            channels=[_make_mock_channel(False, "bob")],
        )
        d = gate.evaluate("write_file", "cid-ch-2", {"path": "/tmp/out"})
        self.assertEqual(d.action, "deny")
        self.assertEqual(d.reason, "user_denied")


def _make_mock_channel(approved: bool, approver: str):
    ch = MagicMock()
    ch.request_approval.return_value = (approved, approver)
    ch.__class__.__name__ = "MockChannel"
    return ch


if __name__ == "__main__":
    unittest.main()
