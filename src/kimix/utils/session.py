import asyncio
import atexit
import inspect
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from kaos.path import KaosPath
from kimi_cli.soul.agent import Runtime
from kosong.chat_provider import ChatProvider

import kimix.base as base
from kimi_agent_sdk import Session
from kimix.ui.printing import Color, Style
from kimix.ui.stream import percentage_and_token

from . import _globals
from .config import _create_config
from .system_prompt import SystemPromptCallback, SystemPromptType, get_system_prompt


_shutdown_hook_registered = False


def _shutdown_all_sessions() -> None:
    """Close every tracked session at interpreter shutdown.

    Sessions hold aiosqlite connections whose worker threads are non-daemon.
    If they are not closed before ``threading._shutdown`` joins non-daemon
    threads, the process hangs on exit (Ctrl+C then lands in
    ``threading._shutdown``). This hook is registered via
    ``threading._register_atexit`` so it runs *before* that join; a plain
    ``atexit`` handler would run too late.
    """
    sessions = list(_globals._live_sessions)
    _globals._live_sessions.clear()
    _globals._default_session = None
    for sess in sessions:
        try:
            close_session(sess)
        except BaseException:
            # Never let cleanup errors (including KeyboardInterrupt raised
            # while the shutdown event loop is being created on Windows)
            # escape during interpreter shutdown — the process must exit.
            pass


def _register_shutdown_hook() -> None:
    global _shutdown_hook_registered
    if _shutdown_hook_registered:
        return
    _shutdown_hook_registered = True
    register = getattr(threading, "_register_atexit", None)
    if register is not None:
        register(_shutdown_all_sessions)
    else:
        # Python < 3.9 fallback: plain atexit (runs after the thread join,
        # but still better than nothing).
        atexit.register(_shutdown_all_sessions)


def context_path() -> Path:
    """Session cache of the current work directory.

    Sessions are stored inside each work directory
    (``<work dir>/.kimix_cache``); the share dir (``~/.kimi/sessions``) is
    never created or used.
    """
    return Path.cwd() / '.kimix_cache'


def delete_session_dir() -> None:
    import shutil
    path = context_path()
    if path.exists():
        shutil.rmtree(path)
        base._stream.colorful_print_word(f'{str(path)} deleted.', fg=Color.BRIGHT_GREEN, styles=[Style.BOLD], require_new_line=True)


def make_kaos_dir(obj: Any) -> KaosPath:
    if type(obj) is not KaosPath:
        return KaosPath(obj)
    return obj


def _ensure_skill_dirs(skill_dirs: Any) -> list[KaosPath]:
    from collections.abc import Iterable
    if skill_dirs is None:
        return []
    if isinstance(skill_dirs, list):
        return [make_kaos_dir(i) for i in skill_dirs]
    if isinstance(skill_dirs, Iterable) and not isinstance(skill_dirs, (str, bytes)):
        return [make_kaos_dir(i) for i in skill_dirs]
    return [make_kaos_dir(skill_dirs)]


async def _create_session_async(
    session_id: Optional[str] = None,
    work_dir: Optional[KaosPath] = None,
    skills_dir: Optional[KaosPath] = None,
    agent_file: Optional[Path] = None,
    resume: bool = False,
    provider_dict: dict[str, Any] | None = None,
    chat_provider: ChatProvider | None = None,
    agent_type: SystemPromptType = SystemPromptType.Worker,
    vfs_path: Path | None = None,
    extra_system_prompt: SystemPromptCallback | None = None,
    anonymous: bool = False,
    custom_data: dict[str, Any] | None = None,
) -> Session:
    # create cache dir
    if work_dir:
        await (work_dir / '.kimix_cache').mkdir(parents=True, exist_ok=True)
    else:
        await KaosPath('.kimix_cache').mkdir(parents=True, exist_ok=True)
        work_dir = KaosPath('.')
    cfg, provider_dict = _create_config(provider_dict)
    session = None
    if agent_file is None:
        agent_file = base._default_agent_file
    else:
        if type(agent_file) is not Path:
            agent_file = Path(agent_file)
        if not agent_file.is_absolute():
            agent_file = base._default_agent_file_dir / agent_file
    skills_dirs = _ensure_skill_dirs(
        skills_dir) if skills_dir is not None else base.get_skill_dirs()
    system_prompts: Callable[[Runtime, bool], str] | None = None
    if system_prompts is None:
        system_prompts = get_system_prompt(
            work_dir=work_dir,
            extra_system_prompt=extra_system_prompt,
            agent_role=agent_type,
            max_system_prompt_tokens=cfg.loop_control.max_system_prompt_tokens,
        )
    if resume and session_id is not None:
        session = await Session.resume(
            session_id=session_id,
            work_dir=work_dir,
            skills_dirs=skills_dirs,
            yolo=base._default_yolo,
            thinking=base._default_thinking,
            config=cfg,
            agent_file=agent_file,
            # custom arguments
            custom_system_prompt=system_prompts,
            chat_provider=chat_provider,
            vfs_path=vfs_path,
            anonymous=anonymous,
            custom_data=custom_data,
            start_mcp_loading=False,
        )
        if not session:
            if not base._quiet:
                base._stream.colorful_print_word(f'Session {session_id} not found.', fg=Color.BRIGHT_CYAN, require_new_line=True)
    if not session:
        session = await Session.create(
            session_id=session_id,
            work_dir=work_dir,
            skills_dirs=skills_dirs,
            yolo=base._default_yolo,
            thinking=base._default_thinking,
            config=cfg,
            agent_file=agent_file,
            # custom arguments
            custom_system_prompt=system_prompts,
            chat_provider=chat_provider,
            vfs_path=vfs_path,
            anonymous=anonymous,
            custom_data=custom_data,
            start_mcp_loading=False,
        )
    # save config
    custom_config = session.get_custom_config()
    if chat_provider:
        custom_config['chat_provider'] = chat_provider
    custom_config['provider_dict'] = provider_dict
    _globals._track_session(session)
    _register_shutdown_hook()
    return session


