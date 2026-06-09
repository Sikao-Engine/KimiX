"""PowerShell tool that executes commands via the system PowerShell executable."""

import sys
from typing import TYPE_CHECKING

import kimi_cli
from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from pydantic import BaseModel, Field
from kimi_cli.session import Session
from kimi_cli.tools import SkipThisTool
from kimi_cli.tools.display import ShellDisplayBlock
from kimix.tools.file.bash.proccess_pwsh import pwsh_transform
from kimix.tools.common import _maybe_export_output_async, ProcessTask, _DEFAULT_FORBIDDEN_COMMANDS

if TYPE_CHECKING:
    from kimi_agent_sdk import CallableTool2 as _CallableTool2


class PowershellParams(BaseModel):
    """Parameters for the Powershell tool — execute a PowerShell command."""

    cmd: str = Field(description="PowerShell command.")
    timeout: int = Field(
        default=10,
        ge=3,
        le=900,
        description="Timeout in seconds."
    )

class Powershell(CallableTool2[PowershellParams]):

    name: str = "Powershell"
    description: str = "Run a simple powershell command. Prefer Python for complex or stateful tasks. "
    params: type[PowershellParams] = PowershellParams

    def __init__(self, session: Session):
        super().__init__()
        self._session = session
        if sys.platform != "win32":
            raise SkipThisTool()
        
        # Pre-normalize forbidden commands once at init time for O(1) per-call lookup.
        raw_forbidden = _DEFAULT_FORBIDDEN_COMMANDS + self._session.custom_config.get("config_json", {}).get("forbidden_commands", [])
        self._forbidden_tokens: list[list[str]] = []
        for cmd in raw_forbidden:
            if not isinstance(cmd, str) or not cmd:
                continue
            self._forbidden_tokens.append(" ".join(cmd.split()).split())

    async def __call__(self, params: PowershellParams) -> ToolReturnValue:
        """Execute the PowerShell command via the system PowerShell executable.

        Args:
            params: The parameters specifying the command and its arguments.

        Returns:
            ToolOk on success, ToolError on failure or timeout.
        """
        from kimix.tools.background.utils import remove_task_id

        if not params.cmd:
            return ToolError(
                output="Empty command.",
                message="No command specified.",
                brief="Empty command",
            )

        # Transform PS7 syntax to PS5.1 compatible syntax
        cmd, transform_warnings = pwsh_transform(params.cmd)
        transform_warning = ""
        if transform_warnings:
            warning_lines = "\n".join(w for w in transform_warnings)
            transform_warning = '\n[WARNING]' + warning_lines
        if self._forbidden_tokens:
            cmd_tokens = " ".join(cmd.split()).split()
            for forbidden_tokens in self._forbidden_tokens:
                if len(forbidden_tokens) > len(cmd_tokens):
                    continue
                if cmd_tokens[:len(forbidden_tokens)] == forbidden_tokens:
                    return ToolError(
                        output="",
                        message=f"`{cmd}` is forbidden by config rule." + transform_warning,
                        brief="Forbidden command",
                    )

        # Build the command line to pass to PowerShell -Command
        process_task = ProcessTask('powershell', ["-NoP", "-NonI", "-Exec", "Bypass", "-NoL", "-C", cmd], None, None)
        task_id = await process_task.start(self._session, "pwsh")

        await process_task.wait(params.timeout)

        if await process_task.thread_is_alive():
            output = await process_task.stream.get_output() if process_task.stream else ""
            return ToolError(
                output=output,
                message=f"`{cmd}` Running in background. task_id: `{task_id}`. use `TaskOutput` or `Input`." + transform_warning,
                brief="Timeout",
            )

        remove_task_id(self._session, task_id)

        output = await process_task.stream.pop_output() if process_task.stream else ""
        success = await process_task.stream.success() if process_task.stream else False

        if not success:
            return ToolError(output=output, message=f"`{cmd}` failed." + transform_warning, brief="Command execution failed")

        output = await _maybe_export_output_async(output)
        return ToolOk(
            output=output,
            message=f'`{cmd}` success.' + transform_warning,
            brief=f"Command executed successfully",
            display_block=ShellDisplayBlock(language="powershell", command=cmd),
        )
