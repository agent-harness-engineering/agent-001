"""Unit tests for the Tool Interception Proxy.

Run with:  python -m pytest proxy/tests/  or  python -m unittest discover proxy/tests/
"""

import json
import unittest
from datetime import timezone

# Adjust sys.path so tests can be run from the repo root without install.
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from proxy import ToolProxy, CanonicalToolCall, ValidationError, ModelFormat
from proxy.parsers.openai_parser import parse_openai
from proxy.parsers.anthropic_parser import parse_anthropic
from proxy.parsers.action_tag_parser import parse_action_tags


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

REGISTRY = {
    "get_weather": {
        "schema": {
            "type": "object",
            "properties": {
                "location": {"type": "string"},
                "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
            },
            "required": ["location"],
        }
    },
    "add_numbers": {
        "schema": {
            "type": "object",
            "properties": {
                "a": {"type": "number"},
                "b": {"type": "number"},
            },
            "required": ["a", "b"],
        }
    },
    "no_params_tool": {
        "schema": {
            "type": "object",
            "properties": {},
        }
    },
}


def make_proxy(audit_cb=None):
    return ToolProxy(tool_registry=REGISTRY, audit_callback=audit_cb)


# ---------------------------------------------------------------------------
# Parser unit tests
# ---------------------------------------------------------------------------

class TestOpenAIParser(unittest.TestCase):

    def test_basic_tool_call(self):
        payload = {
            "tool_calls": [
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": '{"location": "London"}',
                    },
                }
            ]
        }
        results = parse_openai(payload)
        self.assertEqual(len(results), 1)
        name, args, raw = results[0]
        self.assertEqual(name, "get_weather")
        self.assertEqual(args["location"], "London")
        self.assertIn("get_weather", raw)

    def test_pre_parsed_arguments_dict(self):
        """Some providers send arguments as an already-parsed dict."""
        payload = {
            "tool_calls": [
                {
                    "function": {
                        "name": "add_numbers",
                        "arguments": {"a": 1, "b": 2},
                    }
                }
            ]
        }
        results = parse_openai(payload)
        self.assertEqual(len(results), 1)
        _, args, _ = results[0]
        self.assertEqual(args["a"], 1)

    def test_nested_in_choices(self):
        """OpenAI chat completion response nesting."""
        payload = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"location": "Paris"}',
                                }
                            }
                        ],
                    }
                }
            ]
        }
        results = parse_openai(payload)
        self.assertEqual(len(results), 1)
        name, args, _ = results[0]
        self.assertEqual(name, "get_weather")
        self.assertEqual(args["location"], "Paris")

    def test_multiple_tool_calls(self):
        payload = {
            "tool_calls": [
                {"function": {"name": "get_weather", "arguments": '{"location": "NYC"}'}},
                {"function": {"name": "add_numbers", "arguments": '{"a": 3, "b": 4}'}},
            ]
        }
        results = parse_openai(payload)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0][0], "get_weather")
        self.assertEqual(results[1][0], "add_numbers")

    def test_malformed_json_arguments(self):
        payload = {
            "tool_calls": [
                {"function": {"name": "get_weather", "arguments": "{bad json"}}
            ]
        }
        results = parse_openai(payload)
        self.assertEqual(len(results), 1)
        name, args, _ = results[0]
        self.assertEqual(name, "")  # parse failure signals empty name

    def test_empty_tool_calls(self):
        payload = {"tool_calls": []}
        results = parse_openai(payload)
        self.assertEqual(results, [])

    def test_no_tool_calls_key(self):
        payload = {"choices": [{"message": {"content": "Hello"}}]}
        results = parse_openai(payload)
        self.assertEqual(results, [])


class TestAnthropicParser(unittest.TestCase):

    def test_basic_tool_use_block(self):
        payload = {
            "content": [
                {"type": "text", "text": "Let me check the weather."},
                {
                    "type": "tool_use",
                    "id": "toolu_01abc",
                    "name": "get_weather",
                    "input": {"location": "Tokyo"},
                },
            ]
        }
        results = parse_anthropic(payload)
        self.assertEqual(len(results), 1)
        name, args, raw = results[0]
        self.assertEqual(name, "get_weather")
        self.assertEqual(args["location"], "Tokyo")

    def test_multiple_tool_use_blocks(self):
        payload = {
            "content": [
                {"type": "tool_use", "name": "get_weather", "input": {"location": "Berlin"}},
                {"type": "tool_use", "name": "add_numbers", "input": {"a": 5, "b": 6}},
            ]
        }
        results = parse_anthropic(payload)
        self.assertEqual(len(results), 2)

    def test_ignores_non_tool_use_blocks(self):
        payload = {
            "content": [
                {"type": "text", "text": "Hello"},
                {"type": "image", "source": {"type": "base64", "data": "..."}},
            ]
        }
        results = parse_anthropic(payload)
        self.assertEqual(results, [])

    def test_stringified_input_dict(self):
        """Defensive: input provided as a JSON string instead of a dict."""
        payload = {
            "content": [
                {
                    "type": "tool_use",
                    "name": "get_weather",
                    "input": '{"location": "Seoul"}',
                }
            ]
        }
        results = parse_anthropic(payload)
        self.assertEqual(len(results), 1)
        _, args, _ = results[0]
        self.assertEqual(args["location"], "Seoul")

    def test_empty_content(self):
        payload = {"content": []}
        results = parse_anthropic(payload)
        self.assertEqual(results, [])

    def test_no_content_key(self):
        payload = {"role": "assistant", "id": "msg_xyz"}
        results = parse_anthropic(payload)
        self.assertEqual(results, [])


