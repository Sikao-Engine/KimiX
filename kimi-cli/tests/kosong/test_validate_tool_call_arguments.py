"""Tests for kosong.contrib.chat_provider.common.validate_tool_call_arguments.

Regression context: the previous implementation used ``loads_relaxed``
(json_repair), which accepts '{}{}' — produced by gateways that stream
duplicated argument chunks.  The invalid string was sent verbatim to strict
backends (scnet/Qwen), which reject it with HTTP 400 (code 10013).
"""

import orjson

from kosong.contrib.chat_provider.common import validate_tool_call_arguments
from kosong.message import ToolCall


def _tc(name: str, arguments: str | None) -> ToolCall:
    return ToolCall(
        id=f"id-{name}",
        function=ToolCall.FunctionBody(name=name, arguments=arguments),
    )


def test_valid_arguments_unchanged_no_errors():
    calls = [_tc("bash", '{"command": "ls"}')]
    assert validate_tool_call_arguments(calls) == []
    assert calls[0].function.arguments == '{"command": "ls"}'


def test_duplicated_chunk_reset_to_empty_object():
    """The exact production poison: '{}{}' must not reach the wire."""
    calls = [_tc("bash", '{}{}')]
    errors = validate_tool_call_arguments(calls)
    assert calls[0].function.arguments == "{}"
    assert errors and "bash" in errors[0]


def test_trailing_garbage_repaired_to_first_value():
    calls = [_tc("bash", '{"command": "ls"}garbage')]
    errors = validate_tool_call_arguments(calls)
    assert calls[0].function.arguments == '{"command": "ls"}'
    assert errors


def test_truncated_arguments_reset_to_empty_object():
    calls = [_tc("bash", '{"command": ')]
    errors = validate_tool_call_arguments(calls)
    assert calls[0].function.arguments == "{}"
    assert errors


def test_none_arguments_silently_filled_with_empty_object():
    calls = [_tc("bash", None)]
    # Silent hardening — empty/missing arguments are legitimate.
    assert validate_tool_call_arguments(calls) == []
    assert calls[0].function.arguments == "{}"


def test_empty_string_arguments_silently_filled_with_empty_object():
    calls = [_tc("bash", "")]
    assert validate_tool_call_arguments(calls) == []
    assert calls[0].function.arguments == "{}"


def test_non_object_json_reset_to_empty_object():
    calls = [_tc("bash", '[1, 2, 3]')]
    errors = validate_tool_call_arguments(calls)
    assert calls[0].function.arguments == "{}"
    assert any("must be a JSON object" in e for e in errors)


def test_all_results_are_strict_parseable():
    calls = [
        _tc("a", '{}{}'),
        _tc("b", '{"x": 1}ok'),
        _tc("c", '{"x": '),
        _tc("d", None),
        _tc("e", ""),
        _tc("f", "garbage"),
    ]
    validate_tool_call_arguments(calls)
    for call in calls:
        orjson.loads(call.function.arguments)  # must not raise
