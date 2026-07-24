---

Compact the above agent conversation context. Very detailed, comprehensive.

**What to keep (ordered by priority):**
1. **Current Task State** — what is being worked on right now, plus any user-supplied custom instructions, preferences, or constraints for future turns.
2. **Errors & Solutions** — preserve the full error message and the final working solution. For multi-turn debugging, summarize intermediate steps as a brief narrative (1-2 lines).
3. **Code State** — final working versions only (drop intermediate attempts).
4. **Design Decisions** — architectural choices and rationale.
5. **Environment** — OS, work directory, Python version, key dependencies, and other relevant setup.
6. **TODO Items** — unfinished tasks and known issues.
7. **Project Overview** — purpose, scope, tech stack.
8. **Key Decisions** — critical choices, rationale, rejected alternatives.
9. **Current State** — what works, merged/verified, active branch, test results.
10. **Important Files** — key paths and their roles (add, modify, delete).
11. **Architecture / Data Flow** — major components, interfaces, schema changes.
12. **Dependencies** — added, removed, upgraded packages or services.
13. **Risks / Rollback** — breaking changes, migration steps, revert strategy.
14. **Technical Notes** — patterns, constraints, APIs, env setup, performance or security considerations.

**What to remove or condense:**
- **Drop:** redundant explanations, failed intermediate attempts (retain lessons learned), verbose comments, conversational filler.
- **Merge:** similar discussions into single summary points.
- **Condense code:** 
  - Keep full version if ≤ 20 lines.
  - For longer code, keep signature + **key logic** only.
  
  **Key logic** means:
  - The core algorithm or business logic (not boilerplate/imports)
  - Critical control flow (conditionals, loops, error handling)
  - Non-obvious transformations or side effects
  - Exclude: imports, logging, type annotations, docstrings, setup/teardown boilerplate

**Special Handling:**
- **Code:** keep full version if < 20 lines; otherwise keep signature + key logic
- **Errors:** keep full error message + final solution
- **Discussions:** extract decisions and action items only

**Length:** Aim to reduce the context to approximately 20-30% of the original length while preserving all essential information. Err on the side of brevity for aggressive mode and completeness for retentive mode.

**User Instructions:** Preserve any explicit user preferences, constraints, or custom compaction instructions for future turns.

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

<completed_tasks>
- [Task]: [Brief outcome]
</completed_tasks>

<active_issues>
- [Issue]: [Status/Next steps]
</active_issues>

<todo>
- [ ] [Unfinished task]
</todo>

<code_state>
<file name="path/to/file.py">
<summary>What this file does</summary>
<key_elements>
- FunctionA: does X
- ClassB: handles Y
</key_elements>
<latest_version>
[Critical code snippets]
</latest_version>
</file>
</code_state>

<decisions>
- [Decision]: [Rationale]
</decisions>

<key_decisions>
- [Decision]: [Rationale, rejected alternatives]
</key_decisions>

<project_overview>
- Purpose: [project purpose]
- Scope: [project scope]
- Tech stack: [tech stack]
</project_overview>

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
