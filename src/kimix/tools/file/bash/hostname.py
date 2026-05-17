"""hostname tool - show or set system hostname."""
import os
import platform
import socket

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Hostname(CallableTool2[Params]):
    name: str = "Hostname"
    description: str = "Show or set system hostname."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            args = [arg for arg in params.args if not arg.startswith("-")]
            if args:
                # Setting hostname is platform-specific and usually requires privileges
                new_name = args[0]
                if os.name == "nt":
                    import ctypes
                    ret = ctypes.windll.kernel32.SetComputerNameExW(1, new_name)
                    if not ret:
                        return ToolError(message="hostname: permission denied", output="", brief="permission denied")
                else:
                    try:
                        with open("/etc/hostname", "w") as f:
                            f.write(new_name + "\n")
                    except OSError as e:
                        return ToolError(message=f"hostname: {e}", output="", brief="hostname failed")
                output = ""
            else:
                output = socket.gethostname()

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
            return ToolError(message=str(e), output="", brief="hostname failed")