def create_session(
    session_id: Optional[str] = None,
    work_dir: Optional[KaosPath] = None,
    skills_dir: Optional[KaosPath] = None,
    agent_file: Optional[Path] = None,
    resume: bool = False,
    provider_dict: dict[str, Any] | None = None,
    chat_provider: ChatProvider | None = None,
    agent_type: SystemPromptType = SystemPromptType.Worker,
    vfs_path: Path | None = None,
    extra_system_prompt: SystemPromptCallback | None = None,
    anonymous: bool = False,
    custom_data: dict[str, Any] | None = None,
) -> Session:
    return asyncio.run(_create_session_async(
        session_id=session_id,
        work_dir=work_dir,
        skills_dir=skills_dir,
        agent_file=agent_file,
        resume=resume,
        provider_dict=provider_dict,
        chat_provider=chat_provider,
        agent_type=agent_type,
        vfs_path=vfs_path,
        extra_system_prompt=extra_system_prompt,
        anonymous=anonymous,
        custom_data=custom_data,
    ))


def create_supervisor_session(
    session_id: Optional[str] = None,
    work_dir: Optional[KaosPath] = None,
    skills_dir: Optional[KaosPath] = None,
    resume: bool = False,
    provider_dict: dict[str, Any] | None = None,
    chat_provider: ChatProvider | None = None,
    vfs_path: Path | None = None,
    extra_system_prompt: SystemPromptCallback | None = None,
    anonymous: bool = False,
    custom_data: dict[str, Any] | None = None,
) -> Session:
    return create_session(
        session_id=session_id,
        work_dir=work_dir,
        skills_dir=skills_dir,
        agent_file=base._default_agent_file_dir / 'agent_boss.json',
        resume=resume,
        provider_dict=provider_dict,
        chat_provider=chat_provider,
        agent_type=SystemPromptType.Supervisor,
        vfs_path=vfs_path,
        extra_system_prompt=extra_system_prompt,
        anonymous=anonymous,
        custom_data=custom_data,
    )


# ---------------------------------------------------------------------------
# Close hooks
# ---------------------------------------------------------------------------
# Callbacks run right before a session is closed or cleared.  They exist so
# resources created *for* a session cannot outlive it: the ``subagent`` tool
# registers one to close and delete the (anonymous) sub-agent sessions it
# spawned as soon as the parent session goes away.
#
# Hooks are best-effort: a failing hook is reported and never prevents the
# session from being closed.
_session_close_hooks: list[Callable[[Any], Any]] = []


def register_session_close_hook(hook: Callable[[Any], Any]) -> None:
    """Register ``hook(session)`` to run before a session is closed/cleared.

    The hook may be sync or async and is called with the session being torn
    down.  Registering the same callable twice is a no-op.
    """
    if hook not in _session_close_hooks:
        _session_close_hooks.append(hook)


async def _run_session_close_hooks(session: Any) -> None:
    """Notify the registered hooks that *session* is going away (never raises)."""
    if not _session_close_hooks:
        return
    for hook in list(_session_close_hooks):
        try:
            result = hook(session)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            from kimix.ui.printing import print_debug

            name = getattr(hook, "__name__", repr(hook))
            print_debug(
                f"session close hook {name} failed for "
                f"{getattr(session, 'id', session)!r}: {exc}"
            )


def close_session(session: Session) -> None:
    if not session:
        return
    _globals._untrack_session(session)
    try:
        asyncio.run(_close_session_with_hooks(session))
    except KeyboardInterrupt:
        # Ctrl+C can land inside asyncio.run() while it is creating the
        # event loop (e.g. ProactorEventLoop's socketpair() on Windows),
        # especially when a background tool (like a glob) was running.
        # The session is being torn down anyway; the OS reclaims the
        # sockets and the aiosqlite worker thread is a daemon-less thread
        # that the interpreter will still join, but nothing more we can do
        # here — swallow the interrupt so shutdown completes cleanly.
        pass
    except RuntimeError as exc:
        # Transports bound to a now-closed ProactorEventLoop (Windows
        # Python 3.14) raise RuntimeError('Event loop is closed').
        # The OS will reclaim the socket, so we swallow it.
        if "Event loop is closed" not in str(exc):
            raise


