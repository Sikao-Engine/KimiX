"""scriptreplay tool - play back typescripts, using timing information."""
import asyncio
import os
import time
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Scriptreplay(CallableTool2[Params]):
    name: str = "Scriptreplay"
    description: str = "Play back typescripts, using timing information."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            timing_file = None
            script_file = None
            for arg in params.args:
                if not arg.startswith("-"):
                    if timing_file is None:
                        timing_file = arg
                    else:
                        script_file = arg

            if timing_file is None or script_file is None:
                return ToolError(message="scriptreplay: missing operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            if params.output_path:
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")


            t_path = Path(cwd) / timing_file if not Path(timing_file).is_absolute() else Path(timing_file)
            s_path = Path(cwd) / script_file if not Path(script_file).is_absolute() else Path(script_file)

            try:
                with open(t_path, "r", encoding="utf-8", errors="replace") as f:
                    timings = f.readlines()
                with open(s_path, "r", encoding="utf-8", errors="replace") as f:
                    script = f.read()
            except FileNotFoundError as e:
                return ToolError(message=f"scriptreplay: {e.filename}: No such file or directory", output="", brief="scriptreplay failed")

            output_lines = []
            pos = 0
            for line in timings:
                parts = line.strip().split()
                if len(parts) >= 2:
                    delay = float(parts[0])
                    chars = int(parts[1])
                    await asyncio.sleep(delay)
                    chunk = script[pos:pos + chars]
                    pos += chars
                    output_lines.append(chunk)

            output = "".join(output_lines)
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="scriptreplay failed")
