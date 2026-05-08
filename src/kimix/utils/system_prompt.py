from typing import Optional, Callable
from pathlib import Path
import os
from enum import Enum
from kaos.path import KaosPath
import kimix.base as base
from kimi_cli.soul.agent import BuiltinSystemPromptArgs

# This system prompt is designed to stop the modern LLM from over thinking and hallucination
_SYSTEM_PROMP = (
    '{AGENT_ROLE}:\n{NUMBERED}\n{AGENTS_MD}\n{SKILLS}\n{EXTRA}'
)

class SystemPromptType(Enum):
    Worker = 0
    TodoMaker = 1
    Thinker = 2
    SwarmCoordinator = 3
    Recaller = 4
    SkillSearcher = 5
    

def get_system_prompt(
        is_sub_agent: bool = False,
        yolo: bool | None = None,
        work_dir: Optional[KaosPath] = None,
        extra_system_prompt: str | None = None,
        agent_role: SystemPromptType = SystemPromptType.Worker
) -> Callable[[BuiltinSystemPromptArgs], str]:
    agent_md = (Path(str(work_dir)) if work_dir is not None else Path(
        os.curdir)) / 'AGENTS.md'
    yolo = yolo if yolo is not None else base._default_yolo

    def system_prompt_func(args: BuiltinSystemPromptArgs) -> str:
        items: list[str] = []
        agent_md_doc = ''
        skill_doc = ''
        first_rule = ', minimal explanation, concisely, shortly'
        def worker_logic():
            nonlocal role_doc
            role_doc = 'You are a terse ' + ('sub-agent' if is_sub_agent else 'coder') + first_rule
            items.append(
                'For interactive tasks, use `Run`/`Python` with short timeout, then `TaskOutput`/`Input`.'
            )
            items.append(
                'For complex or multi-step tasks, use `SetTodoList` to track progress.'
            )
            if not is_sub_agent:
                items.append(
                    'Use `Agent` for: "parallelizable independent subtasks", '
                    '"large-context analysis or tasks needing different expertise", '
                    '"permission-graded operations like read-only analysis or sandboxed execution".'
                )
            if args.KIMI_OS != 'Windows':
                items.append(f'Bash Shell: {args.KIMI_SHELL}. use `Run`.')
            else:
                items.append('No Shell, use `Run`.')
            if yolo:
                items.append(
                    'Yolo mode: act without asking. Stay in workdir. No system changes unless asked.'
                )
        match agent_role:
            case SystemPromptType.Worker:
                worker_logic()
            case SystemPromptType.TodoMaker:
                role_doc = 'You are a plan maker' + first_rule
                items.append('Only make plan, never implement.')
                items.append('Record all steps using `Note` tool.')
                items.append('No multiple steps at once.')
            case SystemPromptType.SwarmCoordinator:
                role_doc = 'You are a swarm coordinator' + first_rule
                items.append('Build a dependency DAG via `AddNode` and `AddEdge`')
                items.append('AddNode: sub-task with a clear, actionable prompt')
                items.append('AddEdge: execution order (upstream -> downstream)')
                items.append('Keep graph acyclic. Minimize edges to maximize parallelism.')
                items.append('Report all nodes and edges when done.')
            case SystemPromptType.Thinker:
                worker_logic()
                items.append(
                    "Think step by step. "
                    "Put your reasoning in <thinking>...</thinking>. "
                    "When finished, write <quit/>. "
                    "Be concise. No text outside tags."
                )
                items.append('Self-verify: catch errors, omissions, bad assumptions before final answer.')
            case SystemPromptType.Recaller:
                role_doc = 'You are a memory recaller'
                items.append('use `Recall` and `Reflect` tools.')
                items.append('For complex or multi-step tasks, use `SetTodoList` to track progress.')
                items.append('Search memories, analyze, provide insights, report findings concisely. Do not modify anything.')
            case SystemPromptType.SkillSearcher:
                role_doc = 'You are a skill searcher'
                items.append('Only use `SkillSearch` tool.')
                items.append('For complex or multi-step tasks, use `SetTodoList` to track progress.')
                items.append('Search skills, analyze, provide insights, report results concisely. Do not modify anything.')


        items.append('Use `Remember`, `Recall`, `Reflect`, `Forget` whenever memory is needed: long tasks, heavy context, multi-turn work, or anything worth saving.')
        if agent_md.is_file():
            agent_md_content = agent_md.read_text(encoding='utf-8', errors='replace')
            agent_md_doc = f'AGENTS.md:\n```\n{agent_md_content}\n```\n'
        items.append('Use `SkillSearch` tool to search and retrieve skills.')
        if args.KIMI_SKILLS:
            skill_doc = f'Skills:\n{args.KIMI_SKILLS}\n'
        numbered_block = ''
        if items:
            numbered_block = ''.join(
                f'- {item}\n' for item in items
            )

        return _SYSTEM_PROMP.format(
            AGENT_ROLE=role_doc.strip(),
            NUMBERED=numbered_block,
            AGENTS_MD=agent_md_doc,
            SKILLS=skill_doc,
            EXTRA=extra_system_prompt if extra_system_prompt is not None else ''
        ).strip()
    return system_prompt_func
