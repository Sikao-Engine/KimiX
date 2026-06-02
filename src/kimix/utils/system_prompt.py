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

# Concise system prompt to reduce LLM overthinking and hallucination
_SYSTEM_PROMP = (
    '{AGENT_ROLE}:\n{NUMBERED}\n{AGENTS_MD}{SKILLS}'
)


class SystemPromptType(Enum):
    Worker = 0
    TodoMaker = 1
    Thinker = 2
    SwarmCoordinator = 3
    SkillSearcher = 4,
    TrivialSubAgent = 5
    Supervisor = 6


class SystemPromptCallback:
    # called in role
    role_callback: Callable[[SystemPromptType, list[str]], None] | None = None


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


    def system_prompt_func(runtime: Runtime, is_compacting: bool = False) -> str:
        args = runtime.builtin_args
        items: list[str] = []
        agent_md_doc = ''
        skill_doc = ''
        use_agent_md = False
        use_skills = False
        items.append('Call tools in parallel.')
        items.append(f'OS: {args.KIMI_OS}, Work-Dir: {args.KIMI_WORK_DIR}')
        def worker_logic(role: str, is_sub_agent: bool = False):
            nonlocal role_doc, use_agent_md, use_skills
            use_agent_md = True
            use_skills = True
            role_doc = f'You are a {role}'
            items.append(
                'Persist: finish all requirements, keep trying until done.')
            items.append(
                'Interactive: short timeout, then `TaskOutput`/`Input`.')
            # if args.KIMI_OS == 'Windows':
            #     items.append('No shell, powershell or cmd: use `Run`/`Python` instead.')
            # else:
            #     items.append('No shell or bash: use `Run`/`Python` instead.')
            items.append('Multi-step: use `SetTodoList` and `StepMemory`. Finish all before ending.')
            if yolo and not is_sub_agent:
                items.append('Yolo: no asking. accept all.')
            if not is_sub_agent:
                items.append('`Search` to search, retrieve skills, docs.')
                items.append('Drop context aggressively, use `StepMemory` to manage memory.')
                items.append('Use `ContextRetrieval` to recall past conversation turns that were compacted out of context.')
            else:
                items.append('Sub-Agent: only report results.')
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
                items.append('Record comprehensive plan with `Note`.')
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
                worker_logic('terse sub-agent', True)
            case SystemPromptType.Supervisor:
                use_agent_md = True
                use_skills = True
                role_doc = 'You are a supervisor'
                # Supervisor: outline → decompose → dispatch → track → accept/inquire/correct → verify.
                items.append('Outline goals, constraints, unknowns, acceptance criteria before delegating.')
                items.append('Decompose into non-overlapping tasks (Explorer/Worker/Reviewer/Verifier). Serial if same output.')
                items.append('Dispatch via `Agent` with role, goal, scope, non-goal, inputs, acceptance criteria.')
                items.append('Never do sub-agent work yourself. Route failures through inquiry, then narrow correction.')
                items.append('Track with `SetTodoList`. Accept or inquire/reject each result against criteria.')
                items.append('After all accepted and merged, run one overall verification suited to task type.')
                items.append('Final: report tasks, deliverables, verification result, unresolved work, merged conclusion.')

        def _build_agent_md_doc() -> str:
            if use_agent_md and agent_md.is_file():
                agent_md_content = agent_md.read_text(
                    encoding='utf-8', errors='replace')
                if len(agent_md_content.encode('utf-8')) > 4096:
                    return 'read AGENTS.md before work\n'
                return f'AGENTS.md:\n```\n{agent_md_content}\n```\n'
            return ''

        if use_skills and args.KIMI_SKILLS:
            skill_doc = f'Skills:\n{args.KIMI_SKILLS}\n'
        numbered_block = ''
        if items:
            numbered_block = ''.join(
                f'- {item}\n' for item in items
            )
        agent_md_doc = _build_agent_md_doc()
        if count_tokens(agent_md_doc) >= max_system_prompt_tokens:
            agent_md_doc = 'read AGENTS.md before work\n'
        prompt = _SYSTEM_PROMP.format(
            AGENT_ROLE=role_doc.strip(),
            NUMBERED=numbered_block,
            AGENTS_MD=agent_md_doc,
            SKILLS=skill_doc,
        ).strip()
        return prompt

    return system_prompt_func
