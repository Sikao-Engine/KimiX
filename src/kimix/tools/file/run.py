"""run tool for executing a process from a path."""
import anyio
import asyncio
from pathlib import Path
import regex as re
import shlex
import sys
import tempfile
from kimi_cli.tools import SkipThisTool
from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from pydantic import BaseModel, Field
from kimi_cli.session import Session
from kimix.tools.common import (
    _build_session_output_block,
    _env_with_rg_bin_path,
    _extract_export_path,
    _maybe_export_output_async,
    _export_to_temp_file_async,
    _summarize_long_output_async,
    ProcessTask,
)
from kimi_cli.tools.display import ShellDisplayBlock
from kimi_cli.share import get_share_dir
import functools
import shlex
import shutil
_HUGE_CMD_THRESHOLD = 10000
"""Character count above which command display is culled to only the path."""

USE_SYSTEM_PWSH_ON_WINDOWS = True
USE_SYSTEM_SHELL = True

@functools.lru_cache(maxsize=1)
def find_bash() -> str | None:
    """Find the system bash executable."""
    if sys.platform == "darwin":
        # Strategy 1: Homebrew bash (Apple Silicon) – often newer than system bash
        candidate = Path("/opt/homebrew/bin/bash")
        if candidate.exists():
            return str(candidate.resolve())
        # Strategy 2: Homebrew bash (Intel Macs)
        candidate = Path("/usr/local/bin/bash")
        if candidate.exists():
            return str(candidate.resolve())
        # Strategy 3: MacPorts
        candidate = Path("/opt/local/bin/bash")
        if candidate.exists():
            return str(candidate.resolve())
        # Strategy 4: Git bash fallback (official Git installer for macOS)
        git_path = shutil.which("git")
        if git_path:
            git_exe = Path(git_path).resolve()
            if git_exe.parent.name.lower() == "bin":
                git_root = git_exe.parent
            else:
                git_root = git_exe.parent
            for subpath in ("bin/bash", "usr/bin/bash"):
                bash_candidate = git_root / subpath
                if bash_candidate.exists():
                    return str(bash_candidate.resolve())
        # Strategy 5: System bash (older, but guaranteed to exist)
        candidate = Path("/bin/bash")
        if candidate.exists():
            return str(candidate.resolve())

    bash = shutil.which("bash")
    if bash:
        return bash
    return None

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
    env: str | list[str] | None = Field(
        default=None,
        description="Environment variables to set for the subprocess."
    )
    run_in_background: bool = Field(
        default=False,
        description="Run the process in the background and return immediately."
    )
    max_output_length: int = Field(
        default=65536,
        ge=0,
        description="Output length threshold. Exceeding it sends the output to an anonymous sub-agent for summarization. 0 disables."
    )
    task_id: str | None = Field(
        default=None,
        description=(
            "Existing session/task ID to continue. When provided, 'command' is sent to the "
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


class Run(CallableTool2[RunParams]):
    name: str = "Run"
    description: str = (
        "Run an executable or bash command. "
        "Start a background session with run_in_background=True, then reuse the same tool with "
        "task_id=<id> to send input and read output in one step. Use wait_for_pattern to wait "
        "for a prompt. TaskOutput remains available as a fallback for listing/monitoring tasks."
    )
    params: type[RunParams] = RunParams

    def __init__(self, session: Session):
        import os
        super().__init__()
        if USE_SYSTEM_SHELL:
            if sys.platform == "win32" and USE_SYSTEM_PWSH_ON_WINDOWS:
                raise SkipThisTool()
            else:
                if find_bash() is not None:
                    raise SkipThisTool()
        self._session = session
        self._semaphore = asyncio.Semaphore(8)
        self.use_posix = sys.platform != "win32"

        # Pre-normalize forbidden commands once at init time for O(1) per-call lookup.
        raw_forbidden = self._session.custom_config.get(
            "config_json", {}).get("forbidden_commands", [])
        self._forbidden_keywords: list[str] = []
        seen: set[str] = set()
        for cmd in raw_forbidden:
            if not isinstance(cmd, str) or not cmd:
                continue
            normalized = " ".join(cmd.split())
            if normalized not in seen:
                seen.add(normalized)
                self._forbidden_keywords.append(normalized)

    async def __call__(self, params: RunParams) -> ToolReturnValue:
        import os
        if params.task_id is not None:
            return await self._continue_session(params)

        pattern = self._compile_pattern(params.wait_for_pattern)
        if isinstance(pattern, ToolError):
            return pattern

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
            display_args = [
                arg[:100] + '...' if len(arg) > 100 else arg for arg in args_list]
            cmd_str = shlex.join([executable] + display_args)
            display_cmd = executable if len(
                cmd_str) > _HUGE_CMD_THRESHOLD else cmd_str

            # Check forbidden commands (pre-normalized in __init__)
            if self._forbidden_keywords:
                full_cmd = params.command
                normalized_cmd = " ".join(full_cmd.split())
                for keyword in self._forbidden_keywords:
                    if keyword in normalized_cmd:
                        return ToolError(
                            output="",
                            message=f"Command `{full_cmd}` is forbidden by config rule.",
                            brief="Forbidden command",
                        )

            # Check if executable is a valid process (in PATH or existing file),
            # then fall back to bash built-in commands.
            import shutil
            # Refresh PATH/PATHEXT from registry so that tools installed
            # since the last command (e.g. via WinGet) are discoverable.
            if sys.platform == "win32":
                from kimix.utils.windows_env import refresh_env_from_registry
                refresh_env_from_registry()

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
                c_idx = next((i for i, a in enumerate(
                    args_list) if a == '-c'), None)
                if c_idx is not None and c_idx + 1 < len(args_list) and len(args_list[c_idx + 1]) > 30000:
                    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as f:
                        f.write(args_list[c_idx + 1])
                        script_path = f.name
                    # Replace -c <code> with <script_path>, preserving leading options and trailing args
                    args_list = args_list[:c_idx] + \
                        [script_path] + args_list[c_idx + 2:]

            async with self._semaphore:
                env_dict: dict[str, str] | None = None
                if isinstance(params.env, str):
                    tokens = shlex.split(params.env)
                    env_items = []
                    i = 0
                    while i < len(tokens):
                        if i + 2 < len(tokens) and tokens[i + 1] == '=' and '=' not in tokens[i]:
                            env_items.append(f"{tokens[i]}={tokens[i + 2]}")
                            i += 3
                        else:
                            env_items.append(tokens[i])
                            i += 1
                else:
                    env_items = params.env
                if env_items:
                    env_dict = {}
                    for item in env_items:
                        if '=' in item:
                            key, value = item.split('=', 1)
                            env_dict[key] = value
                        else:
                            env_dict[item] = '1'
                task = ProcessTask(executable, args_list, params.cwd, _env_with_rg_bin_path(env_dict))
                task_id = await task.start(self._session, "run", Path(executable).stem)

                wait_matched: bool | None = None
                elapsed_seconds: float | None = None

                if params.run_in_background:
                    if params.wait_for_pattern is not None and task.stream is not None:
                        output, wait_matched, elapsed_seconds = await task.stream.wait_for_output(
                            timeout=params.timeout, pattern=pattern
                        )
                        return await self._format_session_result(
                            task_id, task.stream, params, output, "running",
                            wait_matched=wait_matched, elapsed_seconds=elapsed_seconds,
                            message=f"`{display_cmd}` running in background. task_id: `{task_id}`.",
                            brief="Background task started",
                        )
                    return ToolOk(
                        output="",
                        message=f"`{display_cmd}` running in background. task_id: `{task_id}`. Use `TaskOutput` tool to retrieve output.",
                        brief="Background task started",
                        display_block=ShellDisplayBlock(
                            language="shell", command=display_cmd),
                    )

                # Wait for completion (or a pattern match) with timeout.
                if params.wait_for_pattern is not None and task.stream is not None:
                    output, wait_matched, elapsed_seconds = await task.stream.wait_for_output(
                        timeout=params.timeout, pattern=pattern
                    )
                    if await task.thread_is_alive():
                        return await self._format_session_result(
                            task_id, task.stream, params, output, "running",
                            wait_matched=wait_matched, elapsed_seconds=elapsed_seconds,
                            message=f"`{display_cmd}` matched pattern and is still running.",
                            brief="Pattern matched",
                        )
                else:
                    wait_timeout = params.timeout
                    await task.wait(wait_timeout)

                if await task.thread_is_alive():
                    output = await task.stream.get_output() if task.stream else ""
                    output = await _maybe_export_output_async(output)
                    return ToolError(
                        output=output,
                        message=f"`{display_cmd}` Running in background. task_id: `{task_id}`. use `TaskOutput`",
                        brief="Timeout",
                    )
                # Clean up foreground task registration
                from kimix.tools.background.utils import remove_task_id
                remove_task_id(self._session, task_id)

                # Get output
                output = await task.stream.pop_output() if task.stream else ""

                # Optionally offload a very long output to a sub-agent
                output_truncated = False
                if (
                    params.max_output_length > 0
                    and len(output) > params.max_output_length
                    and not params.output_path
                ):
                    output = await _summarize_long_output_async(
                        self._session, params.command, output
                    )
                    output_truncated = True

                # Handle output export if needed
                output_path: str | None = None
                if params.output_path:
                    async with await anyio.open_file(params.output_path, 'w', encoding='utf-8', errors='replace') as f:
                        await f.write(output)
                    display_path = params.output_path.replace("\\", "/")
                    output_path = display_path
                    output = f'saved to file `{display_path}`'

                # Check success
                success = await task.stream.success() if task.stream else False

                if not success:
                    if output and not params.output_path:
                        if params.max_output_length > 0 and len(output) > params.max_output_length:
                            output = await _summarize_long_output_async(
                                self._session, params.command, output
                            )
                            output_truncated = True
                        else:
                            temp_path, _ = await _export_to_temp_file_async(key=None, content=output, ext='.txt')
                            display_temp_path = temp_path.replace("\\", "/")
                            output_path = display_temp_path
                            output = f'saved to file `{display_temp_path}`'
                    block = _build_session_output_block(
                        task_id=task_id,
                        status="completed",
                        output=output,
                        exit_code=None,
                        wait_matched=wait_matched,
                        elapsed_seconds=elapsed_seconds,
                        output_path=output_path,
                        output_truncated=output_truncated,
                    )
                    return ToolError(
                        output=block,
                        message=f"`{display_cmd}` failed",
                        brief="Command execution failed",
                    )

                output = await _maybe_export_output_async(output)
                if not output_path:
                    output_path = _extract_export_path(output)
                block = _build_session_output_block(
                    task_id=task_id,
                    status="completed",
                    output=output,
                    exit_code=0,
                    wait_matched=wait_matched,
                    elapsed_seconds=elapsed_seconds,
                    output_path=output_path,
                    output_truncated=output_truncated,
                )
                return ToolOk(
                    output=block,
                    message=f"`{display_cmd}` success",
                    brief="Command executed successfully",
                    display_block=ShellDisplayBlock(
                        language="shell", command=display_cmd),
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

    async def _continue_session(self, params: RunParams) -> ToolReturnValue:
        """Send input to an existing Run session and optionally wait for output."""
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

        # Discard prior output so we only report new output produced after this input.
        await stream.pop_output()

        input_text = params.command
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
        self, params: RunParams, output: str
    ) -> tuple[str, str | None, bool]:
        """Summarize/export long output and return (display_output, path, truncated)."""
        output_truncated = False
        if params.max_output_length > 0 and len(output) > params.max_output_length:
            output = await _summarize_long_output_async(
                self._session, params.command, output
            )
            output_truncated = True
        output = await _maybe_export_output_async(output)
        output_path = _extract_export_path(output)
        return output, output_path, output_truncated

    async def _format_session_result(
        self,
        task_id: str,
        stream: 'BackgroundStream' | None,
        params: RunParams,
        output: str,
        status: str,
        *,
        wait_matched: bool | None,
        elapsed_seconds: float | None,
        message: str,
        brief: str,
    ) -> ToolReturnValue:
        """Build a ToolOk response with a structured output block."""
        processed, output_path, output_truncated = await self._process_output(params, output)
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
