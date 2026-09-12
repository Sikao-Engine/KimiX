from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from kosong.message import Message

from kimi_cli.approval_runtime import (
    ApprovalSource,
    get_current_approval_source_or_none,
    reset_current_approval_source,
    set_current_approval_source,
)
from kimi_cli.hooks.engine import HookEngine
from kimi_cli.soul.steer import Steer as Steer
from kimi_cli.utils.aioqueue import QueueShutDown
from kimi_cli.utils.logging import logger
from kimi_cli.wire import Wire
from kimi_cli.wire.file import WireFile
from kimi_cli.wire.types import ContentPart, MCPStatusSnapshot, TextPart, WireMessage

if TYPE_CHECKING:
    from kimi_cli.llm import LLM, ModelCapability
    from kimi_cli.soul.agent import Runtime
    from kimi_cli.soul.kimisoul import KimiSoul
    from kimi_cli.soul.slash import SlashCommandCall, SlashCommandInfo


class LLMNotSet(Exception):
    """Raised when the LLM is not set."""

    def __init__(self) -> None:
        super().__init__("LLM not set")


class LLMNotSupported(Exception):
    """Raised when the LLM does not have required capabilities."""

    def __init__(self, llm: LLM, capabilities: list[ModelCapability]):
        self.llm = llm
        self.capabilities = capabilities
        capabilities_str = "capability" if len(capabilities) == 1 else "capabilities"
        super().__init__(
            f"LLM model '{llm.model_name}' does not support required {capabilities_str}: "
            f"{', '.join(capabilities)}."
        )


class MaxStepsReached(Exception):
    """Raised when the maximum number of steps is reached."""

    n_steps: int
    """The number of steps that have been taken."""

    def __init__(self, n_steps: int):
        super().__init__(f"Max number of steps reached: {n_steps}")
        self.n_steps = n_steps


class SessionRestartRequired(Exception):
    """Raised when the session should be automatically restarted.

    This is raised when retryable errors (e.g. persistent 5xx, connection
    failures) exhaust all retries and the session needs a fresh start.
    The exception propagates through the agent loop and is handled by
    :meth:`kimi_agent_sdk.Session.prompt`, which calls :meth:`Session._restart`
    and re-invokes the prompt while preserving the existing session context.
    """

    def __init__(
        self,
        reason: str,
        original_error: BaseException | None = None,
    ) -> None:
        super().__init__(reason)
        self.original_error = original_error


def format_token_count(n: int) -> str:
    """Format token count as compact string, e.g. 28.5k, 128k, 1.2m."""
    suffix = ""
    if n >= 1_000_000:
        value = n / 1_000_000
        suffix = "m"
    elif n >= 1_000:
        value = n / 1_000
        suffix = "k"
    else:
        return str(n)

    # Keep one decimal when needed, but drop trailing ".0".
    compact = f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{compact}{suffix}"


def format_context_status(
    context_usage: float,
    context_tokens: int = 0,
    max_context_tokens: int = 0,
) -> str:
    """Format context status string for display in status bar."""
    bounded = max(0.0, min(context_usage, 1.0))
    if max_context_tokens > 0:
        used = format_token_count(context_tokens)
        total = format_token_count(max_context_tokens)
        return f"context: {bounded:.1%} ({used}/{total})"
    return f"context: {bounded:.1%}"


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    context_usage: float
    """The usage of the context, in percentage."""
    yolo_enabled: bool = False
    """Whether the explicit YOLO (auto-approve) flag is on. Independent of afk."""
    afk_enabled: bool = False
    """Whether afk (away-from-keyboard) mode is active. Implies auto-approve."""
    context_tokens: int = 0
    """The number of tokens currently in the context."""
    max_context_tokens: int = 0
    """The maximum number of tokens the context can hold."""
    mcp_status: MCPStatusSnapshot | None = None
    """The current MCP startup snapshot, if MCP is configured."""


