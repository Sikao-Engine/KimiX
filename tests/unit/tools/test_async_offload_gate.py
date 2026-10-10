"""FP-12 probes: blocking file/subprocess work must not run on the event loop.

`FindStr.__call__` and the swarm's `run_parallel_sample`/`best_of_n` are `async`,
but they drove `os.walk`/`os.listdir`/`open`/`shutil.copytree`/`git subprocess`
directly. On a large tree that starves every other coroutine (streaming, other
tools) for the whole scan.

Rather than assert wall-clock timing (flaky), these probes pin the *structure*:
every blocking call site must be reached through `anyio.to_thread.run_sync`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
FIND_STR = REPO_ROOT / "kimi-cli/src/kimi_cli/tools/file/find_str.py"
BEST_OF_N = REPO_ROOT / "kimi-cli/src/kimi_cli/tools/swarm/best_of_n.py"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _offloaded_arg_ids(tree: ast.Module) -> set[int]:
    """id() of every Name passed as an offload argument (run_sync / _offload)."""
    ok: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_run_sync = isinstance(func, ast.Attribute) and func.attr == "run_sync"
        is_offload = isinstance(func, ast.Name) and func.id == "_offload"
        if is_run_sync:
            for arg in node.args[:1]:
                ok.add(id(arg))
        elif is_offload:
            for arg in node.args:
                ok.add(id(arg))
    return ok


def _async_body_lines(tree: ast.Module) -> set[int]:
    """Every line that belongs to an `async def` body."""
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            end = node.end_lineno or node.lineno
            lines.update(range(node.lineno, end + 1))
    return lines


def _blocking_calls_on_the_loop(tree: ast.Module, blocking: set[str]) -> list[tuple[int, str]]:
    """Calls to *blocking* helpers that execute on the event loop.

    A call is on the loop when it appears inside an `async def` body and is NOT
    handed to `anyio.to_thread.run_sync` / the local `_offload` wrapper. Calls
    inside ordinary sync helpers are fine: those helpers themselves run in a
    worker thread when their caller offloads them.
    """
    offloaded = _offloaded_arg_ids(tree)
    async_lines = _async_body_lines(tree)
    bad: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in blocking
            and node.lineno in async_lines
            and id(node.func) not in offloaded
        ):
            bad.append((node.lineno, node.func.id))
    return bad


def test_find_str_offloads_the_walk_and_the_scan() -> None:
    bad = _blocking_calls_on_the_loop(_tree(FIND_STR), {"find_files", "find_in_file"})
    assert bad == [], f"FindStr runs these blocking helpers on the event loop: {bad}"


def test_best_of_n_offloads_workspace_work() -> None:
    expected = {
        "create_worker_workspace",
        "_snapshot_files",
        "collect_diff",
        "cleanup_worker_workspace",
        "apply_diff_to_workspace",
    }
    bad = _blocking_calls_on_the_loop(_tree(BEST_OF_N), expected)
    assert bad == [], f"these blocking helpers run on the event loop: {bad}"


def test_anyio_is_imported_where_used() -> None:
    for path in (FIND_STR, BEST_OF_N):
        tree = _tree(path)
        imported = any(
            isinstance(n, ast.Import) and any(a.name == "anyio" for a in n.names)
            for n in ast.walk(tree)
        )
        assert imported, f"{path.name} uses anyio.to_thread but does not import anyio"


def test_the_offloading_happens_inside_async_functions() -> None:
    """Sanity: the run_sync calls live in `async def`s (a sync caller needs none)."""
    tree = _tree(BEST_OF_N)
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == "run_sync":
                return
    pytest.fail("no anyio.to_thread.run_sync call found inside an async function")


# --------------------------------------------------------------------------- #
# functional: FindStr still behaves
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_find_str_still_finds_matches(tmp_path: Path) -> None:
    from kimi_cli.tools.file.find_str import FindStr, FindStrParams

    (tmp_path / "a.txt").write_text("needle here\nnothing\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("also a needle\n", encoding="utf-8")
    (tmp_path / "c.txt").write_text("absent\n", encoding="utf-8")

    result = await FindStr()(FindStrParams(path=str(tmp_path), content="needle"))
    assert not result.is_error
    assert "Found 2 match(es)" in result.output
    assert "a.txt" in result.output and "b.txt" in result.output


@pytest.mark.asyncio
async def test_find_str_case_sensitivity_still_works(tmp_path: Path) -> None:
    from kimi_cli.tools.file.find_str import FindStr, FindStrParams

    (tmp_path / "a.txt").write_text("NEEDLE\n", encoding="utf-8")

    insensitive = await FindStr()(FindStrParams(path=str(tmp_path), content="needle"))
    assert "Found 1 match(es)" in insensitive.output

    sensitive = await FindStr()(
        FindStrParams(path=str(tmp_path), content="needle", case_sensitive=True)
    )
    assert "No matches found" in sensitive.output


@pytest.mark.asyncio
async def test_find_str_handles_a_missing_path_without_raising(tmp_path: Path) -> None:
    from kimi_cli.tools.file.find_str import FindStr, FindStrParams

    result = await FindStr()(FindStrParams(path=str(tmp_path / "nope"), content="x"))
    assert not result.is_error
    assert "No files found" in result.output
