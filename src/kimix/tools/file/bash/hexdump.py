"""hexdump tool - display file contents in hex."""
import os
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Hexdump(CallableTool2[Params]):
    name: str = "Hexdump"
    description: str = "Display file contents in hex."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            canonical = False
            paths = []
            for arg in params.args:
                if arg == "-C":
                    canonical = True
                elif not arg.startswith("-"):
                    paths.append(arg)

            if not paths:
                return ToolError(message="hexdump: missing operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            if params.output_path:
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")


            target = Path(cwd) / paths[0] if not Path(paths[0]).is_absolute() else Path(paths[0])
            try:
                with open(target, "rb") as f:
                    data = f.read()
            except FileNotFoundError:
                return ToolError(message=f"hexdump: {paths[0]}: No such file or directory", output="", brief="hexdump failed")

            lines = []
            for i in range(0, len(data), 16):
                chunk = data[i:i + 16]
                hex_part = " ".join(f"{b:02x}" for b in chunk)
                hex_part = hex_part.ljust(48)
                if canonical:
                    ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
                    lines.append(f"{i:08x}  {hex_part}  |{ascii_part}|")
                else:
                    lines.append(f"{i:08x}  {hex_part}")

            output = "\n".join(lines)
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="hexdump failed")
