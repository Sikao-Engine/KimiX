# Session Server — JSON-RPC 2.0 Protocol Reference

Implementation: [`kimi-cli/src/kimi_cli/session_server.py`](../kimi-cli/src/kimi_cli/session_server.py)
(`kimi_cli.session_server`). Lifecycle / spawn side:
[`src/kimi_agent_sdk/_server_process.py`](../src/kimi_agent_sdk/_server_process.py)
(`kimi_agent_sdk._server_process`).

The session server is a self-contained JSON-RPC 2.0 endpoint that a Kimix
session can run as a nested sub-process and talk to over TCP or HTTP. It exists
to give out-of-process components (LSP bridges, indexers, project hooks) a
standard RPC surface bound to a session, and to expose builtin tool-ish methods
(`read_file`, `write_file`, ...) that a bridge can call instead of
re-implementing them.

Protocol followed: <https://www.jsonrpc.org/specification>.

---

## 1. Transports

Both transports carry the *same* JSON-RPC messages and are served by the same
`MethodRegistry`. Exactly one transport runs per process — chosen by
`--network tcp|http`.

### 1.1 TCP — newline-delimited JSON

* Framing: **one JSON document per line**, `\n`-terminated, in both directions.
  No length prefixes, no chunking, no blank-line protocol (blank lines received
  are skipped silently).
* One request (or one batch array) per line → **at most one response line** per
  request line. A batch is answered with a single line holding a JSON array.
* Frame size limit: `TCP_STREAM_LIMIT = 16 * 1024 * 1024` bytes (16 MiB) per
  line, applied to the server's `StreamReader` *and* to `JsonRpcClient`
  connections. Rationale: asyncio's 64 KiB default would silently truncate big
  payloads (e.g. a large `read_file` result).
* Over-limit behaviour: if a line exceeds 16 MiB the server cannot reassemble
  it — it stops reading and closes the connection **without writing any
  response**. Clients observe EOF or a connection reset. `JsonRpcClient` turns
  the client-side variant into `ConnectionError("server response line exceeds
  TCP_STREAM_LIMIT (...)")`.
* Concurrency: each accepted connection is handled independently; messages on a
  *single* connection are processed sequentially in arrival order. Different
  connections may run concurrently, so requests on two connections may
  interleave — always match responses to requests by `id`.
* Multiple requests may be pipelined into one connection before reading any
  answer; responses come back in the same order on that connection.
* Client-side id matching (`JsonRpcClient`, TCP only): for a single (non-batch)
  request the client skips leading response lines whose `id` does not match the
  request's, up to `_MAX_MISMATCHED_RESPONSES` (= 8) skips — a stale answer left
  buffered by an earlier timed-out call must not desynchronise later calls. If
  every line read is stale, the client raises
  `ConnectionError("no matching response id ...")` rather than returning an
  unrelated payload. Batch (list) payloads have no single id: the first line is
  returned as-is.

### 1.2 HTTP — `POST /rpc`

* Only one route exists: `POST /rpc`. The body is a single JSON-RPC message or
  a batch array (UTF-8, no chunking requirements).
* `Content-Type` is not enforced; the body is parsed as JSON either way.
* Status codes:
  * `200` + a compact JSON body for a response object or a batch response
    array (JSON-RPC errors are *in the body*, not in the status).
  * `204 No Content`, empty body, when nothing is answered (notification, or a
    batch containing only notifications).
  * `405 Method Not Allowed` for other verbs on `/rpc` (e.g. `GET`).
  * `404 Not Found` for any other path.
* HTTP-level degenerate bodies:
  * empty body → `200` with `-32600 Invalid Request`, `id: null`.
  * unparseable JSON → `200` with `-32700 Parse error`, `id: null`.
* Transport-level unexpected failure while handling a message → `200` with
  `-32603 Internal error`, `id: null`.
* HTTP is stateless: there is no connection to keep in sync, so a lost response
  is simply a lost response (unlike TCP, where it would desynchronise the line
  stream).

### 1.3 Wire format

Responses (and requests) are serialized with `orjson`, i.e. **compact** JSON:
no insignificant whitespace, non-ASCII left as-is (UTF-8). Example body exactly
as emitted:

```json
{"jsonrpc":"2.0","id":1,"result":{"message":"ack before_tool: tool=read action=read","tool":"read","action":"read","handled":true}}
```

---

## 2. Message rules

### 2.1 Request

```json
{"jsonrpc": "2.0", "method": "echo", "params": {"text": "hi"}, "id": 1}
```

| Member | Required | Accepted shape | Rejected with |
|---|---|---|---|
| `jsonrpc` | yes | exactly the string `"2.0"` | `-32600` |
| `method` | yes | non-... any `str` (unknown names are fine, see errors) | `-32600` if missing/not a string |
| `params` | no | JSON object, JSON array, `null`, or omitted | `-32600` for any other type (number/string/bool); arrays are accepted at the request level and rejected by *modeled* methods as `-32602` |
| `id` | no | `int` or `str` (other types sanitized, see §2.4) | never rejected — bad ids echo as `null` |

