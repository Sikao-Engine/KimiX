# 教程 7 — 内置工具注入：通过 Session Server 扩展文件工具

本教程讲解 KimiX 的内置文件工具（`read`、`write`、`edit`）如何**由工作区自身扩展**——
无需修改 `kimi-cli` 的任何代码。机制是一条精巧的注入通道：每个会话的
JSON-RPC 子进程（"session server"，在 `.kimix/config.json` 中声明）会在每次
文件工具调用*之前*被询问一次，其回复会被追加到模型看到的工具结果中。

贯穿全文的参考实现是 `tools/kimix_lsp.py`：一个纯标准库的服务器，把
Python 语法诊断信息注入 `write`/`edit` 的结果。

相关契约与源码：

| 文档 / 源码 | 作用 |
|---|---|
| [`docs/tool_connect_bridge.md`](../tool_connect_bridge.md) | `connect` 桥接契约（载荷、提取规则、上限） |
| [`docs/session_server_rpc.md`](../session_server_rpc.md) | 服务器所遵循的 JSON-RPC 2.0 线协议 |
| [`src/kimi_agent_sdk/_server_process.py`](../../src/kimi_agent_sdk/_server_process.py) | 解析 `server`、派生子进程、维护存活注册表 |
| [`kimi-cli/src/kimi_cli/tools/file/session_bridge.py`](../../kimi-cli/src/kimi_cli/tools/file/session_bridge.py) | 客户端一半：`before_tool()` + `merge_message()` |
| [`kimi-cli/src/kimi_cli/session_server.py`](../../kimi-cli/src/kimi_cli/session_server.py) | 默认服务器及内建 echo 版 `before_tool` |
| [`tools/kimix_lsp.py`](../../tools/kimix_lsp.py) | 工作区服务器示例（LSP 式语法检查） |

---

## 1. 注入效果一览

当文件工具执行且 session server 存活时，模型会看到类似这样的内容：

```
Wrote 3412 bytes to src/app.py.

[server] src/app.py:44:9: SyntaxError: unmatched ')'
```

`[server] ...` 这一行**不是**工具产生的。它是工作区 session server 的回复，
在工具执行前取回并合并进工具结果的 `message` 字段。主要用途：项目自带的
LSP 桥接服务器在读写文件的同时返回诊断 / 符号信息，模型无需额外一轮
调用即可看到。

整个通道受两条设计规则约束：

1. **只做增强。** 服务器只能*附加信息*，绝不能改变、阻断或弄垮底层的
   工具调用。所有故障模式（import 循环、注册表为空、连接拒绝、超时、
   JSON-RPC 错误、结果格式错误）都会被吞掉并降级为"没有额外信息"。
2. **单次调用可退出。** 每个接入的工具都有 `connect: bool = True`
   参数；传 `connect=False` 则完全跳过 RPC。

## 2. 架构：五个活动部件

```
.kimix/config.json ──► _server_process.py ──► 存活注册表 ──► session_bridge ──► 文件工具
 "server": [条目]        派生 + <port>        get_live_          before_tool()      read
                         占位符替换           server_processes() merge_message()    write
                                              (kimi_agent_sdk)                      edit
                                                   ▲                                     │
                                        tools/kimix_lsp.py ◄── JSON-RPC 2.0 ────────────┘
                                        （session server 子进程）
```

### 2.1 声明服务器：`.kimix/config.json` → `server`

工作区通过在 `<work_dir>/.kimix/config.json` 放置配置文件来自定义工具行为。
`server` 元素是 session server 条目列表；本仓库自带一条：

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

SDK（`kimi_agent_sdk._server_process`）负责生命周期管理：

* 会话启动时，**每个条目作为子进程被派生**，通过
  `asyncio.create_subprocess_exec`，`cwd` 相对于工作目录解析。
