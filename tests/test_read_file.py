"""Comprehensive tests for ReadFile tool with char_offset and max_char."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kaos.path import KaosPath
from kosong.tooling import ToolOk, ToolError

from kimi_cli.tools.file.read import ReadFile, Params


@pytest.fixture
def read_tool(tmp_path: Path) -> ReadFile:
    """Create a ReadFile tool instance with mocked runtime and session."""
    runtime = MagicMock()
    runtime.builtin_args.KIMI_WORK_DIR = KaosPath(str(tmp_path))
    runtime.additional_dirs = []

    session = MagicMock()
    session.id = "test-session"

    return ReadFile(runtime=runtime, session=session)


# ---------------------------------------------------------------------------
# Basic functionality
# ---------------------------------------------------------------------------
class TestReadFileBasic:
    async def test_read_simple_file(self, read_tool: ReadFile, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello world\nsecond line\n", encoding="utf-8")
        result = await read_tool(Params(path=str(f)))
        assert isinstance(result, ToolOk)
        assert "hello world" in result.output
        assert "second line" in result.output

    async def test_empty_file(self, read_tool: ReadFile, tmp_path: Path) -> None:
        f = tmp_path / "empty.txt"
        f.write_text("", encoding="utf-8")
        result = await read_tool(Params(path=str(f)))
        assert isinstance(result, ToolOk)
        assert result.output == ""

    async def test_missing_file(self, read_tool: ReadFile, tmp_path: Path) -> None:
        result = await read_tool(Params(path=str(tmp_path / "missing.txt")))
        assert isinstance(result, ToolError)
        assert "does not exist" in result.message

    async def test_directory_error(self, read_tool: ReadFile, tmp_path: Path) -> None:
        d = tmp_path / "adir"
        d.mkdir()
        result = await read_tool(Params(path=str(d)))
        assert isinstance(result, ToolError)
        assert "is not a file" in result.message


# ---------------------------------------------------------------------------
# char_offset and max_char slicing
# ---------------------------------------------------------------------------
class TestReadFileCharSlicing:
    async def test_default_max_char(self, read_tool: ReadFile, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello world\n", encoding="utf-8")
        result = await read_tool(Params(path=str(f)))
        assert isinstance(result, ToolOk)
        # Default max_char=65536 should not truncate this small file
        assert "hello world" in result.output

    async def test_char_offset_cuts_beginning(self, read_tool: ReadFile, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("1234567890\n", encoding="utf-8")
        result = await read_tool(Params(path=str(f), char_offset=7))
        assert isinstance(result, ToolOk)
        # char_offset=7 skips the 6-digit line number + tab prefix
        assert result.output == "1234567890\n"

    async def test_max_char_cuts_end(self, read_tool: ReadFile, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("1234567890\n", encoding="utf-8")
        result = await read_tool(Params(path=str(f), max_char=12))
        assert isinstance(result, ToolOk)
        # 7 chars prefix + 5 chars of content
        assert result.output == "     1\t12345"

    async def test_char_offset_and_max_char(self, read_tool: ReadFile, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("0123456789\n", encoding="utf-8")
        result = await read_tool(Params(path=str(f), char_offset=7, max_char=12))
        assert isinstance(result, ToolOk)
        # Skip prefix (7), then take 5 chars of content
        assert result.output == "01234"

    async def test_char_offset_beyond_output(self, read_tool: ReadFile, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("short\n", encoding="utf-8")
        result = await read_tool(Params(path=str(f), char_offset=100))
        assert isinstance(result, ToolOk)
        assert result.output == ""

    async def test_max_char_beyond_output(self, read_tool: ReadFile, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("short\n", encoding="utf-8")
        result = await read_tool(Params(path=str(f), max_char=100000))
        assert isinstance(result, ToolOk)
        assert "short" in result.output

    async def test_zero_max_char_returns_empty(self, read_tool: ReadFile, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello\n", encoding="utf-8")
        result = await read_tool(Params(path=str(f), max_char=0))
        assert isinstance(result, ToolOk)
        assert result.output == ""

    async def test_empty_file_with_slicing(self, read_tool: ReadFile, tmp_path: Path) -> None:
        f = tmp_path / "empty.txt"
        f.write_text("", encoding="utf-8")
        result = await read_tool(Params(path=str(f), char_offset=5, max_char=10))
        assert isinstance(result, ToolOk)
        assert result.output == ""


# ---------------------------------------------------------------------------
# Combined with line_offset
# ---------------------------------------------------------------------------
class TestReadFileLineAndChar:
    async def test_line_offset_with_char_offset(self, read_tool: ReadFile, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("line one\nline two\nline three\n", encoding="utf-8")
        result = await read_tool(Params(path=str(f), line_offset=2, char_offset=7))
        assert isinstance(result, ToolOk)
        # Skip prefix of line 2, should start with actual content
        assert result.output.startswith("line two")

    async def test_line_offset_with_max_char(self, read_tool: ReadFile, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("line one\nline two\nline three\n", encoding="utf-8")
        result = await read_tool(Params(path=str(f), line_offset=2, max_char=6))
        assert isinstance(result, ToolOk)
        # Should contain "line t" (first 6 chars of line 2 output including line number prefix)
        assert len(result.output) == 6

    async def test_negative_line_offset_with_char_slice(self, read_tool: ReadFile, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a\nb\nc\nd\n", encoding="utf-8")
        result = await read_tool(Params(path=str(f), line_offset=-2, char_offset=4))
        assert isinstance(result, ToolOk)
        # Tail 2 lines are c and d, with line numbers "     3\tc\n     4\td\n"
        # char_offset=4 skips first 4 chars
        assert len(result.output) > 0


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
class TestReadFileValidation:
    async def test_negative_char_offset_rejected(self, read_tool: ReadFile, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            Params(path=str(tmp_path / "a.txt"), char_offset=-1)

    async def test_negative_max_char_rejected(self, read_tool: ReadFile, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            Params(path=str(tmp_path / "a.txt"), max_char=-1)

    async def test_zero_line_offset_rejected(self, read_tool: ReadFile, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            Params(path=str(tmp_path / "a.txt"), line_offset=0)

    async def test_line_offset_too_negative_rejected(self, read_tool: ReadFile, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            Params(path=str(tmp_path / "a.txt"), line_offset=-1001)


# ---------------------------------------------------------------------------
# Large file / boundary tests
# ---------------------------------------------------------------------------
class TestReadFileBoundaries:
    async def test_large_char_offset(self, read_tool: ReadFile, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("x" * 100 + "\n", encoding="utf-8")
        result = await read_tool(Params(path=str(f), char_offset=57))
        assert isinstance(result, ToolOk)
        # Skip prefix (7) + 50 x's, remaining is 50 x's + newline
        assert len(result.output) == 51
        assert result.output == "x" * 50 + "\n"

    async def test_max_char_exact_boundary(self, read_tool: ReadFile, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("1234567890\n", encoding="utf-8")
        result = await read_tool(Params(path=str(f), max_char=7))
        assert isinstance(result, ToolOk)
        # Exactly the prefix length
        assert result.output == "     1\t"

    async def test_multibyte_utf8_slicing(self, read_tool: ReadFile, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("你好世界\n", encoding="utf-8")
        result = await read_tool(Params(path=str(f), char_offset=7, max_char=9))
        assert isinstance(result, ToolOk)
        # Python string slicing works on characters (code points), not bytes
        assert result.output == "你好"
