"""wc tool - print newline, word, and byte counts for each file."""
import os
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params

from kimix.tools.common import _maybe_export_output_async

class Wc(CallableTool2[Params]):
    name: str = "Wc"
    description: str = "Print newline, word, and byte counts for each file."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            show_lines = True
            show_words = True
            show_bytes = True
            paths = []
            for arg in params.args:
                if arg == "-l":
                    show_words = False
                    show_bytes = False
                elif arg == "-w":
                    show_lines = False
                    show_bytes = False
                elif arg == "-c":
                    show_lines = False
                    show_words = False
                elif not arg.startswith("-"):
                    paths.append(arg)

            if not paths:
                return ToolError(message="wc: missing file operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            results = []
            total_lines = 0
            total_words = 0
            total_bytes = 0

            for p in paths:
                target = Path(cwd) / p if not Path(p).is_absolute() else Path(p)
                try:
                    with open(target, "rb") as f:
                        content = f.read()
                    text = content.decode("utf-8", errors="replace")
                    lines = text.count("\n")
                    words = len(text.split())
                    nbytes = len(content)
                    total_lines += lines
                    total_words += words
                    total_bytes += nbytes
                    cols = []
                    if show_lines:
                        cols.append(str(lines))
                    if show_words:
                        cols.append(str(words))
                    if show_bytes:
                        cols.append(str(nbytes))
                    cols.append(p)
                    results.append(" ".join(cols))
                except FileNotFoundError:
                    results.append(f"wc: {p}: No such file or directory")
                except OSError as e:
                    results.append(f"wc: {p}: {e}")

            if len(paths) > 1:
                cols = []
                if show_lines:
                    cols.append(str(total_lines))
                if show_words:
                    cols.append(str(total_words))
                if show_bytes:
                    cols.append(str(total_bytes))
                cols.append("total")
                results.append(" ".join(cols))

            output = "\n".join(results)
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="wc failed")
