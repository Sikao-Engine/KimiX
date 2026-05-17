"""id tool - print real and effective user and group IDs."""
import os

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Id(CallableTool2[Params]):
    name: str = "Id"
    description: str = "Print real and effective user and group IDs."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            uid = os.getuid()
            gid = os.getgid()
            try:
                import pwd
                user = pwd.getpwuid(uid).pw_name
            except Exception:
                user = str(uid)
            try:
                import grp
                group = grp.getgrgid(gid).gr_name
            except Exception:
                group = str(gid)
            groups = [str(gid)]
            if hasattr(os, "getgroups"):
                try:
                    gids = os.getgroups()
                    groups = []
                    for g in gids:
                        try:
                            import grp
                            groups.append(grp.getgrgid(g).gr_name)
                        except Exception:
                            groups.append(str(g))
                except Exception:
                    pass
            output = f"uid={uid}({user}) gid={gid}({group}) groups={','.join(groups)}"
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
            return ToolError(message=str(e), output="", brief="id failed")
