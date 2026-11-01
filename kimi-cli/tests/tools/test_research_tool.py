"""Focused probes for the ``research`` tool (no real LLM).

The tool spawns a fresh, anonymous, read-only ``researcher`` child session
(``agent_readonly.json`` + ``SystemPromptType.Reader``), returns the child's
final assistant text *directly* (unlike ``subagent``, which returns only a
file path), always closes the child, and — with ``run_in_background=true`` —
registers a generic ``BackgroundStream`` under a per-session ``research_<n>``
task id so the stock ``job_output`` tool can wait for, read, and kill it.

Session creation and prompting are monkeypatched at the ``kimix.utils``
module attributes (the tool imports them lazily, so patching the module
attribute is exactly what a call observes).
"""

from __future__ import annotations

import asyncio
import importlib
import threading
from types import SimpleNamespace

import pytest

from kaos.path import KaosPath
from kimi_cli.tools import subagent as sa
from kimi_cli.tools.background import TaskOutput, TaskOutputParams
from kimi_cli.tools.background.utils import (
    get_all_tasks,
    get_finished_task,
)
from kimi_cli.tools.research import Research, ResearchParams

MessageType = importlib.import_module("kimix.ui.printing").MessageType
kimix_utils = importlib.import_module("kimix.utils")
kimix_base = importlib.import_module("kimix.base")


class _FakeSession:
    """Minimal stand-in for the parent session (only what the tool touches)."""

    def __init__(self, work_dir, session_id: str = "parent-1") -> None:
        self.id = session_id
        self.work_dir = KaosPath(str(work_dir))
        self.custom_data: dict = {}
        self.custom_config: dict = {}


def _make_tool(tmp_path, *, config: dict | None = None) -> Research:
    session = _FakeSession(tmp_path)
    if config:
        session.custom_config.update(config)
    return Research(session)


def _patch_create_session(monkeypatch, *, children: list | None = None, created: list | None = None):
    """Patch ``kimix.utils._create_session_async``; record the call kwargs."""

    calls: list[dict] = []

    async def fake_create_session_async(**kwargs):
        calls.append(kwargs)
        child = SimpleNamespace(id=f"child-{len(calls)}", _closed=False)
        if children is not None:
            children.append(child)
        if created is not None:
            created.append(child)
        return child

    monkeypatch.setattr(kimix_utils, "_create_session_async", fake_create_session_async)
    return calls


def _patch_prompt(monkeypatch, *, text: str | None = None, error: str | None = None,
                  block: threading.Event | None = None):
    """Patch ``kimix.utils.prompt_async``; record closed sessions."""

    seen: list[dict] = []

    async def fake_prompt_async(prompt_str, session, output_function=None, **kwargs):
        seen.append({"prompt": prompt_str, "child": session, "kwargs": kwargs})
        if block is not None:
            block.set()
            await asyncio.Event().wait()  # until cancelled
        if error is not None:
            raise RuntimeError(error)
        if output_function is not None and text:
            output_function(text, MessageType.Text)
        elif output_function is not None:
            output_function("chunk-1", MessageType.Text)
            output_function("chunk-2", MessageType.Text)

    monkeypatch.setattr(kimix_utils, "prompt_async", fake_prompt_async)
    return seen


def _record_closed(monkeypatch) -> list:
    closed: list = []

    async def fake_close_session_async(session):
        closed.append(session)
        session._closed = True

    monkeypatch.setattr(kimix_utils, "close_session_async", fake_close_session_async)
    return closed


# --------------------------------------------------------------------------- #
# Foreground: child configuration and direct result
# --------------------------------------------------------------------------- #

def test_description_covers_deep_research_and_other_readonly_tasks():
    assert Research.description == (
        "Use for deep research, in-depth investigation, or any other read-only task."
    )


async def test_foreground_creates_readonly_child_and_returns_text_directly(tmp_path, monkeypatch):
    from kimix.utils.system_prompt import SystemPromptType

    tool = _make_tool(tmp_path)
    calls = _patch_create_session(monkeypatch)
    closed = _record_closed(monkeypatch)
    prompts = _patch_prompt(monkeypatch, text="the research findings")

    result = await tool(ResearchParams(query="what is the answer?"))

    assert not result.is_error
    # The research text itself is returned — not an output-file path.
    assert result.output == "the research findings"
    assert "temp_" not in result.output

    assert len(calls) == 1
    cfg = calls[0]
    # Fresh, anonymous, read-only researcher session via the phase-1 mapping.
    assert cfg["agent_file"] == kimix_base._default_agent_file_dir / "agent_readonly.json"
    assert cfg["agent_type"] is SystemPromptType.Reader
    assert cfg["anonymous"] is True
    assert cfg["resume"] is True
    # Inherits the parent work dir; no chat_provider configured on the parent.
    assert cfg["work_dir"] == tool._session.work_dir
    assert cfg["chat_provider"] is None
    # Provider fallback: no researcher sub-provider -> default provider dict.
    assert cfg["provider_dict"] is kimix_base._default_provider

    assert len(prompts) == 1
    assert prompts[0]["prompt"] == "what is the answer?"
    assert prompts[0]["child"].id == "child-1"

    # The child session is closed and forgotten.
    assert len(closed) == 1
    assert closed[0].id == "child-1"


async def test_foreground_researcher_sub_provider_wins_and_chat_provider_inherited(tmp_path, monkeypatch):
    tool = _make_tool(tmp_path, config={
        "chat_provider": object(),
        "provider_dict": {"model": "parent-fallback"},
    })
    calls = _patch_create_session(monkeypatch)
    _record_closed(monkeypatch)
    _patch_prompt(monkeypatch, text="ok")
    researcher = {"role": "researcher", "model": "research-model"}
    monkeypatch.setattr(kimix_base, "get_default_sub_provider", lambda role: researcher)

    result = await tool(ResearchParams(query="deep dive"))

    assert not result.is_error
    assert calls[0]["provider_dict"] is researcher
    assert calls[0]["chat_provider"] is tool._session.custom_config["chat_provider"]


