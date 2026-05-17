"""csplit tool - split a file into sections determined by context lines."""
import os
import re
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Csplit(CallableTool2[Params]):
    name: str = "Csplit"
    description: str = "Split a file into sections determined by context lines."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            prefix = "xx"
            regex = None
            path = None
            i = 0
            while i < len(params.args):
                arg = params.args[i]
                if arg == "-f":
                    i += 1
                    if i < len(params.args):
                        prefix = params.args[i]
                elif arg.startswith("-f"):
                    prefix = arg[2:]
                elif not arg.startswith("-"):
                    if path is None:
                        path = arg
                    elif regex is None:
                        regex = arg
                i += 1

            if path is None or regex is None:
                return ToolError(message="csplit: missing operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            target = Path(cwd) / path if not Path(path).is_absolute() else Path(path)
            try:
                with open(target, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
            except FileNotFoundError:
                return ToolError(message=f"csplit: {path}: No such file or directory", output="", brief="csplit failed")
            except OSError as e:
                return ToolError(message=f"csplit: {path}: {e}", output="", brief="csplit failed")

            pattern = re.compile(regex)
            chunks = []
            current = []
            for line in lines:
                if pattern.search(line):
                    if current:
                        chunks.append(current)
                        current = []
                current.append(line)
            if current:
                chunks.append(current)

            suffix = "00"
            count = 0
            for chunk in chunks:
                out_name = f"{prefix}{suffix}"
                out_path = Path(cwd) / out_name
                with open(out_path, "w", encoding="utf-8") as f:
                    f.writelines(chunk)
                count += 1
                # increment numeric suffix
                suffix = str(int(suffix) + 1).zfill(len(suffix))

            output = f"csplit into {count} files with prefix '{prefix}'"
            if params.output_path:
                cwd = params.cwd or os.getcwd()
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="csplit failed")