Checks performed before dispatch, in order:

1. The message must be a JSON object with `jsonrpc == "2.0"` and a string
   `method`; otherwise `-32600 Invalid Request`.
2. The object is validated against the request model (params must be
   `dict | list | None`); failure → `-32600 Invalid Request` with the pydantic
   detail in `error.data`.
3. `method` is looked up in the registry; unknown → `-32601`.
4. If the method declares a params model, `params` (or `{}` when absent/`null`)
   is validated; failure → `-32602 Invalid params` with the pydantic detail in
   `error.data`.

### 2.2 Notifications

A notification is a request **without the `id` member** — presence, not value:

* `{"jsonrpc":"2.0","method":"ping","id":null}` → a *request*; it is answered
  with `{"jsonrpc":"2.0","id":null,"result":"pong"}`.
* `{"jsonrpc":"2.0","method":"ping"}` → a notification; **no response is ever
  written** (TCP writes nothing at all, HTTP answers `204`).
* Notification handler failures are logged (`WARNING` for a JSON-RPC error,
  `ERROR` for a crash) and produce no response — the server never answers a
  notification, even on catastrophic failure, because that would desynchronise
  the TCP stream.

### 2.3 Response

```python
{"jsonrpc": "2.0", "id": <echoed id>, "result": <any>}        # success
{"jsonrpc": "2.0", "id": <echoed id>, "error": {"code": int, "message": str, "data": any?}}  # failure
```

Hard rules (implemented by `JsonRpcResponse.to_dict`):

* `jsonrpc` and `id` are **always** present. `id` may be `null`.
* **Exactly one** of `result` / `error` is present. A successful `null` result
  still emits `"result": null` (it is not dropped).
* Inside `error`, `data` is present only when it is not `null`.
* `error.code` is always an integer; `error.message` always a string.

### 2.4 `id` sanitization

The echoed `id` never comes from the validated model — it is the raw wire value
passed through `_sanitize_id`:

| Wire `id` | Echoed as |
|---|---|
| `int` (e.g. `42`) | `42` |
| `str` (e.g. `"a"`) | `"a"` |
| missing (notification) | no response |
| `null` | `null` |
| `true` / `false` | `null` (bools are ints in Python but are excluded explicitly) |
| float (`1.5`) | `null` |
| object / array | `null` |

So malformed, attacker-controlled ids degrade to `null` instead of raising
while the response is being built.

### 2.5 Batch

`[req, req, notification, req]` is valid; rules implemented by
`handle_message`:

* The whole batch is answered with **one** JSON array (one TCP line / one HTTP
  body).
* Items are dispatched concurrently (`asyncio.gather`) but the response array
  keeps the **request order**, with notifications simply absent. Matching by
  `id` is still recommended because ids are client-chosen.
* Empty array `[]` → single `-32600 Invalid Request` response with `id: null`.
* Non-object items inside a batch (e.g. `5`) → their own `-32600` response with
  `id: null`; the other items are still answered.
* A batch made only of notifications → no response at all (TCP writes nothing,
  HTTP answers `204`).
* If one item blows up in a way the handler could not convert to a JSON-RPC
  error, that item becomes `-32603 Internal error`; the batch still returns the
  other items (a poison item cannot collapse the batch). `CancelledError` is
  never swallowed — it propagates and closes the connection.

---

## 3. Error codes

Predefined JSON-RPC 2.0 set; the module exports constants with these values.

| Code | Constant | `message` | When | `error.data` |
|---|---|---|---|---|
| `-32700` | `PARSE_ERROR` | `"Parse error"` | Body/line is not valid JSON. | absent |
| `-32600` | `INVALID_REQUEST` | `"Invalid Request"` | Not an object, `jsonrpc != "2.0"`, `method` missing/not a string, request-model validation failure, empty batch, non-object batch item. | pydantic detail for request-model failures; absent otherwise |
| `-32601` | `METHOD_NOT_FOUND` | `"Method not found: <name>"` | Method not registered. | absent |
| `-32602` | `INVALID_PARAMS` | `"Invalid params"` | Params failed the method's pydantic model (also for positional arrays on modeled methods). | pydantic validation detail |
| `-32602` | `INVALID_PARAMS` | `"path escapes server root: <path!r>"` | `read_file`/`write_file` path escapes `--root`. | absent |
| `-32603` | `INTERNAL_ERROR` | `"Internal error"` | Unexpected exception inside a handler, transport-level failure, batch poison item, malformed JSON/`None` from the peer over HTTP (`"Invalid response from server: ..."` on the client side). | `repr(exc)` for handler failures; absent for transport-level/poison cases |

Handlers may raise `JsonRpcError(code, message, data)` to return any of these
explicitly; `JsonRpcError` raised by a handler is passed through untouched.

---

## 4. Params/result semantics

* Registration: `MethodRegistry.method(name, params_model=...)` decorator or
  `MethodRegistry.register(name, handler, params_model=...)`. **Re-registering a
  name overwrites the previous entry** (this is how an external server replaces
  a builtin). `names()` reports registration order; re-registration keeps the
  original slot.
