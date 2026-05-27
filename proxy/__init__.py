"""Tool Interception Proxy for the Maestro Agent.

Every tool call produced by a model passes through ``ToolProxy.process()``.
Nothing bypasses it.

Public exports
--------------
ToolProxy         Main proxy class.
CanonicalToolCall Validated, normalised representation of a tool call.
ValidationError   Describes a call that failed parsing or validation.
ModelFormat       Convenience string literals for the ``source`` / format fields.
"""

from .schemas import CanonicalToolCall, ValidationError
from .tool_proxy import ToolProxy


class ModelFormat:
    """String constants for the ``source`` field and ``source_hint`` parameter."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    ACTION_TAG = "action_tag"
    AUTO = "auto"


__all__ = [
    "ToolProxy",
    "CanonicalToolCall",
    "ValidationError",
    "ModelFormat",
]