class TestActionTagParser(unittest.TestCase):

    def test_xml_style_tag(self):
        text = '<tool_call name="get_weather">{"location": "Madrid"}</tool_call>'
        results = parse_action_tags(text)
        self.assertEqual(len(results), 1)
        name, args, raw = results[0]
        self.assertEqual(name, "get_weather")
        self.assertEqual(args["location"], "Madrid")

    def test_bracket_action_tag(self):
        text = '[ACTION:add_numbers|{"a": 10, "b": 20}]'
        results = parse_action_tags(text)
        self.assertEqual(len(results), 1)
        name, args, raw = results[0]
        self.assertEqual(name, "add_numbers")
        self.assertEqual(args["a"], 10)

    def test_bracket_tag_no_params(self):
        text = "[ACTION:no_params_tool]"
        results = parse_action_tags(text)
        self.assertEqual(len(results), 1)
        name, args, _ = results[0]
        self.assertEqual(name, "no_params_tool")
        self.assertEqual(args, {})

    def test_multiple_tags_in_text(self):
        text = (
            'First call: <tool_call name="get_weather">{"location": "Rome"}</tool_call>\n'
            'Second call: [ACTION:add_numbers|{"a": 1, "b": 2}]'
        )
        results = parse_action_tags(text)
        self.assertEqual(len(results), 2)
        names = {r[0] for r in results}
        self.assertIn("get_weather", names)
        self.assertIn("add_numbers", names)

    def test_malformed_json_body_graceful(self):
        text = '<tool_call name="get_weather">{bad json}</tool_call>'
        results = parse_action_tags(text)
        self.assertEqual(len(results), 1)
        name, args, _ = results[0]
        self.assertEqual(name, "get_weather")
        self.assertEqual(args, {})  # fallback to empty dict

    def test_no_tags_returns_empty(self):
        text = "Hello, this is plain text with no tool calls."
        results = parse_action_tags(text)
        self.assertEqual(results, [])

    def test_xml_tag_single_quotes(self):
        text = "<tool_call name='get_weather'>{\"location\": \"Lisbon\"}</tool_call>"
        results = parse_action_tags(text)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], "get_weather")

    def test_multiline_json_body(self):
        text = (
            '<tool_call name="add_numbers">\n'
            '  {\n'
            '    "a": 7,\n'
            '    "b": 8\n'
            '  }\n'
            '</tool_call>'
        )
        results = parse_action_tags(text)
        self.assertEqual(len(results), 1)
        _, args, _ = results[0]
        self.assertEqual(args["a"], 7)


# ---------------------------------------------------------------------------
# ToolProxy integration tests
# ---------------------------------------------------------------------------

