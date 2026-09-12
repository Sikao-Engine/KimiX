name: pyproject
description: Reference for pyproject.toml files in the kimix monorepo. Use when modifying, adding, or analyzing Python package configs, workspace dependencies, build systems, or linting rules.

Repo Pyproject Overview

uv workspace with 2 Python projects: the root `kimix` app and `kimi-cli-x` (which vendors the former `kosong-x`/`pykaos` packages).

Packages

| Path | Name | Build Backend | Key Deps |
|------|------|---------------|----------|
| ./pyproject.toml | kimix | hatchling | numpy, playwright, fastapi, kimi-cli-x |
| kimi-cli/pyproject.toml | kimi-cli-x | uv_build | typer, aiohttp, rich, anthropic, openai, google-genai, mcp, asyncssh (vendored kosong/kaos deps) |

The former `kimi-cli/packages/kosong` (`kosong-x`), `kimi-cli/packages/kaos` (`pykaos`), and `kimi-cli/packages/kimi-code` were removed. `kosong` and `kaos` now live as vendored top-level modules under `kimi-cli/src/` and ship inside the `kimi-cli-x` wheel (`module-name = ["kimi_cli", "kosong", "kaos"]`).

Workspace

`tool.uv.workspace` members: `kimi-cli` (single member). Workspace sources: `kimi-cli-x`.

Common Tool Configs

- Ruff (all): line-length = 100, select = ["E", "F", "UP", "B", "SIM", "I"]; root adds N, W, ignores E501.
- pyright/ty: strict, pythonVersion = "3.14", src/**/*.py + tests/**/*.py.
- mypy (root only): strict, excludes tests/, scripts/.
- Build backends: hatchling (root, wheel targets src/kimix, src/my_tools); uv_build (kimi-cli, module-name = ["kimi_cli", "kosong", "kaos"]).
- Scripts: kimix → kimix.cli:cli; kimi/kimi-cli-x → kimi_cli.__main__:main.

Optional Deps

kimix[office]: pymupdf, python-docx · kimix[image_process]: pillow · kimix[all]: both + dev/test tools.

Full TOML block templates ([build-system], [project], [tool.ruff], [tool.pyright], etc.): read references/pyproject-reference.md.
