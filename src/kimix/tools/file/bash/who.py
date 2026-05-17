"""who tool - show who is logged on."""
import os

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Who(CallableTool2[Params]):
    name: str = "Who"
    description: str = "Show who is logged on."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes
                user_name = os.getlogin()
                output = f"{user_name} console"
            else:
                try:
                    import pwd
                    user = pwd.getpwuid(os.getuid()).pw_name
                except Exception:
                    user = os.getlogin()
                try:
                    with open("/var/run/utmp", "rb") as f:
                        data = f.read()
                    output = f"{user} pts/0"
                except Exception:
                    output = f"{user} pts/0"
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
            return ToolError(message=str(e), output="", brief="who failed")
