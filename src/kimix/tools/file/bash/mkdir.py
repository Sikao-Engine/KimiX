"""mkdir tool - make directories."""
import os
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async

class Mkdir(CallableTool2[Params]):
    name: str = "Mkdir"
    description: str = "Make directories."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            parents = False
            dirs = []
            for arg in params.args:
                if arg == "-p" or arg == "--parents":
                    parents = True
                elif not arg.startswith("-"):
                    dirs.append(arg)

            if not dirs:
                return ToolError(message="mkdir: missing operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            if params.output_path:
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")

            for p in dirs:
                is_prot, reason = _is_protected_path(p, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")

            errors = []
            for d in dirs:
                target = Path(cwd) / d if not Path(d).is_absolute() else Path(d)
                try:
                    if parents:
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.mkdir(parents=False, exist_ok=False)
                except FileExistsError:
                    errors.append(f"mkdir: cannot create directory '{d}': File exists")
                except OSError as e:
                    errors.append(f"mkdir: cannot create directory '{d}': {e}")

            if errors:
                output = "\n".join(errors)
                if params.output_path:
                    with open(params.output_path, "w", encoding="utf-8") as f:
                        f.write(output)
                    output = f"saved to file `{params.output_path}`"
                return ToolError(message=output, output=output, brief="mkdir failed")

            output = ""
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="mkdir failed")
