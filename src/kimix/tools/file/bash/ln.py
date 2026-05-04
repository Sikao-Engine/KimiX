"""ln tool - make links between files."""
import os
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


class Ln(CallableTool2[Params]):
    name: str = "Ln"
    description: str = "Make links between files."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            symbolic = False
            force = False
            paths = []
            for arg in params.args:
                if arg == "-s" or arg == "--symbolic":
                    symbolic = True
                elif arg == "-f" or arg == "--force":
                    force = True
                elif not arg.startswith("-"):
                    paths.append(arg)

            if len(paths) < 2:
                return ToolError(message="ln: missing file operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            sources = paths[:-1]
            dest = paths[-1]
            dest_path = Path(cwd) / dest if not Path(dest).is_absolute() else Path(dest)

            errors = []
            if len(sources) > 1 or (dest_path.exists() and dest_path.is_dir()):
                if not dest_path.exists() or not dest_path.is_dir():
                    errors.append(f"ln: target '{dest}' is not a directory")
                else:
                    for src in sources:
                        src_path = Path(cwd) / src if not Path(src).is_absolute() else Path(src)
                        link_path = dest_path / src_path.name
                        if force and link_path.exists():
                            link_path.unlink()
                        try:
                            if symbolic:
                                link_path.symlink_to(src_path)
                            else:
                                os.link(src_path, link_path)
                        except OSError as e:
                            errors.append(f"ln: failed to create link '{src}': {e}")
            else:
                src = sources[0]
                src_path = Path(cwd) / src if not Path(src).is_absolute() else Path(src)
                if force and dest_path.exists():
                    dest_path.unlink()
                try:
                    if symbolic:
                        dest_path.symlink_to(src_path)
                    else:
                        os.link(src_path, dest_path)
                except OSError as e:
                    errors.append(f"ln: failed to create link '{src}': {e}")

            if errors:
                output = "\n".join(errors)
                if params.output_path:
                    with open(params.output_path, "w", encoding="utf-8") as f:
                        f.write(output)
                    output = f"saved to file `{params.output_path}`"
                return ToolError(message=output, output=output, brief="ln failed")

            output = ""
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="ln failed")
