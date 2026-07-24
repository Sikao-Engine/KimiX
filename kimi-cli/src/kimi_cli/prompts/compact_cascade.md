---

The above context contains **multiple previous compaction summaries** that have been recursively summarized. Your task is to extract a **flat, deduplicated list of key facts** from the entire history.
Very detailed, comprehensive.

**Rules:**
- Output a bulleted list of non-redundant facts, decisions, and current file states.
- **De-duplicate:** If the same fact appears across multiple previous summaries, include it only once.
- **Discard** narrative flow, transitional language, and meta-commentary about the compaction process itself.
- **Preserve:** error messages, final solutions, tool output results, architectural decisions, design rationale, and current task state.
- **Keep:** project overview (purpose, scope, tech stack), key decisions with rejected alternatives, current state (what works, merged/verified, active branch, test results), important files with roles, architecture/data flow, dependencies (added/removed/upgraded), risks/rollback strategy, technical notes (patterns, constraints, APIs, env setup, performance/security).
- **Condense:** long code blocks → signatures + key logic only (keep full version if < 20 lines).
- **Discussions:** extract decisions and action items only.

**Length:** Aim to reduce the context to a compact fact list while preserving all essential information.

**Output Structure:**

```xml
<current_focus>
[What we're working on now]
</current_focus>

<environment>
- OS: [os]
- Work dir: [path]
- Key deps: [packages]
- [Other relevant setup]
</environment>

<code_state>
[Critical file states — signatures + key changes]
</code_state>

<facts>
- [Decision] [Decision description and rationale]
- [Code] [File path / function / key logic]
- [Env] [Environment detail]
- [Error] [Error message and resolution]
</facts>

<active_issues>
- [Issue]: [Status/Next steps]
</active_issues>

<project_overview>
- Purpose: [project purpose]
- Scope: [project scope]
- Tech stack: [tech stack]
</project_overview>

<key_decisions>
- [Decision]: [Rationale, rejected alternatives]
</key_decisions>

<current_state>
- What works: [summary]
- Merged/Verified: [status]
- Active branch: [branch]
- Test results: [summary]
</current_state>

<important_files>
- [path/to/file.py]: [role]
</important_files>

<architecture>
- Major components: [list]
- Interfaces: [summary]
- Data flow: [summary]
- Schema changes: [summary]
</architecture>

<dependencies>
- Added: [packages]
- Removed: [packages]
- Upgraded: [packages]
</dependencies>

<risks_rollback>
- Breaking changes: [details]
- Migration steps: [details]
- Revert strategy: [details]
</risks_rollback>

<technical_notes>
- Patterns: [details]
- Constraints: [details]
- APIs: [details]
- Env setup: [details]
- Performance/Security: [details]
</technical_notes>

<important_context>
- [Crucial information not covered above]
</important_context>
```
