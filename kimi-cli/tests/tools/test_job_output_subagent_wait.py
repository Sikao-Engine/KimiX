"""``job_output`` can block on a *background subagent* session id.

The ``subagent`` tool tells the parent agent, in its ``run_in_background``
result message, that it can ``job_output(job_id='<subagent id>', wait: true)``
to wait for the detached run.  These probes pin that promise: the id resolves
through the *parent* session's subagent store (the ``job_output`` tool is
constructed with the same session object), a non-``wait`` read never blocks,
a ``wait`` read blocks until the detached task settles, and the settled read
reports the run's recorded state plus the output file path — never a
"task not found" error.

Everything is driven through the real ``TaskOutput`` tool with a real
``asyncio.Task`` standing in for the detached subagent run, so the tests fail
if the subagent branch in ``TaskOutput._get_subagent_output`` is unreachable
or wired to a different registry.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib

import pytest

from kimi_cli.tools import subagent as sa
from kimi_cli.tools.background import TaskOutput, TaskOutputParams
from kimi_cli.tools.background.utils import discard_all_tasks
from kimi_cli.tools.subagent.store import AgentSessionEntry

MessageType = importlib.import_module("kimix.ui.printing").MessageType


class _FakeSession:
    """Minimal stand-in for a CLI session (only what the tools touch)."""

    def __init__(self, session_id: str = "parent-1") -> None:
        self.id = session_id
        self.custom_data: dict = {}
        self.custom_config: dict = {}


def _register_run(
    parent: _FakeSession, session_id: str, *, state: str = "running"
) -> AgentSessionEntry:
    """Put a subagent entry in *parent*'s store (as ``_start_background_task``)."""
    entry = AgentSessionEntry(
        session=object(),  # the tool never touches the child session object
        session_id=session_id,
        created_at=0.0,
        last_accessed=0.0,
        conversation_history=[],
        total_turns=0,
        is_active=True,
        state=state,  # type: ignore[arg-type]
    )
    sa._get_store(parent).put(entry)
    return entry


@pytest.fixture(autouse=True)
def _clean_registries():
    """Keep the module-level task registry from leaking between tests."""
    saved = dict(sa._background_run_tasks)
    sa._background_run_tasks.clear()
    try:
        yield
    finally:
        sa._background_run_tasks.clear()
        sa._background_run_tasks.update(saved)


async def _settle_soon(entry: AgentSessionEntry, delay: float = 0.05) -> None:
    """Stand-in for ``_execute``: flip the entry to completed, then return."""
    await asyncio.sleep(delay)
    entry.state = "completed"


def _start_run(parent: _FakeSession, session_id: str, delay: float = 0.05) -> asyncio.Task:
    entry = _register_run(parent, session_id)
    task = asyncio.create_task(_settle_soon(entry, delay))
    sa._background_run_tasks[session_id] = task

    def _drop(_t: asyncio.Task, sid: str = session_id) -> None:
        sa._background_run_tasks.pop(sid, None)

    task.add_done_callback(_drop)
    return task


# --------------------------------------------------------------------------- #
# The message the subagent tool returns
# --------------------------------------------------------------------------- #


def test_background_message_points_at_job_output() -> None:
    """The background dispatch message documents the job_output wait-idiom."""
    output = sa._background_dispatch_output(["child-1"])

    assert "Session ID: child-1" in output
    assert "job_output(job_id='<session id>', wait: true" in output
    assert "list_agents" in output
    assert "interrupt_agent" in output


def test_background_message_states_the_unbounded_wait_contract() -> None:
    """A subagent wait ignores ``timeout`` and only ends when the run settles."""
    output = sa._background_dispatch_output(["child-1"])

    assert "timeout" in output
    assert "unbounded" in output
    assert "crashes" in output, "the model must know how the wait can end"
    # A subagent wait can no longer time out, so the old resumable-wait
    # hint ("[status: running] ... call it again") must be gone.
    assert "[status: running]" not in output


def test_batch_background_message_lists_every_id() -> None:
    """The single and batch paths share one promise (one line per id)."""
    output = sa._background_dispatch_output(["child-1", "child-2"])

    assert "Session ID: child-1" in output
    assert "Session ID: child-2" in output
    assert "job_output(job_id='<session id>', wait: true" in output


async def test_launch_background_message_uses_the_shared_helper() -> None:
    """``_launch_background``'s ToolOk carries the shared dispatch text."""
    from kimi_cli.tools.subagent import Agent

    tool = Agent(_FakeSession())  # type: ignore[arg-type]

    class _Prepared:
        session_id = "child-1"
        params = sa.SubAgentParams(prompt="hi", run_in_background=True)

    async def _fake_start(_prepared):
        return AgentSessionEntry(
            session=object(),
            session_id="child-1",
            created_at=0.0,
            last_accessed=0.0,
            conversation_history=[],
            total_turns=0,
        )

    original = tool._start_background_task
    tool._start_background_task = _fake_start  # type: ignore[method-assign]
    try:
        result = await tool._launch_background(_Prepared())  # type: ignore[arg-type]
    finally:
        tool._start_background_task = original  # type: ignore[method-assign]

    assert result.output == sa._background_dispatch_output(["child-1"])
    assert result.extras["status"] == "running"
    assert result.extras["session_id"] == "child-1"