async def test_foreground_empty_query_is_rejected(tmp_path):
    tool = _make_tool(tmp_path)
    result = await tool(ResearchParams(query="   "))
    assert result.is_error
    assert "query" in result.message


# --------------------------------------------------------------------------- #
# Foreground: cleanup on error
# --------------------------------------------------------------------------- #

async def test_foreground_prompt_failure_closes_child_and_cleans_bookkeeping(tmp_path, monkeypatch):
    tool = _make_tool(tmp_path)
    _patch_create_session(monkeypatch)
    closed = _record_closed(monkeypatch)
    _patch_prompt(monkeypatch, error="model exploded")

    result = await tool(ResearchParams(query="doomed question"))

    assert result.is_error
    assert "model exploded" in result.message
    assert result.message.strip()
    # The failure carries whatever the child produced (the "(no text output)"
    # placeholder here) so no result text is silently lost.
    assert result.output == "(no text output)"
    # Cleanup still ran: child closed, no bookkeeping left behind.
    assert len(closed) == 1
    assert "child-1" not in sa._agent_sessions
    assert "child-1" not in sa._agent_entries
    assert "child-1" not in sa._child_parent
    assert "child-1" not in sa._children_by_parent.get("parent-1", set())


async def test_foreground_session_creation_failure_is_tool_error(tmp_path, monkeypatch):
    tool = _make_tool(tmp_path)

    async def boom(**kwargs):
        raise RuntimeError("no provider configured")

    monkeypatch.setattr(kimix_utils, "_create_session_async", boom)
    result = await tool(ResearchParams(query="anything"))
    assert result.is_error
    assert "no provider configured" in result.message


# --------------------------------------------------------------------------- #
# Background: ids, job_output wait/result/history
# --------------------------------------------------------------------------- #

async def test_background_ids_increment_per_session_and_wait_returns_text(tmp_path, monkeypatch):
    tool = _make_tool(tmp_path)
    children = []
    _patch_create_session(monkeypatch, children=children)
    closed = _record_closed(monkeypatch)
    _patch_prompt(monkeypatch, text="background findings")

    first = await tool(ResearchParams(query="q1", run_in_background=True))
    second = await tool(ResearchParams(query="q2", run_in_background=True))

    assert not first.is_error and not second.is_error
    assert "task_id: research_1" in first.output
    assert "task_id: research_2" in second.output
    # Both are registered in the same per-session infrastructure as bash/pwsh.
    assert set(get_all_tasks(tool._session)) == {"research_1", "research_2"}

    reader = TaskOutput(tool._session)
    got = await reader(TaskOutputParams(job_id="research_1", wait=True, timeout=30))
    assert not got.is_error
    assert got.output == "background findings"

    # The finished task stays retrievable from the bounded history.
    again = await reader(TaskOutputParams(job_id="research_1"))
    assert not again.is_error
    assert "background findings" in again.output
    assert "[retrieved from finished-task history]" in again.output

    record = get_finished_task(tool._session, "research_1")
    assert record is not None
    assert record.success
    assert record.output == "background findings"

    # The child behind research_1 was closed and forgotten.
    assert children[0] in closed
    assert "child-1" not in sa._agent_sessions
    # research_2 is still registered (not yet read to completion).
    assert "research_2" in get_all_tasks(tool._session)
    await reader(TaskOutputParams(job_id="research_2", wait=True, timeout=30))
    assert children[1] in closed


async def test_background_failure_is_visible_in_job_output(tmp_path, monkeypatch):
    tool = _make_tool(tmp_path)
    _patch_create_session(monkeypatch)
    _record_closed(monkeypatch)
    _patch_prompt(monkeypatch, error="quota exceeded")

    started = await tool(ResearchParams(query="doomed", run_in_background=True))
    task_id = started.output.split("task_id: ", 1)[1].splitlines()[0].strip()

    reader = TaskOutput(tool._session)
    got = await reader(TaskOutputParams(job_id=task_id, wait=True, timeout=30))
    assert got.is_error, "a failed research must surface as a job_output error"
    assert "quota exceeded" in got.message
    record = get_finished_task(tool._session, task_id)
    assert record is not None
    assert not record.success
    # The error text is preserved in the record, not lost.
    assert "quota exceeded" in record.output


# --------------------------------------------------------------------------- #
# Background: kill / cancellation
# --------------------------------------------------------------------------- #

async def test_background_kill_cancels_child_and_cleans_up(tmp_path, monkeypatch):
    tool = _make_tool(tmp_path)
    _patch_create_session(monkeypatch)
    closed = _record_closed(monkeypatch)
    block = threading.Event()
    _patch_prompt(monkeypatch, block=block)

    started = await tool(ResearchParams(query="long research", run_in_background=True))
    task_id = started.output.split("task_id: ", 1)[1].splitlines()[0].strip()
    assert block.wait(timeout=10), "the background prompt never started"

    reader = TaskOutput(tool._session)
    killed = await reader(TaskOutputParams(job_id=task_id, action="kill"))
    assert not killed.is_error
    assert "killed" in killed.brief

    # The cancelled child was closed and its bookkeeping dropped: no leak.
    assert len(closed) == 1
    assert closed[0].id == "child-1"
    assert "child-1" not in sa._agent_sessions
    assert task_id not in get_all_tasks(tool._session)
    record = get_finished_task(tool._session, task_id)
    assert record is not None
    assert not record.success
    assert "cancelled" in record.output


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-x"]))
