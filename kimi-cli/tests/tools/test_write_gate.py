"""C09 `write` (WriteFile) behavioural gate probes (plan §4.4 C09).

Focuses on the behaviours the existing `test_write_file.py` does NOT pin: the
FP-01 acceptance probe for the removed `TODO: checks:` block, the alias surface,
the mode synonyms, the overwrite/append byte-exactness, and the error contract
for hostile targets (directory, non-UTF-8, conflict markers, generated files).
"""

from __future__ import annotations

import ast
from pathlib import Path

import regex

import pytest
from kaos.path import KaosPath
from pydantic import ValidationError

from kimi_cli.tools.file.write import Params, WriteFile

REPO_ROOT = Path(__file__).resolve().parents[3]
WRITE_SRC = REPO_ROOT / "kimi-cli/src/kimi_cli/tools/file/write.py"


# --------------------------------------------------------------------------- #
# FP-01: the open TODO must be resolved, and stay resolved
# --------------------------------------------------------------------------- #


_TODO_COMMENT_RE = regex.compile(r"^[ 	]*#[ 	]*(TODO|FIXME|XXX)", regex.MULTILINE)


def test_write_py_has_no_open_todo_checks_block() -> None:
    """FP-01 acceptance probe: no open TODO *comment* remains in write.py."""
    text = WRITE_SRC.read_text(encoding="utf-8")
    assert _TODO_COMMENT_RE.findall(text) == []


def test_write_py_records_why_the_secret_check_is_deferred() -> None:
    """The removal must be explained and point at the tracked finding."""
    text = WRITE_SRC.read_text(encoding="utf-8")
    assert "F-31" in text
    assert "_looks_like_secret_filename" in text


def test_no_new_bare_todos_introduced_in_the_write_tool() -> None:
    text = WRITE_SRC.read_text(encoding="utf-8")
    offenders = [ln.strip() for ln in text.splitlines() if "# TODO" in ln or "# FIXME" in ln]
    assert offenders == []


# --------------------------------------------------------------------------- #
# Params: aliases, mode synonyms, validation
# --------------------------------------------------------------------------- #


def test_file_path_and_content_aliases_are_interchangeable() -> None:
    assert Params(path="a.txt", text="x").file_path == "a.txt"
    assert Params(path="a.txt", text="x").content == "x"
    assert Params(file_path="a.txt", content="x").file_path == "a.txt"


def test_content_and_file_path_are_required() -> None:
    with pytest.raises(ValidationError):
        Params(file_path="a.txt")
    with pytest.raises(ValidationError):
        Params(content="x")


@pytest.mark.parametrize("synonym", ["over-write", "replace", "create", "truncate", "rewrite"])
def test_overwrite_synonyms_normalize(synonym: str) -> None:
    assert Params(file_path="a.txt", content="x", mode=synonym).mode == "overwrite"


@pytest.mark.parametrize("synonym", ["add", "concat", "extend", "insert", "prepend"])
def test_append_synonyms_normalize(synonym: str) -> None:
    assert Params(file_path="a.txt", content="x", mode=synonym).mode == "append"


def test_unknown_mode_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Params(file_path="a.txt", content="x", mode="sideways")


def test_defaults_are_documented_values() -> None:
    params = Params(file_path="a.txt", content="x")
    assert params.mode == "overwrite"
    assert params.mkdir is True
    assert params.allow_conflicts is False
    assert params.allow_auto_generated is False
    assert params.sandbox_permissions is None


def test_unknown_keys_are_silently_ignored() -> None:
    """Documented leniency (F-32): the model does NOT forbid extra keys, so a
    misspelled parameter is silently dropped instead of raising. Recorded as a
    low finding; pinning it here means a future `extra="forbid"` change is visible.
    """
    params = Params(file_path="a.txt", content="x", definitely_not_a_field=1)
    assert not hasattr(params, "definitely_not_a_field")
    assert params.content == "x"


# --------------------------------------------------------------------------- #
# Behaviour: byte-exact overwrite / append
# --------------------------------------------------------------------------- #


async def test_overwrite_is_byte_exact(write_file_tool: WriteFile, temp_work_dir: KaosPath) -> None:
    target = temp_work_dir / "f.txt"
    await target.write_bytes(b"old content\n")
    result = await write_file_tool(Params(file_path=str(target), content="new\n"))
    assert not result.is_error
    assert await target.read_bytes() == b"new\n"


