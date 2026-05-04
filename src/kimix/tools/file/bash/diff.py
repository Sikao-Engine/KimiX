"""diff tool - compare files line by line."""
import os
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params

from kimix.tools.common import _maybe_export_output_async

def _diff_lines(a: list[str], b: list[str]) -> list[str]:
    # Simple unified diff approximation using difflib
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
            left = Path(cwd) / paths[0] if not Path(paths[0]).is_absolute() else Path(paths[0])
            right = Path(cwd) / paths[1] if not Path(paths[1]).is_absolute() else Path(paths[1])

            if left.is_dir() and right.is_dir():
                # Compare directory contents
                left_files = sorted(left.iterdir())
                right_files = sorted(right.iterdir())
                results = []
                for f in left_files:
                    rf = right / f.name
                    if not rf.exists():
                        results.append(f"Only in {left}: {f.name}")
                    elif f.is_file() and rf.is_file():
                        with open(f, "r", encoding="utf-8", errors="replace") as fh:
                            flines = fh.read().splitlines()
                        with open(rf, "r", encoding="utf-8", errors="replace") as fh:
                            rlines = fh.read().splitlines()
                        if flines != rlines:
                            results.extend(_diff_lines(flines, rlines))
                for f in right_files:
                    lf = left / f.name
                    if not lf.exists():
                        results.append(f"Only in {right}: {f.name}")
                output = "\n".join(results)
            else:
                try:
                    with open(left, "r", encoding="utf-8", errors="replace") as f:
                        left_lines = f.read().splitlines()
                except FileNotFoundError:
                    return ToolError(message=f"diff: {paths[0]}: No such file or directory", output="", brief="file not found")
                try:
                    with open(right, "r", encoding="utf-8", errors="replace") as f:
                        right_lines = f.read().splitlines()
                except FileNotFoundError:
                    return ToolError(message=f"diff: {paths[1]}: No such file or directory", output="", brief="file not found")

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
