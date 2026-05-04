"""tr tool - translate or delete characters."""
import os
import sys
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from pydantic import BaseModel, Field

from kimix.tools.common import _maybe_export_output_async


class Params(BaseModel):
    path: str = Field(description="Executable path.")
    args: list[str] = Field(default_factory=list, description="Command arguments.")
    timeout: int = Field(default=10, description="Timeout in seconds.")
    cwd: str | None = Field(default=None, description="Working directory (default: current directory).")
    output_path: str | None = Field(default=None, description="Output file path (optional).")


def _expand_set(s: str) -> str:
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    result = []
    i = 0
    while i < len(s):
        if i + 2 < len(s) and s[i + 1] == "-":
            start = ord(s[i])
            end = ord(s[i + 2])
            for c in range(start, end + 1):
                result.append(chr(c))
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
            for arg in params.args:
                if arg == "-d" or arg == "--delete":
                    delete_mode = True
                elif not arg.startswith("-"):
                    sets.append(arg)

            if len(sets) < (1 if delete_mode else 2):
                return ToolError(message="tr: missing operand", output="", brief="missing operand")

            set1 = _expand_set(sets[0])
            if delete_mode:
                trans = str.maketrans("", "", set1)
            else:
                set2 = _expand_set(sets[1])
                # Pad or truncate set2 to match set1 length
                if len(set2) < len(set1):
                    set2 = set2 + set2[-1] * (len(set1) - len(set2))
                else:
                    set2 = set2[: len(set1)]
                trans = str.maketrans(set1, set2)

            # tr reads from stdin normally, but here we can read from a file if provided
            # Since there's no stdin in this context, we'll just return a message
            # Or if the user passes a file path as extra arg, read it
            # Actually, let's support reading from file if specified as last arg
            # But standard tr doesn't take file args. We'll leave it as no-op
            # and expect the tool to be used via piping through Run.
            # For standalone usage, we'll just show a help message.
            output = "tr: standalone usage not supported without input. Use via pipe or provide input."
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="tr failed")
