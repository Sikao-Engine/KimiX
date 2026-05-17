"""dc tool - desk calculator."""
import os
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Dc(CallableTool2[Params]):
    name: str = "Dc"
    description: str = "Desk calculator."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            paths = [arg for arg in params.args if not arg.startswith("-")]
            if paths:
                target = Path(params.cwd or os.getcwd()) / paths[0] if not Path(paths[0]).is_absolute() else Path(paths[0])
                try:
                    with open(target, "r", encoding="utf-8", errors="replace") as f:
                        exprs = f.read()
                except FileNotFoundError:
                    return ToolError(message=f"dc: {paths[0]}: No such file or directory", output="", brief="dc failed")
            else:
                return ToolError(message="dc: missing operand", output="", brief="missing operand")

            stack = []
            results = []
            for token in exprs.split():
                if token in "+-*/%^":
                    if len(stack) < 2:
                        return ToolError(message=f"dc: stack underflow", output="", brief="dc failed")
                    b = stack.pop()
                    a = stack.pop()
                    if token == "+":
                        stack.append(a + b)
                    elif token == "-":
                        stack.append(a - b)
                    elif token == "*":
                        stack.append(a * b)
                    elif token == "/":
                        stack.append(int(a / b))
                    elif token == "%":
                        stack.append(a % b)
                    elif token == "^":
                        stack.append(a ** b)
                elif token == "p":
                    if stack:
                        results.append(str(stack[-1]))
                elif token == "n":
                    if stack:
                        results.append(str(stack.pop()))
                elif token == "f":
                    for v in reversed(stack):
                        results.append(str(v))
                elif token == "c":
                    stack.clear()
                elif token == "d":
                    if stack:
                        stack.append(stack[-1])
                elif token == "r":
                    if len(stack) >= 2:
                        stack[-1], stack[-2] = stack[-2], stack[-1]
                else:
                    try:
                        stack.append(int(token))
                    except ValueError:
                        return ToolError(message=f"dc: invalid token '{token}'", output="", brief="dc failed")

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
            return ToolError(message=str(e), output="", brief="dc failed")
