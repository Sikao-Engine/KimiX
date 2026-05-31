from abc import ABC, abstractmethod
from asyncio import Future
import difflib
from functools import lru_cache
import typing
from typing import Any, ClassVar, Protocol, Self, cast, override, runtime_checkable

import jsonschema
import pydantic
from pydantic import BaseModel, GetCoreSchemaHandler, model_validator
from pydantic.json_schema import GenerateJsonSchema
from pydantic_core import core_schema

from kosong.message import ContentPart, ToolCall
from kosong.utils.jsonschema import deref_json_schema
from kosong.utils.typing import JsonType

type ParametersType = dict[str, Any]


class Tool(BaseModel):
    """The definition of a tool that can be recognized by the model."""

    name: str
    """The name of the tool."""

    description: str
    """The description of the tool."""

    parameters: ParametersType
    """The parameters of the tool, in JSON Schema format."""

    @model_validator(mode="after")
    def _validate_parameters(self) -> Self:
        jsonschema.validate(self.parameters, jsonschema.Draft202012Validator.META_SCHEMA)
        return self


class DisplayBlock(BaseModel, ABC):
    """
    A block of content to be displayed to the user.

    Similar to `ContentPart`, but scoped to user-facing UI.
    `ContentPart` is for model-facing message content; `DisplayBlock` is for tool/UI extensions.

    Unlike `ContentPart`, Kosong users may directly subclass `DisplayBlock` to define custom
    display blocks for their applications.
    """

    __display_block_registry: ClassVar[dict[str, type["DisplayBlock"]]] = {}

    type: str
    ...  # to be added by subclasses

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)

        invalid_subclass_error_msg = (
            f"DisplayBlock subclass {cls.__name__} must have a `type` field of type `str`"
        )

        type_value = getattr(cls, "type", None)
        if type_value is None or not isinstance(type_value, str):
            raise ValueError(invalid_subclass_error_msg)

        cls.__display_block_registry[type_value] = cls

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        # If we're dealing with the base DisplayBlock class, use custom validation
        if cls.__name__ == "DisplayBlock":

            def validate_display_block(value: Any) -> Any:
                # if it's already an instance of a DisplayBlock subclass, return it
                if hasattr(value, "__class__") and issubclass(value.__class__, cls):
                    return value

                # if it's a dict with a type field, dispatch to the appropriate subclass
                if isinstance(value, dict) and "type" in value:
                    type_value: Any | None = cast(dict[str, Any], value).get("type")
                    if not isinstance(type_value, str):
                        raise ValueError(f"Cannot validate {value} as DisplayBlock")
                    target_class = cls.__display_block_registry.get(type_value)
                    if target_class is None:
                        data = {k: v for k, v in cast(dict[str, Any], value).items() if k != "type"}
                        return UnknownDisplayBlock.model_validate(
                            {"type": type_value, "data": data}
                        )
                    return target_class.model_validate(value)

                raise ValueError(f"Cannot validate {value} as DisplayBlock")

            return core_schema.no_info_plain_validator_function(validate_display_block)

        # for subclasses, use the default schema
        return handler(source_type)


class UnknownDisplayBlock(DisplayBlock):
    """Fallback display block for unknown types."""

    type: str = "unknown"
    data: JsonType


class BriefDisplayBlock(DisplayBlock):
    """A brief display block with plain string content."""

    type: str = "brief"
    text: str


class ToolReturnValue(BaseModel):
    """The return type of a callable tool."""

    is_error: bool
    """Whether the tool call resulted in an error."""

    # For model
    output: str | list[ContentPart]
    """The output content returned by the tool."""
    message: str
    """An explanatory message to be given to the model."""

    # For user
    display: list[DisplayBlock]
    """The content blocks to be displayed to the user."""

    # For debugging/testing
    extras: dict[str, JsonType] | None = None

    @property
    def brief(self) -> str:
        """Get the brief display block data, if any."""
        for block in self.display:
            if isinstance(block, BriefDisplayBlock):
                return block.text
        return ""


