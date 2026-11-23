"""Regression tests for executing existing script files by path."""

from __future__ import annotations

from pathlib import Path

import pytest

from kimi_cli.session import Session
from kimi_cli.tools import SkipThisTool
from kimi_cli.tools.file.bash.bash_tool import Bash, BashParams, find_bash
from kimi_cli.tools.file.bash.pwsh_tool import Powershell, PowershellParams
from kimi_cli.tools.py import Params as PythonParams
from kimi_cli.tools.py import python
from kosong.tooling import ToolError, ToolOk


def _make_bash(session: Session) -> Bash:
    if find_bash() is None:
        pytest.skip("bash not found on this platform")
    try:
        return Bash(session)
    except SkipThisTool:
        pytest.skip("bash tool disabled on this platform")


def _make_pwsh(session: Session) -> Powershell:
    try:
        return Powershell(session)
    except SkipThisTool:
        pytest.skip("PowerShell not found on this platform")


@pytest.mark.parametrize(
    ("params_type", "field_name", "suffix"),
    [(BashParams, "cmd", ".sh"), (PowershellParams, "command", ".ps1")],
)
def test_shell_command_description_mentions_script_file_path(
    params_type: type[BashParams] | type[PowershellParams],
    field_name: str,
    suffix: str,
) -> None:
    description = params_type.model_fields[field_name].description

    assert description is not None
    assert "path" in description
    assert suffix in description


@pytest.mark.asyncio
async def test_bash_executes_sh_file_contents(session: Session, tmp_path: Path) -> None:
    script = tmp_path / "print marker.sh"
    script.write_text("printf 'bash-file-marker\\n'\n", encoding="utf-8")

    result = await _make_bash(session)(BashParams(command=str(script)))

    assert isinstance(result, ToolOk)
    assert "bash-file-marker" in result.output


@pytest.mark.asyncio
async def test_pwsh_executes_ps1_file_contents(session: Session, tmp_path: Path) -> None:
    script = tmp_path / "print marker.ps1"
    script.write_text("Write-Output 'pwsh-file-marker'\n", encoding="utf-8")

    result = await _make_pwsh(session)(PowershellParams(cmd=str(script)))

    assert isinstance(result, ToolOk)
    assert "pwsh-file-marker" in result.output


@pytest.mark.asyncio
async def test_python_executes_py_file_path(session: Session, tmp_path: Path) -> None:
    script = tmp_path / "print marker.py"
    script.write_text("print('python-file-marker')\n", encoding="utf-8")

    result = await python(session)(PythonParams(file=str(script)))

    assert isinstance(result, ToolOk)
    assert "python-file-marker" in result.output
    assert result.brief == "Python file executed"


@pytest.mark.asyncio
async def test_python_still_executes_inline_code(session: Session) -> None:
    result = await python(session)(PythonParams(code="print('python-inline-marker')"))

    assert isinstance(result, ToolOk)
    assert "python-inline-marker" in result.output
    assert result.brief == "Python code executed"


@pytest.mark.parametrize(
    ("tool_type", "suffix"),
    [(Bash, ".sh"), (Powershell, ".ps1")],
)
def test_shell_script_read_failure_returns_tool_error(
    tool_type: type[Bash] | type[Powershell],
    suffix: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / f"unreadable{suffix}"
    script.write_text("marker", encoding="utf-8")

    def _fail_read(self: Path, *args: object, **kwargs: object) -> str:
        raise OSError("simulated read failure")

    monkeypatch.setattr(Path, "read_text", _fail_read)

    result = tool_type._resolve_script_file(str(script))

    assert isinstance(result, ToolError)
    assert "Failed to read" in result.message
    assert "simulated read failure" in result.message
