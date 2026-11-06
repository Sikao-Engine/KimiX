from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from kaos.path import KaosPath
from kosong.message import Message, ToolCall
from typer.testing import CliRunner

from kimi_cli.cli import cli
from kimi_cli.cli.export import _format_message_timestamp
import pendulum

from kimi_cli.utils import export as export_mod
from kimi_cli.utils.export import (
    _extract_tool_call_hint,
    _format_tool_call_md,
    _message_to_export_dict,
)
from kimi_cli.metadata import load_metadata, save_metadata
from kimi_cli.session import Session
from kimi_cli.wire.types import TextPart, TurnBegin


@pytest.fixture
def isolated_share_dir(monkeypatch, tmp_path: Path) -> Path:
    share_dir = tmp_path / "share"
    share_dir.mkdir()

    def _get_share_dir() -> Path:
        share_dir.mkdir(parents=True, exist_ok=True)
        return share_dir

    monkeypatch.setattr("kimi_cli.share.get_share_dir", _get_share_dir)
    monkeypatch.setattr("kimi_cli.metadata.get_share_dir", _get_share_dir)
    return share_dir


@pytest.fixture
def work_dir(tmp_path: Path) -> KaosPath:
    path = tmp_path / "work"
    path.mkdir()
    return KaosPath.unsafe_from_local_path(path)


def _write_context_message(context_file: Path, text: str) -> None:
    message = Message(role="user", content=[TextPart(text=text)])
    context_file.write_text(message.model_dump_json(exclude_none=True) + "\n", encoding="utf-8")


async def _create_previous_session(work_dir: KaosPath) -> tuple[Session, float]:
    session = await Session.create(work_dir)
    _write_context_message(session.context_file, "export me")

    first_ts = 1_700_000_000.0
    last_ts = 1_700_000_100.0
    await session.wire_file.append_message(
        TurnBegin(user_input=[TextPart(text="previous export target")]),
        timestamp=first_ts,
    )
    await session.wire_file.append_message(
        TurnBegin(user_input=[TextPart(text="latest user message")]),
        timestamp=last_ts,
    )
    await session.refresh()

    metadata = load_metadata()
    work_dir_meta = metadata.get_work_dir_meta(work_dir)
    assert work_dir_meta is not None
    work_dir_meta.last_session_id = session.id
    save_metadata(metadata)

    return session, last_ts


def test_export_previous_session_requires_confirmation(
    isolated_share_dir: Path, work_dir: KaosPath, tmp_path: Path
) -> None:
    session, last_ts = asyncio.run(_create_previous_session(work_dir))
    output = tmp_path / "previous.zip"

    result = CliRunner().invoke(
        cli,
        ["--work-dir", str(work_dir), "export", "--output", str(output)],
        input="n\n",
    )

    assert result.exit_code == 0, result.output
    assert "About to export the previous session for this working directory:" in result.output
    assert f"Work dir: {work_dir}" in result.output
    assert f"Session ID: {session.id}" in result.output
    assert "Title: previous export target" in result.output
    assert f"Last user message: {_format_message_timestamp(last_ts)}" in result.output
    assert "Export cancelled." in result.output
    assert not output.exists()


def test_export_previous_session_after_confirmation(
    isolated_share_dir: Path, work_dir: KaosPath, tmp_path: Path
) -> None:
    session, last_ts = asyncio.run(_create_previous_session(work_dir))
    output = tmp_path / "confirmed.zip"

    result = CliRunner().invoke(
        cli,
        ["--work-dir", str(work_dir), "export", "--output", str(output)],
        input="y\n",
    )

    assert result.exit_code == 0, result.output
    assert f"Session ID: {session.id}" in result.output
    assert f"Last user message: {_format_message_timestamp(last_ts)}" in result.output
    assert str(output) in result.output
    assert output.exists()

    with zipfile.ZipFile(output) as zf:
        names = set(zf.namelist())
        assert "context.db" in names
        assert "wire.jsonl" in names
        assert "manifest.json" in names


def test_export_previous_session_can_skip_confirmation_with_yes(
    isolated_share_dir: Path, work_dir: KaosPath, tmp_path: Path
) -> None:
    asyncio.run(_create_previous_session(work_dir))
    output = tmp_path / "skip-confirm.zip"

    result = CliRunner().invoke(
        cli,
        ["--work-dir", str(work_dir), "export", "--yes", "--output", str(output)],
    )

    assert result.exit_code == 0, result.output
    assert "About to export the previous session" not in result.output
    assert "Export this session?" not in result.output
    assert output.exists()


def test_export_explicit_session_id_skips_confirmation(
    isolated_share_dir: Path, work_dir: KaosPath, tmp_path: Path
) -> None:
    session, _ = asyncio.run(_create_previous_session(work_dir))
    output = tmp_path / "explicit.zip"

    result = CliRunner().invoke(
        cli,
        ["--work-dir", str(work_dir), "export", "--output", str(output), session.id],
    )

    assert result.exit_code == 0, result.output
    assert "About to export the previous session" not in result.output
    assert "Export this session?" not in result.output
    assert output.exists()


