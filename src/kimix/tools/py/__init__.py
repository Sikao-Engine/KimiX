import asyncio
import sys
from pathlib import Path

import anyio
from kimix.tools.common import _maybe_export_output_async, _export_to_temp_file_async, ProcessTask
from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from pydantic import BaseModel, Field
from kimi_cli.session import Session


class Params(BaseModel):
    code: str = Field(
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
        description="Run the Python code in the background and return immediately."
    )


class Python(CallableTool2[Params]):
    name: str = "Python"
    description: str = "Execute Python code or run a .py file directly."
    params: type[Params] = Params

    def __init__(self, session: Session):
        super().__init__()
        self._session = session
        self._semaphore = asyncio.Semaphore(8)
        self._script_counter = 0

    async def __call__(self, params: Params) -> ToolReturnValue:
        async with self._semaphore:
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

            task = ProcessTask(sys.executable, args)
            task_id = await task.start(self._session, "python")

            if params.run_in_background:
                return ToolOk(
                    output=f"{source_label} saved to `{display_script_path}`. Running in background. task_id: `{task_id}`. Use `TaskOutput` tool to retrieve output.",
                    brief="Background task started"
                )

            # Wait for completion with timeout (allow a small buffer for cleanup)
            wait_timeout = params.timeout
            await task.wait_with_monitor(wait_timeout)

            if await task.thread_is_alive():
                output = await task.stream.get_output() if task.stream else ""
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
            output = await task.stream.pop_output() if task.stream else ""

            # Handle output_path parameter if provided
            if params.output_path:
                async with await anyio.open_file(params.output_path, 'w', encoding='utf-8', errors='replace') as f:
                    await f.write(output)
                display_path = params.output_path.replace("\\", "/")
                output = f'output exported to: {display_path}'
            else:
                output = await _maybe_export_output_async(output)

            # Check success
            success = await task.stream.success() if task.stream else False

            if not success:
                if output and not params.output_path:
                    temp_path, _ = await _export_to_temp_file_async(key=None, content=output, ext='.txt')
                    display_temp_path = temp_path.replace("\\", "/")
                    output = f'saved to file `{display_temp_path}`'
                return ToolError(
                    output=f"{source_label}: `{display_script_path}`\n\n{output}",
                    message="Python execution failed",
                    brief="Python execution error"
                )

            success_message = f"{source_label}: `{display_script_path}`"
            if is_file_mode:
                return ToolOk(output=f"{success_message}\n\n{output}", brief=f"Python file executed: {display_script_path}")
            # The code itself is intentionally not echoed back: it is streamed
            # live (formatted and colored) by the CLI printer while the tool
            # call is generated (see kimix.base), so printing it here would
            # show it twice.
            return ToolOk(output=f"{success_message}\n\n{output}", brief="Python code executed successfully")
