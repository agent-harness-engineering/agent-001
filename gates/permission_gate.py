"""
PermissionGate — core evaluation engine for the Maestro Agent P0 gate.

Thread-safe.  Fail-closed on timeout, missing channels, or any error.
"""

from __future__ import annotations

import sys
import threading
from typing import Callable, List, Optional

from .policy import PermissionDecision, PermissionPolicy
from .channels.cli import CLIChannel


class PermissionGate:
    """Evaluates tool-call permission requests against a PermissionPolicy.

    Usage::

        gate = PermissionGate(policy=PermissionPolicy.default())
        decision = gate.evaluate("read_file", "call-123", {"path": "/tmp/x"})
        if decision.action == "allow":
            ...

    The gate is **fail-closed**: any error, timeout, or ambiguity results in
    a *deny* decision.  Multiple concurrent calls to :meth:`evaluate` are
    safe; each is independent.
    """

    def __init__(
        self,
        policy: Optional[PermissionPolicy] = None,
        channels: Optional[List] = None,
        audit_callback: Optional[Callable[[PermissionDecision], None]] = None,
    ) -> None:
        """
        Parameters
        ----------
        policy:
            The :class:`PermissionPolicy` to evaluate against.
            Defaults to ``PermissionPolicy.default()``.
        channels:
            Ordered list of channel objects.  Each must implement
            ``request_approval(tool_name, tool_call_id, parameters,
            timeout_seconds) -> (bool, str)``.
            If *None*, a :class:`CLIChannel` is added when stdin is a TTY;
            otherwise the gate operates with no channels (fail-closed for
            any escalation).
        audit_callback:
            Called synchronously with every :class:`PermissionDecision`
            before it is returned.  Exceptions in the callback are caught
            and silently suppressed to prevent the gate from failing.
        """
        self._policy = policy if policy is not None else PermissionPolicy.default()
        self._audit_callback = audit_callback

        if channels is not None:
            self._channels = list(channels)
        else:
            # Auto-detect: only add CLIChannel when stdin is interactive.
            try:
                tty_available = sys.stdin.isatty()
            except Exception:
                tty_available = False
            self._channels = [CLIChannel()] if tty_available else []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(
        self, tool_name: str, tool_call_id: str, parameters: dict
    ) -> PermissionDecision:
        """Evaluate whether *tool_name* is permitted to execute.

        This method is **thread-safe** — no shared mutable state is written
        during evaluation (the channels themselves serialise their own I/O).

        Returns a :class:`PermissionDecision` with action ``"allow"`` or
        ``"deny"``.
        """
        try:
            return self._evaluate_inner(tool_name, tool_call_id, parameters)
        except Exception as exc:  # pragma: no cover — last-resort safety net
            decision = PermissionDecision.deny(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                reason="internal_error",
                channel="system",
                approver="system",
            )
            self._emit_audit(decision)
            return decision

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _evaluate_inner(
        self, tool_name: str, tool_call_id: str, parameters: dict
    ) -> PermissionDecision:
        rule = self._policy.match(tool_name)

        # ── Auto-allow ──────────────────────────────────────────────────
        if rule.action == "auto_allow":
            decision = PermissionDecision.allow(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                reason="auto_allow",
                channel="system",
                approver="system",
            )
            self._emit_audit(decision)
            return decision

        # ── Explicit deny ────────────────────────────────────────────────
        if rule.action == "deny":
            decision = PermissionDecision.deny(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                reason="policy_deny",
                channel="system",
                approver="system",
            )
            self._emit_audit(decision)
            return decision

        # ── Escalate to channels ─────────────────────────────────────────
        # rule.action == "escalate" (or any unexpected value — fail-closed)
        timeout = self._policy.timeout_seconds
        for channel in self._channels:
            try:
                approved, approver_or_reason = channel.request_approval(
                    tool_name, tool_call_id, parameters, timeout
                )
            except Exception:
                # Channel error → fail-closed, try next channel
                continue

            if approver_or_reason == "no_tty":
                # This channel cannot operate; try the next one.
                decision = PermissionDecision.deny(
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    reason="no_tty",
                    channel=type(channel).__name__,
                    approver="system",
                )
                self._emit_audit(decision)
                return decision

            if approved:
                decision = PermissionDecision.allow(
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    reason="user_approved",
                    channel=type(channel).__name__,
                    approver=approver_or_reason,
                )
                self._emit_audit(decision)
                return decision

            # Operator said no, or timeout from this channel.
            reason = "timeout" if approver_or_reason == "timeout" else "user_denied"
            decision = PermissionDecision.deny(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                reason=reason,
                channel=type(channel).__name__,
                approver=approver_or_reason if not approved else "system",
            )
            self._emit_audit(decision)
            return decision

        # No channels available or all skipped → fail-closed.
        decision = PermissionDecision.deny(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            reason="timeout",
            channel="system",
            approver="system",
        )
        self._emit_audit(decision)
        return decision

    def _emit_audit(self, decision: PermissionDecision) -> None:
        if self._audit_callback is None:
            return
        try:
            self._audit_callback(decision)
        except Exception:
            pass  # Audit failures must never propagate