class ToolOk(ToolReturnValue):
    """Subclass of `ToolReturnValue` representing a successful tool call."""

    def __init__(
        self,
        *,
        output: str | ContentPart | list[ContentPart],
        message: str = "",
        brief: str = "",
        display_block: DisplayBlock | None = None
    ) -> None:
        super().__init__(
            is_error=False,
            output=([output] if isinstance(output, ContentPart) else output),
            message=message,
            display=[display_block] if display_block is not None else ([BriefDisplayBlock(text=brief)] if brief else []),
        )


class ToolError(ToolReturnValue):
    """Subclass of `ToolReturnValue` representing a failed tool call."""

    def __init__(
        self, *, message: str, brief: str, output: str | ContentPart | list[ContentPart] = ""
    ):
        super().__init__(
            is_error=True,
            output=([output] if isinstance(output, ContentPart) else output),
            message=message,
            display=[BriefDisplayBlock(text=brief)] if brief else [],
        )


class CallableTool(Tool, ABC):
    """
    The abstract base class of tools that can be called as callables.

    The tool will be called with the arguments provided in the `ToolCall`.
    If the arguments are given as a JSON array, it will be unpacked into positional arguments.
    If the arguments are given as a JSON object, it will be unpacked into keyword arguments.
    Otherwise, the arguments will be passed as a single argument.
    """

    @property
    def base(self) -> Tool:
        """The base tool definition."""
        return self

    async def call(self, arguments: JsonType) -> ToolReturnValue:
        from kosong.tooling.error import ToolValidateError

        try:
            jsonschema.validate(arguments, self.parameters)
        except jsonschema.ValidationError as e:
            return ToolValidateError(str(e))

        if isinstance(arguments, list):
            ret = await self.__call__(*arguments)
        elif isinstance(arguments, dict):
            ret = await self.__call__(**arguments)
        else:
            ret = await self.__call__(arguments)
        if not isinstance(ret, ToolReturnValue):  # type: ignore[reportUnnecessaryIsInstance]
            # let's do not trust the return type of the tool
            ret = ToolError(
                message=f"Invalid return type: {type(ret)}",
                brief="Invalid return type",
            )
        return ret

    @abstractmethod
    async def __call__(self, *args: Any, **kwargs: Any) -> ToolReturnValue:
        """
        @public

        The implementation of the callable tool.
        """
        ...


# Common LLM field-name substitutions that differ from the canonical schema name.
# Maps the *wrong* key the LLM often sends → the *correct* field name.
#
# These are split into category-specific dicts so tools can opt into only the
# aliases relevant to their parameter schema, reducing false-positive repairs.

FIELD_ALIASES_GENERAL: dict[str, str] = {
    # title / description / message
    "content": "title",
    "text": "title",
    "name": "title",
    "desc": "description",
    "detail": "description",
    "label": "description",
    "summary": "description",
    "info": "description",
    "msg": "message",
    "note": "message",
    # reason / cause
    "cause": "reason",
    "explanation": "reason",
    "rationale": "reason",
    "justification": "reason",
    "purpose": "reason",
    # prompt / instruction
    "instruction": "prompt",
    "task": "prompt",
    "request": "prompt",
    # step / stage
    "stage": "step",
    "phase": "step",
    "entry": "step",
    # result / outcome
    "outcome": "result",
    "return": "result",
    "status": "result",
    # action / operation
    "operation": "action",
    "op": "action",
    # brief
    "short": "brief",
    # status / state
    "state": "status",
    # question / query
    "query": "question",
    # options / choices
    "choices": "options",
    "answers": "options",
    # multi_select
    "multiple": "multi_select",
    "allow_multiple": "multi_select",
    # header
    "category": "header",
    "tag": "header",
}

