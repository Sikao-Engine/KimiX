"""run tool for executing a process from a path."""
import anyio
import asyncio
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from pydantic import BaseModel, Field
from kimi_cli.session import Session
from kimix.tools.common import _maybe_export_output_async, _export_to_temp_file_async, ProcessTask
from kimix.tools.file.bash import (
    Awk, Basename, Bunzip2, Bzip2, Cal, Cat, Cp, Cut, Date, Df, Diff, Dirname, Du, Env, Export, File,
    Find, Grep, Gunzip, Gzip, Head, Hwclock, Ln, Ls, Man, Mkdir, Mktemp, Mv, Netstat, Printenv, Ps, Pwd,
    Realpath, Rm, Rmdir, Sed, Stat, Tac, Tail, Tar, Touch, Tr, Tree, Unxz, Uniq, Unzip, Wc, Which, Xz, Zip,
)


_BASH_COMMANDS: dict[str, CallableTool2] = {
    "awk": Awk(),
    "basename": Basename(),
    "bunzip2": Bunzip2(),
    "bzip2": Bzip2(),
    "cal": Cal(),
    "cat": Cat(),
    "cp": Cp(),
    "cut": Cut(),
    "date": Date(),
    "df": Df(),
    "diff": Diff(),
    "dirname": Dirname(),
    "du": Du(),
    "env": Env(),
    "export": Export(),
    "file": File(),
    "find": Find(),
    "grep": Grep(),
    "gunzip": Gunzip(),
    "gzip": Gzip(),
    "head": Head(),
    "hwclock": Hwclock(),
    "ln": Ln(),
    "ls": Ls(),
    "man": Man(),
    "mkdir": Mkdir(),
    "mktemp": Mktemp(),
    "mv": Mv(),
    "netstat": Netstat(),
    "printenv": Printenv(),
    "ps": Ps(),
    "pwd": Pwd(),
    "realpath": Realpath(),
    "rm": Rm(),
    "rmdir": Rmdir(),
    "sed": Sed(),
    "stat": Stat(),
    "tac": Tac(),
    "tail": Tail(),
    "tar": Tar(),
    "touch": Touch(),
    "tr": Tr(),
    "tree": Tree(),
    "unxz": Unxz(),
    "uniq": Uniq(),
    "unzip": Unzip(),
    "wc": Wc(),
    "which": Which(),
    "xz": Xz(),
    "zip": Zip(),
}


class RunParams(BaseModel):
    path: str = Field(
        description="Executable path or basic bash cmd."
    )
    args: list[str] = Field(
        default_factory=list,
        description="Command arguments."
    )
    timeout: int = Field(
        default=10,
        ge=3,
        le=180,
        description="Timeout in seconds."
    )
    cwd: str | None = Field(
        default=None,
        description="Working directory (optional)."
    )
    output_path: str | None = Field(
        default=None,
        description="Output file path (optional)."
    )

class Run(CallableTool2[RunParams]):
    name: str = "Run"
    description: str = "Run an executable."
    params: type[RunParams] = RunParams

    def __init__(self, session: Session):
        import os
        os.environ['PYTHONIOENCODING'] = 'utf-8'
        super().__init__()
        self._session = session
        self._semaphore = asyncio.Semaphore(8)

    async def _run_bash_tool(self, params: RunParams, bash_tool: CallableTool2) -> ToolReturnValue:
        import queue
        from kimix.tools.background.utils import BackgroundStream, generate_task_id, add_task, remove_task_id

        result_holder: list[ToolReturnValue] = []

        async def wrapper(q: queue.Queue[str]) -> bool:
            try:
                result = await bash_tool(params)
                result_holder.append(result)
                output_str = result.output if isinstance(result.output, str) else str(result.output)
                q.put_nowait(output_str)
                return not result.is_error
            except Exception as e:
                q.put_nowait(f"\n[Error: {str(e)}]")
                return False

        stream = BackgroundStream()
        task_id = generate_task_id(self._session, "run", params.path)
        await stream.start(wrapper, lambda: None)
        add_task(self._session, task_id, stream)

        await stream.wait(params.timeout)

        if await stream.thread_is_alive():
            output = await stream.get_output()
            return ToolError(
                output=output or f'Running in background. task_id: `{task_id}`. use `TaskOutput` or `Input`',
                message="Process timeout",
                brief="Timeout"
            )

        remove_task_id(self._session, task_id)

        if result_holder:
            return result_holder[0]

        output = await stream.pop_output()
        success = await stream.success()
        if not success:
            return ToolError(output=output, message="Command execution failed", brief="Command execution failed")
        output = await _maybe_export_output_async(output)
        return ToolOk(output=output)

    async def __call__(self, params: RunParams) -> ToolReturnValue:
        # params.path may contain arguments, split it with space, then insert to the start of params.args
        if " " in params.path:
            parts = params.path.split(" ")
            params.path = parts[0]
            params.args = parts[1:] + params.args

        async with self._semaphore:
            import sys

            bash_tool = _BASH_COMMANDS.get(params.path)
            if bash_tool:
                return await self._run_bash_tool(params, bash_tool)

            # check if using python
            if params.path == 'python':
                params.path = sys.executable
            task = ProcessTask(params.path, params.args, params.cwd)
            task_id = await task.start(self._session, "run", Path(params.path).stem)

            # Wait for completion with timeout (allow a small buffer for cleanup)
            wait_timeout = params.timeout
            await task.wait(wait_timeout)
            
            if await task.thread_is_alive():
                output = await task.stream.get_output() if task.stream else ""
                return ToolError(
                    output=output,
                    message=f"Running in background. task_id: `{task_id}`. use `TaskOutput` or `Input`",
                    brief="Timeout"
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
                    brief="Command execution failed"
                )

            output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
