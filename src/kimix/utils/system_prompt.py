from typing import Optional, Callable, Any
from pathlib import Path
import os
import orjson
from enum import Enum
from kaos.path import KaosPath
import kimix.base as base
from kimi_cli.soul.agent import BuiltinSystemPromptArgs
from kimi_cli.soul.agent import Runtime
from kimi_cli.tools.reason import ToolCallReason
from kimi_cli.utils.tokens import count_tokens

# Prompt string templates. Multi-line item lists use one bullet per line.
_TEMPLATES: dict[str, str] = {
    'sp_template': '{TOOL_CONVENTIONS}{AGENT_ROLE}:\n{NUMBERED}\n{AGENTS_MD}{SKILLS}',
    'sp_tool_conventions': '''\
# Tools
- Long outputs fold: keep first/last N lines; middle truncated.
- Outputs are deduped and token-filtered.
- Use `rtk <tool> <args>` for known CLI tools (dedupes and truncates).
- `wait_for_pattern` blocks up to `timeout` seconds.
- `cwd`/`workdir` sets the working directory.
''',
    'sp_base_items': 'Parallel tool calls.\nOS: {KIMI_OS} WORK DIR: {KIMI_WORK_DIR}\n',
    'sp_windows_item': 'Windows paths use `\\`, never `/`.\n',
    'sp_worker_core': '''\
Read refs/skills/files first; act on evidence.
Persist until done; one action per turn.
Use `python` for long commands, not `{shell_tool}`.
On error: retry, adjust, or decompose.
Verify with tests/checks — never declare done from reading alone.
''',
    'sp_worker_optional': '{YOLO}\n{SUBAGENT}\n{TRIVIAL}\n',
    'sp_thinker_items': '''\
Think in <thinking>...</thinking>; end with <quit/>. No text outside tags.
Self-verify: catch errors and bad assumptions.
''',
    'sp_todomaker_items': '''\
Plan only; never implement.
Record the plan via `write_plan`/`edit_plan`, with file paths per phase.
No file writes or commands — reject such requirements.
''',
    'sp_reader_items': '''\
Read the content; summarize key results, errors, warnings, next steps.
No commands, edits, or questions.
For large content, cover the key parts and note omissions.
''',
    'sp_supervisor_items': '''\
Delegate, don't implement: outline goals, constraints, acceptance criteria.
Decompose into non-overlapping tasks; serial if same output.
Dispatch via `subagent` (background; resume via session_id; stop via `interrupt_agent`).
Route failures via inquiry, then narrow correction.
Track via `todo_list`; verify each result, then one overall verification.
Final report: tasks, deliverables, verification, unresolved work.
''',
    'sp_swarm_leader_items': '''\
Split into independent homogeneous sub-tasks.
Call `workflow` with description, subagent_type (coder/explore/plan), prompt_template containing {{item}}, items list.
Dispatch and summarize only. If not parallelizable, explain why and stop.
''',
    'sp_agent_md': 'AGENTS.md:\n```\n{content}\n```\n',
    'sp_read_agents_md': 'Read AGENTS.md before work.\n',
    'sp_skills': 'Skills:\n{skills}\n',
}

# Optional worker clauses, substituted into ``sp_worker_optional`` per role.
_WORKER_OPTIONAL_CLAUSES: dict[str, str] = {
'YOLO': 'Yolo: never ask — pick the best option and continue.',
 'SUBAGENT': (
     'Sub-Agent: deliver a self-contained final result — the parent sees only '
     'your result, not your transcript.\n'
 ),
 'TRIVIAL': 'Need clarification? Make your final output the question and stop; the parent answers by resuming your session.',
}

# Todo-tracking clause per worker flavor: a regular worker tracks its own
# plan, while a sub-agent must never touch todo operations (the parent owns
# task tracking and the sub-agent toolset has no todo tool).
_WORKER_TODO_CLAUSES: dict[str, str] = {
    'TRACK': 'Track with todo_* tools.',
    'NO_TODO': (
        'Never use `todo_list` or any todo-tracking operations — the parent '
        'agent owns task tracking.'
    ),
}


def _load(name: str) -> str:
    """Return an embedded prompt template by name."""
    return _TEMPLATES[name]


def _load_items(name: str, **substitutions: str) -> list[str]:
    """Return the one-item-per-line prompt bullets of an embedded template."""
    text = _load(name)
    for key, value in substitutions.items():
        text = text.replace('{' + key + '}', value)
    return [line for line in text.splitlines() if line.strip()]


# Concise system prompt to reduce LLM overthinking and hallucination
_SYSTEM_PROMP = _load('sp_template').rstrip('\n')
_TOOL_CONVENTIONS = _load('sp_tool_conventions').strip() + '\n'


class SystemPromptType(Enum):
    Worker = 0
    TodoMaker = 1
    Thinker = 2
    TrivialSubAgent = 3
    Supervisor = 4
    Reader = 5
    SwarmLeader = 6


class SystemPromptCallback:
    # called in role
    role_callback: Callable[[SystemPromptType, list[str]], None] | None = None


