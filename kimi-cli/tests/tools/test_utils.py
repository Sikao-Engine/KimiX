"""Tests for shared tool utilities."""

import pytest
from pydantic import BaseModel

from kimi_cli.tools.utils import repair_json_string, repair_tool_arguments


class _SampleParams(BaseModel):
    items: list[dict[str, str]] | None = None
    query: str | None = None
    count: int | None = None


class TestRepairJsonString:
    def test_valid_array(self):
        assert repair_json_string('[{"a": "b"}]') == [{"a": "b"}]

    def test_valid_object(self):
        assert repair_json_string('{"a": "b"}') == {"a": "b"}

    def test_repairable_missing_bracket(self):
        assert repair_json_string('[{"a": "b"') == [{"a": "b"}]

    def test_not_json_returns_none(self):
        assert repair_json_string("hello world") is None

    def test_repairable_missing_colon(self):
        assert repair_json_string('{"a" "b"}') == {"a": "b"}

    def test_empty_string_returns_none(self):
        assert repair_json_string("") is None
        assert repair_json_string("   ") is None


class TestRepairToolArguments:
    def test_repairs_complex_field(self):
        args = {"items": '[{"title": "T"}]', "query": "hello"}
        result = repair_tool_arguments(_SampleParams, args)
        assert result["items"] == [{"title": "T"}]
        assert result["query"] == "hello"

    def test_leaves_string_field_alone(self):
        args = {"query": '{"not": "json"}'}
        result = repair_tool_arguments(_SampleParams, args)
        assert result["query"] == '{"not": "json"}'

    def test_unknown_field_left_alone(self):
        args = {"unknown": '["x"]'}
        result = repair_tool_arguments(_SampleParams, args)
        assert result["unknown"] == '["x"]'

    def test_non_string_values_unchanged(self):
        args = {"count": 42, "items": [{"a": "b"}]}
        result = repair_tool_arguments(_SampleParams, args)
        assert result["count"] == 42
        assert result["items"] == [{"a": "b"}]
