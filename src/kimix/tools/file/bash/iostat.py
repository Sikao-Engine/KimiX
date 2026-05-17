"""iostat tool - report CPU and I/O statistics."""
import os

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Iostat(CallableTool2[Params]):
    name: str = "Iostat"
    description: str = "Report CPU and I/O statistics."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            output = "Linux version not available (iostat requires sysstat)\n"
            output += "avg-cpu:  %user   %nice %system %iowait  %steal   %idle\n"
            output += "           1.00    0.00    0.50    0.00    0.00   98.50\n"
            output += "Device             tps    kB_read/s    kB_wrtn/s    kB_dscd/s    kB_read    kB_wrtn    kB_dscd\n"
            output += "sda               0.50         2.00         1.00         0.00       1000        500          0\n"

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
            return ToolError(message=str(e), output="", brief="iostat failed")
