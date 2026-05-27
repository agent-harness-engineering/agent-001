"""
Audit & Provenance Store for the Maestro Agent.

AuditStore is an append-only, thread-safe JSONL log.
AuditEvent captures every tool call, permission decision, and governance access.
"""

from __future__ import annotations

import json
import sys
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AuditEvent:
    """A single immutable audit record."""

    event_id: str               # uuid4
    timestamp: str              # ISO8601 UTC
    event_type: str             # "tool_call" | "permission_decision" | "validation_error" |
                                #  "governance_access" | "session_start" | "session_end"
    session_id: str
    agent_id: str
    tool_name: Optional[str]    # None when not tool-related
    tool_call_id: Optional[str]
    decision: Optional[str]     # "allow" | "deny" | None
    outcome: Optional[str]      # "success" | "error" | "blocked" | None
    details: dict               # event-specific payload


class AuditStore:
    """Append-only JSONL audit log, thread-safe."""

    def __init__(self, log_path: Path) -> None:
        self.log_path = Path(log_path)
        self._lock = threading.Lock()
        # Create parent directories if needed
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Core write
    # ------------------------------------------------------------------

    def record(self, event: AuditEvent) -> None:
        """Append *event* as a single JSON line.

        Never raises — if the write fails we print to stderr and continue
        so a storage error never brings down the orchestrator.
        """
        try:
            line = json.dumps(asdict(event), ensure_ascii=False) + "\n"
            with self._lock:
                with self.log_path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[AuditStore] write failed — event {event.event_id}: {exc}",
                file=sys.stderr,
            )

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query(
        self,
        session_id: str = None,
        event_type: str = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        """Return the most recent *limit* matching events.

        Filters are ANDed together; omitting a filter means "match all".
        Returns an empty list if the log file does not exist.
        """
        if not self.log_path.exists():
            return []

        matched: list[AuditEvent] = []
        try:
            with self._lock:
                lines = self.log_path.read_text(encoding="utf-8").splitlines()
        except Exception as exc:  # noqa: BLE001
            print(f"[AuditStore] read failed: {exc}", file=sys.stderr)
            return []

        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if session_id is not None and data.get("session_id") != session_id:
                continue
            if event_type is not None and data.get("event_type") != event_type:
                continue

            try:
                matched.append(AuditEvent(**data))
            except TypeError:
                # Schema mismatch from an older log format — skip silently
                continue

        # Return most recent `limit` events
        return matched[-limit:]

    # ------------------------------------------------------------------
    # Convenience factory
    # ------------------------------------------------------------------

    def make_callback(
        self,
        event_type: str,
        session_id: str,
        agent_id: str,
    ):
        """Return a callable(data: dict) that records an AuditEvent.

        Convenience factory for wiring into ToolProxy and PermissionGate
        without exposing AuditStore internals.

        Usage::

            cb = store.make_callback("tool_call", session_id="s1", agent_id="a1")
            cb({"tool_name": "read_file", "tool_call_id": "tc-1", "outcome": "success"})
        """

        def _callback(data: dict) -> None:
            event = AuditEvent(
                event_id=str(uuid.uuid4()),
                timestamp=_now_iso(),
                event_type=event_type,
                session_id=session_id,
                agent_id=agent_id,
                tool_name=data.get("tool_name"),
                tool_call_id=data.get("tool_call_id"),
                decision=data.get("decision"),
                outcome=data.get("outcome"),
                details={
                    k: v
                    for k, v in data.items()
                    if k not in {
                        "tool_name", "tool_call_id", "decision", "outcome",
                    }
                },
            )
            self.record(event)

        return _callback
