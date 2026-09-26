"""Todo list tracking tool."""

from __future__ import annotations

import copy
import regex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal, cast, override

import orjson
import rapidfuzz
from kosong.tooling import (
    FIELD_ALIASES_TODO_LIST,
    _COMMON_FIELD_ALIASES,
    CallableTool2,
    ToolError,
    ToolReturnValue,
    alias_note,
)
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.json_schema import GetJsonSchemaHandler, JsonSchemaValue
from pydantic_core import CoreSchema

from kimi_cli import logger
from kimi_cli.session_state import TodoItemState, TodoStatus
from kimi_cli.soul.agent import Runtime
from kimi_cli.tools.display import TodoDisplayBlock, TodoDisplayItem
from kimi_cli.tools.utils import repair_json_string

_TODOLIST_DESCRIPTION = (
    "Read or write the todo plan — one tool, one item shape, every operation.\n\n"
    "Item shape (all modes): `{title, status?, notes?, children?}` plus the edit keys "
    "`parent`, `rename_to`, `complete`. `title` is the one short imperative line that "
    "identifies the item and the only required key; `notes` is optional detail (an item "
    "of only `notes` has no identity).\n\n"
    "Dispatch:\n"
    "- `todos` omitted → read the current tree.\n"
    "- mode='merge' (default) → upsert each item: an existing title patches it in place "
    "(omitted fields keep their value), an unknown title creates it. `parent` / "
    "top-level `scope` pick the sub-tree ('' = root).\n"
    "- mode='replace' → the list IS the tree (children included); needs all existing "
    "todos done unless force=True.\n"
    "- mode='clear' → empty the tree (all-done guard unless force=True).\n\n"
    "Near-duplicate titles are treated as the same task, not a new one: an incoming "
    "title that differs from an existing one only in numbering, punctuation, case or "
    "word order hits the conflict policy — on_conflict='error' (default) refuses the "
    "call and names the existing title plus the exact payload to send instead; "
    "'reuse' patches that item; 'append' really adds a second one.\n\n"
    "Invariants: exactly one item in_progress (auto_fix=True demotes earlier ones, "
    "keeping the last listed); done items never move back to pending/in_progress "
    "unless force=True; done items dropped by replace/clear are archived."
)

def _truncate_prompt(text: str, max_len: int = 200) -> str:
    """Truncate long text, keeping head and tail.

    When ``len(text) > max_len``, keeps the first 100 chars and the last
    100 chars with ``...`` in between. Otherwise returns the text as-is.
    """
    if len(text) > max_len:
        return text[:100] + "..." + text[-100:]
    return text

_ALL_DONE_REMINDER = (
    "All todos are done. "
    "Please review the requirements again to ensure nothing is left unfinished."
)
"""Default reminder shown when all todos are done."""

# Hard limits for harness safety.
_MAX_TODOS = 4096
# Maximum number of archived todos kept in state (oldest are dropped first).
_MAX_ARCHIVED_TODOS = 500
# Maximum number of items printed by read mode before truncating.
_MAX_READ_ITEMS = 100

# ── Cross-tool reference hints (anti-hallucination core) ────────────────────
# Compact one-line hints appended to tool output. Success outputs carry a
# ``Next: Todo<X> ...`` hint; error outputs carry a corrective ``Hint: ...``
# sentence naming the right tool(s). Kept generic (≤1 sentence) on purpose.

def _hint_next(text: str) -> str:
    """Render a one-line 'Next:' hint block appended to success output."""
    return "\nNext: " + text

def _hint_error(text: str) -> str:
    """Render a one-line corrective hint block appended to error output."""
    return "\nHint: " + text

_TODOLIST_SUCCESS_HINT = (
    'todo_list with no todos to read the tree; parent="<title>" to add a child under an item.'
)

# Mode map — only canonical values accepted; every retired spelling of an
# existing intent is folded onto its canonical mode instead of erroring.
_MODE_MAP: dict[str, Literal["merge", "replace", "clear"]] = {
    "merge": "merge",
    "replace": "replace",
    "clear": "clear",
    # merge synonyms (the old append/patch/update spellings).
    "append": "merge",
    "add": "merge",
    "patch": "merge",
    "update": "merge",
    "upsert": "merge",
    "edit": "merge",
    "edits": "merge",
    # replace synonyms.
    "overwrite": "replace",
    "override": "replace",
    "set": "replace",
    "write": "replace",
    "full": "replace",
    "whole": "replace",
    # clear synonyms.
    "delete": "clear",
    "reset": "clear",
    "empty": "clear",
    "remove": "clear",
}

# Status map — only canonical values accepted; `completed` (report spelling)
# is accepted as an alias of the internal `done` value.
_STATUS_MAP: dict[str, TodoStatus] = {
    "pending": "pending",
    "in_progress": "in_progress",
    "done": "done",
    "completed": "done",
}

def _canonical_status(v: Any) -> TodoStatus:
    """Normalize a status value to its canonical form."""
    if not isinstance(v, str):
        raise ValueError(
            f"Invalid status '{v}'. Must be one of: pending, in_progress, done (or completed)."
        )
    normalized = v.strip().lower().replace("-", "_")
    canonical = _STATUS_MAP.get(normalized)
    if canonical is None:
        raise ValueError(
            f"Invalid status '{v}'. Must be one of: pending, in_progress, done (or completed)."
        )
    return canonical

@dataclass(frozen=True)
class _FuzzyResult:
    """Typed wrapper for rapidfuzz>=3 ``(choice, score, index)`` match tuples."""

    choice: str
    score: float
    index: int

# ── Item title contract ──────────────────────────────────────────────────────
# The item title is load-bearing: `merge` upserts items by it, edits locate
# items by it, and the plan is re-injected into context from it. Models
# drop it when the *optional* payload field (`notes`) is described more loudly
# than the required one (observed in real traffic as items shaped
# ``{notes, status}`` and an error that only said "Field required"). The
# contract is therefore stated three times: in the field descriptions (P1), by
# a non-blocking recovery from `notes`, and by an error that names the
# field, the accepted spellings and the exact item path (P0).
_TITLE_FIELD: str = "title"
_TITLE_ALIASES: tuple[str, ...] = ("title", "content", "task", "todo", "item", "name")
_TITLE_HINT: str = "the item title: one short imperative line"
# Short forms used in the nested `children` leaf copy, where the long contract
# text would only repeat itself and burn tokens.
_TITLE_LEAF_DESCRIPTION: str = "Sub-todo title: one short imperative line (required)."
_NOTES_LEAF_DESCRIPTION: str = "Optional detail for this sub-todo; the title is the identity."
_UPDATE_TITLE_ALIASES = _TITLE_ALIASES  # kept for callers that name the lookup key
_UPDATE_TITLE_HINT: str = "the title of the todo to update or create"
_MAX_DERIVED_TITLE_CHARS: int = 80
# How many invalid items one error message names before summarising.
_MAX_REPORTED_PROBLEMS: int = 8

# ── Conflict (near-duplicate title) policy ───────────────────────────────────
# `mode='merge'` creates an item when its title matches nothing exactly. That is
# also how a re-declared plan turns into duplicates: the model re-phrases an
# existing title ("1. README lines 55-63" vs "1. README: lines 55-63") and the
# old append path only *warned*. These knobs decide what happens instead.
_CONFLICT_MAP: dict[str, Literal["error", "reuse", "append"]] = {
    "error": "error",
    "refuse": "error",
    "fail": "error",
    "strict": "error",
    "reuse": "reuse",
    "match": "reuse",
    "merge": "reuse",
    "patch": "reuse",
    "update": "reuse",
    "append": "append",
    "create": "append",
    "add": "append",
    "allow": "append",
    "warn": "append",
}

# A title whose *words* are the same as an existing one (numbering, punctuation,
# casing or word order differ) is the same task re-declared, not a new task.
# This is a set comparison, not a similarity threshold: measured on real
# duplicate traffic it catches every re-declaration observed, while a fuzzy
# threshold also caught legitimate siblings ("Task 10" vs "Task 0", "Add tests
# for contract" vs "…for conflict"). Typos therefore stay in the advisory band
# (`_FUZZY_WARNING_CUTOFF`), where they are warned about and still created.

# Leading list markers ("1.", "2)", "- ", "* ", "3: ") carry no meaning, so they
# are stripped before comparing titles.
_LIST_MARKER_RE = regex.compile(r"^\s*(?:\d+[.)\u3001:\uff1a]|[-*\u2022]+)\s*")

# Keys of the retired single top-level edit form (`title=…, status=…`).
_TITLE_KEYS: tuple[str, ...] = ("title", "content", "task", "todo", "item", "name")
_EDIT_FIELD_KEYS: tuple[str, ...] = ("status", "notes", "rename_to", "complete", "parent")

# Parameter names that still mean "the item list" (retired batch-form aliases).
_TODO_LIST_KEYS: tuple[str, ...] = (
    "todos",
    "updates",
    "edits",
    "items",
    "changes",
    "tasks",
    "operations",
    "actions",
    "list",
)

_SUMMARY_DETAIL_RE = regex.compile(r" \([^()]*\)\.$")


def _terse_summary(summary: str) -> str:
    """`Updated "A" (status=done).` -> `Updated "A".` for the compact message line."""
    return _SUMMARY_DETAIL_RE.sub(".", summary)


