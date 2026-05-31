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
    timeout: int = Field(
        default=60,
        ge=3,
        le=900,
        description="Timeout in seconds."
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
            if params.block:
                await stream.wait(params.timeout)
            task_alive = await stream.thread_is_alive()
            if params.kill and task_alive:
                await stream.stop()
                task_alive = False
            output = await stream.get_output() if task_alive else await stream.pop_output()
            if not task_alive:
                remove_task_id(self._session, params.task_id)
            if params.output_path:
                from pathlib import Path
                import anyio
                path = Path(params.output_path)
                async with await anyio.open_file(path, 'w', encoding='utf-8') as f:
                    await f.write(output)
                display_path = str(path).replace("\\", "/")
                output = f"{f'`{params.task_id}` is still running, call `TaskOutput` again, ' if task_alive else ''}output exported to file `{display_path}`"
            elif output and task_alive and not await stream.success():
                temp_path, _ = await _export_to_temp_file_async(key=None, content=output, ext='.txt')
                display_temp_path = temp_path.replace("\\", "/")
                output = f"Output exported to file `{display_temp_path}`"
            else:
                output = await _maybe_export_output_async(output)
            kind = params.task_id.split("_")[0] if params.task_id else "task"
            status = "running" if task_alive else "completed"
            return ToolOk(
                output=output if output else "(no output)",
                brief="Task output retrieved",
                display_block=BackgroundTaskDisplayBlock(
                    task_id=params.task_id,
                    kind=kind,
                    status=status,
                    description=output[:200] if output else "(no output)",
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
