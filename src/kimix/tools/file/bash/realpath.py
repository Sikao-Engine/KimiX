"""realpath tool - print the resolved path."""
import os

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Realpath(CallableTool2[Params]):
    name: str = "Realpath"
    description: str = "Print the resolved absolute file path."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            paths = []
            for arg in params.args:
                if not arg.startswith("-"):
                    paths.append(arg)

            if not paths:
                return ToolError(message="realpath: missing operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            if params.output_path:
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")


            results = []

            for p in paths:
                target = os.path.join(cwd, p) if not os.path.isabs(p) else p
                try:
                    resolved = os.path.realpath(target, strict=True)
                    results.append(resolved)
                except FileNotFoundError:
                    results.append(f"realpath: {p}: No such file or directory")
                except OSError as e:
                    results.append(f"realpath: {p}: {e}")

            output = "\n".join(results)
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="realpath failed")
