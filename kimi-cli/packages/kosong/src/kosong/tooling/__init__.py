from abc import ABC, abstractmethod
from asyncio import Future
import difflib
from functools import lru_cache
import orjson
import typing
from typing import Any, ClassVar, Protocol, Self, cast, override, runtime_checkable

import jsonschema
import pydantic
from pydantic import BaseModel, GetCoreSchemaHandler, model_validator
from pydantic.json_schema import GenerateJsonSchema
from pydantic_core import core_schema

from kosong.message import ContentPart, ToolCall
from kosong.utils.jsonschema import deref_json_schema
from kosong.utils.jsonx import loads_relaxed
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
    "verb": "action",
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
    "old_string": "old",
    "old_content": "old",
    "replace_with": "new",
    "to": "new",
    "new_str": "new",
    "new_string": "new",
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
    # before_context / after_context / context (Grep -B/-A/-C)
    "before": "before_context",
    "after": "after_context",
    "around": "context",
    # include_content (Search web)
    "include": "include_content",
    "show_content": "include_content",
    "with_content": "include_content",
    # dest_path (Search/Skill)
    "destination_path": "dest_path",
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
    "replace": "mode",
    "override": "mode",
    "overwrite": "mode",
    "append": "mode",
    "merge": "mode",
    "update": "mode",
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
    # session_id
    "session": "session_id",
    "sid": "session_id",
    # close_session
    "close": "close_session",
    "end": "close_session",
    "end_session": "close_session",
    "finish": "close_session",
    "done": "close_session",
    # return_history
    "history": "return_history",
    "full_history": "return_history",
    # response
    "reply": "response",
    "answer": "response",
    # context (AskParentParams)
    "ctx": "context",
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

# Precomputed reverse lookup for the common aliases to avoid rebuilding it
# on every call to ``_repair_dict_for_model``.
_REVERSE_ALIASES: dict[str, list[str]] = {}
for _bad_key, _good_key in _COMMON_FIELD_ALIASES.items():
    _REVERSE_ALIASES.setdefault(_good_key, []).append(_bad_key)


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


def _maybe_parse_json_string(value: Any) -> Any:
    """If *value* is a string containing a JSON object/array, return it parsed.

    Returns the original value when it is not a string, when parsing fails,
    or when the parsed value is not a structural JSON object/array, so that
    normal Pydantic validation can report the error.
    """
    if not isinstance(value, str):
        return value
    try:
        parsed = loads_relaxed(value)
    except Exception:
        return value
    if isinstance(parsed, (dict, list)):
        return parsed
    return value


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
    list[tuple[
        str,
        type[BaseModel] | None,
        tuple[float | int | None, float | int | None, bool, bool] | None,
        bool,
        type | None,
    ]],
    bool,
    frozenset[str],
]:
    """Precompute known key mapping and field metadata for a model.

    Returns ``(known, field_meta, extra_forbid, valid_names)`` where
    ``field_meta`` tuples contain:
    ``(field_name, nested_model, constraints, is_list, scalar_type)``.
    """
    known: dict[str, str] = {}
    field_meta: list[tuple[
        str,
        type[BaseModel] | None,
        tuple[float | int | None, float | int | None, bool, bool] | None,
        bool,
        type | None,
    ]] = []
    extra_forbid = model.model_config.get("extra") == "forbid"

    for fname, finfo in model.model_fields.items():
        known[fname] = fname
        if finfo.alias and finfo.alias != fname:
            known[finfo.alias] = fname
        val_alias = getattr(finfo, "validation_alias", None)
        if isinstance(val_alias, str) and val_alias != fname:
            known[val_alias] = fname

        nested = _get_base_model_type(finfo.annotation)
        constraints = _extract_constraints(finfo)
        origin = typing.get_origin(finfo.annotation)
        origin_name = getattr(origin, "__name__", "") if origin is not None else ""
        is_list = origin_name == "list"
        scalar_type = _resolve_scalar_type(finfo.annotation)
        field_meta.append((fname, nested, constraints, is_list, scalar_type))

    return known, field_meta, extra_forbid, frozenset(model.model_fields.keys())