class TestToolProxyOpenAI(unittest.TestCase):

    def test_valid_call_produces_canonical(self):
        proxy = make_proxy()
        output = {
            "tool_calls": [
                {"function": {"name": "get_weather", "arguments": '{"location": "Berlin"}'}}
            ]
        }
        results = proxy.process(output, source_hint="openai")
        self.assertEqual(len(results), 1)
        call = results[0]
        self.assertIsInstance(call, CanonicalToolCall)
        self.assertEqual(call.tool_name, "get_weather")
        self.assertEqual(call.source, "openai")
        self.assertIsNotNone(call.id)

    def test_unknown_tool_produces_validation_error(self):
        proxy = make_proxy()
        output = {
            "tool_calls": [
                {"function": {"name": "fly_rocket", "arguments": '{}'}}
            ]
        }
        results = proxy.process(output, source_hint="openai")
        self.assertEqual(len(results), 1)
        err = results[0]
        self.assertIsInstance(err, ValidationError)
        self.assertEqual(err.error_type, "unknown_tool")
        self.assertIn("fly_rocket", err.message)

    def test_schema_validation_failure_missing_required(self):
        proxy = make_proxy()
        # add_numbers requires both a and b
        output = {
            "tool_calls": [
                {"function": {"name": "add_numbers", "arguments": '{"a": 5}'}}
            ]
        }
        results = proxy.process(output, source_hint="openai")
        self.assertEqual(len(results), 1)
        err = results[0]
        self.assertIsInstance(err, ValidationError)
        self.assertEqual(err.error_type, "schema_error")

    def test_schema_validation_failure_wrong_type(self):
        proxy = make_proxy()
        # add_numbers expects numbers, not strings
        output = {
            "tool_calls": [
                {"function": {"name": "add_numbers", "arguments": '{"a": "five", "b": 3}'}}
            ]
        }
        results = proxy.process(output, source_hint="openai")
        self.assertEqual(len(results), 1)
        err = results[0]
        self.assertIsInstance(err, ValidationError)
        self.assertEqual(err.error_type, "schema_error")


class TestToolProxyAnthropic(unittest.TestCase):

    def test_valid_call(self):
        proxy = make_proxy()
        output = {
            "content": [
                {
                    "type": "tool_use",
                    "name": "add_numbers",
                    "input": {"a": 3, "b": 4},
                }
            ]
        }
        results = proxy.process(output, source_hint="anthropic")
        self.assertEqual(len(results), 1)
        call = results[0]
        self.assertIsInstance(call, CanonicalToolCall)
        self.assertEqual(call.tool_name, "add_numbers")
        self.assertEqual(call.source, "anthropic")

    def test_unknown_tool(self):
        proxy = make_proxy()
        output = {
            "content": [
                {"type": "tool_use", "name": "bake_bread", "input": {"loaves": 2}}
            ]
        }
        results = proxy.process(output, source_hint="anthropic")
        self.assertIsInstance(results[0], ValidationError)
        self.assertEqual(results[0].error_type, "unknown_tool")


class TestToolProxyActionTag(unittest.TestCase):

    def test_valid_call(self):
        proxy = make_proxy()
        text = '<tool_call name="get_weather">{"location": "Vienna"}</tool_call>'
        results = proxy.process(text, source_hint="action_tag")
        self.assertEqual(len(results), 1)
        call = results[0]
        self.assertIsInstance(call, CanonicalToolCall)
        self.assertEqual(call.source, "action_tag")

    def test_malformed_json_becomes_parse_error(self):
        proxy = make_proxy()
        # Malformed JSON → parser returns empty args dict → missing required "location"
        # This should yield a schema_error for missing required field
        text = '<tool_call name="get_weather">{bad json}</tool_call>'
        results = proxy.process(text, source_hint="action_tag")
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], ValidationError)
        # With malformed JSON, args={} and location is required → schema_error
        self.assertEqual(results[0].error_type, "schema_error")

    def test_no_tags_empty_result(self):
        proxy = make_proxy()
        results = proxy.process("Just plain text with no action tags.")
        self.assertEqual(results, [])


class TestAutoFormatDetection(unittest.TestCase):

    def test_detects_openai(self):
        proxy = make_proxy()
        output = {
            "tool_calls": [
                {"function": {"name": "get_weather", "arguments": '{"location": "Oslo"}'}}
            ]
        }
        results = proxy.process(output)  # source_hint defaults to "auto"
        self.assertIsInstance(results[0], CanonicalToolCall)
        self.assertEqual(results[0].source, "openai")

    def test_detects_anthropic(self):
        proxy = make_proxy()
        output = {
            "content": [
                {"type": "tool_use", "name": "add_numbers", "input": {"a": 1, "b": 2}}
            ]
        }
        results = proxy.process(output)
        self.assertIsInstance(results[0], CanonicalToolCall)
        self.assertEqual(results[0].source, "anthropic")

    def test_detects_action_tag_from_string(self):
        proxy = make_proxy()
        text = '<tool_call name="get_weather">{"location": "Dublin"}</tool_call>'
        results = proxy.process(text)
        self.assertIsInstance(results[0], CanonicalToolCall)
        self.assertEqual(results[0].source, "action_tag")

    def test_detects_bracket_action_tag(self):
        proxy = make_proxy()
        text = '[ACTION:add_numbers|{"a": 2, "b": 3}]'
        results = proxy.process(text)
        self.assertIsInstance(results[0], CanonicalToolCall)
        self.assertEqual(results[0].source, "action_tag")

    def test_plain_string_no_tags_empty(self):
        proxy = make_proxy()
        results = proxy.process("Just a plain reply, no tool calls here.")
        self.assertEqual(results, [])

    def test_unknown_dict_format_returns_empty(self):
        proxy = make_proxy()
        results = proxy.process({"message": "hello"})
        self.assertEqual(results, [])


