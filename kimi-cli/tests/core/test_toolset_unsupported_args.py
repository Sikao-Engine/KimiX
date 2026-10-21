"""Tests for unsupported tool-call argument detection in KimiToolset.handle().

When a tool call carries argument keys the called tool does not support, the
tool must still run (pydantic already ignores extras) and the tool result must
carry a <system-warning> naming the ignored keys, plus a server-side log line.
Arguments are never mutated; MCPTool / WireExternalTool / plugin CallableTools
and extra="allow" models are exempt.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from kimi_cli.soul.toolset import (
    _LEGACY_ARGUMENT_KEYS,
    _UNSUPPORTED_ARGS_MAX_LISTED,
    KimiToolset,
    WireExternalTool,
    _make_unsupported_args_warning,
    _suggest_argument_key,
    _supported_argument_keys,
    _unsupported_argument_keys,
)
from kimi_cli.tools.background import TaskOutputParams
from kimi_cli.tools.file.bash.bash_tool import BashParams
from kimi_cli.tools.file.bash.pwsh_tool import PowershellParams
from kimi_cli.tools.file.edit.params import EditParams
from kimi_cli.tools.file.run import RunParams
from kimi_cli.tools.todo import Params as TodoParams
from kimi_cli.wire.types import ToolCall, ToolResult
from kosong.tooling import CallableTool2, ToolError, ToolOk, ToolReturnValue


class BashEchoTool(CallableTool2[BashParams]):
    """Mirrors bash's params contract (real schema, no shell/process spawned)."""

    name: str = "bash_echo"
    description: str = "Echo params.cmd the way bash would run it (test spy)."
    params: type[BashParams] = BashParams

    def __init__(self) -> None:
        super().__init__()
        self.seen_params: list[BashParams] = []

    async def __call__(self, params: BashParams) -> ToolReturnValue:
        self.seen_params.append(params)
        return ToolOk(output=params.cmd)


class TodoSpyTool(CallableTool2[TodoParams]):
    """Uses the real (extra="forbid") todo Params model with a trivial body."""

    name: str = "todo_spy"
    description: str = "Todo-shaped spy tool using the real todo Params model."
    params: type[TodoParams] = TodoParams

    async def __call__(self, params: TodoParams) -> ToolReturnValue:
        return ToolOk(output=f"todos={len(params.todos or [])}")


class LooseParams(BaseModel):
    model_config = ConfigDict(extra="allow")

    value: str = ""


class LooseTool(CallableTool2[LooseParams]):
    name: str = "loose"
    description: str = "Tool whose params model opts into extras."
    params: type[LooseParams] = LooseParams

    async def __call__(self, params: LooseParams) -> ToolReturnValue:
        return ToolOk(output=params.value)


class ShadowParams(BaseModel):
    """Plugin-style model declaring shadow keys via accepted_extra_arguments."""

    model_config = ConfigDict(extra="ignore")
    accepted_extra_arguments: ClassVar[tuple[str, ...]] = ("shadow_key",)

    value: str = ""


class FailingTool(CallableTool2[BashParams]):
    name: str = "bash_fail"
    description: str = "Always fails, mirroring bash's params contract."
    params: type[BashParams] = BashParams

    async def __call__(self, params: BashParams) -> ToolReturnValue:
        return ToolError(message="boom", brief="boom")


def _make_toolset(*tools: Any) -> KimiToolset:
    ts = KimiToolset()
    for tool in tools:
        ts.add(tool)
    return ts


def _call(name: str, arguments: dict[str, Any], call_id: str = "tc-1") -> ToolCall:
    return ToolCall(
        id=call_id,
        function=ToolCall.FunctionBody(name=name, arguments=json.dumps(arguments)),
    )


async def _run(ts: KimiToolset, tool_call: ToolCall) -> ToolResult:
    result = ts.handle(tool_call)
    assert isinstance(result, asyncio.Task), "expected an async task from handle()"
    return await result


