"""diff tool - compare files line by line."""
import filecmp
import os
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


def _diff_lines(a: list[str], b: list[str]) -> list[str]:
    import difflib
    return list(difflib.unified_diff(a, b, lineterm=""))


class Diff(CallableTool2[Params]):
    name: str = "Diff"
    description: str = "Compare files line by line."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            paths = [arg for arg in params.args if not arg.startswith("-")]
            if len(paths) < 2:
                return ToolError(message="diff: missing operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            if params.output_path:
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")


            left = Path(cwd) / paths[0] if not Path(paths[0]).is_absolute() else Path(paths[0])
            right = Path(cwd) / paths[1] if not Path(paths[1]).is_absolute() else Path(paths[1])

            if left.is_dir() and right.is_dir():
                # Compare directory contents using os.scandir for speed
                left_entries = {e.name: e for e in os.scandir(left)}
                right_entries = {e.name: e for e in os.scandir(right)}
                all_names = sorted(set(left_entries) | set(right_entries))
                results = []
                for name in all_names:
                    le = left_entries.get(name)
                    re = right_entries.get(name)
                    if le is None:
                        results.append(f"Only in {right}: {name}")
                    elif re is None:
                        results.append(f"Only in {left}: {name}")
                    elif le.is_file() and re.is_file():
                        if not filecmp.cmp(le.path, re.path, shallow=False):
                            with open(le.path, "r", encoding="utf-8", errors="replace") as fh:
                                flines = fh.read().splitlines()
                            with open(re.path, "r", encoding="utf-8", errors="replace") as fh:
                                rlines = fh.read().splitlines()
                            results.extend(_diff_lines(flines, rlines))
                output = "\n".join(results)
            else:
                if not left.exists():
                    return ToolError(message=f"diff: {paths[0]}: No such file or directory", output="", brief="file not found")
                if not right.exists():
                    return ToolError(message=f"diff: {paths[1]}: No such file or directory", output="", brief="file not found")

                # Fast path: identical files
                if filecmp.cmp(left, right, shallow=False):
                    output = ""
                else:
                    with open(left, "r", encoding="utf-8", errors="replace") as f:
                        left_lines = f.read().splitlines()
                    with open(right, "r", encoding="utf-8", errors="replace") as f:
                        right_lines = f.read().splitlines()
                    results = _diff_lines(left_lines, right_lines)
                    output = "\n".join(results)

            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="diff failed")
