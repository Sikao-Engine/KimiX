"""bc tool - arbitrary precision calculator language."""
import os
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Bc(CallableTool2[Params]):
    name: str = "Bc"
    description: str = "Arbitrary precision calculator language."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            paths = [arg for arg in params.args if not arg.startswith("-")]
            if paths:
                target = Path(params.cwd or os.getcwd()) / paths[0] if not Path(paths[0]).is_absolute() else Path(paths[0])
                try:
                    with open(target, "r", encoding="utf-8", errors="replace") as f:
                        exprs = f.read()
                except FileNotFoundError:
                    return ToolError(message=f"bc: {paths[0]}: No such file or directory", output="", brief="bc failed")
            else:
                return ToolError(message="bc: missing operand", output="", brief="missing operand")

            results = []
            for line in exprs.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    # Only support basic arithmetic and math functions
                    allowed = {
                        "sqrt": lambda x: x ** 0.5,
                        "s": lambda x: x ** 0.5,
                        "length": lambda x: len(str(int(x))),
                        "scale": lambda x: 0,
                    }
                    result = eval(line, {"__builtins__": {}}, allowed)
                    results.append(str(result))
                except Exception as e:
                    results.append(f"bc: error: {e}")

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
            return ToolError(message=str(e), output="", brief="bc failed")
