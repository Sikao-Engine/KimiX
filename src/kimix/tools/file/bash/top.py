"""top tool - display processes."""
import os
import time

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Top(CallableTool2[Params]):
    name: str = "Top"
    description: str = "Display processes."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes
                output = "PID    USER      PR  NI  VIRT  RES  SHR S %CPU %MEM    TIME+  COMMAND\n"
                # Simplified Windows snapshot
                output += "N/A    N/A       20   0     0    0    0 R  0.0  0.0   0:00.00 python\n"
            else:
                output = "PID USER      PR  NI  VIRT  RES  SHR S %CPU %MEM    TIME+ COMMAND\n"
                for entry in sorted(os.listdir("/proc")):
                    if entry.isdigit():
                        try:
                            with open(f"/proc/{entry}/stat", "r") as f:
                                stat = f.read().split()
                            with open(f"/proc/{entry}/status", "r") as f:
                                status_lines = f.readlines()
                            uid_line = [l for l in status_lines if l.startswith("Uid:")]
                            user = "root" if uid_line and uid_line[0].split()[1] == "0" else "user"
                            comm = stat[1].strip("()")
                            utime = int(stat[13])
                            stime = int(stat[14])
                            total_time = (utime + stime) / 100
                            minutes = int(total_time // 60)
                            seconds = total_time % 60
                            output += f"{entry:>5} {user:<8} 20   0     0    0    0 R  0.0  0.0 {minutes:3}:{seconds:05.2f} {comm}\n"
                        except (OSError, IndexError):
                            pass

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
            return ToolError(message=str(e), output="", brief="top failed")
