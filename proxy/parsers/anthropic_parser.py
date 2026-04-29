"""Parse Anthropic tool_use format.

Expected input: a dict whose ``content`` list contains items with
``"type": "tool_use"``::

    {
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_01...",
                "name": "tool_name",
                "input": {"key": "value"}
            },
            ...
        ]
    }

The ``content`` may also live inside ``choices[0].message`` for providers that
wrap Anthropic responses in an OpenAI envelope.

Returns a list of ``(name, args_dict, raw_fragment)`` triples.
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)


def parse_anthropic(model_output: dict) -> list[tuple[str, dict, str]]:
    """Parse an Anthropic-style response dict.

    Returns:
        List of (tool_name, args_dict, raw_string) tuples.
    """
    results: list[tuple[str, dict, str]] = []

    content = _extract_content(model_output)
    if not content:
        return results

    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use":
            continue

        raw = _safe_dumps(block)
        try:
            name: str = block.get("name", "").strip()
            inp = block.get("input", {})

            # Anthropic always sends input as a pre-parsed dict, but be
            # defensive in case a wrapper stringified it.
            if isinstance(inp, str):
                try:
                    inp = json.loads(inp)
                except json.JSONDecodeError:
                    inp = {}

            if not name:
                log.debug("Anthropic tool_use block missing name: %s", raw)
                results.append(("", {}, raw))
            else:
                results.append((name, inp, raw))

        except Exception as exc:  # noqa: BLE001
            log.debug("Anthropic block parse error: %s — %s", exc, raw)
            results.append(("", {}, raw))

    return results


def _extract_content(model_output: dict) -> list | None:
    """Find the content list regardless of nesting."""
    # Direct content key
    content = model_output.get("content")
    if isinstance(content, list):
        return content

    # Nested inside choices (OpenAI envelope around Anthropic response)
    choices = model_output.get("choices", [])
    if choices:
        msg = choices[0].get("message", {})
        content = msg.get("content")
        if isinstance(content, list):
            return content

    return None


def _safe_dumps(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(obj)