# ── handle(): warning appended, tool still runs ──────────────────────────────


async def test_work_dir_warning_appended_and_command_still_runs() -> None:
    """Reference case: bash has no work_dir; ls must run and warn about work_dir."""
    tool = BashEchoTool()
    ts = _make_toolset(tool)

    tr = await _run(ts, _call("bash_echo", {"cmd": "ls", "work_dir": "/tmp"}))

    assert isinstance(tr.return_value, ToolOk)
    output = tr.return_value.output
    assert isinstance(output, str)
    assert output.startswith("ls")
    assert "<system-warning>" in output
    assert "Ignored unsupported argument(s)" in output
    assert "`work_dir`" in output
    # accepted-parameters list is advertised
    assert "`cmd`" in output
    assert "`timeout`" in output


async def test_no_warning_for_supported_arguments() -> None:
    tool = BashEchoTool()
    ts = _make_toolset(tool)

    tr = await _run(ts, _call("bash_echo", {"cmd": "ls"}))

    assert isinstance(tr.return_value, ToolOk)
    assert tr.return_value.output == "ls"
    assert "<system-warning>" not in tr.return_value.output


async def test_no_warning_for_declared_alias() -> None:
    """`command` is a declared alias of `cmd` — no warning, output unchanged."""
    tool = BashEchoTool()
    ts = _make_toolset(tool)

    tr = await _run(ts, _call("bash_echo", {"command": "ls"}))

    assert isinstance(tr.return_value, ToolOk)
    assert tr.return_value.output == "ls"
    assert "<system-warning>" not in tr.return_value.output


async def test_legacy_interactive_key_not_reported_and_still_honoured() -> None:
    """`interactive` is consumed by the before-validator: no warning, mode set."""
    tool = BashEchoTool()
    ts = _make_toolset(tool)

    tr = await _run(ts, _call("bash_echo", {"cmd": "ls", "interactive": True}))

    assert isinstance(tr.return_value, ToolOk)
    assert tr.return_value.output == "ls"
    assert "<system-warning>" not in tr.return_value.output
    assert len(tool.seen_params) == 1
    assert tool.seen_params[0].mode == "interactive"


async def test_extra_forbid_model_succeeds_with_warning() -> None:
    """The todo Params model is extra="forbid": a stray key is repaired away
    inside CallableTool2.call() today — the call must still succeed, now with
    a warning naming the ignored key."""
    ts = _make_toolset(TodoSpyTool())

    tr = await _run(
        ts, _call("todo_spy", {"todos": [{"title": "T"}], "priority": 1})
    )

    assert isinstance(tr.return_value, ToolOk)
    output = tr.return_value.output
    assert isinstance(output, str)
    assert "todos=1" in output
    assert "<system-warning>" in output
    assert "`priority`" in output


async def test_multiple_unknown_keys_in_one_warning() -> None:
    tool = BashEchoTool()
    ts = _make_toolset(tool)

    tr = await _run(ts, _call("bash_echo", {"cmd": "ls", "a": 1, "b": 2}))

    assert isinstance(tr.return_value, ToolOk)
    output = tr.return_value.output
    assert isinstance(output, str)
    assert output.count("<system-warning>") == 1
    assert "`a`" in output
    assert "`b`" in output
    assert "were ignored" in output


async def test_did_you_mean_suggestion() -> None:
    tool = BashEchoTool()
    ts = _make_toolset(tool)

    tr = await _run(ts, _call("bash_echo", {"cmd": "ls", "cmds": "x"}))

    assert isinstance(tr.return_value, ToolOk)
    output = tr.return_value.output
    assert isinstance(output, str)
    assert "did you mean" in output
    assert "`cmd`" in output


async def test_extra_allow_model_is_exempt() -> None:
    ts = _make_toolset(LooseTool())

    tr = await _run(ts, _call("loose", {"value": "x", "whatever": 1}))

    assert isinstance(tr.return_value, ToolOk)
    assert tr.return_value.output == "x"
    assert "<system-warning>" not in tr.return_value.output


