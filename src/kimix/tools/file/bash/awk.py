"""awk tool - pattern scanning and processing language."""
import os
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Awk(CallableTool2[Params]):
    name: str = "Awk"
    description: str = "Pattern scanning and processing language."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            program = None
            fs = None
            paths = []
            i = 0
            while i < len(params.args):
                arg = params.args[i]
                if arg == "-F":
                    i += 1
                    if i < len(params.args):
                        fs = params.args[i]
                elif arg.startswith("-F"):
                    fs = arg[2:]
                elif arg == "-f":
                    i += 1
                    # ignore program file for simplicity
                elif program is None and not arg.startswith("-"):
                    program = arg
                elif not arg.startswith("-"):
                    paths.append(arg)
                i += 1

            if program is None:
                return ToolError(message="awk: missing program", output="", brief="missing program")

            if not paths:
                return ToolError(message="awk: missing file operand", output="", brief="missing operand")

            delimiter = fs if fs is not None else " "

            # Very simple awk parser: supports {print $1, $2, ...} and {print $0}
            if "{" in program and "}" in program:
                action = program[program.find("{") + 1 : program.find("}")]
            else:
                action = program

            cwd = params.cwd or os.getcwd()
            if params.output_path:
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")


            results = []

            # Pre-parse action to avoid re-parsing per line
            action_starts_with_print = action.startswith("print")
            if action_starts_with_print:
                rest = action[5:].strip()
                if not rest:
                    # {print} or {print } -> print whole line
                    print_mode = "line"
                    print_tokens = None
                else:
                    print_mode = "tokens"
                    print_tokens = []
                    for token in rest.split(","):
                        token = token.strip()
                        if token == "$0":
                            print_tokens.append(("line",))
                        elif token.startswith("$"):
                            try:
                                idx = int(token[1:]) - 1
                                print_tokens.append(("field", idx))
                            except ValueError:
                                print_tokens.append(("literal", token))
                        else:
                            print_tokens.append(("literal", token))
            else:
                print_mode = "none"
                print_tokens = None

            for p in paths:
                target = Path(cwd) / p if not Path(p).is_absolute() else Path(p)
                try:
                    with open(target, "r", encoding="utf-8", errors="replace") as f:
                        if print_mode == "line":
                            for line in f:
                                results.append(line.rstrip("\n\r"))
                        elif print_mode == "tokens":
                            for line in f:
                                line = line.rstrip("\n\r")
                                fields = line.split(delimiter)
                                parts = []
                                for token in print_tokens:
                                    if token[0] == "line":
                                        parts.append(line)
                                    elif token[0] == "field":
                                        idx = token[1]
                                        if 0 <= idx < len(fields):
                                            parts.append(fields[idx])
                                        else:
                                            parts.append("")
                                    else:
                                        parts.append(token[1])
                                results.append(" ".join(parts))
                        else:
                            for line in f:
                                results.append(line.rstrip("\n\r"))
                except FileNotFoundError:
                    results.append(f"awk: cannot open {p}: No such file or directory")
                except OSError as e:
                    results.append(f"awk: {p}: {e}")

            output = "\n".join(results)
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="awk failed")
