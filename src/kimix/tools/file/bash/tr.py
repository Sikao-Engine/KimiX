"""tr tool - translate or delete characters."""
import os
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


def _expand_set(s: str) -> str:
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    result = []
    i = 0
    n = len(s)
    while i < n:
        if i + 2 < n and s[i + 1] == "-":
            start = ord(s[i])
            end = ord(s[i + 2])
            if end <= 255:
                result.append(bytes(range(start, end + 1)).decode("latin-1"))
            else:
                if start <= 255:
                    result.append(bytes(range(start, 256)).decode("latin-1"))
                    result.extend(chr(c) for c in range(256, end + 1))
                else:
                    result.extend(chr(c) for c in range(start, end + 1))
            i += 3
        else:
            result.append(s[i])
            i += 1
    return "".join(result)


class Tr(CallableTool2[Params]):
    name: str = "Tr"
    description: str = "Translate or delete characters."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            delete_mode = False
            sets = []
            paths = []
            for arg in params.args:
                if arg == "-d" or arg == "--delete":
                    delete_mode = True
                elif not arg.startswith("-"):
                    if len(sets) < (1 if delete_mode else 2):
                        sets.append(arg)
                    else:
                        paths.append(arg)

            if len(sets) < (1 if delete_mode else 2):
                return ToolError(message="tr: missing operand", output="", brief="missing operand")

            set1 = _expand_set(sets[0])
            if delete_mode:
                trans = str.maketrans("", "", set1)
            else:
                set2 = _expand_set(sets[1])
                if len(set2) < len(set1):
                    set2 = set2 + set2[-1] * (len(set1) - len(set2))
                else:
                    set2 = set2[: len(set1)]
                trans = str.maketrans(set1, set2)

            if not paths:
                output = "tr: standalone usage not supported without input. Use via pipe or provide input."
                if params.output_path:
                    cwd = params.cwd or os.getcwd()
                    is_prot, reason = _is_protected_path(params.output_path, cwd)
                    if is_prot:
                        return ToolError(message=reason, output=reason, brief="protected path")
                    with open(params.output_path, "w", encoding="utf-8") as f:
                        f.write(output)
                    output = f"saved to file `{params.output_path}`"
                return ToolOk(output=output)

            cwd = params.cwd or os.getcwd()
            cwd_path = Path(cwd)
            results = []
            for p in paths:
                p_path = Path(p)
                target = p_path if p_path.is_absolute() else cwd_path / p
                try:
                    with open(target, "r", encoding="utf-8", errors="replace") as f:
                        results.append(f.read().translate(trans))
                except FileNotFoundError:
                    results.append(f"tr: {p}: No such file or directory")
                except OSError as e:
                    results.append(f"tr: {p}: {e}")

            output = "".join(results)
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
            return ToolError(message=str(e), output="", brief="tr failed")
