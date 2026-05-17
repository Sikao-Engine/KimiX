"""uniq tool - report or omit repeated lines."""
import os
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Uniq(CallableTool2[Params]):
    name: str = "Uniq"
    description: str = "Report or omit repeated lines."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            count = False
            repeated = False
            unique = False
            paths = []
            for arg in params.args:
                if arg == "-c" or arg == "--count":
                    count = True
                elif arg == "-d" or arg == "--repeated":
                    repeated = True
                elif arg == "-u" or arg == "--unique":
                    unique = True
                elif not arg.startswith("-"):
                    paths.append(arg)

            if not paths:
                return ToolError(message="uniq: missing operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            results = []

            for p in paths:
                target = Path(cwd) / p if not Path(p).is_absolute() else Path(p)
                try:
                    with open(target, "r", encoding="utf-8", errors="replace") as f:
                        lines = f.read().splitlines(keepends=True)
                except FileNotFoundError:
                    results.append(f"uniq: {p}: No such file or directory")
                    continue
                except OSError as e:
                    results.append(f"uniq: {p}: {e}")
                    continue

                if not lines:
                    continue

                groups = []
                current = lines[0]
                n = 1
                for line in lines[1:]:
                    if line == current:
                        n += 1
                    else:
                        groups.append((current.rstrip("\n\r"), n))
                        current = line
                        n = 1
                groups.append((current.rstrip("\n\r"), n))

                for line, n in groups:
                    if repeated and n <= 1:
                        continue
                    if unique and n > 1:
                        continue
                    if count:
                        results.append(f"{n:>7} {line}")
                    else:
                        results.append(line)

            output = "\n".join(results)
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
            return ToolError(message=str(e), output="", brief="uniq failed")
