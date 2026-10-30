import importlib
import inspect
import os
import sys
import uuid
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
