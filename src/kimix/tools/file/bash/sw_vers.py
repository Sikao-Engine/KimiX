"""sw_vers tool - print macOS version information."""
import platform

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class SwVers(CallableTool2[Params]):
    name: str = "SwVers"
    description: str = "Print macOS version information."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            ver = platform.mac_ver()
            output_lines = [
                f"ProductName:\t\tmacOS",
                f"ProductVersion:\t\t{ver[0] or 'Unknown'}",
                f"BuildVersion:\t\t{ver[2] or 'Unknown'}",
            ]
            output = "\n".join(output_lines)
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
            return ToolError(message=str(e), output="", brief="sw_vers failed")
