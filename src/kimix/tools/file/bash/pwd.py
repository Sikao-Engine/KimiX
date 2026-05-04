"""pwd tool - print name of current/working directory."""
import os

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from pydantic import BaseModel, Field

from kimix.tools.common import _maybe_export_output_async


class Params(BaseModel):
    path: str = Field(description="Executable path.")
    args: list[str] = Field(default_factory=list, description="Command arguments.")
    timeout: int = Field(default=10, description="Timeout in seconds.")
    cwd: str | None = Field(default=None, description="Working directory (default: current directory).")
    output_path: str | None = Field(default=None, description="Output file path (optional).")


class Pwd(CallableTool2[Params]):
    name: str = "Pwd"
    description: str = "Print name of current/working directory."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            logical = True
            for arg in params.args:
                if arg == "-L":
                    logical = True
                elif arg == "-P":
                    logical = False

            if logical:
                result = params.cwd or os.getcwd()
            else:
                result = os.path.realpath(params.cwd or os.getcwd())

            output = result
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="pwd failed")
