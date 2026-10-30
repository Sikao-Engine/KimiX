import importlib
import inspect
import os
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import orjson
import pendulum

import kimix.base as base

from kimi_cli.tools import resolve_tool_class

from . import constants
from .utils import _input, _split_text


def _read_multi_line(text_arr: list[str], *, allow_cancel: bool = True) -> tuple[list[str], bool]:
    """Read multi-line input until /end or /cancel.

    Returns (lines, cancelled) where lines are the text lines collected
    (empty if /cancel was entered) and cancelled is True if /cancel was entered.
    """
    lines: list[str] = []
    while True:
        s = _input('', text_arr)
        if s.strip() == '/end':
            break
        if allow_cancel and s.strip() == '/cancel':
            return [], True
        lines.append(s)
    return lines, False


import kimix.utils._globals as _globals
from kimix.base import sync_all
from kimix.ui.printing import (
    Color,
    colorful_text,
    print_debug,
    print_error,
    print_info,
    print_success,
    print_warning,
)
from kimix.utils import (
    SystemPromptType,
    _create_default_session,
    clear_default_context,
    close_session,
    collect_tool_invocations,
    compact_default_context,
    create_session,
    create_supervisor_session,
    fix_error,
    get_default_session,
    print_usage,
    prompt,
    prompt_plan,
)
from kimix.utils.event_loop import run_on_event_loop

from .init import init

exec_ctx: dict[str, Any] = {}