* With `params_model`: the handler receives a validated model instance. Params
  `null`/absent are validated as `{}` (so required fields error with `-32602`;
  a method whose params are all optional accepts `"params": null` happily).
  Without it: the raw `dict | list | None` is handed to the handler (which is
  how `ping` ignores everything).
* Pydantic **lax** validation: numeric strings coerce into `int`/`float` fields
  (`{"a": "1.5"}` works for `add`), but numbers do **not** coerce into `str`
  fields (`{"text": 5}` → `-32602`). Unknown keys are ignored.
* Both sync and async handlers are supported.
* Result serialization: `BaseModel` → `model_dump(mode="json")`, dataclass →
  `dataclasses.asdict`, anything else passes through unchanged.

---

## 5. Builtin methods (`DEFAULT_REGISTRY`)

Registered names, in registration order: `ping`, `echo`, `read_file`,
`write_file`, `add`, `before_tool`.

### 5.1 `ping`

Health check. No params model — `params` is ignored entirely (object, array or
absent).

| Result | Type |
|---|---|
| `"pong"` | `str` (bare string, not an object) |

```json
--> {"jsonrpc":"2.0","method":"ping","id":1}
<-- {"jsonrpc":"2.0","id":1,"result":"pong"}
```

### 5.2 `echo`

Params (`EchoParams`):

| Field | Type | Required | Default |
|---|---|---|---|
| `text` | `str` | yes | — |

Result (`EchoResult`): `{"text": str, "length": int}` (`length = len(text)`).

```json
--> {"jsonrpc":"2.0","method":"echo","params":{"text":"hi"},"id":2}
<-- {"jsonrpc":"2.0","id":2,"result":{"text":"hi","length":2}}
```

Payloads are limited only by the transport: a 100 KB echo round-trips over TCP
(see the `TCP_STREAM_LIMIT` test), anything above 16 MiB per line kills the
connection.

### 5.3 `read_file`

Params (`ReadFileParams`):

| Field | Type | Required | Default |
|---|---|---|---|
| `path` | `str` | yes | — |
| `offset` | `int` | no | `0` |
| `limit` | `int` | no | `2000` |

Result: `{"path": str, "lines": list[str], "total": int}`

* The file is read as UTF-8 with `errors="replace"` (undecodable bytes become
  U+FFFD, never an error).
* `total` is the number of lines in the whole file; `lines` is the window
  `lines[offset : offset + limit]` (plain Python slicing, so a negative
  `offset` counts from the end and `limit: 0` yields `[]`; out-of-range windows
  simply yield fewer or no lines, never an error).
* `path` is the resolved path the server actually opened (absolute when
  `--root` is set).
* Missing file / directory / permission errors are **not** mapped to
  `-32602`: they escape the handler and surface as `-32603 Internal error` with
  `data` = `repr(exc)`. An escaping path *is* `-32602` (see §6).

```json
--> {"jsonrpc":"2.0","method":"read_file","params":{"path":"notes.txt","offset":0,"limit":10},"id":3}
<-- {"jsonrpc":"2.0","id":3,"result":{"path":"C:\\work\\notes.txt","lines":["hello root"],"total":1}}
```

### 5.4 `write_file`

Params (`WriteFileParams`):

| Field | Type | Required | Default |
|---|---|---|---|
| `path` | `str` | yes | — |
| `content` | `str` | yes | — |
| `append` | `bool` | no | `false` |

Result: `{"path": str, "bytes_written": int}` where `bytes_written` is the
UTF-8 encoded length of `content`.

* `append: false` → whole-file overwrite (`write_bytes`); `append: true` →
  binary append. Parent directories are **not** created; a missing parent
  directory surfaces as `-32603 Internal error`.

```json
--> {"jsonrpc":"2.0","method":"write_file","params":{"path":"inside.txt","content":"hello root"},"id":4}
<-- {"jsonrpc":"2.0","id":4,"result":{"path":"C:\\work\\inside.txt","bytes_written":10}}
```

### 5.5 `add`

Params (`AddParams`): `{"a": float, "b": float}` — both required, no defaults.

Result: `{"sum": float}`. JSON numbers become floats (`1 + 2` → `3.0`).

```json
--> {"jsonrpc":"2.0","method":"add","params":{"a":1.5,"b":2.25},"id":5}
<-- {"jsonrpc":"2.0","id":5,"result":{"sum":3.75}}
```

### 5.6 `before_tool`

The server half of the `connect` tool bridge
([`docs/tool_connect_bridge.md`](tool_connect_bridge.md)): a tool asks every
live session server for extra information before it runs. The builtin
implementation is a **pure echo** — it only repeats the call back, so the
round-trip is provable end to end. External servers (an LSP bridge, for
instance) replace it with real diagnostics.

Sync handler, **no file IO**, no `--root` interaction, and `cwd` is not used.

Params (`BeforeToolParams`):

