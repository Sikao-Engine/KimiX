"""cal tool - display a calendar."""
import calendar
import datetime
import os

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async

class Cal(CallableTool2[Params]):
    name: str = "Cal"
    description: str = "Display a calendar."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            year = datetime.date.today().year
            month = datetime.date.today().month
            nums = [int(arg) for arg in params.args if arg.isdigit()]
            if len(nums) == 1:
                if nums[0] <= 12:
                    month = nums[0]
                else:
                    year = nums[0]
                    month = None
            elif len(nums) >= 2:
                month = nums[0]
                year = nums[1]

            cal = calendar.TextCalendar()
            if month is not None:
                output = cal.formatmonth(year, month)
            else:
                output = cal.formatyear(year)

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
            return ToolError(message=str(e), output="", brief="cal failed")
