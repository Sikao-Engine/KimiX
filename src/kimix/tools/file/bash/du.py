"""du tool - estimate file space usage."""
import os
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params

from kimix.tools.common import _maybe_export_output_async


def _walk(
    p: Path,
    depth: int,
    summarize: bool,
    max_depth: int | None,
    _fmt,
    collect_output: bool,
) -> tuple[int, list[str]]:
    """Single-pass walk computing size and building output.

    Returns (total_size, results).
    """
    results: list[str] = []
    total = 0
    try:
        is_dir = p.is_dir() and not p.is_symlink()
        if is_dir:
            entries = []
            with os.scandir(p) as it:
                for entry in it:
                    if entry.is_symlink():
                        continue
                    entries.append(entry)
            entries.sort(key=lambda e: e.name)

            child_collect = (
                collect_output
                and not summarize
                and (max_depth is None or depth + 1 <= max_depth)
            )

            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    subtotal, subresults = _walk(
                        Path(entry.path),
                        depth + 1,
                        summarize,
                        max_depth,
                        _fmt,
                        child_collect,
                    )
                    total += subtotal
                    results.extend(subresults)
                else:
                    try:
                        esize = entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        esize = 0
                    total += esize
                    if child_collect:
                        results.append(f"{_fmt(esize)}\t{entry.path}")

            if collect_output:
                results.append(f"{_fmt(total)}\t{p}")
        else:
            try:
                total = p.stat().st_size
            except OSError:
                pass
            if collect_output:
                results.append(f"{_fmt(total)}\t{p}")
    except PermissionError:
        pass
    return total, results


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
            results: list[str] = []

            for p in paths:
                target = Path(cwd) / p if not Path(p).is_absolute() else Path(p)
                _, subresults = _walk(target, 0, summarize, max_depth, _fmt, True)
                results.extend(subresults)

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