def _fuzzy_match_keys(
    missing: set[str],
    available: set[str],
    cutoff_short: float = 0.80,
    cutoff_long: float = 0.75,
    min_length: int = 4,
) -> dict[str, str]:
    """Fuzzy-map missing field names to still-available unmapped keys.

    The matching is case-insensitive so that plausible LLM casing differences
    (e.g. ``base_url`` vs ``base_URL``) do not suppress an otherwise strong
    match. Each available key is consumed at most once, and the strongest
    matches are applied first.

    Returns a mapping ``missing_field -> original_available_key``.
    """
    if not missing or not available:
        return {}

    # Build lowercase lookup so we can match case-insensitively while still
    # returning the original key.
    available_lower: dict[str, str] = {}
    for key in available:
        lower = key.lower()
        # If two keys differ only by case, keep the first encountered; this is
        # deterministic and rare in practice.
        available_lower.setdefault(lower, key)
    available_list = list(available_lower.keys())

    candidates: list[tuple[float, str, str]] = []
    for missing_field in missing:
        if len(missing_field) < min_length:
            continue
        # Longer field names are harder to type exactly; allow a slightly lower cutoff.
        cutoff = cutoff_long if len(missing_field) >= 8 else cutoff_short
        close = difflib.get_close_matches(
            missing_field.lower(), available_list, n=1, cutoff=cutoff
        )
        if not close:
            continue
        matched_lower = close[0]
        matched_key = available_lower[matched_lower]
        if len(matched_key) < min_length:
            continue
        # get_close_matches already applied the cutoff, but we need a numeric
        # score to sort candidates across different missing fields. Compute the
        # ratio once against the lowercased strings.
        ratio = difflib.SequenceMatcher(
            None, missing_field.lower(), matched_lower
        ).ratio()
        candidates.append((ratio, missing_field, matched_key))

    # Apply strongest matches first; each unmapped key can only be used once.
    candidates.sort(key=lambda x: x[0], reverse=True)
    used: set[str] = set()
    result: dict[str, str] = {}
    for _ratio, missing_field, matched_key in candidates:
        if matched_key in used:
            continue
        used.add(matched_key)
        result[missing_field] = matched_key
    return result


