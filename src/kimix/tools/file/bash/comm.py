"""comm tool - compare two sorted files line by line."""
import os
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Comm(CallableTool2[Params]):
    name: str = "Comm"
    description: str = "Compare two sorted files line by line."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            col1 = True
            col2 = True
            col3 = True
            paths = []
            for arg in params.args:
                if arg == "-1":
                    col1 = False
                elif arg == "-2":
                    col2 = False
                elif arg == "-3":
                    col3 = False
                elif not arg.startswith("-"):
                    paths.append(arg)

            if len(paths) < 2:
                return ToolError(message="comm: missing operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            p1 = Path(cwd) / paths[0] if not Path(paths[0]).is_absolute() else Path(paths[0])
            p2 = Path(cwd) / paths[1] if not Path(paths[1]).is_absolute() else Path(paths[1])

            try:
                with open(p1, "r", encoding="utf-8", errors="replace") as f:
                    lines1 = f.read().splitlines()
                with open(p2, "r", encoding="utf-8", errors="replace") as f:
                    lines2 = f.read().splitlines()
            except FileNotFoundError as e:
                return ToolError(message=f"comm: {e.filename}: No such file or directory", output="", brief="comm failed")
            except OSError as e:
                return ToolError(message=f"comm: {e}", output="", brief="comm failed")

            i = j = 0
            out_lines = []
            while i < len(lines1) and j < len(lines2):
                a = lines1[i]
                b = lines2[j]
                if a == b:
                    if col3:
                        out_lines.append(f"\t\t{a}")
                    i += 1
                    j += 1
                elif a < b:
                    if col1:
                        out_lines.append(a)
                    i += 1
                else:
                    if col2:
                        out_lines.append(f"\t{b}")
                    j += 1
            while i < len(lines1):
                if col1:
                    out_lines.append(lines1[i])
                i += 1
            while j < len(lines2):
                if col2:
                    out_lines.append(f"\t{lines2[j]}")
                j += 1

            output = "\n".join(out_lines)
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
            return ToolError(message=str(e), output="", brief="comm failed")
