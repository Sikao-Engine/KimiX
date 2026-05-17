"""unexpand tool - convert spaces to tabs."""
import os
import re
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Unexpand(CallableTool2[Params]):
    name: str = "Unexpand"
    description: str = "Convert spaces to tabs."
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
                return ToolError(message="unexpand: missing operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            results = []
            errors = []
            for p in paths:
                target = Path(cwd) / p if not Path(p).is_absolute() else Path(p)
                try:
                    with open(target, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read()
                    lines = text.split("\n")
                    unexpanded = []
                    for line in lines:
                        result = []
                        i = 0
                        while i < len(line):
                            if line[i] == " ":
                                j = i
                                while j < len(line) and line[j] == " ":
                                    j += 1
                                spaces = j - i
                                tabs = spaces // tabstop
                                rem = spaces % tabstop
                                result.append("\t" * tabs)
                                result.append(" " * rem)
                                i = j
                            else:
                                result.append(line[i])
                                i += 1
                        unexpanded.append("".join(result))
                    results.append("\n".join(unexpanded))
                except FileNotFoundError:
                    errors.append(f"unexpand: {p}: No such file or directory")
                except OSError as e:
                    errors.append(f"unexpand: {p}: {e}")

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
                return ToolError(message=output, output=output, brief="unexpand failed")

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
            return ToolError(message=str(e), output="", brief="unexpand failed")
