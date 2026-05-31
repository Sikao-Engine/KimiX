"""run tool for executing a process from a path."""
import anyio
import asyncio
from pathlib import Path
import shlex
import sys
import tempfile

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from pydantic import BaseModel, Field
from kimi_cli.session import Session
from kimix.tools.common import _maybe_export_output_async, _export_to_temp_file_async, ProcessTask
from kimi_cli.tools.display import ShellDisplayBlock
from kimi_cli.tools import SkipThisTool
from kimix.tools.file.bash.bash_tool import find_bash

_HUGE_CMD_THRESHOLD = 10000
"""Character count above which command display is culled to only the path."""

_DEFAULT_FORBIDDEN_COMMANDS = [
    "taskkill",
    "kill",
    "killall",
    "pkill",
    "xkill",
    "rd",
    "format",
    "fdisk",
    "dd",
    "mkfs",
    "shutdown",
    "reboot",
    "poweroff",
    "halt",
    "reg",
    "regedit",
    "regedt32",
]
class RunParams(BaseModel):
    command: str = Field(
        description=(
            "Executable command line. Only real executables / processes are accepted — "
            "No shell syntax (pipes, redirects, &&, ||, variables, etc.). "
            "Example: `python -c \"print(1)\"` or `git status`."
        )
    )
    timeout: int = Field(
        default=10,
        ge=3,
        le=900,
        description="Timeout in seconds."
    )
    output_path: str | None = Field(
        default=None,
        description="Output file path."
    )
    cwd: str | None = Field(
        default=None,
        description="Working directory."
    )
    env: list[str] | None = Field(
        default=None,
        description="Environment variables to set for the subprocess."
    )
    run_in_background: bool = Field(
        default=False,
        description="Run the process in the background and return immediately."
    )


