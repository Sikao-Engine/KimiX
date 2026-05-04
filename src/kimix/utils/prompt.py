from string import Template
from typing import Any, Callable, Optional
import asyncio
import json
import hashlib
from pathlib import Path
from kimi_agent_sdk import Session
import kimix.base as base
from kimix.utils.system_prompt import SystemPromptType
from kimix.base import print_debug, print_warning, print_error, print_agent_json, print_info
from . import _globals
from .session import close_session_async, _create_default_session, _print_usage, clear_default_context, create_session, close_session
from kimix.tools.common import _export_to_temp_file
from kimi_cli.safety_check import sanitize_for_tokenizer


class PlanLoader:
    def __init__(self, file_path: str | Path) -> None:
        self.file_path = Path(file_path)
        self.steps_count: int = 0
        self.plan_file_path: str = ""
        self.plan_file_hash: str = ""
        self.finished_step_count: int = 0

    def load(self) -> None:
        if self.file_path.exists():
            with open(self.file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.steps_count = data.get('steps_count', 0)
            self.plan_file_path = data.get('plan_file_path', '')
            self.plan_file_hash = data.get('plan_file_hash', '')
            self.finished_step_count = data.get('finished_step_count', 0)

    def delete(self) -> None:
        import os
        if self.file_path.exists():
            try:
                os.unlink(self.file_path)
            except:
                pass

    def store(self) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            'steps_count': self.steps_count,
            'plan_file_path': self.plan_file_path,
            'plan_file_hash': self.plan_file_hash,
            'finished_step_count': self.finished_step_count,
        }
        with open(self.file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)

    @staticmethod
    def compute_file_hash(file_path: str | Path) -> str:
        path = Path(file_path)
        if not path.exists():
            return ""
        with open(path, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()

    @staticmethod
    def compute_hash(content: str) -> str:
        return hashlib.sha256(content.encode('utf-8', 'replace')).hexdigest()


def check_plan_cache(ask_if_use_cache: Callable[[str], bool] | None = None) -> tuple[bool, PlanLoader | None]:
    cache_file = Path.home() / '.kimi' / 'plan' / '.cache.json'
    plan_loader: PlanLoader | None = None
    use_cache = False

    if ask_if_use_cache is not None and cache_file.exists():
        plan_loader = PlanLoader(cache_file)
        plan_loader.load()
        cached_plan_path = Path(plan_loader.plan_file_path)
        if cached_plan_path.exists():
            current_hash = PlanLoader.compute_file_hash(cached_plan_path)
            if current_hash == plan_loader.plan_file_hash and plan_loader.finished_step_count > 0 and plan_loader.finished_step_count < plan_loader.steps_count:
                use_cache = ask_if_use_cache(str(cached_plan_path))
                if use_cache:
                    print_debug(
                        f'Using cache, jumping to step {plan_loader.finished_step_count}.')
    return use_cache, plan_loader


async def prompt_async(
    prompt_str: str,
    session: Session | None = None,
    # settings
    read_agents_md: bool = False,
    skill_name: str | None = None,
    output_function: Callable[[str, bool], Any] | None = None,
    info_print: bool = True,
    cancel_callable: Callable[[], bool] | None = None,
    close_session_after_prompt: bool = False,
    merge_wire_messages: bool = False
) -> None:
    if session is None:
        session = _create_default_session()
        close_session_after_prompt = False
    prompt_str = prompt_str.strip()
    prompt_str = sanitize_for_tokenizer(prompt_str)
    if len(prompt_str) > 65536:  # too long, save to file
        name, new_id = _export_to_temp_file(content=prompt_str)
        prompt_str = f'read and execute: `{name}`'
    try:
        def enable_skill(skill_name: str) -> None:
            nonlocal prompt_str
            if not base._default_skill_dirs:
                print_warning('Skill dir not setted.')
            else:
                skill_found = False
                for skill_dir in base._default_skill_dirs:
                    if (Path(str(skill_dir)) / Path(skill_name) / 'SKILL.md').exists():
                        skill_found = True
                        break
                if not skill_found:
                    print_warning(f'Skill {skill_name} not found.')
                else:
                    prompt_str = f'Use skill:{skill_name}.\n' + prompt_str
        if skill_name:
            enable_skill(skill_name)
        if session.status.context_usage < 1e-4 and read_agents_md and Path('AGENTS.md').exists():
            prompt_str = f'Read AGENTS.md.\n' + prompt_str

        if info_print:
            print_debug(f'Start...', end='\n')

        max_retries = 5
        for attempt in range(max_retries):
            # already canceled.
            if session._cancel_event is not None and session._cancel_event.is_set():
                break
            try:
                import time
                start_time = time.time()
                base.PRINT_STREAM.think = False
                if output_function is not None:
                    merge_wire_messages = True
                async for message in session.prompt(prompt_str, merge_wire_messages=merge_wire_messages):
                    if cancel_callable is not None and cancel_callable():
                        session.cancel()
                        break
                    print_agent_json(message, output_function)
                print()
                if info_print:
                    end_time = time.time()
                    _print_usage(session, end_time - start_time)
                break
            except KeyboardInterrupt as e:
                if session:
                    session.cancel()
            except Exception as e:
                print_error(str(e))
                if session:
                    session.cancel()
                if "429" in str(e) or "400" in str(e) or "500" in str(e) or "502" in str(e) or "503" in str(e):
                    wait_time = min(2 ** attempt, 60)
                    print_warning(f"Rate limited. Waiting {wait_time}s...")
                    await asyncio.sleep(wait_time)
                elif attempt == max_retries - 1:
                    raise
                else:
                    await asyncio.sleep(1)
    finally:
        if close_session_after_prompt and session:
            await close_session_async(session)


def prompt(
    prompt_str: str,
    session: Session | None = None,
    # settings
    read_agents_md: bool = False,
    skill_name: str | None = None,
    output_function: Callable[[str, bool], Any] | None = None,
    info_print: bool = True,
    cancel_callable: Callable[[], bool] | None = None,
    close_session_after_prompt: bool = False,
    merge_wire_messages: bool = False
) -> None:
    asyncio.run(
        prompt_async(
            prompt_str,
            session,
            # settings
            read_agents_md,
            skill_name,
            output_function,
            info_print,
            cancel_callable,
            close_session_after_prompt,
            merge_wire_messages=merge_wire_messages
        ))


def _make_new_plan_file() -> Path:
    import uuid
    return Path.home() / '.kimi' / 'plan' / Path('plan_' + str(uuid.uuid1()).replace('-', '') + '.md')


def execute_plan(prompt_str: str, ask_if_use_cache: Callable[[str], bool] | None = None, ask_if_execute_plan: Callable[[list[str], int], bool] | None = None, plan_loader: PlanLoader | None = None) -> None:
    import os
    from kimix.tools.note import read_file
    use_cache = False
    if plan_loader is not None:
        use_cache = True
    elif ask_if_use_cache is not None:
        use_cache, plan_loader = check_plan_cache(ask_if_use_cache)

    if not use_cache:
        # Step 1: generate plan
        plan_file = _make_new_plan_file()
        try:
            os.unlink(plan_file)
        except:
            pass
        if plan_file.exists():
            print_error(f'plan file {plan_file} already exists. quit.')
            return
        task_finished = False
        plan_session: Session | None = None
        try:
            plan_session = create_session(agent_file='agent_boss.yaml', agent_type=SystemPromptType.TodoMaker)
            custom_data = plan_session.get_custom_data()
            if custom_data is not None:
                custom_data['note_writing_path'] = plan_file
                custom_data['note_called'] = False
            for i in range(4):
                prompt(prompt_str, session=plan_session)
                if not (custom_data is not None and custom_data.get('note_called', False)):
                    print_warning(
                        f'Prompt did not write the proper plan. let it try again({i + 1}/4).')
                else:
                    task_finished = True
                    break
            if not task_finished:
                print_error(
                    'Execute plan failed, the plan file cannot generated.')
                return
        finally:
            if plan_session:
                _cd = plan_session.get_custom_data()
                if _cd is not None:
                    _cd.pop('note_writing_path', None)
                    _cd.pop('note_called', None)
                close_session(plan_session)
        steps = read_file(plan_file)
        if plan_loader is None:
            plan_loader = PlanLoader(Path.home() / '.kimi' / 'plan' / '.cache.json')
        plan_loader.steps_count = len(steps)
        plan_loader.plan_file_path = str(plan_file)
        plan_loader.plan_file_hash = PlanLoader.compute_file_hash(
            plan_file)
        plan_loader.finished_step_count = 0
        plan_loader.store()
    else:
        assert plan_loader is not None
        plan_file = Path(plan_loader.plan_file_path)
        steps = read_file(plan_file)

    if not steps:
        print_warning('No plan made, quit.')
        return

    # Step 2: execute plan
    start_idx = plan_loader.finished_step_count if plan_loader is not None else 0
    if (ask_if_execute_plan is not None) and (not ask_if_execute_plan(steps, start_idx)):
        print_warning('plan quit')
        return
    clear_default_context()
    for idx in range(start_idx, len(steps)):
        print_info(f'Executing step {idx}.')
        step = steps[idx]
        prompt(f'''Implement:

{step}

After done, run `SetTodoList` to record.
''')
        if idx != len(steps) - 1:  # not last
            if plan_loader is not None:
                plan_loader.finished_step_count = idx + 1
                plan_loader.store()
    if plan_loader is not None:
        plan_loader.delete()


def prompt_path(path: Path, split_word: Optional[str] = None, session: Session | None = None, after_prompt_coro: Any = None) -> None:
    try:
        with open(path, 'r', encoding='utf-8') as f:
            s = f.read()
    except:
        print_error(f'File {str(path)} not found.')
    coro = None
    if after_prompt_coro is not None:
        coro = after_prompt_coro()
    if split_word:
        words = s.strip().split(split_word)
        for i in words:
            prompt(i, session=session)
            if coro is not None:
                try:
                    coro.next()
                except StopIteration as e:
                    coro = None
    else:
        prompt(s, session=session)
        if coro is not None:
            try:
                coro.next()
            except StopIteration as e:
                coro = None
