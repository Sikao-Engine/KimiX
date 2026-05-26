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


_MAX_STEP_ENTRIES: int = 200


def _maybe_compact_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(steps) <= _MAX_STEP_ENTRIES:
        return steps
    split = len(steps) // 2
    compacted: list[dict[str, Any]] = []
    for s in steps[:split]:
        compacted.append(
            {
                "seq": s.get("seq"),
                "time": s.get("time"),
                "brief": s.get("brief"),
                "step": "[compacted] " + (s.get("step", ""))[:100],
                "result": "[compacted]",
                "files": [],
            }
        )
    return compacted + steps[split:]


def _maybe_truncate_steps_by_tokens(
    steps: list[dict[str, Any]], budget_chars: int
) -> list[dict[str, Any]]:
    """Keep only the most recent steps whose rendered form fits in *budget_chars*.

    Walks backwards from the newest step, accumulating an approximate character
    budget, and returns the tail slice that fits.
    """
    if not steps:
        return steps
    total = 0
    cutoff = 0
    for i in range(len(steps) - 1, -1, -1):
        s = steps[i]
        line_len = 50  # base overhead for seq/time/brief wrapping
        if s.get("step"):
            line_len += len(str(s["step"]).replace("\n", " "))
        if s.get("result"):
            line_len += len(str(s["result"]).replace("\n", " "))
        if s.get("files"):
            line_len += 30
        total += line_len
        if total > budget_chars:
            cutoff = i + 1
            break
    return steps[cutoff:]


