from typing import Any
import os
from pathlib import Path

import kimix.base as base
from . import constants
from .utils import _input, _split_text
from kimix.base import print_success, print_error, print_warning, print_debug, colorful_text, Color
from kimix.utils import (
    clear_default_context, get_default_session, fix_error, compact_default_context,
    print_usage, execute_plan, check_plan_cache
)
from kimix.dag.agent_swarm import create_swarm_session
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


def _cmd_summarize(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    import asyncio
    from kimix.summarize import summarize
    asyncio.run(summarize())
    return None, False


def _cmd_exit(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    print_success('bye!')
    return None, True


def _cmd_context(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    print_usage()
    return None, False


def _cmd_script(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    print('\n>>>> Start input multiple-lines, end with /end')
    text_lines: list[str] = []
    while True:
        s = _input('', text_arr)
        if s.strip() == '/end':
            break
        text_lines.append(s)
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
    if len(task_split) >= 2:
        file_name_str = ':'.join(task_split[1:])
        file_path = Path(file_name_str)
        if not file_path.is_file():
            print_error(f'file not found: {file_path}')
            return None, False
        prompt_str = file_path.read_text(encoding='utf-8', errors='replace')
        execute_plan(prompt_str)
        return None, False

    def _ask_if_use_cache(path: str) -> bool:
        v = input(f'found cache `{path}`, load it and continue? (y/n) ')
        if v.strip().lower() == 'y':
            return True
        return False

    def ask_if_execute(steps: list[str], start_index: int) -> bool:
        print('Plan steps:\n' + ('\n' + '=' *
              40 + '\n').join(steps[start_index:]))
        if not ask_plan:
            return True
        print_warning('execute the plan? (y/n)')
        return input().strip().lower() == 'y'

    use_cache, plan_loader = check_plan_cache(_ask_if_use_cache)
    if use_cache:
        ask_plan = input(
            'Ask after make plan? no for auto accept-all. (y/n)').strip().lower() == 'y'
        execute_plan('', ask_if_use_cache=None,
                     ask_if_execute_plan=ask_if_execute, plan_loader=plan_loader)
        return None, False

    print(
        f'\n>>>> Make a task-list: input multiple-lines, end with {colorful_text('/end', Color.YELLOW)}, cancel with {colorful_text('/cancel', Color.YELLOW)}')
    text: list[str] = []
    while True:
        s = _input('', text_arr)
        if s.strip() == '/end':
            break
        if s.strip() == '/cancel':
            text.clear()
            break
        text.append(s)
    prompt_str = '\n'.join(text)

    ask_plan = input(
        'Ask after make plan? no for auto accept-all. (y/n)').strip().lower() == 'y'

    if prompt_str.strip():
        execute_plan(prompt_str, ask_if_use_cache=None,
                     ask_if_execute_plan=ask_if_execute)
    return None, False


def _cmd_txt(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    print(
        f'\n>>>> Start input multiple-lines, end with {colorful_text('/end', Color.YELLOW)}, cancel with {colorful_text('/cancel', Color.YELLOW)}')
    text: list[str] = []
    while True:
        s = _input('', text_arr)
        if s.strip() == '/end':
            break
        if s.strip() == '/cancel':
            text.clear()
            break
        text.append(s)
    for i in _split_text(text):
        text_arr.append(i)
    return None, False


def _cmd_swarm(task_split: list[str], text_arr: list[str]) -> tuple[None, bool]:
    """Execute swarm command: input multiple lines as task prompt, call create_swarm_session,
    then execute the returned DAG instance."""
    print(
        f'\n>>>> Start input multiple-lines for swarm task, end with {colorful_text("/end", Color.YELLOW)}, '
        f'cancel with {colorful_text("/cancel", Color.YELLOW)}')
    text: list[str] = []
    while True:
        s = _input('', text_arr)
        if s.strip() == '/end':
            break
        if s.strip() == '/cancel':
            print_warning('Swarm command cancelled.')
            return None, False
        text.append(s)
    task_prompt = '\n'.join(text)
    if not task_prompt.strip():
        print_warning('Empty task prompt, skipping swarm command.')
        return None, False

    print_debug('Creating swarm session...')
    try:
        dag = asyncio.run(create_swarm_session(task_prompt))
    except Exception as e:
        print_error(f'Failed to create swarm session: {e}')
        return None, False

    if dag is None:
        print_warning('Warning: create_swarm_session returned None, skipping execution.')
        return None, False

    print_debug(f'Swarm session created, DAG has {len(dag)} node(s).')

    print_debug('Executing DAG...')
    try:
        executor = Executor()
        results = executor.execute(dag)
        print_success(f'Swarm execution completed. Results: {results}')
    except Exception as e:
        print_error(f'Swarm execution failed: {e}')

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
        print_warning('Ralph loop reset to default.')
        base._default_ralph = None
        return None, False
    val = task_split[1].strip().lower()
    session = get_default_session()
    if val == 'on':
        base._default_ralph = True
        if session:
            session._cli._runtime.config.loop_control.max_ralph_iterations = -1
        print_success('Ralph mode ON.')
    elif val == 'off':
        base._default_ralph = False
        if session:
            session._cli._runtime.config.loop_control.max_ralph_iterations = 0
        print_success('Ralph mode OFF.')
    else:
        print_error('Command must be /ralph:on or /ralph:off')
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
    'swarm': _cmd_swarm,
    'ralph': _cmd_ralph
}
