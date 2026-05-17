"""killall tool - kill processes by name."""
import os
import signal

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_process_name, _is_protected_pid, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Killall(CallableTool2[Params]):
    name: str = "Killall"
    description: str = "Kill processes by name."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            sig = signal.SIGTERM
            names = []
            i = 0
            while i < len(params.args):
                arg = params.args[i]
                if arg.startswith("-") and len(arg) > 1:
                    s = arg[1:]
                    if s.isdigit():
                        sig = int(s)
                    elif s.upper() == "HUP":
                        sig = signal.SIGHUP
                    elif s.upper() == "INT":
                        sig = signal.SIGINT
                    elif s.upper() == "KILL":
                        sig = signal.SIGKILL
                    elif s.upper() == "TERM":
                        sig = signal.SIGTERM
                elif not arg.startswith("-"):
                    names.append(arg)
                i += 1

            for name in names:
                is_prot, reason = _is_protected_process_name(name)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected process")

            if not names:
                return ToolError(message="killall: missing operand", output="", brief="missing operand")

            killed = 0
            errors = []
            if os.name == "nt":
                # Windows fallback: try using taskkill
                for name in names:
                    ret = os.system(f"taskkill /F /IM {name} >nul 2>nul")
                    if ret == 0:
                        killed += 1
                    else:
                        errors.append(f"killall: {name}: no process found")
            else:
                # Scan /proc on Linux
                for entry in os.listdir("/proc"):
                    if entry.isdigit():
                        try:
                            with open(f"/proc/{entry}/comm", "r") as f:
                                comm = f.read().strip()
                            if comm in names:
                                pid_int = int(entry)
                                is_prot, reason = _is_protected_pid(pid_int)
                                if is_prot:
                                    continue
                                os.kill(pid_int, sig)
                                killed += 1
                        except (OSError, ValueError):
                            pass
                if killed == 0:
                    errors.append(f"killall: {', '.join(names)}: no process found")

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
                return ToolError(message=output, output=output, brief="killall failed")

            return ToolOk(output="")
        except Exception as e:
            return ToolError(message=str(e), output="", brief="killall failed")