def normalize_title(text: str) -> str:
    """Reduce a title to the identity used for conflict comparison.

    Drops the leading list marker, case, punctuation and repeated whitespace, so
    "1. README: lines 55-63" and "README LINES 55,63" compare on their words.
    """
    stripped = _LIST_MARKER_RE.sub("", text.lower())
    return " ".join(regex.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", stripped).split())


def _looks_like_json_opener(text: str) -> bool:
    """True when a string starts like a JSON object/array (so it is not a title)."""
    return bool(text) and text[0] in ("{", "[")


def _cap_title(text: str) -> str:
    """Trim a recovered title to a single short line."""
    return text.strip()[:_MAX_DERIVED_TITLE_CHARS]


def _first_text_line(value: Any) -> str:
    """First meaningful line of a text value, for use as a recovered title."""
    if not isinstance(value, str):
        return ""
    for raw in value.splitlines():
        line = raw.strip().lstrip("-*•·+").strip()
        if line:
            return _cap_title(line)
    return ""


def _received_keys(sent: Any) -> str:
    """Render the keys the model actually sent for an item."""
    if isinstance(sent, dict):
        keys: list[str] = sorted(str(key) for key in cast(dict[str, Any], sent))
        return ", ".join(keys) or "(none)"
    if isinstance(sent, str):
        return f"a bare string (no keys): {sent[:40]!r}"
    return type(sent).__name__


def _sent_at(sent: Any, path: tuple[Any, ...]) -> Any:
    """Walk the sent payload along a Pydantic location to the failing sub-item.

    Nested problems must echo the keys of the *offending* item, not of the root
    item that contains it — otherwise "Keys received: [children, content,
    status]" points at a node that is perfectly valid.
    """
    cur: Any = sent
    for seg in path:
        if isinstance(cur, dict):
            cur = cast(dict[str, Any], cur).get(seg)
        elif isinstance(cur, list):
            seq = cast(list[Any], cur)
            cur = seq[seg] if isinstance(seg, int) and 0 <= seg < len(seq) else None
        else:
            return None
    return cur


def _problem_path(loc: tuple[Any, ...]) -> str:
    """Render a Pydantic error location as ``children[1].title``."""
    path = ""
    for seg in loc:
        if isinstance(seg, int):
            path += f"[{seg}]"
        elif path:
            path += f".{seg}"
        else:
            path = str(seg)
    return path


def _describe_item_problems(
    exc: ValidationError,
    *,
    where: str,
    sent: Any,
    title_field: str,
    title_aliases: tuple[str, ...],
    title_hint: str,
) -> list[str]:
    """Turn a nested-model ``ValidationError`` into path- and field-named lines.

    This replaces the previous ``errors[0].msg`` shortcut, which returned a bare
    ``"Field required"`` with no field name, no nested path (a missing title on
    ``children[1]`` was blamed on the root item) and only the first index, so a
    list with three bad items cost three round trips and never told the model
    what to type.
    """
    lines: list[str] = []
    for err in exc.errors():
        loc: tuple[Any, ...] = tuple(err.get("loc", ()))
        field = loc[-1] if loc and isinstance(loc[-1], str) else ""
        sub_loc = loc[:-1] if field else loc
        sub = _problem_path(sub_loc)
        place = f"{where}.{sub}" if sub else where
        item_sent = _sent_at(sent, sub_loc)
        if not isinstance(item_sent, (dict, str)):
            item_sent = sent
        keys = f"Keys received for this item: [{_received_keys(item_sent)}]."
        etype = str(err.get("type", ""))
        msg = str(err.get("msg", "")).rstrip(".")
        if field in title_aliases:
            if etype == "missing":
                lines.append(
                    f"{place}: missing required field '{title_field}' ({title_hint}). "
                    f"{alias_note(*title_aliases, word=False)} {keys}"
                )
            else:
                lines.append(
                    f"{place}: field '{title_field}' is invalid ({title_hint}): {msg}. {keys}"
                )
            continue
        if etype == "missing":
            lines.append(f"{place}: missing required field '{field}'. {keys}")
        elif etype == "extra_forbidden":
            lines.append(f"{place}: unexpected field '{field}' (not part of this item's shape).")
        else:
            lines.append(f"{place}: {msg}. {keys}" if "=" not in msg else f"{place}: {msg}")
    return lines


def _format_item_problems(problems: list[str], *, bad: int, total: int, noun: str) -> str:
    """Join per-item problems into one message, naming every bad index.

    All problems are reported in a single round trip: reporting only the first
    made a list of N bad items cost N turns of the model fixing one index at a
    time.
    """
    shown = problems[:_MAX_REPORTED_PROBLEMS]
    # Partitive grammar: the noun follows the total, the verb follows the count
    # of invalid ones ("1 of 2 todo items is invalid", "3 of 4 todo items are").
    label = noun if total == 1 else f"{noun}s"
    verb = "is" if bad == 1 else "are"
    head = f"{bad} of {total} {label} {verb} invalid:"
    text = head + "\n" + "\n".join(f"- {line}" for line in shown)
    hidden = len(problems) - len(shown)
    if hidden > 0:
        text += f"\n- … and {hidden} more"
    return text


def _derived_title_warnings(todos: list[Todo]) -> list[str]:
    """Warn (never silently accept) about titles recovered from ``notes``.

    A derived title is a best-effort rescue of a call the model should have made
    correctly, so each one produces a non-blocking warning naming the canonical
    key and the accepted spellings — the message the validation error used to
    fail to deliver.
    """
    warnings: list[str] = []

    def walk(items: list[Todo], path: str) -> None:
        for idx, item in enumerate(items):
            where = f"{path}[{idx}]"
            origin: str | None = getattr(item, "_title_derived_from", None)
            if origin:
                warnings.append(
                    f'Warning: {where} had no title, so `content`="{item.title}" was '
                    "derived from its `notes`. Give every todo a short `content` "
                    f"title; {alias_note(*_TITLE_ALIASES, word=False)}"
                )
            walk(item.children, f"{where}.children")

    walk(todos, "todos")
    return warnings


def _collapse_optional_array(schema: JsonSchemaValue, field: str) -> JsonSchemaValue:
    """Rewrite an optional array parameter into a single ``{"type": "array"}``.

    ``list[Model] | Model | None`` renders as a three-branch ``anyOf`` whose item
    shape is duplicated for the array branch, the single-item branch and every
    nested copy. Constrained decoding on many providers does not enforce
    ``required`` inside a nested ``anyOf`` branch, so the duplication costs
    attention while making the required fields *look* optional. Only the
    advertised schema is collapsed; the validators still accept a single object,
    a bare title string or a JSON string of either.
    """
    raw_props = schema.get("properties")
    if not isinstance(raw_props, dict):
        return schema
    properties: dict[str, Any] = cast(dict[str, Any], raw_props)
    raw_prop = properties.get(field)
    if not isinstance(raw_prop, dict):
        return schema
    prop: dict[str, Any] = cast(dict[str, Any], raw_prop)
    raw_branches = prop.get("anyOf")
    if not isinstance(raw_branches, list):
        return schema
    branches: list[Any] = cast(list[Any], raw_branches)
    array_branch: dict[str, Any] | None = next(
        (
            cast(dict[str, Any], b)
            for b in branches
            if isinstance(b, dict) and cast(dict[str, Any], b).get("type") == "array"
        ),
        None,
    )
    if array_branch is None:
        return schema
    flattened: dict[str, Any] = dict(array_branch)
    if "description" in prop:
        flattened["description"] = prop["description"]
    properties[field] = flattened
    return schema


class Todo(BaseModel):
    """One todo item.

    The single item shape for the one todo tool: the same object expresses a
    whole-tree write (``mode='replace'``), an upsert (``mode='merge'``) and a
    targeted edit (``parent`` / ``rename_to`` / ``complete``). Fields that only
    make sense for an edit are ignored by a whole-tree write.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    # Set when a missing title was recovered from `notes` (see
    # ``_normalize_item_shape``) so the write path can warn about it instead of
    # silently guessing. Private: never part of validation, dumps or schema.
    _title_derived_from: str | None = PrivateAttr(default=None)

    title: str =         Field(
        validation_alias=AliasChoices(*_TITLE_ALIASES),
        description=(
            "Required. The task title: one short imperative line, and the item's "
            "identity — mode='merge' matches items by it and `parent`/`scope` look "
            "items up by it, so it must always be sent. "
            + alias_note(*_TITLE_ALIASES, word=False)
        ),
        min_length=1,
        max_length=65536,
    )
    status: TodoStatus =         Field(
        default="pending",
        description=(
            "One of: pending, in_progress, done (or completed). Omit to keep an "
            "existing item's status; created items default to pending."
        ),
    )
    notes: str | None =         Field(
        default=None,
        description=(
            "Optional supporting detail (evidence, file paths, findings). Not the "
            "title — an item with only `notes` has no identity, so always send "
            '`title` too. Omit it (or send "") to keep the current notes.'
        ),
        max_length=65536,
    )
    # Sub todos (children). Leave empty for a leaf. Pydantic recurses
    # automatically; all field validators apply to children too.
    children: list[Todo] =         Field(
        default_factory=list,
        description=(
            "Sub-todos of this item; same shape, any depth. Leave empty for a leaf. "
            "A bare title string is accepted and means a pending item."
        ),
    )
    # ── Edit-only keys (honoured by mode='merge', rejected by 'replace'/'clear') ──
    parent: str | None =         Field(
        default=None,
        description=(
            'Scope this item to the children of the named todo (both its lookup and '
            'its creation). "" = root scope. Overrides the top-level `scope`.'
        ),
    )
    rename_to: str | None =         Field(
        default=None,
        description=(
            "Rename the matched item to this title instead of editing a field. "
            "Renaming onto an existing title is rejected."
        ),
    )
    complete: bool =         Field(
        default=False,
        description=(
            "Mark this item and its whole sub-tree done in one call. Not combined "
            "with an explicit pending/in_progress status or with `children`."
        ),
    )
    fuzzy: bool =         Field(
        default=True,
        description="Per-item override of the top-level `fuzzy`.",
    )
    force: bool =         Field(
        default=False,
        description="Per-item override of the top-level `force`.",
    )

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        json_schema = handler(core_schema)
        cls._truncate_children_schema(json_schema)
        return json_schema

    @staticmethod
    def _truncate_children_schema(json_schema: JsonSchemaValue) -> None:
        """Break JSON-Schema recursion on ``children`` and slim the leaf copy.

        Some providers (e.g. Moonshot) reject tool parameter schemas that
        contain recursive ``$ref`` cycles (HTTP 400
        ``json_schema_refs_recursive``). Runtime validation keeps arbitrary-
        depth recursion; only the *emitted schema* is bounded: child items are
        rendered as a leaf copy of this model (``title``/``status``/``notes``
        and no nested ``children`` property), so no ``$ref`` cycle survives
        ``deref_json_schema``. The edit-only keys are dropped from the leaf too
        — they address an item from outside, and inside a written tree they
        would only add tokens and invite nonsense.
        """
        properties = json_schema.get("properties")
        if not isinstance(properties, dict):
            return
        children = properties.get("children")
        if not isinstance(children, dict):
            return
        leaf = copy.deepcopy(json_schema)
        leaf_properties = leaf.get("properties")
        if isinstance(leaf_properties, dict):
            for key in ("children", "parent", "rename_to", "complete", "fuzzy", "force"):
                leaf_properties.pop(key, None)
            notes = leaf_properties.get("notes")
            if isinstance(notes, dict):
                notes["description"] = _NOTES_LEAF_DESCRIPTION
            title = leaf_properties.get("title")
            if isinstance(title, dict):
                title["description"] = _TITLE_LEAF_DESCRIPTION
        leaf_required = leaf.get("required")
        if isinstance(leaf_required, list):
            leaf["required"] = [r for r in leaf_required if r != "children"]
        children["items"] = leaf

    @model_validator(mode="wrap")
    @classmethod
    def _normalize_item_shape(cls, data: Any, handler: Callable[[Any], Any]) -> Any:
        """Accept synonyms, recover a missing title, and never touch the caller's dict.

        Three fixes for the observed ``{notes, status}``-with-no-title failure:

        * Copy-on-write. The previous before-validator did ``data.pop()`` on the
          model's *own* arguments, so the echoed ``Received:`` payload no longer
          showed what the model had sent (a title written under ``description``
          came back looking like correct ``notes``) and the post-failure repair
          pass could no longer see the original key.
        * Lenient shapes: a bare title string, and a missing ``status``
          (``status`` keeps its default, which — unlike an injected dict key —
          stays out of ``model_fields_set`` so an edit can tell "unset" from
          "set to pending").
        * Last-resort recovery: with no title at all, derive one from the first
          line of ``notes`` and record it, so the write path warns about it
          instead of silently guessing or hard-failing the whole call.
        """
        patched: Any = data
        derived: str | None = None
        if isinstance(data, str):
            text = _cap_title(data)
            if text:
                patched = {_TITLE_FIELD: text}
        elif isinstance(data, dict):
            item: dict[str, Any] = dict(cast(dict[str, Any], data))
            if "description" in item and "notes" not in item:
                item["notes"] = item.pop("description")
            title_key = next((key for key in _TITLE_ALIASES if key in item), None)
            if title_key is None or not str(item[title_key]).strip():
                recovered = _first_text_line(item.get("notes"))
                if recovered:
                    item[_TITLE_FIELD] = recovered
                    derived = recovered
            patched = item
        instance = handler(patched)
        if derived is not None and isinstance(instance, Todo):
            instance._title_derived_from = derived
        return instance

    @field_validator("status", mode="before")
    @classmethod
    def _validate_status(cls, v: Any) -> str:
        return _canonical_status(v)

    @field_validator("notes", mode="before")
    @classmethod
    def _validate_notes(cls, v: Any) -> str | None:
        if v is None:
            return None
        stripped = str(v).strip()
        return stripped if stripped else None

    @field_validator("title")
    @classmethod
    def _validate_title(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError(
                "expected a short imperative title, but the text is empty or whitespace only"
            )
        return stripped

class Params(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        """Emit `todos` as one plain array (see ``_collapse_optional_array``)."""
        return _collapse_optional_array(handler(core_schema), "todos")

    todos: list[Todo] | None =         Field(
        default=None,
        validation_alias=AliasChoices(
            "todos",
            "updates",
            "edits",
            "items",
            "changes",
            "tasks",
            "operations",
            "actions",
            "list"
        ),
        description=(
            "The items to write; omit to READ the tree. In the default mode='merge' "
            "this is an upsert batch — send only the items you mean to touch, each "
            "with `title` plus any of status / notes / children / rename_to / parent "
            "/ complete: an existing title is patched, an unknown one is created. In "
            "mode='replace' send the COMPLETE tree instead. A single object, a bare "
            'title string or a JSON string of those forms also work. '
            + alias_note("todos", "items", word=False)
        ),
    )
    mode: Literal["merge", "replace", "clear"] =         Field(
        default="merge",
        description=(
            "'merge' (default) upserts the given items; 'replace' makes the given "
            "list the whole tree and needs every existing todo done unless "
            'force=True; "clear" empties the tree (same guard).'
        ),
    )
    scope: str | None =         Field(
        default=None,
        validation_alias=AliasChoices("scope", "parent"),
        description=(
            'Restrict this call to the children of the named todo; "" means the '
            "root. An item's own `parent` wins."
        ),
    )
    on_conflict: Literal["error", "reuse", "append"] =         Field(
        default="error",
        description=(
            "What to do when an incoming title is a near-duplicate of an existing "
            "one (same words, different numbering / punctuation / case): 'error' "
            "(default) refuses the call and names the existing title plus the exact "
            "payload to send instead; 'reuse' patches that item; 'append' really "
            "adds a second one."
        ),
    )
    fuzzy: bool =         Field(
        default=True,
        description=(
            "When True (default) a title that misses exactly may still match the "
            "nearest existing todo; False requires exact titles."
        ),
    )
    force: bool =         Field(
        default=False,
        description=(
            "Bypass the guards: the all-done requirement of replace/clear, "
            "reopening a done item, renaming onto one, and the single-in_progress "
            "and regression checks."
        ),
    )
    auto_fix: bool =         Field(
        default=True,
        description=(
            "When True (default) and several items are in_progress, the last listed "
            "one is kept and earlier ones are marked done; False errors instead."
        ),
    )

    @field_validator("mode", mode="before")
    @classmethod
    def _validate_mode(cls, v: Any) -> str:
        if not isinstance(v, str):
            raise ValueError(
                "Invalid mode. Must be 'merge', 'replace', or 'clear'."
            )
        normalized = v.strip().lower().replace("-", "_")
        canonical = _MODE_MAP.get(normalized)
        if canonical is None:
            raise ValueError(
                f"Invalid mode '{v}'. Must be 'merge', 'replace', or 'clear'."
            )
        return canonical

    @field_validator("on_conflict", mode="before")
    @classmethod
    def _validate_on_conflict(cls, v: Any) -> str:
        if not isinstance(v, str):
            raise ValueError("Invalid on_conflict. Must be 'error', 'reuse', or 'append'.")
        normalized = v.strip().lower().replace("-", "_")
        canonical = _CONFLICT_MAP.get(normalized)
        if canonical is None:
            raise ValueError(
                f"Invalid on_conflict '{v}'. Must be 'error', 'reuse', or 'append'."
            )
        return canonical

    @field_validator("scope")
    @classmethod
    def _validate_scope(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return v.strip()

    @model_validator(mode="before")
    @classmethod
    def _translate_legacy_shape(cls, data: Any) -> Any:
        """Fold retired argument shapes onto ``todos`` + ``mode``.

        Two legacy forms are still accepted (the schema does not advertise them):

        * ``mode='force_overwrite'``/``'force_replace'`` → replace + force.
        * the single top-level edit ``title=…, status=…`` (no list) → one
          merge item, so a one-line edit never needs a second tool.

        Runs before field validation and copies instead of mutating, so the
        echoed ``Received:`` payload keeps showing what the model actually sent.
        """
        if not isinstance(data, dict):
            return data
        args = cast(dict[str, Any], data)
        result: Any = data

        mode = args.get("mode")
        if isinstance(mode, str):
            norm = mode.strip().lower().replace("-", "_").replace(" ", "_")
            if norm in (
                "force_overwrite",
                "force_override",
                "force_replace",
                "force",
                "forcewrite",
                "forceoverride",
                "forcereplace",
            ):
                result = {**(result if result is not data else args), "mode": "replace", "force": True}

        has_list = any(key in args for key in _TODO_LIST_KEYS)
        title_key = next((key for key in _TITLE_KEYS if key in args), None)
        patch_key = next(
            (key for key in ("rename_to", "complete") if key in args),
            None,
        )
        if (title_key or patch_key) and not has_list:
            item: dict[str, Any] = {}
            if title_key is not None:
                item[title_key] = args[title_key]
            for key in _EDIT_FIELD_KEYS:
                if key in args:
                    item[key] = args[key]
            consumed = set(_TITLE_KEYS) | set(_EDIT_FIELD_KEYS)
            merged = {**(result if isinstance(result, dict) else args)}
            for key in consumed:
                merged.pop(key, None)  # the retired keys now live inside the item
            merged["todos"] = [item]
            result = merged
        return result

    @field_validator("todos", mode="before")
    @classmethod
    def _validate_todos(cls, v: Any) -> list[Todo] | None:
        if v is None:
            return None
        if isinstance(v, Todo):
            return [v]
        if isinstance(v, str):
            parsed = repair_json_string(v)
            if parsed is None:
                # A bare title in place of the list (e.g. todos="Fix the bug"):
                # one item, mirroring the toolset's item wrapping. Not a list of
                # words — the whole string is the title.
                stripped = v.strip()
                if not stripped or _looks_like_json_opener(stripped):
                    raise ValueError(
                        "todos must be a list of todos, a single todo dict/object, a bare "
                        "title string, or None"
                    )
                return [Todo.model_validate({_TITLE_FIELD: _cap_title(stripped)})]
            v = parsed
        is_list = isinstance(v, list)
        items: list[Any] = cast(list[Any], v) if is_list else [v]
        out: list[Todo] = []
        problems: list[str] = []
        bad = 0
        for idx, item in enumerate(items):
            where = f"todos[{idx}]" if is_list else "todos"
            before = len(problems)
            if isinstance(item, Todo):
                out.append(item)
                continue
            if not isinstance(item, (dict, str)):
                problems.append(
                    f"{where}: expected an item object, a bare title string or a Todo, "
                    f"got {type(item).__name__}"
                )
                bad += 1
                continue
            try:
                out.append(Todo.model_validate(item))
            except ValidationError as exc:
                problems.extend(
                    _describe_item_problems(
                        exc,
                        where=where,
                        sent=item,
                        title_field=_TITLE_FIELD,
                        title_aliases=_TITLE_ALIASES,
                        title_hint=_TITLE_HINT,
                    )
                )
            if len(problems) > before:
                bad += 1
        if problems:
            raise ValueError(
                _format_item_problems(problems, bad=bad, total=len(items), noun="todo item")
            )
        return out


class TodoList(CallableTool2[Params]):
    """The single todo-tracking tool: read, write, merge, edit and rename."""

    name: str = "todo_list"
    description: str = _TODOLIST_DESCRIPTION
    params: type[Params] = Params
    # Per-tool field aliases: common aliases + the retired batch/lookup synonyms.
    # `task`/`todo`/`item`/`name` become `title` on items (via the Todo model's
    # AliasChoices) and `updates`/`edits`/`changes` become `todos`. The toolset's
    # argument repair handles top-level singular keys the flat map cannot express.
    field_aliases: ClassVar[dict[str, str]] = {
        **_COMMON_FIELD_ALIASES,
        **FIELD_ALIASES_TODO_LIST,
    }

    def __init__(self, runtime: Runtime) -> None:
        super().__init__()
        self._runtime = runtime

    @override
    async def __call__(self, params: Params) -> ToolReturnValue:
        if params.todos is None:
            if params.mode == "clear":
                return await self._write_todos([], params)
            return self._read_todos()
        if params.mode == "merge":
            return await self._merge_upsert(params)
        return await self._write_todos(params.todos, params)

    # ---- Write mode --------------------------------------------------------

    @staticmethod
    def _enforce_single_in_progress(todos: list[Todo]) -> list[str] | None:
        """Return list of titles that are in_progress if >1, else None.

        Recursive: the constraint is global across the whole tree (a child
        ``in_progress`` counts just like a root one).
        """
        in_progress: list[str] = []

        def walk(items: list[Todo]) -> None:
            for t in items:
                if t.status == "in_progress":
                    in_progress.append(t.title)
                walk(t.children)

        walk(todos)
        if len(in_progress) > 1:
            return in_progress
        return None

    @staticmethod
    def _auto_fix_in_progress(todos: list[Todo], warnings: list[str]) -> list[Todo]:
        """Enforce single in_progress, keeping the LAST item in DFS pre-order.

        Collects every in_progress node as a ``(container, index)`` pair in the
        same depth-first pre-order as ``_enforce_single_in_progress``, keeps the
        final one (the most recently listed item — the current focus), and
        demotes every earlier one to ``done``. Walks the whole tree so nested
        children participate too; ``warnings`` receives one line per demotion.

        Mutates only the list slots (element replacement), never the Todo
        objects themselves, so aliasing with persisted old items is safe.
        """
        slots: list[tuple[list[Todo], int]] = []

        def walk(items: list[Todo]) -> None:
            for i, item in enumerate(items):
                if item.status == "in_progress":
                    slots.append((items, i))
                walk(item.children)

        walk(todos)
        if len(slots) <= 1:
            return todos
        keep_container, keep_idx = slots[-1]
        kept_title = keep_container[keep_idx].title
        for container, idx in slots[:-1]:
            item = container[idx]
            warnings.append(
                f'Auto-fixed "{item.title}": set to done (only one item may be '
                f'in_progress; keeping "{kept_title}" in_progress)'
            )
            container[idx] = item.model_copy(update={"status": "done"})
        return todos

    async def _write_todos(
        self,
        raw_todos: list[Todo] | Todo,
        params: Params,
    ) -> ToolReturnValue:
        """Validate, merge, and persist todos, saving exactly once on success."""
        new_todos: list[Todo] = [raw_todos] if isinstance(raw_todos, Todo) else list(raw_todos)

        # 0. mode='clear' is a write of nothing — combining it with todos is a mistake.
        if params.mode == "clear" and new_todos:
            return self._error(
                "Error: mode='clear' cannot be combined with todos. "
                "Use mode='merge' or 'replace' to write todos, or call with no todos to read.",
                "mode='clear' cannot be combined with todos.",
            )

        # 1. Validate new inputs
        if params.mode != "clear":
            duplicates = self._find_duplicate_titles(new_todos)
            if duplicates:
                return self._error(
                    f"Error: Duplicate todo titles found: {duplicates}",
                    f"Duplicate todo titles found: {duplicates}",
                    hint="todo_list to read the tree, or parent=... to target one item.",
                )

            if self._count_all(new_todos) > _MAX_TODOS:
                return self._error(
                    f"Error: Todo list exceeds maximum limit of {_MAX_TODOS} items.",
                    f"Todo list exceeds maximum limit of {_MAX_TODOS} items.",
                )

        # 2. Load existing state
        old_todos = self._load_todos()
        old_archived = self._load_archived_todos()

        # 3. Branch on write mode. ``replaces_list`` marks modes that drop old
        # items (replace/clear) so completed ones get archived. Titles recovered
        # from `notes` surface here as non-blocking warnings (see
        # ``_derived_title_warnings``).
        warnings: list[str] = _derived_title_warnings(new_todos)
        replaces_list = False
        if params.mode == "clear":
            if old_todos and not all(t.status == "done" for t in old_todos):
                if not params.force:
                    unfinished = "\n".join(t.title for t in old_todos if t.status != "done")
                    return self._error(
                        "Error: Cannot clear todos while old todos are not all done. "
                        "Next step: mark them done first, "
                        "or call with mode='clear' and force=True to discard them intentionally.\n"
                        f"Unfinished:\n{unfinished}",
                        "Cannot clear todos while old todos are not all done.",
                        display=[self._build_display_block(old_todos)],
                    )
            final_todos = []
            replaces_list = True
        elif params.mode == "replace":
            if old_todos and not all(t.status == "done" for t in old_todos):
                if not params.force:
                    unfinished = "\n".join(t.title for t in old_todos if t.status != "done")
                    return self._error(
                        "Error: Cannot replace todos while old todos are not all done. "
                        "Use force=True if you really want to discard unfinished work.\n"
                        f"Unfinished:\n{unfinished}",
                        "Cannot replace todos while old todos are not all done.",
                    )
            final_todos = list(new_todos)
            replaces_list = True
        else:
            # mode='merge' has its own engine (_merge_upsert); only the
            # whole-tree modes reach _write_todos.
            raise AssertionError(f"unexpected todo write mode: {params.mode}")

        return self._finalize(
            final_todos,
            old_todos=old_todos,
            old_archived=old_archived,
            params=params,
            warnings=warnings,
            mode=params.mode,
            replaces_list=replaces_list,
        )

    @staticmethod
    def _error(
        output: str,
        message: str,
        display: list[Any] | None = None,
        hint: str | None = None,
    ) -> ToolReturnValue:
        """Build an error ToolReturnValue with consistent shape.

        ``hint`` (default: generic) names the follow-up call so the model does
        not have to guess the tool's own contract after a failure.
        """
        if hint is None:
            hint = "call todo_list with `todos` omitted to read the tree."
        return ToolReturnValue(
            is_error=True,
            output=output + _hint_error(hint),
            message=message,
            display=display if display is not None else [],
        )

    @staticmethod
    def _find_duplicate_titles(todos: list[Todo]) -> list[str] | None:
        """Return a sorted list of all duplicate titles, or None if all unique."""
        seen: set[str] = set()
        duplicates: set[str] = set()
        for t in todos:
            if t.title in seen:
                duplicates.add(t.title)
            else:
                seen.add(t.title)
        return sorted(duplicates) if duplicates else None

    @staticmethod
    def _format_todos(
        todos: list[Todo],
        *,
        status_filter: tuple[TodoStatus, ...] = (
            "pending",
            "in_progress"
        ),
        display_status: dict[TodoStatus, str] | None = None,
    ) -> str:
        """Return a dense Markdown summary of selected todos, or '' if none."""
        if display_status is None:
            display_status = {
                "pending": "pending",
                "in_progress": "in progress",
                "done": "done",
            }
        selected = [t for t in todos if t.status in status_filter]
        if not selected:
            return ""
        lines: list[str] = []
        for t in selected:
            todo = f"- [{display_status[t.status]}] {t.title}"
            if t.status == "in_progress" and t.notes:
                todo += f"  Notes: {t.notes}"
            lines.append(todo)

        return "\n".join(lines)

    # Score threshold for user-facing title suggestions. rapidfuzz returns a
    # normalized similarity in [0, 100]; 60 catches minor typos while avoiding
    # suggestions that share only a few characters.
    _FUZZY_TITLE_CUTOFF: float = 60.0

    # Warning threshold for append-mode titles that are fuzzy near-matches of
    # existing titles. Kept moderate; the warning is now non-blocking, so it
    # should flag likely typos without rejecting legitimate new todos that share
    # common words.
    _FUZZY_WARNING_CUTOFF: float = 75.0

    @staticmethod
    def _find_nearest_titles(
        query_titles: list[str],
        candidate_titles: list[str],
        top_k: int = 1,
        *,
        score_cutoff: float | None = None,
        processor: Callable[[str], str] | None = None,
        scorer: Callable[..., float] | None = None,
    ) -> dict[str, list[_FuzzyResult]]:
        """Return nearest candidate titles for each query title.

        Uses a lightweight string similarity matcher (rapidfuzz) instead of
        rebuilding a full inverted index on every call. Returns a mapping
        ``query_title -> [_FuzzyResult(...), ...]``. If no candidate titles
        exist or no match clears the cutoff, the list is empty.

        Args:
            query_titles: Titles to look up.
            candidate_titles: Titles to search against.
            top_k: Maximum number of nearest matches to return per query.
            score_cutoff: Minimum rapidfuzz score to include. Defaults to
                ``_FUZZY_TITLE_CUTOFF`` for backward compatibility.
            processor: Optional preprocessing function applied to both query and
                candidate strings before scoring. The returned candidate title is
                the original (unprocessed) value.
            scorer: rapidfuzz scorer to use. Defaults to ``token_sort_ratio``.
        """
        if not candidate_titles or not query_titles:
            return {q: [] for q in query_titles}

        cutoff = score_cutoff if score_cutoff is not None else TodoList._FUZZY_TITLE_CUTOFF
        scorer = scorer if scorer is not None else rapidfuzz.fuzz.token_sort_ratio

        results: dict[str, list[_FuzzyResult]] = {}
        for query in query_titles:
            matches = rapidfuzz.process.extract(
                query,
                candidate_titles,
                scorer=scorer,
                limit=top_k,
                score_cutoff=cutoff,
                processor=processor,
            )
            # rapidfuzz>=3 process.extract returns (choice, score, index) tuples.
            results[query] = [
                _FuzzyResult(choice=str(choice), score=float(score), index=int(index))
                for choice, score, index in matches
            ]
        return results


    @staticmethod
    def _max_tree_depth(todos: list[Todo]) -> int:
        """Return the maximum nesting depth of the tree (root items = depth 1)."""
        max_depth = 0

        def walk(items: list[Todo], depth: int) -> None:
            nonlocal max_depth
            for t in items:
                if depth > max_depth:
                    max_depth = depth
                walk(t.children, depth + 1)

        walk(todos, 1)
        return max_depth

    def _build_noop_response(self, todos: list[Todo]) -> ToolReturnValue:
        """Response for an append-mode write with an empty todos list (no-op)."""
        counts = self._status_counts(todos)
        total = self._count_all(todos)
        stats = (
            f"({total} total: {counts['done']} done, "
            f"{counts['in_progress']} in progress, {counts['pending']} pending)"
        )
        output = f"Todo list unchanged; no todos provided {stats}"
        active_summary = self._format_todos(todos)
        if active_summary:
            output += "\n" + active_summary
        if total > 0:
            output += _hint_next(_TODOLIST_SUCCESS_HINT)
        return ToolReturnValue(
            is_error=False,
            output=output,
            message="No todos provided; todo list unchanged.",
            display=[self._build_display_block(todos)] if todos else [],
        )

    def _detect_fuzzy_warnings(
        self,
        new_todos: list[Todo],
        old_title_set: set[str],
        old_title_list: list[str],
    ) -> list[str]:
        """Advisory warnings for titles resembling existing ones (below the conflict cutoff)."""
        if not old_title_list:
            return []
        warnings: list[str] = []
        for new_todo in new_todos:
            if new_todo.title in old_title_set:
                continue
            nearest = self._find_nearest_titles(
                [new_todo.title],
                old_title_list,
                top_k=1,
                score_cutoff=TodoList._FUZZY_WARNING_CUTOFF,
                processor=str.lower,
            )
            hits = nearest.get(new_todo.title, [])
            if hits:
                warnings.append(f'"{new_todo.title}" looks like existing "{hits[0].choice}"')
        return warnings

    @staticmethod
    def _check_regressions(
        old_todos: list[Todo], final_todos: list[Todo]
    ) -> tuple[list[Todo], list[str]]:
        """Detect done todos being moved back to pending/in_progress.

        Recursive: the old-status map is built across the whole tree and the
        clamp (done → pending/in_progress reverted to ``done``) applies to
        every descendant. Returns the final list with regressed items clamped
        back to ``done``, plus the list of regressed titles.
        """
        old_status_map: dict[str, str] = {}

        def collect(items: list[Todo]) -> None:
            for t in items:
                old_status_map[t.title] = t.status
                collect(t.children)

        collect(old_todos)

        regressions: list[str] = []

        def clamp(items: list[Todo]) -> list[Todo]:
            out: list[Todo] = []
            for t in items:
                if old_status_map.get(t.title) == "done" and t.status != "done":
                    regressions.append(t.title)
                    t = t.model_copy(update={"status": "done"})
                if t.children:
                    t = t.model_copy(update={"children": clamp(t.children)})
                out.append(t)
            return out

        return clamp(final_todos), regressions

    def _build_success_response(
        self,
        todos: list[Todo],
        mode: str,
        had_old_todos: bool,
        warnings: list[str],
        force: bool = False,
        show_tree: bool = False,
    ) -> ToolReturnValue:
        display_block = self._build_display_block(todos)
        if show_tree:
            # An edit call: show the nested tree, so a child that was just
            # created or patched is visible where it lives.
            active_summary = self._render_read_tree(todos, max_lines=_MAX_READ_ITEMS)
        else:
            active_summary = self._format_todos(todos)
        counts = self._status_counts(todos)

        mode_msg = {
            "merge": "updated",
            "replace": "replaced",
            "clear": "cleared",
        }[mode]

        stats = (
            f"({self._count_all(todos)} total: {counts['done']} done, "
            f"{counts['in_progress']} in progress, {counts['pending']} pending)"
        )
        output_lines: list[str] = [f"Todo list {mode_msg} {stats}"]
        if active_summary:
            output_lines.append(active_summary)
        output = "\n".join(output_lines)

        # Append all-done reminder when all todos are done.
        all_done_reminder = _ALL_DONE_REMINDER
        # Append the original user prompt as context when available.
        current_prompt = getattr(self._runtime, "current_prompt", None)
        if current_prompt:
            all_done_reminder += "\nOriginal prompt:\n\n" + _truncate_prompt(current_prompt)
        if counts["pending"] == 0 and counts["in_progress"] == 0 and len(todos) > 0:
            output_lines.append(all_done_reminder)
            output = "\n".join(output_lines)

        # One-line cross-tool hint (output ONLY — never message; suppressed for
        # the 0-total case so exact-output assertions on empty writes hold).
        if self._count_all(todos) > 0:
            output += _hint_next(_TODOLIST_SUCCESS_HINT)

        message_lines: list[str] = [f"Todo list {mode_msg}."]
        if counts["pending"] == 0 and counts["in_progress"] == 0 and len(todos) > 0:
            message_lines.append(all_done_reminder)
        if force and had_old_todos:
            message_lines.append(
                "Warning: force=True bypassed the all-done guard and replaced the existing todo list."
            )
        if counts["in_progress"] > 1:
            message_lines.append(
                f"Note: {counts['in_progress']} items are in_progress; "
                "prefer exactly one at a time."
            )
        if warnings:
            message_lines.extend(["", *warnings])
        message = "\n".join(message_lines)

        return ToolReturnValue(
            is_error=False,
            output=output,
            message=message,
            display=[display_block],
        )

    @staticmethod
    def _status_counts(todos: list[Todo]) -> dict[TodoStatus, int]:
        """Count todos by status across the whole tree (recursive)."""
        counts: dict[TodoStatus, int] = {"pending": 0, "in_progress": 0, "done": 0}

        def walk(items: list[Todo]) -> None:
            for t in items:
                counts[t.status] += 1
                walk(t.children)

        walk(todos)
        return counts

    @staticmethod
    def _count_all(todos: list[Todo]) -> int:
        """Recursive total item count across the whole tree."""
        total = 0
        pending: list[Todo] = list(todos)
        while pending:
            t = pending.pop()
            total += 1
            pending.extend(t.children)
        return total

    @staticmethod
    def _count_unfinished_descendants(todo: Todo) -> int:
        """Count unfinished descendants (children and deeper) of a todo."""
        total = 0

        def walk(items: list[Todo]) -> None:
            nonlocal total
            for t in items:
                if t.status != "done":
                    total += 1
                walk(t.children)

        walk(todo.children)
        return total

    @staticmethod
    def _mark_subtree_done(node: Todo) -> None:
        """Recursively mark a node and all its descendants done."""
        node.status = "done"
        for child in node.children:
            TodoList._mark_subtree_done(child)

    @staticmethod
    def _build_display_block(todos: list[Todo]) -> TodoDisplayBlock:
        """Build a flattened display block with per-item ``depth`` (root = 0).

        Depth-first so the frontend can indent children under their parent.
        """
        items: list[TodoDisplayItem] = []

        def walk(items_list: list[Todo], depth: int) -> None:
            for todo in items_list:
                items.append(
                    TodoDisplayItem(
                        title=todo.title,
                        status=todo.status,
                        notes=todo.notes,
                        depth=depth,
                    )
                )
                walk(todo.children, depth + 1)

        walk(todos, 0)
        return TodoDisplayBlock(items=items)

    # ---- Read mode ---------------------------------------------------------

    def _render_read_tree(
        self, todos: list[Todo], *, max_lines: int = _MAX_READ_ITEMS
    ) -> str:
        """Render the todo tree for read mode: all statuses, children indented.

        Depth-0 lines are byte-identical to the legacy flat renderer (so flat
        lists render unchanged); children are indented 2 spaces per depth.
        Stops after ``max_lines`` lines (depth-first).
        """
        display_status = {
            "pending": "pending",
            "in_progress": "in_progress",
            "done": "done",
        }
        lines: list[str] = []

        def walk(items: list[Todo], depth: int) -> None:
            for t in items:
                if len(lines) >= max_lines:
                    return
                line = f"{'  ' * depth}- [{display_status[t.status]}] {t.title}"
                if t.status == "in_progress" and t.notes:
                    line += f"  Notes: {t.notes}"
                lines.append(line)
                walk(t.children, depth + 1)

        walk(todos, 0)
        return "\n".join(lines)

    def _read_todos(self) -> ToolReturnValue:
        todos = self._load_todos()
        archived = self._load_archived_todos()

        if not todos:
            empty_lines = ["Todo list is empty."]
            if archived:
                empty_lines.append(f"Archived: {len(archived)} completed todo(s).")
            return ToolReturnValue(
                is_error=False,
                output="\n".join(empty_lines) + _hint_next(_TODOLIST_SUCCESS_HINT),
                message="Todo list is empty.",
                display=[],
            )

        # Recursive counts across the whole tree (identical for flat lists).
        counts = self._status_counts(todos)
        total = self._count_all(todos)
        all_done = total > 0 and counts["pending"] == 0 and counts["in_progress"] == 0

        # Render the tree (children indented 2 spaces per depth), truncating
        # the flattened line list to _MAX_READ_ITEMS.
        output_lines = ["Current todo list:"]
        formatted = self._render_read_tree(todos, max_lines=_MAX_READ_ITEMS)
        if formatted:
            output_lines.append(formatted)

        if total > _MAX_READ_ITEMS:
            output_lines.append(
                f"... and {total - _MAX_READ_ITEMS} more "
                f"({counts['pending']} pending, {counts['in_progress']} in_progress, "
                f"{counts['done']} done total)"
            )
        if archived:
            output_lines.append(f"Archived: {len(archived)} completed todo(s).")

        # Append all-done reminder.
        all_done_reminder = _ALL_DONE_REMINDER
        # Append the original user prompt as context when available.
        current_prompt = getattr(self._runtime, "current_prompt", None)
        if current_prompt:
            all_done_reminder += "\nOriginal prompt:\n\n" + _truncate_prompt(current_prompt)
        if all_done:
            output_lines.append(all_done_reminder)

        # One-line cross-tool hint (output ONLY — never message).
        output_lines.append("Next: " + _TODOLIST_SUCCESS_HINT)

        return ToolReturnValue(
            is_error=False,
            output="\n".join(output_lines),
            message=all_done_reminder if all_done else "Current todo list displayed.",
            display=[],
        )

    # ---- Persistence -------------------------------------------------------

    def _save_todos(self, active: list[Todo], archived: list[TodoItemState]) -> str | None:
        """Persist active and archived todos. Returns error message on failure."""
        active_items = self._item_states(active)

        if self._runtime.role == "root":
            return self._save_root_todos(active_items, archived)
        return self._save_subagent_todos(active_items, archived)

    def _load_todos(self) -> list[Todo]:
        """Load active todos from the appropriate state file."""
        if self._runtime.role == "root":
            return self._load_root_todos()
        return self._load_subagent_todos()

    def _load_archived_todos(self) -> list[TodoItemState]:
        """Load archived todos from the appropriate state file."""
        if self._runtime.role == "root":
            return list(self._runtime.session.state.archived_todos)
        return self._load_subagent_archived_todos()

    def _max_layers(self) -> int:
        """Maximum todo tree depth (layers). Default 4.

        Used to cap how deep a tree can be built with ``parent=...``: the
        deepest explicit-parent level is ``max_layers``, plus one nested
        level under it.
        """
        try:
            value = self._runtime.config.loop_control.todo_max_layers
        except Exception:
            return 4
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        return 4

    def _save_root_todos(
        self, items: list[TodoItemState], archived: list[TodoItemState]
    ) -> str | None:
        try:
            session = self._runtime.session
            session.state.todos = items
            session.state.archived_todos = archived
            session.save_state()
            return None
        except Exception as exc:
            return f"Error: Failed to save root todos: {exc}"

    def _load_root_todos(self) -> list[Todo]:
        from kimi_cli.session_state import load_session_state

        session = self._runtime.session
        fresh = load_session_state(session.dir)
        session.state.todos = fresh.todos
        session.state.archived_todos = fresh.archived_todos
        result: list[Todo] = []
        for t in fresh.todos:
            try:
                result.append(Todo.model_validate(t.model_dump()))
            except Exception:
                logger.warning("Skipping malformed todo item in root state: {t}", t=t)
        return result

    def _save_subagent_todos(
        self, items: list[TodoItemState], archived: list[TodoItemState]
    ) -> str | None:
        state_file = self._subagent_state_file()
        if state_file is None:
            return "Error: Unable to save subagent todos: state file is not available."
        data = self._read_subagent_state(state_file)
        data["todos"] = [item.model_dump() for item in items]
        data["archived_todos"] = [item.model_dump() for item in archived]
        try:
            self._write_subagent_state(state_file, data)
        except Exception as exc:
            return f"Error: Failed to save subagent todos: {exc}"
        return None

    def _load_subagent_todos(self) -> list[Todo]:
        state_file = self._subagent_state_file()
        if state_file is None:
            return []
        data = self._read_subagent_state(state_file)
        raw_todos_val = data.get("todos", [])
        raw_todos = cast(list[Any], raw_todos_val) if isinstance(raw_todos_val, list) else []
        result: list[Todo] = []
        for item in raw_todos:
            try:
                result.append(Todo.model_validate(item))
            except Exception:
                logger.warning("Skipping malformed todo item in subagent state: {item}", item=item)
        return result

    def _load_subagent_archived_todos(self) -> list[TodoItemState]:
        state_file = self._subagent_state_file()
        if state_file is None:
            return []
        data = self._read_subagent_state(state_file)
        raw_archived_val = data.get("archived_todos", [])
        raw_archived = (
            cast(list[Any], raw_archived_val) if isinstance(raw_archived_val, list) else []
        )
        result: list[TodoItemState] = []
        for item in raw_archived:
            try:
                result.append(TodoItemState.model_validate(item))
            except Exception:
                logger.warning(
                    "Skipping malformed archived todo item in subagent state: {item}", item=item
                )
        return result

    @staticmethod
    def _item_states(todos: list[Todo]) -> list[TodoItemState]:
        return [
            TodoItemState(
                title=todo.title,
                status=todo.status,
                notes=todo.notes,
                children=TodoList._item_states(todo.children),
            )
            for todo in todos
        ]

    def _subagent_state_file(self) -> Path | None:
        store = self._runtime.subagent_store
        agent_id = self._runtime.subagent_id
        if store is None or agent_id is None:
            return None
        return store.instance_dir(agent_id) / "state.json"

    @staticmethod
    def _read_subagent_state(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            data = orjson.loads(path.read_text(encoding="utf-8"))
        except (orjson.JSONDecodeError, OSError, UnicodeDecodeError):
            logger.warning("Corrupted subagent todo state, using defaults: {path}", path=path)
            return {}
        if not isinstance(data, dict):
            logger.warning("Invalid subagent todo state type, using defaults: {path}", path=path)
            return {}
        return cast(dict[str, Any], data)

    @staticmethod
    def _write_subagent_state(path: Path, data: dict[str, Any]) -> None:
        from kimi_cli.utils.io import atomic_json_write

        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(data, path)



    def _effective_item(self, item: Todo, params: Params) -> Todo:
        """Apply the call-level defaults (`scope`, `fuzzy`, `force`) to one item.

        An item keeps its own value whenever it was sent explicitly — tracked via
        pydantic's ``model_fields_set`` so a defaulted field is never mistaken
        for a chosen one.
        """
        update: dict[str, Any] = {}
        if params.scope is not None and item.parent is None:
            update["parent"] = params.scope
        if not params.fuzzy and "fuzzy" not in item.model_fields_set:
            update["fuzzy"] = False
        if params.force and "force" not in item.model_fields_set:
            update["force"] = True
        return item.model_copy(update=update) if update else item

    async def _merge_upsert(self, params: Params) -> ToolReturnValue:
        """``mode='merge'``: upsert every item — patch in place, or create.

        The whole call is applied to an in-memory tree and persisted once, so a
        rejected item leaves the stored plan untouched.
        """
        items = params.todos or []
        old_todos = self._load_todos()
        if not items:
            return self._build_noop_response(old_todos)
        old_archived = self._load_archived_todos()

        duplicates = self._find_duplicate_titles(items)
        if duplicates:
            return self._error(
                f"Error: Duplicate todo titles found: {duplicates}",
                f"Duplicate todo titles found: {duplicates}",
                hint="send each item once, or use parent=... to edit a specific sub-tree item.",
            )
        if self._count_all(items) > _MAX_TODOS:
            return self._error(
                f"Error: Todo list exceeds maximum limit of {_MAX_TODOS} items.",
                f"Todo list exceeds maximum limit of {_MAX_TODOS} items.",
            )

        warnings: list[str] = _derived_title_warnings(items)
        # Advisory only: titles that resemble an existing item but are not the
        # same set of words (see `_find_conflict`) are still created, with a note.
        if old_todos:
            old_titles = [t.title for t in old_todos]
            warnings.extend(self._detect_fuzzy_warnings(items, set(old_titles), old_titles))
        tree = old_todos
        patched = False
        summaries: list[str] = []
        for idx, raw in enumerate(items):
            item = self._effective_item(raw, params)
            result = self._upsert_one(tree, item, params, warnings, where=f"todos[{idx}]")
            if isinstance(result, ToolReturnValue):
                return result
            tree, summary = result
            summaries.append(summary)
            patched = patched or summary.startswith("Updated")

        # Per-item lines are the point of an *edit* (a patch, or anything aimed at
        # a scope via parent/scope). A call that only created root items is fully
        # described by the tree summary above, so repeating it item by item would
        # also echo done items that the summary deliberately hides.
        scoped = params.scope is not None or any(i.parent is not None for i in items)
        detail = bool(summaries) and (patched or scoped)
        finalized = self._finalize(
            tree,
            old_todos=old_todos,
            old_archived=old_archived,
            params=params,
            warnings=warnings,
            mode="merge",
            replaces_list=False,
            show_tree=detail,
        )
        if isinstance(finalized, ToolReturnValue) and finalized.is_error:
            return finalized
        output = finalized.output
        if warnings:
            output += "\n" + "\n".join(warnings)
        if detail:
            output = str(output) + "\n" + "\n".join(summaries)
        message = finalized.message
        if detail:
            # The per-item lines are the signal the model wants here, in terse
            # form (the detail stays in `output`). Anything the standard message
            # carries beyond its first line — the all-done review reminder, the
            # force warning — is kept as a suffix instead of being dropped.
            terse = "; ".join(_terse_summary(s) for s in summaries)
            standard_lines = message.split("\n", 1)
            suffix = standard_lines[1] if len(standard_lines) > 1 else ""
            message = terse + (("\n" + suffix) if suffix else "")
            if warnings:
                message = message + "\n" + "\n".join(warnings)
        if output != finalized.output or message != finalized.message:
            finalized = ToolReturnValue(
                is_error=False,
                output=output,
                message=message,
                display=finalized.display,
            )
        return finalized

    def _upsert_one(
        self,
        tree: list[Todo],
        item: Todo,
        params: Params,
        warnings: list[str],
        *,
        where: str,
    ) -> tuple[list[Todo], str] | ToolReturnValue:
        """Upsert a single item into ``tree``; returns the new tree and a summary line."""
        if item.rename_to is not None or item.complete:
            # A pure edit (rename / finish a sub-tree): the edit engine already
            # owns those semantics, including its force/reopen guards.
            if item.children:
                return self._error(
                    f"Error: {where} combines rename_to/complete with children.",
                    "rename_to/complete cannot carry children.",
                    hint=(
                        "rename or complete the parent first, then write its children "
                        "as separate items with parent=..."
                    ),
                )
            result = self._apply_one_update(tree, item, warnings)
            if isinstance(result, ToolReturnValue):
                return result
            todos, summary, _, _ = result
            return todos, summary

        if item.parent:
            container_path: list[int] | None = self._resolve_scope_path(
                tree, item.parent, params, warnings, where=where
            )
            if container_path is None:
                return self._error(
                    f'Error: No parent todo matching "{item.parent}" found.',
                    f'No parent todo matching "{item.parent}" found.',
                    hint="Omit `todos` to read the tree, or fix the parent title.",
                )
        else:
            # No scope: an existing item anywhere in the tree is patched in place
            # instead of being duplicated at the root.
            path = self._find_path(tree, item.title)
            if path is not None:
                node = self._node_at_path(tree, path)
                merged = self._merge_node(node, item, params, warnings)
                if isinstance(merged, ToolReturnValue):
                    return merged
                return (
                    self._replace_node(tree, path, merged),
                    self._summary(node, item, merged),
                )
            container_path = []

        container = tree if not container_path else self._node_at_path(tree, container_path).children
        scope_label = item.parent or ("root" if not container_path else "")
        new_container, summary = self._upsert_in_container(
            container,
            item,
            params,
            warnings,
            where=where,
            depth=len(container_path) + 1,
            scope_label=scope_label,
        )
        if isinstance(new_container, ToolReturnValue):
            return new_container
        if not container_path:
            return new_container, summary
        parent_node = self._node_at_path(tree, container_path)
        return (
            self._replace_node(tree, container_path, parent_node.model_copy(update={"children": new_container})),
            summary,
        )

    def _upsert_in_container(
        self,
        container: list[Todo],
        item: Todo,
        params: Params,
        warnings: list[str],
        *,
        where: str,
        depth: int = 1,
        scope_label: str = "",
    ) -> tuple[list[Todo] | ToolReturnValue, str]:
        """Patch ``item`` onto an existing sibling, or append it as a new one."""
        for idx, existing in enumerate(container):
            if existing.title == item.title:
                patched = self._merge_node(existing, item, params, warnings)
                if isinstance(patched, ToolReturnValue):
                    return patched, ""
                updated = list(container)
                updated[idx] = patched
                return updated, self._summary(existing, item, patched)

        conflict = (
            self._find_conflict(item.title, container)
            if item.fuzzy and params.fuzzy
            else None
        )
        if conflict is not None:
            if params.on_conflict == "error":
                return self._conflict_error(item, conflict, where=where), ""
            payload_title = conflict
            if params.on_conflict == "reuse":
                warnings.append(
                    f'"{item.title}" matched existing "{conflict}" (near-duplicate title); '
                    "patched that item instead of adding a new one."
                )
                for idx, existing in enumerate(container):
                    if existing.title == payload_title:
                        patched = self._merge_node(existing, item, params, warnings)
                        if isinstance(patched, ToolReturnValue):
                            return patched, ""
                        updated = list(container)
                        updated[idx] = patched
                        return updated, (
                            f'Updated "{conflict}" ({item.title} was a near-duplicate).'
                        )
            else:
                warnings.append(
                    f'"{item.title}" was added next to the near-duplicate "{conflict}" '
                    "(on_conflict='append')."
                )

        if depth > self._max_layers() + 1:
            return (
                self._error(
                    f"Error: cannot add a child at depth {depth}: children would "
                    f"exceed the maximum depth ({self._max_layers() + 1}).",
                    "Cannot add children deeper than "
                    f"{self._max_layers()} layers.",
                    hint="Cannot add children deeper than "
                    f"{self._max_layers() + 1} layers.",
                ),
                "",
            )
        created = item.model_copy(
            update={
                "status": item.status if "status" in item.model_fields_set else "pending",
                "children": [
                    c.model_copy(update={"status": c.status if "status" in c.model_fields_set else "pending"})
                    for c in item.children
                ],
            }
        )
        where = f' under "{scope_label}".' if scope_label else "."
        return [*container, created], f'Created "{item.title}"' + where

    def _merge_node(
        self, old: Todo, item: Todo, params: Params, warnings: list[str]
    ) -> Todo | ToolReturnValue:
        """Patch an existing node: only the fields the caller actually sent change."""
        update: dict[str, Any] = {}
        if "status" in item.model_fields_set:
            update["status"] = item.status
        if item.notes is not None and item.notes.strip():
            # Non-destructive default: an omitted *or* blank `notes` keeps what is
            # stored, because models send "" to mean "nothing to add", not
            # "erase the detail". Only real text replaces it.
            update["notes"] = item.notes
        if "children" in item.model_fields_set:
            children = list(old.children)
            for child in item.children:
                merged, _summary = self._upsert_in_container(
                    children, child, params, warnings, where=f"{item.title}.children"
                )
                if isinstance(merged, ToolReturnValue):
                    return merged
                children = merged
            update["children"] = children
        return old.model_copy(update=update) if update else old

    @staticmethod
    def _summary(old: Todo, item: Todo, merged: Todo) -> str:
        """One line saying what an upsert actually changed (empty string for a no-op)."""
        parts: list[str] = []
        if "status" in item.model_fields_set and old.status != merged.status:
            parts.append(f"status={merged.status}")
        if item.notes is not None and item.notes.strip() and old.notes != merged.notes:
            parts.append("notes updated")
        if "children" in item.model_fields_set and len(old.children) != len(merged.children):
            added = len(merged.children) - len(old.children)
            parts.append(f"{added:+d} sub-todo{'s' if added != 1 else ''}")
        elif "children" in item.model_fields_set and old.children != merged.children:
            parts.append("sub-todos updated")
        body = f" ({', '.join(parts)})" if parts else ""
        return f'Updated "{old.title}"{body}.'

    def _find_conflict(self, title: str, container: list[Todo]) -> str | None:
        """Return the sibling title that is the same task under a different spelling.

        Same words (any order, any punctuation, any leading numbering) = same
        task. Anything else — including a typo — is left to the advisory warning
        tier, so legitimate siblings are never refused. Returns the first match.
        """
        words = frozenset(normalize_title(title).split())
        if not words:
            return None
        for existing in container:
            if existing.title == title:
                continue
            if frozenset(normalize_title(existing.title).split()) == words:
                return existing.title
        return None

    def _conflict_error(self, item: Todo, conflict: str, *, where: str) -> ToolReturnValue:
        """Name the existing item and the exact payload that patches it instead."""
        payload: dict[str, Any] = {"title": conflict}
        if "status" in item.model_fields_set:
            payload["status"] = item.status
        if item.notes:
            payload["notes"] = item.notes
        replacement = orjson.dumps(payload).decode("utf-8")
        return self._error(
            f'Error: {where} "{item.title}" is a near-duplicate of the existing "{conflict}"\n'
            f"Patch that item instead of re-declaring the plan — send: {replacement}\n"
            'Or pass on_conflict="append" to really add a second item, '
            'or on_conflict="reuse" to patch it silently.',
            f'Near-duplicate title "{item.title}" (existing: "{conflict}").',
            hint=(
                'todo_list to read the tree, then resend with the existing title, '
                'or on_conflict="append" for a genuinely new item.'
            ),
        )

    def _resolve_scope_path(
        self,
        tree: list[Todo],
        parent_title: str,
        params: Params,
        warnings: list[str],
        *,
        where: str,
    ) -> list[int] | None:
        """Locate the container named by ``parent_title`` (exact, then fuzzy)."""
        path = self._find_path(tree, parent_title)
        if path is not None:
            return path
        if not params.fuzzy:
            return None
        hits = self._find_nearest_titles(
            [parent_title],
            self._collect_titles(tree),
            top_k=1,
            score_cutoff=TodoList._FUZZY_TITLE_CUTOFF,
            processor=str.lower,
        ).get(parent_title, [])
        if not hits:
            return None
        warnings.append(f'{where}: parent "{parent_title}" matched "{hits[0].choice}".')
        return self._find_path(tree, hits[0].choice)

    def _replace_node(self, tree: list[Todo], path: list[int], node: Todo) -> list[Todo]:
        """Return a copy of ``tree`` with the node at ``path`` replaced by ``node``."""
        if not path:
            return tree
        root = list(tree)
        cursor = root
        for i, idx in enumerate(path):
            current = cursor[idx]
            if i == len(path) - 1:
                cursor[idx] = node
                break
            replacement = current.model_copy(update={"children": list(current.children)})
            cursor[idx] = replacement
            cursor = cast(list[Todo], replacement.children)
        return root

    def _finalize(
        self,
        final_todos: list[Todo],
        *,
        old_todos: list[Todo],
        old_archived: list[TodoItemState],
        params: Params,
        warnings: list[str],
        mode: str,
        replaces_list: bool,
        show_tree: bool = False,
    ) -> ToolReturnValue:
        """Shared tail of every write path: guards, archive, one save, response."""
        max_layers = self._max_layers()
        max_depth = max_layers + 1
        if self._max_tree_depth(final_todos) > max_depth:
            return self._error(
                f"Error: Todo tree exceeds maximum nesting depth of {max_depth} levels "
                f"(todo_max_layers={max_layers}). Flatten the tree, or build it one level "
                "at a time with parent=...",
                f"Todo tree exceeds maximum nesting depth of {max_depth} levels.",
                display=[self._build_display_block(final_todos)],
            )

        if not params.force and mode != "clear" and old_todos:
            final_todos, regressions = self._check_regressions(old_todos, final_todos)
            if regressions:
                return self._error(
                    "Error: Cannot regress completed todos back to pending/in_progress: "
                    + ", ".join(regressions)
                    + "\nNext step: resend with these items kept as 'done', "
                    "or use force=True to restart them intentionally.",
                    "Cannot regress completed todos.",
                    display=[self._build_display_block(final_todos)],
                )

        archived = list(old_archived)
        if replaces_list and old_todos:
            kept_titles = {t.title for t in final_todos}
            newly_archived = [
                t for t in old_todos if t.status == "done" and t.title not in kept_titles
            ]
            if newly_archived:
                archived.extend(self._item_states(newly_archived))
                archived = archived[-_MAX_ARCHIVED_TODOS:]

        if not params.force and mode != "clear":
            conflicts = self._enforce_single_in_progress(final_todos)
            if conflicts:
                if params.auto_fix:
                    final_todos = self._auto_fix_in_progress(final_todos, warnings)
                else:
                    return self._error(
                        f"Error: Multiple items are in_progress: {conflicts}. "
                        "Keep exactly one item in_progress at a time. "
                        "Mark the current item as 'done' before starting another, "
                        "use force=True to override, "
                        "or set auto_fix=True to automatically resolve conflicts.",
                        "Multiple items in_progress",
                        display=[self._build_display_block(final_todos)],
                    )

        save_error = self._save_todos(final_todos, archived)
        if save_error:
            return self._error(save_error, "Failed to save todos.")
        return self._build_success_response(
            final_todos, mode, bool(old_todos), warnings, params.force, show_tree=show_tree
        )

    def _apply_one_update(
        self,
        todos: list[Todo],
        params: Todo,
        warnings: list[str],
    ) -> tuple[list[Todo], str, str, tuple[str, str] | None] | ToolReturnValue:
        """Apply a single update item to an in-memory todo tree.

        Returns ``(new_tree, summary, message, rename_pair)`` on success, where
        ``summary`` is the detailed human-readable line, ``message`` is the
        short confirmation, and ``rename_pair`` is ``(old_title, new_title)`` if
        a rename happened. On failure returns a ``ToolReturnValue`` error.
        """
        parent_raw = params.parent.strip() if params.parent is not None else None

        if parent_raw is None:
            if not todos:
                hint = (
                    'Use parent="" with a title to create a root todo, '
                    "or mode='replace' to set the whole list."
                )
                return ToolError(
                    message="No todos to update.",
                    brief=hint,
                    output="Error: No todos exist." + _hint_error(hint),
                )
            return self._update_global_in_memory(todos, params, warnings)

        return self._update_or_create_under_parent_in_memory(todos, parent_raw, params, warnings)

    def _update_global_in_memory(
        self,
        todos: list[Todo],
        params: Todo,
        warnings: list[str],
    ) -> tuple[list[Todo], str, str, tuple[str, str] | None] | ToolReturnValue:
        target_title = params.title
        path = self._find_path(todos, target_title)
        matched_title = target_title

        if path is None:
            if not params.fuzzy:
                hint = 'Omit `todos` to read the tree, or set fuzzy=True to search by similarity.'
                return ToolError(
                    message=f'Todo "{target_title}" not found.',
                    brief=hint,
                    output=f'Error: No todo titled "{target_title}" found.' + _hint_error(hint),
                )
            nearest = self._find_nearest_titles(
                [target_title],
                self._collect_titles(todos),
                top_k=1,
                score_cutoff=TodoList._FUZZY_TITLE_CUTOFF,
                processor=str.lower,
            )
            hits = nearest.get(target_title, [])
            if not hits:
                hint = "Omit `todos` to read the tree."
                return ToolError(
                    message=f'No todo matching "{target_title}" found.',
                    brief=hint,
                    output=f'Error: No todo matching "{target_title}" found.' + _hint_error(hint),
                )
            matched_title = hits[0].choice
            path = self._find_path(todos, matched_title)
            warnings.append(f'Fuzzy matched "{target_title}" to "{matched_title}".')

        assert path is not None
        return self._apply_update_to_tree(todos, path, matched_title, params, warnings)

    def _update_or_create_under_parent_in_memory(
        self,
        todos: list[Todo],
        parent_title: str,
        params: Todo,
        warnings: list[str],
    ) -> tuple[list[Todo], str, str, tuple[str, str] | None] | ToolReturnValue:
        if parent_title == "":
            parent_path: list[int] | None = []
            parent_node: Todo | None = None
            resolved_parent_title = "root"
        else:
            parent_path = self._find_path(todos, parent_title)
            if parent_path is None:
                if not params.fuzzy:
                    hint = 'Omit `todos` to read the tree, or set fuzzy=True to search by similarity.'
                    return ToolError(
                        message=f'Parent todo "{parent_title}" not found.',
                        brief=hint,
                        output=f'Error: No parent todo titled "{parent_title}" found.' + _hint_error(hint),
                    )
                nearest = self._find_nearest_titles(
                    [parent_title],
                    self._collect_titles(todos),
                    top_k=1,
                    score_cutoff=TodoList._FUZZY_TITLE_CUTOFF,
                    processor=str.lower,
                )
                hits = nearest.get(parent_title, [])
                if not hits:
                    hint = "Omit `todos` to read the tree."
                    return ToolError(
                        message=f'No parent todo matching "{parent_title}" found.',
                        brief=hint,
                        output=f'Error: No parent todo matching "{parent_title}" found.' + _hint_error(hint),
                    )
                resolved_parent_title = hits[0].choice
                parent_path = self._find_path(todos, resolved_parent_title)
                warnings.append(f'Fuzzy matched parent "{parent_title}" to "{resolved_parent_title}".')
                assert parent_path is not None
                parent_node = self._node_at_path(todos, parent_path)
            else:
                parent_node = self._node_at_path(todos, parent_path)
                resolved_parent_title = parent_node.title

        scope = parent_node.children if parent_node is not None else todos
        target_title = params.title
        child_index = next((i for i, t in enumerate(scope) if t.title == target_title), None)

        if child_index is not None:
            path = [*parent_path, child_index] if parent_path else [child_index]
            return self._apply_update_to_tree(todos, path, target_title, params, warnings)

        # New child creation under the resolved parent.
        if params.rename_to is not None:
            hint = f'Cannot rename a new child; use title="{params.rename_to}" to create it.'
            return ToolError(
                message=f'Cannot rename non-existent todo "{target_title}".',
                brief=hint,
                output=f'Error: "{target_title}" does not exist under "{resolved_parent_title}".' + _hint_error(hint),
            )

        if params.complete:
            hint = (
                f'complete=True requires an existing todo; "{target_title}" does not exist '
                f'under "{resolved_parent_title}". Create it first or use status="done".'
            )
            return ToolError(
                message=f'Cannot complete non-existent todo "{target_title}".',
                brief=hint,
                output=f'Error: "{target_title}" does not exist under "{resolved_parent_title}".' + _hint_error(hint),
            )

        parent_depth = len(parent_path)
        max_layers = self._max_layers()
        if parent_depth > max_layers:
            hint = f"Cannot add children deeper than {max_layers + 1} layers."
            return ToolError(
                message=f'Cannot add a child under "{resolved_parent_title}": too deep.',
                brief=hint,
                output=(
                    f'Error: "{resolved_parent_title}" is at depth {parent_depth}; '
                    f"children would exceed the maximum depth ({max_layers + 1})."
                    + _hint_error(hint)
                ),
            )

        new_child = Todo(
            title=target_title,
            status=params.status if "status" in params.model_fields_set else "pending",
            notes=params.notes,
        )
        final_todos = self._insert_child_at_path(todos, parent_path, new_child)

        summary = f'Created "{target_title}" under "{resolved_parent_title}".'
        return final_todos, summary, summary, None

    def _apply_update_to_tree(
        self,
        todos: list[Todo],
        path: list[int],
        matched_title: str,
        params: Todo,
        warnings: list[str],
    ) -> tuple[list[Todo], str, str, tuple[str, str] | None] | ToolReturnValue:
        old_node = self._node_at_path(todos, path)
        new_status = (
            params.status if "status" in params.model_fields_set else old_node.status
        )

        # complete=True marks the subtree done — it cannot be combined with a
        # pending/in_progress status (ambiguous intent).
        if (
            params.complete
            and "status" in params.model_fields_set
            and params.status in ("pending", "in_progress")
        ):
            hint = f'Send "{matched_title}" with status="done" instead of complete=True, or omit status.'
            return ToolError(
                message=f'complete=True cannot be combined with status="{params.status}".',
                brief=hint,
                output=(
                    f'Error: complete=True cannot be combined with status="{params.status}" '
                    f'for "{matched_title}". complete=True always marks everything done.'
                    + _hint_error(hint)
                ),
            )

        # Regression guard: done -> pending/in_progress is blocked unless force=True.
        if not params.force and old_node.status == "done" and new_status != "done":
            hint = (
                f'Send "{matched_title}" with force=True to reopen a done item, '
                "or mode='replace' with force=True to restart the whole list."
            )
            return ToolError(
                message=f'Cannot regress completed todo "{matched_title}" back to {new_status}.',
                brief=hint,
                output=(
                    f'Error: Cannot regress completed todo "{matched_title}" back to {new_status}.'
                    + _hint_error(hint)
                ),
            )

        # Rename collision guard within the matched scope.
        final_title = matched_title
        rename_pair: tuple[str, str] | None = None
        if params.rename_to is not None and params.rename_to != matched_title:
            new_title = params.rename_to
            parent_path = path[:-1]
            siblings = (
                self._node_at_path(todos, parent_path).children
                if parent_path
                else todos
            )
            if any(t.title == new_title for i, t in enumerate(siblings) if i != path[-1]):
                hint = f'Send "{new_title}" to update the existing item instead of renaming.'
                return ToolError(
                    message=f'Cannot rename "{matched_title}" to "{new_title}": title already exists.',
                    brief=hint,
                    output=(
                        f'Error: Cannot rename "{matched_title}" to "{new_title}": '
                        "title already exists in this scope."
                        + _hint_error(hint)
                    ),
                )
            final_title = new_title
            rename_pair = (matched_title, final_title)

        # Notes: None means keep existing; empty string clears notes.
        final_notes = old_node.notes if params.notes is None else (params.notes.strip() or None)

        final_todos = self._update_at_path(
            todos,
            path,
            {
                "title": final_title,
                "status": new_status,
                "notes": final_notes,
            },
        )

        change_parts: list[str] = []
        if params.complete:
            # Mark the matched node and every descendant done (in-place on the
            # fresh copies produced by _update_at_path — safe to mutate).
            node = self._node_at_path(final_todos, path)
            self._mark_subtree_done(node)
            n = self._count_all([node])
            change_parts.append(f"completed with {n} sub-todo{'s' if n != 1 else ''} marked done")
        if "status" in params.model_fields_set:
            change_parts.append(f"status={new_status}")
        if params.notes is not None:
            change_parts.append("notes updated")
        if params.rename_to is not None:
            change_parts.append(f'renamed to "{final_title}"')
        change_summary = ", ".join(change_parts) if change_parts else "no changes"
        summary = f'Updated "{matched_title}" ({change_summary}).'
        message = f'Updated "{matched_title}".'
        return final_todos, summary, message, rename_pair

    @staticmethod
    def _insert_child_at_path(
        items: list[Todo], parent_path: list[int] | None, child: Todo
    ) -> list[Todo]:
        """Return a new tree with ``child`` appended to the children of the node at ``parent_path``.

        ``parent_path`` of ``None`` or ``[]`` appends to the root list.
        """
        if not parent_path:
            return [*items, child]
        new_items = list(items)
        if len(parent_path) == 1:
            parent = items[parent_path[0]]
            new_items[parent_path[0]] = parent.model_copy(update={"children": [*parent.children, child]})
        else:
            child_updates = TodoList._insert_child_at_path(
                items[parent_path[0]].children, parent_path[1:], child
            )
            new_items[parent_path[0]] = items[parent_path[0]].model_copy(
                update={"children": child_updates}
            )
        return new_items

    @staticmethod
    def _collect_titles(items: list[Todo]) -> list[str]:
        """Return every title in the tree, depth-first."""
        titles: list[str] = []
        for t in items:
            titles.append(t.title)
            titles.extend(TodoList._collect_titles(t.children))
        return titles

    @staticmethod
    def _find_path(items: list[Todo], title: str, path: list[int] | None = None) -> list[int] | None:
        """Return the index path to the first todo with the given title, or None."""
        if path is None:
            path = []
        for i, t in enumerate(items):
            if t.title == title:
                return [*path, i]
            child_path = TodoList._find_path(t.children, title, [*path, i])
            if child_path is not None:
                return child_path
        return None

    @staticmethod
    def _node_at_path(items: list[Todo], path: list[int]) -> Todo:
        """Return the node at the given index path."""
        node = items[path[0]]
        for idx in path[1:]:
            node = node.children[idx]
        return node

    @staticmethod
    def _update_at_path(items: list[Todo], path: list[int], updates: dict[str, Any]) -> list[Todo]:
        """Return a new tree with the node at ``path`` updated via ``updates``."""
        new_items = list(items)
        if len(path) == 1:
            new_items[path[0]] = items[path[0]].model_copy(update=updates)
        else:
            child_updates = TodoList._update_at_path(
                items[path[0]].children, path[1:], updates
            )
            new_items[path[0]] = items[path[0]].model_copy(update={"children": child_updates})
        return new_items


# Alias matching the tool name (agent specs reference the lowercase name).
todo_list = TodoList
