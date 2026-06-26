from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, cast, override

import orjson
import rapidfuzz
from kosong.tooling import CallableTool2, ToolReturnValue
from pydantic import BaseModel, Field, field_validator

from kimi_cli.session_state import TodoItemState
from kimi_cli.soul.agent import Runtime
from kimi_cli.tools.display import TodoDisplayBlock, TodoDisplayItem
from kimi_cli import logger

# Fuzzy mode map — maps common synonyms to canonical write modes
_MODE_MAP: dict[str, Literal["overwrite", "append"]] = {
    # overwrite synonyms
    "overwrite": "overwrite",
    "over_write": "overwrite",
    "over-write": "overwrite",
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
}


# Jumbo fuzzy status map — maps common synonyms to canonical values
_STATUS_MAP: dict[str, Literal["pending", "in_progress", "done"]] = {
    # pending
    "pending": "pending",
    "wait": "pending",
    "waiting": "pending",
    "todo": "pending",
    "to_do": "pending",
    "to-do": "pending",
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


class Todo(BaseModel):
    title: str = Field(description="Title", min_length=1, max_length=65536)
    status: Literal["pending", "in_progress", "done"] = Field(description="Status")

    @field_validator("status", mode="before")
    @classmethod
    def _validate_status(cls, v: str) -> str:
        normalized = v.strip().lower().replace("-", "_")
        canonical = _STATUS_MAP.get(normalized)
        if canonical is None:
            raise ValueError(
                f"Invalid status '{v}'. Must be one of: pending, in_progress, done (or a known synonym)."
            )
        return canonical

    @field_validator("title")
    @classmethod
    def _validate_title(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("Title cannot be empty or contain only whitespace")
        return stripped


class Params(BaseModel):
    todos: list[Todo] | Todo | None = Field(
        default=None,
        description="Updated list, a single Todo item, or omit to return current list unchanged.",
    )
    mode: Literal["overwrite", "append"] = Field(
        default="append",
        description="Write mode: 'overwrite' replaces the existing todo list; 'append' merges the provided todos into the existing list.",
    )
    force: bool = Field(
        default=False,
        description="If true, bypass the requirement that all existing todos must be done before overwrite mode replaces the list.",
    )

    @field_validator("mode", mode="before")
    @classmethod
    def _validate_mode(cls, v: str) -> str:
        normalized = v.strip().lower().replace("-", "_")
        canonical = _MODE_MAP.get(normalized)
        if canonical is None:
            raise ValueError(
                f"Invalid mode '{v}'. Must be 'overwrite' or 'append' (or a known synonym)."
            )
        return canonical


class TodoList(CallableTool2[Params]):
    name: str = "TodoList"
    description: str = "Track progress with a todo list."
    params: type[Params] = Params

    def __init__(self, runtime: Runtime) -> None:
        super().__init__()
        self._runtime = runtime

    @override
    async def __call__(self, params: Params) -> ToolReturnValue:
        if params.todos is None:
            return self._read_todos()

        new_todos = [params.todos] if isinstance(params.todos, Todo) else params.todos

        return self._write_todos(new_todos, mode=params.mode, force=params.force)

    # ---- Write mode --------------------------------------------------------

    def _write_todos(self, todos: list[Todo], *, mode: Literal["overwrite", "append"], force: bool) -> ToolReturnValue:
        old_todos = self._load_todos()

        # Validate new todos
        dup = self._find_duplicate_titles(todos)
        if dup:
            error_text = f"Error: Duplicate todo titles found: {dup}"
            return ToolReturnValue(
                is_error=True,
                output=error_text,
                message=error_text,
                display=[],
            )

        if len(todos) > 4096:
            error_text = "Error: Todo list exceeds maximum limit of 4096 items."
            return ToolReturnValue(
                is_error=True,
                output=error_text,
                message=error_text,
                display=[],
            )

        if mode == "overwrite" and not force and old_todos and not all(t.status == "done" for t in old_todos):
            unfinished = "\n".join(t.title for t in old_todos if t.status != "done")
            error_text = (
                "Error: Cannot overwrite todos while old todos are not all done. "
                f"Unfinished:\n{unfinished}"
            )
            return ToolReturnValue(
                is_error=True,
                output=error_text,
                message="Cannot overwrite todos while old todos are not all done.",
                display=[],
            )

        if mode == "append" and old_todos:
            result = self._merge_todos(old_todos, todos)
            if isinstance(result, ToolReturnValue):
                return result
            todos = result

            # Detect regression: done items changed back to pending/in_progress
            old_status_map = {t.title: t.status for t in old_todos}
            regressions: list[str] = []
            for t in todos:
                if old_status_map.get(t.title) == "done" and t.status != "done":
                    regressions.append(t.title)
                    t.status = "done"
            if regressions:
                save_error = self._save_todos(todos)
                if save_error:
                    return ToolReturnValue(
                        is_error=True,
                        output=save_error,
                        message="Failed to save todos.",
                        display=[],
                    )
                items = [TodoDisplayItem(title=todo.title, status=todo.status) for todo in todos]
                reg_msg = (
                    "Error: Cannot regress completed todos back to pending/in_progress: "
                    + ", ".join(regressions)
                )
                return ToolReturnValue(
                    is_error=True,
                    output=reg_msg,
                    message="Cannot regress completed todos.",
                    display=[TodoDisplayBlock(items=items)],
                )

        save_error = self._save_todos(todos)
        if save_error:
            return ToolReturnValue(
                is_error=True,
                output=save_error,
                message="Failed to save todos.",
                display=[],
            )

        items = [TodoDisplayItem(title=todo.title, status=todo.status) for todo in todos]

        active_summary = self._format_todos(todos)

        output_lines: list[str] = ["Todo list updated"]
        if active_summary:
            output_lines.append(active_summary)
        output = "\n".join(output_lines)

        message_lines: list[str] = ["Todo list updated."]
        if mode == "overwrite" and force:
            message_lines.append("Warning: mode='overwrite' with force=True replaces the existing todo list and bypasses merge validation logic.")
        message = "\n".join(message_lines)

        return ToolReturnValue(
            is_error=False,
            output=output,
            message=message,
            display=[TodoDisplayBlock(items=items)],
        )

    @staticmethod
    def _find_duplicate_titles(todos: list[Todo]) -> str | None:
        seen: set[str] = set()
        for t in todos:
            if t.title in seen:
                return t.title
            seen.add(t.title)
        return None

    @staticmethod
    def _format_todos(
        todos: list[Todo],
        *,
        status_filter: tuple[Literal["pending", "in_progress", "done"], ...] = (
            "pending",
            "in_progress",
        ),
        display_status: dict[Literal["pending", "in_progress", "done"], str] | None = None,
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
        return "\n".join(
            f"- [{display_status[t.status]}] {t.title}" for t in selected
        )

    # Score threshold for user-facing title suggestions. rapidfuzz returns a
    # normalized similarity in [0, 100]; 60 catches minor typos while avoiding
    # suggestions that share only a few characters.
    _FUZZY_TITLE_CUTOFF: float = 60.0

    # Warning threshold for append-mode titles that are fuzzy near-matches of
    # existing titles. Higher than the suggestion cutoff because the warning is
    # emitted before any merge logic runs and must avoid flagging genuinely new
    # titles that merely share common words (e.g. "New task" vs "Old task").
    _FUZZY_WARNING_CUTOFF: float = 85.0

    @staticmethod
    def _find_nearest_titles(
        query_titles: list[str],
        candidate_titles: list[str],
        top_k: int = 1,
        *,
        score_cutoff: float | None = None,
        processor: Callable[[str], str] | None = None,
    ) -> dict[str, list[tuple[str, float]]]:
        """Return nearest candidate titles for each query title.

        Uses a lightweight string similarity matcher (rapidfuzz) instead of
        rebuilding a full inverted index on every call. Returns a mapping
        ``query_title -> [(nearest_title, score), ...]``. If no candidate titles
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
        """
        if not candidate_titles or not query_titles:
            return {q: [] for q in query_titles}

        cutoff = score_cutoff if score_cutoff is not None else TodoList._FUZZY_TITLE_CUTOFF

        results: dict[str, list[tuple[str, float]]] = {}
        for query in query_titles:
            matches = rapidfuzz.process.extract(
                query,
                candidate_titles,
                scorer=rapidfuzz.fuzz.token_sort_ratio,
                limit=top_k,
                score_cutoff=cutoff,
                processor=processor,
            )
            results[query] = [
                (str(match[0]), float(match[1])) for match in matches
            ]
        return results

    def _merge_todos(
        self, old_todos: list[Todo], new_todos: list[Todo]
    ) -> ToolReturnValue | list[Todo]:
        if not old_todos:
            return new_todos

        old_title_list = [t.title for t in old_todos]
        old_title_set = set(old_title_list)

        # Detect fuzzy near-matches before any merge logic runs. This prevents
        # LLM-hallucinated title variations from being silently treated as new
        # todos and also avoids partial state changes when some titles match.
        fuzzy_warnings: list[tuple[str, str]] = []
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
                fuzzy_warnings.append((new_todo.title, hits[0][0]))

        if fuzzy_warnings:
            lines = [
                "Warning: The following new todo titles are very similar to existing titles but not identical. "
                "If you meant the same todo, please use the exact existing title; if it is a new todo, please use a clearly different title.",
                "",
            ]
            for new_title, existing_title in fuzzy_warnings:
                lines.append(f'- "{new_title}" looks like existing "{existing_title}"')
            warning_text = "\n".join(lines)
            return ToolReturnValue(
                is_error=True,
                output=warning_text,
                message=warning_text,
                display=[],
            )

        # Empty list: treat as clear operation
        if not new_todos:
            all_old_done = all(t.status == "done" for t in old_todos)
            if not all_old_done:
                unfinished = ", ".join(t.title for t in old_todos if t.status != "done")
                error_text = (
                    "Error: Cannot clear todos while old todos are not all done. "
                    f"Unfinished: {unfinished}"
                )
                return ToolReturnValue(
                    is_error=True,
                    output=error_text,
                    message=error_text,
                    display=[],
                )
            return new_todos
        old_titles = {t.title for t in old_todos}
        new_titles = {t.title for t in new_todos}

        # Partial update: when there's overlap, merge instead of replacing
        if new_titles & old_titles:
            status_map = {t.title: t.status for t in old_todos}
            for new_todo in new_todos:
                status_map[new_todo.title] = new_todo.status
            merged: list[Todo] = []
            seen: set[str] = set()
            for t in old_todos:
                merged.append(Todo(title=t.title, status=cast(Literal["pending", "in_progress", "done"], status_map[t.title])))
                seen.add(t.title)
            for new_todo in new_todos:
                if new_todo.title not in seen:
                    merged.append(new_todo)
                    seen.add(new_todo.title)
            return merged

        unmatched = new_titles - old_titles
        has_new_titles = bool(unmatched)
        all_old_done = all(t.status == "done" for t in old_todos)
        if has_new_titles and not all_old_done:
            unfinished = ", ".join(t.title for t in old_todos if t.status != "done")
            base_output = (
                "Error: Cannot replace with new todos while old todos are not all done. "
                f"Unfinished: {unfinished}"
            )
            base_message = (
                "Cannot replace with new todos while old todos are not all done."
            )

            suggestions: list[str] = []
            if unmatched:
                nearest = self._find_nearest_titles(
                    sorted(unmatched),
                    [t.title for t in old_todos],
                    top_k=1,
                )
                for query in sorted(nearest):
                    hits = nearest[query]
                    if hits and hits[0][1] >= TodoList._FUZZY_TITLE_CUTOFF:
                        suggestions.append(f'- "{query}" -> "{hits[0][0]}"')

            if suggestions:
                suggestion_text = "\nDid you mean:\n" + "\n".join(suggestions)
                base_output += suggestion_text
                base_message += "\nDid you mean:\n" + "\n".join(suggestions)

            return ToolReturnValue(
                is_error=True,
                output=base_output,
                message=base_message,
                display=[],
            )

        if all_old_done:
            # When all old todos are done, replace instead of incremental update
            return new_todos

        if new_titles <= old_titles:
            # Incremental update: update statuses for matching titles, preserve order
            status_map = {t.title: t.status for t in old_todos}
            for new_todo in new_todos:
                status_map[new_todo.title] = new_todo.status
            merged = [
                Todo(
                    title=t.title,
                    status=cast(Literal["pending", "in_progress", "done"], status_map[t.title]),
                )
                for t in old_todos
            ]
            return merged

        return new_todos

    # ---- Read mode ---------------------------------------------------------

    def _read_todos(self) -> ToolReturnValue:
        todos = self._load_todos()
        if not todos:
            return ToolReturnValue(
                is_error=False,
                output="Todo list is empty.",
                message="",
                display=[],
            )
        formatted = self._format_todos(
            todos,
            status_filter=("pending", "in_progress", "done"),
            display_status={
                "pending": "pending",
                "in_progress": "in_progress",
                "done": "done",
            },
        )
        return ToolReturnValue(
            is_error=False,
            output="\n".join(["Current todo list:", formatted]),
            message="",
            display=[],
        )

    # ---- Persistence -------------------------------------------------------

    def _save_todos(self, todos: list[Todo]) -> str | None:
        """Persist todos to the appropriate state file. Returns error message on failure."""
        items = [TodoItemState(title=t.title, status=t.status) for t in todos]

        if self._runtime.role == "root":
            self._save_root_todos(items)
            return None
        else:
            return self._save_subagent_todos(items)

    def _load_todos(self) -> list[Todo]:
        """Load todos from the appropriate state file."""
        if self._runtime.role == "root":
            return self._load_root_todos()
        else:
            return self._load_subagent_todos()

    def _save_root_todos(self, items: list[TodoItemState]) -> None:
        session = self._runtime.session
        session.state.todos = items
        session.save_state()

    def _load_root_todos(self) -> list[Todo]:
        from kimi_cli.session_state import load_session_state

        session = self._runtime.session
        fresh = load_session_state(session.dir)
        session.state.todos = fresh.todos
        result: list[Todo] = []
        for t in fresh.todos:
            try:
                result.append(Todo(title=t.title, status=cast(Literal["pending", "in_progress", "done"], t.status)))
            except Exception:
                logger.warning("Skipping malformed todo item in root state: {t}", t=t)
        return result

    def _save_subagent_todos(self, items: list[TodoItemState]) -> str | None:
        state_file = self._subagent_state_file()
        if state_file is None:
            return "Error: Unable to save subagent todos: state file is not available."
        data = self._read_subagent_state(state_file)
        data["todos"] = [item.model_dump() for item in items]
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
                result.append(Todo(**item))
            except Exception:
                logger.warning("Skipping malformed todo item in subagent state: {item}", item=item)
        return result

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
