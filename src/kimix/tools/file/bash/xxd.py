"""xxd tool - make a hexdump or do the reverse."""
import os
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Xxd(CallableTool2[Params]):
    name: str = "Xxd"
    description: str = "Make a hexdump or do the reverse."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            reverse = False
            paths = []
            for arg in params.args:
                if arg == "-r" or arg == "-revert":
                    reverse = True
                elif not arg.startswith("-"):
                    paths.append(arg)

            if not paths:
                return ToolError(message="xxd: missing operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            if params.output_path:
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")


            target = Path(cwd) / paths[0] if not Path(paths[0]).is_absolute() else Path(paths[0])

            if reverse:
                try:
                    with open(target, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read()
                except FileNotFoundError:
                    return ToolError(message=f"xxd: {paths[0]}: No such file or directory", output="", brief="xxd failed")
                import re
                hex_str = re.sub(r"[^0-9a-fA-F]", "", text)
                try:
                    data = bytes.fromhex(hex_str)
                except ValueError:
                    return ToolError(message="xxd: invalid hex data", output="", brief="xxd failed")
                output = data.decode("utf-8", errors="replace")
            else:
                try:
                    with open(target, "rb") as f:
                        data = f.read()
                except FileNotFoundError:
                    return ToolError(message=f"xxd: {paths[0]}: No such file or directory", output="", brief="xxd failed")
                lines = []
                for i in range(0, len(data), 16):
                    chunk = data[i:i + 16]
                    hex_part = " ".join(f"{b:02x}" for b in chunk)
                    hex_part = hex_part.ljust(48)
                    ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
                    lines.append(f"{i:08x}: {hex_part}  {ascii_part}")
                output = "\n".join(lines)

            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="xxd failed")
