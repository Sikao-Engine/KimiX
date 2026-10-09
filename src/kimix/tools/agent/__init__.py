from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import orjson
from kaos.path import KaosPath
from kimi_cli.session import Session
from pydantic import AliasChoices, BaseModel, Field

import kimix.base as base
import kimix.utils as utils
from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from kimi_agent_sdk import Session as SdkSession
from kimi_agent_sdk._session import _sdk_sessions_dir
from kimix.tools.common import _create_script_file, _display_temp_path
from kimix.tools.prompt_common import accepts_alias_text
from kimix.ui.printing import MessageType
from kimix.utils import _create_session_async, close_session_async
from kimix.utils import _globals as _session_globals
from kimix.utils.session import register_session_close_hook
from kimix.utils.system_prompt import SystemPromptType

from .store import AgentSessionEntry, AgentSessionStore, ConversationTurn

# Module-level registry mapping sub-agent session id -> its store entry.
_agent_entries: dict[str, AgentSessionEntry] = {}

# Detached background subagent runs are tracked per parent session (in the
# parent session's ``custom_data`` under ``_BACKGROUND_TASKS_KEY``) so that
# ``kimix.utils.prompt`` can wait for a session's own subagents — and only
# those — when the session's prompt finishes.  A started task is kept in the
# set so it is not garbage collected before it settles; a done callback
# discards it.
_BACKGROUND_TASKS_KEY = "agent_background_tasks"


def _background_tasks_for(session: Any) -> set[asyncio.Task]:
    """Return (creating if needed) the detached-task set owned by *session*.

    Accepts either a ``kimi_cli.session.Session`` or the SDK wrapper around
    one (``kimi_agent_sdk.Session``); both expose the owning CLI session's
    ``custom_data``.  Returns a detached throwaway set when no custom data
    is available, so callers can uniformly iterate the result.
    """
    cli_session = session
    custom_data = getattr(cli_session, "custom_data", None)
    if not isinstance(custom_data, dict):
        # SDK-wrapped session: the owning CLI session hangs off ``_cli``.
        wrapped = getattr(getattr(session, "_cli", None), "session", None)
        if wrapped is not None:
            cli_session = wrapped
            custom_data = getattr(cli_session, "custom_data", None)
    if not isinstance(custom_data, dict):
        return set()
    tasks = custom_data.get(_BACKGROUND_TASKS_KEY)
    if tasks is None:
        tasks = set()
        custom_data[_BACKGROUND_TASKS_KEY] = tasks
    return tasks


