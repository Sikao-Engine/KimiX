"""traceroute tool - trace the route to a host."""
import os
import socket
import struct
import time

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Traceroute(CallableTool2[Params]):
    name: str = "Traceroute"
    description: str = "Trace the route to a host."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            host = None
            for arg in params.args:
                if not arg.startswith("-"):
                    host = arg
            if host is None:
                return ToolError(message="traceroute: missing host operand", output="", brief="missing operand")

            try:
                addr = socket.getaddrinfo(host, None)[0][4][0]
            except socket.gaierror:
                return ToolError(message=f"traceroute: {host}: Name or service not known", output="", brief="traceroute failed")

            output_lines = [f"traceroute to {host} ({addr}), 30 hops max"]
            for ttl in range(1, 31):
                try:
                    recv_socket = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
                    recv_socket.settimeout(2)
                    recv_socket.bind(("", 0))
                    send_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                    send_socket.setsockopt(socket.SOL_IP, socket.IP_TTL, ttl)
                    send_socket.settimeout(2)
                    start = time.time()
                    send_socket.sendto(b"", (addr, 33434))
                    _, curr_addr = recv_socket.recvfrom(512)
                    elapsed = (time.time() - start) * 1000
                    curr_addr = curr_addr[0]
                    try:
                        curr_name = socket.gethostbyaddr(curr_addr)[0]
                    except socket.herror:
                        curr_name = curr_addr
                    output_lines.append(f"{ttl}  {curr_name} ({curr_addr})  {elapsed:.3f} ms")
                    if curr_addr == addr:
                        break
                except (PermissionError, OSError, socket.timeout):
                    output_lines.append(f"{ttl}  * * *")
                finally:
                    try:
                        send_socket.close()
                    except Exception:
                        pass
                    try:
                        recv_socket.close()
                    except Exception:
                        pass

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
            return ToolError(message=str(e), output="", brief="traceroute failed")
