"""Multi-dispatch tests for the ``subagent`` tool (R1).

The ``subagent`` tool accepts ``prompt`` as ``str | list[str]`` and
``session_id`` as ``str | list[str | None] | None``.  A list prompt dispatches
N subagents in one call.  These probes cover:

* param validation (count match, bare-string rejection, duplicate ids, schema
  key set after removing ``description``/``context_files``/``context_data``);
* batch background dispatch (N ids, N store entries, N detached tasks);
* batch foreground dispatch (combined output, per-run output files);
* partial / all-fail foreground aggregation;
* mixed resume (existing id + fresh) in one batch;
* backwards compatibility of the single-string path (singular ``session_id``);
* preparation-phase failure (``@missing.md``) aborting before any launch.

Style mirrors ``test_agent_gate.py``: patch ``kimix.utils._create_session_async``
/ ``prompt_async`` / ``close_session_async`` and drive ``Agent`` directly.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kaos.path import KaosPath
from pydantic import ValidationError

import kimi_cli.tools.subagent as agent_mod
from kimix.base import MessageType
from kimi_cli.tools.subagent import (
    Agent,
    SubAgentParams,
    _background_tasks_for,
    _get_store,
)


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


async def _pump(n: int = 60) -> None:
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
    def _reset() -> None:
        agent_mod._agent_entries.clear()
        agent_mod._agent_sessions.clear()
        agent_mod._children_by_parent.clear()
        agent_mod._child_parent.clear()

    _reset()
    yield
    _reset()


# ---------------------------------------------------------------------------
# 1. Param validation
# ---------------------------------------------------------------------------
async def test_param_single_string_still_valid() -> None:
    p = SubAgentParams(prompt="x")
    assert p.prompt == "x"
    assert p.session_id is None
    # ``task`` alias still resolves to ``prompt``.
    assert SubAgentParams(**{"task": "y"}).prompt == "y"


async def test_param_list_prompt_and_session_id_rules() -> None:
    # None session_id -> all fresh.
    assert SubAgentParams(prompt=["a", "b"]).session_id is None
    # Matching-length id list (mix of resume + None) is valid.
    p = SubAgentParams(prompt=["a", "b"], session_id=["s1", None])
    assert p.session_id == ["s1", None]
    # One-element list allowed; behaves like batch-of-1.
    assert SubAgentParams(prompt=["a"], session_id=[None]).prompt == ["a"]


async def test_param_empty_list_prompt_raises() -> None:
    with pytest.raises(ValidationError, match="must not be empty"):
        SubAgentParams(prompt=[])


async def test_param_mismatched_count_raises() -> None:
    with pytest.raises(ValidationError, match="count"):
        SubAgentParams(prompt=["a", "b"], session_id=["only"])
    with pytest.raises(ValidationError):
        SubAgentParams(prompt=["a", "b"], session_id=["s1", "s2", "s3"])


async def test_param_bare_string_session_with_list_raises() -> None:
    with pytest.raises(ValidationError, match="ambiguous"):
        SubAgentParams(prompt=["a", "b"], session_id="s1")


async def test_param_duplicate_ids_raise() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        SubAgentParams(prompt=["a", "b"], session_id=["dup", "dup"])


async def test_param_list_session_with_string_prompt_raises() -> None:
    with pytest.raises(ValidationError, match="not a list"):
        SubAgentParams(prompt="single", session_id=["s1", None])


async def test_param_str_stays_str_in_union() -> None:
    # A JSON string that is just text must never coerce to a list.
    assert SubAgentParams.model_validate_json('{"prompt": "x"}').prompt == "x"


async def test_param_dump_key_set_no_removed_fields() -> None:
    keys = set(SubAgentParams(prompt="x").model_dump())
    assert keys == {
        "prompt",
        "run_in_background",
        "session_id",
        "close_session",
        "return_history",
        "history_format",
        "inherit_context",
    }
    assert "description" not in keys
    assert "context_files" not in keys
    assert "context_data" not in keys


async def test_split_params_shapes() -> None:
    # Single string -> one run, singular session_id preserved.
    runs = SubAgentParams(prompt="x", session_id="s1")._split_params()
    assert len(runs) == 1
    assert runs[0].prompt == "x"
    assert runs[0].session_id == "s1"
    # List -> N runs sharing the scalar options.
    runs = SubAgentParams(
        prompt=["a", "b", "c"],
        session_id=[None, "s2", None],
        run_in_background=False,
        close_session=False,
        return_history=True,
        history_format="markdown",
        inherit_context=True,
    )._split_params()
    assert [r.prompt for r in runs] == ["a", "b", "c"]
    assert [r.session_id for r in runs] == [None, "s2", None]
    assert all(r.run_in_background is False for r in runs)
    assert all(r.close_session is False for r in runs)
    assert all(r.return_history is True for r in runs)
    assert all(r.history_format == "markdown" for r in runs)
    assert all(r.inherit_context is True for r in runs)


# ---------------------------------------------------------------------------
# 2. Batch background dispatch
# ---------------------------------------------------------------------------
async def test_batch_background_returns_ids_and_launches_tasks() -> None:
    parent = _parent("bg-parent")
    with (
        patch("kimix.utils._create_session_async", new_callable=AsyncMock) as mc,
        patch("kimix.utils.prompt_async", new_callable=AsyncMock) as mp,
        patch("kimix.utils.close_session_async", new_callable=AsyncMock),
    ):
        mc.side_effect = lambda **kw: _sub_session(kw["session_id"])
        result = await Agent(parent)(SubAgentParams(prompt=["a", "b", "c"]))
        session_ids = result.extras["session_ids"]
        assert result.is_error is False
        assert len(session_ids) == 3
        assert len(set(session_ids)) == 3  # distinct
        # Each id is listed on its own line in the output.
        for sid in session_ids:
            assert f"Session ID: {sid}" in result.output
        assert result.extras["status"] == "running"
        # Singular ``session_id`` must not be present in batch mode.
        assert "session_id" not in result.extras
        # Returned before any child ran.
        mp.assert_not_awaited()
        # N detached tasks tracked for the parent.
        assert len([t for t in _background_tasks_for(parent)]) == 3
        # N store entries.
        store = _get_store(parent)
        for sid in session_ids:
            assert store.get(sid) is not None
        await _pump()

    for task in list(_background_tasks_for(parent)):
        task.cancel()
    await _pump()


# ---------------------------------------------------------------------------
# 3. Batch foreground dispatch
# ---------------------------------------------------------------------------
async def test_batch_foreground_combines_outputs(tmp_path: Path) -> None:
    parent = _parent("fg-parent", work_dir=KaosPath(str(tmp_path)))
    captured: list[str] = []
    with (
        patch("kimix.utils._create_session_async", new_callable=AsyncMock) as mc,
        patch("kimix.utils.prompt_async", side_effect=_capturing_prompt(captured)),
        patch("kimix.utils.close_session_async", new_callable=AsyncMock),
    ):
        mc.side_effect = lambda **kw: _sub_session(kw["session_id"])
        result = await Agent(parent)(
            SubAgentParams(prompt=["one", "two", "three"], run_in_background=False)
        )
    assert result.is_error is False
    assert result.extras["status"] == "completed"
    ids = result.extras["session_ids"]
    assert len(ids) == 3
    # All three prompts reached the child sessions.
    assert set(captured) == {"one", "two", "three"}
    # Combined output carries a Session ID line for every run.
    for sid in ids:
        assert f"Session ID: {sid}" in result.output
    # Per-run output files exist.
    files = result.extras["output_files"]
    assert len(files) == 3
    for f in files:
        assert (tmp_path / Path(f)).exists()


# ---------------------------------------------------------------------------
# 4. Partial failure (foreground)
# ---------------------------------------------------------------------------
async def test_batch_foreground_partial_failure(tmp_path: Path) -> None:
    parent = _parent("partial-parent", work_dir=KaosPath(str(tmp_path)))

    async def flaky(*, prompt_str: str, output_function: Any = None, **kwargs: Any) -> None:
        if prompt_str == "boom":
            raise RuntimeError("child exploded")
        if output_function is not None:
            output_function("ok", MessageType.Text)

    with (
        patch("kimix.utils._create_session_async", new_callable=AsyncMock) as mc,
        patch("kimix.utils.prompt_async", side_effect=flaky),
        patch("kimix.utils.close_session_async", new_callable=AsyncMock),
    ):
        mc.side_effect = lambda **kw: _sub_session(kw["session_id"])
        result = await Agent(parent)(
            SubAgentParams(prompt=["fine", "boom", "also"], run_in_background=False)
        )

    assert result.is_error is False  # partial success still a ToolOk
    assert result.extras["status"] == "partial"
    ids = result.extras["session_ids"]
    assert len(ids) == 3
    failures = result.extras["failures"]
    assert len(failures) == 1
    assert failures[0]["index"] == 1
    assert failures[0]["session_id"] == ids[1]
    assert "child exploded" in failures[0]["message"]
    # The successful ids are still returned (not lost).
    assert ids[0] in result.output and ids[2] in result.output


# ---------------------------------------------------------------------------
# 5. All-fail (foreground) -> ToolError keeps all ids
# ---------------------------------------------------------------------------
async def test_batch_foreground_all_fail_is_error(tmp_path: Path) -> None:
    parent = _parent("allfail-parent", work_dir=KaosPath(str(tmp_path)))

    async def always_raises(*, prompt_str: str, **kwargs: Any) -> None:
        raise RuntimeError(f"nope:{prompt_str}")

    with (
        patch("kimix.utils._create_session_async", new_callable=AsyncMock) as mc,
        patch("kimix.utils.prompt_async", side_effect=always_raises),
        patch("kimix.utils.close_session_async", new_callable=AsyncMock),
    ):
        mc.side_effect = lambda **kw: _sub_session(kw["session_id"])
        result = await Agent(parent)(
            SubAgentParams(prompt=["a", "b"], run_in_background=False)
        )

    assert result.is_error is True
    assert len(result.extras["session_ids"]) == 2
    assert "nope:a" in result.message and "nope:b" in result.message


# ---------------------------------------------------------------------------
# 6. Mixed resume in one batch
# ---------------------------------------------------------------------------
async def test_batch_mixed_resume_reuses_existing_session() -> None:
    parent = _parent("mix-parent")
    existing = _sub_session("keep-me")
    existing._closed = False  # MagicMock attributes are truthy by default
    _get_store(parent).put(
        agent_mod.AgentSessionEntry(
            session=existing,
            session_id="keep-me",
            created_at=time.time(),
            last_accessed=time.time(),
            conversation_history=[],
            total_turns=1,
            is_active=True,
            state="completed",
        )
    )
    captured: list[str] = []
    with (
        patch("kimix.utils._create_session_async", new_callable=AsyncMock) as mc,
        patch("kimix.utils.prompt_async", side_effect=_capturing_prompt(captured)),
        patch("kimix.utils.close_session_async", new_callable=AsyncMock),
    ):
        mc.side_effect = lambda **kw: _sub_session(kw["session_id"])
        result = await Agent(parent)(
            SubAgentParams(
                prompt=["resume it", "fresh one"],
                session_id=["keep-me", None],
                run_in_background=False,
            )
        )

    assert result.is_error is False
    ids = result.extras["session_ids"]
    assert ids[0] == "keep-me"  # resumed
    assert ids[1] != "keep-me"  # fresh uuid
    # Only the fresh run created a session; the resumed id was reused.
    created_ids = [c.kwargs["session_id"] for c in mc.await_args_list]
    assert "keep-me" not in created_ids
    assert ids[1] in created_ids


# ---------------------------------------------------------------------------
# 7. Compat: single string unchanged
# ---------------------------------------------------------------------------
async def test_single_string_extras_unchanged() -> None:
    parent = _parent("compat-parent")
    with (
        patch("kimix.utils._create_session_async", new_callable=AsyncMock) as mc,
        patch("kimix.utils.prompt_async", side_effect=_capturing_prompt([])),
        patch("kimix.utils.close_session_async", new_callable=AsyncMock),
    ):
        mc.return_value = _sub_session()
        result = await Agent(parent)(SubAgentParams(prompt="solo"))
    assert result.is_error is False
    # Singular ``session_id`` key retained; ``session_ids`` NOT present.
    assert "session_id" in result.extras
    assert "session_ids" not in result.extras
    assert result.extras["status"] == "running"
    assert f"Session ID: {result.extras['session_id']}" in result.output
    await _pump()


# ---------------------------------------------------------------------------
# 8. Prep-phase failure aborts the whole batch before any launch
# ---------------------------------------------------------------------------
async def test_batch_prep_failure_launches_nothing(
    monkeypatch, tmp_path: Path
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "here.md").write_text("present", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    parent = _parent("abort-parent", work_dir=KaosPath(str(work)))

    created: list[str] = []
    closed: list[Any] = []

    async def fake_create(**kw: Any) -> MagicMock:
        created.append(kw["session_id"])
        return _sub_session(kw["session_id"])

    async def fake_close(session: Any) -> None:
        closed.append(session)

    with (
        patch("kimix.utils._create_session_async", side_effect=fake_create),
        patch("kimix.utils.prompt_async", new_callable=AsyncMock) as mp,
        patch("kimix.utils.close_session_async", side_effect=fake_close),
    ):
        result = await Agent(parent)(
            SubAgentParams(prompt=["@here.md", "@missing.md"])
        )

    assert result.is_error is True
    assert result.brief == "Failed to create sub-agent session"
    # The offending prompt index is named.
    assert "prompt 1" in result.message
    # Nothing launched: no detached tasks and no prompt ran.
    mp.assert_not_awaited()
    assert len([t for t in _background_tasks_for(parent) if not t.done()]) == 0
    # The successful sibling's fresh session was rolled back, leaving no live
    # or resumable sub-agent behind.
    assert len(created) == 1
    assert len(closed) == 1
    assert _get_store(parent).get(created[0]) is None
    assert created[0] not in agent_mod._agent_sessions
    assert created[0] not in agent_mod._agent_entries
