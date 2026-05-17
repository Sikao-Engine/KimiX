"""crontab tool - maintain crontab files for individual users."""
import os
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Crontab(CallableTool2[Params]):
    name: str = "Crontab"
    description: str = "Maintain crontab files for individual users."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            list_mode = False
            file_input = None
            delete = False
            i = 0
            while i < len(params.args):
                arg = params.args[i]
                if arg == "-l":
                    list_mode = True
                elif arg == "-r":
                    delete = True
                elif arg == "-e":
                    return ToolError(message="crontab: -e not supported", output="", brief="not supported")
                elif not arg.startswith("-"):
                    file_input = arg
                i += 1

            cwd = params.cwd or os.getcwd()
            if params.output_path:
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")

            if file_input:
                is_prot, reason = _is_protected_path(file_input, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")

            cron_path = Path.home() / ".crontab"

            if delete:
                if cron_path.exists():
                    cron_path.unlink()
                return ToolOk(output="")

            if list_mode:
                if cron_path.exists():
                    with open(cron_path, "r", encoding="utf-8", errors="replace") as f:
                        output = f.read()
                else:
                    output = "no crontab for user"
                if params.output_path:
                    with open(params.output_path, "w", encoding="utf-8") as f:
                        f.write(output)
                    output = f"saved to file `{params.output_path}`"
                else:
                    output = await _maybe_export_output_async(output)
                return ToolOk(output=output)

            if file_input:
                src = Path(params.cwd or os.getcwd()) / file_input if not Path(file_input).is_absolute() else Path(file_input)
                try:
                    with open(src, "r", encoding="utf-8", errors="replace") as f:
                        data = f.read()
                    with open(cron_path, "w", encoding="utf-8") as f:
                        f.write(data)
                except FileNotFoundError:
                    return ToolError(message=f"crontab: {file_input}: No such file or directory", output="", brief="crontab failed")
                return ToolOk(output="")

            return ToolError(message="crontab: usage error", output="", brief="usage error")
        except Exception as e:
            return ToolError(message=str(e), output="", brief="crontab failed")
