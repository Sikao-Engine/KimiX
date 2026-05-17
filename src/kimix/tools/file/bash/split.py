"""split tool - split a file into pieces."""
import os
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Split(CallableTool2[Params]):
    name: str = "Split"
    description: str = "Split a file into pieces."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            lines_per_file = 1000
            prefix = "x"
            path = None
            i = 0
            while i < len(params.args):
                arg = params.args[i]
                if arg == "-l":
                    i += 1
                    if i < len(params.args):
                        lines_per_file = int(params.args[i])
                elif arg.startswith("-l"):
                    lines_per_file = int(arg[2:])
                elif not arg.startswith("-"):
                    if path is None:
                        path = arg
                    else:
                        prefix = arg
                i += 1

            if path is None:
                return ToolError(message="split: missing file operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            target = Path(cwd) / path if not Path(path).is_absolute() else Path(path)
            try:
                with open(target, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
            except FileNotFoundError:
                return ToolError(message=f"split: {path}: No such file or directory", output="", brief="split failed")
            except OSError as e:
                return ToolError(message=f"split: {path}: {e}", output="", brief="split failed")

            suffix = "aa"
            count = 0
            for start in range(0, len(lines), lines_per_file):
                chunk = lines[start:start + lines_per_file]
                out_name = f"{prefix}{suffix}"
                out_path = Path(cwd) / out_name
                with open(out_path, "w", encoding="utf-8") as f:
                    f.writelines(chunk)
                count += 1
                # increment suffix: aa -> ab -> ... -> az -> ba -> ...
                chars = list(suffix)
                for j in range(len(chars) - 1, -1, -1):
                    if chars[j] == "z":
                        chars[j] = "a"
                    else:
                        chars[j] = chr(ord(chars[j]) + 1)
                        break
                suffix = "".join(chars)

            output = f"split into {count} files with prefix '{prefix}'"
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
            return ToolError(message=str(e), output="", brief="split failed")
