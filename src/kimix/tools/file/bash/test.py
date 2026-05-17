"""test tool - check file types and compare values."""
import os
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params

from kimix.tools.common import _maybe_export_output_async


class Test(CallableTool2[Params]):
    __test__ = False
    name: str = "Test"
    description: str = "Check file types and compare values."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            args = params.args
            cwd = params.cwd or os.getcwd()

            if not args:
                # test with no args -> false
                return ToolOk(output="")

            def _eval(args):
                if len(args) == 1:
                    return bool(args[0])
                if len(args) == 2:
                    op = args[0]
                    operand = args[1]
                    if op == "-e":
                        return os.path.exists(operand)
                    elif op == "-f":
                        return os.path.isfile(operand)
                    elif op == "-d":
                        return os.path.isdir(operand)
                    elif op == "-r":
                        return os.access(operand, os.R_OK)
                    elif op == "-w":
                        return os.access(operand, os.W_OK)
                    elif op == "-x":
                        return os.access(operand, os.X_OK)
                    elif op == "-s":
                        return os.path.exists(operand) and os.path.getsize(operand) > 0
                    elif op == "-L" or op == "-h":
                        return os.path.islink(operand)
                    elif op == "-p":
                        return os.path.exists(operand) and os.path.isdir(operand) is False and os.path.isfile(operand) is False
                    elif op == "-S":
                        return False  # socket detection hard in pure python
                    elif op == "-b" or op == "-c":
                        return False
                    elif op == "-n":
                        return len(operand) > 0
                    elif op == "-z":
                        return len(operand) == 0
                    else:
                        return False
                if len(args) == 3:
                    a, op, b = args
                    if op == "=":
                        return a == b
                    elif op == "!=":
                        return a != b
                    elif op == "-eq":
                        return int(a) == int(b)
                    elif op == "-ne":
                        return int(a) != int(b)
                    elif op == "-lt":
                        return int(a) < int(b)
                    elif op == "-le":
                        return int(a) <= int(b)
                    elif op == "-gt":
                        return int(a) > int(b)
                    elif op == "-ge":
                        return int(a) >= int(b)
                    elif op == "-nt":
                        return os.path.getmtime(a) > os.path.getmtime(b)
                    elif op == "-ot":
                        return os.path.getmtime(a) < os.path.getmtime(b)
                    elif op == "-ef":
                        return os.path.samefile(a, b)
                    else:
                        return False
                # Complex expressions with !, -a, -o, parentheses - basic support
                if args[0] == "!":
                    return not _eval(args[1:])
                return False

            result = _eval(args)
            # test returns 0 if true, 1 if false. We return empty output on true, error-like on false?
            # But bash convention: test false does not print anything, just exits 1.
            # For our tool, let's return empty output and success if true, error if false.
            if result:
                return ToolOk(output="")
            else:
                return ToolError(message="", output="", brief="test returned false")
        except Exception as e:
            return ToolError(message=str(e), output="", brief="test failed")