| Field | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `tool` | `str` | yes | — | Tool name being called (`read`, `write`, `edit`, `read_image`, ...) |
| `action` | `str` | no | `"pre"` | Coarse action label; the bridge sends `read` / `write` / `edit` |
| `path` | `str \| null` | no | `null` | Primary target path (raw, unresolved) |
| `paths` | `list[str] \| null` | no | `null` | All target paths (multi-file reads) |
| `cwd` | `str \| null` | no | `null` | Tool's working directory |

Result (`BeforeToolResult`):

| Field | Type | Value |
|---|---|---|
| `message` | `str` | `ack before_tool: tool=<tool> action=<action>`, plus ` path=<path>` when `path` is present, plus ` paths=<python list repr>` when `paths` is present |
| `tool` | `str` | echoed `params.tool` |
| `action` | `str` | echoed `params.action` |
| `handled` | `bool` | `true` (default) |

Omission rules: `path`/`paths` are appended when the member is present in the
params object — an explicit `"paths": []` therefore *is* echoed as
`paths=[]`, while an absent member adds nothing.

```json
--> {"jsonrpc":"2.0","method":"before_tool","params":{"tool":"read","action":"read","path":"src/main.py","paths":["src/main.py","src/util.py"],"cwd":"C:\\work"},"id":6}
<-- {"jsonrpc":"2.0","id":6,"result":{"message":"ack before_tool: tool=read action=read path=src/main.py paths=['src/main.py', 'src/util.py']","tool":"read","action":"read","handled":true}}
```

```json
--> {"jsonrpc":"2.0","method":"before_tool","params":{"tool":"write"},"id":7}
<-- {"jsonrpc":"2.0","id":7,"result":{"message":"ack before_tool: tool=write action=pre","tool":"write","action":"pre","handled":true}}
```

```json
--> {"jsonrpc":"2.0","method":"before_tool","params":{"path":"x.py"},"id":8}
<-- {"jsonrpc":"2.0","id":8,"error":{"code":-32602,"message":"Invalid params","data":"1 validation error for BeforeToolParams\ntool\n  Field required [type=missing, ..."}}
```

(`error.data` is the full pydantic message; abridged above — it ends with the
`input_value`/`input_type` detail and a link to the pydantic error catalogue.)

Because `before_tool` never touches the filesystem, a `path` outside `--root`
is answered normally (no `-32602`) — confinement applies to `read_file` and
`write_file` only.

---

## 6. Path confinement (`--root`)

`read_file` and `write_file` resolve their `path` through `_resolve_path`:

* No `--root` (default): unrestricted, the path is used exactly as given —
  relative to the server process' own CWD.
* With `--root DIR`: relative paths are joined onto the root; everything is
  `resolve()`d; the result must stay inside the resolved root, otherwise
  `-32602 Invalid params` with `message = "path escapes server root: <repr>"`
  and **no filesystem access happens at all**.
* Absolute paths inside the root are allowed. On Windows the containment check
  is case-insensitive (`os.path.normcase`); on POSIX it is case-sensitive.
* `--root` affects only these two methods. `ping`, `echo`, `add` and
  `before_tool` ignore it.

---

## 7. CLI

```
python -m kimi_cli.session_server [flags]
```

| Flag | Default | Meaning |
|---|---|---|
| `--network tcp\|http` | `tcp` | Transport to bind. |
| `--host` | `127.0.0.1` | Bind address. |
| `--port` | `0` | Bind port; `0` = ephemeral (the real port is read back from `--config-out`). A child spawned from a `server` entry always receives a concrete, already-known port (§8.2). |
| `--config-out PATH` | none | Atomically write `{"network","address","port"}` to `PATH` once bound, so a launcher that used `--port 0` can discover the real port. Manual / raw-protocol use only: a `server` entry no longer reads this file (§8.2). Parent dirs are created. |
| `--stop-file PATH` | none | Exit once `PATH` appears (polled every 0.2 s). |
| `--root PATH` | none | Confine `read_file`/`write_file` to `PATH` (§6). |

Startup/shutdown sequence (`_run`):

1. Apply `--root` (if given) to the module-level root used by the file methods.
2. Build `JsonRpcServer(DEFAULT_REGISTRY, host, port)` and start the requested
   transport; the bound `(address, port)` is returned.
3. If `--config-out` is set, write the JSON connection-info file
   `{"network","address","port"}` **atomically**: a temp file in the target
   directory, `orjson.dumps(..., OPT_INDENT_2)` + `flush` + `fsync`, then
   `os.replace`. Readers therefore never observe a partial file. The `network`
   field is the requested one; `address`/`port` are the bound values.
4. Log `session_server listening on {network}://{address}:{port}` to **stderr**
   and block until stopped.
5. If `--stop-file` was given, a watcher task polls for the file and calls
   `stop()`. `stop()` is idempotent, closes the TCP listener / HTTP runner and
   clears the bound-address state; `serve_forever`/`wait_stopped` return.
6. `KeyboardInterrupt` is suppressed, so the process exits cleanly (status 0).

Note: `--config-out` is an aid for manual runs and the raw protocol tests. A
`.kimix/config.json` `server` entry does not need it — the client allocates the
port and substitutes it into the child's arguments before the spawn (§8.2).

