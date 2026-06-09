# Built-in Tools Guide

A coding agent's power comes from efficient interaction with the environment. This guide covers all 14 built-in tools in `agent_worker.json` and how to prompt the agent to use them effectively.

> **Note:** `agent_worker.json` overrides the `tools` field via `extend: default`, so only the 14 tools listed there are available.

---

## Tool Overview

| Category | Tools | Typical Use |
|----------|-------|-------------|
| **File & I/O** | `WriteFile`, `ReadFile`, `EditFile`, `Glob`, `Grep` | Create, read, modify, search files |
| **Code Execution** | `Run`, `Python` | Execute commands or Python code |
| **Process Management** | `TaskOutput`, `Input` | Interact with background processes |
| **Search & Info** | `FetchURL`, `Search` | Fetch web content, search local skills |
| **State & Tracking** | `SetTodoList` | Track progress |
| **Sub-agent** | `Agent` | Delegate subtasks |

---

## File & I/O

#### `WriteFile`
Write to a file. Modes: `overwrite` (default), `append`. For content >100 lines, split into multiple calls (first `overwrite`, rest `append`).

#### `ReadFile`
Read text files by line. Options: `line_offset`, `n_lines`, negative offset for tail reading. Long lines are auto-truncated. Read large files in chunks.

#### `EditFile`
String-level replacement in text files. Supports single/multi-line edits and `replace_all`. **Preferred for minimal diffs** — preserves formatting, comments, and blank lines.

#### `Glob`
Wildcard file search (`*`, `?`). Avoid `**` prefix or very large directories.

#### `Grep`
Regex content search (ripgrep-powered). Options: `-i` (case-insensitive), `multiline`, `-B`/`-A`/`-C` (context), `type`/`glob` filters.

---

## Code Execution & Process Management

#### `Run`
Execute programs or built-in mapped commands (100+ commands: `cat`, `ls`, `grep`, `find`, `curl`, `git`, etc.). Options: `args`, `cwd`, `output_path`, `timeout` (default 10s, range 3–180s). Exceeds timeout → background task with `task_id`.

> Not a shell interpreter — runs executables directly for safer, more predictable behavior.

#### `Python`
Execute Python code in a subprocess. Params: `code` (required), `output_path`, `timeout` (default 10s, range 3–60s). Exceeds timeout → background task. Max 8 concurrent Python processes. Code >30000 chars auto-saved to temp `.py` file.

#### `TaskOutput`
Get output from background tasks. Supports blocking wait, polling, `kill`, and `output_path` export.

#### `Input`
Send text to a running background process's stdin. For interactive programs.

**Typical workflow:**
1. `Run` to start a long command
2. If timeout, get `task_id`
3. `TaskOutput` to monitor progress
4. `Input` for interactive responses

---

## Search & Information

#### `FetchURL`
Fetch web content as Markdown via headless browser. Use for docs, API references, GitHub issues.

#### `Search`
Semantic search in local skill directories. Optional `dest_path` filter.

---

## State & Tracking

#### `SetTodoList`
Track multi-step task progress. States: `pending`, `in_progress`, `done`. Always pass the **complete list** on update.


---

## Sub-agent

#### `Agent`
Spawn an independent sub-agent for a specific subtask. Use for parallel work: code review, translation, module development.

---

## Prompting Strategies

### 1. Direct Instruction
Explicitly name tools and their purpose.

> "Use `Glob` to find all `.cpp` files under `src/`, then `ReadFile` each to check for `deprecated` markers. Write results to `report.md` with `WriteFile`."

### 2. Goal-Oriented
Describe the goal, let the agent choose tools.

> "Find this project's entry point and its third-party dependencies. Search the codebase and report back."

### 3. Constrained Execution
Add explicit constraints.

> "Change `MAX_RETRIES` to `5` in `config.py`. Requirements:
> 1. Use `EditFile` for minimal changes
> 2. `ReadFile` first to confirm line numbers
> 3. `ReadFile` again after editing to verify"

### 4. Step-by-Step Workflow
Break complex tasks into tool-annotated steps.

> "1. **Research**: `Glob` + `ReadFile` existing CLI commands
> 2. **Implement**: `WriteFile` or `EditFile` for new command
> 3. **Verify**: `Run` tests
> 4. **Track**: `SetTodoList` to mark complete"

### 5. Meta-Prompting
Embed tool guidelines in system prompts.

> "- **Observe before acting**: `ReadFile`/`Grep` before modifying
> - **Minimal changes**: prefer `EditFile`, avoid full-file overwrites
> - **Async long tasks**: `Run` + `TaskOutput` for >10s commands
> - **Delegate**: use `Agent` for independent subtasks"

---

## Best Practices

**Refactor a function safely:**
1. `Grep` for all occurrences
2. `ReadFile` to verify context (avoid string false-positives)
3. `EditFile` for replacements
4. `Grep` again to verify no leftovers
5. `Run` tests

**Interactive command:**
1. `Run` to start (timeout → background task)
2. `TaskOutput` to read prompts
3. `Input` to respond
4. Loop until complete

**Multi-file feature:**
1. `Glob` + `ReadFile` to research existing structure
2. Draft implementation plan
3. `EditFile`/`WriteFile` changes
4. `SetTodoList` track subtasks
5. `Run` tests

**External document analysis:**
1. `FetchURL` for web docs
2. Extract requirements
3. `Grep` + `ReadFile` verify implementation
4. Output diff report

---

## Plan Mode

Plan mode (`/plan`) is a two-stage workflow that separates **planning** from **implementation**:

1. **Generate** — a specialized planner agent (loaded from `agent_planner.json`) analyzes your requirement and writes a comprehensive plan to a Markdown file.
2. **Implement** — after you review the plan, a regular worker agent executes it.

#### Usage

```
/plan              # writes to plan.md (default)
/plan:roadmap.md   # writes to a custom file
```

After invoking the command, type your requirement. End input with `/end`, or cancel with `/cancel`.

#### Workflow

1. **Input requirement** — describe what you want to build or refactor.
2. **Planner generates plan** — the planner uses the Note tool to save the plan and auto-opens the file for review.
3. **Confirm implementation** — answer `y` to hand the plan to a worker agent, or `n` to keep the plan for manual execution.

#### Why use Plan Mode?

- **Complex features** — multi-file refactors, architecture changes, or new modules benefit from upfront design.
- **Review before commit** — inspect the plan, adjust scope, or split work before any code is written.
- **Delegation** — the planner acts as a dedicated architect, while the worker focuses on execution.

---

## Summary

1. **Observe first**: `ReadFile` / `Grep` / `Glob` before modifying
2. **Minimize changes**: `EditFile` preferred; `WriteFile` only for new files or full rewrites
3. **Async long tasks**: `Run` timeout → background, manage via `TaskOutput` / `Input`
4. **Integrate external info**: `FetchURL` for docs and references
5. **Plan before build**: use `/plan` for complex tasks to separate design from implementation
