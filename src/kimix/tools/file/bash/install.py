"""install tool - copy files and set attributes."""
import os
import shutil
import stat
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Install(CallableTool2[Params]):
    name: str = "Install"
    description: str = "Copy files and set attributes."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            mode = None
            owner = None
            group = None
            paths = []
            i = 0
            while i < len(params.args):
                arg = params.args[i]
                if arg == "-m":
                    i += 1
                    if i < len(params.args):
                        mode = int(params.args[i], 8)
                elif arg.startswith("-m"):
                    mode = int(arg[2:], 8)
                elif arg == "-o":
                    i += 1
                    if i < len(params.args):
                        owner = params.args[i]
                elif arg == "-g":
                    i += 1
                    if i < len(params.args):
                        group = params.args[i]
                elif not arg.startswith("-"):
                    paths.append(arg)
                i += 1

            if len(paths) < 2:
                return ToolError(message="install: missing file operand", output="", brief="missing operand")

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
                if not dest_path.exists():
                    errors.append(f"install: target '{dest}' is not a directory")
                else:
                    for src in sources:
                        src_path = Path(cwd) / src if not Path(src).is_absolute() else Path(src)
                        try:
                            shutil.copy2(src_path, dest_path)
                            final = dest_path / src_path.name
                            if mode is not None:
                                os.chmod(final, mode)
                            if owner is not None or group is not None:
                                try:
                                    shutil.chown(final, owner, group)
                                except Exception:
                                    pass
                        except FileNotFoundError:
                            errors.append(f"install: cannot stat '{src}': No such file or directory")
                        except OSError as e:
                            errors.append(f"install: cannot copy '{src}': {e}")
            else:
                src = sources[0]
                src_path = Path(cwd) / src if not Path(src).is_absolute() else Path(src)
                try:
                    shutil.copy2(src_path, dest_path)
                    if mode is not None:
                        os.chmod(dest_path, mode)
                    if owner is not None or group is not None:
                        try:
                            shutil.chown(dest_path, owner, group)
                        except Exception:
                            pass
                except FileNotFoundError:
                    errors.append(f"install: cannot stat '{src}': No such file or directory")
                except OSError as e:
                    errors.append(f"install: cannot copy '{src}': {e}")

            if errors:
                output = "\n".join(errors)
                if params.output_path:
                    with open(params.output_path, "w", encoding="utf-8") as f:
                        f.write(output)
                    output = f"saved to file `{params.output_path}`"
                return ToolError(message=output, output=output, brief="install failed")

            return ToolOk(output="")
        except Exception as e:
            return ToolError(message=str(e), output="", brief="install failed")
