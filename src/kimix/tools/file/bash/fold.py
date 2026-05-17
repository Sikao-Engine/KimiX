"""fold tool - wrap each input line to fit in specified width."""
import os
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Fold(CallableTool2[Params]):
    name: str = "Fold"
    description: str = "Wrap each input line to fit in specified width."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            width = 80
            paths = []
            i = 0
            while i < len(params.args):
                arg = params.args[i]
                if arg == "-w" or arg == "--width":
                    i += 1
                    if i < len(params.args):
                        width = int(params.args[i])
                elif arg.startswith("-w"):
                    width = int(arg[2:])
                elif arg.startswith("--width="):
                    width = int(arg.split("=", 1)[1])
                elif not arg.startswith("-"):
                    paths.append(arg)
                i += 1

            if not paths:
                return ToolError(message="fold: missing operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            results = []
            errors = []
            for p in paths:
                target = Path(cwd) / p if not Path(p).is_absolute() else Path(p)
                try:
                    with open(target, "r", encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                    folded = []
                    for line in lines:
                        line = line.rstrip("\n")
                        while len(line) > width:
                            folded.append(line[:width])
                            line = line[width:]
                        folded.append(line)
                    results.append("\n".join(folded))
                except FileNotFoundError:
                    errors.append(f"fold: {p}: No such file or directory")
                except OSError as e:
                    errors.append(f"fold: {p}: {e}")

            if errors:
                output = "\n".join(errors)
                if params.output_path:
                    cwd = params.cwd or os.getcwd()
                    is_prot, reason = _is_protected_path(params.output_path, cwd)
                    if is_prot:
                        return ToolError(message=reason, output=reason, brief="protected path")
                    with open(params.output_path, "w", encoding="utf-8") as f:
                        f.write(output)
                    output = f"saved to file `{params.output_path}`"
                return ToolError(message=output, output=output, brief="fold failed")

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
            return ToolError(message=str(e), output="", brief="fold failed")
