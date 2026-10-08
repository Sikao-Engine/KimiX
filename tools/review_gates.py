"""Per-tool mechanical gate harness for the built-in tools review (plan §3 G1/G3/G5).

Runs the *mechanical* part of every per-tool gate block — the parts a machine can
decide — and writes the raw output to `reviews/tools/gates/<ID>.txt` (which the
per-tool reviewer then reads instead of re-running everything) plus a
machine-readable `reviews/tools/gates/<ID>.json` summary.

What it runs per tool ID:

* ``G1``: ``ruff check`` and ``ruff check --select C901 --max-complexity=12`` over
  the in-scope files, plus ``tools/review_metrics.py --json --max-fn-loc 120
  --max-complexity 12``.
* ``G3``: the in-scope test files, with ``--cov`` on the tool's primary module
  when a dotted module can be derived (``pytest-cov`` is fetched ephemerally).
* ``G5``: the in-scope test files (plain run) — recorded separately from coverage.

Each evidence file starts with ``git rev-parse HEAD`` then ``git status
--porcelain`` so G6 can bind the numbers to an exact commit.

Exit code is always 0: this records evidence, it does not judge. The reviewer
owns the verdict.

Usage::

    uv run tools/review_gates.py --id C08 [--id K04 ...]
    uv run tools/review_gates.py --all
    uv run tools/review_gates.py --all --only-mechanical     # skip pytest/coverage
"""

from __future__ import annotations

import argparse
import glob as globmod
import os
import subprocess
import sys
import time
from pathlib import Path

import orjson

ROOT = Path(__file__).resolve().parent.parent
GATES = ROOT / "reviews" / "tools" / "gates"

sys.path.insert(0, str(ROOT / "tools"))
from gen_inventory import INVENTORY  # noqa: E402

# Tool ID -> dotted module used for --cov (the tool's primary module).
COV_MODULE: dict[str, str] = {
    "C00": "kimi_cli.tools",
    "C01": "kimi_cli.tools.agent",
    "C02": "kimi_cli.tools.ask_user",
    "C03": "kimi_cli.tools.todo",
    "C04": "kimi_cli.tools.memory",
    "C05": "kimi_cli.tools.file.read",
    "C06": "kimi_cli.tools.file.read_media",
    "C07": "kimi_cli.tools.file.glob",
    "C08": "kimi_cli.tools.file.grep_local",
    "C09": "kimi_cli.tools.file.write",
    "C10": "kimi_cli.tools.file.edit",
    "C11": "kimi_cli.tools.web.search",
    "C12": "kimi_cli.tools.web.fetch",
    "C13": "kimi_cli.tools.web.extract",
    "C14": "kimi_cli.tools.context_prune",
    "C15": "kimi_cli.tools.file.hash_line",
    "C16": "kimi_cli.tools.test",
    "K00": "kimix.tools.common",
    "K01": "kimix.tools.agent",
    "K02": "kimix.tools.agent",
    "K03": "kimix.tools.agent",
    "K04": "kimix.tools.file.bash.bash_tool",
    "K05": "kimix.tools.file.bash.pwsh_tool",
    "K06": "kimix.tools.file.run",
    "K07": "kimix.tools.py",
    "K08": "kimix.tools.background",
    "K09": "kimix.tools.context",
    "K10": "kimix.tools.note",
    "K11": "kimix.tools.note",
    "K12": "kimix.tools.note",
    "K13": "kimix.tools.swarm",
    "K14": "kimix.tools.web.fetch_url",
    "K15": "kimix.tools.parser",
    "K16": "kimix.tools.file.find_str",
    "K17": "kimix.tools.file",
    "K18": "kimix.tools.file",
}

# Tools that share a probe file / suite with a sibling (report says so).


def _run(cmd: list[str], timeout: int = 900) -> tuple[int, str]:
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        out = f"<<TIMEOUT after {timeout}s>>"
        rc = -9
    except FileNotFoundError as exc:  # pragma: no cover
        out = f"<<NOT FOUND: {exc}>>"
        rc = -1
    return rc, f"{out.rstrip()}\n[elapsed {time.time() - started:.1f}s, rc={rc}]\n"


def _expand(patterns: list[str]) -> list[str]:
    """Expand globs to repo-relative posix-style paths, dropping missing ones."""
    out: list[str] = []
    for pat in patterns:
        if any(ch in pat for ch in "*?["):
            hits = globmod.glob(pat, root_dir=ROOT)
            out.extend(sorted(h.replace("\\", "/") for h in hits))
        elif (ROOT / pat).exists():
            out.append(pat.replace("\\", "/"))
    seen: set[str] = set()
    uniq: list[str] = []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def _emit(fh, title: str, body: str) -> None:
    fh.write(f"\n=== {title} ===\n{body}")


