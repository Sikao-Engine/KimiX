"""lsof tool - list open files."""
import os

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Lsof(CallableTool2[Params]):
    name: str = "Lsof"
    description: str = "List open files."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            pid = None
            for arg in params.args:
                if arg.startswith("-p"):
                    pid = arg[2:]
                elif not arg.startswith("-"):
                    pid = arg

            cwd = params.cwd or os.getcwd()
            if params.output_path:
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")

            if os.name == "nt":
                output = "lsof: not fully supported on Windows without additional libraries"
            else:
                if pid:
                    fd_dir = f"/proc/{pid}/fd"
                    try:
                        entries = os.listdir(fd_dir)
                    except OSError as e:
                        return ToolError(message=f"lsof: {e}", output="", brief="lsof failed")
                    lines = []
                    for entry in entries:
                        try:
                            target = os.readlink(os.path.join(fd_dir, entry))
                            lines.append(f"{pid}\t{entry}\t{target}")
                        except OSError:
                            pass
                    output = "\n".join(lines)
                else:
                    output = "lsof: no pid specified (simplified implementation)"

            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="lsof failed")
