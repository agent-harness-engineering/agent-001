"""
CLI approval channel for the Permission Gate.

Blocks on stdin until the operator responds or the timeout expires.
Fail-closed: timeout → deny, no-TTY → deny.
"""

from __future__ import annotations

import os
import select
import sys
import threading
from typing import Optional


_PARAM_TRUNCATE = 500


def _truncate(value: str, limit: int = _PARAM_TRUNCATE) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"… [truncated, {len(value)} chars total]"


class CLIChannel:
    """Synchronous CLI approval relay.

    Blocks until the operator responds *y* or *n*, or until *timeout_seconds*
    elapses.  Works safely in multi-threaded contexts — each call acquires an
    internal lock so that concurrent requests are serialised on the terminal.
    """

    def __init__(self) -> None:
        # Serialise concurrent approvals on the same terminal.
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def request_approval(
        self,
        tool_name: str,
        tool_call_id: str,
        parameters: dict,
        timeout_seconds: int,
    ) -> tuple[bool, str]:
        """Present an approval prompt and wait for a y/n response.

        Returns:
            (True,  username)   — operator typed *y*
            (False, "timeout")  — no response within *timeout_seconds*
            (False, "no_tty")   — stdin is not a terminal (non-interactive)
        """
        if not self._is_tty():
            return False, "no_tty"

        with self._lock:
            return self._prompt(tool_name, tool_call_id, parameters, timeout_seconds)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _is_tty() -> bool:
        try:
            return sys.stdin.isatty()
        except Exception:
            return False

    def _prompt(
        self,
        tool_name: str,
        tool_call_id: str,
        parameters: dict,
        timeout_seconds: int,
    ) -> tuple[bool, str]:
        param_str = _truncate(str(parameters))
        banner = (
            "\n"
            "╔══════════════════════════════════════════════════════╗\n"
            "║          PERMISSION GATE — APPROVAL REQUIRED         ║\n"
            "╚══════════════════════════════════════════════════════╝\n"
            f"  Tool       : {tool_name}\n"
            f"  Call ID    : {tool_call_id}\n"
            f"  Parameters : {param_str}\n"
            f"  Timeout    : {timeout_seconds}s\n"
            "──────────────────────────────────────────────────────\n"
            f"  Allow this tool call? [y/N] (timeout in {timeout_seconds}s): "
        )
        sys.stdout.write(banner)
        sys.stdout.flush()

        response = self._read_with_timeout(timeout_seconds)

        if response is None:
            sys.stdout.write("\n[Permission Gate] No response — denying (timeout).\n")
            sys.stdout.flush()
            return False, "timeout"

        answer = response.strip().lower()
        if answer in ("y", "yes"):
            approver = self._get_approver()
            sys.stdout.write(f"[Permission Gate] Approved by {approver}.\n")
            sys.stdout.flush()
            return True, approver

        sys.stdout.write("[Permission Gate] Denied by operator.\n")
        sys.stdout.flush()
        return False, "operator"

    @staticmethod
    def _read_with_timeout(timeout_seconds: int) -> Optional[str]:
        """Read one line from stdin with a wall-clock timeout.

        Uses *select* on POSIX.  Falls back to a threading.Timer approach
        on platforms where select on stdin is unreliable.
        """
        try:
            ready, _, _ = select.select([sys.stdin], [], [], timeout_seconds)
            if ready:
                return sys.stdin.readline()
            return None  # timeout
        except (select.error, ValueError):
            # Fallback: threading approach (covers Windows / unusual fds)
            result: list[Optional[str]] = [None]
            event = threading.Event()

            def _reader() -> None:
                try:
                    result[0] = sys.stdin.readline()
                except Exception:
                    result[0] = None
                finally:
                    event.set()

            t = threading.Thread(target=_reader, daemon=True)
            t.start()
            event.wait(timeout=timeout_seconds)
            # If the thread is still blocking on readline we cannot cleanly
            # interrupt it, but the gate will correctly return None because
            # event.wait timed out and result[0] is still None.
            return result[0]

    @staticmethod
    def _get_approver() -> str:
        """Best-effort: return the OS login name of the approving user."""
        for envvar in ("SUDO_USER", "USER", "LOGNAME", "USERNAME"):
            name = os.environ.get(envvar, "")
            if name:
                return name
        try:
            import pwd
            return pwd.getpwuid(os.getuid()).pw_name
        except Exception:
            return "unknown"
