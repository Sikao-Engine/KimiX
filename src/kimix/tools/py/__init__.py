"""Python tool that executes code or runs .py files via the system Python executable."""

import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import regex as re
from kimix.tools.common import (
    _build_session_output_block,
    _extract_export_path,
    _interactive_scope_text,
    _maybe_export_output_async,
    _summarize_long_output_async,
    _token_filter_output,
    ProcessTask,
)
from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from pydantic import BaseModel, Field, model_validator
from kimi_cli.session import Session

if TYPE_CHECKING:
    from kimix.tools.background.utils import BackgroundStream


class Params(BaseModel):
    code: str = Field(
        default="",
        description="Python code to execute, or path to a .py file to run directly.",
    )
    output_path: str | None = Field(
        default=None,
        description="Output file path."
    )
    timeout: int = Field(
        default=10,
        ge=3,
        le=900,
        description="Timeout in seconds."
    )
    run_in_background: bool = Field(
        default=False,
        description=(
            "Run the Python code in the background and return immediately. "
            "Use this for one-shot background execution (no stdin interaction). "
            "For persistent interactive sessions, use interactive=True + task_id."
        )
    )
    interactive: bool = Field(
        default=False,
        description=(
            "Run Python interactively. The process stays alive and accepts "
            "further input via task_id. Returns a task_id immediately; "
            "use TaskOutput to read output."
        ),
    )
    task_id: str | None = Field(
        default=None,
        description=(
            "Existing session/task ID to continue. When provided, 'code' is sent to "
            "the process stdin instead of being executed as a new script."
        ),
    )
    wait_for_pattern: str | None = Field(
        default=None,
        description=(
            "Optional regex pattern. After starting or sending input, the tool blocks up "
            "to 'timeout' seconds until the pattern appears in output."
        ),
    )
    max_lines: int | None = Field(
        default=None,
        ge=3,
        description="Max lines to return via head+tail fold. <N> head lines + <N> tail lines kept; middle collapsed. None = unlimited.",
    )

    @model_validator(mode="after")
    def _validate_code(self) -> "Params":
        if self.task_id is None and not self.interactive and not self.code:
            raise ValueError("code cannot be empty unless interactive=True")
        if self.task_id is not None and not self.code:
            raise ValueError("code cannot be empty when continuing a session via task_id")
        return self


