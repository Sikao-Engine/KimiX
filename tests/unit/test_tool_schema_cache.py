"""Regression tests for the memoized tool parameter-schema handling.

Building a toolset is one of the most expensive parts of creating a CLI, and
``/clear`` recreates the whole CLI. Two things are repeated needlessly on every
rebuild:

* validating each tool's JSON Schema against the Draft 2020-12 meta-schema
  (tens of milliseconds per tool for a realistic schema), and
* deriving each tool's JSON Schema from its pydantic params model.

Both are derived from static classes, so they are memoized. These tests pin
that the results stay correct (invalid schemas still raise, callers never share
mutable state).
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from kosong import tooling
from kosong.tooling import CallableTool2, Tool, _parameters_schema, _parameters_schema_json


class _SampleParams(BaseModel):
    """A sample params model."""

    path: str
    limit: int = 100
    nested: dict[str, int] = {}


class _OtherParams(BaseModel):
    value: bool = False


class _SampleTool(CallableTool2[_SampleParams]):
    name = "sample"
    description = "A sample tool."
    params = _SampleParams

    async def __call__(self, params: _SampleParams):  # noqa: ANN204
        return params


@pytest.fixture(autouse=True)
def _clear_caches():
    _parameters_schema_json.cache_clear()
    tooling._validate_parameters_schema.cache_clear()
    yield
    _parameters_schema_json.cache_clear()
    tooling._validate_parameters_schema.cache_clear()


class _CountingValidator:
    def __init__(self, inner) -> None:  # noqa: ANN001
        self.inner = inner
        self.calls = 0

    def validate(self, schema) -> None:  # noqa: ANN001
        self.calls += 1
        self.inner.validate(schema)


# ── meta-schema validation memoization ───────────────────────────────────


def test_repeated_identical_schemas_validate_once(monkeypatch: pytest.MonkeyPatch) -> None:
    counting = _CountingValidator(tooling._META_SCHEMA_VALIDATOR)
    monkeypatch.setattr(tooling, "_META_SCHEMA_VALIDATOR", counting)

    schema = _SampleParams.model_json_schema()
    for _ in range(5):
        Tool(name="sample", description="d", parameters=schema)

    assert counting.calls == 1


def test_key_order_does_not_defeat_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    counting = _CountingValidator(tooling._META_SCHEMA_VALIDATOR)
    monkeypatch.setattr(tooling, "_META_SCHEMA_VALIDATOR", counting)

    Tool(name="a", description="d", parameters={"type": "object", "title": "t"})
    Tool(name="b", description="d", parameters={"title": "t", "type": "object"})

    assert counting.calls == 1


def test_distinct_schemas_are_validated_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    counting = _CountingValidator(tooling._META_SCHEMA_VALIDATOR)
    monkeypatch.setattr(tooling, "_META_SCHEMA_VALIDATOR", counting)

    Tool(name="a", description="d", parameters={"type": "object"})
    Tool(name="b", description="d", parameters={"type": "array"})

    assert counting.calls == 2


def test_invalid_schema_still_raises_every_time(monkeypatch: pytest.MonkeyPatch) -> None:
    import jsonschema

    bad = {"type": "not-a-real-json-schema-type"}
    for _ in range(3):
        with pytest.raises(jsonschema.ValidationError):
            Tool(name="bad", description="d", parameters=bad)


def test_schema_validator_never_caches_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    counting = _CountingValidator(tooling._META_SCHEMA_VALIDATOR)
    monkeypatch.setattr(tooling, "_META_SCHEMA_VALIDATOR", counting)

    import jsonschema

    with pytest.raises(jsonschema.ValidationError):
        Tool(name="bad", description="d", parameters={"type": "nope"})
    with pytest.raises(jsonschema.ValidationError):
        Tool(name="bad", description="d", parameters={"type": "nope"})

    assert counting.calls == 2


# ── params -> JSON schema memoization ────────────────────────────────────


def test_parameters_schema_is_cached() -> None:
    _parameters_schema(_SampleParams)
    info = _parameters_schema_json.cache_info()
    _parameters_schema(_SampleParams)
    after = _parameters_schema_json.cache_info()
    assert after.misses == info.misses
    assert after.hits == info.hits + 1


def test_parameters_schema_returns_independent_structures() -> None:
    first = _parameters_schema(_SampleParams)
    second = _parameters_schema(_SampleParams)

    assert first == second
    # Callers must not share nested mutable state with the cache.
    assert first is not second
    assert first["properties"] is not second["properties"]
    first["properties"]["limit"]["type"] = "mutated"
    assert _parameters_schema(_SampleParams)["properties"]["limit"]["type"] == "integer"


def test_parameters_schema_differs_per_model() -> None:
    assert _parameters_schema(_SampleParams) != _parameters_schema(_OtherParams)


def test_parameters_schema_is_a_valid_draft_2020_12_schema() -> None:
    import jsonschema

    schema = _parameters_schema(_SampleParams)
    assert "$defs" not in schema, "schema should be de-referenced"
    jsonschema.Draft202012Validator.check_schema(schema)


def test_tool_construction_reuses_the_cached_schema() -> None:
    _SampleTool()
    misses = _parameters_schema_json.cache_info().misses
    _SampleTool()
    info = _parameters_schema_json.cache_info()
    assert info.misses == misses
    assert info.hits >= 1

    # Each instance still gets its own schema object.
    a, b = _SampleTool(), _SampleTool()
    assert a.base.parameters == b.base.parameters
    assert a.base.parameters is not b.base.parameters
