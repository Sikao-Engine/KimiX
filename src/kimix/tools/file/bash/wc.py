"""wc tool - print newline, word, and byte counts for each file."""
import os
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async

CHUNK_SIZE = 1024 * 1024  # 1MB


def _count_all(path: Path) -> tuple[int, int, int]:
    lines = 0
    words = 0
    nbytes = 0
    prev_in_word = False
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            nbytes += len(chunk)
            lines += chunk.count(10)  # ord('\n')
            starts_in_word = chunk[0] > 0x20
            if starts_in_word and prev_in_word:
                words -= 1
            words += len(chunk.split())
            prev_in_word = chunk[-1] > 0x20
    return lines, words, nbytes


def _count_lines(path: Path) -> int:
    lines = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            lines += chunk.count(10)
    return lines


def _count_words(path: Path) -> int:
    words = 0
    prev_in_word = False
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            starts_in_word = chunk[0] > 0x20
            if starts_in_word and prev_in_word:
                words -= 1
            words += len(chunk.split())
            prev_in_word = chunk[-1] > 0x20
    return words


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
                    if show_lines and show_words and show_bytes:
                        lines, words, nbytes = _count_all(target)
                    elif show_lines and not show_words and not show_bytes:
                        lines = _count_lines(target)
                        words = 0
                        nbytes = 0
                    elif show_words and not show_lines and not show_bytes:
                        lines = 0
                        words = _count_words(target)
                        nbytes = 0
                    elif show_bytes and not show_lines and not show_words:
                        lines = 0
                        words = 0
                        nbytes = target.stat().st_size
                    else:
                        lines, words, nbytes = _count_all(target)

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
                cwd = params.cwd or os.getcwd()
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="wc failed")
