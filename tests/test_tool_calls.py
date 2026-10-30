"""Tests for the session tool-invocation iterator (``kimix.utils.tool_calls``)."""

from __future__ import annotations

from types import SimpleNamespace

import orjson
import pytest
from kosong.message import Message, TextPart, ToolCall

from kimix.utils import tool_calls
from kimix.utils.tool_calls import (
    STATUS_FAILED,
    STATUS_NO_RESULT,
    STATUS_OK,
    collect_tool_invocations,
    iter_tool_invocations,
    session_history,
    split_tool_result,
    tool_call_error_dicts,
)

# ── helpers ──────────────────────────────────────────────────────────────


def _call(call_id: str, name: str, arguments: str | None) -> ToolCall:
    return ToolCall(id=call_id, function=ToolCall.FunctionBody(name=name, arguments=arguments))


def _assistant(*calls: ToolCall) -> Message:
    return Message(role="assistant", content="", tool_calls=list(calls))


def _result(call_id: str, *texts: str) -> Message:
    return Message(role="tool", content=[TextPart(text=t) for t in texts], tool_call_id=call_id)


def _session(history: list[Message]) -> SimpleNamespace:
    return SimpleNamespace(
        id="sess",
        _cli=SimpleNamespace(soul=SimpleNamespace(context=SimpleNamespace(history=history))),
        status=SimpleNamespace(context_usage=0.5, context_tokens=10),
    )


# ── session_history ──────────────────────────────────────────────────────


def test_session_history_from_sdk_layout():
    history = [Message(role="user", content="hi")]
    assert session_history(_session(history)) == history


def test_session_history_from_accessor_and_garbage():
    assert session_history(SimpleNamespace(history=lambda: [1, 2])) == [1, 2]
    assert session_history(SimpleNamespace(history=lambda: (_ for _ in ()).throw(RuntimeError()))) == []
    assert session_history(None) == []
    assert session_history(SimpleNamespace()) == []


# ── pairing ──────────────────────────────────────────────────────────────


def test_pairs_calls_with_results_and_marks_failures():
    history = [
        Message(role="user", content="run things"),
        _assistant(
            _call("c1", "bash", '{"cmd": "ls"}'),
            _call("c2", "bash", '{"cmd": "cat missing"}'),
        ),
        _result("c1", "<system>success</system>", "a.txt"),
        _result("c2", "<system>ERROR: failed Hint: not found</system>", "bash: cat: no such file"),
    ]
    invocations = collect_tool_invocations(_session(history))

    assert [inv.index for inv in invocations] == [0, 1]
    ok, failed = invocations
    assert ok.tool_name == "bash"
    assert ok.arguments == {"cmd": "ls"}
    assert ok.command == "ls"
    assert ok.message == "success"
    assert ok.output == "a.txt"
    assert ok.is_error is False
    assert ok.has_result is True
    assert ok.status == STATUS_OK

    assert failed.is_error is True
    assert failed.has_result is True
    assert failed.status == STATUS_FAILED
    # The soul's "ERROR: " prefix is stripped from the reported message.
    assert failed.message == "failed Hint: not found"
    assert failed.output == "bash: cat: no such file"


def test_call_without_result_is_reported_not_dropped():
    history = [_assistant(_call("c1", "bash", '{"cmd": "sleep 1"}'))]
    (invocation,) = collect_tool_invocations(_session(history))
    assert invocation.has_result is False
    assert invocation.is_error is False
    assert invocation.status == STATUS_NO_RESULT
    assert invocation.message is None
    assert invocation.output is None


def test_later_result_wins_for_a_repeated_call_id():
    history = [
        _assistant(_call("c1", "bash", '{"cmd": "ls"}')),
        _result("c1", "<system>ERROR: timeout</system>"),
        _result("c1", "<system>success</system>", "fine"),
    ]
    (invocation,) = collect_tool_invocations(_session(history))
    assert invocation.status == STATUS_OK
    assert invocation.output == "fine"


def test_orphan_results_are_ignored():
    history = [_result("nope", "<system>success</system>", "x")]
    assert collect_tool_invocations(_session(history)) == []


# ── filters ──────────────────────────────────────────────────────────────


def test_tool_name_filter_and_failed_only():
    history = [
        _assistant(
            _call("c1", "bash", '{"cmd": "ls"}'),
            _call("c2", "Read", '{"path": "a.txt"}'),
            _call("c3", "BASH", '{"cmd": "false"}'),
        ),
        _result("c1", "<system>success</system>"),
        _result("c2", "<system>success</system>", "contents"),
        _result("c3", "<system>ERROR: failed</system>"),
    ]
    bash_only = collect_tool_invocations(_session(history), tool_names=("bash",))
    assert [inv.call_id for inv in bash_only] == ["c1", "c3"]
    assert [inv.index for inv in bash_only] == [0, 1]

    failed = collect_tool_invocations(_session(history), tool_names=("bash",), failed_only=True)
    assert [inv.call_id for inv in failed] == ["c3"]
    assert failed[0].status == STATUS_FAILED


