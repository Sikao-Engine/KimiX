# RTK (Rust Token Killer) — Usage Summary & Integration Guide

> Repo: `https://github.com/rtk-ai/rtk` — cloned to `C:\dev\rtk`

---

## Table of Contents

1. [What is RTK?](#1-what-is-rtk)
2. [Installation](#2-installation)
3. [Core Concepts](#3-core-concepts)
4. [Complete Command Reference](#4-complete-command-reference)
5. [Filtering Strategies & Savings](#5-filtering-strategies--savings)
6. [Unrecognized Commands — Passthrough Behavior](#6-unrecognized-commands--passthrough-behavior)
7. [Compatibility Matrix](#7-compatibility-matrix)
8. [Integration with Kimix Tools](#8-integration-with-kimix-tools)
9. [Configuration & Customization](#9-configuration--customization)
10. [Analytics & Tracking](#10-analytics--tracking)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. What is RTK?

**RTK (Rust Token Killer)** is a high-performance CLI proxy that reduces LLM token consumption by **60–90%** when used with AI coding assistants. It is a **single Rust binary** with <10ms overhead per command, zero external runtime dependencies.

### Problem It Solves

LLM-powered coding agents (Claude Code, Cursor, Copilot, Gemini CLI, etc.) consume tokens for every CLI command output they process. Most command outputs contain:
- Boilerplate and verbose formatting (`On branch main`, `Your branch is up to date with...`)
- Progress bars and ANSI escape sequences
- Hundreds of passing test lines (when only failures matter)
- Repetitive log lines

RTK sits **between the agent and the CLI**, intercepting commands via hooks, executing them, filtering the output, and returning only the compact, actionable result to the LLM.

### Architecture Diagram

```
Agent / User
    |
    v
+------------------------------------------+
|  LLM Agent Hook (thin delegate)          |
|  Intercepts: "git status" → "rtk git status"
+------------------+-----------------------+
                   |
                   v
+------------------------------------------+
|  RTK Binary (main.rs)                    |
|                                           |
|  +-------------+    +-----------------+  |
|  | Clap Parser | → | Command Routing  |  |
|  | (Commands   |    | (match on enum) |  |
|  |  enum)      |    +--------+--------+  |
|  +-------------+             |           |
|                    +---------+---------+ |
|                    v         v         v |
|             +----------+ +--------+ +--+ |
|             |Rust Filter| |TOML DSL| |PS| |
|             |(cmds/**)  | |Filter  | |TH| |
|             +-----+----+ +----+---+ +--+ |
|                   |           |           |
|                   +-----+-----+           |
|                         v                 |
|              +---------------------+      |
|              |   Token Tracking    |      |
|              |   SQLite DB         |      |
|              +---------------------+      |
+------------------------------------------+
```

---

## 2. Installation

### Verify Correct Package (⚠️ Name Collision Warning)

Two projects share the name `rtk`:

| If `rtk gain`... | You have |
|------------------|----------|
| Shows token savings dashboard | ✅ **Rust Token Killer** (this project) |
| Returns `"not a rtk command"` | ❌ **Rust Type Kit** (wrong package) |

```bash
rtk --version   # Should show "rtk 0.28.2" (or newer)
rtk gain        # Should show token savings stats
```

### Installation Methods

| Method | Command |
|--------|---------|
| **Homebrew** | `brew install rtk-ai/tap/rtk` |
| **Quick Install (Linux/macOS)** | `curl -fsSL https://raw.githubusercontent.com/rtk-ai/rtk/master/install.sh \| sh` |
| **Cargo (from Git)** | `cargo install --git https://github.com/rtk-ai/rtk` |
| **Pre-built binaries** | Download from [GitHub Releases](https://github.com/rtk-ai/rtk/releases) — Windows: `rtk-x86_64-pc-windows-msvc.zip` |

### Verify

```bash
rtk --version           # rtk x.y.z
rtk gain                # Token savings dashboard (shows 0 initially)
```

### Project Initialization

```bash
# Global hook (recommended — all projects, automatic)
rtk init -g

# Single project (no hook, instructions in CLAUDE.md)
cd /your/project && rtk init

# Other agents
rtk init -g --gemini            # Gemini CLI
rtk init -g --codex             # Codex CLI
rtk init --agent cursor         # Cursor
rtk init --agent windsurf       # Windsurf
rtk init --agent cline          # Cline / Roo Code
rtk init --agent kilocode       # Kilo Code
rtk init --agent pi             # Pi
rtk init --agent hermes         # Hermes

# Uninstall
rtk init -g --uninstall
```

### Dry-run Preview

```bash
rtk init --global --dry-run        # See what would change
rtk init --global --dry-run -v     # Also show file content
```

---

## 3. Core Concepts

### How RTK Saves Tokens

Four strategies applied per command type:

| Strategy | Description | Example |
|----------|-------------|---------|
| **Smart Filtering** | Removes noise (comments, whitespace, boilerplate) | `ls -la` → compact tree |
| **Grouping** | Aggregates similar items (files by directory, errors by type) | Tests grouped by file |
| **Truncation** | Keeps relevant context, cuts redundancy | Diff condensed |
| **Deduplication** | Collapses repeated log lines with counts | `error x42` |

### Hook System

RTK uses **thin delegate hooks** — shell scripts or plugins that:
1. Read the agent's JSON event (e.g., `PreToolUse`)
2. Call `rtk rewrite "<command>"` as a subprocess
3. Return the rewritten command to the agent in its specific JSON format

The rewrite logic lives **entirely in the Rust binary** (`src/discover/registry.rs`). Hooks contain zero filtering logic — they are just JSON format adapters.

### Two Hook Strategies

| Strategy | Mechanism | Adoption |
|----------|-----------|----------|
| **Auto-Rewrite** (default) | Hook intercepts & rewrites before execution | 100% |
| **Suggest** (non-intrusive) | Hook emits a hint; agent decides | ~70–85% |

### Tee System

When a command **fails**, RTK saves the full raw output to a local file and prints the path:

```
FAILED: 2/15 tests
[full output: ~/.local/share/rtk/tee/1707753600_cargo_test.log]
```

The agent can then read the file for full detail without re-running.

### Verbosity Levels

| Level | Output |
|-------|--------|
| (default) | Compact filtered output only |
| `-v` | Show debug messages on stderr |
| `-vv` | Show command being executed |
| `-vvv` | Show raw output before filtering |

### Global Flags

| Flag | Short | Effect |
|------|-------|--------|
| `--verbose` | `-v` | Increase verbosity (-v, -vv, -vvv) |
| `--ultra-compact` | `-u` | ASCII icons, inline format — extra token reduction |
| `--skip-env` | — | Sets `SKIP_ENV_VALIDATION=1` for child processes (Next.js, tsc) |

---

## 4. Complete Command Reference

### File Commands

```bash
rtk ls [args...]              # Compact directory tree (~80% savings)
rtk tree [args...]            # Compact tree output (~80% savings)
rtk read <file> [options]     # Smart file reading with filtering levels
rtk read - [options]          # Read from stdin
rtk smart <file>              # 2-line heuristic code summary (~95% savings)
rtk find <pattern> [path]     # Compact grouped results (~80% savings)
rtk grep <pattern> [path]     # Grouped, truncated search results (~80% savings)
rtk rg <pattern> [path]       # Same as grep, uses ripgrep natively
rtk diff <file1> [file2]      # Ultra-condensed diff (~60% savings)
rtk wc [args...]              # Compact word/line/byte count
rtk json <file>               # JSON compact (keys-only mode available)
```

**`rtk read` filter levels:**

| Level | Description | Savings |
|-------|-------------|---------|
| `none` | No filtering, raw output | 0% |
| `minimal` | Strips comments and excessive blank lines | ~30% |
| `aggressive` | Signatures only (strips function bodies) | ~74% |

Example: `rtk read main.rs -l aggressive`

### Git Commands

```bash
rtk git status                # "main | 3M 1? 1A" — ~80% savings
rtk git log [args...]         # One-line commits — ~80% savings
rtk git diff [args...]        # Condensed diff — ~75% savings
rtk git show [args...]        # Commit summary + compact diff — ~80%
rtk git add [args...]         # Output: "ok" — ~92% savings
rtk git commit -m "msg"       # Output: "ok abc1234" — ~92%
rtk git push [args...]        # Output: "ok main" — ~92%
rtk git pull [args...]        # Output: "ok 3 files +10 -2" — ~92%
rtk git branch [args...]      # Compact branch listing
rtk git fetch [args...]       # Output: "ok fetched (N new refs)"
rtk git stash [subcmd]        # Compact stash management
rtk git worktree [subcmd]     # Compact worktree management
```

All other git subcommands pass through unfiltered but tracked.

### Cargo / Rust

```bash
rtk cargo test                # Failures only — ~90% savings
rtk cargo nextest             # Failures only — ~90%
rtk cargo build               # Errors and warnings only — ~80%
rtk cargo check               # Errors and warnings only — ~80%
rtk cargo clippy              # Lint warnings grouped by file — ~80%
```

### JavaScript / TypeScript

```bash
rtk vitest [args...]          # Failures only — ~94-99%
rtk jest [args...]            # Failures only — ~94-99%
rtk tsc [args...]             # Type errors grouped by file — ~75%
rtk lint [args...]            # ESLint — grouped violations — ~84%
rtk prettier [args...]        # Compact format check output
rtk format [args...]          # Universal formatter (auto-detect)
rtk next [args...]            # Next.js build — route summary + errors — ~80%
rtk prisma [subcommand]       # Migration status only — ~75%
rtk playwright [args...]      # Failures + trace links — ~90%
rtk npm [args...]             # npm run — strip boilerplate
rtk npx [args...]             # npx — intelligent routing (tsc, eslint, prisma)
rtk pnpm [subcommand]         # pnpm — ultra-compact
```

### Python

```bash
rtk pytest [args...]          # Failures only — ~80-90%
rtk ruff [args...]            # Violations grouped by file — ~75%
rtk mypy [args...]            # Type errors grouped by file — ~75%
rtk pip [args...]             # Installed packages only — ~70%
rtk uv [args...]              # uv run — preserve uv-managed env semantics
```

### Go

```bash
rtk go test                   # Failures only — ~80-90%
rtk go build                  # Errors only — ~75%
rtk golangci-lint [args...]   # Violations grouped by file — ~75%
```

### Ruby

```bash
rtk rspec [args...]           # Failures only — ~80-90%
rtk rubocop [args...]         # Offenses grouped by file — ~75%
rtk rake [args...]            # Task output, errors highlighted — ~70%
```

### .NET

```bash
rtk dotnet build              # Errors and warnings only — ~80%
rtk dotnet test               # Failures only — ~85-90%
rtk dotnet format             # Changed files only — ~75%
```

### Docker / Kubernetes

```bash
rtk docker ps                 # Essential columns only — ~65%
rtk docker images             # Name + tag + size only — ~60%
rtk docker logs               # Deduplicated, last N lines — ~70%
rtk docker compose up         # Service status, errors highlighted — ~75%
rtk kubectl get pods          # Name + status + restarts only — ~65%
rtk kubectl logs              # Deduplicated entries — ~70%
rtk oc [subcommand]           # OpenShift CLI — compact
```

### Cloud & CLI Tools

```bash
rtk aws <service> [args...]   # JSON condensed, relevant fields only — ~70%
rtk gh <subcmd> [args...]     # GitHub CLI — ~79-87%
rtk glab <subcmd> [args...]   # GitLab CLI — compact
rtk gt <subcommand>           # Graphite (stacked PRs) — ~70-75%
rtk curl [args...]            # Auto-JSON detection — ~60%
rtk wget <url> [options]      # Strip progress bars — ~60%
rtk psql [args...]            # Strip borders, compact tables — ~65%
```

### Other Commands

```bash
rtk err <command>             # Run & show only errors/warnings
rtk test <command>            # Run & show only failures
rtk summary <command>         # Run & show heuristic summary
rtk log [file]                # Filter & deduplicate log output
rtk env [--filter=<name>]     # Show environment variables (filtered)
rtk deps [path]               # Summarize project dependencies
rtk php [args...]             # PHP command runner (artisan, syntax check)
rtk phpunit [args...]         # PHPUnit — compact
rtk phpstan [args...]         # PHPStan — compact
rtk pest [args...]            # Pest — compact
rtk paratest [args...]        # ParaTest — compact
rtk ecs [args...]             # EasyCodingStandard (PHP) — compact
rtk pint [args...]            # Laravel Pint — compact
rtk gradlew [args...]         # Gradle wrapper — compact (build, test, lint)
rtk mvn [args...]             # Maven — compact
```

### Meta Commands

```bash
rtk gain                      # Token savings dashboard
rtk gain --daily              # Daily breakdown
rtk gain --weekly             # Weekly breakdown
rtk gain --monthly            # Monthly breakdown
rtk gain --all                # All breakdowns
rtk gain --graph              # ASCII graph, last 30 days
rtk gain --history            # Last 10 commands
rtk gain --quota              # Monthly quota savings estimate
rtk gain --all --format json  # JSON export
rtk gain --all --format csv   # CSV export
rtk gain --failures           # Show parse failure log

rtk session                   # RTK adoption per Claude Code session
rtk discover                  # Find missed savings opportunities
rtk discover --all --since 7  # Last 7 days, all projects

rtk config                    # Show current configuration
rtk config --create           # Create config with defaults

rtk proxy <command> [args]    # Execute without filtering, track usage
rtk rewrite "<command>"       # Check how a command would be rewritten
rtk run -c "<command>"        # Raw execution (no filtering, no tracking)

rtk pipe [--filter=<name>]    # Unix pipe mode — read stdin, filter, print
rtk hook claude               # Process Claude Code PreToolUse hook (stdin JSON)
rtk hook cursor               # Process Cursor hook (stdin JSON)
rtk hook gemini               # Process Gemini CLI hook (stdin JSON)
rtk hook copilot              # Process Copilot hook (stdin JSON)

rtk trust                     # Trust project-local TOML filters
rtk untrust                   # Revoke filter trust
rtk verify                    # Verify hook integrity & TOML filter tests
rtk telemetry [subcommand]    # Manage telemetry consent
rtk learn                     # CLI corrections from error history
rtk hook-audit                # Hook rewrite audit metrics
```

### Environment Variables

| Variable | Purpose |
|----------|---------|
| `RTK_DISABLED=1` | Disable RTK for a single command |
| `RTK_TEE_DIR=<path>` | Override the tee directory |
| `RTK_TELEMETRY_DISABLED=1` | Disable telemetry |
| `RTK_HOOK_AUDIT=1` | Enable hook audit logging |
| `SKIP_ENV_VALIDATION=1` | Skip env validation (Next.js) |

---

## 5. Filtering Strategies & Savings

### Token Savings (30-min Claude Code Session)

| Operation | Frequency | Standard | rtk | Savings |
|-----------|-----------|----------|-----|---------|
| `ls` / `tree` | 10× | 2,000 | 400 | -80% |
| `cat` / `read` | 20× | 40,000 | 12,000 | -70% |
| `grep` / `rg` | 8× | 16,000 | 3,200 | -80% |
| `git status` | 10× | 3,000 | 600 | -80% |
| `git diff` | 5× | 10,000 | 2,500 | -75% |
| `git log` | 5× | 2,500 | 500 | -80% |
| `git add/commit/push` | 8× | 1,600 | 120 | -92% |
| `cargo test` / `npm test` | 5× | 25,000 | 2,500 | -90% |
| `ruff check` | 3× | 3,000 | 600 | -80% |
| `pytest` | 4× | 8,000 | 800 | -90% |
| `go test` | 3× | 6,000 | 600 | -90% |
| `docker ps` | 3× | 900 | 180 | -80% |
| **Total** | | **~118,000** | **~23,900** | **-80%** |

### Savings by Ecosystem

| Ecosystem | Savings Range |
|-----------|---------------|
| Git | 75–93% |
| Rust (Cargo) | 80–90% |
| JavaScript/TypeScript | 70–99% |
| Python | 70–90% |
| Go | 75–90% |
| Ruby | 60–90% |
| .NET | 70–85% |
| Docker/Kubernetes | 60–75% |
| File commands | 60–80% |
| GitHub CLI | 79–87% |
| Cloud/Data | 60–70% |

### Token Estimation

RTK estimates tokens using `text.len() / 4` (4 characters per token). Accurate to ±10% compared to actual LLM tokenization.

```
Input Tokens  = estimate_tokens(raw_command_output)
Output Tokens = estimate_tokens(rtk_filtered_output)
Saved Tokens  = Input - Output
Savings %     = (Saved / Input) × 100
```

---

## 6. Unrecognized Commands — Passthrough Behavior

### What Happens When a Command Is Not Recognized

RTK is designed to be **always safe** — it never blocks or drops output. The passthrough mechanism works at multiple levels:

#### At the Hook Level (Command Rewriting)

When `rtk rewrite "<command>"` is called, the `rewrite_command()` function in `src/discover/registry.rs` classifies every command:

```
Command input
    ↓
rewrite_command(cmd, excluded)
    ↓
    ├─ Empty command → None (passthrough)
    ├─ Contains heredoc (<<) or $(( → None (passthrough)
    ├─ Already "rtk ..." → return as-is
    ├─ Single command → rewrite_segment_inner()
    │       ↓
    │    ├─ Known command → return "rtk <command>"
    │    ├─ Unsupported (no match) → None (passthrough)
    │    └─ Ignored → None (passthrough)
    └─ Compound (&&, ||, |, ;) → rewrite_compound()
            ↓
         Rewrites each segment individually
         Pipe → rewrites left side only
         Unknown command in segment → leaves raw
```

**Outcomes:**
- **Recognized command** → exit 0, prints `rtk <rewritten_command>`
- **Unrecognized command** → exit 1, **no output** → hook exits silently, original command runs unchanged
- **Command on exclude list** → exit 2, prints "excluded" message
- **Command with `RTK_DISABLED=1` prefix** → prints skip message, returns None → passthrough

#### At the Binary Level (Direct Execution)

When you run `rtk <command>` directly and the command isn't a known subcommand, `main.rs` handles it via `run_fallback()`:

```rust
// Phase 1: Check if it's an RTK meta-command
if RTK_META_COMMANDS.contains(&args[0]) {
    parse_error.exit();  // Unknown subcommand → show Clap error
}

// Phase 2: Try TOML filter lookup
let toml_match = toml_filter::find_matching_filter(&lookup_cmd);

// Phase 3a: TOML match found → capture stdout, apply TOML filter, output
if let Some(filter) = toml_match { ... }

// Phase 3b: No TOML match → passthrough with Stdio::inherit
else {
    // Directly execute the original command with inherited streams
    // No capturing, no filtering — as if RTK wasn't there
    resolved_command(&args[0])
        .args(&args[1..])
        .stdin(Stdio::inherit())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .status();
}
```

**Key behaviors:**
- **No TOML match** → **passthrough**: the underlying command is spawned directly with stdin/stdout/stderr inherited. It runs exactly as if RTK weren't involved.
- **Command not found** → exit code 127, error message `[rtk: <error>]`
- **Filter error** → falls back to raw output, never truncates silently
- **Exit codes are preserved** — RTK never swallows non-zero exits
- **Usage is always tracked** — even passthrough commands are recorded in the SQLite database

#### Safety Guarantees

1. **Never blocks** — all unknown commands pass through unchanged
2. **Never loses output** — if filtering fails, raw output is shown
3. **Never changes exit codes** — CI/CD pipelines work correctly
4. **Always tracks** — even passthrough commands appear in `rtk gain --history`
5. **Silent failure** — if the hook script itself fails (jq missing, rtk not found), the command runs raw without any error to the agent

---

## 7. Compatibility Matrix

### Platform Support

| Platform | CLI | Full Hook (Auto-Rewrite) | Notes |
|----------|-----|-------------------------|-------|
| Linux | ✅ Full | ✅ Full | All agents supported |
| macOS (Intel) | ✅ Full | ✅ Full | Homebrew available |
| macOS (Apple Silicon) | ✅ Full | ✅ Full | Native arm64 binary |
| Windows (native) | ✅ CLI works | ❌ Falls back to CLAUDE.md mode | No shell hook; agent must manually use `rtk <cmd>` |
| Windows (WSL) | ✅ Full | ✅ Full | Full hook support inside WSL |

### Agent Support

| Agent | Mechanism | Auto-Rewrite | Notes |
|-------|-----------|--------------|-------|
| **Claude Code** | Shell hook (`PreToolUse`) | ✅ Yes | Default target for `rtk init -g` |
| **VS Code Copilot Chat** | Rust binary hook | ✅ Yes | Via `rtk hook copilot` |
| **GitHub Copilot CLI** | Rust binary hook | ✅ Deny-with-suggestion | Agent retries with suggested command |
| **Cursor** | Shell hook | ✅ Yes | Via `rtk init --agent cursor` |
| **Gemini CLI** | Rust binary hook | ✅ Yes | Via `rtk init -g --gemini` |
| **Cline / Roo Code** | Rules file (`.clinerules`) | ⚠️ Prompt-level guidance | Not automatic |
| **Windsurf** | Rules file (`.windsurfrules`) | ⚠️ Prompt-level guidance | Not automatic |
| **Codex CLI** | AGENTS.md / instructions | ⚠️ Prompt-level guidance | Not automatic |
| **OpenCode** | TypeScript plugin | ✅ Yes | `tool.execute.before` event |
| **Pi** | TypeScript extension | ✅ Yes | `tool_call` event |
| **Hermes** | Python plugin | ✅ Yes | `pre_tool_call` hook |
| **Factory Droid** | Shell hook | ✅ Yes | Via `rtk init --agent droid` |

### Command Ecosystem Coverage

| Category | Count | Examples |
|----------|-------|---------|
| Git operations | 13+ | status, log, diff, show, add, commit, push, pull, branch, fetch, stash, worktree, **all others passthrough** |
| Rust/Cargo | 5+ | test, nextest, build, check, clippy, **all others passthrough** |
| JavaScript/TypeScript | 10+ | vitest, jest, tsc, eslint, prettier, next, prisma, playwright, npm, npx, pnpm |
| Python | 5+ | pytest, ruff, mypy, pip, uv |
| Go | 3+ | go test, go build, golangci-lint |
| Ruby | 3+ | rspec, rubocop, rake |
| .NET | 3+ | dotnet build/test/format |
| PHP | 5+ | php, phpunit, phpstan, pest, paratest, ecs, pint |
| Docker/K8s | 6+ | docker, compose, kubectl, oc |
| Cloud/CLI | 6+ | aws, gh, glab, gt, curl, wget |
| File/Search | 9+ | ls, tree, read, smart, find, grep, rg, diff, wc, json, log, env, deps |
| Android | 2+ | gradlew, mvn |
| **Total** | **100+ commands** | |

---

## 8. Integration with Kimix Tools

This section explains how RTK can be used with the three execution tools in the current project (`C:\dev\kimi-agent`).

### 8.1 Overview of Kimix Tools

The project has three tools that execute commands:

| Tool File | Class | How It Executes |
|-----------|-------|-----------------|
| `src/kimix/tools/file/bash/bash_tool.py` | `Bash` | Spawns `bash -c "<cmd>"` via `ProcessTask` |
| `src/kimix/tools/file/bash/pwsh_tool.py` | `Powershell` | Spawns `pwsh -NoP -NonI -C "<cmd>"` via `ProcessTask` |
| `src/kimix/tools/file/run.py` | `Run` | Spawns `Popen([executable, ...args])` directly — **no shell** |

### 8.2 Using RTK with the `Bash` Tool

**How it works:** The Bash tool runs `bash -c "<command>"`. It supports shell syntax (pipes, redirects, `&&`, variables, etc.).

**Integration patterns:**

#### Pattern A: Agent prepends `rtk` explicitly (recommended)

The agent (you or an LLM) wraps recognized commands with `rtk` before passing them to the Bash tool:

```python
# Instead of:
Bash(cmd="cargo test")

# Use:
Bash(cmd="rtk cargo test")
```

When executed, bash runs: `bash -c "rtk cargo test"` — RTK intercepts, runs `cargo test`, filters output, returns compact result.

**Compound commands** also work because RTK's rewrite engine handles `&&`, `||`, `;`, and `|`:

```python
Bash(cmd="rtk cargo test -- --nocapture && rtk cargo fmt -- --check")
```

The Bash tool is the **best choice** for RTK integration when you need:
- Shell syntax (pipes, redirects, `&&`)
- Interactive sessions (the Bash tool supports `interactive=True` with persistent sessions)
- Compound commands

#### Pattern B: Direct execution without shell overhead

For simple commands, you can skip the shell entirely using the Run tool (see §8.4).

### 8.3 Using RTK with the `Powershell` Tool

**How it works:** The Powershell tool spawns `pwsh -NoP -NonI -C "<command>"`. On Windows, it also runs PowerShell syntax through a compatibility transformer (`proccess_pwsh.py`) that converts PS7-specific syntax to PS5.1-compatible syntax.

**Integration patterns:**

#### Pattern A: Agent prepends `rtk` explicitly

```python
# Instead of:
Powershell(cmd="git status")

# Use:
Powershell(cmd="rtk git status")
```

RTK commands are simple process invocations (`rtk.exe` on Windows) — they don't use PowerShell-specific syntax, so they pass through the `proccess_pwsh.py` transformer unchanged.

#### Pattern B: On Windows native (no hook)

On Windows native, the Claude Code hook doesn't work (no shell-level auto-rewrite). The agent must **always explicitly** prepend `rtk` to commands when using the Powershell tool on Windows. This is the main integration point for Windows users.

#### Interacting with PS sessions

For interactive PowerShell sessions (`interactive=True`), you can send RTK commands to the running session:

```python
# Start interactive session
result = Powershell(interactive=True)
task_id = result.task_id

# Send RTK commands to the session
Powershell(task_id=task_id, cmd="rtk git status")
Powershell(task_id=task_id, cmd="rtk cargo test")
```

### 8.4 Using RTK with the `Run` Tool

**How it works:** The Run tool spawns executables **directly** — no shell involved:

```python
# Equivalent to: subprocess.Popen(["git", "status"])
Run(command="git status")

# Equivalent to: subprocess.Popen(["python", "-c", "print(1)"])
Run(command='python -c "print(1)"')
```

**This is the most natural fit for RTK** because:
1. RTK is itself an executable (`rtk.exe` / `rtk`) — the Run tool resolves it via `shutil.which()`
2. RTK commands are pure process invocations with no shell syntax needed
3. The Run tool already supports background tasks, streaming, and session management

**Integration patterns:**

#### Pattern A: Direct process call (recommended for simple commands)

```python
# Instead of:
Run(command="git status")

# Use:
Run(command="rtk git status")  
# → Spawns: rtk with args ["git", "status"]
# → RTK routes to git filter, returns compact output
```

This is the **cleanest** integration — no shell wrapping, no quoting issues. RTK handles the routing internally.

#### Pattern B: Background tasks

```python
# Run RTK-filtered build in background
result = Run(command="rtk cargo build", run_in_background=True, timeout=120)
task_id = result.task_id

# Later, get output
from TaskOutput import TaskOutput
output = TaskOutput(task_id=task_id)
```

#### Pattern C: Session continuation

```python
# Start long-running RTK process
result = Run(command="rtk cargo test", run_in_background=True)

# Send input if needed (e.g., for interactive test runners)
Run(task_id=result.task_id, command="n")
```

#### Limitations with the Run Tool

The Run tool **does not support shell syntax** — no pipes, redirects, `&&`, `||`, or variables:

```python
# ❌ These will NOT work with Run:
Run(command="rtk cargo test | grep failure")    # Pipe not supported
Run(command="rtk cargo test && rtk cargo fmt")  # && not supported
Run(command="rtk ls $HOME")                     # Variable not expanded

# ✅ Use Bash tool for these:
Bash(cmd="rtk cargo test | grep failure")
Bash(cmd="rtk cargo test && rtk cargo fmt")
```

### 8.5 Choosing the Right Tool

| Scenario | Recommended Tool | Example |
|----------|----------------|---------|
| Simple command (no shell syntax) | `Run` | `Run(command="rtk git status")` |
| Compound commands (`&&`, `\|\|`, `;`) | `Bash` | `Bash(cmd="rtk cargo test && rtk cargo fmt")` |
| Pipes (`\|`) | `Bash` | `Bash(cmd="rtk ls \| head -5")` |
| Interactive session | `Bash` or `Powershell` | `Bash(interactive=True)` |
| Windows native (no hook) | `Powershell` | `Powershell(cmd="rtk git status")` |
| Background task | `Run` | `Run(command="rtk cargo build", run_in_background=True)` |
| Long timeout | any | `Run(command="rtk cargo test", timeout=300)` |

### 8.6 File Operations with RTK

The Kimix tools have built-in `Grep`, `Glob`, and `Read` operations that don't go through Bash. RTK's `rtk grep`, `rtk find`, and `rtk read` can be used as **alternatives** for more compact output:

```python
# Instead of the built-in grep (which returns full lines):
Run(command="rtk grep 'fn run' src/")
# → Returns grouped results with truncated lines (~80% savings)

# Instead of Glob for file listing:
Run(command="rtk find '*.py' src/")
# → Returns grouped tree format

# Instead of Read for a quick scan:
Run(command="rtk read src/main.py -l aggressive")
# → Returns function signatures only (~74% savings)
```

### 8.7 Practical Workflow Examples

#### Development Loop

```python
# 1. Check status
cmd = "rtk git status"
result = Run(command=cmd)

# 2. Build
cmd = "rtk cargo build"
result = Run(command=cmd, timeout=120)

# 3. Test
cmd = "rtk cargo test"
result = Run(command=cmd, timeout=120)

# 4. Check lint
cmd = "rtk cargo clippy"
result = Run(command=cmd, timeout=60)

# 5. Commit
cmd = 'rtk git commit -m "Fix bug"'
result = Run(command=cmd)

# 6. Push
cmd = "rtk git push"
result = Run(command=cmd)
```

#### Batch Operations (via Bash)

```python
# Format + check + test in sequence
Bash(cmd="rtk cargo fmt -- --check && rtk cargo clippy && rtk cargo test")

# Or run in parallel using background tasks
task1 = Run(command="rtk cargo fmt -- --check", run_in_background=True)
task2 = Run(command="rtk cargo clippy", run_in_background=True)
task3 = Run(command="rtk cargo test", run_in_background=True)
```

#### Analytics

```python
# Check token savings
Run(command="rtk gain")

# Find missed opportunities
Run(command="rtk discover")

# Export for dashboard
Run(command="rtk gain --all --format json")

# Check configuration
Run(command="rtk config")
```

### 8.8 Understanding Passthrough with Kimix Tools

When using RTK through any of the three tools, the passthrough behavior means:

```python
# Even if RTK doesn't recognize a command, it's safe:
Run(command="rtk unusual-tool --flag value")
# → RTK runs "unusual-tool --flag value" with inherited streams
# → Output is raw/unfiltered (no token savings, but no loss either)
# → Usage is tracked in SQLite

# To explicitly bypass RTK (tracking only):
Run(command="rtk proxy some-command --args")
# → Runs "some-command --args" without any filtering
# → Still tracked for analytics
```

---

## 9. Configuration & Customization

### Config File

| Platform | Path |
|----------|------|
| Linux | `~/.config/rtk/config.toml` |
| macOS | `~/Library/Application Support/rtk/config.toml` |
| Windows | `%APPDATA%\rtk\config.toml` |

```bash
rtk config            # Show current configuration
rtk config --create   # Create config file with defaults
```

### Full Config Structure

```toml
[tracking]
enabled = true              # Enable/disable token tracking
history_days = 90           # Retention in days (auto-cleanup)
database_path = "/custom/path/history.db"

[display]
colors = true               # Colored output
emoji = true                # Use emojis in output
max_width = 120             # Maximum output width

[filters]
ignore_dirs = [".git", "node_modules", "target", "__pycache__", ".venv", "vendor"]
ignore_files = ["*.lock", "*.min.js", "*.min.css"]

[tee]
enabled = true              # Save raw output on failure
mode = "failures"           # "failures" (default), "always", "never"
max_files = 20              # Rotation: keep last N files

[telemetry]
enabled = true              # Anonymous daily ping

[hooks]
exclude_commands = []       # Commands to never auto-rewrite
```

### Custom TOML Filters

Add your own filters (or override built-ins) in:

- **Project-local**: `.rtk/filters.toml` in your project root (committed with the repo)
- **User-global**: `~/.config/rtk/filters.toml` (applies to every project)

Custom filters must be **trusted** before they take effect:

```bash
rtk trust       # Review and trust filters
rtk untrust     # Revoke trust
```

Trust is tied to the file's SHA-256 hash — editing a trusted file requires re-trusting.

### Excluding Commands from Auto-Rewrite

```toml
[hooks]
exclude_commands = ["git rebase", "git cherry-pick", "docker exec"]
```

Or for a single invocation:

```bash
RTK_DISABLED=1 git rebase main
```

---

## 10. Analytics & Tracking

### Database

RTK stores all tracking data in a local SQLite database:

| Platform | Path |
|----------|------|
| Linux | `~/.local/share/rtk/history.db` |
| macOS | `~/Library/Application Support/rtk/history.db` |
| Windows | `%APPDATA%\rtk\history.db` |

- **Retention**: 90 days (automatic cleanup)
- **Scope**: Global across all projects and sessions
- **Schema**: commands table with timestamp, original_cmd, rtk_cmd, input_tokens, output_tokens, exec_time_ms

### Commands

```bash
rtk gain                          # Default summary
rtk gain --daily                  # All days
rtk gain --weekly                 # Weekly aggregation
rtk gain --monthly                # Monthly aggregation
rtk gain --all                    # All breakdowns
rtk gain --graph                  # ASCII graph, last 30 days
rtk gain --history                # Last 10 commands
rtk gain --quota                  # Monthly quota estimate
rtk gain --all --format json      # JSON export
rtk gain --all --format csv       # CSV export
rtk gain --failures               # Parse failure log

rtk discover                      # Find missed savings
rtk session                       # Adoption tracking
```

### Quota Estimate

```bash
rtk gain --quota                    # Default 20× tier
rtk gain --quota -t pro             # Claude Pro plan
rtk gain --quota -t 5x              # 5× usage plan
rtk gain --quota -t 20x             # 20× usage plan
```

### Data Export Example (JSON)

```json
{
  "summary": {
    "total_commands": 196,
    "total_input": 1276098,
    "total_output": 59244,
    "total_saved": 1220217,
    "avg_savings_pct": 95.62
  },
  "daily": [...],
  "weekly": [...],
  "monthly": [...]
}
```

---

## 11. Troubleshooting

### Symptom: `rtk gain` says "not a rtk command"

**Cause:** You installed **Rust Type Kit** instead of **Rust Token Killer**.

**Fix:**
```bash
cargo uninstall rtk
curl -fsSL https://raw.githubusercontent.com/rtk-ai/rtk/master/install.sh | sh
rtk gain  # Should now show token savings stats
```

### Symptom: AI assistant not using RTK

**Checklist:**
1. Verify RTK is installed: `rtk --version && rtk gain`
2. Initialize the hook: `rtk init -g`
3. Restart your AI assistant
4. Verify hook status: `rtk init --show`
5. Check `settings.json`: `cat ~/.claude/settings.json | grep rtk`

### Symptom: RTK not found after `cargo install`

**Cause:** `~/.cargo/bin` not in PATH.

**Fix:**
```bash
# Add to ~/.bashrc or ~/.zshrc
export PATH="$HOME/.cargo/bin:$PATH"
source ~/.zshrc
rtk --version
```

### Symptom: Double-clicking `rtk.exe` on Windows does nothing

**Cause:** RTK is a command-line tool — with no arguments, it prints usage and exits.

**Fix:** Open a terminal first, then run `rtk --version`.

### Symptom: Hook not working on Windows (no auto-rewrite)

**Cause:** Auto-rewrite hook requires a Unix shell. Native Windows doesn't have one.

**Fix:** Use WSL for full hook support, or use RTK manually on native Windows (agent must explicitly call `rtk <cmd>`).

### Symptom: Node.js tools not found on Windows

**Fix:** Update to RTK v0.23.1+ which handles `.CMD`/`.BAT` wrappers.

### Symptom: Compilation error

**Fix:**
```bash
rustup update stable
rustup default stable
cargo clean
cargo build --release
```

Minimum Rust version: 1.70+.

### Diagnostic Script

```bash
# From the RTK repository root
bash scripts/check-installation.sh
```

Checks:
- RTK installed and in PATH
- Correct version (Token Killer, not Type Kit)
- Available features
- Claude Code integration
- Hook status

---

## Appendix: RTK Source Code Layout

```
C:\dev\rtk\
├── src/
│   ├── main.rs              # CLI entry point, Commands enum, routing
│   ├── cmds/                # Command filter modules by ecosystem
│   │   ├── git/             # Git filters (status, log, diff, etc.)
│   │   ├── rust/            # Cargo filters (test, build, clippy)
│   │   ├── js/              # JS/TS filters (vitest, jest, tsc, next, etc.)
│   │   ├── python/          # Python filters (pytest, ruff, mypy)
│   │   ├── go/              # Go filters (test, build, golangci-lint)
│   │   ├── ruby/            # Ruby filters (rspec, rubocop, rake)
│   │   ├── dotnet/          # .NET filters (build, test, format)
│   │   ├── cloud/           # Docker/K8s/aws filters
│   │   ├── php/             # PHP filters (phpunit, phpstan, pint)
│   │   ├── system/          # File commands (ls, read, find, grep, etc.)
│   │   └── jvm/             # JVM filters (gradlew, mvn)
│   ├── core/                # Shared infrastructure
│   │   ├── filter.rs        # Filter pipeline
│   │   ├── tracking.rs      # SQLite token tracking
│   │   ├── tee.rs           # Tee system for failure output
│   │   ├── config.rs        # Configuration management
│   │   ├── toml_filter.rs   # TOML DSL filter engine
│   │   └── runner.rs        # Output emission
│   ├── hooks/               # Hook installation/management
│   │   ├── init.rs          # Hook setup for all agents
│   │   ├── rewrite_cmd.rs   # Command rewrite for hooks
│   │   └── integrity.rs     # Hook integrity verification
│   ├── discover/            # Command registry
│   │   └── registry.rs      # Rewrite patterns (70+ commands)
│   └── analytics/           # Analytics commands
│       ├── gain.rs          # Token savings dashboard
│       └── session_cmd.rs   # Session adoption tracking
├── hooks/                   # Deployed hook artifacts
│   ├── claude/              # Claude Code shell hook
│   ├── cursor/              # Cursor shell hook
│   ├── copilot/             # Copilot Rust binary hook
│   ├── cline/               # Cline rules file
│   ├── windsurf/            # Windsurf rules file
│   ├── codex/               # Codex awareness document
│   ├── opencode/            # OpenCode TS plugin
│   ├── pi/                  # Pi TS extension
│   └── hermes/              # Hermes Python plugin
├── docs/                    # Documentation
│   ├── guide/               # User guide
│   ├── usage/               # Usage docs
│   └── contributing/        # Contributor docs
└── tests/                   # Test fixtures
```
