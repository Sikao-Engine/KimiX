"""ping tool - send ICMP echo requests."""
import os
import socket
import struct
import time

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Ping(CallableTool2[Params]):
    name: str = "Ping"
    description: str = "Send ICMP echo requests."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            count = 4
            host = None
            i = 0
            while i < len(params.args):
                arg = params.args[i]
                if arg == "-c":
                    i += 1
                    if i < len(params.args):
                        count = int(params.args[i])
                elif arg.startswith("-c"):
                    count = int(arg[2:])
                elif not arg.startswith("-"):
                    host = arg
                i += 1

            if host is None:
                return ToolError(message="ping: missing operand", output="", brief="missing operand")

            try:
                addr = socket.getaddrinfo(host, None)[0][4][0]
            except socket.gaierror as e:
                return ToolError(message=f"ping: {host}: Name or service not known", output="", brief="ping failed")

            # ICMP requires raw socket, which often needs privileges.
            # Fallback to TCP connection test if ICMP fails.
            output_lines = [f"PING {host} ({addr}) 56(84) bytes of data."]
            transmitted = 0
            received = 0
            for seq in range(1, count + 1):
                transmitted += 1
                start = time.time()
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
                    sock.settimeout(2)
                    icmp_id = os.getpid() & 0xFFFF
                    header = struct.pack("!BBHHH", 8, 0, 0, icmp_id, seq)
                    checksum = 0
                    data = b"abcdefghijklmnopqrstuvwabcdefghi"
                    packet = header + data
                    # Simple checksum
                    if len(packet) % 2:
                        packet += b"\0"
                        s = sum(struct.unpack("!%dH" % (len(packet) // 2), packet))
                        s = (s >> 16) + (s & 0xFFFF)
                        s += s >> 16
                        checksum = ~s & 0xFFFF
                        header = struct.pack("!BBHHH", 8, 0, checksum, icmp_id, seq)
                        packet = header + data
                        sock.sendto(packet, (addr, 0))
                        reply, peer = sock.recvfrom(1024)
                        elapsed = (time.time() - start) * 1000
                        output_lines.append(f"64 bytes from {peer[0]}: icmp_seq={seq} ttl=64 time={elapsed:.1f} ms")
                        received += 1
                except (PermissionError, OSError):
                    # Fallback: TCP connect
                    try:
                        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        sock.settimeout(2)
                        sock.connect((addr, 80))
                        sock.close()
                        elapsed = (time.time() - start) * 1000
                        output_lines.append(f"Connected to {addr}:80 time={elapsed:.1f} ms (TCP fallback, ICMP requires privileges)")
                        received += 1
                    except Exception:
                        output_lines.append(f"Request timeout for icmp_seq {seq}")
                finally:
                    try:
                        sock.close()
                    except Exception:
                        pass

            loss = ((transmitted - received) / transmitted) * 100 if transmitted else 0
            output_lines.append(f"\\n--- {host} ping statistics ---")
            output_lines.append(f"{transmitted} packets transmitted, {received} received, {loss:.0f}% packet loss, time 0ms")
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
            return ToolError(message=str(e), output="", brief="ping failed")
