"""fmt tool - simple optimal text formatter."""
import os
import textwrap
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Fmt(CallableTool2[Params]):
    name: str = "Fmt"
    description: str = "Simple optimal text formatter."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            width = 75
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
                return ToolError(message="fmt: missing operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            results = []
            errors = []
            for p in paths:
                target = Path(cwd) / p if not Path(p).is_absolute() else Path(p)
                try:
                    with open(target, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read()
                    paragraphs = text.split("\n\n")
                    formatted = []
                    for para in paragraphs:
                        lines = para.splitlines()
                        para_text = " ".join(line.strip() for line in lines)
                        wrapped = textwrap.fill(para_text, width=width)
                        formatted.append(wrapped)
                    results.append("\n\n".join(formatted))
                except FileNotFoundError:
                    errors.append(f"fmt: {p}: No such file or directory")
                except OSError as e:
                    errors.append(f"fmt: {p}: {e}")

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
                return ToolError(message=output, output=output, brief="fmt failed")

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
            return ToolError(message=str(e), output="", brief="fmt failed")
