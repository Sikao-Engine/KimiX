"""hwclock tool - query or set the hardware clock."""
import time

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Hwclock(CallableTool2[Params]):
    name: str = "Hwclock"
    description: str = "Query or set the hardware clock (RTC)."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            utc = False
            for arg in params.args:
                if arg == "--utc":
                    utc = True
                elif arg == "--localtime":
                    utc = False
                elif arg == "--set":
                    return ToolError(
                        message="hwclock: --set not supported in pure-Python implementation",
                        output="",
                        brief="set not supported"
                    )

            if utc:
                t = time.gmtime()
                output = time.strftime("%Y-%m-%d %H:%M:%S UTC", t)
            else:
                t = time.localtime()
                output = time.strftime("%Y-%m-%d %H:%M:%S %Z", t)

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
            return ToolError(message=str(e), output="", brief="hwclock failed")
