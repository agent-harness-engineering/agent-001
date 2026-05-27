"""
Pre-write hook for GovernanceGuard.

Wrap ``GovernanceGuard.check_write`` as a callable suitable for injection
into any component that emits write events (ToolProxy, file handler, etc.).

Usage::

    from pathlib import Path
    from governance.self_exemption import GovernanceGuard
    from governance.governance_guard_hook import make_pre_write_hook

    guard = GovernanceGuard(repo_root=Path("/app"))
    pre_write = make_pre_write_hook(guard, session_id="session-123")

    # In your write path:
    pre_write("/app/morals.md")   # raises PermissionError
    pre_write("/app/output.txt")  # returns None — proceed
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

from governance.self_exemption import GovernanceGuard


def make_pre_write_hook(
    guard: GovernanceGuard,
    session_id: str = "unknown",
):
    """Return a pre-write hook bound to *guard* and *session_id*.

    The returned callable accepts a target path and raises ``PermissionError``
    when the path targets a governance-protected location. It returns ``None``
    silently when the write is permitted.

    Args:
        guard: A configured ``GovernanceGuard`` instance.
        session_id: Identifies the session for audit logging.

    Returns:
        Callable[[str | Path], None] — raises ``PermissionError`` if blocked.
    """

    def _pre_write_hook(target_path: Union[str, Path]) -> None:
        if not guard.check_write(target_path, session_id=session_id):
            raise PermissionError(
                f"GovernanceGuard: write to '{target_path}' is blocked — "
                "target is a protected governance file or directory."
            )

    return _pre_write_hook
