from kosong.tooling import ToolError


class ToolNotFoundError(ToolError):
    """The tool was not found."""

    def __init__(self, tool_name: str, suggestions: list[str] | None = None):
        message = f"Tool `{tool_name}` not found"
        brief = f"Tool `{tool_name}` not found"
        if suggestions:
            hint = "did you mean " + ", ".join(f"`{s}`" for s in suggestions) + "?"
            message += f" - {hint}"
            brief += f" - {hint}"
        super().__init__(message=message, brief=brief)


class ToolParseError(ToolError):
    """The arguments of the tool are not valid JSON."""

    def __init__(self, message: str):
        super().__init__(
            message=f"Error parsing JSON arguments: {message}",
            brief="Invalid arguments",
        )


class ToolValidateError(ToolError):
    """The arguments of the tool are not valid."""

    def __init__(self, message: str):
        super().__init__(
            message=f"Error validating JSON arguments: {message}",
            brief="Invalid arguments",
        )


class ToolRuntimeError(ToolError):
    """The tool failed to run."""

    def __init__(self, message: str):
        super().__init__(
            message=f"Error running tool: {message}",
            brief="Tool runtime error",
        )
