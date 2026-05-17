"""rm tool - remove files or directories."""
import os
import shutil
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async

_RECURSIVE_FLAGS = frozenset({"-r", "-R", "--recursive"})
_FORCE_FLAGS = frozenset({"-f", "--force"})
_BOTH_FLAGS = frozenset({"-rf", "-fr", "-Rf", "-fR", "--recursive --force", "--force --recursive"})

_DANGEROUS_LITERALS = frozenset({".", "..", "*", "/", "~", "~/", "/*"})


def _is_dangerous_rm_target(path: str, cwd: str) -> tuple[bool, str]:
    """Check if a path is dangerous for rm -rf. Returns (is_dangerous, reason)."""
    if path in _DANGEROUS_LITERALS:
        return True, f"rm: refusing to remove '{path}': dangerous target with recursive+force"

    target = Path(path) if os.path.isabs(path) else Path(cwd) / path
    try:
        resolved = target.resolve()
    except (OSError, ValueError):
        return False, ""

    cwd_resolved = Path(cwd).resolve()
    try:
        if resolved == cwd_resolved:
            return True, f"rm: refusing to remove '{path}': targets current working directory"
    except (OSError, ValueError):
        pass

    try:
        home = Path.home().resolve()
        if resolved == home:
            return True, f"rm: refusing to remove '{path}': targets home directory"
    except (OSError, ValueError):
        pass

    try:
        root = Path("/").resolve()
        if resolved == root:
            return True, f"rm: refusing to remove '{path}': targets root directory"
    except (OSError, ValueError):
        pass

    return False, ""


class Rm(CallableTool2[Params]):
    name: str = "Rm"
    description: str = "Remove files or directories."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            recursive = False
            force = False
            paths = []
            for arg in params.args:
                if arg in _RECURSIVE_FLAGS:
                    recursive = True
                elif arg in _FORCE_FLAGS:
                    force = True
                elif arg in _BOTH_FLAGS:
                    recursive = True
                    force = True
                elif not arg.startswith("-"):
                    paths.append(arg)

            if not paths:
                return ToolError(message="rm: missing operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            if params.output_path:
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")

            for p in paths:
                is_prot, reason = _is_protected_path(p, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")

            if recursive and force:
                for p in paths:
                    is_dang, reason = _is_dangerous_rm_target(p, cwd)
                    if is_dang:
                        return ToolError(message=reason, output=reason, brief="dangerous rm target")

            errors = []
            for p in paths:
                target = os.path.join(cwd, p) if not os.path.isabs(p) else p
                try:
                    if os.path.isdir(target):
                        if recursive:
                            shutil.rmtree(target)
                        elif not force:
                            errors.append(f"rm: cannot remove '{p}': Is a directory")
                    else:
                        os.remove(target)
                except FileNotFoundError:
                    if not force:
                        errors.append(f"rm: cannot remove '{p}': No such file or directory")
                except OSError as e:
                    if not force:
                        errors.append(f"rm: cannot remove '{p}': {e}")

            if errors:
                output = "\n".join(errors)
                if params.output_path:
                    with open(params.output_path, "w", encoding="utf-8") as f:
                        f.write(output)
                    output = f"saved to file `{params.output_path}`"
                return ToolError(message=output, output=output, brief="rm failed")

            output = ""
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="rm failed")
