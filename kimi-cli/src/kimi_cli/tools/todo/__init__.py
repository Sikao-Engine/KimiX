"""Todo list tracking tool."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast, override

import orjson
import rapidfuzz
from kosong.tooling import CallableTool2, ToolReturnValue
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from kimi_cli import logger
from kimi_cli.session_state import TodoItemState, TodoPriority, TodoStatus
from kimi_cli.soul.agent import Runtime
from kimi_cli.tools.display import TodoDisplayBlock, TodoDisplayItem
from kimi_cli.tools.utils import repair_json_string


# Fuzzy mode map — maps common synonyms to canonical write modes
_MODE_MAP: dict[str, Literal["overwrite", "append", "force_overwrite"]] = {
    # overwrite synonyms
    "overwrite": "overwrite",
    "over_write": "overwrite",
    "replace": "overwrite",
    "write": "overwrite",
    "set": "overwrite",
    "put": "overwrite",
    "truncate": "overwrite",
    "rewrite": "overwrite",
    "new": "overwrite",
    # append synonyms
    "append": "append",
    "add": "append",
    "merge": "append",
    "update": "append",
    "patch": "append",
    "extend": "append",
    "concat": "append",
    "concatenate": "append",
    # force_overwrite synonyms
    "force_overwrite": "force_overwrite",
    "forceoverwrite": "force_overwrite",
    "force overwrite": "force_overwrite",
    "force-write": "force_overwrite",
    "force_write": "force_overwrite",
    "force": "force_overwrite",
    "forced": "force_overwrite",
    "forcereplace": "force_overwrite",
    "force_replace": "force_overwrite",
    "force replace": "force_overwrite",
}


# Fuzzy status map — maps common synonyms to canonical values
_STATUS_MAP: dict[str, TodoStatus] = {
    # pending
    "pending": "pending",
    "wait": "pending",
    "waiting": "pending",
    "todo": "pending",
    "to_do": "pending",
    "not_started": "pending",
    "notstarted": "pending",
    "not started": "pending",
    "backlog": "pending",
    "queued": "pending",
    "unstarted": "pending",
    "open": "pending",
    "new": "pending",
    "planned": "pending",
    "scheduled": "pending",
    "upcoming": "pending",
    "ready": "pending",
    "idle": "pending",
    # in_progress
    "in_progress": "in_progress",
    "inprogress": "in_progress",
    "in progress": "in_progress",
    "started": "in_progress",
    "start": "in_progress",
    "active": "in_progress",
    "ongoing": "in_progress",
    "working": "in_progress",
    "work": "in_progress",
    "doing": "in_progress",
    "underway": "in_progress",
    "under way": "in_progress",
    "wip": "in_progress",
    "current": "in_progress",
    "progress": "in_progress",
    "busy": "in_progress",
    "developing": "in_progress",
    "partial": "in_progress",
    "partially_done": "in_progress",
    "partially done": "in_progress",
    # done
    "done": "done",
    "completed": "done",
    "complete": "done",
    "finished": "done",
    "finish": "done",
    "resolved": "done",
    "closed": "done",
    "close": "done",
    "verified": "done",
    "approved": "done",
    "ok": "done",
    "yes": "done",
    "success": "done",
    "successful": "done",
    "passed": "done",
    "fixed": "done",
    "shipped": "done",
    "delivered": "done",
    "archived": "done",
    "merged": "done",
    "deployed": "done",
    "released": "done",
    "published": "done",
    "live": "done",
    "accepted": "done",
    "confirmed": "done",
    "finalized": "done",
    "finalised": "done",
    "ready_for_review": "done",
    "ready for review": "done",
}


# Priority synonyms
_PRIORITY_MAP: dict[str, TodoPriority] = {
    "low": "low",
    "l": "low",
    "medium": "medium",
    "med": "medium",
    "m": "medium",
    "mid": "medium",
    "high": "high",
    "h": "high",
    "urgent": "high",
}


def _canonical_status(v: Any) -> TodoStatus:
    """Normalize a status value to its canonical form."""
    if not isinstance(v, str):
        raise ValueError(
            f"Invalid status '{v}'. Must be one of: pending, in_progress, done (or a known synonym)."
        )
    normalized = v.strip().lower().replace("-", "_")
    canonical = _STATUS_MAP.get(normalized)
    if canonical is None:
        raise ValueError(
            f"Invalid status '{v}'. Must be one of: pending, in_progress, done (or a known synonym)."
        )
    return canonical


@dataclass(frozen=True)
class _FuzzyResult:
    """Typed wrapper for rapidfuzz match results.

    rapidfuzz 3.x+ returns ``Result`` objects with ``choice``/``score``/``index``
    attributes, while older versions return plain 3-tuples. This dataclass gives
    us attribute access regardless of the underlying format.
    """

    choice: str
    score: float
    index: int


class Todo(BaseModel):
    title: str = Field(description="Title", min_length=1, max_length=65536)
    status: TodoStatus = Field(description="Status")
    priority: TodoPriority | None = Field(
        default=None,
        description="Optional priority: low, medium, or high.",
    )
    tags: list[str] | None = Field(
        default=None,
        description="Optional list of tags.",
    )
    notes: str | None = Field(
        default=None,
        description="Optional notes.",
        max_length=65536,
    )
    created_at: float | None = Field(
        default=None,
        description="Optional creation timestamp (Unix epoch).",
    )
    updated_at: float | None = Field(
        default=None,
        description="Optional last-update timestamp (Unix epoch).",
    )

    @field_validator("status", mode="before")
    @classmethod
    def _validate_status(cls, v: Any) -> str:
        return _canonical_status(v)

    @field_validator("priority", mode="before")
    @classmethod
    def _validate_priority(cls, v: Any) -> TodoPriority | None:
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError(
                "Invalid priority. Must be one of: low, medium, high (or a known synonym)."
            )
        normalized = v.strip().lower()
        canonical = _PRIORITY_MAP.get(normalized)
        if canonical is None:
            raise ValueError(
                "Invalid priority. Must be one of: low, medium, high (or a known synonym)."
            )
        return canonical

    @field_validator("tags", mode="before")
    @classmethod
    def _validate_tags(cls, v: Any) -> list[str] | None:
        if v is None:
            return None
        if not isinstance(v, list):
            raise ValueError("tags must be a list of strings")
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in v:
            if not isinstance(item, str):
                raise ValueError("tags must be a list of strings")
            stripped = item.strip()
            if not stripped:
                continue
            if stripped not in seen:
                cleaned.append(stripped)
                seen.add(stripped)
        return cleaned or None

    @field_validator("notes")
    @classmethod
    def _validate_notes(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        return stripped if stripped else None

    @field_validator("title")
    @classmethod
    def _validate_title(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("Title cannot be empty or contain only whitespace")
        return stripped


class Params(BaseModel):
    model_config = ConfigDict(extra="forbid")

    todos: list[Todo] | Todo | None = Field(
        default=None,
        description="Updated list, a single Todo item, or omit to return current list unchanged.",
    )
    mode: Literal["overwrite", "append", "force_overwrite"] = Field(
        default="overwrite",
        description=(
            "Write mode: 'overwrite' safely replaces the existing todo list only when all old todos are done; "
            "'append' merges the provided todos into the existing list (existing titles are updated, new titles are appended); "
            "'force_overwrite' replaces the existing todo list unconditionally."
        ),
    )
    delete: list[str] | str | None = Field(
        default=None,
        description="Remove todos by exact title. Accepts a single title or a list of titles.",
    )
    reorder: list[str] | None = Field(
        default=None,
        description="Reorder existing todos by listing every title in the desired order.",
    )
    status_filter: list[TodoStatus] | TodoStatus | None = Field(
        default=None,
        description="Filter read output by status(es).",
    )
    search: str | None = Field(
        default=None,
        description="Fuzzy search todo titles when reading.",
    )
    limit: int | None = Field(
        default=None,
        ge=0,
        description="Limit the number of todos returned when reading.",
    )
    offset: int | None = Field(
        default=None,
        ge=0,
        description="Offset into the read results.",
    )
    mark_all: TodoStatus | None = Field(
        default=None,
        description="Transition all existing todos to the given status.",
    )
    mark_matching: dict[str, TodoStatus] | None = Field(
        default=None,
        description="Transition todos whose titles contain each key (case-insensitive) to the corresponding status.",
    )
    archive_done: bool = Field(
        default=False,
        description="Move completed todos to a separate archive list.",
    )

    @field_validator("mode", mode="before")
    @classmethod
    def _validate_mode(cls, v: Any) -> str:
        if not isinstance(v, str):
            raise ValueError(
                "Invalid mode. Must be 'overwrite', 'append', or 'force_overwrite' (or a known synonym)."
            )
        normalized = v.strip().lower().replace("-", "_")
        canonical = _MODE_MAP.get(normalized)
        if canonical is None:
            raise ValueError(
                f"Invalid mode '{v}'. Must be 'overwrite', 'append', or 'force_overwrite' (or a known synonym)."
            )
        return canonical

    @field_validator("todos", mode="before")
    @classmethod
    def _validate_todos(cls, v: Any) -> list[Todo] | Todo | None:
        if v is None:
            return None
        if isinstance(v, Todo):
            return v
        if isinstance(v, str):
            parsed = repair_json_string(v)
            if parsed is None:
                raise ValueError("todos must be a list of todos, a single todo dict/object, or None")
            v = parsed
        if isinstance(v, dict):
            try:
                return Todo.model_validate(v)
            except ValidationError as exc:
                msg = _first_pydantic_message(exc)
                raise ValueError(f"Invalid todo: {msg}") from exc
        if isinstance(v, list):
            out: list[Todo] = []
            for idx, item in enumerate(v):
                if isinstance(item, Todo):
                    out.append(item)
                    continue
                if isinstance(item, dict):
                    try:
                        out.append(Todo.model_validate(item))
                    except ValidationError as exc:
                        msg = _first_pydantic_message(exc)
                        raise ValueError(f"Invalid todo at index {idx}: {msg}") from exc
                    continue
                raise ValueError(
                    f"Invalid todo at index {idx}: expected a dict or Todo, got {type(item).__name__}"
                )
            return out
        raise ValueError("todos must be a list of todos, a single todo dict/object, or None")

    @field_validator("delete", mode="before")
    @classmethod
    def _validate_delete(cls, v: Any) -> list[str] | None:
        if v is None:
            return None
        if isinstance(v, str):
            stripped = v.strip()
            if not stripped:
                raise ValueError("delete title cannot be empty or whitespace only")
            return [stripped]
        if isinstance(v, list):
            out: list[str] = []
            for idx, item in enumerate(v):
                if not isinstance(item, str):
                    raise ValueError(
                        f"delete item at index {idx} must be a string, got {type(item).__name__}"
                    )
                stripped = item.strip()
                if not stripped:
                    raise ValueError(
                        f"delete title at index {idx} cannot be empty or whitespace only"
                    )
                out.append(stripped)
            return out
        raise ValueError("delete must be a string or a list of strings")

    @field_validator("reorder", mode="before")
    @classmethod
    def _validate_reorder(cls, v: Any) -> list[str] | None:
        if v is None:
            return None
        if not isinstance(v, list):
            raise ValueError("reorder must be a list of titles")
        out: list[str] = []
        for idx, item in enumerate(v):
            if not isinstance(item, str):
                raise ValueError(
                    f"reorder item at index {idx} must be a string, got {type(item).__name__}"
                )
            stripped = item.strip()
            if not stripped:
                raise ValueError(f"reorder title at index {idx} cannot be empty or whitespace only")
            out.append(stripped)
        return out

    @field_validator("status_filter", mode="before")
    @classmethod
    def _validate_status_filter(cls, v: Any) -> list[TodoStatus] | None:
        if v is None:
            return None
        if isinstance(v, str):
            return [_canonical_status(v)]
        if isinstance(v, list):
            return [_canonical_status(x) for x in v]
        raise ValueError("status_filter must be a status string or a list of status strings")

    @field_validator("mark_all", mode="before")
    @classmethod
    def _validate_mark_all(cls, v: Any) -> TodoStatus | None:
        if v is None:
            return None
        return _canonical_status(v)

    @field_validator("mark_matching", mode="before")
    @classmethod
    def _validate_mark_matching(cls, v: Any) -> dict[str, TodoStatus] | None:
        if v is None:
            return None
        if not isinstance(v, dict):
            raise ValueError("mark_matching must be a dict mapping title substrings to statuses")
        out: dict[str, TodoStatus] = {}
        for key, val in v.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("mark_matching keys must be non-empty strings")
            out[key.strip()] = _canonical_status(val)
        return out


def _first_pydantic_message(exc: ValidationError) -> str:
    """Return the first human-readable message from a Pydantic ValidationError."""
    errors = exc.errors()
    if errors:
        return errors[0].get("msg", str(exc))
    return str(exc)


@dataclass
class MergeResult:
    """Result of merging old and new todo lists.

    Attributes:
        todos: Merged todo list on success (``None`` means error).
        error: Error value set when the merge cannot proceed.
        warnings: Non-blocking warnings accumulated during the merge.
    """

    todos: list[Todo] | None = None
    error: ToolReturnValue | None = None
    warnings: list[str] = field(default_factory=list)


class TodoList(CallableTool2[Params]):
    name: str = "TodoList"
    description: str = "Track progress with a todo list."
    params: type[Params] = Params

    def __init__(self, runtime: Runtime) -> None:
        super().__init__()
        self._runtime = runtime

    @override
    async def __call__(self, params: Params) -> ToolReturnValue:
        write_requested = (
            params.todos is not None
            or params.delete is not None
            or params.reorder is not None
            or params.mark_all is not None
            or params.mark_matching is not None
            or params.archive_done
        )
        if not write_requested:
            return self._read_todos(params)

        return self._write_todos(params.todos, params)

    # ---- Write mode --------------------------------------------------------

    def _write_todos(
        self,
        raw_todos: list[Todo] | Todo | None,
        params: Params,
    ) -> ToolReturnValue:
        """Validate, merge, and persist todos, saving exactly once on success."""
        # Normalize the todos input while preserving the distinction between
        # "not provided" (None) and "explicitly empty" ([]).
        if raw_todos is None:
            new_todos: list[Todo] = []
            todos_provided = False
        elif isinstance(raw_todos, Todo):
            new_todos = [raw_todos]
            todos_provided = True
        else:
            new_todos = raw_todos
            todos_provided = True

        # 1. Validate new inputs
        if todos_provided:
            duplicates = self._find_duplicate_titles(new_todos)
            if duplicates:
                return ToolReturnValue(
                    is_error=True,
                    output=f"Error: Duplicate todo titles found: {duplicates}",
                    message=f"Duplicate todo titles found: {duplicates}",
                    display=[],
                )

            if len(new_todos) > 4096:
                return ToolReturnValue(
                    is_error=True,
                    output="Error: Todo list exceeds maximum limit of 4096 items.",
                    message="Todo list exceeds maximum limit of 4096 items.",
                    display=[],
                )

        # 2. Load existing state
        old_todos = self._load_todos()
        old_archived = self._load_archived_todos()

        # 3. Branch on write mode
        warnings: list[str] = []
        if params.mode == "force_overwrite":
            final_todos = self._with_timestamps(new_todos)
        elif params.mode == "overwrite":
            if old_todos and not all(t.status == "done" for t in old_todos):
                unfinished = "\n".join(t.title for t in old_todos if t.status != "done")
                return ToolReturnValue(
                    is_error=True,
                    output=(
                        "Error: Cannot overwrite todos while old todos are not all done. "
                        "Use mode='force_overwrite' if you really want to discard unfinished work.\n"
                        f"Unfinished:\n{unfinished}"
                    ),
                    message="Cannot overwrite todos while old todos are not all done.",
                    display=[],
                )
            final_todos = self._with_timestamps(new_todos)
        else:  # append
            if not todos_provided and not new_todos:
                final_todos = list(old_todos)
            else:
                result = self._merge_todos(old_todos, new_todos, todos_provided)
                if result.error is not None:
                    return result.error
                final_todos = result.todos or []
                warnings.extend(result.warnings)

        # 4. Apply explicit deletions
        if params.delete:
            final_todos, delete_warnings = self._apply_delete(final_todos, params.delete)
            warnings.extend(delete_warnings)

        # 5. Bulk status transitions
        if params.mark_all is not None:
            final_todos = [self._update_status(t, params.mark_all) for t in final_todos]
        if params.mark_matching is not None:
            final_todos = self._apply_mark_matching(final_todos, params.mark_matching)

        # 6. Regression detection (before archive/reorder so errors leave state unchanged)
        if params.mode != "force_overwrite" and old_todos:
            final_todos, regressions = self._check_regressions(old_todos, final_todos)
            if regressions:
                return ToolReturnValue(
                    is_error=True,
                    output=(
                        "Error: Cannot regress completed todos back to pending/in_progress: "
                        + ", ".join(regressions)
                    ),
                    message="Cannot regress completed todos.",
                    display=[self._build_display_block(final_todos)],
                )

        # 7. Reorder
        if params.reorder is not None:
            reorder_result, reorder_error = self._apply_reorder(final_todos, params.reorder)
            if reorder_error is not None:
                return reorder_error
            final_todos = reorder_result

        # 8. Archive completed todos
        archived = list(old_archived)
        if params.archive_done:
            active, done = self._split_by_status(final_todos)
            final_todos = active
            archived.extend(self._item_states(done))

        # 9. Persist exactly once
        save_error = self._save_todos(final_todos, archived)
        if save_error:
            return ToolReturnValue(
                is_error=True,
                output=save_error,
                message="Failed to save todos.",
                display=[],
            )

        # 10. Build response
        return self._build_success_response(
            final_todos,
            params=params,
            warnings=warnings,
            had_old_todos=bool(old_todos),
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
            "in_progress",
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
        return "\n".join(f"- [{display_status[t.status]}] {t.title}" for t in selected)

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
            results[query] = [
                _FuzzyResult(
                    choice=str(match.choice if hasattr(match, "choice") else match[0]),
                    score=float(match.score if hasattr(match, "score") else match[1]),
                    index=int(
                        match.index
                        if hasattr(match, "choice")
                        else match[2]
                        if len(match) > 2
                        else -1
                    ),
                )
                for match in matches
            ]
        return results

    def _merge_todos(
        self,
        old_todos: list[Todo],
        new_todos: list[Todo],
        clear_requested: bool = False,
    ) -> MergeResult:
        """Merge ``new_todos`` into ``old_todos`` using append/update semantics.

        * Existing titles update status (and any provided metadata) in place.
        * Brand-new titles are appended to the end.
        * An explicitly empty ``new_todos`` (``clear_requested=True``) clears the
          list only when all old todos are done.
        * Fuzzy near-matches are reported as non-blocking warnings.
        """
        if not old_todos:
            return MergeResult(todos=self._with_timestamps(new_todos))

        old_title_list = [t.title for t in old_todos]
        old_title_set = set(old_title_list)

        warnings = self._detect_fuzzy_warnings(new_todos, old_title_set, old_title_list)

        # Explicitly empty list: treat as clear operation
        if clear_requested and not new_todos:
            if not all(t.status == "done" for t in old_todos):
                unfinished = ", ".join(t.title for t in old_todos if t.status != "done")
                return MergeResult(
                    error=ToolReturnValue(
                        is_error=True,
                        output=(
                            "Error: Cannot clear todos while old todos are not all done. "
                            f"Unfinished: {unfinished}"
                        ),
                        message="Cannot clear todos while old todos are not all done.",
                        display=[],
                    )
                )
            return MergeResult(todos=[])

        merged = self._merge_by_title_update(old_todos, new_todos)
        return MergeResult(todos=merged, warnings=warnings)

    def _detect_fuzzy_warnings(
        self,
        new_todos: list[Todo],
        old_title_set: set[str],
        old_title_list: list[str],
    ) -> list[str]:
        """Return non-blocking warnings for new titles that look like existing ones."""
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

    def _merge_by_title_update(self, old_todos: list[Todo], new_todos: list[Todo]) -> list[Todo]:
        """Update existing titles and append brand-new ones, preserving metadata."""
        new_by_title = {t.title: t for t in new_todos}
        now = time.time()
        merged: list[Todo] = []
        seen: set[str] = set()

        for old in old_todos:
            new = new_by_title.get(old.title)
            if new is not None:
                merged.append(self._merge_one(old, new, now))
            else:
                merged.append(old)
            seen.add(old.title)

        for new in new_todos:
            if new.title not in seen:
                merged.append(self._with_timestamp(new, now))
                seen.add(new.title)

        return merged

    @staticmethod
    def _merge_one(old: Todo, new: Todo, now: float) -> Todo:
        """Produce an updated todo preserving old metadata when new omits it."""
        return Todo(
            title=old.title,
            status=new.status,
            priority=new.priority if new.priority is not None else old.priority,
            tags=new.tags if new.tags is not None else old.tags,
            notes=new.notes if new.notes is not None else old.notes,
            created_at=old.created_at,
            updated_at=now,
        )

    @staticmethod
    def _with_timestamp(todo: Todo, now: float) -> Todo:
        """Return a copy of ``todo`` with timestamps initialized if missing."""
        return Todo(
            title=todo.title,
            status=todo.status,
            priority=todo.priority,
            tags=todo.tags,
            notes=todo.notes,
            created_at=todo.created_at if todo.created_at is not None else now,
            updated_at=now,
        )

    def _with_timestamps(self, todos: list[Todo]) -> list[Todo]:
        now = time.time()
        return [self._with_timestamp(t, now) for t in todos]

    @staticmethod
    def _apply_delete(todos: list[Todo], delete_titles: list[str]) -> tuple[list[Todo], list[str]]:
        """Remove exact titles and return warnings for titles that did not exist."""
        existing = {t.title for t in todos}
        remaining = [t for t in todos if t.title not in delete_titles]
        not_found = sorted(t for t in delete_titles if t not in existing)
        warnings: list[str] = []
        if not_found:
            warnings.append(f"Note: delete titles not found: {', '.join(not_found)}")
        return remaining, warnings

    @staticmethod
    def _update_status(todo: Todo, status: TodoStatus) -> Todo:
        return Todo(
            title=todo.title,
            status=status,
            priority=todo.priority,
            tags=todo.tags,
            notes=todo.notes,
            created_at=todo.created_at,
            updated_at=time.time(),
        )

    def _apply_mark_matching(
        self, todos: list[Todo], mark_matching: dict[str, TodoStatus]
    ) -> list[Todo]:
        result = list(todos)
        for pattern, status in mark_matching.items():
            pattern_lower = pattern.lower()
            result = [
                self._update_status(t, status) if pattern_lower in t.title.lower() else t
                for t in result
            ]
        return result

    @staticmethod
    def _check_regressions(
        old_todos: list[Todo], final_todos: list[Todo]
    ) -> tuple[list[Todo], list[str]]:
        """Detect done todos being moved back to pending/in_progress.

        Returns the final list with regressed items clamped back to ``done``,
        plus the list of regressed titles.
        """
        old_status_map = {t.title: t.status for t in old_todos}
        regressions: list[str] = []
        clamped: list[Todo] = []
        for t in final_todos:
            if old_status_map.get(t.title) == "done" and t.status != "done":
                regressions.append(t.title)
                clamped.append(TodoList._update_status(t, "done"))
            else:
                clamped.append(t)
        return clamped, regressions

    @staticmethod
    def _apply_reorder(
        todos: list[Todo], reorder: list[str]
    ) -> tuple[list[Todo] | None, ToolReturnValue | None]:
        """Validate and apply an explicit ordering to the todo list."""
        current_titles = [t.title for t in todos]
        current_set = set(current_titles)
        reorder_set = set(reorder)

        if current_set != reorder_set:
            missing = sorted(current_set - reorder_set)
            extra = sorted(reorder_set - current_set)
            parts: list[str] = []
            if missing:
                parts.append(f"missing from reorder: {', '.join(missing)}")
            if extra:
                parts.append(f"extra in reorder: {', '.join(extra)}")
            error_text = "Error: reorder list does not match existing todos. " + "; ".join(parts)
            return None, ToolReturnValue(
                is_error=True,
                output=error_text,
                message="reorder list does not match existing todos.",
                display=[],
            )

        if len(reorder) != len(reorder_set):
            return None, ToolReturnValue(
                is_error=True,
                output="Error: reorder list contains duplicate titles.",
                message="reorder list contains duplicate titles.",
                display=[],
            )

        order_index = {title: idx for idx, title in enumerate(reorder)}
        return sorted(todos, key=lambda t: order_index[t.title]), None

    @staticmethod
    def _split_by_status(todos: list[Todo]) -> tuple[list[Todo], list[Todo]]:
        active = [t for t in todos if t.status != "done"]
        done = [t for t in todos if t.status == "done"]
        return active, done

    def _build_success_response(
        self,
        todos: list[Todo],
        *,
        params: Params,
        warnings: list[str],
        had_old_todos: bool,
    ) -> ToolReturnValue:
        display_block = self._build_display_block(todos)
        active_summary = self._format_todos(todos)

        output_lines: list[str] = ["Todo list updated"]
        if active_summary:
            output_lines.append(active_summary)
        output = "\n".join(output_lines)

        message_lines: list[str] = ["Todo list updated."]
        if params.mode == "force_overwrite" and had_old_todos:
            message_lines.append(
                "Warning: mode='force_overwrite' replaces the existing todo list and bypasses merge validation logic."
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
    def _build_display_block(todos: list[Todo]) -> TodoDisplayBlock:
        return TodoDisplayBlock(
            items=[
                TodoDisplayItem(
                    title=todo.title,
                    status=todo.status,
                    priority=todo.priority,
                    tags=todo.tags,
                    notes=todo.notes,
                    created_at=todo.created_at,
                    updated_at=todo.updated_at,
                )
                for todo in todos
            ]
        )

    # ---- Read mode ---------------------------------------------------------

    def _read_todos(self, params: Params) -> ToolReturnValue:
        todos = self._load_todos()
        archived = self._load_archived_todos()

        filtered = list(todos)
        if params.status_filter:
            allowed = set(params.status_filter)
            filtered = [t for t in filtered if t.status in allowed]

        if params.search:
            title_list = [t.title for t in filtered]
            if title_list:
                matches = self._find_nearest_titles(
                    [params.search],
                    title_list,
                    top_k=len(title_list),
                    score_cutoff=self._FUZZY_TITLE_CUTOFF,
                    processor=str.lower,
                    scorer=rapidfuzz.fuzz.partial_ratio,
                )
                matched_titles = {m.choice for m in matches.get(params.search, [])}
                filtered = [t for t in filtered if t.title in matched_titles]
            else:
                filtered = []

        if params.offset:
            filtered = filtered[params.offset :]
        if params.limit is not None:
            filtered = filtered[: params.limit]

        if not filtered and not todos:
            return ToolReturnValue(
                is_error=False,
                output="Todo list is empty.",
                message="Todo list is empty.",
                display=[],
            )

        formatted = self._format_todos(
            filtered,
            status_filter=("pending", "in_progress", "done"),
            display_status={
                "pending": "pending",
                "in_progress": "in_progress",
                "done": "done",
            },
        )
        output_lines = ["Current todo list:"]
        if formatted:
            output_lines.append(formatted)
        if archived:
            output_lines.append(f"Archived: {len(archived)} completed todo(s).")
        return ToolReturnValue(
            is_error=False,
            output="\n".join(output_lines),
            message="Current todo list displayed.",
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
        self._write_subagent_state(state_file, data)
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
        return [TodoItemState(**todo.model_dump()) for todo in todos]

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