async def _close_session_with_hooks(session: Session) -> None:
    """Run the close hooks, then close *session* (used by ``close_session``)."""
    await _run_session_close_hooks(session)
    await session.close()


async def close_session_async(session: Session) -> None:
    if not session:
        return
    _globals._untrack_session(session)
    await _close_session_with_hooks(session)


async def compact_context_async(session: Session | None = None) -> None:
    """Compact the context of a session."""
    if session is None:
        session = get_default_session()
    if session is None:
        return
    await session.compact()


def compact_context(session: Session | None = None) -> None:
    """Compact the context of a session (sync wrapper)."""
    asyncio.run(compact_context_async(session))


async def clear_session_async(session: Session | None = None) -> None:
    """Clear a session's context, tearing down what the conversation spawned.

    The conversation is discarded, so everything created by it (e.g. the
    anonymous sub-agent sessions of the ``subagent`` tool) is closed and
    deleted with it instead of leaking a session directory and an aiosqlite
    worker thread.
    """
    if session is None:
        session = get_default_session()
    if session is None:
        return
    await _run_session_close_hooks(session)
    await session.clear()


async def clear_context_async(session: Session | None = None) -> None:
    """Clear the context of a session (alias of :func:`clear_session_async`)."""
    await clear_session_async(session)


def clear_context(session: Session | None = None) -> None:
    """Clear the context of a session (sync wrapper)."""
    asyncio.run(clear_context_async(session))


def get_cancel_event(session: Session | None = None) -> asyncio.Event | None:
    """Get the cancel event of a session."""
    if session is None:
        session = get_default_session()
    return getattr(session, '_cancel_event', None)


def cancel_prompt(session: Session | None = None) -> None:
    """Set the cancel event on a session to cancel the current prompt."""
    if session is None:
        session = get_default_session()
    if session is not None:
        session.cancel()


def get_default_session() -> Session | None:
    return _globals._default_session


async def _create_default_session_async(resume: bool = True) -> Session:
    """Async variant of ``_create_default_session``.

    Safe to call from within a running event loop: it awaits
    ``_create_session_async`` directly instead of nesting ``asyncio.run``.
    """
    if _globals._default_session:
        return _globals._default_session
    _globals._default_session = await _create_session_async(session_id=None, resume=resume)
    _globals._default_role = SystemPromptType.Worker

    # Populate _cli_sessions cache
    try:
        _globals._refresh_cli_sessions(_globals._default_session._cli.session.work_dir)
    except Exception:
        pass

    return _globals._default_session


def _create_default_session(resume: bool = True) -> Session:
    return asyncio.run(_create_default_session_async(resume))


def _print_usage(session: Session, time_seconds: float | None = None) -> None:
    if not getattr(_globals._should_print_usage, 'value', False):
        return
    s = percentage_and_token(session)
    if time_seconds is not None:
        hours = int(time_seconds) // 3600
        minutes = (int(time_seconds) % 3600) // 60
        seconds = int(time_seconds) % 60
        time_text = f'  time: {hours}:{minutes:02d}:{seconds:02d}'
    else:
        time_text = ''
    base._stream.colorful_print_word(
        f'Finished, context usage: {s}{time_text}', fg=Color.BRIGHT_GREEN, styles=[Style.BOLD], require_new_line=True
    )


def print_usage(session: Session | None = None) -> None:
    if session is None:
        session = _create_default_session()
    s = percentage_and_token(session)
    base._stream.colorful_print_word(
        f'Context usage: {s}', fg=Color.BRIGHT_GREEN, styles=[Style.BOLD], require_new_line=True
    )


def compact_default_context() -> None:
    if _globals._default_session and _globals._default_session.status.context_usage > 1e-8:
        if not base._quiet:
            base._stream.colorful_print_word('Start compacting...', fg=Color.BRIGHT_CYAN, require_new_line=True, flush=True)
        import time
        start_time = time.time()
        old_usage = percentage_and_token(_globals._default_session)
        asyncio.run(_globals._default_session.compact())
        new_usage = percentage_and_token(_globals._default_session)
        end_time = time.time()
        time_seconds = end_time - start_time
        hours = int(time_seconds) // 3600
        minutes = (int(time_seconds) % 3600) // 60
        seconds = int(time_seconds) % 60
        time_text = f'  time: {hours}:{minutes:02d}:{seconds:02d}'
        base._stream.colorful_print_word(
            f'Context usage from {old_usage} to {new_usage}{time_text}', fg=Color.BRIGHT_GREEN, styles=[Style.BOLD], require_new_line=True
        )


def get_tool_call_errors(session: Session | None = None) -> list[dict[str, Any]]:
    """Return a list of tool-call errors from the session."""
    return []


def clear_default_context(force_create: bool = False, resume: bool = False, print_info: bool = True) -> None:
    if _globals._default_session:
        if not force_create and _globals._default_session.status.context_usage < 1e-8:
            if print_info:
                _print_usage(_globals._default_session)
            return
        asyncio.run(clear_session_async(_globals._default_session))
        session = _globals._default_session
    else:
        session = _create_default_session(resume)
    if print_info:
        _print_usage(session)