def _lint_gates(fh, tid: str, py_files: list[str], summary: dict) -> None:
    if not py_files:
        for key in ("ruff", "c901", "metrics"):
            summary[key] = "not-run"
        summary["complexity_max"] = summary["longest_fn_loc"] = 0
        summary["module_loc"] = 0
        summary["functions_over_120_loc"] = 0
        summary["functions_over_12_cx"] = 0
        summary["metrics_breaches"] = []
        _emit(fh, "metrics", "no in-scope .py files\n")
        return

    rc, out = _run(["uv", "run", "ruff", "check", *py_files])
    _emit(fh, f"uv run ruff check {' '.join(py_files)}", out)
    summary["ruff_rc"] = rc
    summary["ruff"] = "pass" if rc == 0 else "fail"

    rc, out = _run(
        [
            "uv", "run", "ruff", "check", "--select", "C901",
            "--config", "lint.mccabe.max-complexity=12", *py_files,
        ]
    )
    _emit(fh, "uv run ruff check --select C901 --max-complexity=12", out)
    summary["c901_rc"] = rc
    summary["c901"] = "pass" if rc == 0 else "fail"

    rc, out = _run(
        [
            "uv", "run", "tools/review_metrics.py", *py_files, "--json",
            "--max-fn-loc", "120", "--max-complexity", "12",
        ]
    )
    _emit(fh, "uv run tools/review_metrics.py --json --max-fn-loc 120", out)
    metrics: dict = {}
    try:
        metrics = orjson.loads(out.split("\n[elapsed")[0])
    except Exception:  # noqa: BLE001
        metrics = {}
    _summarize_metrics(metrics, rc, summary)


def _summarize_metrics(metrics: dict, rc: int, summary: dict) -> None:
    fns = [f for r in metrics.get("files", []) for f in r["functions"]]
    summary["metrics_rc"] = rc
    summary["metrics"] = "pass" if rc == 0 else "fail"
    summary["complexity_max"] = max((f["complexity"] for f in fns), default=0)
    summary["longest_fn_loc"] = max((f["loc"] for f in fns), default=0)
    summary["longest_fn"] = max(((f["loc"], f["name"]) for f in fns), default=(0, None))[1]
    summary["module_loc"] = sum(r["loc"] for r in metrics.get("files", []))
    summary["functions_over_120_loc"] = sum(1 for f in fns if f["loc"] > 120)
    summary["functions_over_12_cx"] = sum(1 for f in fns if f["complexity"] > 12)
    summary["metrics_breaches"] = metrics.get("breaches", [])


def _test_gates(fh, tid: str, tests: list[str], summary: dict, only_mechanical: bool) -> None:
    if not tests or only_mechanical:
        summary["pytest"] = "not-run" if not tests else "skipped-mechanical"
        summary["cov"] = "not-run"
        return
    rc, out = _run(["uv", "run", "pytest", *tests, "-q", "-rs"])
    _emit(fh, f"uv run pytest {' '.join(tests)} -q -rs", out)
    summary["pytest_rc"] = rc
    summary["pytest"] = "pass" if rc == 0 else "fail"

    cov = COV_MODULE.get(tid)
    if not cov:
        summary["cov"] = "not-run"
        return
    rc, out = _run(
        [
            "uv", "run", "--with", "pytest-cov", "pytest", *tests,
            f"--cov={cov}", "--cov-branch", "--cov-report=term-missing", "-q",
        ]
    )
    _emit(fh, f"uv run --with pytest-cov pytest {' '.join(tests)} --cov={cov}", out)
    summary["cov_rc"] = rc
    summary["cov"] = "pass" if rc == 0 else "fail"
    for line in out.splitlines():
        if cov in line and "%" in line:
            summary["cov_line"] = line.strip()


def gate_one(tid: str, only_mechanical: bool) -> dict:
    tool, sources, test_pats = INVENTORY[tid]
    files = _expand(sources)
    py_files = [f for f in files if f.endswith(".py")]
    tests = _expand(test_pats)
    GATES.mkdir(parents=True, exist_ok=True)
    ev = GATES / f"{tid}.txt"

    _, sha = _run(["git", "rev-parse", "HEAD"])
    _, porcelain = _run(["git", "status", "--porcelain"])

    summary: dict = {
        "id": tid,
        "tool": tool,
        "files": files,
        "tests": tests,
        "cov_module": COV_MODULE.get(tid),
        "sha": sha.strip(),
    }

    with ev.open("w", encoding="utf-8") as fh:
        fh.write(sha)
        fh.write(porcelain)
        fh.write("\n")
        _lint_gates(fh, tid, py_files, summary)
        _test_gates(fh, tid, tests, summary, only_mechanical)

    (GATES / f"{tid}.json").write_bytes(orjson.dumps(summary, option=orjson.OPT_INDENT_2))
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Per-tool mechanical gate harness.")
    parser.add_argument("--id", action="append", default=[], help="tool id (repeatable)")
    parser.add_argument("--all", action="store_true", help="every tool id")
    parser.add_argument(
        "--only-mechanical", action="store_true", help="skip pytest/coverage (fast)"
    )
    args = parser.parse_args(argv)

    ids = sorted(INVENTORY) if args.all else args.id
    if not ids:
        parser.error("pass --all or at least one --id")
    for tid in ids:
        if tid not in INVENTORY:
            parser.error(f"unknown tool id {tid!r}")
        summary = gate_one(tid, args.only_mechanical)
        print(
            f"{tid}: ruff={summary.get('ruff')} c901={summary.get('c901')} "
            f"metrics={summary.get('metrics')} cx_max={summary.get('complexity_max')} "
            f"longest={summary.get('longest_fn_loc')} pytest={summary.get('pytest')} "
            f"cov={summary.get('cov')}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
