"""du tool - estimate file space usage."""
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


def _dir_size(p: Path) -> int:
    total = 0
    try:
        if p.is_dir() and not p.is_symlink():
            for entry in p.iterdir():
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    total += _dir_size(entry)
                else:
                    try:
                        total += entry.stat().st_size
                    except OSError:
                        pass
        else:
            try:
                total += p.stat().st_size
            except OSError:
                pass
    except PermissionError:
        pass
    return total


class Du(CallableTool2[Params]):
    name: str = "Du"
    description: str = "Estimate file space usage."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            human_readable = False
            summarize = False
            max_depth = None
            paths = []
            i = 0
            while i < len(params.args):
                arg = params.args[i]
                if arg == "-h" or arg == "--human-readable":
                    human_readable = True
                elif arg == "-s" or arg == "--summarize":
                    summarize = True
                elif arg == "-d" or arg == "--max-depth":
                    i += 1
                    if i < len(params.args):
                        max_depth = int(params.args[i])
                elif arg.startswith("--max-depth="):
                    max_depth = int(arg.split("=", 1)[1])
                elif not arg.startswith("-"):
                    paths.append(arg)
                i += 1

            if not paths:
                paths = ["."]

            def _fmt(size: int) -> str:
                if not human_readable:
                    # du outputs in 1024-byte blocks by default
                    return str((size + 1023) // 1024)
                for unit in ["K", "M", "G", "T", "P"]:
                    if size < 1024:
                        return f"{size:.1f}{unit}" if unit != "K" else f"{size}K"
                    size /= 1024
                return f"{size:.1f}E"

            cwd = params.cwd or os.getcwd()
            results = []

            def _du(p: Path, depth: int):
                size = _dir_size(p)
                if summarize:
                    results.append(f"{_fmt(size)}\t{p}")
                elif max_depth is not None and depth >= max_depth:
                    results.append(f"{_fmt(size)}\t{p}")
                else:
                    if p.is_dir() and not p.is_symlink():
                        results.append(f"{_fmt(size)}\t{p}")
                        if not summarize:
                            try:
                                for entry in sorted(p.iterdir(), key=lambda e: e.name):
                                    if entry.is_dir() and not entry.is_symlink():
                                        _du(entry, depth + 1)
                                    else:
                                        esize = _dir_size(entry)
                                        results.append(f"{_fmt(esize)}\t{entry}")
                            except PermissionError:
                                pass
                    else:
                        results.append(f"{_fmt(size)}\t{p}")

            for p in paths:
                target = Path(cwd) / p if not Path(p).is_absolute() else Path(p)
                _du(target, 0)

            output = "\n".join(results)
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="du failed")
