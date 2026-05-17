"""unzip tool - list, test and extract compressed files in a ZIP archive."""
import os
import zipfile
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async

class Unzip(CallableTool2[Params]):
    name: str = "Unzip"
    description: str = "Extract files from a ZIP archive."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            list_mode = False
            paths = []
            for arg in params.args:
                if arg == "-l":
                    list_mode = True
                elif not arg.startswith("-"):
                    paths.append(arg)

            if not paths:
                return ToolError(message="unzip: missing file operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            if params.output_path:
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")

            for p in paths:
                is_prot, reason = _is_protected_path(p, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")

            archive = Path(cwd) / paths[0] if not Path(paths[0]).is_absolute() else Path(paths[0])
            dest = Path(cwd)
            if len(paths) > 1:
                dest = Path(cwd) / paths[1] if not Path(paths[1]).is_absolute() else Path(paths[1])
                dest.mkdir(parents=True, exist_ok=True)

            if not archive.exists():
                return ToolError(message=f"unzip: {paths[0]}: No such file", output="", brief="file not found")

            with zipfile.ZipFile(archive, "r") as zf:
                if list_mode:
                    results = [info.filename for info in zf.infolist()]
                    output = "\n".join(results)
                else:
                    zf.extractall(path=dest)
                    output = f"Extracted {archive} to {dest}"

            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="unzip failed")
