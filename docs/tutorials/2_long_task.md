# 长任务处理

对于复杂或耗时的任务，Kimix 提供两种主要处理方式：

1. **`/plan` 命令**：将任务拆分为有序的步骤列表，串行执行并支持断点续传
2. **`/swarm` 命令**：通过 Agent Swarm 将任务拆分为 DAG，并行调度多个子 Agent

---

## /plan 命令

`/plan` 命令采用**计划生成 + 逐步执行**的两阶段模式处理长任务。适合需要明确步骤、顺序执行、可中断恢复的场景。

### 基本用法

进入交互终端后执行：

```
/plan
```

进入多行输入模式：
- 以 `/end` 结束输入提交任务描述
- 以 `/cancel` 取消当前操作

**示例**：

```text
/plan
>>>> Make a task-list: input multiple-lines, end with /end, cancel with /cancel
为这个项目添加完整的错误处理机制：
1. 为所有函数添加参数校验
2. 统一异常类型和错误码
3. 添加日志记录
/end
```

### 执行流程 (`execute_plan`)

参考 `src/kimix/utils/prompt.py` 和 `src/kimix/cli_impl/commands.py`：

#### 阶段 1：计划生成

1. **创建计划会话**：使用 `agent_boss.json` 配置和 `TodoMaker` 系统提示词创建专用会话
2. **生成计划文件**：LLM 将任务拆解为步骤列表，写入 `~/.kimi/plan/plan_<uuid>.md`。生成过程最多重试 4 次，确保计划被正确写入
3. **缓存机制**：计划信息（步骤数、文件路径、文件 SHA256 哈希值）存入 `~/.kimi/plan/.cache.json`。恢复时会校验文件哈希，若计划文件已被修改则放弃缓存

#### 阶段 2：步骤执行

1. **断点恢复**：若检测到缓存且未完成，询问是否从断点继续
2. **逐行执行**：按顺序执行每个步骤，每步完成后提示调用 `SetTodoList` 记录进度
3. **进度更新**：每完成一步更新 `finished_step_count` 到缓存文件
4. **完成清理**：所有步骤执行完毕后删除缓存

### 从文件加载任务描述

可直接指定包含任务描述的文件路径，Kimix 会读取文件内容作为任务提示词并自动生成执行计划：

```
/plan:path/to/task_description.md
```

### 缓存与恢复

- 执行中断后重新运行 `/plan`，会检测到未完成的缓存（`~/.kimi/plan/.cache.json`）
- 提示：`found cache '...', load it and continue? (y/n)`
- 选择 `y` 从上次中断的步骤继续执行
- 选择 `n` 生成新计划并覆盖缓存
- 缓存恢复需同时满足以下条件：
  - 计划文件仍存在且 SHA256 哈希未改变
  - 已完成的步骤数大于 0 且小于总步骤数（即计划处于部分完成状态）

### 执行确认

计划生成后、开始执行前，可选择是否开启执行确认：
- 提示：`Ask after make plan? no for auto accept-all. (y/n)`
- 选择 `y`：在正式开始执行前，打印剩余所有步骤并询问一次确认
- 选择 `n`：自动执行所有步骤，不再询问

---

## 长提示词与错误处理

参考 `src/kimix/utils/prompt.py` 中的 `prompt_async`：

### 自动截断超长提示词

当输入的提示词超过 **65536 字符**时，系统会自动将其导出到临时文件，并将提示词替换为 `read and execute: <temp_file>`，避免超出模型上下文限制。

### 自动重试机制

`prompt_async` 内置了最多 **5 次**的重试逻辑，遇到以下 HTTP 状态码时会自动指数退避等待：

| 状态码 | 行为 |
|--------|------|
| `429` | 请求过快，按 `min(2^attempt, 60)` 秒等待后重试 |
| `400`, `500`, `502`, `503` | 服务端错误，同样触发指数退避 |
| 其他异常 | 等待 1 秒后重试，最后一次直接抛出 |

这意味着在 `/plan` 的长时间执行过程中，偶发的网络波动或 API 限流通常会被自动恢复，无需手动干预。

---

## SetTodoList 工具

`SetTodoList` 是用于跟踪任务进度的工具，在执行过程中自动调用。

### 功能概述

参考 `kimi-cli/src/kimi_cli/tools/todo/__init__.py`：

- **读取模式**（`todos` 为 `null`）：返回当前待办列表
- **写入模式**（提供 `todos` 列表）：更新并持久化待办事项