def _cmd_help(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    print(constants.HELP_STR)
    return None, False


def _cmd_clear(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    clear_default_context()
    return None, False


def _cmd_compact(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    compact_default_context()
    return None, False

def _cmd_export(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    session = get_default_session()
    if session is None:
        print_error('No active session to export.')
        return None, False
    if len(task_split) < 2:
        print_error('Command must be /export:file')
        return None, False
    output_path = ':'.join(task_split[1:]) if len(task_split) > 1 else None
    try:
        output, count = run_on_event_loop(session.export(output_path=output_path))
        print_success(f'Exported {count} messages to {output}')
    except Exception as e:
        print_error(f'Export failed: {e}')

    return None, False


def _cmd_resume(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    if len(task_split) < 2:
        print_error('Command must be /resume:session_id')
        return None, False
    session_id = ':'.join(task_split[1:])
    session = get_default_session()
    if session:
        close_session(session)
    _globals._default_session = None
    _globals._default_role = None
    try:
        new_session = create_session(session_id=session_id, resume=True)
        _globals._default_session = new_session
        _globals._default_role = SystemPromptType.Worker

        # Update _cli_sessions cache
        cli_sess = new_session._cli.session
        _globals._add_cli_session(
            cli_sess.id,
            cli_sess.title,
            cli_sess.updated_at,
            context_usage=new_session.status.context_usage,
            context_tokens=new_session.status.context_tokens,
        )

        print_success(f'Resumed session {session_id}')
    except Exception as e:
        print_error(f'Failed to resume session: {e}')
    return None, False


async def _release_session_resources(session: Any) -> None:
    """Release file/network resources of an SDK session without deleting it."""
    if session._cancel_event is not None:
        session._cancel_event.set()
    await session._cleanup_tools()
    soul = getattr(session._cli, "soul", None)
    if soul is not None:
        try:
            await soul.close()
        except Exception:
            pass
    await session._cli.session.close_context_db()
    try:
        await session._close_chat_provider()
    except Exception:
        pass


def _cmd_store(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    if len(task_split) < 2:
        print_error('Command must be /store:session_id')
        return None, False
    target_id = ':'.join(task_split[1:])
    session = get_default_session()
    if session is None:
        print_error('No active session to store.')
        return None, False

    source_id = session.id
    if target_id == source_id:
        print_error('Target session name must be different from current session name.')
        return None, False

    work_dir = session._cli.session.work_dir
    old_anonymous = session._anonymous

    async def _do_copy() -> Any:
        from kimi_cli.session import Session as CliSession
        await _release_session_resources(session)
        return await CliSession.copy(work_dir, source_id, target_id)

    try:
                    target = run_on_event_loop(_do_copy())
    except Exception as e:
        import traceback
        print_error(f'Store failed: {e}')
        print_error(traceback.format_exc())
        # Attempt to recover the original session so the CLI is not left broken.
        try:
            new_session = create_session(
                session_id=source_id,
                work_dir=work_dir,
                resume=True,
                anonymous=old_anonymous,
            )
            _globals._default_session = new_session
            _globals._default_role = SystemPromptType.Worker

            # Update _cli_sessions cache
            cli_sess = new_session._cli.session
            _globals._add_cli_session(
                cli_sess.id,
                cli_sess.title,
                cli_sess.updated_at,
                context_usage=-1.0,
                context_tokens=0,
            )
        except Exception as resume_err:
            print_error(f'Failed to resume original session: {resume_err}')
        return None, False

    # Prevent the old anonymous SDK object from deleting the original directory on GC.
    session._closed = True

    try:
        new_session = create_session(
            session_id=source_id,
            work_dir=work_dir,
            resume=True,
            anonymous=old_anonymous,
        )
        _globals._default_session = new_session
        _globals._default_role = SystemPromptType.Worker

        # Update _cli_sessions cache
        cli_sess = new_session._cli.session
        _globals._add_cli_session(
            cli_sess.id,
            cli_sess.title,
            cli_sess.updated_at,
            context_usage=new_session.status.context_usage,
            context_tokens=new_session.status.context_tokens,
        )
    except Exception as e:
        import traceback
        print_error(f'Store succeeded but failed to resume original session: {e}')
        print_error(traceback.format_exc())
        return None, False

    print_success(f'Session stored as {target.id}')
    return None, False


def _cmd_load(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    if len(task_split) < 2:
        print_error('Command must be /load:session_id')
        return None, False
    source_id = ':'.join(task_split[1:])

    from kaos.path import KaosPath
    from kimi_cli.session import Session as CliSession

    current = get_default_session()
    work_dir = KaosPath('.')
    if current is not None:
        work_dir = current._cli.session.work_dir

    # Confirm replacing a current session that has used context tokens.
    if current is not None:
        try:
            current_token_count = current._cli.soul.context.token_count
        except Exception:
            current_token_count = 0
        if current_token_count > 0:
            print_warning(
                f'Current session "{current.id}" has {current_token_count} context tokens. '
                'Loading will release it. Continue? (y/n)'
            )
            answer = _input('', text_arr).strip().lower()
            while answer not in ('y', 'n'):
                print_warning('Please enter y or n.')
                answer = _input('', text_arr).strip().lower()
            if answer != 'y':
                print_info('Load cancelled.')
                return None, False

    async def _do_copy() -> str:
        new_id = uuid.uuid4().hex
        if current is not None and current.id == source_id:
            # The source is the active session: release its locks first.
            await _release_session_resources(current)
        await CliSession.copy(work_dir, source_id, new_id)
        return new_id

    try:
                    new_id = run_on_event_loop(_do_copy())
    except Exception as e:
        import traceback
        print_error(f'Load failed: {e}')
        print_error(traceback.format_exc())
        return None, False

    # Close the previous current session now that the copy is safely on disk.
    if current is not None:
        close_session(current)

    try:
        new_session = create_session(
            session_id=new_id,
            work_dir=work_dir,
            resume=True,
            anonymous=True,
        )
        _globals._default_session = new_session
        _globals._default_role = SystemPromptType.Worker

        # Update _cli_sessions cache
        cli_sess = new_session._cli.session
        _globals._add_cli_session(
            cli_sess.id,
            cli_sess.title,
            cli_sess.updated_at,
            context_usage=new_session.status.context_usage,
            context_tokens=new_session.status.context_tokens,
        )
    except Exception as e:
        import traceback
        print_error(f'Loaded session but failed to resume copy: {e}')
        print_error(traceback.format_exc())
        return None, False

    print_success(f'Loaded session {source_id} into anonymous session {new_id}')
    return None, False


def _cmd_sessions(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    """Handle /sessions and /sessions:<name> commands.

    /sessions        - list all known sessions from in-memory cache
    /sessions:<name> - create a new session named <name> and switch to it
    """
    from kimix.utils import _create_default_session

    # Case 1: /sessions:<name> — create a new named session
    if len(task_split) >= 2:
        new_name = ':'.join(task_split[1:])

        # Close current session if any
        session = get_default_session()
        if session:
            close_session(session)
        _globals._default_session = None
        _globals._default_role = None

        try:
            new_session = create_session(session_id=new_name, resume=False)
            _globals._default_session = new_session
            _globals._default_role = SystemPromptType.Worker

            # Update cache
            cli_sess = new_session._cli.session
            _globals._add_cli_session(
                cli_sess.id,
                cli_sess.title,
                cli_sess.updated_at,
                context_usage=new_session.status.context_usage,
                context_tokens=new_session.status.context_tokens,
            )

            print_success(f'Created and switched to session: {new_name}')
        except Exception as e:
            print_error(f'Failed to create session "{new_name}": {e}')
            # Try to recover: create a fresh anonymous session
            try:
                _create_default_session(resume=False)
            except Exception:
                pass

        return None, False

    # Case 2: /sessions — list all known sessions
    sessions_dict = _globals._cli_sessions

    if not sessions_dict:
        print_warning('No sessions found.')
        return None, False

    # Get current session id and update its context usage in the cache
    session = get_default_session()
    current_id = session._cli.session.id if session else None
    if session is not None:
        try:
            status = session.status
            sessions_dict[current_id]['context_usage'] = status.context_usage
            sessions_dict[current_id]['context_tokens'] = status.context_tokens
        except Exception:
            pass

    # Build sortable list
    items = [
        (
            sid,
            info.get('title', 'Untitled'),
            info.get('updated_at', 0.0),
            info.get('context_usage', -1.0),
            info.get('context_tokens', 0),
        )
        for sid, info in sessions_dict.items()
    ]
    items.sort(key=lambda x: x[2], reverse=True)

    id_width = max(len('session id'), *(len(sid) for sid, _, _, _, _ in items))
    print_info(
        f'{" ":1}  {"session id":<{id_width}}  {"updated at":<19}  {"context usage":<22}  title'
    )
    for sid, title, updated_at, context_usage, context_tokens in items:
        marker = '*' if sid == current_id else ' '
        updated_str = pendulum.from_timestamp(updated_at).strftime('%Y-%m-%d %H:%M:%S')
        if context_usage >= 0.0:
            usage_str = f'{context_usage * 100:.1f}% ({context_tokens} tokens)'
        else:
            usage_str = '-'
        print(f'{marker}  {sid:<{id_width}}  {updated_str}  {usage_str:<22}  {title}')
    return None, False


def _cmd_exit(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    session = get_default_session()
    if session:
        close_session(session)
    _globals._default_session = None
    _globals._default_role = None
    # The process is about to finish: remove the shared tool temp folder
    # (``.kimix_cache/tmp_<pid>``) plus leftovers from previously killed
    # processes before the user sees the goodbye, so anonymous CLI runs leave
    # no trace behind.  Best-effort; never blocks the exit.
    try:
        from kimi_cli.tools.common import cleanup_temp_folder
        cleanup_temp_folder()
    except Exception:
        pass
    print_success('bye!')
    return None, True


def _cmd_context(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    print_usage()
    return None, False



def _cmd_cmd(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    if len(task_split) < 2:
        print_error('Command must be /cmd:xx yy')
        return None, False
    cmd = ':'.join(task_split[1:])
    try:
        result = os.system(cmd)
        if result == 0:
            print_success('Done.')
        else:
            print_warning('Failed.')
    except Exception as e:
        print_error(str(e))
    return None, False



def _cmd_fix(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    if len(task_split) < 2:
        print_error('Command must be /fix:<command>')
        return None, False
    command_to_fix = (':'.join(task_split[1:])).strip()
    if not command_to_fix:
        print_error('Command must be /fix:<command>')
        return None, False
    fix_error(command_to_fix, session=get_default_session())
    return None, False


def _cmd_plan(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    file_path: str | None = None
    if len(task_split) >= 2:
        file_path = ':'.join(task_split[1:]).strip()
    else:
        import secrets
        cache_dir = Path('.kimix_cache')
        cache_dir.mkdir(parents=True, exist_ok=True)
        file_path = str(cache_dir / f'plan_{secrets.token_hex(8)}.md')
    print(
        f'\n>>>> Start input requirement for plan, end with {colorful_text("/end", Color.YELLOW)}, '
        f'cancel with {colorful_text("/cancel", Color.YELLOW)}')
    text, _ = _read_multi_line(text_arr)
    requirement = '\n'.join(text).strip()
    if not requirement:
        print_warning('No requirement provided.')
        return None, False
    prompt_plan(requirement, file_path)
    return None, False


def _cmd_txt(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    print(
        f'\n>>>> Start input multiple-lines, end with {colorful_text('/end', Color.YELLOW)}, cancel with {colorful_text('/cancel', Color.YELLOW)}')
    text, _ = _read_multi_line(text_arr)
    for i in _split_text(text, _command_map_keys):
        text_arr.append(i)
    return None, False


def _cmd_file(task_split: list[str], text_arr: list[str]) -> tuple[str | None, bool]:
    if len(task_split) < 2:
        print_error(f'command format error, must be /file:path')
        return None, False
    file_name_str = ':'.join(task_split[1:])
    file_path = Path(file_name_str)
    if not file_path.is_file():
        print_error(f'file not found: {file_path}')
        return None, False
    return file_path.read_text(encoding='utf-8', errors='replace'), False


def _cmd_init(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    init()
    _globals._default_session = None
    _globals._default_role = None
    _create_default_session()
    print_success('Initialized.')
    return None, False


def _cmd_supervisor(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    """Start a supervisor session with multi-line input text."""
    print(
        f'\n>>>> Start input for supervisor, end with {colorful_text("/end", Color.YELLOW)}, '
        f'cancel with {colorful_text("/cancel", Color.YELLOW)}')
    text, _ = _read_multi_line(text_arr)
    task_prompt = '\n'.join(text).strip()
    if not task_prompt:
        print_warning('No input provided for supervisor.')
        return None, False

    print_debug('Creating supervisor session...')
    try:
        supervisor_session = create_supervisor_session()
    except Exception as e:
        print_error(f'Failed to create supervisor session: {e}')
        return None, False

    try:
        prompt(prompt_str=task_prompt, session=supervisor_session, format_output=True)
    except Exception as e:
        print_error(f'Supervisor prompt failed: {e}')
    finally:
        close_session(supervisor_session)

    return None, False


def _cmd_swarm(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    """Start a swarm session with multi-line input text."""
    print(
        f'\n>>>> Start input for swarm, end with {colorful_text("/end", Color.YELLOW)}, '
        f'cancel with {colorful_text("/cancel", Color.YELLOW)}')
    text, cancelled = _read_multi_line(text_arr)
    if cancelled:
        return None, False
    swarm_prompt = '\n'.join(text).strip()
    if not swarm_prompt:
        print_warning('No input provided for swarm.')
        return None, False

    print_debug('Creating swarm session...')
    try:
        swarm_session = create_session(
            agent_file=base._default_agent_file_dir / 'agent_worker.json',
            agent_type=SystemPromptType.SwarmLeader,
            custom_data={'is_swarm_session': True},
        )
    except Exception as e:
        print_error(f'Failed to create swarm session: {e}')
        return None, False

    try:
        prompt(prompt_str=swarm_prompt, session=swarm_session, format_output=True)
    except Exception as e:
        print_error(f'Swarm prompt failed: {e}')
    finally:
        close_session(swarm_session)

    return None, False


def _cmd_todo(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    if len(task_split) < 2:
        print_error('Command must be /todo:<path>')
        return None, False
    file_name_str = ':'.join(task_split[1:])
    file_path = Path(file_name_str)
    if not file_path.is_file():
        print_error(f'file not found: {file_path}')
        return None, False

    import regex as re

    from kimix.parser import (
        CParser,
        HtmlParser,
        LispParser,
        PascalParser,
        PythonParser,
        ShellParser,
        SqlParser,
    )

    suffix = file_path.suffix.lower()
    parser = None
    if suffix == '.py':
        parser = PythonParser()
    elif suffix in {'.c', '.cpp', '.cc', '.cxx', '.h', '.hpp', '.java', '.js', '.ts', '.jsx', '.tsx', '.cs', '.go', '.rs'}:
        parser = CParser()
    elif suffix in {'.sh', '.bash', '.zsh'}:
        parser = ShellParser()
    elif suffix in {'.html', '.htm', '.xml', '.svg'}:
        parser = HtmlParser()
    elif suffix in {'.pas', '.pp', '.inc', '.dpr'}:
        parser = PascalParser()
    elif suffix in {'.lisp', '.lsp', '.clj', '.scm', '.ss', '.el'}:
        parser = LispParser()
    elif suffix == '.sql':
        parser = SqlParser()
    else:
        print_error(f'Unsupported file type: {suffix}')
        return None, False

    try:
        result = parser.parse_file(str(file_path))
    except Exception as e:
        print_error(f'Parse failed: {e}')
        return None, False

    todos = [c for c in result.comments if re.search(r'(?<![a-zA-Z0-9])TODO(?![a-zA-Z0-9])', c.content.upper())]
    if not todos:
        print_warning('No TODO comments found.')
        return None, False

    # Build formatted TODO items
    if len(todos) == 1:
        # Single TODO: short format, no numbering
        single = todos[0]
        todo_items = f'Line {single.line}: {single.content.strip()}'
        prompt_str = (
            f'Implement the TODO in {file_path}:\n'
            f'{todo_items}'
            '\n\nRemove TODO comment after done.'
        )
    else:
        format_todo = lambda i, todo: f'{i}. Line {todo.line}: {todo.content.strip()}'
        todo_lines = [format_todo(i, todo) for i, todo in enumerate(todos, 1)]
        todo_items = '\n'.join(todo_lines)
        prompt_str = (
            f'Implement all TODOs in {file_path} at once:\n\n'
            f'{todo_items}\n\n'
            'Make sure to handle each TODO completely.'
            '\n\nRemove TODO comment after done.'
        )

    try:
        print_info(prompt_str)
        prompt(prompt_str=prompt_str, format_output=True)
    except Exception as e:
        print_error(f'Prompt failed: {e}')

    return None, False


def _cmd_code(task_split: list[str], text_arr: list[str]) -> tuple[str | None, bool]:
    """Run a script file with optional arguments.

    Usage: /code:<script_path> [arg1] [arg2] ...

    For Python scripts (.py), runs with exec like core.py.
    For other scripts, runs directly as an executable.
    Supports both absolute and relative paths.
    """
    global exec_ctx
    if len(task_split) < 2:
        print_error("Command must be /code:<script_path> [args...]")
        return None, False

    cmd_part = ":".join(task_split[1:])
    parts = cmd_part.split()
    if not parts:
        print_error("Script path is required.")
        return None, False

    script_path_str = parts[0]
    script_args = parts[1:]

    script_path = Path(script_path_str)
    if not script_path.is_file():
        # Try resolving relative to current directory
        resolved = constants.curr_dir / script_path_str
        if resolved.is_file():
            script_path = resolved
        else:
            print_error(f"Script file not found: {script_path}")
            return None, False

    if script_path.suffix.lower() == ".py":
        # Use exec like core.py does for .py files
        import sys
        with open(script_path, "r", encoding="utf-8", errors="replace") as f:
            s = f.read()
        # Temporarily replace sys.argv so argparse-based scripts see the right args
        _old_argv = sys.argv
        sys.argv = [str(script_path)] + script_args
        print_info(f"Executing {script_path.name}", end="\n\n")
        try:
            exec_ctx["__file__"] = str(script_path)
            exec(s, exec_ctx)
            print_success(f"Done.")
        except KeyboardInterrupt:
            print_warning("Keyboard Interrupt.")
        except Exception as e:
            import traceback
            print_error(str(e))
            print_error(traceback.format_exc())
        finally:
            sys.argv = _old_argv
            sync_all()
    else:
        import subprocess
        import sys

        try:
            cmd = [str(script_path)] + script_args
            print_info(f"Running: {' '.join(cmd)}")
            # Let subprocess inherit stdout/stderr so output is displayed live
            result = subprocess.run(cmd, capture_output=False, text=True)
            if result.returncode == 0:
                print_success(f"Done (exit code 0).")
            else:
                print_warning(f"Exited with code {result.returncode}.")
        except FileNotFoundError:
            print_error(f"Executable not found: {script_path}")
        except Exception as e:
            print_error(str(e))

    return None, False


def _context_is_non_empty(session: Any) -> bool:
    """Return True when the session has real conversation content."""
    try:  # primary signal, mirrors clear/compact context checks
        if session.status.context_usage > 1e-8:
            return True
    except Exception:
        pass
    try:  # fallback: soul context token count
        if session._cli.soul.context.token_count > 0:
            return True
    except Exception:
        pass
    try:  # fallback: kimi_cli Session.is_empty() (wire file / context.db / context.jsonl)
        cli_session = session._cli.session
        is_empty = getattr(cli_session, 'is_empty', None)
        if callable(is_empty) and not is_empty():
            return True
    except Exception:
        pass
    return False


_CONTINUE_PROMPT = (
    # Terse on purpose: this synthetic user message is appended to the
    # session context, so keep token cost low. Mirrors the soul's built-in
    # continuation nudge style (see _MAX_TEXT_BLOCK_CONTINUATION_ROUNDS in
    # kimi_cli/soul/kimisoul.py).
    '[Continue] Pick up your unfinished work where you left off; '
    'finish the remaining steps and report only when fully done.'
)


def _session_is_done(session: Any) -> bool:
    """Return True when the session is marked done.

    Primary signal: the SDK session's ``_done`` flag, which is True only
    when the last LLM stream finished normally (see
    ``kimi_agent_sdk.Session.prompt``). Fallback: a non-empty todo list
    whose items are all ``done`` (covers resumed sessions whose flag has
    not been re-established yet).
    """
    if getattr(session, '_done', False):
        return True
    from kimix.utils.prompt import _get_session_todos

    todos = _get_session_todos(session)
    if not todos:
        return False

    def _all_done(items: list[Any]) -> bool:
        for item in items:
            status = item.get('status') if isinstance(item, dict) else getattr(item, 'status', None)
            if status != 'done':
                return False
            children = item.get('children') if isinstance(item, dict) else getattr(item, 'children', None)
            if children and not _all_done(children):
                return False
        return True

    return _all_done(todos)


def _reflection_context_stats(session: Any) -> str:
    """Return a short human-readable description of the session context size."""
    try:  # preferred: soul context history + token count
        context = session._cli.soul.context
        n_messages = len(context.history)
        token_count = context.token_count
        return f'{n_messages} messages, {token_count} tokens'
    except Exception:
        pass
    try:  # fallback: status token count
        return f'{session.status.context_tokens} tokens'
    except Exception:
        pass
    return 'visible above'


def _builtin_tools_listing(repo_root: Path, kimix_tools_dir: Path) -> str:
    """Return the exact file path of every builtin tool, one tool per line.

    Derived from the worker tool manifest in ``src/kimix/agent_worker.json``
    (whose ``tools`` entries are all ``kimi_cli.tools.*``) so it can never drift from the
    actual toolset: each ``module:attr`` entry is resolved through the shared
    ``resolve_tool_class`` helper to the source file that defines the tool
    class. Paths are relative to the repo root (e.g.
    ``kimi-cli/src/kimi_cli/tools/file/bash/bash_tool.py``) so the reflection agent can
    edit the precise source file.
    """
    manifest_path = kimix_tools_dir.parent / "agent_worker.json"
    try:
        manifest = orjson.loads(manifest_path.read_bytes())
    except (OSError, ValueError):
        return ""

    lines: list[str] = []
    for tool_path in (manifest.get("agent") or {}).get("tools") or []:
        module_name, _, attr = tool_path.partition(":")
        if not module_name or not attr:
            continue
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        tool_cls = resolve_tool_class(module, attr)
        if tool_cls is None:
            continue
        try:
            path = Path(inspect.getfile(tool_cls)).resolve()
            rel = path.relative_to(repo_root.resolve()).as_posix()
        except (TypeError, OSError, ValueError):
            continue
        lines.append(f"- `{attr}` — `{rel}`")
    return "\n".join(lines)


def _build_reflection_prompt(session: Any, *, report_path: Path | None = None) -> str:
    """Build the /reflection prompt embedding source paths, AGENTS.md and rules."""
    agent_src = Path(__file__).resolve().parent.parent          # ...\src\kimix
    repo_root = agent_src.parent.parent                          # ...\kimi-agent
    kimix_tools_dir = agent_src / 'tools'                        # ...\src\kimix\tools
    kimi_cli_tools_dir = repo_root / 'kimi-cli' / 'src' / 'kimi_cli' / 'tools'
    agents_md_path = repo_root / 'AGENTS.md'
    system_prompt_path = agent_src / 'utils' / 'system_prompt.py'  # ...\src\kimix\utils\system_prompt.py
    worker_agent_json = agent_src / 'agent_worker.json'            # ...\src\kimix\agent_worker.json
    soul_dir = repo_root / 'kimi-cli' / 'src' / 'kimi_cli' / 'soul'  # ...\kimi-cli\src\kimi_cli\soul
    config_path = repo_root / 'kimi-cli' / 'src' / 'kimi_cli' / 'config.py'
    try:
        agents_md = agents_md_path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        agents_md = '(AGENTS.md not found)'
    if report_path is None:
        report_path = repo_root / 'docs' / f'reflection_report_{pendulum.now().format("YYYYMMDD_HHMMSS")}.md'
    context_stats = _reflection_context_stats(session)
    builtin_tools = _builtin_tools_listing(repo_root, kimix_tools_dir)
    return f'''# Reflection Task

Reflect on the conversation context above. Find misunderstandings caused by the
current agent design, then change the source code to make this project better.

## Context
- Agent source code path: `{agent_src}`
- Builtin tools dirs:
  - `{kimix_tools_dir}`
  - `{kimi_cli_tools_dir}`
- Current context: {context_stats} (full conversation is visible above)

## Builtin tools (exact file paths)
Every builtin tool and the exact file where its implementation lives:
{builtin_tools}

## Architecture map (source of truth)
- System prompt: `{system_prompt_path}` — builds the per-role system prompt as
  `{{TOOL_CONVENTIONS}}{{AGENT_ROLE}}:{{NUMBERED}}{{AGENTS_MD}}{{SKILLS}}`; roles via
  `SystemPromptType` (Worker/TodoMaker/Thinker/TrivialSubAgent/Supervisor/Reader/SwarmLeader);
  `get_system_prompt()` returns a per-runtime builder, picks the active shell, enforces
  `max_system_prompt_tokens`.
- Worker tool manifest: `{worker_agent_json}` — the exact tool list for the worker agent
  (`agent.extend=default`): Bash, pwsh, Run, python, job_output, todo_list,
  read, read_image, edit, write, subagent, list_agents, interrupt_agent,
  workflow, glob, grep, fetch_url, web_search, compact (from `kimi_cli.tools.*`
  and `kimi_cli.tools.*`).
- Soul runtime: `{soul_dir}`
  - agent.py — Runtime + BuiltinSystemPromptArgs; loads AGENTS.md, skills, additional dirs
  - kimisoul.py — main agent loop: step/turn lifecycle, retries, auto-compaction, reminders
  - compaction.py — context compaction (modes, adaptive preserve depth, auto-trigger)
  - context.py / context_db.py / context_records.py — context storage (JSONL/SQLite + migration), records
  - context_pruning.py — smart history removal (Tier A ephemeral / B elision / C micro-compress)
  - message.py — message helpers + `<system-reminder>` tags + history normalization
  - verification_gate.py — soul-layer verification nudges for todos/code edits
  - approval.py — approval state (yolo/afk/auto-approve)
  - btw.py / steer.py / slash.py — side questions, mid-stream steering, slash registry
  - toolset.py / tool_taxonomy.py — tool loading & classification
  - denwarenji.py — D-Mail; history_index.py — BM25 history index; llm_request_recorder.py — request tracing

- Config: `{config_path}` — pydantic `Config` (model/provider, loop_control, background,
  notifications, services, mcp, hooks, skills); `LoopControl` drives loop/compaction/prune/
  reminder thresholds and toggles.

## AGENTS.md
{agents_md}

## Testing rules
- After writing any Python file, run: `uv run tools/syntax_check.py <file> [<file> ...]`
- After writing any code, write tests to cover it.
- Fix all errors reported by the syntax checker before proceeding.
- Review diffs with: `uv run tools/git_diff.py <file> [<file> ...]`
- After changing `pyproject.toml`, run: `uv sync --extra=all`

## Change code rules
- No case-by-case fixes: only generic, root-cause changes.
- Common usage must be written into shared/generic code.
- Be extremely careful about adding new tools: add only when very necessary.
- Follow the AGENTS.md performance rule (orjson, msgspec, uvloop, apsw, regex,
  rapidfuzz, xxhash, pybase64, pendulum instead of builtin counterparts).

## Report
After finishing all changes, write a report introducing the changes to:
`{report_path}`
Include: the misunderstanding found, what was changed, and why.
'''


# ---------------------------------------------------------------------------
# /reflection_bash:<prompt_file_path>
# ---------------------------------------------------------------------------
# The Bash IR review command: it reads the *current session context*, extracts
# every ``bash`` tool invocation (arguments + result message + result output,
# failed ones marked), runs the live fixer over each command so the emitted
# prompt shows what the parser produced, and writes one markdown prompt file that
# asks the agent to review the IR parser.

#: Tool names whose invocations are collected by ``/reflection_bash``.
_BASH_TOOL_NAMES: tuple[str, ...] = ('bash',)

#: Per-invocation output budget for the emitted JSON (head+tail folding): tool
#: output can be hundreds of KB and the failed *message* is what matters.
_BASH_OUTPUT_LIMIT = 4000
_BASH_OUTPUT_HEAD = 3000


def _fold_text(text: str, *, limit: int = _BASH_OUTPUT_LIMIT, head: int = _BASH_OUTPUT_HEAD) -> tuple[str, bool]:
    """Fold *text* to ~*limit* chars keeping its head and tail (returns folded, truncated)."""
    if len(text) <= limit:
        return text, False
    head = max(0, min(head, limit))
    tail = max(0, limit - head)
    omitted = len(text) - head - tail
    marker = f'\n...[{omitted} characters omitted]...\n'
    folded = text[:head] + marker + (text[len(text) - tail:] if tail else '')
    return folded, True


def _code_block(content: str, language: str = '') -> str:
    """Fence *content* with a backtick run longer than any run it contains.

    Commands and tool output routinely embed markdown fences themselves, so a
    fixed ``` fence would end the block early.
    """
    longest = run = 0
    for char in content:
        if char == '`':
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    fence = '`' * max(3, longest + 1)
    return f'{fence}{language}\n{content}\n{fence}'


def _bash_ir_fix(command: str) -> dict[str, Any] | None:
    """Run the live Bash fixer pipeline over *command* and describe the result.

    Uses ``shell_common.inspect_bash_command`` — the exact pipeline the Bash tool
    applies (``_prepare_bash_cmd`` + ``fix_bash_command``) — so the emitted
    prompt shows both the translated command and the structured change notes.
    Never raises: a broken/unavailable fixer is reported in the payload instead
    of failing the command.
    """
    try:
        from kimi_cli.tools.file.bash import shell_common

        fix = shell_common.inspect_bash_command(command)
    except Exception as exc:
        return {'error': f'{type(exc).__name__}: {exc}'}
    return {
        'platform': sys.platform,
        'changed': fix.command != command,
        'command': fix.command,
        'replacements': list(fix.replacements),
        'path_changes': list(fix.path_changes),
        'shell_wrappers': list(getattr(fix, 'shell_wrappers', ()) or ()),
        'nul_fixes': list(getattr(fix, 'nul_fixes', ()) or ()),
        'unsupported': list(getattr(fix, 'unsupported', ()) or ()),
        'cmd_conditional_fixes': list(getattr(fix, 'cmd_conditional_fixes', ()) or ()),
        'native_flag_fixes': list(getattr(fix, 'native_flag_fixes', ()) or ()),
    }


def _bash_invocation_payload(invocation: Any) -> dict[str, Any]:
    """Build the JSON record of one ``bash`` invocation for the prompt file."""
    payload: dict[str, Any] = {
        'index': invocation.index + 1,
        'status': invocation.status,
        'tool': invocation.tool_name,
        'call_id': invocation.call_id,
        'command': invocation.command,
        'arguments': invocation.arguments,
        'message': invocation.message,
        'output': None,
    }
    if invocation.arguments_error is not None:
        payload['arguments_raw'] = invocation.arguments_raw
        payload['arguments_error'] = invocation.arguments_error
    output = invocation.output or ''
    folded, truncated = _fold_text(output)
    payload['output'] = folded or None
    if truncated:
        payload['output_truncated'] = True
        payload['output_characters'] = len(output)
    if invocation.command:
        payload['ir_fix'] = _bash_ir_fix(invocation.command)
    return payload


def _build_bash_reflection_prompt(
    session: Any,
    invocations: list[Any],
    *,
    prompt_path: Path | None = None,
) -> str:
    """Build the ``/reflection_bash`` markdown prompt.

    The prompt carries (1) the exact source paths of the Bash fixer and its
    consumers, (2) the architecture and the per-command workflow of the IR
    parser, (3) the invariants a correct translation must keep, and (4) every
    observed ``bash`` invocation as one JSON code block — arguments, result
    ``message``, result ``output`` and the fixer's own translation of the
    command — with the failed ones marked.
    """
    agent_src = Path(__file__).resolve().parent.parent          # ...\src\kimix
    repo_root = agent_src.parent.parent                          # ...\kimi-agent
    bash_dir = repo_root / 'kimi-cli' / 'src' / 'kimi_cli' / 'tools' / 'file' / 'bash'
    shell_compat_path = repo_root / 'bin' / 'kimix_native' / '_shell_compat.py'
    shell_compat_pkg = repo_root / 'bin' / 'kimix_native' / 'shell_compat'
    bash_fix_path = bash_dir / 'bash_fix.py'
    shell_common_path = bash_dir / 'shell_common.py'
    bash_tool_path = bash_dir / 'bash_tool.py'
    output_enhance_path = bash_dir / 'output_enhance.py'
    pwsh_fix_path = shell_compat_pkg / 'pwsh_fix.py'
    test_bash = repo_root / 'tests' / 'test_bash.py'
    test_bash_e2e = repo_root / 'tests' / 'test_bash_e2e.py'
    python_path = sys.executable or 'python'
    session_id = getattr(session, 'id', None) or 'unknown'
    now = pendulum.now()

    failed = [item for item in invocations if item.status != 'ok']
    payload = orjson.dumps(
        [_bash_invocation_payload(item) for item in invocations],
        option=orjson.OPT_INDENT_2,
    ).decode('utf-8')

    failed_lines = '\n'.join(
        f'- invocation #{item.index + 1}: `{_fold_text(item.command or "<no command>", limit=160)[0]}`'
        f' — {item.message or item.status}'
        for item in failed
    ) or '- (none: every recorded bash call succeeded)'

    header = f'''# Bash IR Reflection Task

`fix_bash_command` translates native POSIX (`bash`) commands into Windows
Git-Bash compatible commands.  It is **not stable**: some inputs still pass
through in a form that Git Bash cannot run (or run differently).  Review the
translator against the real invocations recorded at the end of this document,
find the root cause of each break, and fix the parser.

## Context
- Prompt file: `{prompt_path if prompt_path is not None else "<not written yet>"}`
- Session: `{session_id}` — generated {now.format('YYYY-MM-DD HH:mm:ss')}
- Working directory: `{Path.cwd()}`
- Host platform: `{sys.platform}` (the fixer only edits on `win32`; elsewhere it returns its input unchanged)
- Recorded bash invocations: {len(invocations)} ({len(failed)} not `ok`)
- Agent source: `{agent_src}`

## Failed / unanswered invocations (quick index)
{failed_lines}

## 1. Source of truth — read before changing anything

Bash fixer (the translation engine, **this is the file to review**):
- `{shell_compat_path}` — the bash fixer module: `fix_bash_command()`,
  `_process_unquoted()`, `bash_compatibility_prelude()`.  It is a *stable
  facade*: it re-exports the complete historical module-level surface (195
  names, explicit imports, no `*`) and holds no rule logic of its own.  It is
  also loaded directly by file path by the test suite, so it must stay importable
  standalone.
- `{shell_compat_pkg}` — the implementation package (one module, one
  responsibility):
  - `models.py` / `spans.py` — lossless IR nodes (`ScriptNode`, `CommandNode`,
    `PipelineNode`, `CommandListNode`, `WordNode`, `RedirectionNode`,
    `HeredocNode`, `ExpansionNode`, `RawNode`) and the coordinate layer
    (`SourceSpan`, `Edit`, `Diagnostic`).  Nodes reference the single source
    string *by reference*; nothing is copied or re-normalized, so rendering
    reproduces the input byte-for-byte.
  - `lexer.py` — one O(n) tokenizer plus the canonical quote / escape /
    substitution boundary oracles (`find_dq_end`, `find_ansi_c_end`,
    `find_backtick_end`, `find_matching_paren`).
  - `parser.py` — tolerant command-context IR builder.  It is deliberately NOT a
    POSIX grammar: command word vs arguments vs assignments, redirections,
    heredocs, comments, nested executable substitutions and wrapper hints are
    recognized only where recognition is safe; anything ambiguous stays
    `RawNode` and the parser NEVER raises on malformed input.  Terms tile their
    span (children + gaps == parent text).
  - `translator.py` — the single orchestration entry `translate()`: scan flow,
    the `EditPlan` / `render_splices` render contract (splices in original-source
    coordinates, overlaps and descending spans rejected, original string object
    returned when nothing changed) and the fallback prefix.
  - `ir_command.py` — the genuine per-command IR decision engine (dispatcher
    slots 4-8, `translate_ir()`), the independent proof that the decisions are
    IR-owned rather than flow-owned.
  - `bash_core.py` — `BashFix`, the `_BashFixScanner` compatibility reader that
    drives the scan flow, and `fix_bash_command`.
  - `paths.py` — Windows path, null-device (`nul`/`NUL`) and `cd /d` rules.
  - `wrappers.py` — compatibility classification, fallback registration
    (`command -v`-guarded definitions), wrapper-operand/unwrap rules, runtime
    reparse (`bash -c`, `sh -c`, `eval`).
  - `cmd_translator.py` — cmd.exe `@` / `if` / `for` translation and the
    unsupported `goto`/`call` refusals.
  - `heredoc.py` — heredoc trailing-operator repair and the heredoc event model.
  - `fallback_data.py` — guarded fallback bodies and unsupported-command reasons.
  - `unquoted.py` — `_process_unquoted` (unquoted `\\` -> `/`).
  - `native_flags.py` — doubled-slash flag repair (`msiexec //a` -> `/a`).
  - `{pwsh_fix_path}` (and `pwsh_transform.py`) — the PowerShell siblings; out of scope here.

Consumers (how the fixer is reached from a tool call):
- `{bash_fix_path}` — tool-side `fix_bash_command()`: the `win32` gate plus the
  heredoc-operator repair.
- `{shell_common_path}` — `inspect_bash_command()` =
  `_prepare_bash_cmd()` (`_process_unquoted`) + `fix_bash_command()`.  This is the
  ONLY pipeline the tool applies; the `ir_fix` field of every invocation below
  is exactly this function's output.
- `{bash_tool_path}` — the `bash` tool: `_prepare_command()` turns
  `BashFix.unsupported` into a `ToolError` (so a refusal is visible instead of a
  guaranteed "command not found"), then executes the prepared command.
- `{output_enhance_path}` — exit-code interpretation / failure annotation.  It
  explains a result and never fixes a command — do not "fix" parse bugs there.

Tests: `{test_bash}` (unit: fixer rules, native vs reference parity) and
`{test_bash_e2e}` (src -> dest corpus; `pytest {test_bash_e2e} --case=<file>`,
`--record` refreshes expectations).  Every parser fix needs a regression case.

## 2. Architecture in one paragraph
The pipeline is a **lossless tolerant IR**: `lexer` turns the source into tokens
with exact quote/expansion boundaries; `parser` builds IR nodes that reference
source spans (never copies), so `render_source()` is byte-identical; the rule
modules (`paths`, `wrappers`, `cmd_translator`, `heredoc`, `fallback_data`,
`native_flags`) make per-rule decisions over those nodes and emit `Edit` splices
in original-source coordinates; `translator.EditPlan` collects the splices
(rejecting overlaps), renders them once, and prepends the fallback prefix only
when a fallback was actually registered.  `bash_core._BashFixScanner` is the
compatibility *reader* that owns the scan FLOW (the linear word/redirection walk,
nested-region recursion, report ordering) while every rule DECISION delegates to
the IR modules, so there is exactly one implementation per rule.  The
conservative pass-through policy governs everything: an already Git-Bash
compatible command must come back **byte-for-byte** (the same string object), and
only verified compatibility cases may produce an edit or a guarded fallback.

## 3. Workflow of one `bash` tool call
1. The model calls the `bash` tool with `{{"cmd": "...", "mode": ..., "timeout": ...}}`
   (`BashParams`; `command` is accepted as an alias of `cmd`).
2. `bash_tool._prepare_command()` -> `shell_common.inspect_bash_command(cmd)`
   -> `bash_tool._prepare_bash_cmd(cmd)` = `_process_unquoted`: every unquoted
   `\\` that is not an escape (i.e. not before a bash metacharacter and not inside
   `'...'`, `"..."`, `$'...'`, `$(...)`, backticks) becomes `/`.
3. `bash_fix.fix_bash_command()` applies the `sys.platform == "win32"` gate and
   calls `_shell_compat.fix_bash_command()`.
4. The facade delegates to the native kernel when the compiled runtime is active,
   otherwise to the pure-Python reference in `shell_compat`; the two must stay
   byte-equal.  The facade then applies the doubled-slash flag post-pass.
5. `shell_compat.translator.translate()` drives the reader, collects the edits,
   renders them, adds the fallback prefix, and finally applies the heredoc
   trailing-operator repair.
6. `BashFix(command, replacements, path_changes, shell_wrappers, nul_fixes,
   unsupported, cmd_conditional_fixes, native_flag_fixes)` is returned;
   `unsupported` non-empty means the command has no faithful Git-Bash equivalent
   and the tool refuses it with the recorded reason.
7. The tool executes the prepared command via `bash -c` and wraps stdout/stderr
   and the exit code into the tool result (`ToolOk` / `ToolError`).  A failed
   result reaches the history as `role="tool"` with a `<system>ERROR: ...</system>`
   metadata part.

## 4. What to review (invariants a correct translation must keep)
- **Byte-for-byte pass-through**: an already compatible command returns the
  original string object — no re-rendering, no reordering, no re-quoting, no
  unrelated normalization.  A diff on such input is a bug.
- **Edit safety**: every change is a `(start, end, replacement)` splice in
  original-source coordinates; `EditPlan.add` must reject overlapping or
  descending spans, and a splice must never cut through a quote, heredoc body,
  `${{...}}`, `$((...))`, `$(...)`, backtick region, comment or escape sequence.
- **Never rewrite data**: inside single quotes, heredoc bodies (unless the
  construct itself is being repaired) and comments the source is data.
- **Backslashes are ambiguous**: `D:\\repo\\src` is a path (spaces/drive), while
  `my\\ file.txt` and `foo\\bar` are escapes.  Aggressive unquoted-slash
  conversion and quote-region detection are the historical weak spot — check the
  failing commands against both readings.
- **Tolerance**: malformed, adversarial or deeply nested input must pass through
  unchanged (`RawNode`, depth limit, `RecursionError` caught) — never raise, never
  emit half-translated output.
- **Guarded fallbacks**: fallback definitions are emitted only when the command
  word is a registered missing tool, are wrapped in a `command -v` guard, appear
  once, in discovery order, and never shadow a real executable.
- **Refusals stay refusals**: `unsupported` keeps the source untouched and
  carries the reason plus a native alternative; it must not silently mangle.
- **Ordering**: `replacements` / `path_changes` / `shell_wrappers` / `nul_fixes`
  / `unsupported` are reported in source discovery order; the reports feed the
  tool messages and the test corpus.
- **Generalize**: no case-by-case string patches keyed on a specific command;
  fix the rule that decided wrongly, and cover it with a corpus case.

## 5. Observed bash invocations (this session)
Every `bash` tool call of the current session, in conversation order, as one JSON
array: `arguments` is the parsed call payload, `message` is the tool-result
metadata (`ERROR: ...` prefix stripped; `null` when the call was never answered),
`output` is the tool-result payload (head/tail folded when very long;
`output_truncated` / `output_characters` tell you when), `status` is `ok`,
`failed` or `no_result`, and `ir_fix` is what the current fixer produced for
`command` — compare it with the invocation's `output` to see whether the
translation was wrong, merely incomplete, or irrelevant to the failure.

{_code_block(payload, 'json')}

## 6. How to reproduce and verify
- Reproduce one translation exactly as the tool does:
  `{python_path} -c "from kimi_cli.tools.file.bash.shell_common import inspect_bash_command as i; print(i(r'<cmd>'))"`
- Check the minimum fix point: prefer a rule change in
  `{shell_compat_pkg}` (never in the tool-only wrappers) and keep the facade's
  exported surface intact.
- Add the failing command as a regression case in `{test_bash_e2e}`
  (`--case=<file>` / `--record`) and/or a rule unit test in `{test_bash}`.
- Run `uv run tools/syntax_check.py <file>` on every edited Python file, then
  `uv run pytest {test_bash} {test_bash_e2e} -x -q`.

## 7. Deliverables
1. The root cause of every `failed` invocation above (why the emitted Git-Bash
   command cannot run, or runs differently from the POSIX original).
2. The generic fix in the parser, with the invariant it restores.
3. Regression tests (corpus case and/or unit test) that fail before the fix and
   pass after it.
4. A short report of the changes: what was misunderstood, what was changed, and
   why — written to `{repo_root / 'docs' / f'reflection_bash_report_{now.format("YYYYMMDD_HHMMSS")}.md'}`.
'''
    return header


def _cmd_reflection_bash(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    """Write a Bash IR review prompt for the current session to a markdown file.

    Usage: ``/reflection_bash:<prompt_file_path>``.  The prompt lists every
    ``bash`` tool invocation of the current session (arguments, result message,
    result output, failed ones marked) and asks the agent to review the IR bash
    parser so native POSIX commands survive the Windows Git-Bash translation.
    """
    raw_path = ':'.join(task_split[1:]).strip() if len(task_split) > 1 else ''
    if not raw_path:
        print_error('Command must be /reflection_bash:<prompt_file_path>')
        return None, False
    session = get_default_session()
    if session is None:
        print_error('No active session. Start a conversation first.')
        return None, False
    target = Path(raw_path)
    if not target.is_absolute():
        target = constants.curr_dir / target
    try:
        invocations = collect_tool_invocations(session, tool_names=_BASH_TOOL_NAMES)
    except Exception as exc:  # noqa: BLE001 - a broken context must not kill the CLI
        print_error(f'Failed to read the session context: {exc}')
        return None, False
    try:
        content = _build_bash_reflection_prompt(session, invocations, prompt_path=target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')
    except OSError as exc:
        print_error(f'Failed to write {target}: {exc}')
        return None, False
    failed = sum(1 for item in invocations if item.status != 'ok')
    print_success(
        f'Wrote {len(invocations)} bash invocation(s) ({failed} not ok) to {target}'
    )
    if not invocations:
        print_warning('No bash tool call was recorded in this session; the prompt has no evidence section.')
    print_info(f'Run it with: /file:{target}')
    return None, False


# ---------------------------------------------------------------------------
# /reflection_<tool>:<prompt_file_path> — builtin tool review prompts
# ---------------------------------------------------------------------------
# ``/reflection_bash`` is the reference implementation: it reads the *current
# session context*, extracts every invocation of one tool, and writes one
# markdown prompt that asks the agent to review that tool's implementation
# against the recorded evidence.
#
# The machinery below generalizes it to the other tools listed in
# ``src/kimix/agent_worker.json`` (minus the ones without a meaningful parser:
# workflow, compact, skill).  Every generated document keeps the same
# seven-section skeleton as ``/reflection_bash``; only sections 1-4 (source of
# truth, architecture, workflow, invariants) and the reproduce/verify recipe are
# tool-specific.

#: Tool names that get a ``/reflection_<tool>:<path>`` prompt generator.
_REFLECTION_TOOL_NAMES: tuple[str, ...] = (
    'pwsh',
    'python',
    'job_output',
    'todo_list',
    'read',
    'read_image',
    'write',
    'edit',
    'subagent',
    'list_agents',
    'glob',
    'grep',
    'fetch_url',
    'web_search',
)

#: Argument keys that name "the thing the tool acted on", in priority order.
#: ``bash``/``pwsh``/``python`` use ``cmd``/``command``/``code``.
_REFLECTION_TARGET_KEYS: tuple[str, ...] = (
    'file_path',
    'path',
    'pattern',
    'url',
    'query',
    'job_id',
    'session_id',
    'prompt',
    'code',
    'cmd',
    'command',
)


@dataclass(frozen=True, slots=True)
class _ReflectionContext:
    """Paths and session facts every tool prompt quotes."""

    session_id: str
    prompt_path: Path | None
    now: pendulum.DateTime
    repo_root: Path
    agent_src: Path
    kimi_cli_src: Path
    tools_src: Path
    tests_root: Path
    kimi_cli_tests: Path
    python_path: str

    def tool(self, *parts: str) -> Path:
        """A path inside ``kimi-cli/src/kimi_cli/tools``."""
        return self.tools_src.joinpath(*parts)

    def test(self, *parts: str) -> Path:
        """A path inside the repository ``tests`` tree."""
        return self.tests_root.joinpath(*parts)

    def cli_test(self, *parts: str) -> Path:
        """A path inside the ``kimi-cli/tests`` tree."""
        return self.kimi_cli_tests.joinpath(*parts)

    def report(self, slug: str) -> Path:
        """Where the agent must write its change report."""
        return self.repo_root / 'docs' / f'{slug}_report_{self.now.format("YYYYMMDD_HHmmss")}.md'


def _reflection_context(prompt_path: Path | None, session: Any = None) -> _ReflectionContext:
    """Build the context shared by every generated prompt."""
    agent_src = Path(__file__).resolve().parent.parent          # ...\src\kimix
    repo_root = agent_src.parent.parent                          # ...\kimi-agent
    kimi_cli_src = repo_root / 'kimi-cli' / 'src' / 'kimi_cli'
    return _ReflectionContext(
        session_id=getattr(session, 'id', None) or 'unknown',
        prompt_path=prompt_path,
        now=pendulum.now(),
        repo_root=repo_root,
        agent_src=agent_src,
        kimi_cli_src=kimi_cli_src,
        tools_src=kimi_cli_src / 'tools',
        tests_root=repo_root / 'tests',
        kimi_cli_tests=repo_root / 'kimi-cli' / 'tests',
        python_path=sys.executable or 'python',
    )


@dataclass(frozen=True, slots=True)
class _PromptBody:
    """The tool-specific half of one reflection prompt."""

    sections: str
    """Sections 1-4: source of truth, architecture, workflow, invariants."""

    verify: str
    """Section 6: how to reproduce and verify the fix."""

    context: tuple[str, ...] = ()
    """Extra ``## Context`` bullets, one line each (backticks allowed)."""


@dataclass(frozen=True, slots=True)
class _ReflectionToolSpec:
    """Everything needed to emit one ``/reflection_<tool>`` document."""

    tool: str
    title: str
    entry: str
    summary: str
    body: Callable[[_ReflectionContext], _PromptBody]
    slug: str
    extras: Callable[[dict[str, Any], str], dict[str, Any]] | None = None

    @property
    def command(self) -> str:
        """Slash-command name, e.g. ``reflection_read_image``."""
        return f'reflection_{self.tool}'


def _first_argument(arguments: dict[str, Any], *keys: str) -> Any:
    """First present value among *keys* (tool aliases: ``path``/``file_path``...)."""
    for key in keys:
        value = arguments.get(key)
        if value is not None:
            return value
    return None


def _item_count(value: Any) -> int | None:
    """Number of items a scalar-or-list argument carries (``None`` = absent)."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return len(value)
    return 1


def _length_of(value: Any) -> int | None:
    """Character/item length of a scalar-or-list argument (``None`` = absent)."""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (list, tuple)):
        return len(value)
    return None


#: Per-tool derived evidence added to each JSON record.  Keep these cheap and
#: total: :func:`_reflection_invocation_payload` isolates failures.
_REFLECTION_EXTRAS: dict[str, Callable[[dict[str, Any], str], dict[str, Any]]] = {
    'pwsh': lambda a, o: {'mode': a.get('mode') or 'execute'},
    'python': lambda a, o: {'mode': a.get('mode') or 'execute'},
    'job_output': lambda a, o: {'action': a.get('action') or 'get'},
    'todo_list': lambda a, o: {
        'mode': a.get('mode') or 'merge',
        'todo_items': _item_count(
            _first_argument(a, 'todos', 'updates', 'edits', 'items', 'changes', 'tasks')
        ),
    },
    'read': lambda a, o: {
        'files_requested': _item_count(_first_argument(a, 'file_path', 'path')),
    },
    'read_image': lambda a, o: {'has_region': bool(a.get('region') or a.get('region_pct'))},
    'write': lambda a, o: {
        'mode': a.get('mode') or 'overwrite',
        'content_characters': _length_of(_first_argument(a, 'content', 'text')),
    },
    'edit': lambda a, o: {
        'mode': a.get('mode') or 'auto',
        'edit_items': _item_count(_first_argument(a, 'edit', 'edits', 'old_string')),
    },
    'subagent': lambda a, o: {
        'prompts': _item_count(_first_argument(a, 'prompt', 'task')),
        'return_history': bool(a.get('return_history')),
    },
    'glob': lambda a, o: {'result_lines': o.count('\n') + 1 if o else 0},
    'grep': lambda a, o: {'returned_lines': o.count('\n') + 1 if o else 0},
    'fetch_url': lambda a, o: {'markdown_characters': len(o)},
}


def _reflection_target(invocation: Any) -> str | None:
    """The argument the tool acted on (path/pattern/URL/cmd), for the index."""
    arguments = invocation.arguments if isinstance(invocation.arguments, dict) else {}
    for key in _REFLECTION_TARGET_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
            return ', '.join(value)
    return invocation.command


def _reflection_invocation_payload(
    invocation: Any,
    extras: Callable[[dict[str, Any], str], dict[str, Any]] | None,
) -> dict[str, Any]:
    """Build the JSON record of one tool invocation for the prompt file."""
    payload: dict[str, Any] = {
        'index': invocation.index + 1,
        'status': invocation.status,
        'tool': invocation.tool_name,
        'call_id': invocation.call_id,
        'target': _reflection_target(invocation),
        'arguments': invocation.arguments,
        'message': invocation.message,
        'output': None,
    }
    if invocation.arguments_error is not None:
        payload['arguments_raw'] = invocation.arguments_raw
        payload['arguments_error'] = invocation.arguments_error
    output = invocation.output or ''
    folded, truncated = _fold_text(output)
    payload['output'] = folded or None
    if truncated:
        payload['output_truncated'] = True
        payload['output_characters'] = len(output)
    if extras is not None:
        try:
            payload.update(extras(invocation.arguments or {}, output))
        except Exception:  # noqa: BLE001 - derived evidence must never break the command
            payload['extras_error'] = True
    return payload


def _build_tool_reflection_prompt(
    spec: _ReflectionToolSpec,
    ctx: _ReflectionContext,
    invocations: list[Any],
) -> str:
    """Build one tool-specific review prompt with the reflection_bash skeleton."""
    body = spec.body(ctx)
    failed = [item for item in invocations if item.status != 'ok']
    payload = orjson.dumps(
        [_reflection_invocation_payload(item, spec.extras) for item in invocations],
        option=orjson.OPT_INDENT_2,
    ).decode('utf-8')

    failed_lines = '\n'.join(
        f'- invocation #{item.index + 1}: `{_fold_text(_reflection_target(item) or "<no target>", limit=160)[0]}`'
        f' — {item.message or item.status}'
        for item in failed
    ) or f'- (none: every recorded `{spec.tool}` call succeeded)'

    context_extra = ''.join(f'- {line}\n' for line in body.context)
    prompt_display = ctx.prompt_path if ctx.prompt_path is not None else '<not written yet>'
    return (
        f'# {spec.title}\n\n'
        f'{spec.summary.strip()}\n\n'
        '## Context\n'
        f'- Prompt file: `{prompt_display}`\n'
        f'- Session: `{ctx.session_id}` — generated {ctx.now.format("YYYY-MM-DD HH:mm:ss")}\n'
        f'- Working directory: `{Path.cwd()}`\n'
        f'- Host platform: `{sys.platform}`\n'
        f'- Recorded `{spec.tool}` invocations: {len(invocations)} ({len(failed)} not `ok`)\n'
        f'- Tool entry point: `{spec.entry}`\n'
        f'- Agent source: `{ctx.agent_src}`\n'
        f'{context_extra}'
        '\n## Failed / unanswered invocations (quick index)\n'
        f'{failed_lines}\n\n'
        f'{body.sections.strip()}\n\n'
        f'## 5. Observed `{spec.tool}` invocations (this session)\n'
        f'Every `{spec.tool}` tool call of the current session, in conversation order, as one\n'
        'JSON array: `arguments` is the parsed call payload (`arguments_raw` /\n'
        '`arguments_error` when the model produced unparsable JSON), `target` is the\n'
        'argument the call acted on, `message` is the tool-result metadata (`ERROR: ...`\n'
        'prefix stripped; `null` when the call was never answered), `output` is the\n'
        'tool-result payload (head/tail folded when very long; `output_truncated` /\n'
        '`output_characters` tell you when), and `status` is `ok`, `failed` or\n'
        '`no_result`.  Read this evidence before changing code: a rule that only fixes\n'
        'the listed calls without covering the general shape is not a fix.\n\n'
        f'{_code_block(payload, "json")}\n\n'
        '## 6. How to reproduce and verify\n'
        f'{body.verify.strip()}\n\n'
        '## 7. Deliverables\n'
        '1. The root cause of every `failed` invocation above (not just "it did not\n'
        '   work": name the wrong decision and the input shape that triggered it).\n'
        '2. The generic fix, with the invariant it restores (no case-by-case patch\n'
        '   keyed on one recorded example).\n'
        '3. Regression tests that fail before the fix and pass after it.\n'
        '4. A short report of the changes: what was misunderstood, what was changed,\n'
        f'   and why — written to `{ctx.report(spec.slug)}`.\n'
    )


def _run_tool_reflection(
    spec: _ReflectionToolSpec,
    task_split: list[str],
    text_arr: list[str],
) -> tuple[None, bool]:
    """Shared body of every ``/reflection_<tool>:<prompt_file_path>`` command."""
    raw_path = ':'.join(task_split[1:]).strip() if len(task_split) > 1 else ''
    if not raw_path:
        print_error(f'Command must be /{spec.command}:<prompt_file_path>')
        return None, False
    session = get_default_session()
    if session is None:
        print_error('No active session. Start a conversation first.')
        return None, False
    target = Path(raw_path)
    if not target.is_absolute():
        target = constants.curr_dir / target
    try:
        invocations = collect_tool_invocations(session, tool_names=(spec.tool,))
    except Exception as exc:  # noqa: BLE001 - a broken context must not kill the CLI
        print_error(f'Failed to read the session context: {exc}')
        return None, False
    ctx = _reflection_context(target, session)
    try:
        content = _build_tool_reflection_prompt(spec, ctx, invocations)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')
    except OSError as exc:
        print_error(f'Failed to write {target}: {exc}')
        return None, False
    failed = sum(1 for item in invocations if item.status != 'ok')
    print_success(
        f'Wrote {len(invocations)} {spec.tool} invocation(s) ({failed} not ok) to {target}'
    )
    if not invocations:
        print_warning(
            f'No {spec.tool} tool call was recorded in this session; '
            'the prompt has no evidence section.'
        )
    print_info(f'Run it with: /file:{target}')
    return None, False


def _make_reflection_command(spec: _ReflectionToolSpec) -> Any:
    """Create the dispatcher entry for one tool's reflection command."""

    def _cmd(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
        return _run_tool_reflection(spec, task_split, text_arr)

    _cmd.__name__ = f'_cmd_{spec.command}'
    _cmd.__doc__ = (
        f'Write a `{spec.tool}` review prompt for the current session: every '
        f'`{spec.tool}` invocation plus the invariants the implementation must keep.'
    )
    return _cmd


# ---------------------------------------------------------------------------
# Per-tool prompt bodies
# ---------------------------------------------------------------------------


def _prompt_pwsh(ctx: _ReflectionContext) -> _PromptBody:
    tool_path = ctx.tool('file', 'bash', 'pwsh_tool.py')
    fix_path = ctx.tool('file', 'bash', 'pwsh_fix.py')
    transform_path = ctx.tool('file', 'bash', 'process_pwsh.py')
    shell_common = ctx.tool('file', 'bash', 'shell_common.py')
    safety = ctx.tool('file', 'bash', 'safety.py')
    enhance = ctx.tool('file', 'bash', 'output_enhance.py')
    desc = ctx.tool('file', 'bash', 'pwsh_tool.md')
    common = ctx.tool('common.py')
    stream = ctx.tool('background', 'utils.py')
    prompt_common = ctx.tool('prompt_common.py')
    compat = ctx.repo_root / 'bin' / 'kimix_native' / '_shell_compat.py'
    compat_pkg = ctx.repo_root / 'bin' / 'kimix_native' / 'shell_compat'
    tests = ', '.join(
        str(path)
        for path in (
            ctx.test('test_powershell.py'),
            ctx.test('test_pwsh_fix.py'),
            ctx.test('test_process_pwsh.py'),
            ctx.test('test_shell_common.py'),
            ctx.cli_test('tools', 'test_pwsh_tool.py'),
        )
    )
    sections = f'''## 1. Source of truth — read before changing anything

PowerShell tool (**the policy layer to review**):
- `{tool_path}` — `Powershell(CallableTool2[PowershellParams])`, `name = "pwsh"`,
  description loaded from `{desc}`.  It owns platform discovery (`find_pwsh`,
  lru-cached), the safety gates, argument preparation, process wiring and result
  shaping.
- `{fix_path}` — `fix_pwsh_command()`: a stable façade re-exporting the single
  canonical scanner (no rule logic of its own).
- `{transform_path}` — `pwsh_transform()`: the PowerShell 7 -> Windows PowerShell
  5.1 downgrader, same façade pattern.

Shared shell layer and helpers:
- `{shell_common}` — `wrap_pwsh_command()` (console UTF-8 init + try/catch/exit
  wrapper + `;exit $LASTEXITCODE`), `PWSH_ONESHOT_FLAGS`,
  `find_powershell_fallback()`.
- `{safety}` — `check_hardline_blocked()`, `self_kill_hint()`,
  `foreground_background_guidance()`.
- `{enhance}` — `interpret_exit_code()`, `is_expected_exit()`,
  `annotate_failure()`; secret redaction is `redact_sensitive_output()` in
  `{ctx.tool('security.py')}`.
- `{common}` — `ProcessTask`, `_maybe_rewrite_shell_command_with_rtk`,
  `_token_filter_output`, `_summarize_long_output_async`,
  `_build_session_output_block`, `_subprocess_elapsed`.
- `{stream}` — `BackgroundStream` (one-shot / background / interactive output).
- `{prompt_common}` — `mode_field`, `task_id_field`, `wait_for_pattern_field`,
  `max_lines_field`, `normalize_mode_validator`, `shell_cmd_required_validator`,
  `MAX_TIMEOUT_SECONDS`.
- `{compat}` and `{compat_pkg}` — the compiled/pure-Python kernel the two façades
  delegate to (`pwsh_fix.py`, `pwsh_transform.py`); both must stay byte-equal to
  the reference implementation.

Tests: `{tests}`.  `test_pwsh_fix.py` / `test_process_pwsh.py` pin the
translation rules, `test_powershell.py` pins argument building, session
continuation, the RTK rewrite and the safety wiring.

## 2. Architecture in one paragraph
Three rings.  (1) A *façade* ring (`pwsh_fix.py`, `process_pwsh.py`) re-exports
the canonical scanner so the parsing logic exists once and stays importable
standalone.  (2) A *shared one-shot shell* ring (`shell_common.py`) builds the
argv/environment, deliberately import-cycle-safe.  (3) The *tool* ring
(`pwsh_tool.py`) owns policy: hardline/self-kill/forbidden-keyword refusal,
platform discovery, PS7-vs-5.1 downgrade, timeout semantics and result shaping.
Process execution is delegated wholesale to `ProcessTask` + `BackgroundStream`,
so the tool never touches a subprocess directly; every exit path funnels through
`_process_output` / `_format_session_result` / `_format_background_output` so
foreground, background and interactive output are identical.

## 3. Workflow of one `pwsh` tool call
1. `PowershellParams` normalizes the call (`cmd` -> `command` alias,
   `timeoutMs` -> seconds, `run_in_background` -> `mode="send"`); an empty
   command is rejected by `shell_cmd_required_validator`.
2. `Powershell.__call__` runs the hardline and self-kill safety checks.
3. Routing: `task_id` -> `_continue_session`; `mode == "send"` ->
   `_execute_background`; otherwise the one-shot path.
4. `_maybe_rewrite_shell_command_with_rtk(..., pwsh=True)` optionally prefixes
   the RTK wrapper (dedupe/truncate pipeline).
5. Interpreter selection: PowerShell 7 present -> verbatim; otherwise the 5.1
   fallback plus `pwsh_transform()` and a downgrade warning.
6. `_validate_command_for_ps` (naive quote parity; warnings only) then
   `fix_pwsh_command()`; `None` -> the single "invalid command" refusal.
7. `_compile_pattern` for `wait_for_pattern`; forbidden-keyword scan of the
   prepared text.
8. Windows only: `refresh_env_from_registry()`.
9. Preparation: `Set-Location -LiteralPath` for `workdir`,
   `shell_common.wrap_pwsh_command()`, `_maybe_encode_command()` (>8000 chars ->
   `-EncodedCommand` base64 UTF-16LE), `ProcessTask(...).start()`.
10. Wait: `stream.wait_for_output` (pattern) or `ProcessTask.wait_with_monitor`;
    `CancelledError` stops the tree and unregisters the task id; a still-running
    process yields guidance instead of output.
11. Shaping: `remove_task_id` -> `pop_output` -> `interpret_exit_code` /
    `annotate_failure` / `is_expected_exit` -> `_process_output` (redact, RTK
    export, token filter, 65536-char summarize gate, disk export) ->
    `_build_session_output_block` -> `ToolOk` / `ToolError`.

## 4. What to review (invariants a correct `pwsh` call must keep)
- **Safety runs twice**: hardline, self-kill and forbidden-keyword checks must
  see both the raw and the rewritten/prepared command.  Moving a check before
  preprocessing (or after only) re-opens a bypass.
- **One refusal gate**: `fix_pwsh_command() is None` is the only "invalid
  command" error; the naive quote-parity check is advisory.
- **Wrapper contract**: `wrap_pwsh_command()` output must keep the UTF-8 console
  init, the try/catch `$_ | Out-String | Write-Error; exit 1` block and the
  trailing `;exit $LASTEXITCODE`; native exit codes and Ctrl+C-as-input depend
  on all three.
- **Discovery**: `find_pwsh()` is `lru_cache(maxsize=1)`; a missing `pwsh` must
  raise `SkipThisTool` only when the 5.1 fallback is also absent, and the
  downgrade must warn.
- **Encoding**: the 8000-character `-EncodedCommand` threshold and UTF-16LE
  encoding are both load-bearing; keep the flag consistent at every call site.
- **Three-way timeout semantics**: long-running guidance keeps the task alive,
  "no output yet" keeps it alive, "output then stall" kills the process tree.
  Collapsing them into one timeout re-introduces zombie jobs.
- **Truncation order**: secret redaction -> RTK original export -> token filter
  -> 65536-char summarize (with disk spill).  A reordering can leak a secret or
  lose the original.
- **Platform gates**: `sys.platform == "win32"` guards must stay where they are;
  POSIX maps the `Powershell` alias to `bash` in `soul/toolset.py`.
- **No case-by-case fixes**: a bad translation belongs in the translation rule
  (under `{compat_pkg}`), not in a string patch keyed on one command.'''
    verify = f'''- Reproduce one translation exactly as the tool does:
  `{ctx.python_path} -c "from kimi_cli.tools.file.bash.pwsh_fix import fix_pwsh_command as f; print(f(r'<command>'))"`
  and, for the 5.1 downgrade,
  `{ctx.python_path} -c "from kimi_cli.tools.file.bash.process_pwsh import pwsh_transform as t; print(t(r'<command>'))"`.
- Prefer the minimal fix point: a rule module under `{compat_pkg}` (never the
  tool-only wrappers), keeping both façades' exported surface intact.
- Add a regression case to `{ctx.test('test_pwsh_fix.py')}` or
  `{ctx.test('test_process_pwsh.py')}`, plus a tool-level case in
  `{ctx.test('test_powershell.py')}` when wiring changed.
- Run `uv run tools/syntax_check.py <file> [<file> ...]` on every edited Python
  file, then
  `uv run pytest {ctx.test('test_pwsh_fix.py')} {ctx.test('test_powershell.py')} -x -q`.'''
    return _PromptBody(
        sections=sections,
        verify=verify,
        context=(
            'PowerShell translation is Windows-only; elsewhere the fixers return '
            'their input unchanged.',
        ),
    )


def _prompt_python(ctx: _ReflectionContext) -> _PromptBody:
    tool_path = ctx.tool('py', '__init__.py')
    common = ctx.tool('common.py')
    security = ctx.tool('security.py')
    prompt_common = ctx.tool('prompt_common.py')
    stream = ctx.tool('background', 'utils.py')
    tests = str(ctx.test('unit', 'tools', 'test_python.py'))
    sections = f'''## 1. Source of truth — read before changing anything

Print/Python tool (**the file to review**):
- `{tool_path}` — `python(CallableTool2[Params])`, `name = "python"`, the whole
  tool lives in this single file (note: the package shadows the stdlib ``py``
  name, always import it as `kimi_cli.tools.py`).

Shared execution layer (do not "fix" tool behaviour here without checking the
other shell tools):
- `{common}` — `ProcessTask`, `_create_script_file`, `_display_temp_path`,
  `_token_filter_output`, `_summarize_long_output_async`,
  `_build_session_output_block`, `_subprocess_elapsed`.
- `{stream}` — `BackgroundStream` (`pop_output`, `wait`, `wait_with_inactivity_timeout`,
  `wait_for_output`, `stop`, `remove_task_id`).
- `{security}` — `scrub_child_env`, `redact_sensitive_output`.
- `{prompt_common}` — `mode_field`, `task_id_field`, `wait_for_pattern_field`,
  `max_lines_field`, `normalize_mode_validator`, `MAX_TIMEOUT_SECONDS`.

Tests: `{tests}` — interpreter resolution, env construction, module-not-found
hints, ProcessTask wiring, the syntax pre-check, the summarize gate, env
scrubbing, the script temp folder and the background formatter.

## 2. Architecture in one paragraph
A thin single-file tool.  Interpreter resolution (`_resolve_python` /
`_venv_python_candidates`) and environment construction (`_build_env`) are small
private helpers; execution is exactly one `ProcessTask`; and all presentation is
delegated to `tools/common.py`, so the tool's only unique logic is *which*
interpreter runs and *how* a failure is explained.  Configuration is read
defensively through `_python_config`, which keeps `check_syntax`, `scrub_env`,
`env_passthrough`, `redact_secrets` and `summarize_long_output` working with a
documented default when the config is absent or malformed.  Interactive/session
mode reuses the same `BackgroundStream` machinery as one-shot mode, differing
only in argv (`-i`) and newline handling.  Generated scripts are materialized in
the share temp folder, never in the session directory.

## 3. Workflow of one `python` tool call
1. `Params` normalizes the call (`code` / `source_code` / `file` alias,
   `run_in_background` -> `mode="send"`); `code` is required unless
   `mode="interactive"` or a `task_id` resumes an existing run.
2. `task_id` -> `_continue_session` (outside the concurrency semaphore).
3. Otherwise `async with self._semaphore` (limit 8) -> `_start_interactive` or
   `_execute_code(background=...)`.
4. `_resolve_script_source`: an existing `.py` path is executed as-is, anything
   else goes through `_create_script_file` (share temp folder).
5. `_syntax_check_error`: `compile()` pre-check gated by
   `config.python.check_syntax` (default on); reports line and column.
6. `_compile_pattern` for `wait_for_pattern` (invalid regex -> `ToolError` before
   spawning).
7. `_resolve_python`: `KIMIX_PYTHON_EXECUTABLE` -> `.venv` walk-up ->
   `VIRTUAL_ENV` -> `sys.executable`; `_build_env` scrubs the child env and
   prepends the share `bin` to `PATH`.
8. `ProcessTask(...).start(session, "python")`.
9. Wait: `stream.wait_for_output` or `ProcessTask.wait_with_monitor`;
   `CancelledError` stops the task and unregisters the id.
10. Shaping: `pop_output` -> optional `output_path` export ->
    `_process_output` -> `_build_session_output_block` ->
    `_module_not_found_hint` -> `ToolError` / `ToolOk`.  Background reads reuse
    `_format_background_output`.

## 4. What to review (invariants a correct `python` run must keep)
- **Interpreter ordering**: `_venv_python_candidates` must stay host-ordered
  (`Scripts/python.exe` first only on Windows); a foreign-layout executable on
  POSIX fails cryptically through interop.
- **Zero-copy env fast path**: `_build_env` may return `None` only when the
  interpreter is not a venv and the share `bin` is already first in `PATH`;
  returning a partial dict re-introduces scrubbed secrets.
- **Both env gates at both call sites**: `scrub_env` and `env_passthrough` must
  be applied identically in one-shot and interactive paths.
- **Syntax pre-check scope**: never for a pure REPL, never for
  `_continue_session`; it is a pre-spawn gate and must report line/column.
- **`output_path` legacy shape**: that branch bypasses the structured block on
  purpose; keep it byte-compatible.
- **Semaphore**: created in `__init__` and never awaited there or bound to a
  loop; the limit of 8 concurrent runs is part of the contract.
- **No broad excepts**: the only defensive catch is the config read; a new
  `except Exception` inside `__call__` hides real tool bugs.
- **Failure explanation**: a `ModuleNotFoundError` must keep producing the
  actionable hint, and every `ToolError` must carry a non-empty message.'''
    verify = f'''- Reproduce resolution/env logic directly:
  `{ctx.python_path} -c "from kimi_cli.tools.py import Params, _venv_python_candidates; print(Params(code='print(1)').mode)"`.
- Exercise one real run through the tool (mock-free) with a tiny script, then
  inspect the emitted `ToolOk`/`ToolError` message and `output_path` behaviour.
- Add a regression case to `{tests}`; keep the class-level layout
  (`TestResolvepython`, `TestBuildEnv`, `TestSyntaxPrecheck`, `TestEnvScrubbing`).
- Run `uv run tools/syntax_check.py <file> [<file> ...]` on every edited Python
  file, then `uv run pytest {tests} -x -q`.'''
    return _PromptBody(sections=sections, verify=verify)


def _prompt_job_output(ctx: _ReflectionContext) -> _PromptBody:
    tool_path = ctx.tool('background', '__init__.py')
    stream = ctx.tool('background', 'utils.py')
    common = ctx.tool('common.py')
    display = ctx.tool('display.py')
    tests = ', '.join(
        str(path)
        for path in (
            ctx.test('unit', 'tools', 'test_taskoutput.py'),
            ctx.test('test_background_utils.py'),
            ctx.test('test_process_task.py'),
            ctx.cli_test('tools', 'test_job_output_error_message.py'),
        )
    )
    sections = f'''## 1. Source of truth — read before changing anything

Job output tool (**the presentation/routing layer to review**):
- `{tool_path}` — `TaskOutput(CallableTool2)`, `name = "job_output"`,
  `TaskOutputParams` (`job_id`/`task_id` alias, `action`, `wait`/`block` alias,
  `timeout`/`timeout_ms` alias, `output_path`, `wait_for_pattern`, deprecated
  `kill`), `_failure_message`, `_exception_message`, `_list_tasks`,
  `_get_output`, `_kill_task`, `_get_history_output`,
  `_process_completed_output`.

State layer:
- `{stream}` — `BackgroundStream` (`get_output`, `drain_output_sync`,
  `pop_output`, `wait`, `wait_with_inactivity_timeout`, `wait_for_output`,
  `stop`, `format_output`), `bounded_append`, `bounded_put`, `FinishedTask`,
  `TaskData`, `record_finished_task`, `get_finished_task`, `add_task`,
  `get_all_tasks`, `remove_task_id`, `MAX_FINISHED_TASKS`, `_FINISHED_TASKS_LOCK`.
- `{common}` — `ProcessTask`, `_append_elapsed`, `_maybe_export_output_async`,
  `_maybe_export_rtk_original_async`, `_original_saved_message`.
- `{display}` — `BackgroundTaskDisplayBlock`.

Tests: `{tests}` — legacy `timeout_ms`, kill success semantics, `__del__`
cleanup, pattern waiting, original-file saving, the formatter callback, the
finished-task history and the never-empty error message.

## 2. Architecture in one paragraph
Two layers with a strict ownership boundary.  `background/utils.py` owns *state*:
per-session `TaskData` in `session.custom_data`, a thread-safe `BackgroundStream`
(single producer queue plus bounded buffers) and a bounded `OrderedDict` history
of finished tasks.  `background/__init__.py` owns *presentation*: routing,
messages, display blocks — and never spawns a process.  Output post-processing
is caller-supplied through `BackgroundStream.format_output`, which is how
`bash` / `pwsh` / `python` keep their own result shaping reachable through
`job_output`.  Completion is never lost: the rich `record_finished_task` entry
plus `remove_task_id`'s best-effort safety net keep a finished job retrievable
for `MAX_FINISHED_TASKS` reads.  Every string that can reach the model is forced
non-empty by `_failure_message` / `_exception_message`.

## 3. Workflow of one `job_output` tool call
1. `get_all_tasks(session)` snapshots the live tasks.
2. Route: `action == "list"` (or no id with `get`) -> `_list_tasks`;
   `action == "kill"` -> `_kill_task`; otherwise `_get_output`.
3. Any exception becomes `ToolError(_exception_message(exc))` — the catch-all is
   the outermost frame.
4. `_get_output`: strip the id, look up the live registry, then
   `get_finished_task` -> `_get_history_output`; a miss reports the available
   ids.
5. Pattern path: compile `wait_for_pattern`, then `wait_for_output` /
   `pop_output` (inactivity timeout `min(900, timeout)`); plain wait goes through
   `wait_with_inactivity_timeout`.
6. Completion: `_process_completed_output` (the owning tool's formatter, else
   RTK export + `_maybe_export_output_async`) -> `record_finished_task` ->
   `remove_task_id` -> `ToolError` (`_failure_message`) / `ToolOk` with a
   `BackgroundTaskDisplayBlock` and the `[status: ...]` / elapsed suffix.
7. `kill`: `stream.stop()` -> 5 s grace wait -> `pop_output` ->
   `_process_completed_output` -> record -> remove -> `ToolOk` (an explicit kill
   is always success).
8. `__del__` schedules `discard_all_tasks` via `call_soon_threadsafe`, guarded
   against interpreter finalization.

## 4. What to review (invariants a correct `job_output` result must keep)
- **`ToolError.message` is never empty**: an empty message aborts the whole turn
  in the conversation layer.  Both `_failure_message` and `_exception_message`
  must keep synthesizing a one-liner.
- **Exactly-once consumption**: `pop_output` truncates the buffer and
  `wait_for_output` clears it too; a path that returns without clearing
  double-reports output.
- **History cap 25 under the lock**: `_store_finished_locked` must refresh
  recency on re-insert and never grow past `MAX_FINISHED_TASKS`.
- **Lock ordering**: never take a stream lock and then the history lock
  (documented next to `_FINISHED_TASKS_LOCK`).
- **Rich record wins**: `remove_task_id` must only store its best-effort entry
  when the id is not already in `finished_tasks`.
- **`success` semantics**: `stream.success()["_success"]` is `result is False`,
  so `None` counts as success; flipping it changes every completed job.
- **Bounded buffers**: head-40%/tail-60% folding plus drop-oldest queue; both
  constants must stay patchable at call time.
- **Timeouts**: the `min(900, timeout)` inactivity cap, the 5 s kill grace and
  `MAX_JOB_OUTPUT_TIMEOUT_SECONDS = 7200` are deliberate; do not duplicate the
  magic numbers in new code.
- **No new broad excepts**: the single catch-all is the outermost frame; inner
  handlers must stay narrow.'''
    verify = f'''- Reproduce parameter normalization directly:
  `{ctx.python_path} -c "from kimi_cli.tools.background import TaskOutputParams; print(TaskOutputParams(job_id='a', timeout_ms=5000).timeout)"`.
- Reproduce buffer folding: `bounded_append(io.StringIO(), 'x'*20, 10)` and
  inspect the marker line.
- Add a regression case to `{ctx.test('unit', 'tools', 'test_taskoutput.py')}`
  (routing/kill/history) or `{ctx.test('test_background_utils.py')}` (state).
- Run `uv run tools/syntax_check.py <file> [<file> ...]` on every edited Python
  file, then
  `uv run pytest {ctx.test('unit', 'tools', 'test_taskoutput.py')} {ctx.test('test_background_utils.py')} -x -q`.'''
    return _PromptBody(
        sections=sections,
        verify=verify,
        context=(
            '`job_output` never spawns a process; it only observes tasks started '
            'by bash/pwsh/python.',
        ),
    )


def _prompt_todo_list(ctx: _ReflectionContext) -> _PromptBody:
    tool_path = ctx.tool('todo', '__init__.py')
    session_state = ctx.kimi_cli_src / 'session_state.py'
    display = ctx.tool('display.py')
    utils = ctx.tool('utils.py')
    tests = ', '.join(
        str(path)
        for path in (
            ctx.test('unit', 'tools', 'test_todolist.py'),
            ctx.test('regression', 'tools', 'test_backward_compat.py'),
            ctx.test('test_prompt_todo_reminder.py'),
            ctx.cli_test('tools', 'test_todo.py'),
            ctx.cli_test('tools', 'test_todo_list_contract.py'),
        )
    )
    sections = f'''## 1. Source of truth — read before changing anything

Todo list tool (**the engine to review**):
- `{tool_path}` — `TodoList(CallableTool2[Params])`, `name = "todo_list"`,
  `Params` (`todos` + 8 aliases, `mode`, `scope`/`parent`, `on_conflict`,
  `fuzzy`, `force`, `auto_fix`), `Todo` (title aliases, `status`, `notes`,
  `children`, `parent`, `rename_to`, `complete`, `fuzzy`, `force`),
  `normalize_title`, `_merge_upsert`, `_write_todos`, `_finalize`,
  `_read_todos`, `_enforce_single_in_progress`, `_auto_fix_in_progress`,
  `_check_regressions`, `_save_todos`, `_build_success_response`.
- `{session_state}` — `TodoItemState`, `TodoStatus`, `load_session_state`;
  persistence is role-split (root -> `session.state` + `save_state()`,
  subagents -> a per-session JSON state file).
- `{display}` — `TodoDisplayBlock`, `TodoDisplayItem` (what the user sees).
- `{utils}` — `repair_json_string` (used before pydantic for sloppy JSON).

Tests: `{tests}` — single `in_progress` enforcement, the all-done reminder on
write and read, prompt truncation, backward-compatible shapes, the merge
contract and title diagnosis.

## 2. Architecture in one paragraph
One tool with two engines that converge on a single exit.  *Whole-tree writes*
(`_write_todos`: `replace` / `clear`, archiving dropped done items) and a
*title-keyed upsert engine* (`_merge_upsert` -> `_upsert_one` -> `_apply_one_update`
/ `_resolve_scope_path` / `_upsert_in_container` / `_find_conflict`) are the only
ways the tree changes; both end in `_finalize`, so depth, regression,
single-`in_progress` and save guards run exactly once per call.  The model-facing
schema is *derived*, not hand-written (`_collapse_optional_array` emits `todos` as
a plain array and `_truncate_children_schema` flattens `children` to a leaf copy)
because recursive `$ref` is rejected by some providers.  Almost everything else
is defensive input repair (`_normalize_item_shape`, `_translate_legacy_shape`,
the mode/conflict alias maps, title-alias recovery), so a malformed call becomes
an actionable hint instead of a crash.

## 3. Workflow of one `todo_list` tool call
1. `todos is None`: `mode == "clear"` clears the tree (`_write_todos([])`),
   otherwise `_read_todos` renders the current tree.
2. `mode == "merge"` -> `_merge_upsert`; `replace` / `clear` with items ->
   `_write_todos`.
3. `_merge_upsert`: no-op detection -> `_build_noop_response`; duplicate titles
   via `_find_duplicate_titles`; total-node cap `_MAX_TODOS` (4096);
   `_derived_title_warnings`; advisory `_detect_fuzzy_warnings`.
4. Per item: `_effective_item` -> `_upsert_one` -> `_apply_one_update` /
   `_resolve_scope_path` / `_upsert_in_container` / `_find_conflict`
   (`normalize_title` + rapidfuzz for the nearest-title hint).
5. `_write_todos`: clear+items conflict, duplicates, node cap, load, all-done
   guards for `clear` / `replace`, then `_finalize`.
6. `_finalize`: tree-depth guard (`todo_max_layers + 1`, default 4),
   `_check_regressions` (done -> pending needs `force`), archive bounded at
   `_MAX_ARCHIVED_TODOS` (500), `_enforce_single_in_progress` /
   `_auto_fix_in_progress` (keep the last DFS pre-order, demote earlier ones), one
   `_save_todos`, then `_build_success_response` + `_build_display_block`.
7. `_read_todos`: status counts, `_render_read_tree` capped at `_MAX_READ_ITEMS`,
   the all-done reminder and the next-step hint.

## 4. What to review (invariants a correct `todo_list` call must keep)
- **Exactly one save per call**, at the end of `_finalize`; a rejected item must
  leave stored state untouched.
- **Single `in_progress`**: the enforcement must replace list slots rather than
  mutate `Todo` objects (they alias persisted items) and must keep the last
  in-progress item in DFS pre-order.
- **Regression guard** runs before archiving and is bypassable only by `force`.
- **Archive is bounded** (500) and only done items dropped by `replace`/`clear`
  are archived.
- **Depth cap**: `todo_max_layers + 1`; the config read must degrade to a
  documented default and log, never raise.
- **Title semantics**: `normalize_title` strips list markers/case/punctuation
  (set-of-words comparison, not a fuzzy threshold); the typo band is advisory
  only.  `on_conflict` decides what happens on a real collision.
- **Alias contract**: `extra="forbid"` on both models plus the alias tables is
  the anti-hallucination contract; dropping an alias silently breaks real
  traffic.
- **Schema derivation**: any new field must be considered by
  `_truncate_children_schema` (leaf copy) and `_collapse_optional_array`.
- **IO boundaries**: the broad `except Exception` blocks are load/save
  boundaries only — they must log and degrade, never leak into a successful
  write path.'''
    verify = f'''- Reproduce title normalization and alias handling:
  `{ctx.python_path} -c "from kimi_cli.tools.todo import normalize_title, Params; print(normalize_title('1. README: lines 55-63')); print(Params(todos=['x'], mode='add').mode)"`.
- Build a `TodoList` with a fake runtime/session and replay one recorded merge
  call; assert the stored tree, the single-in-progress rule and the response
  text (no pydantic-only test — the engine lives behind validation).
- Add a regression case to `{ctx.test('unit', 'tools', 'test_todolist.py')}` or
  the deeper `kimi-cli/tests/tools/test_todo_*.py` suites.
- Run `uv run tools/syntax_check.py <file> [<file> ...]` on every edited Python
  file, then
  `uv run pytest {ctx.test('unit', 'tools', 'test_todolist.py')} {ctx.cli_test('tools', 'test_todo_list_contract.py')} -x -q`.'''
    return _PromptBody(
        sections=sections,
        verify=verify,
        context=(
            '`/todo:<path>` (TODO comment scanner) is a different command; this '
            'review targets the `todo_list` tool.',
        ),
    )


def _prompt_read(ctx: _ReflectionContext) -> _PromptBody:
    tool_path = ctx.tool('file', 'read.py')
    extract = ctx.tool('file', 'read_extract.py')
    archive = ctx.tool('file', 'read_archive.py')
    sqlite = ctx.tool('file', 'read_sqlite.py')
    pdf = ctx.tool('file', 'read_pdf_pages.py')
    profiles = ctx.tool('file', 'read_profiles.py')
    markit = ctx.tool('file', 'read_markit.py')
    snapshot = ctx.tool('file', 'snapshot_store.py')
    conflict = ctx.tool('file', 'conflict_detect.py')
    file_utils = ctx.tool('file', 'utils.py')
    desc = ctx.tool('file', 'read.md')
    tests = ', '.join(
        str(path)
        for path in (
            ctx.cli_test('tools', 'test_read_file.py'),
            ctx.cli_test('tools', 'test_read_extract.py'),
            ctx.cli_test('tools', 'test_read_conflict.py'),
            ctx.test('unit', 'tools', 'test_read_window.py'),
            ctx.test('unit', 'tools', 'test_read_rich_dispatch.py'),
        )
    )
    sections = f'''## 1. Source of truth — read before changing anything

Read tool (**the file to review**):
- `{tool_path}` — `ReadFile(CallableTool2[Params])`, `name = "read"`,
  `MAX_LINES=5000`, `MAX_LINE_LENGTH=4000`, `MAX_FILES=32`,
  `MAX_BYTES=100 KiB`, `MAX_EXTRACT_BYTES=50 MiB`,
  `MAX_ARCHIVE_LIST_ENTRIES=500`; description from `{desc}`.

Rich-format and support modules (each one owns exactly one format):
- `{extract}` — `is_extractable_document`, `extract_document_markdown/text`
  (docx/html/pdf/…), `is_markdown_document`, `is_html_document`,
  `markdown_to_text`, `html_to_text`.
- `{archive}` — `is_archive_path`, `sniff_archive`, `ArchiveReader`,
  `normalize_member_path`, `format_archive_listing`, `is_binary_data`,
  `_SizeGuard` (8 MiB/member, 32 MiB total, ratio 10).
- `{sqlite}` — `read_sqlite`, `_validate_raw_select`, `_first_keyword`,
  `_WHERE_VIOLATION_PATTERN` (read-only SELECT only; `sql_where` rejects
  `;`, comments, `LIMIT`/`UNION`/`ATTACH`/...).
- `{pdf}` — `render_pdf_page` (page -> image part).
- `{profiles}` — `render_cpu_profile`, `render_sample_profile`.
- `{markit}` — markdown/HTML conversion helpers.
- `{snapshot}` — `record_file_snapshot` (best-effort drift baseline).
- `{conflict}` — `scan_conflict_lines`, `scan_file_for_conflicts`,
  `format_conflict_warning`, `parse_conflict_uri`.
- `{file_utils}` — `detect_file_type`, `MEDIA_SNIFF_BYTES`, `resolve_vfs`,
  `is_sensitive_file`, `kaos_path_from_tool_input`.

Tests: `{tests}` — line/byte/char windows, rich-format dispatch, archive
traversal, SQLite read-only enforcement, profile rendering and the conflict
footer.

## 2. Architecture in one paragraph
Three layers.  A *planner* (`_call_core`) normalizes scalar-or-list options,
expands globs, canonicalizes/dedupes paths and formats multi-file output.  A
*single-file policy pipeline* (`_read_single_file`) applies path, sensitivity and
type gates, then a fixed precedence: rich format -> extractable document ->
markdown/HTML conversion -> plain text.  A *renderer* (`_render_lines` ->
`_render_forward` / `_render_tail` -> `_render_result`) is shared by on-disk reads
and in-memory extracted text through one async line iterable.  Budgeting is
layered: line count, then a 100 KiB UTF-8 budget, then the
`char_offset`/`max_char` content window applied *before* line-number prefixes so
the window is identical with `show_line_numbers` on or off.  All read-side side
effects (snapshot, conflict footer, file mtime) are best-effort and
exception-suppressed.

## 3. Workflow of one `read` tool call
1. `ReadFile.__call__` -> optional `before_tool("read", ...)` bridge; the result
   is re-wrapped through `merge_message`.
2. `_call_core`: empty path and `MAX_FILES` checks; `_broadcast_option` repeats
   scalars over the path list; a path containing `conflict://` or ending with
   `:conflicts` routes to `_read_conflict_entries`.
3. Glob mode: `_is_glob_pattern` -> `_split_glob_path` /
   `_split_conflicts_suffix` -> `_expand_glob_path` (with
   `_validate_glob_directory`); every path is canonicalized, results deduped by
   canonical path, re-checked against `MAX_FILES`, then joined with
   `======== path ========` separators.
4. `_read_single_file`: `kaos_path_from_tool_input` -> `_validate_path`
   (protected read paths) -> `resolve_vfs` -> `is_sensitive_file` -> existence /
   `is_file` -> `detect_file_type(header=read_bytes(MEDIA_SNIFF_BYTES))` ->
   image/video refusal -> `_read_rich_format`.
5. `_read_rich_format` dispatch order: `pdf_page` -> archive -> SQLite ->
   `*.cpuprofile` / `*.sample.txt` -> `None` (docx/xlsx/pptx/ipynb fall through
   to document extraction when `render_markdown`).
6. Documents: `is_extractable_document` -> size gate -> extraction in a thread
   -> `_read_content`.  Markdown/HTML: `markdown_to_text` / `html_to_text` ->
   `_read_content`.
7. Plain text: `_read_as_text` for misnamed SQLite/archive, otherwise
   `_read_forward` / `_read_tail` -> `_render_lines` -> `_render_result`.
8. Post-processing: `_apply_conflict_footer` (scan, register, warn),
   `record_file_snapshot` (suppressed), `session.file_mtime.clean_file`.

## 4. What to review (invariants a correct `read` result must keep)
- **Line cap is visible**: `limit` clamps to `MAX_LINES` and `_render_result`
  must still print `Max 5000 lines reached.` when the clamp fires.
- **Byte budget**: `MAX_BYTES` is a hard 100 KiB cap in both forward and tail
  rendering; a truncation must be announced, never silent.
- **Char window**: `char_offset`/`max_char` slice *content characters* before
  line-number prefixes, keep original line numbers, and emit the three-branch
  (head/tail/middle) notice with counts.
- **Line format**: `f"{{line_no:6d}}\\t{{content}}"` with per-line truncation at
  4000 characters preserving the trailing newline.
- **Option grammar**: `offset != 0`, `offset >= -MAX_LINES`, `limit >= 1`,
  `max_char >= 0`; per-file option lists must have exactly one value per path;
  `pdf` / `archive` / `sql` are mutually exclusive and `sql_query` cannot mix
  with `sql_*`.
- **Archive safety**: traversal (`..`, absolute, backslash, NUL) rejected;
  decompression bounded by `_SizeGuard` and the compression-ratio cap.
- **SQL safety**: raw SQL is read-only (`SELECT` only) and `sql_where` cannot
  smuggle statement terminators/comments/destructive keywords.
- **Best-effort side effects**: snapshot, conflict footer and mtime updates may
  never fail a read; they log and continue.
- **Decoding**: reads use `errors="replace"` deliberately; do not turn a
  best-effort decode into a hard failure without an explicit, documented gate.
- **Multi-read shape**: dedupe by canonical path, re-check `MAX_FILES` after
  dedupe, and keep the bare single-result shape for one entry.'''
    verify = f'''- Reproduce the window logic:
  `{ctx.python_path} -c "from kimi_cli.tools.file.read import _apply_char_window_to_entries as f; print(f([(1, 'abcdef', False, 6)], 0, 3))"`.
- Replay one recorded call with a real temp file and assert the exact rendered
  lines (line numbers, separators, truncation notices) rather than a substring.
- Add a regression case to `{ctx.test('unit', 'tools', 'test_read_window.py')}`
  or `{ctx.cli_test('tools', 'test_read_file.py')}`.
- Run `uv run tools/syntax_check.py <file> [<file> ...]` on every edited Python
  file, then
  `uv run pytest {ctx.test('unit', 'tools', 'test_read_window.py')} {ctx.cli_test('tools', 'test_read_file.py')} -x -q`.'''
    return _PromptBody(sections=sections, verify=verify)


def _prompt_read_image(ctx: _ReflectionContext) -> _PromptBody:
    tool_path = ctx.tool('file', 'read_media.py')
    shared = ctx.tool('file', 'read_media_shared.py')
    file_utils = ctx.tool('file', 'utils.py')
    desc = ctx.tool('file', 'read_media.md')
    image_compress = ctx.kimi_cli_src / 'utils' / 'image_compress.py'
    format_policy = ctx.kimi_cli_src / 'utils' / 'image_format_policy.py'
    media_tags = ctx.kimi_cli_src / 'utils' / 'media_tags.py'
    tests = ', '.join(
        str(path)
        for path in (
            ctx.cli_test('tools', 'test_read_media_file.py'),
            ctx.cli_test('tools', 'test_read_media_gate.py'),
            ctx.cli_test('tools', 'test_read_media_file_desc.py'),
        )
    )
    sections = f'''## 1. Source of truth — read before changing anything

Image/video read tool (**the policy core to review**):
- `{tool_path}` — `ReadMediaFile(CallableTool2[Params])`, `name = "read_image"`,
  `Params` (`file_path`/`path` alias, `region`, `region_pct`, `full_resolution`,
  `info_only`, `max_dimension`, `quality`, `auto_convert`), `_call_core`,
  `_read_media`, `_validate_path`, `_build_image_decode_limit_error`,
  `_build_full_resolution_limit_error`, `_try_mipmap_fallback`.
- `{shared}` — `build_image_delivery_limit_error`, `build_media_note`,
  `ImageDelivery`, `wrap_media_part`, the note vocabulary.

Budgets and format policy (shared with other media consumers):
- `{image_compress}` — `IMAGE_BYTE_BUDGET`, `MAX_IMAGE_DECODE_BYTES`,
  `resolve_max_image_edge_px`, `resolve_read_image_byte_budget`,
  `sniff_image_dimensions`, `compress_image_for_model`,
  `crop_image_for_model`, `mipmap_downsample`.
- `{format_policy}` — `is_model_accepted_image_mime`.
- `{media_tags}` — media part wiring.
- `{file_utils}` — `detect_file_type`, `MEDIA_SNIFF_BYTES`.
- `{desc}` — the model-facing description template.

Tests: `{tests}` — the capability gate, the delivery/limit errors, the argument
shape and the description contract.

## 2. Architecture in one paragraph
A thin capability-gated wrapper over a policy core split into *validation*
(`_call_core`: identity, MIME, size, model capability and parameter-shape gates)
and *delivery* (`_read_media`: untouched / downsampled / cropped, producing a wire
part plus a model-facing note).  The tool raises `SkipThisTool` at construction
when the model supports neither `image_in` nor `video_in`.  Numeric policy lives
in `image_compress` (byte budgets, decode limit, max edge, mipmap fallback),
format policy in `image_format_policy`, and the note/delivery vocabulary in
`read_media_shared`, so the tool itself only decides *which* gate applies.
Delivery is fail-closed: when compression cannot meet the byte/edge budget the
original is never sent; the note always reports the original dimensions so
region coordinates stay derivable.

## 3. Workflow of one `read_image` tool call
1. `__call__` -> optional `before_tool("read_image", "read", ...)` bridge ->
   `_call_core`.
2. `_validate_path` (absolute-outside-workspace rule) -> existence / `is_file`
   -> `detect_file_type`.
3. Provider-rejected MIME: with `auto_convert`, convert via PIL to a sibling
   `<original>.converted.png`; otherwise return the conversion guidance.
4. Capability/limit gates: video without `video_in`; empty file; size >
   `MAX_MEDIA_MEGABYTES`; video with `region`/`full_resolution`;
   image over `MAX_IMAGE_DECODE_BYTES` with crop/native request;
   `full_resolution` without `region` over `IMAGE_BYTE_BUDGET`.
5. `_read_media`: `info_only` metadata fast path (PIL dimensions) ->
   `sniff_image_dimensions` -> `region_pct` -> `Region` -> `max_edge` and
   byte budget from the config resolvers.
6. Delivery: `crop_image_for_model(..., skip_resize=full_resolution)` for
   regions, else `compress_image_for_model` / `mipmap_downsample` with
   `_try_mipmap_fallback`; a failure returns
   `build_image_delivery_limit_error`.
7. Part assembly: `ImageURLPart` / `VideoURLPart` + `wrap_media_part`,
   `to_data_url` (pybase64), `ImageDelivery` + `build_media_note` (the
   `<system>`-wrapped note) -> `ToolOk`.

## 4. What to review (invariants a correct `read_image` result must keep)
- **Construction gate**: `SkipThisTool` (not a runtime error) when the model has
  neither `image_in` nor `video_in`; registering the tool in that state breaks
  every manifest that lists it.
- **No poisoned MIME in history**: a provider-rejected image must be converted
  or refused with guidance — never forwarded as an `image_url`.
- **The note never lies**: original pixel dimensions, the delivery kind
  (`untouched` / `downsampled` / `crop` / `full`) and any downgrade must be
  explicit; no silent re-encode or resize.
- **Fail closed on budget**: an over-budget image is never sent; the error tells
  the model which parameter to change (region / smaller `max_dimension` /
  `quality`).
- **Argument exclusivity**: `region` XOR `region_pct`; `full_resolution` XOR
  `info_only`; region/full-resolution are image-only.
- **`region_pct` needs sniffable dimensions**, else the explicit "use pixel
  coordinates" error; percentages must map to the original, not the delivered
  image.
- **Clamps**: `quality` stays 1..100 and `max_dimension` only overrides the model
  default (never the budget).
- **Side effects**: auto-conversion writes a sibling `.converted.png` on
  purpose; do not assume a cleanup pass exists.'''
    verify = f'''- Reproduce the budget constants and a dimension sniff:
  `{ctx.python_path} -c "from kimi_cli.utils.image_compress import IMAGE_BYTE_BUDGET, sniff_image_dimensions; print(IMAGE_BYTE_BUDGET)"`.
- Replay a recorded call against a real image file and assert the delivered part
  (mime, byte size, dimensions) plus the exact note text.
- Add a regression case to `{ctx.cli_test('tools', 'test_read_media_file.py')}`
  (delivery) or `{ctx.cli_test('tools', 'test_read_media_gate.py')}` (gates).
- Run `uv run tools/syntax_check.py <file> [<file> ...]` on every edited Python
  file, then
  `uv run pytest {ctx.cli_test('tools', 'test_read_media_file.py')} {ctx.cli_test('tools', 'test_read_media_gate.py')} -x -q`.'''
    return _PromptBody(sections=sections, verify=verify)


def _prompt_write(ctx: _ReflectionContext) -> _PromptBody:
    tool_path = ctx.tool('file', 'write.py')
    auto_generated = ctx.tool('file', 'auto_generated.py')
    conflict = ctx.tool('file', 'conflict_detect.py')
    check_fmt = ctx.tool('file', 'check_fmt.py')
    edit_safety = ctx.tool('file', 'edit_safety.py')
    snapshot = ctx.tool('file', 'snapshot_store.py')
    fs_cache = ctx.tool('file', 'fs_cache.py')
    file_utils = ctx.tool('file', 'utils.py')
    desc = ctx.tool('file', 'write.md')
    tests = ', '.join(
        str(path)
        for path in (
            ctx.cli_test('tools', 'test_write_file.py'),
            ctx.cli_test('tools', 'test_write_conflict.py'),
            ctx.cli_test('tools', 'test_write_gate.py'),
            ctx.cli_test('tools', 'test_auto_generated_guard.py'),
            ctx.test('test_custom_config.py'),
        )
    )
    sections = f'''## 1. Source of truth — read before changing anything

Write tool (**the guard pipeline to review**):
- `{tool_path}` — `WriteFile(CallableTool2[Params])`, `name = "write"`,
  `Params` (`file_path`/`path`, `content`/`text`, `sandbox_permissions`,
  `justification`, `mode`, `auto_fix_json`, `mkdir`, `show_diff`,
  `allow_conflicts`, `allow_auto_generated`), `_call_core`,
  `_dispatch_conflict_write`, `_resolve_single_conflict_by_id`,
  `_resolve_all_conflicts`, `_conflict_guard`, `_conflict_markers_error`,
  `_MODE_MAP`; description template `{desc}`.

Guards and side-effect helpers:
- `{auto_generated}` — `assert_editable_file` (path/name/header based),
  `assert_editable_file_content`, `detect_auto_generated_marker`,
  `should_block_auto_generated`.
- `{conflict}` — `scan_conflict_lines`, `find_dangling_openers`,
  `format_conflict_warning`, `splice_conflict`.
- `{check_fmt}` — `check_json_text` / `check_yaml_text` / `check_toml_text` /
  `check_xml_text` and `format_unified_diff`.
- `{edit_safety}` — `create_edit_parse_guard` (`observe_applied`, `finish`).
- `{snapshot}` — `record_content_snapshot` (4 MiB per snapshot, 64 entries).
- `{fs_cache}` — `invalidate_fs_scan_after_write`.
- `{file_utils}` — `kaos_path_from_tool_input`, `resolve_vfs`,
  `check_path_protected`, `is_within_directory`.

Tests: `{tests}` — protected paths, conflict refusal, the auto-generated guard,
mode handling and format repair.

## 2. Architecture in one paragraph
A single linear, guard-ordered pipeline rather than mode dispatch: normalize ->
path/approval gates -> auto-generated gate -> snapshot -> format check/repair ->
diff -> approval -> conflict guard -> write -> byte-exact verification ->
snapshot/cache invalidation -> guard notes.  `mode` only changes text
composition, diff construction and `append_text` vs `write_text`; every guard is
shared.  Safety is layered by *refusal point*: path, auto-generated, conflict and
format failures return `ToolError` deliberately placed before approval, so no
diff is shown for a write that will be rejected.  Recoverability comes from
`snapshot_store` (pre- and post-write snapshots) plus FS-cache invalidation, and
all of it is wrapped in `contextlib.suppress(Exception)` so bookkeeping can never
break a successful write.

## 3. Workflow of one `write` tool call
1. `WriteFile.__call__` -> optional `before_tool("write", "write", ...)` bridge
   -> `_call_core`.
2. Empty path -> error; a `conflict://` path -> `_dispatch_conflict_write`
   (`_resolve_single_conflict_by_id` / `_resolve_all_conflicts`) before any
   path resolution.
3. `kaos_path_from_tool_input` -> `is_within_directory` -> `_validate_path`
   (outside-dir needs an absolute path; protected write paths) ->
   `resolve_vfs(for_write=True)` -> `is_dir` refusal -> parent `mkdir` (or the
   `mkdir=False` error).
4. Read `old_text` with `errors="strict"` (a non-UTF-8 target fails) and record
   `file_existed`; run `assert_editable_file` (auto-generated guard); snapshot
   the pre-write content.
5. Compose `new_text` (overwrite or `old_text + content`); run
   `check_json/yaml/toml/xml_text`; for `.json` with `auto_fix_json`, repair and
   keep the repair diff.
6. Build the diff (`show_diff`, append fast path with 2 context lines) ->
   `approval.request(name, FileActions.EDIT | EDIT_OUTSIDE, ...)` ->
   `rejection_error()` on refusal.
7. `_conflict_guard`: overwrite refuses any new marker block; append also
   refuses when the existing file ends inside an unclosed block
   (`find_dangling_openers`); `allow_conflicts` downgrades to a warning.
8. `session.file_mtime.mark_dirty` -> `append_text` / `write_text` (UTF-8) ->
   `stat().st_size` verification against the expected byte count -> post-write
   snapshot + cache invalidation -> parse-guard `observe_applied`/`finish` ->
   result notes (diff, repair diff, conflict/drift/guard notes).

## 4. What to review (invariants a correct `write` call must keep)
- **Approval exactly once**, after the auto-generated and conflict guards, before
  any filesystem mutation.
- **Byte-exact verification**: compare UTF-8 byte counts, not characters; a
  mismatch must stay `Write verification failed (size mismatch)`.
- **Strict pre-read**: `old_text` uses `errors="strict"`; switching to `replace`
  silently corrupts every non-UTF-8 rewrite.
- **Auto-generated guard**: path/name/header based (first 1024 bytes, 40 header
  lines, LRU keyed by mtime+size), gated by `edit.block_auto_generated`;
  `allow_auto_generated=True` bypasses it fully.
- **Conflict asymmetries**: write refuses based on the *new* content; append also
  checks dangling openers in the old file; `allow_conflicts` only downgrades.
- **JSON repair scope**: `.json` + `auto_fix_json` only, and the repair must be
  surfaced both in the note and as a diff.
- **`mkdir=False` never creates parents**; a missing parent is an error.
- **Bookkeeping is non-fatal**: snapshots, FS-cache invalidation and parse-guard
  calls stay inside `suppress`.
- **Format failure narration**: the file is already written, so the error text
  must say "successfully overwritten, but ..." and keep its `is_error` shape.
- **Append diff anchor**: `old_start = max(1, len(old_lines) - 2)` — the two
  context lines are part of the contract.'''
    verify = f'''- Reproduce the auto-generated refusal:
  `{ctx.python_path} -c "from kimi_cli.tools.file.auto_generated import assert_editable_file_content as a; print(a('// Code generated by protoc. DO NOT EDIT.', 'x.pb.go'))"`.
- Replay one recorded write against a temp file and assert the *post-write*
  content, the byte count and the notes (diff/repair/conflict/drift).
- Add a regression case to `{ctx.cli_test('tools', 'test_write_file.py')}` or
  `{ctx.cli_test('tools', 'test_write_conflict.py')}`.
- Run `uv run tools/syntax_check.py <file> [<file> ...]` on every edited Python
  file, then
  `uv run pytest {ctx.cli_test('tools', 'test_write_file.py')} {ctx.test('test_custom_config.py')} -x -q`.'''
    return _PromptBody(sections=sections, verify=verify)


def _prompt_edit(ctx: _ReflectionContext) -> _PromptBody:
    tool_path = ctx.tool('file', 'edit', '__init__.py')
    base = ctx.tool('file', 'edit', 'base.py')
    params = ctx.tool('file', 'edit', 'params.py')
    modes = ctx.tool('file', 'edit', 'modes', '__init__.py')
    replace_mode = ctx.tool('file', 'edit', 'modes', 'replace.py')
    sloppy_mode = ctx.tool('file', 'edit', 'modes', 'sloppy.py')
    replace_shim = ctx.tool('file', 'replace.py')
    edit_safety = ctx.tool('file', 'edit_safety.py')
    conflict = ctx.tool('file', 'conflict_detect.py')
    auto_generated = ctx.tool('file', 'auto_generated.py')
    snapshot = ctx.tool('file', 'snapshot_store.py')
    check_fmt = ctx.tool('file', 'check_fmt.py')
    fs_cache = ctx.tool('file', 'fs_cache.py')
    tests = ', '.join(
        str(path)
        for path in (
            ctx.cli_test('tools', 'test_edit_file.py'),
            ctx.cli_test('tools', 'test_edit_file_fuzzy.py'),
            ctx.cli_test('tools', 'test_edit_sloppy.py'),
            ctx.cli_test('tools', 'test_edit_modes_detect.py'),
            ctx.cli_test('tools', 'test_edit_conflict.py'),
            ctx.test('test_note.py'),
        )
    )
    sections = f'''## 1. Source of truth — read before changing anything

Edit tool (**the dispatcher and the two executors to review**):
- `{tool_path}` — `EditFile(CallableTool2[EditParams])`, `name = "edit"`; a
  stateless façade that resolves a mode (`auto` -> `detect_mode`) and delegates
  to one executor held in `self._tool`; back-compat shims `_apply_edit`,
  `_find_similar`, `_apply_replace_all`, `_apply_fuzzy_fallback`.
- `{base}` — `BaseEditTool`: path validation, VFS resolve, text read/write,
  format check, JSON repair, approval, conflict refusal, staleness, exception
  wrapping (`_validate_path`, `_resolve_for_write`, `_read_text`, `_write_text`,
  `_check_format`, `_check_conflicts`, `_try_repair_json`).
- `{params}` — `EditParams` (`mode`, `file_path`/`path`, `edit`/`edits`,
  `old_string`, `new_string`, `replace_all`, `input`, `sandbox_permissions`,
  `justification`, `allow_conflicts`, `allow_auto_generated`, `resolved_mode`),
  `ReplaceEditItem`, `detect_mode`, `normalize_edit_mode`.
- `{modes}` — `MODE_REGISTRY` (`replace`, `sloppy`).
- `{replace_mode}` — `ReplaceModeExecutor`: literal -> strip -> fuzzy ladder,
  `_apply_edit`, `_apply_replace_all`, `_find_similar`, `_find_best_fuzzy_match`,
  `_try_strip_match`, `_try_snapshot_recovery`.
- `{sloppy_mode}` — `SloppyModeExecutor`: `parse_sloppy_input`, `_split_sections`,
  `_parse_op`, `_apply_block_op`, `_apply_inline_op`, `_find_exact_block`,
  `_find_fuzzy_block`.
- `{replace_shim}` — must keep re-exporting `Edit`, `EditFile`, `Params`.

Guards and side effects (shared with `write`):
- `{edit_safety}` — `create_edit_parse_guard`; `{conflict}` — conflict scan and
  marker handling; `{auto_generated}` — `assert_editable_file_content`;
  `{snapshot}` — `record_content_snapshot`; `{check_fmt}` — format checks;
  `{fs_cache}` — `invalidate_fs_scan_after_write`.

Tests: `{tests}` — replace/fuzzy/sloppy modes, mode detection, conflict
asymmetry, protected paths and the plan tool that reuses the internals.

## 2. Architecture in one paragraph
A dispatcher/executor split.  `EditFile` resolves a mode and delegates; *all*
shared safety lives in `BaseEditTool`, so both executors inherit identical path,
approval, conflict, format and staleness behaviour.  Each executor owns only its
search-and-replace algorithm: `ReplaceModeExecutor` walks a literal -> stripped ->
fuzzy ladder with rapidfuzz suggestions and snapshot-anchor recovery, while
`SloppyModeExecutor` parses every `§`/`⟪│⟫` operation, preflights every target
path, then applies atomically.  Matching normalizes CRLF to LF while the returned
content keeps the original line endings (re-attached from the untouched
prefix/suffix).  Snapshot and parse-guard hooks make drift recoverable and report
model mis-parses without failing the edit.

## 3. Workflow of one `edit` tool call
1. `EditFile.__call__` -> optional `before_tool("edit", "edit", ...)` bridge ->
   `_call_core` (repairs a missing `resolved_mode` to `replace`) ->
   `MODE_REGISTRY[mode].execute(self._tool, params)`.
2. Normalize the edit list (`_normalize_single_edit` / `_coerce_edit`) ->
   `kaos_path_from_tool_input` -> `_validate_path` -> `_resolve_for_write` ->
   `stat` + `S_ISREG` -> `_read_text`.
3. Pre-edit: `scan_conflict_lines` + history registration ->
   `assert_editable_file_content` -> `record_content_snapshot` (suppressed).
4. Apply in a worker thread (`asyncio.to_thread(_work)`): `_apply_edit` per item;
   identical content -> `_try_snapshot_recovery`, otherwise
   `No replacements were made` plus a `Did you mean:` suggestion.
5. Gates: `build_diff_blocks` -> `approval.request("edit", EDIT | EDIT_OUTSIDE,
   ...)` -> `_check_format` + `_try_repair_json` -> `_check_conflicts` ->
   `file_mtime.mark_dirty`.
6. Write `_write_text` -> post-write snapshot + cache invalidation ->
   post-apply conflict bookkeeping (`introduced N block(s)`) -> parse-guard
   `observe_applied`/`finish` -> result shaping.
7. `_apply_edit`: no-op on empty/identical; normalize CRLF; `replace_all` ->
   `_apply_replace_all`; first exact hit -> single replace; `match_mode="exact"`
   -> suggestion only (`_find_similar`); otherwise `_apply_fuzzy_fallback`.
8. Sloppy mode: `parse_sloppy_input` -> atomic path preflight -> `_apply_block_op`
   / `_apply_inline_op` -> write.

## 4. What to review (invariants a correct `edit` call must keep)
- **Always resolve a mode**: the validator populates `resolved_mode` and the
  dispatcher repairs `None` to `replace`; an unknown mode stays
  `ToolError("Invalid edit mode ...")`.
- **Recovery before failure**: the unchanged-content branch must try the snapshot
  anchor first and its message must say "applied against the version read at ...".
- **Line endings**: matching normalizes CRLF, the result restores the original
  endings (`_try_strip_match` re-attaches the exact ending).
- **Fuzzy threshold**: default 75.0; `match_mode="exact"` must never apply a fuzzy
  edit, only suggest one.
- **Conflict asymmetry**: `edit` refuses based on the *original* content,
  `write` on the *new* content; keep both.
- **Auto-generated refusal is content-based** for `edit` (path/header based for
  `write`); both honour `allow_auto_generated`.
- **Lenient IO with verification**: `BaseEditTool` reads/writes with
  `errors="replace"` and has no post-write byte check; if a fix adds
  verification it must not tighten the lenient read into a hard failure.
- **`mark_dirty` placement**: after approval/diff, immediately before the write,
  so drift is reported rather than fatal.
- **Dispatch table**: `MODE_REGISTRY` is the only dispatch point and
  `file/replace.py` must keep its re-exports for old imports.
- **Stateless shims**: `EditFile`'s compatibility methods run on
  `object.__new__` instances in existing tests; they may not depend on `__init__`.'''
    verify = f'''- Reproduce matching without touching disk:
  `{ctx.python_path} -c "from kimi_cli.tools.file.edit.modes.replace import ReplaceModeExecutor; from kimi_cli.tools.file.edit.params import ReplaceEditItem; print(ReplaceModeExecutor()._apply_edit('a\\nb\\n', ReplaceEditItem(old='b', new='c')))"`.
- Replay one recorded edit against a temp file and assert content, replacement
  count, mode chosen and notes (suggestion/repair/conflict).
- Add a regression case to `{ctx.cli_test('tools', 'test_edit_file.py')}` or
  `{ctx.cli_test('tools', 'test_edit_sloppy.py')}`.
- Run `uv run tools/syntax_check.py <file> [<file> ...]` on every edited Python
  file, then
  `uv run pytest {ctx.cli_test('tools', 'test_edit_file.py')} {ctx.cli_test('tools', 'test_edit_sloppy.py')} -x -q`.'''
    return _PromptBody(sections=sections, verify=verify)


def _prompt_subagent(ctx: _ReflectionContext) -> _PromptBody:
    tool_path = ctx.tool('subagent', '__init__.py')
    store = ctx.tool('subagent', 'store.py')
    common = ctx.tool('common.py')
    prompt_common = ctx.tool('prompt_common.py')
    session_py = ctx.kimi_cli_src / 'session.py'
    tool_pairing = ctx.kimi_cli_src / 'soul' / 'tool_pairing.py'
    tests = ', '.join(
        str(path)
        for path in (
            ctx.test('kimix', 'test_subagent_batch.py'),
            ctx.test('kimix', 'test_subagent_interrupt_race.py'),
            ctx.test('kimix', 'test_subagent_anonymous_session.py'),
            ctx.test('kimix', 'test_agent_conversation.py'),
            ctx.cli_test('tools', 'test_subagent_output_file.py'),
        )
    )
    sections = f'''## 1. Source of truth — read before changing anything

Sub-agent tool (**the orchestration to review**):
- `{tool_path}` — `Agent(CallableTool2)`, `name = "subagent"`,
  `SubAgentParams` (`prompt`/`task` alias, `session_id`/`session` alias,
  `close_session`, `return_history`, `history_format`, `inherit_context`),
  `_check_batch_shape`, `_split_params`, `_PreparedRun`.
- Lifecycle helpers: `_claim_session_run`, `_release_session_run`,
  `_abandon_unlaunched`, `_prepare_run`, `_resolve_prompt` (`@file` prompts),
  `_resolve_session`, `_inherit_parent_context`, `_execute`,
  `_AgentConversationCollector`, `_write_subagent_output`, `_output_message`,
  `_build_extras`, `_update_store`, `_dispatch_batch`,
  `_dispatch_batch_foreground`, `_rollback_prepared`.
- Registries: `_agent_entries`, `_agent_sessions`, `_child_sessions`,
  `_active_runs`; teardown via `_ensure_close_hook_registered`,
  `_on_parent_session_closed`, `_destroy_child_sessions_async`.
- `{store}` — `AgentSessionStore` (`get`, `put`, `list_active`, `close`,
  `was_closed`, `evict_lru_if_needed`, `MAX_SESSIONS=10`,
  `MAX_TOMBSTONES=64`), stored on `session.custom_data`.
- `{session_py}` — `Session.copy` (context inheritance).
- `{common}` — `_create_script_file`, `_export_to_temp_file_async`
  (large-prompt spill).
- `{prompt_common}` — `accepts_alias_text` (description only).
- `{tool_pairing}` — the balanced-cut rule: a sub-agent turn is one tool call
  plus one tool result, never split across a compaction boundary.

Tests: `{tests}` — batching, interrupt races, anonymous sessions, conversation
history formats and the output-file contract.

## 2. Architecture in one paragraph
Three module-level registries (`_agent_entries`, `_agent_sessions`,
`_child_sessions`) plus `_active_runs` as a one-run-per-session claim token; the
token compare in `_release_session_run` makes a late release a no-op.  Sessions
are real SDK sessions (anonymous unless a session dir already exists) created
with `agent_subagent.json`, `SystemPromptType.TrivialSubAgent` and
`resume=True`, then recorded in `AgentSessionStore` so `close_session=False`
sessions stay resumable.  Child sessions are owned by the parent: close hooks
destroy them, and there is deliberately no `Agent.__del__` — teardown is driven by
session lifecycle.  Output is written to a file (`.kimix_cache/temp_<id>.md`) and
only the path is returned, so a sub-agent's transcript can never bloat the
parent context; the parent only sees the tool result plus the file path.

## 3. Workflow of one `subagent` tool call
1. `Agent.__call__` rejects recursion through
   `session.custom_config["is_sub_agent"]`; a list prompt -> `_dispatch_batch`,
   a string prompt -> acquire the semaphore (limit 8) -> `_prepare_run`.
2. `_prepare_run`: `_session_work_dir` -> `_resolve_prompt` (`@file` inlined) ->
   prompts over 100 KB are spilled to a script file ("read the task from ...")
   -> `_resolve_session`.
3. `_resolve_session`: reuse a live store entry when it is active and not closed;
   otherwise `session_id = params.session_id or uuid4()`, `anonymous = not
   session_dir(...).exists()`, optional `_inherit_parent_context` (with the
   system prompt reset), `_create_session_async(..., resume=True)`, set
   `sub_custom_config["is_sub_agent"]`, register the child and discard closed
   entries.
4. Busy check (`_cancel_event`) then `_claim_session_run`; a rejected claim
   abandons only the freshly created session; effective `close_session`
   defaults to `True`.
5. `_execute`: closed-session gate -> `_AgentConversationCollector` ->
   `utils.prompt_async(..., ensure_todo_finished=False,
   merge_wire_messages=True, format_output=True)` ->
   `_write_subagent_output` -> `_output_message` ("Output saved to ...\\
   Session ID: ...") -> `_build_extras` -> `_update_store` -> `ToolOk`.
6. Error branch: close the child (`close_session_async` + `store.close` +
   unregister entry/session/child) then return `ToolError`; `finally` always
   releases the run claim.
7. Batch: prepare all with `asyncio.gather`; any preparation failure ->
   `_rollback_prepared`; `_dispatch_batch_foreground` merges `output_files`,
   `turn_count`, failures and `extras["status"] = completed | partial`.

## 4. What to review (invariants a correct `subagent` call must keep)
- **One run per session id**: claim before any prompt, release in `finally`; a
  duplicate resume id in one batch must be rejected by `_check_batch_shape`.
- **Never prompt a closed session**: `_session_is_closed` must stay
  duck-typed (`getattr(session, "_closed", False) is True`).
- **No orphan on preparation errors**: prompt resolution precedes session
  creation, and rollback/abandon only close sessions created here
  (`is_reused is False`).
- **Failures still report**: a failed run returns its `session_id` and
  `output_files`; all-failed batches return `ToolError` with extras.
- **Output is file-only**: `_output_file_name` flattens ids to
  `[A-Za-z0-9._-]` and caps length, so path traversal stays impossible.
- **Store must not resurrect a closed session**: `was_closed` and the
  stale-entry identity check (`existing.session is not session`).
- **Eviction never touches a busy session**: `is_busy` is
  `sid in _active_runs`, and LRU skips `state == "running"`.
- **Child ownership**: close hooks must close every child; sub-agent sessions are
  anonymous so their scratch dir is deleted on close.
- **Recursion guard stays**: `is_sub_agent` is set on the child's custom config.
- **One tool result per turn**: the outcome is a single result plus an
  out-of-band file path (`{tool_pairing}`); do not stream the transcript inline.'''
    verify = f'''- Reproduce the identity/wiring surface:
  `{ctx.python_path} -c "from kimi_cli.tools.subagent import Agent, _claim_session_run, SubAgentParams; print(Agent.name, list(SubAgentParams.model_fields))"`.
- Replay a batch with fake sessions and assert: claims released, child sessions
  closed exactly once, `output_files` present on both success and failure.
- Add a regression case to `{ctx.test('kimix', 'test_subagent_batch.py')}` or
  `{ctx.test('kimix', 'test_subagent_interrupt_race.py')}`.
- Run `uv run tools/syntax_check.py <file> [<file> ...]` on every edited Python
  file, then
  `uv run pytest {ctx.test('kimix', 'test_subagent_batch.py')} {ctx.test('kimix', 'test_subagent_interrupt_race.py')} -x -q`.'''
    return _PromptBody(
        sections=sections,
        verify=verify,
        context=(
            'Sub-agents belong to the spawning session: closing or clearing the '
            'parent closes them all.',
        ),
    )


def _prompt_list_agents(ctx: _ReflectionContext) -> _PromptBody:
    tool_path = ctx.tool('subagent', '__init__.py')
    store = ctx.tool('subagent', 'store.py')
    tests = ', '.join(
        str(path)
        for path in (
            ctx.test('kimix', 'test_subagent_batch.py'),
            ctx.test('kimix', 'test_subagent_interrupt_race.py'),
            ctx.test('kimix', 'test_agent_conversation.py'),
        )
    )
    sections = f'''## 1. Source of truth — read before changing anything

List-agents tool (**the view to review**):
- `{tool_path}` — `AgentList(CallableTool2)`, `name = "list_agents"`,
  `AgentListParams` (`scope`), `AgentList.__call__`.  Its sibling
  `AgentClose` (`name = "interrupt_agent"`) is the only writer that removes an
  entry.
- `{store}` — `AgentSessionStore.list_active`, `get`, `put`, `close`,
  `_notify_closed`, `AgentSessionEntry`, `evict_lru_if_needed`.

Tests: `{tests}` — batch visibility, interrupt races and conversation-history
turn counts.

## 2. Architecture in one paragraph
A read-only view of `AgentSessionStore.entries`.  A session appears only if the
store still considers it active: `_update_store` puts it (state `completed`) when
`close_session=False`, and `store.close()` + `_notify_closed` removes it when a
session is interrupted or closed.  Rendering is a single `orjson.dumps` of plain
dicts, so the response is stable, ordered and cheap.  The tool owns no state and
must never mutate the store — its only job is to make the resumable ids
discoverable so `subagent(session_id=...)` can continue them.

## 3. Workflow of one `list_agents` tool call
1. `AgentList.__call__` -> `_get_store(self._session)` (the per-session
   `AgentSessionStore`).
2. `store.list_active()` snapshots the active entries.
3. `orjson.dumps(sessions, option=orjson.OPT_INDENT_2)` renders them.
4. `ToolOk(brief="Listed active subagents")`.

## 4. What to review (invariants a correct `list_agents` result must keep)
- **Only active entries**: a closed/tombstoned session must never be listed, and
  every listed id must be resumable through `subagent(session_id=...)`.
- **`total_turns` is `len(conversation_history)`**, not an independent counter.
- **Stable JSON**: key order and types stay deterministic (`orjson`, plain
  dicts); the web UI and tests depend on it.
- **No side effects**: listing must not evict, tombstone, refresh recency or
  claim a run.
- **Close symmetry**: `interrupt_agent` / `close_session=True` must notify the
  module registries so nothing keeps pointing at a dead session.
- **`scope`**: `children` and `descendants` currently return the same list; a
  real filtering change is a behaviour change that needs a test.
- **Unknown ids**: `interrupt_agent` on an unknown id returns
  `ToolError("Session not found")`; `list_agents` never errors on an empty
  store.'''
    verify = f'''- Reproduce the store view with a fake session:
  `{ctx.python_path} -c "from kimi_cli.tools.subagent import AgentList, AgentListParams; print(AgentList.name, list(AgentListParams.model_fields))"`.
- Add a regression case to `{ctx.test('kimix', 'test_subagent_batch.py')}` that
  closes a session and asserts it disappears from the listing.
- Run `uv run tools/syntax_check.py <file> [<file> ...]` on every edited Python
  file, then
  `uv run pytest {ctx.test('kimix', 'test_subagent_batch.py')} -x -q`.'''
    return _PromptBody(sections=sections, verify=verify)


def _prompt_glob(ctx: _ReflectionContext) -> _PromptBody:
    tool_path = ctx.tool('file', 'glob.py')
    fs_cache = ctx.tool('file', 'fs_cache.py')
    micro = ctx.tool('file', 'micro_compress.py')
    output_utils = ctx.tool('file', 'output_utils.py')
    file_utils = ctx.tool('file', 'utils.py')
    desc = ctx.tool('file', 'glob.md')
    native = ctx.repo_root / 'bin' / 'kimix_native'
    tests = ', '.join(
        str(path)
        for path in (
            ctx.cli_test('tools', 'test_glob.py'),
            ctx.test('unit', 'tools', 'test_glob_gitignore_hint.py'),
            ctx.cli_test('tools', 'test_fs_cache_invalidation.py'),
        )
    )
    sections = f'''## 1. Source of truth — read before changing anything

Glob tool (**the file to review**):
- `{tool_path}` — `Glob(CallableTool2[Params])`, `name = "glob"`,
  `Params` (`pattern`, `path`/`directory` alias, `include_dirs`,
  `respect_gitignore`, deprecated `include_ignored`, `verbose`, `timeout`,
  `max_results`/`fold` alias), `MAX_MATCHES=1000`, `MAX_BYTES=100 KiB`,
  `_is_unsafe_recursive_pattern`, `_get_gitignore_rules`, `_parse_gitignore`,
  `_gitignore_match`, `_is_ignored_by_gitignore`, `_top_dirs_summary`.
- `{desc}` — the model-facing description (kept in sync with behaviour).
- `{fs_cache}` — `invalidate_fs_scan_after_write` -> `_bust_gitignore_cache`.
- `{micro}` — `compress_lines` / `MicroCompressConfig`
  (`near_dup_collapse=False` for glob).
- `{output_utils}` — `fold_lines`, `truncate_line`.
- `{file_utils}` — `resolve_vfs`, `kaos_path_from_tool_input`.
- `{native}` — the optional native `.gitignore` parser used by `_parse_gitignore`.

Tests: `{tests}` — gitignore negation/ancestor rules, the exclusion hint, fold
and truncation, the max-results sentinel and cache invalidation.

## 2. Architecture in one paragraph
A thin async wrapper over `KaosPath.glob` with a `.gitignore` layer cached per
resolved root.  The cache is self-validating on every call (per-file mtime plus a
set-diff of discovered `.gitignore` paths) and is invalidated by the FS cache
after writes/deletes/renames, so a just-created `.gitignore` applies immediately.
Rules are parsed (native fast path when available) into `_GitignoreRule` and
evaluated in discovery order so last-match-wins gives negation semantics;
directory-only rules also match descendants by walking ancestor prefixes.
Results are path-sorted (lexicographic), not mtime-ordered — mtime only appears in
`verbose` output.  Budgets apply in a fixed order: collection cap -> line fold ->
micro-compress -> byte cap, each with its own visible message.

## 3. Workflow of one `glob` tool call
1. `__call__` -> `_is_unsafe_recursive_pattern` (`**`, `**/*`, ...) ->
   `ToolError("Unsafe pattern ...")` before any I/O.
2. `dir_path = kaos_path_from_tool_input(params.path, work_dir)` (else the work
   dir) -> `resolve_vfs(..., for_write=False)` -> exists / is_dir checks.
3. Inside `asyncio.timeout(params.timeout)`:
   `_get_gitignore_rules(resolved_root)` in a thread (failures swallowed).
4. `async for match in dir_path.glob(pattern)`: skip non-files unless
   `include_dirs`; `_is_ignored_by_gitignore` on the resolved path, retried on the
   walked path when `resolve()` escaped the root (junctions, `.venv`); count
   ignored entries; cap at `MAX_MATCHES` (`truncated=True`).
5. Sort matches -> relative-path lines (or `(size bytes, kind, mtime)` in
   `verbose`) -> `MAX_BYTES` budget -> `truncated_by_bytes`.
6. `fold_lines(output_lines, max_results)` -> micro-compress ->
   message assembly (`Found N matches`, gitignore hint, fold note +
   `_top_dirs_summary`, cap/timeout/byte notes) -> `ToolOk`; any exception logs
   and returns `ToolError`.

## 4. What to review (invariants a correct `glob` result must keep)
- **Unsafe patterns rejected before I/O**: `**`, `**/*` and backslash variants.
- **Ignore defaults**: `respect_gitignore` defaults to `True`;
  `include_ignored=True` must force it to `False` (deprecated alias).
- **Negation semantics**: `!pattern` un-ignores, including directory-only
  negations un-ignoring descendants.
- **Ancestor rules**: a rule matching an ancestor directory must exclude its
  descendants.
- **Hint on exclusions**: `ignored_count > 0` must produce the "excluded by
  .gitignore — pass respect_gitignore=False" hint.
- **Cache freshness**: mtime change, new file and deletion must all refresh;
  `invalidate_gitignore_cache` treats the path and its parent as candidates and
  must never raise.
- **Hard caps**: 1000 collected matches, 100 KiB output; `max_results=0` means
  unlimited folding, not unlimited collection.
- **Timeouts return partial results** with an explicit message — never an empty
  success.
- **Symlink/junction escape**: the ignore check runs on both the resolved and the
  walked path so a junction cannot silently defeat ignore rules.
- **No mtime ordering**: results are lexicographic; only `verbose` exposes mtime.'''
    verify = f'''- Reproduce the ignore layer:
  `{ctx.python_path} -c "from kimi_cli.tools.file.glob import _get_gitignore_rules, _is_ignored_by_gitignore, Params; from pathlib import Path; root=Path('.').resolve(); rules=_get_gitignore_rules(root); print(_is_ignored_by_gitignore((root/'.venv'/'x.py'), rules, root))"`.
- Replay one recorded call in a temp tree with a `.gitignore` containing a
  negation and a directory rule; assert the exact file list and the hint text.
- Add a regression case to `{ctx.cli_test('tools', 'test_glob.py')}` or
  `{ctx.test('unit', 'tools', 'test_glob_gitignore_hint.py')}`.
- Run `uv run tools/syntax_check.py <file> [<file> ...]` on every edited Python
  file, then
  `uv run pytest {ctx.cli_test('tools', 'test_glob.py')} {ctx.test('unit', 'tools', 'test_glob_gitignore_hint.py')} -x -q`.'''
    return _PromptBody(sections=sections, verify=verify)


def _prompt_grep(ctx: _ReflectionContext) -> _PromptBody:
    tool_path = ctx.tool('file', 'grep_local.py')
    archive = ctx.tool('file', 'grep_archive.py')
    output = ctx.tool('file', 'grep_output.py')
    recorder = ctx.tool('file', 'grep_recorder.py')
    selectors = ctx.tool('file', 'grep_selectors.py')
    micro = ctx.tool('file', 'micro_compress.py')
    output_utils = ctx.tool('file', 'output_utils.py')
    file_utils = ctx.tool('file', 'utils.py')
    ripgrep_common = ctx.kimi_cli_src / '_ripgrep_common.py'
    rtk_common = ctx.kimi_cli_src / '_rtk_common.py'
    tests = ', '.join(
        str(path)
        for path in (
            ctx.cli_test('tools', 'test_grep.py'),
            ctx.cli_test('tools', 'test_grep_selectors.py'),
            ctx.cli_test('tools', 'test_grep_archive.py'),
            ctx.cli_test('tools', 'test_grep_output.py'),
            ctx.test('test_grep_filter.py'),
            ctx.test('test_filter_output.py'),
        )
    )
    sections = f'''## 1. Source of truth — read before changing anything

Grep tool (**the file to review**):
- `{tool_path}` — `Grep(CallableTool2[Params])`, `name = "grep"`,
  `Params` (`pattern`, `path`, `grouped`, `record`, `include`/`glob`, `output_mode`,
  `-B`/`-A`/`-C` context aliases, `-n`, `-i`, `type`, `head_limit`, `offset`,
  `multiline`, `include_ignored`, `timeout`, `deduplicate_output`/`token_kill`,
  `max_output_lines`/`fold`).
- Key functions: `_build_rg_args`, `_run_rg_subprocess`, `_postprocess`,
  `_rich_call`, `parse_content_line`, `_strip_path_prefix`,
  `_range_filter_lines`, `_join_with_byte_limit`, `_is_windows_reserved_name`,
  `_is_sensitive_cached`, `_is_eagain`, `backup_grep` / `_backup_grep_impl` /
  `_backup_grep_search`, `_ensure_rg_path`, `_ensure_rtk_path`.
- Supporting modules: `{archive}` (`parse_archive_path_candidates`,
  `materialize_archive_members`), `{output}` (`group_lines_by_file`,
  `format_grouped_output`), `{recorder}` (`record_grep_files`,
  `RECORDER_CAP=500`), `{selectors}` (`expand_path_entries`,
  `split_path_and_sel`, `selector_line_ranges`), `{micro}`,
  `{output_utils}` (`parse_rtk_rg_output`, `fold_lines`, `truncate_line`),
  `{file_utils}`, `{ripgrep_common}`, `{rtk_common}`.

Tests: `{tests}` — backend selection and argv, rich/archive selectors, grouped
output, the recorder, byte caps and token-filter interaction.

## 2. Architecture in one paragraph
Two backends behind one tool: ripgrep (optionally wrapped by RTK) and a
pure-Python scanner reached only when `rg` cannot be resolved or the VFS is dirty;
there is deliberately no native grep kernel.  Backend selection happens once per
call and degrades (`rtk -> rg -> backup_grep`), never raising just because an
accelerator is missing.  The `rg` run and the post-processing pass share
`params.timeout`, so a pathological match stream cannot hang the call, and output
is byte-capped twice (pipe buffer, then the final join).  Line identity is
preserved through one `path:LN:text` / `path-LN-text` grammar, so prefix
stripping, sensitive filtering, range filtering and grouped rendering all parse
the same shape.  Archive members are materialized into a temp dir and remapped to
`archive:member` display keys, and matched files are recorded per session so a
follow-up read/edit pass sees exactly the same set.

## 3. Workflow of one `grep` tool call
1. `__call__`: a dirty VFS -> `backup_grep`; a missing `rg` -> `_ensure_rg_path`
   (else `backup_grep`).
2. `expand_path_entries` + `_entries_are_rich`; RTK is resolved only when
   `deduplicate_output`; rich entries -> `_rich_call`.
3. Plain path: normalize the path -> `_is_windows_reserved_name` (Windows only)
   -> `is_within_workspace` (absolute paths exempt) -> `_build_rg_args`
   (`--no-config --hidden`, VCS excludes, context/line flags,
   `--max-count = offset + head_limit + 1000`, `--glob`/`--type`, mode flag,
   `--`, pattern, path; RTK prefixed as `argv[0]`).
4. `_run_rg_subprocess`: `create_subprocess_exec(..., cwd=work_dir,
   env=_env_with_shared_bin_path())`, `_read_stream` x2 with
   `RG_MAX_BUFFER=20 MB` truncation, timeout -> `_kill_process`
   (terminate -> 5 s -> kill).
5. Exit codes: neither 0 nor 1 plus `_is_eagain(stderr)` -> one retry with
   `-j 1`; anything else -> `ToolError`.
6. `_postprocess` under a second `wait_for`:
   rtk protocol lines -> newest-first mtime ordering for `files_with_matches` ->
   `_strip_path_prefix` / `_remap_display` -> sensitive-file filtering ->
   range filtering + `record_grep_files` -> summaries -> dedup (only when RTK did
   not run and output is not grouped) -> offset/head_limit pagination ->
   `fold_lines` -> `truncate_line` -> micro-compress -> `_join_with_byte_limit`.
7. `_rich_call`: parse archive candidates -> temp dir -> resolve selectors
   (workspace check, materialize members, reject globs/dirs) -> per-entry rg
   fan-out -> `_reattach_single_file_prefix` -> `_postprocess` -> cleanup.
8. `backup_grep`: `os.walk` with sorted entries, `_should_skip_dir`,
   per-file 5 MB cap, NUL-byte binary skip, `_search_content_single`,
   `_extract_path` (rg grammar first so `C:` survives on Windows).

## 4. What to review (invariants a correct `grep` result must keep)
- **RTK argv contract**: with `deduplicate_output=True` the argv is
  `[rtk, rg, ...]`; any RTK failure degrades silently to plain `rg`, and
  `deduplicate_output=False` must produce a byte-identical plain-rg argv.
- **Dedup exactly once**: local `dedup_lines` runs only when RTK did not run and
  the output is not grouped.
- **EAGAIN retry**: exactly one retry with `-j 1`; a timeout returns partial
  output with a message, and truly empty output is a `ToolError`.
- **Path safety**: Windows reserved device names rejected before spawn; path
  traversal blocked unless the user passed an absolute path.
- **Sensitive files**: filtered after prefix-strip/remap for `rg` (before it in
  the backup scanner), with the filtered paths surfaced in the message.
- **Backup-only limits**: the NUL binary skip and the 5 MB per-file cap live only
  in `backup_grep`; `_read_stream` must keep draining after the pipe cap so the
  child never blocks.
- **Byte caps**: `RG_MAX_BUFFER` truncation drops the final partial line;
  `_join_with_byte_limit` enforces the 100 KiB output cap with an explicit note.
- **Selectors**: range selectors require `output_mode="content"`, disable RTK and
  drop context outside the ranges.
- **Ordering**: `fold_lines` runs after pagination; `head_limit=0` is unlimited
  but the byte cap remains; the `+1000` `--max-count` margin must stay so
  sensitive filtering cannot starve results.
- **Bounded post-processing**: the `asyncio.wait_for` around `_postprocess` is
  load-bearing; truncate single long lines before compression to avoid O(n^2).'''
    verify = f'''- Reproduce the argv and the line grammar:
  `{ctx.python_path} -c "from kimi_cli.tools.file.grep_local import _build_rg_args, Params, parse_content_line; print(_build_rg_args('rg', Params(pattern='x', output_mode='content'))); print(parse_content_line('a.py-3-ctx'))"`.
- Replay one recorded call in a temp tree with an ignored file, a sensitive file
  and a binary file; assert the exact lines, notes and ordering.
- Add a regression case to `{ctx.cli_test('tools', 'test_grep.py')}` or
  `{ctx.cli_test('tools', 'test_grep_output.py')}`.
- Run `uv run tools/syntax_check.py <file> [<file> ...]` on every edited Python
  file, then
  `uv run pytest {ctx.cli_test('tools', 'test_grep.py')} {ctx.test('test_grep_filter.py')} -x -q`.'''
    return _PromptBody(sections=sections, verify=verify)


def _prompt_fetch_url(ctx: _ReflectionContext) -> _PromptBody:
    tool_path = ctx.tool('web', 'kimix_fetch.py')
    fetcher = ctx.tool('web', 'web_fetcher', 'fetcher.py')
    content = ctx.tool('web', 'content.py')
    micro = ctx.tool('file', 'micro_compress.py')
    common = ctx.tool('common.py')
    unused = ctx.tool('web', 'fetch.py')
    tests = ', '.join(
        str(path)
        for path in (
            ctx.test('unit', 'tools', 'test_fetchurl.py'),
            ctx.test('test_fetch_url_parity.py'),
        )
    )
    sections = f'''## 1. Source of truth — read before changing anything

Shipped fetch tool (**the manifest entry, this is the file to review**):
- `{tool_path}` — `fetch_url(CallableTool2[Params])`, `name = "fetch_url"`,
  `Params` (`url`, `output_path`).  `agent_worker.json` resolves
  `kimi_cli.tools.web.kimix_fetch:fetch_url`, i.e. this class — **not**
  `{unused}`, which declares the same tool name but is not the worker's tool.
  Threading the wrong module is a real bug class here.

Fetch pipeline:
- `{fetcher}` — `fetch_to_markdown()`: `_fetch_html` (Playwright Chromium) ->
  fallback `fetch_html_http_with_fallback` (httpx, verify -> no-verify ->
  TLS 1.2 pinned, retries with backoff) -> `_html_to_markdown` (BeautifulSoup
  boilerplate removal + `markdownify`) -> login-wall/mobile-UA retry.
- `{content}` — shared content helpers (`truncate_with_footer`).
- `{micro}` — `compress(markdown, kind="prose", ...)`: whitespace/encoding
  normalization and prefix folding, annotated and idempotent.
- `{common}` — `_maybe_export_output` (large-payload disk spill).

Tests: `{tests}` — the kimix fetch pipeline (params, HTTP path, output file) and
the cross-implementation parity check against `web/fetch.py`.

## 2. Architecture in one paragraph
A two-stage pipeline plus a presentation shell.  Stage 1 is transport:
`fetch_to_markdown` prefers a real headless browser (JS execution, desktop
viewport), falls back to plain HTTP with progressively relaxed TLS settings, then
converts the DOM to Markdown after stripping scripts/styles/media/nav/footer.
Stage 2 is model presentation inside the tool: the raw Markdown is
micro-compressed (prose mode) before it is written to the tool result, while an
explicit `output_path` always saves the *uncompressed* document.  Errors are
converted to a non-empty `ToolError` at the tool boundary, so a transport failure
never escapes as a traceback.  There is no SSRF guard or provider abstraction in
this path: those live in the unused `web/fetch.py`, which is why the manifest
must keep pointing at this module.

## 3. Workflow of one `fetch_url` tool call
1. `fetch_url.__call__` -> `await fetch_to_markdown(params.url)`.
2. `fetch_to_markdown`: `_fetch_html(url, _DESKTOP_UA, 1920x1080, "networkidle")`
   via Playwright; on any exception ->
   `fetch_html_http_with_fallback(url, _DESKTOP_UA)`.
3. HTTP fallback: `_fetch_html_http` with a browser header set, 30 s timeout,
   `follow_redirects=True`, retries only for 429/5xx/connect/timeout/TLS, then
   the three TLS configurations.
4. `_html_to_markdown`: decompose `script/style/noscript/img/video/.../nav/
   aside/footer/header`, prefer `main` (>= 500 characters) else `body`,
   `markdownify(heading_style="ATX")`, collapse 3+ newlines.
5. Login-wall/low-content detection (`< 300` non-space chars or a login pattern)
   -> retry with the mobile UA/viewport; keep the longer result.
6. `output_path`: create parents, write the raw Markdown, return
   `Content saved to <path> (<n> characters).`; a write failure returns
   `ToolError` with the Markdown as `output`.
7. Otherwise micro-compress as prose, `_maybe_export_output`, `ToolOk`.

## 4. What to review (invariants a correct `fetch_url` result must keep)
- **Manifest identity**: the tool must stay `web.kimix_fetch:fetch_url`; the
  same-named class in `web/fetch.py` is a trap, and changing which one is loaded
  changes the whole feature set (provider services, SSRF guard).
- **Browser first, HTTP second**: the Playwright path is preferred and every
  failure of it must fall back, never propagate; the HTTP path must keep its
  three TLS attempts and its 429/5xx-only retry policy.
- **No infinite retries**: exactly 3 HTTP attempts and one mobile retry; adding a
  loop here turns a dead site into a hung tool.
- **Raw vs compressed**: `output_path` writes the raw Markdown; only the
  model-facing output is compressed, and compression must stay annotated and
  idempotent.
- **Errors are model-readable**: every failure returns `ToolError` with a
  non-empty message (`str(exc) or "Failed to fetch <url>"`), never an empty
  result or a raised exception.
- **Boilerplate removal is shared**: `_html_to_markdown` must stay the single
  converter so both the desktop and mobile pass agree.
- **Login detection**: the `< 300` character threshold and the login pattern list
  are heuristics; tuning them needs a recorded page in the tests.
- **No size cap in this tool**: large pages are handled by
  `_maybe_export_output` / micro-compress; do not add a silent truncation without
  a visible marker.'''
    verify = f'''- Reproduce the converter directly:
  `{ctx.python_path} -c "from kimi_cli.tools.web.web_fetcher.fetcher import _html_to_markdown as c; print(c('<html><body><main>' + 'x'*600 + '</main></body></html>')[:40])"`.
- Replay one recorded URL against a mocked transport (httpx `MockTransport` or a
  patched `fetch_to_markdown`) and assert the returned Markdown and the
  `output_path` file content.
- Add a regression case to `{ctx.test('unit', 'tools', 'test_fetchurl.py')}` and
  keep `{ctx.test('test_fetch_url_parity.py')}` green.
- Run `uv run tools/syntax_check.py <file> [<file> ...]` on every edited Python
  file, then
  `uv run pytest {ctx.test('unit', 'tools', 'test_fetchurl.py')} {ctx.test('test_fetch_url_parity.py')} -x -q`.'''
    return _PromptBody(
        sections=sections,
        verify=verify,
        context=(
            'The shipped `fetch_url` is the Markdown fetcher from `kimix_fetch.py`; '
            '`web/fetch.py` declares the same name but is not used by '
            '`agent_worker.json`.',
        ),
    )


def _prompt_web_search(ctx: _ReflectionContext) -> _PromptBody:
    tool_path = ctx.tool('web', 'search.py')
    providers = ctx.tool('web', 'providers.py')
    desc = ctx.tool('web', 'search.md')
    tests = ', '.join(
        str(path)
        for path in (
            ctx.cli_test('tools', 'test_web_search_dispatch.py'),
            ctx.cli_test('tools', 'test_web_providers.py'),
            ctx.cli_test('tools', 'test_deepseek_web.py'),
        )
    )
    sections = f'''## 1. Source of truth — read before changing anything

Search tool (**the renderer/entry to review**):
- `{tool_path}` — `SearchWeb(CallableTool2[Params])`, `name = "web_search"`,
  `Params` (`query`, `limit` 1..20, `include_content`), `Response` /
  `SearchResult`, `__init__`, `__call__`, `__getattr__` (lazy aiohttp helper kept
  monkeypatchable).
- `{providers}` — `WebSearchProvider` ABC (`supports_search` /
  `supports_extract`), the process-global registry (`register_provider`,
  `_registry`, `_lock`, `_reset_for_tests`, `list_providers`),
  `get_active_search_provider`, `_resolve`, `_explicit_config_name`,
  `service_type`, `deepseek_endpoint_url`, `_derived_service_url`, and the
  provider implementations (Kimi, DeepSeek, Tavily, Exa, Brave, SearXNG,
  Firecrawl, Parallel, xAI, DDGS, local trafilatura).
- `{desc}` — the model-facing description.

Tests: `{tests}` — dispatch/capability selection, the registry and active
provider resolution, provider transports and the DeepSeek service flavour.

## 2. Architecture in one paragraph
`search.py` is a thin renderer: it owns the pydantic contract, the
`SkipThisTool` gate and the Hermes-shaped text layout, while every transport lives
in `providers.py` behind the `WebSearchProvider` ABC.  The registry is
process-global and populated at import time, so availability is evaluated lazily
at resolution time with cheap env checks.  Two hosted service flavours (Kimi and
DeepSeek) share one auth/transport pattern (user agent, bearer key,
`X-Msh-Tool-Call-Id`, OAuth headers) and both emit
`{{"success", "data": {{"web": [...]}}}}`; every other provider is a direct HTTP
client.  Sync and async providers are unified by an `inspect.iscoroutine` await
instead of a protocol split.  With no credentials the tool is omitted entirely
(`SkipThisTool`), never registered as a broken tool.

## 3. Workflow of one `web_search` tool call
1. `SearchWeb.__init__`: import `providers` (self-registering providers), then
   register a service provider when `config.services.search` is configured.
2. Gate: `get_active_search_provider(config) is None` -> `SkipThisTool()`.
3. `search.__call__`: re-resolve the provider (config can change between calls)
   -> `provider.search(query, limit[, include_content=True])` -> await when the
   result is a coroutine.
4. `result.get("success")` false -> `builder.error(...)` with the provider
   message; otherwise render `data["web"]` as `Title` / `Date` / `URL` /
   `Summary` blocks separated by `---`, plus content when
   `include_content=True`.
5. Kimi transport: `{{text_query, limit, enable_page_crawling, timeout_seconds}}`
   with a 180 s total budget; a single 404 retry against the derived service URL.
6. DeepSeek transport: `{{query, count, ori_question, need_content}}` with a 120 s
   budget; service order and `summary`/`snippet` mapping.

## 4. What to review (invariants a correct `web_search` result must keep)
- **No provider -> `SkipThisTool`**, never a hard error; `__init__` must not make
  a network call, and `is_available()` must stay cheap/env-only.
- **Explicit config wins over availability**: a mis-typed `web.search_backend`
  degrades to "not registered; falling back" and then the legacy preference walk,
  instead of crashing.
- **Registry hygiene**: `register_provider` requires an instance and a non-empty
  name; re-registration overwrites; tests must call `_reset_for_tests` so state
  cannot leak between files.
- **Providers never raise**: HTTP 4xx/5xx/timeouts become
  `{{"success": False, "error": ...}}`; the renderer only inspects `success`.
- **`include_content` is forwarded only when true**, so two-argument sync
  providers keep working (DDGS ignores it by design).
- **One 404 self-heal only** (`_derived_service_url`); 5xx/timeout/DNS are
  terminal and user-visible.
- **Tool-call context**: the Kimi path asserts `get_current_tool_call_or_none()`
  is not `None`; the DeepSeek path tolerates `None`.  Do not make one path
  depend on the other's context assumptions.
- **Timeouts**: Kimi 180 s total / 90 s read, DeepSeek 120 s; the
  `timeout_seconds: 30` in the Kimi payload is server-side and may be exceeded by
  crawling.
- **Ranking/description semantics differ per flavour** (Kimi renumbers
  `position: i+1`, DeepSeek trusts service order and maps `summary` or
  `snippet`); a "unification" changes user-visible ordering.
- **Output budget**: `limit <= 20`, but `include_content=True` is uncapped per
  item — keep it visible in the message rather than silently trimming.'''
    verify = f'''- Inspect provider resolution without network:
  `{ctx.python_path} -c "from kimi_cli.tools.web.providers import list_providers, get_active_search_provider; print(list_providers()); print(get_active_search_provider(None))"`.
- Replay one recorded query with a fake provider registered under the resolved
  name and assert the exact rendered block layout and the error branch.
- Add a regression case to `{ctx.cli_test('tools', 'test_web_search_dispatch.py')}`
  or `{ctx.cli_test('tools', 'test_web_providers.py')}`.
- Run `uv run tools/syntax_check.py <file> [<file> ...]` on every edited Python
  file, then
  `uv run pytest {ctx.cli_test('tools', 'test_web_search_dispatch.py')} {ctx.cli_test('tools', 'test_web_providers.py')} -x -q`.'''
    return _PromptBody(
        sections=sections,
        verify=verify,
        context=(
            'With no configured/credentialed provider the `web_search` tool is '
            'skipped entirely, so an empty invocation list can be expected.',
        ),
    )


#: Registry of the tool-reflection prompt generators, keyed by tool name.
_REFLECTION_TOOL_SPECS: dict[str, _ReflectionToolSpec] = {
    'pwsh': _ReflectionToolSpec(
        tool='pwsh',
        title='PowerShell Tool Reflection Task',
        entry='kimi_cli.tools.file.bash.pwsh_tool:Powershell',
        summary=(
            'Review the `pwsh` tool against the real calls recorded at the end of this\n'
            'document: the safety gates, the PS7 -> 5.1 downgrade and the PowerShell\n'
            'translation either run the command the model meant, or they refuse it\n'
            'cleanly.  A command that silently changes meaning is a bug in the tool.'
        ),
        body=_prompt_pwsh,
        slug='reflection_pwsh',
        extras=_REFLECTION_EXTRAS['pwsh'],
    ),
    'python': _ReflectionToolSpec(
        tool='python',
        title='Python Tool Reflection Task',
        entry='kimi_cli.tools.py:python',
        summary=(
            'Review the `python` tool against the real runs recorded at the end of\n'
            'this document: interpreter resolution, environment scrubbing, the\n'
            'syntax pre-check and result shaping either run the code the model wrote\n'
            'in the interpreter the project expects, or they explain why not.'
        ),
        body=_prompt_python,
        slug='reflection_python',
        extras=_REFLECTION_EXTRAS['python'],
    ),
    'job_output': _ReflectionToolSpec(
        tool='job_output',
        title='Job Output Tool Reflection Task',
        entry='kimi_cli.tools.background:TaskOutput',
        summary=(
            'Review the `job_output` tool against the real reads recorded at the end\n'
            'of this document: every background job must stay retrievable exactly\n'
            'once, with a non-empty message and the owning tool\'s own formatting.'
        ),
        body=_prompt_job_output,
        slug='reflection_job_output',
        extras=_REFLECTION_EXTRAS['job_output'],
    ),
    'todo_list': _ReflectionToolSpec(
        tool='todo_list',
        title='Todo List Tool Reflection Task',
        entry='kimi_cli.tools.todo:TodoList',
        summary=(
            'Review the `todo_list` tool against the real updates recorded at the end\n'
            'of this document: merge/replace/clear must converge on one saved tree,\n'
            'keep exactly one `in_progress`, and explain every rejected item.'
        ),
        body=_prompt_todo_list,
        slug='reflection_todo_list',
        extras=_REFLECTION_EXTRAS['todo_list'],
    ),
    'read': _ReflectionToolSpec(
        tool='read',
        title='Read Tool Reflection Task',
        entry='kimi_cli.tools.file.read:ReadFile',
        summary=(
            'Review the `read` tool against the real reads recorded at the end of this\n'
            'document: path/sensitivity gates, rich-format dispatch and the\n'
            'line/byte/character windows must produce exactly the text and the\n'
            'truncation notices the model sees.'
        ),
        body=_prompt_read,
        slug='reflection_read',
        extras=_REFLECTION_EXTRAS['read'],
    ),
    'read_image': _ReflectionToolSpec(
        tool='read_image',
        title='Read Image Tool Reflection Task',
        entry='kimi_cli.tools.file.read_media:ReadMediaFile',
        summary=(
            'Review the `read_image` tool against the real calls recorded at the end of\n'
            'this document: capability gates, crop/downsample/full-resolution delivery\n'
            'and the model-facing note must never lie about what was sent.'
        ),
        body=_prompt_read_image,
        slug='reflection_read_image',
        extras=_REFLECTION_EXTRAS['read_image'],
    ),
    'write': _ReflectionToolSpec(
        tool='write',
        title='Write Tool Reflection Task',
        entry='kimi_cli.tools.file.write:WriteFile',
        summary=(
            'Review the `write` tool against the real writes recorded at the end of\n'
            'this document: the guard order (path -> auto-generated -> approval ->\n'
            'conflict -> write -> verification) must stay intact, and no refused\n'
            'write may touch the filesystem.'
        ),
        body=_prompt_write,
        slug='reflection_write',
        extras=_REFLECTION_EXTRAS['write'],
    ),
    'edit': _ReflectionToolSpec(
        tool='edit',
        title='Edit Tool Reflection Task',
        entry='kimi_cli.tools.file.edit:EditFile',
        summary=(
            'Review the `edit` tool against the real edits recorded at the end of this\n'
            'document: mode resolution, the literal -> strip -> fuzzy ladder and the\n'
            'shared guard pipeline must apply the intended replacement or fail with an\n'
            'actionable suggestion.'
        ),
        body=_prompt_edit,
        slug='reflection_edit',
        extras=_REFLECTION_EXTRAS['edit'],
    ),
    'subagent': _ReflectionToolSpec(
        tool='subagent',
        title='Subagent Tool Reflection Task',
        entry='kimi_cli.tools.subagent:Agent',
        summary=(
            'Review the `subagent` tool against the real runs recorded at the end of\n'
            'this document: session creation/reuse, the one-run-per-session claim,\n'
            'batch rollback and child teardown must never leak or double-drive a\n'
            'session.'
        ),
        body=_prompt_subagent,
        slug='reflection_subagent',
        extras=_REFLECTION_EXTRAS['subagent'],
    ),
    'list_agents': _ReflectionToolSpec(
        tool='list_agents',
        title='List Agents Tool Reflection Task',
        entry='kimi_cli.tools.subagent:AgentList',
        summary=(
            'Review the `list_agents` tool against the real listings recorded at the\n'
            'end of this document: the view must expose exactly the resumable agent\n'
            'sessions, with stable JSON and no side effects.'
        ),
        body=_prompt_list_agents,
        slug='reflection_list_agents',
    ),
    'glob': _ReflectionToolSpec(
        tool='glob',
        title='Glob Tool Reflection Task',
        entry='kimi_cli.tools.file.glob:Glob',
        summary=(
            'Review the `glob` tool against the real searches recorded at the end of\n'
            'this document: pattern safety, `.gitignore` semantics (negation and\n'
            'ancestors), cache freshness and the fold/byte budgets decide which files\n'
            'the model ever sees.'
        ),
        body=_prompt_glob,
        slug='reflection_glob',
        extras=_REFLECTION_EXTRAS['glob'],
    ),
    'grep': _ReflectionToolSpec(
        tool='grep',
        title='Grep Tool Reflection Task',
        entry='kimi_cli.tools.file.grep_local:Grep',
        summary=(
            'Review the `grep` tool against the real searches recorded at the end of\n'
            'this document: backend selection (rtk -> rg -> backup), line identity,\n'
            'sensitive filtering, selectors and the byte budgets decide whether the\n'
            'model sees the match that exists.'
        ),
        body=_prompt_grep,
        slug='reflection_grep',
        extras=_REFLECTION_EXTRAS['grep'],
    ),
    'fetch_url': _ReflectionToolSpec(
        tool='fetch_url',
        title='Fetch URL Tool Reflection Task',
        entry='kimi_cli.tools.web.kimix_fetch:fetch_url',
        summary=(
            'Review the `fetch_url` tool against the real fetches recorded at the end\n'
            'of this document: browser-first transport with an HTTP/TLS fallback,\n'
            'DOM -> Markdown conversion and the raw-vs-compressed split must return\n'
            'the page the model asked for, or a readable error.'
        ),
        body=_prompt_fetch_url,
        slug='reflection_fetch_url',
        extras=_REFLECTION_EXTRAS['fetch_url'],
    ),
    'web_search': _ReflectionToolSpec(
        tool='web_search',
        title='Web Search Tool Reflection Task',
        entry='kimi_cli.tools.web.search:SearchWeb',
        summary=(
            'Review the `web_search` tool against the real searches recorded at the end\n'
            'of this document: provider resolution, the never-raise provider contract\n'
            'and the Hermes-shaped rendering must return the results the service\n'
            'actually produced.'
        ),
        body=_prompt_web_search,
        slug='reflection_web_search',
    ),
}

#: Slash-command implementations, built once at import time.
_REFLECTION_COMMANDS: dict[str, Any] = {
    spec.command: _make_reflection_command(spec) for spec in _REFLECTION_TOOL_SPECS.values()
}


def _cmd_reflection(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    """Reflect on the current context and refactor the agent source code."""
    session = get_default_session()
    if session is None:
        print_error('No active session. Start a conversation first.')
        return None, False
    if not _context_is_non_empty(session):
        print_error('Context is empty. /reflection requires a non-empty context.')
        return None, False
    prompt_str = _build_reflection_prompt(session)
    print_info(prompt_str)
    try:
        prompt(prompt_str=prompt_str, session=session, format_output=True)
    except Exception as e:
        print_error(f'Reflection failed: {e}')
    return None, False


def _cmd_continue(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    """Resume the session's unfinished work by prompting the LLM to continue."""
    session = get_default_session()
    if session is None:
        print_error('No active session. Start a conversation first.')
        return None, False
    if not _context_is_non_empty(session):
        print_error('Context is empty. /continue requires a non-empty context.')
        return None, False
    if _session_is_done(session):
        print_info('Session is already marked as done. Nothing to continue.')
        return None, False
    try:
        prompt(prompt_str=_CONTINUE_PROMPT, session=session, format_output=True)
    except Exception as e:
        print_error(f'Continue failed: {e}')
    return None, False


def _cmd_unknown(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    print_warning('Unrecognized command.')
    return None, False


_command_map = {
    'help': _cmd_help,
    'clear': _cmd_clear,
    'exit': _cmd_exit,
    'context': _cmd_context,
    'cmd': _cmd_cmd,
    'fix': _cmd_fix,
    'txt': _cmd_txt,
    'file': _cmd_file,
    'plan': _cmd_plan,
    'compact': _cmd_compact,
    'continue': _cmd_continue,
    'export': _cmd_export,
    'resume': _cmd_resume,
    'store': _cmd_store,
    'load': _cmd_load,
    'sessions': _cmd_sessions,
    'reflection': _cmd_reflection,
    'reflection_bash': _cmd_reflection_bash,
    'supervisor': _cmd_supervisor,
    'swarm': _cmd_swarm,
    'init': _cmd_init,
    'todo': _cmd_todo,
    'code': _cmd_code
}
# The generic /reflection_<tool>:<path> commands are generated from the spec table
# so every builtin tool shares one implementation and one document skeleton.
_command_map.update(_REFLECTION_COMMANDS)
_command_map_keys = set(_command_map.keys())

# Argument-type categories used by the readline Tab completer in utils.py.
_command_arg_types: dict[str, str] = {
    "file": "file",
    "todo": "file",
    "export": "file",
    "plan": "file",
    "swarm": "swarm",
    "code": "file",
    "reflection_bash": "file",
}
_command_arg_types.update({name: "file" for name in _REFLECTION_COMMANDS})
