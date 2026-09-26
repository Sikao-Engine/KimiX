"""Regression tests for the reported missing-title failure of the todo tool.

Reported symptom (real agent traffic)::

    1. `todos` — Value error, Invalid todo at index 0: Field required
      Received: [{"notes":"README lines 55-63 ...","status":"done"}, ...]
      Hint: this value is invalid — check field constraints.

The agent wrote items shaped ``{notes, status}`` with no title.  Every test in
this file reproduces that class of mistake and pins the fixed behaviour:

* ``notes``-only items are *recovered* (title derived from the notes) with a
  non-blocking warning instead of a hard validation error;
* when nothing can be derived, the error names the missing field (``content``),
  echoes the keys actually sent, lists the accepted spellings, reports the full
  path for nested children, and reports *every* bad index in one round trip;
* validation never mutates the caller's argument dict (the old in-place
  ``description`` -> ``notes`` rewrite erased the evidence the model needed to
  fix its own call);
* the emitted JSON schema keeps ``content``/``status`` required on a single,
  decoder-visible array path (no ``anyOf`` dilution);
* descriptions no longer invert the salience of ``content`` (required) and
  ``notes`` (optional).
"""

from __future__ import annotations

import copy
from typing import Any

import orjson

from kimi_cli.soul.agent import Runtime
from kimi_cli.tools.todo import (
    _TODOLIST_DESCRIPTION,
    Params,
    TodoList,
)
from kosong.tooling import _parameters_schema

# The payload shape from the bug report: research citations in `notes`,
# `status` set, no title at all.
NOTES_ONLY_ITEM: dict[str, Any] = {
    "notes": (
        "README lines 55-63 (Docs/Slides/多维表格边界), line 33 (私人/团队工作区改版), "
        "line 85 (权威与兼容边界); code_map.md line 26; section 7.1-7.3 内容创作, 14.3 symbol index."
    ),
    "status": "done",
}


def _item_schema() -> dict[str, Any]:
    """The ``todos`` array-item schema as the model sees it."""
    return _parameters_schema(Params)["properties"]["todos"]["items"]


