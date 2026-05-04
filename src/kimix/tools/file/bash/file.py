"""file tool - determine file type."""
import os
import stat
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from pydantic import BaseModel, Field

from kimix.tools.common import _maybe_export_output_async


class Params(BaseModel):
    path: str = Field(description="Executable path.")
    args: list[str] = Field(default_factory=list, description="Command arguments.")
    timeout: int = Field(default=10, description="Timeout in seconds.")
    cwd: str | None = Field(default=None, description="Working directory (default: current directory).")
    output_path: str | None = Field(default=None, description="Output file path (optional).")


def _guess_type(p: Path) -> str:
    if p.is_symlink():
        try:
            target = p.readlink()
            return f"symbolic link to {target}"
        except OSError:
            return "broken symbolic link"
    if p.is_dir():
        return "directory"
    if p.is_fifo():
        return "fifo (named pipe)"
    if p.is_socket():
        return "socket"
    if p.is_block_device():
        return "block special"
    if p.is_char_device():
        return "character special"
    if not p.exists():
        return "cannot open (No such file or directory)"

    # Try to detect text vs binary
    try:
        with open(p, "rb") as f:
            chunk = f.read(8192)
        if not chunk:
            return "empty"
        if b"\x00" in chunk:
            return "data"
        try:
            chunk.decode("utf-8")
            return "ASCII text"
        except UnicodeDecodeError:
            return "data"
    except OSError as e:
        return f"cannot open ({e})"


class File(CallableTool2[Params]):
    name: str = "File"
    description: str = "Determine file type."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            paths = [arg for arg in params.args if not arg.startswith("-")]
            if not paths:
                return ToolError(message="file: missing operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            results = []
            for p in paths:
                target = Path(cwd) / p if not Path(p).is_absolute() else Path(p)
                ft = _guess_type(target)
                results.append(f"{p}: {ft}")

            output = "\n".join(results)
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="file failed")
