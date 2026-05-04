"""xz tool - compress files."""
import lzma
import os
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params

from kimix.tools.common import _maybe_export_output_async

class Xz(CallableTool2[Params]):
    name: str = "Xz"
    description: str = "Compress files using xz."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            keep = False
            decompress = False
            paths = []
            for arg in params.args:
                if arg in ("-k", "--keep"):
                    keep = True
                elif arg in ("-d", "--decompress"):
                    decompress = True
                elif not arg.startswith("-"):
                    paths.append(arg)

            if not paths:
                return ToolError(message="xz: missing file operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            errors = []
            for p in paths:
                target = Path(cwd) / p if not Path(p).is_absolute() else Path(p)
                try:
                    if decompress:
                        out_path = target.with_suffix("") if target.suffix == ".xz" else target.parent / (target.name + ".decompressed")
                        with lzma.open(target, "rb") as src:
                            data = src.read()
                        with open(out_path, "wb") as dst:
                            dst.write(data)
                        if not keep:
                            target.unlink()
                    else:
                        out_path = target.parent / (target.name + ".xz")
                        with open(target, "rb") as src:
                            data = src.read()
                        with lzma.open(out_path, "wb") as dst:
                            dst.write(data)
                        if not keep:
                            target.unlink()
                except FileNotFoundError:
                    errors.append(f"xz: {p}: No such file or directory")
                except OSError as e:
                    errors.append(f"xz: {p}: {e}")

            if errors:
                output = "\n".join(errors)
                if params.output_path:
                    with open(params.output_path, "w", encoding="utf-8") as f:
                        f.write(output)
                    output = f"saved to file `{params.output_path}`"
                return ToolError(message=output, output=output, brief="xz failed")

            output = ""
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="xz failed")
