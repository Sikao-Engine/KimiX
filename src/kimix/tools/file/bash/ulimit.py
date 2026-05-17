"""ulimit tool - get and set user limits."""
import os

try:
    import resource
except ModuleNotFoundError:
    resource = None  # type: ignore

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Ulimit(CallableTool2[Params]):
    name: str = "Ulimit"
    description: str = "Get and set user limits."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            if resource is None:
                return ToolError(message="ulimit: not supported on this platform", output="", brief="not supported")

            soft = True
            hard = False
            limit_type = resource.RLIMIT_NOFILE
            value = None

            for arg in params.args:
                if arg == "-H":
                    hard = True
                    soft = False
                elif arg == "-S":
                    soft = True
                    hard = False
                elif arg == "-a":
                    lines = []
                    for name, res in [
                        ("core file size", resource.RLIMIT_CORE),
                        ("data seg size", resource.RLIMIT_DATA),
                        ("scheduling priority", resource.RLIMIT_NICE if hasattr(resource, "RLIMIT_NICE") else None),
                        ("file size", resource.RLIMIT_FSIZE),
                        ("pending signals", resource.RLIMIT_SIGPENDING if hasattr(resource, "RLIMIT_SIGPENDING") else None),
                        ("max locked memory", resource.RLIMIT_MEMLOCK if hasattr(resource, "RLIMIT_MEMLOCK") else None),
                        ("max memory size", resource.RLIMIT_AS if hasattr(resource, "RLIMIT_AS") else None),
                        ("open files", resource.RLIMIT_NOFILE),
                        ("pipe size", None),
                        ("POSIX message queues", None),
                        ("real-time priority", None),
                        ("stack size", resource.RLIMIT_STACK),
                        ("cpu time", resource.RLIMIT_CPU),
                        ("max user processes", resource.RLIMIT_NPROC if hasattr(resource, "RLIMIT_NPROC") else None),
                        ("virtual memory", resource.RLIMIT_AS if hasattr(resource, "RLIMIT_AS") else None),
                        ("file locks", None),
                    ]:
                        if res is not None:
                            try:
                                soft_lim, hard_lim = resource.getrlimit(res)
                                lines.append(f"{name}          ({res}) {soft_lim if soft_lim != resource.RLIM_INFINITY else 'unlimited'} {hard_lim if hard_lim != resource.RLIM_INFINITY else 'unlimited'}")
                            except (OSError, ValueError):
                                pass
                    output = "\n".join(lines)
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
                elif arg.startswith("-"):
                    opt = arg[1:]
                    mapping = {
                        "c": resource.RLIMIT_CORE,
                        "d": resource.RLIMIT_DATA,
                        "f": resource.RLIMIT_FSIZE,
                        "n": resource.RLIMIT_NOFILE,
                        "s": resource.RLIMIT_STACK,
                        "t": resource.RLIMIT_CPU,
                        "u": resource.RLIMIT_NPROC if hasattr(resource, "RLIMIT_NPROC") else None,
                        "v": resource.RLIMIT_AS if hasattr(resource, "RLIMIT_AS") else None,
                        "m": resource.RLIMIT_RSS if hasattr(resource, "RLIMIT_RSS") else None,
                        "l": resource.RLIMIT_MEMLOCK if hasattr(resource, "RLIMIT_MEMLOCK") else None,
                    }
                    limit_type = mapping.get(opt, resource.RLIMIT_NOFILE)
                else:
                    if arg == "unlimited":
                        value = resource.RLIM_INFINITY
                    else:
                        value = int(arg)

            if limit_type is None:
                return ToolError(message="ulimit: limit not supported on this platform", output="", brief="ulimit failed")

            if value is not None:
                if soft and hard:
                    resource.setrlimit(limit_type, (value, value))
                elif hard:
                    cur, _ = resource.getrlimit(limit_type)
                    resource.setrlimit(limit_type, (cur, value))
                else:
                    _, max_val = resource.getrlimit(limit_type)
                    resource.setrlimit(limit_type, (value, max_val))
                return ToolOk(output="")

            soft_lim, hard_lim = resource.getrlimit(limit_type)
            output = str(soft_lim if soft else hard_lim)
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
            return ToolError(message=str(e), output="", brief="ulimit failed")