### 数据结构

```python
class Todo:
    title: str           # 待办事项标题
    status: str          # 状态："pending" | "in_progress" | "done"

class Params:
    todos: list[Todo] | None  # 为 null 时读取，提供时写入
```

### 显示效果

CLI 中以可视化卡片形式展示：

```
┌─────────────────────────────────┐
│ [done] 添加参数校验              │
│ [in_progress] 统一异常类型       │
│ [pending] 添加日志记录           │
└─────────────────────────────────┘
```

### 持久化

- **Root Agent**：保存在会话状态的 `state.todos` 中
- **Sub Agent**：保存在子代理目录的 `state.json` 中

---

## Agent Swarm 多智能体协作

Agent Swarm 通过协调者（Coordinator）将复杂任务拆分为有向无环图（DAG），并调度多个子 Agent 并行执行，最后合并结果。本文档介绍如何在 CLI 中使用 `/swarm` 命令调用该能力。

---

### 交互命令

进入 Kimix 交互终端后，执行：

```
/swarm
```

#### 输入任务描述

执行后进入多行输入模式：

- 以 `/end` 结束输入并提交任务
- 以 `/cancel` 取消当前操作
- 空输入会被忽略并提示跳过

**示例**：

```text
/swarm
>>>> Start input multiple-lines for swarm task, end with /end, cancel with /cancel
请为这个项目生成单元测试，覆盖 src/utils.py 和 src/core.py 的核心函数
/end
```

#### 执行流程

`_cmd_swarm` 内部按以下步骤执行（参考 `src/kimix/cli_impl/commands.py`）：

1. **收集提示词**：将多行文本拼接为完整任务描述 `task_prompt`。
2. **创建 Swarm 会话**：调用 `create_swarm_session(task_prompt)`，由协调者 Agent 根据 `agent_swarm.json` 规划 DAG 节点。
3. **执行 DAG**：使用 `Executor().execute(dag)` 按依赖关系调度各节点运行。
4. **输出结果**：执行完成后打印各节点返回结果；若任一阶段失败，打印对应错误信息。

---

### 状态与输出

| 场景 | 输出 |
|------|------|
| 创建 DAG 成功 | `Swarm session created, DAG has N node(s).` |
| DAG 执行完成 | `Swarm execution completed. Results: ...` |
| 空输入 | `Empty task prompt, skipping swarm command.` |
| 创建失败 | `Failed to create swarm session: ...` |
| 执行失败 | `Swarm execution failed: ...` |
| 取消操作 | `Swarm command cancelled.` |

---

### 典型场景

#### 场景 A：批量代码审查

```text
/swarm
请审查 src/ 目录下所有 Python 文件的代码风格，找出潜在的性能瓶颈和安全隐患，并给出修改建议。
/end
```

协调者会为每个文件或模块创建独立节点，并行执行审查任务。

#### 场景 B：多模块重构

```text
/swarm
将项目中的日志模块从 print 替换为标准 logging，涉及 utils.py、core.py 和 cli.py。
/end
```

协调者会按依赖顺序编排节点，先处理底层模块，再处理上层调用方。

---

### 注意事项

1. **耗时较长**：Swarm 任务涉及多次 LLM 调用与 DAG 调度，请耐心等待。
2. **结果合并**：子 Agent 的输出若涉及文件修改，最终会通过 VFS（虚拟文件系统）合并到统一目录；冲突时由协调者或额外会话仲裁。
3. **错误隔离**：单个节点失败不会阻塞其他独立节点，但最终结果中会包含错误信息。

---

## 方案对比

| 特性 | `/plan` | `/swarm` |
|------|---------|----------|
| **执行方式** | 串行执行 | 并行执行（DAG） |
| **任务拆分** | 线性步骤列表 | 有向无环图 |
| **断点续传** | ✅ 支持 | ❌ 不支持 |
| **进度跟踪** | Steps 可视化 | DAG 节点状态 |
| **适用场景** | 步骤明确、顺序依赖的任务 | 可并行、模块独立的任务 |
| **调用开销** | 较低（单会话） | 较高（多 Agent） |
| **典型用例** | 功能实现、代码重构 | 批量审查、多模块分析 |

### 选择建议

- **使用 `/plan`**：任务有明确的先后顺序，需要确保每一步完成后再进行下一步，或可能需要中断后恢复
- **使用 `/swarm`**：任务可分解为多个相对独立的子任务，希望并行加速执行，或需要多视角协作
