"""zip tool - package and compress files."""
import os
import zipfile
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from pydantic import BaseModel, Field

from kimix.tools.common import _maybe_export_output_async


class Params(BaseModel):
    path: str = Field(description="Executable path.")
    args: list[str] = Field(default_factory=list, description="Command arguments.")
    timeout: int = Field(default=10, description="Timeout in seconds.")
    cwd: str | None = Field(default=None, description="Working directory (default: current directory).")
    output_path: str | None = Field(default=None, description="Output file path (optional).")


class Zip(CallableTool2[Params]):
    name: str = "Zip"
    description: str = "Package and compress files into a zip archive."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            recursive = False
            paths = []
            for arg in params.args:
                if arg == "-r":
                    recursive = True
                elif not arg.startswith("-"):
                    paths.append(arg)

            if len(paths) < 2:
                return ToolError(message="zip: missing operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            archive = Path(cwd) / paths[0] if not Path(paths[0]).is_absolute() else Path(paths[0])
            sources = paths[1:]

            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in sources:
                    target = Path(cwd) / p if not Path(p).is_absolute() else Path(p)
                    if target.is_dir() and recursive:
                        for f in target.rglob("*"):
                            arcname = f.relative_to(target.parent)
                            zf.write(f, arcname)
                    else:
                        zf.write(target, arcname=target.name)

            output = f"Added to {archive}"
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="zip failed")
