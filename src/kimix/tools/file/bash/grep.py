"""grep tool - print lines that match patterns."""
import os
import re
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Grep(CallableTool2[Params]):
    name: str = "Grep"
    description: str = "Print lines that match patterns."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            invert = False
            ignore_case = False
            line_number = False
            count_only = False
            fixed_strings = False
            recursive = False
            pattern = None
            paths = []
            i = 0
            while i < len(params.args):
                arg = params.args[i]
                if arg == "-v" or arg == "--invert-match":
                    invert = True
                elif arg == "-i" or arg == "--ignore-case":
                    ignore_case = True
                elif arg == "-n" or arg == "--line-number":
                    line_number = True
                elif arg == "-c" or arg == "--count":
                    count_only = True
                elif arg == "-F" or arg == "--fixed-strings":
                    fixed_strings = True
                elif arg == "-r" or arg == "-R" or arg == "--recursive":
                    recursive = True
                elif arg == "-e":
                    i += 1
                    if i < len(params.args):
                        pattern = params.args[i]
                elif arg.startswith("-"):
                    pass
                elif pattern is None:
                    pattern = arg
                else:
                    paths.append(arg)
                i += 1

            if pattern is None:
                return ToolError(message="grep: missing pattern", output="", brief="missing pattern")

            if not paths:
                return ToolError(message="grep: missing file operand", output="", brief="missing operand")

            # Fast path for fixed strings: use substring search instead of regex.
            if fixed_strings and not ignore_case:
                def _match(line: str) -> bool:
                    return pattern in line
            elif fixed_strings and ignore_case:
                _pat_lower = pattern.lower()
                def _match(line: str) -> bool:
                    return _pat_lower in line.lower()
            else:
                flags = 0
                if ignore_case:
                    flags |= re.IGNORECASE
                regex = re.compile(pattern, flags)
                def _match(line: str) -> bool:
                    return bool(regex.search(line))

            cwd = params.cwd or os.getcwd()
            if params.output_path:
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")


            results = []
            total_matches = 0
            multi_file = len(paths) > 1 or recursive

            def _grep_file(fpath: Path, label: str = ""):
                nonlocal total_matches
                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        count = 0
                        for lineno, line in enumerate(f, 1):
                            if not count_only:
                                line = line.rstrip("\n\r")
                            match = _match(line)
                            if invert:
                                match = not match
                            if match:
                                count += 1
                                if not count_only:
                                    prefix = ""
                                    if multi_file:
                                        prefix = f"{label}:"
                                    if line_number:
                                        prefix += f"{lineno}:"
                                    results.append(prefix + line)
                        if count_only:
                            prefix = ""
                            if multi_file:
                                prefix = f"{label}:"
                            results.append(f"{prefix}{count}")
                        total_matches += count
                except (OSError, UnicodeDecodeError):
                    pass

            def _walk(p: Path):
                if p.is_dir():
                    for entry in p.iterdir():
                        if entry.is_file():
                            _grep_file(entry, str(entry))
                        elif entry.is_dir() and recursive:
                            _walk(entry)
                else:
                    _grep_file(p, str(p))

            for p in paths:
                target = Path(cwd) / p if not Path(p).is_absolute() else Path(p)
                _walk(target)

            output = "\n".join(results)
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="grep failed")