# --------------------------------------------------------------------------- #
# TaskOutput._get_subagent_output
# --------------------------------------------------------------------------- #


async def test_job_output_list_does_not_list_subagents() -> None:
    """`action='list'` covers shell jobs only; subagents are id-addressable."""
    parent = _FakeSession()
    _register_run(parent, "child-1")
    tool = TaskOutput(parent)  # type: ignore[arg-type]

    result = await tool(TaskOutputParams(action="list"))

    assert not result.is_error
    assert "No running tasks." in str(result.output)


async def test_job_output_without_wait_reports_running_without_blocking() -> None:
    """A plain read is non-blocking and tells the model how to wait."""
    parent = _FakeSession()
    _start_run(parent, "child-1", delay=30.0)
    tool = TaskOutput(parent)  # type: ignore[arg-type]

    result = await asyncio.wait_for(
        tool(TaskOutputParams(job_id="child-1")), timeout=2.0
    )

    assert not result.is_error
    assert "still running" in str(result.output)
    assert "wait: true" in str(result.output)


async def test_job_output_wait_blocks_until_the_subagent_settles() -> None:
    """`wait: true` returns only after the detached run leaves ``running``."""
    parent = _FakeSession()
    _start_run(parent, "child-1", delay=0.05)
    tool = TaskOutput(parent)  # type: ignore[arg-type]

    result = await tool(TaskOutputParams(job_id="child-1", wait=True, timeout=10))

    assert not result.is_error
    output = str(result.output)
    assert "finished (status: completed)" in output
    assert "still running" not in output
    # The extras a caller programmatically consumes.
    assert result.extras["session_id"] == "child-1"
    assert result.extras["status"] == "completed"


async def test_job_output_wait_ignores_the_timeout_for_subagents() -> None:
    """A subagent wait blocks past ``timeout`` until the run settles.

    The run settles *after* ``timeout`` seconds: the old bounded behaviour
    would have returned ``[status: running]`` at the 1 s mark.  The new
    contract ignores ``timeout`` and only returns once the run finished.
    """
    parent = _FakeSession()
    task = _start_run(parent, "child-1", delay=2.0)
    tool = TaskOutput(parent)  # type: ignore[arg-type]

    result = await tool(TaskOutputParams(job_id="child-1", wait=True, timeout=1))

    assert task.done(), "the wait must last until the run settles, past timeout"
    assert not result.is_error
    assert "[status: running]" not in str(result.output)
    assert "finished (status: completed)" in str(result.output)


def test_job_output_timeout_schema_cap_is_7200() -> None:
    """``job_output`` accepts up to 7200 s and rejects anything above it."""
    from pydantic import ValidationError

    from kimi_cli.tools.background import MAX_JOB_OUTPUT_TIMEOUT_SECONDS

    assert MAX_JOB_OUTPUT_TIMEOUT_SECONDS == 7200
    params = TaskOutputParams(job_id="child-1", wait=True, timeout=7200)
    assert params.timeout == 7200
    with pytest.raises(ValidationError):
        TaskOutputParams(job_id="child-1", wait=True, timeout=7201)


async def test_job_output_wait_reports_the_output_file_when_it_exists(
    tmp_path, monkeypatch
) -> None:
    """A settled run reports its output path so the parent can read the report."""
    monkeypatch.chdir(tmp_path)
    parent = _FakeSession()
    _start_run(parent, "child-1", delay=0.02)
    saved = sa._write_subagent_output(parent, "child-1", "the report")
    assert saved
    tool = TaskOutput(parent)  # type: ignore[arg-type]

    result = await tool(TaskOutputParams(job_id="child-1", wait=True, timeout=10))

    assert not result.is_error
    assert f"Output: {saved}" in str(result.output)
    assert result.extras["output_file"] == saved


async def test_job_output_unknown_id_is_a_loud_error_not_a_wait() -> None:
    """An unknown id falls through to a named not-found error."""
    parent = _FakeSession()
    tool = TaskOutput(parent)  # type: ignore[arg-type]

    result = await tool(TaskOutputParams(job_id="not-a-session", wait=True, timeout=1))

    assert result.is_error
    assert "not found" in str(result.message)
    assert "not-a-session" in str(result.message)
    assert "list_agents" in str(result.message)


