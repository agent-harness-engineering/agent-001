"""Parse OpenAI function-calling format.

Expected input: a dict with a ``tool_calls`` list whose items have the shape::

    {
        "id": "...",
        "type": "function",
        "function": {
            "name": "tool_name",
            "arguments": "{\"key\": \"value\"}"   # JSON-encoded string
        }
    }

Returns a list of ``(name, args_dict, raw_fragment)`` triples where
``raw_fragment`` is a compact JSON string of the original tool_call object,
used for the audit ``raw`` field.
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)


def parse_openai(model_output: dict) -> list[tuple[str, dict, str]]:
    """Parse an OpenAI-style response dict.

    Returns:
        List of (tool_name, args_dict, raw_string) tuples.
        Tuples where parsing fails carry ("", {}, raw_string) so the proxy
        can emit a ValidationError with error_type="parse_error".
    """
    results: list[tuple[str, dict, str]] = []

    tool_calls = model_output.get("tool_calls")
    if not tool_calls:
        # Also handle the nested choices[0].message.tool_calls form
        choices = model_output.get("choices", [])
        if choices:
            message = choices[0].get("message", {})
            tool_calls = message.get("tool_calls", [])

    if not tool_calls:
        return results

    for tc in tool_calls:
        raw = _safe_dumps(tc)
        try:
            fn = tc.get("function", {})
            name: str = fn.get("name", "").strip()
            arguments_raw = fn.get("arguments", "{}")

            if isinstance(arguments_raw, dict):
                # Some providers pre-parse arguments
                args: dict = arguments_raw
            elif isinstance(arguments_raw, str):
                args = json.loads(arguments_raw)
            else:
                args = {}

            if not name:
                log.debug("OpenAI tool call missing function name: %s", raw)
                results.append(("", {}, raw))
            else:
                results.append((name, args, raw))

        except json.JSONDecodeError as exc:
            log.debug("OpenAI arguments JSON parse error: %s — %s", exc, raw)
            results.append(("", {}, raw))

    return results


def _safe_dumps(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(obj)
