"""The ``subagent`` tool always saves a run's result to ``.kimix_cache``.

The parent agent is handed the *path* (``.kimix_cache/temp_<session id>.md``)
instead of the full text, so a long sub-agent report is read on demand and
never floods the parent's context.  These probes pin the file location (one
file per sub-agent session, path-hostile ids flattened), the save fallbacks
(the shared temp folder, then a pathless failure notice) and — importantly —
that the tool result never echoes the sub-agent's own text back to the parent.
"""

from __future__ import annotations

import importlib
from pathlib import Path

from kaos.path import KaosPath
from kimi_cli.tools import subagent as sa
from kimi_cli.tools.subagent import Agent, SubAgentParams, _PreparedRun

MessageType = importlib.import_module("kimix.ui.printing").MessageType


class _FakeSession:
    """Minimal stand-in for an SDK session (only what the tool touches)."""

    def __init__(self, work_dir: Path, session_id: str = "parent-1") -> None:
        self.id = session_id
        self.work_dir = KaosPath(str(work_dir))
        self.custom_data: dict = {}
        self.custom_config: dict = {}


def _patch_utils(monkeypatch, *, text: str | None = None, error: str | None = None):
    """Patch ``kimix.utils`` prompt/close entry points; record closed sessions."""
    utils = importlib.import_module("kimix.utils")
    closed: list = []

    async def fake_prompt_async(prompt_str, session, output_function=None, **kwargs):
        if error is not None:
            raise RuntimeError(error)
        if output_function is not None and text:
            output_function(text, MessageType.Text)

    async def fake_close_session(session):
        closed.append(session)

    monkeypatch.setattr(utils, "prompt_async", fake_prompt_async)
    monkeypatch.setattr(utils, "close_session_async", fake_close_session)
    return closed


def _prepared(session, params: SubAgentParams, prompt: str) -> _PreparedRun:
    return _PreparedRun(
        params=params,
        session=session,
        session_id="child-1",
        is_reused=False,
        prompt=prompt,
        # A real _prepare_run reserves the session; here the run is driven
        # directly, so any token will do (releasing it is a no-op).
        claim=object(),
    )


# --------------------------------------------------------------------------- #
# _write_subagent_output / _display_output_path
# --------------------------------------------------------------------------- #


def test_write_subagent_output_uses_cache_dir_and_session_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    session = _FakeSession(tmp_path)

    saved = sa._write_subagent_output(session, "child-1", "the final report")

    assert saved == ".kimix_cache/temp_child-1.md"
    assert (tmp_path / Path(saved)).read_text(encoding="utf-8") == "the final report"
    # The session cache root holds the file, not a sibling directory.
    assert (tmp_path / ".kimix_cache").is_dir()


def test_write_subagent_output_is_one_file_per_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    session = _FakeSession(tmp_path)

    first = sa._write_subagent_output(session, "child-1", "first turn")
    again = sa._write_subagent_output(session, "child-1", "second turn")
    other = sa._write_subagent_output(session, "child-2", "first turn")

    assert first == again, "one file per sub-agent session"
    assert (tmp_path / Path(again)).read_text(encoding="utf-8") == "second turn"
    assert other == ".kimix_cache/temp_child-2.md"
    assert (tmp_path / Path(other)).read_text(encoding="utf-8") == "first turn"


