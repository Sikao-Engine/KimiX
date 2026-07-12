# 多 Provider 配置

Kimix 现在支持将不同 Agent 角色路由到不同的 LLM Provider。你不再只能配置一个可选的 `sub_provider`，而是可以声明一个带 `role` 标签的 `sub_providers` 列表。

---

## 为什么使用多 Provider

| 场景 | 收益 |
|------|------|
| Planner 消耗主模型 token 过多 | 为 `/plan` 使用更轻量的模型 |
| 子代理需要不同能力 | 将编码子代理路由到编程模型，审查子代理路由到推理模型 |
| 成本控制 | 仅在最终回复中使用最强主模型 |

---

## 配置格式

在配置文件中加入 `sub_providers` 数组。每个条目都是一个完整的 Provider 字典，并额外包含 `role` 字段。

```json
{
  "model": "kimi-for-coding",
  "max_context_size": 262144,
  "capabilities": ["thinking"],
  "url": "https://api.kimi.com/coding/v1",
  "type": "kimi",
  "api_key": "sk-main",
  "sub_providers": [
    {
      "role": "sub_agent",
      "model": "kimi-k2.6",
      "max_context_size": 200000,
      "capabilities": ["thinking"],
      "url": "https://api.moonshot.cn/v1/chat/completions",
      "type": "kimi",
      "api_key": "sk-sub"
    },
    {
      "role": "planner",
      "model": "claude-opus-4-6",
      "max_context_size": 200000,
      "capabilities": ["thinking"],
      "url": "https://api.minimaxi.com/anthropic",
      "type": "anthropic",
      "api_key": "sk-plan"
    }
  ]
}
```

`model` 字段可以是 `kimi-for-coding` 或 `kimi-for-coding-highspeed`，两者均受支持。

### 支持的角色
| 角色 | 使用者 |
|------|--------|
| `sub_agent` | `Agent` 工具、超长输出总结 |
| `planner` | `/plan` 命令 (`prompt_plan_async`) |

如果省略 `role`，默认值为 `sub_agent`。

---

## 向后兼容

旧的单一 `sub_provider` 字段仍然会被解析，并在内部被规范化为 `sub_providers`。

```json
{
  "sub_provider": {
    "role": "sub_agent",
    "model": "...",
    "type": "...",
    "url": "...",
    "max_context_size": 200000,
    "api_key": "..."
  }
}
```

等价于：

```json
{
  "sub_providers": [
    {
      "role": "sub_agent",
      "model": "...",
      "type": "...",
      "url": "...",
      "max_context_size": 200000,
      "api_key": "..."
    }
  ]
}
```

你也可以同时混用两个字段；所有条目会被合并到一个规范化列表中。

---

## 工作原理

1. 启动时，`kimix` 从配置文件加载主 Provider。
2. 它取出 `sub_provider` 和 `sub_providers`，进行规范化，然后通过 `set_default_sub_providers()` 保存列表。
3. 当某个组件需要辅助 Provider 时，调用 `get_default_sub_provider("<role>")`。
   - 如果找到匹配角色，则使用该 Provider。
   - 否则回退到主 Provider。

---

## 最佳实践

1. **显式声明角色** —— 即使只有一个 sub-provider，加上 `"role": "sub_agent"` 也能让意图更清晰。
2. **Planner 可以用轻量模型** —— `/plan` 主要负责任务拆解，不一定需要最强模型。
3. **保留必填字段** —— 每个 sub-provider 必须包含 `type`、`model`、`url` 和 `max_context_size`，缺少的条目会被忽略并弹出警告。
4. **首个匹配生效** —— 如果同一角色出现多次，使用第一个匹配的条目。
