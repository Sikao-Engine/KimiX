"""envsubst tool - substitutes environment variables in shell format strings."""
import os
import re
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class EnvsSubst(CallableTool2[Params]):
    name: str = "EnvsSubst"
    description: str = "Substitutes environment variables in shell format strings."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            paths = [arg for arg in params.args if not arg.startswith("-")]
            if paths:
                target = Path(params.cwd or os.getcwd()) / paths[0] if not Path(paths[0]).is_absolute() else Path(paths[0])
                try:
                    with open(target, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read()
                except FileNotFoundError:
                    return ToolError(message=f"envsubst: {paths[0]}: No such file or directory", output="", brief="envsubst failed")
            else:
                return ToolError(message="envsubst: missing input", output="", brief="missing operand")

            def repl(m):
                var = m.group(1) or m.group(2)
                return os.environ.get(var, "")

            output = re.sub(r"\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)", repl, text)
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
            return ToolError(message=str(e), output="", brief="envsubst failed")
