# `connect: bool` — tool ↔ session-server bridge contract

Related: [`docs/session_server_rpc.md`](session_server_rpc.md) (the wire protocol
and the server-side `before_tool` method),
[`kimi-cli/src/kimi_cli/session_server.py`](../kimi-cli/src/kimi_cli/session_server.py),
[`src/kimi_agent_sdk/_server_process.py`](../src/kimi_agent_sdk/_server_process.py).

**Status of this contract.** Both halves are **implemented**:
`kimi_cli.session_server` registers a builtin `before_tool` method on
`DEFAULT_REGISTRY` (pure echo, no IO) and
`kimi_agent_sdk._server_process` maintains the live-server registry
(`get_live_server_processes()`), covered by root tests
(`tests/test_session_server_protocol.py`, `tests/test_server_process.py`).
The **tool half** lives in `kimi_cli/tools/file/session_bridge.py`
(`before_tool()` + `merge_message()`), with the `connect` param on the four
file tools (`read`, `write`, `edit`, `read_image` wired through a thin
`__call__` wrapper over `_call_core`), covered by
`kimi-cli/tests/tools/test_session_bridge.py`.

---

## 1. What it does

When a file tool runs with `connect` enabled (the default) **and** a session
server sub-process is live, the tool first asks that server for extra
information about the file it is about to touch, waits for the reply, and
appends the answer to the tool result's `message` field:

```
Read 42 lines from src/main.py.

[server] 3 diagnostics: line 12 unused import; line 44 possibly-unbound; line 51 type mismatch
```

Primary use case: a project-provided LSP bridge returns diagnostics / symbol
information alongside reads and edits, so the model sees them without a
separate round trip. With the stock server the reply is the builtin ack echo,
which is what proves the round-trip works.

The bridge is an **enhancement channel only**: it can add information, it can
never change, block, or fail the underlying tool call.

---

## 2. Which tools carry `connect`

| Tool (`before_tool` `tool` value) | Source | Param | `action` sent | Path(s) sent |
|---|---|---|---|---|
| `read` | `kimi_cli/tools/file/read.py` | `connect: bool = True` | `read` | `params.file_path`: `str` → `path`; `list[str]` → `paths` (all of them) and `path` = the first one |
| `write` | `kimi_cli/tools/file/write.py` | `connect: bool = True` | `write` | `path` = `params.file_path`, `paths` = `null` |
| `edit` | `kimi_cli/tools/file/edit/params.py` | `connect: bool = True` | `edit` | `path` = `params.file_path` (nullable in the model → `null` when absent), `paths` = `null` |
| `read_image` | `kimi_cli/tools/file/read_media.py` | `connect: bool = True` | `read` | `path` = `params.file_path`, `paths` = `null` |

* Default `true`; pass `connect: false` to opt one call out (no RPC at all).
* Rule servers can rely on: **`path` is the primary/first target, `paths` is the
  full list and is only non-`null` when the tool received several targets.**
* Paths are forwarded **raw and unresolved** — exactly what the caller passed
  (glob patterns, relative paths, URL-ish values included). The server is
  expected to interpret them; the bridge does no normalisation beyond
  stringification.
* `cwd` is the tool's working directory (`self._work_dir`, read defensively via
  `getattr(self, "_work_dir", None)`) — some tool instances are built without a
  session object, and the bridge must not depend on one.

---

## 3. When the RPC is sent

Inside `__call__`, **before any validation or core tool logic**, the bridge
computes `server_info`; the merge happens on the way out:

  ```python
  @override
  async def __call__(self, params: Params) -> ToolReturnValue:
      server_info: str | None = None
      if getattr(params, "connect", False):
          try:  # belt-and-braces; before_tool itself never raises
              work_dir = getattr(self, "_work_dir", None)
              server_info = await before_tool(
                  "read", "read",
                  path=path_of(params.file_path),
                  paths=paths_of(params.file_path),  # None for a single target
                  cwd=str(work_dir) if work_dir is not None else None,
              )
          except Exception:
              server_info = None
      ret = await self._call_core(params)  # the previous __call__ body, verbatim
      if server_info:
          ret = ret.model_copy(
              update={"message": merge_message(ret.message, server_info)}
          )
      return ret
  ```

