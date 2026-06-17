from typing import Any
import os
from pathlib import Path

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
        s = _input('', text_arr, multi_line_mode=True)
        if s.strip() == '/end':
            break
        if allow_cancel and s.strip() == '/cancel':
            return [], True
        lines.append(s)
    return lines, False

from kimix.base import print_success, print_error, print_warning, print_info, print_debug, colorful_text, Color
from kimix.utils import (
    clear_default_context, get_default_session, fix_error, compact_default_context,
    print_usage, set_ralph_loop,
    _create_default_session, close_session, create_session, create_supervisor_session,
    SystemPromptType,
    prompt_plan, prompt,
)
import kimix.utils._globals as _globals
from .init import init
from kimix.dag import Executor
import asyncio


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


def _cmd_rename(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    if len(task_split) < 2:
        print_error('Command must be /rename:session_id')
        return None, False
    new_session_id = ':'.join(task_split[1:])
    session = get_default_session()
    if session is None:
        print_error('No active session to rename.')
        return None, False
    try:
        asyncio.run(session.rename(new_session_id))
        print_success(f'Session renamed to {new_session_id}')
    except Exception as e:
        import traceback
        print_error(f'Rename failed: {e}')
        print_error(traceback.format_exc())
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
        prompt(prompt_str=task_prompt, session=supervisor_session)
    except Exception as e:
        print_error(f'Supervisor prompt failed: {e}')
    finally:
        close_session(supervisor_session)

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

    import re
    from kimix.parser import (
        PythonParser, CParser, ShellParser, HtmlParser, PascalParser, LispParser, SqlParser
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
        prompt(prompt_str=prompt_str)
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
    'rename': _cmd_rename,
    'ralph': _cmd_ralph,
    'cot': _cmd_cot,
    'supervisor': _cmd_supervisor,
    'init': _cmd_init,
    'todo': _cmd_todo
}
_command_map_keys = set(_command_map.keys())
