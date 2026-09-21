"""Regression tests for the fast (subprocess-free) shell detection.

``Environment.detect()`` runs on every session creation, including ``/clear``,
which recreates the CLI just to reset its context. It used to spawn
``where.exe`` (three times) plus ``git --exec-path`` (which can take over a
second on Git for Windows). These tests pin the replacement: a memoized pure
Python PATH scan plus an ancestor scan for the git-bash layout.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from kaos.path import KaosPath

from kimi_cli.utils import environment as env_mod
from kimi_cli.utils.environment import (
    Environment,
    _find_git_bash_path,
    _git_bash_candidates_from_ancestors,
    _which_all,
)


@pytest.fixture(autouse=True)
def _clear_which_cache():
    env_mod._which_all_cached.cache_clear()
    yield
    env_mod._which_all_cached.cache_clear()


def _touch(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("", encoding="utf-8")
    return path


# ── _which_all ───────────────────────────────────────────────────────────


def test_which_all_returns_every_match_in_path_order(tmp_path: Path) -> None:
    first = _touch(tmp_path / "a", "tool.exe")
    second = _touch(tmp_path / "b", "tool.exe")

    import os

    found = _which_all("tool", os.pathsep.join([str(tmp_path / "a"), str(tmp_path / "b")]))
    assert found == [str(first), str(second)]


def test_which_all_missing_tool_returns_empty(tmp_path: Path) -> None:
    import os

    assert _which_all("definitely-not-here", str(tmp_path)) == []


def test_which_all_dedupes_repeated_path_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    exe = _touch(tmp_path, "tool.exe")
    entry = str(tmp_path)
    # Same directory twice (case-insensitively) -> a single match.
    found = _which_all("tool", os.pathsep.join([entry, entry.upper()]))
    assert found == [str(exe)]


def test_which_all_ignores_empty_path_entries(tmp_path: Path) -> None:
    import os

    exe = _touch(tmp_path, "tool.exe")
    path = os.pathsep.join(["", "   ", str(tmp_path)])
    assert _which_all("tool", path) == [str(exe)]


def test_which_all_is_memoized_per_path(tmp_path: Path) -> None:
    _touch(tmp_path / "a", "tool.exe")
    path = str(tmp_path / "a")

    _which_all("tool", path)
    misses = env_mod._which_all_cached.cache_info().misses
    _which_all("tool", path)
    info = env_mod._which_all_cached.cache_info()
    assert info.misses == misses
    assert info.hits >= 1


def test_which_all_cache_is_invalidated_by_a_different_path(tmp_path: Path) -> None:
    _touch(tmp_path / "a", "tool.exe")
    _touch(tmp_path / "b", "tool.exe")

    a_path = str(tmp_path / "a")
    b_path = str(tmp_path / "b")
    assert [Path(p).parent.name for p in _which_all("tool", a_path)] == ["a"]
    assert [Path(p).parent.name for p in _which_all("tool", b_path)] == ["b"]


def test_which_all_matches_extensionless_files(tmp_path: Path) -> None:
    """A bare (extension-less) binary on PATH is still a match."""
    exe = _touch(tmp_path, "mytool")
    assert _which_all("mytool", str(tmp_path)) == [str(exe)]


# ── git-bash ancestor resolution ─────────────────────────────────────────


def test_git_bash_candidates_from_ancestors_covers_git_for_windows_layout() -> None:
    candidates = [
        str(c) for c in _git_bash_candidates_from_ancestors(r"C:\Program Files\Git\mingw64\bin\git.exe")
    ]
    assert r"C:\Program Files\Git\bin\bash.exe" in candidates
    assert r"C:\Program Files\Git\git-bash.exe" in candidates
    # The immediate parent must be probed first so the closest install wins.
    assert candidates[0].startswith(r"C:\Program Files\Git\mingw64\bin")


def test_git_bash_candidates_from_ancestors_are_bounded() -> None:
    candidates = _git_bash_candidates_from_ancestors(r"C:\a\b\c\d\e\f\g\h\git.exe")
    assert len(candidates) <= 2 * env_mod._GIT_BASH_ANCESTOR_DEPTH


@pytest.mark.asyncio
async def test_find_git_bash_path_uses_ancestor_scan_not_git_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Standard Git for Windows layouts must not spawn ``git --exec-path``."""
    git_root = tmp_path / "Git"
    git_exe = _touch(git_root / "mingw64" / "bin", "git.exe")
    bash_exe = _touch(git_root / "bin", "bash.exe")

    monkeypatch.delenv("KIMI_CLI_GIT_BASH_PATH", raising=False)
    monkeypatch.setattr(
        env_mod, "_where_git_executables", lambda: [str(git_exe)]
    )

    def _boom(git_path: str) -> str | None:
        raise AssertionError("git --exec-path must not be spawned")

    monkeypatch.setattr(env_mod, "_git_exec_path", _boom)

    resolved = await _find_git_bash_path()
    assert str(resolved) == str(bash_exe)


@pytest.mark.asyncio
async def test_find_git_bash_path_falls_back_to_git_exec_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A git shim with no sibling bash still resolves via ``--exec-path``."""
    shim = _touch(tmp_path / "shims", "git.exe")
    install_root = tmp_path / "scoop" / "apps" / "git" / "current"
    bash_exe = _touch(install_root / "bin", "bash.exe")

    monkeypatch.delenv("KIMI_CLI_GIT_BASH_PATH", raising=False)
    monkeypatch.setattr(env_mod, "_where_git_executables", lambda: [str(shim)])
    monkeypatch.setattr(
        env_mod,
        "_git_exec_path",
        lambda git_path: str(install_root / "mingw64" / "libexec" / "git-core"),
    )

    resolved = await _find_git_bash_path()
    assert str(resolved) == str(bash_exe)


# ── detect() must not spawn subprocesses ─────────────────────────────────


@pytest.mark.asyncio
async def test_environment_detect_never_spawns_where_exe(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess
    import sys

    spawned: list[object] = []

    def fake_run(args, **kwargs):  # noqa: ANN001, ANN003
        spawned.append(args)
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="")

    monkeypatch.setattr(env_mod.subprocess, "run", fake_run)

    import kaos

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("shell detection must not spawn processes via kaos.exec")

    monkeypatch.setattr(kaos, "exec", _boom, raising=False)

    env = await Environment.detect()

    assert env.os_kind
    assert env.shell_name
    if sys.platform == "win32":
        flat = [str(a) for a in spawned]
        assert not any("where.exe" in a for a in flat), spawned
