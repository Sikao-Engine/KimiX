from typing import Any, Callable, Optional
import asyncio
from pathlib import Path
import os
from kaos.path import KaosPath
from kimi_agent_sdk import Session
from kosong.chat_provider import ChatProvider
import kimix.base as base
from kimix.base import print_success, print_debug, percentage_str
from . import _globals
from .config import _create_config
from .system_prompt import get_system_prompt, SystemPromptType, SystemPromptCallback
from kimi_cli.soul.agent import BuiltinSystemPromptArgs
from kimi_agent_sdk import Config

def context_path() -> Path:
    user_home = Path.home()
    return user_home / '.kimi' / 'sessions'


def delete_session_dir() -> None:
    import shutil
    path = context_path()
    if path.exists():
        shutil.rmtree(path)
        print_success(f'{str(path)} deleted.')


def make_kaos_dir(obj: Any) -> KaosPath:
    if type(obj) is not KaosPath:
        return KaosPath(obj)
    return obj


def _ensure_skill_dirs(skill_dirs: Any) -> list[KaosPath]:
    from collections.abc import Iterable
    if skill_dirs is None:
        return []
    if type(skill_dirs) == list:
        return [make_kaos_dir(i) for i in skill_dirs]
    if isinstance(skill_dirs, Iterable) and not isinstance(skill_dirs, (str, bytes)):
        return [make_kaos_dir(i) for i in skill_dirs]
    return [make_kaos_dir(skill_dirs)]


async def _create_session_async(
    session_id: Optional[str] = None,
    work_dir: Optional[KaosPath] = None,
    skills_dir: Optional[KaosPath] = None,
    thinking: Optional[bool] = None,
    yolo: Optional[bool] = None,
    agent_file: Optional[Path] = None,
    resume: bool = False,
    provider_dict: dict[str, Any] | None = None,
    chat_provider: ChatProvider | None = None,
    agent_type: SystemPromptType = SystemPromptType.Worker,
    vfs_path: Path | None = None,
    extra_system_prompt: SystemPromptCallback | None = None,
    max_steps_per_turn: int | None = None,
    max_retries_per_step: int | None = None,
    max_ralph_iterations: int | None = None,
) -> Session:
    # create cache dir
    if work_dir:
        await (work_dir / '.kimix_cache').mkdir(parents=True, exist_ok=True)
    else:
        await KaosPath('.kimix_cache').mkdir(parents=True, exist_ok=True)
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
    system_prompts: Callable[[BuiltinSystemPromptArgs], str] | None = None
    if system_prompts is None:
        system_prompts = get_system_prompt(yolo, work_dir, extra_system_prompt, agent_type)
    if resume:
        session = await Session.resume(
            session_id=session_id,
            work_dir=work_dir if work_dir is not None else KaosPath('.'),
            skills_dirs=skills_dirs,
            yolo=yolo if yolo is not None else base._default_yolo,
            thinking=thinking if thinking is not None else base._default_thinking,
            config=cfg,
            agent_file=agent_file,
            # custom arguments
            custom_system_prompt=system_prompts,
            chat_provider=chat_provider,
            vfs_path=vfs_path,
            max_steps_per_turn=max_steps_per_turn,
            max_retries_per_step=max_retries_per_step,
            max_ralph_iterations=max_ralph_iterations,
        )
        if not session:
            print_debug(f'Session {session_id} not found.')
    if not session:
        session = await Session.create(
            session_id=session_id,
            work_dir=work_dir if work_dir is not None else KaosPath('.'),
            skills_dirs=skills_dirs,
            yolo=yolo if yolo is not None else base._default_yolo,
            thinking=thinking if thinking is not None else base._default_thinking,
            config=cfg,
            agent_file=agent_file,
            # custom arguments
            custom_system_prompt=system_prompts,
            chat_provider=chat_provider,
            vfs_path=vfs_path,
            max_steps_per_turn=max_steps_per_turn,
            max_retries_per_step=max_retries_per_step,
            max_ralph_iterations=max_ralph_iterations,
        )
    # save config
    custom_config = session.get_custom_config()
    if chat_provider:
        custom_config['chat_provider'] = chat_provider
    custom_config['provider_dict'] = provider_dict
    return session


