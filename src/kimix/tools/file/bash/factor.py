"""factor tool - factor numbers."""
import os
import math

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Factor(CallableTool2[Params]):
    name: str = "Factor"
    description: str = "Factor numbers."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            numbers = [arg for arg in params.args if not arg.startswith("-")]
            if not numbers:
                return ToolError(message="factor: missing operand", output="", brief="missing operand")

            results = []
            for n_str in numbers:
                n = int(n_str)
                factors = []
                temp = n
                d = 2
                while d * d <= temp:
                    while temp % d == 0:
                        factors.append(str(d))
                        temp //= d
                    d += 1
                if temp > 1:
                    factors.append(str(temp))
                results.append(f"{n}: {' '.join(factors)}")

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
            return ToolError(message=str(e), output="", brief="factor failed")