@runtime_checkable
class Soul(Protocol):
    @property
    def name(self) -> str:
        """The name of the soul."""
        ...

    @property
    def model_name(self) -> str:
        """The name of the LLM model used by the soul. Empty string if LLM is not set."""
        ...

    @property
    def model_capabilities(self) -> set[ModelCapability] | None:
        """The capabilities of the LLM model used by the soul. None if LLM is not set."""
        ...

    @property
    def thinking(self) -> bool | None:
        """
        Whether thinking mode is currently enabled.
        None if LLM is not set or thinking mode is not set explicitly.
        """
        ...

    @property
    def status(self) -> StatusSnapshot:
        """The current status of the soul. The returned value is immutable."""
        ...

    @property
    def hook_engine(self) -> HookEngine:
        """The hook engine for this soul."""
        ...

    @property
    def available_slash_commands(self) -> list[SlashCommandInfo]:
        """List of available slash commands supported by the soul."""
        ...

    async def run(
        self,
        user_input: str | list[ContentPart],
    ):
        """
        Run the agent with the given user input until the max steps or no more tool calls.

        Slash-command dispatch, the empty-input guard, the approval-source
        lifecycle, and session auto-titling are handled by :func:`run_soul`
        before ``run`` is invoked; direct ``run`` callers receive
        natural-language input only and must handle those concerns
        themselves.

        Args:
            user_input (str | list[ContentPart]): The user input to the agent.

        Raises:
            LLMNotSet: When the LLM is not set.
            LLMNotSupported: When the LLM does not have required capabilities.
            ChatProviderError: When the LLM provider returns an error.
            MaxStepsReached: When the maximum number of steps is reached.
            asyncio.CancelledError: When the run is cancelled by user.
        """
        ...


type UILoopFn = Callable[[Wire], Coroutine[Any, Any, None]]
"""A long-running async function to visualize the agent behavior."""


class RunCancelled(Exception):
    """The run was cancelled by the cancel event."""


def _user_input_is_empty(user_input: str | list[ContentPart]) -> bool:
    """Return ``True`` when *user_input* contains no sendable content.

    An empty turn is produced when callers invoke the soul with a blank
    prompt (an empty Enter, a reconnect/replay, or a stray empty steer from
    the JSON-RPC wire server, which passes ``user_input`` straight through).
    Forwarding it appends an empty ``user`` message to the session and makes
    the LLM answer with a spurious "the user sent an empty message" turn.

    A string is empty when it is ``""`` or whitespace-only. A part list is
    empty when it has no parts or every part is a whitespace-only
    ``TextPart``; non-text parts (e.g. images) are always sendable.
    """
    if isinstance(user_input, str):
        return not user_input.strip()
    if not user_input:
        return True
    for part in user_input:
        if isinstance(part, TextPart):
            if part.text.strip():
                return False
        else:
            return False
    return True


def _extract_text_input(user_input: str | list[ContentPart]) -> str:
    """Extract the plain-text form of *user_input*.

    Mirrors ``Message.extract_text(" ")`` without requiring callers to
    construct a ``Message`` for the common string case.
    """
    if isinstance(user_input, str):
        return user_input
    return Message(role="user", content=user_input).extract_text(" ")


async def _run_slash_command(
    soul: KimiSoul,
    call: SlashCommandCall,
    raw_input: str | list[ContentPart],
) -> None:
    """Dispatch a parsed slash command with turn wire framing.

    Runs as the soul task inside :func:`run_soul` so wire recording and the
    approval-source lifecycle apply exactly like a normal turn. Emits the
    same ``TurnBegin``/``TurnEnd`` pair a natural-language turn produces.
    """
    from kimi_cli.soul.slash import find_command
    from kimi_cli.wire.types import TurnBegin, TurnEnd

    wire_send(TurnBegin(user_input=raw_input))
    try:
        command_func = find_command(call.name)
        if command_func is None:
            wire_send(TextPart(text=f'Unknown slash command "/{call.name}".'))
        else:
            ret = command_func(soul, call.args)
            if isinstance(ret, Awaitable):
                await ret
    finally:
        wire_send(TurnEnd())


async def _maybe_set_session_title(
    soul: KimiSoul,
    user_input: str | list[ContentPart],
) -> None:
    """Auto-set the session title after the first real (non-slash) turn."""
    session = soul.runtime.session
    if session.state.custom_title is not None:
        return

    from kimi_cli.session_state import load_session_state, save_session_state
    from kimi_cli.utils.string import shorten

    title = shorten(_extract_text_input(user_input), width=50)
    if not title:
        return

    # Read-modify-write: load fresh state to avoid overwriting concurrent
    # changes from other clients (e.g. the web UI renaming the session).
    fresh = load_session_state(session.dir)
    if fresh.custom_title is None:
        fresh.custom_title = title
        save_session_state(fresh, session.dir)
    session.state.custom_title = fresh.custom_title