* **地址由客户端决定。** 派生前先分配一个空闲 TCP 端口（条目中的
  `port` 仅作*首选起始端口*，冲突时向上探测）。`<address>`、`<port>`
  和 `<session_id>` 占位符会被替换进 `cmd`/`args`——缺失的 `session_id`
  替换为字面字符串 `"default"`。子进程因此从自己的命令行参数中得知
  监听地址。
* 进程一旦派生即视为就绪；声明旧式 `config` 键的条目会在解析时被拒绝。
* 每个已派生的服务器都会在模块级**存活注册表**
  （`get_live_server_processes()`）中注册，并在 session server 停止时
  注销——包括取消清理，因此不会留下孤儿进程。

### 2.2 线协议：JSON-RPC 2.0

Session server 使用 `docs/session_server_rpc.md` 描述的协议——TCP 上按行
分隔的 JSON-RPC（或 HTTP 上 `POST /rpc`）。`tools/kimix_lsp.py` 实现了内建
方法集（`ping`、`echo`、`read_file`、`write_file`、`add`）以及本文的主角：

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

错误遵循标准 JSON-RPC 错误码（`-32700` 解析错误、`-32600` 无效请求、
`-32601` 方法未找到、`-32602` 无效参数、`-32603` 内部错误）。任何会说
这套协议的进程都可以充当 session server——这正是 `kimix_lsp.py`
刻意只用标准库的原因（见 §4）。

### 2.3 客户端一半：`session_bridge.py`

`kimi_cli/tools/file/session_bridge.py` 暴露两个函数：

* **`before_tool(tool_name, action, *, path, paths, cwd, timeout=5.0)`** —
  快照 SDK 存活服务器注册表，过滤出 `ready`、进程仍在运行、网络为
  `tcp`/`http` 的条目，然后向每个服务器发一次 `before_tool` JSON-RPC
  调用。载荷固定包含 `{tool, action, path, paths, cwd}` 五个键
  （未设置时为 `null`）。服务器可依赖的规则：**`path` 是首要/第一个
  目标；`paths` 是完整列表，仅当工具收到多个目标时非 `null`。** 路径以
  **原始未解析**形式转发——即调用方传入的原样（含 glob 模式、相对路径、
  形似 URL 的值），不做任何规范化，由服务器自行解释。
* **`merge_message(existing, info)`** — 把回复追加到工具结果：
  `info` 为空 → `existing` 原样返回；否则空行加 `[server] <info>`
  标记行。

回复提取规则（契约 §5）：

| 结果类型 | 提取的文本 |
|---|---|
| `str` | 原样 |
| `dict` | 其 `message` 字段（字符串），否则整个 dict 的紧凑 JSON |
| 其他 | 紧凑 JSON |

提取的文本去除首尾空白后截断到 **4096 字符**（`MAX_APPEND_CHARS`）。
每台服务器的整个往返有 **5 秒**预算（`DEFAULT_TIMEOUT`，同时约束
connect 与完整调用，防止拖拖拉拉的服务器靠多次读超时叠加拖延）。

**故障隔离**是核心不变量：`before_tool` *绝不抛出*。SDK 注册表的导入
是惰性的（避免 `kimix → kimi_agent_sdk → kimi_cli` 的 import 循环）；
SDK 不可用即意味着"没有服务器、没有信息"。每台服务器的查询独立包裹
异常，外层还有兜底 catch-all，最终上报 `None`，让工具完全像没有服务器
一样继续执行。

### 2.4 工具一半：纤薄的 `__call__` 包装

注入以无装饰器的包装形式接入工具：`__call__` 先跑桥接，再跑
`_call_core` 真正逻辑，最后合并消息。三个文件工具的形状完全一致
（以 `write` 为例）：

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

各工具的接入方式（见 `docs/tool_connect_bridge.md` §2）：