class Run(CallableTool2[RunParams]):
    name: str = "Run"
    description: str = "Run an executable or bash command."
    params: type[RunParams] = RunParams

    def __init__(self, session: Session):
        import os
        os.environ['PYTHONIOENCODING'] = 'utf-8'
        super().__init__()
        if find_bash() is not None:
            raise SkipThisTool()
        self._session = session
        self._semaphore = asyncio.Semaphore(8)
        self.use_posix = sys.platform != "win32"

    async def __call__(self, params: RunParams) -> ToolReturnValue:
        import os
        script_path: str | None = None
        use_posix = self.use_posix

        # Split the full command into parts.
        # Use posix=False on Windows to preserve backslashes in paths;
        # on POSIX systems use posix=True for correct quoting/escaping.
        # double quotes are NOT stripped in posix=False mode, so we strip them below.
        cmd_parts: list[str] = shlex.split(params.command, posix=use_posix)
        if not cmd_parts:
            return ToolError(
                output="",
                message="Empty command.",
                brief="Empty command",
            )

        # -- Resolve the executable: progressive prefix lookup for paths with spaces --
        # First, strip outer double quotes from the first element if present.
        # (Only needed when posix=False; posix=True already strips shell quotes.)
        first = cmd_parts[0]
        if not use_posix and first.startswith('"') and first.endswith('"'):
            first = first[1:-1]
        executable = first
        args_raw: list[str] = cmd_parts[1:]

        if len(cmd_parts) > 1:
            # Try progressively longer prefixes to find an existing file,
            # so unquoted paths with spaces are handled correctly.
            for i in range(2, len(cmd_parts) + 1):
                candidate = " ".join(cmd_parts[:i])
                # Strip outer double quotes if present on the candidate
                if not use_posix and candidate.startswith('"') and candidate.endswith('"'):
                    candidate = candidate[1:-1]
                try:
                    if Path(candidate).is_file():
                        executable = candidate
                        args_raw = cmd_parts[i:]
                        break
                except OSError:
                    pass

        # Strip surrounding double quotes from each arg.
        # Only needed when posix=False, because posix=True already strips them.
        if not use_posix:
            args_list: list[str] = []
            for arg in args_raw:
                if arg.startswith('"') and arg.endswith('"'):
                    args_list.append(arg[1:-1])
                else:
                    args_list.append(arg)
        else:
            args_list = list(args_raw)

        try:
            display_args = [arg[:100] + '...' if len(arg) > 100 else arg for arg in args_list]
            cmd_str = shlex.join([executable] + display_args)
            display_cmd = executable if len(cmd_str) > _HUGE_CMD_THRESHOLD else cmd_str

            # Check forbidden commands (default + user-configured)
            forbidden_commands = _DEFAULT_FORBIDDEN_COMMANDS + self._session.custom_config.get("config_json", {}).get("forbidden_commands", [])
            if forbidden_commands:
                full_cmd = params.command
                normalized_cmd = " ".join(full_cmd.split())
                cmd_tokens = normalized_cmd.split()
                for forbidden in forbidden_commands:
                    if not isinstance(forbidden, str) or not forbidden:
                        continue
                    normalized_forbidden = " ".join(forbidden.split())
                    forbidden_tokens = normalized_forbidden.split()
                    if len(forbidden_tokens) > len(cmd_tokens):
                        continue
                    if cmd_tokens[:len(forbidden_tokens)] == forbidden_tokens:
                        return ToolError(
                            output="",
                            message=f"Command `{full_cmd}` is forbidden by config rule: `{forbidden}`.",
                            brief=display_cmd,
                        )

            # Check if executable is a valid process (in PATH or existing file),
            # then fall back to bash built-in commands.
            import shutil

            is_process = False
            is_py = False
            if (executable == 'python' and (shutil.which('python') is None) and (not Path('./python').exists())) or (executable == 'python.exe' and (shutil.which('python.exe') is None) and (not Path('./python.exe').exists())):
                executable = sys.executable
                is_process = True
                is_py = True
            elif os.sep in executable or "/" in executable:
                # Contains path separator - check if it's an existing file
                is_process = Path(executable).is_file()
            else:
                # Bare command name - check if it's in PATH
                is_process = shutil.which(executable) is not None

            if not is_process:
                # Not a real process - check if it's a bash built-in command.
                error_msg = " This tool does not support shell commands; use `Bash` tool."
                return ToolError(
                    output='',
                    message=error_msg,
                    brief='Bash not supported.'
                )

            # Handle extremely long python -c scripts via temp file (Windows CreateProcessW ~32767 limit)
            if is_py:
                c_idx = next((i for i, a in enumerate(args_list) if a == '-c'), None)
                if c_idx is not None and c_idx + 1 < len(args_list) and len(args_list[c_idx + 1]) > 30000:
                    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as f:
                        f.write(args_list[c_idx + 1])
                        script_path = f.name
                    # Replace -c <code> with <script_path>, preserving leading options and trailing args
                    args_list = args_list[:c_idx] + [script_path] + args_list[c_idx + 2:]

            async with self._semaphore:
                env_dict: dict[str, str] | None = None
                if params.env:
                    env_dict = {}
                    for item in params.env:
                        if '=' in item:
                            key, value = item.split('=', 1)
                            env_dict[key] = value
                        else:
                            env_dict[item] = '1'
                task = ProcessTask(executable, args_list, params.cwd, env_dict)
                task_id = await task.start(self._session, "run", Path(executable).stem)

                if params.run_in_background:
                    return ToolOk(
                        output=f"Running in background. task_id: `{task_id}`. Use `TaskOutput` tool to retrieve output.",
                        brief="Background task started",
                        display_block=ShellDisplayBlock(language="shell", command=display_cmd),
                    )

                # Wait for completion with timeout (allow a small buffer for cleanup)
                wait_timeout = params.timeout
                await task.wait(wait_timeout)

                if await task.thread_is_alive():
                    output = await task.stream.get_output() if task.stream else ""
                    return ToolError(
                        output=output,
                        message=f"Running in background. task_id: `{task_id}`. use `TaskOutput` or `Input`",
                        brief=f"Timeout: {display_cmd}",
                    )
                # Clean up foreground task registration
                from kimix.tools.background.utils import remove_task_id
                remove_task_id(self._session, task_id)

                # Get output
                output = await task.stream.pop_output() if task.stream else ""

                # Handle output export if needed
                if params.output_path:
                    async with await anyio.open_file(params.output_path, 'w', encoding='utf-8', errors='replace') as f:
                        await f.write(output)
                    output = f'saved to file `{params.output_path}`'

                # Check success
                success = await task.stream.success() if task.stream else False

                if not success:
                    if output and not params.output_path:
                        temp_path, _ = await _export_to_temp_file_async(key=None, content=output, ext='.txt')
                        output = f'saved to file `{temp_path}`'
                    return ToolError(
                        output=output,
                        message="Command execution failed",
                        brief=display_cmd,
                    )

                output = await _maybe_export_output_async(output)
                return ToolOk(
                    output=output,
                    brief="Command executed successfully",
                    display_block=ShellDisplayBlock(language="shell", command=display_cmd),
                )
        except Exception as e:
            return ToolError(
                output='',
                message='Internal error, quit current session now.',
                brief='Internal error'
            )
        finally:
            if script_path is not None:
                try:
                    os.remove(script_path)
                except Exception:
                    pass
