# Multi-Provider Configuration

Kimix now supports routing different agent roles to different LLM providers. Instead of a single optional `sub_provider`, you can declare a `sub_providers` list and tag each entry with a `role`.

---

## Why Use Multiple Providers

| Scenario | Benefit |
|----------|---------|
| Planner consumes too many main-model tokens | Use a cheap/light model for `/plan` |
| Sub-agents need different capabilities | Route coding sub-agents to a coding model, review sub-agents to a reasoning model |
| Cost control | Keep the main model only for final replies |

---

## Config Format

Add a `sub_providers` array to your config file. Each entry is a complete provider dict plus a `role` field.

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

### Supported roles

| Role | Used by |
|------|---------|
| `sub_agent` | `Agent` tool, long-output summarization |
| `planner` | `/plan` command (`prompt_plan_async`) |

If `role` is omitted, it defaults to `sub_agent`.

---

## Backward Compatibility

The old single `sub_provider` field is still parsed but normalized into `sub_providers` internally.

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

This is equivalent to:

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

You can also mix both fields; entries are merged into one normalized list.

---

## How It Works

1. At startup, `kimix` loads the main provider from the config file.
2. It pops `sub_provider` and `sub_providers`, normalizes them, and stores the list via `set_default_sub_providers()`.
3. When a component needs an auxiliary provider, it calls `get_default_sub_provider("<role>")`.
   - If a matching role is found, that provider is used.
   - Otherwise it falls back to the main provider.

---

## Best Practices

1. **Use roles explicitly** — even if you only have one sub-provider, adding `"role": "sub_agent"` makes the intent clear.
2. **Planner can be lightweight** — `/plan` mainly structures tasks; it does not need your strongest model.
3. **Keep required keys** — every sub-provider must include `type`, `model`, `url`, and `max_context_size`. Invalid entries are ignored with a warning.
4. **First match wins** — if multiple entries share the same role, the first one is used.