class Python(CallableTool2[Params]):
    name: str = "Python"
    description: str = (
        "Execute Python code or run a .py file directly. "
        + _interactive_scope_text(is_shell=False)
    )
    params: type[Params] = Params

    def __init__(self, session: Session):
        super().__init__()
        self._session = session
        self._semaphore = asyncio.Semaphore(8)
        self._script_counter = 0

    async def __call__(self, params: Params) -> ToolReturnValue:
        # Early dispatch: continue an existing session
        if params.task_id is not None:
            return await self._continue_session(params)

        async with self._semaphore:
            # Interactive mode path
            if params.interactive:
                return await self._start_interactive(params)

            # Non-interactive path — execute code and wait for completion
            return await self._execute_code(params)

    async def _start_interactive(self, params: Params) -> ToolReturnValue:
        """Start an interactive Python session."""
        # Determine script path if code is provided
        if params.code:
            code_path = Path(params.code)
            is_file_mode = params.code.endswith('.py') and code_path.is_file()

            if is_file_mode:
                script_path = params.code
                args = ["-i", script_path]
            else:
                # Inline mode: write code to a numbered file
                session_dir = Path(self._session.dir)
                script_name = f"{self._script_counter}.py"
                script_path = str(session_dir / script_name)
                self._script_counter += 1

                async with await anyio.open_file(script_path, 'w', encoding='utf-8', errors='replace') as f:
                    await f.write(params.code)

                args = ["-i", script_path]
        else:
            # Pure interactive REPL (no initial code)
            args = ["-i"]

        pattern = self._compile_pattern(params.wait_for_pattern)
        if isinstance(pattern, ToolError):
            return pattern

        process_task = ProcessTask(sys.executable, args, append_newline=True)
        task_id = await process_task.start(self._session, "python")

        if params.wait_for_pattern is not None and process_task.stream is not None:
            inactivity_timeout = min(30.0, float(params.timeout))
            output, matched, elapsed = await process_task.stream.wait_for_output(
                timeout=params.timeout, pattern=pattern,
                inactivity_timeout=inactivity_timeout,
            )
            alive = await process_task.thread_is_alive()
            status = "running" if alive else "completed"
            return await self._format_session_result(
                task_id, process_task.stream, params, output, status,
                wait_matched=matched, elapsed_seconds=elapsed,
                message=(
                    f"Interactive Python started. task_id: `{task_id}`. "
                    "Send 'exit()' to close the session."
                ),
                brief="Interactive Python started",
            )

        return ToolOk(
            output="",
            message=(
                f"Interactive Python started. task_id: `{task_id}`. "
                "Use task_id to send commands and TaskOutput to read results. "
                "Send 'exit()' to close the session."
            ),
            brief="Interactive Python started",
        )

    async def _execute_code(self, params: Params) -> ToolReturnValue:
        """Execute Python code (non-interactive, one-shot)."""
        # Determine if code is a .py file path or inline code
        code_path = Path(params.code)
        is_file_mode = params.code.endswith('.py') and code_path.is_file()

        if is_file_mode:
            # File mode: run the given .py file directly
            script_path = params.code
            display_script_path = script_path.replace("\\", "/")
            args = [script_path]
            source_label = "File"
        else:
            # Inline mode: write code to a numbered file in the session directory
            session_dir = Path(self._session.dir)
            script_name = f"{self._script_counter}.py"
            script_path = str(session_dir / script_name)
            self._script_counter += 1

            async with await anyio.open_file(script_path, 'w', encoding='utf-8', errors='replace') as f:
                await f.write(params.code)

            display_script_path = script_path.replace("\\", "/")
            args = [script_path]
            source_label = "Script"

        process_task = ProcessTask(sys.executable, args)
        task_id = await process_task.start(self._session, "python")

        if params.run_in_background:
            return ToolOk(
                output=f"{source_label} saved to `{display_script_path}`. Running in background. task_id: `{task_id}`. Use `TaskOutput` tool to retrieve output.",
                brief="Background task started"
            )

        wait_matched: bool | None = None
        elapsed_seconds: float | None = None
        try:
            if params.wait_for_pattern is not None and process_task.stream is not None:
                pattern = self._compile_pattern(params.wait_for_pattern)
                if isinstance(pattern, ToolError):
                    return pattern
                inactivity_timeout = min(30.0, float(params.timeout))
                output, wait_matched, elapsed_seconds = await process_task.stream.wait_for_output(
                    timeout=params.timeout, pattern=pattern,
                    inactivity_timeout=inactivity_timeout,
                )
                if await process_task.thread_is_alive():
                    return await self._format_session_result(
                        task_id, process_task.stream, params, output, "running",
                        wait_matched=wait_matched, elapsed_seconds=elapsed_seconds,
                        message=f"Python code matched pattern and is still running.",
                        brief="Pattern matched",
                    )
            else:
                # Wait for completion with timeout (allow a small buffer for cleanup)
                await process_task.wait_with_monitor(params.timeout)
        except asyncio.CancelledError:
            await process_task.stop()
            from kimix.tools.background.utils import remove_task_id
            remove_task_id(self._session, task_id)
            output = await process_task.stream.get_output() if process_task.stream else ""
            output = await _maybe_export_output_async(output)
            return ToolError(
                output=output,
                message=f"Python execution was cancelled.",
                brief="Execution cancelled",
            )

        if await process_task.thread_is_alive():
            output = await process_task.stream.pop_output() if process_task.stream else ""
            output = await _maybe_export_output_async(output)
            return ToolError(
                output=output,
                message=f"{source_label} saved to `{display_script_path}`. Running in background. task_id: `{task_id}`. use `TaskOutput`",
                brief="Timeout"
            )

        # Clean up foreground task registration
        from kimix.tools.background.utils import remove_task_id
        remove_task_id(self._session, task_id)

        # Get output
        output = await process_task.stream.pop_output() if process_task.stream else ""
        stream = process_task.stream
        success = await stream.success() if stream else False
        real_exit_code = stream.exit_code if stream else None

        # Handle output_path parameter if provided
        if params.output_path:
            async with await anyio.open_file(params.output_path, 'w', encoding='utf-8', errors='replace') as f:
                await f.write(output)
            display_path = params.output_path.replace("\\", "/")
            output = f'output exported to: {display_path}'
            # Use plain output for legacy output_path — skip structured block
            if not success:
                return ToolError(
                    output=output,
                    message="Python execution failed",
                    brief="Python execution error"
                )
            success_message = f"{source_label}: `{display_script_path}`"
            return ToolOk(output=f"{success_message}\n\n{output}", brief=f"Python file executed: {display_script_path}")

        # Process output through token filter and summarization
        processed, output_path, output_truncated, original_path = await self._process_output(
            params, output
        )
        block = _build_session_output_block(
            task_id=task_id,
            status="completed",
            output=processed,
            exit_code=real_exit_code,
            wait_matched=wait_matched,
            elapsed_seconds=elapsed_seconds,
            output_path=output_path,
            output_truncated=output_truncated,
            original_path=original_path,
        )

        if not success:
            return ToolError(
                output=block,
                message=f"{source_label}: `{display_script_path}` failed",
                brief="Python execution error"
            )

        success_message = f"{source_label}: `{display_script_path}`"
        return ToolOk(
            output=block,
            message=success_message,
            brief=f"Python {'file' if is_file_mode else 'code'} executed successfully"
        )

    def _compile_pattern(self, wait_for_pattern: str | None) -> re.Pattern[str] | ToolError:
        """Compile a regex pattern, returning ToolError on invalid input."""
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

    async def _continue_session(self, params: Params) -> ToolReturnValue:
        """Send input to an existing Python session and optionally wait for output."""
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

        input_text = params.code
        if not input_text.endswith("\n"):
            input_text += "\n"
        if not await stream.input(input_text):
            return ToolError(
                output="",
                message=f"Failed to send input to task '{task_id}'",
                brief="Send input failed",
            )

        inactivity_timeout = min(30.0, float(params.timeout))
        output, matched, elapsed = await stream.wait_for_output(
            timeout=params.timeout, pattern=pattern,
            inactivity_timeout=inactivity_timeout,
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
        self, params: Params, output: str
    ) -> tuple[str, str | None, bool, str | None]:
        """Summarize/export long output. Returns (display_output, path, truncated, original_path)."""
        # Run token filter pipeline (dedup, truncate).
        # Python output does not use RTK, so token_kill=False and rtk_rewritten=False.
        output, original_path = await _token_filter_output(
            output,
            token_kill=False,
            max_lines=params.max_lines,
            rtk_rewritten=False,
        )
        output_truncated = False
        if len(output) > 65536:
            # Use the code itself as the 'command' for summarization context
            output = await _summarize_long_output_async(self._session, params.code, output)
            output_truncated = True
        output = await _maybe_export_output_async(output)
        output_path = _extract_export_path(output)
        return output, output_path, output_truncated, original_path

    async def _format_session_result(
        self,
        task_id: str,
        stream: 'BackgroundStream' | None,
        params: Params,
        output: str,
        status: str,
        *,
        wait_matched: bool | None,
        elapsed_seconds: float | None,
        message: str,
        brief: str,
    ) -> ToolReturnValue:
        """Build a ToolOk response with a structured output block."""
        processed, output_path, output_truncated, original_path = await self._process_output(
            params, output
        )
        if status != "completed":
            real_exit_code = None
        else:
            real_exit_code = stream.exit_code if stream else None
            if real_exit_code is None:
                real_exit_code = 0 if await stream.success() else None
        block = _build_session_output_block(
            task_id=task_id,
            status=status,
            output=processed,
            exit_code=real_exit_code,
            wait_matched=wait_matched,
            elapsed_seconds=elapsed_seconds,
            output_path=output_path,
            output_truncated=output_truncated,
            original_path=original_path,
        )
        return ToolOk(output=block, message=message, brief=brief)
