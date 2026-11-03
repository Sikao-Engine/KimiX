r"""LIVE end-to-end check: a worker LLM drives ``research`` into a report file.

This script answers a single question in a **fresh process** (the running agent
cannot pick up code changes): does a real model, given the worker toolset, call
the ``research`` tool, does the read-only researcher child write its report
inside its ``.kimix_cache/`` sandbox through the cache-only ``write``/``edit``
mode, and does the tool result hand those artifact paths back to the parent?

What it does
------------
1. Loads a provider dict from ``C:/dev/ds_flash.json`` (``--provider`` to
   override). The API key is **never** printed: only a ``len=NN, head=xxxx…``
   fingerprint appears, and the raw dict is never logged.
2. Creates a throwaway work dir holding ``research_source.md`` — the only input
   the researcher may read.
3. Creates a worker session (``agent_worker.json``, which owns ``research``) and
   prompts the model to call ``research`` exactly once, asking the researcher to
   save a report to the relative path ``e2e-report.md``.
  4. Verifies from the session history and the filesystem:
     * the parent actually called ``research`` and its result is not an error;
     * the result carries the ``## Research artifacts`` section listing
       ``.kimix_cache/research/<child session id>/e2e-report.md``;
     * that file exists with real content;
     * the child left **no** file outside ``.kimix_cache/``.

Usage (from the repo root, Windows paths with backslashes)::

    cd D:\kimi-agent
    uv run python scripts\verify_research_report_e2e.py
    uv run python scripts\verify_research_report_e2e.py --verbose --timeout 600

Exit code is 0 only when every check passes. ``--keep`` keeps the work dir for
inspection.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import orjson

DEFAULT_PROVIDER = Path(r"C:/dev/ds_flash.json")
CACHE_DIR = ".kimix_cache"
REPORT_REL = "e2e-report.md"
SOURCE_NAME = "research_source.md"
SOURCE_TEXT = (
    "# Quarterly platform notes\n"
    "\n"
    "- Latency budget: p99 under 300 ms for the read path.\n"
    "- Storage: single SQLite file per work directory, no external service.\n"
    "- Rollout: feature flags default off; on-call owns the ramp.\n"
    "- Risk: the ledger writes must never block compaction.\n"
)


def _mask(secret: Any) -> str:
    """A length-only fingerprint; no key material is ever returned."""
    if not isinstance(secret, str):
        return "<none>"
    return f"len={len(secret)}, value hidden"


class Checker:
    """Accumulates named checks so one failure does not hide the others."""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        if ok:
            self.passed.append(name)
            print(f"  [ok]   {name}" + (f" — {detail}" if detail else ""))
        else:
            self.failures.append(name)
            print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))
        return ok


def _load_provider(path: Path) -> dict[str, Any]:
    data = orjson.loads(path.read_bytes())
    if not isinstance(data, dict):
        raise SystemExit(f"provider file {path} does not contain a JSON object")
    return data


def _research_prompt(work_dir: Path, timeout: int) -> str:
    return (
        "Call the `research` tool exactly once and nothing else.\n"
        "Use this query for it (verbatim):\n"
        "---\n"
        f"Read the file `{SOURCE_NAME}` in the working directory.\n"
        "Save a 4-6 line report of its key points with the `write` tool, using the\n"
        f"relative path `{REPORT_REL}` (a relative path is required; it is stored\n"
        "under `.kimix_cache/`). Then return the report text as your findings.\n"
        "---\n"
        "Do not call any other tool yourself. When the research tool returns, "
        "reply with the exact artifact paths it listed under 'Research artifacts'.\n"
        f"You have at most {timeout} seconds."
    )


async def _run(
    work_dir: Path, provider: dict[str, Any], timeout: int, verbose: bool
) -> tuple[Any, str]:
    from kaos.path import KaosPath

    import kimix.base as base
    from kimix.utils import _create_session_async, prompt_async

    base._quiet = True
    base.set_default_provider(provider)
    # yolo: there is no user in this verification process to answer approvals.
    base.set_default_yolo(True)

    session = await _create_session_async(
        work_dir=KaosPath(str(work_dir)),
        agent_file=base._default_agent_file_dir / "agent_worker.json",
        provider_dict=provider,
    )

    streamed: list[str] = []

    def output_function(text: str, msg_type: Any) -> None:
        if text:
            streamed.append(text)
            if verbose:
                # Streamed model text only; the provider key is never part of it.
                print(f"    | {text[:400]}")

    started = time.monotonic()
    await prompt_async(
        prompt_str=_research_prompt(work_dir, timeout),
        session=session,
        output_function=output_function,
        info_print=verbose,
        merge_wire_messages=True,
        format_output=True,
        ensure_todo_finished=False,
        timeout=float(timeout),
    )
    print(f"  prompt finished in {time.monotonic() - started:.1f}s")
    # Collect the evidence *before* closing: a closed session no longer exposes
    # its history, and closing must happen on the loop that owns it.
    from kimix.utils import close_session_async, collect_tool_invocations

    invocations = collect_tool_invocations(session)
    try:
        await close_session_async(session)
    except Exception:
        pass
    return invocations, "\n".join(streamed)


def _verify(
    checks: Checker,
    invocations: list[Any],
    work_dir: Path,
    parent_text: str,
) -> None:
    by_name: dict[str, list[Any]] = {}
    for invocation in invocations:
        by_name.setdefault(invocation.tool_name, []).append(invocation)

    # 1. The parent model actually reached for the research tool.
    research_calls = by_name.get("research", [])
    checks.check(
        "parent called the `research` tool",
        bool(research_calls),
        f"{len(research_calls)} call(s); tools used: {sorted(by_name)}",
    )
    if not research_calls:
        return

    call = research_calls[-1]
    payload = call.output or call.message or ""
    checks.check(
        "research tool result is not an error",
        call.has_result and not call.is_error,
        (call.message or "")[:200],
    )

    # 2. The result reports the child's artifact paths.  The sandbox is
    #    per-session, so the full path is only known after parsing the result.
    checks.check(
        "result lists the 'Research artifacts' section",
        "## Research artifacts" in payload,
        payload[-400:].replace("\n", " ⏎ "),
    )
    artifacts = [
        line[2:].strip().strip("`")
        for line in payload.splitlines()
        if line.startswith(f"- {CACHE_DIR}/")
    ]
    expected = next((p for p in artifacts if p.endswith(f"/{REPORT_REL}")), "")
    checks.check(
        f"result lists `*/{REPORT_REL}`",
        bool(expected),
        f"listed: {artifacts}",
    )
    checks.check(
        "artifact path is a per-session research sandbox",
        expected.startswith(f"{CACHE_DIR}/research/"),
        expected,
    )

    # 3. The child really wrote the report inside its sandbox.
    report = work_dir / expected if expected else None
    exists = report.is_file() if report is not None else False
    size = report.stat().st_size if exists and report is not None else 0
    checks.check(f"`{expected or REPORT_REL}` exists on disk", exists, f"{size} bytes")
    if report is not None and exists:
        text = report.read_text(encoding="utf-8", errors="replace")
        checks.check(
            "the report has real content",
            len(text.strip()) >= 20,
            text.strip().splitlines()[0][:120] if text.strip() else "<empty>",
        )

    # 4. Nothing escaped the sandbox: the work dir only holds our source file,
    #    the sandbox itself and session bookkeeping.
    stray: list[str] = []
    if work_dir.is_dir():
        for path in work_dir.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(work_dir).as_posix()
            if rel == SOURCE_NAME or rel.startswith(f"{CACHE_DIR}/"):
                continue
            stray.append(rel)
    checks.check(
        "child wrote nothing outside `.kimix_cache/`",
        not stray,
        f"unexpected files: {stray[:5]}" if stray else "",
    )

    # 5. The parent's final answer mentions the artifact path it was given.
    checks.check(
        "parent conversation surfaces an artifact path",
        REPORT_REL in parent_text,
        parent_text.strip()[-300:].replace("\n", " ⏎ "),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default=str(DEFAULT_PROVIDER))
    parser.add_argument("--timeout", type=int, default=420)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--keep", action="store_true", help="keep the work dir")
    parser.add_argument(
        "--work-dir",
        default=None,
        help="use this work dir instead of a fresh temp dir",
    )
    args = parser.parse_args()

    # Same proxy caveat as scripts/verify_web_search_live.py: the sandbox
    # resolves public hosts to a fake-IP range that the SSRF guard blocks.
    os.environ.setdefault("KIMI_ALLOW_PRIVATE_URLS", "1")

    provider_path = Path(args.provider)
    print(f"provider file: {provider_path}")
    if not provider_path.is_file():
        print(f"FAIL: provider file not found: {provider_path}")
        return 1
    provider = _load_provider(provider_path)
    # Never print the dict itself: mask every credential-shaped value.
    print(
        "provider: "
        f"model={provider.get('model')!r} type={provider.get('type')!r} "
        f"url={provider.get('url')!r} api_key=({_mask(provider.get('api_key'))})"
    )

    if args.work_dir:
        work_dir = Path(args.work_dir).resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
        keep = True
    else:
        work_dir = Path(tempfile.mkdtemp(prefix="kimix_research_e2e_"))
        keep = args.keep
    (work_dir / SOURCE_NAME).write_text(SOURCE_TEXT, encoding="utf-8")
    print(f"work dir: {work_dir}")

    checks = Checker()
    invocations: list[Any] = []
    parent_text = ""
    try:
        invocations, parent_text = asyncio.run(
            _run(work_dir, provider, args.timeout, args.verbose)
        )
        _verify(checks, invocations, work_dir, parent_text)
    except Exception as exc:  # noqa: BLE001 - report, never leak the provider
        checks.check("end-to-end run completed", False, f"{type(exc).__name__}: {exc}")

    print()
    print(f"passed: {len(checks.passed)}  failed: {len(checks.failures)}")
    if checks.failures:
        print("FAILED: " + "; ".join(checks.failures))
        print(f"work dir kept for inspection: {work_dir}")
        return 1
    if not keep:
        shutil.rmtree(work_dir, ignore_errors=True)
    else:
        print(f"work dir kept: {work_dir}")
    print("PASS: research report workflow verified against a live model")
    return 0


if __name__ == "__main__":
    sys.exit(main())
