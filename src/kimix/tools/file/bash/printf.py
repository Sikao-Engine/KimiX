"""printf tool - format and print data."""
import os

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Printf(CallableTool2[Params]):
    name: str = "Printf"
    description: str = "Format and print data."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            if not params.args:
                return ToolError(message="printf: missing operand", output="", brief="missing operand")
            fmt = params.args[0]
            args = params.args[1:]
            # Basic support for %s, %d, %i, %f, %%, %b (basic backslash), %c, %o, %x, %X, %u, %e, %E, %g, %G
            # Replace %% first
            output = fmt.replace("%%", "%")
            import re
            # Simple formatter: process args in order
            def repl(m):
                nonlocal args
                if not args:
                    return m.group(0)
                spec = m.group(0)
                arg = args.pop(0)
                if spec.endswith("s"):
                    return str(arg)
                elif spec.endswith("d") or spec.endswith("i"):
                    return str(int(arg))
                elif spec.endswith("u"):
                    return str(int(arg))
                elif spec.endswith("o"):
                    return oct(int(arg))[2:]
                elif spec.endswith("x"):
                    return hex(int(arg))[2:]
                elif spec.endswith("X"):
                    return hex(int(arg))[2:].upper()
                elif spec.endswith("f"):
                    return str(float(arg))
                elif spec.endswith("e"):
                    return "{:e}".format(float(arg))
                elif spec.endswith("E"):
                    return "{:E}".format(float(arg))
                elif spec.endswith("g"):
                    return "{:g}".format(float(arg))
                elif spec.endswith("G"):
                    return "{:G}".format(float(arg))
                elif spec.endswith("c"):
                    return chr(int(arg))
                elif spec.endswith("b"):
                    # basic backslash interpretation
                    s = str(arg)
                    s = s.replace("\\n", "\n")
                    s = s.replace("\\t", "\t")
                    s = s.replace("\\\\", "\\")
                    return s
                else:
                    return str(arg)

            # Match format specifiers like %s, %-10s, %10d, etc.
            pattern = re.compile(r"%[-+0 #]*\d*(?:\.\d+)?[bcdeEfgGiosuxX]")
            output = pattern.sub(repl, output)
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
            return ToolError(message=str(e), output="", brief="printf failed")
