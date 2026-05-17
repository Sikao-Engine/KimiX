"""uname tool - print system information."""
import os
import platform

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Uname(CallableTool2[Params]):
    name: str = "Uname"
    description: str = "Print system information."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            all_info = True
            kernel = False
            nodename = False
            kernel_release = False
            machine = False
            operating_system = False
            for arg in params.args:
                if arg == "-a" or arg == "--all":
                    all_info = True
                elif arg == "-s" or arg == "--kernel-name":
                    kernel = True
                    all_info = False
                elif arg == "-n" or arg == "--nodename":
                    nodename = True
                    all_info = False
                elif arg == "-r" or arg == "--kernel-release":
                    kernel_release = True
                    all_info = False
                elif arg == "-m" or arg == "--machine":
                    machine = True
                    all_info = False
                elif arg == "-o" or arg == "--operating-system":
                    operating_system = True
                    all_info = False

            if all_info:
                output = f"{platform.system()} {platform.node()} {platform.release()} {platform.version()} {platform.machine()} {platform.system()}"
            else:
                parts = []
                if kernel:
                    parts.append(platform.system())
                if nodename:
                    parts.append(platform.node())
                if kernel_release:
                    parts.append(platform.release())
                if machine:
                    parts.append(platform.machine())
                if operating_system:
                    parts.append(platform.system())
                if not parts:
                    parts.append(platform.system())
                output = " ".join(parts)

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
            return ToolError(message=str(e), output="", brief="uname failed")
