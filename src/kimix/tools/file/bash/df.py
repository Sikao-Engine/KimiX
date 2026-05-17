"""df tool - report file system disk space usage."""
import os
import platform
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

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


def _find_mount_point(path: str) -> str:
    if platform.system() == "Windows":
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.kernel32.GetVolumePathNameW(path, buf, 260):
            return buf.value
        # Fallback for edge cases (e.g. invalid drive)
        mount = Path(path).resolve()
        while not mount.is_mount() and mount.parent != mount:
            mount = mount.parent
        return str(mount)
    # Unix: walk up until st_dev changes — faster than ismount()
    dev = os.stat(path).st_dev
    while True:
        parent = os.path.dirname(path)
        if parent == path:
            break
        try:
            parent_dev = os.stat(parent).st_dev
        except OSError:
            break
        if parent_dev != dev:
            break
        path = parent
    return path

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

            cwd = params.cwd or os.getcwd()
            if params.output_path:
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")


            def _fmt(size: int) -> str:
                if not human_readable:
                    return str(size // 1024)
                for unit in ["K", "M", "G", "T", "P"]:
                    if size < 1024:
                        return f"{size:.1f}{unit}" if unit != "K" else f"{size}K"
                    size /= 1024
                return f"{size:.1f}E"

            results = ["Filesystem     1K-blocks     Used Available Use% Mounted on"]
            seen_devs = set()
            for p in paths:
                try:
                    target = os.path.realpath(p)
                    dev = os.stat(target).st_dev
                    if dev in seen_devs:
                        continue
                    seen_devs.add(dev)
                    mount = _find_mount_point(target)
                    total, used, free = _get_disk_usage(mount)
                    percent = f"{int(used / total * 100)}%" if total else "0%"
                    results.append(
                        f"{mount:15} {_fmt(total):>10} {_fmt(used):>10} {_fmt(free):>10} {percent:>4} {mount}"
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
