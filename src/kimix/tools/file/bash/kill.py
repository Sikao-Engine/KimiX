"""kill tool - send a signal to a process."""
import os
import signal

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_pid, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Kill(CallableTool2[Params]):
    name: str = "Kill"
    description: str = "Send a signal to a process."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            sig = signal.SIGTERM
            pids = []
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
                    elif s.upper() == "USR1":
                        sig = signal.SIGUSR1
                    elif s.upper() == "USR2":
                        sig = signal.SIGUSR2
                    elif s.upper() == "STOP":
                        sig = signal.SIGSTOP
                    elif s.upper() == "CONT":
                        sig = signal.SIGCONT
                elif not arg.startswith("-"):
                    pids.append(int(arg))
                i += 1

            errors = []
            for pid in pids:
                is_prot, reason = _is_protected_pid(pid)
                if is_prot:
                    errors.append(f"kill: {reason}")
                    continue
                try:
                    os.kill(pid, sig)
                except OSError as e:
                    errors.append(f"kill: ({pid}) - {e}")

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
                return ToolError(message=output, output=output, brief="kill failed")

            return ToolOk(output="")
        except Exception as e:
            return ToolError(message=str(e), output="", brief="kill failed")
