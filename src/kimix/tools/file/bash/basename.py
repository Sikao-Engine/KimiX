"""basename tool - strip directory and suffix from filenames."""
import os

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


def _basename(path: str) -> str:
    """Fast basename matching GNU basename behavior."""
    # Strip trailing separators (GNU basename strips trailing slashes)
    i = len(path)
    while i > 1 and path[i - 1] in ("/", "\\"):
        i -= 1
    path = path[:i]

    sep = os.sep
    idx = path.rfind(sep)
    if idx == -1 and sep != "/":
        idx = path.rfind("/")
    if idx == -1:
        return path
    if idx == 0 and len(path) == 1:
        return path
    return path[idx + 1:]


class Basename(CallableTool2[Params]):
    name: str = "Basename"
    description: str = "Strip directory and suffix from filenames."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        args = params.args
        if not args:
            return ToolError(message="basename: missing operand", output="", brief="missing operand")

        path = args[0]
        suffix = args[1] if len(args) > 1 else None

        name = _basename(path)
        if suffix and name.endswith(suffix) and len(name) > len(suffix):
            name = name[: -len(suffix)]

        if params.output_path:
            cwd = params.cwd or os.getcwd()
            is_prot, reason = _is_protected_path(params.output_path, cwd)
            if is_prot:
                return ToolError(message=reason, output=reason, brief="protected path")
            with open(params.output_path, "w", encoding="utf-8") as f:
                f.write(name)
            return ToolOk(output=f"saved to file `{params.output_path}`")

        output = await _maybe_export_output_async(name)
        return ToolOk(output=output)