| 工具 | 发送的 `tool`/`action` | 发送的路径 |
|---|---|---|
| `read`（`read.py`） | `read` / `read` | `params.file_path`：`str` → `path`；`list[str]` → `paths`（全部）且 `path` = 第一个 |
| `write`（`write.py`） | `write` / `write` | `path` = `params.file_path`，`paths` = `null` |
| `edit`（`edit/__init__.py`） | `edit` / `edit` | `path` = `params.file_path`（可为空 → 缺省时 `null`），`paths` = `null` |
| `read_image`（`read_media.py`） | `read` / `read` | `path` = `params.file_path`，`paths` = `null` |

关于 `replace.py`：它是一个**向后兼容的 shim**，从
`kimi_cli.tools.file.edit` 重导出 `EditFile` / `Params` / `Edit`。注入逻辑
位于 edit 包中，因此 edit 模式的工具自动获得注入能力；服务器端并不区分
"replace"与其他 edit 模式——`action` 始终是规范值 `edit`。

由于包装层只*读*结果消息，桥接无法破坏工具语义：审批、diff、各类护栏
（受保护路径、自动生成文件、冲突标记）都在 `_call_core` 内原样运行。

## 3. 默认服务器及其 echo 钩子

`kimi_cli.session_server` 自带一个注册在 `DEFAULT_REGISTRY` 上的默认
`before_tool` 实现：

```python
@DEFAULT_REGISTRY.method("before_tool", params_model=BeforeToolParams)
def _before_tool(params: BeforeToolParams) -> BeforeToolResult:
    """默认 echo 实现…… 纯同步，无文件 IO。"""
    message = f"ack before_tool: tool={params.tool} action={params.action}"
    ...
```

它只是把调用"复述"回来，使端到端往返可验证。外部服务器——例如 LSP
桥接——则**用真正的诊断替换这个方法**。这一句话就是整个注入设计的精髓：
该钩子是一个稳定的扩展点，工作区通过提供自己的服务器进程来覆盖它。

## 4. 参考实现：`tools/kimix_lsp.py`

`kimix_lsp.py` 是一个独立的 JSON-RPC 2.0 session server——纯标准库，
不 import `kimi_cli`——遵循 `tests/dummy_rpc_server.py` 的参考模式。
它实现了内建方法集，但把 `before_tool` 的 echo 替换为 **Python 语法检查**。

为何独立成文：SDK 直接派生条目的 `cmd`，而在 Windows 上裸 `python`
解析到的是系统解释器——未必装有 `kimi_cli` 的那个环境。只依赖标准库
使得 `.kimix/config.json` 条目在任何解释器下都能工作。（这也是它
使用标准库 `json` 而非项目 orjson 规则的原因。）

其行为：

* `action` 为 `write` / `edit`、目标为 `*.py` → 用 `compile()` 编译
  当前磁盘上的内容——与 `python -m py_compile` 和
  `tools/syntax_check.py` 相同的检查。错误以 `path:line:offset: msg`
  形式返回。
* 没有语法错误、非 Python 文件、或 `write` 创建的是尚不存在的新文件
  → `message` 为空 → 桥接不附加任何内容（"没有语法错误就什么都不返回"）。
* 结果按 `(path, mtime_ns, size)` **缓存**（近似 LRU，上限 128），
  因此对未改动的文件重复调用也能稳在桥接约 5 秒的每服务器预算内。
* 文件读取用 `tokenize.open`，与解释器一样严格遵循 PEP 263 编码声明；
  编码声明错误和 null 字节被报告为诊断而不是让处理器崩溃（未预期的
  异常会映射为 JSON-RPC `-32603`——处理器自身也是故障隔离的）。

语义要点（编写自己的服务器时务必注意）：桥接在工具运行*之前*触发，
且不会转发工具的内容参数——只有 `tool`/`action`/`path`/`paths`/`cwd`。
因此 `kimix_lsp.py` 检查的是**调用时刻磁盘上的文件**（写入/编辑前的
状态），而非即将应用的内容。`write` 上报的语法错误描述的是*本次写入
之前*的文件；新写入的内容会在*下一次*调用时被检查到。

