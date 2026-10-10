"""FP-02: divergence guard for the two `fetch_url` tools.

`kimi_cli.tools.web.fetch.fetch_url` (C12) and `kimi_cli.tools.web.kimix_fetch.fetch_url`
(K14) share a model-facing name but are **parallel implementations for two disjoint
runtimes**, which is why `tools/gate_dup_allowlist.txt` waives the duplicate:

* C12 - `url`/`timeout`/`method`/`headers`/`body`/`follow_redirects`/`max_redirects`,
  aiohttp transport, URL-safety checks, runs on `kimi_cli.soul.Runtime` + `Config`.
* K14 - `url`/`output_path`, Playwright transport
  (`web_fetcher/fetcher.py::fetch_to_markdown`), runs on `kimi_agent_sdk` + `Session`.

Neither can delegate to the other (C12 cannot be constructed without a kimi_cli
Config/Runtime; kimi_cli has no Playwright at all), and the name `fetch_url` is
load-bearing in the kimix API reference, so a rename is not an option either.

These probes pin both contracts so the two can never *silently* drift together or
apart: any future unification must delete these tests deliberately.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

from kimi_cli.tools.web.fetch import fetch_url as cli_fetch_url
from kimi_cli.tools.web.kimix_fetch import fetch_url as kimix_fetch_url

CLI_MODULE = REPO_ROOT / "kimi-cli/src/kimi_cli/tools/web/fetch.py"
KIMIX_MODULE = REPO_ROOT / "kimi-cli/src/kimi_cli/tools/web/kimix_fetch.py"
API_REF = REPO_ROOT / "kimi-cli/src/kimi_cli/skills/kimix_api/references/api.md"


# --------------------------------------------------------------------------- #
# the collision is deliberate and pinned
# --------------------------------------------------------------------------- #


def test_both_are_tool_classes_named_fetch_url_in_different_modules() -> None:
    assert cli_fetch_url.name == "fetch_url"
    assert kimix_fetch_url.name == "fetch_url"
    assert cli_fetch_url is not kimix_fetch_url
    assert cli_fetch_url.__module__ != kimix_fetch_url.__module__


def test_gate_dup_waives_fetch_url_and_exits_zero() -> None:
    proc = subprocess.run(
        [sys.executable, "tools/gate_dup.py"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "[WAIVED] fetch_url" in out
    assert "G-DUP: PASS" in out


def test_the_waiver_carries_a_rationale() -> None:
    text = (REPO_ROOT / "tools" / "gate_dup_allowlist.txt").read_text(encoding="utf-8")
    entry = next(
        (ln for ln in text.splitlines() if ln.strip().startswith("fetch_url") and "#" in ln),
        None,
    )
    assert entry is not None, "fetch_url must be waived with an inline rationale"
    assert len(entry.split("#", 1)[1].strip()) > 30


# --------------------------------------------------------------------------- #
# the two param surfaces are pinned (they must not converge by accident)
# --------------------------------------------------------------------------- #


def test_kimix_surface_is_url_and_output_path_only() -> None:
    assert set(kimix_fetch_url.params.model_fields) == {"url", "output_path"}
    assert kimix_fetch_url.params.model_fields["output_path"].default is None


def test_cli_surface_is_the_rich_http_one() -> None:
    fields = set(cli_fetch_url.params.model_fields)
    assert fields == {
        "url",
        "timeout",
        "method",
        "headers",
        "body",
        "follow_redirects",
        "max_redirects",
    }


def test_cli_surface_has_no_output_path_and_kimix_has_no_http_controls() -> None:
    assert "output_path" not in cli_fetch_url.params.model_fields
    assert "method" not in kimix_fetch_url.params.model_fields
    assert "headers" not in kimix_fetch_url.params.model_fields
    assert "timeout" not in kimix_fetch_url.params.model_fields


# --------------------------------------------------------------------------- #
# the transports stay distinct
# --------------------------------------------------------------------------- #


def test_kimix_uses_the_playwright_fetcher() -> None:
    text = KIMIX_MODULE.read_text(encoding="utf-8")
    assert "from kimi_cli.tools.web.web_fetcher import fetch_to_markdown" in text
    assert "fetch_to_markdown(params.url)" in text


def test_c12_fetch_has_no_playwright_dependency() -> None:
    """FP-02 (updated for the tool relocation): the two ``fetch_url`` tools stay
    parallel implementations even though K14's Playwright fetcher now lives inside
    kimi-cli.

    * C12 (``kimi_cli.tools.web.fetch``) must remain transport-independent of
      Playwright (aiohttp only).
    * The relocated K14 fetcher (``kimi_cli.tools.web.web_fetcher``) is the ONLY
      place Playwright may be referenced, and it must import it *lazily* (inside
      a function) so Playwright never becomes a hard kimi-cli dependency.
    """
    c12 = (REPO_ROOT / "kimi-cli/src/kimi_cli/tools/web/fetch.py").read_text(
        encoding="utf-8"
    )
    assert "playwright" not in c12.lower(), "C12 fetch must not depend on playwright"

    proc = subprocess.run(
        [
            "git",
            "grep",
            "-l",
            "-E",
            r"^(import playwright|from playwright)|\b(sync_playwright|async_playwright|playwright\.sync_api)\b",
            "--",
            "kimi-cli/src/kimi_cli",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    hits = [line for line in proc.stdout.splitlines() if line.strip()]
    # Any Playwright reference must be confined to the relocated K14 fetcher tree.
    assert all(
        "/web/web_fetcher/" in h or h.endswith("/web/kimix_fetch.py") for h in hits
    ), f"playwright leaked outside the relocated K14 fetcher: {hits}"

    # Playwright must stay a lazy/optional import, never a declared kimi-cli dep.
    pyproject = (REPO_ROOT / "kimi-cli" / "pyproject.toml").read_text(encoding="utf-8")
    assert "playwright" not in pyproject, "playwright must not become a kimi-cli dependency"


def test_kimix_fetch_url_is_documented_with_its_signature() -> None:
    text = API_REF.read_text(encoding="utf-8")
    assert (
        "- `fetch_url` — fetch web page as Markdown; params: `url`, `output_path`" in text
    )
    assert "`fetch_to_markdown(url, wait_until=\"networkidle\")`" in text


# --------------------------------------------------------------------------- #
# behaviour of the kimix tool (its own G4 surface)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_kimix_fetch_failure_returns_tool_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import importlib

    mod = importlib.import_module("kimi_cli.tools.web.kimix_fetch")

    async def _boom(_url: str) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(mod, "fetch_to_markdown", _boom)
    result = await kimix_fetch_url()(mod.Params(url="https://example.invalid/x"))
    assert result.is_error
    assert result.brief == "Failed to fetch https://example.invalid/x"


@pytest.mark.asyncio
async def test_kimix_output_path_writes_the_file_and_creates_parents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import importlib

    mod = importlib.import_module("kimi_cli.tools.web.kimix_fetch")

    async def _markdown(_url: str) -> str:
        return "# Title\n\nbody\n"

    monkeypatch.setattr(mod, "fetch_to_markdown", _markdown)
    target = tmp_path / "nested" / "out.md"
    result = await kimix_fetch_url()(mod.Params(url="https://example.com", output_path=str(target)))
    assert not result.is_error
    assert target.read_text(encoding="utf-8") == "# Title\n\nbody\n"
    assert f"({len('# Title\n\nbody\n')} characters)" in result.output
    assert "\\" not in result.output.split("(")[0]


@pytest.mark.asyncio
async def test_kimix_write_failure_keeps_the_fetched_markdown_in_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import importlib

    mod = importlib.import_module("kimi_cli.tools.web.kimix_fetch")

    async def _markdown(_url: str) -> str:
        return "payload\n"

    monkeypatch.setattr(mod, "fetch_to_markdown", _markdown)
    unwritable = tmp_path / "as_dir"
    unwritable.mkdir()
    result = await kimix_fetch_url()(
        mod.Params(url="https://example.com", output_path=str(unwritable))
    )
    assert result.is_error
    assert result.output == "payload\n"
