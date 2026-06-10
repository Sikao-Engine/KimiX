"""Parser tool: wraps kimix.parser parsers into a CallableTool2 for use by agents."""

from __future__ import annotations

from typing import Any

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from pydantic import BaseModel, Field
from kimix.parser import (
    PythonParser,
    CParser,
    ShellParser,
    HtmlParser,
    PascalParser,
    LispParser,
    SqlParser,
    BaseParser,
)

try:
    import orjson

    _HAS_ORJSON = True
except ImportError:
    import json

    _HAS_ORJSON = False


# ── Language → Parser mapping ──────────────────────────────────────────────────

_LANGUAGE_MAP: dict[str, BaseParser] = {
    # Python
    "python": PythonParser(),
    # C-family (C, C++, Java, JavaScript, TypeScript, Go, Rust, C#, Swift, Kotlin, PHP)
    "c": CParser(),
    "cpp": CParser(),
    "java": CParser(),
    "javascript": CParser(),
    "typescript": CParser(),
    "go": CParser(),
    "rust": CParser(),
    "csharp": CParser(),
    "swift": CParser(),
    "kotlin": CParser(),
    "php": CParser(),
    # Shell / Bash
    "shell": ShellParser(),
    "bash": ShellParser(),
    # HTML / XML
    "html": HtmlParser(),
    "xml": HtmlParser(),
    # Pascal / Delphi / Ada
    "pascal": PascalParser(),
    "delphi": PascalParser(),
    "ada": PascalParser(),
    # Lisp-family
    "lisp": LispParser(),
    "scheme": LispParser(),
    "clojure": LispParser(),
    # SQL
    "sql": SqlParser(),
    "mysql": SqlParser(),
    "postgresql": SqlParser(),
}


def _to_json(data: Any) -> str:
    """Serialize *data* to a JSON string using orjson (preferred) or stdlib json."""
    if _HAS_ORJSON:
        return orjson.dumps(data, option=orjson.OPT_INDENT_2).decode("utf-8")
    return json.dumps(data, indent=2, ensure_ascii=False, default=str)


# ── Params ─────────────────────────────────────────────────────────────────────


class Params(BaseModel):
    """Parameters for the ParserTool."""

    language: str = Field(
        description=(
            "Target language. "
            "Choices: python, c, cpp, java, javascript, typescript, go, rust, "
            "csharp, swift, kotlin, php, shell, bash, html, xml, pascal, delphi, "
            "ada, lisp, scheme, clojure, sql, mysql, postgresql"
        ),
    )
    source_code: str | None = Field(
        default=None,
        description="Source code text to parse (optional if file_path provided).",
    )
    file_path: str | None = Field(
        default=None,
        description="Path to source file (optional if source_code provided).",
    )
    mode: str = Field(
        default="extract",
        description=(
            'Operation mode: "extract" (return only comments), '
            '"strip" (return code without comments), '
            '"both" (return both).'
        ),
    )
    encoding: str = Field(
        default="utf-8",
        description="File encoding (used when reading from file_path).",
    )


# ── ParserTool ─────────────────────────────────────────────────────────────────


class ParserTool(CallableTool2[Params]):
    """Parse source code or files to extract or strip comments."""

    name: str = "ParserTool"
    description: str = (
        "Parse source code or files to extract or strip comments. "
        "Supports many programming languages."
    )
    params: type[Params] = Params

    def __init__(self, session: Any = None) -> None:
        """Initialise the parser tool.

        Args:
            session: Optional session object (currently unused but kept for
                     compatibility with the CallableTool2 interface).
        """
        super().__init__()
        self._session = session

    async def __call__(self, params: Params) -> ToolReturnValue:
        """Execute the parser tool."""
        # ── 1. Validate inputs ────────────────────────────────────────────────
        if not params.source_code and not params.file_path:
            return ToolError(
                output="",
                message="Either 'source_code' or 'file_path' must be provided.",
                brief="Missing input",
            )

        # ── 2. Resolve parser ─────────────────────────────────────────────────
        language_key = params.language.strip().lower()
        parser = _LANGUAGE_MAP.get(language_key)
        if parser is None:
            supported = sorted(_LANGUAGE_MAP.keys())
            return ToolError(
                output="",
                message=(
                    f"Unsupported language: '{params.language}'. "
                    f"Supported languages: {', '.join(supported)}"
                ),
                brief="Unsupported language",
            )

        # ── 3. Parse ──────────────────────────────────────────────────────────
        try:
            if params.source_code:
                result = parser.parse(params.source_code)
            else:
                # file_path is guaranteed to be a string here
                result = parser.parse_file(params.file_path, params.encoding)  # type: ignore[arg-type]
        except FileNotFoundError:
            return ToolError(
                output="",
                message=f"File not found: '{params.file_path}'",
                brief="File not found",
            )
        except UnicodeDecodeError as exc:
            return ToolError(
                output="",
                message=f"Encoding error reading '{params.file_path}': {exc}",
                brief="Encoding error",
            )
        except Exception as exc:
            return ToolError(
                output="",
                message=f"Parsing error: {exc}",
                brief="Parsing error",
            )

        # ── 4. Format output based on mode ────────────────────────────────────
        try:
            if params.mode == "extract":
                comments_data = [
                    {
                        "content": c.content,
                        "line": c.line,
                        "column": c.column,
                        "kind": c.kind,
                    }
                    for c in result.comments
                ]
                output = _to_json(comments_data)
                return ToolOk(
                    output=output,
                    brief=f"Extracted {len(result.comments)} comment(s) from {language_key} source",
                )

            elif params.mode == "strip":
                return ToolOk(
                    output=result.code_without_comments,
                    brief=f"Stripped {len(result.comments)} comment(s) from {language_key} source",
                )

            elif params.mode == "both":
                comments_data = [
                    {
                        "content": c.content,
                        "line": c.line,
                        "column": c.column,
                        "kind": c.kind,
                    }
                    for c in result.comments
                ]
                output = _to_json(
                    {
                        "comments": comments_data,
                        "code_without_comments": result.code_without_comments,
                    }
                )
                return ToolOk(
                    output=output,
                    brief=(
                        f"Extracted {len(result.comments)} comment(s) and "
                        f"stripped code from {language_key} source"
                    ),
                )

            else:
                return ToolError(
                    output="",
                    message=(
                        f"Invalid mode: '{params.mode}'. "
                        "Expected 'extract', 'strip', or 'both'."
                    ),
                    brief="Invalid mode",
                )
        except Exception as exc:
            return ToolError(
                output="",
                message=f"Output formatting error: {exc}",
                brief="Formatting error",
            )
