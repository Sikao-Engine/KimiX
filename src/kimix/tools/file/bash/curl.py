"""curl tool - transfer a URL."""
import os
import urllib.request
import urllib.error

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params, _is_protected_path

from kimix.tools.common import _maybe_export_output_async


class Curl(CallableTool2[Params]):
    name: str = "Curl"
    description: str = "Transfer a URL."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            url = None
            output_file = None
            method = "GET"
            headers = {}
            data = None
            i = 0
            while i < len(params.args):
                arg = params.args[i]
                if arg == "-o" or arg == "--output":
                    i += 1
                    if i < len(params.args):
                        output_file = params.args[i]
                elif arg == "-X" or arg == "--request":
                    i += 1
                    if i < len(params.args):
                        method = params.args[i]
                elif arg == "-H" or arg == "--header":
                    i += 1
                    if i < len(params.args):
                        h = params.args[i]
                        if ":" in h:
                            k, v = h.split(":", 1)
                            headers[k.strip()] = v.strip()
                elif arg == "-d" or arg == "--data":
                    i += 1
                    if i < len(params.args):
                        data = params.args[i].encode("utf-8")
                elif not arg.startswith("-"):
                    url = arg
                i += 1

            if url is None:
                return ToolError(message="curl: missing URL", output="", brief="missing URL")

            req = urllib.request.Request(url, data=data, method=method)
            for k, v in headers.items():
                req.add_header(k, v)
            with urllib.request.urlopen(req, timeout=30) as resp:
                content = resp.read().decode("utf-8", errors="replace")

            if output_file:
                target = os.path.join(params.cwd or os.getcwd(), output_file) if not os.path.isabs(output_file) else output_file
                with open(target, "w", encoding="utf-8") as f:
                    f.write(content)
                output = f"saved to file `{target}`"
            elif params.output_path:
                cwd = params.cwd or os.getcwd()
                is_prot, reason = _is_protected_path(params.output_path, cwd)
                if is_prot:
                    return ToolError(message=reason, output=reason, brief="protected path")
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(content)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(content)
            return ToolOk(output=output)
        except urllib.error.URLError as e:
            return ToolError(message=f"curl: {e}", output="", brief="curl failed")
        except Exception as e:
            return ToolError(message=str(e), output="", brief="curl failed")
