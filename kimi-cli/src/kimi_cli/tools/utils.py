import regex as re
from pathlib import Path
from typing import Any, Union, get_args, get_origin

import json_repair
import orjson
from jinja2 import Environment, Undefined
from kosong.tooling import BriefDisplayBlock, DisplayBlock, ToolError, ToolReturnValue
from kosong.utils.typing import JsonType
from pydantic import BaseModel


def _looks_like_json(value: str) -> bool:
    """Return True if the string starts with a JSON array/object opener."""
    stripped = value.strip()
    return bool(stripped) and stripped[0] in ("[", "{")


def repair_json_string(value: str) -> Any | None:
    """Parse or repair a JSON string. Return None if it cannot be parsed/repaired.

    First attempts a fast ``orjson.loads`` parse. If that fails, falls back to
    ``json_repair.repair_json(return_objects=True)`` to recover from common
    serialization mistakes such as missing closing brackets or trailing commas.
    """
    if not _looks_like_json(value):
        return None
    try:
        return orjson.loads(value)
    except orjson.JSONDecodeError:
        pass
    try:
        repaired = json_repair.repair_json(value, return_objects=True)
        if repaired is None or repaired == "":
            return None
        return repaired
    except Exception:
        return None


def _is_plain_string_annotation(annotation: Any) -> bool:
    """Return True if the annotation is ``str`` or ``str | None``/``Optional[str]``."""
    if annotation is str:
        return True
    origin = get_origin(annotation)
    if origin is Union:
        return all(arg is type(None) or _is_plain_string_annotation(arg) for arg in get_args(annotation))
    return False


def repair_tool_arguments(
    params_model: type[BaseModel], arguments: Any
) -> dict[str, Any]:
    """Repair JSON-string values in tool arguments based on the params schema.

    Only fields annotated as plain ``str`` (or optional str) are left untouched;
    all other fields are candidates for JSON-string repair when the value looks
    like JSON.

    Non-dict inputs are coerced to an empty dict so callers never see a raw
    ``dict()`` construction error.
    """
    if arguments is None:
        return {}
    if isinstance(arguments, str):
        parsed = repair_json_string(arguments)
        if isinstance(parsed, dict):
            arguments = parsed
        else:
            return {}
    if not isinstance(arguments, dict):
        return {}
    repaired = dict(arguments)
    fields = params_model.model_fields
    for key, value in list(repaired.items()):
        if not isinstance(value, str):
            continue
        field_info = fields.get(key)
        if field_info is None:
            continue
        if _is_plain_string_annotation(field_info.annotation):
            continue
        parsed = repair_json_string(value)
        if parsed is not None:
            repaired[key] = parsed
    return repaired


class _KeepPlaceholderUndefined(Undefined):
    def __str__(self) -> str:
        if self._undefined_name is None:
            return ""
        return f"${{{self._undefined_name}}}"

    __repr__ = __str__


def load_desc(path: Path, context: dict[str, object] | None = None) -> str:
    """Load a tool desc from a file via Jinja2."""
    description = path.read_text(encoding="utf-8")
    env = Environment(
        keep_trailing_newline=True,
        lstrip_blocks=True,
        trim_blocks=True,
        variable_start_string="${",
        variable_end_string="}",
        undefined=_KeepPlaceholderUndefined,
    )
    template = env.from_string(description)
    return template.render(context or {})


def truncate_line(line: str, max_length: int, marker: str = "...") -> str:
    """
    Truncate a line if it exceeds `max_length`, preserving the beginning and the line break.
    The output may be longer than `max_length` if it is too short to fit the marker.
    """
    if len(line) <= max_length:
        return line

    # Find line breaks at the end of the line
    m = re.search(r"[\r\n]+$", line)
    linebreak = m.group(0) if m else ""
    end = marker + linebreak
    max_length = max(max_length, len(end))
    return line[: max_length - len(end)] + end


# Default output limits
DEFAULT_MAX_CHARS = 50_000
DEFAULT_MAX_LINE_LENGTH = 2000


