"""cp tool - copy files and directories."""
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


class Cp(CallableTool2[Params]):
    name: str = "Cp"
    description: str = "Copy files and directories."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            recursive = False
            paths = []
            for arg in params.args:
                if arg == "-r" or arg == "-R" or arg == "--recursive":
                    recursive = True
                elif not arg.startswith("-"):
                    paths.append(arg)

            if len(paths) < 2:
                return ToolError(message="cp: missing file operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            sources = paths[:-1]
            dest = paths[-1]
            dest_path = Path(cwd) / dest if not Path(dest).is_absolute() else Path(dest)

            errors = []
            if len(sources) > 1 or (dest_path.exists() and dest_path.is_dir()):
                # copy into directory
                if not dest_path.exists():
                    errors.append(f"cp: target '{dest}' is not a directory")
                else:
                    for src in sources:
                        src_path = Path(cwd) / src if not Path(src).is_absolute() else Path(src)
                        try:
                            if src_path.is_dir():
                                if recursive:
                                    shutil.copytree(src_path, dest_path / src_path.name)
                                else:
                                    errors.append(f"cp: -r not specified; omitting directory '{src}'")
                            else:
                                shutil.copy2(src_path, dest_path)
                        except FileNotFoundError:
                            errors.append(f"cp: cannot stat '{src}': No such file or directory")
                        except OSError as e:
                            errors.append(f"cp: cannot copy '{src}': {e}")
            else:
                src = sources[0]
                src_path = Path(cwd) / src if not Path(src).is_absolute() else Path(src)
                try:
                    if src_path.is_dir():
                        if recursive:
                            shutil.copytree(src_path, dest_path)
                        else:
                            errors.append(f"cp: -r not specified; omitting directory '{src}'")
                    else:
                        shutil.copy2(src_path, dest_path)
                except FileNotFoundError:
                    errors.append(f"cp: cannot stat '{src}': No such file or directory")
                except OSError as e:
                    errors.append(f"cp: cannot copy '{src}': {e}")

            if errors:
                output = "\n".join(errors)
                if params.output_path:
                    with open(params.output_path, "w", encoding="utf-8") as f:
                        f.write(output)
                    output = f"saved to file `{params.output_path}`"
                return ToolError(message=output, output=output, brief="cp failed")

            output = ""
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="cp failed")
