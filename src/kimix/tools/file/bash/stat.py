"""stat tool - display file or file system status."""
import os
import time
import platform
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


# Pre-compute platform check once at module load
_IS_WINDOWS = platform.system() == "Windows"


def _fmt_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _build_stat_windows(target: Path, st) -> str:
    mode = st.st_mode
    blocks = (st.st_size + 511) // 512
    return (
        f"  File: {target}\n"
        f"  Size: {st.st_size}           Blocks: {blocks}          IO Block: 4096   regular file\n"
        f"Device: {st.st_dev}         Inode: {st.st_ino}         Links: {st.st_nlink}\n"
        f"Access: ({oct(mode)[-4:]})  Uid: {st.st_uid}   Gid: {st.st_gid}\n"
        f"Access: {_fmt_time(st.st_atime)}\n"
        f"Modify: {_fmt_time(st.st_mtime)}\n"
        f"Change: {_fmt_time(st.st_ctime)}\n"
        f" Birth: {_fmt_time(st.st_ctime)}"
    )


def _build_stat_unix(target: Path, st) -> str:
    mode = st.st_mode
    blocks = (st.st_size + 511) // 512
    return (
        f"  File: {target}\n"
        f"  Size: {st.st_size}           Blocks: {blocks}          IO Block: 4096   regular file\n"
        f"Device: {st.st_dev}h/{st.st_dev}d    Inode: {st.st_ino}         Links: {st.st_nlink}\n"
        f"Access: ({oct(mode)[-4:]}/{oct(mode)})  Uid: ({st.st_uid})   Gid: ({st.st_gid})\n"
        f"Access: {_fmt_time(st.st_atime)}\n"
        f"Modify: {_fmt_time(st.st_mtime)}\n"
        f"Change: {_fmt_time(st.st_ctime)}\n"
        f" Birth: {_fmt_time(st.st_ctime)}"
    )


class Stat(CallableTool2[Params]):
    name: str = "Stat"
    description: str = "Display file or file system status."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            paths = [arg for arg in params.args if not arg.startswith("-")]
            if not paths:
                return ToolError(message="stat: missing operand", output="", brief="missing operand")

            cwd = params.cwd or os.getcwd()
            if params.output_path:
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")


            cwd_path = Path(cwd)
            results = []
            build_stat = _build_stat_windows if _IS_WINDOWS else _build_stat_unix

            for p in paths:
                target = cwd_path / p if not os.path.isabs(p) else Path(p)
                try:
                    st = target.stat(follow_symlinks=True)
                    results.append(build_stat(target, st))
                except FileNotFoundError:
                    results.append(f"stat: cannot stat '{p}': No such file or directory")
                except OSError as e:
                    results.append(f"stat: cannot stat '{p}': {e}")

            output = "\n\n".join(results)
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="stat failed")
