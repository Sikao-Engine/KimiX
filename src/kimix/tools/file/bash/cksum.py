"""cksum tool - compute CRC checksum and byte count."""
import os
import zlib
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Cksum(CallableTool2[Params]):
    name: str = "Cksum"
    description: str = "Compute CRC checksum and byte count."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            paths = [arg for arg in params.args if not arg.startswith("-")]
            if not paths:
                return ToolError(message="cksum: missing operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            results = []
            errors = []
            for p in paths:
                target = Path(cwd) / p if not Path(p).is_absolute() else Path(p)
                try:
                    c = 0
                    size = 0
                    with open(target, "rb") as f:
                        while chunk := f.read(8192):
                            c = zlib.crc32(chunk, c)
                            size += len(chunk)
                    c = c & 0xFFFFFFFF
                    results.append(f"{c} {size} {p}")
                except FileNotFoundError:
                    errors.append(f"cksum: {p}: No such file or directory")
                except OSError as e:
                    errors.append(f"cksum: {p}: {e}")

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
                return ToolError(message=output, output=output, brief="cksum failed")

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
            return ToolError(message=str(e), output="", brief="cksum failed")
