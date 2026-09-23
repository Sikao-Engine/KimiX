"""Tests for kosong.utils.jsonx — strict sanitizing of tool call arguments.

Regression context: the scnet/Qwen gateway streams duplicated argument chunks
which merge into strings like ``'{}{}'``.  ``json_repair`` parses such input
leniently, so the poison only surfaces when the history is echoed back to the
API — strict backends reject it with HTTP 400 (code 10013).  These tests pin
the strict sanitizing behavior.
"""

import orjson
import pytest

from kosong.utils.jsonx import sanitize_tool_arguments, truncate_to_first_json_value


class TestTruncateToFirstJsonValue:
    @pytest.mark.parametrize(
        "data",
        [
            '{}{}',  # duplicated gateway chunk (the original production bug)
            '{"a": 1}garbage',
            '{"a": 1} {"b": 2}',  # two concatenated objects
            '[1, 2]extra',
            '"str"trailing',
            '42xyz',
        ],
    )
    def test_extracts_first_complete_value(self, data: str):
        prefix = truncate_to_first_json_value(data)
        assert prefix is not None
        # The prefix must be strict-parseable on its own.
        orjson.loads(prefix)
        assert data.lstrip().startswith(prefix)

    def test_whitespace_is_stripped_from_result(self):
        assert truncate_to_first_json_value('   {"a": 1}junk') == '{"a": 1}'

    @pytest.mark.parametrize(
        "data",
        [
            '',  # empty
            '   ',  # whitespace only
            '{',  # truncated object
            '{"a": ',  # truncated object
            'not json at all',  # no JSON value at all
        ],
    )
    def test_returns_none_when_no_complete_value(self, data: str):
        assert truncate_to_first_json_value(data) is None


class TestSanitizeToolArguments:
    @pytest.mark.parametrize(
        "arguments",
        [
            '{"command": "ls"}',
            '{}',
            '[]',
            '"just a string"',
            '42',
            'null',
            'true',
        ],
    )
    def test_valid_json_returned_unchanged(self, arguments: str):
        assert sanitize_tool_arguments(arguments) == arguments

    @pytest.mark.parametrize("arguments", [None, ''])
    def test_missing_arguments_becomes_empty_object(self, arguments: str | None):
        assert sanitize_tool_arguments(arguments) == '{}'

    def test_duplicated_chunk_repaired(self):
        """The exact production poison: two '{}' chunks merged into '{}{}'."""
        assert sanitize_tool_arguments('{}{}') == '{}'

    def test_trailing_garbage_repaired(self):
        assert sanitize_tool_arguments('{"command": "ls"} trailing') == '{"command": "ls"}'

    def test_truncated_arguments_fall_back_to_empty_object(self):
        assert sanitize_tool_arguments('{"command": ') == '{}'

    def test_garbage_falls_back_to_empty_object(self):
        assert sanitize_tool_arguments('not json at all') == '{}'

    @pytest.mark.parametrize(
        "arguments",
        [
            '{}{}',
            '{"command": "ls"}junk',
            '{"a": ',
            None,
            '',
        ],
    )
    def test_result_is_always_strict_parseable(self, arguments: str | None):
        result = sanitize_tool_arguments(arguments)
        orjson.loads(result)  # must not raise