FIELD_ALIASES_FILE: dict[str, str] = {
    # path / file / directory
    "file": "path",
    "filepath": "path",
    "file_path": "path",
    "filename": "path",
    "file_name": "path",
    "dir": "path",
    "directory": "path",
    "folder": "path",
    "location": "path",
    # content / data
    "data": "content",
    "body": "content",
    "source": "content",
    "value": "content",
    # mode / method
    "method": "mode",
    "write_mode": "mode",
    # line_offset
    "offset": "line_offset",
    "start": "line_offset",
    "start_line": "line_offset",
    # n_lines
    "lines": "n_lines",
    "count": "n_lines",
    "num_lines": "n_lines",
    # max_char
    "chars": "max_char",
    "max_chars": "max_char",
    "char_limit": "max_char",
    # char_offset
    "byte_offset": "char_offset",
    "position": "char_offset",
    # include_dirs
    "dirs": "include_dirs",
    "directories": "include_dirs",
    # include_ignored
    "ignored": "include_ignored",
    "gitignore": "include_ignored",
    "hidden": "include_ignored",
    # case_insensitive
    "ignore_case": "case_insensitive",
    "insensitive": "case_insensitive",
    # head_limit
    "max": "head_limit",
    "max_results": "head_limit",
    "limit": "head_limit",
    # multiline
    "multi_line": "multiline",
    # output_mode
    "format": "output_mode",
    # type (file type)
    "file_type": "type",
    "kind": "type",
    # pattern / regex
    "regex": "pattern",
    "expr": "pattern",
    "expression": "pattern",
    "match": "pattern",
    # edit / changes
    "changes": "edit",
    "modifications": "edit",
    "patch": "edit",
    # edit nested fields
    "original": "old",
    "find": "old",
    "target": "old",
    "old_str": "old",
    "old_content": "old",
    "replace_with": "new",
    "to": "new",
    "new_str": "new",
    "new_content": "new",
    "all": "replace_all",
    # output_path / destination
    "out": "output_path",
    "output": "output_path",
    "destination": "output_path",
    "dest": "output_path",
    # files / paths
    "paths": "files",
    "file_list": "files",
    # glob / filter
    "filter": "glob",
    "file_pattern": "glob",
    # directory (Glob)
    "path": "directory",
    # pages (ReadFile)
    "page": "pages",
    "page_range": "pages",
    # edit (EditFile)
    "edits": "edit",
    # line_number (Grep)
    "show_line_numbers": "line_number",
    "line_numbers": "line_number",
    "show_numbers": "line_number",
    "number": "line_number",
    # ignore_case (Grep) - reciprocal
    "case_insensitive": "ignore_case",
}

FIELD_ALIASES_SHELL: dict[str, str] = {
    # command / code / script
    "cmd": "command",
    "script": "command",
    "shell_command": "command",
    "program": "code",
    "snippet": "code",
    # timeout / wait
    "wait": "timeout",
    "delay": "timeout",
    "time_limit": "timeout",
    "duration": "timeout",
    # run_in_background
    "background": "run_in_background",
    "async": "run_in_background",
    "detach": "run_in_background",
    "bg": "run_in_background",
    # args / arguments
    "arguments": "args",
    "params": "args",
    "arg": "args",
    "parameters": "args",
    # cwd
    "working_dir": "cwd",
    "work_dir": "cwd",
    # env / variables
    "environment": "env",
    "vars": "env",
    "variables": "env",
    # command
    "code": "command",
}

FIELD_ALIASES_WEB: dict[str, str] = {
    # url / link
    "link": "url",
    "href": "url",
    "address": "url",
    "uri": "url",
    "site": "url",
    # query / search
    "q": "query",
    "search": "query",
    "keyword": "query",
    "keywords": "query",
    "term": "query",
    "question": "query",
}

FIELD_ALIASES_TASK: dict[str, str] = {
    # task_id
    "id": "task_id",
    "job_id": "task_id",
    # block / sync
    "blocking": "block",
    "sync": "block",
    # kill / stop
    "force": "kill",
    "terminate": "kill",
    "stop": "kill",
    # limit (TaskList)
    "head_limit": "limit",
    # reason (TaskStop)
    "message": "reason",
}

FIELD_ALIASES_INPUT: dict[str, str] = {
    # text / input / stdin
    "input": "text",
    "stdin": "text",
}

FIELD_ALIASES_SEARCH: dict[str, str] = {
    # k / n / top_k
    "n": "k",
    "top_k": "k",
    "num": "k",
    # questions
    "queries": "questions",
    "msgs": "questions",
}

FIELD_ALIASES_MODEL: dict[str, str] = {
    # model / llm
    "llm": "model",
    "model_name": "model",
    # resume / session
    "continue": "resume",
    "agent_id": "resume",
}

