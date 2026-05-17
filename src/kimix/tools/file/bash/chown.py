"""chown tool - change file owner and group."""
import os
import shutil
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Chown(CallableTool2[Params]):
    name: str = "Chown"
    description: str = "Change file owner and group."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            recursive = False
            owner = None
            group = None
            paths = []
            i = 0
            while i < len(params.args):
                arg = params.args[i]
                if arg == "-R" or arg == "--recursive":
                    recursive = True
                elif arg.startswith("-"):
                    pass
                elif owner is None:
                    if ":" in arg:
                        owner, group = arg.split(":", 1)
                    else:
                        owner = arg
                else:
                    paths.append(arg)
                i += 1

            if owner is None and group is None:
                return ToolError(message="chown: missing operand", output="", brief="missing operand")
            if not paths:
                return ToolError(message="chown: missing file operand", output="", brief="missing operand")

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
            uid = None
            gid = None
            if owner:
                try:
                    import pwd
                    uid = pwd.getpwnam(owner).pw_uid
                except (ImportError, KeyError):
                    try:
                        uid = int(owner)
                    except ValueError:
                        errors.append(f"chown: invalid user: '{owner}'")
            if group:
                try:
                    import grp
                    gid = grp.getgrnam(group).gr_gid
                except (ImportError, KeyError):
                    try:
                        gid = int(group)
                    except ValueError:
                        errors.append(f"chown: invalid group: '{group}'")

            if errors:
                output = "\n".join(errors)
                if params.output_path:
                    with open(params.output_path, "w", encoding="utf-8") as f:
                        f.write(output)
                    output = f"saved to file `{params.output_path}`"
                return ToolError(message=output, output=output, brief="chown failed")

            for p in paths:
                target = os.path.join(cwd, p) if not os.path.isabs(p) else p
                try:
                    if recursive and os.path.isdir(target):
                        for root, dirs, files in os.walk(target):
                            for name in dirs + files:
                                full = os.path.join(root, name)
                                shutil.chown(full, uid, gid)
                        shutil.chown(target, uid, gid)
                    else:
                        shutil.chown(target, uid, gid)
                except FileNotFoundError:
                    errors.append(f"chown: cannot access '{p}': No such file or directory")
                except OSError as e:
                    errors.append(f"chown: changing ownership of '{p}': {e}")

            if errors:
                output = "\n".join(errors)
                if params.output_path:
                    with open(params.output_path, "w", encoding="utf-8") as f:
                        f.write(output)
                    output = f"saved to file `{params.output_path}`"
                return ToolError(message=output, output=output, brief="chown failed")

            output = ""
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="chown failed")
