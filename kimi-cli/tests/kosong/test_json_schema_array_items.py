"""Tests for the Gemini-compatibility JSON Schema normalizer.

Gemini converts tool parameter schemas into its ``Schema`` protobuf, which
requires an ``items`` sub-schema on every array. Pydantic emits fixed-length
tuples as ``{"type": "array", "prefixItems": [...], "minItems": n,
"maxItems": n}`` — no ``items`` — so a single ``tuple[int, int]`` parameter made
every ``GenerateContent`` request fail with::

    * GenerateContentRequest.tools[0].function_declarations[15].parameters
      .properties[coordinate].any_of[0].items: missing field.

``ensure_array_items`` is the provider-side patch. It must add exactly the
missing key, must never mutate its input (tool schemas are shared across
providers within a session), and must leave non-array schemas untouched.
"""

from __future__ import annotations

from pydantic import BaseModel

from kosong.utils.jsonschema import ensure_array_items


def _coordinate_any_of() -> dict[str, object]:
    """The exact shape Pydantic emits for ``coordinate: tuple[int, int] | None``."""

    class Params(BaseModel):
        coordinate: tuple[int, int] | None = None

    return Params.model_json_schema()


def test_tuple_prefix_items_get_synthesized_integer_items() -> None:
    schema = _coordinate_any_of()

    patched = ensure_array_items(schema)  # type: ignore[arg-type]

    array_branch = patched["properties"]["coordinate"]["anyOf"][0]  # type: ignore[index]
    assert array_branch["type"] == "array"
    assert array_branch["prefixItems"] == [{"type": "integer"}, {"type": "integer"}]
    # The synthesized items schema is what Gemini validates against.
    assert array_branch["items"] == {"type": "integer"}
    # Positional constraints are preserved for providers that understand them.
    assert array_branch["minItems"] == 2
    assert array_branch["maxItems"] == 2


def test_nullable_array_missing_items_is_patched_in_every_branch() -> None:
    schema = {
        "type": "object",
        "properties": {
            "pair": {
                "anyOf": [
                    {"type": "array", "prefixItems": [{"type": "number"}, {"type": "number"}]},
                    {"type": "array"},
                ],
            }
        },
    }

    patched = ensure_array_items(schema)

    branch_with_prefix, bare_branch = patched["properties"]["pair"]["anyOf"]
    assert branch_with_prefix["items"] == {"type": "number"}
    # A bare `{"type": "array"}` carries no element information; `{}` keeps it
    # unconstrained and is accepted by Gemini (`items` must merely be present).
    assert bare_branch["items"] == {}


def test_prefix_items_with_mixed_types_become_any_of() -> None:
    schema = {
        "type": "array",
        "minItems": 2,
        "maxItems": 2,
        "prefixItems": [{"type": "string"}, {"type": "integer"}],
    }

    patched = ensure_array_items(schema)

    assert patched["items"] == {"anyOf": [{"type": "integer"}, {"type": "string"}]}


def test_prefix_items_number_and_integer_collapse_to_number() -> None:
    schema = {
        "type": "array",
        "prefixItems": [{"type": "integer"}, {"type": "number"}],
    }

    assert ensure_array_items(schema)["items"] == {"type": "number"}


def test_prefix_items_without_plain_type_falls_back_to_unconstrained_items() -> None:
    schema = {
        "type": "array",
        "prefixItems": [{"enum": ["a", "b"]}, {"$ref": "#/x"}],
    }

    # Unification is impossible without guessing, so keep the array permissive.
    assert ensure_array_items(schema)["items"] == {}


def test_identical_prefix_items_are_copied_including_constraints() -> None:
    schema = {
        "type": "array",
        "prefixItems": [
            {"type": "integer", "minimum": 0},
            {"type": "integer", "minimum": 0},
        ],
    }

    patched = ensure_array_items(schema)

    items = patched["items"]
    assert items == {"type": "integer", "minimum": 0}
    # A copy, not an alias: mutating the patched items must not touch the source.
    items["minimum"] = 5
    assert schema["prefixItems"][0]["minimum"] == 0


def test_draft7_tuple_items_array_is_collapsed_to_one_schema() -> None:
    schema = {"type": "array", "items": [{"type": "string"}, {"type": "string"}]}

    assert ensure_array_items(schema)["items"] == {"type": "string"}


def test_existing_items_schema_is_left_alone() -> None:
    schema = {
        "type": "array",
        "items": {"type": "string", "description": "keep me"},
        "prefixItems": [{"type": "integer"}],
    }

    assert ensure_array_items(schema)["items"] == {
        "type": "string",
        "description": "keep me",
    }


def test_nested_arrays_are_patched_through_every_child_slot() -> None:
    schema = {
        "type": "object",
        "properties": {
            "matrix": {
                "type": "array",
                "items": {
                    "type": "array",
                    "prefixItems": [{"type": "boolean"}, {"type": "boolean"}],
                },
            },
            "objects": {
                "type": "array",
                "prefixItems": [
                    {
                        "type": "object",
                        "properties": {"point": {"type": "array"}},
                    }
                ],
            },
        },
    }

    patched = ensure_array_items(schema)

    inner = patched["properties"]["matrix"]["items"]
    assert inner["items"] == {"type": "boolean"}
    nested_point = patched["properties"]["objects"]["prefixItems"][0]["properties"]["point"]
    assert nested_point["items"] == {}


def test_input_schema_is_not_mutated() -> None:
    schema = _coordinate_any_of()

    ensure_array_items(schema)  # type: ignore[arg-type]

    array_branch = schema["properties"]["coordinate"]["anyOf"][0]  # type: ignore[index]
    assert "items" not in array_branch


def test_non_array_schemas_are_returned_unchanged() -> None:
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "maxLength": 10},
            "mode": {"enum": ["fast", "slow"]},
        },
        "required": ["name"],
    }

    assert ensure_array_items(schema) == schema


def test_type_list_containing_array_is_patched() -> None:
    schema = {"type": ["array", "null"], "prefixItems": [{"type": "integer"}]}

    patched = ensure_array_items(schema)

    assert patched["items"] == {"type": "integer"}
    assert patched["type"] == ["array", "null"]


def test_todo_like_tool_schema_is_untouched() -> None:
    """A realistic non-tuple tool schema must come out byte-identical."""

    class Params(BaseModel):
        action: str
        coordinate: tuple[int, int] | None = None
        element: int | None = None

    schema = Params.model_json_schema()
    patched = ensure_array_items(schema)  # type: ignore[arg-type]

    assert patched["properties"]["action"] == schema["properties"]["action"]
    assert patched["properties"]["element"] == schema["properties"]["element"]
    assert patched["required"] == schema["required"]