Programmatic use instead of the CLI: instantiate `JsonRpcServer(registry, host,
port)` and `await server.start("tcp" | "http")`, or `start_tcp()` /
`start_http()`; `bound_port`, `network` and `is_running` are public.

---

## 8. `.kimix/config.json` — the `server` entry

The session SDK reads the `server` key of the session's `.kimix/config.json`
(`kimi_agent_sdk._server_process`) and spawns one child process per list entry.
The **client** owns the connection info: for every entry it allocates a free TCP
port itself and hands address, port and session id to the child through
placeholders substituted into `cmd`/`args` (§8.2). The child does not report a
bound port back through a handshake file any more.

### 8.0 How `config.json` is loaded (`kimix/utils/config.py`)

The `server` list is **not** parsed by `kimix/utils/config.py` itself — that
module only loads and caches the raw JSON; the SDK session consumes the
`server` key. The full chain from disk to spawned child:

```
<work_dir>/.kimix/config.json
        |  kimix.utils.config.load_config_json(work_dir) # orjson parse, cached per path
        v
config_json: dict # {} on missing file / bad JSON / non-object
        |  kimix.utils.session.create_session(...) -> _create_session_async(...)
        v
Session.create(...) / Session.resume(..., config_json=config_json) # kimi_agent_sdk._session
        |  _make_custom_config -> session.custom_config["config_json"] (tools see it)
        |  _start_configured_servers(session, work_dir, config_json)
        v
config_json["server"]  ->  start_server_processes(work_dir, servers_cfg,
                                                  out=live, session_id=session.id)
        |  per entry: parse_server_entry (§8.1) -> _find_free_port (§8.2)
        |  -> substitute <address>/<port>/<session_id> into cmd+args -> create_subprocess_exec
        v
one ServerProcess child per valid entry (§8.1), ready as soon as it is spawned
(§8.2), each registered in the live registry (§9) and reachable via
session.get_server_processes()
```

Loader semantics (`load_config_json`, `src/kimix/utils/config.py`):

* Reads exactly `<work_dir>/.kimix/config.json` (`work_dir` defaults to CWD),
  parsed with `orjson` **once per resolved path** and cached in a module-level
  dict — later calls never touch the filesystem. Callers must not mutate the
  returned dict; use `clear_config_json_cache()` after editing the file (tests).
* Fully failure-tolerant: a missing file, invalid JSON or a non-object payload
  all yield `{}` (no exception, no warning) — the session simply gets no
  servers.
* The top-level JSON object mixes provider settings (consumed elsewhere) with
  session keys; only the `server` key (plus e.g. `skill_dir`) matters here.

Session-side semantics (`_start_configured_servers`, `kimi_agent_sdk._session`):

* Called at the end of **both** `Session.create` and `Session.resume`, after
  the session object exists; falsy/missing `server` (`null`, `[]`, absent key)
  is a no-op.
* Children are spawned with the session's canonical work dir as the base for
  relative `cwd` paths, and appended to `session._server_processes`
  *before* each `start()` completes (cancellation-safe, see §8.3).
* The owning session's id is forwarded as
  `start_server_processes(..., session_id=session.id)` — that value is what the
  `<session_id>` placeholder expands to (§8.1 / §8.2).
* Never raises: any failure is logged (`Failed to start nested sub-process
  servers from config`) and session creation proceeds.
* `session.close()` → `stop_server_processes()` terminates every child and
  unregisters it from the live registry (§9); the sync close path uses
  `stop_server_processes_sync()`.

Full-file example — `<work_dir>/.kimix/config.json` with one `server` entry
written in placeholder style: the child receives its connection info as ordinary
arguments that the client fills in immediately before spawning (§8.2).

```json
{
  "server": [
    {
      "cmd": "python",
      "args": [
        "-m", "kimi_cli.session_server",
        "--network", "tcp",
        "--host", "<address>",
        "--port", "<port>",
        "--stop-file", ".kimix/stop.flag",
        "--root", "."
      ]
    }
  ]
}
```

The child is therefore launched as
`python -m kimi_cli.session_server --network tcp --host 127.0.0.1 --port 38123
--stop-file .kimix/stop.flag --root .`: it binds the address and port it was
handed, and the parent already knows both, so there is no handshake file to
write, nothing to poll and nothing to validate.

Substitution is a plain string replace applied to `cmd` and to every string in
`args`: the tokens `<address>`, `<port>` and `<session_id>` may appear any
number of times and embedded inside a longer argument (`--port=<port>` →
`--port=38123`). The token table and the allocation rules are in §8.1 / §8.2.

`kimi_cli.session_server` has **no `--session` flag**, so the entry above passes
no session id at all: `<session_id>` is there for whatever a server wants to do
with it — a path component (`--root .kimix/servers/<session_id>`), an extra flag
of a custom server, or nothing.

### 8.1 Entry schema