CLI 契约与 `python -m kimi_cli.session_server` 完全一致
（`--network`、`--host`、`--port`、`--stop-file`、`--root`、`--config-out`），
因此可以直接嵌入 `server` 条目：占位符替换后它绑定到指定给它的
host/port，`--config-out` 在绑定后原子写出 `{network, address, port}`
握手文件。

## 5. 构建你自己的注入行为

按顺序的配方：

1. **写一个纯标准库的 JSON-RPC 服务器。** 不用继承任何类；在 TCP 上
   说按行分隔的 JSON-RPC 2.0（或 HTTP 上 `POST /rpc`）。import 仅限
   标准库，保证任何解释器都能运行。想实现什么方法都行，但把
   `before_tool` 作为主角。
2. **从 `before_tool` 返回 `{"message": ..., "handled": True, ...}`。**
   `message` 为空/纯空白 = 保持沉默。任何非空内容都会被追加（上限
   4096 字符）。非 dict 结果会被桥接降级为整个结果的紧凑 JSON。
3. **守住 5 秒预算。** 使用以文件身份为键的缓存（`(path, mtime, size)`
   是验证过的模式），绝不做无边界的 IO。
4. **把路径当原始字符串对待。** 桥接不做规范化；相对路径请用载荷中的
   `cwd` 自行解析（参考 `kimix_lsp.py::_resolve_target`）。
5. **保持故障隔离。** 处理器异常会变成 `-32603`，桥接只会记录日志并
   忽略——但行为良好的服务器应校验参数（`-32602`），绝不让一个
   坏文件弄崩分发器。
6. **在 `.kimix/config.json` 的 `server` 下注册**，在 `args` 中使用
   `<address>`/`<port>`/`<session_id>` 占位符。`network` 与你的监听器
   保持一致（`tcp` 或 `http`）。
7. **尊重"前态"语义。** 你看到的是工具应用变更*之前*的文件。若需要
   后态诊断，请在*下一次*调用时报告（语法检查正是这样覆盖新写入文件
   的）。

最小配置骨架：

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

## 6. 验证接线

* **端到端冒烟测试** — 配置就位后，让 agent 写入一个含语法错误的
  Python 文件；工具结果应携带 `[server] path:line:col: ...` 诊断。
  读取同一文件则不应出现（`read` 动作不属于 `write`/`edit`）。
* **往返证明** — 对着原版 `kimi_cli.session_server`，结果中会出现
  echo ack（`ack before_tool: tool=...`），无需任何自定义代码即可
  证明通道可用。
* **故障演练** — 会话中途杀掉服务器：工具调用照常进行，只是没有
  `[server]` 行；传 `connect=False`：完全不发 RPC。相关测试见
  `kimi-cli/tests/tools/test_session_bridge.py`、
  `tests/test_session_server_protocol.py` 和 `tests/test_server_process.py`。

## 7. 小结

| 层 | 文件 | 职责 |
|---|---|---|
| 声明 | `.kimix/config.json` 的 `server` | 每个会话派生哪些子进程 |
| 派生 | `kimi_agent_sdk/_server_process.py` | 端口分配、`<address>`/`<port>`/`<session_id>` 替换、存活注册表 |
| 线协议 | `docs/session_server_rpc.md` | JSON-RPC 2.0 分帧、内建方法、错误码 |
| 客户端桥接 | `tools/file/session_bridge.py` | 尽力而为的 `before_tool` 查询，5 秒预算、4096 字符上限、完全故障隔离 |
| 工具接线 | `read.py` / `write.py` / `edit/__init__.py` | `connect` 参数（默认开）、`__call__` 包装、`merge_message` |
| 扩展点 | `session_server.py` 默认 echo / `tools/kimix_lsp.py` | 工作区用真正的诊断替换 echo |

最终结果：内置工具保持精简与安全，而工作区可以把自己领域的知识——
编译器诊断、风格规则、密钥扫描、文件归属元数据——直接投射进模型的
工具结果中。
