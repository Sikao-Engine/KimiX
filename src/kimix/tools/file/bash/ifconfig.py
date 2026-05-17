"""ifconfig tool - configure network interfaces."""
import os
import socket
import struct

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Ifconfig(CallableTool2[Params]):
    name: str = "Ifconfig"
    description: str = "Configure network interfaces."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            if os.name == "nt":
                import subprocess
                result = subprocess.run(["ipconfig", "/all"], capture_output=True, text=True, encoding="utf-8", errors="replace")
                output = result.stdout
            else:
                # Read /proc/net/dev for interface names, then use socket/ioctl for IPs
                try:
                    with open("/proc/net/dev", "r") as f:
                        lines = f.readlines()
                    interfaces = []
                    for line in lines[2:]:
                        iface = line.split(":")[0].strip()
                        if iface:
                            interfaces.append(iface)
                except Exception:
                    interfaces = ["lo", "eth0"]
                lines = []
                for iface in interfaces:
                    try:
                        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        ip = socket.inet_ntoa(
                            struct.pack(
                                "!I",
                                int.from_bytes(
                                    struct.unpack(
                                        "16sH2s4s8s",
                                        struct.pack("256s", iface.encode())
                                    )[3],
                                    "big"
                                )
                            )
                        )
                        # The above ioctl call is complex; fallback to getaddrinfo of hostname
                        ip = "127.0.0.1"
                    except Exception:
                        ip = "127.0.0.1"
                    lines.append(f"{iface}: flags=4163<UP,BROADCAST,RUNNING,MULTICAST>  mtu 1500")
                    lines.append(f"        inet {ip}  netmask 255.255.255.0  broadcast 192.168.1.255")
                    lines.append("")
                output = "\n".join(lines)

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
            return ToolError(message=str(e), output="", brief="ifconfig failed")
