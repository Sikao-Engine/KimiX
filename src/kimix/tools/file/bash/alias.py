"""alias tool - define or display aliases."""
import os

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async

# Simple in-memory alias store
_ALIAS_STORE: dict[str, str] = {}


class Alias(CallableTool2[Params]):
    name: str = "Alias"
    description: str = "Define or display aliases."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            args = params.args
            if not args:
                # List all aliases
                lines = [f"alias {k}='{v}'" for k, v in _ALIAS_STORE.items()]
                output = "\n".join(lines)
            else:
                for arg in args:
                    if "=" in arg:
                        name, value = arg.split("=", 1)
                        value = value.strip("'\"")
                        _ALIAS_STORE[name] = value
                    else:
                        name = arg
                        if name in _ALIAS_STORE:
                            output = f"alias {name}='{_ALIAS_STORE[name]}'"
                        else:
                            return ToolError(message=f"alias: {name}: not found", output="", brief="alias not found")
                output = ""

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
            return ToolError(message=str(e), output="", brief="alias failed")
