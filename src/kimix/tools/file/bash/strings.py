"""strings tool - print the strings of printable characters in files."""
import os
import re
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Strings(CallableTool2[Params]):
    name: str = "Strings"
    description: str = "Print the strings of printable characters in files."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            min_len = 4
            paths = []
            i = 0
            while i < len(params.args):
                arg = params.args[i]
                if arg == "-n":
                    i += 1
                    if i < len(params.args):
                        min_len = int(params.args[i])
                elif arg.startswith("-n"):
                    min_len = int(arg[2:])
                elif not arg.startswith("-"):
                    paths.append(arg)
                i += 1

            if not paths:
                return ToolError(message="strings: missing operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            if params.output_path:
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")


            results = []
            errors = []
            for p in paths:
                target = Path(cwd) / p if not Path(p).is_absolute() else Path(p)
                try:
                    with open(target, "rb") as f:
                        data = f.read()
                    # Find printable sequences
                    pattern = re.compile(rb"[\x20-\x7e]{" + str(min_len).encode() + rb",}")
                    for match in pattern.finditer(data):
                        results.append(match.group().decode("ascii"))
                except FileNotFoundError:
                    errors.append(f"strings: {p}: No such file or directory")
                except OSError as e:
                    errors.append(f"strings: {p}: {e}")

            if errors:
                output = "\n".join(errors)
                if params.output_path:
                    with open(params.output_path, "w", encoding="utf-8") as f:
                        f.write(output)
                    output = f"saved to file `{params.output_path}`"
                return ToolError(message=output, output=output, brief="strings failed")

            output = "\n".join(results)
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="strings failed")
