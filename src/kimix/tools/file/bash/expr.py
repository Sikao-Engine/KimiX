"""expr tool - evaluate expressions."""
import os
import operator
import re

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Expr(CallableTool2[Params]):
    name: str = "Expr"
    description: str = "Evaluate expressions."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            if not params.args:
                return ToolError(message="expr: missing operand", output="", brief="missing operand")

            tokens = params.args
            # Basic arithmetic and string matching
            # expr supports: + - * / % = != < <= > >= & | : (regex match)
            # Simple parser for 2 or 3 operand expressions
            def _eval(tokens):
                if len(tokens) == 1:
                    try:
                        return int(tokens[0])
                    except ValueError:
                        return tokens[0]
                if len(tokens) == 3:
                    a, op, b = tokens
                    try:
                        ai = int(a)
                        bi = int(b)
                        if op == "+":
                            return ai + bi
                        elif op == "-":
                            return ai - bi
                        elif op == "*":
                            return ai * bi
                        elif op == "/":
                            return ai // bi
                        elif op == "%":
                            return ai % bi
                        elif op == "=":
                            return 1 if ai == bi else 0
                        elif op == "!=":
                            return 1 if ai != bi else 0
                        elif op == "<":
                            return 1 if ai < bi else 0
                        elif op == "<=":
                            return 1 if ai <= bi else 0
                        elif op == ">":
                            return 1 if ai > bi else 0
                        elif op == ">=":
                            return 1 if ai >= bi else 0
                        elif op == "&":
                            return ai if ai != 0 and bi != 0 else 0
                        elif op == "|":
                            return ai if ai != 0 else bi
                    except ValueError:
                        pass
                    # String operations
                    if op == "=":
                        return 1 if a == b else 0
                    elif op == "!=":
                        return 1 if a != b else 0
                    elif op == "<":
                        return 1 if a < b else 0
                    elif op == ">":
                        return 1 if a > b else 0
                    elif op == ":":
                        m = re.search(b, a)
                        return m.group(0) if m else ""
                # Substr / index / length with spaces
                if tokens[0] == "length" and len(tokens) == 2:
                    return len(tokens[1])
                if tokens[0] == "index" and len(tokens) == 3:
                    return tokens[2].find(tokens[1]) + 1
                if tokens[0] == "substr" and len(tokens) == 4:
                    s = tokens[1]
                    start = int(tokens[2]) - 1
                    length = int(tokens[3])
                    return s[start:start + length]
                return 0

            result = _eval(tokens)
            output = str(result) if result is not None else ""
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
            return ToolError(message=str(e), output="", brief="expr failed")
