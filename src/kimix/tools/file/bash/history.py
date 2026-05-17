"""history tool - display or manipulate the history list."""
import os

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async

_HISTORY: list[str] = []


class History(CallableTool2[Params]):
    name: str = "History"
    description: str = "Display or manipulate the history list."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            if not params.args:
                lines = [f" {i + 1}  {cmd}" for i, cmd in enumerate(_HISTORY)]
                output = "\n".join(lines)
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

            arg = params.args[0]
            if arg == "-c":
                _HISTORY.clear()
                return ToolOk(output="")
            elif arg.startswith("-"):
                pass
            else:
                _HISTORY.append(arg)
                return ToolOk(output="")

            return ToolOk(output="")
        except Exception as e:
            return ToolError(message=str(e), output="", brief="history failed")
