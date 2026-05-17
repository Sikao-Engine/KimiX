"""zip tool - package and compress files."""
import os
import zipfile
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async

class Zip(CallableTool2[Params]):
    name: str = "Zip"
    description: str = "Package and compress files into a zip archive."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            recursive = False
            paths = []
            for arg in params.args:
                if arg == "-r":
                    recursive = True
                elif not arg.startswith("-"):
                    paths.append(arg)

            if len(paths) < 2:
                return ToolError(message="zip: missing operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            if params.output_path:
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")

            for p in paths:
                is_prot, reason = _is_protected_path(p, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")

            cwd_path = Path(cwd)
            archive_raw = paths[0]
            archive = cwd_path / archive_raw if not Path(archive_raw).is_absolute() else Path(archive_raw)
            sources = paths[1:]

            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
                for p in sources:
                    p_path = Path(p)
                    target = cwd_path / p if not p_path.is_absolute() else p_path
                    if target.is_dir() and recursive:
                        parent = target.parent
                        for f in target.rglob("*"):
                            zf.write(f, f.relative_to(parent))
                    else:
                        zf.write(target, arcname=target.name)

            output = f"Added to {archive}"
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="zip failed")
