"""One-shot generator for reviews/tools/00-inventory.md (Phase 0 freeze).

Kept as a Phase-0 artifact generator; it reads the reviewed trees to record the
exact LOC / registration / test-file facts of the frozen inventory.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "kimi-cli" / "src" / "kimi_cli" / "tools"
KIMIX = ROOT / "src" / "kimix" / "tools"

# ID -> (tool name, [source paths relative to repo root], [test glob patterns])
INVENTORY: dict[str, tuple[str, list[str], list[str]]] = {
    "C00": (
        "toolset plumbing & shared helpers",
        ["kimi-cli/src/kimi_cli/tools/__init__.py", "kimi-cli/src/kimi_cli/tools/utils.py",
         "kimi-cli/src/kimi_cli/tools/display.py", "kimi-cli/src/kimi_cli/tools/reason.py"],
        ["kimi-cli/tests/tools/test_extract_key_argument.py", "kimi-cli/tests/tools/test_utils.py",
         "kimi-cli/tests/tools/test_reason.py", "kimi-cli/tests/core/test_toolset*.py"],
    ),
    "C01": (
        "subagent (AgentTool)",
        ["kimi-cli/src/kimi_cli/tools/agent/__init__.py",
         "kimi-cli/src/kimi_cli/tools/agent/description.md"],
        ["kimi-cli/tests/tools/test_agent_tool.py", "kimi-cli/tests/tools/test_tool_descriptions.py"],
    ),
    "C02": (
        "AskUserQuestion",
        ["kimi-cli/src/kimi_cli/tools/ask_user/__init__.py",
         "kimi-cli/src/kimi_cli/tools/ask_user/description.md"],
        ["kimi-cli/tests/tools/test_ask_user.py"],
    ),
    "C03": (
        "todo_list (TodoList)",
        ["kimi-cli/src/kimi_cli/tools/todo/__init__.py", "kimi-cli/src/kimi_cli/tools/display.py"],
        ["kimi-cli/tests/tools/test_todo*.py"],
    ),
    "C05": (
        "read (ReadFile)",
        ["kimi-cli/src/kimi_cli/tools/file/read.py", "kimi-cli/src/kimi_cli/tools/file/read.md",
         "kimi-cli/src/kimi_cli/tools/file/read_extract.py",
         "kimi-cli/src/kimi_cli/tools/file/read_archive.py",
         "kimi-cli/src/kimi_cli/tools/file/read_sqlite.py",
         "kimi-cli/src/kimi_cli/tools/file/read_pdf_pages.py",
         "kimi-cli/src/kimi_cli/tools/file/read_profiles.py",
         "kimi-cli/src/kimi_cli/tools/file/read_markit.py",
         "kimi-cli/src/kimi_cli/tools/file/read_media_shared.py",
         "kimi-cli/src/kimi_cli/tools/file/utils.py"],
        ["kimi-cli/tests/tools/test_read_file.py", "kimi-cli/tests/tools/test_read_extract.py",
         "kimi-cli/tests/tools/test_read_conflict.py", "tests/unit/tools/test_read_*.py"],
    ),
    "C06": (
        "read_image (ReadMediaFile)",
        ["kimi-cli/src/kimi_cli/tools/file/read_media.py",
         "kimi-cli/src/kimi_cli/tools/file/read_media.md",
         "kimi-cli/src/kimi_cli/tools/file/read_media_shared.py"],
        ["kimi-cli/tests/tools/test_read_media_file.py",
         "kimi-cli/tests/tools/test_read_media_file_desc.py"],
    ),
    "C07": (
        "glob (Glob)",
        ["kimi-cli/src/kimi_cli/tools/file/glob.py", "kimi-cli/src/kimi_cli/tools/file/glob.md",
         "kimi-cli/src/kimi_cli/tools/file/output_utils.py",
         "kimi-cli/src/kimi_cli/tools/file/fs_cache.py"],
        ["kimi-cli/tests/tools/test_glob.py", "kimi-cli/tests/tools/test_fs_cache_invalidation.py",
         "kimi-cli/tests/tools/test_additional_dirs.py",
         "kimi-cli/tests/tools/test_work_dir_resolution.py",
         "kimi-cli/tests/tools/test_output_utils.py"],
    ),
    "C08": (
        "grep (Grep)",
        ["kimi-cli/src/kimi_cli/tools/file/grep_local.py",
         "kimi-cli/src/kimi_cli/tools/file/grep_archive.py",
         "kimi-cli/src/kimi_cli/tools/file/grep_output.py",
         "kimi-cli/src/kimi_cli/tools/file/grep_recorder.py",
         "kimi-cli/src/kimi_cli/tools/file/grep_selectors.py"],
        ["kimi-cli/tests/tools/test_grep*.py"],
    ),
    "C09": (
        "write (WriteFile)",
        ["kimi-cli/src/kimi_cli/tools/file/write.py", "kimi-cli/src/kimi_cli/tools/file/write.md",
         "kimi-cli/src/kimi_cli/tools/file/check_fmt.py",
         "kimi-cli/src/kimi_cli/tools/file/parse_check.py",
         "kimi-cli/src/kimi_cli/tools/file/auto_repair.py",
         "kimi-cli/src/kimi_cli/tools/file/auto_generated.py",
         "kimi-cli/src/kimi_cli/tools/file/blackbox.py",
         "kimi-cli/src/kimi_cli/tools/file/conflict_detect.py",
         "kimi-cli/src/kimi_cli/tools/file/snapshot_store.py",
         "kimi-cli/src/kimi_cli/tools/file/fs_cache.py",
         "kimi-cli/src/kimi_cli/tools/file/edit_safety.py"],
        ["kimi-cli/tests/tools/test_write_file.py", "kimi-cli/tests/tools/test_write_conflict.py",
         "kimi-cli/tests/tools/test_auto_repair.py", "kimi-cli/tests/tools/test_parse_check.py",
         "kimi-cli/tests/tools/test_auto_generated_guard.py",
         "kimi-cli/tests/tools/test_check_fmt.py", "kimi-cli/tests/tools/test_blackbox.py",
         "kimi-cli/tests/tools/test_conflict_detect.py",
         "kimi-cli/tests/tools/test_snapshot_store.py"],
    ),
    "C10": (
        "edit (EditFile)",
        ["kimi-cli/src/kimi_cli/tools/file/edit/__init__.py",
         "kimi-cli/src/kimi_cli/tools/file/edit/base.py",
         "kimi-cli/src/kimi_cli/tools/file/edit/params.py",
         "kimi-cli/src/kimi_cli/tools/file/edit/modes/replace.py",
         "kimi-cli/src/kimi_cli/tools/file/edit/modes/sloppy.py",
         "kimi-cli/src/kimi_cli/tools/file/replace.py"],
        ["kimi-cli/tests/tools/test_edit_*.py"],
    ),
    "C11": (
        "web_search (SearchWeb)",
        ["kimi-cli/src/kimi_cli/tools/web/search.py",
         "kimi-cli/src/kimi_cli/tools/web/search.md",
         "kimi-cli/src/kimi_cli/tools/web/providers.py",
         "kimi-cli/src/kimi_cli/tools/web/content.py"],
        ["kimi-cli/tests/tools/test_web_search_dispatch.py",
         "kimi-cli/tests/tools/test_web_providers.py",
         "kimi-cli/tests/tools/test_web_providers_extra.py"],
    ),
    "C12": (
        "fetch_url (kimi_cli)",
        ["kimi-cli/src/kimi_cli/tools/web/fetch.py", "kimi-cli/src/kimi_cli/tools/web/fetch.md",
         "kimi-cli/src/kimi_cli/tools/web/content.py",
         "kimi-cli/src/kimi_cli/tools/web/url_safety.py",
         "kimi-cli/src/kimi_cli/tools/web/providers.py"],
        ["kimi-cli/tests/tools/test_fetch_url.py", "kimi-cli/tests/tools/test_url_safety.py"],
    ),
    "C13": (
        "web_extract (WebExtract)",
        ["kimi-cli/src/kimi_cli/tools/web/extract.py", "kimi-cli/src/kimi_cli/tools/web/extract.md",
         "kimi-cli/src/kimi_cli/tools/web/content.py",
         "kimi-cli/src/kimi_cli/tools/web/url_safety.py",
         "kimi-cli/src/kimi_cli/tools/web/providers.py"],
        ["kimi-cli/tests/tools/test_web_extract.py", "kimi-cli/tests/tools/test_url_safety.py"],
    ),
    "C15": (
        "HashRead / HashLine / HashEdit",
        ["kimi-cli/src/kimi_cli/tools/file/hash_line.py"],
        ["kimi-cli/tests/tools/test_hash_line.py", "kimi-cli/tests/tools/test_plan25_integration.py",
         "kimi-cli/tests/native/test_diff_glob_tools_equivalence.py"],
    ),
    "K00": (
        "shared infra",
        ["src/kimix/tools/common.py", "src/kimix/tools/prompt_common.py",
         "src/kimix/tools/security.py", "src/kimix/tools/check_fmt.py"],
        ["tests/test_tools_async.py", "tests/unit/tools/test_prompt_common.py",
         "tests/unit/tools/test_security.py", "tests/test_check_fmt.py",
         "tests/test_token_filter.py", "tests/test_filter_output.py",
         "tests/test_temp_folder_cleanup.py"],
    ),
    "K01": ("subagent (Agent)", ["src/kimix/tools/agent/__init__.py", "src/kimix/tools/agent/store.py"],
            ["tests/kimix/test_agent_conversation.py", "tests/kimix/test_subagent_anonymous_session.py",
             "tests/kimix/test_subagent_interrupt_race.py",
             "tests/test_prompt_waits_background_agents.py", "tests/kimix/test_report_sync.py"]),
    "K02": ("list_agents (AgentList)", ["src/kimix/tools/agent/__init__.py"],
            ["tests/kimix/test_agent_conversation.py"]),
    "K03": ("interrupt_agent (AgentClose)", ["src/kimix/tools/agent/__init__.py"],
            ["tests/kimix/test_agent_conversation.py"]),
    "K04": ("bash (Bash)",
            ["src/kimix/tools/file/bash/bash_tool.py", "src/kimix/tools/file/bash/bash_fix.py",
             "src/kimix/tools/file/bash/shell_common.py", "src/kimix/tools/file/bash/safety.py",
             "src/kimix/tools/file/bash/output_enhance.py", "src/kimix/tools/file/bash/__init__.py"],
            ["tests/test_bash.py", "tests/test_bash_e2e.py", "tests/test_shell_common.py",
             "tests/test_shell_safety.py", "tests/test_output_enhance.py",
             "tests/native/test_shell_security_equivalence.py"]),
    "K05": ("pwsh (Powershell)",
            ["src/kimix/tools/file/bash/pwsh_tool.py", "src/kimix/tools/file/bash/pwsh_fix.py",
             "src/kimix/tools/file/bash/process_pwsh.py", "src/kimix/tools/file/bash/pwsh_tool.md"],
            ["tests/test_powershell.py", "tests/test_pwsh_fix.py", "tests/test_process_pwsh.py",
             "tests/test_windows_env.py"]),
    "K06": ("Run", ["src/kimix/tools/file/run.py"],
            ["tests/test_run.py", "tests/test_custom_config.py", "tests/test_windows_env.py"]),
    "K07": ("python", ["src/kimix/tools/py/__init__.py"],
            ["tests/unit/tools/test_python.py", "tests/test_tools_async.py",
             "tests/unit/tools/test_elapsed_report.py"]),
    "K08": ("job_output (TaskOutput)", ["src/kimix/tools/background/__init__.py",
                                       "src/kimix/tools/background/utils.py"],
            ["tests/unit/tools/test_taskoutput.py", "tests/test_background_utils.py",
             "tests/test_stream_repeat_output.py"]),
    "K09": ("compact", ["src/kimix/tools/context/__init__.py"],
            ["tests/unit/tools/test_context_compact.py", "tests/test_integration_compaction.py"]),
    "K10": ("write_plan", ["src/kimix/tools/note/__init__.py"], ["tests/test_note.py"]),
    "K11": ("read_plan", ["src/kimix/tools/note/__init__.py"], ["tests/test_note.py"]),
    "K12": ("edit_plan", ["src/kimix/tools/note/__init__.py"], ["tests/test_note.py"]),
    "K13": ("workflow (AgentSwarm)", ["src/kimix/tools/swarm/__init__.py",
                                     "src/kimix/tools/swarm/best_of_n.py"],
            ["tests/kimix/test_agent_swarm.py", "tests/test_best_of_n.py"]),
    "K14": ("fetch_url (kimix)",
            ["src/kimix/tools/web/fetch_url.py", "src/kimix/tools/web/web_fetcher/__init__.py",
             "src/kimix/tools/web/web_fetcher/fetcher.py"],
            ["tests/unit/tools/test_fetchurl.py"]),
    "K15": ("ParserTool", ["src/kimix/tools/parser/__init__.py"], ["tests/test_parsers.py"]),
    "K16": ("FindStr", ["src/kimix/tools/file/find_str.py"],
            ["tests/native/test_behavior_equivalence.py"]),
}


def _loc(rel: str) -> int:
    p = ROOT / rel
    if not p.exists():
        return -1
    return len(p.read_text(encoding="utf-8", errors="replace").splitlines())


def _glob(patterns: list[str]) -> tuple[list[str], list[str]]:
    """Return (matched files, unmatched patterns)."""
    out: list[str] = []
    missing: list[str] = []
    for pat in patterns:
        hits = sorted(str(p.relative_to(ROOT)).replace("\\", "/") for p in ROOT.glob(pat))
        if not hits:
            missing.append(pat)
        out.extend(hits)
    return out, missing


def main() -> None:
    lines: list[str] = []
    lines.append("# Built-in tools review — 00 inventory (frozen)\n")
    lines.append("Phase 0 freeze of the reviewed surface. Branch `review/tools-hardgate`.\n")
    cli_py = sorted(CLI.rglob("*.py"))
    kimix_py = sorted(KIMIX.rglob("*.py"))
    cli_loc = sum(len(p.read_text(encoding='utf-8', errors='replace').splitlines()) for p in cli_py)
    kimix_loc = sum(len(p.read_text(encoding='utf-8', errors='replace').splitlines()) for p in kimix_py)
    lines.append("## Roots\n")
    lines.append(f"* `kimi-cli/src/kimi_cli/tools/**` — {len(cli_py)} `.py`, "
                 f"{len(list(CLI.rglob('*.md')))} `.md`, {cli_loc} LOC")
    lines.append(f"* `src/kimix/tools/**` — {len(kimix_py)} `.py`, "
                 f"{len(list(KIMIX.rglob('*.md')))} `.md`, {kimix_loc} LOC\n")
    lines.append("> Note: the plan §1 mentions `_ripgrep_common.py` (C08) and a second")
    lines.append("> `kimix/tools` `.md`; neither exists in the frozen tree "
                 "(recorded as a plan/tree drift finding).\n")

    lines.append("## Per-unit inventory\n")
    lines.append("| ID | tool | source (LOC) | tests present | tests missing |")
    lines.append("|---|---|---|---|---|")
    unmatched_all: dict[str, list[str]] = {}
    for uid, (tool, srcs, test_pats) in INVENTORY.items():
        src_txt = "<br>".join(f"`{s}` ({_loc(s)})" for s in srcs)
        tests, unmatched = _glob(test_pats)
        test_txt = "<br>".join(f"`{t}`" for t in tests) or "**none**"
        missing = "<br>".join(f"`{u}`" for u in unmatched) or ""
        if not test_pats:
            missing = "**no test glob declared**"
        unmatched_all[uid] = unmatched
        lines.append(f"| {uid} | {tool} | {src_txt} | {test_txt} | {missing} |")

    lines.append("\n## Plan-declared test files that do NOT exist\n")
    any_missing = False
    for uid, pats in unmatched_all.items():
        for pat in pats:
            any_missing = True
            lines.append(f"* {uid}: `{pat}`")
    if not any_missing:
        lines.append("* (none)")

    lines.append("\n## Registration sources\n")
    lines.append("* `kimi-cli/src/kimi_cli/agents/{default,okabe}/*.yaml`")
    lines.append("* `src/kimix/agent_{worker,subagent,boss,planner,readonly}.json`")
    lines.append("* programmatic: `kimi-cli/src/kimi_cli/soul/kimisoul.py:488-490`\n")
    lines.append("## Known tree drift vs the plan\n")
    lines.append("* `file/_ripgrep_common.py` listed for C08 — absent.")
    lines.append("* `file/grep_output.py` exists and is in C08 scope.")
    lines.append("* `ignore`d stale `__pycache__` trees exist under both roots (FP-06).")
    lines.append("* Plan §4.3 places the `read` unit tests at "
                 "`kimi-cli/tests/unit/tools/`; they actually live at "
                 "`tests/unit/tools/test_read_*.py` (root project). "
                 "Gate commands were corrected accordingly.")
    lines.append("* Plan §4.3 also names tests that do not exist in either tree "
                 "(e.g. `kimi-cli/tests/tools/test_snapshot_store.py` exists but "
                 "`kimi-cli/tests/unit/tools/...` does not).")
    lines.append("\n## Declared-scope additions (deviation from plan §4.1 file list)")
    lines.append("* `tools/gate_dup_allowlist.txt` — G-DUP needs a durable waiver file to "
                 "stay re-runnable with the bare command; created in P0.")
    lines.append("* `tools/gen_inventory.py` — reproducible generator for this inventory.")

    out = ROOT / "reviews" / "tools" / "00-inventory.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
