"""ss tool - investigate sockets."""
import os
import socket

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Ss(CallableTool2[Params]):
    name: str = "Ss"
    description: str = "Investigate sockets."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            if os.name == "nt":
                import subprocess
                result = subprocess.run(["netstat", "-an"], capture_output=True, text=True, encoding="utf-8", errors="replace")
                output = result.stdout
            else:
                try:
                    with open("/proc/net/tcp", "r") as f:
                        lines = f.readlines()
                    output_lines = ["State   Recv-Q  Send-Q   Local Address:Port   Peer Address:Port"]
                    for line in lines[1:]:
                        parts = line.split()
                        if len(parts) >= 4:
                            local = parts[1]
                            remote = parts[2]
                            state = parts[3]
                            output_lines.append(f"{state}  0       0        {local}  {remote}")
                    output = "\n".join(output_lines)
                except Exception:
                    output = "ss: socket information unavailable"

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
            return ToolError(message=str(e), output="", brief="ss failed")
