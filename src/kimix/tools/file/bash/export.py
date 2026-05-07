"""export tool - set environment variables."""
import os

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params

from kimix.tools.common import _maybe_export_output_async


class Export(CallableTool2[Params]):
    name: str = "Export"
    description: str = "Set environment variables for the current process."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            env = os.environ
            results: list[str] = []
            env_lines: list[str] | None = None

            for arg in params.args:
                if "=" in arg:
                    key, value = arg.split("=", 1)
                    key = key.strip()
                    if key:
                        env[key] = value
                        results.append(f"export {key}={value}")
                        env_lines = None
                elif arg == "-p":
                    if env_lines is None:
                        env_lines = [f"export {k}={v}" for k, v in sorted(env.items())]
                    results.extend(env_lines)
                elif not arg.startswith("-"):
                    key = arg.strip()
                    value = env.get(key)
                    if value is not None:
                        results.append(f"export {key}={value}")
                    else:
                        results.append(f"export: {key}: not set")

            if not results:
                if env_lines is None:
                    env_lines = [f"export {k}={v}" for k, v in sorted(env.items())]
                results.extend(env_lines)

            output = "\n".join(results)
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="export failed")
