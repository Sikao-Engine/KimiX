"""Tests for _is_protected_path logic in bash commands."""
import os
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

from kimi_agent_sdk import ToolOk, ToolError
from kimix.tools.file.bash.params import _is_protected_path
import kimix.tools.file.bash.params as _params_module
from kimix.tools.file.bash import (
    # Write commands
    Rm,
    Cp,
    Mv,
    Touch,
    Mkdir,
    Rmdir,
    Chmod,
    Chown,
    Chgrp,
    Ln,
    Mkfifo,
    Install,
    Mktemp,
    Gzip,
    Gunzip,
    Bzip2,
    Bunzip2,
    Xz,
    Unxz,
    Zip,
    Unzip,
    Tar,
    Crontab,
    # Read commands with file paths
    Cat,
    Ls,
    Grep,
    Head,
    Tail,
    Awk,
    Cut,
    Diff,
    Cksum,
    Md5sum,
    Sha256sum,
    Find,
    File,
    Du,
    Stat,
    Strings,
    Hexdump,
    Od,
    Xxd,
    Wc,
    Sed,
    Tr,
    Basename,
    Dirname,
    Readlink,
    Realpath,
    Expand,
    Unexpand,
    Fold,
    Fmt,
    Nl,
    Rev,
    Tac,
    Shuf,
    Split,
    Csplit,
    Cmp,
    Comm,
    # For output_path tests
    Echo,
    Printf,
)

FAKE_PROTECTED = frozenset({"/fake_protected"})
FAKE_PROTECTED_PATH = "/fake_protected/test_file_12345"


async def _run(tool_cls, args, cwd=None, output_path=None):
    tool = tool_cls()
    params = tool_cls.params(path="", args=args, cwd=cwd, output_path=output_path)
    with patch(
        "kimix.tools.common._maybe_export_output_async",
        new_callable=AsyncMock,
        side_effect=lambda x: x,
    ):
        return await tool(params)


@pytest.fixture
def fake_protected():
    with patch.object(_params_module, "_PROTECTED_PATHS", FAKE_PROTECTED):
        yield


class TestIsProtectedPathDirectly:
    def test_protected_directory(self, fake_protected, tmp_path):
        is_prot, reason = _is_protected_path("/fake_protected")
        assert is_prot is True
        assert "system-critical" in reason

    def test_file_inside_protected(self, fake_protected, tmp_path):
        is_prot, reason = _is_protected_path("/fake_protected/file.txt")
        assert is_prot is True
        assert "inside system-critical" in reason

    def test_non_protected_path(self, fake_protected, tmp_path):
        # On Windows every existing path is inside a system drive. Neutralise
        # the drive-existence check so the temp path is treated as safe.
        real_exists = os.path.exists

        def _mock_exists(p):
            if isinstance(p, str) and len(p) == 3 and p[1:] == ":\\":
                return False
            return real_exists(p)

        with patch.object(_params_module.os.path, "exists", _mock_exists):
            is_prot, reason = _is_protected_path(str(tmp_path / "file.txt"))
        assert is_prot is False
        assert reason == ""


class TestWriteCommandsBlocked:
    @pytest.mark.parametrize(
        "tool_cls,args",
        [
            (Rm, [FAKE_PROTECTED_PATH]),
            (Cp, [FAKE_PROTECTED_PATH, "safe_dst"]),
            (Mv, [FAKE_PROTECTED_PATH, "safe_dst"]),
            (Touch, [FAKE_PROTECTED_PATH]),
            (Mkdir, [FAKE_PROTECTED_PATH]),
            (Rmdir, [FAKE_PROTECTED_PATH]),
            (Chmod, ["755", FAKE_PROTECTED_PATH]),
            (Chown, ["user", FAKE_PROTECTED_PATH]),
            (Chgrp, ["group", FAKE_PROTECTED_PATH]),
            (Ln, ["-s", FAKE_PROTECTED_PATH, "safe_link"]),
            (Mkfifo, [FAKE_PROTECTED_PATH]),
            (Install, [FAKE_PROTECTED_PATH, "safe_dst"]),
            (Mktemp, ["-p", FAKE_PROTECTED_PATH]),
            (Gzip, [FAKE_PROTECTED_PATH]),
            (Gunzip, [FAKE_PROTECTED_PATH]),
            (Bzip2, [FAKE_PROTECTED_PATH]),
            (Bunzip2, [FAKE_PROTECTED_PATH]),
            (Xz, [FAKE_PROTECTED_PATH]),
            (Unxz, [FAKE_PROTECTED_PATH]),
            (Zip, ["safe.zip", FAKE_PROTECTED_PATH]),
            (Unzip, [FAKE_PROTECTED_PATH]),
            (Tar, ["-cf", "safe.tar", FAKE_PROTECTED_PATH]),
            (Crontab, [FAKE_PROTECTED_PATH]),
        ],
    )
    async def test_write_command_blocked(self, fake_protected, tool_cls, args):
        result = await _run(tool_cls, args)
        assert isinstance(result, ToolError)
        assert result.brief == "protected path"


