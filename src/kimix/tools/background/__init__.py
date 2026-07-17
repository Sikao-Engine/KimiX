"""Background task management tools."""
import sys
import asyncio

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from pydantic import BaseModel, Field
from kimi_cli.session import Session

from .utils import generate_task_id, remove_task_id, add_task, get_all_tasks, BackgroundStream, discard_all_tasks
from kimix.tools.common import _maybe_export_output_async, _export_to_temp_file_async
from kimi_cli.tools.display import BackgroundTaskDisplayBlock


class TaskOutputParams(BaseModel):
    """Parameters for TaskOutput."""
    task_id: str | None = Field(
        default=None,
        description="task id"
    )
    block: bool = Field(
        default=True,
        description='block and wait task.'
    )
    timeout: int | None = Field(
        default=None,
        ge=3,
        le=7200,
        description="Timeout in seconds. Defaults to 60 when `kill` is False, or 0 when `kill` is True. When blocking, if no stdout/stderr output is received for longer than min(900, timeout) seconds, the current output is returned immediately."
    )
    output_path: str | None = Field(
        default=None,
        description="Output file path."
    )
    kill: bool = Field(
        default=False,
        description="Force stop the process after timeout."
    )


class TaskOutput(CallableTool2):
    """Get output from a background task, or list all tasks if no task_id is provided."""
    name: str = "TaskOutput"
    description: str = "Get background task output or list tasks."
    params: type[BaseModel] = TaskOutputParams

    def __del__(self):
        if sys.is_finalizing():
            return
        session = getattr(self, '_session', None)
        if session is not None:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(discard_all_tasks(session))
            except RuntimeError:
                try:
                    asyncio.run(discard_all_tasks(session))
                except:
                    pass

    def __init__(self, session: Session):
        super().__init__()
        self._session = session

    async def __call__(self, params: TaskOutputParams) -> ToolReturnValue:
        """Return the output of a task_id, or list all tasks if task_id is None."""
        try:
            tasks = get_all_tasks(self._session)

            async def _list_started() -> list[str]:
                """Return list of started task IDs."""
                lines = []
                for task_id, stream in tasks.items():
                    if await stream.is_started():
                        lines.append(task_id)
                return lines

            if params.task_id is None:
                if not tasks:
                    return ToolOk(output="No running task", brief="No background tasks")
                started = await _list_started()
                task_list = ", ".join(started) if started else "No running task"
                return ToolOk(output=task_list, brief="Background tasks listed")

            stream: BackgroundStream | None = tasks.get(params.task_id.strip())
            if stream is None:
                started = await _list_started()
                if not started:
                    return ToolError(
                        message="No running task",
                        output="",
                        brief="No running task"
                    )
                task_list = ", ".join(started)
                return ToolError(
                    message=f"Task '{params.task_id}' not found. Available tasks: [{task_list}]",
                    output="",
                    brief=f"Task '{params.task_id}' not found"
                )
            timeout = params.timeout
            if timeout is None:
                timeout = 0 if params.kill else 60
            inactivity_timed_out = False
            if params.block:
                inactivity_timeout = min(900, timeout)
                completed, _elapsed, inactivity_timed_out = await stream.wait_with_inactivity_timeout(
                    timeout, inactivity_timeout
                )
                task_alive = not completed
            else:
                task_alive = await stream.thread_is_alive()
            if params.kill and task_alive:
                await stream.stop()
                task_alive = False
            # Use pop_output to ensure each call returns only new data
            # since the last call, avoiding repeated output.
            output = await stream.pop_output()
            if not task_alive:
                remove_task_id(self._session, params.task_id)
                # If the process failed (non-zero return), return error message
                if not await stream.success():
                    elapsed = stream.process_elapsed
                    msg = output if output else "Task process failed (non-zero exit)"
                    if elapsed is not None:
                        msg += f" ({elapsed:.1f}s)"
                    return ToolError(
                        message=msg,
                        output=output if output else "",
                        brief=f"Task '{params.task_id}' failed"
                    )
            if params.output_path:
                from pathlib import Path
                import anyio
                path = Path(params.output_path)
                async with await anyio.open_file(path, 'w', encoding='utf-8') as f:
                    await f.write(output)
                display_path = str(path).replace("\\", "/")
                output = f"{f'`{params.task_id}` is still running, call `TaskOutput` again, ' if task_alive else ''}output exported to file `{display_path}`"
            else:
                output = await _maybe_export_output_async(output)
            kind = params.task_id.split("_")[0] if params.task_id else "task"
            status = "running" if task_alive else "completed"
            output_text = output if output else "(no output)"
            if not task_alive:
                elapsed = stream.process_elapsed
                if elapsed is not None:
                    output_text += f"\n[Process completed in {elapsed:.2f}s]"
            return ToolOk(
                output=output_text,
                brief="Task output retrieved",
                display_block=BackgroundTaskDisplayBlock(
                    task_id=params.task_id,
                    kind=kind,
                    status=status,
                    description=output_text[:200] if output_text else "(no output)",
                ),
            )
        except Exception as e:
            return ToolError(
                message=str(e),
                output="Failed to get task output",
                brief="Task output error"
            )


__all__ = [
    # Tool classes
    "TaskOutput",
    "TaskOutputParams",
    # Utility functions
    "generate_task_id",
    "remove_task_id",
    "add_task",
    "get_all_tasks",
]