async def test_job_output_cannot_wait_on_another_sessions_subagent() -> None:
    """The store is per parent session: a foreign id is not waitable."""
    owner = _FakeSession("owner")
    _start_run(owner, "child-1", delay=0.02)
    intruder = _FakeSession("intruder")
    tool = TaskOutput(intruder)  # type: ignore[arg-type]

    result = await tool(TaskOutputParams(job_id="child-1", wait=True, timeout=1))

    assert result.is_error
    assert "not found" in str(result.message)


async def test_job_output_serves_a_settled_run_after_the_task_is_gone() -> None:
    """The finished run stays answerable once the detached task was discarded."""
    parent = _FakeSession()
    _start_run(parent, "child-1", delay=0.02)
    tool = TaskOutput(parent)  # type: ignore[arg-type]
    await tool(TaskOutputParams(job_id="child-1", wait=True, timeout=10))
    assert "child-1" not in sa._background_run_tasks

    # A later read must still resolve the id (no task, completed entry).
    result = await tool(TaskOutputParams(job_id="child-1"))

    assert not result.is_error
    assert "finished (status: completed)" in str(result.output)


async def test_job_output_reports_a_run_whose_session_was_closed() -> None:
    """An interrupted run (tombstoned entry) reads as closed, not as running."""
    parent = _FakeSession()
    task = _start_run(parent, "child-1", delay=30.0)
    store = sa._get_store(parent)
    store.close("child-1")
    tool = TaskOutput(parent)  # type: ignore[arg-type]

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    result = await tool(TaskOutputParams(job_id="child-1", wait=True, timeout=5))

    assert not result.is_error
    assert "finished (status: closed)" in str(result.output)


async def test_job_output_survives_a_running_entry_with_no_waitable_task() -> None:
    """A ``running`` entry with no tracked task is reported, not faked as done."""
    parent = _FakeSession()
    _register_run(parent, "child-1", state="running")
    tool = TaskOutput(parent)  # type: ignore[arg-type]

    result = await tool(TaskOutputParams(job_id="child-1", wait=True, timeout=1))

    assert not result.is_error
    assert "no waitable background task" in str(result.output)
    assert "list_agents" in str(result.output)


async def test_discard_all_tasks_does_not_disturb_the_subagent_store() -> None:
    """The shell-job registry and the subagent store are independent."""
    parent = _FakeSession()
    _register_run(parent, "child-1")

    await discard_all_tasks(parent)  # type: ignore[arg-type]

    assert sa._get_store(parent).get("child-1") is not None


# --------------------------------------------------------------------------- #
# End to end: real Agent._start_background_task -> real TaskOutput
# --------------------------------------------------------------------------- #


async def test_real_background_launch_is_waitable_through_job_output(
    tmp_path, monkeypatch
) -> None:
    """The production dispatch path registers a run ``job_output`` can block on.

    No stand-in task: ``Agent._launch_background`` (the code the ``subagent``
    tool runs) spawns the real detached task, and ``TaskOutput`` is then asked
    to wait on the id it returned.  This is the wiring the tool's message
    promises; if the two registries ever drift apart, this fails.
    """
    monkeypatch.chdir(tmp_path)
    utils = importlib.import_module("kimix.utils")

    async def _fake_prompt_async(prompt_str, session, output_function=None, **kwargs):
        await asyncio.sleep(0.05)
        if output_function is not None:
            output_function("the report", MessageType.Text)

    async def _fake_close_session(session):
        return None

    monkeypatch.setattr(utils, "prompt_async", _fake_prompt_async)
    monkeypatch.setattr(utils, "close_session_async", _fake_close_session)
    # Steer delivery needs a parent SDK session; absent here, it is a no-op.
    monkeypatch.setattr(sa, "_get_agent_session", lambda _sid: None)
    monkeypatch.setattr(sa, "_sdk_session_by_id", lambda _sid: None)

    parent = _FakeSession()
    agent = sa.Agent(parent)  # type: ignore[arg-type]
    child = _FakeSession("child-1")
    prepared = sa._PreparedRun(
        params=sa.SubAgentParams(
            prompt="do it", run_in_background=True, close_session=False
        ),
        session=child,
        session_id="child-1",
        is_reused=False,
        prompt="do it",
        background=True,
    )
    agent._register_agent_sessions = lambda *_a, **_k: None  # type: ignore[method-assign]

    launched = await agent._launch_background(prepared)
    assert not launched.is_error
    assert launched.extras["status"] == "running"

    # ``wait: true`` must block until the detached run actually settles.
    waited = await TaskOutput(parent)(  # type: ignore[arg-type]
        TaskOutputParams(job_id="child-1", wait=True, timeout=30)
    )

    assert not waited.is_error
    assert "finished (status: completed)" in str(waited.output)
    assert waited.extras["status"] == "completed"
    assert (tmp_path / ".kimix_cache/temp_child-1.md").read_text(
        encoding="utf-8"
    ) == "the report"