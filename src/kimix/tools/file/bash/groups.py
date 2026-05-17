"""groups tool - print group memberships."""
import os

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Groups(CallableTool2[Params]):
    name: str = "Groups"
    description: str = "Print group memberships."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            user = None
            for arg in params.args:
                if not arg.startswith("-"):
                    user = arg
            if user is None:
                if hasattr(os, "getgroups"):
                    gids = os.getgroups()
                else:
                    gids = [os.getgid()]
            else:
                try:
                    import pwd
                    pw = pwd.getpwnam(user)
                    if hasattr(os, "getgrouplist"):
                        gids = os.getgrouplist(user, pw.pw_gid)
                    else:
                        gids = [pw.pw_gid]
                except Exception:
                    return ToolError(message=f"groups: {user}: no such user", output="", brief="groups failed")

            names = []
            for g in gids:
                try:
                    import grp
                    names.append(grp.getgrgid(g).gr_name)
                except Exception:
                    names.append(str(g))
            output = " ".join(names)
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
            return ToolError(message=str(e), output="", brief="groups failed")
