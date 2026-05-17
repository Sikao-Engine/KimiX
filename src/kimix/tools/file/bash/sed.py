"""sed tool - stream editor for filtering and transforming text."""
import os
import re
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

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
            if params.output_path:
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")


            cwd_path = Path(cwd)

            if not paths:
                return ToolError(message="sed: missing file operand", output="", brief="missing operand")

            is_delete = script.startswith("d")
            output_path = params.output_path

            if output_path:
                # Stream directly to output file to avoid building a large list in memory.
                with open(output_path, "w", encoding="utf-8") as out:
                    first_write = True
                    sep = ""
                    for p in paths:
                        p_path = Path(p)
                        target = p_path if p_path.is_absolute() else cwd_path / p
                        try:
                            with open(target, "r", encoding="utf-8", errors="replace") as f:
                                if is_delete:
                                    for lineno, line in enumerate(f, 1):
                                        line = line.rstrip("\n\r")
                                        res = _apply(line, lineno)
                                        if res is not None:
                                            out.write(sep)
                                            out.write(res)
                                            sep = "\n"
                                else:
                                    for line in f:
                                        line = line.rstrip("\n\r")
                                        res = _apply(line)
                                        if res is not None:
                                            out.write(sep)
                                            out.write(res)
                                            sep = "\n"
                        except FileNotFoundError:
                            out.write(sep)
                            out.write(f"sed: can't read {p}: No such file or directory")
                            sep = "\n"
                        except OSError as e:
                            out.write(sep)
                            out.write(f"sed: {p}: {e}")
                            sep = "\n"
                output = f"saved to file `{output_path}`"
            else:
                results = []
                for p in paths:
                    p_path = Path(p)
                    target = p_path if p_path.is_absolute() else cwd_path / p
                    try:
                        with open(target, "r", encoding="utf-8", errors="replace") as f:
                            if is_delete:
                                for lineno, line in enumerate(f, 1):
                                    line = line.rstrip("\n\r")
                                    res = _apply(line, lineno)
                                    if res is not None:
                                        results.append(res)
                            else:
                                for line in f:
                                    line = line.rstrip("\n\r")
                                    res = _apply(line)
                                    if res is not None:
                                        results.append(res)
                    except FileNotFoundError:
                        results.append(f"sed: can't read {p}: No such file or directory")
                    except OSError as e:
                        results.append(f"sed: {p}: {e}")

                output = "\n".join(results)
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="sed failed")
