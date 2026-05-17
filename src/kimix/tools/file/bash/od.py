"""od tool - dump files in octal and other formats."""
import os
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Od(CallableTool2[Params]):
    name: str = "Od"
    description: str = "Dump files in octal and other formats."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            fmt = "o"
            paths = []
            for arg in params.args:
                if arg == "-x":
                    fmt = "x"
                elif arg == "-d":
                    fmt = "d"
                elif arg == "-c":
                    fmt = "c"
                elif arg == "-o":
                    fmt = "o"
                elif not arg.startswith("-"):
                    paths.append(arg)

            if not paths:
                return ToolError(message="od: missing operand", output="", brief="missing operand")

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
                return ToolError(message=f"od: {paths[0]}: No such file or directory", output="", brief="od failed")

            lines = []
            if fmt == "c":
                for i in range(0, len(data), 16):
                    chunk = data[i:i + 16]
                    chars = " ".join(repr(chr(b)) if 32 <= b < 127 else f"\\{b:03o}" for b in chunk)
                    lines.append(f"{i:07o}  {chars}")
            else:
                word_size = 2 if fmt in ("x", "d") else 1
                for i in range(0, len(data), 16):
                    chunk = data[i:i + 16]
                    if fmt == "x":
                        words = [chunk[j:j + 2].hex() for j in range(0, len(chunk), 2)]
                    elif fmt == "d":
                        words = [str(int.from_bytes(chunk[j:j + 2], "big")) for j in range(0, len(chunk), 2)]
                    else:
                        words = [f"{b:03o}" for b in chunk]
                    lines.append(f"{i:07o}  {' '.join(words)}")

            output = "\n".join(lines)
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="od failed")
