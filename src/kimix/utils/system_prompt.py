from typing import Optional, Callable
from pathlib import Path
import os
from enum import Enum
from kaos.path import KaosPath
import kimix.base as base
from kimi_cli.soul.agent import BuiltinSystemPromptArgs

# Concise system prompt to reduce LLM overthinking and hallucination
_SYSTEM_PROMP = (
    '{AGENT_ROLE}:\n{NUMBERED}\n{AGENTS_MD}\n{SKILLS}'
)


class SystemPromptType(Enum):
    Worker = 0
    TodoMaker = 1
    Thinker = 2
    SwarmCoordinator = 3
    SkillSearcher = 4,
    TrivialSubAgent = 5


class SystemPromptCallback:
    # called in role
    role_callback: Callable[[SystemPromptType, list[str]], None] | None = None


def get_system_prompt(
        yolo: bool | None = None,
        work_dir: Optional[KaosPath] = None,
        extra_system_prompt: SystemPromptCallback | None = None,
        agent_role: SystemPromptType = SystemPromptType.Worker,
) -> Callable[[BuiltinSystemPromptArgs], str]:
    agent_md = (Path(str(work_dir)) if work_dir is not None else Path(
        os.curdir)) / 'AGENTS.md'
    yolo = yolo if yolo is not None else base._default_yolo

    def system_prompt_func(args: BuiltinSystemPromptArgs) -> str:
        items: list[str] = []
        agent_md_doc = ''
        skill_doc = ''
        use_agent_md = False
        use_skills = False
        items.append('call tools in parallel.')
        def worker_logic(role: str, is_sub_agent: bool = False):
            nonlocal role_doc, use_agent_md, use_skills
            use_agent_md = True
            use_skills = True
            role_doc = f'You are a {role}'
            items.append(
                'Interactive: `Run` short timeout, then `TaskOutput`/`Input`.')
            items.append('Python: `python -c <code>`.')
            items.append(
                'Multi-step: use `SetTodoList`. Finish all before ending.')
            if args.KIMI_OS != 'Windows':
                items.append(f'Shell: {args.KIMI_SHELL}. prefer Use `Run`.')
            else:
                items.append('No Shell, use `Run`.')
            if yolo and not is_sub_agent:
                items.append('Yolo: no asking. Stay in workdir.')
            if not is_sub_agent:
                items.append('`Search` to search, retrieve skills, docs.')
                items.append('Remember: Drop context aggressively, write memory to dir `.kimix_cache/` after task.')
        if extra_system_prompt and extra_system_prompt.role_callback:
            extra_system_prompt.role_callback(agent_role, items)

        match agent_role:
            case SystemPromptType.Worker:
                worker_logic('terse coder')
            case SystemPromptType.TodoMaker:
                use_agent_md = True
                use_skills = True
                role_doc = 'You are a planner'
                items.append('Plan only. Do not implement.')
                items.append('Record steps with `Note`.')
                items.append('No multiple steps at once.')
                items.append(
                    'Use `Agent` to enable sub-agent, for research, analyze, find, retrieval.')
            case SystemPromptType.SwarmCoordinator:
                use_agent_md = True
                use_skills = True
                role_doc = 'You are a swarm coordinator'
                items.append('Build DAG with `AddNode` and `AddEdge`.')
                items.append('AddNode: clear, actionable sub-task prompt')
                items.append('AddEdge: upstream → downstream')
                items.append(
                    'Keep acyclic. Minimize edges, maximize parallelism.')
                items.append('Report nodes and edges.')
                items.append('`Search` to search, retrieve skills, docs.')
            case SystemPromptType.Thinker:
                worker_logic('thinker')
                items.append(
                    'Think in <thinking>...</thinking>. End with <quit/>. Concise. No text outside tags.')
                items.append('Self-verify: catch errors and bad assumptions.')
            case SystemPromptType.SkillSearcher:
                role_doc = 'You are a searcher'
                items.append('Search, analyze, report concisely.')
            case SystemPromptType.TrivialSubAgent:
                worker_logic('read-only sub-agent', True)
                items.append('Read only, Reject write/edit tasks.')

        if use_agent_md and agent_md.is_file():
            agent_md_content = agent_md.read_text(
                encoding='utf-8', errors='replace')
            agent_md_doc = f'AGENTS.md:\n```\n{agent_md_content}\n```\n'
        if use_skills and args.KIMI_SKILLS:
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
        ).strip()
    return system_prompt_func
