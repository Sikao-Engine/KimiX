"""tail tool - output the last part of files."""
import os
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params

from kimix.tools.common import _maybe_export_output_async

class Tail(CallableTool2[Params]):
    name: str = "Tail"
    description: str = "Output the last part of files."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            lines_count = 10
            paths = []
            i = 0
            while i < len(params.args):
                arg = params.args[i]
                if arg == "-n":
                    i += 1
                    if i < len(params.args):
                        lines_count = int(params.args[i])
                elif arg.startswith("-n"):
                    lines_count = int(arg[2:])
                elif not arg.startswith("-"):
                    paths.append(arg)
                i += 1

            cwd = params.cwd or os.getcwd()
            errors = []
            contents = []
            for p in paths:
                target = Path(cwd) / p if not Path(p).is_absolute() else Path(p)
                try:
                    with open(target, "r", encoding="utf-8", errors="replace") as f:
                        # Use deque for efficiency with large files
                        from collections import deque
                        d = deque(maxlen=lines_count)
                        for line in f:
                            d.append(line)
                        contents.append("".join(d))
                except FileNotFoundError:
                    errors.append(f"tail: cannot open '{p}' for reading: No such file or directory")
                except OSError as e:
                    errors.append(f"tail: {p}: {e}")

            if errors:
                output = "\n".join(errors)
                if params.output_path:
                    with open(params.output_path, "w", encoding="utf-8") as f:
                        f.write(output)
                    output = f"saved to file `{params.output_path}`"
                return ToolError(message=output, output=output, brief="tail failed")

            output = "".join(contents)
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="tail failed")
