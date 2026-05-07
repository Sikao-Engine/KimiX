"""netstat tool - print network connections, routing tables, interface statistics."""
import os
import platform
import struct
import ctypes
import ctypes.wintypes

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params

from kimix.tools.common import _maybe_export_output_async


class _MIB_TCPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("dwState", ctypes.wintypes.DWORD),
        ("dwLocalAddr", ctypes.wintypes.DWORD),
        ("dwLocalPort", ctypes.wintypes.DWORD),
        ("dwRemoteAddr", ctypes.wintypes.DWORD),
        ("dwRemotePort", ctypes.wintypes.DWORD),
        ("dwOwningPid", ctypes.wintypes.DWORD),
    ]


class _MIB_TCPTABLE_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("dwNumEntries", ctypes.wintypes.DWORD),
        ("table", _MIB_TCPROW_OWNER_PID * 1),
    ]


def _ntohs(x: int) -> int:
    return struct.unpack("!H", struct.pack("H", x))[0]


def _fmt_addr(addr: int) -> str:
    return f"{(addr >> 0) & 0xff}.{(addr >> 8) & 0xff}.{(addr >> 16) & 0xff}.{(addr >> 24) & 0xff}"


def _fmt_port(port: int) -> int:
    return _ntohs(port)


def _get_tcp_table():
    iphlpapi = ctypes.windll.iphlpapi
    size = ctypes.wintypes.DWORD(0)
    iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), 0, 5, 5, 0)
    buf = ctypes.create_string_buffer(size.value)
    if iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), 0, 5, 5, 0) != 0:
        return []
    table = ctypes.cast(buf, ctypes.POINTER(_MIB_TCPTABLE_OWNER_PID)).contents
    return [table.table[i] for i in range(table.dwNumEntries)]


def _get_process_name(pid: int) -> str:
    kernel32 = ctypes.windll.kernel32
    h = kernel32.OpenProcess(0x0410, False, pid)
    if not h:
        return str(pid)
    try:
        psapi = ctypes.windll.psapi
        name_buf = ctypes.create_unicode_buffer(512)
        if psapi.GetModuleBaseNameW(h, None, name_buf, 512):
            return name_buf.value
        return str(pid)
    finally:
        kernel32.CloseHandle(h)


def _build_linux_inode_map() -> dict[str, str]:
    """Scan /proc once and build a map of socket inode -> PID/comm."""
    inode_map: dict[str, str] = {}
    try:
        for entry in os.scandir("/proc"):
            if not entry.is_dir() or not entry.name.isdigit():
                continue
            pid = entry.name
            comm_path = f"/proc/{pid}/comm"
            try:
                with open(comm_path, "r") as f:
                    comm = f.read().strip()
            except Exception:
                comm = "unknown"
            fd_dir = f"/proc/{pid}/fd"
            try:
                for fd_entry in os.scandir(fd_dir):
                    try:
                        link = os.readlink(fd_entry.path)
                        if link.startswith("socket:["):
                            inode = link[8:-1]
                            inode_map[inode] = f"{pid}/{comm}"
                    except (OSError, ValueError):
                        continue
            except (PermissionError, FileNotFoundError):
                continue
    except Exception:
        pass
    return inode_map


def _linux_netstat_tlnp() -> list[str]:
    results = [
        "Proto Recv-Q Send-Q Local Address           Foreign Address         State       PID/Program name"
    ]
    inode_map = _build_linux_inode_map()
    for proto, path in (("tcp", "/proc/net/tcp"), ("tcp6", "/proc/net/tcp6")):
        try:
            with open(path, "r") as f:
                lines = f.readlines()
        except (FileNotFoundError, PermissionError):
            continue
        if len(lines) < 1:
            continue
        for line in lines[1:]:
            parts = line.strip().split()
            if len(parts) < 10:
                continue
            local = parts[1]
            state = int(parts[3], 16)
            inode = parts[9]
            if state != 10:  # TCP_LISTEN = 0x0A
                continue
            local_addr, local_port = local.split(":")
            if proto == "tcp6":
                try:
                    addr_bytes = bytes.fromhex(local_addr.zfill(32))
                    a = struct.unpack("!8H", addr_bytes)
                    local_addr = f"[{a[0]:04x}:{a[1]:04x}:{a[2]:04x}:{a[3]:04x}:{a[4]:04x}:{a[5]:04x}:{a[6]:04x}:{a[7]:04x}]"
                except ValueError:
                    local_addr = "[::]"
            else:
                try:
                    addr = struct.unpack("<I", bytes.fromhex(local_addr.zfill(8)))[0]
                    local_addr = _fmt_addr(addr)
                except ValueError:
                    local_addr = "0.0.0.0"
            try:
                local_port = int(local_port, 16)
            except ValueError:
                local_port = 0
            rem_addr = "[::]" if proto == "tcp6" else "0.0.0.0"
            pid_name = inode_map.get(inode, "-") if inode != "0" else "-"
            results.append(
                f"{proto:5}      0      0 {local_addr}:{local_port:<15} {rem_addr:<23} LISTEN      {pid_name}"
            )
    return results


def _windows_netstat_tlnp() -> list[str]:
    results = [
        "Proto  Local Address          Foreign Address        State           PID"
    ]
    rows = _get_tcp_table()
    name_cache: dict[int, str] = {}
    for row in rows:
        if row.dwState != 2:  # MIB_TCP_STATE_LISTEN
            continue
        local = f"{_fmt_addr(row.dwLocalAddr)}:{_fmt_port(row.dwLocalPort)}"
        rem = f"{_fmt_addr(row.dwRemoteAddr)}:{_fmt_port(row.dwRemotePort)}"
        pid = row.dwOwningPid
        if pid not in name_cache:
            name_cache[pid] = _get_process_name(pid)
        results.append(
            f"tcp    {local:<22} {rem:<22} LISTENING       {pid}/{name_cache[pid]}"
        )
    return results


class Netstat(CallableTool2[Params]):
    name: str = "Netstat"
    description: str = "Print network connections, routing tables, interface statistics, masquerade connections, and multicast memberships."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            tlnp = "-tlnp" in params.args
            if not tlnp:
                for a in params.args:
                    if a in ("-t", "-l", "-n", "-p"):
                        tlnp = True
                        break
            if platform.system() == "Windows":
                lines = _windows_netstat_tlnp()
            else:
                lines = _linux_netstat_tlnp()
            output = "\n".join(lines)
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="netstat failed")
