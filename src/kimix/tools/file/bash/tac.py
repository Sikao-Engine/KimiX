"""tac tool - concatenate and print files in reverse line order."""
import os
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Tac(CallableTool2[Params]):
    name: str = "Tac"
    description: str = "Concatenate and print files in reverse line order."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            paths = [arg for arg in params.args if not arg.startswith("-")]
            cwd = params.cwd or os.getcwd()
            errors = []
            all_lines = []

            for p in paths:
                target = Path(cwd) / p if not Path(p).is_absolute() else Path(p)
                try:
                    with open(target, "r", encoding="utf-8", errors="replace") as f:
                        all_lines.extend(f.readlines())
                except FileNotFoundError:
                    errors.append(f"tac: {p}: No such file or directory")
                except IsADirectoryError:
                    errors.append(f"tac: {p}: Is a directory")
                except OSError as e:
                    errors.append(f"tac: {p}: {e}")

            if errors and not all_lines:
                output = "\n".join(errors)
                if params.output_path:
                    cwd = params.cwd or os.getcwd()
                    is_prot, reason = _is_protected_path(params.output_path, cwd)
                    if is_prot:
                        return ToolError(message=reason, output=reason, brief="protected path")
                    with open(params.output_path, "w", encoding="utf-8") as f:
                        f.write(output)
                    output = f"saved to file `{params.output_path}`"
                return ToolError(message=output, output=output, brief="tac failed")

            all_lines.reverse()
            if errors:
                all_lines.append("\n")
                all_lines.append("\n".join(errors))

            if params.output_path:
                cwd = params.cwd or os.getcwd()
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")
                with open(params.output_path, "w", encoding="utf-8") as f:
                    chunk: list[str] = []
                    for line in all_lines:
                        chunk.append(line)
                        if len(chunk) >= 10000:
                            f.write("".join(chunk))
                            chunk.clear()
                    if chunk:
                        f.write("".join(chunk))
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async("".join(all_lines))
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="tac failed")