def test_export_explicit_session_id_accepts_options_after_argument(
    isolated_share_dir: Path, work_dir: KaosPath, tmp_path: Path
) -> None:
    session, _ = asyncio.run(_create_previous_session(work_dir))
    output = tmp_path / "explicit-after.zip"

    result = CliRunner().invoke(
        cli,
        ["--work-dir", str(work_dir), "export", session.id, "--output", str(output)],
    )

    assert result.exit_code == 0, result.output
    assert output.exists()


def test_export_previous_session_errors_when_missing(
    isolated_share_dir: Path, work_dir: KaosPath
) -> None:
    result = CliRunner().invoke(cli, ["--work-dir", str(work_dir), "export"])

    assert result.exit_code == 1
    assert "Error: no previous session found for the working directory." in result.output


def test_export_help_is_leaf_command() -> None:
    result = CliRunner().invoke(cli, ["export", "--help"])

    assert result.exit_code == 0, result.output
    assert "Usage: root export [OPTIONS] [session_id]" in result.output


def test_extract_tool_call_hint_handles_stdlib_json_decode_error() -> None:
    """_extract_tool_call_hint must swallow stdlib json.JSONDecodeError from
    loads_relaxed and return an empty hint instead of leaking the exception."""
    with patch(
        "kimi_cli.utils.export.loads_relaxed",
        side_effect=json.JSONDecodeError("Extra data", "doc", 0),
    ):
        assert _extract_tool_call_hint('{"path": "x"}') == ""


def test_format_tool_call_md_falls_back_to_raw_args_on_stdlib_json_error() -> None:
    """_format_tool_call_md must fall back to raw arguments when loads_relaxed
    raises stdlib json.JSONDecodeError."""
    with patch(
        "kimi_cli.utils.export.loads_relaxed",
        side_effect=json.JSONDecodeError("Extra data", "doc", 0),
    ):
        tool_call = ToolCall(
            id="call_1",
            function=ToolCall.FunctionBody(name="ReadFile", arguments='{"path": "x"}'),
        )
        md = _format_tool_call_md(tool_call)
    assert "ReadFile" in md
    assert '{"path": "x"}' in md
    assert "COMMAND [ARGS]..." not in md

# ---------------------------------------------------------------------------
# Export markdown: native bridge selection (soul packed vs tools list[dict])
# ---------------------------------------------------------------------------


def _export_bridge_history() -> list[Message]:
    """A history exercising think-free text, a tool call and its result."""
    return [
        Message(role="user", content=[TextPart(text="中文问题 with ascii")]),
        Message(
            role="assistant",
            content=[TextPart(text="let me look")],
            tool_calls=[
                ToolCall(
                    id="call_1",
                    function=ToolCall.FunctionBody(
                        name="ReadFile", arguments='{"path": "a.txt"}'
                    ),
                )
            ],
        ),
        Message(role="tool", content=[TextPart(text="file body")], tool_call_id="call_1"),
    ]


def _export_bridge_opts() -> dict[str, object]:
    return {
        "session_id": "sess-1",
        "exported_at": "2026-01-02T03:04:05+00:00",
        "work_dir": "C:/work",
        "token_count": 42,
    }


def test_build_export_markdown_routes_through_the_packed_soul_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`build_export_markdown` must prefer soul.build_export_markdown.

    The packed bridge is the reason the kernel exists (one KMB2 blob instead of
    a per-message list[dict]); the tools list[dict] call is only the fallback
    for KIMIX_NATIVE_SOUL=0.
    """
    soul_calls: list[tuple[object, object]] = []
    tools_calls: list[tuple[object, object]] = []

    class _Recorder:
        def __init__(self, sink, payload: bytes) -> None:
            self._sink = sink
            self._payload = payload

        def build_export_markdown(self, history, opts) -> bytes:
            self._sink.append((history, opts))
            return self._payload

    monkeypatch.setattr(export_mod, "_NATIVE_SOUL", _Recorder(soul_calls, b"soul"))
    monkeypatch.setattr(export_mod, "_NATIVE_TOOLS", _Recorder(tools_calls, b"tools"))
    monkeypatch.setattr(export_mod, "_native_use_native", lambda kernel: True)

    result = export_mod.build_export_markdown(
        "sess-1", "C:/work", _export_bridge_history(), 42, pendulum.datetime(2026, 1, 2, 3, 4, 5)
    )

    assert result == "soul"
    assert len(soul_calls) == 1
    assert tools_calls == []
    history_arg, opts_arg = soul_calls[0]
    assert [m["role"] for m in history_arg] == ["user", "assistant", "tool"]
    assert history_arg[1]["tool_calls"][0]["function"]["name"] == "ReadFile"
    assert opts_arg["session_id"] == "sess-1"


def test_build_export_markdown_matches_the_python_reference() -> None:
    """The bridge the export command uses is byte-identical to the reference.

    Compares the native kernel (soul, KMB2 bridge) with the shim's pure-Python
    mirror of the same document, on the same plain-dict history.
    """
    soul = export_mod._NATIVE_SOUL
    reference = getattr(export_mod._NATIVE_TOOLS, "_compat_build_export_markdown", None)
    if soul is None or reference is None:
        pytest.skip("native runtime unavailable; the pure-Python path is in use")

    history = [_message_to_export_dict(m) for m in _export_bridge_history()]
    opts = _export_bridge_opts()

    native = soul.build_export_markdown(history, opts)
    assert isinstance(native, bytes)  # one blob, never a per-message list
    assert native == reference(history, opts)
