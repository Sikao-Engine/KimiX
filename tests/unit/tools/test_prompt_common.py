"""Tests for the shared tool-prompt helpers (plan.md Part 1).

Layer-1 invariant: the four shell/python tools compose their ``description``
and ``Params`` field descriptions from ``kimi_cli.tools.prompt_common`` and the
composed wire text stayed byte-identical to the pre-refactor text through
Layer 2(a).  Layer 2(b) then hoisted the *generic* conventions (head+tail
fold, output dedup, ``rtk``, parameter aliases, ``wait_for_pattern``,
``timeout`` ranges) into the "# Tool Conventions" block of
``kimi-cli/src/kimi_cli/agents/default/system.md`` and deleted those
fragments from the four tools, leaving only tool-specific sentences.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from pydantic import BaseModel, ValidationError

from kimi_cli.tools.prompt_common import (
    accepts_alias_text,
    normalize_mode_validator,
)



class _FakeSession:
    """Minimal stand-in for kimi_cli.session.Session used by tool __init__s."""

    def __init__(self) -> None:
        self.custom_data: dict[str, Any] = {}
        self.custom_config = mock.MagicMock()
        self.custom_config.get.side_effect = lambda key, default=None: (
            {} if key == "config_json" else default
        )


def _build_tools() -> dict[str, Any]:
    """Instantiate the four tools exactly as serialized on Windows.

    ``sys.platform`` is forced to ``"win32"`` so the Windows-specific
    sentences are always appended, keeping the snapshots deterministic on
    every platform.
    """
    from kimi_cli.tools.file import run as rt
    from kimi_cli.tools.file.bash import bash_tool as bt
    from kimi_cli.tools.file.bash import pwsh_tool as pt
    from kimi_cli.tools.file.bash.bash_tool import Bash
    from kimi_cli.tools.file.bash.pwsh_tool import Powershell
    from kimi_cli.tools.file.run import Run
    from kimi_cli.tools.py import python

    session = _FakeSession()
    tools: dict[str, Any] = {}
    with mock.patch.object(bt.sys, "platform", "win32"), mock.patch.object(
        bt, "_should_enable_bash", return_value=True
    ), mock.patch.object(bt, "find_bash", return_value=r"C:\Git\bin\bash.exe"):
        tools["bash"] = Bash(session)
    with mock.patch.object(pt.sys, "platform", "win32"), mock.patch.object(
        pt, "find_pwsh", return_value=r"C:\pwsh\pwsh.exe"
    ), mock.patch.object(pt, "load_desc", return_value="<PWSH_MD>"):
        tools["pwsh"] = Powershell(session)
    tools["python"] = python(session)
    with mock.patch.object(rt, "USE_SYSTEM_SHELL", True), mock.patch.object(
        rt, "USE_SYSTEM_PWSH_ON_WINDOWS", False
    ), mock.patch.object(rt, "find_bash", return_value=None):
        tools["Run"] = Run(session)
    return tools


# ── shared fragment text ──────────────────────────────────────────────────

def test_shared_fragments_identical() -> None:
    """Shared param descriptions must be the same object text in all four tools."""
    from kimi_cli.tools.file.bash.bash_tool import BashParams
    from kimi_cli.tools.file.bash.pwsh_tool import PowershellParams
    from kimi_cli.tools.file.run import RunParams
    from kimi_cli.tools.py import Params as PyParams

    def desc(model: type, field: str) -> str:
        return model.model_fields[field].description  # type: ignore[attr-defined]

    # wait_for_pattern / max_lines are byte-identical in all four.
    for field in ("wait_for_pattern", "max_lines"):
        values = {
            name: desc(model, field)
            for name, model in (
                ("bash", BashParams),
                ("pwsh", PowershellParams),
                ("Python", PyParams),
                ("Run", RunParams),
            )
        }
        assert len(set(values.values())) == 1, f"{field} descs diverged: {values}"

    # timeout: all four shell/python tools use seconds; pwsh keeps an extra
    # executor sentence but shares the same unit/range as the shared field.
    for model in (BashParams, PyParams, RunParams):
        assert desc(model, "timeout") == "Timeout in seconds."
    assert desc(PowershellParams, "timeout").startswith("Timeout in seconds")
    assert "timeout" in PowershellParams.model_json_schema()["properties"]
    assert "timeoutMs" not in PowershellParams.model_json_schema()["properties"]

    # task_id: Bash == Powershell (payload 'cmd'); Python uses 'code' + tail.
    assert desc(BashParams, "task_id") == desc(PowershellParams, "task_id")
    assert "'cmd' to stdin" in desc(BashParams, "task_id")
    assert "'code' to stdin" in desc(PyParams, "task_id")
    assert "running a new script" in desc(PyParams, "task_id")
    assert "'command' to stdin" in desc(RunParams, "task_id")

    # deduplicate_output was removed from every running tool (always-on dedup).
    for model in (BashParams, PowershellParams, PyParams, RunParams):
        assert "deduplicate_output" not in model.model_json_schema()["properties"]
        assert "token_kill" not in model.model_json_schema()["properties"]

    # cwd/workdir: only Run keeps it; Bash/Powershell/Python dropped it.
    assert "cwd" not in BashParams.model_json_schema()["properties"]
    assert "workdir" not in BashParams.model_json_schema()["properties"]
    assert "cwd" not in PowershellParams.model_json_schema()["properties"]
    assert "cwd" not in PyParams.model_json_schema()["properties"]
    assert "cwd" in RunParams.model_json_schema()["properties"]

    # mode: execute/send/interactive modes documented; deprecated aliases are
    # normalized silently and not exposed to the LLM.
    for model in (BashParams, PowershellParams, PyParams):
        assert "'execute'" in desc(model, "mode")
        assert "'send'" in desc(model, "mode")
        assert "'interactive'" in desc(model, "mode")
        assert "(alias: 'run')" not in desc(model, "mode")
        assert "(alias: 'background')" not in desc(model, "mode")
    assert "(alias: 'run')" not in desc(RunParams, "mode")


def test_accepts_alias_text() -> None:
    assert accepts_alias_text("cmd", "command") == "Accepts `cmd` or `command` parameter."
    assert accepts_alias_text("command", "cmd") == "Accepts `command` or `cmd` parameter."
    assert accepts_alias_text("code", "code_file") == "Accepts `code` or `code_file` parameter."
    # field-level prose omits the " parameter" suffix
    assert accepts_alias_text("cmd", "command", word=False) == "Accepts `cmd` or `command`."
    with pytest.raises(ValueError):
        accepts_alias_text("only-one")


def test_normalize_mode_validator() -> None:
    assert normalize_mode_validator({"interactive": True}) == {"interactive": True, "mode": "interactive"}
    assert normalize_mode_validator({"mode": "run"}) == {"mode": "execute"}
    assert normalize_mode_validator({"mode": "background"}) == {"mode": "send"}
    assert normalize_mode_validator({"mode": "execute"}) == {"mode": "execute"}
    # non-dict input passes through untouched
    assert normalize_mode_validator("junk") == "junk"  # type: ignore[arg-type]


# ── Layer-2(b) description snapshots ──────────────────────────────────────

def test_descriptions_unchanged() -> None:
    """The four composed descriptions must match the expected snapshots.

    Snapshot reflects the post-Layer-2(b) wire text: the generic conventions
    (fold, dedup, ``rtk``, ``cwd``/``workdir`` aliases) were deleted from the
    descriptions — they now live once in the system.md conventions block —
    and only tool-specific sentences remain.
    """
    tools = _build_tools()

    # Proactive self-kill hint appended by the shell tools at init time; the
    # PID is the live process PID, so the snapshot interpolates it.
    self_kill_hint_text = (
        f" Safety: runs in the agent process (PID {os.getpid()}); never kill that PID, "
        "its parents, or this process's image — the self-kill guard blocks it."
    )

    assert tools["bash"].description == (
        "Execute a bash command, ALWAYS native POSIX syntax. "
        "For long sessions: interactive=True, then task_id=<id> to continue; "
        "wait_for_pattern to block; job_output to monitor; send 'exit' to close."
          " On Windows prefer the `pwsh` tool. Git Bash `cmd` is auto-fixed before it runs:"
          " unquoted `\\`→`/`, `/tmp/…`, `/c/…`→native paths, `nul`→`/dev/null`, `cd /d`→`cd`,"
          " msiexec-style `//flag`→`/flag` (MSYS path conversion is off; `/flag` passes"
          " verbatim), missing POSIX tools (`open`, `watch`, `sudo`, …)"
          "→fallbacks, cmd.exe forms→bash; unfixable commands are rejected with the reason."
          + self_kill_hint_text
    )

    assert tools["pwsh"].description == (
        "<PWSH_MD> "
        "For long sessions: interactive=True, then task_id=<id> to continue; "
        "wait_for_pattern to block; job_output to monitor; send 'exit' to close. "
        "Windows paths must use backslashes (`\\`) instead of forward slashes (`/`)."
        + self_kill_hint_text
    )

    assert tools["python"].description == (
        "Execute Python code or a .py file (auto-detected via `code`). "
        "Scripts run with a resolved interpreter (project .venv, else backend). "
        "Install packages with '<python> -m pip install <pkg>' or 'uv pip install <pkg>' "
        "in the project dir — not bare 'pip install'. "
        "The child env is scrubbed of secret-looking vars by default. "
        "For long runs: run_in_background=True, then task_id=<id> to continue; "
        "wait_for_pattern to block; job_output to monitor."
    )

    assert tools["Run"].description == (
        "Run an executable or bash command. "
        "For long runs: run_in_background=True, then task_id=<id> to continue; "
        "wait_for_pattern to block; job_output to monitor."
    )


def test_bash_windows_description_mentions_fix_rules() -> None:
    """On Windows the Bash description states the automatic Git Bash fix rules.

    The tool rewrites every ``cmd`` (unquoted backslashes + ``fix_bash_command``)
    before running it, so the wire text must say so — and stay short enough to
    travel with every request.  It must also point the model at the ``pwsh``
    tool first, because PowerShell is the native Windows shell.
    """
    from kimi_cli.tools.file.bash import bash_tool as bt

    tools = _build_tools()
    desc = tools["bash"].description
    for fragment in (
        "auto-fixed",
        "unquoted",  # `_prepare_bash_cmd`: unquoted `\` -> `/`
        "/tmp/",  # Git Bash virtual POSIX path -> native temp dir
        "/c/",  # drive-letter spelling -> `C:/`
          "/dev/null",  # `nul` rewrite
          "cd /d",
          "//flag",  # doubled-slash native-tool flag rewrite
          "rejected",  # BashFix.unsupported gate
    ):
        assert fragment in desc, f"Windows bash description missing {fragment!r}"
    note = bt._WINDOWS_BASH_FIX_DESCRIPTION
    assert note.startswith(" ")
    # Windows preference: point at the native-shell tool before the fix rules.
    assert "prefer the `pwsh` tool" in note
    assert note.index("`pwsh`") < note.index("auto-fixed")
    # Concise: one short paragraph, no line breaks, no doubled spaces.
    assert "\n" not in note and len(note) < 420


def test_bash_non_windows_description_omits_fix_rules() -> None:
    """Off Windows the fix note is absent: nothing rewrites the command there."""
    from kimi_cli.tools.file.bash import bash_tool as bt
    from kimi_cli.tools.file.bash.bash_tool import Bash

    with mock.patch.object(bt.sys, "platform", "linux"), mock.patch.object(
        bt, "_should_enable_bash", return_value=True
    ), mock.patch.object(bt, "find_bash", return_value="/bin/bash"):
        tool = Bash(_FakeSession())
    assert tool.description.startswith("Execute a bash command, ALWAYS native POSIX syntax. ")
    assert "auto-fixed" not in tool.description
    assert "/dev/null" not in tool.description


def test_generic_conventions_removed_from_tool_text() -> None:
    """Layer 2(b): generic conventions no longer repeat inside each tool."""
    tools = _build_tools()
    for tool in tools.values():
        desc = tool.description
        assert "head+tail fold" not in desc
        assert "token_kill" not in desc
        assert "`rtk`" not in desc and "rtk " not in desc
        assert "`cwd`/`workdir` sets" not in desc
        assert "Accepts `cmd` or `command` parameter" not in desc
        assert "Accepts `command` or `cmd` parameter" not in desc
        # per-tool param schemas keep only the minimal, tool-specific text
        schema = tool.params.model_json_schema()
        timeout_prop = "timeout"
        assert schema["properties"][timeout_prop]["description"]
        assert schema["properties"]["max_lines"]["description"] == "Max lines to return. None = unlimited."
        assert schema["properties"]["wait_for_pattern"]["description"] == "Pattern to wait for in the tool output."


def test_conventions_block_in_system_prompt() -> None:
    """The generic conventions live once in the default system prompt."""
    system_md = (
        Path(__file__).resolve().parents[3]
        / "kimi-cli" / "src" / "kimi_cli" / "agents" / "default" / "system.md"
    )
    text = system_md.read_text(encoding="utf-8")
    assert "# Tool Conventions" in text
    for fragment in (
        "head+tail fold",
        "deduplicated automatically",
        "`rtk <process> <arguments...>`",
        "Parameter aliases",
        "`wait_for_pattern`",
        "`timeout`",
        "Working directory",
        "Only the `Run` tool accepts `cwd`/`workdir`",
    ):
        assert fragment in text, f"conventions block missing: {fragment}"
    # Removed params are no longer documented as opt-outs.
    assert "deduplicate_output=False" not in text
    assert "`cwd`/`workdir` sets" not in text


# ── shared validators ─────────────────────────────────────────────────────

def test_validators_shared() -> None:
    """``_normalize_mode`` behaves identically across the three Params models."""
    from kimi_cli.tools.file.bash.bash_tool import BashParams
    from kimi_cli.tools.file.bash.pwsh_tool import PowershellParams
    from kimi_cli.tools.py import Params as PyParams

    models = (BashParams, PowershellParams, PyParams)
    for model in models:
        payload = {"cmd": "x"} if model is not PyParams else {"code": "x"}
        assert model.model_validate({**payload, "interactive": True}).mode == "interactive"
        assert model.model_validate({**payload, "mode": "run"}).mode == "execute"
        assert model.model_validate({**payload, "mode": "background"}).mode == "send"
        assert model.model_validate({**payload, "mode": "execute"}).mode == "execute"


def test_pwsh_legacy_timeout_ms_converts_to_seconds() -> None:
    """Legacy ``timeoutMs`` (milliseconds) converts to canonical ``timeout`` seconds."""
    from kimi_cli.tools.file.bash.pwsh_tool import PowershellParams

    assert PowershellParams(cmd="echo hi", timeoutMs=30000).timeout == 30
    # Canonical `timeout` wins when both spellings are supplied.
    assert PowershellParams(cmd="echo hi", timeout=5, timeoutMs=30000).timeout == 5
    # Sub-second legacy values floor to the 1s minimum.
    assert PowershellParams(cmd="echo hi", timeoutMs=500).timeout == 1


def test_shell_cmd_required_validator_shared() -> None:
    """Bash/Powershell share the input-required after-validator semantics."""
    from kimi_cli.tools.file.bash.bash_tool import BashParams
    from kimi_cli.tools.file.bash.pwsh_tool import PowershellParams

    for model, field in ((BashParams, "cmd"), (PowershellParams, "command")):
        with pytest.raises(ValidationError, match=rf"{field} cannot be empty when mode='execute'"):
            model.model_validate({"mode": "execute"})
        with pytest.raises(ValidationError, match=rf"{field} cannot be empty when continuing"):
            model.model_validate({"mode": "send", "task_id": "abc"})
        ok = model.model_validate({"mode": "send", "task_id": "abc", "cmd": "echo hi"})
        assert ok.task_id == "abc"
        assert ok.mode == "send"


def test_python_params_still_validate_source() -> None:
    """Python keeps its own (looser) input rules; sanity-check unchanged."""
    from kimi_cli.tools.py import Params as PyParams

    with pytest.raises(ValidationError, match="code"):
        PyParams.model_validate({})
    # interactive without code is allowed for Python
    ok = PyParams.model_validate({"mode": "interactive"})
    assert ok.mode == "interactive"
    with pytest.raises(ValidationError, match="code cannot be empty when continuing"):
        PyParams.model_validate({"mode": "send", "task_id": "abc"})


def test_timeout_cap_shared_by_all_long_running_tools() -> None:
    """bash / pwsh / python / Run advertise one cap (1200s).

    ``MAX_TIMEOUT_SECONDS`` in ``prompt_common`` is the single source of truth
    for the *process-spawning* tools: ``timeout_field()`` (bash, python, Run)
    and the hand-written ``Field`` of ``pwsh`` all bind their ``le=`` to it, so
    the schema maximum cannot drift per tool.  ``job_output`` only observes an
    already-running task instead of spawning one, so it intentionally documents
    its own (longer) cap, ``background.MAX_JOB_OUTPUT_TIMEOUT_SECONDS``, which
    is pinned here as well.
    """
    from kimi_cli.tools.background import MAX_JOB_OUTPUT_TIMEOUT_SECONDS, TaskOutputParams
    from kimi_cli.tools.file.bash.bash_tool import BashParams
    from kimi_cli.tools.file.bash.pwsh_tool import PowershellParams
    from kimi_cli.tools.file.run import RunParams
    from kimi_cli.tools.prompt_common import MAX_TIMEOUT_SECONDS, timeout_field
    from kimi_cli.tools.py import Params as PyParams

    assert MAX_TIMEOUT_SECONDS == 1200
    # The shared factory itself carries the cap.
    class _TimeoutProbe(BaseModel):
        timeout: int = timeout_field()

    probe = _TimeoutProbe.model_json_schema()["properties"]["timeout"]
    assert probe["default"] == 30
    assert (probe["minimum"], probe["maximum"]) == (1, MAX_TIMEOUT_SECONDS)
    models = (BashParams, PowershellParams, PyParams, RunParams)
    for model in models:
        prop = model.model_json_schema()["properties"]["timeout"]
        assert prop["minimum"] == 1, model.__name__
        assert prop["maximum"] == MAX_TIMEOUT_SECONDS, model.__name__
    # job_output observes an already-running task, so it carries its own cap.
    observe_prop = TaskOutputParams.model_json_schema()["properties"]["timeout"]
    assert observe_prop["minimum"] == 1
    assert observe_prop["maximum"] == MAX_JOB_OUTPUT_TIMEOUT_SECONDS
    assert MAX_JOB_OUTPUT_TIMEOUT_SECONDS > MAX_TIMEOUT_SECONDS
    # Boundary behaviour matches the advertised range for every tool.
    payloads: list[tuple[type, dict[str, Any]]] = [
        (BashParams, {"cmd": "x"}),
        (PowershellParams, {"cmd": "x"}),
        (PyParams, {"code": "x"}),
        (RunParams, {"command": "x"}),
        (TaskOutputParams, {}),
    ]
    for model, payload in payloads:
        cap = MAX_TIMEOUT_SECONDS if model is not TaskOutputParams else (
            MAX_JOB_OUTPUT_TIMEOUT_SECONDS
        )
        assert model.model_validate({**payload, "timeout": cap}).timeout == cap
        with pytest.raises(ValidationError):
            model.model_validate({**payload, "timeout": cap + 1})


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
