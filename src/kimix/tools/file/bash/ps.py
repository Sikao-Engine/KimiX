"""ps tool - report a snapshot of the current processes."""
import os
import platform

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Ps(CallableTool2[Params]):
    name: str = "Ps"
    description: str = "Report a snapshot of the current processes."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            results = [f"{'PID':>8} {'TTY':>8} {'TIME':>10} {'CMD':<20}"]

            if platform.system() == "Windows":
                import ctypes
                import ctypes.wintypes

                kernel32 = ctypes.windll.kernel32

                class _PROCESSENTRY32W(ctypes.Structure):
                    _fields_ = [
                        ("dwSize", ctypes.wintypes.DWORD),
                        ("cntUsage", ctypes.wintypes.DWORD),
                        ("th32ProcessID", ctypes.wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.c_void_p),
                        ("th32ModuleID", ctypes.wintypes.DWORD),
                        ("cntThreads", ctypes.wintypes.DWORD),
                        ("th32ParentProcessID", ctypes.wintypes.DWORD),
                        ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", ctypes.wintypes.DWORD),
                        ("szExeFile", ctypes.c_wchar * 260),
                    ]

                TH32CS_SNAPPROCESS = 0x00000002
                h_snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
                if h_snap == -1:
                    return ToolError(message="ps: failed to snapshot processes", output="", brief="snapshot failed")

                entry = _PROCESSENTRY32W()
                entry.dwSize = ctypes.sizeof(entry)
                if kernel32.Process32FirstW(h_snap, ctypes.byref(entry)):
                    while True:
                        pid = entry.th32ProcessID
                        if pid != 0:
                            name = entry.szExeFile
                            results.append(f"{pid:>8} {'?':>8} {'00:00:00':>10} {name:<20}")
                        if not kernel32.Process32NextW(h_snap, ctypes.byref(entry)):
                            break
                kernel32.CloseHandle(h_snap)
            else:
                proc_dir = "/proc"
                for entry in os.scandir(proc_dir):
                    if not entry.is_dir():
                        continue
                    name = entry.name
                    if not name.isdigit():
                        continue
                    try:
                        pid = int(name)
                        with open(os.path.join(entry.path, "comm"), "r") as f:
                            comm = f.read().strip()
                        results.append(f"{pid:>8} {'?':>8} {'00:00:00':>10} {comm:<20}")
                    except Exception:
                        pass

            output = "\n".join(results)
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
            return ToolError(message=str(e), output="", brief="ps failed")
