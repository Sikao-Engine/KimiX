import os
import uuid
from pathlib import Path
import pendulum

import kimix.base as base

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

import asyncio

import kimix.utils._globals as _globals
from kimix.base import (
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
    compact_default_context,
    create_session,
    create_supervisor_session,
    fix_error,
    get_default_session,
    print_usage,
    prompt,
    prompt_plan,
    set_ralph_loop,
)

from .init import init


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
    import asyncio
    session = get_default_session()
    if session is None:
        print_error('No active session to export.')
        return None, False
    if len(task_split) < 2:
        print_error('Command must be /export:file')
        return None, False
    output_path = ':'.join(task_split[1:]) if len(task_split) > 1 else None
    try:
        output, count = asyncio.run(session.export(output_path=output_path))
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
        target = asyncio.run(_do_copy())
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
        new_id = asyncio.run(_do_copy())
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
    except Exception as e:
        import traceback
        print_error(f'Loaded session but failed to resume copy: {e}')
        print_error(traceback.format_exc())
        return None, False

    print_success(f'Loaded session {source_id} into anonymous session {new_id}')
    return None, False


def _cmd_sessions(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    from kaos.path import KaosPath
    from kimi_cli.session import Session as CliSession

    session = get_default_session()
    current_id = None
    if session is None:
        work_dir = KaosPath('.')
    else:
        cli_session = session._cli.session
        work_dir = cli_session.work_dir
        current_id = cli_session.id

    try:
        sessions = asyncio.run(CliSession.list(work_dir))
    except Exception as e:
        print_error(f'Failed to list sessions: {e}')
        return None, False

    if not sessions:
        print_warning('No sessions found.')
        return None, False

    id_width = max(len('session id'), *(len(item.id) for item in sessions))
    print_info(f'{" ":1}  {"session id":<{id_width}}  {"updated at":<19}  title')
    for item in sessions:
        marker = '*' if item.id == current_id else ' '
        updated_at = pendulum.from_timestamp(item.updated_at).strftime('%Y-%m-%d %H:%M:%S')
        print(f'{marker}  {item.id:<{id_width}}  {updated_at}  {item.title}')
    return None, False


def _cmd_summarize(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    import asyncio

    from kimix.summarize import summarize
    asyncio.run(summarize())
    return None, False


def _cmd_exit(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    session = get_default_session()
    if session:
        close_session(session)
    _globals._default_session = None
    _globals._default_role = None
    print_success('bye!')
    return None, True


def _cmd_context(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    print_usage()
    return None, False


def _cmd_script(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    print('\n>>>> Start input multiple-lines, end with /end')
    text_lines, _ = _read_multi_line(text_arr, allow_cancel=False)
    text = '\n'.join(text_lines)
    try:
        exec(text, constants.globals_dict, constants.locals_dict)
        print_success('Done.')
    except Exception as e:
        print_error(str(e))
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


def _cmd_cd(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    if len(task_split) < 2:
        print_error('Command must be /cd:PATH')
        return None, False
    path = ':'.join(task_split[1:])
    try:
        os.chdir(path)
        base._default_skill_dirs = []
        if get_default_session():
            clear_default_context(True, True)
        print_success(f'Changed directory to: {Path(".").resolve()}')
    except Exception as e:
        print_error(f'Failed to change directory: {e}')
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


def _cmd_ralph(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    if len(task_split) < 2:
        print_error(f'command format error, must be /ralph:path')
        return None, False
    val = task_split[1].strip().lower()
    session = get_default_session()
    if val == 'on':
        set_ralph_loop(1)
        print_success(f'Ralph mode set to 1.')
    elif val == 'off':
        base._default_ralph = None
        set_ralph_loop(0)
        print_success(f'Ralph mode set to default.')
    else:
        try:
            num = int(val)
            set_ralph_loop(num)
            print_success(f'Ralph mode set to {num}.')
        except ValueError:
            print_error('Command must be /ralph:on, /ralph:off, /ralph:<num>')
    return None, False


def _cmd_cot(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    if len(task_split) < 2:
        print_error('Command must be /cot:on or /cot:off')
        return None, False
    val = task_split[1].strip().lower()
    if val == 'on':
        base.set_default_manually_cot(True)
        print_success('Manually CoT mode ON.')
    elif val == 'off':
        base.set_default_manually_cot(False)
        print_success('Manually CoT mode OFF.')
    else:
        print_error('Command must be /cot:on or /cot:off')
    return None, False


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
        )
    else:
        format_todo = lambda i, todo: f'{i}. Line {todo.line}: {todo.content.strip()}'
        todo_lines = [format_todo(i, todo) for i, todo in enumerate(todos, 1)]
        todo_items = '\n'.join(todo_lines)
        prompt_str = (
            f'Implement all TODOs in {file_path} at once:\n\n'
            f'{todo_items}\n\n'
            'Make sure to handle each TODO completely.'
        )

    try:
        print_info(prompt_str)
        prompt(prompt_str=prompt_str, format_output=True)
    except Exception as e:
        print_error(f'Prompt failed: {e}')

    return None, False


def _cmd_unknown(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    print_warning('Unrecognized command.')
    return None, False


_command_map = {
    'help': _cmd_help,
    'clear': _cmd_clear,
    'summarize': _cmd_summarize,
    'exit': _cmd_exit,
    'context': _cmd_context,
    'script': _cmd_script,
    'cmd': _cmd_cmd,
    'cd': _cmd_cd,
    'fix': _cmd_fix,
    'txt': _cmd_txt,
    'file': _cmd_file,
    'plan': _cmd_plan,
    'compact': _cmd_compact,
    'export': _cmd_export,
    'resume': _cmd_resume,
    'store': _cmd_store,
    'load': _cmd_load,
    'sessions': _cmd_sessions,
    'ralph': _cmd_ralph,
    'cot': _cmd_cot,
    'supervisor': _cmd_supervisor,
    'swarm': _cmd_swarm,
    'init': _cmd_init,
    'todo': _cmd_todo
}
_command_map_keys = set(_command_map.keys())

# Argument-type categories used by the readline Tab completer in utils.py.
_command_arg_types: dict[str, str] = {
    "cd": "dir",
    "file": "file",
    "todo": "file",
    "export": "file",
    "plan": "file",
    "ralph": "ralph",
    "cot": "bool_on_off",
    "swarm": "swarm",
}
