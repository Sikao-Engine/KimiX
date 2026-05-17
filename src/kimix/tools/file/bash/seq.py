"""seq tool - print a sequence of numbers."""
import os

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Seq(CallableTool2[Params]):
    name: str = "Seq"
    description: str = "Print a sequence of numbers."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            args = [arg for arg in params.args if not arg.startswith("-")]
            if not args:
                return ToolError(message="seq: missing operand", output="", brief="missing operand")

            if len(args) == 1:
                first, increment, last = 1.0, 1.0, float(args[0])
            elif len(args) == 2:
                first, increment, last = float(args[0]), 1.0, float(args[1])
            else:
                first, increment, last = float(args[0]), float(args[1]), float(args[2])

            result = []
            current = first
            # Determine number of decimal places for formatting
            decimals = max(
                str(arg).split(".")[1] if "." in str(arg) else "" for arg in args
            )
            dec_len = len(decimals)
            while (increment > 0 and current <= last) or (increment < 0 and current >= last):
                if dec_len > 0:
                    result.append(f"{current:.{dec_len}f}")
                else:
                    result.append(str(int(current)))
                current += increment

            output = "\n".join(result)
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
            return ToolError(message=str(e), output="", brief="seq failed")
