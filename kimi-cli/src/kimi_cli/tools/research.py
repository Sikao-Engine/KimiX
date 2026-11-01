"""The ``research`` tool: fresh, anonymous, read-only research child sessions.

Every call spawns a throwaway ``researcher`` child session — the read-only
``agent_readonly.json`` manifest and the ``SystemPromptType.Reader`` system
prompt — prompts it with the caller's query, and hands the child's final
assistant text back to the main agent directly.  Unlike ``subagent`` (which
returns only an output-file path), the research text itself is the tool result.
The child session is anonymous scratch space: it is always closed and its
bookkeeping dropped when the run ends, on success, failure and cancellation.

With ``run_in_background=true`` the run is registered in the same per-session
background-task infrastructure as ``bash``/``pwsh`` under a ``research_<n>``
task id, so the generic ``job_output`` tool can wait for, read, and kill it
without any tool-specific support.

``kimi_cli`` must not import ``kimix``/``kimi_agent_sdk`` at module load
(``kimix`` depends on ``kimi_cli``, and the SDK imports ``kimi_cli.app``,
which would form a cycle); every ``kimix.*`` symbol is imported lazily inside
the function that uses it, exactly like ``kimi_cli.tools.subagent``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from kimi_cli.session import Session
from kimi_cli.tools.background.utils import (
    BackgroundStream,
    add_task,
    get_all_tasks,
)
from kimi_cli.tools.subagent import (
    _AgentConversationCollector,
    _cli_session_id,
    _forget_child_session_record,
    _register_child_session,
    _session_work_dir,
)
from kosong.tooling import CallableTool2, ToolError, ToolOk, ToolReturnValue

if TYPE_CHECKING:
    import queue


def _error_text(exc: BaseException) -> str:
    """A never-empty description of *exc* (``str(exc)`` is empty for many)."""
    text = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {text}" if text else name


class ResearchParams(BaseModel):
    model_config = {"populate_by_name": True}

    query: str = Field(
        description=(
            "The self-contained research question or topic. The researcher "
            "runs in its own context and cannot see this conversation, so "
            "include all background it needs."
        ),
    )
    run_in_background: bool = Field(
        default=False,
        description=(
            "Run in the background and return a task id immediately "
            "(read with job_output; stop with job_output action='kill'). "
            "No timeout applies."
        ),
    )


class Research(CallableTool2):
    name: str = "research"
    description: str = "Use for deep research, in-depth investigation, or any other read-only task."
    params: type[ResearchParams] = ResearchParams

    def __init__(self, session: Session):
        super().__init__()
        self._session = session

    async def __call__(self, params: ResearchParams) -> ToolReturnValue:
        if not params.query.strip():
            return ToolError(
                output="",
                message="query must not be empty.",
                brief="Empty query",
            )
        if params.run_in_background:
            return await self._start_background(params.query)
        try:
            child, child_id = await self._create_child_session()
        except Exception as exc:
            return ToolError(
                output="",
                message=_error_text(exc),
                brief="Failed to create research session",
            )
        text, err = await self._research(child, child_id, params.query)
        if err is not None:
            return ToolError(
                output=text,
                message=err,
                brief="Research task failed",
            )
        return ToolOk(output=text, brief="Research completed")

    # ------------------------------------------------------------------ #
    # Child session
    # ------------------------------------------------------------------ #

    async def _create_child_session(self) -> tuple[Any, str]:
        """Create the fresh, anonymous, read-only researcher child session.

        Mirrors the ``subagent`` provider fallback: a ``researcher``-role
        sub-provider wins, then the parent's ``provider_dict``, then
        ``base._default_provider``.  The manifest comes from the phase-1
        role mapping (``_ROLE_DEFAULT_AGENT_FILES['researcher']``), the
        system prompt is ``SystemPromptType.Reader``, and the child inherits
        the parent's work dir and ``chat_provider``.
        """
        import kimix.base as base
        from kimix.utils import _create_session_async
        from kimix.utils.config import _ROLE_DEFAULT_AGENT_FILES
        from kimix.utils.system_prompt import SystemPromptType

        custom_config = getattr(self._session, "custom_config", None) or {}
        chat_provider = custom_config.get("chat_provider")
        provider = custom_config.get(
            "provider_dict", base._default_provider
        )
        provider = base.get_default_sub_provider("researcher") or provider
        agent_file = base._default_agent_file_dir / _ROLE_DEFAULT_AGENT_FILES.get(
            "researcher", "agent_readonly.json"
        )
        child = await _create_session_async(
            work_dir=_session_work_dir(self._session),
            agent_file=agent_file,
            agent_type=SystemPromptType.Reader,
            provider_dict=provider,
            chat_provider=chat_provider,
            resume=True,
            anonymous=True,
        )
        child_id = _cli_session_id(child)
        _register_child_session(_cli_session_id(self._session), child_id)
        return child, child_id

    async def _research(self, child: Any, child_id: str, query: str) -> tuple[str, str | None]:
        """Prompt the child and return its final assistant text plus an error.

        The child session is closed and every bookkeeping record of it is
        dropped in a ``finally`` — on success, prompt failure, and caller
        cancellation — so a finished research run never leaks sessions.
        ``CancelledError`` propagates so an interrupted foreground call (or
        a killed background run, which converts it below) stays interruptible.
        """
        import kimix.utils as utils
        from kimix.utils import close_session_async

        collector = _AgentConversationCollector()
        collector.finalize_user_turn(query)

        def output_function(text: str, msg_type: Any) -> None:
            if text:
                collector.consume(text, msg_type)

        err: str | None = None
        text = ""
        try:
            try:
                await utils.prompt_async(
                    prompt_str=query,
                    session=child,
                    output_function=output_function,
                    info_print=False,
                    merge_wire_messages=True,
                    format_output=True,
                    # The outcome is reported by this tool, so the child must
                    # not swallow its own failure (same rationale as
                    # ``subagent``).
                    ensure_todo_finished=False,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                err = _error_text(exc)
            text = collector.finalize_assistant_turn()
            if not text:
                text = "(no text output)"
        finally:
            # Close and forget on success, prompt failure AND caller
            # cancellation — a finished (or killed) research run must never
            # leak a live child session.  ``BaseException`` suppression covers
            # a second cancellation delivered while the close is pending.
            with contextlib.suppress(BaseException):
                await close_session_async(child)
            _forget_child_session_record(child_id)
        return text, err

    # ------------------------------------------------------------------ #
    # Background execution
    # ------------------------------------------------------------------ #

    def _next_task_id(self, tasks: dict[str, BackgroundStream]) -> str:
        """Per-parent-session monotonically increasing ``research_<n>`` id.

        The count lives in the parent's ``custom_data`` and starts at 1
        (``research_1``, ``research_2``, ...); counts already taken in the
        active task registry are skipped, so an id is never reused while it
        could still collide with a live or finished-traceable task.
        """
        count = int(self._session.custom_data.get("research_task_count", 0))
        while True:
            count += 1
            task_id = f"research_{count}"
            if task_id not in tasks:
                break
        self._session.custom_data["research_task_count"] = count
        return task_id

    async def _start_background(self, query: str) -> ToolReturnValue:
        """Register the research run as a generic background task and return.

        The worker coroutine runs on its own event loop inside the
        stream's daemon thread (``BackgroundStream`` runs coroutine
        functions with ``asyncio.run``), so it keeps making progress no
        matter what the parent loop does; the stop function cancels it
        thread-safely for ``job_output action='kill'``.
        """
        tasks = get_all_tasks(self._session)
        task_id = self._next_task_id(tasks)
        stream = BackgroundStream()
        holder: dict[str, Any] = {}

        async def format_output(
            output: str,
            success: bool,
            exit_code: int | None,
            elapsed: float | None,
            wait_matched: bool | None,
        ) -> tuple[str, str, str | None, str | None, bool]:
            """Keep the research error detail in the job_output message.

            Runs when the stream settled (or was killed and joined), so
            ``holder['err']`` is final by then; without it the generic path
            would reduce a failure to "Task 'research_N' failed".
            """
            if success:
                return output, "", None, None, False
            detail = holder.get("err") or "unknown error"
            return output, f"Research failed: {detail}", None, None, False

        stream.format_output = format_output

        def stop() -> None:
            holder["cancel_requested"] = True
            loop = holder.get("loop")
            task = holder.get("task")
            if loop is not None and task is not None:
                with contextlib.suppress(Exception):
                    loop.call_soon_threadsafe(task.cancel)

        async def worker(q: queue.Queue[str]) -> tuple[bool, int]:
            holder["loop"] = asyncio.get_running_loop()
            holder["task"] = asyncio.current_task()
            if holder.get("cancel_requested"):
                holder["task"].cancel()
            start = time.monotonic()
            err: str | None = None
            text = ""
            try:
                child, child_id = await self._create_child_session()
                try:
                    text, err = await self._research(child, child_id, query)
                except asyncio.CancelledError:
                    err = "Research task was cancelled."
            except asyncio.CancelledError:
                err = "Research task was cancelled."
            except Exception as exc:
                err = _error_text(exc)
            finally:
                stream.process_elapsed = time.monotonic() - start
            holder["err"] = err
            if err is None:
                payload = text
            elif text and text != "(no text output)":
                # Keep the partial findings visible next to the failure.
                payload = f"{text}\n[research failed: {err}]"
            else:
                # Failures must surface a nonempty, greppable message.
                payload = f"research failed: {err}"
            if payload:
                q.put(payload)
            return (err is None, 0 if err is None else 1)

        await stream.start(worker, stop)
        add_task(self._session, task_id, stream)
        return ToolOk(
            output=f"task_id: {task_id}\nstatus: running",
            message=(
                f"Research running in background. task_id: `{task_id}`. "
                "Poll with job_output (wait: true to block); stop with "
                "job_output action='kill'."
            ),
            brief=f"Research started: {task_id}",
        )


__all__ = ["Research", "ResearchParams"]
