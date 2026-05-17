"""chgrp tool - change group ownership."""
import os
import shutil
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Chgrp(CallableTool2[Params]):
    name: str = "Chgrp"
    description: str = "Change group ownership."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            recursive = False
            group = None
            paths = []
            i = 0
            while i < len(params.args):
                arg = params.args[i]
                if arg == "-R" or arg == "--recursive":
                    recursive = True
                elif arg.startswith("-"):
                    pass
                elif group is None:
                    group = arg
                else:
                    paths.append(arg)
                i += 1

            if group is None:
                return ToolError(message="chgrp: missing operand", output="", brief="missing operand")
            if not paths:
                return ToolError(message="chgrp: missing file operand", output="", brief="missing operand")

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
            gid = None
            try:
                import grp
                gid = grp.getgrnam(group).gr_gid
            except (ImportError, KeyError):
                try:
                    gid = int(group)
                except ValueError:
                    errors.append(f"chgrp: invalid group: '{group}'")

            if errors:
                output = "\n".join(errors)
                if params.output_path:
                    with open(params.output_path, "w", encoding="utf-8") as f:
                        f.write(output)
                    output = f"saved to file `{params.output_path}`"
                return ToolError(message=output, output=output, brief="chgrp failed")

            for p in paths:
                target = os.path.join(cwd, p) if not os.path.isabs(p) else p
                try:
                    if recursive and os.path.isdir(target):
                        for root, dirs, files in os.walk(target):
                            for name in dirs + files:
                                full = os.path.join(root, name)
                                shutil.chown(full, group=gid)
                        shutil.chown(target, group=gid)
                    else:
                        shutil.chown(target, group=gid)
                except FileNotFoundError:
                    errors.append(f"chgrp: cannot access '{p}': No such file or directory")
                except OSError as e:
                    errors.append(f"chgrp: changing group of '{p}': {e}")

            if errors:
                output = "\n".join(errors)
                if params.output_path:
                    with open(params.output_path, "w", encoding="utf-8") as f:
                        f.write(output)
                    output = f"saved to file `{params.output_path}`"
                return ToolError(message=output, output=output, brief="chgrp failed")

            output = ""
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="chgrp failed")