class ToolResultBuilder:
    """
    Builder for tool results with character and line limits.
    """

    def __init__(
        self,
        max_chars: int = DEFAULT_MAX_CHARS,
        max_line_length: int | None = DEFAULT_MAX_LINE_LENGTH,
    ):
        self.max_chars = max_chars
        self.max_line_length = max_line_length
        self._marker = "[...truncated]"
        if max_line_length is not None:
            assert max_line_length > len(self._marker)
        self._buffer: list[str] = []
        self._n_chars = 0
        self._n_lines = 0
        self._truncation_happened = False
        self._display: list[DisplayBlock] = []
        self._extras: dict[str, JsonType] | None = None

    @property
    def is_full(self) -> bool:
        """Check if output buffer is full due to character limit."""
        return self._n_chars >= self.max_chars

    @property
    def n_chars(self) -> int:
        """Get current character count."""
        return self._n_chars

    @property
    def n_lines(self) -> int:
        """Get current line count."""
        return self._n_lines

    def write(self, text: str) -> int:
        """
        Write text to the output buffer.

        Returns:
            int: Number of characters actually written
        """
        if self.is_full:
            return 0

        lines = text.splitlines(keepends=True)
        if not lines:
            return 0

        chars_written = 0

        for line in lines:
            if self.is_full:
                break

            original_line = line
            remaining_chars = self.max_chars - self._n_chars
            limit = (
                min(remaining_chars, self.max_line_length)
                if self.max_line_length is not None
                else remaining_chars
            )
            line = truncate_line(line, limit, self._marker)
            if line != original_line:
                self._truncation_happened = True

            self._buffer.append(line)
            chars_written += len(line)
            self._n_chars += len(line)
            if line.endswith("\n"):
                self._n_lines += 1

        return chars_written

    def tail(self, max_lines: int = 5, max_line_len: int = 200) -> str:
        """Return the last non-empty lines from the buffer, joined with newlines.

        Useful for surfacing actionable error context (stderr) in tool result briefs.
        """
        collected: list[str] = []
        for chunk in reversed(self._buffer):
            for line in reversed(chunk.splitlines()):
                stripped = line.rstrip()
                if not stripped.strip():
                    continue
                if len(stripped) > max_line_len:
                    stripped = stripped[:max_line_len] + "..."
                collected.append(stripped)
                if len(collected) >= max_lines:
                    break
            if len(collected) >= max_lines:
                break
        return "\n".join(reversed(collected))

    def display(self, *blocks: DisplayBlock) -> None:
        """Add display blocks to the tool result."""
        self._display.extend(blocks)

    def extras(self, **extras: JsonType) -> None:
        """Add extra data to the tool result."""
        if self._extras is None:
            self._extras = {}
        self._extras.update(extras)

    def ok(self, message: str = "", *, brief: str = "") -> ToolReturnValue:
        """Create a ToolReturnValue with is_error=False and the current output."""
        output = "".join(self._buffer)

        final_message = message
        if final_message and not final_message.endswith("."):
            final_message += "."
        truncation_msg = "Output truncated."
        if self._truncation_happened:
            if final_message:
                final_message += f" {truncation_msg}"
            else:
                final_message = truncation_msg
        return ToolReturnValue(
            is_error=False,
            output=output,
            message=final_message,
            display=([BriefDisplayBlock(text=brief)] if brief else []) + self._display,
            extras=self._extras,
        )

    def error(self, message: str, *, brief: str) -> ToolReturnValue:
        """Create a ToolReturnValue with is_error=True and the current output."""
        output = "".join(self._buffer)

        final_message = message
        if self._truncation_happened:
            truncation_msg = "Output truncated."
            if final_message:
                final_message += f" {truncation_msg}"
            else:
                final_message = truncation_msg

        return ToolReturnValue(
            is_error=True,
            output=output,
            message=final_message,
            display=([BriefDisplayBlock(text=brief)] if brief else []) + self._display,
            extras=self._extras,
        )


class ToolRejectedError(ToolError):
    has_feedback: bool = False

    def __init__(
        self,
        message: str | None = None,
        brief: str = "Rejected by user",
        has_feedback: bool = False,
    ):
        super().__init__(
            message=message
            or (
                "Tool call rejected by user. Stop and wait for instructions."
            ),
            brief=brief,
        )
        self.has_feedback = has_feedback
