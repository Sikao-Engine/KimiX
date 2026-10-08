"""G4 gate probes for the plan tools: K10 WritePlan, K11 ReadPlan, K12 EditPlan.

`tests/test_note.py` already covers the mainstream paths (overwrite/append, parent
dirs, file-not-found, MAX_LINES/MAX_BYTES). This suite pins the plan §4.3 K10-K12
items it does not: alias parity, the exact validation guidance, the boundary
values of every numeric field, `_find_similar` at/above/below its cutoff, CRLF
normalisation, and the no-match error contract.

Where the implementation is weaker than the plan's probe text, the probe pins the
*actual* behaviour and the gap is recorded as a finding (F-40, F-41, F-42) instead
of asserting something that is not there.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from kimi_cli.session import Session
from kimi_cli.tools import SkipThisTool
from pydantic import ValidationError

import kimix.tools.note as note
from kimix.tools.note import (
    MAX_LINES,
    EditPlan,
    EditPlanParams,
    ReadPlan,
    ReadPlanParams,
    WritePlan,
    WritePlanParams,
)


@pytest.fixture(autouse=True)
def _enable_plan():
    note._enable_plan = True
    yield
    note._enable_plan = False


@pytest.fixture
def session(tmp_path: Path) -> MagicMock:
    s = MagicMock(spec=Session)
    s.custom_data = {"plan_writing_path": tmp_path / "plan.md"}
    return s


@pytest.fixture
def plan_path(session: MagicMock) -> Path:
    return Path(session.custom_data["plan_writing_path"])


# --------------------------------------------------------------------------- #
# K10 WritePlan
# --------------------------------------------------------------------------- #


def test_writepan_alias_text_and_content_are_equivalent() -> None:
    assert WritePlanParams(content="a").content == "a"
    assert WritePlanParams(text="a").content == "a"


def test_writepan_mode_defaults_and_values() -> None:
    assert WritePlanParams(content="a").mode == "overwrite"
    assert WritePlanParams(content="a", mode="append").mode == "append"


def test_writepan_content_is_required() -> None:
    with pytest.raises(ValidationError):
        WritePlanParams()


def test_writepan_raises_skip_this_tool_when_disabled() -> None:
    note._enable_plan = False
    with pytest.raises(SkipThisTool):
        WritePlan(session=MagicMock(spec=Session, custom_data={}))


async def test_writepan_missing_plan_path_is_an_error(session: MagicMock) -> None:
    session.custom_data = {}
    result = await WritePlan(session=session)(WritePlanParams(content="x"))
    assert result.is_error
    assert "plan_writing_path" in result.message
    assert result.brief == "invalid tool."


async def test_writepan_overwrite_truncates_and_append_extends(
    session: MagicMock, plan_path: Path
) -> None:
    tool = WritePlan(session=session)
    await tool(WritePlanParams(content="first\n"))
    # NOTE: the plan file is opened in text mode, so on Windows the bytes carry
    # CRLF.  read_text() (universal newlines) pins the logical content the model
    # reads back - the byte-level newline is finding F-40.
    assert plan_path.read_text(encoding="utf-8") == "first\n"

    await tool(WritePlanParams(content="second\n"))
    assert plan_path.read_text(encoding="utf-8") == "second\n"

    await tool(WritePlanParams(content="third\n", mode="append"))
    assert plan_path.read_text(encoding="utf-8") == "second\nthird\n"


async def test_writepan_creates_parent_directories(session: MagicMock) -> None:
    nested = Path(session.custom_data["plan_writing_path"]).parent / "a" / "b" / "plan.md"
    session.custom_data["plan_writing_path"] = nested
    result = await WritePlan(session=session)(WritePlanParams(content="deep\n"))
    assert not result.is_error
    assert nested.read_text(encoding="utf-8") == "deep\n"


async def test_writepan_success_sets_plan_called(session: MagicMock) -> None:
    session.custom_data.pop("plan_called", None)
    result = await WritePlan(session=session)(WritePlanParams(content="x"))
    assert not result.is_error
    assert session.custom_data.get("plan_called") is True


# --------------------------------------------------------------------------- #
# K11 ReadPlan
# --------------------------------------------------------------------------- #


def test_readplan_line_offset_zero_is_rejected_with_guidance() -> None:
    with pytest.raises(ValidationError) as exc:
        ReadPlanParams(line_offset=0)
    assert "line_offset" in str(exc.value)


def test_readplan_line_offset_below_minus_max_lines_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ReadPlanParams(line_offset=-(MAX_LINES + 1))


def test_readplan_numeric_field_boundaries() -> None:
    assert ReadPlanParams(n_lines=1).n_lines == 1
    assert ReadPlanParams(n_lines=MAX_LINES).n_lines == MAX_LINES
    with pytest.raises(ValidationError):
        ReadPlanParams(n_lines=0)
    assert ReadPlanParams(max_char=0).max_char == 0
    assert ReadPlanParams(char_offset=0).char_offset == 0
    with pytest.raises(ValidationError):
        ReadPlanParams(max_char=-1)
    with pytest.raises(ValidationError):
        ReadPlanParams(char_offset=-1)


async def test_readplan_negative_one_reads_the_tail(session: MagicMock, plan_path: Path) -> None:
    plan_path.write_text("l1\nl2\nl3\n", encoding="utf-8")
    result = await ReadPlan(session=session)(ReadPlanParams(line_offset=-1))
    assert not result.is_error
    assert "l3" in result.output


async def test_readplan_missing_file_is_an_error(session: MagicMock, plan_path: Path) -> None:
    assert not plan_path.exists()
    result = await ReadPlan(session=session)(ReadPlanParams())
    assert result.is_error
    assert plan_path.name in result.message


async def test_readplan_missing_plan_path_is_an_error(session: MagicMock) -> None:
    session.custom_data = {}
    result = await ReadPlan(session=session)(ReadPlanParams())
    assert result.is_error
    assert "plan_writing_path" in result.message


# --------------------------------------------------------------------------- #
# K12 EditPlan
# --------------------------------------------------------------------------- #


def test_editplan_alias_edits_and_edit_are_equivalent() -> None:
    """`edit` accepts the single form and `edits` the list form, same content."""
    single = EditPlanParams(edit={"old": "a", "new": "b"})
    listed = EditPlanParams(edits=[{"old": "a", "new": "b"}])

    assert single.edit.old == "a"
    assert single.edit.new == "b"
    assert isinstance(listed.edit, list) and len(listed.edit) == 1
    assert listed.edit[0].old == "a"
    assert listed.edit[0].new == "b"


def test_editplan_edit_is_required() -> None:
    with pytest.raises(ValidationError):
        EditPlanParams()


def test_editplan_find_similar_finds_the_exact_line(session: MagicMock) -> None:
    tool = EditPlan(session=session)
    assert tool._find_similar("hello world", "hello world\nunrelated\n", cutoff=100.0) == (
        "hello world"
    )


def test_editplan_find_similar_below_cutoff_returns_none(session: MagicMock) -> None:
    """Boundary: a 100-cutoff request must reject a 1-character-off target."""
    tool = EditPlan(session=session)
    assert tool._find_similar("hello worl", "hello world\nunrelated\n", cutoff=100.0) is None


def test_editplan_find_similar_above_cutoff_returns_the_line(session: MagicMock) -> None:
    tool = EditPlan(session=session)
    assert tool._find_similar("hello worl", "hello world\nunrelated\n", cutoff=75.0) == (
        "hello world"
    )


def test_editplan_normalizes_crlf_only(session: MagicMock) -> None:
    """CRLF -> LF (documented). A lone CR is deliberately left alone (F-41)."""
    tool = EditPlan(session=session)
    assert tool._normalize_line_endings("a\r\nb\r\n") == "a\nb\n"
    assert tool._normalize_line_endings("a\rb") == "a\rb"


async def test_editplan_applies_an_exact_edit(session: MagicMock, plan_path: Path) -> None:
    plan_path.write_text("alpha\nbeta\n", encoding="utf-8")
    result = await EditPlan(session=session)(EditPlanParams(edit={"old": "beta", "new": "gamma"}))
    assert not result.is_error
    assert plan_path.read_text(encoding="utf-8") == "alpha\ngamma\n"


async def test_editplan_no_match_is_an_error_with_a_clear_brief(
    session: MagicMock, plan_path: Path
) -> None:
    """The plan's K12 probe 16 also asks the message to name the missing target.

    It does NOT (finding F-42), so this pins today's wording instead of asserting
    a behaviour that is not there.
    """
    plan_path.write_text("alpha\nbeta\n", encoding="utf-8")
    result = await EditPlan(session=session)(
        EditPlanParams(edit={"old": "zzzzzzzzzzzzzzzzzzzz", "new": "x"})
    )
    assert result.is_error
    assert result.brief == "No replacements made"
    assert result.message == (
        "No replacements were made. The old string was not found in the plan file."
    )


async def test_editplan_missing_plan_path_is_an_error(session: MagicMock) -> None:
    session.custom_data = {}
    result = await EditPlan(session=session)(EditPlanParams(edit={"old": "a", "new": "b"}))
    assert result.is_error
    assert "plan_writing_path" in result.message
