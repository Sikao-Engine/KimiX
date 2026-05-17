"""cat tool - concatenate files and print on the standard output."""
import os
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async

class Cat(CallableTool2[Params]):
    name: str = "Cat"
    description: str = "Concatenate files and print on the standard output."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            paths = [arg for arg in params.args if not arg.startswith("-")]
            cwd = params.cwd or os.getcwd()
            if params.output_path:
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")


            errors = []
            contents = []
            cwd_path = Path(cwd)
            for p in paths:
                pp = Path(p)
                target = pp if pp.is_absolute() else cwd_path / p
                try:
                    with open(target, "r", encoding="utf-8", errors="replace") as f:
                        contents.append(f.read())
                except FileNotFoundError:
                    errors.append(f"cat: {p}: No such file or directory")
                except IsADirectoryError:
                    errors.append(f"cat: {p}: Is a directory")
                except OSError as e:
                    errors.append(f"cat: {p}: {e}")

            if errors:
                output = "\n".join(errors)
                if params.output_path:
                    with open(params.output_path, "w", encoding="utf-8") as f:
                        f.write(output)
                    output = f"saved to file `{params.output_path}`"
                return ToolError(message=output, output=output, brief="cat failed")

            output = "".join(contents)
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="cat failed")