def _shell_tool_name() -> str:
    """Return the shell tool name active for this agent.

    The agent config's ``agent.shell`` key (e.g. ``"shell": "powershell"`` in
    ``agent_worker.json``) selects the shell tool: the enablement logic in
    ``kimi_cli.tools.file.bash.bash_tool`` — ``_should_enable_bash`` and
    ``_should_enable_powershell`` — is config-aware, so the prompt always
    names the shell tool that is actually loaded.
    """
    from kimi_cli.tools.file.bash.bash_tool import (
        _should_enable_bash,
        _should_enable_powershell,
    )

    if _should_enable_powershell():
        return 'pwsh'
    if _should_enable_bash():
        return 'bash'
    return 'Run'


def get_system_prompt(
        yolo: bool | None = None,
        work_dir: Optional[KaosPath] = None,
        extra_system_prompt: SystemPromptCallback | None = None,
        agent_role: SystemPromptType = SystemPromptType.Worker,
        max_system_prompt_tokens: int = 4_000,
) -> Callable[[BuiltinSystemPromptArgs], str]:
    agent_md = (Path(str(work_dir)) if work_dir is not None else Path(
        os.curdir)) / 'AGENTS.md'
    yolo = yolo if yolo is not None else base._default_yolo


    def system_prompt_func(runtime: Runtime, is_compacting: bool = False, compact_export_path: str | None = None) -> str:
        args = runtime.builtin_args
        tool_conventions = ''
        items: list[str] = []
        agent_md_doc = ''
        skill_doc = ''
        use_agent_md = False
        use_skills = False
        items.extend(_load_items('sp_base_items', KIMI_OS=args.KIMI_OS, KIMI_WORK_DIR=str(args.KIMI_WORK_DIR)))
        if args.KIMI_OS == 'Windows':
            items.extend(_load_items('sp_windows_item'))
        def worker_logic(role: str, is_sub_agent: bool = False):
            nonlocal tool_conventions
            tool_conventions = _TOOL_CONVENTIONS
            nonlocal role_doc, use_agent_md, use_skills
            use_agent_md = True
            use_skills = True
            role_doc = role
            items.extend(_load_items('sp_worker_core', shell_tool=_shell_tool_name()))
            if is_sub_agent:
                items.append(_WORKER_TODO_CLAUSES['NO_TODO'])
            else:
                items.append(_WORKER_TODO_CLAUSES['TRACK'])
            subs = {'YOLO': '', 'SUBAGENT': '', 'TRIVIAL': ''}
            if is_sub_agent:
                subs['SUBAGENT'] = _WORKER_OPTIONAL_CLAUSES['SUBAGENT']
            else:
                if yolo:
                    subs['YOLO'] = _WORKER_OPTIONAL_CLAUSES['YOLO']
            items.extend(_load_items('sp_worker_optional', **subs))
        if extra_system_prompt and extra_system_prompt.role_callback:
            extra_system_prompt.role_callback(agent_role, items)

        match agent_role:
            case SystemPromptType.Worker:
                worker_logic("Software engineer assistant")
            case SystemPromptType.TodoMaker:
                use_agent_md = True
                use_skills = True
                role_doc = "Software engineer planner"
                items.extend(_load_items('sp_todomaker_items'))
            case SystemPromptType.Thinker:
                worker_logic("Software engineer thinker")
                items.extend(_load_items('sp_thinker_items'))
            case SystemPromptType.TrivialSubAgent:
                worker_logic("Sub-agent of a software engineer assistant", True)
                items.append(_WORKER_OPTIONAL_CLAUSES['TRIVIAL'])
            case SystemPromptType.Reader:
                role_doc = "Software engineer reader"
                items.extend(_load_items('sp_reader_items'))
            case SystemPromptType.Supervisor:
                use_agent_md = True
                use_skills = True
                role_doc = "Software engineer supervisor"
                items.extend(_load_items('sp_supervisor_items'))
            case SystemPromptType.SwarmLeader:
                use_agent_md = True
                use_skills = True
                role_doc = "Swarm orchestrator"
                items.extend(_load_items('sp_swarm_leader_items'))

        def _build_agent_md_doc() -> str:
            if use_agent_md and agent_md.is_file():
                agent_md_content = agent_md.read_text(
                    encoding='utf-8', errors='replace')
                if len(agent_md_content.encode('utf-8')) > 4096:
                    return _load('sp_read_agents_md')
                return _load('sp_agent_md').replace('{content}', agent_md_content)
            return ''

        if compact_export_path:
            items.append(f"Pre-compaction context exported to: {compact_export_path}")

        if use_skills and args.KIMI_SKILLS:
            skill_doc = _load('sp_skills').replace('{skills}', args.KIMI_SKILLS)
        numbered_block = ''
        if items:
            numbered_block = ''.join(
                f'- {item}\n' for item in items
            )
        agent_md_doc = _build_agent_md_doc()
        if count_tokens(agent_md_doc) >= max_system_prompt_tokens:
            agent_md_doc = _load('sp_read_agents_md')
        prompt = _SYSTEM_PROMP.format(
            TOOL_CONVENTIONS=tool_conventions,
            AGENT_ROLE=role_doc.strip(),
            NUMBERED=numbered_block,
            AGENTS_MD=agent_md_doc,
            SKILLS=skill_doc,
        ).strip()
        return prompt

    return system_prompt_func
