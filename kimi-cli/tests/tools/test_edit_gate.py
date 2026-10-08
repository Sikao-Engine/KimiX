"""C10 hard-gate G4 behavioural probe suite for the multi-mode ``edit`` tool.

Adversarial probes for ``kimi_cli.tools.file.edit`` (EditFile): mode detection,
every public alias, boundary values, hostile/empty/oversized payloads and the
error contract.  Every probe asserts concrete values — never only
``is not None``.

Required probes (plan §4.4 C10, 12 items) are labelled ``PROBE 01`` … ``PROBE 12``;
``PROBE 13``+ are extra adversarial cover.

Evidence file: ``reviews/tools/gates/C10.txt``.
Report: ``reviews/tools/C10-edit-editfile.md``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import orjson
import pytest
from kaos.path import KaosPath
from pydantic import ValidationError

from kimi_cli.session import Session
from kimi_cli.soul.agent import Runtime
from kimi_cli.soul.approval import Approval, ApprovalResult
from kimi_cli.tools.file import EditFile as PkgEditFile
from kimi_cli.tools.file.edit import EditFile
from kimi_cli.tools.file.edit.modes import MODE_REGISTRY
from kimi_cli.tools.file.edit.params import (
    EditParams,
    ReplaceEditItem,
    detect_mode,
    normalize_edit_mode,
)
from kimi_cli.tools.file.replace import Edit as ShimEdit
from kimi_cli.tools.file.replace import EditFile as ShimEditFile
from kimi_cli.tools.file.replace import Params as ShimParams
from tests.conftest import tool_call_context

OURS = "<" * 7
SEP = "=" * 7
THEIRS = ">" * 7


def _conflicted(before: str = "before", after: str = "after") -> str:
    return f"{before}\n{OURS} HEAD\nours\n{SEP}\ntheirs\n{THEIRS} branch\n{after}\n"


# ---------------------------------------------------------------------------
# PROBE 01 — mode=auto detection for replace-shaped vs sloppy-shaped payloads
# ---------------------------------------------------------------------------


def test_probe01_auto_mode_detection_replace_and_sloppy_shapes() -> None:
    replace_shaped = EditParams(path="a.txt", old_string="old", new_string="new")
    assert replace_shaped.resolved_mode == "replace"
    assert detect_mode(replace_shaped) == "replace"

    list_shaped = EditParams(path="a.txt", edits=[{"old": "old", "new": "new"}])
    assert list_shaped.resolved_mode == "replace"
    assert detect_mode(list_shaped) == "replace"

    sloppy_shaped = EditParams(input="§a.txt\n⟪old│new⟫\n")
    assert sloppy_shaped.resolved_mode == "sloppy"
    assert detect_mode(sloppy_shaped) == "sloppy"

    # § may be indented and preceded by blank lines.
    indented = EditParams(input="\n\n   §a.txt\n⟪a│b⟫")
    assert indented.resolved_mode == "sloppy"

    # input that does not start with § is neither sloppy nor replace-shaped.
    with pytest.raises(ValidationError):
        EditParams(input="no section marker here")

    # no payload at all is ambiguous -> refused.
    with pytest.raises(ValidationError):
        EditParams(path="a.txt")

    # mode string normalisation contract (helper is usable standalone) ...
    assert normalize_edit_mode("REPLACE") == "replace"
    assert normalize_edit_mode("replace") == "replace"
    assert normalize_edit_mode("sloppy") == "sloppy"
    assert normalize_edit_mode("sloppy-mode") is None
    assert normalize_edit_mode("patch") is None
    assert normalize_edit_mode("") is None
    # ... but surrounding whitespace is NOT stripped, so "  sloppy  " is rejected
    # (report §3 finding S1-6).
    assert normalize_edit_mode("  sloppy  ") is None


# ---------------------------------------------------------------------------
# PROBE 02 — explicit mode=replace and mode=sloppy
# ---------------------------------------------------------------------------


async def test_probe02_explicit_replace_and_sloppy_modes(
    edit_file_tool: EditFile, temp_work_dir: KaosPath
) -> None:
    replace_file = temp_work_dir / "explicit_replace.txt"
    await replace_file.write_text("alpha beta\n")
    rep = await edit_file_tool(
        EditParams(
            path=str(replace_file),
            mode="replace",
            edits=[ReplaceEditItem(old="alpha", new="ALPHA")],
        )
    )
    assert not rep.is_error
    assert await replace_file.read_text() == "ALPHA beta\n"

    sloppy_file = temp_work_dir / "explicit_sloppy.txt"
    await sloppy_file.write_text("alpha beta\n")
    slo = await edit_file_tool(
        EditParams(mode="sloppy", input=f"§{sloppy_file}\n⟪beta│BETA⟫\n")
    )
    assert not slo.is_error
    assert slo.message == f"Edited `{str(sloppy_file).replace(chr(92), '/')}`."
    assert await sloppy_file.read_text() == "alpha BETA\n"

    # An explicit, unsupported mode is rejected at validation time.
    with pytest.raises(ValidationError):
        EditParams(mode="patch", edits=[{"old": "a", "new": "b"}])

    # The `mode` field is a closed Literal, so the case/whitespace tolerance
    # implemented in normalize_edit_mode() is UNREACHABLE through this field:
    # "REPLACE" is refused before the custom validator runs (report §3 S1-6).
    with pytest.raises(ValidationError):
        EditParams(mode="REPLACE", edits=[{"old": "a", "new": "b"}])
    with pytest.raises(ValidationError):
        EditParams(mode=" Sloppy ", input="§p\n⟪a│b⟫")


# ---------------------------------------------------------------------------
# PROBE 03 — replace_all=True with max_replacements boundary
# ---------------------------------------------------------------------------


async def test_probe03_replace_all_max_replacements_boundary(
    edit_file_tool: EditFile, temp_work_dir: KaosPath
) -> None:
    capped = temp_work_dir / "capped.txt"
    await capped.write_text("a a a a a\n")
    res = await edit_file_tool(
        EditParams(
            path=str(capped),
            edits=[
                ReplaceEditItem(
                    old="a", new="A", replace_all=True, max_replacements=2
                )
            ],
        )
    )
    assert not res.is_error
    assert "1 edit(s) with 2 total replacement(s)" in res.message
    assert await capped.read_text() == "A A a a a\n"

    capped_high = temp_work_dir / "capped_high.txt"
    await capped_high.write_text("b b\n")
    res2 = await edit_file_tool(
        EditParams(
            path=str(capped_high),
            edits=[
                ReplaceEditItem(old="b", new="B", replace_all=True, max_replacements=5)
            ],
        )
    )
    assert not res2.is_error
    # max_replacements larger than the occurrence count caps at the real count.
    assert "1 edit(s) with 2 total replacement(s)" in res2.message
    assert await capped_high.read_text() == "B B\n"

    # max_replacements is >= 1; 0 is a schema violation.
    with pytest.raises(ValidationError):
        ReplaceEditItem(old="a", new="b", replace_all=True, max_replacements=0)
    with pytest.raises(ValidationError):
        EditParams(
            path="p",
            edits=[{"old": "a", "new": "b", "replace_all": True, "max_replacements": -1}],
        )

    # max_replacements without replace_all is ignored (documented single-shot path).
    ignored = temp_work_dir / "ignored_cap.txt"
    await ignored.write_text("x x x\n")
    res3 = await edit_file_tool(
        EditParams(
            path=str(ignored),
            edits=[ReplaceEditItem(old="x", new="Y", max_replacements=1)],
        )
    )
    assert not res3.is_error
    assert "1 edit(s) with 1 total replacement(s)" in res3.message
    assert await ignored.read_text() == "Y x x\n"


# ---------------------------------------------------------------------------
# PROBE 04 — single-edit shorthand (old/new) equals the list form
# ---------------------------------------------------------------------------


async def test_probe04_single_edit_shorthand_equals_list_form(
    edit_file_tool: EditFile, temp_work_dir: KaosPath
) -> None:
    shorthand = EditParams(path="p.txt", old_string="old", new_string="new")
    explicit = EditParams(path="p.txt", edits=[{"old": "old", "new": "new"}])
    assert shorthand.edit == explicit.edit
    assert shorthand.edit == [ReplaceEditItem(old="old", new="new")]

    file_a = temp_work_dir / "shorthand.txt"
    file_b = temp_work_dir / "listform.txt"
    await file_a.write_text("old text\n")
    await file_b.write_text("old text\n")

    res_a = await edit_file_tool(
        EditParams(path=str(file_a), old_string="old", new_string="new")
    )
    res_b = await edit_file_tool(
        EditParams(path=str(file_b), edits=[ReplaceEditItem(old="old", new="new")])
    )
    assert not res_a.is_error and not res_b.is_error
    assert res_a.message == res_b.message
    assert await file_a.read_text() == await file_b.read_text() == "new text\n"

    # `edit` and `edits` are the same field under two spellings.
    via_edit = EditParams(path="p.txt", edit={"old_string": "a", "new_string": "b"})
    via_edits = EditParams(path="p.txt", edits=[{"old": "a", "new": "b"}])
    assert via_edit.edit == via_edits.edit == [ReplaceEditItem(old="a", new="b")]


# ---------------------------------------------------------------------------
# PROBE 05 — fuzzy: whitespace-only drift succeeds, below threshold refuses
# ---------------------------------------------------------------------------


def test_probe05_fuzzy_whitespace_drift_and_threshold_boundary() -> None:
    tool = object.__new__(EditFile)
    content = "hello world  \nnext line"

    # whitespace-only drift -> strip match, counted, no fuzzy marker
    result, count, suggestion = tool._apply_edit(
        content, ReplaceEditItem(old="hello world", new="hi universe")
    )
    assert count == 1
    assert suggestion is None
    assert result == "hi universe  \nnext line"

    # below the cutoff -> nothing replaced, and no near-miss suggestion
    untouched, count_bad, suggestion_bad = tool._apply_edit(
        "hello world\nfoo bar\nbaz qux",
        ReplaceEditItem(old="xyz123_not_close", new="replacement"),
    )
    assert count_bad == 0
    assert untouched == "hello world\nfoo bar\nbaz qux"
    assert suggestion_bad is None

    # exact mode never fuzzy-replaces: it only suggests
    exact_content, exact_count, exact_suggestion = tool._apply_edit(
        "helo world\nnext", ReplaceEditItem(old="hello world", new="hi", match_mode="exact")
    )
    assert exact_count == 0
    assert exact_content == "helo world\nnext"
    assert exact_suggestion == "helo world"

    # cutoff boundary: score == cutoff is accepted, score < cutoff is refused.
    assert tool._find_best_fuzzy_match("hello world", "hello world", cutoff=100.0) == (
        "hello world",
        100.0,
    )
    assert tool._find_best_fuzzy_match("hello world", "hello world", cutoff=100.1) is None
    assert tool._find_similar("hello", "hello\nworld", cutoff=100.0) == "hello"
    assert tool._find_similar("hello", "hello\nworld", cutoff=100.1) is None


# ---------------------------------------------------------------------------
# PROBE 06 — conflict markers refused; allow_conflicts=True proceeds
# ---------------------------------------------------------------------------


async def test_probe06_conflict_markers_refused_and_allowed(
    edit_file_tool: EditFile, temp_work_dir: KaosPath
) -> None:
    blocked = temp_work_dir / "blocked.txt"
    original = _conflicted()
    await blocked.write_text(original)

    res = await edit_file_tool(
        EditParams(path=str(blocked), edits=[ReplaceEditItem(old="before", new="start")])
    )
    assert res.is_error
    assert res.brief == "Conflict markers detected"
    assert "line 2" in res.message
    assert await blocked.read_text() == original

    allowed = temp_work_dir / "allowed.txt"
    await allowed.write_text(original)
    res_ok = await edit_file_tool(
        EditParams(
            path=str(allowed),
            edits=[ReplaceEditItem(old="before", new="start")],
            allow_conflicts=True,
        )
    )
    assert not res_ok.is_error
    assert await allowed.read_text() == original.replace("before", "start", 1)

    # A bare `=======` separator is treated as a marker (whole-file pre-guard).
    bare_sep = temp_work_dir / "bare_sep.txt"
    await bare_sep.write_text("a\n=======\nb\n")
    res_sep = await edit_file_tool(
        EditParams(path=str(bare_sep), edits=[ReplaceEditItem(old="a", new="A")])
    )
    assert res_sep.is_error
    assert res_sep.brief == "Conflict markers detected"

    # 8 '=' characters are NOT a 7-char marker and must not be refused.
    eight = temp_work_dir / "eight.txt"
    await eight.write_text("a\n========\nb\n")
    res_eight = await edit_file_tool(
        EditParams(path=str(eight), edits=[ReplaceEditItem(old="a", new="A")])
    )
    assert not res_eight.is_error
    assert await eight.read_text() == "A\n========\nb\n"


# ---------------------------------------------------------------------------
# PROBE 07 — parse-repair path (introduce-then-rollback) via the guard
# ---------------------------------------------------------------------------


async def test_probe07_parse_repair_introduce_then_rollback(
    edit_file_tool: EditFile, temp_work_dir: KaosPath, session: Session
) -> None:
    broken = temp_work_dir / "broken.py"
    await broken.write_text("x = 1\n")

    res = await edit_file_tool(
        EditParams(path=str(broken), edits=[ReplaceEditItem(old="x = 1", new="x =")])
    )
    assert not res.is_error
    assert "no longer parses" in res.message
    assert await broken.read_text() == "x =\n"

    log = Path(str(temp_work_dir)) / ".kimix_cache" / "edit-blackbox.jsonl"
    assert log.exists()
    records = [orjson.loads(line) for line in log.read_text().strip().splitlines()]
    assert len(records) == 1
    assert records[0]["next"] == "x =\n"
    assert records[0]["prev"] == "x = 1\n"
    assert str(records[0]["path"]).endswith("broken.py")

    # Rollback in a later call: the guard observes a clean transition again.
    rolled_back = await edit_file_tool(
        EditParams(path=str(broken), edits=[ReplaceEditItem(old="x =", new="x = 1")])
    )
    assert not rolled_back.is_error
    assert "no longer parses" not in rolled_back.message
    assert await broken.read_text() == "x = 1\n"

    # Introduce-and-restore inside ONE call never surfaces a regression note.
    churn = temp_work_dir / "churn.py"
    await churn.write_text("y = 1\n")
    churn_res = await edit_file_tool(
        EditParams(
            path=str(churn),
            edits=[
                ReplaceEditItem(old="y = 1", new="y ="),
                ReplaceEditItem(old="y =", new="y = 2"),
            ],
        )
    )
    assert not churn_res.is_error
    assert "no longer parses" not in churn_res.message
    assert await churn.read_text() == "y = 2\n"


# ---------------------------------------------------------------------------
# PROBE 08 — snapshot recorded and fs_cache invalidated
# ---------------------------------------------------------------------------


async def test_probe08_snapshot_recorded_and_fs_cache_invalidated(
    edit_file_tool: EditFile, temp_work_dir: KaosPath, session: Session
) -> None:
    from kimi_cli.tools.file.fs_cache import register_invalidator, unregister_invalidator
    from kimi_cli.tools.file.snapshot_store import (
        canonical_snapshot_key,
        get_file_snapshot_store,
    )

    seen: list[str] = []
    register_invalidator(seen.append)
    try:
        target = temp_work_dir / "snap_gate.txt"
        await target.write_text("alpha\n")
        result = await edit_file_tool(
            EditParams(path=str(target), edits=[ReplaceEditItem(old="alpha", new="beta")])
        )
    finally:
        unregister_invalidator(seen.append)

    assert not result.is_error
    key = canonical_snapshot_key(str(target))
    versions = get_file_snapshot_store(session).versions(key)
    assert versions, "post-edit snapshot version was not recorded"
    texts = [v.text for v in versions]
    assert "beta\n" in texts  # newest version is the written content
    assert "alpha\n" in texts  # the pre-edit version is retained too
    assert seen, "fs-cache invalidator was never invoked"
    assert key in seen


# ---------------------------------------------------------------------------
# PROBE 09 — multi-edit ordering and non-overlapping application
# ---------------------------------------------------------------------------


async def test_probe09_multi_edit_ordering_and_overlap(
    edit_file_tool: EditFile, temp_work_dir: KaosPath
) -> None:
    chained = temp_work_dir / "chained.txt"
    await chained.write_text("one two\n")
    res = await edit_file_tool(
        EditParams(
            path=str(chained),
            edits=[
                ReplaceEditItem(old="one", new="1"),
                ReplaceEditItem(old="1", new="uno"),
            ],
        )
    )
    assert not res.is_error
    # Edits are applied strictly in order, each observing the previous result.
    assert await chained.read_text() == "uno two\n"
    assert "2 total replacement(s)" in res.message

    reversed_file = temp_work_dir / "reversed.txt"
    await reversed_file.write_text("one two\n")
    res_rev = await edit_file_tool(
        EditParams(
            path=str(reversed_file),
            edits=[
                ReplaceEditItem(old="1", new="uno"),
                ReplaceEditItem(old="one", new="1"),
            ],
        )
    )
    # Order matters: the same two edits in the other order give a DIFFERENT file.
    assert not res_rev.is_error
    assert await reversed_file.read_text() == "1 two\n"
    assert "2 edit(s) with 1 total replacement(s)" in res_rev.message

    # Only when EVERY edit misses does the call fail with "No replacements".
    nomatch = temp_work_dir / "nomatch_multi.txt"
    await nomatch.write_text("one two\n")
    res_nomatch = await edit_file_tool(
        EditParams(
            path=str(nomatch),
            edits=[
                ReplaceEditItem(old="absent1", new="x"),
                ReplaceEditItem(old="absent2", new="y"),
            ],
        )
    )
    assert res_nomatch.is_error
    assert res_nomatch.brief == "No replacements made"
    assert await nomatch.read_text() == "one two\n"

    disjoint = temp_work_dir / "disjoint.txt"
    await disjoint.write_text("aaa bbb ccc\n")
    res_dis = await edit_file_tool(
        EditParams(
            path=str(disjoint),
            edits=[
                ReplaceEditItem(old="aaa", new="AAA"),
                ReplaceEditItem(old="ccc", new="CCC"),
            ],
        )
    )
    assert not res_dis.is_error
    assert await disjoint.read_text() == "AAA bbb CCC\n"

    # Two edits sharing an anchor: the first consumes it, the second silently
    # does nothing, and the call still reports success.  Only the aggregate
    # replacement count in the message reveals the dropped edit.
    overlap = temp_work_dir / "overlap.txt"
    await overlap.write_text("one two\n")
    res_ov = await edit_file_tool(
        EditParams(
            path=str(overlap),
            edits=[
                ReplaceEditItem(old="one", new="1"),
                ReplaceEditItem(old="one", new="2"),
            ],
        )
    )
    assert not res_ov.is_error
    assert res_ov.message.startswith("File successfully edited")
    assert "2 edit(s) with 1 total replacement(s)" in res_ov.message
    assert await overlap.read_text() == "1 two\n"

    # The same anchor twice with replace_all=True does replace both occurrences.
    overlap_all = temp_work_dir / "overlap_all.txt"
    await overlap_all.write_text("dup dup\n")
    res_ov_all = await edit_file_tool(
        EditParams(
            path=str(overlap_all),
            edits=[ReplaceEditItem(old="dup", new="D", replace_all=True)],
        )
    )
    assert not res_ov_all.is_error
    assert "2 total replacement(s)" in res_ov_all.message
    assert await overlap_all.read_text() == "D D\n"


# ---------------------------------------------------------------------------
# PROBE 10 — missing file / no-match / ambiguous match
# ---------------------------------------------------------------------------


async def test_probe10_missing_no_match_and_ambiguous_outcomes(
    edit_file_tool: EditFile, temp_work_dir: KaosPath
) -> None:
    missing = temp_work_dir / "does_not_exist.txt"
    res_missing = await edit_file_tool(
        EditParams(path=str(missing), edits=[ReplaceEditItem(old="a", new="b")])
    )
    assert res_missing.is_error
    assert res_missing.brief == "File not found"
    assert "does not exist" in res_missing.message

    empty_path = await edit_file_tool(EditParams(path="", edits=[{"old": "a", "new": "b"}]))
    assert empty_path.is_error
    assert empty_path.brief == "Empty file path"

    directory = temp_work_dir / "adir"
    await directory.mkdir(exist_ok=True)
    res_dir = await edit_file_tool(
        EditParams(path=str(directory), edits=[ReplaceEditItem(old="a", new="b")])
    )
    assert res_dir.is_error
    assert res_dir.brief == "Invalid path"
    assert "is not a file" in res_dir.message

    nomatch_file = temp_work_dir / "nomatch.txt"
    await nomatch_file.write_text("hello world\n")
    res_nomatch = await edit_file_tool(
        EditParams(path=str(nomatch_file), edits=[ReplaceEditItem(old="zzz", new="q")])
    )
    assert res_nomatch.is_error
    assert res_nomatch.brief == "No replacements made"
    assert "No replacements were made" in res_nomatch.message
    assert await nomatch_file.read_text() == "hello world\n"

    briefs = {
        res_missing.brief,
        empty_path.brief,
        res_dir.brief,
        res_nomatch.brief,
    }
    assert len(briefs) == 4

    # DEVIATION from the gate text: there is NO "ambiguous match" ToolError.
    # A repeated anchor with replace_all=False silently replaces the FIRST
    # occurrence and reports success (see report §4 (a)/(i)).
    ambiguous = temp_work_dir / "ambiguous.txt"
    await ambiguous.write_text("x x x\n")
    res_amb = await edit_file_tool(
        EditParams(path=str(ambiguous), edits=[ReplaceEditItem(old="x", new="Y")])
    )
    assert not res_amb.is_error
    assert "1 total replacement(s)" in res_amb.message
    assert await ambiguous.read_text() == "Y x x\n"


# ---------------------------------------------------------------------------
# PROBE 11 — shim identity
# ---------------------------------------------------------------------------


def test_probe11_shim_identity() -> None:
    import kimi_cli.tools.file as file_pkg
    import kimi_cli.tools.file.edit as edit_pkg
    import kimi_cli.tools.file.replace as shim

    assert shim.EditFile is edit_pkg.EditFile
    assert EditFile is edit_pkg.EditFile
    assert PkgEditFile is edit_pkg.EditFile
    assert file_pkg.EditFile is edit_pkg.EditFile
    assert ShimEditFile is EditFile
    assert shim.Params is edit_pkg.EditParams
    assert ShimParams is EditParams
    # shim.Params() must be the real params model, not a copy.
    assert ShimParams(path="p", edits=[{"old": "a", "new": "b"}]).resolved_mode == "replace"

    # DEVIATION from the gate text: the plan says `Edit is file.edit.Edit`, but
    # `kimi_cli.tools.file.edit` exports `ReplaceEditItem`, not `Edit` (see
    # report §9).  The shim's `Edit` is that very class.
    assert not hasattr(edit_pkg, "Edit")
    assert ShimEdit is edit_pkg.ReplaceEditItem
    assert ShimEdit(old="a", new="b").model_dump() == {
        "old": "a",
        "new": "b",
        "replace_all": False,
        "max_replacements": None,
        "match_mode": "fuzzy",
    }


# ---------------------------------------------------------------------------
# PROBE 12 — every EditParams alias probed
# ---------------------------------------------------------------------------


async def test_probe12_every_params_alias(
    edit_file_tool: EditFile, temp_work_dir: KaosPath
) -> None:
    # file_path / path
    assert EditParams(file_path="p", edits=[{"old": "a", "new": "b"}]).file_path == "p"
    assert EditParams(path="p", edits=[{"old": "a", "new": "b"}]).file_path == "p"
    # edit / edits
    assert EditParams(path="p", edit={"old": "a", "new": "b"}).edit == [
        ReplaceEditItem(old="a", new="b")
    ]
    assert EditParams(path="p", edits=[{"old": "a", "new": "b"}]).edit == [
        ReplaceEditItem(old="a", new="b")
    ]
    # old / old_string and new / new_string inside an item
    by_name = ReplaceEditItem(old="a", new="b")
    by_alias = ReplaceEditItem(old_string="a", new_string="b")
    assert by_name == by_alias
    assert by_alias.old == "a" and by_alias.new == "b"

    # Schema advertises the aliases, hides the internal resolved_mode.
    schema = EditParams.model_json_schema()
    props = schema["properties"]
    assert {"file_path", "edits", "old_string", "new_string", "mode"} <= set(props)
    # DEVIATION/finding: the internal `resolved_mode` field leaks into the
    # advertised parameter schema (exclude=True only affects dumps), and the
    # live tool advertisement inherits it (report §4 (k)).
    assert "resolved_mode" in props
    assert props["resolved_mode"]["anyOf"][0]["enum"] == ["replace", "sloppy"]
    assert "resolved_mode" in edit_file_tool.base.parameters["properties"]
    item = schema["$defs"]["ReplaceEditItem"]
    assert item["required"] == ["old_string", "new_string"]
    assert set(item["properties"]) == {
        "old_string",
        "new_string",
        "replace_all",
        "max_replacements",
        "match_mode",
    }
    assert "Accepts `file_path` or `path`" in props["file_path"]["description"]
    assert "Accepts `edit` or `edits`" in props["edits"]["description"]

    # Alias round-trip: by_alias dump revalidates to the same edits.
    dumped = EditParams(file_path="p", edits=[{"old": "a", "new": "b"}]).model_dump(
        by_alias=True
    )
    assert "edits" in dumped and "resolved_mode" not in dumped
    assert EditParams.model_validate(dumped).edit == [ReplaceEditItem(old="a", new="b")]

    # End-to-end: four spellings of the same edit, four distinct files.
    expected = "new text\n"
    spellings: list[dict[str, Any]] = [
        {"file_path": "{p}", "edits": [{"old_string": "old", "new_string": "new"}]},
        {"path": "{p}", "edit": {"old": "old", "new": "new"}},
        {"path": "{p}", "old_string": "old", "new_string": "new"},
        {"file_path": "{p}", "edits": [{"old": "old", "new_string": "new"}]},
    ]
    for i, spelling in enumerate(spellings):
        target = temp_work_dir / f"alias_{i}.txt"
        await target.write_text("old text\n")
        args = {k: (str(target) if v == "{p}" else v) for k, v in spelling.items()}
        result = await edit_file_tool.call(args)
        assert not result.is_error, (i, args, result.message)
        assert await target.read_text() == expected


# ---------------------------------------------------------------------------
# PROBE 13 — sloppy mode adversarial cover
# ---------------------------------------------------------------------------


async def test_probe13a_sloppy_parse_errors(
    edit_file_tool: EditFile, temp_work_dir: KaosPath
) -> None:
    missing_input = await edit_file_tool(EditParams(mode="sloppy", input=""))
    assert missing_input.is_error
    assert missing_input.brief == "Missing input"
    assert "requires an input payload" in missing_input.message

    # A sloppy payload with no § section at all is a parse error.
    no_section = await edit_file_tool(EditParams(mode="sloppy", input="hello world\nno marker\n"))
    assert no_section.is_error
    assert no_section.brief == "Parse error"
    assert "No sloppy operations found" in no_section.message

    bare_first = await edit_file_tool(EditParams(mode="sloppy", input="§\nbody line\n"))
    assert bare_first.is_error
    assert bare_first.brief == "Parse error"
    assert "Bare `§` requires a previous section with a path" in bare_first.message

    missing_file = temp_work_dir / "ghost.ts"
    res = await edit_file_tool(
        EditParams(mode="sloppy", input=f"§{missing_file}\n⟪a│b⟫\n")
    )
    assert res.is_error
    assert res.brief == "File not found"
    assert "does not exist" in res.message

    res_rel = await edit_file_tool(
        EditParams(mode="sloppy", input="§../outside.ts\n⟪a│b⟫\n")
    )
    assert res_rel.is_error
    assert res_rel.brief == "Invalid path"
    assert "absolute path" in res_rel.message.lower()


async def test_probe13b_sloppy_block_inline_allmatch_and_fuzzy(
    edit_file_tool: EditFile, temp_work_dir: KaosPath
) -> None:
    # inline, single occurrence
    inline = temp_work_dir / "inline.ts"
    await inline.write_text("const timeout = 1000;\n")
    res = await edit_file_tool(
        EditParams(input=f"§{inline}\nconst timeout = ⟪1000│5000⟫;\n")
    )
    assert not res.is_error
    assert await inline.read_text() == "const timeout = 5000;\n"

    # all_match inline: every occurrence replaced
    allm = temp_work_dir / "allmatch.ts"
    await allm.write_text("a b a b a\n")
    res_allm = await edit_file_tool(EditParams(input=f"§*{allm}\n⟪a│z⟫\n"))
    assert not res_allm.is_error
    assert await allm.read_text() == "z b z b z\n"

    # exact block rewrite (payload without a trailing newline)
    block = temp_work_dir / "block.ts"
    await block.write_text("line one\nline two\nkeep\n")
    res_block = await edit_file_tool(
        EditParams(mode="sloppy", input=f"§{block}\nline one\nline two\n»\nLINE ONE")
    )
    assert not res_block.is_error
    assert await block.read_text() == "LINE ONE\nkeep\n"

    # Characterisation (bug): a payload that ends with a newline — the natural
    # shape — makes the final empty line part of the REWRITE block, so the
    # rewritten file gains an extra blank line (report §4 (a)/(i)).
    block_nl = temp_work_dir / "block_nl.ts"
    await block_nl.write_text("line one\nline two\nkeep\n")
    res_block_nl = await edit_file_tool(
        EditParams(mode="sloppy", input=f"§{block_nl}\nline one\nline two\n»\nLINE ONE\n")
    )
    assert not res_block_nl.is_error
    assert await block_nl.read_text() == "LINE ONE\n\nkeep\n"

    # block deletion swallows the trailing newline
    delete = temp_work_dir / "delete.ts"
    await delete.write_text("gone\nkeep\n")
    res_del = await edit_file_tool(EditParams(mode="sloppy", input=f"§{delete}\ngone\n»\n"))
    assert not res_del.is_error
    assert await delete.read_text() == "keep\n"

    # all_match block deletion removes every occurrence
    delall = temp_work_dir / "delall.ts"
    await delall.write_text("dup\nkeep\ndup\n")
    res_delall = await edit_file_tool(EditParams(input=f"§*{delall}\ndup\n»\n"))
    assert not res_delall.is_error
    assert await delall.read_text() == "keep\n"

    # fuzzy block: whitespace drift in the MATCH block is tolerated (>=0.75)
    fuzzy = temp_work_dir / "fuzzy.ts"
    await fuzzy.write_text("def foo():\n    return 1\n")
    res_fuzzy = await edit_file_tool(
        EditParams(input=f"§{fuzzy}\ndef foo():\n  return 1\n»\ndef bar():\n    return 2")
    )
    assert not res_fuzzy.is_error
    assert await fuzzy.read_text() == "def bar():\n    return 2\n"

    # MATCH block that is empty is rejected
    empty_match = temp_work_dir / "empty_match.ts"
    await empty_match.write_text("a\n")
    res_empty = await edit_file_tool(EditParams(input=f"§{empty_match}\n»\nreplacement\n"))
    assert res_empty.is_error
    assert res_empty.brief == "Sloppy edit failed"
    assert "MATCH block is empty" in res_empty.message

    # MATCH block that is nowhere near the content is rejected
    locate = temp_work_dir / "locate.ts"
    await locate.write_text("a\n")
    res_locate = await edit_file_tool(
        EditParams(input=f"§{locate}\ncompletely different anchor\n»\nX\n")
    )
    assert res_locate.is_error
    assert "Could not locate MATCH block" in res_locate.message

    # A section with no selection and no rewrite is a no-op -> refused.
    nochange = temp_work_dir / "nochange.ts"
    await nochange.write_text("plain\n")
    res_nochange = await edit_file_tool(EditParams(input=f"§{nochange}\nplain\n"))
    assert res_nochange.is_error
    assert res_nochange.brief == "No change"
    assert "produced no change" in res_nochange.message

    # An inline anchor that does not occur is rejected with the corrected payload.
    missing_anchor = temp_work_dir / "missing_anchor.ts"
    await missing_anchor.write_text("const a = 1;\n")
    res_anchor = await edit_file_tool(
        EditParams(input=f"§{missing_anchor}\n⟪999│777⟫\n")
    )
    assert res_anchor.is_error
    assert res_anchor.brief == "Sloppy edit failed"
    assert "Could not locate inline selection" in res_anchor.message
    assert "Corrected payload" in res_anchor.message
    assert await missing_anchor.read_text() == "const a = 1;\n"


async def test_probe13c_sloppy_multi_section_duplicate_path_characterisation(
    edit_file_tool: EditFile, temp_work_dir: KaosPath
) -> None:
    # Characterisation (bug): a bare `§` repeats the previous section's path, so
    # two sections are always planned against the same file.  Both new contents
    # are computed from the SAME pre-read text, the first write lands, and the
    # second write is then refused by the write-after-write staleness guard —
    # the tool returns an error AFTER having already committed a partial edit.
    # See report §4 (a)/(c)/(j).
    first = temp_work_dir / "multi_a.ts"
    await first.write_text("alpha\nbeta\n")
    res = await edit_file_tool(
        EditParams(mode="sloppy", input=f"§{first}\n⟪alpha│ALPHA⟫\n§\n⟪beta│BETA⟫\n")
    )
    assert res.is_error
    assert res.brief == "Stale file"
    # The first section was already written; the second was not.
    assert await first.read_text() == "ALPHA\nbeta\n"

    # The same clobbering shape with two explicit `§<same path>` headers.
    same = temp_work_dir / "multi_same.ts"
    await same.write_text("a c\n")
    res_same = await edit_file_tool(
        EditParams(mode="sloppy", input=f"§{same}\n⟪a│b⟫\n§{same}\n⟪c│d⟫\n")
    )
    assert res_same.is_error
    assert res_same.brief == "Stale file"
    assert await same.read_text() == "b c\n"

    # Two sections targeting DIFFERENT files still work (each write is first-touch).
    file_x = temp_work_dir / "multi_x.ts"
    file_y = temp_work_dir / "multi_y.ts"
    await file_x.write_text("xx\n")
    await file_y.write_text("yy\n")
    res_two = await edit_file_tool(
        EditParams(mode="sloppy", input=f"§{file_x}\n⟪xx│XX⟫\n§{file_y}\n⟪yy│YY⟫\n")
    )
    assert not res_two.is_error
    assert await file_x.read_text() == "XX\n"
    assert await file_y.read_text() == "YY\n"


async def test_probe13d_sloppy_conflict_markers_and_approval_rejection(
    runtime: Runtime, session: Session, temp_work_dir: KaosPath
) -> None:
    conflicted = temp_work_dir / "sloppy_conflict.ts"
    await conflicted.write_bytes(_conflicted().encode())

    with tool_call_context("EditFile"):
        tool = EditFile(runtime, Approval(yolo=True), session)
        res = await tool(EditParams(input=f"§{conflicted}\n⟪before│start⟫\n"))
    assert res.is_error
    assert res.brief == "Conflict markers detected"
    assert "refusing to edit" in res.message

    allowed = temp_work_dir / "sloppy_conflict_ok.ts"
    await allowed.write_bytes(_conflicted().encode())
    with tool_call_context("EditFile"):
        tool_ok = EditFile(runtime, Approval(yolo=True), session)
        res_ok = await tool_ok(
            EditParams(
                input=f"§{allowed}\n⟪before│start⟫\n", allow_conflicts=True
            )
        )
    assert not res_ok.is_error
    assert (await allowed.read_text()).startswith("start\n")

    rejected = temp_work_dir / "sloppy_rejected.ts"
    await rejected.write_text("const a = 1;\n")
    approval = Approval(yolo=False)
    request_mock = AsyncMock(return_value=ApprovalResult(approved=False))
    approval.request = cast(Any, request_mock)
    with tool_call_context("EditFile"):
        rej_tool = EditFile(runtime, approval, session)
        res_rej = await rej_tool(EditParams(input=f"§{rejected}\n⟪1│2⟫\n"))
    assert res_rej.is_error
    assert "rejected" in res_rej.message.lower()
    request_mock.assert_awaited_once()
    assert await rejected.read_text() == "const a = 1;\n"


async def test_probe13e_staleness_policy_diverges_between_modes(
    edit_file_tool: EditFile, temp_work_dir: KaosPath
) -> None:
    # replace mode: a write-after-write is allowed and only annotated.
    rep = temp_work_dir / "stale_replace.txt"
    await rep.write_text("one\n")
    r1 = await edit_file_tool(
        EditParams(path=str(rep), edits=[ReplaceEditItem(old="one", new="two")])
    )
    assert not r1.is_error
    r2 = await edit_file_tool(
        EditParams(path=str(rep), edits=[ReplaceEditItem(old="two", new="three")])
    )
    assert not r2.is_error
    assert "Note: file changed since last read" in r2.message
    assert await rep.read_text() == "three\n"

    # sloppy mode: the same write-after-write is refused outright.
    slo = temp_work_dir / "stale_sloppy.txt"
    await slo.write_text("one\n")
    s1 = await edit_file_tool(EditParams(input=f"§{slo}\n⟪one│two⟫\n"))
    assert not s1.is_error
    s2 = await edit_file_tool(EditParams(input=f"§{slo}\n⟪two│three⟫\n"))
    assert s2.is_error
    assert s2.brief == "Stale file"
    assert await slo.read_text() == "two\n"


# ---------------------------------------------------------------------------
# PROBE 14 — error contract: hostile types, oversized input, no escaping
# ---------------------------------------------------------------------------


async def test_probe14a_error_contract_never_escapes(
    edit_file_tool: EditFile, temp_work_dir: KaosPath
) -> None:
    target = temp_work_dir / "hostile.txt"
    await target.write_text("payload\n")

    none_args = await edit_file_tool.call(None)
    assert none_args.is_error
    assert "expected a JSON object" in none_args.message

    empty_args = await edit_file_tool.call("")
    assert empty_args.is_error
    assert "expected a JSON object" in empty_args.message

    list_args = await edit_file_tool.call([1, 2, 3])
    assert list_args.is_error
    assert "got `list`" in list_args.message

    wrong_type = await edit_file_tool.call({"path": str(target), "edits": 5})
    assert wrong_type.is_error
    assert "Invalid arguments" in wrong_type.message

    missing_field = await edit_file_tool.call(
        {"path": str(target), "edits": [{"old": "payload"}]}
    )
    assert missing_field.is_error
    assert "new_string" in missing_field.message

    no_mode = await edit_file_tool.call({"path": str(target)})
    assert no_mode.is_error
    assert "Could not determine edit mode" in no_mode.message

    # Wrong host type for mode.
    bad_mode = await edit_file_tool.call({"mode": "auto", "path": str(target), "edits": 7})
    assert bad_mode.is_error
    assert "Invalid arguments" in bad_mode.message

    # JSON-string edit argument is repaired rather than rejected.
    repaired = await edit_file_tool.call(
        {"path": str(target), "edits": '{"old": "payload", "new": "fixed"}'}
    )
    assert not repaired.is_error
    assert await target.read_text() == "fixed\n"


async def test_probe14b_oversized_and_hostile_payloads(
    edit_file_tool: EditFile, temp_work_dir: KaosPath
) -> None:
    # Oversized sloppy payload (2000 inline selections) must still succeed.
    big = temp_work_dir / "big.ts"
    await big.write_text("".join(f"k{i}\n" for i in range(2000)))
    body = "".join(f"k{i}⟪k{i}│v{i}⟫\n" for i in range(2000))
    result = await edit_file_tool(EditParams(input=f"§{big}\n{body}"))
    assert not result.is_error
    text = await big.read_text()
    assert len(text.splitlines()) == 2000
    assert "v0\n" in text and "v1999\n" in text
    assert "k0\n" not in text

    # Oversized replace payload: a very long old string that cannot match.
    long_file = temp_work_dir / "long.txt"
    await long_file.write_text("x\n")
    long_old = "y" * 200_000
    res_long = await edit_file_tool(
        EditParams(path=str(long_file), edits=[ReplaceEditItem(old=long_old, new="z")])
    )
    assert res_long.is_error
    assert res_long.brief == "No replacements made"

    # Hostile content: NUL bytes and unicode round-trip untouched on no-match.
    hostile = temp_work_dir / "hostile_utf8.txt"
    await hostile.write_bytes("héllo\u0000wörld\n".encode())
    res_hostile = await edit_file_tool(
        EditParams(path=str(hostile), edits=[ReplaceEditItem(old="absent", new="x")])
    )
    assert res_hostile.is_error
    assert res_hostile.brief == "No replacements made"

    # Shell metacharacters in old/new are literal, never interpreted.
    shell = temp_work_dir / "shell.txt"
    await shell.write_text("CMD=echo; rm -rf /\n")
    res_shell = await edit_file_tool(
        EditParams(
            path=str(shell),
            edits=[ReplaceEditItem(old="CMD=echo; rm -rf /", new="$(whoami)`id`")],
        )
    )
    assert not res_shell.is_error
    assert await shell.read_text() == "$(whoami)`id`\n"

    # Windows separators in the path are accepted.
    win = temp_work_dir / "win_sep.txt"
    await win.write_text("sep\n")
    res_win = await edit_file_tool.call(
        {"path": str(win).replace("/", "\\"), "old_string": "sep", "new_string": "SEP"}
    )
    assert not res_win.is_error
    assert await win.read_text() == "SEP\n"


# ---------------------------------------------------------------------------
# PROBE 15 — dispatcher defensive paths
# ---------------------------------------------------------------------------


async def test_probe15_dispatcher_repairs_missing_resolved_mode(
    edit_file_tool: EditFile, temp_work_dir: KaosPath
) -> None:
    target = temp_work_dir / "dispatcher.txt"
    await target.write_text("aaa\n")
    params = EditParams(path=str(target), edits=[ReplaceEditItem(old="aaa", new="bbb")])
    params.resolved_mode = None  # simulate a hand-built params object
    result = await edit_file_tool(params)
    assert not result.is_error
    assert await target.read_text() == "bbb\n"


async def test_probe15_dispatcher_unknown_mode_is_a_tool_error(
    edit_file_tool: EditFile, temp_work_dir: KaosPath, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = temp_work_dir / "unknown_mode.txt"
    await target.write_text("aaa\n")
    monkeypatch.setitem(MODE_REGISTRY, "replace", None)
    result = await edit_file_tool(
        EditParams(path=str(target), edits=[ReplaceEditItem(old="aaa", new="bbb")])
    )
    assert result.is_error
    assert result.brief == "Invalid edit mode"
    assert await target.read_text() == "aaa\n"


# ---------------------------------------------------------------------------
# PROBE 16 — remaining ToolError return sites (protected path, format guard,
#            json repair, generic write failure) + non-UTF-8 characterisation
# ---------------------------------------------------------------------------


async def test_probe16a_protected_write_path_refused(
    edit_file_tool: EditFile, temp_work_dir: KaosPath, session: Session
) -> None:
    secret = temp_work_dir / "secret.txt"
    await secret.write_text("token\n")
    session.custom_config = {
        "config_json": {"protected_write_paths": [str(secret)]}
    }

    res = await edit_file_tool(
        EditParams(path=str(secret), edits=[ReplaceEditItem(old="token", new="leak")])
    )
    assert res.is_error
    assert res.brief == "Protected path"
    assert "blocked by protected path rule" in res.message
    assert await secret.read_text() == "token\n"

    # Sloppy mode runs the same guard (wrapped in its own wording).
    res_sloppy = await edit_file_tool(
        EditParams(mode="sloppy", input=f"§{secret}\n⟪token│leak⟫\n")
    )
    assert res_sloppy.is_error
    assert res_sloppy.brief == "Invalid path"
    assert "protected path rule" in res_sloppy.message
    assert await secret.read_text() == "token\n"


async def test_probe16b_format_guard_yaml_and_toml(edit_file_tool, temp_work_dir) -> None:
    yaml_file = temp_work_dir / "cfg.yaml"
    await yaml_file.write_text("a: 1\n")
    res_yaml = await edit_file_tool(
        EditParams(path=str(yaml_file), edits=[ReplaceEditItem(old="a: 1", new="a: [")])
    )
    assert res_yaml.is_error
    assert res_yaml.brief == "Format validation failed"
    assert "YAML parse error" in res_yaml.message
    assert await yaml_file.read_text() == "a: [\n"

    toml_file = temp_work_dir / "cfg.toml"
    await toml_file.write_text('a = "1"\n')
    res_toml = await edit_file_tool(
        EditParams(
            path=str(toml_file),
            edits=[ReplaceEditItem(old='"1"', new='"unterminated')],
        )
    )
    assert res_toml.is_error
    assert res_toml.brief == "Format validation failed"
    assert "TOML parse error" in res_toml.message

    # A valid YAML edit passes the guard.
    ok_file = temp_work_dir / "ok.yaml"
    await ok_file.write_text("a: 1\n")
    res_ok = await edit_file_tool(
        EditParams(path=str(ok_file), edits=[ReplaceEditItem(old="1", new="2")])
    )
    assert not res_ok.is_error
    assert await ok_file.read_text() == "a: 2\n"


async def test_probe16c_json_repair_path(edit_file_tool, temp_work_dir) -> None:
    # An edit that breaks JSON but is mechanically repairable is auto-repaired
    # and reported as a success.
    fixable = temp_work_dir / "fixable.json"
    await fixable.write_text('{"a": 1}')
    res = await edit_file_tool(
        EditParams(path=str(fixable), edits=[ReplaceEditItem(old='"a": 1', new='"a": 1,')])
    )
    assert not res.is_error
    assert orjson.loads(await fixable.read_text()) == {"a": 1}

    # Unrepairable JSON writes the edit AND reports a format error.
    broken = temp_work_dir / "broken.json"
    await broken.write_text('{"k": "value"}')
    from unittest.mock import patch

    with patch("json_repair.repair_json", return_value=""):
        res_broken = await edit_file_tool(
            EditParams(
                path=str(broken),
                edits=[ReplaceEditItem(old='"value"', new="oops")],
            )
        )
    assert res_broken.is_error
    assert res_broken.brief == "Format validation failed"
    assert "JSON decode error" in res_broken.message
    assert await broken.read_text() == '{"k": oops}'


async def test_probe16d_write_failure_is_a_tool_error(
    edit_file_tool: EditFile, temp_work_dir: KaosPath, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = temp_work_dir / "diskfull.txt"
    await target.write_text("old content\n")

    async def boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(KaosPath, "write_text", boom)
    result = await edit_file_tool(
        EditParams(path=str(target), edits=[ReplaceEditItem(old="old", new="new")])
    )
    assert result.is_error
    assert result.brief == "Failed to edit file"
    assert "disk full" in result.message
    assert "Path" in result.message
    monkeypatch.undo()
    assert await target.read_text() == "old content\n"


async def test_probe16e_non_utf8_bytes_are_silently_replaced(
    edit_file_tool: EditFile, temp_work_dir: KaosPath
) -> None:
    """Characterisation (risk): non-UTF-8 bytes are read with errors="replace" and
    written back, so any edit silently rewrites them as U+FFFD — a lossy,
    whole-file corruption of a Latin-1/CP-1252 file (report §4 (i)).
    """
    latin1 = temp_work_dir / "latin1.txt"
    await latin1.write_bytes(b"caf\xe9 here\n")
    result = await edit_file_tool(
        EditParams(path=str(latin1), edits=[ReplaceEditItem(old="here", new="HERE")])
    )
    assert not result.is_error
    assert await latin1.read_bytes() == b"caf\xef\xbf\xbd HERE\n"


async def test_probe17_allow_auto_generated_field(
    edit_file_tool: EditFile, temp_work_dir: KaosPath
) -> None:
    generated = temp_work_dir / "zz_generated.py"
    await generated.write_text("value = 1\n")

    refused = await edit_file_tool(
        EditParams(path=str(generated), edits=[ReplaceEditItem(old="1", new="2")])
    )
    assert refused.is_error
    assert refused.brief == "Auto-generated file"
    assert await generated.read_text() == "value = 1\n"

    allowed = await edit_file_tool(
        EditParams(
            path=str(generated),
            edits=[ReplaceEditItem(old="1", new="2")],
            allow_auto_generated=True,
        )
    )
    assert not allowed.is_error
    assert await generated.read_text() == "value = 2\n"

    # The header-marker variant is refused too.
    header = temp_work_dir / "plain_name.py"
    await header.write_text("# @generated by toolgen\nvalue = 3\n")
    refused_header = await edit_file_tool(
        EditParams(path=str(header), edits=[ReplaceEditItem(old="3", new="4")])
    )
    assert refused_header.is_error
    assert refused_header.brief == "Auto-generated file"


async def test_probe18_justification_and_sandbox_permissions_fields(
    runtime: Runtime, session: Session, temp_work_dir: KaosPath
) -> None:
    target = temp_work_dir / "prompted.txt"
    await target.write_text("aaa\n")

    approval = Approval(yolo=False)
    calls: list[tuple[Any, Any, str]] = []

    async def record_request(name: Any, action: Any, prompt: str, **kwargs: Any):
        calls.append((name, action, prompt))
        return ApprovalResult(approved=True)

    approval.request = cast(Any, record_request)
    with tool_call_context("EditFile"):
        tool = EditFile(runtime, approval, session)
        result = await tool(
            EditParams(
                path=str(target),
                edits=[ReplaceEditItem(old="aaa", new="bbb")],
                justification="because the ticket says so",
                sandbox_permissions="workspace-write",
            )
        )

    assert not result.is_error
    assert await target.read_text() == "bbb\n"
    assert len(calls) == 1
    name, _action, prompt = calls[0]
    assert name == "edit"
    assert prompt.endswith("because the ticket says so")
    # `sandbox_permissions` is accepted and then never read by the edit tool: the
    # field is schema noise today (report §4 (k)).
    assert "workspace-write" not in prompt
    assert "workspace-write" not in result.message
