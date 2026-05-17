"""host tool - DNS lookup utility."""
import os
import socket

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Host(CallableTool2[Params]):
    name: str = "Host"
    description: str = "DNS lookup utility."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            name = None
            for arg in params.args:
                if not arg.startswith("-"):
                    name = arg
            if name is None:
                return ToolError(message="host: missing operand", output="", brief="missing operand")

            try:
                addr = socket.getaddrinfo(name, None)[0][4][0]
                output = f"{name} has address {addr}"
            except socket.gaierror as e:
                return ToolError(message=f"host: {name} not found: {e}", output="", brief="host failed")

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
            return ToolError(message=str(e), output="", brief="host failed")
