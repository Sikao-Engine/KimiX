"""lsb_release tool - print distribution-specific information."""
import os
import platform

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class LsbRelease(CallableTool2[Params]):
    name: str = "LsbRelease"
    description: str = "Print distribution-specific information."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            all_info = False
            id_only = False
            desc_only = False
            release_only = False
            codename_only = False
            for arg in params.args:
                if arg == "-a" or arg == "--all":
                    all_info = True
                elif arg == "-i" or arg == "--id":
                    id_only = True
                elif arg == "-d" or arg == "--description":
                    desc_only = True
                elif arg == "-r" or arg == "--release":
                    release_only = True
                elif arg == "-c" or arg == "--codename":
                    codename_only = True

            distro = "Unknown"
            version = "Unknown"
            codename = "Unknown"
            try:
                with open("/etc/os-release", "r", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("ID="):
                            distro = line.split("=", 1)[1].strip().strip('"')
                        elif line.startswith("VERSION_ID="):
                            version = line.split("=", 1)[1].strip().strip('"')
                        elif line.startswith("VERSION_CODENAME="):
                            codename = line.split("=", 1)[1].strip().strip('"')
            except Exception:
                pass

            parts = []
            if all_info or (not id_only and not desc_only and not release_only and not codename_only):
                parts = [
                    f"Distributor ID:\t{distro}",
                    f"Description:\t{distro} {version}",
                    f"Release:\t{version}",
                    f"Codename:\t{codename}",
                ]
            else:
                if id_only:
                    parts.append(distro)
                if desc_only:
                    parts.append(f"{distro} {version}")
                if release_only:
                    parts.append(version)
                if codename_only:
                    parts.append(codename)

            output = "\n".join(parts)
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
            return ToolError(message=str(e), output="", brief="lsb_release failed")
