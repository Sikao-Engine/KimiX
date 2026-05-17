"""tar tool - manipulate tape archives."""
import os
import tarfile
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async

class Tar(CallableTool2[Params]):
    name: str = "Tar"
    description: str = "Manipulate tape archives."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            create_mode = False
            extract_mode = False
            list_mode = False
            verbose = False
            file_path = None
            paths = []
            compression = ""
            i = 0
            while i < len(params.args):
                arg = params.args[i]
                if arg in ("-c", "--create"):
                    create_mode = True
                elif arg in ("-x", "--extract", "--get"):
                    extract_mode = True
                elif arg in ("-t", "--list"):
                    list_mode = True
                elif arg in ("-v", "--verbose"):
                    verbose = True
                elif arg in ("-f", "--file"):
                    i += 1
                    if i < len(params.args):
                        file_path = params.args[i]
                elif arg.startswith("-") and len(arg) > 1:
                    for ch in arg[1:]:
                        if ch == "c":
                            create_mode = True
                        elif ch == "x":
                            extract_mode = True
                        elif ch == "t":
                            list_mode = True
                        elif ch == "v":
                            verbose = True
                        elif ch == "z":
                            compression = "gz"
                        elif ch == "j":
                            compression = "bz2"
                        elif ch == "J":
                            compression = "xz"
                        elif ch == "f":
                            pass  # next arg is file
                elif not arg.startswith("-"):
                    if file_path is None:
                        file_path = arg
                    else:
                        paths.append(arg)
                i += 1

            if file_path is None:
                return ToolError(message="tar: missing archive path", output="", brief="missing archive")

            cwd = params.cwd or os.getcwd()
            if params.output_path:
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")

            is_prot, reason = _is_protected_path(file_path, cwd)
            if is_prot:
                return ToolError(message=reason, output=reason, brief="protected path")

            for p in paths:
                is_prot, reason = _is_protected_path(p, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")

            archive = Path(cwd) / file_path if not Path(file_path).is_absolute() else Path(file_path)

            if list_mode:
                if not archive.exists():
                    return ToolError(message=f"tar: {file_path}: No such file", output="", brief="file not found")
                mode = f"r:{compression}" if compression else "r"
                with tarfile.open(archive, mode) as tf:
                    results = [m.name for m in tf.getmembers()]
                output = "\n".join(results)
            elif extract_mode:
                if not archive.exists():
                    return ToolError(message=f"tar: {file_path}: No such file", output="", brief="file not found")
                mode = f"r:{compression}" if compression else "r"
                with tarfile.open(archive, mode) as tf:
                    tf.extractall(path=cwd)
                output = f"Extracted {file_path}"
            elif create_mode:
                mode = f"w:{compression}" if compression else "w"
                with tarfile.open(archive, mode) as tf:
                    for p in paths:
                        target = Path(cwd) / p if not Path(p).is_absolute() else Path(p)
                        tf.add(target, arcname=target.name)
                output = f"Created {file_path}"
            else:
                return ToolError(message="tar: missing operation mode", output="", brief="missing mode")

            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="tar failed")
