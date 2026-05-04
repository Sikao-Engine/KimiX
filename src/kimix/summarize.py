from string import Template
from kimix.base import print_warning
from kimi_agent_sdk import Session
from kimix.utils import *
import kimix.base as base


async def summarize(temp_file: str | None = None, session: Session | None = None, only_return_remember_str: bool = False) -> str | None:
    from pathlib import Path
    from kimix.utils import prompt_async, get_default_session
    from kimix.base import percentage_str, print_success
    from kimix.tools.common import _create_temp_file_name
    if session is None:
        session = get_default_session()
    if not session or session.status.context_usage <= 1e-5:
        print_warning('Context is empty.')
        return None
    if temp_file is None:
        temp_file = _create_temp_file_name()
    try:
        Path(temp_file).unlink(missing_ok=True)
    except:
        pass
    last_usage = session.status.context_usage
    from kimix.base import generate_memory
    lines = []

    def export_func(text: str, is_thinking: bool):
        if not is_thinking:
            lines.append(text)
    await prompt_async(generate_memory, session=session, info_print=False, output_function=export_func, merge_wire_messages=True)
    if lines:
        memory_content = '\n'.join(lines)
        if only_return_remember_str:
            memory_content = f'Remember this:\n```\n{memory_content}\n```\n'
            return memory_content
        system_prompts = get_system_prompt(False, base._default_yolo, '.', f'Memory:\n\n{memory_content}', SystemPromptType.Worker)
        await session.clear(custom_system_prompt=system_prompts)
    else:
        print_warning('No memory generated.')
        return None
    new_usage = session.status.context_usage
    print_success(
        f'Compact from {percentage_str(last_usage)} to {percentage_str(new_usage)}')
    return None

summarize_mistakes_prompt = Template('''Summarize these tool call errors concisely:
$errors
Output:
1. **Patterns**: common error types and causes
2. **Root Causes**: why they happen
3. **Fixes**: how to avoid them
4. **Key Takeaways**: brief lessons''')


def summarize_mistake(result_file: str, session=None) -> None:
    errors = get_tool_call_errors(session)
    if not errors:
        print_warning('No errors.')
        return
    from kimix.utils import prompt
    from kimix.tools.common import _maybe_export_output
    prompt(_maybe_export_output(summarize_mistakes_prompt.substitute(
        errors='\n'.join(str(e) for e in errors),
        result_file=result_file
    )), session=session, info_print=False)