FIELD_ALIASES_TODO: dict[str, str] = {
    # todos
    "items": "todos",
    "list": "todos",
    "tasks": "todos",
    "entries": "todos",
    # force_replace
    "replace": "force_replace",
    "override": "force_replace",
}

FIELD_ALIASES_ACTIVE: dict[str, str] = {
    # active_only
    "active": "active_only",
    "running": "active_only",
    "current": "active_only",
}

FIELD_ALIASES_SUBAGENT: dict[str, str] = {
    # subagent_type
    "agent_type": "subagent_type",
}

# Merge all categories into the common set for backward compatibility.
_COMMON_FIELD_ALIASES: dict[str, str] = {
    **FIELD_ALIASES_GENERAL,
    **FIELD_ALIASES_FILE,
    **FIELD_ALIASES_SHELL,
    **FIELD_ALIASES_WEB,
    **FIELD_ALIASES_TASK,
    **FIELD_ALIASES_INPUT,
    **FIELD_ALIASES_SEARCH,
    **FIELD_ALIASES_MODEL,
    **FIELD_ALIASES_TODO,
    **FIELD_ALIASES_ACTIVE,
    **FIELD_ALIASES_SUBAGENT,
}


def _get_base_model_type(annotation: Any) -> type[BaseModel] | None:
    """Extract a BaseModel subclass from a type annotation, unwrapping generics."""
    origin = typing.get_origin(annotation)
    if origin is not None:
        for arg in typing.get_args(annotation):
            result = _get_base_model_type(arg)
            if result is not None:
                return result
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None


def _clamp_numeric_value(value: Any, finfo: Any) -> Any | None:
    """Clamp a numeric value to the field's min/max constraints.

    Returns the clamped value if it was out of bounds, otherwise None.
    """
    constraints = _extract_constraints(finfo)
    if constraints is None:
        return None
    return _apply_constraints(value, constraints)


def _extract_constraints(finfo: Any) -> tuple[float | int | None, float | int | None, bool, bool] | None:
    """Extract numeric constraints from a FieldInfo's metadata.

    Returns (min_val, max_val, min_inclusive, max_inclusive) if any constraints exist,
    otherwise None.
    """
    min_val: float | int | None = None
    max_val: float | int | None = None
    min_inclusive = True
    max_inclusive = True
    has_any = False

    for m in getattr(finfo, "metadata", ()):
        if hasattr(m, "ge"):
            min_val = m.ge
            min_inclusive = True
            has_any = True
        elif hasattr(m, "gt"):
            min_val = m.gt
            min_inclusive = False
            has_any = True
        elif hasattr(m, "le"):
            max_val = m.le
            max_inclusive = True
            has_any = True
        elif hasattr(m, "lt"):
            max_val = m.lt
            max_inclusive = False
            has_any = True

    return (min_val, max_val, min_inclusive, max_inclusive) if has_any else None


