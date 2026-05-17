"""expand tool - convert tabs to spaces."""
import os
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Expand(CallableTool2[Params]):
    name: str = "Expand"
    description: str = "Convert tabs to spaces."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            tabstop = 8
            paths = []
            i = 0
            while i < len(params.args):
                arg = params.args[i]
                if arg == "-t":
                    i += 1
                    if i < len(params.args):
                        tabstop = int(params.args[i])
                elif arg.startswith("-t"):
                    tabstop = int(arg[2:])
                elif not arg.startswith("-"):
                    paths.append(arg)
                i += 1

            if not paths:
                return ToolError(message="expand: missing operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            results = []
            errors = []
            for p in paths:
                target = Path(cwd) / p if not Path(p).is_absolute() else Path(p)
                try:
                    with open(target, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read()
                    lines = text.split("\n")
                    expanded_lines = []
                    for line in lines:
                        parts = line.split("\t")
                        new_line = parts[0]
                        for part in parts[1:]:
                            spaces = tabstop - (len(new_line) % tabstop)
                            new_line += " " * spaces + part
                        expanded_lines.append(new_line)
                    results.append("\n".join(expanded_lines))
                except FileNotFoundError:
                    errors.append(f"expand: {p}: No such file or directory")
                except OSError as e:
                    errors.append(f"expand: {p}: {e}")

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
                return ToolError(message=output, output=output, brief="expand failed")

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
            return ToolError(message=str(e), output="", brief="expand failed")