def test_output_file_name_flattens_path_hostile_session_ids(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    session = _FakeSession(tmp_path)

    saved = sa._write_subagent_output(session, "../evil/../id:with spaces", "text")

    assert saved == ".kimix_cache/temp_.._evil_.._id_with_spaces.md"
    assert (tmp_path / ".kimix_cache").is_dir()
    assert (tmp_path / Path(saved)).parent == tmp_path / ".kimix_cache"
    # Degenerate ids still yield a usable file name.
    assert sa._output_file_name("") == "temp_unknown.md"
    assert sa._output_file_name("..") == "temp_unknown.md"


def test_write_subagent_output_falls_back_to_the_shared_temp_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    saved = sa._write_subagent_output(_FakeSession(blocker), "child-1", "rescued text")

    assert saved.startswith(".kimix_cache/tmp_")
    assert saved.endswith(".md")
    assert (tmp_path / Path(saved)).read_text(encoding="utf-8") == "rescued text"


def test_write_subagent_output_returns_empty_when_no_write_succeeds(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    def _boom(*args, **kwargs):
        raise OSError("no temp folder either")

    monkeypatch.setattr(sa, "_create_script_file", _boom)

    assert sa._write_subagent_output(_FakeSession(blocker), "child-1", "text") == ""


def test_display_output_path_falls_back_to_absolute_outside_the_cwd(tmp_path, monkeypatch):
    other = tmp_path / "cwd"
    other.mkdir()
    monkeypatch.chdir(other)
    target = tmp_path / "elsewhere" / "temp_child-1.md"

    shown = sa._display_output_path(target)

    assert shown.endswith("temp_child-1.md")
    assert "\\" not in shown
    assert Path(shown).is_absolute()


# --------------------------------------------------------------------------- #
# Result message
# --------------------------------------------------------------------------- #


def test_output_message_returns_path_when_saved():
    assert sa._output_message("child-1", ".kimix_cache/temp_child-1.md") == (
        "Output saved to .kimix_cache/temp_child-1.md\nSession ID: child-1"
    )


def test_output_message_never_contains_the_result_text():
    message = sa._output_message("child-1", "")

    assert message == "Output could not be saved to a file\nSession ID: child-1"
    assert "(no text output)" not in message


# --------------------------------------------------------------------------- #
# Agent._execute
# --------------------------------------------------------------------------- #


async def test_execute_writes_result_and_returns_the_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _patch_utils(monkeypatch, text="final answer")
    tool = Agent(_FakeSession(tmp_path))

    result = await tool._execute(
        _prepared(
            _FakeSession(tmp_path, "child-1"),
            SubAgentParams(prompt="do it"),
            "do it",
        )
    )

    assert not result.is_error
    saved = result.extras["output_file"]
    assert saved == ".kimix_cache/temp_child-1.md"
    assert result.output == f"Output saved to {saved}\nSession ID: child-1"
    assert "final answer" not in result.output
    assert (tmp_path / Path(saved)).read_text(encoding="utf-8") == "final answer"


async def test_execute_writes_partial_result_and_reports_the_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    closed = _patch_utils(monkeypatch, error="boom")
    tool = Agent(_FakeSession(tmp_path))
    child = _FakeSession(tmp_path, "child-1")

    result = await tool._execute(
        _prepared(child, SubAgentParams(prompt="do it"), "do it")
    )

    assert result.is_error
    assert result.message.startswith("boom")
    saved = result.extras["output_file"]
    assert result.output.startswith(f"Output saved to {saved}")
    assert (tmp_path / Path(saved)).read_text(encoding="utf-8") == "(no text output)"
    assert closed == [child]


async def test_execute_never_returns_the_text_when_the_primary_save_fails(
    tmp_path, monkeypatch
):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _patch_utils(monkeypatch, text="final answer")
    tool = Agent(_FakeSession(blocker))

    result = await tool._execute(
        _prepared(
            _FakeSession(tmp_path, "child-1"),
            SubAgentParams(prompt="do it"),
            "do it",
        )
    )

    saved = result.extras["output_file"]
    assert saved.startswith(".kimix_cache/tmp_"), "temp-folder fallback"
    assert result.output == f"Output saved to {saved}\nSession ID: child-1"
    assert "final answer" not in result.output
    assert (tmp_path / Path(saved)).read_text(encoding="utf-8") == "final answer"


async def test_execute_reports_a_failed_save_without_echoing_the_text(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _patch_utils(monkeypatch, text="final answer")
    tool = Agent(_FakeSession(blocker))

    def _boom(*args, **kwargs):
        raise OSError("no temp folder either")

    monkeypatch.setattr(sa, "_create_script_file", _boom)

    result = await tool._execute(
        _prepared(
            _FakeSession(tmp_path, "child-1"),
            SubAgentParams(prompt="do it"),
            "do it",
        )
    )

    assert result.output == "Output could not be saved to a file\nSession ID: child-1"
    assert "final answer" not in result.output
    assert "output_file" not in result.extras
