"""Parse raw-text action tag formats.

Two supported syntaxes:

1. XML-like tool_call tags::

       <tool_call name="tool_name">{"key": "value"}</tool_call>

2. Bracket action tags::

       [ACTION:tool_name|{"key": "value"}]

The parser is intentionally lenient:
- Whitespace around the JSON body is stripped.
- Malformed JSON falls back to an empty dict (the proxy will emit a
  parse_error ValidationError because the resulting name maps to nothing or
  schema fails, but we still surface the fragment for audit).
- Multiple tags in a single string are all extracted.
"""

from __future__ import annotations

import json
import logging
import re

log = logging.getLogger(__name__)

# <tool_call name="...">...</tool_call>
# Allows optional whitespace around the name attribute value and body.
_XML_TAG_RE = re.compile(
    r'<tool_call\s+name=["\']([^"\']+)["\']\s*>(.*?)</tool_call>',
    re.DOTALL | re.IGNORECASE,
)

# [ACTION:name|json_body]
# The body section (after |) is optional — defaults to {}.
_ACTION_TAG_RE = re.compile(
    r'\[ACTION:([^\]|]+)(?:\|([^\]]*))?\]',
    re.DOTALL | re.IGNORECASE,
)


def parse_action_tags(text: str) -> list[tuple[str, dict, str]]:
    """Extract all action tags from a raw text string.

    Returns:
        List of (tool_name, args_dict, raw_string) tuples.
        Malformed JSON bodies produce ("name", {}, raw) so the proxy can
        still route and validate them — the schema check will fail if params
        are required.
    """
    results: list[tuple[str, dict, str]] = []

    # Try XML-style tags first
    for match in _XML_TAG_RE.finditer(text):
        name = match.group(1).strip()
        body = match.group(2).strip()
        raw = match.group(0)
        args = _try_parse_json(body, raw)
        results.append((name, args, raw))

    # Then bracket-style tags
    for match in _ACTION_TAG_RE.finditer(text):
        name = match.group(1).strip()
        body = (match.group(2) or "{}").strip()
        raw = match.group(0)
        args = _try_parse_json(body, raw)
        results.append((name, args, raw))

    return results


def _try_parse_json(text: str, raw_context: str) -> dict:
    """Attempt to parse text as JSON dict; return {} on failure."""
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        # If it parsed but isn't a dict (e.g. a list or scalar), wrap it
        log.debug("Action tag body is not a JSON object, wrapping: %s", raw_context)
        return {"value": parsed}
    except json.JSONDecodeError as exc:
        log.debug("Action tag JSON parse error (%s): %s", exc, raw_context)
        return {}
