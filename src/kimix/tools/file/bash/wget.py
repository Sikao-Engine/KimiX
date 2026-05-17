"""wget tool - network downloader."""
import os
import urllib.request
import urllib.error

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Wget(CallableTool2[Params]):
    name: str = "Wget"
    description: str = "Network downloader."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            url = None
            output_file = None
            for arg in params.args:
                if arg == "-O":
                    i = params.args.index(arg)
                    if i + 1 < len(params.args):
                        output_file = params.args[i + 1]
                elif arg.startswith("-"):
                    pass
                else:
                    url = arg

            if url is None:
                return ToolError(message="wget: missing URL", output="", brief="missing URL")

            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()

            if output_file:
                target = os.path.join(params.cwd or os.getcwd(), output_file) if not os.path.isabs(output_file) else output_file
                with open(target, "wb") as f:
                    f.write(data)
                output = f"saved to file `{target}`"
            elif params.output_path:
                cwd = params.cwd or os.getcwd()
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")
                with open(params.output_path, "wb") as f:
                    f.write(data)
                output = f"saved to file `{params.output_path}`"
            else:
                # Extract filename from URL
                filename = os.path.basename(url.split("?")[0]) or "index.html"
                target = os.path.join(params.cwd or os.getcwd(), filename)
                with open(target, "wb") as f:
                    f.write(data)
                output = f"saved to file `{target}`"
            return ToolOk(output=output)
        except urllib.error.URLError as e:
            return ToolError(message=f"wget: {e}", output="", brief="wget failed")
        except Exception as e:
            return ToolError(message=str(e), output="", brief="wget failed")