| Key | Type | Default | Notes |
|---|---|---|---|
| `cmd` | `str` | — | **Required**, non-blank. Executed directly (`create_subprocess_exec`), not via a shell. Placeholder tokens are substituted before the spawn (§8.2). |
| `args` | `list[str]` | `[]` | Command arguments; placeholder tokens are substituted in every string. |
| `cwd` | `str` | `"."` | Child CWD; relative paths resolve against the session work dir. |
| `network` | `"tcp"` \| `"http"` | `"tcp"` | Always applies — an entry resolves to one triple of connection info, there is no static/dynamic split any more. Any other value rejects the entry. |
| `address` | `str` | `"127.0.0.1"` | Always applies: the address the free-port probe binds and the value `<address>` expands to. |
| `port` | `int` | `0` | **Optional.** Absent or `0` = auto-assign an OS-chosen free port; a positive integer = *preferred starting port*, probed and incremented on conflict up to `MAX_PORT_ATTEMPTS` (= 100) attempts (§8.2). A `bool` or a negative value rejects the entry. |
| `config` | — | — | **Removed** (it used to point at the dynamic-mode handshake file). Any entry containing `config` is invalid: logged as `server[i]: 'config' key is no longer supported` and skipped (§8.3). |

Placeholder tokens (usable anywhere in `cmd` or in any `args` string, §8.2):

| Token | Resolves to |
|---|---|
| `<address>` | the entry's resolved address (`ServerSpec.address`, default `127.0.0.1`) |
| `<port>` | the free TCP port the client allocated for this entry |
| `<session_id>` | the owning session's id string — the SDK `Session.id` (a user-given name or a uuid hex); the literal `default` when no session id is known |

Minimal auto-port entry — nothing declared beyond `cmd`/`args`; `port` absent
means the client assigns one (§8.2) and the placeholders are filled in before
the spawn:

```json
{
  "server": [
    {
      "cmd": "python",
      "args": ["-m", "kimi_cli.session_server", "--network", "tcp", "--host", "<address>", "--port", "<port>"]
    }
  ]
}
```

Preferred-port entry — `port` is where the probe starts, so the number stays
stable while still yielding to a conflicting listener:

```json
{
  "server": [
    {
      "cmd": "python",
      "args": ["-m", "kimi_cli.session_server", "--network", "tcp", "--host", "<address>", "--port", "<port>"],
      "network": "tcp",
      "address": "127.0.0.1",
      "port": 38123,
      "cwd": "."
    }
  ]
}
```

### 8.2 Port allocation and placeholder substitution

The client owns the connection info. The entry point is
`start_server_processes(work_dir, server_cfg, out=None, *, session_id=None)`:
for each valid entry it resolves `(address, port, session_id)` *before* the
child exists, substitutes them into the command line, and spawns. The child only
has to bind what it was given.

**Port allocation** — `_find_free_port(address, preferred)`
(`src/kimi_agent_sdk/_server_process.py`) opens a probe socket, binds it and
closes it again:

* `preferred <= 0` (the `port` key absent or `0`): bind port `0` and take the
  OS-assigned ephemeral port.
* `preferred > 0`: try to bind `preferred`; if it is in use, bind
  `preferred + 1`, then `preferred + 2`, … giving up after
  `MAX_PORT_ATTEMPTS = 100` probes. The first port that binds wins, so `port` is
  a starting point rather than a guarantee (the usual probe-then-bind race with
  other processes still applies; entries are probed sequentially before their
  spawns, which keeps same-session collisions unlikely but not impossible).
  Probing also stops at the top of the port range (`65535`), so a preferred port
  near it yields the short tail rather than 100 invalid numbers.
* The probe binds without `SO_REUSEADDR`, so the result reflects what a real
  listener would see — including Windows' administratively excluded port ranges
  (`netsh int ipv4 show excludedportrange protocol=tcp`), which are simply
  reported as "in use" and skipped.
* Probe address: `""` when the entry's `address` is `0.0.0.0` or empty (that is
  what such a listener binds anyway), otherwise the entry's `address` itself.
* If no port frees up within the attempt budget the entry fails:
  `ready = false`, the reason is recorded in `error`, nothing is spawned and the
  session is unaffected (§8.3).

**Placeholder substitution** — right before the spawn, the resolved values
replace the tokens `<address>`, `<port>` and `<session_id>` in `cmd` and in each
string of `args`:

* A plain `str.replace` of the whole token: it may appear multiple times and
  embedded in a longer argument (`"--port=<port>"` → `"--port=38123"`,
  `"--log=.kimix/<session_id>.log"`). There is no shell, no escape syntax and no
  other expansion, so a literal `<port>` inside an argument is always replaced.
* `<session_id>` comes from the keyword-only `session_id` parameter
  (`_start_configured_servers` passes `session.id`); when it is `None` or
  otherwise unknown the literal `default` is substituted instead.
* `ServerProcess.address` / `.port` / `.network` hold the values that were
  substituted — which is what consumers read from the live registry (§9).
  One normalization: a wildcard spec address (`0.0.0.0` / `""` / `::`) is
  stored as `127.0.0.1` on the `ServerProcess`, because clients cannot
  connect *to* a wildcard address (the child still binds the wildcard it was
  given via `<address>`).