async def run_soul(
    soul: Soul,
    user_input: str | list[ContentPart],
    ui_loop_fn: UILoopFn,
    cancel_event: asyncio.Event,
    wire_file: WireFile | None = None,
    runtime: Runtime | None = None,
) -> None:
    """
    Run the soul with the given user input, connecting it to the UI loop with a `Wire`.

    This is the common entry funnel for all clients (wire server, web/ACP
    backends, subagent runner). Besides driving the soul it owns the
    entry-point hygiene moved out of ``KimiSoul.run``:

    - Empty-input guard — blank prompts are ignored (no turn is started).
    - Slash-command dispatch — a leading ``/name [args]`` input is dispatched
      to the soul slash-command table with ``TurnBegin``/``TurnEnd`` framing
      instead of being sent to the LLM.
    - Approval-source lifecycle — a ``foreground_turn`` approval source is
      installed for the duration of the run (unless an outer layer, e.g. the
      subagent runner, already installed one) and its pending approval
      requests are cancelled on cleanup.
    - Session auto-titling — the first successful non-slash turn titles the
      session from the user input.

    `cancel_event` is a outside handle that can be used to cancel the run. When the
    event is set, the run will be gracefully stopped and a `RunCancelled` will be raised.

    Raises:
        LLMNotSet: When the LLM is not set.
        LLMNotSupported: When the LLM does not have required capabilities.
        ChatProviderError: When the LLM provider returns an error.
        MaxStepsReached: When the maximum number of steps is reached.
        RunCancelled: When the run is cancelled by the cancel event.
    """
    # ── Empty-input guard ────────────────────────────────────────────
    # Never start a turn for blank input. The JSON-RPC wire server's
    # ``_handle_prompt`` passes ``msg.params.user_input`` straight through,
    # plus legacy CLIs can invoke run_soul with an empty/whitespace prompt —
    # an empty Enter, a reconnect/replay, or a stray empty steer. Without
    # this guard the soul appends an empty ``user`` message to the session
    # and the LLM answers with a spurious "the user sent an empty message"
    # turn. The SDK (``kimi_agent_sdk.Session.prompt``) and the web backend
    # already reject empty input; this guard covers every remaining path so
    # no empty turn is ever started.
    if _user_input_is_empty(user_input):
        logger.debug("Ignoring empty user input; no turn started")
        return

    # ── Slash-command interception ───────────────────────────────────
    # Resolved before the wire/run tasks start so a slash input never
    # reaches the LLM. Only KimiSoul supports the built-in command table.
    slash_call: SlashCommandCall | None = None
    from kimi_cli.soul.kimisoul import KimiSoul

    if isinstance(soul, KimiSoul):
        from kimi_cli.soul.slash import parse_slash_command_call

        slash_call = parse_slash_command_call(_extract_text_input(user_input).strip())

    # ── Approval-source lifecycle ────────────────────────────────────
    # A foreground turn gets a stable source so its pending approval
    # requests can be cancelled reliably when the turn ends. Outer layers
    # that already installed a source (subagent runner, background agent
    # runner) are left untouched.
    approval_source_token = None
    created_approval_source: ApprovalSource | None = None
    if get_current_approval_source_or_none() is None:
        created_approval_source = ApprovalSource(kind="foreground_turn", id=uuid.uuid4().hex)
        approval_source_token = set_current_approval_source(created_approval_source)

    # Effective runtime for approval cleanup: callers normally pass it, but
    # fall back to the soul's own runtime so direct ``run_soul`` invocations
    # still cancel stale approval requests.
    effective_runtime = runtime
    if effective_runtime is None and isinstance(soul, KimiSoul):
        effective_runtime = soul.runtime

    wire = Wire(file_backend=wire_file)
    wire_token = _current_wire.set(wire)
    soul_token = _current_soul.set(soul)

    logger.debug("Starting UI loop with function: {ui_loop_fn}", ui_loop_fn=ui_loop_fn)
    ui_task = asyncio.create_task(ui_loop_fn(wire))

    logger.debug("Starting soul run")
    if slash_call is not None:
        soul_task = asyncio.create_task(_run_slash_command(soul, slash_call, user_input))
    else:
        soul_task = asyncio.create_task(soul.run(user_input))
    notification_task = asyncio.create_task(_pump_notifications_to_wire(runtime, wire))

    cancel_event_task = asyncio.create_task(cancel_event.wait())
    await asyncio.wait(
        [soul_task, cancel_event_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    try:
        if cancel_event.is_set():
            logger.debug("Cancelling the run task")
            soul_task.cancel()
            try:
                # Shield the await so that an outer task cancellation
                # (e.g. asyncio.run() shutdown) does not interrupt us
                # before the inner task has finished cancelling. After the
                # shielded await returns, we can reliably tell whether the
                # cancellation came from the inner task or from our own
                # task being cancelled.
                await asyncio.shield(soul_task)
            except asyncio.CancelledError:
                # If our own task is being cancelled, propagate the
                # cancellation instead of converting it to RunCancelled.
                # Converting it would make asyncio's shutdown logic report
                # an unhandled exception.
                if asyncio.current_task().cancelling():
                    raise
                # Inner task was cancelled by us; fall through to raise
                # RunCancelled below.
                pass
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError
            raise RunCancelled from None
        else:
            assert soul_task.done()  # either stop event is set or the run task is done
            cancel_event_task.cancel()
            try:
                await cancel_event_task
            except asyncio.CancelledError:
                # If our own task is being cancelled (e.g. during asyncio.run()
                # shutdown), propagate it instead of swallowing it together
                # with the expected cancel_event_task cancellation.
                if asyncio.current_task().cancelling():
                    raise
            soul_task.result()  # this will raise if any exception was raised in the run task
            # Auto-set title after first real turn (skip slash commands).
            if slash_call is None and isinstance(soul, KimiSoul):
                await _maybe_set_session_title(soul, user_input)
    finally:
        notification_task.cancel()
        try:
            await notification_task
        except asyncio.CancelledError:
            # Same as above: do not swallow an outer task cancellation.
            if asyncio.current_task().cancelling():
                raise
        try:
            await _deliver_notifications_to_wire_once(runtime, wire)
        except Exception:
            logger.exception("Failed to flush notifications to wire during shutdown")
        logger.debug("Shutting down the UI loop")
        # shutting down the wire should break the UI loop
        wire.shutdown()
        await wire.join()
        try:
            await asyncio.wait_for(ui_task, timeout=0.5)
        except QueueShutDown:
            logger.debug("UI loop shut down")
            pass
        except TimeoutError:
            logger.warning("UI loop timed out")
        finally:
            _current_wire.reset(wire_token)
            _current_soul.reset(soul_token)
        if (
            created_approval_source is not None
            and effective_runtime is not None
            and effective_runtime.approval_runtime is not None
        ):
            effective_runtime.approval_runtime.cancel_by_source(
                created_approval_source.kind,
                created_approval_source.id,
            )
        if approval_source_token is not None:
            reset_current_approval_source(approval_source_token)


_current_wire = ContextVar[Wire | None]("current_wire", default=None)
_current_soul = ContextVar[Soul | None]("current_soul", default=None)


def get_current_soul_or_none() -> Soul | None:
    """Return the soul that is currently running, if any."""
    return _current_soul.get()


def get_wire_or_none() -> Wire | None:
    """
    Get the current wire or None.
    Expect to be not None when called from anywhere in the agent loop.
    """
    return _current_wire.get()


def wire_send(msg: WireMessage) -> None:
    """
    Send a wire message to the current wire.
    Take this as `print` and `input` for souls.
    Souls should always use this function to send wire messages.
    """
    wire = get_wire_or_none()
    assert wire is not None, "Wire is expected to be set when soul is running"
    wire.soul_side.send(msg)


async def _pump_notifications_to_wire(runtime: Runtime | None, wire: Wire) -> None:
    while True:
        try:
            await _deliver_notifications_to_wire_once(runtime, wire)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Notification wire pump failed")
        await asyncio.sleep(1.0)


async def _deliver_notifications_to_wire_once(runtime: Runtime | None, wire: Wire) -> None:
    if runtime is None or runtime.role != "root":
        return

    from kimi_cli.notifications import NotificationView, to_wire_notification

    def _send_notification(view: NotificationView) -> None:
        wire.soul_side.send(to_wire_notification(view))

    await runtime.notifications.deliver_pending(
        "wire",
        limit=8,
        before_claim=runtime.background_tasks.reconcile,
        on_notification=_send_notification,
    )
