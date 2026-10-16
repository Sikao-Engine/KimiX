"""Tests for shared tool utilities."""

from pydantic import BaseModel

from kimi_cli.tools.utils import repair_json_string, repair_tool_arguments


class _SampleParams(BaseModel):
    items: list[dict[str, str]] | None = None
    query: str | None = None
    count: int | None = None


class _UnionStrParams(BaseModel):
    # Mirrors the subagent ``prompt`` field: a bare string is valid input and
    # must survive untouched even when it starts with '[' or '{'.
    prompt: str | list[str] = ""
    payload: list[str] | None = None


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

    def test_none_returns_empty_dict(self):
        assert repair_tool_arguments(_SampleParams, None) == {}

    def test_list_returns_empty_dict(self):
        assert repair_tool_arguments(_SampleParams, []) == {}

    def test_tuple_like_returns_empty_dict(self):
        """A malformed positional/tuple-like input must not raise a dict() error."""
        assert repair_tool_arguments(_SampleParams, [("a", "b", "c")]) == {}

    def test_json_string_object_parsed(self):
        result = repair_tool_arguments(_SampleParams, '{"query": "hello"}')
        assert result == {"query": "hello"}

    def test_json_string_non_object_returns_empty(self):
        result = repair_tool_arguments(_SampleParams, '"hello"')
        assert result == {}


class TestRepairStrUnionField:
    def test_str_member_union_not_json_parsed(self):
        """A string valid for a ``str | list[str]`` field must never be repaired.

        The old rule only skipped plain ``str``/``Optional[str]`` fields, so a
        widened union (subagent ``prompt``) got JSON-parsed: a legitimate prompt
        beginning with ``[`` or ``{`` was silently turned into a list/dict.
        """
        for text in (
            "[read the file] and summarize",
            "{a: 1} fix this",
            "[1, 2, 3]",
        ):
            result = repair_tool_arguments(_UnionStrParams, {"prompt": text})
            assert result["prompt"] == text

    def test_non_string_field_still_repaired(self):
        args = {"payload": '["a", "b"]', "prompt": "do it"}
        result = repair_tool_arguments(_UnionStrParams, args)
        assert result["payload"] == ["a", "b"]
        assert result["prompt"] == "do it"
