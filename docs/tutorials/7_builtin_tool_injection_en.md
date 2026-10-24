# Tutorial 7 — Built-in Tool Injection: Extending File Tools via Session Servers

This tutorial explains how KimiX's built-in file tools (`read`, `write`, `edit`)
can be **extended from the workspace itself** — no changes to `kimi-cli` required.
The mechanism is a small injection channel: a per-session JSON-RPC sub-process
("session server", declared in `.kimix/config.json`) gets asked for extra
information *before* every file-tool call, and its reply is appended to the
tool result the model sees.

The reference implementation used throughout is `tools/kimix_lsp.py`, a
pure-stdlib server that injects Python syntax diagnostics into `write`/`edit`
results.

Related contracts and sources:

| Document / source | Role |
|---|---|
| [`docs/tool_connect_bridge.md`](../tool_connect_bridge.md) | The `connect` bridge contract (payload, extraction, caps) |
| [`docs/session_server_rpc.md`](../session_server_rpc.md) | JSON-RPC 2.0 wire protocol the servers speak |
| [`src/kimi_agent_sdk/_server_process.py`](../../src/kimi_agent_sdk/_server_process.py) | Parses `server`, spawns children, live registry |
| [`kimi-cli/src/kimi_cli/tools/file/session_bridge.py`](../../kimi-cli/src/kimi_cli/tools/file/session_bridge.py) | Client half: `before_tool()` + `merge_message()` |
| [`kimi-cli/src/kimi_cli/session_server.py`](../../kimi-cli/src/kimi_cli/session_server.py) | Default server + builtin echo `before_tool` |
| [`tools/kimix_lsp.py`](../../tools/kimix_lsp.py) | Example workspace server (LSP-style syntax check) |

---

## 1. What the injection looks like

When a file tool runs and a session server is live, the model sees something
like:

```
Wrote 3412 bytes to src/app.py.

[server] src/app.py:44:9: SyntaxError: unmatched ')'
```

The `[server] ...` line is **not** produced by the tool. It is the reply of the
workspace's session server, fetched before the tool executed and merged into
the tool result's `message`. The primary use case: a project-provided LSP
bridge returns diagnostics / symbol information alongside reads and edits, so
the model sees them without a separate round trip.

Two design rules govern the whole channel:

1. **Enhancement only.** The server can *add information*; it can never change,
   block, or fail the underlying tool call. Every failure mode (import cycle,
   empty registry, connection refused, timeout, JSON-RPC error, malformed
   result) is swallowed and degrades to "no extra information".
2. **Opt-out per call.** Every wired tool has a `connect: bool = True`
   parameter; passing `connect=False` skips the RPC entirely.

## 2. Architecture: five moving parts

```
.kimix/config.json ──► _server_process.py ──► live registry ──► session_bridge ──► file tools
 "server": [entry]      spawn + <port>         get_live_          before_tool()      read
                        substitution           server_processes() merge_message()    write
                                              (kimi_agent_sdk)                      edit
                                                   ▲                                     │
                                        tools/kimix_lsp.py ◄── JSON-RPC 2.0 ────────────┘
                                        (session server child process)
```

### 2.1 Declaring a server: `.kimix/config.json` → `server`

A workspace customizes tool behavior by placing a config at
`<work_dir>/.kimix/config.json`. The `server` element is a list of session
server entries; this workspace ships one:

```json
"server": [
  {
    "cmd": "python",
    "args": [
      "tools/kimix_lsp.py",
      "--network", "tcp",
      "--host", "<address>",
      "--port", "<port>"
    ],
    "cwd": ".",
    "network": "tcp"
  }
]
```

The SDK (`kimi_agent_sdk._server_process`) handles the lifecycle:

* When a session starts, **each entry is spawned as a child process** via
  `asyncio.create_subprocess_exec` with `cwd` resolved against the work dir.
