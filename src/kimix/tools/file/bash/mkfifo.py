"""mkfifo tool - make FIFOs (named pipes)."""
import os

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Mkfifo(CallableTool2[Params]):
    name: str = "Mkfifo"
    description: str = "Make FIFOs (named pipes)."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            paths = [arg for arg in params.args if not arg.startswith("-")]
            if not paths:
                return ToolError(message="mkfifo: missing operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            if params.output_path:
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")

            for p in paths:
                is_prot, reason = _is_protected_path(p, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")

            errors = []
            for p in paths:
                target = os.path.join(cwd, p) if not os.path.isabs(p) else p
                try:
                    os.mkfifo(target)
                except AttributeError:
                    errors.append(f"mkfifo: {p}: not supported on this platform")
                except OSError as e:
                    errors.append(f"mkfifo: cannot create fifo '{p}': {e}")

            if errors:
                output = "\n".join(errors)
                if params.output_path:
                    with open(params.output_path, "w", encoding="utf-8") as f:
                        f.write(output)
                    output = f"saved to file `{params.output_path}`"
                return ToolError(message=output, output=output, brief="mkfifo failed")

            return ToolOk(output="")
        except Exception as e:
            return ToolError(message=str(e), output="", brief="mkfifo failed")
