"""mv tool - move (rename) files."""
import os
import shutil
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async

class Mv(CallableTool2[Params]):
    name: str = "Mv"
    description: str = "Move (rename) files."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            paths = [arg for arg in params.args if not arg.startswith("-")]

            if len(paths) < 2:
                return ToolError(message="mv: missing file operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            if params.output_path:
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")

            for p in paths:
                is_prot, reason = _is_protected_path(p, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")

            sources = paths[:-1]
            dest = paths[-1]
            dest_path = Path(cwd) / dest if not Path(dest).is_absolute() else Path(dest)

            errors = []
            if len(sources) > 1 or (dest_path.exists() and dest_path.is_dir()):
                if not dest_path.exists() or not dest_path.is_dir():
                    errors.append(f"mv: target '{dest}' is not a directory")
                else:
                    for src in sources:
                        src_path = Path(cwd) / src if not Path(src).is_absolute() else Path(src)
                        try:
                            shutil.move(str(src_path), str(dest_path / src_path.name))
                        except FileNotFoundError:
                            errors.append(f"mv: cannot stat '{src}': No such file or directory")
                        except OSError as e:
                            errors.append(f"mv: cannot move '{src}': {e}")
            else:
                src = sources[0]
                src_path = Path(cwd) / src if not Path(src).is_absolute() else Path(src)
                try:
                    shutil.move(str(src_path), str(dest_path))
                except FileNotFoundError:
                    errors.append(f"mv: cannot stat '{src}': No such file or directory")
                except OSError as e:
                    errors.append(f"mv: cannot move '{src}': {e}")

            if errors:
                output = "\n".join(errors)
                if params.output_path:
                    with open(params.output_path, "w", encoding="utf-8") as f:
                        f.write(output)
                    output = f"saved to file `{params.output_path}`"
                return ToolError(message=output, output=output, brief="mv failed")

            output = ""
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="mv failed")
