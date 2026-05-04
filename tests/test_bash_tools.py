"""Comprehensive tests for all bash tools in kimix.tools.file.bash."""

import asyncio
import gzip
import bz2
import lzma
import os
import platform
import stat
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from kimi_agent_sdk import ToolOk, ToolError
from kimix.tools.file.bash import (
    Awk,
    Bunzip2,
    Bzip2,
    Cal,
    Cat,
    Cp,
    Cut,
    Date,
    Df,
    Diff,
    Du,
    Env,
    File,
    Find,
    Grep,
    Gunzip,
    Gzip,
    Head,
    Ln,
    Ls,
    Mkdir,
    Mv,
    Printenv,
    Ps,
    Pwd,
    Rm,
    Rmdir,
    Sed,
    Tail,
    Tar,
    Touch,
    Tr,
    Unxz,
    Unzip,
    Wc,
    Xz,
    Zip,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def mock_export():
    with patch("kimix.tools.file.bash.cat._maybe_export_output_async", side_effect=lambda x: asyncio.Future().set_result(x) or asyncio.Future()) as m:
        # Need a proper async mock
        pass


async def _run(tool_cls, args, cwd=None, output_path=None):
    """Instantiate a bash tool and run it with the given args."""
    tool = tool_cls()
    params = tool_cls.params(path="", args=args, cwd=cwd, output_path=output_path)
    with patch("kimix.tools.common._maybe_export_output_async", side_effect=lambda x: x):
        return await tool(params)


# ---------------------------------------------------------------------------
# Cat
# ---------------------------------------------------------------------------
class TestCat:
    async def test_single_file(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello world", encoding="utf-8")
        result = await _run(Cat, [str(f)])
        assert isinstance(result, ToolOk)
        assert result.output == "hello world"

    async def test_multiple_files(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("hello ", encoding="utf-8")
        f2.write_text("world", encoding="utf-8")
        result = await _run(Cat, [str(f1), str(f2)])
        assert isinstance(result, ToolOk)
        assert result.output == "hello world"

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Cat, [str(tmp_path / "missing.txt")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.output

    async def test_directory_error(self, tmp_path: Path) -> None:
        d = tmp_path / "adir"
        d.mkdir()
        result = await _run(Cat, [str(d)])
        assert isinstance(result, ToolError)
        assert "Is a directory" in result.output or "Permission denied" in result.output

    async def test_output_path(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        out = tmp_path / "out.txt"
        f.write_text("data", encoding="utf-8")
        result = await _run(Cat, [str(f)], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output
        assert out.read_text(encoding="utf-8") == "data"

    async def test_cwd_relative(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("relative", encoding="utf-8")
        result = await _run(Cat, ["a.txt"], cwd=str(tmp_path))
        assert isinstance(result, ToolOk)
        assert result.output == "relative"

    async def test_ignores_flags(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("content", encoding="utf-8")
        result = await _run(Cat, ["-n", str(f)])
        assert isinstance(result, ToolOk)
        assert result.output == "content"


# ---------------------------------------------------------------------------
# Ls
# ---------------------------------------------------------------------------
class TestLs:
    async def test_list_directory(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")
        result = await _run(Ls, [str(tmp_path)])
        assert isinstance(result, ToolOk)
        assert "a.txt" in result.output
        assert "b.txt" in result.output

    async def test_long_format(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("a")
        result = await _run(Ls, ["-l", str(tmp_path)])
        assert isinstance(result, ToolOk)
        assert "total" in result.output
        assert "a.txt" in result.output

    async def test_all_files(self, tmp_path: Path) -> None:
        (tmp_path / ".hidden").write_text("h")
        result = await _run(Ls, ["-a", str(tmp_path)])
        assert isinstance(result, ToolOk)
        assert ".hidden" in result.output

    async def test_recursive(self, tmp_path: Path) -> None:
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "c.txt").write_text("c")
        result = await _run(Ls, ["-R", str(tmp_path)])
        assert isinstance(result, ToolOk)
        assert "c.txt" in result.output

    async def test_reverse(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")
        result = await _run(Ls, ["-r", str(tmp_path)])
        assert isinstance(result, ToolOk)
        lines = [l for l in result.output.splitlines() if l]
        assert lines.index("b.txt") < lines.index("a.txt")

    async def test_human_readable(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("a")
        result = await _run(Ls, ["-lh", str(tmp_path)])
        assert isinstance(result, ToolOk)
        assert "a.txt" in result.output

    async def test_missing_directory(self, tmp_path: Path) -> None:
        result = await _run(Ls, [str(tmp_path / "missing")])
        assert isinstance(result, ToolOk)
        assert "No such file" in result.output

    async def test_symlink(self, tmp_path: Path) -> None:
        target = tmp_path / "target.txt"
        target.write_text("t")
        link = tmp_path / "link.txt"
        link.symlink_to(target)
        # symlinks shown with -> only when listing their containing directory
        result = await _run(Ls, ["-l", str(tmp_path)])
        assert isinstance(result, ToolOk)
        assert "->" in result.output


# ---------------------------------------------------------------------------
# Grep
# ---------------------------------------------------------------------------
class TestGrep:
    async def test_basic_match(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello world\nfoo bar\n", encoding="utf-8")
        result = await _run(Grep, ["hello", str(f)])
        assert isinstance(result, ToolOk)
        assert "hello world" in result.output
        assert "foo bar" not in result.output

    async def test_invert_match(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello\nworld\n", encoding="utf-8")
        result = await _run(Grep, ["-v", "hello", str(f)])
        assert isinstance(result, ToolOk)
        assert "world" in result.output
        assert "hello" not in result.output

    async def test_ignore_case(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("Hello\n", encoding="utf-8")
        result = await _run(Grep, ["-i", "hello", str(f)])
        assert isinstance(result, ToolOk)
        assert "Hello" in result.output

    async def test_line_number(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a\nb\n", encoding="utf-8")
        result = await _run(Grep, ["-n", "b", str(f)])
        assert isinstance(result, ToolOk)
        assert "2:b" in result.output

    async def test_count_only(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a\na\n", encoding="utf-8")
        result = await _run(Grep, ["-c", "a", str(f)])
        assert isinstance(result, ToolOk)
        assert result.output == "2"

    async def test_fixed_strings(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a.b\n", encoding="utf-8")
        result = await _run(Grep, ["-F", "a.b", str(f)])
        assert isinstance(result, ToolOk)
        assert "a.b" in result.output

    async def test_recursive(self, tmp_path: Path) -> None:
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "a.txt").write_text("findme\n")
        result = await _run(Grep, ["-r", "findme", str(tmp_path)])
        assert isinstance(result, ToolOk)
        assert "findme" in result.output

    async def test_missing_pattern(self, tmp_path: Path) -> None:
        result = await _run(Grep, [])
        assert isinstance(result, ToolError)
        assert "missing pattern" in result.message.lower()

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Grep, ["pattern"])
        assert isinstance(result, ToolError)
        assert "missing file" in result.message.lower()

    async def test_multiple_files(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("x\n")
        f2.write_text("x\n")
        result = await _run(Grep, ["x", str(f1), str(f2)])
        assert isinstance(result, ToolOk)
        assert str(f1) in result.output or str(f2) in result.output


# ---------------------------------------------------------------------------
# Mkdir
# ---------------------------------------------------------------------------
class TestMkdir:
    async def test_create_directory(self, tmp_path: Path) -> None:
        d = tmp_path / "newdir"
        result = await _run(Mkdir, [str(d)])
        assert isinstance(result, ToolOk)
        assert d.is_dir()

    async def test_parents(self, tmp_path: Path) -> None:
        d = tmp_path / "a" / "b" / "c"
        result = await _run(Mkdir, ["-p", str(d)])
        assert isinstance(result, ToolOk)
        assert d.is_dir()

    async def test_existing_without_parents(self, tmp_path: Path) -> None:
        d = tmp_path / "existing"
        d.mkdir()
        result = await _run(Mkdir, [str(d)])
        assert isinstance(result, ToolError)
        assert "File exists" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(Mkdir, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

    async def test_cwd_relative(self, tmp_path: Path) -> None:
        result = await _run(Mkdir, ["newdir"], cwd=str(tmp_path))
        assert isinstance(result, ToolOk)
        assert (tmp_path / "newdir").is_dir()


# ---------------------------------------------------------------------------
# Awk
# ---------------------------------------------------------------------------
class TestAwk:
    async def test_print_all(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a b c\n", encoding="utf-8")
        result = await _run(Awk, ['{print $0}', str(f)])
        assert isinstance(result, ToolOk)
        assert "a b c" in result.output

    async def test_print_field(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a b c\n", encoding="utf-8")
        result = await _run(Awk, ['{print $2}', str(f)])
        assert isinstance(result, ToolOk)
        assert result.output == "b"

    async def test_custom_delimiter(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a,b,c\n", encoding="utf-8")
        result = await _run(Awk, ["-F,", '{print $2}', str(f)])
        assert isinstance(result, ToolOk)
        assert result.output == "b"

    async def test_missing_program(self, tmp_path: Path) -> None:
        result = await _run(Awk, [])
        assert isinstance(result, ToolError)
        assert "missing program" in result.message.lower()

    async def test_missing_file(self) -> None:
        result = await _run(Awk, ['{print $0}'])
        assert isinstance(result, ToolError)
        assert "missing file" in result.message.lower()

    async def test_missing_file_not_found(self, tmp_path: Path) -> None:
        result = await _run(Awk, ['{print $0}', str(tmp_path / "missing.txt")])
        assert isinstance(result, ToolOk)
        assert "No such file" in result.output


# ---------------------------------------------------------------------------
# Bunzip2 / Bzip2
# ---------------------------------------------------------------------------
class TestBunzip2:
    async def test_decompress(self, tmp_path: Path) -> None:
        src = tmp_path / "file.txt"
        src.write_text("hello", encoding="utf-8")
        compressed = tmp_path / "file.txt.bz2"
        with bz2.open(compressed, "wb") as f:
            f.write(b"hello")
        result = await _run(Bunzip2, [str(compressed)])
        assert isinstance(result, ToolOk)
        assert not compressed.exists()
        assert (tmp_path / "file.txt").exists()

    async def test_keep(self, tmp_path: Path) -> None:
        compressed = tmp_path / "file.txt.bz2"
        with bz2.open(compressed, "wb") as f:
            f.write(b"hello")
        result = await _run(Bunzip2, ["-k", str(compressed)])
        assert isinstance(result, ToolOk)
        assert compressed.exists()

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Bunzip2, [str(tmp_path / "missing.bz2")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(Bunzip2, [])
        assert isinstance(result, ToolError)
        assert "missing" in result.message.lower()


class TestBzip2:
    async def test_compress(self, tmp_path: Path) -> None:
        src = tmp_path / "file.txt"
        src.write_text("hello", encoding="utf-8")
        result = await _run(Bzip2, [str(src)])
        assert isinstance(result, ToolOk)
        assert not src.exists()
        assert (tmp_path / "file.txt.bz2").exists()

    async def test_decompress_flag(self, tmp_path: Path) -> None:
        compressed = tmp_path / "file.txt.bz2"
        with bz2.open(compressed, "wb") as f:
            f.write(b"hello")
        result = await _run(Bzip2, ["-d", str(compressed)])
        assert isinstance(result, ToolOk)
        assert not compressed.exists()
        assert (tmp_path / "file.txt").exists()

    async def test_keep(self, tmp_path: Path) -> None:
        src = tmp_path / "file.txt"
        src.write_text("hello", encoding="utf-8")
        result = await _run(Bzip2, ["-k", str(src)])
        assert isinstance(result, ToolOk)
        assert src.exists()
        assert (tmp_path / "file.txt.bz2").exists()


# ---------------------------------------------------------------------------
# Cal
# ---------------------------------------------------------------------------
class TestCal:
    async def test_current_month(self) -> None:
        result = await _run(Cal, [])
        assert isinstance(result, ToolOk)
        assert "Mo Tu We Th Fr Sa Su" in result.output or "Su Mo Tu We Th Fr Sa" in result.output

    async def test_specific_month(self) -> None:
        result = await _run(Cal, ["3", "2024"])
        assert isinstance(result, ToolOk)
        assert "March" in result.output or "2024" in result.output

    async def test_year_only(self) -> None:
        result = await _run(Cal, ["2024"])
        assert isinstance(result, ToolOk)
        assert "2024" in result.output


# ---------------------------------------------------------------------------
# Cp
# ---------------------------------------------------------------------------
class TestCp:
    async def test_copy_file(self, tmp_path: Path) -> None:
        src = tmp_path / "a.txt"
        dst = tmp_path / "b.txt"
        src.write_text("hello", encoding="utf-8")
        result = await _run(Cp, [str(src), str(dst)])
        assert isinstance(result, ToolOk)
        assert dst.read_text(encoding="utf-8") == "hello"

    async def test_copy_to_directory(self, tmp_path: Path) -> None:
        src = tmp_path / "a.txt"
        d = tmp_path / "dest"
        d.mkdir()
        src.write_text("hello", encoding="utf-8")
        result = await _run(Cp, [str(src), str(d)])
        assert isinstance(result, ToolOk)
        assert (d / "a.txt").read_text(encoding="utf-8") == "hello"

    async def test_copy_directory_recursive(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("hello")
        dst = tmp_path / "dst"
        result = await _run(Cp, ["-r", str(src), str(dst)])
        assert isinstance(result, ToolOk)
        assert (dst / "a.txt").read_text(encoding="utf-8") == "hello"

    async def test_copy_directory_without_recursive(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        dst = tmp_path / "dst"
        result = await _run(Cp, [str(src), str(dst)])
        assert isinstance(result, ToolError)
        assert "-r not specified" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(Cp, [])
        assert isinstance(result, ToolError)
        assert "missing" in result.message.lower()

    async def test_missing_source(self, tmp_path: Path) -> None:
        result = await _run(Cp, [str(tmp_path / "missing.txt"), str(tmp_path / "dst")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.output


# ---------------------------------------------------------------------------
# Cut
# ---------------------------------------------------------------------------
class TestCut:
    async def test_field(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a:b:c\n", encoding="utf-8")
        result = await _run(Cut, ["-d:", "-f2", str(f)])
        assert isinstance(result, ToolOk)
        assert result.output == "b"

    async def test_field_range(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a:b:c:d\n", encoding="utf-8")
        result = await _run(Cut, ["-d:", "-f2-3", str(f)])
        assert isinstance(result, ToolOk)
        assert result.output == "b:c"

    async def test_bytes(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello\n", encoding="utf-8")
        result = await _run(Cut, ["-b1-3", str(f)])
        assert isinstance(result, ToolOk)
        assert result.output == "hel"

    async def test_chars(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello\n", encoding="utf-8")
        result = await _run(Cut, ["-c1-3", str(f)])
        assert isinstance(result, ToolOk)
        assert result.output == "hel"

    async def test_missing_file(self) -> None:
        result = await _run(Cut, ["-f1"])
        assert isinstance(result, ToolError)
        assert "missing" in result.message.lower()

    async def test_file_not_found(self, tmp_path: Path) -> None:
        result = await _run(Cut, ["-f1", str(tmp_path / "missing.txt")])
        assert isinstance(result, ToolOk)
        assert "No such file" in result.output


# ---------------------------------------------------------------------------
# Date
# ---------------------------------------------------------------------------
class TestDate:
    async def test_default(self) -> None:
        result = await _run(Date, [])
        assert isinstance(result, ToolOk)
        assert len(result.output) > 0

    async def test_format(self) -> None:
        result = await _run(Date, ["+%Y-%m-%d"])
        assert isinstance(result, ToolOk)
        assert len(result.output.split("-")) == 3

    async def test_utc(self) -> None:
        result = await _run(Date, ["-u"])
        assert isinstance(result, ToolOk)
        assert len(result.output) > 0


# ---------------------------------------------------------------------------
# Df
# ---------------------------------------------------------------------------
class TestDf:
    async def test_default(self) -> None:
        result = await _run(Df, [])
        assert isinstance(result, ToolOk)
        assert "Filesystem" in result.output

    async def test_human_readable(self) -> None:
        result = await _run(Df, ["-h"])
        assert isinstance(result, ToolOk)
        assert "Filesystem" in result.output

    async def test_specific_path(self, tmp_path: Path) -> None:
        result = await _run(Df, [str(tmp_path)])
        assert isinstance(result, ToolOk)
        assert "Filesystem" in result.output


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------
class TestDiff:
    async def test_identical_files(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("hello\n", encoding="utf-8")
        f2.write_text("hello\n", encoding="utf-8")
        result = await _run(Diff, [str(f1), str(f2)])
        assert isinstance(result, ToolOk)
        # unified diff of identical files is empty
        assert result.output == ""

    async def test_different_files(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("hello\n", encoding="utf-8")
        f2.write_text("world\n", encoding="utf-8")
        result = await _run(Diff, [str(f1), str(f2)])
        assert isinstance(result, ToolOk)
        assert "hello" in result.output or "world" in result.output or "@@" in result.output

    async def test_directories(self, tmp_path: Path) -> None:
        d1 = tmp_path / "d1"
        d2 = tmp_path / "d2"
        d1.mkdir()
        d2.mkdir()
        (d1 / "a.txt").write_text("a")
        (d2 / "a.txt").write_text("b")
        result = await _run(Diff, [str(d1), str(d2)])
        assert isinstance(result, ToolOk)

    async def test_missing_operand(self) -> None:
        result = await _run(Diff, [])
        assert isinstance(result, ToolError)
        assert "missing" in result.message.lower()

    async def test_missing_file(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a")
        result = await _run(Diff, [str(f), str(tmp_path / "missing.txt")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.message


# ---------------------------------------------------------------------------
# Du
# ---------------------------------------------------------------------------
class TestDu:
    async def test_directory(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("a")
        result = await _run(Du, [str(tmp_path)])
        assert isinstance(result, ToolOk)
        assert str(tmp_path) in result.output

    async def test_human_readable(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("a")
        result = await _run(Du, ["-h", str(tmp_path)])
        assert isinstance(result, ToolOk)
        assert "K" in result.output or "B" in result.output or "0" in result.output

    async def test_summarize(self, tmp_path: Path) -> None:
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "a.txt").write_text("a")
        result = await _run(Du, ["-s", str(tmp_path)])
        assert isinstance(result, ToolOk)
        assert str(tmp_path) in result.output
        assert str(sub) not in result.output

    async def test_max_depth(self, tmp_path: Path) -> None:
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "a.txt").write_text("a")
        result = await _run(Du, ["-d", "0", str(tmp_path)])
        assert isinstance(result, ToolOk)
        assert str(tmp_path) in result.output


# ---------------------------------------------------------------------------
# Env / Printenv
# ---------------------------------------------------------------------------
class TestEnv:
    async def test_default(self) -> None:
        result = await _run(Env, [])
        assert isinstance(result, ToolOk)
        assert "=" in result.output or "exported" in result.output

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "env.txt"
        result = await _run(Env, [], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output
        assert "=" in out.read_text(encoding="utf-8")


class TestPrintenv:
    async def test_all(self) -> None:
        result = await _run(Printenv, [])
        assert isinstance(result, ToolOk)
        assert "=" in result.output or "exported" in result.output

    async def test_specific_var(self, tmp_path: Path) -> None:
        os.environ["TEST_VAR_KIMIX"] = "test_value"
        try:
            result = await _run(Printenv, ["TEST_VAR_KIMIX"])
            assert isinstance(result, ToolOk)
            assert result.output == "test_value"
        finally:
            os.environ.pop("TEST_VAR_KIMIX", None)

    async def test_missing_var(self) -> None:
        result = await _run(Printenv, ["NONEXISTENT_VAR_12345"])
        assert isinstance(result, ToolOk)
        assert result.output == ""


# ---------------------------------------------------------------------------
# File
# ---------------------------------------------------------------------------
class TestFile:
    async def test_text_file(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello", encoding="utf-8")
        result = await _run(File, [str(f)])
        assert isinstance(result, ToolOk)
        assert "text" in result.output.lower() or "ASCII" in result.output

    async def test_directory(self, tmp_path: Path) -> None:
        d = tmp_path / "adir"
        d.mkdir()
        result = await _run(File, [str(d)])
        assert isinstance(result, ToolOk)
        assert "directory" in result.output.lower()

    async def test_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.txt"
        f.write_text("", encoding="utf-8")
        result = await _run(File, [str(f)])
        assert isinstance(result, ToolOk)
        assert "empty" in result.output.lower()

    async def test_binary_file(self, tmp_path: Path) -> None:
        f = tmp_path / "binary.dat"
        f.write_bytes(b"\x00\x01\x02")
        result = await _run(File, [str(f)])
        assert isinstance(result, ToolOk)
        assert "data" in result.output.lower()

    async def test_symlink(self, tmp_path: Path) -> None:
        target = tmp_path / "target.txt"
        target.write_text("t")
        link = tmp_path / "link.txt"
        link.symlink_to(target)
        result = await _run(File, [str(link)])
        assert isinstance(result, ToolOk)
        assert "symbolic link" in result.output.lower()

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(File, [str(tmp_path / "missing")])
        assert isinstance(result, ToolOk)
        assert "cannot open" in result.output.lower() or "No such file" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(File, [])
        assert isinstance(result, ToolError)
        assert "missing" in result.message.lower()


# ---------------------------------------------------------------------------
# Find
# ---------------------------------------------------------------------------
class TestFind:
    async def test_default(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("a")
        result = await _run(Find, [str(tmp_path)])
        assert isinstance(result, ToolOk)
        assert "a.txt" in result.output

    async def test_name_pattern(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.py").write_text("b")
        result = await _run(Find, [str(tmp_path), "-name", "*.txt"])
        assert isinstance(result, ToolOk)
        assert "a.txt" in result.output
        assert "b.py" not in result.output

    async def test_type_directory(self, tmp_path: Path) -> None:
        sub = tmp_path / "sub"
        sub.mkdir()
        (tmp_path / "a.txt").write_text("a")
        result = await _run(Find, [str(tmp_path), "-type", "d"])
        assert isinstance(result, ToolOk)
        assert "sub" in result.output
        assert "a.txt" not in result.output

    async def test_type_file(self, tmp_path: Path) -> None:
        sub = tmp_path / "sub"
        sub.mkdir()
        (tmp_path / "a.txt").write_text("a")
        result = await _run(Find, [str(tmp_path), "-type", "f"])
        assert isinstance(result, ToolOk)
        assert "a.txt" in result.output
        assert "sub" not in result.output

    async def test_maxdepth(self, tmp_path: Path) -> None:
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "a.txt").write_text("a")
        result = await _run(Find, [str(tmp_path), "-maxdepth", "1"])
        assert isinstance(result, ToolOk)
        # maxdepth 1 should not recurse into sub
        assert "a.txt" not in result.output

    async def test_missing_path(self, tmp_path: Path) -> None:
        result = await _run(Find, [str(tmp_path / "missing")])
        assert isinstance(result, ToolOk)
        assert "No such file" in result.output


# ---------------------------------------------------------------------------
# Gunzip / Gzip
# ---------------------------------------------------------------------------
class TestGunzip:
    async def test_decompress(self, tmp_path: Path) -> None:
        compressed = tmp_path / "file.txt.gz"
        with gzip.open(compressed, "wb") as f:
            f.write(b"hello")
        result = await _run(Gunzip, [str(compressed)])
        assert isinstance(result, ToolOk)
        assert not compressed.exists()
        assert (tmp_path / "file.txt").read_bytes() == b"hello"

    async def test_keep(self, tmp_path: Path) -> None:
        compressed = tmp_path / "file.txt.gz"
        with gzip.open(compressed, "wb") as f:
            f.write(b"hello")
        result = await _run(Gunzip, ["-k", str(compressed)])
        assert isinstance(result, ToolOk)
        assert compressed.exists()

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Gunzip, [str(tmp_path / "missing.gz")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(Gunzip, [])
        assert isinstance(result, ToolError)
        assert "missing" in result.message.lower()


class TestGzip:
    async def test_compress(self, tmp_path: Path) -> None:
        src = tmp_path / "file.txt"
        src.write_text("hello", encoding="utf-8")
        result = await _run(Gzip, [str(src)])
        assert isinstance(result, ToolOk)
        assert not src.exists()
        compressed = tmp_path / "file.txt.gz"
        assert compressed.exists()
        with gzip.open(compressed, "rb") as f:
            assert f.read() == b"hello"

    async def test_decompress_flag(self, tmp_path: Path) -> None:
        compressed = tmp_path / "file.txt.gz"
        with gzip.open(compressed, "wb") as f:
            f.write(b"hello")
        result = await _run(Gzip, ["-d", str(compressed)])
        assert isinstance(result, ToolOk)
        assert not compressed.exists()
        assert (tmp_path / "file.txt").read_bytes() == b"hello"

    async def test_keep(self, tmp_path: Path) -> None:
        src = tmp_path / "file.txt"
        src.write_text("hello", encoding="utf-8")
        result = await _run(Gzip, ["-k", str(src)])
        assert isinstance(result, ToolOk)
        assert src.exists()
        assert (tmp_path / "file.txt.gz").exists()


# ---------------------------------------------------------------------------
# Head
# ---------------------------------------------------------------------------
class TestHead:
    async def test_default_lines(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("\n".join(str(i) for i in range(20)) + "\n", encoding="utf-8")
        result = await _run(Head, [str(f)])
        assert isinstance(result, ToolOk)
        assert len(result.output.strip().split("\n")) == 10

    async def test_custom_lines(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("\n".join(str(i) for i in range(5)) + "\n", encoding="utf-8")
        result = await _run(Head, ["-n", "3", str(f)])
        assert isinstance(result, ToolOk)
        assert len(result.output.strip().split("\n")) == 3

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Head, [str(tmp_path / "missing.txt")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.output

    async def test_output_path(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        out = tmp_path / "out.txt"
        f.write_text("hello\n", encoding="utf-8")
        result = await _run(Head, [str(f)], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert out.read_text(encoding="utf-8") == "hello\n"


# ---------------------------------------------------------------------------
# Ln
# ---------------------------------------------------------------------------
class TestLn:
    async def test_symbolic_link(self, tmp_path: Path) -> None:
        src = tmp_path / "a.txt"
        src.write_text("hello")
        dst = tmp_path / "link.txt"
        result = await _run(Ln, ["-s", str(src), str(dst)])
        assert isinstance(result, ToolOk)
        assert dst.is_symlink()
        assert dst.read_text() == "hello"

    async def test_hard_link(self, tmp_path: Path) -> None:
        src = tmp_path / "a.txt"
        src.write_text("hello")
        dst = tmp_path / "link.txt"
        result = await _run(Ln, [str(src), str(dst)])
        # Hard links may fail on Windows without admin rights
        if isinstance(result, ToolError) and platform.system() == "Windows":
            pytest.skip("Hard links require admin on Windows")
        assert isinstance(result, ToolOk)
        assert dst.exists()
        assert dst.read_text() == "hello"

    async def test_force(self, tmp_path: Path) -> None:
        src = tmp_path / "a.txt"
        src.write_text("hello")
        dst = tmp_path / "link.txt"
        dst.write_text("existing")
        result = await _run(Ln, ["-s", "-f", str(src), str(dst)])
        assert isinstance(result, ToolOk)
        assert dst.is_symlink()

    async def test_missing_operand(self) -> None:
        result = await _run(Ln, [])
        assert isinstance(result, ToolError)
        assert "missing" in result.message.lower()

    async def test_link_to_directory(self, tmp_path: Path) -> None:
        src = tmp_path / "a.txt"
        src.write_text("hello")
        d = tmp_path / "dest"
        d.mkdir()
        result = await _run(Ln, ["-s", str(src), str(d)])
        assert isinstance(result, ToolOk)
        assert (d / "a.txt").is_symlink()


# ---------------------------------------------------------------------------
# Mv
# ---------------------------------------------------------------------------
class TestMv:
    async def test_rename_file(self, tmp_path: Path) -> None:
        src = tmp_path / "a.txt"
        dst = tmp_path / "b.txt"
        src.write_text("hello")
        result = await _run(Mv, [str(src), str(dst)])
        assert isinstance(result, ToolOk)
        assert not src.exists()
        assert dst.read_text() == "hello"

    async def test_move_to_directory(self, tmp_path: Path) -> None:
        src = tmp_path / "a.txt"
        d = tmp_path / "dest"
        d.mkdir()
        src.write_text("hello")
        result = await _run(Mv, [str(src), str(d)])
        assert isinstance(result, ToolOk)
        assert (d / "a.txt").read_text() == "hello"

    async def test_missing_operand(self) -> None:
        result = await _run(Mv, [])
        assert isinstance(result, ToolError)
        assert "missing" in result.message.lower()

    async def test_missing_source(self, tmp_path: Path) -> None:
        result = await _run(Mv, [str(tmp_path / "missing.txt"), str(tmp_path / "dst")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.output


# ---------------------------------------------------------------------------
# Ps
# ---------------------------------------------------------------------------
class TestPs:
    async def test_default(self) -> None:
        result = await _run(Ps, [])
        assert isinstance(result, ToolOk)
        assert "PID" in result.output or "exported" in result.output

    async def test_all_users(self) -> None:
        result = await _run(Ps, ["aux"])
        assert isinstance(result, ToolOk)
        assert "PID" in result.output or "exported" in result.output

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "ps.txt"
        result = await _run(Ps, [], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "PID" in out.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Pwd
# ---------------------------------------------------------------------------
class TestPwd:
    async def test_default(self, tmp_path: Path) -> None:
        result = await _run(Pwd, [])
        assert isinstance(result, ToolOk)
        assert len(result.output) > 0

    async def test_cwd(self, tmp_path: Path) -> None:
        result = await _run(Pwd, [], cwd=str(tmp_path))
        assert isinstance(result, ToolOk)
        assert str(tmp_path) in result.output

    async def test_physical(self, tmp_path: Path) -> None:
        result = await _run(Pwd, ["-P"])
        assert isinstance(result, ToolOk)
        assert len(result.output) > 0

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "pwd.txt"
        result = await _run(Pwd, [], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert out.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Rm
# ---------------------------------------------------------------------------
class TestRm:
    async def test_remove_file(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a")
        result = await _run(Rm, [str(f)])
        assert isinstance(result, ToolOk)
        assert not f.exists()

    async def test_remove_directory_recursive(self, tmp_path: Path) -> None:
        d = tmp_path / "adir"
        d.mkdir()
        (d / "a.txt").write_text("a")
        result = await _run(Rm, ["-r", str(d)])
        assert isinstance(result, ToolOk)
        assert not d.exists()

    async def test_remove_directory_without_recursive(self, tmp_path: Path) -> None:
        d = tmp_path / "adir"
        d.mkdir()
        result = await _run(Rm, [str(d)])
        assert isinstance(result, ToolError)
        assert "Is a directory" in result.output

    async def test_force_missing(self, tmp_path: Path) -> None:
        result = await _run(Rm, ["-f", str(tmp_path / "missing.txt")])
        assert isinstance(result, ToolOk)

    async def test_missing_operand(self) -> None:
        result = await _run(Rm, [])
        assert isinstance(result, ToolError)
        assert "missing" in result.message.lower()

    async def test_rf(self, tmp_path: Path) -> None:
        d = tmp_path / "adir"
        d.mkdir()
        (d / "a.txt").write_text("a")
        result = await _run(Rm, ["-rf", str(d)])
        assert isinstance(result, ToolOk)
        assert not d.exists()


# ---------------------------------------------------------------------------
# Rmdir
# ---------------------------------------------------------------------------
class TestRmdir:
    async def test_remove_empty(self, tmp_path: Path) -> None:
        d = tmp_path / "empty"
        d.mkdir()
        result = await _run(Rmdir, [str(d)])
        assert isinstance(result, ToolOk)
        assert not d.exists()

    async def test_remove_nonempty(self, tmp_path: Path) -> None:
        d = tmp_path / "nonempty"
        d.mkdir()
        (d / "a.txt").write_text("a")
        result = await _run(Rmdir, [str(d)])
        assert isinstance(result, ToolError)

    async def test_parents(self, tmp_path: Path) -> None:
        d = tmp_path / "a" / "b" / "c"
        d.mkdir(parents=True)
        result = await _run(Rmdir, ["-p", str(d)])
        assert isinstance(result, ToolOk)
        assert not (tmp_path / "a").exists()

    async def test_missing_operand(self) -> None:
        result = await _run(Rmdir, [])
        assert isinstance(result, ToolError)
        assert "missing" in result.message.lower()

    async def test_missing_directory(self, tmp_path: Path) -> None:
        result = await _run(Rmdir, [str(tmp_path / "missing")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.output


# ---------------------------------------------------------------------------
# Sed
# ---------------------------------------------------------------------------
class TestSed:
    async def test_substitute(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello world\n", encoding="utf-8")
        result = await _run(Sed, ["s/hello/hi/", str(f)])
        assert isinstance(result, ToolOk)
        assert "hi world" in result.output
        assert "hello" not in result.output

    async def test_substitute_global(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a a a\n", encoding="utf-8")
        result = await _run(Sed, ["s/a/b/g", str(f)])
        assert isinstance(result, ToolOk)
        assert result.output == "b b b"

    async def test_substitute_count(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a a a\n", encoding="utf-8")
        result = await _run(Sed, ["s/a/b/2", str(f)])
        assert isinstance(result, ToolOk)
        assert result.output == "b b a"

    async def test_delete_line(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a\nb\nc\n", encoding="utf-8")
        result = await _run(Sed, ["d2", str(f)])
        assert isinstance(result, ToolOk)
        assert "a" in result.output
        assert "b" not in result.output
        assert "c" in result.output

    async def test_missing_script(self, tmp_path: Path) -> None:
        result = await _run(Sed, [])
        assert isinstance(result, ToolError)
        assert "missing script" in result.message.lower()

    async def test_missing_file(self) -> None:
        result = await _run(Sed, ["s/a/b/"])
        assert isinstance(result, ToolError)
        assert "missing" in result.message.lower()

    async def test_file_not_found(self, tmp_path: Path) -> None:
        result = await _run(Sed, ["s/a/b/", str(tmp_path / "missing.txt")])
        assert isinstance(result, ToolOk)
        assert "No such file" in result.output

    async def test_bad_script(self, tmp_path: Path) -> None:
        result = await _run(Sed, ["s/a", str(tmp_path / "a.txt")])
        assert isinstance(result, ToolError)
        assert "bad script" in result.message.lower()


# ---------------------------------------------------------------------------
# Tail
# ---------------------------------------------------------------------------
class TestTail:
    async def test_default_lines(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("\n".join(str(i) for i in range(20)) + "\n", encoding="utf-8")
        result = await _run(Tail, [str(f)])
        assert isinstance(result, ToolOk)
        assert len(result.output.strip().split("\n")) == 10

    async def test_custom_lines(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("\n".join(str(i) for i in range(5)) + "\n", encoding="utf-8")
        result = await _run(Tail, ["-n", "3", str(f)])
        assert isinstance(result, ToolOk)
        assert len(result.output.strip().split("\n")) == 3

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Tail, [str(tmp_path / "missing.txt")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.output

    async def test_output_path(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        out = tmp_path / "out.txt"
        f.write_text("hello\n", encoding="utf-8")
        result = await _run(Tail, [str(f)], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert out.read_text(encoding="utf-8") == "hello\n"


# ---------------------------------------------------------------------------
# Tar
# ---------------------------------------------------------------------------
class TestTar:
    async def test_create_and_list(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello")
        archive = tmp_path / "archive.tar"
        result = await _run(Tar, ["-cf", str(archive), str(f)])
        assert isinstance(result, ToolOk)
        assert archive.exists()

        result = await _run(Tar, ["-tf", str(archive)])
        assert isinstance(result, ToolOk)
        assert "a.txt" in result.output

    async def test_extract(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello")
        archive = tmp_path / "archive.tar"
        result = await _run(Tar, ["-cf", str(archive), str(f)])
        assert isinstance(result, ToolOk)

        extract_dir = tmp_path / "extract"
        extract_dir.mkdir()
        result = await _run(Tar, ["-xf", str(archive)], cwd=str(extract_dir))
        assert isinstance(result, ToolOk)
        assert (extract_dir / "a.txt").read_text() == "hello"

    async def test_missing_archive(self, tmp_path: Path) -> None:
        result = await _run(Tar, ["-tf", str(tmp_path / "missing.tar")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.message

    async def test_missing_mode(self, tmp_path: Path) -> None:
        archive = tmp_path / "archive.tar"
        result = await _run(Tar, ["-f", str(archive)])
        assert isinstance(result, ToolError)
        assert "missing operation mode" in result.message.lower()

    async def test_missing_archive_path(self) -> None:
        result = await _run(Tar, ["-c"])
        assert isinstance(result, ToolError)
        assert "missing archive" in result.message.lower()


# ---------------------------------------------------------------------------
# Touch
# ---------------------------------------------------------------------------
class TestTouch:
    async def test_create_file(self, tmp_path: Path) -> None:
        f = tmp_path / "new.txt"
        result = await _run(Touch, [str(f)])
        assert isinstance(result, ToolOk)
        assert f.exists()

    async def test_update_existing(self, tmp_path: Path) -> None:
        f = tmp_path / "existing.txt"
        f.write_text("a")
        mtime_before = f.stat().st_mtime
        import time
        time.sleep(0.05)
        result = await _run(Touch, [str(f)])
        assert isinstance(result, ToolOk)
        assert f.stat().st_mtime > mtime_before

    async def test_missing_operand(self) -> None:
        result = await _run(Touch, [])
        assert isinstance(result, ToolError)
        assert "missing" in result.message.lower()

    async def test_cwd_relative(self, tmp_path: Path) -> None:
        result = await _run(Touch, ["new.txt"], cwd=str(tmp_path))
        assert isinstance(result, ToolOk)
        assert (tmp_path / "new.txt").exists()


# ---------------------------------------------------------------------------
# Tr
# ---------------------------------------------------------------------------
class TestTr:
    async def test_translate(self) -> None:
        result = await _run(Tr, ["a-z", "A-Z"])
        assert isinstance(result, ToolOk)
        # tr standalone returns a help message
        assert "standalone" in result.output.lower() or "not supported" in result.output.lower()

    async def test_delete(self) -> None:
        result = await _run(Tr, ["-d", "a-z"])
        assert isinstance(result, ToolOk)
        assert "standalone" in result.output.lower() or "not supported" in result.output.lower()

    async def test_missing_operand(self) -> None:
        result = await _run(Tr, [])
        assert isinstance(result, ToolError)
        assert "missing" in result.message.lower()


# ---------------------------------------------------------------------------
# Unxz / Xz
# ---------------------------------------------------------------------------
class TestUnxz:
    async def test_decompress(self, tmp_path: Path) -> None:
        compressed = tmp_path / "file.txt.xz"
        with lzma.open(compressed, "wb") as f:
            f.write(b"hello")
        result = await _run(Unxz, [str(compressed)])
        assert isinstance(result, ToolOk)
        assert not compressed.exists()
        assert (tmp_path / "file.txt").read_bytes() == b"hello"

    async def test_keep(self, tmp_path: Path) -> None:
        compressed = tmp_path / "file.txt.xz"
        with lzma.open(compressed, "wb") as f:
            f.write(b"hello")
        result = await _run(Unxz, ["-k", str(compressed)])
        assert isinstance(result, ToolOk)
        assert compressed.exists()

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Unxz, [str(tmp_path / "missing.xz")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.output


class TestXz:
    async def test_compress(self, tmp_path: Path) -> None:
        src = tmp_path / "file.txt"
        src.write_text("hello", encoding="utf-8")
        result = await _run(Xz, [str(src)])
        assert isinstance(result, ToolOk)
        assert not src.exists()
        compressed = tmp_path / "file.txt.xz"
        assert compressed.exists()
        with lzma.open(compressed, "rb") as f:
            assert f.read() == b"hello"

    async def test_decompress_flag(self, tmp_path: Path) -> None:
        compressed = tmp_path / "file.txt.xz"
        with lzma.open(compressed, "wb") as f:
            f.write(b"hello")
        result = await _run(Xz, ["-d", str(compressed)])
        assert isinstance(result, ToolOk)
        assert not compressed.exists()
        assert (tmp_path / "file.txt").read_bytes() == b"hello"

    async def test_keep(self, tmp_path: Path) -> None:
        src = tmp_path / "file.txt"
        src.write_text("hello", encoding="utf-8")
        result = await _run(Xz, ["-k", str(src)])
        assert isinstance(result, ToolOk)
        assert src.exists()
        assert (tmp_path / "file.txt.xz").exists()


# ---------------------------------------------------------------------------
# Unzip / Zip
# ---------------------------------------------------------------------------
class TestUnzip:
    async def test_extract(self, tmp_path: Path) -> None:
        archive = tmp_path / "archive.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("a.txt", "hello")
        extract_dir = tmp_path / "extract"
        extract_dir.mkdir()
        result = await _run(Unzip, [str(archive), str(extract_dir)])
        assert isinstance(result, ToolOk)
        assert (extract_dir / "a.txt").read_text() == "hello"

    async def test_list(self, tmp_path: Path) -> None:
        archive = tmp_path / "archive.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("a.txt", "hello")
        result = await _run(Unzip, ["-l", str(archive)])
        assert isinstance(result, ToolOk)
        assert "a.txt" in result.output

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Unzip, [str(tmp_path / "missing.zip")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.message

    async def test_missing_operand(self) -> None:
        result = await _run(Unzip, [])
        assert isinstance(result, ToolError)
        assert "missing" in result.message.lower()


class TestZip:
    async def test_create(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello")
        archive = tmp_path / "archive.zip"
        result = await _run(Zip, [str(archive), str(f)])
        assert isinstance(result, ToolOk)
        assert archive.exists()
        with zipfile.ZipFile(archive, "r") as zf:
            assert zf.read("a.txt").decode() == "hello"

    async def test_recursive(self, tmp_path: Path) -> None:
        d = tmp_path / "dir"
        d.mkdir()
        (d / "a.txt").write_text("hello")
        archive = tmp_path / "archive.zip"
        result = await _run(Zip, ["-r", str(archive), str(d)])
        assert isinstance(result, ToolOk)
        assert archive.exists()
        with zipfile.ZipFile(archive, "r") as zf:
            names = zf.namelist()
            assert any("a.txt" in n for n in names)

    async def test_missing_operand(self) -> None:
        result = await _run(Zip, [])
        assert isinstance(result, ToolError)
        assert "missing" in result.message.lower()


# ---------------------------------------------------------------------------
# Wc
# ---------------------------------------------------------------------------
class TestWc:
    async def test_default(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello world\nfoo bar\n", encoding="utf-8")
        result = await _run(Wc, [str(f)])
        assert isinstance(result, ToolOk)
        # lines words bytes filename
        parts = result.output.split()
        assert parts[0] == "2"  # lines
        assert parts[1] == "4"  # words
        assert parts[2] == "22"  # bytes
        assert parts[3] == str(f)

    async def test_lines_only(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a\nb\nc\n", encoding="utf-8")
        result = await _run(Wc, ["-l", str(f)])
        assert isinstance(result, ToolOk)
        assert result.output == f"3 {f}"

    async def test_words_only(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a b c\n", encoding="utf-8")
        result = await _run(Wc, ["-w", str(f)])
        assert isinstance(result, ToolOk)
        assert result.output == f"3 {f}"

    async def test_bytes_only(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_bytes(b"abc\n")
        result = await _run(Wc, ["-c", str(f)])
        assert isinstance(result, ToolOk)
        assert result.output == f"4 {f}"

    async def test_multiple_files(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("a\n")
        f2.write_text("b\n")
        result = await _run(Wc, [str(f1), str(f2)])
        assert isinstance(result, ToolOk)
        assert "total" in result.output

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Wc, [str(tmp_path / "missing.txt")])
        assert isinstance(result, ToolOk)
        assert "No such file" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(Wc, [])
        assert isinstance(result, ToolError)
        assert "missing" in result.message.lower()
