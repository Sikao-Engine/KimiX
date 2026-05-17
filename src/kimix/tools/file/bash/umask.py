"""umask tool - get or set the file mode creation mask."""
import os

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Umask(CallableTool2[Params]):
    name: str = "Umask"
    description: str = "Get or set the file mode creation mask."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            args = [arg for arg in params.args if not arg.startswith("-")]
            if args:
                new_mask = int(args[0], 8)
                old = os.umask(new_mask)
                output = f"{old:04o}"
            else:
                current = os.umask(0)
                os.umask(current)
                output = f"{current:04o}"

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
            return ToolError(message=str(e), output="", brief="umask failed")
