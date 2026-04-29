"""Tool Interception Proxy — every tool call passes through here.

Architecture
============

``ToolProxy.process()`` is the single entry point.  It:

1.  Detects (or uses the caller-supplied hint for) the model output format.
2.  Delegates to the appropriate parser to extract (name, args, raw) triples.
3.  For each triple:
    a. Looks up the tool in the registry — unknown tools become
       ``ValidationError(error_type="unknown_tool")``.
    b. Validates the args dict against the tool's declared JSON schema —
       failures become ``ValidationError(error_type="schema_error")``.
    c. On success, wraps the call in a ``CanonicalToolCall``.
4.  Calls ``audit_callback`` for **every** result (pass or fail).
5.  Returns the complete list.

Thread safety
=============
``ToolProxy`` is intentionally stateless beyond the two immutable attributes
set at construction time (``_registry`` and ``_audit_callback``).  Concurrent
``process()`` calls are safe.

JSON schema validation
======================
``jsonschema`` is used when available.  If it is not installed, a lightweight
built-in validator checks ``required`` fields and basic ``type`` constraints
(enough for CI / local testing without pip install).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from .parsers import parse_openai, parse_anthropic, parse_action_tags
from .schemas import CanonicalToolCall, ValidationError

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional jsonschema import
# ---------------------------------------------------------------------------

try:
    import jsonschema as _jsonschema  # type: ignore
    _JSONSCHEMA_AVAILABLE = True
except ImportError:  # pragma: no cover
    _jsonschema = None  # type: ignore
    _JSONSCHEMA_AVAILABLE = False

# Type alias for the result list
ProxyResult = list[CanonicalToolCall | ValidationError]


# ---------------------------------------------------------------------------
# Lightweight fallback validator
# ---------------------------------------------------------------------------

_JSON_TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
    "null": type(None),
}


def _builtin_validate(params: dict, schema: dict) -> list[str]:
    """Return a list of error messages (empty = valid).

    Checks: required fields present, and declared property types match.
    """
    errors: list[str] = []
    if not isinstance(schema, dict):
        return errors

    # Required fields
    for req in schema.get("required", []):
        if req not in params:
            errors.append(f"Missing required parameter: '{req}'")

    # Type check declared properties
    for prop, prop_schema in schema.get("properties", {}).items():
        if prop not in params:
            continue
        expected_type = prop_schema.get("type")
        if expected_type and expected_type in _JSON_TYPE_MAP:
            py_type = _JSON_TYPE_MAP[expected_type]
            val = params[prop]
            # JSON numbers: Python bools are ints; treat bool as not-integer/number
            if expected_type in ("integer", "number") and isinstance(val, bool):
                errors.append(
                    f"Parameter '{prop}' expected {expected_type}, got boolean"
                )
            elif not isinstance(val, py_type):
                errors.append(
                    f"Parameter '{prop}' expected {expected_type}, "
                    f"got {type(val).__name__}"
                )

    return errors


# ---------------------------------------------------------------------------
# ToolProxy
# ---------------------------------------------------------------------------

class ToolProxy:
    """Intercept, validate, and canonicalise model tool calls.

    Parameters
    ----------
    tool_registry:
        Mapping of tool name → ``{"schema": <JSON Schema dict>}``.
        Additional keys (description, handler, …) are ignored by the proxy.
    audit_callback:
        Optional callable invoked with every ``CanonicalToolCall`` or
        ``ValidationError`` produced.  Exceptions raised inside the callback
        are logged and swallowed so they never corrupt the main result.
    """

    def __init__(
        self,
        tool_registry: dict[str, dict],
        audit_callback: Callable[[CanonicalToolCall | ValidationError], None] | None = None,
    ) -> None:
        # Defensive copy so callers cannot mutate the registry under us.
        self._registry: dict[str, dict] = dict(tool_registry)
        self._audit_callback = audit_callback

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(
        self,
        model_output: str | dict,
        source_hint: str = "auto",
    ) -> ProxyResult:
        """Process a model output and return validated tool calls.

        Parameters
        ----------
        model_output:
            Raw model response — either a parsed dict or a text string.
        source_hint:
            ``"auto"`` (default) to infer the format, or one of
            ``"openai"``, ``"anthropic"``, ``"action_tag"`` to force a
            specific parser.

        Returns
        -------
        List of ``CanonicalToolCall`` and/or ``ValidationError`` objects.
        The caller inspects the type of each element.
        """
        source = source_hint if source_hint != "auto" else self._detect_format(model_output)

        raw_triples = self._parse(model_output, source)

        results: ProxyResult = []
        for name, args, raw_fragment in raw_triples:
            result = self._validate_and_build(name, args, raw_fragment, source)
            results.append(result)
            self._emit_audit(result)

        return results

    # ------------------------------------------------------------------
    # Format detection
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_format(model_output: str | dict) -> str:
        """Infer which parser to use from the shape of the output."""
        if isinstance(model_output, dict):
            if "tool_calls" in model_output:
                return "openai"
            # tool_calls nested inside choices[0].message
            choices = model_output.get("choices", [])
            if choices and isinstance(choices, list):
                msg = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
                if "tool_calls" in msg:
                    return "openai"

            content = model_output.get("content")
            if not content:
                # Also check choices nesting
                if choices and isinstance(choices, list):
                    msg = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
                    content = msg.get("content")
            if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_use" for b in content
            ):
                return "anthropic"

            return "unknown"

        if isinstance(model_output, str):
            if "<tool_call" in model_output:
                return "action_tag"
            if "[ACTION:" in model_output:
                return "action_tag"
            return "action_tag"  # string, but no tags → parser returns []

        return "unknown"

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse(
        self, model_output: str | dict, source: str
    ) -> list[tuple[str, dict, str]]:
        """Dispatch to the appropriate parser; never raises."""
        try:
            if source == "openai":
                return parse_openai(model_output)  # type: ignore[arg-type]
            if source == "anthropic":
                return parse_anthropic(model_output)  # type: ignore[arg-type]
            if source == "action_tag":
                text = model_output if isinstance(model_output, str) else json.dumps(model_output)
                return parse_action_tags(text)
        except Exception as exc:  # noqa: BLE001
            log.error("Parser raised unexpectedly (source=%s): %s", source, exc)
        return []

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_and_build(
        self,
        name: str,
        args: dict,
        raw: str,
        source: str,
    ) -> CanonicalToolCall | ValidationError:
        ts = datetime.now(timezone.utc).isoformat()

        # Handle parse-level failures surfaced as empty name
        if not name:
            return ValidationError(
                error_type="parse_error",
                message="Could not extract tool name from model output",
                raw=raw,
                timestamp=ts,
            )

        # Registry lookup
        if name not in self._registry:
            return ValidationError(
                error_type="unknown_tool",
                message=f"Tool '{name}' is not registered",
                raw=raw,
                timestamp=ts,
            )

        tool_def = self._registry[name]
        schema = tool_def.get("schema", {})

        # Schema validation
        schema_errors = self._schema_validate(args, schema)
        if schema_errors:
            return ValidationError(
                error_type="schema_error",
                message="; ".join(schema_errors),
                raw=raw,
                timestamp=ts,
            )

        return CanonicalToolCall(
            id=str(uuid.uuid4()),
            tool_name=name,
            parameters=args,
            source=source,
            raw=raw,
            timestamp=ts,
        )

    @staticmethod
    def _schema_validate(params: dict, schema: dict) -> list[str]:
        """Return validation error messages (empty = valid)."""
        if not schema:
            return []

        if _JSONSCHEMA_AVAILABLE:
            validator = _jsonschema.Draft7Validator(schema)
            return [e.message for e in validator.iter_errors(params)]

        return _builtin_validate(params, schema)

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    def _emit_audit(self, result: CanonicalToolCall | ValidationError) -> None:
        if self._audit_callback is None:
            return
        try:
            self._audit_callback(result)
        except Exception as exc:  # noqa: BLE001
            log.warning("audit_callback raised: %s", exc)
