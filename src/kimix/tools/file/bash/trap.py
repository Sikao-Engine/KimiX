"""trap tool - trap signals."""
import os
import signal

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async

# Simple in-memory trap store
_TRAP_STORE: dict[int, str] = {}


def _trap_handler(sig_num):
    def handler(signum, frame):
        pass
    return handler


class Trap(CallableTool2[Params]):
    name: str = "Trap"
    description: str = "Trap signals."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            if not params.args:
                lines = [f"trap -- '{cmd}' {sig}" for sig, cmd in _TRAP_STORE.items()]
                output = "\n".join(lines)
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

            action = params.args[0]
            sigs = params.args[1:]
            if not sigs:
                return ToolError(message="trap: missing signal operand", output="", brief="missing operand")

            for s in sigs:
                sig_num = getattr(signal, s, None)
                if sig_num is None:
                    try:
                        sig_num = int(s)
                    except ValueError:
                        return ToolError(message=f"trap: {s}: invalid signal specification", output="", brief="trap failed")
                if action == "-":
                    _TRAP_STORE.pop(sig_num, None)
                    signal.signal(sig_num, signal.SIG_DFL)
                else:
                    _TRAP_STORE[sig_num] = action
                    signal.signal(sig_num, _trap_handler(sig_num))

            return ToolOk(output="")
        except Exception as e:
            return ToolError(message=str(e), output="", brief="trap failed")
