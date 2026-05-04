"""find tool - search for files in a directory hierarchy."""
import fnmatch
import os
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params

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

            results = []

            def _walk(p: Path, depth: int):
                try:
                    for entry in p.iterdir():
                        matched = True
                        if name_pattern is not None:
                            matched = fnmatch.fnmatch(entry.name, name_pattern)
                        if matched and ftype is not None:
                            if ftype == "d" and not entry.is_dir():
                                matched = False
                            elif ftype == "f" and not entry.is_file():
                                matched = False
                            elif ftype == "l" and not entry.is_symlink():
                                matched = False
                        if matched:
                            results.append(str(entry))
                        if entry.is_dir() and (maxdepth is None or depth < maxdepth):
                            _walk(entry, depth + 1)
                except PermissionError:
                    pass
                except OSError:
                    pass

            for p in paths:
                target = Path(cwd) / p if not Path(p).is_absolute() else Path(p)
                if target.exists():
                    if target.is_dir():
                        results.append(str(target))
                        _walk(target, 1)
                    else:
                        # Check if file itself matches
                        matched = True
                        if name_pattern is not None:
                            matched = fnmatch.fnmatch(target.name, name_pattern)
                        if matched and ftype is not None:
                            if ftype == "d" and not target.is_dir():
                                matched = False
                            elif ftype == "f" and not target.is_file():
                                matched = False
                            elif ftype == "l" and not target.is_symlink():
                                matched = False
                        if matched:
                            results.append(str(target))
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