def _apply_constraints(value: Any, constraints: tuple[float | int | None, float | int | None, bool, bool]) -> Any | None:
    """Clamp a numeric value to pre-extracted constraints.

    Returns the clamped value if it was out of bounds, otherwise None.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None

    min_val, max_val, min_inclusive, max_inclusive = constraints
    clamped = value

    if min_val is not None:
        if min_inclusive and clamped < min_val:
            clamped = min_val
        elif not min_inclusive and clamped <= min_val:
            if isinstance(clamped, int) and isinstance(min_val, int):
                clamped = min_val + 1
            else:
                clamped = min_val

    if max_val is not None:
        if max_inclusive and clamped > max_val:
            clamped = max_val
        elif not max_inclusive and clamped >= max_val:
            if isinstance(clamped, int) and isinstance(max_val, int):
                clamped = max_val - 1
            else:
                clamped = max_val

    return clamped if clamped != value else None


@lru_cache(maxsize=512)
def _cached_model_field_info(model: type[BaseModel]) -> tuple[
    dict[str, str],
    list[tuple[str, type[BaseModel] | None, tuple[float | int | None, float | int | None, bool, bool] | None]],
]:
    """Precompute known key mapping and field metadata for a model."""
    known: dict[str, str] = {}
    field_meta: list[tuple[str, type[BaseModel] | None, tuple[float | int | None, float | int | None, bool, bool] | None]] = []

    for fname, finfo in model.model_fields.items():
        known[fname] = fname
        if finfo.alias and finfo.alias != fname:
            known[finfo.alias] = fname
        val_alias = getattr(finfo, "validation_alias", None)
        if isinstance(val_alias, str) and val_alias != fname:
            known[val_alias] = fname

        nested = _get_base_model_type(finfo.annotation)
        constraints = _extract_constraints(finfo)
        field_meta.append((fname, nested, constraints))

    return known, field_meta


def _repair_dict_for_model(
    data: dict[str, Any],
    model: type[BaseModel],
    common_aliases: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Recursively repair dict keys to match a Pydantic model's expected fields.

    1. Map exact field names and declared aliases.
    2. For remaining missing fields, try common LLM aliases.
    3. Fuzzy-match unmapped keys to missing fields.
    4. Clamp numeric values that exceed field constraints.
    5. Recurse into nested BaseModel fields and list items.
    """
    if common_aliases is None:
        common_aliases = _COMMON_FIELD_ALIASES

    known, field_meta = _cached_model_field_info(model)

    # First pass: map known keys.
    mapped: dict[str, Any] = {}
    unmapped_keys: set[str] = set()
    for key, value in data.items():
        canonical = known.get(key)
        if canonical is not None:
            mapped[canonical] = value
        else:
            unmapped_keys.add(key)
            mapped[key] = value

    # Second pass: try common aliases for fields that are still missing.
    # Build reverse alias map for O(1) lookup of candidate bad keys per good key.
    reverse_aliases: dict[str, list[str]] = {}
    for bad_key, good_key in common_aliases.items():
        reverse_aliases.setdefault(good_key, []).append(bad_key)

    missing = set(model.model_fields.keys()) - set(mapped.keys())
    for missing_field in list(missing):
        for bad_key in reverse_aliases.get(missing_field, ()):
            if bad_key in unmapped_keys:
                mapped[missing_field] = mapped.pop(bad_key)
                unmapped_keys.discard(bad_key)
                missing.discard(missing_field)
                break

    # Third pass: fuzzy match remaining missing fields against still-available unmapped keys.
    available = list(unmapped_keys)
    if available and missing:
        candidates: list[tuple[float, str, str]] = []
        for missing_field in missing:
            if len(missing_field) < 4:
                continue
            # Longer field names are harder to type exactly; allow a slightly lower cutoff.
            cutoff = 0.75 if len(missing_field) >= 8 else 0.80
            close = difflib.get_close_matches(
                missing_field, available, n=1, cutoff=cutoff
            )
            if close:
                matched_key = close[0]
                if len(matched_key) < 4:
                    continue
                ratio = difflib.SequenceMatcher(None, missing_field, matched_key).ratio()
                candidates.append((ratio, missing_field, matched_key))
        # Apply strongest matches first; each unmapped key can only be used once.
        candidates.sort(key=lambda x: x[0], reverse=True)
        used: set[str] = set()
        for _ratio, missing_field, matched_key in candidates:
            if matched_key in used:
                continue
            used.add(matched_key)
            mapped[missing_field] = mapped.pop(matched_key)

    # Fourth pass: recurse into nested models and clamp numeric values.
    for fname, nested, constraints in field_meta:
        if fname not in mapped:
            continue
        val = mapped[fname]
        if nested is not None:
            if isinstance(val, dict):
                mapped[fname] = _repair_dict_for_model(val, nested, common_aliases)
            elif isinstance(val, list):
                mapped[fname] = [
                    _repair_dict_for_model(item, nested, common_aliases) if isinstance(item, dict) else item
                    for item in val
                ]
        elif constraints is not None:
            clamped = _apply_constraints(val, constraints)
            if clamped is not None:
                mapped[fname] = clamped

    return mapped


class _GenerateJsonSchemaNoTitles(GenerateJsonSchema):
    """Custom JSON schema generator that omits titles."""

    @override
    def field_title_should_be_set(self, schema) -> bool:  # type: ignore[reportMissingParameterType]
        return False

    @override
    def _update_class_schema(self, json_schema, cls, config) -> None:  # type: ignore[reportMissingParameterType]
        super()._update_class_schema(json_schema, cls, config)
        json_schema.pop("title", None)