# Concise system prompt to reduce LLM overthinking and hallucination
_SYSTEM_PROMP = (
    '{AGENT_ROLE}:\n{NUMBERED}\n{AGENTS_MD}{SKILLS}{EXTRA}'
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

    def _build_extra(
        runtime: Runtime,
        *,
        step_mem_limit_chars: int | None = None,
        max_changed_files: int | None = None,
        max_todos: int | None = None,
    ) -> str:
        extra = ''
        context_dir = runtime.session.dir
        step_mem_path = context_dir / 'steps' / f'{runtime.session.id}.json'
        # Memory: only include when context has been compacted
        if step_mem_path.is_file():
            try:
                steps = orjson.loads(step_mem_path.read_text(encoding='utf-8'))
                if isinstance(steps, list) and steps:
                    steps = _maybe_compact_steps(steps)
                    if step_mem_limit_chars is not None:
                        steps = _maybe_truncate_steps_by_tokens(steps, step_mem_limit_chars)
                    lines: list[str] = []
                    for s in steps:
                        seq = s.get('seq')
                        time = s.get('time')
                        brief = s.get('brief')
                        step = s.get('step')
                        result = s.get('result')
                        files = s.get('files', [])

                        parts: list[str] = []
                        if seq is not None:
                            parts.append(f"**#{seq}**")
                        if time:
                            parts.append(f"`{time}`")
                        if brief:
                            parts.append(f"*{brief}*")
                        if step:
                            step_text = str(step).replace('\n', ' ')
                            parts.append(f"step:{step_text}")
                        if result:
                            result_text = str(result).replace('\n', ' ')
                            parts.append(f"result:{result_text}")
                        if files:
                            files_list = files if isinstance(files, list) else [str(files)]
                            if work_dir is not None:
                                wd = Path(str(work_dir)).resolve()
                                rel_files: list[str] = []
                                for f in files_list:
                                    try:
                                        rel_files.append(str(Path(f).resolve().relative_to(wd)))
                                    except (ValueError, OSError):
                                        rel_files.append(str(f))
                                files_str = ', '.join(rel_files)
                            else:
                                files_str = ', '.join(files_list)
                            parts.append(f"files:{files_str}")
                        if parts:
                            lines.append('- ' + ' | '.join(parts))
                    if lines:
                        extra = "Context compacted, content may be stale. Before-compact Memory:\n" + "\n".join(lines) + "\n"
            except Exception:
                pass

        # Changed files from ToolCallReason
        tool_call_reason = runtime.session.custom_data.get("tool_call_reason")
        if isinstance(tool_call_reason, ToolCallReason) and tool_call_reason.changed_files:
            cwd = Path.cwd()
            tcr_md = tool_call_reason.to_markdown(cwd=cwd, max_count=max_changed_files if max_changed_files is not None else 100)
            if tcr_md:
                if extra:
                    extra = extra.rstrip("\n") + "\n\nBefore-compact " + tcr_md + "\n"
                else:
                    extra = tcr_md + "\n"

        # Todo list
        try:
            todos: list[dict[str, str]] = []
            if runtime.role == "root":
                for t in runtime.session.state.todos:
                    todos.append({"title": t.title, "status": t.status})
            elif runtime.subagent_store is not None and runtime.subagent_id is not None:
                state_file = runtime.subagent_store.instance_dir(runtime.subagent_id) / "state.json"
                if state_file.exists():
                    data = orjson.loads(state_file.read_text(encoding="utf-8"))
                    raw = data.get("todos", [])
                    if isinstance(raw, list):
                        for item in raw:
                            if isinstance(item, dict) and "title" in item and "status" in item:
                                todos.append(item)
            if todos:
                if max_todos is not None:
                    todos = todos[-max_todos:]
                todo_lines = ["Todo:"]
                for t in todos:
                    status = t["status"]
                    title = t["title"]
                    icon = {"pending": "○", "in_progress": "◐", "done": "●"}.get(status, "○")
                    todo_lines.append(f"- {icon} [{status}] {title}")
                todo_md = 'Before-compact Todo-List:\n' + "\n".join(todo_lines) + "\n"
                if extra:
                    extra = extra.rstrip("\n") + "\n\n" + todo_md
                else:
                    extra = todo_md
        except Exception:
            pass
        return extra

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
                'Interactive: `Run`/`Python` short timeout, then `TaskOutput`/`Input`.')
            items.append('No Shell or Bash: use `Run`/`Python` instead.')
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
                items.append('Record steps with `Note`.')
                items.append('No multiple steps at once.')
                items.append('Use `Agent` to enable sub-agent, for research, analyze, find, retrieval.')
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
        # Attempt progressively stricter budgets until the prompt fits.
        budgets = [
            # Full everything
            {"step_mem_limit_chars": None, "max_changed_files": None, "agent_md_mode": "full", "max_todos": None},
            # Truncate step memory to ~4k chars (~1000 tokens)
            {"step_mem_limit_chars": 4_000, "max_changed_files": None, "agent_md_mode": "full", "max_todos": None},
            # Truncate step memory to ~4k chars (~1000 tokens), Also cap changed files at 50
            {"step_mem_limit_chars": 4_000, "max_changed_files": 50, "agent_md_mode": "drop", "max_todos": None},
            # Drop AGENTS.md inline entirely
            {"step_mem_limit_chars": 2_000, "max_changed_files": 20, "agent_md_mode": "drop", "max_todos": 15},
        ]
        for budget in budgets:
            if is_compacting:
                extra = _build_extra(
                    runtime,
                    step_mem_limit_chars=budget["step_mem_limit_chars"],
                    max_changed_files=budget["max_changed_files"],
                    max_todos=budget["max_todos"],
                )
            else:
                extra = ''
            if budget["agent_md_mode"] == "drop":
                agent_md_doc = 'read AGENTS.md before work\n'
            else:
                agent_md_doc = _build_agent_md_doc()

            prompt = _SYSTEM_PROMP.format(
                AGENT_ROLE=role_doc.strip(),
                NUMBERED=numbered_block,
                AGENTS_MD=agent_md_doc,
                SKILLS=skill_doc,
                EXTRA=extra
            ).strip()

            if count_tokens(prompt) <= max_system_prompt_tokens:
                return prompt

        # If even the most aggressive budget still exceeds the limit, return it anyway;
        # the caller can decide to drop the system prompt entirely or raise.
        return prompt
    return system_prompt_func
