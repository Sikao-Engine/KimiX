"""free tool - display amount of free and used memory in the system."""
import os

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Free(CallableTool2[Params]):
    name: str = "Free"
    description: str = "Display amount of free and used memory in the system."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            human = False
            for arg in params.args:
                if arg == "-h" or arg == "--human":
                    human = True

            def fmt(val):
                if human:
                    for unit in ["B", "Ki", "Mi", "Gi", "Ti"]:
                        if val < 1024:
                            return f"{val:.1f}{unit}"
                        val /= 1024
                    return f"{val:.1f}Pi"
                return str(int(val))

            if os.name == "nt":
                import ctypes
                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                    ]
                mem = MEMORYSTATUSEX()
                mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
                ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
                total = mem.ullTotalPhys // 1024
                free = mem.ullAvailPhys // 1024
                used = total - free
                output = f"              total        used        free      shared  buff/cache   available\n"
                output += f"Mem:    {fmt(total):>10} {fmt(used):>10} {fmt(free):>10} {fmt(0):>10} {fmt(0):>10} {fmt(free):>10}\n"
                output += f"Swap:   {fmt(0):>10} {fmt(0):>10} {fmt(0):>10}\n"
            else:
                try:
                    with open("/proc/meminfo", "r") as f:
                        lines = f.readlines()
                    meminfo = {}
                    for line in lines:
                        parts = line.split(":")
                        if len(parts) == 2:
                            key = parts[0].strip()
                            val = int(parts[1].strip().split()[0])
                            meminfo[key] = val
                    total = meminfo.get("MemTotal", 0)
                    free = meminfo.get("MemFree", 0)
                    available = meminfo.get("MemAvailable", free)
                    buffers = meminfo.get("Buffers", 0)
                    cached = meminfo.get("Cached", 0)
                    used = total - free - buffers - cached
                    swap_total = meminfo.get("SwapTotal", 0)
                    swap_free = meminfo.get("SwapFree", 0)
                    swap_used = swap_total - swap_free
                    output = f"              total        used        free      shared  buff/cache   available\n"
                    output += f"Mem:    {fmt(total):>10} {fmt(used):>10} {fmt(free):>10} {fmt(0):>10} {fmt(buffers+cached):>10} {fmt(available):>10}\n"
                    output += f"Swap:   {fmt(swap_total):>10} {fmt(swap_used):>10} {fmt(swap_free):>10}\n"
                except Exception:
                    output = "Memory information unavailable"

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
            return ToolError(message=str(e), output="", brief="free failed")
