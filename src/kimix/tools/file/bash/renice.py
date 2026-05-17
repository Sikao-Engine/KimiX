"""renice tool - alter priority of running processes."""
import os

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_pid, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Renice(CallableTool2[Params]):
    name: str = "Renice"
    description: str = "Alter priority of running processes."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            priority = None
            pids = []
            i = 0
            while i < len(params.args):
                arg = params.args[i]
                if arg == "-n":
                    i += 1
                    if i < len(params.args):
                        priority = int(params.args[i])
                elif arg.startswith("-n"):
                    priority = int(arg[2:])
                elif not arg.startswith("-"):
                    pids.append(int(arg))
                i += 1

            if priority is None or not pids:
                return ToolError(message="renice: missing operand", output="", brief="missing operand")

            errors = []
            for pid in pids:
                is_prot, reason = _is_protected_pid(pid)
                if is_prot:
                    errors.append(f"renice: {reason}")
                    continue
                try:
                    if hasattr(os, "setpriority"):
                        os.setpriority(os.PRIO_PROCESS, pid, priority)
                    else:
                        import ctypes
                        kernel = ctypes.windll.kernel32
                        handle = kernel.OpenProcess(0x0200 | 0x0400, False, pid)
                        if not handle:
                            raise OSError(f"Cannot open process {pid}")
                        kernel.SetPriorityClass(handle, priority)
                        kernel.CloseHandle(handle)
                except OSError as e:
                    errors.append(f"renice: ({pid}): {e}")

            if errors:
                output = "\n".join(errors)
                if params.output_path:
                    cwd = params.cwd or os.getcwd()
                    is_prot, reason = _is_protected_path(params.output_path, cwd)
                    if is_prot:
                        return ToolError(message=reason, output=reason, brief="protected path")
                    with open(params.output_path, "w", encoding="utf-8") as f:
                        f.write(output)
                    output = f"saved to file `{params.output_path}`"
                return ToolError(message=output, output=output, brief="renice failed")

            return ToolOk(output="")
        except Exception as e:
            return ToolError(message=str(e), output="", brief="renice failed")