async def test_callable_tool_without_params_model_is_exempt() -> None:
    """Old-style CallableTool (jsonschema **kwargs contract) — local schema is
    not authoritative, so unknown keys must not be reported."""
    ts = _make_toolset(
        WireExternalTool(
            name="ext",
            description="external",
            parameters={"type": "object", "properties": {"value": {"type": "string"}}},
        )
    )

    tr = await _run(ts, _call("ext", {"value": "v", "mystery": 1}))

    # Without a wire the tool cannot complete; the failure shape is a
    # pre-existing handle() gap for old-style tools (no ``params`` attribute),
    # but whichever error comes back must NOT carry our warning.
    assert tr.return_value.is_error is True
    output = tr.return_value.output
    assert isinstance(output, str)
    assert "<system-warning>" not in output


async def test_warning_on_error_result() -> None:
    """The warning lands on ToolError too: is_error stays True, message untouched."""
    ts = _make_toolset(FailingTool())

    tr = await _run(ts, _call("bash_fail", {"cmd": "ls", "work_dir": "/tmp"}))

    assert isinstance(tr.return_value, ToolError)
    assert tr.return_value.is_error is True
    assert tr.return_value.message == "boom"
    output = tr.return_value.output
    assert isinstance(output, str)
    assert "<system-warning>" in output
    assert "`work_dir`" in output


async def test_arguments_not_mutated_and_dedup_unchanged() -> None:
    """Different unsupported keys keep call_keys distinct: both calls execute."""
    tool = BashEchoTool()
    ts = _make_toolset(tool)

    tr_a = await _run(ts, _call("bash_echo", {"cmd": "ls", "work_dir": "/a"}, "tc-a"))
    tr_b = await _run(ts, _call("bash_echo", {"cmd": "ls", "work_dir": "/b"}, "tc-b"))

    assert len(tool.seen_params) == 2, "distinct call_keys must not be deduped"
    assert tool.seen_params[0].model_extra is None
    assert tool.seen_params[1].model_extra is None
    for tr in (tr_a, tr_b):
        output = tr.return_value.output
        assert isinstance(output, str)
        assert output.startswith("ls")
        assert "<system-warning>" in output


async def test_logger_warning_emitted(monkeypatch: Any) -> None:
    captured: list[tuple[str, dict[str, Any]]] = []

    class _LogStub:
        def warning(self, message: str, **kwargs: Any) -> None:
            captured.append((message, kwargs))

        def __getattr__(self, name: str) -> Any:
            return lambda *args, **kwargs: None

    monkeypatch.setattr("kimi_cli.soul.toolset.logger", _LogStub())
    ts = _make_toolset(BashEchoTool())

    await _run(ts, _call("bash_echo", {"cmd": "ls", "work_dir": "/tmp"}))

    assert len(captured) == 1
    message, kwargs = captured[0]
    assert "Ignoring unsupported argument(s) for tool {tool_name}" in message
    assert kwargs["tool_name"] == "bash_echo"
    assert kwargs["call_id"] == "tc-1"
    rendered = message.format(**kwargs)
    assert "Ignoring unsupported argument(s) for tool bash_echo" in rendered
    assert "work_dir" in rendered


# ── Helper-level: schema-derived supported keys ──────────────────────────────


def test_supported_keys_include_fields_and_aliases() -> None:
    supported = _supported_argument_keys(BashParams)
    assert "cmd" in supported  # field name
    assert "command" in supported  # alias=
    run_supported = _supported_argument_keys(RunParams)
    assert "cwd" in run_supported  # field name
    assert "workdir" in run_supported  # AliasChoices member
    todo_supported = _supported_argument_keys(TodoParams)
    assert "scope" in todo_supported
    assert "parent" in todo_supported  # AliasChoices member