class TestNotesOnlyItemIsRecovered:
    """The reported failure must not be a failure any more."""

    async def test_notes_only_item_succeeds_with_warning(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        res = await tool.call({"todos": [dict(NOTES_ONLY_ITEM)]})
        assert not res.is_error, f"reported bug still errors: {res.message}"
        assert "Warning" in res.message, res.message

        saved = tool._load_todos()
        assert len(saved) == 1
        # Title derived from the notes; notes preserved verbatim.
        assert saved[0].title == NOTES_ONLY_ITEM["notes"][:80]
        assert saved[0].notes == NOTES_ONLY_ITEM["notes"]
        assert saved[0].status == "done"

    async def test_several_notes_only_items_all_recovered(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        items = [
            {"notes": "PRD.md §1/§4/§5/§7 + 2026-09-06 工作区增补", "status": "done"},
            {"notes": "ADR-0002/0004/0005/0006", "status": "done"},
            {"notes": "TEST_REPORT_CONTENT_WORKSPACE.md 主链/竞态", "status": "pending"},
        ]
        res = await tool.call({"todos": copy.deepcopy(items)})
        assert not res.is_error, res.message
        saved = tool._load_todos()
        assert [t.title for t in saved] == [i["notes"] for i in items]

    async def test_derived_title_uses_first_line(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        res = await tool.call(
            {"todos": [{"notes": "Fix the SSE reconnect loop\n  details here", "status": "pending"}]}
        )
        assert not res.is_error, res.message
        assert tool._load_todos()[0].title == "Fix the SSE reconnect loop"

    async def test_recovery_is_a_warning_not_a_silent_guess(self, runtime: Runtime) -> None:
        """The warning must teach the model the canonical key for next time."""
        tool = TodoList(runtime)
        res = await tool.call({"todos": [dict(NOTES_ONLY_ITEM)]})
        assert not res.is_error, res.message
        msg = res.message
        assert "`content`" in msg, msg
        assert "title" in msg, msg


class TestValidationErrorNamesTheField:
    """When recovery is impossible, say exactly what is missing and where."""

    async def test_missing_title_names_the_field(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        res = await tool.call({"todos": [{"status": "done"}]})
        assert res.is_error
        msg = res.message
        assert "'title'" in msg, msg
        assert "Field required" not in msg, msg
        # Accepted spellings + the keys actually sent, so the model can self-fix.
        assert "title" in msg and "task" in msg, msg
        assert "status" in msg, msg

    async def test_nested_child_reports_its_path(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        res = await tool.call(
            {
                "todos": [
                    {
                        "content": "root",
                        "status": "pending",
                        "children": [
                            {"content": "ok child", "status": "done"},
                            {"status": "done"},  # no title, nothing to derive
                        ],
                    }
                ]
            }
        )
        assert res.is_error
        msg = res.message
        assert "children[1]" in msg, msg
        assert "'title'" in msg, msg
        # The echoed keys must be the offending child's, not the valid root's.
        assert "Keys received for this item: [status]" in msg, msg
        # The root item is fine; blaming index 0 is the old, misleading message.
        assert "at index 0" not in msg, msg

    async def test_every_bad_index_reported_in_one_round_trip(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        res = await tool.call(
            {
                "todos": [
                    {"content": "fine", "status": "pending"},
                    {"status": "done"},
                    {"status": "pending"},
                    {"status": "in_progress"},
                ]
            }
        )
        assert res.is_error
        msg = res.message
        for idx in (1, 2, 3):
            assert f"[{idx}]" in msg, f"index {idx} not reported: {msg}"
        assert "3 of 4 todo items are invalid" in msg, msg

    async def test_single_problem_is_counted_per_item(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        res = await tool.call({"todos": [{"content": "fine", "status": "pending"}, {"status": "done"}]})
        assert res.is_error
        assert "1 of 2 todo items is invalid" in res.message, res.message

    async def test_unknown_title_key_is_reported_as_such(self, runtime: Runtime) -> None:
        """A typo'd title key must not vanish behind a bare 'Field required'."""
        tool = TodoList(runtime)
        res = await tool.call({"todos": [{"label": "ship it", "status": "pending"}]})
        assert res.is_error
        msg = res.message
        assert "'title'" in msg, msg
        assert "label" in msg, msg

    async def test_empty_title_names_the_field(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        res = await tool.call({"todos": [{"content": "   ", "status": "pending"}]})
        assert res.is_error
        assert "'title'" in res.message, res.message
        # Messages must stay readable: the pydantic text and the key echo are
        # separated by punctuation, not glued together.
        assert "only. Keys received" in res.message, res.message
    async def test_notes_only_update_recovers_with_a_warning(self, runtime: Runtime) -> None:
        """One item shape now: a notes-only item recovers to a titled item, loudly."""
        tool = TodoList(runtime)
        res = await tool.call({"updates": [{"status": "done", "notes": "did the thing"}]})
        assert not res.is_error, res.message
        assert "did the thing" in [t.title for t in tool._load_todos()]
        assert "Warning:" in res.output or "no title" in res.output
        # A bare title list still works, and `content` is still accepted.
        assert not (await tool.call({"todos": ["third item"]})).is_error
        assert not (await tool.call({"todos": [{"content": "fourth", "status": "done"}]})).is_error


class TestArgumentsAreNeverMutated:
    """The old in-place ``description`` -> ``notes`` rewrite erased evidence."""

    async def test_description_key_survives_the_call(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        args: dict[str, Any] = {
            "todos": [{"description": "Wire up SSE reconnect", "status": "done"}]
        }
        snapshot = copy.deepcopy(args)
        res = await tool.call(args)
        assert args == snapshot, f"arguments were mutated in place: {args}"
        assert not res.is_error, res.message
        assert tool._load_todos()[0].title == "Wire up SSE reconnect"

    async def test_legacy_force_mode_survives_the_call(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        args: dict[str, Any] = {
            "todos": [{"content": "solo", "status": "done"}],
            "mode": "force_overwrite",
        }
        snapshot = copy.deepcopy(args)
        res = await tool.call(args)
        assert args == snapshot, f"arguments were mutated in place: {args}"
        assert not res.is_error, res.message


class TestTitleAliasesStillWork:
    """Leniency must not regress the accepted spellings."""

    async def test_alias_spellings_accepted(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        for key in ("content", "title", "task", "todo", "item", "name"):
            res = await tool.call({"todos": [{key: f"item via {key}", "status": "pending"}]})
            assert not res.is_error, f"{key}: {res.message}"
        titles = {t.title for t in tool._load_todos()}
        assert titles == {f"item via {k}" for k in ("content", "title", "task", "todo", "item", "name")}

    async def test_bare_string_items_accepted(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        res = await tool.call({"todos": ["plain task"]})
        assert not res.is_error, res.message
        assert tool._load_todos()[0].title == "plain task"

    async def test_children_accept_bare_strings(self, runtime: Runtime) -> None:
        """``todos`` was lenient about bare strings; ``children`` was not."""
        tool = TodoList(runtime)
        res = await tool.call(
            {"todos": [{"content": "root", "status": "pending", "children": ["sub task", "other"]}]}
        )
        assert not res.is_error, res.message
        children = tool._load_todos()[0].children
        assert [c.title for c in children] == ["sub task", "other"]
        assert all(c.status == "pending" for c in children)

    async def test_single_todo_dict_still_accepted(self, runtime: Runtime) -> None:
        """Schema collapse to an array must not reject a single object."""
        tool = TodoList(runtime)
        res = await tool.call({"todos": {"content": "solo", "status": "pending"}})
        assert not res.is_error, res.message
        assert tool._load_todos()[0].title == "solo"

    async def test_update_single_dict_and_bare_string_still_accepted(self, runtime: Runtime) -> None:
        write = TodoList(runtime)
        assert not (await write.call({"todos": [{"content": "solo", "status": "pending"}]})).is_error
        tool = TodoList(runtime)
        res = await tool.call({"updates": {"title": "solo", "status": "done"}})
        assert not res.is_error, res.message
        res = await tool.call({"updates": ["solo"]})
        assert not res.is_error, res.message


class TestSchemaShape:
    """P2: required fields on a single decoder-visible path, P1: wording."""

    def test_one_item_shape_carries_writes_and_edits(self) -> None:
        """The batch synonyms now land on one field with one item shape."""
        todos = _parameters_schema(Params)["properties"]["todos"]
        assert "anyOf" not in todos
        assert todos["type"] == "array"
        item = todos["items"]
        assert "title" in item["required"]
        assert "`title`" in todos["description"]
        assert {"status", "notes", "children", "parent", "rename_to", "complete"} <= set(
            item["properties"]
        )
        # `status` is optional on the wire: omitted means keep / default pending.
        assert "status" not in item["required"]

    def test_todos_is_a_plain_array_without_anyof(self) -> None:
        todos = _parameters_schema(Params)["properties"]["todos"]
        assert "anyOf" not in todos, "single-item branch dilutes `required`"
        assert todos["type"] == "array"
        assert set(todos["items"]["required"]) == {"title"}

    def test_item_schema_emitted_once_per_nesting_level(self) -> None:
        schema = _parameters_schema(Params)
        # content/status/notes must not be duplicated 4x as before.
        assert str(schema).count("imperative task title") <= 2

    def test_title_description_is_actionable(self) -> None:
        desc = _item_schema()["properties"]["title"]["description"]
        assert "required" in desc.lower(), desc
        assert "report item shape" not in desc, desc
        # the retired spelling is documented as an accepted alias
        assert "content" in desc, desc

    def test_notes_description_does_not_outweigh_required_fields(self) -> None:
        desc = _item_schema()["properties"]["notes"]["description"]
        assert "MUST" not in desc, desc
        assert "optional" in desc.lower(), desc

    def test_tool_description_names_the_title_key(self) -> None:
        assert "`title`" in _TODOLIST_DESCRIPTION, _TODOLIST_DESCRIPTION
        # The "edits = status + notes" wording was the cue that produced the bug.
        assert "edits (status, notes" not in _TODOLIST_DESCRIPTION
        # One tool: the description must not send the model to a sibling.
        from kimi_cli.tools import RETIRED_TODO_TOOL_NAMES
        for retired in RETIRED_TODO_TOOL_NAMES:
            assert retired not in _TODOLIST_DESCRIPTION

    def test_status_wording_matches_the_schema(self) -> None:
        desc = _item_schema()["properties"]["status"]["description"]
        assert "pending, in_progress, done" in desc, desc
        assert "completed" in desc, desc  # accepted alias, documented

    def test_single_tool_fits_the_token_budget(self) -> None:
        """One tool must cost clearly less than the two it replaced.

        Measured before the merge: the two retired tools cost 4,452 + 5,111 = 9,563
        characters of schema + description per request.
        """
        schema = orjson.dumps(_parameters_schema(Params)).decode("utf-8")
        total = len(schema) + len(_TODOLIST_DESCRIPTION)
        assert total < 7_000, total
        assert total < 9_563 * 0.75, total
