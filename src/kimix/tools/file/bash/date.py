"""date tool - print or set the system date and time."""
import datetime
import os

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params

from kimix.tools.common import _maybe_export_output_async

class Date(CallableTool2[Params]):
    name: str = "Date"
    description: str = "Print or set the system date and time."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            fmt = None
            utc = False
            for arg in params.args:
                if arg == "-u" or arg == "--utc" or arg == "--universal":
                    utc = True
                elif arg.startswith("+"):
                    fmt = arg[1:]
                elif not arg.startswith("-"):
                    # Setting date is not supported in pure Python cross-platform safely
                    pass

            now = datetime.datetime.now(datetime.timezone.utc if utc else None)
            if fmt:
                # Map common strftime directives
                output = now.strftime(fmt)
            else:
                output = now.strftime("%a %b %d %H:%M:%S %Z %Y")

            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="date failed")
