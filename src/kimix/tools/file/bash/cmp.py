"""cmp tool - compare two files byte by byte."""
import os
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Cmp(CallableTool2[Params]):
    name: str = "Cmp"
    description: str = "Compare two files byte by byte."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            silent = False
            paths = []
            for arg in params.args:
                if arg == "-s" or arg == "--silent" or arg == "--quiet":
                    silent = True
                elif not arg.startswith("-"):
                    paths.append(arg)

            if len(paths) < 2:
                return ToolError(message="cmp: missing operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            p1 = Path(cwd) / paths[0] if not Path(paths[0]).is_absolute() else Path(paths[0])
            p2 = Path(cwd) / paths[1] if not Path(paths[1]).is_absolute() else Path(paths[1])

            try:
                with open(p1, "rb") as f1, open(p2, "rb") as f2:
                    byte_num = 0
                    line_num = 1
                    while True:
                        b1 = f1.read(1)
                        b2 = f2.read(1)
                        if b1 != b2:
                            if not b1 or not b2:
                                if silent:
                                    return ToolOk(output="")
                                return ToolOk(output=f"cmp: EOF on {paths[0] if not b1 else paths[1]}")
                            if silent:
                                return ToolOk(output="")
                            return ToolOk(output=f"{paths[0]} {paths[1]} differ: byte {byte_num + 1}, line {line_num}")
                        if not b1:
                            break
                        byte_num += 1
                        if b1 == b"\n":
                            line_num += 1
            except FileNotFoundError as e:
                return ToolError(message=f"cmp: {e.filename}: No such file or directory", output="", brief="cmp failed")
            except OSError as e:
                return ToolError(message=f"cmp: {e}", output="", brief="cmp failed")

            output = "" if silent else ""
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
            return ToolError(message=str(e), output="", brief="cmp failed")
