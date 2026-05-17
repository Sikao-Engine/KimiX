"""uptime tool - show how long the system has been running."""
import os
import time
import datetime

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Uptime(CallableTool2[Params]):
    name: str = "Uptime"
    description: str = "Show how long the system has been running."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            if os.name == "nt":
                import ctypes
                class LASTINPUTINFO(ctypes.Structure):
                    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_ulong)]
                lii = LASTINPUTINFO()
                lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
                ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
                tick = ctypes.windll.kernel32.GetTickCount()
                idle = (tick - lii.dwTime) / 1000.0
                uptime_sec = tick / 1000.0
            else:
                with open("/proc/uptime", "r") as f:
                    uptime_sec = float(f.read().split()[0])
                idle = uptime_sec  # simplified

            now = datetime.datetime.now()
            uptime_td = datetime.timedelta(seconds=int(uptime_sec))
            days = uptime_td.days
            hours, rem = divmod(uptime_td.seconds, 3600)
            minutes = rem // 60
            output = f" {now.strftime('%H:%M:%S')} up {days} days, {hours}:{minutes:02d},  load average: N/A"
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
            return ToolError(message=str(e), output="", brief="uptime failed")
