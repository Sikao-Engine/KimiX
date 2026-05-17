"""base64 tool - base64 encode/decode."""
import base64
import os
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Base64(CallableTool2[Params]):
    name: str = "Base64"
    description: str = "Base64 encode/decode."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            decode = False
            paths = []
            for arg in params.args:
                if arg == "-d" or arg == "--decode":
                    decode = True
                elif not arg.startswith("-"):
                    paths.append(arg)

            if paths:
                target = Path(paths[0])
                if not target.is_absolute():
                    target = Path(params.cwd or os.getcwd()) / target
                try:
                    with open(target, "rb") as f:
                        data = f.read()
                except FileNotFoundError:
                    return ToolError(message=f"base64: {paths[0]}: No such file or directory", output="", brief="base64 failed")
            else:
                data = b""

            if decode:
                result = base64.b64decode(data)
                output = result.decode("utf-8", errors="replace")
            else:
                output = base64.b64encode(data).decode("ascii")

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
            return ToolError(message=str(e), output="", brief="base64 failed")