Why this shape: the file tools' `__call__` has many early `return`s (and most
failures are returned as a `ToolReturnValue` rather than raised), so the
recommended implementation moves the original body into `_call_core` and keeps
`__call__` as this thin wrapper — one merge point covers every return path.

The RPC is sent *before* the tool logic runs, i.e. even when the tool will later
fail on that same call: the server is told about intent, not outcomes.

One `before_tool` request per **qualifying live server**, issued sequentially
(usually one server). Worst case latency is **one `timeout` per live server**:
the budget is a hard deadline, enforced with `asyncio.wait_for` around both the
connect *and* the whole request/response round trip, so a server that dribbles
partial or unrelated data cannot stretch it (the client-side per-read timeouts
alone could otherwise stack up).

---

## 4. Server discovery: the live registry

The bridge never learns about the SDK `Session` object; it reads the module-level
registry instead:

```python
from kimi_agent_sdk._server_process import get_live_server_processes

servers = [
    srv for srv in get_live_server_processes()
    if srv.ready
    and srv.process is not None
    and srv.process.returncode is None
    and srv.network in ("tcp", "http")
]
```

* The import is **lazy** (inside the function, wrapped in `try/except
  Exception`) because the dependency stack is `kimix → kimi_agent_sdk →
  kimi_cli`; a lazy in-function import is the established pattern here. If the
  import fails, the bridge returns `None` and the tool proceeds untouched.
* `get_live_server_processes()` returns a snapshot copy and never raises;
  every entry must still be re-checked (a session may stop servers at any
  moment), which is what the filter above does.
* No qualifying servers → fast path: return `None` immediately, no client is
  constructed and no socket is opened. So `connect: true` costs nothing when no
  server is configured.

---

## 5. The wire call

Transport chosen from the server's resolved info: `JsonRpcClient.connect_tcp(srv.address, srv.port)`
or `JsonRpcClient.connect_http(srv.address, srv.port)`, then

```json
{"jsonrpc":"2.0","method":"before_tool","params":{
  "tool":"read","action":"read","path":"src/main.py",
  "paths":["src/main.py","src/util.py"],
  "cwd":"C:\\work\\proj"},"id":1}
```

Params (validated by `BeforeToolParams` server-side, see
[`session_server_rpc.md` §5.6](session_server_rpc.md)):

| Field | Type | Always sent? | Notes |
|---|---|---|---|
| `tool` | `str` | yes | required by the model |
| `action` | `str` | yes | `read` / `write` / `edit` (server default would be `pre`) |
| `path` | `str \| null` | yes (`null` when absent) | primary target |
| `paths` | `list[str] \| null` | yes (`null` when absent) | all targets |
| `cwd` | `str \| null` | yes (`null` when unknown) | tool work dir |

The client is always closed in a `finally`. Default per-call timeout: **5.0 s**.

Result extraction (`result` member of the response):

1. `str` → used as-is.
  2. `dict` (the normal case — `BeforeToolResult` serializes to a dict) →
     `result.get("message")` when it is a `str`; otherwise compact JSON
     (orjson, `default=str`) of the whole result.
  3. Anything else → compact JSON encoding of the value.
4. Whitespace-stripped; empty or `None` → treated as "no info" (returns `None`).
5. Capped at **4096 characters** before it is appended.

If several servers answer, the non-empty messages are joined into one string
(order = registry order).

---

## 6. How the message is extended

  ```python
  def merge_message(existing: str, info: str | None) -> str:
      if not info:
          return existing
      if not existing:
          return f"[server] {info}"
      return f"{existing}\n\n[server] {info}"
  ```
  (An empty `existing` message gets the marker block without leading blank
  lines; the merge is applied through `model_copy(update=...)` because
  results are taken as immutable pydantic values.)

* The `[server] ` prefix marks the text as externally supplied, so the model can
  tell tool output from server commentary.
* No change at all when `info` is `None`/empty: byte-identical results.
* `ToolReturnValue.message` is a pydantic field; the implemented tools merge
  via `ret = ret.model_copy(update={"message": merged})` in the wrapper.
* Because the merge sits in the `__call__` wrapper, it applies to whatever
  `_call_core` returns — successful *and* error-shaped results alike.

Example transcript, stock server (echo) with one live dynamic entry:

