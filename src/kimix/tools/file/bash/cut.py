"""cut tool - remove sections from each line of files."""
import os
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params

from kimix.tools.common import _maybe_export_output_async

def _parse_fields(spec: str) -> list[int]:
    fields = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            start = int(a) if a else 1
            end = int(b) if b else None
            if end is None:
                fields.append((start, None))
            else:
                fields.append((start, end))
        else:
            fields.append((int(part), int(part)))
    return fields

class Cut(CallableTool2[Params]):
    name: str = "Cut"
    description: str = "Remove sections from each line of files."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            delimiter = "\t"
            field_spec = None
            byte_spec = None
            char_spec = None
            paths = []
            i = 0
            while i < len(params.args):
                arg = params.args[i]
                if arg == "-d":
                    i += 1
                    if i < len(params.args):
                        delimiter = params.args[i]
                elif arg.startswith("-d"):
                    delimiter = arg[2:]
                elif arg == "-f":
                    i += 1
                    if i < len(params.args):
                        field_spec = _parse_fields(params.args[i])
                elif arg.startswith("-f"):
                    field_spec = _parse_fields(arg[2:])
                elif arg == "-b":
                    i += 1
                    if i < len(params.args):
                        byte_spec = _parse_fields(params.args[i])
                elif arg.startswith("-b"):
                    byte_spec = _parse_fields(arg[2:])
                elif arg == "-c":
                    i += 1
                    if i < len(params.args):
                        char_spec = _parse_fields(params.args[i])
                elif arg.startswith("-c"):
                    char_spec = _parse_fields(arg[2:])
                elif not arg.startswith("-"):
                    paths.append(arg)
                i += 1

            if not paths:
                return ToolError(message="cut: missing file operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            results = []

            for p in paths:
                target = Path(cwd) / p if not Path(p).is_absolute() else Path(p)
                try:
                    with open(target, "r", encoding="utf-8", errors="replace") as f:
                        for line in f:
                            line = line.rstrip("\n\r")
                            if field_spec is not None:
                                parts = line.split(delimiter)
                                out = []
                                for start, end in field_spec:
                                    if end is None:
                                        out.extend(parts[start - 1 :])
                                    else:
                                        out.extend(parts[start - 1 : end])
                                results.append(delimiter.join(out))
                            elif byte_spec is not None:
                                encoded = line.encode("utf-8")
                                out = []
                                for start, end in byte_spec:
                                    if end is None:
                                        out.append(encoded[start - 1 :])
                                    else:
                                        out.append(encoded[start - 1 : end])
                                results.append(b"".join(out).decode("utf-8", errors="replace"))
                            elif char_spec is not None:
                                chars = list(line)
                                out = []
                                for start, end in char_spec:
                                    if end is None:
                                        out.extend(chars[start - 1 :])
                                    else:
                                        out.extend(chars[start - 1 : end])
                                results.append("".join(out))
                            else:
                                results.append(line)
                except FileNotFoundError:
                    results.append(f"cut: {p}: No such file or directory")
                except OSError as e:
                    results.append(f"cut: {p}: {e}")

            output = "\n".join(results)
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="cut failed")
