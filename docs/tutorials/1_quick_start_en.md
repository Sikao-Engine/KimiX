# Kimix Quick Start

This guide covers environment setup, installation, and basic CLI usage.

---

## Quick Install

```bash
pip install kimix
python -m kimix.cli
# or
python -m kimix
```

For source-based development, see the detailed steps below.

---

## Git Submodules

Kimix uses Git submodules for some dependencies.

**Clone with submodules:**
```bash
git clone --recursive <repo-url>
```

**Update existing clone:**
```bash
uv run clone_submodule.py
# or manually:
git submodule update --init --recursive
```

---

## Install with uv

Recommended: use [uv](https://docs.astral.sh/uv/) for Python environment management.

```bash
cd /path/to/kimix
uv tool install -e .
uv run kimix
```

- `-e .` installs in editable mode (changes reflect without reinstall)
- `uv run kimix` uses uv's managed environment automatically

---

## Environment Variables

Configure API keys before running. Priority: JSON config `api_key` field > `KIMI_API_KEY` > `KIMIX_API_KEY`.

| Variable | Description |
|----------|-------------|
| `KIMI_API_KEY` | Kimi API key |
| `KIMIX_API_KEY` | Fallback API key |

**Linux / macOS:**
```bash
export KIMI_API_KEY=your-api-key
```

**Windows PowerShell:**
```powershell
$env:KIMI_API_KEY="your-api-key"
```

---

## CLI Usage

### Subcommands

| Subcommand | Description | Common Options |
|------------|-------------|----------------|
| `serve` | Start HTTP server (OpenCode style) | `--host` (default `127.0.0.1`), `--port` (default `4096`) |
| `ssecli` | SSE CLI debugger for `kimix serve` | `--host`, `--port`, `--debug` (saves raw SSE events to `sse_log_<timestamp>.txt`) |

**Examples:**
```bash
uv run kimix serve --port 4096
uv run kimix ssecli --host 127.0.0.1 --port 4096 --debug
```

### LLM Config Initialization

If no `--config` is provided, the built-in default (`src/kimix/default_config.json`) is used.

Run `/init` in the interactive terminal to create the default config interactively:
```
/init
```

**Config fields:**

| Field | Required | Description |
|-------|----------|-------------|
| `type` | Yes | Provider: `kimi`, `openai_legacy`, `openai_responses`, `anthropic`, `google_genai`, `gemini`, `vertexai` |
| `model` | Yes | Model name for API requests |
| `url` | Yes | API base URL |
| `max_context_size` | Yes | Max context length (`128k`, `200k`, `256k`, `512k`, `1M`) |
| `model_name` | No | Model alias (default `unknown_model`) |
| `name` | No | Provider name (default `unknown`) |
| `capabilities` | No | Model capabilities: `thinking`, `always_thinking`, `image_in`, `video_in` |
| `api_key` | No | API key (falls back to env vars) |
| `custom_headers` | No | Custom HTTP headers |
| `oauth` | No | OAuth config, e.g. `{"storage": "file", "key": "my-key"}` |
| `loop_control` | No | Loop params: `max_steps_per_turn`, `max_retries_per_step`, `max_ralph_iterations`, `reserved_context_size`, `compaction_trigger_ratio` |
| `max_tokens` | No | Max tokens per request |
| `show_thinking_stream` | No | Stream thinking process |
| `thinking_effort` | No | `off`, `low`, `medium`, `high`, `xhigh`, `max` |
| `temperature` | No | Sampling temperature `[0.0, 2.0]` |
| `background` | No | Background task settings |
| `notifications` | No | Notification settings |
| `mcp` | No | MCP (Model Context Protocol) config |
| `env` | No | Extra env vars (dict) |
| `sub_provider` | No | Sub-agent provider config (same structure, `loop_control.max_ralph_iterations` fixed to `0`) |

Load custom config:
```bash
uv run kimix --config <path>
```

### Launch Options

| Flag | Description |
|------|-------------|
| `-c`, `--clean` | Auto-delete cache on exit |
| `--no_think` | Disable thinking mode |
| `--no_yolo` | Disable YOLO mode |
| `--no_color` | Disable colored output |
| `--manually-cot` | Enable manual CoT (may use multiple sessions) |
| `--ralph` | Enable Ralph mode (optional iteration count) |
| `--supervisor` | Use Supervisor role instead of default Worker |
| `-s`, `--skill-dir` | Custom skill directory (repeatable) |
| `--config` | JSON config path. Searches: cwd parents, package parents, `PATH` |

### Interactive Commands

| Command | Description |
|---------|-------------|
| `<path>` | Load file. Non-`.py` parsed as multi-line prompt; `.py` executed directly |
| `/file:<path>` | Read entire file as a single prompt |
| `/todo:<path>` | Scan code file for TODO comments and prompt agent to implement each one |
| `/clear` | Clear current context |
| `/summarize` | Summarize context to memory |
| `/exit` | Exit |
| `/help` | Show help |
| `/context` | Print context usage |
| `/fix:<command>` | Run command, auto-retry on error |
| `/txt` | Multi-line text mode (end with `/end`, cancel with `/cancel`) |
| `/init` | Interactive LLM config initialization (resets session) |
| `/compact` | Compact conversation context |
| `/export:<path>` | Export session messages to file |
| `/resume:<id>` | Close current session and resume a session by ID |
| `/rename:<id>` | Rename the current session to a new ID |
| `/swarm` | Multi-agent swarm execution (end with `/end`, cancel with `/cancel`) |
| `/ralph:on/off/<num>` | Set Ralph mode |
| `/supervisor:on/off` | Toggle Supervisor mode (rebuilds session) |
| `/cot:on/off` | Toggle manual CoT mode |
| `/plan` | Generate task plan with TodoMaker Agent (supports `/plan:<file>`) |
| `/script` | Write and execute Python script (end with `/end`) |
| `/cmd:<command>` | Execute system command |
| `/cd:<path>` | Change working directory (resets skills and clears context) |
