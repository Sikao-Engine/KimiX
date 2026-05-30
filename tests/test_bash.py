"""Comprehensive tests for the Bash tool (bash_tool.py) which uses the system bash executable."""

import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kimi_agent_sdk import ToolError, ToolOk
from kimi_cli.session import Session

from kimi_cli.tools import SkipThisTool
from kimix.tools.file.bash import (
    Bash,
    BashParams,
)
from kimix.tools.file.bash.bash_tool import find_bash
from kimix.tools.background.utils import _pop_task_data


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_session() -> MagicMock:
    session = MagicMock(spec=Session)
    session.custom_data = {}
    return session


@pytest.fixture(autouse=True)
def cleanup_task_data(mock_session: MagicMock) -> Any:
    yield
    _pop_task_data(mock_session)


# ============================================================================
# find_bash
# ============================================================================

class TestFindBash:
    def test_returns_path_on_this_system(self) -> None:
        path = find_bash()
        assert path is not None
        assert Path(path).exists()

    def test_returns_basename_bash(self) -> None:
        path = find_bash()
        assert path is not None
        assert Path(path).name.lower() in ("bash.exe", "bash")


# ============================================================================
# BashParams
# ============================================================================

class TestBashParams:
    def test_defaults(self) -> None:
        p = BashParams(cmd="ls")
        assert p.cmd == "ls"
        assert p.timeout == 10
        assert p.output_path is None
        assert p.cwd is None

    def test_full(self) -> None:
        p = BashParams(cmd="cat -n file.txt", timeout=30, output_path="/tmp/out", cwd="/home")
        assert p.cmd == "cat -n file.txt"
        assert p.timeout == 30
        assert p.output_path == "/tmp/out"
        assert p.cwd == "/home"

    def test_timeout_min(self) -> None:
        with pytest.raises(Exception):
            BashParams(cmd="ls", timeout=1)

    def test_timeout_max(self) -> None:
        with pytest.raises(Exception):
            BashParams(cmd="ls", timeout=200)


# ============================================================================
# Bash.__call__
# ============================================================================

