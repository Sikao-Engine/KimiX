"""vmstat tool - report virtual memory statistics."""
import os
import time

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Vmstat(CallableTool2[Params]):
    name: str = "Vmstat"
    description: str = "Report virtual memory statistics."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
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
                output = f"procs -----------memory---------- ---swap-- -----io---- -system-- ------cpu-----\n"
                output += f" r  b   swpd   free   buff  cache   si   so    bi    bo   in   cs us sy id wa st\n"
                output += f" 0  0      0 {free:6}      0      0    0    0     0     0    0    0  0  0 100  0  0\n"
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
                    free = meminfo.get("MemFree", 0)
                    buffers = meminfo.get("Buffers", 0)
                    cached = meminfo.get("Cached", 0)
                    output = f"procs -----------memory---------- ---swap-- -----io---- -system-- ------cpu-----\n"
                    output += f" r  b   swpd   free   buff  cache   si   so    bi    bo   in   cs us sy id wa st\n"
                    output += f" 0  0      0 {free:6} {buffers:6} {cached:6}    0    0     0     0    0    0  0  0 100  0  0\n"
                except Exception:
                    output = "vmstat: statistics unavailable"

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
            return ToolError(message=str(e), output="", brief="vmstat failed")
