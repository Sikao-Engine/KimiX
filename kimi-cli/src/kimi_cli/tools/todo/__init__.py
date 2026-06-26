from pathlib import Path
from typing import Any, Literal, cast, override

import orjson
from kosong.tooling import CallableTool2, ToolReturnValue
from pydantic import BaseModel, Field, field_validator

from kimi_cli.session_state import TodoItemState
from kimi_cli.soul.agent import Runtime
from kimi_cli.tools.display import TodoDisplayBlock, TodoDisplayItem
from kimi_cli import logger

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
    force_replace: bool = Field(
        default=False,
        description="If true, directly replace the old todo-list without validation.",
    )


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

        return self._write_todos(new_todos, force_replace=params.force_replace)

    # ---- Write mode --------------------------------------------------------

    def _write_todos(self, todos: list[Todo], *, force_replace: bool) -> ToolReturnValue:
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

        if not force_replace and old_todos:
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
        if force_replace:
            message_lines.append("Warning: force_replace=True bypasses all validation logic.")
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

    @staticmethod
    def _find_nearest_titles(
        query_titles: list[str],
        candidate_titles: list[str],
        top_k: int = 1,
    ) -> dict[str, list[tuple[str, float]]]:
        """Return nearest candidate titles for each query title using BM25 retrieval.

        Returns a mapping ``query_title -> [(nearest_title, score), ...]``.  If no
        candidate titles exist or BM25 returns no hits for a query, the list is
        empty.
        """
        from kimix.retrieval import InvertedIndex, NgramTokenizer, Searcher

        if not candidate_titles or not query_titles:
            return {q: [] for q in query_titles}

        tokenizer = NgramTokenizer(n=2)
        index = InvertedIndex()
        for doc_id, title in enumerate(candidate_titles):
            index.add_document(doc_id, tokenizer.tokenize(title))
        index.finalize(stop_threshold=1.0)
        searcher = Searcher(index, tokenizer=tokenizer)

        results: dict[str, list[tuple[str, float]]] = {}
        for query in query_titles:
            hits = searcher.search(query, top_k=top_k)
            results[query] = [
                (candidate_titles[doc_id], float(score)) for doc_id, score in hits
            ]
        return results

    def _merge_todos(
        self, old_todos: list[Todo], new_todos: list[Todo]
    ) -> ToolReturnValue | list[Todo]:
        if not old_todos:
            return new_todos

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
                    if hits and hits[0][1] > 0:
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
