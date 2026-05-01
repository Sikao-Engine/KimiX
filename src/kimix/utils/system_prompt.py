from typing import Optional, Callable
from pathlib import Path
import os
from enum import Enum
from kaos.path import KaosPath
import kimix.base as base
from kimi_cli.soul.agent import BuiltinSystemPromptArgs

# This system prompt is designed to stop the modern LLM from over thinking and hallucination
_SYSTEM_PROMP = (
    '{AGENT_ROLE}.\n{RULES}\n{NOTE}\n{NUMBERED}{AGENTS_MD}{SKILLS}\n'
)

_NOTE = (
    'Note:\n'
    '1. For long tasks, use `Run` with `run_in_background=true`, then manage via '
    '`TaskList`, `TaskOutput`, `Input`. Return control immediately after starting.\n'
    '2. For complex or multi-step tasks, use `SetTodoList` to track progress.'
)

class SystemPromptType(Enum):
    Worker = 0
    TodoMaker = 1
    SwarmCoordinator = 1
    

def get_system_prompt(
        is_sub_agent: bool = False,
        plan_mode: bool | None = None,
        yolo: bool | None = None,
        work_dir: Optional[KaosPath] = None,
        skills_dirs: Optional[list[KaosPath]] = None,
        agent_role: SystemPromptType = SystemPromptType.Worker
) -> Callable[[BuiltinSystemPromptArgs], str]:
    agent_md = (Path(str(work_dir)) if work_dir is not None else Path(
        os.curdir)) / 'AGENTS.md'
    plan_mode = plan_mode if plan_mode is not None else base._default_plan_mode
    yolo = yolo if yolo is not None else base._default_yolo

    def system_prompt_func(args: BuiltinSystemPromptArgs) -> str:
        items: list[str] = []
        rules: str | None = None
        note_doc = ''
        agent_md_doc = ''
        skill_doc = ''

        match agent_role:
            case SystemPromptType.Worker:
                rules = (
                    'Rules: Direct output only. No chain-of-thought. No analysis. '
                    'No step-by-step. No reasoning blocks. No thinking-effort. zero preamble. '
                    'No postamble. Minimal explanation. Concisely. Shortly.'
                )
                role_doc = 'You are a terse ' + ('sub-agent' if is_sub_agent else 'coder')
                note_doc = _NOTE
                if not is_sub_agent:
                    items.append(
                        'Use `Agent` for: "parallelizable independent subtasks", '
                        '"large-context analysis or tasks needing different expertise", '
                        '"permission-graded operations like read-only analysis or sandboxed execution".'
                    )
                    items.append('Run Python: `python -c <code>`.')
                if not args.KIMI_OS == 'Windows':
                    items.append(f'Bash Shell: {args.KIMI_SHELL}. use `Run`')
                if plan_mode:
                    items.append('Plan mode: draft, ExitPlanMode, execute.')
                start_index = 3
                if yolo:
                    items.append(
                        'Yolo mode: act without asking. Stay in workdir. No system changes unless asked.'
                    )
            case SystemPromptType.TodoMaker:
                role_doc = '''You are a plan maker. Only make plan, never implement.
Record all steps using `Note` tool.
No multiple steps at once.
'''
                rules = (
                    'Rules: Direct output only. No chain-of-thought. No analysis. '
                    'No reasoning blocks. No thinking-effort. zero preamble. '
                    'No postamble. Minimal explanation. Concisely. Shortly.'
                )
                start_index = 1
            case SystemPromptType.SwarmCoordinator:
                role_doc = (
                    'You are a swarm coordinator. Build a dependency DAG via `AddNode` and `AddEdge`.\n'
                    '- AddNode: sub-task with a clear, actionable prompt\n'
                    '- AddEdge: execution order (upstream -> downstream)\n'
                    'Keep graph acyclic. Minimize edges to maximize parallelism.\n'
                    'Report all nodes and edges when done.\n\n'
                )
                rules = (
                    'Rules: Direct output only. No chain-of-thought. No analysis. '
                    'No reasoning blocks. No thinking-effort. zero preamble. '
                    'No postamble. Minimal explanation. Concisely. Shortly.'
                )
                start_index = 1


        if agent_md.is_file():
            agent_md_content = agent_md.read_text(encoding='utf-8', errors='replace')
            agent_md_doc = f'AGENTS.md:\n```\n{agent_md_content}\n```\n'
        items.append('Use `SkillRag` tool to search and retrieve skills.')
        if args.KIMI_SKILLS:
            skill_doc = f'Skills:\n{args.KIMI_SKILLS}\n'

        numbered_block = ''.join(
            f'{i + start_index}. {item}\n' for i, item in enumerate(items)
        )

        return _SYSTEM_PROMP.format(
            AGENT_ROLE=role_doc.strip(),
            RULES=rules.strip() if rules else '',
            NOTE=note_doc,
            NUMBERED=numbered_block,
            AGENTS_MD=agent_md_doc,
            SKILLS=skill_doc,
        ).strip()
    return system_prompt_func