* **The client decides the address.** Before spawning, a free TCP port is
  allocated (an entry's `port` is only a *preferred starting port*, probed
  upward on conflict). The tokens `<address>`, `<port>` and `<session_id>`
  are substituted into `cmd`/`args` — a missing `session_id` becomes the
  literal string `"default"`. The child therefore learns where to listen from
  its own command line.
* The process is considered ready as soon as it is spawned; entries declaring
  a legacy `config` key are rejected at parse time.
* Every spawned server registers itself in the module-level **live registry**
  (`get_live_server_processes()`) and is unregistered when session servers
  stop — including cancellation cleanup, so no orphaned children remain.

### 2.2 The wire protocol: JSON-RPC 2.0

Session servers speak the protocol of `docs/session_server_rpc.md` — newline
delimited JSON-RPC over TCP (or `POST /rpc` over HTTP). `tools/kimix_lsp.py`
implements the builtin method set (`ping`, `echo`, `read_file`, `write_file`,
`add`) with the reference wire semantics, plus the method that matters here:

```json
{"jsonrpc": "2.0", "id": 1, "method": "before_tool",
 "params": {"tool": "write", "action": "write",
            "path": "src/app.py", "paths": null, "cwd": "D:\\work"}}
```

→

```json
{"jsonrpc": "2.0", "id": 1,
 "result": {"message": "src/app.py:44:9: SyntaxError: unmatched ')'",
            "tool": "write", "action": "write", "handled": true}}
```

Errors follow the standard JSON-RPC codes (`-32700` parse, `-32600` invalid
request, `-32601` method not found, `-32602` invalid params, `-32603`
internal). Any process speaking this protocol can act as a session server —
that is why `kimix_lsp.py` is deliberately pure stdlib (see §4).

### 2.3 The client half: `session_bridge.py`

`kimi_cli/tools/file/session_bridge.py` exposes two functions:

* **`before_tool(tool_name, action, *, path, paths, cwd, timeout=5.0)`** —
  snapshots the SDK live-server registry, filters to entries that are
  `ready` with a running process and a `tcp`/`http` network, then issues one
  `before_tool` JSON-RPC call per server. Payload keys are always all five of
  `{tool, action, path, paths, cwd}` (`null` when unset). Rule servers can
  rely on: **`path` is the primary/first target; `paths` is the full list and
  is only non-`null` when the tool received several targets.** Paths are
  forwarded **raw and unresolved** — exactly what the caller passed (glob
  patterns, relative paths, URL-ish values included); the server interprets
  them.
* **`merge_message(existing, info)`** — appends the reply to the tool result:
  `info` empty → `existing` byte-identical; otherwise a blank line plus a
  `[server] <info>` marker line.

The reply extraction (contract §5):

| Result type | Extracted text |
|---|---|
| `str` | as-is |
| `dict` | its `message` field (string), else the compact JSON of the whole dict |
| anything else | compact JSON |

Extracted text is whitespace-stripped and capped at **4096 characters**
(`MAX_APPEND_CHARS`). The whole round trip per server is bounded by a
**5-second** budget (`DEFAULT_TIMEOUT`, applied to connect *and* to the full
call so a dribbling server cannot stack per-read timeouts).

**Failure isolation** is the central invariant: `before_tool` *never raises*.
The SDK registry import is lazy (avoiding the `kimix → kimi_agent_sdk →
kimi_cli` import cycle); a missing SDK means "no servers, no info". Each
server query is independently wrapped, and an outer catch-all reports `None`
so the tool proceeds exactly as if no server existed.

### 2.4 The tool half: a thin `__call__` wrapper

The injection is wired into the tools as a decorator-free wrapper:
`__call__` runs the bridge, then `_call_core` runs the real logic, then the
messages merge. All three file tools share the identical shape
(shown here for `write`):

```python
async def __call__(self, params: Params) -> ToolReturnValue:
    server_info: str | None = None
    if getattr(params, "connect", False):
        try:
            work_dir = getattr(self, "_work_dir", None)
            server_info = await before_tool(
                "write", "write",
                path=params.file_path,
                paths=None,
                cwd=str(work_dir) if work_dir is not None else None,
            )
        except Exception:
            server_info = None
    ret = await self._call_core(params)
    if server_info:
        ret = ret.model_copy(update={"message": merge_message(ret.message, server_info)})
    return ret
```

