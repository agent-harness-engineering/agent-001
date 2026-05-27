"""
Self-Exemption Prevention for the Maestro Agent.

GovernanceGuard ensures the agent cannot modify its own governance
configuration files — 4M modules, gates, audit, or governance packages.

Path resolution uses os.path.realpath (follows symlinks) so traversal
attacks via symlinks are defeated before any comparison.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Union

if TYPE_CHECKING:
    from audit.store import AuditStore


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class GovernanceGuard:
    """Prevents agent from modifying its own governance configuration.

    Protected paths are evaluated relative to *repo_root* and are resolved
    to absolute real paths before every comparison so that symlink traversal
    cannot bypass protection.
    """

    # Files / directories the agent must NOT be able to write.
    # Trailing "/" denotes a directory (everything under it is protected).
    PROTECTED: list[str] = [
        "mission.md",
        "mind.md",
        "morals.md",
        "memory_module.md",
        "gates/",
        "governance/",
        "audit/",
    ]

    def __init__(
        self,
        repo_root: Union[str, Path],
        audit_store: Optional["AuditStore"] = None,
    ) -> None:
        self.repo_root = Path(os.path.realpath(repo_root))
        self.audit_store = audit_store

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_write(
        self,
        target_path: Union[str, Path],
        session_id: str = "unknown",
    ) -> bool:
        """Return True if the write is permitted, False if it is blocked.

        Blocked attempts are logged to *audit_store* (if provided) with
        event_type "governance_access" and decision "deny".
        """
        if self.is_governance_path(target_path):
            self._log_denial(target_path, session_id)
            return False
        return True

    def is_governance_path(self, path: Union[str, Path]) -> bool:
        """Return True if *path* resolves to a protected governance location.

        Resolution strategy:
        1. If the path already exists on disk, use os.path.realpath.
        2. If it does not exist, normalise without symlink resolution
           (realpath on a non-existent path still resolves existing prefixes,
           which is sufficient to defeat most traversal attempts).
        """
        try:
            resolved = Path(os.path.realpath(path))
        except (OSError, ValueError):
            resolved = Path(os.path.normpath(os.path.abspath(path)))

        for protected in self.get_protected_paths():
            # Directory: check if resolved is protected_dir itself OR a child
            if str(protected).endswith("/") or protected.is_dir():
                protected_dir = protected if str(protected).endswith("/") else protected
                try:
                    resolved.relative_to(protected_dir)
                    return True
                except ValueError:
                    pass
            # File: exact match
            if resolved == protected:
                return True

        return False

    def get_protected_paths(self) -> list[Path]:
        """Return resolved absolute paths for all protected entries."""
        result: list[Path] = []
        for entry in self.PROTECTED:
            if entry.endswith("/"):
                p = self.repo_root / entry.rstrip("/")
                result.append(Path(os.path.realpath(p)))
            else:
                p = self.repo_root / entry
                result.append(Path(os.path.realpath(p)))
        return result

    @staticmethod
    def verify_read_only_mounts(governance_paths: list[Path]) -> dict[str, bool]:
        """Check whether governance paths are read-only to the current process.

        Returns {str(path): is_read_only} using os.access(path, os.W_OK).
        A path that does not exist is reported as read-only (it cannot be
        written because it is absent).
        """
        result: dict[str, bool] = {}
        for p in governance_paths:
            if not p.exists():
                # Non-existent path: cannot write it, treat as protected
                result[str(p)] = True
            else:
                can_write = os.access(p, os.W_OK)
                result[str(p)] = not can_write
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _log_denial(
        self,
        target_path: Union[str, Path],
        session_id: str,
    ) -> None:
        """Emit a governance_access / deny audit event if store is wired up."""
        if self.audit_store is None:
            return
        try:
            from audit.store import AuditEvent

            event = AuditEvent(
                event_id=str(uuid.uuid4()),
                timestamp=_now_iso(),
                event_type="governance_access",
                session_id=session_id,
                agent_id="governance_guard",
                tool_name=None,
                tool_call_id=None,
                decision="deny",
                outcome="blocked",
                details={"target_path": str(target_path)},
            )
            self.audit_store.record(event)
        except Exception:  # noqa: BLE001 — audit must never crash the guard
            pass
