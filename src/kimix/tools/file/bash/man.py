"""man tool - display information about available bash commands."""
import os
from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from .params import Params

from kimix.tools.common import _maybe_export_output_async


class Man(CallableTool2[Params]):
    name: str = "Man"
    description: str = "Display information about available bash commands."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            from kimix.tools.file.run import _BASH_COMMANDS

            cwd = params.cwd or os.getcwd()

            # Check if a specific command is requested
            cmd_name = None
            for i, arg in enumerate(params.args):
                if not arg.startswith("-"):
                    cmd_name = arg
                    break

            if cmd_name:
                # Show info for specific command
                if cmd_name in _BASH_COMMANDS:
                    tool = _BASH_COMMANDS[cmd_name]
                    output = f"NAME\n    {cmd_name} - {tool.description}\n\n"
                    output += f"TOOL\n    {tool.name}\n\n"
                    output += f"PARAMETERS\n    {tool.params.__doc__ or 'See tool documentation'}\n"
                else:
                    output = f"No manual entry for '{cmd_name}'. Available commands: {', '.join(sorted(_BASH_COMMANDS.keys()))}"
            else:
                # List all available commands
                output = "AVAILABLE BASH COMMANDS\n\n"
                output += "The following bash commands are implemented in pure Python:\n\n"
                for name in sorted(_BASH_COMMANDS.keys()):
                    tool = _BASH_COMMANDS[name]
                    output += f"  {name:12} - {tool.description}\n"

                output += "\n\nUse 'man <command>' for detailed information about a specific command.\n"

            if params.output_path:
                with open(params.output_path, "w", encoding="utf-8") as f:
                    f.write(output)
                output = f"saved to file `{params.output_path}`"
            else:
                output = await _maybe_export_output_async(output)
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="man failed")
