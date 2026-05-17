"""type tool - display information about command type."""
import os
import shutil

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Type(CallableTool2[Params]):
    name: str = "Type"
    description: str = "Display information about command type."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            args = [arg for arg in params.args if not arg.startswith("-")]
            if not args:
                return ToolError(message="type: missing operand", output="", brief="missing operand")

            # Import here to avoid circular dependency at module load time
            from kimix.tools.file.run import _BASH_COMMANDS

            results = []
            for cmd in args:
                if cmd in _BASH_COMMANDS:
                    results.append(f"{cmd} is a shell builtin")
                else:
                    path = shutil.which(cmd)
                    if path:
                        results.append(f"{cmd} is {path}")
                    else:
                        results.append(f"bash: type: {cmd}: not found")

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
            return ToolError(message=str(e), output="", brief="type failed")