def test_unsupported_keys_require_model_and_dict() -> None:
    assert _unsupported_argument_keys(object(), {"cmd": "ls"}) == []
    assert _unsupported_argument_keys(BashParams, None) == []
    assert _unsupported_argument_keys(BashParams, "cmd") == []
    assert _unsupported_argument_keys(BashParams, {}) == []
    assert _unsupported_argument_keys(BashParams, {"cmd": "ls", "work_dir": "/tmp"}) == [
        "work_dir"
    ]


def test_no_warning_for_alias_choices() -> None:
    assert _unsupported_argument_keys(RunParams, {"command": "echo", "cwd": "."}) == []
    assert _unsupported_argument_keys(RunParams, {"command": "echo", "workdir": "."}) == []
    assert _unsupported_argument_keys(TodoParams, {"scope": "x"}) == []
    assert _unsupported_argument_keys(TodoParams, {"parent": "x"}) == []


def test_legacy_timeout_keys_not_reported() -> None:
    assert _unsupported_argument_keys(
        PowershellParams, {"command": "x", "timeoutMs": 5000}
    ) == []
    assert _unsupported_argument_keys(TaskOutputParams, {"timeout_ms": 1000}) == []


def test_legacy_edit_shorthand_not_reported() -> None:
    assert _unsupported_argument_keys(
        EditParams, {"old_string": "a", "new_string": "b"}
    ) == []


def test_legacy_todo_shapes_not_reported() -> None:
    assert _unsupported_argument_keys(TodoParams, {"title": "t", "status": "done"}) == []
    assert _unsupported_argument_keys(TodoParams, {"updates": [{"title": "t"}]}) == []


def test_extra_allow_model_helper_is_exempt() -> None:
    assert _unsupported_argument_keys(LooseParams, {"value": "x", "whatever": 1}) == []


def test_accepted_extra_arguments_hook() -> None:
    """A plugin params model can declare its own shadow keys."""
    assert _unsupported_argument_keys(ShadowParams, {"value": "x", "shadow_key": 1}) == []
    assert _unsupported_argument_keys(ShadowParams, {"value": "x", "mystery": 1}) == [
        "mystery"
    ]


# ── Helper-level: suggestion and warning text ────────────────────────────────


def test_suggest_argument_key() -> None:
    supported = _supported_argument_keys(BashParams)
    assert _suggest_argument_key("cmds", supported) == "cmd"
    assert _suggest_argument_key("work_dir", supported) is None
    assert _suggest_argument_key("zzzzzz", supported) is None
    # Legacy allowlist keys are never suggested.
    assert _suggest_argument_key("interactiv", supported) != "interactive"
    assert _suggest_argument_key("anything", []) is None


def test_make_unsupported_args_warning_lists_are_capped() -> None:
    keys = [f"key_{i:02d}" for i in range(15)]
    text = _make_unsupported_args_warning("bash_echo", BashParams, keys)
    first_line = next(line for line in text.splitlines() if line.startswith("Ignored"))
    assert "… and 10 more" in first_line
    # tool name + at most _UNSUPPORTED_ARGS_MAX_LISTED backticked key names
    assert first_line.count("`") <= 2 * (1 + _UNSUPPORTED_ARGS_MAX_LISTED)


def test_make_unsupported_args_warning_singular_and_plural() -> None:
    singular = _make_unsupported_args_warning("bash_echo", BashParams, ["work_dir"])
    assert "was ignored" in singular
    assert "… and" not in singular
    plural = _make_unsupported_args_warning("bash_echo", BashParams, ["a", "b"])
    assert "were ignored" in plural


def test_legacy_allowlist_is_disjoint_from_suggestions() -> None:
    """Accepted-parameters listings never advertise legacy shadow keys."""
    supported = _supported_argument_keys(BashParams)
    advertised = sorted(supported - _LEGACY_ARGUMENT_KEYS)
    assert "interactive" not in advertised
    assert "old_string" not in advertised
