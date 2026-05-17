"""find tool - search for files in a directory hierarchy."""
import fnmatch
import os
import re
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Find(CallableTool2[Params]):
    name: str = "Find"
    description: str = "Search for files in a directory hierarchy."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            cwd = params.cwd or os.getcwd()
            paths = []
            name_pattern = None
            ftype = None
            maxdepth = None
            i = 0
            while i < len(params.args):
                arg = params.args[i]
                if arg == "-name":
                    i += 1
                    if i < len(params.args):
                        name_pattern = params.args[i]
                elif arg == "-type":
                    i += 1
                    if i < len(params.args):
                        ftype = params.args[i]
                elif arg == "-maxdepth":
                    i += 1
                    if i < len(params.args):
                        maxdepth = int(params.args[i])
                elif not arg.startswith("-"):
                    paths.append(arg)
                i += 1

            if not paths:
                paths = ["."]

            if params.output_path:
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")


            results = []
            name_re = None
            if name_pattern is not None:
                name_re = re.compile(fnmatch.translate(name_pattern))

            def _match(entry_name: str, is_dir: bool, is_file: bool, is_symlink: bool) -> bool:
                if name_re is not None and not name_re.match(entry_name):
                    return False
                if ftype == "d" and not is_dir:
                    return False
                if ftype == "f" and not is_file:
                    return False
                if ftype == "l" and not is_symlink:
                    return False
                return True

            def _walk(base_path: str, depth: int):
                try:
                    for entry in os.scandir(base_path):
                        is_dir = entry.is_dir()
                        is_file = entry.is_file()
                        is_symlink = entry.is_symlink()
                        if _match(entry.name, is_dir, is_file, is_symlink):
                            results.append(entry.path)
                        if is_dir and (maxdepth is None or depth < maxdepth):
                            _walk(entry.path, depth + 1)
                except PermissionError:
                    pass
                except OSError:
                    pass

            for p in paths:
                target = Path(cwd) / p if not Path(p).is_absolute() else Path(p)
                target_str = str(target)
                if target.exists():
                    is_dir = target.is_dir()
                    is_file = target.is_file()
                    is_symlink = target.is_symlink()
                    if is_dir:
                        results.append(target_str)
                        _walk(target_str, 1)
                    else:
                        if _match(target.name, is_dir, is_file, is_symlink):
                            results.append(target_str)
                else:
                    results.append(f"find: '{p}': No such file or directory")

            output = "\n".join(results)
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="find failed")
