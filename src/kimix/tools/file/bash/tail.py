"""tail tool - output the last part of files."""
import os
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


def _tail_file(path: Path, lines_count: int) -> str:
    """Return the last *lines_count* lines of *path* efficiently.

    Reads backwards from the end of the file in chunks so that large files
    are handled in constant time / memory regardless of total line count.
    """
    chunk_size = 8192
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        if size == 0 or lines_count <= 0:
            return ""

        lines: list[str] = []
        remaining = lines_count
        pos = size
        buffer = b""

        while pos > 0 and remaining > 0:
            read_size = min(chunk_size, pos)
            pos -= read_size
            f.seek(pos)
            chunk = f.read(read_size)
            buffer = chunk + buffer
            newline_count = buffer.count(b"\n")

            # We need at least *remaining + 1* newlines when we are not at
            # the start of the file so that the first element after split is
            # a complete line rather than a partial chunk.
            if newline_count >= remaining + 1 or pos == 0:
                text = buffer.decode("utf-8", errors="replace")
                # Normalise line endings the same way text mode does.
                text = text.replace("\r\n", "\n").replace("\r", "\n")
                all_lines = text.split("\n")
                had_trailing_newline = text.endswith("\n")
                if had_trailing_newline and all_lines and all_lines[-1] == "":
                    all_lines = all_lines[:-1]

                # Drop a partial leading line unless we read from the start.
                if pos > 0 and not text.startswith("\n"):
                    all_lines = all_lines[1:]

                reconstructed: list[str] = []
                for i, line in enumerate(all_lines):
                    if i < len(all_lines) - 1:
                        reconstructed.append(line + "\n")
                    elif had_trailing_newline:
                        reconstructed.append(line + "\n")
                    else:
                        reconstructed.append(line)

                selected = (
                    reconstructed[-remaining:]
                    if len(reconstructed) >= remaining
                    else reconstructed
                )
                lines = selected + lines
                if len(reconstructed) >= remaining:
                    break
                remaining -= len(reconstructed)
                buffer = b""

        return "".join(lines)


class Tail(CallableTool2[Params]):
    name: str = "Tail"
    description: str = "Output the last part of files."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            lines_count = 10
            paths = []
            i = 0
            while i < len(params.args):
                arg = params.args[i]
                if arg == "-n":
                    i += 1
                    if i < len(params.args):
                        lines_count = int(params.args[i])
                elif arg.startswith("-n"):
                    lines_count = int(arg[2:])
                elif not arg.startswith("-"):
                    paths.append(arg)
                i += 1

            cwd = params.cwd or os.getcwd()
            if params.output_path:
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")


            errors = []
            contents = []
            for p in paths:
                target = Path(cwd) / p if not Path(p).is_absolute() else Path(p)
                try:
                    contents.append(_tail_file(target, lines_count))
                except FileNotFoundError:
                    errors.append(
                        f"tail: cannot open '{p}' for reading: No such file or directory"
                    )
                except OSError as e:
                    errors.append(f"tail: {p}: {e}")

            if errors:
                output = "\n".join(errors)
                if params.output_path:
                    with open(
                        params.output_path, "w", encoding="utf-8"
                    ) as f:
                        f.write(output)
                    output = f"saved to file `{params.output_path}`"
                return ToolError(message=output, output=output, brief="tail failed")

            output = "".join(contents)
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="tail failed")
