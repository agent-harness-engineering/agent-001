"""Canonical data types for the Tool Interception Proxy."""

from dataclasses import dataclass, field
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CanonicalToolCall:
    """A validated, normalised tool call ready for execution."""

    id: str            # uuid4 string
    tool_name: str     # normalised tool name (stripped, lowercased if desired)
    parameters: dict   # validated against tool's JSON schema
    source: str        # "openai" | "anthropic" | "action_tag"
    raw: str           # original model output (for audit)
    timestamp: str = field(default_factory=_now_iso)


@dataclass
class ValidationError:
    """Describes a tool call that could not be validated."""

    error_type: str    # "parse_error" | "unknown_tool" | "schema_error" | "auth_error"
    message: str
    raw: str           # original input that triggered the error
    timestamp: str = field(default_factory=_now_iso)
