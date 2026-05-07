"""tree tool - list contents of directories in a tree-like format."""
import os
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params

from kimix.tools.common import _maybe_export_output_async


def _tree(
    p: str | Path,
    prefix: str = "",
    max_depth: int | None = None,
    depth: int = 0,
    show_files: bool = True,
    out: list[str] | None = None,
) -> list[str]:
    if out is None:
        out = []
    try:
        entries = sorted(
            os.scandir(p),
            key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()),
        )
    except PermissionError:
        out.append(f"{prefix}[permission denied]")
        return out
    except OSError:
        return out

    last_idx = len(entries) - 1
    for i, entry in enumerate(entries):
        is_last = i == last_idx
        connector = "└── " if is_last else "├── "
        out.append(f"{prefix}{connector}{entry.name}")
        if entry.is_dir(follow_symlinks=False) and not entry.is_symlink():
            if max_depth is None or depth < max_depth:
                extension = "    " if is_last else "│   "
                _tree(entry.path, prefix + extension, max_depth, depth + 1, show_files, out)
    return out


class Tree(CallableTool2[Params]):
    name: str = "Tree"
    description: str = "List contents of directories in a tree-like format."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            paths = []
            max_depth = None
            i = 0
            while i < len(params.args):
                arg = params.args[i]
                if arg in ("-L", "--max-depth"):
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

            cwd = params.cwd or os.getcwd()
            results = []

            for p in paths:
                target = Path(cwd) / p if not Path(p).is_absolute() else Path(p)
                results.append(str(target))
                if target.is_dir():
                    _tree(str(target), max_depth=max_depth, out=results)
                else:
                    results.append("[error opening dir]")
                results.append("")

            output = "\n".join(results).rstrip()
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="tree failed")