def test_explicit_history_overrides_session():
    history = [_assistant(_call("c1", "bash", '{"cmd": "ls"}'))]
    invocations = collect_tool_invocations(None, history=history)
    assert len(invocations) == 1
    assert invocations[0].command == "ls"


def test_iterator_is_lazy():
    history = [_assistant(_call("c1", "bash", "{}"))]
    iterator = iter_tool_invocations(_session(history))
    assert next(iterator).call_id == "c1"
    assert next(iterator, None) is None


# ── arguments ────────────────────────────────────────────────────────────


def test_arguments_parsing_variants():
    history = [
        _assistant(
            _call("c1", "bash", '{"command": "/tmp/x", "mode": "execute", "timeout": 30}'),
            _call("c2", "bash", ""),
            _call("c3", "bash", None),
            _call("c4", "bash", "'{'cmd': 'ls -l',}"),
        )
    ]
    by_id = {inv.call_id: inv for inv in collect_tool_invocations(_session(history))}

    assert by_id["c1"].arguments == {"command": "/tmp/x", "mode": "execute", "timeout": 30}
    # ``command`` is accepted as an alias of ``cmd``.
    assert by_id["c1"].command == "/tmp/x"
    assert by_id["c2"].arguments == {}
    assert by_id["c2"].arguments_error is None
    assert by_id["c3"].arguments is None
    assert by_id["c3"].arguments_error is None
    # Relaxed (LLM-style) JSON is repaired instead of reported as an error.
    assert by_id["c4"].arguments == {"cmd": "ls -l"}
    assert by_id["c4"].arguments_error is None


def test_unparsable_arguments_keep_the_raw_payload():
    raw = '[1, 2, 3]'
    history = [_assistant(_call("c1", "bash", raw))]
    (invocation,) = collect_tool_invocations(_session(history))
    assert invocation.arguments is None
    assert invocation.command is None
    assert invocation.arguments_raw == raw
    assert invocation.arguments_error == "arguments are not a JSON object (list)"
    data = invocation.to_dict()
    assert data["arguments_raw"] == raw
    assert data["arguments_error"] == invocation.arguments_error


# ── result splitting ─────────────────────────────────────────────────────


def test_split_tool_result_multiple_system_parts_and_error_marker():
    message = _result(
        "c1",
        "<system>Results truncated to 20 lines.</system>",
        "line one",
        "<system>ERROR: Command execution failed</system>",
        "boom",
    )
    metadata, output, is_error = split_tool_result(message)
    assert is_error is True
    assert metadata == "Results truncated to 20 lines.\nCommand execution failed"
    assert output == "line one\nboom"


def test_split_tool_result_handles_non_text_parts_and_dicts():
    message = {
        "role": "tool",
        "tool_call_id": "c1",
        "content": [{"type": "text", "text": "<system>success</system>"}, {"type": "image_url"}],
    }
    metadata, output, is_error = split_tool_result(message)
    assert (metadata, output, is_error) == ("success", "[image_url]", False)

    assert split_tool_result(_result("c1", "<system>Tool output is empty.</system>")) == (
        "Tool output is empty.",
        None,
        False,
    )


# ── dict-shaped history / dict view ──────────────────────────────────────


def test_dict_shaped_history_is_supported():
    history = [
        {
            "role": "assistant",
            "content": [],
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": '{"cmd": "ls"}'}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": [{"type": "text", "text": "<system>ERROR: nope</system>"}]},
    ]
    (invocation,) = collect_tool_invocations(None, history=history)
    assert invocation.tool_name == "bash"
    assert invocation.command == "ls"
    assert invocation.is_error is True
    assert invocation.message == "nope"


def test_tool_call_error_dicts_is_json_serializable():
    history = [
        _assistant(_call("c1", "bash", '{"cmd": "false"}')),
        _result("c1", "<system>ERROR: failed</system>", "exit 1"),
    ]
    payload = tool_call_error_dicts(_session(history))
    assert len(payload) == 1
    assert payload[0]["status"] == STATUS_FAILED
    assert payload[0]["arguments"] == {"cmd": "false"}
    assert payload[0]["message"] == "failed"
    assert payload[0]["output"] == "exit 1"
    # Round-trips through orjson (the CLI emits it as JSON).
    assert orjson.loads(orjson.dumps(payload))[0]["call_id"] == "c1"


def test_get_tool_call_errors_uses_the_default_session(monkeypatch):
    history = [
        _assistant(_call("c1", "bash", '{"cmd": "false"}'), _call("c2", "bash", '{"cmd": "true"}')),
        _result("c1", "<system>ERROR: failed</system>"),
        _result("c2", "<system>success</system>"),
    ]
    monkeypatch.setattr(tool_calls, "session_history", lambda session: history)
    assert [entry["call_id"] for entry in tool_calls.tool_call_error_dicts(None)] == ["c1"]


def test_empty_history_yields_nothing():
    assert collect_tool_invocations(_session([])) == []
    assert collect_tool_invocations(None) == []


@pytest.mark.parametrize("session", [None, SimpleNamespace(), SimpleNamespace(history="nope")])
def test_broken_sessions_never_raise(session):
    assert collect_tool_invocations(session) == []