```
--> before_tool {"tool":"edit","action":"edit","path":"src/app.ts","paths":null,"cwd":"C:\\work"}
<-- result {"message":"ack before_tool: tool=edit action=edit path=src/app.ts","tool":"edit","action":"edit","handled":true}

Edit applied to src/app.ts (1 replacement).

[server] ack before_tool: tool=edit action=edit path=src/app.ts
```

Same call against an LSP bridge that replaced `before_tool`:

```
<-- result {"message":"2 diagnostics: line 18 TS2345 argument type; line 40 TS6133 unused","tool":"edit","action":"edit","handled":true}

Edit applied to src/app.ts (1 replacement).

[server] 2 diagnostics: line 18 TS2345 argument type; line 40 TS6133 unused
```

---

## 7. Failure isolation (non-negotiable)

Every failure mode degrades to "no extra information"; nothing propagates to the
tool, the model, or the session:

| Failure | Bridge behaviour |
|---|---|
| `kimi_agent_sdk._server_process` import fails | return `None` (logged) |
| Registry empty / no server qualifies | return `None`, no client created |
| TCP connect refused / HTTP error / socket closed | return `None` (logged) |
| Call exceeds the timeout | return `None` (logged); the client is closed |
| Server answers a JSON-RPC `error` (e.g. `-32602`) | return `None` (logged) |
| Malformed / non-JSON result, missing `message` | best-effort JSON text; empty → `None` |
| Handler crash on the server (`-32603`) | return `None` (logged) |
| Any other exception | catch-all `except Exception` → log via `kimi_cli.utils.logging` → `None` |

Server-side the guarantee is the mirror image: the `before_tool` builtin is pure
and sync (no IO, no `--root` checks), so a builtin-only server cannot fail the
call; and notifications to it are never answered but also never break the
stream.

---

## 8. Running a server that answers `before_tool`

Configure it in the project's `.kimix/config.json` (dynamic discovery — no port
bickering):

```json
{
  "server": [
    {
      "cmd": "python",
      "args": [
        "lsp_bridge.py",
        "--network", "tcp",
        "--host", "127.0.0.1",
        "--port", "0",
        "--config-out", ".kimix/lsp.json",
        "--stop-file", ".kimix/lsp.stop"
      ],
      "config": ".kimix/lsp.json"
    }
  ]
}
```

`lsp_bridge.py` reuses the session-server module and **replaces** the echo
implementation — re-registering a name overwrites the builtin entry
(`MethodRegistry` has no delete API):

```python
from kimi_cli.session_server import (
    BeforeToolParams,
    BeforeToolResult,
    DEFAULT_REGISTRY,
    main,
)


def before_tool_hook(params: BeforeToolParams) -> BeforeToolResult:
    diags = lsp_diagnostics(params.path or "", cwd=params.cwd)  # your LSP client
    return BeforeToolResult(
        message="; ".join(diags) if diags else "no diagnostics",
        tool=params.tool,
        action=params.action,
        handled=True,
    )


DEFAULT_REGISTRY.register("before_tool", before_tool_hook, params_model=BeforeToolParams)

if __name__ == "__main__":
    main()
```

Notes:

* The SDK entry must point `config` at the file the server writes via
  `--config-out`; discovery backs off 1, 2, 4, 8, 16 s (see
  [`session_server_rpc.md` §8.2](session_server_rpc.md)).
* If the server never writes its config file, the entry simply ends up
  `ready = false`, its child is terminated, and every tool call takes the "no
  live server" fast path. A broken server can never hang a session.
* Keep the handler fast and idempotent: it is called on every read/write/edit
  while the server is live. Return an empty `message` when there is nothing to
  say — empty answers add nothing to the tool result.

---

## 9. Testing the bridge

* Unit: monkeypatch `kimi_agent_sdk._server_process.get_live_server_processes`
  (that is the attribute the lazy import resolves) — empty list → `None`; a
  fake `ServerProcess`-shaped object pointing at a real in-process
  `JsonRpcServer` on TCP → the ack message containing `before_tool`.
* Isolation: point the fake entry at a closed port or a server whose handler
  sleeps past the timeout → `None`, no exception.
* Params: each of the four tools' `Params` exposes `connect` with default
  `True`; with `connect: false` the patched `before_tool` must not be called.
* Tool integration: patch the imported `before_tool` name inside each tool
  module to return e.g. `"LSP: 2 diagnostics"` → assert
  `result.message` contains `"[server] LSP"`.