class TestAuditCallback(unittest.TestCase):

    def test_callback_called_for_each_result(self):
        audited = []
        proxy = make_proxy(audit_cb=audited.append)
        output = {
            "tool_calls": [
                {"function": {"name": "get_weather", "arguments": '{"location": "Zurich"}'}},
                {"function": {"name": "fly_rocket", "arguments": "{}"}},
            ]
        }
        results = proxy.process(output, source_hint="openai")
        self.assertEqual(len(results), 2)
        self.assertEqual(len(audited), 2)

    def test_callback_receives_canonical_and_error(self):
        audited = []
        proxy = make_proxy(audit_cb=audited.append)
        output = {
            "tool_calls": [
                {"function": {"name": "get_weather", "arguments": '{"location": "Zurich"}'}},
                {"function": {"name": "fly_rocket", "arguments": "{}"}},
            ]
        }
        proxy.process(output, source_hint="openai")
        types = {type(r).__name__ for r in audited}
        self.assertIn("CanonicalToolCall", types)
        self.assertIn("ValidationError", types)

    def test_callback_called_even_on_unknown_tool(self):
        audited = []
        proxy = make_proxy(audit_cb=audited.append)
        output = {
            "content": [
                {"type": "tool_use", "name": "mystery_tool", "input": {}}
            ]
        }
        proxy.process(output, source_hint="anthropic")
        self.assertEqual(len(audited), 1)
        self.assertIsInstance(audited[0], ValidationError)

    def test_callback_exception_does_not_propagate(self):
        def bad_callback(result):
            raise RuntimeError("callback exploded")

        proxy = make_proxy(audit_cb=bad_callback)
        output = {
            "tool_calls": [
                {"function": {"name": "get_weather", "arguments": '{"location": "Prague"}'}}
            ]
        }
        # Should not raise even though callback raises
        try:
            results = proxy.process(output, source_hint="openai")
            self.assertEqual(len(results), 1)
        except RuntimeError:
            self.fail("audit_callback exception leaked out of process()")

    def test_no_callback_works_fine(self):
        proxy = ToolProxy(tool_registry=REGISTRY)  # no audit_callback
        output = {
            "tool_calls": [
                {"function": {"name": "get_weather", "arguments": '{"location": "Athens"}'}}
            ]
        }
        results = proxy.process(output)
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], CanonicalToolCall)


class TestCanonicalToolCallFields(unittest.TestCase):

    def test_id_is_uuid4(self):
        import uuid
        proxy = make_proxy()
        output = {
            "tool_calls": [
                {"function": {"name": "get_weather", "arguments": '{"location": "Helsinki"}'}}
            ]
        }
        results = proxy.process(output)
        call = results[0]
        self.assertIsInstance(call, CanonicalToolCall)
        # uuid4 should parse without raising
        parsed = uuid.UUID(call.id, version=4)
        self.assertEqual(str(parsed), call.id)

    def test_timestamp_is_iso8601_utc(self):
        from datetime import datetime, timezone
        proxy = make_proxy()
        output = {
            "tool_calls": [
                {"function": {"name": "get_weather", "arguments": '{"location": "Brussels"}'}}
            ]
        }
        results = proxy.process(output)
        call = results[0]
        # Should parse without raising
        dt = datetime.fromisoformat(call.timestamp)
        self.assertIsNotNone(dt)

    def test_raw_field_contains_original_input(self):
        proxy = make_proxy()
        tc_obj = {"function": {"name": "get_weather", "arguments": '{"location": "Amsterdam"}'}}
        output = {"tool_calls": [tc_obj]}
        results = proxy.process(output)
        call = results[0]
        self.assertIn("get_weather", call.raw)


class TestMixedResults(unittest.TestCase):
    """A single process() call can return both successes and errors."""

    def test_mixed_valid_and_invalid(self):
        proxy = make_proxy()
        output = {
            "content": [
                {"type": "tool_use", "name": "get_weather", "input": {"location": "Warsaw"}},
                {"type": "tool_use", "name": "undefined_tool", "input": {}},
                {"type": "tool_use", "name": "add_numbers", "input": {"a": 1, "b": 2}},
            ]
        }
        results = proxy.process(output, source_hint="anthropic")
        self.assertEqual(len(results), 3)
        self.assertIsInstance(results[0], CanonicalToolCall)
        self.assertIsInstance(results[1], ValidationError)
        self.assertIsInstance(results[2], CanonicalToolCall)


if __name__ == "__main__":
    unittest.main()
