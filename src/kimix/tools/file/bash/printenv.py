"""printenv tool - print all or part of environment."""
import os

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async

class Printenv(CallableTool2[Params]):
    name: str = "Printenv"
    description: str = "Print all or part of environment."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            vars = [arg for arg in params.args if not arg.startswith("-")]
            if vars:
                results = []
                for v in vars:
                    val = os.environ.get(v)
                    if val is not None:
                        results.append(val)
                output = "\n".join(results)
            else:
                results = [f"{k}={v}" for k, v in sorted(os.environ.items())]
                output = "\n".join(results)
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
            return ToolError(message=str(e), output="", brief="printenv failed")
