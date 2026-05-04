"""sed tool - stream editor for filtering and transforming text."""
import os
import re
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params

from kimix.tools.common import _maybe_export_output_async

class Sed(CallableTool2[Params]):
    name: str = "Sed"
    description: str = "Stream editor for filtering and transforming text."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            script = None
            paths = []
            i = 0
            while i < len(params.args):
                arg = params.args[i]
                if arg == "-e":
                    i += 1
                    if i < len(params.args):
                        script = params.args[i]
                elif arg == "-i":
                    i += 1
                    # inplace editing, consume suffix if present
                elif arg.startswith("-"):
                    pass
                elif script is None:
                    script = arg
                else:
                    paths.append(arg)
                i += 1

            if script is None:
                return ToolError(message="sed: missing script", output="", brief="missing script")

            # Parse basic s/old/new/flags
            if script.startswith("s"):
                delim = script[1]
                parts = script[2:].split(delim)
                if len(parts) < 2:
                    return ToolError(message="sed: bad script", output="", brief="bad script")
                pattern = parts[0]
                repl = parts[1]
                flags = parts[2] if len(parts) > 2 else ""
                count = 0
                if flags.isdigit():
                    count = int(flags)
                global_replace = "g" in flags
                regex = re.compile(pattern)

                def _apply(line: str) -> str:
                    if global_replace and count == 0:
                        return regex.sub(repl, line)
                    elif count > 0:
                        return regex.sub(repl, line, count=count)
                    else:
                        return regex.sub(repl, line, count=1)
            elif script.startswith("d"):
                # delete lines matching pattern
                line_range = script[1:]
                if line_range.isdigit():
                    target_line = int(line_range)

                    def _apply(line: str, lineno: int) -> str | None:
                        return None if lineno == target_line else line
                else:
                    return ToolError(message="sed: unsupported script", output="", brief="unsupported script")
            else:
                return ToolError(message="sed: unsupported script", output="", brief="unsupported script")

            cwd = params.cwd or os.getcwd()
            results = []

            if not paths:
                return ToolError(message="sed: missing file operand", output="", brief="missing operand")

            for p in paths:
                target = Path(cwd) / p if not Path(p).is_absolute() else Path(p)
                try:
                    with open(target, "r", encoding="utf-8", errors="replace") as f:
                        for lineno, line in enumerate(f, 1):
                            line = line.rstrip("\n\r")
                            if script.startswith("d"):
                                res = _apply(line, lineno)
                            else:
                                res = _apply(line)
                            if res is not None:
                                results.append(res)
                except FileNotFoundError:
                    results.append(f"sed: can't read {p}: No such file or directory")
                except OSError as e:
                    results.append(f"sed: {p}: {e}")

            output = "\n".join(results)
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="sed failed")
