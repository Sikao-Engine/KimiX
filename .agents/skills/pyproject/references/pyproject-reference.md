# Repo Pyproject Reference

Full reference for pyproject.toml files in the kimix monorepo: package table, workspace config, tool configs, and block templates.

## Packages

| Path | Name | Build Backend | Key Deps |
|------|------|---------------|----------|
| `./pyproject.toml` | `kimix` | `hatchling` | `numpy`, `playwright`, `fastapi`, `kimi-cli-x` |
| `kimi-cli/pyproject.toml` | `kimi-cli-x` | `uv_build` | `typer`, `aiohttp`, `rich`, plus vendored `kosong`/`kaos` deps (`anthropic`, `openai`, `google-genai`, `mcp`, `asyncssh`, …) |

The former `packages/kosong` (`kosong-x`), `packages/kaos` (`pykaos`), and `packages/kimi-code` were removed; `kosong` and `kaos` are vendored inside `kimi-cli/src/` (`src/kosong/`, `src/kaos/`) and shipped in the `kimi-cli-x` wheel via `module-name = ["kimi_cli", "kosong", "kaos"]`.

## Workspace

Root `tool.uv.workspace` includes: `kimi-cli` (single member; `kosong`/`kaos` live inside it as vendored modules, not workspace packages).
Workspace sources in root: `kimi-cli-x`.

## Common Tool Configs

### Ruff (all packages)

`line-length = 100`, `select = ["E", "F", "UP", "B", "SIM", "I"]`. Root `kimix` also includes `N`, `W`, ignores `E501`.

### Type Checkers

- **pyright** / **ty**: `typeCheckingMode = "strict"`, `pythonVersion = "3.14"`, includes `src/**/*.py`, `tests/**/*.py`.
- **mypy** (root only): strict mode, `disallow_untyped_defs = true`, excludes `tests/`, `scripts/`.

### Build Backends

- `hatchling` (root only). Wheel targets: `src/kimix`, `src/my_tools`.
- `uv_build` (all other packages). Set `module-name` in `tool.uv.build-backend`.

### Scripts

- `kimix` -> `kimix.cli:cli`
- `kimi` / `kimi-cli-x` -> `kimi_cli.__main__:main`

## Optional Deps

- `kimix[office]`: `pymupdf`, `python-docx`
- `kimix[image_process]`: `pillow`
- `kimix[all]`: both of the above plus dev/test tools (`pytest`, `ruff`, `mypy`, …)

## Block Templates

### `[build-system]`

Root uses `hatchling`; subpackages use `uv_build`.

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
# or
requires = ["uv_build>=0.10.0,<0.12.0"]
build-backend = "uv_build"

[tool.uv.build-backend]
module-name = ["kimi_cli"] # package import name
source-exclude = ["tests/**/*"] # omit from sdist/wheel
```

### `[project]`

Core metadata. Always present.

```toml
[project]
name = "kimix"
version = "0.1.9"
description = "..."
readme = "README.md"
license = { text = "MIT" } # or "Apache-2.0"
requires-python = ">=3.14"
authors = [{ name = "...", email = "..." }]
dependencies = ["numpy", "kimi-cli-x>=1.39.1"]
```

### `[project.optional-dependencies]`

Feature flags installable as `pkg[extra]`.

```toml
[project.optional-dependencies]
office = ["pymupdf>=1.23.0", "python-docx>=1.1.0"]
image_process = ["pillow>=10.0.0"]
```

### `[dependency-groups]`

`uv`-native dev dependencies (not packaged into wheel).

```toml
[dependency-groups]
dev = ["pytest>=9.0.2", "ruff>=0.14.10", "pyright>=1.1.407"]
```

### `[tool.uv.workspace]` / `[tool.uv.sources]`

Root workspace declaration. Subpackages become editable installs.

```toml
[tool.uv.workspace]
[tool.uv.workspace]
members = ["kimi-cli"]
[tool.uv.sources]
kimi-cli-x = { workspace = true }
```

### `[tool.uv.index]`

Mirror config (root only).

```toml
[[tool.uv.index]]
url = "https://mirrors.aliyun.com/pypi/simple/"
default = true
```

### `[project.scripts]`

CLI entrypoints.

```toml
[project.scripts]
kimix = "kimix.cli:cli"
kimi = "kimi_cli.__main__:main"
```

### `[tool.ruff]`

```toml
[tool.ruff]
target-version = "py314" # or omitted
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "UP", "B", "SIM", "I"]
ignore = ["E501"]

[tool.ruff.lint.per-file-ignores]
"src/kimi_cli/web/api/**/*.py" = ["B008"]
```

### `[tool.pyright]` / `[tool.ty]`

```toml
[tool.pyright]
typeCheckingMode = "strict"
pythonVersion = "3.14"
include = ["src/**/*.py", "tests/**/*.py"]

[tool.ty.environment]
python-version = "3.14"

[tool.ty.src]
include = ["src/**/*.py", "tests/**/*.py"]
```

### `[tool.mypy]`

Root only. Strict.

```toml
[tool.mypy]
strict = true
disallow_untyped_defs = true
disallow_any_generics = true
show_error_codes = true
ignore_missing_imports = true
exclude = ["tests/", "scripts/"]
```

### `[tool.pytest.ini_options]`

Root only.

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```