def _repair_dict_for_model(
    data: dict[str, Any],
    model: type[BaseModel],
    common_aliases: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Recursively repair dict keys to match a Pydantic model's expected fields.

    1. Map exact field names and declared aliases.
    2. For remaining missing fields, try common LLM aliases.
    3. Fuzzy-match unmapped keys to missing fields.
    4. Recurse into nested BaseModel fields and list items.
    5. Clamp numeric values that exceed field constraints.
    6. Wrap/unwrap list ↔ scalar mismatches (LLMs often send a single value
       for a list field, or a single-element list for a scalar field).
    7. Apply type coercion for common LLM type mistakes (string→int/float/bool,
       int↔float).
    8. Strip unmapped keys when the model has ``extra="forbid"``.
    """
    if not isinstance(data, dict):
        return {} if data is None else data  # type: ignore[return-value]
    if not data:
        return {}

    if common_aliases is None:
        common_aliases = _COMMON_FIELD_ALIASES

    known, field_meta, extra_forbid, valid_names = _cached_model_field_info(model)

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
    # Use the precomputed reverse map when the default aliases are in play.
    if common_aliases is _COMMON_FIELD_ALIASES:
        reverse_aliases = _REVERSE_ALIASES
    else:
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
    fuzzy_matches = _fuzzy_match_keys(missing, unmapped_keys)
    for missing_field, matched_key in fuzzy_matches.items():
        mapped[missing_field] = mapped.pop(matched_key)

    # Fourth pass: recurse, clamp, fix list/scalar, coerce, all in one loop.
    for fname, nested, constraints, is_list, scalar_type in field_meta:
        if fname not in mapped:
            continue
        val = mapped[fname]
        if nested is not None:
            # LLMs sometimes serialize nested objects as JSON strings; parse them
            # before recursing so the nested model repair rules apply.
            if isinstance(val, str):
                val = _maybe_parse_json_string(val)
                mapped[fname] = val

            if isinstance(val, dict):
                mapped[fname] = _repair_dict_for_model(val, nested, common_aliases)
            elif isinstance(val, list):
                repaired_items: list[Any] = []
                for item in val:
                    parsed_item = _maybe_parse_json_string(item)
                    if isinstance(parsed_item, dict):
                        repaired_items.append(
                            _repair_dict_for_model(parsed_item, nested, common_aliases)
                        )
                    else:
                        repaired_items.append(item)
                mapped[fname] = repaired_items
        elif constraints is not None:
            clamped = _apply_constraints(val, constraints)
            if clamped is not None:
                val = clamped
                mapped[fname] = val

        # Fix list ↔ scalar mismatches.
        if is_list and not isinstance(val, list):
            val = [val]
            mapped[fname] = val
        elif not is_list and isinstance(val, list) and len(val) == 1:
            val = val[0]
            mapped[fname] = val

        # Type coercion for common LLM type mistakes.
        if scalar_type is not None and val is not None:
            coerced = _coerce_value(val, scalar_type)
            if coerced is not None:
                mapped[fname] = coerced

    # Fifth pass: strip unmapped keys for "extra=forbid" models.
    if extra_forbid:
        mapped = {k: v for k, v in mapped.items() if k in valid_names}

    return mapped


@lru_cache(maxsize=512)
def _resolve_scalar_type(annotation: Any) -> type | None:
    """Resolve a type annotation to a simple scalar type (str, int, float, bool).

    Handles Optional[X], Union[X, None], X | None, and plain X.
    Returns None if the annotation is not a simple scalar.
    """
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)

    if origin is None:
        # Plain type like str, int, float, bool
        if annotation in (str, int, float, bool):
            return annotation
        return None

    # Handle Union / Optional types
    origin_name = getattr(origin, "__name__", "")
    if origin_name in ("Union", "Optional"):
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1 and non_none[0] in (str, int, float, bool):
            return non_none[0]

    return None


def _coerce_value(value: Any, target_type: type) -> Any | None:
    """Attempt to coerce a value to *target_type*.  Returns None if coercion
    is not possible or would be lossy."""
    if isinstance(value, bool):
        # bool is a subclass of int — don't coerce it unless target is int/float
        if target_type is int:
            return int(value)
        if target_type is float:
            return float(value)
        if target_type is str:
            return str(value).lower()
        return None

    if target_type is str and not isinstance(value, str):
        if isinstance(value, (int, float)):
            return str(value)
        return None

    if target_type is int and not isinstance(value, int):
        if isinstance(value, str):
            try:
                return int(value)
            except (ValueError, TypeError):
                pass
        if isinstance(value, float) and value == int(value):
            # Only coerce if no precision loss
            return int(value)
        return None

    if target_type is float and not isinstance(value, float):
        if isinstance(value, str):
            try:
                return float(value)
            except (ValueError, TypeError):
                pass
        if isinstance(value, int):
            return float(value)
        return None

    if target_type is bool and not isinstance(value, bool):
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("true", "1", "yes", "on"):
                return True
            if lowered in ("false", "0", "no", "off", ""):
                return False
        if isinstance(value, int):
            return bool(value)
        return None

    return None


# Known lowercase Python/JSONSchema builtin type names that Pydantic may
# include in error locations as union-branch discriminators.  These are NOT
# real field names and must be filtered out of error paths.
_BUILTIN_TYPE_NAMES: frozenset[str] = frozenset({
    "str", "int", "float", "bool", "list", "dict", "tuple", "set",
    "frozenset", "bytes", "None", "none", "null", "object", "array",
    "number", "string", "boolean", "integer",
})


def _clean_error_loc(loc: tuple[str | int, ...]) -> str:
    """Remove union-discriminator noise from a Pydantic error location.

    Pydantic v2 includes union-branch names (e.g. ``Edit``, ``list[Edit]``,
    ``str``, ``int``) in ``loc`` tuples.  This makes the path hard for an
    LLM to read and often points to a branch that was *not* intended.  We
    keep only the real field / index segments.
    """
    cleaned: list[str] = []
    for part in loc:
        s = str(part)
        # Union branch names are capitalised model names or type expressions
        # like ``list[Edit]``.  They may also start with an underscore in
        # private helper classes (e.g. ``_Edit``).  Real field names in this
        # codebase are snake_case; integers are list indices.
        stripped = s.lstrip("_")
        if (stripped and stripped[:1].isupper()) or "[" in s:
            continue
        # Filter lowercase builtin type names that Pydantic uses as
        # union-branch discriminators (e.g. ``"value", "str"``).
        if stripped in _BUILTIN_TYPE_NAMES:
            continue
        cleaned.append(s)
    return ".".join(cleaned) if cleaned else ".".join(str(p) for p in loc)


def _format_pydantic_validation_error(
    error: pydantic.ValidationError,
    tool_name: str,
    params_schema: dict[str, Any] | None = None,
) -> str:
    """Format a pydantic ``ValidationError`` into a concise, LLM-friendly message."""
    errors_list = error.errors()
    lines: list[str] = []
    lines.append(
        f"Invalid arguments for tool `{tool_name}` — "
        f"{len(errors_list)} validation error(s):"
    )
    lines.append("")

    for i, err in enumerate(errors_list, 1):
        loc = _clean_error_loc(err["loc"])
        msg = err["msg"]
        err_type = err.get("type", "")

        lines.append(f"{i}. `{loc}` — {msg}")

        inp = err.get("input")
        if inp is not None:
            inp_str = orjson.dumps(inp).decode()
            if len(inp_str) > 300:
                inp_str = inp_str[:300] + " ..."
            lines.append(f"  Received: {inp_str}")

        # Actionable hints for common error types
        ctx = err.get("ctx", {})
        match err_type:
            case "missing":
                lines.append("  Hint: this field is required but was not provided.")
            case "list_type":
                lines.append("  Hint: this field should be an array (list).")
            case "dict_type":
                lines.append("  Hint: this field should be an object (dict).")
            case "string_type":
                lines.append("  Hint: this field should be a string.")
            case "int_type":
                lines.append("  Hint: this field should be an integer.")
            case "bool_type":
                lines.append("  Hint: this field should be a boolean.")
            case "float_type":
                lines.append("  Hint: this field should be a number.")
            case "literal_error":
                expected = ctx.get("expected", "")
                if expected:
                    lines.append(f"  Hint: the value must be one of: {expected}")
                else:
                    lines.append("  Hint: the value must be one of the allowed options.")
            case "extra_forbidden":
                lines.append(
                    "  Hint: this field is not recognized — remove it or check the schema."
                )
            case "url_parsing":
                lines.append("  Hint: the value must be a valid URL.")
            case "too_short":
                lines.append("  Hint: the value is too short.")
            case "too_long":
                lines.append("  Hint: the value is too long.")
            case "enum":
                expected = ctx.get("expected", "")
                if expected:
                    lines.append(f"  Hint: this value is not one of the allowed options: {expected}")
                else:
                    lines.append("  Hint: this value is not one of the allowed options.")
            case "value_error":
                lines.append("  Hint: this value is invalid — check field constraints.")
            case "union_tag_not_found":
                lines.append("  Hint: a required discriminator field is missing.")
            case "union_tag_invalid":
                tag = ctx.get("discriminator", "")
                if tag:
                    lines.append(f"  Hint: the discriminator value for '{tag}' is not recognized.")
                else:
                    lines.append("  Hint: the discriminator value is not recognized.")
            case "model_type":
                lines.append("  Hint: this field should be a JSON object (dict).")
            case "pattern_mismatch":
                pat = ctx.get("pattern", "")
                if pat:
                    lines.append(f"  Hint: the string does not match the required pattern '{pat}'.")
                else:
                    lines.append("  Hint: the string does not match the required pattern.")
            case "multiple_of":
                mult = ctx.get("multiple_of", "?")
                lines.append(f"  Hint: the number must be a multiple of {mult}.")
            case "finite_number":
                lines.append("  Hint: the value must be a finite number (not NaN or Infinity).")
            case "set_type":
                lines.append("  Hint: this field should be a set/array.")
            case "tuple_type":
                lines.append("  Hint: this field should be a tuple/array.")
            case "date_type":
                lines.append("  Hint: this field should be a valid date.")
            case "datetime_type":
                lines.append("  Hint: this field should be a valid datetime.")
            case "time_type":
                lines.append("  Hint: this field should be a valid time.")
            case "json_type":
                lines.append("  Hint: this field should be a valid JSON string.")
            case "json_invalid":
                lines.append("  Hint: the JSON string is invalid.")
            case "url_scheme":
                allowed = ctx.get("allowed_schemes", "")
                if allowed:
                    lines.append(f"  Hint: the URL scheme must be one of: {allowed}")
                else:
                    lines.append("  Hint: the URL scheme is not allowed.")
            case "none_required":
                lines.append("  Hint: this field must be null/None.")
            case "invalid_key":
                lines.append("  Hint: one or more keys in the object are invalid.")
            case "get_attribute_error":
                lines.append("  Hint: an attribute access error occurred.")
            case "bytes_too_long":
                lines.append("  Hint: the byte string is too long.")
            case "bytes_too_short":
                lines.append("  Hint: the byte string is too short.")
            case "decimal_max_digits":
                lines.append("  Hint: the number has too many digits.")
            case "decimal_max_places":
                lines.append("  Hint: the number has too many decimal places.")
            case "recursion_loop":
                lines.append("  Hint: the data structure contains a recursion loop.")
            case _ if err_type.startswith("greater_than"):
                lines.append("  Hint: the value is too small.")
            case _ if err_type.startswith("less_than"):
                lines.append("  Hint: the value is too large.")
            case _:
                # Fallback: produce a generic hint for unknown error types
                lines.append(f"  Hint: check the field constraints (error type: {err_type}).")

    # Only include the full JSON schema when the errors suggest structural
    # confusion (extra fields, discriminator issues) or when there are many
    # errors (>2), to keep simple error messages concise.
    if params_schema is not None:
        structural_error_types = frozenset({
            "extra_forbidden", "union_tag_not_found", "union_tag_invalid",
        })
        has_structural_error = any(
            e.get("type", "") in structural_error_types
            for e in errors_list
        )
        if has_structural_error or len(errors_list) > 2:
            lines.append("")
            lines.append("Expected JSON schema:")
            schema_str = orjson.dumps(params_schema, option=orjson.OPT_INDENT_2).decode()
            lines.append(schema_str)

    return "\n".join(lines)


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

        if arguments is None or arguments == "":
            return ToolValidateError(
                f"Invalid arguments for tool `{self.name}`: "
                "expected a JSON object, got an empty value."
            )
        if not isinstance(arguments, dict):
            return ToolValidateError(
                f"Invalid arguments for tool `{self.name}`: "
                f"expected a JSON object, got `{type(arguments).__name__}`."
            )

        try:
            params = self.params.model_validate(arguments)
        except pydantic.ValidationError as e:
            # Attempt to repair common LLM field-name mismatches and re-validate.
            repair_attempted = False
            if isinstance(arguments, dict):
                repaired = _repair_dict_for_model(arguments, self.params, self.field_aliases)
                if repaired != arguments:
                    repair_attempted = True
                    try:
                        params = self.params.model_validate(repaired)
                    except pydantic.ValidationError:
                        pass  # fall through to return the original error
                    else:
                        return await self.__call__(params)
            message = _format_pydantic_validation_error(e, self.name, self.base.parameters)
            if repair_attempted:
                message += (
                    "\n\nNote: automatic argument repair was attempted "
                    "but could not resolve all issues. Please review the "
                    "schema and correct the arguments manually."
                )
            return ToolValidateError(message)

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