class CallableTool2[Params: BaseModel](ABC):
    """
    The abstract base class of tools that can be called as callables, with typed parameters.

    The tool will be called with the arguments provided in the `ToolCall`.
    The arguments must be a JSON object, and will be validated by Pydantic to the `Params` type.
    """

    name: str
    """The name of the tool."""
    description: str
    """The description of the tool."""
    params: type[Params]
    """The Pydantic model type of the tool parameters."""
    field_aliases: ClassVar[dict[str, str]] = _COMMON_FIELD_ALIASES
    """Aliases used when repairing dict keys to match this tool's parameter schema."""

    def __init__(
        self,
        name: str | None = None,
        description: str | None = None,
        params: type[Params] | None = None,
    ) -> None:
        cls = self.__class__

        self.name = name or getattr(cls, "name", "")
        if not self.name:
            raise ValueError(
                "Tool name must be provided either as class variable or constructor argument"
            )
        if not isinstance(self.name, str):  # type: ignore[reportUnnecessaryIsInstance]
            raise ValueError("Tool name must be a string")

        self.description = description or getattr(cls, "description", "")
        if not self.description:
            raise ValueError(
                "Tool description must be provided either as class variable or constructor argument"
            )
        if not isinstance(self.description, str):  # type: ignore[reportUnnecessaryIsInstance]
            raise ValueError("Tool description must be a string")

        self.params = params or getattr(cls, "params", None)  # type: ignore
        if not self.params:
            raise ValueError(
                "Tool param must be provided either as class variable or constructor argument"
            )
        if not isinstance(self.params, type) or not issubclass(self.params, BaseModel):  # type: ignore[reportUnnecessaryIsInstance]
            raise ValueError("Tool params must be a subclass of pydantic.BaseModel")

        self._base = Tool(
            name=self.name,
            description=self.description,
            parameters=deref_json_schema(
                self.params.model_json_schema(schema_generator=_GenerateJsonSchemaNoTitles)
            ),
        )

    @property
    def base(self) -> Tool:
        """The base tool definition."""
        return self._base

    async def call(self, arguments: JsonType) -> ToolReturnValue:
        from kosong.tooling.error import ToolValidateError

        try:
            params = self.params.model_validate(arguments)
        except pydantic.ValidationError as e:
            # Attempt to repair common LLM field-name mismatches and re-validate.
            if isinstance(arguments, dict):
                repaired = _repair_dict_for_model(arguments, self.params, self.field_aliases)
                if repaired != arguments:
                    try:
                        params = self.params.model_validate(repaired)
                    except pydantic.ValidationError:
                        pass  # fall through to return the original error
                    else:
                        return await self.__call__(params)
            return ToolValidateError(str(e))

        ret = await self.__call__(params)
        if not isinstance(ret, ToolReturnValue):  # type: ignore[reportUnnecessaryIsInstance]
            # let's do not trust the return type of the tool
            ret = ToolError(
                message=f"Invalid return type: {type(ret)}",
                brief="Invalid return type",
            )
        return ret

    @abstractmethod
    async def __call__(self, params: Params) -> ToolReturnValue:
        """
        @public

        The implementation of the callable tool.
        """
        ...


class ToolResult(BaseModel):
    """The result of a tool call."""

    tool_call_id: str
    """The ID of the tool call."""
    return_value: ToolReturnValue
    """The actual return value of the tool call."""


ToolResultFuture = Future[ToolResult]
type HandleResult = ToolResultFuture | ToolResult


@runtime_checkable
class Toolset(Protocol):
    """
    The interface of toolsets that can register tools and handle tool calls.
    """

    @property
    def tools(self) -> list[Tool]:
        """The list of tool definitions registered in this toolset."""
        ...

    def handle(self, tool_call: ToolCall) -> HandleResult:
        """
        Handle a tool call.
        The result of the tool call, or the async future of the result, should be returned.
        The result should be a `ToolReturnValue`.

        This method MUST NOT do any blocking operations because it will be called during
        consuming the chat response stream.
        This method MUST NOT raise any exception except for `asyncio.CancelledError`. Any other
        error should be returned as a `ToolReturnValue` with `is_error=True`.
        """
        ...