class TestBashCall:
    async def test_echo_hello(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo hello")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "hello" in result.output

    async def test_true_command(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="true")
        result = await bash(params)
        assert isinstance(result, ToolOk)

    async def test_false_command(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="false")
        result = await bash(params)
        assert isinstance(result, ToolError)

    async def test_unknown_command_error(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="no_such_command_12345", timeout=5)
        result = await bash(params)
        assert isinstance(result, ToolError)
        assert "command not found" in result.output or "not found" in result.output.lower()

    async def test_ls_current_dir(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="ls .", timeout=10)
        result = await bash(params)
        assert isinstance(result, ToolOk)

    async def test_echo_with_multiple_args(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo hello world")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "hello world" in result.output

    async def test_echo_with_timeout(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo quick", timeout=30)
        result = await bash(params)
        assert isinstance(result, ToolOk)

    async def test_cat_file(self, mock_session: MagicMock, tmp_path: Path) -> None:
        f = tmp_path / "test.txt"
        f.write_text("hello cat", encoding="utf-8")
        bash = Bash(session=mock_session)
        # Use forward slashes so bash does not interpret backslashes as escapes
        posix_path = str(f).replace("\\", "/")
        params = BashParams(cmd=f"cat {posix_path}")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "hello cat" in result.output

    async def test_pwd(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="pwd")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert len(result.output) > 0

    async def test_whoami(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="whoami")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert len(result.output) > 0

    async def test_empty_command(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="", timeout=5)
        result = await bash(params)
        assert isinstance(result, ToolError)
        assert "Empty command" in result.output

    async def test_timeout(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="sleep 5", timeout=3)
        result = await bash(params)
        assert isinstance(result, ToolError)
        assert "Timeout" in result.brief

    async def test_with_output_path(self, mock_session: MagicMock, tmp_path: Path) -> None:
        f = tmp_path / "src.txt"
        out = tmp_path / "dst.txt"
        f.write_text("output_path_test", encoding="utf-8")
        bash = Bash(session=mock_session)
        # Use forward slashes so bash does not interpret backslashes as escapes
        posix_path = str(f).replace("\\", "/")
        params = BashParams(cmd=f"cat {posix_path}", output_path=str(out))
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "saved to file" in result.output
        assert "output_path_test" in out.read_text(encoding="utf-8")

    async def test_with_cwd(self, mock_session: MagicMock, tmp_path: Path) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="pwd", cwd=str(tmp_path))
        result = await bash(params)
        assert isinstance(result, ToolOk)
        # Git bash on Windows translates Windows paths to Unix-style paths (e.g. /c/... or /tmp/...)
        assert tmp_path.name in result.output or str(tmp_path).replace("\\", "/") in result.output

    async def test_bash_not_found_fallback(self, mock_session: MagicMock) -> None:
        """When bash is not found, Bash.__init__ raises SkipThisTool."""
        with patch("kimix.tools.file.bash.bash_tool.find_bash", return_value=None):
            with pytest.raises(SkipThisTool):
                Bash(session=mock_session)


# ============================================================================
# Edge cases
# ============================================================================

class TestEdgeCases:
    async def test_command_with_special_chars(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo 'hello\tworld'")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        # Tab may be preserved or converted by echo depending on bash version
        assert "hello" in result.output
        assert "world" in result.output

    async def test_command_with_quotes(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd='echo "quoted text"')
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "quoted text" in result.output


# ============================================================================
# Complex bash commands — pipes, redirects, substitution, etc.
# ============================================================================

class TestComplexCommands:
    """Tests for complex bash commands: pipes, redirects, substitution, conditionals, etc."""

    # -- pipes ---------------------------------------------------------------

    async def test_pipe_echo_to_wc(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo hello | wc -l")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "1" in result.output

    async def test_pipe_echo_to_grep(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo -e 'apple\\nbanana\\ncherry' | grep ana")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "banana" in result.output

    async def test_pipe_ls_to_head(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="ls / | head -1")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert len(result.output) > 0

    async def test_multiple_pipes(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo hello | tr 'a-z' 'A-Z' | tr 'A-Z' 'a-z'")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "hello" in result.output

    # -- redirects -----------------------------------------------------------

    async def test_redirect_stdout_to_file(
        self, mock_session: MagicMock, tmp_path: Path
    ) -> None:
        bash = Bash(session=mock_session)
        outfile = tmp_path / "redirected.txt"
        posix = str(outfile).replace("\\", "/")
        params = BashParams(cmd=f"echo redirected_content > {posix}")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert outfile.read_text(encoding="utf-8").strip() == "redirected_content"

    async def test_redirect_append(
        self, mock_session: MagicMock, tmp_path: Path
    ) -> None:
        bash = Bash(session=mock_session)
        outfile = tmp_path / "append.txt"
        posix = str(outfile).replace("\\", "/")
        await bash(BashParams(cmd=f"echo line1 > {posix}"))
        await bash(BashParams(cmd=f"echo line2 >> {posix}"))
        result = await bash(BashParams(cmd=f"cat {posix}"))
        assert isinstance(result, ToolOk)
        lines = result.output.strip().splitlines()
        assert "line1" in lines[0]
        assert "line2" in lines[-1]

    async def test_stderr_redirect(
        self, mock_session: MagicMock, tmp_path: Path
    ) -> None:
        bash = Bash(session=mock_session)
        outfile = tmp_path / "stderr.txt"
        posix = str(outfile).replace("\\", "/")
        # Redirect stderr to file; command fails so we expect ToolError
        params = BashParams(cmd=f"ls nonexisistent 2> {posix}")
        await bash(params)
        content = outfile.read_text(encoding="utf-8").lower()
        assert "nonexisistent" in content or "cannot access" in content or "no such" in content

    # -- command substitution ------------------------------------------------

    async def test_command_substitution(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo $(echo nested)")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "nested" in result.output

    async def test_backtick_substitution(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo `echo backtick`")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "backtick" in result.output

    # -- environment variables -----------------------------------------------

    async def test_env_var_home(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo $HOME")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert len(result.output.strip()) > 0

    async def test_env_var_user(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo $USER")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        # USER may be empty on some systems; just check no error

    # -- semicolon-separated commands ----------------------------------------

    async def test_semicolon_chain(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo first; echo second")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "first" in result.output
        assert "second" in result.output

    async def test_and_or_operators(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="true && echo yes || echo no")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "yes" in result.output

    async def test_and_or_false_branch(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="false && echo yes || echo no")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "no" in result.output

    # -- conditionals --------------------------------------------------------

    async def test_if_statement(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="if true; then echo TRUE; else echo FALSE; fi")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "TRUE" in result.output

    async def test_test_bracket(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="[ 1 -eq 1 ] && echo equal")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "equal" in result.output

    # -- exit codes ----------------------------------------------------------

    async def test_exit_code_success_check(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="true; echo $?")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "0" in result.output

    async def test_exit_code_failure_check(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        # The `echo $?` succeeds (exit 0) so overall ToolOk
        params = BashParams(cmd="false; echo $?")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "1" in result.output

    # -- here-strings / here-docs --------------------------------------------

    async def test_here_string(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="cat <<< 'herestring'")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "herestring" in result.output

    async def test_here_doc(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="cat <<EOF\nheredoc_line\nEOF")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "heredoc_line" in result.output

    # -- globbing ------------------------------------------------------------

    async def test_glob_expansion(self, mock_session: MagicMock, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")
        bash = Bash(session=mock_session)
        posix = str(tmp_path).replace("\\", "/")
        params = BashParams(cmd=f"cd {posix} && ls *.txt", cwd=str(tmp_path))
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "a.txt" in result.output
        assert "b.txt" in result.output

    # -- arithmetic expansion ------------------------------------------------

    async def test_arithmetic_expansion(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo $((3 + 4))")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "7" in result.output

    # -- brace expansion -----------------------------------------------------

    async def test_brace_expansion(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo {a,b,c}")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "a b c" in result.output

    # -- sub-shell -----------------------------------------------------------

    async def test_subshell(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="(cd / && pwd)")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert result.output.strip() == "/"

    # -- process substitution -------------------------------------------------

    async def test_process_substitution_diff(
        self, mock_session: MagicMock, tmp_path: Path
    ) -> None:
        f1 = tmp_path / "f1.txt"
        f2 = tmp_path / "f2.txt"
        f1.write_text("same")
        f2.write_text("same")
        bash = Bash(session=mock_session)
        posix1 = str(f1).replace("\\", "/")
        posix2 = str(f2).replace("\\", "/")
        params = BashParams(cmd=f"diff <(cat {posix1}) <(cat {posix2})")
        result = await bash(params)
        # diff returns 0 (success) when files are identical
        assert isinstance(result, ToolOk)

    async def test_process_substitution_diff_differs(
        self, mock_session: MagicMock, tmp_path: Path
    ) -> None:
        f1 = tmp_path / "f1.txt"
        f2 = tmp_path / "f2.txt"
        f1.write_text("one")
        f2.write_text("two")
        bash = Bash(session=mock_session)
        posix1 = str(f1).replace("\\", "/")
        posix2 = str(f2).replace("\\", "/")
        params = BashParams(cmd=f"diff <(cat {posix1}) <(cat {posix2})")
        result = await bash(params)
        # diff returns 1 (ToolError) when files differ
        assert isinstance(result, ToolError)

    # -- inline env ----------------------------------------------------------

    async def test_inline_env_override(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="MYVAR=42 bash -c 'echo $MYVAR'")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "42" in result.output

    # -- negation ------------------------------------------------------------

    async def test_negation_bang(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="! false; echo $?")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "0" in result.output

    # -- loop ----------------------------------------------------------------

    async def test_for_loop(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="for i in 1 2 3; do echo $i; done")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "1" in result.output
        assert "2" in result.output
        assert "3" in result.output

    async def test_while_loop(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="i=0; while [ $i -lt 3 ]; do echo $i; i=$((i+1)); done")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "0" in result.output
        assert "1" in result.output
        assert "2" in result.output

    # -- temp file with mktemp -----------------------------------------------

    async def test_mktemp(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="mktemp")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "/tmp" in result.output or "/temp" in result.output.lower()

    # -- printf --------------------------------------------------------------

    async def test_printf(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="printf '%s %s' hello world")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "hello world" in result.output

    # -- array ---------------------------------------------------------------

    async def test_array(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="arr=(one two three); echo ${arr[1]}")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "two" in result.output

    # -- string manipulation -------------------------------------------------

    async def test_string_length(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="s=abcdef; echo ${#s}")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "6" in result.output

    async def test_string_substring(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="s=hello; echo ${s:1:3}")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "ell" in result.output

    # -- sed -----------------------------------------------------------------

    async def test_sed_substitution(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo foo | sed 's/foo/bar/'")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "bar" in result.output

    # -- awk -----------------------------------------------------------------

    async def test_awk_field(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo 'a b c' | awk '{print $2}'")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "b" in result.output

    # -- cut -----------------------------------------------------------------

    async def test_cut_delimiter(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo 'a:b:c' | cut -d: -f2")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "b" in result.output

    # -- sort / uniq ---------------------------------------------------------

    async def test_sort_uniq(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo -e 'c\\na\\nb\\na' | sort | uniq")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        lines = result.output.strip().splitlines()
        assert lines == ["a", "b", "c"]

    # -- head / tail ---------------------------------------------------------

    async def test_head_n(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="seq 10 | head -3")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        lines = result.output.strip().splitlines()
        assert len(lines) == 3

    async def test_tail_n(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="seq 10 | tail -3")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        lines = result.output.strip().splitlines()
        assert "8" in lines[0]
        assert "10" in lines[-1]

    # -- tee -----------------------------------------------------------------

    async def test_tee(
        self, mock_session: MagicMock, tmp_path: Path
    ) -> None:
        bash = Bash(session=mock_session)
        outfile = tmp_path / "tee_out.txt"
        posix = str(outfile).replace("\\", "/")
        params = BashParams(cmd=f"echo hello_tee | tee {posix}")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "hello_tee" in result.output
        assert outfile.read_text(encoding="utf-8").strip() == "hello_tee"

    # -- exit with explicit code ---------------------------------------------

    async def test_exit_explicit_code(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="exit 42")
        result = await bash(params)
        # bash -c "exit 42" exits with code 42 -> ToolError
        assert isinstance(result, ToolError)

    # -- chained pipes with special chars ------------------------------------

    async def test_pipe_with_dollar_signs(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo '$HOME' | cat")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        # Single quotes preserve literal $HOME
        assert "$HOME" in result.output

    # -- background process via & --------------------------------------------

    async def test_background_ampersand(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="sleep 1 & wait", timeout=10)
        result = await bash(params)
        assert isinstance(result, ToolOk)

    # -- dirname / basename --------------------------------------------------

    async def test_dirname_basename(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="dirname /usr/bin/bash && basename /usr/bin/bash")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "/usr/bin" in result.output
        assert "bash" in result.output

    # -- xargs ---------------------------------------------------------------

    async def test_xargs(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo 'a b c' | xargs -n1 echo")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "a" in result.output
        assert "b" in result.output
        assert "c" in result.output

    # -- trap ----------------------------------------------------------------

    async def test_trap_does_not_crash(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="trap 'echo trapped' EXIT; echo done")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "done" in result.output
        assert "trapped" in result.output
