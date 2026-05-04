"""rm tool - remove files or directories."""
import os
import shutil
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
                if arg == "-r" or arg == "-R" or arg == "--recursive":
                    recursive = True
                elif arg == "-f" or arg == "--force":
                    force = True
                elif arg == "-rf" or arg == "-fr":
                    recursive = True
                    force = True
                elif not arg.startswith("-"):
                    paths.append(arg)

            if not paths:
                return ToolError(message="rm: missing operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            errors = []
            for p in paths:
                target = Path(cwd) / p if not Path(p).is_absolute() else Path(p)
                try:
                    if target.is_dir():
                        if recursive:
                            shutil.rmtree(target)
                        else:
                            if not force:
                                errors.append(f"rm: cannot remove '{p}': Is a directory")
                    else:
                        target.unlink()
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
