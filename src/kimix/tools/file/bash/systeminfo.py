"""systeminfo tool - display Windows system configuration."""
import os
import platform

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Systeminfo(CallableTool2[Params]):
    name: str = "Systeminfo"
    description: str = "Display Windows system configuration."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            uname = platform.uname()
            output_lines = [
                f"Host Name:\t\t{uname.node}",
                f"OS Name:\t\t{uname.system}",
                f"OS Version:\t\t{uname.release}",
                f"OS Manufacturer:\t\t{uname.system}",
                f"OS Configuration:\t\tStandalone Workstation",
                f"OS Build Type:\t\tMultiprocessor Free",
                f"Registered Owner:\t\t{os.getlogin() if hasattr(os, 'getlogin') else 'N/A'}",
                f"System Type:\t\t{uname.machine}",
                f"Processor(s):\t\t{uname.processor or 'N/A'}",
                f"Boot Device:\t\t\\Device\\HarddiskVolume1",
                f"System Locale:\t\t{locale.getdefaultlocale()[0] if 'locale' in globals() else 'en-US'}",
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
            return ToolError(message=str(e), output="", brief="systeminfo failed")