Per-tool wiring (see `docs/tool_connect_bridge.md` §2):

| Tool | `tool`/`action` sent | Paths sent |
|---|---|---|
| `read` (`read.py`) | `read` / `read` | `params.file_path`: `str` → `path`; `list[str]` → `paths` (all) and `path` = first |
| `write` (`write.py`) | `write` / `write` | `path` = `params.file_path`, `paths` = `null` |
| `edit` (`edit/__init__.py`) | `edit` / `edit` | `path` = `params.file_path` (nullable → `null` when absent), `paths` = `null` |
| `read_image` (`read_media.py`) | `read` / `read` | `path` = `params.file_path`, `paths` = `null` |

Note on `replace.py`: it is a **backward-compatible shim** that re-exports
`EditFile` / `Params` / `Edit` from `kimi_cli.tools.file.edit`. The injection
lives in the edit package, so `edit` mode tools get it automatically; nothing
server-side distinguishes "replace" from other edit modes — the `action` is
always the canonical `edit`.

Because the wrapper only *reads* the result message, the bridge cannot
corrupt tool semantics: approvals, diffs, guards (protected paths,
auto-generated files, conflict markers) all run untouched inside
`_call_core`.

## 3. The default server and its echo hook

`kimi_cli.session_server` ships a default `before_tool` implementation
registered on `DEFAULT_REGISTRY`:

```python
@DEFAULT_REGISTRY.method("before_tool", params_model=BeforeToolParams)
def _before_tool(params: BeforeToolParams) -> BeforeToolResult:
    """Default echo ... pure sync, no file IO."""
    message = f"ack before_tool: tool={params.tool} action={params.action}"
    ...
```

It only "repeats" the call back so the round trip is provable end to end.
External servers — an LSP bridge, for example — **replace this method with
real diagnostics**. That single sentence is the whole injection design: the
hook is a stable extension point, and workspaces override it by shipping
their own server process.

## 4. Reference implementation: `tools/kimix_lsp.py`

`kimix_lsp.py` is a standalone JSON-RPC 2.0 session server — pure stdlib, no
`kimi_cli` import — following the reference pattern of
`tests/dummy_rpc_server.py`. It implements the builtin method set but
replaces the `before_tool` echo with a **Python syntax check**.

