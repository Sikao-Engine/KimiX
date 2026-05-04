"""ls tool - list directory contents."""
import os
import stat
import time
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params

from kimix.tools.common import _maybe_export_output_async

def _format_mode(mode: int) -> str:
    perms = [
        ("r" if mode & 0o400 else "-"),
        ("w" if mode & 0o200 else "-"),
        ("x" if mode & 0o100 else "-"),
        ("r" if mode & 0o040 else "-"),
        ("w" if mode & 0o020 else "-"),
        ("x" if mode & 0o010 else "-"),
        ("r" if mode & 0o004 else "-"),
        ("w" if mode & 0o002 else "-"),
        ("x" if mode & 0o001 else "-"),
    ]
    return "".join(perms)

def _format_size(size: int, human_readable: bool = False) -> str:
    if not human_readable:
        return str(size)
    for unit in ["B", "K", "M", "G", "T"]:
        if size < 1024:
            return f"{size:.1f}{unit}" if unit != "B" else f"{size}B"
        size /= 1024
    return f"{size:.1f}P"

def _format_time(mtime: float) -> str:
    return time.strftime("%b %d %H:%M", time.localtime(mtime))

class Ls(CallableTool2[Params]):
    name: str = "Ls"
    description: str = "List directory contents."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            cwd = params.cwd or os.getcwd()
            long_fmt = False
            all_files = False
            human_readable = False
            recursive = False
            reverse = False
            sort_time = False
            paths = []

            i = 0
            while i < len(params.args):
                arg = params.args[i]
                if arg.startswith("-") and len(arg) > 1:
                    for ch in arg[1:]:
                        if ch == "l":
                            long_fmt = True
                        elif ch == "a":
                            all_files = True
                        elif ch == "h":
                            human_readable = True
                        elif ch == "R":
                            recursive = True
                        elif ch == "r":
                            reverse = True
                        elif ch == "t":
                            sort_time = True
                else:
                    paths.append(arg)
                i += 1

            if not paths:
                paths = ["."]

            def _ls_dir(dir_path: Path, prefix: str = "") -> list[str]:
                lines = []
                try:
                    entries = list(dir_path.iterdir())
                except PermissionError:
                    return [f"{prefix}ls: cannot open directory '{dir_path}': Permission denied"]
                except FileNotFoundError:
                    return [f"{prefix}ls: cannot access '{dir_path}': No such file or directory"]

                if not all_files:
                    entries = [e for e in entries if not e.name.startswith(".")]

                if sort_time:
                    entries.sort(key=lambda e: e.stat().st_mtime, reverse=not reverse)
                elif reverse:
                    entries.sort(key=lambda e: e.name, reverse=True)
                else:
                    entries.sort(key=lambda e: e.name)

                if long_fmt:
                    total = sum(max(1, (e.stat().st_size + 4095) // 4096) for e in entries)
                    lines.append(f"{prefix}total {total}")
                    for e in entries:
                        st = e.stat()
                        mode = "d" if e.is_dir() else ("l" if e.is_symlink() else "-")
                        mode += _format_mode(st.st_mode)
                        nlink = str(st.st_nlink)
                        size = _format_size(st.st_size, human_readable)
                        mtime = _format_time(st.st_mtime)
                        name = e.name
                        if e.is_symlink():
                            try:
                                name += " -> " + str(e.readlink())
                            except OSError:
                                pass
                        lines.append(f"{prefix}{mode} {nlink:>3} {'':8} {'':8} {size:>10} {mtime} {name}")
                else:
                    for e in entries:
                        lines.append(f"{prefix}{e.name}")
                return lines

            def _ls(path: Path, prefix: str = "") -> list[str]:
                lines = []
                if path.is_dir() and not path.is_symlink():
                    if recursive and prefix:
                        lines.append("")
                        lines.append(f"{path}:")
                    lines.extend(_ls_dir(path, prefix))
                    if recursive:
                        for e in sorted(path.iterdir(), key=lambda x: x.name):
                            if e.is_dir() and not e.is_symlink():
                                if not all_files and e.name.startswith("."):
                                    continue
                                lines.extend(_ls(e, prefix))
                else:
                    if path.exists() or path.is_symlink():
                        if long_fmt:
                            st = path.stat()
                            mode = "-"
                            mode += _format_mode(st.st_mode)
                            nlink = str(st.st_nlink)
                            size = _format_size(st.st_size, human_readable)
                            mtime = _format_time(st.st_mtime)
                            lines.append(f"{prefix}{mode} {nlink:>3} {'':8} {'':8} {size:>10} {mtime} {path.name}")
                        else:
                            lines.append(f"{prefix}{path.name}")
                    else:
                        lines.append(f"{prefix}ls: cannot access '{path}': No such file or directory")
                return lines

            output_lines = []
            for p in paths:
                target = Path(cwd) / p if not Path(p).is_absolute() else Path(p)
                output_lines.extend(_ls(target))

            output = "\n".join(output_lines)
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="ls failed")
