"""fuser tool - identify processes using files or sockets."""
import os

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Fuser(CallableTool2[Params]):
    name: str = "Fuser"
    description: str = "Identify processes using files or sockets."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            paths = [arg for arg in params.args if not arg.startswith("-")]
            if not paths:
                return ToolError(message="fuser: missing operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            if params.output_path:
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")


            if os.name == "nt":
                return ToolError(message="fuser: not supported on Windows", output="", brief="not supported")

            results = []
            target = paths[0]
            target = os.path.abspath(target) if not os.path.isabs(target) else target
            for entry in os.listdir("/proc"):
                if entry.isdigit():
                    fd_dir = f"/proc/{entry}/fd"
                    try:
                        for fd in os.listdir(fd_dir):
                            try:
                                link = os.readlink(os.path.join(fd_dir, fd))
                                if link == target:
                                    results.append(entry)
                                    break
                            except OSError:
                                pass
                    except OSError:
                        pass
            output = " ".join(results)
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="fuser failed")
