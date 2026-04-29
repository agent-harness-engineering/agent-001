"""
Approval channels for the Permission Gate.

A channel is any object that implements:
    request_approval(tool_name, tool_call_id, parameters, timeout_seconds)
        -> tuple[bool, str]   # (approved, approver_or_reason)
"""

from .cli import CLIChannel

__all__ = ["CLIChannel"]
