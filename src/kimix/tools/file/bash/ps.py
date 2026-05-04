"""ps tool - report a snapshot of the current processes."""
import os
import platform

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params

from kimix.tools.common import _maybe_export_output_async

class Ps(CallableTool2[Params]):
    name: str = "Ps"
    description: str = "Report a snapshot of the current processes."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            all_users = False
            for arg in params.args:
                if arg in ("-e", "-A", "aux", "-ef"):
                    all_users = True

            results = [f"{'PID':>8} {'TTY':>8} {'TIME':>10} {'CMD':<20}"]

            if platform.system() == "Windows":
                import ctypes
                import ctypes.wintypes

                kernel32 = ctypes.windll.kernel32
                psapi = ctypes.windll.psapi

                arr = (ctypes.wintypes.DWORD * 1024)()
                cb_needed = ctypes.wintypes.DWORD()
                if not psapi.EnumProcesses(ctypes.byref(arr), ctypes.sizeof(arr), ctypes.byref(cb_needed)):
                    return ToolError(message="ps: failed to enumerate processes", output="", brief="enum failed")
                count = cb_needed.value // ctypes.sizeof(ctypes.wintypes.DWORD)
                for i in range(count):
                    pid = arr[i]
                    if pid == 0:
                        continue
                    try:
                        h = kernel32.OpenProcess(0x0410, False, pid)
                        if not h:
                            continue
                        name = ""
                        mod = ctypes.wintypes.HMODULE()
                        cb = ctypes.wintypes.DWORD()
                        if psapi.EnumProcessModules(h, ctypes.byref(mod), ctypes.sizeof(mod), ctypes.byref(cb)):
                            buf = ctypes.create_unicode_buffer(260)
                            psapi.GetModuleBaseNameW(h, mod, buf, 260)
                            name = buf.value
                        kernel32.CloseHandle(h)
                        results.append(f"{pid:>8} {'?':>8} {'00:00:00':>10} {name:<20}")
                    except Exception:
                        pass
            else:
                import glob
                import time

                for status_path in glob.glob("/proc/[0-9]*/status"):
                    try:
                        pid = int(status_path.split("/")[-2])
                        with open(status_path, "r") as f:
                            lines = f.readlines()
                        name = ""
                        for line in lines:
                            if line.startswith("Name:"):
                                name = line.split(":", 1)[1].strip()
                                break
                        results.append(f"{pid:>8} {'?':>8} {'00:00:00':>10} {name:<20}")
                    except Exception:
                        pass

            output = "\n".join(results)
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="ps failed")