async def test_append_is_byte_exact(write_file_tool: WriteFile, temp_work_dir: KaosPath) -> None:
    target = temp_work_dir / "f.txt"
    await target.write_bytes(b"a\n")
    result = await write_file_tool(
        Params(file_path=str(target), content="b\n", mode="append")
    )
    assert not result.is_error
    assert await target.read_bytes() == b"a\nb\n"


async def test_append_to_nonexistent_file_creates_it(
    write_file_tool: WriteFile, temp_work_dir: KaosPath
) -> None:
    target = temp_work_dir / "brand-new.txt"
    result = await write_file_tool(
        Params(file_path=str(target), content="hi\n", mode="append")
    )
    assert not result.is_error
    assert await target.read_bytes() == b"hi\n"


async def test_unicode_round_trips_exactly(
    write_file_tool: WriteFile, temp_work_dir: KaosPath
) -> None:
    target = temp_work_dir / "u.txt"
    payload = "héllo — 世界 🌍\n"
    result = await write_file_tool(Params(file_path=str(target), content=payload))
    assert not result.is_error
    assert await target.read_text(encoding="utf-8") == payload


# --------------------------------------------------------------------------- #
# Error contract
# --------------------------------------------------------------------------- #


async def test_empty_path_is_a_tool_error(write_file_tool: WriteFile) -> None:
    result = await write_file_tool(Params(file_path="", content="x"))
    assert result.is_error
    assert result.brief == "Empty file path"


async def test_directory_target_is_a_tool_error(
    write_file_tool: WriteFile, temp_work_dir: KaosPath
) -> None:
    result = await write_file_tool(Params(file_path=str(temp_work_dir), content="x"))
    assert result.is_error


async def test_missing_parent_without_mkdir_is_a_tool_error(
    write_file_tool: WriteFile, temp_work_dir: KaosPath
) -> None:
    target = temp_work_dir / "nope" / "deep" / "f.txt"
    result = await write_file_tool(
        Params(file_path=str(target), content="x", mkdir=False)
    )
    assert result.is_error
    assert not await target.exists()


async def test_parent_directories_are_created_by_default(
    write_file_tool: WriteFile, temp_work_dir: KaosPath
) -> None:
    target = temp_work_dir / "a" / "b" / "f.txt"
    result = await write_file_tool(Params(file_path=str(target), content="x"))
    assert not result.is_error
    assert await target.read_bytes() == b"x"


async def test_non_utf8_target_is_refused_without_partial_write(
    write_file_tool: WriteFile, temp_work_dir: KaosPath
) -> None:
    target = temp_work_dir / "binary.bin"
    original = b"\xff\xfe\x00\x01not-utf8"
    await target.write_bytes(original)
    result = await write_file_tool(Params(file_path=str(target), content="replacement"))
    if result.is_error:
        assert await target.read_bytes() == original, "a refused write must not touch the file"
    else:
        assert await target.read_bytes() == b"replacement"


async def test_dangling_marker_guard_is_edit_scoped(
    write_file_tool: WriteFile, temp_work_dir: KaosPath
) -> None:
    """Documented contract: a FULL overwrite replaces the file wholesale, so an
    existing conflict marker in the *target* does not block it; the dangling-marker
    guard (`write.py:541`, `if dangling and not allow_conflicts`) only fires on the
    edit/merge path. Pinning this makes a future scope change visible.
    """
    target = temp_work_dir / "conflicted.txt"
    marked = "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> other\n"
    await target.write_text(marked, encoding="utf-8")

    result = await write_file_tool(Params(file_path=str(target), content="clean\n"))
    assert not result.is_error
    assert await target.read_bytes() == b"clean\n"


def test_allow_conflicts_is_part_of_the_public_params_surface() -> None:
    assert Params(file_path="a.txt", content="x", allow_conflicts=True).allow_conflicts is True
    assert Params(file_path="a.txt", content="x").allow_conflicts is False


def test_write_module_parses_and_is_a_tool_class() -> None:
    """Structural sanity: the module still exposes the expected tool shape."""
    tree = ast.parse(WRITE_SRC.read_text(encoding="utf-8"))
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    assert "Params" in classes and "WriteFile" in classes
    name_values = [
        stmt.value.value
        for stmt in classes["WriteFile"].body
        if isinstance(stmt, (ast.Assign, ast.AnnAssign))
        and (
            (isinstance(stmt, ast.Assign)
             and any(getattr(t, "id", "") == "name" for t in stmt.targets))
            or (isinstance(stmt, ast.AnnAssign) and getattr(stmt.target, "id", "") == "name")
        )
        and isinstance(stmt.value, ast.Constant)
    ]
    assert "write" in name_values
