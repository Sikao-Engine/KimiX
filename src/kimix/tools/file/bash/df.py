"""df tool - report file system disk space usage."""
import os
import platform
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params

from kimix.tools.common import _maybe_export_output_async

def _get_disk_usage(path: str):
    if platform.system() == "Windows":
        import ctypes
        free_bytes = ctypes.c_ulonglong(0)
        total_bytes = ctypes.c_ulonglong(0)
        total_free_bytes = ctypes.c_ulonglong(0)
        ctypes.windll.kernel32.GetDiskFreeSpaceExW(
            ctypes.c_wchar_p(path),
            ctypes.pointer(free_bytes),
            ctypes.pointer(total_bytes),
            ctypes.pointer(total_free_bytes),
        )
        total = total_bytes.value
        free = free_bytes.value
        used = total - free
        return total, used, free
    else:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        return total, used, free

class Df(CallableTool2[Params]):
    name: str = "Df"
    description: str = "Report file system disk space usage."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            human_readable = False
            paths = [arg for arg in params.args if not arg.startswith("-")]
            for arg in params.args:
                if arg == "-h" or arg == "--human-readable":
                    human_readable = True

            if not paths:
                paths = ["."]

            def _fmt(size: int) -> str:
                if not human_readable:
                    return str(size // 1024)
                for unit in ["K", "M", "G", "T", "P"]:
                    if size < 1024:
                        return f"{size:.1f}{unit}" if unit != "K" else f"{size}K"
                    size /= 1024
                return f"{size:.1f}E"

            results = ["Filesystem     1K-blocks     Used Available Use% Mounted on"]
            seen = set()
            for p in paths:
                target = Path(p).resolve()
                try:
                    # Find mount point
                    mount = target
                    while not mount.is_mount() and mount.parent != mount:
                        mount = mount.parent
                    if mount in seen:
                        continue
                    seen.add(mount)
                    total, used, free = _get_disk_usage(str(mount))
                    percent = f"{int(used / total * 100)}%" if total else "0%"
                    fs = str(mount)
                    results.append(
                        f"{fs:15} {_fmt(total):>10} {_fmt(used):>10} {_fmt(free):>10} {percent:>4} {fs}"
                    )
                except OSError as e:
                    results.append(f"df: {p}: {e}")

            output = "\n".join(results)
            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="df failed")