class TestReadCommandsNotBlocked:
    @pytest.mark.parametrize(
        "tool_cls,args",
        [
            (Cat, [FAKE_PROTECTED_PATH]),
            (Ls, [FAKE_PROTECTED_PATH]),
            (Grep, ["pattern", FAKE_PROTECTED_PATH]),
            (Head, [FAKE_PROTECTED_PATH]),
            (Tail, [FAKE_PROTECTED_PATH]),
            (Awk, ["{print}", FAKE_PROTECTED_PATH]),
            (Cut, ["-f1", FAKE_PROTECTED_PATH]),
            (Diff, [FAKE_PROTECTED_PATH, FAKE_PROTECTED_PATH]),
            (Cksum, [FAKE_PROTECTED_PATH]),
            (Md5sum, [FAKE_PROTECTED_PATH]),
            (Sha256sum, [FAKE_PROTECTED_PATH]),
            (Find, [FAKE_PROTECTED_PATH]),
            (File, [FAKE_PROTECTED_PATH]),
            (Du, [FAKE_PROTECTED_PATH]),
            (Stat, [FAKE_PROTECTED_PATH]),
            (Strings, [FAKE_PROTECTED_PATH]),
            (Hexdump, [FAKE_PROTECTED_PATH]),
            (Od, [FAKE_PROTECTED_PATH]),
            (Xxd, [FAKE_PROTECTED_PATH]),
            (Wc, [FAKE_PROTECTED_PATH]),
            (Sed, ["s/a/b/", FAKE_PROTECTED_PATH]),
            (Tr, ["a", "b", FAKE_PROTECTED_PATH]),
            (Basename, [FAKE_PROTECTED_PATH]),
            (Dirname, [FAKE_PROTECTED_PATH]),
            (Readlink, [FAKE_PROTECTED_PATH]),
            (Realpath, [FAKE_PROTECTED_PATH]),
            (Expand, [FAKE_PROTECTED_PATH]),
            (Unexpand, [FAKE_PROTECTED_PATH]),
            (Fold, [FAKE_PROTECTED_PATH]),
            (Fmt, [FAKE_PROTECTED_PATH]),
            (Nl, [FAKE_PROTECTED_PATH]),
            (Rev, [FAKE_PROTECTED_PATH]),
            (Tac, [FAKE_PROTECTED_PATH]),
            (Shuf, [FAKE_PROTECTED_PATH]),
            (Split, [FAKE_PROTECTED_PATH]),
            (Csplit, [FAKE_PROTECTED_PATH, "1"]),
            (Cmp, [FAKE_PROTECTED_PATH, FAKE_PROTECTED_PATH]),
            (Comm, [FAKE_PROTECTED_PATH, FAKE_PROTECTED_PATH]),
        ],
    )
    async def test_read_command_not_blocked(self, fake_protected, tool_cls, args):
        result = await _run(tool_cls, args)
        if isinstance(result, ToolError):
            assert result.brief != "protected path"


class TestOutputPathProtection:
    @pytest.mark.parametrize(
        "tool_cls,args",
        [
            (Rm, ["dummy"]),
            (Cp, ["dummy1", "dummy2"]),
            (Mv, ["dummy1", "dummy2"]),
            (Touch, ["dummy"]),
            (Mkdir, ["dummy"]),
            (Cat, ["dummy"]),
            (Ls, ["dummy"]),
            (Echo, ["hello"]),
            (Printf, ["hello"]),
            (Grep, ["pattern", "dummy"]),
            (Head, ["dummy"]),
            (Tail, ["dummy"]),
        ],
    )
    async def test_output_path_blocked(self, fake_protected, tool_cls, args):
        result = await _run(tool_cls, args, output_path=FAKE_PROTECTED_PATH)
        assert isinstance(result, ToolError)
        assert result.brief == "protected path"
