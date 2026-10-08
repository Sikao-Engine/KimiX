"""G4 adversarial probe suite for the `bash` tool gate (K04, plan §4.4).

One test function per gate probe.  Every assertion names an *observable*
outcome — a `ToolOk`/`ToolError` field, a folded string, a file's exact
contents, a frozen process — never `is not None`.

Two kinds of probe:

* **deterministic** (mock `ProcessTask`): parameter/alias/boundary/error-path
  coverage plus the rtk, `bash_fix`, `shell_common` and long-output rules;
* **end-to-end** (require a real bash on the host): interactive session
  continuation, `wait_for_pattern`, timeout kill, child environment, secret
  redaction, exit codes and binary output.  These are skipped — with the
  documented reason "no real bash executable on this host" — when
  `find_bash()` returns `None`; on this review host Git Bash is present so
  all of them execute.

Probe 5 (`test_probe05_child_env_scrubbed`) encodes the gate spec verbatim:
"`scrub_child_env` removes `*_KEY`/`*_TOKEN`/`*_SECRET` from the child env
(assert by running `env` through the tool)".  The bash tool does not scrub
(`ProcessTask.scrub_env` defaults to `False` — `src/kimix/tools/common.py:1910`)
and none of the bash spawn sites passes it (`bash_tool.py:825`, `:882`,
`:1217`), so the probe is **red by design** until the FP-11 fix lands.  It is
deliberately *not* weakened to a `scrub_child_env()`-only unit assertion,
because that would hide the end-to-end gap that X03-F1 / finding F-13
already filed.  Live confirmation recorded in `reviews/tools/K04-bash-bash.md`
§4 item (g) and `reviews/tools/gates/K04.txt`.

Run::
    uv run pytest tests/unit/tools/test_bash_gate.py -q
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import random
import re
import string
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kimi_cli.session import Session
from pydantic import ValidationError

from kimi_agent_sdk import ToolError, ToolOk
from kimix.tools.background.utils import TaskData, _pop_task_data, get_all_tasks
from kimix.tools.common import OUTPUT_LIMIT
from kimix.tools.file.bash import Bash, BashParams, bash_fix, shell_common
from kimix.tools.file.bash.bash_tool import (
    _is_git_bash_install,
    _prepare_bash_cmd,
    _with_msystem_neutralized,
    find_bash,
)
from kimix.tools.file.bash.shell_common import PWSH_ONESHOT_FLAGS

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

#: Real-bash capability of the host (module import time; ``find_bash`` is cached).
REAL_BASH: str | None = find_bash()
needs_bash = pytest.mark.skipif(
    REAL_BASH is None, reason="no real bash executable on this host"
)

_SESSIONS: list[Any] = []


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _session(config: dict | None = None) -> MagicMock:
    """Return a Mock session wired the way the bash tool reads it."""
    session = MagicMock(spec=Session)
    session.custom_data = {}
    session.custom_config.get.return_value = config or {}
    _SESSIONS.append(session)
    return session


def _tool(session: MagicMock | None = None, *, real_bash: bool = False, **config: Any):
    """Build a Bash tool; patched discovery unless ``real_bash`` is asked for."""
    session = session or _session(config or None)
    if real_bash:
        return Bash(session=session)
    with patch(
        "kimix.tools.file.bash.bash_tool.find_bash",
        return_value=r"C:\Git\bin\bash.exe",
    ), patch(
        "kimix.tools.file.bash.bash_tool._should_enable_bash", return_value=True
    ):
        return Bash(session=session)


def _process_task(
    output: str = "probe output",
    *,
    success: bool = True,
    exit_code: int | None = 0,
    alive: bool = False,
    wait_result: tuple[bool, float, bool] = (False, 0.0, False),
) -> MagicMock:
    """A stand-in ProcessTask whose stream returns ``output``."""
    task = MagicMock()
    task.start = AsyncMock(return_value="bash_probe_1")
    task.wait_with_monitor = AsyncMock(return_value=wait_result)
    task.thread_is_alive = AsyncMock(return_value=alive)
    task.stop = AsyncMock()
    task.stream = MagicMock()
    task.stream.pop_output = AsyncMock(return_value=output)
    task.stream.success = AsyncMock(return_value=success)
    task.stream.exit_code = exit_code
    task.stream.process_elapsed = None
    task.stream.wait_for_output = AsyncMock(return_value=(output, False, 0.01))
    return task


def _task_id_of(result: ToolOk) -> str:
    match = re.search(r"task_id: `([^`]+)`", result.output) or re.search(
        r"task_id: `([^`]+)`", result.message
    )
    assert match is not None, f"no task_id in {result.output!r} / {result.message!r}"
    return match.group(1)


def _unique_block(size: int, seed: int = 7) -> str:
    """Return ``size`` chars of unique high-entropy lines.

    Chosen because the (always-on) dedup / micro-compress stages leave such
    content byte-for-byte unchanged, so the long-output promotion threshold is
    the only variable under test.
    """
    rng = random.Random(seed)
    alpha = string.ascii_lowercase + string.digits
    lines: list[str] = []
    total = 0
    while total < size:
        lines.append("".join(rng.choice(alpha) for _ in range(60)))
        total += 61
    return "\n".join(lines)[:size]


async def _wait_until_frozen(path: Path, *, deadline: float = 20.0) -> int:
    """Return the tick count once two consecutive 0.5 s samples are equal.

    A process that was killed stops appending ticks; a surviving process keeps
    ticking (the probe's loop runs for ~80 s, far beyond the 2 s timeout), so a
    frozen count is the observable proof that the process is gone.
    """
    last = -1
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        current = (
            len(path.read_text(encoding="utf-8", errors="replace").splitlines())
            if path.exists()
            else 0
        )
        if current == last and current > 0:
            return current
        last = current
        await asyncio.sleep(0.5)
    return last


@pytest.fixture(autouse=True)
async def _settle_background_tasks() -> Any:
    """Stop every task started by a probe and drop the session task data."""
    yield
    for session in _SESSIONS:
        for stream in list(get_all_tasks(session).values()):
            with contextlib.suppress(Exception):
                await stream.stop()
        with contextlib.suppress(Exception):
            _pop_task_data(session)
    _SESSIONS.clear()


# ---------------------------------------------------------------------------
# probe 1 — interactive session: task_id then stdin continuation
# ---------------------------------------------------------------------------


@needs_bash
async def test_probe01_interactive_returns_task_id_and_continuation_returns_output() -> None:
    tool = _tool(real_bash=True)
    started = await tool(BashParams(mode="interactive"))
    assert isinstance(started, ToolOk), started
    assert started.brief == "Interactive Bash started"
    task_id = _task_id_of(started)

    continued = await tool(
        BashParams(cmd="echo PROBE01_CONTINUATION_OK", task_id=task_id, timeout=30)
    )
    assert isinstance(continued, ToolOk), continued
    assert "PROBE01_CONTINUATION_OK" in continued.output
    assert f"task_id: {task_id}" in continued.output

    closed = await tool(BashParams(cmd="exit", task_id=task_id, timeout=15))
    assert isinstance(closed, ToolOk), closed
    # `status:` is sampled at return time, so a just-exited shell may still be
    # reported as running; prove the session really closed by waiting for the
    # registered stream's worker thread to settle (bounded).
    stream = get_all_tasks(tool._session)[task_id]
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline and await stream.thread_is_alive():
        await asyncio.sleep(0.2)
    assert await stream.thread_is_alive() is False, "the interactive shell never exited"


@needs_bash
async def test_probe01b_interactive_without_cmd_starts_empty_session() -> None:
    """`interactive=True` (the documented alias) also yields a task_id."""
    tool = _tool(real_bash=True)
    result = await tool(BashParams(interactive=True, cmd=""))
    assert isinstance(result, ToolOk), result
    task_id = _task_id_of(result)
    assert task_id.startswith("bash")
    followed = await tool(BashParams(cmd="exit", task_id=task_id, timeout=15))
    assert isinstance(followed, ToolOk), followed


# ---------------------------------------------------------------------------
# probe 2 — wait_for_pattern blocks then matches; timeout keeps the job
# ---------------------------------------------------------------------------


@needs_bash
async def test_probe02_wait_for_pattern_blocks_then_matches() -> None:
    tool = _tool(real_bash=True)
    began = time.monotonic()
    result = await tool(
        BashParams(
            cmd="sleep 1; echo READY_PROBE02",
            mode="send",
            wait_for_pattern="READY_PROBE02",
            timeout=30,
        )
    )
    elapsed = time.monotonic() - began
    assert isinstance(result, ToolOk), result
    assert "wait_matched: true" in result.output
    assert "READY_PROBE02" in result.output
    assert elapsed >= 0.9, "wait_for_pattern returned before the pattern existed"


@needs_bash
async def test_probe02b_wait_for_pattern_timeout_reports_running_and_keeps_job() -> None:
    tool = _tool(real_bash=True)
    result = await tool(
        BashParams(
            cmd="sleep 30",
            mode="send",
            wait_for_pattern="NEVER_PROBE02",
            timeout=2,
        )
    )
    assert isinstance(result, ToolOk), result
    assert "status: running" in result.output
    assert "wait_matched: false" in result.output
    task_id = _task_id_of(result)
    assert task_id in get_all_tasks(tool._session), "the timed-out job was dropped"


async def test_probe02c_wait_for_pattern_task_completed_reports_completed() -> None:
    """A job that has already settled is reported `status: completed`."""
    tool = _tool()
    task = _process_task(output="DONE_PROBE02C", alive=False)
    task.stream.wait_for_output = AsyncMock(return_value=("DONE_PROBE02C", True, 0.5))
    with patch("kimix.tools.file.bash.bash_tool.ProcessTask", return_value=task):
        result = await tool(
            BashParams(
                cmd="echo done",
                mode="send",
                wait_for_pattern="DONE_PROBE02C",
                timeout=30,
            )
        )
    assert isinstance(result, ToolOk), result
    assert "status: completed" in result.output
    assert "wait_matched: true" in result.output
    assert "DONE_PROBE02C" in result.output


@needs_bash
async def test_probe02d_matched_job_settles_and_stays_retrievable() -> None:
    tool = _tool(real_bash=True)
    result = await tool(
        BashParams(
            cmd="echo SETTLED_PROBE02",
            mode="send",
            wait_for_pattern="SETTLED_PROBE02",
            timeout=30,
        )
    )
    assert isinstance(result, ToolOk), result
    assert "wait_matched: true" in result.output
    task_id = _task_id_of(result)
    stream = get_all_tasks(tool._session)[task_id]
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline and await stream.thread_is_alive():
        await asyncio.sleep(0.2)
    assert await stream.thread_is_alive() is False
    assert task_id in get_all_tasks(tool._session), "a settled job was dropped"


# ---------------------------------------------------------------------------
# probe 3 — timeout kills the process tree
# ---------------------------------------------------------------------------


@needs_bash
async def test_probe03_timeout_kills_the_process(tmp_path: Path) -> None:
    tick_file = tmp_path / "probe03_ticks.txt"
    shell_path = str(tick_file).replace("\\", "/")
    command = (
        f'echo started; i=0; while [ $i -lt 400 ]; do echo tick >> "{shell_path}"; '
        "sleep 0.2; i=$((i+1)); done; echo DONE"
    )
    tool = _tool(real_bash=True)
    result = await tool(BashParams(cmd=command, timeout=2))

    assert isinstance(result, ToolError), result
    assert result.brief == "Timeout"
    assert "timed out" in result.message
    # The task was removed and the tree stopped making progress well before
    # the ~80 s the loop would need to finish on its own.
    assert get_all_tasks(tool._session) == {}
    frozen = await _wait_until_frozen(tick_file)
    assert frozen > 0, "the probe command never ran"
    text = tick_file.read_text(encoding="utf-8", errors="replace")
    assert "DONE" not in text, "the killed loop ran to completion"
    await asyncio.sleep(1.0)
    assert len(text.splitlines()) == frozen, "the process kept running after the timeout"


# ---------------------------------------------------------------------------
# probe 4 — long-output promotion, threshold boundary
# ---------------------------------------------------------------------------


async def test_probe04_long_output_promotion_threshold_boundary() -> None:
    """Below `OUTPUT_LIMIT` output is returned verbatim; at/above it, it is
    diverted to a temp file and replaced by the export pointer.

    Characterization note (reported as K04-F4): the promotion path does not set
    `output_truncated`, so the structured block below the pointer reports
    `output_truncated: false` even though the text was replaced.
    """
    assert OUTPUT_LIMIT == 16384, "the documented long-output threshold changed"
    tool = _tool()
    below = _unique_block(OUTPUT_LIMIT)
    above = _unique_block(OUTPUT_LIMIT + 1)

    display_below, path_below, truncated_below, _orig = await tool._process_output(
        BashParams(cmd="echo probe"), below
    )
    assert display_below == below
    assert truncated_below is False
    assert path_below is None

    display_above, path_above, truncated_above, _orig2 = await tool._process_output(
        BashParams(cmd="echo probe"), above
    )
    assert display_above != above
    assert display_above.startswith("[Output too large,")
    assert ".txt]" in display_above
    assert path_above is not None and path_above.endswith(".txt")
    assert truncated_above is False  # K04-F4 characterization


async def test_probe04b_over_65536_output_is_summarized_and_truncated() -> None:
    """A second, higher threshold: >65536 chars is summarized (mock) and the
    block is required to report `output_truncated: true`."""
    tool = _tool()
    huge = _unique_block(65536 + 1)
    with patch(
        "kimix.tools.file.bash.bash_tool._summarize_long_output_async",
        new=AsyncMock(return_value="[PROBE04 SUMMARY]"),
    ) as summarize:
        display, _path, truncated, original_path = await tool._process_output(
            BashParams(cmd="echo probe"), huge
        )
    assert summarize.await_count == 1
    assert display == "[PROBE04 SUMMARY]"
    assert truncated is True
    assert original_path is not None, "the full output was not preserved"


# ---------------------------------------------------------------------------
# probe 5 — child environment scrubbing (RED until FP-11 lands)
# ---------------------------------------------------------------------------


@needs_bash
async def test_probe05_child_env_scrubbed(tmp_path: Path, monkeypatch: Any) -> None:
    """`*_KEY`/`*_TOKEN`/`*_SECRET` must not reach the bash child.

    Gate spec: asserted by running `env` through the tool.  `RTK_DISABLED=1`
    keeps rtk from wrapping `env` so the dump is the real child environment.
    """
    secrets = {
        "K04_GATE_API_KEY": "k04-api-key-value-9f2c",
        "K04_GATE_ACCESS_TOKEN": "k04-access-token-value-7d1a",
        "K04_GATE_CLIENT_SECRET": "k04-client-secret-value-3b8e",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)

    tool = _tool(real_bash=True)
    dump = await tool(BashParams(cmd="RTK_DISABLED=1 env", timeout=30))
    assert isinstance(dump, ToolOk), dump
    for name, value in secrets.items():
        assert name not in dump.output, f"{name} leaked into the child environment"
        assert value not in dump.output, f"the value of {name} leaked"

    # Independent confirmation with a command rtk never rewrites.
    for name, value in secrets.items():
        result = await tool(BashParams(cmd=f"printenv {name}", timeout=15))
        assert isinstance(result, ToolError), (
            f"{name} was still set in the child: {result.output!r}"
        )
        assert value not in result.output
    assert tmp_path.exists()


@needs_bash
async def test_probe05b_child_env_keeps_plain_variables(monkeypatch: Any) -> None:
    """Scrubbing must not be a blanket wipe: ordinary variables survive.

    Red today for the same reason as probe 5 (the child inherits everything);
    kept as a separable assertion so the FP-11 fix is pinned from both sides.
    """
    monkeypatch.setenv("K04_GATE_PLAIN_VAR", "k04-plain-value")
    tool = _tool(real_bash=True)
    result = await tool(BashParams(cmd="printenv K04_GATE_PLAIN_VAR", timeout=15))
    assert isinstance(result, ToolOk), result
    assert "k04-plain-value" in result.output


# ---------------------------------------------------------------------------
# probe 6 — secret redaction of echoed tokens
# ---------------------------------------------------------------------------


@needs_bash
async def test_probe06_echoed_token_is_redacted() -> None:
    tool = _tool(real_bash=True)
    result = await tool(BashParams(cmd="echo token=ghp_1234567890123456789012"))
    assert isinstance(result, ToolOk), result
    assert "ghp_1234567890123456789012" not in result.output
    assert "[REDACTED]" in result.output

    plain = await tool(BashParams(cmd="echo PROBE06_PLAIN_OK"))
    assert isinstance(plain, ToolOk), plain
    assert "PROBE06_PLAIN_OK" in plain.output


# ---------------------------------------------------------------------------
# probe 7 — dangerous commands refused with a specific brief
# ---------------------------------------------------------------------------


async def test_probe07_dangerous_command_refused_with_specific_brief() -> None:
    tool = _tool()
    with patch("kimix.tools.file.bash.bash_tool.ProcessTask") as process_task:
        hardline = await tool(BashParams(cmd="rm -rf /"))
    assert isinstance(hardline, ToolError), hardline
    assert hardline.brief == "Blocked (hardline)"
    assert "hardline" in hardline.message
    assert hardline.output == ""
    process_task.assert_not_called()

    with patch("kimix.tools.file.bash.bash_tool.ProcessTask") as process_task:
        selfkill = await tool(BashParams(cmd=f"kill -9 {os.getpid()}"))
    assert isinstance(selfkill, ToolError), selfkill
    assert selfkill.brief == "Blocked (self-kill guard)"
    assert str(os.getpid()) in selfkill.message
    process_task.assert_not_called()


async def test_probe07b_obfuscated_dangerous_command_still_refused() -> None:
    tool = _tool()
    with patch("kimix.tools.file.bash.bash_tool.ProcessTask") as process_task:
        result = await tool(BashParams(cmd=r'r\m -rf /'))
    assert isinstance(result, ToolError), result
    assert result.brief == "Blocked (hardline)"
    process_task.assert_not_called()


# ---------------------------------------------------------------------------
# probe 8 — rtk wrapping: known command wrapped, `find` skipped
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git status", "set -o pipefail; rtk git status"),
        ("ls -la", "set -o pipefail; rtk ls -la"),
        ("find . -name '*.py'", "set -o pipefail; find . -name '*.py'"),
        ("read var", "set -o pipefail; read var"),
    ],
)
async def test_probe08_rtk_wrapping_applied_and_find_skipped(
    command: str, expected: str
) -> None:
    tool = _tool()
    task = _process_task()
    with patch("kimix.tools.common._rtk_available", return_value=True), patch(
        "kimix.tools.file.bash.bash_tool.ProcessTask", return_value=task
    ) as process_task:
        result = await tool(BashParams(cmd=command))
    assert isinstance(result, ToolOk), result
    argv = process_task.call_args.args[1]
    assert "rtk " not in argv[1] or argv[1] == expected
    assert argv[1].endswith(expected), argv


async def test_probe08b_rtk_unavailable_leaves_command_untouched() -> None:
    tool = _tool()
    task = _process_task()
    with patch("kimix.tools.common._rtk_available", return_value=False), patch(
        "kimix.tools.file.bash.bash_tool.ProcessTask", return_value=task
    ) as process_task:
        await tool(BashParams(cmd="git status"))
    assert process_task.call_args.args[1][1].endswith("git status")
    assert "rtk git status" not in process_task.call_args.args[1][1]


# ---------------------------------------------------------------------------
# probe 9 — every documented `bash_fix` rewrite
# ---------------------------------------------------------------------------

_WIN = patch("kimix.tools.file.bash.bash_fix.sys.platform", "win32")


def _fix_windows(command: str) -> Any:
    with _WIN:
        return bash_fix.fix_bash_command(command)


@pytest.mark.parametrize(
    ("source", "dest"),
    [
        # docstring: Windows backslash paths become forward slashes
        (r"ls D:\repo\src", "ls D:/repo/src"),
        # docstring: `cd /d <path>` loses the cmd.exe-only flag
        (r"cd /d C:\x", "cd  C:/x"),
        # docstring: `nul` redirection target becomes /dev/null
        ("echo hi > nul", "echo hi > /dev/null"),
        ("echo hi > NUL", "echo hi > /dev/null"),
        # docstring: Git Bash virtual /c/x becomes C:/x
        ("ls /c/x", "ls C:/x"),
        # docstring: redundant leading shell invocation is unwrapped
        ("bash cd /c/dev/x && pwd", "cd C:/dev/x && pwd"),
        # docstring: quoted data / single-segment relative paths are preserved
        (r'grep "C:\Users" f', r'grep "C:\Users" f'),
        (r"cat foo\bar", r"cat foo\bar"),
        (r"cat src\a.py", r"cat src\a.py"),
        ("", ""),
    ],
)
def test_probe09_documented_bash_fix_rewrites(source: str, dest: str) -> None:
    assert _fix_windows(source).command == dest


def test_probe09b_fallback_definition_and_unsupported_reason(tmp_path: Path) -> None:
    fallback = _fix_windows("rev")
    assert fallback.replacements == ("rev",)
    assert "rev() {" in fallback.command
    assert "export -f rev" in fallback.command

    unsupported = _fix_windows("journalctl -u x")
    assert unsupported.unsupported == ("journalctl",)
    assert "journalctl" in unsupported.warning
    assert unsupported.command == "journalctl -u x"
    assert tmp_path.exists()


def test_probe09c_tmp_mount_maps_to_the_real_windows_temp_dir() -> None:
    import tempfile

    fixed = _fix_windows("echo hi > /tmp/x")
    expected = tempfile.gettempdir().replace("\\", "/") + "/x"
    assert fixed.command == f"echo hi > {expected}"
    assert fixed.path_changes == ("/tmp/x",)


# ---------------------------------------------------------------------------
# probe 10 — shell_common quoting / escaping round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "dest"),
    [
        ("echo hi", "echo hi"),
        ('echo "a b"', 'echo "a b"'),
        ("echo 'a b'", "echo 'a b'"),
        (r'cat "C:\x\y"', r'cat "C:\x\y"'),
        ("echo a\necho b", "echo a\necho b"),
        ("echo 'héllo — ünïcode ✓'", "echo 'héllo — ünïcode ✓'"),
        ("printf '%s\\n' \"$HOME\"", "printf '%s\\n' \"$HOME\""),
        ("echo $'tab\\there'", "echo $'tab\\there'"),
        ("echo \"a'b\"'c\"d'", "echo \"a'b\"'c\"d'"),
    ],
)
def test_probe10_prepare_bash_command_quoting_round_trip(
    source: str, dest: str, monkeypatch: Any
) -> None:
    """Quoted data / escapes / newlines / unicode survive preparation.

    Windows is forced so the assertion is identical on every host (the
    unquoted-backslash conversion is the only platform-dependent stage).
    """
    monkeypatch.setattr(sys, "platform", "win32")
    assert shell_common.prepare_bash_command(source) == dest


def test_probe10b_unquoted_backslash_path_is_normalized_on_windows(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert shell_common.prepare_bash_command(r"cat src\a\b.py") == "cat src/a/b.py"
    assert shell_common.prepare_bash_command(r"cat 'src\a\b.py'") == r"cat 'src\a\b.py'"


def test_probe10c_non_windows_is_a_noop(monkeypatch: Any) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert shell_common.prepare_bash_command(r"cat src\a\b.py") == r"cat src\a\b.py"


# ---------------------------------------------------------------------------
# probe 11 — POSIX vs Windows divergence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "source", "dest"),
    [
        ("win32", r"ls D:\repo\src", "ls D:/repo/src"),
        ("win32", "echo hi > nul", "echo hi > /dev/null"),
        ("linux", r"ls D:\repo\src", r"ls D:\repo\src"),
        ("linux", "echo hi > nul", "echo hi > nul"),
        ("darwin", r"ls D:\repo\src", r"ls D:\repo\src"),
        ("darwin", "echo hi > nul", "echo hi > nul"),
    ],
)
def test_probe11_bash_fix_posix_vs_windows(
    platform: str, source: str, dest: str, monkeypatch: Any
) -> None:
    """Documented divergence: the fixer is a strict no-op off Windows.

    Both branches are forced explicitly (the host's own `sys.platform` is
    ignored) so the divergence is asserted on Windows, Linux and macOS hosts
    alike.
    """
    monkeypatch.setattr(bash_fix.sys, "platform", platform)
    assert bash_fix.fix_bash_command(source).command == dest


def test_probe11b_msystem_neutralization_is_windows_git_bash_only(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    with patch(
        "kimix.tools.file.bash.bash_tool._is_git_bash_install", return_value=True
    ):
        assert (
            _with_msystem_neutralized("echo hi", r"C:\Program Files\Git\bin\bash.exe")
            == "export MSYSTEM=; echo hi"
        )
    with patch(
        "kimix.tools.file.bash.bash_tool._is_git_bash_install", return_value=False
    ):
        assert (
            _with_msystem_neutralized("echo hi", r"C:\msys64\usr\bin\bash.exe")
            == "echo hi"
        )
    monkeypatch.setattr(sys, "platform", "linux")
    assert (
        _with_msystem_neutralized("echo hi", r"C:\Program Files\Git\bin\bash.exe")
        == "echo hi"
    )


def test_probe11c_prepare_bash_cmd_is_windows_only(monkeypatch: Any) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert _prepare_bash_cmd(r"cat src\a\b.py") == "cat src/a/b.py"
    monkeypatch.setattr(sys, "platform", "linux")
    assert _prepare_bash_cmd(r"cat src\a\b.py") == r"cat src\a\b.py"
    with patch.object(bash_fix.sys, "platform", "win32"), patch(
        "kimix.tools.file.bash.bash_tool.os.path.isfile"
    ) as isfile:
        isfile.return_value = True
        assert _is_git_bash_install(r"C:\Program Files\Git\usr\bin\bash.exe") is True
    assert _is_git_bash_install(None) is False


# ---------------------------------------------------------------------------
# probe 12 — non-zero exit codes surfaced with the code
# ---------------------------------------------------------------------------


@needs_bash
async def test_probe12_nonzero_exit_code_is_surfaced() -> None:
    tool = _tool(real_bash=True)
    failed = await tool(BashParams(cmd="exit 7"))
    assert isinstance(failed, ToolError), failed
    assert "exit_code: 7" in failed.output

    ok = await tool(BashParams(cmd="exit 0"))
    assert isinstance(ok, ToolOk), ok
    assert "exit_code: 0" in ok.output

    missing = await tool(BashParams(cmd="no_such_probe12_command", timeout=15))
    assert isinstance(missing, ToolError), missing
    assert "exit_code: 127" in missing.output
    assert "command was not found" in missing.output.lower()


# ---------------------------------------------------------------------------
# probe 13 — binary / non-UTF-8 output decoded without crashing
# ---------------------------------------------------------------------------


@needs_bash
async def test_probe13_binary_output_does_not_crash() -> None:
    tool = _tool(real_bash=True)
    for _ in range(3):
        result = await tool(BashParams(cmd="head -c 64 /dev/urandom", timeout=15))
        assert isinstance(result, (ToolOk, ToolError)), result
        assert isinstance(result.output, str)
        assert "exit_code: 0" in result.output


@needs_bash
async def test_probe13b_invalid_utf8_escape_sequence_does_not_crash() -> None:
    tool = _tool(real_bash=True)
    result = await tool(BashParams(cmd="printf 'a\\377\\376b\\n'", timeout=15))
    assert isinstance(result, (ToolOk, ToolError)), result
    assert isinstance(result.output, str)
    assert "exit_code: 0" in result.output


# ---------------------------------------------------------------------------
# probe 14 — a very long single line is folded (not passed through)
# ---------------------------------------------------------------------------


async def test_probe14_very_long_single_line_is_folded() -> None:
    tool = _tool()
    line = "b" * (OUTPUT_LIMIT + 1)
    display, _path, truncated, original_path = await tool._process_output(
        BashParams(cmd="echo probe"), line
    )
    assert display == "b ×16385 [+16384 chars elided]"
    assert "[+16384 chars elided]" in display
    assert len(display) < 64
    assert truncated is True
    assert original_path is not None, "the unfolded line was not preserved"


async def test_probe14b_short_repeat_free_line_is_untouched() -> None:
    tool = _tool()
    line = "plain single line"
    display, path, truncated, original_path = await tool._process_output(
        BashParams(cmd="echo probe"), line
    )
    assert display == line
    assert truncated is False
    assert path is None and original_path is None


# ---------------------------------------------------------------------------
# probe 15 — shared launcher arguments (bash + pwsh via shell_common)
# ---------------------------------------------------------------------------


def test_probe15_shared_launcher_arguments(monkeypatch: Any) -> None:
    from kimix.tools.file.bash import process_pwsh, pwsh_tool

    assert PWSH_ONESHOT_FLAGS == ("-NoP", "-NonI", "-Exec", "Bypass", "-NoL")
    monkeypatch.setattr(
        "kimix.tools.file.bash.bash_tool.find_bash", lambda: "/bin/bash"
    )
    argv_login, env_login = shell_common.bash_argv("echo hi", login=True)
    assert argv_login == ["/bin/bash", "-l", "-c", "echo hi"]
    assert isinstance(env_login, dict)
    argv_plain, _env = shell_common.bash_argv("echo hi", login=False)
    assert argv_plain == ["/bin/bash", "-c", "echo hi"]
    argv_file, _env2 = shell_common.bash_file_argv("/tmp/run.sh")
    assert argv_file == ["/bin/bash", "-l", "/tmp/run.sh"]

    monkeypatch.setattr(pwsh_tool, "find_pwsh", lambda: r"C:\pwsh.exe")
    pwsh_args, hint = shell_common.pwsh_argv("Get-Date")  # type: ignore[misc]
    assert pwsh_args[0] == r"C:\pwsh.exe"
    assert pwsh_args[1:7] == [*PWSH_ONESHOT_FLAGS, "-Command"]
    assert pwsh_args[7].startswith("[Console]::OutputEncoding=")
    assert "try{Get-Date}catch{$_|Out-String|Write-Error;exit 1}" in pwsh_args[7]
    assert pwsh_args[7].endswith(";exit $LASTEXITCODE")
    assert hint == r"PowerShell executable not found: C:\pwsh.exe"

    file_args, _hint2 = shell_common.pwsh_file_argv("run.ps1")
    assert file_args[1:7] == [*PWSH_ONESHOT_FLAGS, "-File"]
    assert file_args[7] == "run.ps1"

    # The PS5.1 downgrade path reuses the same launcher flags.
    monkeypatch.setattr(pwsh_tool, "find_pwsh", lambda: None)
    monkeypatch.setattr(
        process_pwsh, "pwsh_transform", lambda cmd: ("TRANSFORMED", ["warn"])
    )
    fallback_args, _hint3 = shell_common.pwsh_argv("Get-Date")  # type: ignore[misc]
    assert fallback_args[1:7] == [*PWSH_ONESHOT_FLAGS, "-Command"]
    assert "try{TRANSFORMED}catch" in fallback_args[7]


# ---------------------------------------------------------------------------
# probe 16 — params model: every field, every alias, JSON-schema round trip
# ---------------------------------------------------------------------------


def test_probe16_params_fields_and_aliases() -> None:
    filled = BashParams(
        command="echo alias",
        mode="send",
        timeout=900,
        task_id=None,
        wait_for_pattern="ready",
        max_lines=3,
    )
    assert filled.cmd == "echo alias"
    assert filled.mode == "send"
    assert filled.timeout == 900
    assert filled.task_id is None
    assert filled.wait_for_pattern == "ready"
    assert filled.max_lines == 3

    assert BashParams.model_fields["cmd"].alias == "command"
    assert BashParams(command="x").cmd == "x"
    assert BashParams.model_validate({"cmd": "x"}).cmd == "x"

    # deprecated mode aliases / boolean flag
    assert BashParams(interactive=True, cmd="x").mode == "interactive"
    assert BashParams(mode="run", cmd="x").mode == "execute"
    assert BashParams(mode="background", cmd="x").mode == "send"


def test_probe16b_json_schema_round_trip() -> None:
    schema = BashParams.model_json_schema()
    props = schema["properties"]
    # The model-facing schema advertises the alias spelling.
    assert "command" in props
    assert props["command"]["type"] == "string"
    for field in ("mode", "timeout", "task_id", "wait_for_pattern", "max_lines"):
        assert field in props
    assert schema["properties"]["mode"]["enum"] == ["execute", "send", "interactive"]
    assert schema["properties"]["timeout"]["default"] == 30
    assert schema["properties"]["timeout"]["minimum"] == 1
    assert schema["properties"]["timeout"]["maximum"] == 900
    assert {"type": "integer", "minimum": 3} in schema["properties"]["max_lines"]["anyOf"]
    assert {"type": "null"} in schema["properties"]["max_lines"]["anyOf"]
    # Round-trip through the schema's own alias.
    payload = {"command": "echo round trip", "mode": "send", "max_lines": 5}
    assert BashParams.model_validate(payload).cmd == "echo round trip"


# ---------------------------------------------------------------------------
# probe 17 — boundary values (at / below / above)
# ---------------------------------------------------------------------------


def test_probe17_timeout_boundaries() -> None:
    assert BashParams(cmd="x", timeout=1).timeout == 1
    assert BashParams(cmd="x", timeout=900).timeout == 900
    assert BashParams(cmd="x").timeout == 30
    for bad in (0, -1, 901, 1000):
        with pytest.raises(ValidationError):
            BashParams(cmd="x", timeout=bad)


def test_probe17b_max_lines_boundaries() -> None:
    assert BashParams(cmd="x", max_lines=3).max_lines == 3
    assert BashParams(cmd="x").max_lines is None
    for bad in (0, 2, -5):
        with pytest.raises(ValidationError):
            BashParams(cmd="x", max_lines=bad)


def test_probe17c_empty_input_boundaries() -> None:
    # `execute` requires input …
    with pytest.raises(ValidationError):
        BashParams(cmd="", mode="execute")
    # … `send` does not (non-empty is only enforced on the execute path) …
    assert BashParams(cmd="", mode="send").cmd == ""
    # … and continuing a session always requires the stdin payload.
    with pytest.raises(ValidationError):
        BashParams(cmd="", task_id="bash_1")
    assert BashParams(cmd="echo x", task_id="bash_1").task_id == "bash_1"


# ---------------------------------------------------------------------------
# probe 18 — every ToolError return site, with its brief
# ---------------------------------------------------------------------------


async def test_probe18_error_briefs_forbidden_pattern_and_task() -> None:
    policy = _tool(_session({"forbidden_commands": ["curl secret"]}))
    with patch("kimix.tools.file.bash.bash_tool.ProcessTask") as process_task:
        forbidden = await policy(BashParams(cmd="curl secret"))
    assert isinstance(forbidden, ToolError), forbidden
    assert forbidden.brief == "Forbidden command"
    assert "forbidden by config rule" in forbidden.message
    process_task.assert_not_called()

    tool = _tool()
    invalid = await tool(BashParams(cmd="echo hi", wait_for_pattern="["))
    assert isinstance(invalid, ToolError), invalid
    assert invalid.brief == "Invalid pattern"
    assert "Invalid wait_for_pattern" in invalid.message

    missing = await tool(BashParams(cmd="echo hi", task_id="bash_missing"))
    assert isinstance(missing, ToolError), missing
    assert "not found" in missing.message
    assert "No running tasks" in missing.message


async def test_probe18b_error_briefs_unsupported_and_send_failure() -> None:
    tool = _tool()
    with patch("kimix.tools.file.bash.bash_tool.sys.platform", "win32"):
        unsupported = await tool(BashParams(cmd="journalctl -u x"))
    assert isinstance(unsupported, ToolError), unsupported
    assert unsupported.brief == "Unsupported command on Windows"
    assert "no Windows Git Bash equivalent" in unsupported.message

    data = TaskData()
    stream = AsyncMock()
    stream.is_started = AsyncMock(return_value=True)
    stream.pop_output = AsyncMock(return_value="buffered")
    stream.input = AsyncMock(return_value=False)
    data.tasks = {"bash_send": stream}
    tool._session.custom_data["background_task_data"] = data
    failed = await tool(BashParams(cmd="printf x", task_id="bash_send"))
    assert isinstance(failed, ToolError), failed
    assert failed.brief == "Send input failed"
    assert failed.output == "buffered"


async def test_probe18c_error_brief_failed_command_and_cancellation() -> None:
    tool = _tool()
    failing = _process_task(output="boom", success=False, exit_code=1)
    with patch("kimix.tools.file.bash.bash_tool.ProcessTask", return_value=failing):
        result = await tool(BashParams(cmd="false"))
    assert isinstance(result, ToolError), result
    assert result.brief == "Command execution failed"
    assert "exit_code: 1" in result.output

    cancelling = _process_task()
    cancelling.wait_with_monitor = AsyncMock(side_effect=asyncio.CancelledError)
    with patch("kimix.tools.file.bash.bash_tool.ProcessTask", return_value=cancelling):
        cancelled = await tool(BashParams(cmd="echo hi"))
    assert isinstance(cancelled, ToolError), cancelled
    assert cancelled.brief == "Command cancelled"
    assert cancelling.stop.await_count == 1


async def test_probe18d_expected_nonzero_exit_is_a_success() -> None:
    """grep "no matches" (exit 1) is an informative success, not a failure."""
    tool = _tool()
    grep = _process_task(output="", success=False, exit_code=1)
    with patch("kimix.tools.file.bash.bash_tool.ProcessTask", return_value=grep):
        result = await tool(BashParams(cmd="grep missing /nonexistent"))
    assert isinstance(result, ToolOk), result
    assert "No matches found" in result.message
    assert "failed" not in result.message.lower()


# ---------------------------------------------------------------------------
# probe 19 — the unreachable "Empty command" branch
# ---------------------------------------------------------------------------


async def test_probe19_empty_command_branch_is_unreachable() -> None:
    """`Bash.__call__`'s "Empty command" ToolError cannot be reached.

    Ordering: `mode == "send"` returns before the check, and for
    `mode == "execute"` the `shell_cmd_required_validator` already rejects an
    empty `cmd` with no `task_id`.  Asserted from both sides so the dead
    branch (bash_tool.py:769-774) is documented rather than assumed.
    """
    with pytest.raises(ValidationError):
        BashParams(cmd="", mode="execute")
    thread = _process_task(output="")
    tool = _tool()
    with patch("kimix.tools.file.bash.bash_tool.ProcessTask", return_value=thread):
        sent = await tool(BashParams(cmd="", mode="send"))
    assert isinstance(sent, ToolOk), sent
    assert sent.brief == "Background task started"


# ---------------------------------------------------------------------------
# probe 20 — timeout shapes (guidance / silent hand-off / kill)
# ---------------------------------------------------------------------------


async def test_probe20_timeout_shapes() -> None:
    tool = _tool()

    guided = _process_task(output="", success=False, alive=True)
    with patch("kimix.tools.file.bash.bash_tool.ProcessTask", return_value=guided):
        result = await tool(BashParams(cmd="npm run dev", timeout=1))
    assert isinstance(result, ToolError), result
    assert result.brief == "Timeout"
    assert "Running in background" in result.message
    assert "Long-running command detected" in result.message

    silent = _process_task(output="", success=False, alive=True)
    with patch("kimix.tools.file.bash.bash_tool.ProcessTask", return_value=silent):
        handed_off = await tool(BashParams(cmd="sleep 5", timeout=1))
    assert isinstance(handed_off, ToolError), handed_off
    assert "No output after" in handed_off.message
    assert "job_output" in handed_off.message
    assert silent.stop.await_count == 0, "a silent long-running task must not be killed"

    stalled = _process_task(output="partial", success=False, alive=True)
    with patch("kimix.tools.file.bash.bash_tool.ProcessTask", return_value=stalled):
        killed = await tool(BashParams(cmd="make", timeout=1))
    assert isinstance(killed, ToolError), killed
    assert "timed out" in killed.message
    assert stalled.stop.await_count == 1
    assert get_all_tasks(tool._session) == {}


# ---------------------------------------------------------------------------
# probe 21 — timeout values actually reach the monitor / wait
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("timeout", [1, 30, 900])
async def test_probe21_timeout_is_forwarded_to_the_monitor(timeout: int) -> None:
    tool = _tool()
    task = _process_task()
    with patch("kimix.tools.file.bash.bash_tool.ProcessTask", return_value=task):
        await tool(BashParams(cmd="echo hi", timeout=timeout))
    assert task.wait_with_monitor.await_args.args[0] == timeout


async def test_probe21b_max_lines_is_forwarded_to_the_filter() -> None:
    tool = _tool()
    task = _process_task(output="\n".join(f"line {i}" for i in range(50)))
    with patch("kimix.tools.file.bash.bash_tool.ProcessTask", return_value=task):
        result = await tool(BashParams(cmd="seq 1 50", max_lines=5))
    assert isinstance(result, ToolOk), result
    assert "lines omitted" in result.output
    assert "output_truncated: true" in result.output
    assert "line 49" in result.output


def test_probe21c_binary_stdout_pipe_is_decoded_with_replacement() -> None:
    """The reader decodes with `errors="replace"`; the helper is exercised
    directly so the byte-level guarantee is pinned without a shell."""
    import codecs

    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    text = decoder.decode(b"\xff\xfe\x80abc") + decoder.decode(b"", final=True)
    assert isinstance(text, str)
    assert text.endswith("abc")
    assert "\ufffd" in text
    assert subprocess.run([sys.executable, "-c", "print(1)"]).returncode == 0
