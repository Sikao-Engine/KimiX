"""unxz tool - decompress files."""
import lzma
import os
import shutil
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async

class Unxz(CallableTool2[Params]):
    name: str = "Unxz"
    description: str = "Decompress files compressed with xz."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            keep = False
            paths = []
            for arg in params.args:
                if arg in ("-k", "--keep"):
                    keep = True
                elif not arg.startswith("-"):
                    paths.append(arg)

            if not paths:
                return ToolError(message="unxz: missing file operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            if params.output_path:
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")

            for p in paths:
                is_prot, reason = _is_protected_path(p, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")

            errors = []
            for p in paths:
                target = Path(cwd) / p if not Path(p).is_absolute() else Path(p)
                try:
                    out_path = target.with_suffix("") if target.suffix == ".xz" else target.parent / (target.name + ".decompressed")
                    with lzma.open(target, "rb") as src, open(out_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    if not keep:
                        target.unlink()
                except FileNotFoundError:
                    errors.append(f"unxz: {p}: No such file or directory")
                except OSError as e:
                    errors.append(f"unxz: {p}: {e}")

            if errors:
                output = "\n".join(errors)
                if params.output_path:
                    with open(params.output_path, "w", encoding="utf-8") as f:
                        f.write(output)
                    output = f"saved to file `{params.output_path}`"
                return ToolError(message=output, output=output, brief="unxz failed")

            output = ""
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="unxz failed")
