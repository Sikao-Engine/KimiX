"""nslookup tool - query DNS servers."""
import socket

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Nslookup(CallableTool2[Params]):
    name: str = "Nslookup"
    description: str = "Query DNS servers."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            name = None
            for arg in params.args:
                if not arg.startswith("-"):
                    name = arg
            if name is None:
                return ToolError(message="nslookup: missing operand", output="", brief="missing operand")

            try:
                addr = socket.getaddrinfo(name, None)[0][4][0]
                output = f"Server:\t\t{socket.gethostname()}\nAddress:\t127.0.0.1\n\nName:\t{name}\nAddress: {addr}"
            except socket.gaierror as e:
                return ToolError(message=f"nslookup: {name} not found: {e}", output="", brief="nslookup failed")

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
            return ToolError(message=str(e), output="", brief="nslookup failed")
