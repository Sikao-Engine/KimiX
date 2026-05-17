"""chmod tool - change file mode bits."""
import os
import stat
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Chmod(CallableTool2[Params]):
    name: str = "Chmod"
    description: str = "Change file mode bits."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            mode_str = None
            recursive = False
            paths = []
            i = 0
            while i < len(params.args):
                arg = params.args[i]
                if arg == "-R" or arg == "--recursive":
                    recursive = True
                elif arg.startswith("-"):
                    pass
                elif mode_str is None:
                    mode_str = arg
                else:
                    paths.append(arg)
                i += 1

            if mode_str is None or not paths:
                return ToolError(message="chmod: missing operand", output="", brief="missing operand")

            def _parse_mode(s: str) -> int:
                if s.isdigit() or (len(s) == 3 and s.isdigit()):
                    return int(s, 8)
                if len(s) == 4 and s.isdigit():
                    return int(s, 8)
                raise ValueError("symbolic mode not fully supported")

            try:
                mode = _parse_mode(mode_str)
            except ValueError:
                return ToolError(message=f"chmod: invalid mode: {mode_str}", output="", brief="invalid mode")

            cwd = params.cwd or os.getcwd()
            if params.output_path:
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")

            for p in paths:
                is_prot, reason = _is_protected_path(p, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")

            errors = []
            for p in paths:
                target = os.path.join(cwd, p) if not os.path.isabs(p) else p
                try:
                    if recursive and os.path.isdir(target):
                        for root, dirs, files in os.walk(target):
                            for name in dirs + files:
                                full = os.path.join(root, name)
                                os.chmod(full, mode)
                        os.chmod(target, mode)
                    else:
                        os.chmod(target, mode)
                except FileNotFoundError:
                    errors.append(f"chmod: cannot access '{p}': No such file or directory")
                except OSError as e:
                    errors.append(f"chmod: changing permissions of '{p}': {e}")

            if errors:
                output = "\n".join(errors)
                if params.output_path:
                    with open(params.output_path, "w", encoding="utf-8") as f:
                        f.write(output)
                    output = f"saved to file `{params.output_path}`"
                return ToolError(message=output, output=output, brief="chmod failed")

            output = ""
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="chmod failed")
