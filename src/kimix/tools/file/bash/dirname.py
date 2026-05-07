"""dirname tool - strip last component from file name."""
import os

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params

from kimix.tools.common import _maybe_export_output_async


class Dirname(CallableTool2[Params]):
    name: str = "Dirname"
    description: str = "Strip last component from file name."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            args = params.args
            if not args:
                return ToolError(message="dirname: missing operand", output="", brief="missing operand")

            path = args[0]
            # Strip trailing slashes to match GNU dirname behavior
            if len(path) > 1:
                path = path.rstrip("/\\")
            result = os.path.dirname(path) or "."

            output = result
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="dirname failed")
