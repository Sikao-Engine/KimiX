"""file tool - determine file type."""
import os
import stat
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async

def _guess_type(p: Path) -> str:
    try:
        st = os.lstat(p)
    except OSError:
        return "cannot open (No such file or directory)"

    mode = st.st_mode
    if stat.S_ISLNK(mode):
        try:
            target = p.readlink()
            return f"symbolic link to {target}"
        except OSError:
            return "broken symbolic link"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISFIFO(mode):
        return "fifo (named pipe)"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISBLK(mode):
        return "block special"
    if stat.S_ISCHR(mode):
        return "character special"

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
            if params.output_path:
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")


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
