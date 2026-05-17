"""pwd tool - print name of current/working directory."""
import os

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async

class Pwd(CallableTool2[Params]):
    name: str = "Pwd"
    description: str = "Print name of current/working directory."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            logical = True
            for arg in params.args:
                if arg == "-L":
                    logical = True
                elif arg == "-P":
                    logical = False

            if logical:
                result = params.cwd or os.getcwd()
            else:
                result = os.path.realpath(params.cwd or os.getcwd())

            output = result
            if params.output_path:
                cwd = params.cwd or os.getcwd()
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="pwd failed")
