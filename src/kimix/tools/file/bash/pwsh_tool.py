"""PowerShell tool that executes commands via the system PowerShell executable."""

import asyncio
import contextlib
import functools
import os
import re
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

import kimi_cli
from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from pydantic import BaseModel, Field, model_validator
from kimi_cli.session import Session
from kimi_cli.tools import SkipThisTool
from kimi_cli.tools.display import ShellDisplayBlock
from kimix.tools.file.bash import bash_tool as _bash_tool
from kimix.tools.file.bash.proccess_pwsh import pwsh_transform
from kimix.tools.common import (
    _build_session_output_block,
    _env_with_rg_bin_path,
    _extract_export_path,
    _maybe_export_output_async,
    _summarize_long_output_async,
    ProcessTask,
)

if TYPE_CHECKING:
    from kimi_agent_sdk import CallableTool2 as _CallableTool2

def _print_warning(message: str) -> None:
    """Print a yellow WARNING message to stderr."""
    yellow = "\033[33m"
    reset = "\033[0m"
    print(f"{yellow}WARNING: {message}{reset}", file=sys.stderr, flush=True)


def _pwsh_major_version(path: str) -> int | None:
    """Return the major version reported by a PowerShell executable, or None."""
    try:
        output = subprocess.check_output(
            [path, "-NoP", "-NonI", "-C", "$PSVersionTable.PSVersion.Major"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        ).strip()
        return int(output)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def _where_candidates(name: str) -> list[str]:
    """Return candidate paths reported by ``where.exe <name>``."""
    try:
        result = subprocess.run(
            ["where.exe", name],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


@functools.lru_cache(maxsize=1)
def find_pwsh() -> str | None:
    """Find PowerShell 7.x on the current platform.

    Resolution order:
      1. ``pwsh`` / ``pwsh.exe`` on PATH (via ``shutil.which``).
      2. ``where.exe pwsh.exe`` on Windows.
      3. Common fixed installation paths.

    Returns the absolute path to a PowerShell 7+ executable, or ``None`` if
    only Windows PowerShell 5.1 (or no PowerShell) is available.
    """
    candidates: list[str] = []

    if sys.platform == "win32":
        names = ["pwsh.exe", "pwsh"]
    else:
        names = ["pwsh"]

    # 1. PATH
    for name in names:
        resolved = shutil.which(name)
        if resolved:
            candidates.append(resolved)

    # 2. where.exe (Windows only)
    if sys.platform == "win32":
        for name in names:
            candidates.extend(_where_candidates(name))

    # 3. Fixed common install locations
    if sys.platform == "win32":
        candidates.extend(
            [
                r"C:\Program Files\PowerShell\7\pwsh.exe",
                r"C:\Program Files (x86)\PowerShell\7\pwsh.exe",
            ]
        )
    else:
        candidates.extend(
            [
                "/opt/microsoft/powershell/7/pwsh",
                "/usr/local/bin/pwsh",
                "/usr/bin/pwsh",
            ]
        )

    seen: set[str] = set()
    for candidate in candidates:
        candidate = shutil.which(candidate) or candidate
        if not os.path.exists(candidate):
            continue
        norm = os.path.normcase(os.path.abspath(candidate))
        if norm in seen:
            continue
        seen.add(norm)
        major = _pwsh_major_version(candidate)
        if major is not None and major >= 7:
            return candidate

    return None


class PowershellParams(BaseModel):
    """Parameters for the Powershell tool — execute a PowerShell command."""

    cmd: str = Field(default="", description="PowerShell command or input text for an existing session.")
    timeout: int = Field(
        default=10,
        ge=3,
        le=900,
        description="Timeout in seconds."
    )
    max_output_length: int = Field(
        default=65536,
        ge=0,
        description="Output length threshold. Exceeding it sends the output to an anonymous sub-agent for summarization. 0 disables."
    )
    interactive: bool = Field(
        default=False,
        description=(
            "Run PowerShell interactively. "
            "The process stays alive and accepts further input via task_id. "
            "Returns a task_id immediately; use TaskOutput to read output."
        ),
    )
    task_id: str | None = Field(
        default=None,
        description=(
            "Existing session/task ID to continue. When provided, 'cmd' is sent to the "
            "process stdin instead of being executed."
        ),
    )
    wait_for_pattern: str | None = Field(
        default=None,
        description=(
            "Optional regex pattern. After starting or sending input, the tool blocks up "
            "to 'timeout' seconds until the pattern appears in output."
        ),
    )

    @model_validator(mode="after")
    def _validate_cmd(self) -> "PowershellParams":
        if self.task_id is None and not self.interactive and not self.cmd:
            raise ValueError("cmd cannot be empty unless interactive=True")
        if self.task_id is not None and not self.cmd:
            raise ValueError("cmd cannot be empty when continuing a session via task_id")
        return self

class Powershell(CallableTool2[PowershellParams]):

    name: str = "Powershell"
    description: str = (
        "Run a simple PowerShell command. Prefer Python for complex or stateful tasks. "
        "Start a persistent session with interactive=True, then reuse the same tool with "
        "task_id=<id> to send input and read output in one step. Use wait_for_pattern to wait "
        "for a prompt. TaskOutput remains available as a fallback for listing/monitoring tasks. "
        "Send 'exit' to close the session.\n"
        "PowerShell quick reference:\n"
        "- Cmdlets use Verb-Noun names: Get-ChildItem (list files), Get-Content (read file), "
        "Set-Location (cd), Copy-Item, Move-Item, Remove-Item, New-Item, "
        "Select-String (grep), Get-Command, Get-Help.\n"
        "- The pipeline `|` passes .NET objects, not plain text; shape results with "
        "Where-Object, Select-Object, ForEach-Object, Sort-Object, Measure-Object.\n"
        "- Comparison operators: -eq -ne -gt -ge -lt -le, -like (wildcard), -match (regex), "
        "-contains (collection membership), -replace (regex replace). "
        "Logical operators: -and -or -not (alias `!`).\n"
        "- Chain commands with `;` (always run next) or `&&` / `||` "
        "(PowerShell 7+: run next only on success / only on failure).\n"
        "- Strings: 'single quotes' are literal; \"double quotes\" expand $variables and "
        "$(subexpressions).\n"
        "- Redirection: `>` overwrite file, `>>` append, `2>&1` merge error stream into output.\n"
        "- $LASTEXITCODE holds the exit code of the last native command; "
        "$? is $true if the last command succeeded."
    )
    params: type[PowershellParams] = PowershellParams

    def __init__(self, session: Session):
        super().__init__()
        self._session = session
        if not _bash_tool._should_enable_powershell():
            raise SkipThisTool()

        if sys.platform == "win32":
            self.description += (
                " Windows paths must use backslashes (`\\`) instead of forward slashes (`/`)."
            )

        self._pwsh_path = find_pwsh()
        if self._pwsh_path is None:
            _print_warning(
                "PowerShell 7.x not found on this system; falling back to Windows PowerShell 5.1. "
                "PowerShell 7 syntax will be downgraded automatically, which may change command behavior."
            )

        # Pre-normalize forbidden commands once at init time for O(1) per-call lookup.
        # PowerShell is case-insensitive; normalize to lowercase.
        raw_forbidden = self._session.custom_config.get("config_json", {}).get("forbidden_commands", [])
        self._forbidden_keywords: list[str] = []
        seen: set[str] = set()
        for cmd in raw_forbidden:
            if not isinstance(cmd, str) or not cmd:
                continue
            normalized = " ".join(cmd.split()).lower()
            if normalized not in seen:
                seen.add(normalized)
                self._forbidden_keywords.append(normalized)

    async def __call__(self, params: PowershellParams) -> ToolReturnValue:
        """Execute the PowerShell command via the system PowerShell executable.

        Args:
            params: The parameters specifying the command and its arguments.

        Returns:
            ToolOk on success, ToolError on failure or timeout.
        """
        if params.task_id is not None:
            return await self._continue_session(params)

        if not params.interactive and not params.cmd:
            return ToolError(
                output="Empty command.",
                message="No command specified.",
                brief="Empty command",
            )

        if self._pwsh_path is not None:
            # PowerShell 7 is available: run the command as-is without syntax transforms.
            cmd = params.cmd
            transform_warning = ""
            executable = self._pwsh_path
        else:
            # Fall back to Windows PowerShell 5.1 and downgrade PS7 syntax.
            cmd, transform_warnings = pwsh_transform(params.cmd)
            transform_warning = ""
            if transform_warnings:
                warning_lines = "\n".join(w for w in transform_warnings)
                transform_warning = '\n[WARNING]' + warning_lines
            executable = "powershell"

        pattern = self._compile_pattern(params.wait_for_pattern)
        if isinstance(pattern, ToolError):
            return pattern

        if params.cmd and self._forbidden_keywords:
            # PowerShell is case-insensitive: compare lowercased strings.
            normalized_cmd = " ".join(cmd.split()).lower()
            for keyword in self._forbidden_keywords:
                if keyword in normalized_cmd:
                    return ToolError(
                        output="",
                        message=f"`{cmd}` is forbidden by config rule." + transform_warning,
                        brief="Forbidden command",
                    )
        # Refresh PATH/PATHEXT from registry so that tools installed
        # since the last command (e.g. via WinGet) are discoverable.
        if sys.platform == "win32":
            from kimix.utils.windows_env import refresh_env_from_registry
            refresh_env_from_registry()

        if params.interactive:
            ps_args = ["-NoP", "-Exec", "Bypass", "-NoL"]
            if cmd:
                ps_args.extend(["-NoExit", "-Command", cmd])
            else:
                ps_args.append("-NoExit")
            process_task = ProcessTask(executable, ps_args, None, _env_with_rg_bin_path(), append_newline=True)
            task_id = await process_task.start(self._session, "pwsh")
            if params.wait_for_pattern is not None and process_task.stream is not None:
                output, matched, elapsed = await process_task.stream.wait_for_output(
                    timeout=params.timeout, pattern=pattern
                )
                alive = await process_task.thread_is_alive()
                status = "running" if alive else "completed"
                return await self._format_session_result(
                    task_id, process_task.stream, params, output, status,
                    wait_matched=matched, elapsed_seconds=elapsed,
                    message=(
                        f"Interactive PowerShell started. task_id: `{task_id}`. "
                        "Send 'exit' to close the session."
                    ) + transform_warning,
                    brief="Interactive PowerShell started",
                )
            return ToolOk(
                output="",
                message=(
                    f"Interactive PowerShell started. task_id: `{task_id}`. "
                    "Use task_id to send commands and TaskOutput to read results. "
                    "Send 'exit' to close the session."
                ) + transform_warning,
                brief="Interactive PowerShell started",
            )

        # Build the command line to pass to PowerShell -Command
        process_task = ProcessTask(executable, ["-NoP", "-NonI", "-Exec", "Bypass", "-NoL", "-C", "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;$OutputEncoding=[System.Text.Encoding]::UTF8;", cmd], None, _env_with_rg_bin_path())
        task_id = await process_task.start(self._session, "pwsh")

        wait_matched: bool | None = None
        elapsed_seconds: float | None = None
        try:
            if params.wait_for_pattern is not None and process_task.stream is not None:
                output, wait_matched, elapsed_seconds = await process_task.stream.wait_for_output(
                    timeout=params.timeout, pattern=pattern
                )
                if await process_task.thread_is_alive():
                    return await self._format_session_result(
                        task_id, process_task.stream, params, output, "running",
                        wait_matched=wait_matched, elapsed_seconds=elapsed_seconds,
                        message=f"`{cmd}` matched pattern and is still running." + transform_warning,
                        brief="Pattern matched",
                    )
            else:
                await process_task.wait_with_monitor(params.timeout)
        except asyncio.CancelledError:
            # The tool call was cancelled (e.g. by a tool-level timeout or
            # shutdown). Stop the subprocess and return a tool error so the
            # conversation stream can continue.
            with contextlib.suppress(asyncio.CancelledError):
                await process_task.stop()
            from kimix.tools.background.utils import remove_task_id
            remove_task_id(self._session, task_id)
            output = await process_task.stream.get_output() if process_task.stream else ""
            output = await _maybe_export_output_async(output)
            transform_warning = transform_warning or ""
            return ToolError(
                output=output,
                message=f"`{cmd}` was cancelled." + transform_warning,
                brief="Command cancelled",
            )

        if await process_task.thread_is_alive():
            output = await process_task.stream.get_output() if process_task.stream else ""
            output = await _maybe_export_output_async(output)
            return ToolError(
                output=output,
                message=f"`{cmd}` Running in background. task_id: `{task_id}`. use `TaskOutput`." + transform_warning,
                brief="Timeout",
            )

        from kimix.tools.background.utils import remove_task_id
        remove_task_id(self._session, task_id)

        output = await process_task.stream.pop_output() if process_task.stream else ""
        success = await process_task.stream.success() if process_task.stream else False

        if not success:
            processed, output_path, output_truncated = await self._process_output(cmd, params, output)
            block = _build_session_output_block(
                task_id=task_id,
                status="completed",
                output=processed,
                exit_code=None,
                wait_matched=wait_matched,
                elapsed_seconds=elapsed_seconds,
                output_path=output_path,
                output_truncated=output_truncated,
            )
            return ToolError(output=block, message=f"`{cmd}` failed." + transform_warning, brief="Command execution failed")

        processed, output_path, output_truncated = await self._process_output(cmd, params, output)
        block = _build_session_output_block(
            task_id=task_id,
            status="completed",
            output=processed,
            exit_code=0,
            wait_matched=wait_matched,
            elapsed_seconds=elapsed_seconds,
            output_path=output_path,
            output_truncated=output_truncated,
        )
        return ToolOk(
            output=block,
            message=f'`{cmd}` success.' + transform_warning,
            brief=f"Command executed successfully",
            display_block=ShellDisplayBlock(language="powershell", command=cmd),
        )

    def _compile_pattern(self, wait_for_pattern: str | None) -> re.Pattern[str] | ToolError:
        if wait_for_pattern is None:
            return None
        try:
            return re.compile(wait_for_pattern)
        except re.error as e:
            return ToolError(
                output="",
                message=f"Invalid wait_for_pattern: {e}",
                brief="Invalid pattern",
            )

    async def _continue_session(self, params: PowershellParams) -> ToolReturnValue:
        """Send input to an existing PowerShell session and optionally wait for output."""
        from kimix.tools.background.utils import get_all_tasks

        tasks = get_all_tasks(self._session)
        task_id = params.task_id.strip() if params.task_id else ""
        stream = tasks.get(task_id)
        if stream is None:
            started = [tid for tid, s in tasks.items() if await s.is_started()]
            if not started:
                return ToolError(
                    output="",
                    message=f"Task '{params.task_id}' not found. No running tasks.",
                    brief="Task not found",
                )
            return ToolError(
                output="",
                message=(
                    f"Task '{params.task_id}' not found. "
                    f"Available tasks: [{', '.join(started)}]"
                ),
                brief=f"Task '{params.task_id}' not found",
            )

        pattern = self._compile_pattern(params.wait_for_pattern)
        if isinstance(pattern, ToolError):
            return pattern

        await stream.pop_output()

        input_text = params.cmd
        if not input_text.endswith("\n"):
            input_text += "\n"
        if not await stream.input(input_text):
            return ToolError(
                output="",
                message=f"Failed to send input to task '{task_id}'",
                brief="Send input failed",
            )

        output, matched, elapsed = await stream.wait_for_output(
            timeout=params.timeout, pattern=pattern
        )
        alive = await stream.thread_is_alive()
        status = "running" if alive else "completed"
        return await self._format_session_result(
            task_id, stream, params, output, status,
            wait_matched=matched, elapsed_seconds=elapsed,
            message=f"Data sent to `{task_id}`. Status: {status}.",
            brief="Data sent and output retrieved",
        )

    async def _process_output(
        self, command: str, params: PowershellParams, output: str
    ) -> tuple[str, str | None, bool]:
        """Summarize/export long output and return (display_output, path, truncated)."""
        output_truncated = False
        if params.max_output_length > 0 and len(output) > params.max_output_length:
            output = await _summarize_long_output_async(self._session, command, output)
            output_truncated = True
        output = await _maybe_export_output_async(output)
        output_path = _extract_export_path(output)
        return output, output_path, output_truncated

    async def _format_session_result(
        self,
        task_id: str,
        stream: 'BackgroundStream' | None,
        params: PowershellParams,
        output: str,
        status: str,
        *,
        wait_matched: bool | None,
        elapsed_seconds: float | None,
        message: str,
        brief: str,
    ) -> ToolReturnValue:
        """Build a ToolOk response with a structured output block."""
        processed, output_path, output_truncated = await self._process_output(params.cmd, params, output)
        block = _build_session_output_block(
            task_id=task_id,
            status=status,
            output=processed,
            exit_code=None if status != "completed" else (0 if await stream.success() else None),
            wait_matched=wait_matched,
            elapsed_seconds=elapsed_seconds,
            output_path=output_path,
            output_truncated=output_truncated,
        )
        return ToolOk(output=block, message=message, brief=brief)
