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
    Alias,
    Awk,
    Base64,
    Basename,
    Bc,
    Bunzip2,
    Bzip2,
    Cal,
    Cat,
    Chgrp,
    Chmod,
    Chown,
    Cksum,
    Cmp,
    Comm,
    Cp,
    Crontab,
    Csplit,
    Curl,
    Cut,
    Date,
    Dc,
    Df,
    Diff,
    Dirname,
    Du,
    Echo,
    EnvsSubst,
    Env,
    Expand,
    Expr,
    Export,
    Factor,
    FalseCmd,
    File,
    Find,
    Fold,
    Fmt,
    Free,
    Fuser,
    Grep,
    Groups,
    Gunzip,
    Gzip,
    Head,
    Hexdump,
    History,
    Host,
    Hostname,
    Hwclock,
    Id,
    Ip,
    Ifconfig,
    Install,
    Iostat,
    Kill,
    Killall,
    Ln,
    Ls,
    LsbRelease,
    Lsof,
    Man,
    Md5sum,
    Mkdir,
    Mkfifo,
    Mktemp,
    Mv,
    Netstat,
    Nl,
    Nslookup,
    Od,
    Ping,
    Printf,
    Printenv,
    Ps,
    Pwd,
    Readlink,
    Realpath,
    Renice,
    Rev,
    Rm,
    Rmdir,
    Scriptreplay,
    Sed,
    Seq,
    Sha256sum,
    Shuf,
    Sleep,
    Split,
    Ss,
    Stat,
    Strings,
    SwVers,
    Systeminfo,
    Tac,
    Tail,
    Tar,
    Test,
    Top,
    Touch,
    Tr,
    Traceroute,
    Trap,
    Tree,
    TrueCmd,
    Ulimit,
    Umask,
    Uname,
    Unexpand,
    Uniq,
    Unxz,
    Unzip,
    Uptime,
    Vmstat,
    Wc,
    Wget,
    Which,
    Who,
    Whoami,
    Xxd,
    Xz,
    Yes,
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
# Cksum
# ---------------------------------------------------------------------------
class TestCksum:
    async def test_single_file(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello", encoding="utf-8")
        result = await _run(Cksum, [str(f)])
        assert isinstance(result, ToolOk)
        assert "907060870 5" in result.output
        assert str(f) in result.output

    async def test_multiple_files(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("hello", encoding="utf-8")
        f2.write_text("world", encoding="utf-8")
        result = await _run(Cksum, [str(f1), str(f2)])
        assert isinstance(result, ToolOk)
        assert "907060870 5" in result.output
        assert "a.txt" in result.output
        assert "b.txt" in result.output

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Cksum, [str(tmp_path / "missing.txt")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(Cksum, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

    async def test_output_path(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        out = tmp_path / "cksum.txt"
        f.write_text("hello", encoding="utf-8")
        result = await _run(Cksum, [str(f)], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output
        assert "907060870 5" in out.read_text(encoding="utf-8")


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



# ---------------------------------------------------------------------------
# Netstat
# ---------------------------------------------------------------------------
class TestNetstat:
    async def test_default(self) -> None:
        result = await _run(Netstat, ["-tlnp"])
        assert isinstance(result, ToolOk)
        assert "Proto" in result.output or "tcp" in result.output.lower() or result.output == ""

    async def test_listening_ports(self) -> None:
        result = await _run(Netstat, ["-t", "-l", "-n", "-p"])
        assert isinstance(result, (ToolOk, ToolError))

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "netstat.txt"
        result = await _run(Netstat, ["-tlnp"], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output


# ---------------------------------------------------------------------------
# Tree
# ---------------------------------------------------------------------------
class TestTree:
    async def test_directory(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")
        result = await _run(Tree, [str(tmp_path)])
        assert isinstance(result, ToolOk)
        assert "a.txt" in result.output
        assert "b.txt" in result.output

    async def test_subdirectories(self, tmp_path: Path) -> None:
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "c.txt").write_text("c")
        result = await _run(Tree, [str(tmp_path)])
        assert isinstance(result, ToolOk)
        assert "sub" in result.output
        assert "c.txt" in result.output

    async def test_max_depth(self, tmp_path: Path) -> None:
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "c.txt").write_text("c")
        result = await _run(Tree, ["-L", "1", str(tmp_path)])
        assert isinstance(result, ToolOk)
        assert "sub" in result.output
        # c.txt may or may not appear at depth 1 depending on implementation

    async def test_missing_directory(self, tmp_path: Path) -> None:
        result = await _run(Tree, [str(tmp_path / "missing")])
        assert isinstance(result, ToolOk)

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "tree.txt"
        (tmp_path / "a.txt").write_text("a")
        result = await _run(Tree, [str(tmp_path)], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output
        assert "a.txt" in out.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Tac
# ---------------------------------------------------------------------------
class TestTac:
    async def test_single_file(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("line1\nline2\nline3\n", encoding="utf-8")
        result = await _run(Tac, [str(f)])
        assert isinstance(result, ToolOk)
        lines = result.output.splitlines()
        assert lines[0] == "line3"
        assert lines[1] == "line2"
        assert lines[2] == "line1"

    async def test_multiple_files(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("a1\na2\n", encoding="utf-8")
        f2.write_text("b1\nb2\n", encoding="utf-8")
        result = await _run(Tac, [str(f1), str(f2)])
        assert isinstance(result, ToolOk)
        assert "a2" in result.output
        assert "b1" in result.output

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Tac, [str(tmp_path / "missing.txt")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.output

    async def test_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.txt"
        f.write_text("", encoding="utf-8")
        result = await _run(Tac, [str(f)])
        assert isinstance(result, ToolOk)
        assert result.output == ""

    async def test_output_path(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        out = tmp_path / "out.txt"
        f.write_text("hello\nworld\n", encoding="utf-8")
        result = await _run(Tac, [str(f)], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert out.read_text(encoding="utf-8").splitlines()[0] == "world"


# ---------------------------------------------------------------------------
# Stat
# ---------------------------------------------------------------------------
class TestStat:
    async def test_file(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello", encoding="utf-8")
        result = await _run(Stat, [str(f)])
        assert isinstance(result, ToolOk)
        assert "Size:" in result.output
        assert str(f) in result.output

    async def test_directory(self, tmp_path: Path) -> None:
        d = tmp_path / "adir"
        d.mkdir()
        result = await _run(Stat, [str(d)])
        assert isinstance(result, ToolOk)
        assert "Size:" in result.output

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Stat, [str(tmp_path / "missing.txt")])
        assert isinstance(result, ToolOk)
        assert "No such file" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(Stat, [])
        assert isinstance(result, ToolError)
        assert "missing" in result.message.lower()

    async def test_output_path(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        out = tmp_path / "out.txt"
        f.write_text("hello", encoding="utf-8")
        result = await _run(Stat, [str(f)], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output
        assert "Size:" in out.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Uniq
# ---------------------------------------------------------------------------
class TestUniq:
    async def test_default(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a\na\nb\nb\nb\nc\n", encoding="utf-8")
        result = await _run(Uniq, [str(f)])
        assert isinstance(result, ToolOk)
        lines = result.output.splitlines()
        assert lines == ["a", "b", "c"]

    async def test_count(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a\na\nb\n", encoding="utf-8")
        result = await _run(Uniq, ["-c", str(f)])
        assert isinstance(result, ToolOk)
        assert "2 a" in result.output
        assert "1 b" in result.output

    async def test_repeated(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a\na\nb\n", encoding="utf-8")
        result = await _run(Uniq, ["-d", str(f)])
        assert isinstance(result, ToolOk)
        assert "a" in result.output
        assert "b" not in result.output

    async def test_unique_only(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a\na\nb\n", encoding="utf-8")
        result = await _run(Uniq, ["-u", str(f)])
        assert isinstance(result, ToolOk)
        assert "b" in result.output
        assert "a" not in result.output

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Uniq, [str(tmp_path / "missing.txt")])
        assert isinstance(result, ToolOk)
        assert "No such file" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(Uniq, [])
        assert isinstance(result, ToolError)
        assert "missing" in result.message.lower()

    async def test_output_path(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        out = tmp_path / "out.txt"
        f.write_text("a\na\nb\n", encoding="utf-8")
        result = await _run(Uniq, [str(f)], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert out.read_text(encoding="utf-8") == "a\nb"


# ---------------------------------------------------------------------------
# Which
# ---------------------------------------------------------------------------
class TestWhich:
    async def test_python(self) -> None:
        result = await _run(Which, ["python"])
        assert isinstance(result, ToolOk)
        # may or may not find python depending on PATH

    async def test_all(self) -> None:
        result = await _run(Which, ["-a", "python"])
        assert isinstance(result, ToolOk)

    async def test_missing_command(self) -> None:
        result = await _run(Which, ["nonexistent_command_xyz"])
        assert isinstance(result, ToolOk)
        assert "no nonexistent_command_xyz" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(Which, [])
        assert isinstance(result, ToolError)
        assert "no command" in result.message.lower()

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "which.txt"
        result = await _run(Which, ["python"], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output


# ---------------------------------------------------------------------------
# Hwclock
# ---------------------------------------------------------------------------
class TestHwclock:
    async def test_default(self) -> None:
        result = await _run(Hwclock, ["--show"])
        assert isinstance(result, ToolOk)
        assert len(result.output) > 0

    async def test_utc(self) -> None:
        result = await _run(Hwclock, ["--utc"])
        assert isinstance(result, ToolOk)
        assert "UTC" in result.output or len(result.output) > 0

    async def test_localtime(self) -> None:
        result = await _run(Hwclock, ["--localtime"])
        assert isinstance(result, ToolOk)
        assert len(result.output) > 0

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "hwclock.txt"
        result = await _run(Hwclock, [], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output
        assert len(out.read_text(encoding="utf-8")) > 0


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
class TestExport:
    async def test_set_var(self) -> None:
        result = await _run(Export, ["TEST_EXPORT_VAR=hello"])
        assert isinstance(result, ToolOk)
        assert "TEST_EXPORT_VAR=hello" in result.output
        assert os.environ.get("TEST_EXPORT_VAR") == "hello"
        os.environ.pop("TEST_EXPORT_VAR", None)

    async def test_print_all(self) -> None:
        result = await _run(Export, ["-p"])
        assert isinstance(result, ToolOk)
        assert "=" in result.output or "exported" in result.output

    async def test_print_specific(self) -> None:
        os.environ["TEST_EXPORT_VAR2"] = "value2"
        try:
            result = await _run(Export, ["TEST_EXPORT_VAR2"])
            assert isinstance(result, ToolOk)
            assert "value2" in result.output
        finally:
            os.environ.pop("TEST_EXPORT_VAR2", None)

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "export.txt"
        result = await _run(Export, ["-p"], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output
        assert "=" in out.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Basename
# ---------------------------------------------------------------------------
class TestBasename:
    async def test_basic(self) -> None:
        result = await _run(Basename, ["/foo/bar/baz.txt"])
        assert isinstance(result, ToolOk)
        assert result.output == "baz.txt"

    async def test_with_suffix(self) -> None:
        result = await _run(Basename, ["/foo/bar/baz.txt", ".txt"])
        assert isinstance(result, ToolOk)
        assert result.output == "baz"

    async def test_single_component(self) -> None:
        result = await _run(Basename, ["file.txt"])
        assert isinstance(result, ToolOk)
        assert result.output == "file.txt"

    async def test_missing_operand(self) -> None:
        result = await _run(Basename, [])
        assert isinstance(result, ToolError)
        assert "missing" in result.message.lower()

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "basename.txt"
        result = await _run(Basename, ["/a/b/c"], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert out.read_text(encoding="utf-8") == "c"


# ---------------------------------------------------------------------------
# Dirname
# ---------------------------------------------------------------------------
class TestDirname:
    async def test_basic(self) -> None:
        result = await _run(Dirname, ["/foo/bar/baz.txt"])
        assert isinstance(result, ToolOk)
        assert result.output == "/foo/bar"

    async def test_single_component(self) -> None:
        result = await _run(Dirname, ["file.txt"])
        assert isinstance(result, ToolOk)
        assert result.output == "."

    async def test_trailing_slash(self) -> None:
        result = await _run(Dirname, ["/foo/bar/"])
        assert isinstance(result, ToolOk)
        assert result.output == "/foo"

    async def test_missing_operand(self) -> None:
        result = await _run(Dirname, [])
        assert isinstance(result, ToolError)
        assert "missing" in result.message.lower()

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "dirname.txt"
        result = await _run(Dirname, ["/a/b/c"], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert out.read_text(encoding="utf-8") == "/a/b"


# ---------------------------------------------------------------------------
# Realpath
# ---------------------------------------------------------------------------
class TestRealpath:
    async def test_file(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello")
        result = await _run(Realpath, [str(f)])
        assert isinstance(result, ToolOk)
        assert Path(result.output).name == "a.txt"

    async def test_relative_path(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello")
        result = await _run(Realpath, ["a.txt"], cwd=str(tmp_path))
        assert isinstance(result, ToolOk)
        assert Path(result.output).name == "a.txt"

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Realpath, [str(tmp_path / "missing.txt")])
        assert isinstance(result, ToolOk)
        assert "No such file" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(Realpath, [])
        assert isinstance(result, ToolError)
        assert "missing" in result.message.lower()

    async def test_output_path(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        out = tmp_path / "out.txt"
        f.write_text("hello")
        result = await _run(Realpath, [str(f)], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert out.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Mktemp
# ---------------------------------------------------------------------------
class TestMktemp:
    async def test_default_file(self) -> None:
        result = await _run(Mktemp, [])
        assert isinstance(result, ToolOk)
        assert Path(result.output).exists()
        # clean up
        Path(result.output).unlink(missing_ok=True)

    async def test_directory(self) -> None:
        result = await _run(Mktemp, ["-d"])
        assert isinstance(result, ToolOk)
        assert Path(result.output).is_dir()
        # clean up
        import shutil
        shutil.rmtree(result.output, ignore_errors=True)

    async def test_dry_run(self) -> None:
        result = await _run(Mktemp, ["-u"])
        assert isinstance(result, ToolOk)
        assert not Path(result.output).exists()

    async def test_suffix(self) -> None:
        result = await _run(Mktemp, ["--suffix", ".txt"])
        assert isinstance(result, ToolOk)
        assert result.output.endswith(".txt")
        Path(result.output).unlink(missing_ok=True)

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "mktemp.txt"
        result = await _run(Mktemp, [], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output
        assert Path(out.read_text(encoding="utf-8")).exists()
        Path(out.read_text(encoding="utf-8")).unlink(missing_ok=True)

# ---------------------------------------------------------------------------
# Alias
# ---------------------------------------------------------------------------
class TestAlias:
    async def test_list_empty(self) -> None:
        result = await _run(Alias, [])
        assert isinstance(result, ToolOk)

    async def test_set_and_get(self) -> None:
        result = await _run(Alias, ["ll=ls -l"])
        assert isinstance(result, ToolOk)
        result = await _run(Alias, ["ll"])
        assert isinstance(result, ToolOk)
        # Alias lookup returns empty output on success; stored in _ALIAS_STORE

    async def test_get_missing(self) -> None:
        result = await _run(Alias, ["nonexistent_alias"])
        assert isinstance(result, ToolError)

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "alias.txt"
        result = await _run(Alias, [], output_path=str(out))
        assert isinstance(result, ToolOk)

# ---------------------------------------------------------------------------
# Base64
# ---------------------------------------------------------------------------
class TestBase64:
    async def test_encode(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello", encoding="utf-8")
        result = await _run(Base64, [str(f)])
        assert isinstance(result, ToolOk)
        import base64
        assert base64.b64encode(b"hello").decode() in result.output

    async def test_decode(self, tmp_path: Path) -> None:
        import base64
        f = tmp_path / "a.b64"
        f.write_bytes(base64.b64encode(b"hello"))
        result = await _run(Base64, ["-d", str(f)])
        assert isinstance(result, ToolOk)
        assert "hello" in result.output

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Base64, [str(tmp_path / "missing")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.message

# ---------------------------------------------------------------------------
# Bc
# ---------------------------------------------------------------------------
class TestBc:
    async def test_basic_math(self, tmp_path: Path) -> None:
        f = tmp_path / "calc.bc"
        f.write_text("2+3\n", encoding="utf-8")
        result = await _run(Bc, [str(f)])
        assert isinstance(result, ToolOk)
        assert "5" in result.output

    async def test_sqrt(self, tmp_path: Path) -> None:
        f = tmp_path / "calc.bc"
        f.write_text("sqrt(16)\n", encoding="utf-8")
        result = await _run(Bc, [str(f)])
        assert isinstance(result, ToolOk)
        assert "4.0" in result.output

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Bc, [str(tmp_path / "missing.bc")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.message

    async def test_missing_operand(self) -> None:
        result = await _run(Bc, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

# ---------------------------------------------------------------------------
# Chgrp
# ---------------------------------------------------------------------------
class TestChgrp:
    async def test_change_group(self, tmp_path: Path) -> None:
        if not hasattr(os, "getgid"):
            pytest.skip("getgid not available on this platform")
        f = tmp_path / "a.txt"
        f.write_text("hello")
        result = await _run(Chgrp, [str(os.getgid()), str(f)])
        assert isinstance(result, (ToolOk, ToolError))

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Chgrp, ["99999", str(tmp_path / "missing.txt")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.message or "chown" in result.message

    async def test_missing_operand(self) -> None:
        result = await _run(Chgrp, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

# ---------------------------------------------------------------------------
# Chmod
# ---------------------------------------------------------------------------
class TestChmod:
    async def test_change_mode(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello")
        result = await _run(Chmod, ["755", str(f)])
        assert isinstance(result, ToolOk)

    async def test_invalid_mode(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello")
        result = await _run(Chmod, ["abc", str(f)])
        assert isinstance(result, ToolError)
        assert "invalid mode" in result.message

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Chmod, ["755", str(tmp_path / "missing.txt")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(Chmod, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

# ---------------------------------------------------------------------------
# Chown
# ---------------------------------------------------------------------------
class TestChown:
    async def test_change_owner(self, tmp_path: Path) -> None:
        if not hasattr(os, "getuid"):
            pytest.skip("getuid not available on this platform")
        f = tmp_path / "a.txt"
        f.write_text("hello")
        result = await _run(Chown, [str(os.getuid()), str(f)])
        assert isinstance(result, (ToolOk, ToolError))

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Chown, ["99999", str(tmp_path / "missing.txt")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.message or "chown" in result.message

    async def test_missing_operand(self) -> None:
        result = await _run(Chown, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

# ---------------------------------------------------------------------------
# Cmp
# ---------------------------------------------------------------------------
class TestCmp:
    async def test_identical_files(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("hello")
        f2.write_text("hello")
        result = await _run(Cmp, [str(f1), str(f2)])
        assert isinstance(result, ToolOk)

    async def test_different_files(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("hello")
        f2.write_text("world")
        result = await _run(Cmp, [str(f1), str(f2)])
        assert isinstance(result, ToolOk)
        assert "differ" in result.output

    async def test_silent(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("hello")
        f2.write_text("world")
        result = await _run(Cmp, ["-s", str(f1), str(f2)])
        assert isinstance(result, ToolOk)

    async def test_missing_operand(self) -> None:
        result = await _run(Cmp, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

# ---------------------------------------------------------------------------
# Comm
# ---------------------------------------------------------------------------
class TestComm:
    async def test_common_lines(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("a\nb\nc\n", encoding="utf-8")
        f2.write_text("b\nc\nd\n", encoding="utf-8")
        result = await _run(Comm, [str(f1), str(f2)])
        assert isinstance(result, ToolOk)
        assert "b" in result.output
        assert "c" in result.output

    async def test_suppress_columns(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("a\n", encoding="utf-8")
        f2.write_text("a\n", encoding="utf-8")
        result = await _run(Comm, ["-1", "-2", str(f1), str(f2)])
        assert isinstance(result, ToolOk)
        assert "\t\ta" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(Comm, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

# ---------------------------------------------------------------------------
# Crontab
# ---------------------------------------------------------------------------
class TestCrontab:
    async def test_list_empty(self) -> None:
        result = await _run(Crontab, ["-l"])
        assert isinstance(result, ToolOk)

    async def test_edit_not_supported(self) -> None:
        result = await _run(Crontab, ["-e"])
        assert isinstance(result, ToolError)
        assert "not supported" in result.message

    async def test_delete(self) -> None:
        result = await _run(Crontab, ["-r"])
        assert isinstance(result, ToolOk)

    async def test_install(self, tmp_path: Path) -> None:
        f = tmp_path / "cron.txt"
        f.write_text("* * * * * echo hello\n", encoding="utf-8")
        result = await _run(Crontab, [str(f)])
        assert isinstance(result, ToolOk)

# ---------------------------------------------------------------------------
# Csplit
# ---------------------------------------------------------------------------
class TestCsplit:
    async def test_basic_split(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello\nworld\nfoo\n", encoding="utf-8")
        result = await _run(Csplit, [str(f), "world"])
        assert isinstance(result, ToolOk)
        assert "split into" in result.output

    async def test_prefix(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a\nb\nc\n", encoding="utf-8")
        result = await _run(Csplit, ["-f", "out", str(f), "b"])
        assert isinstance(result, ToolOk)
        assert "out" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(Csplit, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

# ---------------------------------------------------------------------------
# Curl
# ---------------------------------------------------------------------------
class TestCurl:
    async def test_missing_url(self) -> None:
        result = await _run(Curl, [])
        assert isinstance(result, ToolError)
        assert "missing URL" in result.message

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "curl.txt"
        result = await _run(Curl, ["https://httpbin.org/get", "-o", str(out)])
        assert isinstance(result, (ToolOk, ToolError))

# ---------------------------------------------------------------------------
# Dc
# ---------------------------------------------------------------------------
class TestDc:
    async def test_add(self, tmp_path: Path) -> None:
        f = tmp_path / "calc.dc"
        f.write_text("2 3 + p\n", encoding="utf-8")
        result = await _run(Dc, [str(f)])
        assert isinstance(result, ToolOk)
        assert "5" in result.output

    async def test_multiply(self, tmp_path: Path) -> None:
        f = tmp_path / "calc.dc"
        f.write_text("4 5 * p\n", encoding="utf-8")
        result = await _run(Dc, [str(f)])
        assert isinstance(result, ToolOk)
        assert "20" in result.output

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Dc, [str(tmp_path / "missing.dc")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.message

    async def test_missing_operand(self) -> None:
        result = await _run(Dc, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

# ---------------------------------------------------------------------------
# Echo
# ---------------------------------------------------------------------------
class TestEcho:
    async def test_basic(self) -> None:
        result = await _run(Echo, ["hello", "world"])
        assert isinstance(result, ToolOk)
        assert result.output == "hello world\n"

    async def test_no_newline(self) -> None:
        result = await _run(Echo, ["-n", "hello"])
        assert isinstance(result, ToolOk)
        assert result.output == "hello"

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "echo.txt"
        result = await _run(Echo, ["hello"], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output
        assert out.read_text(encoding="utf-8") == "hello\n"

# ---------------------------------------------------------------------------
# EnvsSubst
# ---------------------------------------------------------------------------
class TestEnvsSubst:
    async def test_substitute(self, tmp_path: Path) -> None:
        os.environ["TEST_ENVSUBST"] = "world"
        try:
            f = tmp_path / "a.txt"
            f.write_text("hello $TEST_ENVSUBST", encoding="utf-8")
            result = await _run(EnvsSubst, [str(f)])
            assert isinstance(result, ToolOk)
            assert "hello world" in result.output
        finally:
            os.environ.pop("TEST_ENVSUBST", None)

    async def test_braces(self, tmp_path: Path) -> None:
        os.environ["TEST_ENVSUBST2"] = "world"
        try:
            f = tmp_path / "a.txt"
            f.write_text("hello ${TEST_ENVSUBST2}", encoding="utf-8")
            result = await _run(EnvsSubst, [str(f)])
            assert isinstance(result, ToolOk)
            assert "hello world" in result.output
        finally:
            os.environ.pop("TEST_ENVSUBST2", None)

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(EnvsSubst, [str(tmp_path / "missing.txt")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.message

    async def test_missing_operand(self) -> None:
        result = await _run(EnvsSubst, [])
        assert isinstance(result, ToolError)
        assert "missing" in result.message.lower()

# ---------------------------------------------------------------------------
# Expand
# ---------------------------------------------------------------------------
class TestExpand:
    async def test_basic(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a\tb\n", encoding="utf-8")
        result = await _run(Expand, [str(f)])
        assert isinstance(result, ToolOk)
        assert "a" in result.output
        assert "b" in result.output

    async def test_tabstop(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a\tb\n", encoding="utf-8")
        result = await _run(Expand, ["-t", "4", str(f)])
        assert isinstance(result, ToolOk)

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Expand, [str(tmp_path / "missing.txt")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(Expand, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

# ---------------------------------------------------------------------------
# Expr
# ---------------------------------------------------------------------------
class TestExpr:
    async def test_addition(self) -> None:
        result = await _run(Expr, ["2", "+", "3"])
        assert isinstance(result, ToolOk)
        assert result.output == "5"

    async def test_string_equal(self) -> None:
        result = await _run(Expr, ["abc", "=", "abc"])
        assert isinstance(result, ToolOk)
        assert result.output == "1"

    async def test_string_not_equal(self) -> None:
        result = await _run(Expr, ["abc", "=", "def"])
        assert isinstance(result, ToolOk)
        assert result.output == "0"

    async def test_length(self) -> None:
        result = await _run(Expr, ["length", "hello"])
        assert isinstance(result, ToolOk)
        assert result.output == "5"

    async def test_regex_match(self) -> None:
        result = await _run(Expr, ["hello", ":", "el+o"])
        assert isinstance(result, ToolOk)
        assert "ello" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(Expr, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

# ---------------------------------------------------------------------------
# Factor
# ---------------------------------------------------------------------------
class TestFactor:
    async def test_prime(self) -> None:
        result = await _run(Factor, ["13"])
        assert isinstance(result, ToolOk)
        assert "13: 13" in result.output

    async def test_composite(self) -> None:
        result = await _run(Factor, ["12"])
        assert isinstance(result, ToolOk)
        assert "2" in result.output
        assert "3" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(Factor, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

# ---------------------------------------------------------------------------
# FalseCmd
# ---------------------------------------------------------------------------
class TestFalseCmd:
    async def test_returns_error(self) -> None:
        result = await _run(FalseCmd, [])
        assert isinstance(result, ToolError)

# ---------------------------------------------------------------------------
# Fmt
# ---------------------------------------------------------------------------
class TestFmt:
    async def test_basic(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello world foo bar\n", encoding="utf-8")
        result = await _run(Fmt, [str(f)])
        assert isinstance(result, ToolOk)
        assert "hello" in result.output

    async def test_width(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello world\n", encoding="utf-8")
        result = await _run(Fmt, ["-w", "5", str(f)])
        assert isinstance(result, ToolOk)

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Fmt, [str(tmp_path / "missing.txt")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(Fmt, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

# ---------------------------------------------------------------------------
# Fold
# ---------------------------------------------------------------------------
class TestFold:
    async def test_basic(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a" * 100 + "\n", encoding="utf-8")
        result = await _run(Fold, [str(f)])
        assert isinstance(result, ToolOk)
        lines = result.output.strip().split("\n")
        assert all(len(line) <= 80 for line in lines)

    async def test_custom_width(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a" * 20 + "\n", encoding="utf-8")
        result = await _run(Fold, ["-w", "5", str(f)])
        assert isinstance(result, ToolOk)
        lines = result.output.strip().split("\n")
        assert all(len(line) <= 5 for line in lines)

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Fold, [str(tmp_path / "missing.txt")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(Fold, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

# ---------------------------------------------------------------------------
# Free
# ---------------------------------------------------------------------------
class TestFree:
    async def test_default(self) -> None:
        result = await _run(Free, [])
        assert isinstance(result, ToolOk)
        assert "Mem" in result.output

    async def test_human(self) -> None:
        result = await _run(Free, ["-h"])
        assert isinstance(result, ToolOk)
        assert "Mem" in result.output

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "free.txt"
        result = await _run(Free, [], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output

# ---------------------------------------------------------------------------
# Fuser
# ---------------------------------------------------------------------------
class TestFuser:
    async def test_missing_operand(self) -> None:
        result = await _run(Fuser, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

    async def test_windows_not_supported(self) -> None:
        if platform.system() != "Windows":
            pytest.skip("Windows-only test")
        result = await _run(Fuser, ["/tmp"])
        assert isinstance(result, ToolError)
        assert "not supported" in result.message

# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------
class TestGroups:
    async def test_current_user(self) -> None:
        if not hasattr(os, "getgid"):
            pytest.skip("getgid not available on this platform")
        result = await _run(Groups, [])
        assert isinstance(result, ToolOk)

    async def test_specific_user(self) -> None:
        result = await _run(Groups, ["root"])
        assert isinstance(result, (ToolOk, ToolError))

    async def test_output_path(self, tmp_path: Path) -> None:
        if not hasattr(os, "getgid"):
            pytest.skip("getgid not available on this platform")
        out = tmp_path / "groups.txt"
        result = await _run(Groups, [], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output

# ---------------------------------------------------------------------------
# Hexdump
# ---------------------------------------------------------------------------
class TestHexdump:
    async def test_basic(self, tmp_path: Path) -> None:
        f = tmp_path / "a.bin"
        f.write_bytes(b"\x00\x01\x02")
        result = await _run(Hexdump, [str(f)])
        assert isinstance(result, ToolOk)
        assert "00000000" in result.output

    async def test_canonical(self, tmp_path: Path) -> None:
        f = tmp_path / "a.bin"
        f.write_bytes(b"hello")
        result = await _run(Hexdump, ["-C", str(f)])
        assert isinstance(result, ToolOk)
        assert "hello" in result.output

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Hexdump, [str(tmp_path / "missing")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.message

    async def test_missing_operand(self) -> None:
        result = await _run(Hexdump, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
class TestHistory:
    async def test_empty(self) -> None:
        result = await _run(History, [])
        assert isinstance(result, ToolOk)

    async def test_add_and_list(self) -> None:
        await _run(History, ["ls -l"])
        result = await _run(History, [])
        assert isinstance(result, ToolOk)
        assert "ls -l" in result.output

    async def test_clear(self) -> None:
        await _run(History, ["ls"])
        await _run(History, ["-c"])
        result = await _run(History, [])
        assert isinstance(result, ToolOk)
        assert "ls" not in result.output

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "history.txt"
        result = await _run(History, [], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output

# ---------------------------------------------------------------------------
# Host
# ---------------------------------------------------------------------------
class TestHost:
    async def test_localhost(self) -> None:
        result = await _run(Host, ["localhost"])
        assert isinstance(result, ToolOk)
        assert "address" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(Host, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "host.txt"
        result = await _run(Host, ["localhost"], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output

# ---------------------------------------------------------------------------
# Hostname
# ---------------------------------------------------------------------------
class TestHostname:
    async def test_default(self) -> None:
        result = await _run(Hostname, [])
        assert isinstance(result, ToolOk)
        assert len(result.output) > 0

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "hostname.txt"
        result = await _run(Hostname, [], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output

# ---------------------------------------------------------------------------
# Id
# ---------------------------------------------------------------------------
class TestId:
    async def test_default(self) -> None:
        if not hasattr(os, "getuid"):
            pytest.skip("getuid not available on this platform")
        result = await _run(Id, [])
        assert isinstance(result, ToolOk)
        assert "uid=" in result.output

    async def test_output_path(self, tmp_path: Path) -> None:
        if not hasattr(os, "getuid"):
            pytest.skip("getuid not available on this platform")
        out = tmp_path / "id.txt"
        result = await _run(Id, [], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output

# ---------------------------------------------------------------------------
# Ifconfig
# ---------------------------------------------------------------------------
class TestIfconfig:
    async def test_default(self) -> None:
        result = await _run(Ifconfig, [])
        assert isinstance(result, ToolOk)
        assert len(result.output) > 0

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "ifconfig.txt"
        result = await _run(Ifconfig, [], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------
class TestInstall:
    async def test_copy_file(self, tmp_path: Path) -> None:
        src = tmp_path / "a.txt"
        dst = tmp_path / "b.txt"
        src.write_text("hello")
        result = await _run(Install, [str(src), str(dst)])
        assert isinstance(result, ToolOk)
        assert dst.read_text() == "hello"

    async def test_copy_to_directory(self, tmp_path: Path) -> None:
        src = tmp_path / "a.txt"
        d = tmp_path / "dest"
        d.mkdir()
        src.write_text("hello")
        result = await _run(Install, [str(src), str(d)])
        assert isinstance(result, ToolOk)
        assert (d / "a.txt").read_text() == "hello"

    async def test_missing_operand(self) -> None:
        result = await _run(Install, [])
        assert isinstance(result, ToolError)
        assert "missing" in result.message.lower()

# ---------------------------------------------------------------------------
# Iostat
# ---------------------------------------------------------------------------
class TestIostat:
    async def test_default(self) -> None:
        result = await _run(Iostat, [])
        assert isinstance(result, ToolOk)
        assert "avg-cpu" in result.output or "Device" in result.output

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "iostat.txt"
        result = await _run(Iostat, [], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output

# ---------------------------------------------------------------------------
# Kill
# ---------------------------------------------------------------------------
class TestKill:
    async def test_invalid_pid(self) -> None:
        result = await _run(Kill, ["999999"])
        assert isinstance(result, (ToolOk, ToolError))

    async def test_missing_operand(self) -> None:
        result = await _run(Kill, [])
        assert isinstance(result, ToolOk)

# ---------------------------------------------------------------------------
# Killall
# ---------------------------------------------------------------------------
class TestKillall:
    async def test_missing_operand(self) -> None:
        result = await _run(Killall, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

    async def test_nonexistent_process(self) -> None:
        result = await _run(Killall, ["nonexistent_process_12345"])
        assert isinstance(result, (ToolOk, ToolError))

# ---------------------------------------------------------------------------
# LsbRelease
# ---------------------------------------------------------------------------
class TestLsbRelease:
    async def test_default(self) -> None:
        result = await _run(LsbRelease, [])
        assert isinstance(result, ToolOk)
        assert "Distributor ID" in result.output

    async def test_id_only(self) -> None:
        result = await _run(LsbRelease, ["-i"])
        assert isinstance(result, ToolOk)

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "lsb.txt"
        result = await _run(LsbRelease, [], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output

# ---------------------------------------------------------------------------
# Lsof
# ---------------------------------------------------------------------------
class TestLsof:
    async def test_default(self) -> None:
        result = await _run(Lsof, [])
        assert isinstance(result, ToolOk)

    async def test_pid(self) -> None:
        result = await _run(Lsof, [str(os.getpid())])
        assert isinstance(result, (ToolOk, ToolError))

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "lsof.txt"
        result = await _run(Lsof, [], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output

# ---------------------------------------------------------------------------
# Man
# ---------------------------------------------------------------------------
class TestMan:
    async def test_list_all(self) -> None:
        result = await _run(Man, [])
        assert isinstance(result, ToolOk)
        # Output may be exported if too large; check for bash commands reference
        assert "AVAILABLE BASH COMMANDS" in result.output or "saved to file" in result.output or "exported to file" in result.output

    async def test_specific_command(self) -> None:
        result = await _run(Man, ["cat"])
        assert isinstance(result, ToolOk)
        assert "Cat" in result.output

    async def test_unknown_command(self) -> None:
        result = await _run(Man, ["nonexistent_xyz"])
        assert isinstance(result, ToolOk)

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "man.txt"
        result = await _run(Man, ["cat"], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output

# ---------------------------------------------------------------------------
# Md5sum
# ---------------------------------------------------------------------------
class TestMd5sum:
    async def test_single_file(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_bytes(b"hello")
        result = await _run(Md5sum, [str(f)])
        assert isinstance(result, ToolOk)
        import hashlib
        expected = hashlib.md5(b"hello").hexdigest()
        assert expected in result.output

    async def test_multiple_files(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_bytes(b"a")
        f2.write_bytes(b"b")
        result = await _run(Md5sum, [str(f1), str(f2)])
        assert isinstance(result, ToolOk)
        assert "\n" in result.output.strip()

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Md5sum, [str(tmp_path / "missing.txt")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(Md5sum, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

# ---------------------------------------------------------------------------
# Mkfifo
# ---------------------------------------------------------------------------
class TestMkfifo:
    async def test_create_fifo(self, tmp_path: Path) -> None:
        p = tmp_path / "myfifo"
        result = await _run(Mkfifo, [str(p)])
        if platform.system() == "Windows":
            assert isinstance(result, ToolError)
            assert "not supported" in result.output
        else:
            assert isinstance(result, ToolOk)
            assert p.exists()

    async def test_missing_operand(self) -> None:
        result = await _run(Mkfifo, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

# ---------------------------------------------------------------------------
# Nl
# ---------------------------------------------------------------------------
class TestNl:
    async def test_basic(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a\nb\n", encoding="utf-8")
        result = await _run(Nl, [str(f)])
        assert isinstance(result, ToolOk)
        assert "1" in result.output
        assert "2" in result.output

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Nl, [str(tmp_path / "missing.txt")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(Nl, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

# ---------------------------------------------------------------------------
# Nslookup
# ---------------------------------------------------------------------------
class TestNslookup:
    async def test_localhost(self) -> None:
        result = await _run(Nslookup, ["localhost"])
        assert isinstance(result, ToolOk)
        assert "Name:" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(Nslookup, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "nslookup.txt"
        result = await _run(Nslookup, ["localhost"], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output

# ---------------------------------------------------------------------------
# Od
# ---------------------------------------------------------------------------
class TestOd:
    async def test_octal(self, tmp_path: Path) -> None:
        f = tmp_path / "a.bin"
        f.write_bytes(b"\x00\x01\x02")
        result = await _run(Od, [str(f)])
        assert isinstance(result, ToolOk)
        assert "0000000" in result.output

    async def test_hex(self, tmp_path: Path) -> None:
        f = tmp_path / "a.bin"
        f.write_bytes(b"\x00\x01\x02")
        result = await _run(Od, ["-x", str(f)])
        assert isinstance(result, ToolOk)

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Od, [str(tmp_path / "missing")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.message

    async def test_missing_operand(self) -> None:
        result = await _run(Od, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

# ---------------------------------------------------------------------------
# Ping
# ---------------------------------------------------------------------------
class TestPing:
    async def test_localhost(self) -> None:
        result = await _run(Ping, ["-c", "1", "127.0.0.1"])
        assert isinstance(result, ToolOk)
        assert "127.0.0.1" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(Ping, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "ping.txt"
        result = await _run(Ping, ["-c", "1", "127.0.0.1"], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output

# ---------------------------------------------------------------------------
# Printf
# ---------------------------------------------------------------------------
class TestPrintf:
    async def test_string(self) -> None:
        result = await _run(Printf, ["%s", "hello"])
        assert isinstance(result, ToolOk)
        assert "hello" in result.output

    async def test_integer(self) -> None:
        result = await _run(Printf, ["%d", "42"])
        assert isinstance(result, ToolOk)
        assert "42" in result.output

    async def test_hex(self) -> None:
        result = await _run(Printf, ["%x", "255"])
        assert isinstance(result, ToolOk)
        assert "ff" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(Printf, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

# ---------------------------------------------------------------------------
# Readlink
# ---------------------------------------------------------------------------
class TestReadlink:
    async def test_symlink(self, tmp_path: Path) -> None:
        target = tmp_path / "target.txt"
        target.write_text("t")
        link = tmp_path / "link.txt"
        link.symlink_to(target)
        result = await _run(Readlink, [str(link)])
        assert isinstance(result, ToolOk)
        assert "target.txt" in result.output

    async def test_canonicalize(self, tmp_path: Path) -> None:
        target = tmp_path / "target.txt"
        target.write_text("t")
        link = tmp_path / "link.txt"
        link.symlink_to(target)
        result = await _run(Readlink, ["-f", str(link)])
        assert isinstance(result, ToolOk)
        assert "target.txt" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(Readlink, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

# ---------------------------------------------------------------------------
# Renice
# ---------------------------------------------------------------------------
class TestRenice:
    async def test_invalid_pid(self) -> None:
        result = await _run(Renice, ["-n", "10", "999999"])
        assert isinstance(result, ToolError)

    async def test_missing_operand(self) -> None:
        result = await _run(Renice, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

# ---------------------------------------------------------------------------
# Rev
# ---------------------------------------------------------------------------
class TestRev:
    async def test_basic(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello\nworld\n", encoding="utf-8")
        result = await _run(Rev, [str(f)])
        assert isinstance(result, ToolOk)
        assert "olleh" in result.output
        assert "dlrow" in result.output

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Rev, [str(tmp_path / "missing.txt")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(Rev, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

# ---------------------------------------------------------------------------
# Scriptreplay
# ---------------------------------------------------------------------------
class TestScriptreplay:
    async def test_basic(self, tmp_path: Path) -> None:
        timing = tmp_path / "timing.txt"
        script = tmp_path / "script.txt"
        timing.write_text("0.1 5\n", encoding="utf-8")
        script.write_text("hello", encoding="utf-8")
        result = await _run(Scriptreplay, [str(timing), str(script)])
        assert isinstance(result, ToolOk)
        assert "hello" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(Scriptreplay, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Scriptreplay, ["timing.txt", "script.txt"])
        assert isinstance(result, ToolError)
        assert "No such file" in result.message

# ---------------------------------------------------------------------------
# Seq
# ---------------------------------------------------------------------------
class TestSeq:
    async def test_one_arg(self) -> None:
        result = await _run(Seq, ["5"])
        assert isinstance(result, ToolOk)
        lines = result.output.strip().split("\n")
        assert lines == ["1", "2", "3", "4", "5"]

    async def test_two_args(self) -> None:
        result = await _run(Seq, ["2", "5"])
        assert isinstance(result, ToolOk)
        lines = result.output.strip().split("\n")
        assert lines == ["2", "3", "4", "5"]

    async def test_three_args(self) -> None:
        result = await _run(Seq, ["1", "2", "7"])
        assert isinstance(result, ToolOk)
        lines = result.output.strip().split("\n")
        assert lines == ["1", "3", "5", "7"]

    async def test_missing_operand(self) -> None:
        result = await _run(Seq, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

# ---------------------------------------------------------------------------
# Sha256sum
# ---------------------------------------------------------------------------
class TestSha256sum:
    async def test_single_file(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_bytes(b"hello")
        result = await _run(Sha256sum, [str(f)])
        assert isinstance(result, ToolOk)
        import hashlib
        expected = hashlib.sha256(b"hello").hexdigest()
        assert expected in result.output

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Sha256sum, [str(tmp_path / "missing.txt")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(Sha256sum, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

# ---------------------------------------------------------------------------
# Shuf
# ---------------------------------------------------------------------------
class TestShuf:
    async def test_basic(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a\nb\nc\n", encoding="utf-8")
        result = await _run(Shuf, [str(f)])
        assert isinstance(result, ToolOk)
        lines = result.output.strip().split("\n")
        assert sorted(lines) == ["a", "b", "c"]

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Shuf, [str(tmp_path / "missing.txt")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(Shuf, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

# ---------------------------------------------------------------------------
# Sleep
# ---------------------------------------------------------------------------
class TestSleep:
    async def test_seconds(self) -> None:
        import time
        start = time.time()
        result = await _run(Sleep, ["0.1"])
        elapsed = time.time() - start
        assert isinstance(result, ToolOk)
        assert elapsed >= 0.08

    async def test_missing_operand(self) -> None:
        result = await _run(Sleep, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

# ---------------------------------------------------------------------------
# Split
# ---------------------------------------------------------------------------
class TestSplit:
    async def test_basic(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("line1\nline2\n", encoding="utf-8")
        result = await _run(Split, [str(f)], cwd=str(tmp_path))
        assert isinstance(result, ToolOk)
        assert "split into" in result.output
        assert (tmp_path / "xaa").exists()

    async def test_lines(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a\nb\nc\nd\n", encoding="utf-8")
        result = await _run(Split, ["-l", "2", str(f)], cwd=str(tmp_path))
        assert isinstance(result, ToolOk)

    async def test_prefix(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a\n", encoding="utf-8")
        result = await _run(Split, [str(f), "out"], cwd=str(tmp_path))
        assert isinstance(result, ToolOk)
        assert (tmp_path / "outaa").exists()

    async def test_missing_operand(self) -> None:
        result = await _run(Split, [])
        assert isinstance(result, ToolError)
        assert "missing" in result.message.lower()

# ---------------------------------------------------------------------------
# Ss
# ---------------------------------------------------------------------------
class TestSs:
    async def test_default(self) -> None:
        result = await _run(Ss, [])
        assert isinstance(result, ToolOk)
        assert len(result.output) > 0

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "ss.txt"
        result = await _run(Ss, [], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output

# ---------------------------------------------------------------------------
# Strings
# ---------------------------------------------------------------------------
class TestStrings:
    async def test_basic(self, tmp_path: Path) -> None:
        f = tmp_path / "a.bin"
        f.write_bytes(b"hello\x00world\x00")
        result = await _run(Strings, [str(f)])
        assert isinstance(result, ToolOk)
        assert "hello" in result.output
        assert "world" in result.output

    async def test_min_length(self, tmp_path: Path) -> None:
        f = tmp_path / "a.bin"
        f.write_bytes(b"hi\x00hello\x00")
        result = await _run(Strings, ["-n", "5", str(f)])
        assert isinstance(result, ToolOk)
        assert "hello" in result.output
        assert "hi" not in result.output

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Strings, [str(tmp_path / "missing")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(Strings, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

# ---------------------------------------------------------------------------
# SwVers
# ---------------------------------------------------------------------------
class TestSwVers:
    async def test_default(self) -> None:
        result = await _run(SwVers, [])
        assert isinstance(result, ToolOk)
        assert "ProductName" in result.output

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "sw_vers.txt"
        result = await _run(SwVers, [], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output

# ---------------------------------------------------------------------------
# Systeminfo
# ---------------------------------------------------------------------------
class TestSysteminfo:
    async def test_default(self) -> None:
        result = await _run(Systeminfo, [])
        assert isinstance(result, ToolOk)
        assert "Host Name" in result.output

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "systeminfo.txt"
        result = await _run(Systeminfo, [], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output

# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------
class TestTest:
    async def test_exists_true(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello")
        result = await _run(Test, ["-e", str(f)])
        assert isinstance(result, ToolOk)

    async def test_exists_false(self, tmp_path: Path) -> None:
        result = await _run(Test, ["-e", str(tmp_path / "missing.txt")])
        assert isinstance(result, ToolError)

    async def test_is_file(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello")
        result = await _run(Test, ["-f", str(f)])
        assert isinstance(result, ToolOk)

    async def test_is_directory(self, tmp_path: Path) -> None:
        d = tmp_path / "adir"
        d.mkdir()
        result = await _run(Test, ["-d", str(d)])
        assert isinstance(result, ToolOk)

    async def test_string_equal(self) -> None:
        result = await _run(Test, ["abc", "=", "abc"])
        assert isinstance(result, ToolOk)

    async def test_string_not_equal(self) -> None:
        result = await _run(Test, ["abc", "!=", "def"])
        assert isinstance(result, ToolOk)

    async def test_numeric_less(self) -> None:
        result = await _run(Test, ["2", "-lt", "5"])
        assert isinstance(result, ToolOk)

    async def test_numeric_greater(self) -> None:
        result = await _run(Test, ["5", "-gt", "2"])
        assert isinstance(result, ToolOk)

    async def test_empty_no_args(self) -> None:
        result = await _run(Test, [])
        assert isinstance(result, ToolOk)

# ---------------------------------------------------------------------------
# Top
# ---------------------------------------------------------------------------
class TestTop:
    async def test_default(self) -> None:
        result = await _run(Top, [])
        assert isinstance(result, ToolOk)
        assert "PID" in result.output or "COMMAND" in result.output

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "top.txt"
        result = await _run(Top, [], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output

# ---------------------------------------------------------------------------
# Traceroute
# ---------------------------------------------------------------------------
class TestTraceroute:
    async def test_localhost(self) -> None:
        result = await _run(Traceroute, ["127.0.0.1"])
        assert isinstance(result, ToolOk)
        assert "127.0.0.1" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(Traceroute, [])
        assert isinstance(result, ToolError)
        assert "missing host" in result.message.lower()

# ---------------------------------------------------------------------------
# Trap
# ---------------------------------------------------------------------------
class TestTrap:
    async def test_list_empty(self) -> None:
        result = await _run(Trap, [])
        assert isinstance(result, ToolOk)

    async def test_set_trap(self) -> None:
        import signal
        result = await _run(Trap, ["echo caught", "SIGINT"])
        assert isinstance(result, ToolOk)

    async def test_remove_trap(self) -> None:
        import signal
        result = await _run(Trap, ["-", "SIGINT"])
        assert isinstance(result, ToolOk)

    async def test_invalid_signal(self) -> None:
        result = await _run(Trap, ["echo", "INVALIDSIG"])
        assert isinstance(result, ToolError)
        assert "invalid signal" in result.message

    async def test_missing_signal(self) -> None:
        result = await _run(Trap, ["echo"])
        assert isinstance(result, ToolError)
        assert "missing signal" in result.message.lower()

# ---------------------------------------------------------------------------
# TrueCmd
# ---------------------------------------------------------------------------
class TestTrueCmd:
    async def test_returns_ok(self) -> None:
        result = await _run(TrueCmd, [])
        assert isinstance(result, ToolOk)

# ---------------------------------------------------------------------------
# Ulimit
# ---------------------------------------------------------------------------
class TestUlimit:
    async def test_default(self) -> None:
        result = await _run(Ulimit, [])
        assert isinstance(result, (ToolOk, ToolError))

    async def test_all(self) -> None:
        result = await _run(Ulimit, ["-a"])
        assert isinstance(result, (ToolOk, ToolError))

    async def test_not_supported(self) -> None:
        if platform.system() != "Windows":
            pytest.skip("Windows-only test")
        result = await _run(Ulimit, [])
        assert isinstance(result, ToolError)
        assert "not supported" in result.message

# ---------------------------------------------------------------------------
# Umask
# ---------------------------------------------------------------------------
class TestUmask:
    async def test_get(self) -> None:
        result = await _run(Umask, [])
        assert isinstance(result, ToolOk)
        assert result.output.isdigit() or len(result.output) == 4

    async def test_set(self) -> None:
        result = await _run(Umask, ["022"])
        assert isinstance(result, ToolOk)

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "umask.txt"
        result = await _run(Umask, [], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output

# ---------------------------------------------------------------------------
# Uname
# ---------------------------------------------------------------------------
class TestUname:
    async def test_default(self) -> None:
        result = await _run(Uname, [])
        assert isinstance(result, ToolOk)
        assert len(result.output) > 0

    async def test_kernel(self) -> None:
        result = await _run(Uname, ["-s"])
        assert isinstance(result, ToolOk)
        assert len(result.output) > 0

    async def test_all(self) -> None:
        result = await _run(Uname, ["-a"])
        assert isinstance(result, ToolOk)
        assert len(result.output) > 0

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "uname.txt"
        result = await _run(Uname, [], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output

# ---------------------------------------------------------------------------
# Unexpand
# ---------------------------------------------------------------------------
class TestUnexpand:
    async def test_basic(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a        b\n", encoding="utf-8")
        result = await _run(Unexpand, [str(f)])
        assert isinstance(result, ToolOk)
        assert "a" in result.output
        assert "b" in result.output

    async def test_tabstop(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a    b\n", encoding="utf-8")
        result = await _run(Unexpand, ["-t", "4", str(f)])
        assert isinstance(result, ToolOk)

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Unexpand, [str(tmp_path / "missing.txt")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.output

    async def test_missing_operand(self) -> None:
        result = await _run(Unexpand, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

# ---------------------------------------------------------------------------
# Uptime
# ---------------------------------------------------------------------------
class TestUptime:
    async def test_default(self) -> None:
        result = await _run(Uptime, [])
        assert isinstance(result, ToolOk)
        assert "up" in result.output

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "uptime.txt"
        result = await _run(Uptime, [], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output

# ---------------------------------------------------------------------------
# Vmstat
# ---------------------------------------------------------------------------
class TestVmstat:
    async def test_default(self) -> None:
        result = await _run(Vmstat, [])
        assert isinstance(result, ToolOk)
        assert "memory" in result.output.lower() or "free" in result.output

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "vmstat.txt"
        result = await _run(Vmstat, [], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output

# ---------------------------------------------------------------------------
# Wget
# ---------------------------------------------------------------------------
class TestWget:
    async def test_missing_url(self) -> None:
        result = await _run(Wget, [])
        assert isinstance(result, ToolError)
        assert "missing URL" in result.message

    async def test_output_file(self, tmp_path: Path) -> None:
        out = tmp_path / "wget.html"
        result = await _run(Wget, ["https://httpbin.org/get", "-O", str(out)])
        assert isinstance(result, (ToolOk, ToolError))

# ---------------------------------------------------------------------------
# Who
# ---------------------------------------------------------------------------
class TestWho:
    async def test_default(self) -> None:
        result = await _run(Who, [])
        assert isinstance(result, ToolOk)
        assert len(result.output) > 0

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "who.txt"
        result = await _run(Who, [], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output

# ---------------------------------------------------------------------------
# Whoami
# ---------------------------------------------------------------------------
class TestWhoami:
    async def test_default(self) -> None:
        result = await _run(Whoami, [])
        assert isinstance(result, ToolOk)
        assert len(result.output) > 0

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "whoami.txt"
        result = await _run(Whoami, [], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output

# ---------------------------------------------------------------------------
# Xxd
# ---------------------------------------------------------------------------
class TestXxd:
    async def test_basic(self, tmp_path: Path) -> None:
        f = tmp_path / "a.bin"
        f.write_bytes(b"hello")
        result = await _run(Xxd, [str(f)])
        assert isinstance(result, ToolOk)
        assert "00000000" in result.output

    async def test_reverse(self, tmp_path: Path) -> None:
        f = tmp_path / "hex.txt"
        f.write_text("68656c6c6f", encoding="utf-8")
        result = await _run(Xxd, ["-r", str(f)])
        assert isinstance(result, ToolOk)
        assert "hello" in result.output

    async def test_missing_file(self, tmp_path: Path) -> None:
        result = await _run(Xxd, [str(tmp_path / "missing")])
        assert isinstance(result, ToolError)
        assert "No such file" in result.message

    async def test_missing_operand(self) -> None:
        result = await _run(Xxd, [])
        assert isinstance(result, ToolError)
        assert "missing operand" in result.message.lower()

# ---------------------------------------------------------------------------
# Yes
# ---------------------------------------------------------------------------
class TestYes:
    async def test_default(self) -> None:
        result = await _run(Yes, [])
        assert isinstance(result, ToolOk)
        lines = result.output.strip().split("\n")
        assert all(line == "y" for line in lines)
        assert len(lines) == 1000

    async def test_custom_text(self) -> None:
        result = await _run(Yes, ["hello", "world"])
        assert isinstance(result, ToolOk)
        # Output may be exported if very large
        if "saved to file" in result.output or "exported to file" in result.output:
            pass
        else:
            lines = result.output.strip().split("\n")
            assert all(line == "hello world" for line in lines)

    async def test_output_path(self, tmp_path: Path) -> None:
        out = tmp_path / "yes.txt"
        result = await _run(Yes, ["ok"], output_path=str(out))
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output
