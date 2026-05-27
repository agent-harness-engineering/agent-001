"""
Permission policy definitions for the Maestro Agent Permission Gate.

Defines PolicyRule, PermissionPolicy, and PermissionDecision dataclasses.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class PolicyRule:
    """A single rule in a permission policy."""

    pattern: str        # glob or exact match against tool_name
    action: str         # "auto_allow" | "deny" | "escalate"
    channels: list[str] = field(default_factory=list)  # relevant when action="escalate"
    reason: str = ""    # human-readable rationale

    def matches(self, tool_name: str) -> bool:
        """Return True if this rule applies to the given tool name."""
        return fnmatch.fnmatch(tool_name, self.pattern)


@dataclass
class PermissionPolicy:
    """A collection of rules plus fallback behaviour."""

    rules: list[PolicyRule] = field(default_factory=list)
    default_action: str = "escalate"   # safe default
    timeout_seconds: int = 300

    # ------------------------------------------------------------------
    # Core matching logic
    # ------------------------------------------------------------------

    def match(self, tool_name: str) -> PolicyRule:
        """Return the first matching rule for *tool_name*.

        Falls back to a synthetic rule using *default_action* when no
        explicit rule matches.
        """
        for rule in self.rules:
            if rule.matches(tool_name):
                return rule
        # Synthetic fallback rule
        return PolicyRule(
            pattern="*",
            action=self.default_action,
            channels=["cli"],
            reason="default_action fallback",
        )

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, d: dict) -> "PermissionPolicy":
        """Load a PermissionPolicy from a JSON-serialisable dict."""
        rules = [
            PolicyRule(
                pattern=r["pattern"],
                action=r["action"],
                channels=r.get("channels", []),
                reason=r.get("reason", ""),
            )
            for r in d.get("rules", [])
        ]
        return cls(
            rules=rules,
            default_action=d.get("default_action", "escalate"),
            timeout_seconds=int(d.get("timeout_seconds", 300)),
        )

    def to_dict(self) -> dict:
        """Serialise to a JSON-serialisable dict."""
        return {
            "rules": [
                {
                    "pattern": r.pattern,
                    "action": r.action,
                    "channels": r.channels,
                    "reason": r.reason,
                }
                for r in self.rules
            ],
            "default_action": self.default_action,
            "timeout_seconds": self.timeout_seconds,
        }

    # ------------------------------------------------------------------
    # Factory: sensible defaults
    # ------------------------------------------------------------------

    @classmethod
    def default(cls) -> "PermissionPolicy":
        """Return a sensible default policy.

        * Explicit deny for destructive-sounding tools (rm, delete, drop,
          truncate, shutdown, format).
        * read_file is auto-allowed.
        * Everything else escalates for human review.
        """
        DENY_PATTERNS = [
            "rm", "rm_*", "*_rm",
            "delete", "delete_*", "*_delete",
            "drop", "drop_*", "*_drop",
            "truncate", "truncate_*", "*_truncate",
            "shutdown", "shutdown_*", "*_shutdown",
            "format", "format_*", "*_format",
        ]
        rules: list[PolicyRule] = []

        for pattern in DENY_PATTERNS:
            rules.append(
                PolicyRule(
                    pattern=pattern,
                    action="deny",
                    channels=[],
                    reason="destructive operation — auto-denied by default policy",
                )
            )

        rules.append(
            PolicyRule(
                pattern="read_file",
                action="auto_allow",
                channels=[],
                reason="read-only file access — safe to auto-allow",
            )
        )

        return cls(
            rules=rules,
            default_action="escalate",
            timeout_seconds=300,
        )


@dataclass
class PermissionDecision:
    """The outcome of a permission evaluation."""

    tool_name: str
    tool_call_id: str
    action: str         # "allow" | "deny"
    reason: str         # "auto_allow" | "user_approved" | "user_denied" | "timeout" | "policy_deny" | "no_tty"
    channel: str        # which channel produced the decision
    approver: str       # username or "system" for automated decisions
    timestamp: str      # ISO8601 UTC

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @classmethod
    def allow(
        cls,
        tool_name: str,
        tool_call_id: str,
        reason: str,
        channel: str = "system",
        approver: str = "system",
    ) -> "PermissionDecision":
        return cls(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            action="allow",
            reason=reason,
            channel=channel,
            approver=approver,
            timestamp=cls._now_iso(),
        )

    @classmethod
    def deny(
        cls,
        tool_name: str,
        tool_call_id: str,
        reason: str,
        channel: str = "system",
        approver: str = "system",
    ) -> "PermissionDecision":
        return cls(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            action="deny",
            reason=reason,
            channel=channel,
            approver=approver,
            timestamp=cls._now_iso(),
        )
