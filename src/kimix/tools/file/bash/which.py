"""which tool - locate a command."""
import os
import platform
import shutil

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


_IS_WINDOWS: bool = platform.system() == "Windows"
_PATHEXT: list[str] = (
    os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep) if _IS_WINDOWS else [""]
)


class Which(CallableTool2[Params]):
    name: str = "Which"
    description: str = "Locate a command in the user's path."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            all_matches = False
            commands = []
            for arg in params.args:
                if arg == "-a" or arg == "--all":
                    all_matches = True
                elif not arg.startswith("-"):
                    commands.append(arg)

            if not commands:
                return ToolError(message="which: no command specified", output="", brief="no command")

            results = []
            path_env = os.environ.get("PATH", "")
            path_dirs = path_env.split(os.pathsep)
            pathext = _PATHEXT

            for cmd in commands:
                found = []
                if not all_matches:
                    loc = shutil.which(cmd, path=path_env)
                    if loc:
                        found.append(loc)
                else:
                    for d in path_dirs:
                        for ext in pathext:
                            candidate = os.path.join(d, cmd + ext) if ext else os.path.join(d, cmd)
                            try:
                                if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                                    found.append(candidate)
                            except (OSError, PermissionError):
                                continue

                if found:
                    results.extend(found)
                else:
                    results.append(f"which: no {cmd} in ({path_env})")

            output = "\n".join(results)
            if params.output_path:
                cwd = params.cwd or os.getcwd()
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="which failed")