def create_session(
    session_id: Optional[str] = None,
    work_dir: Optional[KaosPath] = None,
    skills_dir: Optional[KaosPath] = None,
    thinking: Optional[bool] = None,
    yolo: Optional[bool] = None,
    agent_file: Optional[Path] = None,
    resume: bool = False,
    provider_dict: dict[str, Any] | None = None,
    chat_provider: ChatProvider | None = None,
    agent_type: SystemPromptType = SystemPromptType.Worker,
    vfs_path: Path | None = None,
    extra_system_prompt: SystemPromptCallback | None = None,
    max_steps_per_turn: int | None = None,
    max_retries_per_step: int | None = None,
    max_ralph_iterations: int | None = None,
) -> Session:
    return asyncio.run(_create_session_async(
        session_id=session_id,
        work_dir=work_dir,
        skills_dir=skills_dir,
        thinking=thinking,
        yolo=yolo,
        agent_file=agent_file,
        resume=resume,
        provider_dict=provider_dict,
        chat_provider=chat_provider,
        agent_type=agent_type,
        vfs_path=vfs_path,
        extra_system_prompt=extra_system_prompt,
        max_steps_per_turn=max_steps_per_turn,
        max_retries_per_step=max_retries_per_step,
        max_ralph_iterations=max_ralph_iterations,
    ))


def set_ralph_loop(value: int, session: Session | None = None) -> None:
    if session is None:
        session = get_default_session()
    if value < 0:
        value = -1
    base._default_ralph = value
    if session:
        session._cli._runtime.config.loop_control.max_ralph_iterations = value


def close_session(session: Session) -> None:
    if not session:
        return
    asyncio.run(session.close())


async def close_session_async(session: Session) -> None:
    if not session:
        return
    await session.close()


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


def _create_default_session(resume: bool = True) -> Session:
    if _globals._default_session:
        return _globals._default_session
    if base._default_supervisor:
        _globals._default_session = create_session(
            session_id=None,
            resume=resume,
            agent_type=SystemPromptType.Supervisor,
            agent_file=base._default_agent_file_dir / 'agent_boss.json',
        )
        _globals._default_role = SystemPromptType.Supervisor
    else:
        _globals._default_session = create_session(session_id=None, resume=resume)
        _globals._default_role = SystemPromptType.Worker
    return _globals._default_session


def _print_usage(session: Session, time_seconds: float | None = None) -> None:
    if not getattr(_globals._should_print_usage, 'value', False):
        return
    s = percentage_str(session.status.context_usage)
    if time_seconds is not None:
        hours = int(time_seconds) // 3600
        minutes = (int(time_seconds) % 3600) // 60
        seconds = int(time_seconds) % 60
        time_text = f'  time: {hours}:{minutes:02d}:{seconds:02d}'
    else:
        time_text = ''
    print_success(
        f'Finished, context usage: {s}{time_text}'
    )


def print_usage(session: Session | None = None) -> None:
    if session is None:
        session = _create_default_session()
    s = percentage_str(session.status.context_usage)
    print_success(
        f'Context usage: {s}'
    )


def compact_default_context() -> None:
    if _globals._default_session and _globals._default_session.status.context_usage > 1e-8:
        print_debug('Start compacting...')
        import time
        start_time = time.time()
        last_usage = _globals._default_session.status.context_usage
        asyncio.run(_globals._default_session.compact())
        curr_usage = _globals._default_session.status.context_usage
        old_usage = percentage_str(last_usage)
        new_usage = percentage_str(curr_usage)
        end_time = time.time()
        time_seconds = end_time - start_time
        hours = int(time_seconds) // 3600
        minutes = (int(time_seconds) % 3600) // 60
        seconds = int(time_seconds) % 60
        time_text = f'  time: {hours}:{minutes:02d}:{seconds:02d}'
        print_success(
            f'Context usage from {old_usage} to {new_usage}  time: {time_text}'
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
        asyncio.run(_globals._default_session.clear())
        session = _globals._default_session
    else:
        session = _create_default_session(resume)
    if print_info:
        _print_usage(session)