Why standalone: the SDK spawns the entry's `cmd` directly, and on Windows a
bare `python` resolves to the system interpreter — not necessarily the
environment that has `kimi_cli` installed. Depending only on the standard
library makes the `.kimix/config.json` entry work under any interpreter.
(That is also why it uses stdlib `json` despite the project's orjson rule.)

What it does:

* `action` `write` / `edit` on a `*.py` target → the current on-disk content
  is compiled with `compile()` — the same check as `python -m py_compile` and
  `tools/syntax_check.py`. Errors come back as `path:line:offset: msg`.
* No syntax error, a non-Python file, or a `write` creating a brand-new file
  that does not exist yet → empty `message` → the bridge appends nothing
  ("return nothing if there is no syntax error").
* Results are **cached per `(path, mtime_ns, size)`** (LRU-ish, cap 128), so
  repeated calls on an untouched file stay well inside the bridge's ~5 s
  per-server budget.
* File reads honor PEP 263 coding cookies exactly like the interpreter
  (`tokenize.open`); bad encoding cookies and null bytes are reported as
  diagnostics instead of crashing the handler (an unexpected exception would
  map to JSON-RPC `-32603` — the handler itself is failure-isolated).

Semantics note (important when writing your own server): the bridge fires
*before* the tool runs and does not forward the tool's content parameter —
only `tool`/`action`/`path`/`paths`/`cwd`. So `kimix_lsp.py` checks the file
**as it is on disk at call time** (the pre-write / pre-edit state), never the
content about to be applied. A syntax error reported on `write` therefore
describes the file *before* this write; the freshly written content is
checked on the *next* call.

The CLI contract is identical to `python -m kimi_cli.session_server`
(`--network`, `--host`, `--port`, `--stop-file`, `--root`, `--config-out`),
so it drops into a `server` entry as-is: it binds exactly the host/port it is
handed after placeholder substitution, and `--config-out` atomically writes a
`{network, address, port}` handshake file once bound.

## 5. Building your own injected behavior

A recipe, in order:

1. **Write a stdlib-only JSON-RPC server.** Subclass nothing; speak newline
   delimited JSON-RPC 2.0 over TCP (or `POST /rpc` over HTTP). Keep imports
   to the standard library so any interpreter can run it. Implement whatever
   methods you like, but make `before_tool` the star.
2. **Return `{"message": ..., "handled": True, ...}`** from `before_tool`.
   Empty/whitespace `message` = stay silent. Anything non-empty is appended
   (capped at 4096 chars). For non-dict results, the bridge falls back to
   compact JSON of the result.
3. **Keep the 5 s budget.** Use caching keyed on file identity
   (`(path, mtime, size)` is the proven pattern) and never do open-ended IO.
4. **Treat paths as raw strings.** The bridge does no normalization; resolve
   relative paths against the payload's `cwd` yourself
   (`kimix_lsp.py::_resolve_target`).
5. **Stay failure-isolated.** Handler exceptions become `-32603`, which the
   bridge logs and ignores — but a well-behaved server validates params
   (`-32602`) and never lets a bad file crash the dispatcher.
6. **Register it** in `.kimix/config.json` under `server`, using
   `<address>`/`<port>`/`<session_id>` placeholders in `args`. Set
   `network` to `tcp` or `http` to match your listener.
7. **Respect the pre-state semantics.** You see the file before the tool
   applies its change. If you need post-state diagnostics, report them on the
   *next* call (which is exactly how the syntax check covers newly written
   files).

Minimal config skeleton:

```json
{
  "server": [
    {
      "cmd": "python",
      "args": ["tools/my_server.py", "--host", "<address>", "--port", "<port>"],
      "cwd": ".",
      "network": "tcp"
    }
  ]
}
```

## 6. Verifying the wiring

* **End-to-end smoke test** — with the config in place, ask the agent to
  write a Python file containing a syntax error; the tool result should carry
  the `[server] path:line:col: ...` diagnostic. Reading the same file should
  not (the `read` action is outside `write`/`edit`).
* **Round-trip proof** — against the stock `kimi_cli.session_server`, the
  echo ack (`ack before_tool: tool=...`) appears in results, proving the
  channel without any custom code.
* **Failure drills** — kill the server mid-session: tool calls continue
  normally with no `[server]` line; pass `connect=False`: no RPC is issued
  at all. Covered by `kimi-cli/tests/tools/test_session_bridge.py`,
  `tests/test_session_server_protocol.py` and `tests/test_server_process.py`.

## 7. Summary

| Layer | File | Responsibility |
|---|---|---|
| Declaration | `.kimix/config.json` `server` | Which child processes to spawn per session |
| Spawning | `kimi_agent_sdk/_server_process.py` | Port allocation, `<address>`/`<port>`/`<session_id>` substitution, live registry |
| Wire protocol | `docs/session_server_rpc.md` | JSON-RPC 2.0 framing, builtin methods, error codes |
| Client bridge | `tools/file/session_bridge.py` | Fire-and-forget `before_tool` query, 5 s budget, 4096-char cap, total failure isolation |
| Tool wiring | `read.py` / `write.py` / `edit/__init__.py` | `connect` param (default on), `__call__` wrapper, `merge_message` |
| Extension point | `session_server.py` default echo / `tools/kimix_lsp.py` | Workspaces replace the echo with real diagnostics |

The result: built-in tools stay lean and safe, while a workspace can project
its own knowledge — compiler diagnostics, style rules, secret scanners,
ownership metadata — directly into the model's tool results.