async def wait_for_background_agents(session: Any) -> None:
    """Wait until every background subagent run owned by *session* settles.

    ``kimix.utils.prompt.prompt_async`` calls this when the session's prompt
    is finished but detached background subagents are still running, so they
    complete (closing/updating their own sessions) before the parent session
    is torn down — closing the parent would otherwise cascade-close them
    mid-run.  Never raises.
    """
    pending = [t for t in _background_tasks_for(session) if not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


# Live sub-agent session registry: sub-agent session id -> live SDK Session.
# Populated when the ``Agent`` tool spawns/resumes a sub-agent; used to
# resolve sessions for completion notices (``Steer.from_session`` needs
# ``session._cli.soul``) and for parent-close cascading.
_agent_sessions: dict[str, SdkSession] = {}


def _register_agent_session(session_id: str, session: SdkSession | None) -> None:
    if session_id and session is not None:
        _agent_sessions[session_id] = session


# Parent session id -> ids of the sub-agent sessions it spawned that are still
# alive.  The parent hands its sub-agents' ids to the model so it can resume
# them with ``subagent(session_id=...)``, which means those sessions are
# *continuable* — but only for as long as the parent session lives.  When the
# parent closes (CLI exit, web-server delete, destructor, process shutdown) the
# children are anonymous scratch sessions whose directories must go away with
# it, so the cascade below runs from a session close hook.
_children_by_parent: dict[str, set[str]] = {}

# Reverse lookup so forgetting a child (it closed on its own) is O(1).
_child_parent: dict[str, str] = {}


def _register_child_session(parent_id: str, child_id: str) -> None:
    """Remember that *parent_id* spawned the still-open session *child_id*."""
    if not parent_id or not child_id:
        return
    _children_by_parent.setdefault(parent_id, set()).add(child_id)
    _child_parent[child_id] = parent_id


def _forget_child_session(child_id: str) -> None:
    """Drop *child_id* from its parent's registry (it is closed/forgotten)."""
    parent_id = _child_parent.pop(child_id, None)
    if parent_id is None:
        return
    siblings = _children_by_parent.get(parent_id)
    if siblings is None:
        return
    siblings.discard(child_id)
    if not siblings:
        _children_by_parent.pop(parent_id, None)


def _take_child_sessions(parent_id: str) -> list[str]:
    """Pop and return the sub-agent session ids registered for *parent_id*."""
    if not parent_id:
        return []
    child_ids = sorted(_children_by_parent.pop(parent_id, set()))
    for child_id in child_ids:
        _child_parent.pop(child_id, None)
    return child_ids


def _forget_child_session_record(child_id: str) -> None:
    """Drop every bookkeeping record of *child_id* (its session is gone)."""
    _unregister_entry(child_id)
    _unregister_agent_session(child_id)
    _forget_child_session(child_id)


def _release_child_session(child_id: str) -> SdkSession | None:
    """Forget *child_id* and return the live session that has to be closed."""
    session = _agent_sessions.get(child_id)
    _forget_child_session_record(child_id)
    return session if session is not None else _sdk_session_by_id(child_id)


async def _destroy_child_sessions_async(parent_id: str) -> list[str]:
    """Close and delete every sub-agent session spawned by *parent_id*.

    Called when the parent session closes or its conversation is cleared.  The
    children are anonymous sessions (see ``Agent._resolve_session``), so closing
    them also removes their ``.kimix_cache/<id>`` directories.  Best-effort: a
    child that cannot be closed is dropped anyway and its own destructor /
    shutdown callback still reclaims the directory.  Returns the child ids.
    """
    destroyed: list[str] = []
    for child_id in _take_child_sessions(parent_id):
        session = _release_child_session(child_id)
        if session is not None:
            with contextlib.suppress(Exception):
                await close_session_async(session)
        destroyed.append(child_id)
    return destroyed


async def _on_parent_session_closed(session: Any) -> list[str]:
    """Session close hook: tear down the sub-agents of the closing session."""
    parent_id = _cli_session_id(session)
    if not parent_id or parent_id not in _children_by_parent:
        return []
    return await _destroy_child_sessions_async(parent_id)


# Registered once per process: every session closed through kimix (CLI exit,
# web server, the sub-agent store, the interpreter-shutdown hook) cascades to
# the sub-agent sessions it spawned.
register_session_close_hook(_on_parent_session_closed)


def _get_agent_session(session_id: str) -> SdkSession | None:
    if not session_id:
        return None
    return _agent_sessions.get(session_id)


def _unregister_agent_session(session_id: str) -> None:
    _agent_sessions.pop(session_id, None)


def _cli_session_id(session: Any) -> str:
    """Best-effort session id for a (possibly SDK-wrapped) session object.

    ``kimi_cli.session.Session`` exposes ``.id`` directly; the SDK wrapper
    (``kimi_agent_sdk.Session``) keeps it at ``session._cli.session.id``.
    """
    if session is None:
        return ""
    raw = getattr(session, "id", None)
    if raw:
        return str(raw)
    cli = getattr(session, "_cli", None)
    cli_session = getattr(cli, "session", None) if cli is not None else None
    return str(getattr(cli_session, "id", None) or "")


def _session_work_dir(session: Any) -> KaosPath | None:
    """Best-effort working directory of a (possibly SDK-wrapped) session.

    ``kimi_cli.session.Session`` exposes ``.work_dir`` directly; the SDK
    wrapper (``kimi_agent_sdk.Session``) keeps it at
    ``session._cli.session.work_dir``. Returns ``None`` when the session has
    no resolvable work dir (callers fall back to the process CWD).
    """
    if session is None:
        return None
    raw = getattr(session, "work_dir", None)
    if raw:
        return raw if isinstance(raw, KaosPath) else KaosPath(str(raw))
    cli = getattr(session, "_cli", None)
    cli_session = getattr(cli, "session", None) if cli is not None else None
    nested = getattr(cli_session, "work_dir", None)
    if nested:
        return nested if isinstance(nested, KaosPath) else KaosPath(str(nested))
    return None


def _session_dir(session: Any, session_id: str) -> Path:
    """On-disk directory the SDK uses for *session_id* in *session*'s work dir.

    Delegates to ``kimi_agent_sdk._session._sdk_sessions_dir`` so this check can
    never drift from where the SDK actually stores sessions
    (``<work dir>/.kimix_cache/<session id>``).  A session without a work dir
    falls back to ``KaosPath('.')``, exactly like ``_create_session_async``.
    """
    work_dir = _session_work_dir(session) or KaosPath(".")
    return _sdk_sessions_dir(work_dir) / session_id


def _sdk_session_by_id(session_id: str) -> SdkSession | None:
    """Fallback: find the live SDK session whose CLI session id matches.

    Sessions created through ``kimix.utils.session`` are tracked in
    ``_session_globals._live_sessions``; this rescues targets that were never
    explicitly registered via :func:`_register_agent_session`.
    """
    if not session_id:
        return None
    for sdk in list(_session_globals._live_sessions):
        if _cli_session_id(sdk) == session_id:
            return sdk
    return None


def _register_entry(session_id: str, entry: AgentSessionEntry) -> None:
    _agent_entries[session_id] = entry


def _get_entry(session_id: str) -> AgentSessionEntry | None:
    return _agent_entries.get(session_id)


def _unregister_entry(session_id: str) -> None:
    _agent_entries.pop(session_id, None)


def _resolve_prompt(prompt: str, base_dir: Path | None) -> str:
    """Return the effective task text.

    ``prompt`` is either inline text, or ``@path`` referencing a file whose
    UTF-8 content becomes the prompt. Relative paths resolve against
    *base_dir* (the parent session work dir), falling back to the process
    CWD so retry hints pointing at the shared temp folder
    (``.kimix_cache/tmp_<pid>/<n>.md``, which is CWD-relative) resolve even
    when the session work dir differs from the process cwd.
    Raises FileNotFoundError when the referenced file is missing.
    """
    if not prompt.startswith("@"):
        return prompt
    rel = prompt[1:]
    base = base_dir if base_dir is not None else Path(".")
    path = Path(rel)
    if not path.is_absolute():
        candidate = base / path
        if not candidate.exists():
            candidate = path  # fall back to CWD-relative resolution
        path = candidate
    if not path.exists():
        raise FileNotFoundError(f"prompt file not found: {rel}")
    return path.read_text(encoding="utf-8", errors="replace")


def _prompt_saved_message(prompt: str, ext: str = ".md") -> str:
    """Save *prompt* to the shared temp folder; return a retry hint suffix.

    Returns "" for empty prompts. Format matches the bash tool so the LLM
    can retry with subagent(prompt="@<path>") without re-emitting the text.
    """
    if not prompt:
        return ""
    saved = _create_script_file(prompt, ext=ext)
    shown = _display_temp_path(saved)
    return (
        f"[prompt saved to {shown}] "
        f"Retry with subagent(prompt=@{shown})."
    )


class SubAgentParams(BaseModel):
    model_config = {"populate_by_name": True}

    description: str | None = Field(
        default=None,
        description="Short (3-5 word) task label, for display.",
    )
    prompt: str = Field(
        validation_alias=AliasChoices("prompt", "task"),
        description=(
            "The complete, self-contained task for the subagent. "
            "Inline text, or @path to read the task from a file. "
            + accepts_alias_text("prompt", "task", word=False)
        ),
    )
    run_in_background: bool = Field(
        default=True,
        description=(
            "Whether to run in the background and return a durable subagent id "
            "immediately. Defaults to true; set false to block for the result."
        ),
    )
    session_id: str | None = Field(
        default=None,
        alias="session",  # common LLM variant
        description=(
            "Session ID to resume an existing sub-agent session. Sub-agent "
            "sessions are scratch: one created here is anonymous, so its dir is "
            "deleted on close and the id then resumes from a fresh conversation. "
            + accepts_alias_text("session_id", "session", word=False)
        ),
    )
    close_session: bool | None = Field(
        default=None,
        description=(
            "Close the subagent session after this prompt. Unset (default): a "
            "foreground run closes it, a background run keeps it open so its "
            "durable id stays listed by list_agents and resumable. Set "
            "true/false to override. Closing deletes the scratch dir "
            "(`.kimix_cache/<id>`), so the session can no longer be resumed "
            "with its history."
        ),
    )
    return_history: bool = Field(
        default=False,
        description="Return the full conversation history in extras.",
    )
    history_format: Literal["json", "markdown", "summary"] = Field(
        default="json",
        description="json: raw turns as JSON. markdown: transcript with headings. summary: what the sub-agent did.",
    )
    context_files: list[str] | None = Field(
        default=None,
        description="File paths to pre-read into the sub-agent's context before the prompt.",
    )
    context_data: dict[str, Any] | None = Field(
        default=None,
        description="Structured JSON data to pass as context to the sub-agent.",
    )
    inherit_context: bool = Field(
        default=False,
        description=(
            "For a NEW session, initialize it by copying the parent session's "
            "context (conversation history so far) into the sub-agent's id, like "
            "the CLI `/store`+`/load`, and resume from that copy. Ignored when "
            "`session_id` resolves to an active session (reused as-is)."
        ),
    )


@dataclass
class _PreparedRun:
    """Everything ``_execute`` needs, resolved before a run starts.

    ``params.close_session`` already holds the *effective* value (background
    runs keep the session unless the caller explicitly said otherwise).
    """

    params: SubAgentParams
    session: Any
    session_id: str
    is_reused: bool
    prompt: str
    background: bool


def _consume_background_task_error(task: asyncio.Task) -> None:
    """Retrieve a finished background task's exception (never raises)."""
    if task.cancelled():
        return
    with contextlib.suppress(Exception):
        task.exception()


def _get_store(session: Session) -> AgentSessionStore:
    store = session.custom_data.get("agent_conversation_store")
    if store is None:
        # The store keeps sessions alive across ``subagent`` calls; tell the
        # module registries when it releases one (LRU eviction) so nothing keeps
        # pointing at a closed session.
        store = AgentSessionStore(on_close=_forget_child_session_record)
        session.custom_data["agent_conversation_store"] = store
    return store


class _AgentConversationCollector:
    def __init__(self) -> None:
        self.turns: list[ConversationTurn] = []
        self.text_buffer: list[str] = []
        self.think_buffer: list[str] = []
        self.tool_buffer: list[str] = []
        self.last_msg_type: MessageType | None = None

    def _finalize_previous(self) -> None:
        if self.text_buffer:
            text = "".join(self.text_buffer)
            self.text_buffer.clear()
            self.turns.append(ConversationTurn(
                role="assistant",
                content=text,
                timestamp=time.time(),
                metadata={"type": "text"},
            ))
        if self.think_buffer:
            text = "".join(self.think_buffer)
            self.think_buffer.clear()
            self.turns.append(ConversationTurn(
                role="assistant",
                content=text,
                timestamp=time.time(),
                metadata={"type": "thinking"},
            ))
        if self.tool_buffer:
            text = "".join(self.tool_buffer)
            self.tool_buffer.clear()
            self.turns.append(ConversationTurn(
                role="tool",
                content=text,
                timestamp=time.time(),
                metadata={"type": "tool_call"},
            ))

    def consume(self, text: str, msg_type: MessageType) -> None:
        if msg_type == MessageType.Text:
            if self.last_msg_type not in (None, MessageType.Text):
                self._finalize_previous()
            self.text_buffer.append(text)
        elif msg_type == MessageType.Thinking:
            if self.last_msg_type not in (None, MessageType.Thinking):
                self._finalize_previous()
            self.think_buffer.append(text)
        elif msg_type in (MessageType.ToolCalling, MessageType.ToolCallingPart):
            if self.last_msg_type not in (None, MessageType.ToolCalling, MessageType.ToolCallingPart):
                self._finalize_previous()
            if text:
                self.tool_buffer = [text]
        elif msg_type == MessageType.ToolResult:
            self._finalize_previous()
            self.turns.append(ConversationTurn(
                role="tool",
                content=text,
                timestamp=time.time(),
                metadata={"type": "tool_result"},
            ))
        self.last_msg_type = msg_type

    def finalize_user_turn(self, prompt: str) -> None:
        self._finalize_previous()
        self.turns.append(ConversationTurn(
            role="user",
            content=prompt,
            timestamp=time.time(),
        ))

    def finalize_assistant_turn(self) -> str:
        self._finalize_previous()
        output_parts: list[str] = []
        for turn in self.turns:
            if (
                turn.role == "assistant"
                and turn.metadata is not None
                and turn.metadata.get("type") == "text"
            ) and isinstance(turn.content, str):
                output_parts.append(turn.content)
        return "".join(output_parts)


class Agent(CallableTool2):
    name: str = "subagent"
    description: str = (
        "Delegate a self-contained task to a subagent (a separate agent in its "
        "own context) to offload focused, independent work — research, a scoped "
        "implementation, an analysis — without consuming this conversation's "
        "context. It returns its result, not its intermediate steps, and does "
        "not see this conversation, so give it a complete standalone prompt. "
        "Runs in the background by default, returning a durable subagent id and "
        "keeping the child conversation resumable via "
        "subagent(session_id='<id>', ...). Set run_in_background: false only "
        "when your next action depends on the result. Sub-agents belong to the "
        "session that spawned them: closing or clearing that session closes "
        "them all and deletes their scratch dirs, so ids work only while the "
        "parent session lives."
    )
    params: type[SubAgentParams] = SubAgentParams

    def __init__(self, session: Session):
        super().__init__()
        self._session = session
        self._semaphore = asyncio.Semaphore(8)

    # NOTE: there is deliberately no ``__del__`` here.  The sub-agent sessions
    # belong to the *parent session*, not to this tool object: throwaway
    # instances are created all the time (the whole toolset is rebuilt on
    # ``/clear``) while the parent
    # conversation keeps running.  Reaping them from ``__del__`` therefore used
    # to wipe the parent's sub-agent store — and would delete its live sessions —
    # whenever such a helper was garbage collected.  Teardown happens where the
    # session lifecycle says so: ``kimix.utils.session`` runs the close hooks
    # (``_on_parent_session_closed``) when the parent session is closed, cleared
    # or destroyed at process exit.

    async def __call__(self, params: SubAgentParams) -> ToolReturnValue:
        if self._session is not None and self._session.custom_config.get("is_sub_agent"):
            return ToolError(
                output='',
                message='Recursive sub-agent call detected',
                brief='sub-agent recursively'
            )
        try:
            async with self._semaphore:
                prepared = await self._prepare_run(params)
        except Exception as exc:
            return ToolError(
                output="",
                message=str(exc),
                brief="Failed to create sub-agent session",
            )
        if prepared.background:
            return await self._launch_background(prepared)
        return await self._execute(prepared)

    async def _prepare_run(self, params: SubAgentParams) -> _PreparedRun:
        """Resolve the target session and build the effective prompt.

        Everything that can fail synchronously — session creation, ``@file``
        prompt resolution, context reads — happens here, before a background
        run is launched, so errors surface in the caller instead of being
        lost inside the detached task.
        """
        session, session_id, is_reused = await self._resolve_session(params)
        _get_store(self._session)

        # Resolve @file prompt references to the full task text first.
        work_dir = _session_work_dir(self._session)
        base_dir = Path(str(work_dir)) if work_dir is not None else Path(".")
        task_text = _resolve_prompt(params.prompt, base_dir)

        # Handle very long prompts by offloading to a shared temp file.
        prompt_bytes = task_text.encode('utf-8')
        if len(prompt_bytes) > 100 * 1024:
            temp_path = _create_script_file(task_text, ext=".md")
            task_prompt = f"Please read the task from `{_display_temp_path(temp_path)}` and execute it."
        else:
            task_prompt = task_text

        # Build prompt with context files / context_data if provided
        prompt = task_prompt
        if params.context_files or params.context_data:
            context_parts = ["<context>"]
            if params.context_files:
                for fp in params.context_files:
                    try:
                        file_path = base_dir / fp
                        content = file_path.read_text(encoding="utf-8", errors="replace")
                        context_parts.append(f"<file path='{fp}'>\n{content}\n</file>")
                    except Exception as e:
                        context_parts.append(f"<file path='{fp}' error='{e}'/>")
            if params.context_data:
                import orjson as _orjson
                context_parts.append(f"<data>\n{_orjson.dumps(params.context_data, option=_orjson.OPT_INDENT_2).decode()}\n</data>")
            context_parts.append("</context>")
            context_block = "\n".join(context_parts)
            prompt = f"{context_block}\n\n{prompt}"

        # A session mid-run cannot take another prompt (the SDK raises
        # ``SessionStateError`` which ``prompt_async`` swallows, turning the
        # call into a silent no-op) — reject it up front with real guidance.
        if isinstance(getattr(session, "_cancel_event", None), asyncio.Event):
            raise RuntimeError(
                f"Sub-agent '{session_id}' is currently running; wait for it "
                "to finish before resuming it with more work."
            )

        background = bool(params.run_in_background)
        # Effective close policy: background runs keep the session open by
        # default so the durable id stays listed/resumable/messageable (the
        # documented background contract); foreground runs close by default.
        # An explicit close_session always wins.
        effective_close = not background if params.close_session is None else params.close_session
        effective_params = params.model_copy(update={"close_session": effective_close})
        return _PreparedRun(
            params=effective_params,
            session=session,
            session_id=session_id,
            is_reused=is_reused,
            prompt=prompt,
            background=background,
        )

    async def _launch_background(self, prepared: _PreparedRun) -> ToolReturnValue:
        """Start ``_execute`` as a detached task and return the durable id now."""
        store = _get_store(self._session)
        existing = store.get(prepared.session_id)
        if existing is None:
            await store.evict_lru_if_needed()
        entry = AgentSessionEntry(
            session=prepared.session,
            session_id=prepared.session_id,
            created_at=existing.created_at if existing else time.time(),
            last_accessed=time.time(),
            conversation_history=list(existing.conversation_history) if existing else [],
            total_turns=existing.total_turns if existing else 0,
            is_active=True,
            state="running",
        )
        store.put(entry)
        _register_entry(prepared.session_id, entry)

        task = asyncio.create_task(self._run_background(prepared))
        owned_tasks = _background_tasks_for(self._session)
        owned_tasks.add(task)
        task.add_done_callback(owned_tasks.discard)
        task.add_done_callback(_consume_background_task_error)

        result = ToolOk(
            output=(
                f"Session ID: {prepared.session_id}\n\n"
                "Started in the background; you'll be notified when it "
                "finishes. Use list_agents to check its state."
            ),
            brief="Background subagent started",
        )
        extras: dict[str, Any] = {
            "session_id": prepared.session_id,
            "status": "running",
            "turn_count": entry.total_turns,
        }
        if prepared.params.return_history:
            extras["conversation_history"] = self._format_history(
                entry.conversation_history, prepared.params.history_format
            )
        result.extras = extras
        return result

    async def _run_background(self, prepared: _PreparedRun) -> None:
        """Detached task body for a background subagent run."""
        try:
            result = await self._execute(prepared)
        except Exception as exc:
            # ``_execute`` handles its own errors; this is a defensive net so
            # a background failure still notifies the parent and never logs
            # an unretrieved task exception.
            result = ToolError(
                output="",
                message=str(exc),
                brief="Background subagent failed",
            )
        await self._notify_parent_background_finished(prepared.session_id, result)

    async def _notify_parent_background_finished(
        self, session_id: str, result: ToolReturnValue
    ) -> None:
        """Best-effort steer telling the parent the background run settled.

        Delivered as a steer into the parent's running loop; when the parent
        is idle the steer is dropped (its queue is rebuilt at the next turn).
        """
        parent_id = _cli_session_id(self._session)
        if not parent_id:
            return
        parent = _get_agent_session(parent_id) or _sdk_session_by_id(parent_id)
        if parent is None:
            return
        status = "failed" if result.is_error else "completed"
        text = _truncate_text(result.output or result.message or "(no output)", 2000)
        extras = getattr(result, "extras", None) or {}
        store = _get_store(self._session)
        if _was_closed_mid_turn(result, extras, store, session_id):
            # The run was interrupted (its session was closed mid-turn), so the
            # output is partial — never present it as a clean completion with a
            # bogus "(no text output)" final message.
            notice = _interrupted_notice(session_id, text)
        else:
            notice = f"Background subagent '{session_id}' {status}. Final message:\n{text}"
        await _push_steer_notice(parent, notice)

    async def _execute(self, prepared: _PreparedRun) -> ToolReturnValue:
        session = prepared.session
        session_id = prepared.session_id
        params = prepared.params
        store = _get_store(self._session)

        try:
            collector = _AgentConversationCollector()
            collector.finalize_user_turn(prepared.prompt)

            def output_function(text: str, msg_type: MessageType) -> None:
                if text:
                    collector.consume(text, msg_type)

            err_msg: str | None = None
            try:
                await utils.prompt_async(
                    prompt_str=prepared.prompt,
                    session=session,
                    output_function=output_function,
                    info_print=False,
                    merge_wire_messages=True, format_output=True
                )
            except Exception as e:
                err_msg = str(e)
                collector.turns.append(ConversationTurn(
                    role="error",
                    content=err_msg,
                    timestamp=time.time(),
                    metadata={"error_type": type(e).__name__},
                ))

            output_text = collector.finalize_assistant_turn()
            if not output_text:
                output_text = "(no text output)"

            output_prefix = f"Session ID: {session_id}\n\n"

            if err_msg:
                # The prompt is intentionally not echoed in the brief: it is
                # streamed live (formatted and colored) by the CLI printer
                # while the tool call is generated (see kimix.base), so
                # printing it here would show it twice.
                saved_suffix = _prompt_saved_message(prepared.prompt)  # full effective prompt actually sent
                message = f"{err_msg} {saved_suffix}".strip() if saved_suffix else err_msg
                result = ToolError(
                    output=output_prefix + output_text,
                    message=message,
                    brief="sub-agent task failed",
                )
                extras = self._build_extras(
                    params, session_id, collector.turns, "closed"
                )
                if saved_suffix:
                    prompt_file = saved_suffix.split("[prompt saved to ", 1)[1].split("]", 1)[0]
                    extras["prompt_file"] = prompt_file
                result.extras = extras
                await close_session_async(session)
                store.close(session_id)
                _unregister_entry(session_id)
                _unregister_agent_session(session_id)
                _forget_child_session(session_id)
                return result

            extras = self._build_extras(
                params,
                session_id,
                collector.turns,
                "closed" if params.close_session else "continued",
            )

            await self._update_store(
                params, session, session_id, prepared.is_reused, collector.turns
            )

            result = ToolOk(
                output=output_prefix + output_text,
                brief="Sub-agent task completed",
            )
            result.extras = extras
            return result

        except Exception as exc:
            return ToolError(
                output="",
                message=str(exc),
                brief="Failed to create sub-agent session",
            )

    def _build_extras(
        self,
        params: SubAgentParams,
        session_id: str,
        turns: list[ConversationTurn],
        status: str,
        **extra: Any,
    ) -> dict[str, Any]:
        """Build the shared extras dict for ``Agent`` result branches.

        Every branch includes ``session_id``, ``status`` and ``turn_count``;
        ``conversation_history`` is added when ``params.return_history`` is
        set. Branch-specific keys are passed as keyword extras (e.g.
        ``question`` for the awaiting-response branch).
        """
        extras: dict[str, Any] = {
            "session_id": session_id,
            "status": status,
            "turn_count": len(turns),
            **extra,
        }
        if params.return_history:
            extras["conversation_history"] = self._format_history(
                turns, params.history_format
            )
        return extras

    def _format_history(self, turns: list[ConversationTurn], format: str) -> list[dict[str, Any]] | str:
        """Format conversation turns according to history_format."""
        if format == "json":
            return _history_as_json(turns)
        if format == "markdown":
            return _history_as_markdown(turns)
        if format == "summary":
            return _history_as_summary(turns)
        return []

    async def _resolve_session(self, params: SubAgentParams) -> tuple[Any, str, bool]:
        store = _get_store(self._session)

        if params.session_id:
            entry = store.get(params.session_id)
            if entry is not None and entry.is_active:
                entry.last_accessed = time.time()
                _register_entry(params.session_id, entry)
                self._register_agent_sessions(entry.session, params.session_id)
                return entry.session, params.session_id, True

        session_id = params.session_id or str(uuid.uuid4())

        # Sub-agent sessions are scratch space owned by this tool: it creates
        # them here and closes them again on ``close_session`` (the default), on
        # LRU eviction and on ``interrupt_agent``.  They must therefore be
        # *anonymous* — the SDK only deletes ``<work dir>/.kimix_cache/<id>``
        # when an anonymous session closes (``kimi_agent_sdk.Session.close``),
        # so a non-anonymous sub-agent leaves its session directory behind for
        # good, and that stale directory is then indexed as a user session by
        # the web UI (``kimix.server.session_manager`` lists ``.kimix_cache``
        # through ``CliSession.list``).
        #
        # Exception: a caller-supplied session id whose directory already exists
        # was *not* created here (a previously saved sub-agent conversation, or
        # a named session the caller owns), so it is resumed as a durable, named
        # session and its files are left alone.
        #
        # Evaluated before ``inherit_context`` copies the parent directory in,
        # so the copied scratch session stays anonymous.
        anonymous = not _session_dir(self._session, session_id).exists()

        # Inherit the parent agent's context: copy the parent session
        # directory into the new sub-agent session id (mirrors the CLI
        # `/store` + `/load` logic in src/kimix/cli_impl/commands.py) so the
        # resumed sub-agent starts from the parent's conversation so far.
        inherited = False
        if params.inherit_context:
            await self._inherit_parent_context(session_id)
            inherited = True

        custom_config = self._session.custom_config
        chat_provider = custom_config.get("chat_provider")
        default_sub_provider = (
            base.get_default_sub_provider("sub_agent")
            or custom_config.get("provider_dict", base._default_provider)
        )

        session = await _create_session_async(
            session_id=session_id,
            work_dir=_session_work_dir(self._session),
            agent_file=base._default_agent_file_dir / 'agent_subagent.json',
            agent_type=SystemPromptType.TrivialSubAgent,
            provider_dict=default_sub_provider,
            chat_provider=chat_provider,
            resume=True,
            anonymous=anonymous,
        )

        sub_custom_config = session.get_custom_config()
        if sub_custom_config is not None:
            sub_custom_config['is_sub_agent'] = True

        if inherited:
            await self._reset_inherited_system_prompt(session)

        # A fresh session for this id: any tombstone left by an earlier
        # close (interrupt_agent / close_session) is stale now — the new run
        # owns the id and registers normally at the end of its run.
        store.discard_closed(session_id)
        self._register_agent_sessions(session, session_id)

        return session, session_id, False

    async def _inherit_parent_context(self, target_session_id: str) -> None:
        """Copy the parent session's context into *target_session_id*.

        Mirrors the CLI ``/store`` / ``/load`` commands
        (``src/kimix/cli_impl/commands.py``): the parent session directory is
        copied to the new sub-agent session id with
        ``kimi_cli.session.Session.copy`` — the same primitive ``/store`` uses
        to save a snapshot under a name and ``/load`` uses to load a snapshot
        into a fresh session — and the sub-agent session is then resumed from
        that copy.

        Unlike the CLI commands, the parent session is *not* released before
        copying: the parent agent's loop is still running (this tool call is
        part of its current turn), so its resources must stay intact. The
        parent's context database is in WAL mode and commits after every
        append, and no writes happen while this tool call executes, so the
        directory copy captures a consistent snapshot (SQLite replays the
        copied WAL on open).

        Raises:
            ValueError: If the parent session cannot be copied (no session id
                / work dir, or a session with the target id already exists).
        """
        parent_id = _cli_session_id(self._session)
        work_dir = _session_work_dir(self._session)
        if not parent_id:
            raise ValueError(
                'Cannot inherit parent context: parent session has no id'
            )
        if work_dir is None:
            raise ValueError(
                'Cannot inherit parent context: parent session has no work dir'
            )
        await Session.copy(work_dir, parent_id, target_session_id)

    async def _reset_inherited_system_prompt(self, session: Any) -> None:
        """Replace the inherited parent system prompt with the sub-agent's.

        The copied session directory carries the parent's persisted system
        prompt, which the soul adopts on resume
        (``agent.system_prompt_cached``). Reset it so the sub-agent runs with
        its own (TrivialSubAgent) system prompt instead of the parent's, which
        would describe the parent's toolset and role.

        Best-effort: silently skips when the soul internals are unavailable or
        rendering the prompt fails.
        """
        cli = getattr(session, '_cli', None)
        soul = getattr(cli, 'soul', None)
        agent = getattr(soul, 'agent', None)
        context = getattr(soul, 'context', None)
        if agent is None or context is None:
            return
        try:
            agent.system_prompt_cached = None
            prompt = agent.get_system_prompt()
            if prompt:
                await context.write_system_prompt(prompt)
        except Exception:
            return

    def _register_agent_sessions(
        self, child_session: Any, child_session_id: str
    ) -> None:
        """Register the parent and child SDK sessions for agent bookkeeping.

        The parent is registered under its own id so background completion
        notices can steer it; the child is recorded as belonging to the
        parent session so that closing/destroying the parent tears the child
        down with it.
        """
        parent_id = _cli_session_id(self._session)
        parent_sdk = _sdk_session_by_id(parent_id)
        if parent_id and parent_sdk is not None:
            _register_agent_session(parent_id, parent_sdk)
        if child_session_id:
            _register_agent_session(child_session_id, child_session)
            _register_child_session(parent_id, child_session_id)

    async def _update_store(
        self,
        params: SubAgentParams,
        session: Any,
        session_id: str,
        is_reused: bool,
        turns: list[ConversationTurn],
    ) -> None:
        store = _get_store(self._session)
        if params.close_session:
            await close_session_async(session)
            store.close(session_id)
            _unregister_entry(session_id)
            _unregister_agent_session(session_id)
            _forget_child_session(session_id)
        else:
            existing = store.get(session_id)
            if existing is None:
                if store.was_closed(session_id):
                    # The session was closed externally (interrupt_agent)
                    # while this run was still finishing.  Re-putting the
                    # entry here would resurrect the dead session, listed as
                    # completed/active, contradicting the close.
                    return
                await store.evict_lru_if_needed()
            created_at = existing.created_at if existing else time.time()
            entry = AgentSessionEntry(
                session=session,
                session_id=session_id,
                created_at=created_at,
                last_accessed=time.time(),
                conversation_history=turns,
                total_turns=len(turns),
                is_active=True,
                # A finished run is "completed".
                state="completed",
            )
            store.put(entry)
            _register_entry(session_id, entry)


class AgentListParams(BaseModel):
    scope: str = Field(
        default="children",
        description=(
            "`children` (default) lists direct children only. `descendants` is "
            "accepted but returns the same list."
        ),
    )


class AgentList(CallableTool2):
    name: str = "list_agents"
    description: str = (
        "List your continuable background subagents by durable id and label. "
        "Use it to recall which ones you started, not to poll for completion — "
        "you're told when one finishes. Each entry reports session_id, "
        "created_at, last_accessed, total_turns, state, is_active; sessions "
        "closed via interrupt_agent are removed. A listed child stays resumable: "
        "subagent(session_id=..., ...) starts a new turn on the same "
        "conversation."
    )
    params: type[BaseModel] = AgentListParams

    def __init__(self, session: Session):
        super().__init__()
        self._session = session

    async def __call__(self, params: AgentListParams) -> ToolReturnValue:
        store = _get_store(self._session)
        sessions = store.list_active()
        output = orjson.dumps(sessions, option=orjson.OPT_INDENT_2)
        return ToolOk(output=output, brief="Listed active subagents")


class AgentCloseParams(BaseModel):
    model_config = {"populate_by_name": True}

    agent_id: str = Field(
        validation_alias=AliasChoices("agent_id", "session", "session_id"),
        description=(
            "The agent id of the running agent to interrupt. "
            + accepts_alias_text("agent_id", "session", "session_id", word=False)
        ),
    )


class AgentClose(CallableTool2):
    name: str = "interrupt_agent"
    description: str = (
        "Request cancellation of a background agent's current turn by its agent "
        "id. The target may be your direct child or a deeper agent under you. "
        "Its current turn stops (agents it started keep running) and the "
        "subagent session is closed and removed from the active list. Returns "
        "as soon as the stop is accepted, so the target may run briefly; "
        "interrupting an already-finished agent still closes its session."
    )
    params: type[BaseModel] = AgentCloseParams

    def __init__(self, session: Session):
        super().__init__()
        self._session = session

    async def __call__(self, params: AgentCloseParams) -> ToolReturnValue:
        store = _get_store(self._session)
        entry = store.get(params.agent_id)
        if entry is None:
            return ToolError(
                output="",
                message="Session not found",
                brief="Session not found",
            )
        await close_session_async(entry.session)
        store.close(params.agent_id)
        _unregister_agent_session(params.agent_id)
        _forget_child_session(params.agent_id)
        return ToolOk(
            output=f"Session {params.agent_id} closed.",
            brief="Session closed",
        )


# ---------------------------------------------------------------------------
# History formatting / background-notice helpers.
#
# Split out of `Agent._format_history` and `Agent._notify_parent_background_finished`
# so both methods stay under the G1 cyclomatic-complexity threshold of 12
# (finding F-19).  Pure functions of their arguments; no state is read.
# ---------------------------------------------------------------------------

_ROLE_ICONS = {"user": "👤", "assistant": "🤖", "tool": "🔧", "error": "❌", "system": "⚙️"}


def _history_as_json(turns: list[ConversationTurn]) -> list[dict[str, Any]]:
    """Return the raw turn models."""
    return [turn.model_dump() for turn in turns]


def _history_as_markdown(turns: list[ConversationTurn]) -> str:
    """Render the turns as a markdown transcript."""
    lines: list[str] = []
    for i, turn in enumerate(turns):
        icon = _ROLE_ICONS.get(turn.role, "?")
        label = turn.metadata.get("type", turn.role) if turn.metadata else turn.role
        lines.append(f"### Turn {i + 1}: {icon} {label}")
        content = turn.content if isinstance(turn.content, str) else str(turn.content)
        lines.append(content)
        lines.append("")
    return "\n".join(lines)


def _history_as_summary(turns: list[ConversationTurn]) -> str:
    """Return the one-line tool-call / text-response summary."""

    def _count(kind: str) -> int:
        return sum(
            1 for t in turns if t.metadata and t.metadata.get("type") == kind
        )

    text_turns = [t for t in turns if _is_text_turn(t)]
    total_chars = sum(len(str(t.content)) for t in text_turns)
    return (
        f"Sub-agent made {_count('tool_call')} tool call(s) with "
        f"{_count('tool_result')} result(s), and produced {len(text_turns)} text "
        f"response(s) ({total_chars} total characters)."
    )


def _is_text_turn(turn: ConversationTurn) -> bool:
    """True for an assistant turn carrying text content."""
    if turn.role != "assistant" or not turn.metadata:
        return False
    return turn.metadata.get("type") == "text"


def _truncate_text(text: str, limit: int) -> str:
    """Return *text*, truncated with an ellipsis when longer than *limit*."""
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _was_closed_mid_turn(
    result: ToolReturnValue, extras: dict, store: Any, session_id: str
) -> bool:
    """True when the run was interrupted because its session was closed mid-turn."""
    if result.is_error:
        return False
    if extras.get("status") != "continued":
        return False
    return store.get(session_id) is None and store.was_closed(session_id)


def _interrupted_notice(session_id: str, text: str) -> str:
    """Build the 'was interrupted' steer text, dropping the session preamble."""
    partial = text
    if partial.startswith("Session ID:") and "\n\n" in partial:
        partial = partial.split("\n\n", 1)[1]
    if partial == "(no text output)":
        partial = "(interrupted before producing any text output)"
    partial = _truncate_text(partial, 1500)
    return f"Background subagent '{session_id}' was interrupted. Partial output:\n{partial}"


async def _push_steer_notice(parent: Any, notice: str) -> None:
    """Best-effort steer delivery; a dropped steer is not an error."""
    from kimi_cli.soul.steer import Steer

    try:
        steer = Steer.from_session(parent)
        if steer is not None:
            await steer.push(notice)
    except Exception:
        pass