* Each spawn logs the **fully resolved command line as one string**
  (`server: <cmd> <args...>`, args with whitespace quoted) at INFO level and
  mirrors it to the UI via `kimix.ui.printing.print_debug` — the same channel
  as the other startup lines (`Native acceleration enabled.`, `Provider
  model: ...`, `skill dir: ...`).

**Ready condition**: `start()` marks the server `ready = true` as soon as
`create_subprocess_exec` returned successfully. No waiting, no polling, no
timeout — the resolved `(network, address, port)` is the connection info from
the first moment the server exists, and a child that has not bound its listener
yet simply refuses the connection attempt (a cold start is bounded by the
bridge's own ~5 s per-server deadline, §10, not by the spawn path).

`kimi_cli.session_server` still accepts `--config-out` (§7): the raw protocol
tests and manual invocations use it to learn the bound port of a `--port 0`
server. The `server` entry schema no longer reads such a file, and a configured
child does not need to write one.

### 8.3 Failure policy

* Bad entry (missing/blank `cmd`, non-dict entry, bad `port`/`network` type, or
  an entry that still carries the removed `config` key — `'config' key is no
  longer supported`) → logged as `server[i]: ...`, entry skipped. A non-list
  `server` value → logged, nothing spawned.
* No free port within `MAX_PORT_ATTEMPTS` (= 100) probes from the preferred port
  → `ready = false`, `error` records the reason, the entry is skipped: no child
  is spawned and the session is unaffected.
* Spawn failure → `ready = false`, `error = "spawn failed: ..."`.
* Every public entry point swallows and logs exceptions: server management can
  never break or fail the session.
* Cancellation during startup does not orphan children: servers are attached to
  the caller's live list *before* `start()` completes, and the interrupted
  startup terminates everything spawned so far (2 s terminate timeout, then
  `kill()`).
* Cross-loop teardown (Ctrl+C): session close may run on a *fresh* event loop
  (`asyncio.run()`) while the children were spawned on the interrupted one —
  awaiting `proc.wait()` then raises `RuntimeError` ("attached to a different
  loop"). `stop_server_processes` catches exactly that and falls back to
  `_reap_without_loop()`: on Windows `terminate()` is `TerminateProcess`
  (immediate, no zombie to reap); on POSIX the pid is reaped via
  `os.waitpid(WNOHANG)` polling, with `kill()` + blocking reap after the grace
  period. If the exit still cannot be confirmed a single `warning` is logged
  instead of a traceback.

---

## 9. Live server registry

`kimi_agent_sdk._server_process` keeps a module-level registry of the servers
that currently exist, so in-process consumers that cannot see the owning
`Session` object (the
[`connect` tool bridge](tool_connect_bridge.md)) can discover them:

```python
from kimi_agent_sdk._server_process import get_live_server_processes

for srv in get_live_server_processes():
    if not (srv.ready and srv.process is not None and srv.process.returncode is None
            and srv.network in ("tcp", "http")):
        continue
    # srv.address / srv.port are the connection info the client assigned (§8.2)
```

Contract:

* `_LIVE_SERVERS` is a `list[ServerProcess]` guarded by a `threading.Lock`;
  registration happens while the async startup runs, reads may come from any
  thread.
* A server is registered at the moment it is appended to the live list — i.e.
  **before** its `await server.start()` completes, so every entry the parser
  accepted is discoverable even while its child is still booting (and after a
  spawn or port-allocation failure) — which is exactly why consumers must
  re-check `ready` on every entry.
* `stop_server_processes` (async) and `stop_server_processes_sync` remove every
  server passed to them, by identity, whether or not the child was still
  running. Removal is tolerant of entries that were never registered.
* A server whose `start()` raises is unregistered immediately, so a caller that
  discards the returned list (`out=None`) cannot leave a dead entry behind.
* `get_live_server_processes()` returns a **snapshot copy** (mutating the
  returned list cannot affect the registry) and never raises — even the lock
  acquisition is guarded.
* Snapshots are not transactional: re-check `ready` / `process` /
  `process.returncode` / `network` on every entry before using it, because a
  session may stop servers concurrently.
* `_clear_live_registry_for_tests()` empties the registry without touching any
  child process (test isolation helper only).

`ServerProcess` attributes worth knowing: `spec` (the validated `ServerSpec`),
`process` (`asyncio.subprocess.Process | None`), `work_dir`, `network`,
`address`, `port`, `ready`, `error`.

---

## 10. Writing your own server

Anything that speaks the protocol above works; the easiest route is to reuse
this module. Re-register a name to **replace** a builtin (`before_tool`
included) — a second registration overwrites the first entry. For a
fully standalone server that imports nothing from `kimi_cli`, see the worked
example `tests/dummy_rpc_server.py` (§12).

```python
# my_lsp_server.py — server entry args: python my_lsp_server.py --host <address> --port <port>
from kimi_cli.session_server import (
    BeforeToolParams,
    BeforeToolResult,
    DEFAULT_REGISTRY,
    main,
)


def query_my_lsp(path: str) -> list[str]:
    ...  # talk to your language server; return human-readable diagnostics


def lsp_before_tool(params: BeforeToolParams) -> BeforeToolResult:
    diags = query_my_lsp(params.path or "")
    return BeforeToolResult(
        message=f"{len(diags)} diagnostics for {params.path}",
        tool=params.tool,
        action=params.action,
        handled=True,
    )


DEFAULT_REGISTRY.register("before_tool", lsp_before_tool, params_model=BeforeToolParams)

if __name__ == "__main__":
    main()  # keeps --network/--host/--port/--config-out/--stop-file/--root
```

Rules of thumb:

* Return a `BaseModel` (or dict) with a `message` field for `before_tool`; the
  bridge consumes `result.message` and ignores everything else.
* Keep `before_tool` handlers fast — the bridge's default budget is a hard
  ~5 s deadline per server (connect + full round trip) and it drops the result
  entirely when the deadline is exceeded.
* Never rely on the caller retrying: the bridge swallows all errors, so a slow
  or broken server degrades to "no extra info", never to a failed tool call.
* Build your own `MethodRegistry` instead of mutating `DEFAULT_REGISTRY` if you
  want the builtins gone; `JsonRpcServer(your_registry, host, port)` is all it
  takes to serve it from code.
* Accept the connection info the parent hands you: in a `.kimix/config.json`
  entry the client allocates a free port and substitutes `<address>` / `<port>`
  into your arguments before the spawn (§8.2), so bind those values instead of
  picking your own port and reporting it back through a file.

---

## 11. Reference tests

* `tests/test_session_server_protocol.py` — raw-wire conformance: id/jsonrpc
  presence, malformed-id echo, batch with a bad-id item, parse error, 16 MiB
  payload round-trip, `--root` confinement, `before_tool` (ack, defaults,
  `-32602`, notification silence).
* `tests/test_session_server_e2e.py` — `JsonRpcClient` against real child
  processes on both transports, batch ordering, notify-then-request, file
  round-trip, graceful `--stop-file` exit, and the SDK lifecycle loop
  (client-allocated port → substituted args → call → terminate).
tests/test_server_process.py — entry parsing (including rejection of the
 removed config key), free-port allocation, placeholder substitution,
 the resolved-command spawn log line, cross-loop reap fallback
 (stop on a fresh asyncio.run loop over a child spawned on a closed one),
 cancellation cleanup, and live-registry behaviour.
* `tests/test_dummy_rpc_server_e2e.py` — the example dummy server (§12) as a
  real sub-process on both transports: the example return value of every
  builtin over the wire, error paths, notification silence, `--root`
  confinement, `--stop-file` exit, and the full SDK lifecycle loop
  (substituted args → live registry → call → stop).
* `kimi-cli/tests/tools/test_session_bridge_e2e.py` — the `connect` bridge
  against the same real sub-process: `read` / `write` / `edit` / `read_image`
  spawned through `start_server_processes` (no bridge monkeypatching), the
  `[server] ack before_tool: ...` round trip in each tool result,
  `connect=False` opt-out, and live-registry cleanup on stop.

---

## 12. Example dummy server — `tests/dummy_rpc_server.py`

A complete, standalone example server implementing this protocol with the
**standard library only** (no `kimi_cli` import): every builtin method of
`DEFAULT_REGISTRY` (`ping`, `echo`, `read_file`, `write_file`, `add`,
`before_tool`) with the exact wire semantics and example return values of §5,
both transports (newline-delimited TCP and `POST /rpc` HTTP), the §3 error
codes, notification silence, batch handling, the atomic `--config-out`
connection-info file (manual runs only — no longer part of `server`-entry
discovery, §8.2), the `--stop-file` watcher and `--root` confinement of §6.

```console
$ python tests/dummy_rpc_server.py --network tcp --host 127.0.0.1 --port 38123 \
    --stop-file stop.flag --root .
```

Its CLI contract is identical to `python -m kimi_cli.session_server`, so it
drops into a `.kimix/config.json` `server` entry as-is: the client allocates the
port and substitutes the connection info into the arguments (§8.2), and the
script binds exactly what it is handed.

```json
{
  "server": [
    {
      "cmd": "python",
      "args": [
        "tests/dummy_rpc_server.py",
        "--network", "tcp",
        "--host", "<address>",
        "--port", "<port>",
        "--stop-file", ".kimix/stop.flag",
        "--root", "."
      ]
    }
  ]
}
```

A manual run with `--port 0` can still recover the bound port through
`--config-out`; when the SDK spawns the entry that flag is unnecessary, because
the port was chosen by the client before the child existed. The e2e suite
(`tests/test_dummy_rpc_server_e2e.py`, §11) starts the same script through such
a placeholder-style entry, so the address and port under test are the ones the
SDK allocated.

It is used as the real sub-process in the tests of §11 — proof that any process
speaking this protocol works as a session server, including for the `connect`
tool bridge (§5.6 / `docs/tool_connect_bridge.md`).
