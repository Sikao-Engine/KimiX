"""gunzip tool - decompress files."""
import gzip
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


class Gunzip(CallableTool2[Params]):
    name: str = "Gunzip"
    description: str = "Decompress files compressed with gzip."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            keep = False
            paths = []
            for arg in params.args:
                if arg in ("-k", "--keep"):
                    keep = True
                elif not arg.startswith("-"):
                    paths.append(arg)

            if not paths:
                return ToolError(message="gunzip: missing file operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            errors = []
            for p in paths:
                target = Path(cwd) / p if not Path(p).is_absolute() else Path(p)
                try:
                    out_path = target.with_suffix("") if target.suffix == ".gz" else target.parent / (target.name + ".decompressed")
                    with gzip.open(target, "rb") as src:
                        data = src.read()
                    with open(out_path, "wb") as dst:
                        dst.write(data)
                    if not keep:
                        target.unlink()
                except FileNotFoundError:
                    errors.append(f"gunzip: {p}: No such file or directory")
                except OSError as e:
                    errors.append(f"gunzip: {p}: {e}")

            if errors:
                output = "\n".join(errors)
                if params.output_path:
                    with open(params.output_path, "w", encoding="utf-8") as f:
                        f.write(output)
                    output = f"saved to file `{params.output_path}`"
                return ToolError(message=output, output=output, brief="gunzip failed")

            output = ""
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="gunzip failed")
