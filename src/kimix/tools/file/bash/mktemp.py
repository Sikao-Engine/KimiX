"""mktemp tool - create a temporary file or directory."""
import os
import random
import string
import tempfile
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async

_ALPHANUM = string.ascii_letters + string.digits


class Mktemp(CallableTool2[Params]):
    name: str = "Mktemp"
    description: str = "Create a temporary file or directory."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            directory = False
            dry_run = False
            suffix = ""
            prefix = "tmp."
            dir_path = None
            args = params.args
            i = 0
            n = len(args)
            while i < n:
                arg = args[i]
                if arg == "-d" or arg == "--directory":
                    directory = True
                elif arg == "-u" or arg == "--dry-run":
                    dry_run = True
                elif arg == "--suffix":
                    i += 1
                    if i < n:
                        suffix = args[i]
                elif arg.startswith("--suffix="):
                    suffix = arg[9:]
                elif arg == "-p" or arg == "--tmpdir":
                    i += 1
                    if i < n:
                        dir_path = args[i]
                elif arg.startswith("-p"):
                    dir_path = arg[2:]
                elif arg[0] != "-":
                    if "X" in arg:
                        prefix = arg[: arg.rfind("X") + 1]
                i += 1

            cwd = params.cwd or os.getcwd()
            if params.output_path:
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")

            if dir_path is None:
                dir_path = tempfile.gettempdir()

            is_prot, reason = _is_protected_path(dir_path, cwd)
            if is_prot:
                return ToolError(message=reason, output=reason, brief="protected path")

            if dry_run:
                name = prefix + "".join(random.choices(_ALPHANUM, k=6)) + suffix
                output = str(Path(dir_path) / name)
            elif directory:
                output = tempfile.mkdtemp(suffix=suffix, prefix=prefix, dir=dir_path)
            else:
                fd, output = tempfile.mkstemp(suffix=suffix, prefix=prefix, dir=dir_path)
                os.close(fd)

            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="mktemp failed")
