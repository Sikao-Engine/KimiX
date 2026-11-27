---
name: pyproject
description: Reference for pyproject.toml files in the kimix monorepo. Use when modifying, adding, or analyzing Python package configs, workspace dependencies, build systems, or linting rules.
---

# Repo Pyproject Overview

uv workspace with 2 Python projects: the root `kimix` app and `kimi-cli-x` (which vendors the former `kosong-x`/`pykaos` packages).

## Packages

| Path | Name | Build Backend | Key Deps |
|------|------|---------------|----------|
| ./pyproject.toml | kimix | hatchling | numpy, playwright, fastapi, kimi-cli-x |
| kimi-cli/pyproject.toml | kimi-cli-x | uv_build | typer, aiohttp, rich, anthropic, openai, google-genai, mcp, asyncssh (vendored kosong/kaos deps) |

The former `kimi-cli/packages/kosong` (`kosong-x`), `kimi-cli/packages/kaos` (`pykaos`), and `kimi-cli/packages/kimi-code` were removed. `kosong` and `kaos` now live as vendored top-level modules under `kimi-cli/src/` and ship inside the `kimi-cli-x` wheel (`module-name = ["kimi_cli", "kosong", "kaos"]`).

## Root Wheel Targets

- `[tool.hatch.build.targets.wheel]`: `packages = ["src/kimix", "src/kimi_agent_sdk"]`, `sources = ["src"]`.
- `[tool.hatch.build.targets.wheel.force-include]`: `bin/kimix_native` → `kimix_native` (the pure-Python reference shim that `src/kimix` re-exports; the compiled extension stays optional).

## Workspace

`tool.uv.workspace` members: `kimi-cli` (single member). Workspace sources: `kimi-cli-x = { workspace = true }`. Index: `https://mirrors.aliyun.com/pypi/simple/` (default). `link-mode = "symlink"`.

## Common Tool Configs

- Ruff (root): `target-version = "py315"`, `line-length = 100`, `select = ["E", "F", "I", "N", "W"]`, `ignore = ["E501"]`.
- Ruff (kimi-cli): `line-length = 100`, `select = ["E", "F", "UP", "B", "SIM", "I"]`, `per-file-ignores` drop `E501` under `tests/**` and `tests_e2e/**`.
- mypy (root only): `strict = true`, `python_version = "3.15"`, `ignore_missing_imports = true`, `exclude = ["tests/", "scripts/"]`, plus explicit `disallow_*`/`warn_*` flags.
- pyright (kimi-cli only): `typeCheckingMode = "standard"`, `pythonVersion = "3.15"`, includes `src/**`, `tests/**`, `tests_ai/scripts/**`, `tests_e2e/**`. The root project has no pyright/ty config.
- Build backends: hatchling (root, wheel targets `src/kimix` + `src/kimi_agent_sdk`); uv_build (kimi-cli, `module-name = ["kimi_cli", "kosong", "kaos"]`).
- Scripts: `kimix` → `kimix.cli:cli`; `kimi` / `kimi-cli-x` → `kimi_cli.__main__:main`.
- pytest (root): `asyncio_mode = "auto"`, `testpaths = ["tests"]`, `pythonpath = ["."]`.
- Dev dependency-group: pytest, pytest-asyncio, inline-snapshot, ruff, mypy, respx, xlwt, autopep8 + pygls (`>=1.3,<2`), cattrs, typing_extensions.

## Optional Deps

- `kimix[office]`: pymupdf, python-docx
- `kimix[image_process]`: pillow
- `kimix[all]`: both + dev/test tools (pytest, pytest-asyncio, ...)

## References

Full TOML block templates (`[build-system]`, `[project]`, `[tool.ruff]`, `[tool.pyright]`, etc.): read `references/pyproject-reference.md`.

## Rules

- After editing any `pyproject.toml`, run `uv sync --extra=all` and check it succeeds.
