"""G4 adversarial probe suite for K01 — `subagent` (`Agent`).

Plan §4.4 K01 requires >= 14 probes in ``tests/kimix/test_agent_gate.py``.
Every probe asserts an *observable* outcome (never ``is not None`` alone) and
collectively covers:

* every field of ``SubAgentParams`` (``description``, ``prompt``,
  ``run_in_background``, ``session_id``, ``close_session``, ``return_history``,
  ``history_format``, ``context_files``, ``context_data``, ``inherit_context``);
* every declared alias (``task`` -> ``prompt``; ``session`` -> ``session_id``;
  ``agent_id`` | ``session`` | ``session_id`` for ``interrupt_agent``);
* boundary values (empty prompt, the 100 KiB offload boundary at/above);
* hostile input (``@`` alone, ``@`` traversal escaping the work dir, missing
  absolute path, oversized payload);
* every reachable ``ToolError`` return site of ``kimi_cli.tools.subagent``.

This file is part of the review deliverable (read-only review of the source;
this is the *only* source-tree file the K01 reviewer may add).
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kaos.path import KaosPath
from pydantic import ValidationError

import kimi_cli.tools.subagent as agent_mod
from kimi_agent_sdk._session import _sdk_sessions_dir
from kimix.base import MessageType
from kimi_cli.tools.subagent import (
    Agent,
    AgentClose,
    AgentCloseParams,
    AgentList,
    AgentListParams,
    SubAgentParams,
    _background_tasks_for,
    _child_parent,
    _children_by_parent,
    _destroy_child_sessions_async,
    _forget_child_session,
    _get_agent_session,
    _get_store,
    _register_agent_session,
    _register_child_session,
    _session_dir,
    _take_child_sessions,
    _unregister_agent_session,
)
from kimi_cli.tools.subagent.store import AgentSessionEntry, ConversationTurn


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------
def _parent(session_id: str = "parent-1", work_dir: Any = None) -> MagicMock:
    session = MagicMock()
    session.custom_data = {}
    session.custom_config = {"chat_provider": None}
    session.id = session_id
    session.work_dir = work_dir
    session._cli = None
    return session


def _sub_session(session_id: str = "sub-123") -> MagicMock:
    session = MagicMock()
    session.id = session_id
    session.get_custom_config.return_value = {}
    session.close = AsyncMock()
    return session


def _entry(session: Any, session_id: str, state: str = "running") -> AgentSessionEntry:
    now = time.time()
    return AgentSessionEntry(
        session=session,
        session_id=session_id,
        created_at=now,
        last_accessed=now,
        conversation_history=[],
        total_turns=0,
        is_active=True,
        state=state,  # type: ignore[arg-type]
    )


async def _pump(n: int = 60) -> None:
    """Let detached background tasks run to completion."""
    for _ in range(n):
        await asyncio.sleep(0)


def _capturing_prompt(captured: list[str], text: str = "done"):
    async def _mock(*, prompt_str: str, output_function: Any = None, **kwargs: Any) -> None:
        captured.append(prompt_str)
        if output_function is not None:
            output_function(text, MessageType.Text)

    return _mock


@pytest.fixture(autouse=True)
def _clean_agent_registries():
    """Process-wide agent registries must not leak between probes."""

    def _reset() -> None:
        agent_mod._agent_entries.clear()
        agent_mod._agent_sessions.clear()
        _children_by_parent.clear()
        _child_parent.clear()

    _reset()
    yield
    _reset()


class _FakeSoul:
    """Stand-in for ``KimiSoul`` with just what ``Session.close`` touches."""

    def __init__(self) -> None:
        self.agent = SimpleNamespace(toolset=None)

    async def close(self) -> None:
        return None


class _FakeCLI:
    """Stand-in for ``KimiCLI`` that keeps the *real* ``CliSession``."""

    def __init__(self, session: Any) -> None:
        self.session = session
        self.soul = _FakeSoul()


@pytest.fixture
def real_session_env(monkeypatch, tmp_path: Path) -> Path:
    """Real SDK session machinery with the LLM/config/tool loading stubbed."""
    share_dir = tmp_path / "share"
    share_dir.mkdir()
    monkeypatch.setattr("kimi_cli.share.get_share_dir", lambda: share_dir)
    monkeypatch.setattr("kimi_cli.metadata.get_share_dir", lambda: share_dir)

    from kimi_cli.config import Config

    import kimi_agent_sdk._session as sdk_session

    async def fake_cli_create(cli_session: Any, **kwargs: Any) -> _FakeCLI:
        return _FakeCLI(cli_session)

    monkeypatch.setattr(sdk_session.KimiCLI, "create", fake_cli_create)
    monkeypatch.setattr(
        "kimix.utils.session._create_config",
        lambda provider_dict=None: (Config(), provider_dict),
    )
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    return work_dir


# ---------------------------------------------------------------------------
# Probe 1 — prompt / task alias parity
# ---------------------------------------------------------------------------
async def test_probe_prompt_task_alias_parity() -> None:
    via_task = SubAgentParams(**{"task": "do the thing"})
    via_prompt = SubAgentParams(prompt="do the thing")

    assert via_task.prompt == "do the thing"
    assert via_prompt.prompt == "do the thing"
    assert via_task.model_dump() == via_prompt.model_dump()
    # ... the alias round-trips through JSON (model-facing schema path) too.
    assert (
        SubAgentParams.model_validate_json(json.dumps({"task": "from json"})).prompt
        == "from json"
    )
    assert "prompt" in SubAgentParams.model_json_schema()["properties"]


# ---------------------------------------------------------------------------
# Probe 2 — session_id / session alias parity (+ interrupt_agent aliases)
# ---------------------------------------------------------------------------
async def test_probe_session_aliases_are_interchangeable() -> None:
    by_alias = SubAgentParams(prompt="x", **{"session": "s-1"})
    by_name = SubAgentParams(prompt="x", session_id="s-2")
    assert by_alias.session_id == "s-1"
    assert by_name.session_id == "s-2"
    # Both spellings reach the same field, so the JSON payload is identical.
    assert by_alias.model_dump()["session_id"] == "s-1"
    # The model-facing schema advertises the *alias* only (documented drift).
    props = SubAgentParams.model_json_schema()["properties"]
    assert "session" in props
    assert "session_id" in SubAgentParams.model_fields
    # interrupt_agent accepts agent_id | session | session_id.
    for key in ("agent_id", "session", "session_id"):
        assert AgentCloseParams(**{key: "a-1"}).agent_id == "a-1"


# ---------------------------------------------------------------------------
# Probe 3 — @path resolution: work dir, CWD fallback, missing file
# ---------------------------------------------------------------------------
async def test_probe_at_path_resolution(monkeypatch, tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "in-work.md").write_text("from work dir", encoding="utf-8")
    (tmp_path / "in-cwd.md").write_text("from cwd", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    parent = _parent(work_dir=KaosPath(str(work)))
    captured: list[str] = []
    with (
        patch("kimix.utils._create_session_async", new_callable=AsyncMock) as mock_create,
        patch("kimix.utils.prompt_async", side_effect=_capturing_prompt(captured)),
        patch("kimix.utils.close_session_async", new_callable=AsyncMock),
    ):
        mock_create.return_value = _sub_session()
        agent = Agent(parent)
        r_work = await agent(SubAgentParams(prompt="@in-work.md", run_in_background=False))
        r_cwd = await agent(SubAgentParams(prompt="@in-cwd.md", run_in_background=False))
        r_missing = await agent(SubAgentParams(prompt="@missing.md", run_in_background=False))

    assert r_work.is_error is False and captured[0] == "from work dir"
    assert r_cwd.is_error is False and captured[1] == "from cwd"
    assert r_missing.is_error is True
    assert r_missing.message == "prompt file not found: missing.md"
    assert r_missing.brief == "Failed to create sub-agent session"


# ---------------------------------------------------------------------------
# Probe 4 — run_in_background defaults to True (contrast with C01's False)
# ---------------------------------------------------------------------------
async def test_probe_run_in_background_defaults_true() -> None:
    assert SubAgentParams(prompt="x").run_in_background is True
    assert SubAgentParams(prompt="x", run_in_background=False).run_in_background is False

    parent = _parent()
    with (
        patch("kimix.utils._create_session_async", new_callable=AsyncMock) as mock_create,
        patch("kimix.utils.prompt_async", new_callable=AsyncMock) as mock_prompt,
        patch("kimix.utils.close_session_async", new_callable=AsyncMock),
    ):
        mock_create.return_value = _sub_session()
        result = await Agent(parent)(
            SubAgentParams(prompt="x", description="3-5 word label")
        )
        assert result.extras["status"] == "running"
        assert result.extras["session_id"] in result.output
        # Returned before the child ran: the prompt was never awaited yet.
        mock_prompt.assert_not_awaited()

    for task in list(_background_tasks_for(parent)):
        task.cancel()
    await _pump()


# ---------------------------------------------------------------------------
# Probe 5 — close_session default per mode + explicit overrides
# ---------------------------------------------------------------------------
async def test_probe_close_session_defaults_and_overrides() -> None:
    # Foreground, default -> closed.
    parent_fg = _parent("fg")
    with (
        patch("kimix.utils._create_session_async", new_callable=AsyncMock) as mc,
        patch("kimix.utils.prompt_async", side_effect=_capturing_prompt([])),
        patch("kimix.utils.close_session_async", new_callable=AsyncMock) as mclose,
    ):
        mc.return_value = _sub_session()
        r_fg = await Agent(parent_fg)(
            SubAgentParams(prompt="x", run_in_background=False)
        )
    assert r_fg.extras["status"] == "closed"
    assert mclose.await_count == 1
    assert _get_store(parent_fg).get(r_fg.extras["session_id"]) is None

    # Background, default -> kept open, completed, resumable.
    parent_bg = _parent("bg")
    with (
        patch("kimix.utils._create_session_async", new_callable=AsyncMock) as mc,
        patch("kimix.utils.prompt_async", side_effect=_capturing_prompt([])),
        patch("kimix.utils.close_session_async", new_callable=AsyncMock) as mclose,
    ):
        mc.return_value = _sub_session()
        r_bg = await Agent(parent_bg)(SubAgentParams(prompt="x"))
        assert r_bg.extras["status"] == "running"
        await _pump()
    assert mclose.await_count == 0
    entry = _get_store(parent_bg).get(r_bg.extras["session_id"])
    assert entry is not None and entry.state == "completed" and entry.is_active is True

    # Background, explicit close_session=True -> closed on completion.
    parent_bgc = _parent("bgc")
    with (
        patch("kimix.utils._create_session_async", new_callable=AsyncMock) as mc,
        patch("kimix.utils.prompt_async", side_effect=_capturing_prompt([])),
        patch("kimix.utils.close_session_async", new_callable=AsyncMock) as mclose,
    ):
        mc.return_value = _sub_session()
        r_bgc = await Agent(parent_bgc)(
            SubAgentParams(prompt="x", close_session=True)
        )
        await _pump()
    assert mclose.await_count == 1
    assert _get_store(parent_bgc).get(r_bgc.extras["session_id"]) is None

    # Foreground, explicit close_session=False -> kept open ("continued").
    parent_fgk = _parent("fgk")
    with (
        patch("kimix.utils._create_session_async", new_callable=AsyncMock) as mc,
        patch("kimix.utils.prompt_async", side_effect=_capturing_prompt([])),
        patch("kimix.utils.close_session_async", new_callable=AsyncMock) as mclose,
    ):
        mc.return_value = _sub_session()
        r_fgk = await Agent(parent_fgk)(
            SubAgentParams(prompt="x", run_in_background=False, close_session=False)
        )
    assert r_fgk.extras["status"] == "continued"
    assert mclose.await_count == 0
    kept = _get_store(parent_fgk).get(r_fgk.extras["session_id"])
    assert kept is not None and kept.state == "completed"


# ---------------------------------------------------------------------------
# Probe 6 — inherit_context copies the parent session and resumes the copy
# ---------------------------------------------------------------------------
async def test_probe_inherit_context_copies_and_resumes(tmp_path: Path) -> None:
    parent = _parent(work_dir=KaosPath(str(tmp_path)))
    with (
        patch("kimi_cli.tools.subagent.Session.copy", new_callable=AsyncMock) as mock_copy,
        patch("kimix.utils._create_session_async", new_callable=AsyncMock) as mock_create,
        patch("kimix.utils.prompt_async", side_effect=_capturing_prompt([])),
        patch("kimix.utils.close_session_async", new_callable=AsyncMock),
        patch.object(Agent, "_reset_inherited_system_prompt", new_callable=AsyncMock) as mock_reset,
    ):
        mock_create.return_value = _sub_session()
        result = await Agent(parent)(
            SubAgentParams(
                prompt="x",
                inherit_context=True,
                session_id="inherit-1",
                run_in_background=False,
            )
        )

    assert result.is_error is False
    assert result.extras["session_id"] == "inherit-1"
    assert mock_copy.await_args.args == (KaosPath(str(tmp_path)), "parent-1", "inherit-1")
    assert mock_create.await_args.kwargs["session_id"] == "inherit-1"
    assert mock_create.await_args.kwargs["resume"] is True
    assert mock_reset.await_count == 1

    # A parent without a resolvable id cannot donate its context.
    no_id_parent = _parent()
    no_id_parent.id = ""
    with patch("kimi_cli.tools.subagent.Session.copy", new_callable=AsyncMock) as mock_copy2:
        failed = await Agent(no_id_parent)(
            SubAgentParams(prompt="x", inherit_context=True, run_in_background=False)
        )
    assert failed.is_error is True
    assert failed.message == "Cannot inherit parent context: parent session has no id"
    mock_copy2.assert_not_awaited()


# ---------------------------------------------------------------------------
# Probe 7 — context_files pre-read + context_data passthrough
# ---------------------------------------------------------------------------
async def test_probe_context_files_and_context_data(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "marker.txt").write_text("hello marker", encoding="utf-8")
    parent = _parent(work_dir=KaosPath(str(work)))
    captured: list[str] = []
    with (
        patch("kimix.utils._create_session_async", new_callable=AsyncMock) as mock_create,
        patch("kimix.utils.prompt_async", side_effect=_capturing_prompt(captured)),
        patch("kimix.utils.close_session_async", new_callable=AsyncMock),
    ):
        mock_create.return_value = _sub_session()
        agent = Agent(parent)
        ok = await agent(
            SubAgentParams(
                prompt="do X",
                context_files=["marker.txt"],
                context_data={"k": [1, 2]},
                run_in_background=False,
            )
        )
        # A missing context file is reported inline instead of raising.
        soft = await agent(
            SubAgentParams(
                prompt="do X",
                context_files=["nope.txt"],
                run_in_background=False,
            )
        )

    assert ok.is_error is False
    text = captured[0]
    assert "<file path='marker.txt'>\nhello marker\n</file>" in text
    assert "<data>" in text
    assert '"k": [' in text  # orjson OPT_INDENT_2 rendering
    assert text.startswith("<context>")
    assert "</context>\n\ndo X" in text  # context block precedes the task
    assert soft.is_error is False
    assert "<file path='nope.txt' error='" in captured[1]


# ---------------------------------------------------------------------------
# Probe 8 — return_history + all three history_format values
# ---------------------------------------------------------------------------
async def test_probe_return_history_all_three_formats() -> None:
    turns = [
        ConversationTurn(role="user", content="hi", timestamp=0.0),
        ConversationTurn(
            role="assistant", content="yo", timestamp=0.0, metadata={"type": "text"}
        ),
        ConversationTurn(
            role="tool", content="T", timestamp=0.0, metadata={"type": "tool_call"}
        ),
        ConversationTurn(
            role="tool", content="R", timestamp=0.0, metadata={"type": "tool_result"}
        ),
    ]
    agent = Agent(_parent())

    as_json = agent._format_history(turns, "json")
    assert isinstance(as_json, list) and len(as_json) == 4
    assert as_json[0]["role"] == "user" and as_json[1]["metadata"] == {"type": "text"}

    as_md = agent._format_history(turns, "markdown")
    assert isinstance(as_md, str)
    assert "### Turn 1: 👤 user" in as_md
    assert "### Turn 2: 🤖 text" in as_md
    assert "hi" in as_md and "yo" in as_md

    as_summary = agent._format_history(turns, "summary")
    assert as_summary == (
        "Sub-agent made 1 tool call(s) with 1 result(s), and produced 1 "
        "text response(s) (2 total characters)."
    )

    # Unknown format is defensively empty (never raises).
    assert agent._format_history(turns, "xml") == []

    # ... and every format flows through the ToolOk extras contract.
    for fmt in ("json", "markdown", "summary"):
        parent = _parent(f"fmt-{fmt}")
        captured: list[str] = []
        with (
            patch("kimix.utils._create_session_async", new_callable=AsyncMock) as mc,
            patch("kimix.utils.prompt_async", side_effect=_capturing_prompt(captured)),
            patch("kimix.utils.close_session_async", new_callable=AsyncMock),
        ):
            mc.return_value = _sub_session()
            result = await Agent(parent)(
                SubAgentParams(
                    prompt="x",
                    return_history=True,
                    history_format=fmt,
                    run_in_background=False,
                )
            )
        assert result.is_error is False
        history = result.extras["conversation_history"]
        if fmt == "json":
            assert isinstance(history, list)
            assert any(turn["role"] == "user" for turn in history)
        else:
            assert isinstance(history, str) and history != ""


# ---------------------------------------------------------------------------
# Probe 9 — _session_dir == _sdk_sessions_dir(work_dir) / id
# ---------------------------------------------------------------------------
async def test_probe_session_dir_matches_sdk_layout(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    kwork = KaosPath(str(work))
    session = SimpleNamespace(id="s1", work_dir=kwork, custom_data={})

    assert _session_dir(session, "abc") == _sdk_sessions_dir(kwork) / "abc"
    resolved = Path(str(_session_dir(session, "abc")))
    assert resolved.name == "abc"
    assert resolved.parent.name == ".kimix_cache"

    # No resolvable work dir -> the SDK's process-CWD fallback.
    no_dir = SimpleNamespace(id="s2", custom_data={})
    assert _session_dir(no_dir, "abc") == Path.cwd() / ".kimix_cache" / "abc"


# ---------------------------------------------------------------------------
# Probe 10 — the background task set is per parent session
# ---------------------------------------------------------------------------
async def test_probe_background_tasks_are_per_parent() -> None:
    parent_a = SimpleNamespace(custom_data={})
    parent_b = SimpleNamespace(custom_data={})

    tasks_a = _background_tasks_for(parent_a)
    assert _background_tasks_for(parent_a) is tasks_a  # memoised in custom_data
    assert _background_tasks_for(parent_b) is not tasks_a
    assert parent_a.custom_data["agent_background_tasks"] is tasks_a

    tasks_a.add(asyncio.create_task(asyncio.sleep(0)))
    assert len(_background_tasks_for(parent_a)) == 1
    assert len(_background_tasks_for(parent_b)) == 0

    # The SDK wrapper resolves to the same owning set as the CLI session.
    sdk_wrapper = SimpleNamespace(_cli=SimpleNamespace(session=parent_a))
    assert _background_tasks_for(sdk_wrapper) is tasks_a

    # A session without custom_data still yields an iterable (throwaway) set.
    assert _background_tasks_for(SimpleNamespace()) == set()

    for task in list(tasks_a):
        task.cancel()
    await _pump()


# ---------------------------------------------------------------------------
# Probe 11 — _forget_child_session / _take_child_sessions reverse lookup
# ---------------------------------------------------------------------------
async def test_probe_child_session_reverse_lookup() -> None:
    _register_child_session("p1", "c1")
    _register_child_session("p1", "c2")
    _register_child_session("p2", "c3")
    assert _children_by_parent == {"p1": {"c1", "c2"}, "p2": {"c3"}}
    assert _child_parent == {"c1": "p1", "c2": "p1", "c3": "p2"}

    # Empty ids are ignored on both sides of the relation.
    _register_child_session("", "c9")
    _register_child_session("p9", "")
    assert "c9" not in _child_parent
    assert "p9" not in _children_by_parent

    assert _take_child_sessions("p1") == ["c1", "c2"]
    assert _child_parent == {"c3": "p2"}
    assert "p1" not in _children_by_parent
    assert _take_child_sessions("p1") == []  # idempotent
    assert _take_child_sessions("") == []

    _register_child_session("p3", "c4")
    _forget_child_session("c4")
    assert "c4" not in _child_parent
    assert "p3" not in _children_by_parent
    _forget_child_session("never-seen")  # no-op, never raises
    assert _child_parent == {"c3": "p2"}


# ---------------------------------------------------------------------------
# Probe 12 — parent close cascade deletes the child session directory
# ---------------------------------------------------------------------------
async def test_probe_parent_close_cascade_deletes_child_dir(
    real_session_env: Path,
) -> None:
    from kimi_agent_sdk import Session as SdkSession

    work_dir = real_session_env
    child = await SdkSession.create(
        work_dir=KaosPath(str(work_dir)), session_id="cascade-child", anonymous=True
    )
    child_dir = Path(str(work_dir)) / ".kimix_cache" / "cascade-child"
    assert child_dir.exists()
    _register_agent_session("cascade-child", child)
    _register_child_session("cascade-parent", "cascade-child")
    try:
        destroyed = await _destroy_child_sessions_async("cascade-parent")
        assert destroyed == ["cascade-child"]
        assert child_dir.exists() is False
        assert "cascade-parent" not in _children_by_parent
        assert "cascade-child" not in _child_parent
        assert _get_agent_session("cascade-child") is None
    finally:
        _unregister_agent_session("cascade-child")
        _forget_child_session("cascade-child")

    # A parent with no registered children destroys nothing (no raise).
    assert await _destroy_child_sessions_async("nobody") == []


# ---------------------------------------------------------------------------
# Probe 13 — a failed prompt is saved with an "@<path>" retry hint
# ---------------------------------------------------------------------------
async def test_probe_failed_prompt_saves_retry_file(tmp_path: Path) -> None:
    parent = _parent()
    saved = tmp_path / "saved.md"
    with (
        patch("kimix.utils._create_session_async", new_callable=AsyncMock) as mock_create,
        patch("kimix.utils.prompt_async", new_callable=AsyncMock) as mock_prompt,
        patch("kimix.utils.close_session_async", new_callable=AsyncMock) as mock_close,
        patch("kimi_cli.tools.subagent._create_script_file", return_value=str(saved)) as mock_script,
        patch(
            "kimi_cli.tools.subagent._display_temp_path",
            return_value=".kimix_cache/tmp_9/0.md",
        ),
    ):
        mock_create.return_value = _sub_session()
        mock_prompt.side_effect = RuntimeError("boom")
        result = await Agent(parent)(
            SubAgentParams(prompt="do X", run_in_background=False)
        )

    assert result.is_error is True
    assert result.brief == "sub-agent task failed"
    assert "boom" in result.message
    assert "[prompt saved to .kimix_cache/tmp_9/0.md]" in result.message
    assert "Subagent(prompt=@.kimix_cache/tmp_9/0.md)".lower() in result.message.lower()
    assert result.extras["prompt_file"] == ".kimix_cache/tmp_9/0.md"
    assert result.extras["status"] == "closed"
    assert mock_script.call_args.args[0] == "do X"
    assert mock_script.call_args.kwargs["ext"] == ".md"
    assert mock_close.await_count == 1


# ---------------------------------------------------------------------------
# Probe 14 — session_id determinism: busy, unknown, empty
# ---------------------------------------------------------------------------
async def test_probe_session_id_determinism() -> None:
    parent = _parent()
    busy = _sub_session("busy-1")
    busy._cancel_event = asyncio.Event()  # truthy while a prompt owns it
    store = _get_store(parent)
    store.put(_entry(busy, "busy-1"))

    running = await Agent(parent)(
        SubAgentParams(prompt="more", session_id="busy-1", run_in_background=False)
    )
    assert running.is_error is True
    assert running.message == (
        "Sub-agent 'busy-1' is currently running; wait for it to finish before "
        "resuming it with more work."
    )
    assert running.brief == "Failed to create sub-agent session"
    assert running.output == ""

    # An unknown explicit id is reused verbatim (deterministic, no error).
    with (
        patch("kimix.utils._create_session_async", new_callable=AsyncMock) as mock_create,
        patch("kimix.utils.prompt_async", side_effect=_capturing_prompt([])),
        patch("kimix.utils.close_session_async", new_callable=AsyncMock),
    ):
        mock_create.return_value = _sub_session()
        fresh = await Agent(parent)(
            SubAgentParams(prompt="x", session_id="never-seen-1", run_in_background=False)
        )
    assert fresh.is_error is False
    assert fresh.extras["session_id"] == "never-seen-1"
    assert mock_create.await_args.kwargs["session_id"] == "never-seen-1"

    # An empty session id is treated as "new": a fresh uuid4 is generated.
    with (
        patch("kimix.utils._create_session_async", new_callable=AsyncMock) as mock_create,
        patch("kimix.utils.prompt_async", side_effect=_capturing_prompt([])),
        patch("kimix.utils.close_session_async", new_callable=AsyncMock),
    ):
        mock_create.return_value = _sub_session()
        generated = await Agent(parent)(
            SubAgentParams(prompt="x", session_id="", run_in_background=False)
        )
    new_id = generated.extras["session_id"]
    assert len(new_id) == 36 and mock_create.await_args.kwargs["session_id"] == new_id


# ---------------------------------------------------------------------------
# Probe 15 — interrupt race leaves no orphan session / registry entry
# ---------------------------------------------------------------------------
async def _prepare_bg(parent: MagicMock, sub: MagicMock, session_id: str) -> Any:
    with patch("kimix.utils._create_session_async", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = sub
        agent = Agent(parent)
        return await agent._prepare_run(
            SubAgentParams(prompt="do X", session_id=session_id, close_session=False)
        )


async def test_probe_interrupt_race_leaves_no_orphan() -> None:
    parent = _parent("race-parent")
    started = asyncio.Event()
    release = asyncio.Event()

    async def gated(*, prompt_str: str, output_function: Any = None, **kwargs: Any) -> None:
        started.set()
        await release.wait()
        if output_function is not None:
            output_function("partial work", MessageType.Text)

    prepared = await _prepare_bg(parent, _sub_session(), "race-child")
    with (
        patch("kimix.utils.prompt_async", side_effect=gated),
        patch("kimix.utils.close_session_async", new_callable=AsyncMock) as mock_close,
    ):
        store = _get_store(parent)
        store.put(_entry(prepared.session, "race-child"))
        task = asyncio.create_task(Agent(parent)._run_background(prepared))
        await asyncio.wait_for(started.wait(), timeout=5)
        assert _child_parent.get("race-child") == "race-parent"

        close_result = await AgentClose(parent)(AgentCloseParams(agent_id="race-child"))
        assert close_result.is_error is False
        assert close_result.brief == "Session closed"
        assert store.get("race-child") is None
        assert store.was_closed("race-child") is True

        release.set()
        await asyncio.wait_for(task, timeout=5)

    assert store.get("race-child") is None  # late _update_store did not resurrect
    assert all(entry["session_id"] != "race-child" for entry in store.list_active())
    assert _get_agent_session("race-child") is None
    assert "race-child" not in _child_parent
    assert mock_close.await_count == 1


# ---------------------------------------------------------------------------
# Probe 16 — prompt size boundary (100 KiB) and empty prompt
# ---------------------------------------------------------------------------
async def test_probe_prompt_size_boundary_and_empty(tmp_path: Path) -> None:
    parent = _parent(work_dir=None)
    captured: list[str] = []
    offloaded: list[tuple[str, str]] = []

    def fake_script(content: str, ext: str = ".md") -> str:
        offloaded.append((content, ext))
        return str(tmp_path / f"t{len(offloaded)}.md")

    with (
        patch("kimix.utils._create_session_async", new_callable=AsyncMock) as mock_create,
        patch("kimix.utils.prompt_async", side_effect=_capturing_prompt(captured)),
        patch("kimix.utils.close_session_async", new_callable=AsyncMock),
        patch("kimi_cli.tools.subagent._create_script_file", side_effect=fake_script),
    ):
        mock_create.return_value = _sub_session()
        agent = Agent(parent)

        empty = await agent(SubAgentParams(prompt="", run_in_background=False))
        assert empty.is_error is False
        assert captured[0] == ""  # documented: no empty-prompt validation

        exact = "x" * (100 * 1024)
        at_boundary = await agent(SubAgentParams(prompt=exact, run_in_background=False))
        assert at_boundary.is_error is False
        assert offloaded == []  # 100 KiB is still inline
        assert captured[1] == exact

        over = "x" * (100 * 1024 + 1)
        above = await agent(SubAgentParams(prompt=over, run_in_background=False))
        assert above.is_error is False
        assert offloaded == [(over, ".md")]
        assert "Please read the task from `" in captured[2]
        assert "` and execute it." in captured[2]


# ---------------------------------------------------------------------------
# Probe 17 — hostile @path input
# ---------------------------------------------------------------------------
async def test_probe_hostile_at_paths(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    secret = tmp_path / "secret.md"  # outside the work dir
    secret.write_text("TOP SECRET", encoding="utf-8")
    parent = _parent(work_dir=KaosPath(str(work)))
    captured: list[str] = []
    with (
        patch("kimix.utils._create_session_async", new_callable=AsyncMock) as mock_create,
        patch("kimix.utils.prompt_async", side_effect=_capturing_prompt(captured)),
        patch("kimix.utils.close_session_async", new_callable=AsyncMock),
    ):
        mock_create.return_value = _sub_session()
        agent = Agent(parent)
        # "@" alone resolves to the work dir itself -> IsADirectoryError.
        bare = await agent(SubAgentParams(prompt="@", run_in_background=False))
        # A missing absolute path is a deterministic ToolError.
        missing_abs = await agent(
            SubAgentParams(prompt=f"@{tmp_path / 'nope.md'}", run_in_background=False)
        )
        # "../" escapes the work dir and is read anyway (finding: no containment).
        escaped = await agent(SubAgentParams(prompt="@../secret.md", run_in_background=False))

    assert bare.is_error is True
    assert bare.brief == "Failed to create sub-agent session"
    assert missing_abs.is_error is True
    assert "prompt file not found" in missing_abs.message
    assert escaped.is_error is False
    assert captured[-1] == "TOP SECRET"


# ---------------------------------------------------------------------------
# Probe 18 — wrong types & JSON-schema round-trip
# ---------------------------------------------------------------------------
async def test_probe_wrong_types_and_schema_roundtrip() -> None:
    with pytest.raises(ValidationError):
        SubAgentParams()  # prompt is required
    with pytest.raises(ValidationError):
        SubAgentParams(prompt=123)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        SubAgentParams(prompt="x", history_format="xml")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        SubAgentParams(prompt="x", context_data=["a"])  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        SubAgentParams(prompt="x", context_files="not-a-list")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        SubAgentParams(prompt="x", return_history="maybe")  # type: ignore[arg-type]

    # The lax coercions pydantic performs are asserted, not assumed.
    assert SubAgentParams(prompt="x", run_in_background="yes").run_in_background is True  # type: ignore[arg-type]
    assert SubAgentParams(prompt="x", close_session=1).close_session is True  # type: ignore[arg-type]
    assert SubAgentParams(prompt="x", description="lbl").description == "lbl"

    payload = SubAgentParams(prompt="x").model_dump()
    assert set(payload) == {
        "description",
        "prompt",
        "run_in_background",
        "session_id",
        "close_session",
        "return_history",
        "history_format",
        "context_files",
        "context_data",
        "inherit_context",
    }
    assert SubAgentParams.model_validate(payload) == SubAgentParams(prompt="x")


# ---------------------------------------------------------------------------
# Probe 19 — recursive guard + companion tools (list_agents / interrupt_agent)
# ---------------------------------------------------------------------------
async def test_probe_recursive_guard_and_companions() -> None:
    parent = _parent()
    parent.custom_config = {"is_sub_agent": True}
    guarded = await Agent(parent)(SubAgentParams(prompt="x"))
    assert guarded.is_error is True
    assert guarded.message == "Recursive sub-agent call detected"
    assert guarded.brief == "sub-agent recursively"
    assert guarded.output == ""

    lister = _parent("lister")
    lister.custom_config = {"chat_provider": None}
    for scope in ("children", "descendants"):
        listing = await AgentList(lister)(AgentListParams(scope=scope))
        assert listing.is_error is False
        assert json.loads(listing.output) == []

    _get_store(lister).put(_entry(_sub_session(), "listed-1", state="completed"))
    populated = await AgentList(lister)(AgentListParams())
    payload = json.loads(populated.output)
    assert len(payload) == 1
    assert payload[0]["session_id"] == "listed-1"
    assert payload[0]["state"] == "completed"
    assert payload[0]["is_active"] is True


# ---------------------------------------------------------------------------
# Probe 20 — defensive background error site + double-close determinism
# ---------------------------------------------------------------------------
async def test_probe_background_defensive_error_and_double_close() -> None:
    parent = _parent("bg-err")
    prepared = await _prepare_bg(parent, _sub_session(), "bg-err-child")
    notified: list[tuple[str, Any]] = []

    async def fake_notify(self: Any, session_id: str, result: Any) -> None:
        notified.append((session_id, result))

    with (
        patch.object(Agent, "_execute", side_effect=RuntimeError("kaboom")),
        patch.object(Agent, "_notify_parent_background_finished", fake_notify),
    ):
        await Agent(parent)._run_background(prepared)

    assert len(notified) == 1
    session_id, result = notified[0]
    assert session_id == "bg-err-child"
    assert result.is_error is True
    assert result.message == "kaboom"
    assert result.brief == "Background subagent failed"

    # interrupt_agent: unknown id and re-close are deterministic ToolErrors.
    lister = _parent("closer")
    unknown = await AgentClose(lister)(AgentCloseParams(agent_id="ghost"))
    assert unknown.is_error is True
    assert unknown.message == "Session not found"
    assert unknown.brief == "Session not found"

    _get_store(lister).put(_entry(_sub_session(), "doomed"))
    with patch("kimix.utils.close_session_async", new_callable=AsyncMock) as mock_close:
        first = await AgentClose(lister)(AgentCloseParams(agent_id="doomed"))
        second = await AgentClose(lister)(AgentCloseParams(agent_id="doomed"))
    assert first.is_error is False
    assert first.output == "Session doomed closed."
    assert mock_close.await_count == 1
    assert second.is_error is True
    assert second.message == "Session not found"
