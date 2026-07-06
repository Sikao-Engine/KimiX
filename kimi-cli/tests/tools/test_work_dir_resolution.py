"""Tests that builtin tools resolve relative paths against Session.work_dir."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from kaos import get_current_kaos, set_current_kaos
from kaos.local import LocalKaos
from kaos.path import KaosPath
from kosong.chat_provider.mock import MockChatProvider

from kimi_cli.background import BackgroundTaskManager
from kimi_cli.config import Config, get_default_config
from kimi_cli.llm import ALL_MODEL_CAPABILITIES, LLM
from kimi_cli.metadata import WorkDirMeta
from kimi_cli.notifications import NotificationManager
from kimi_cli.session import Session
from kimi_cli.session_state import SessionState
from kimi_cli.soul.agent import BuiltinSystemPromptArgs, Runtime
from kimi_cli.soul.approval import Approval
from kimi_cli.soul.toolset import current_tool_call
from kimi_cli.tools.file.glob import Glob, Params as GlobParams
from kimi_cli.tools.file.grep_local import Grep, Params as GrepParams
from kimi_cli.tools.file.read import Params as ReadParams
from kimi_cli.tools.file.read import ReadFile
from kimi_cli.tools.file.replace import Edit, EditFile, Params as EditParams
from kimi_cli.tools.file.write import Params as WriteParams
from kimi_cli.tools.file.write import WriteFile
from kimi_cli.wire.types import ToolCall
from kimi_cli.utils.environment import Environment
from kimi_cli.wire.file import WireFile


def _make_runtime(work_dir: KaosPath) -> Runtime:
    """Create a minimal Runtime whose session work_dir is *work_dir*."""
    config = get_default_config()
    llm = LLM(
        chat_provider=MockChatProvider([]),
        max_context_size=100_000,
        capabilities=ALL_MODEL_CAPABILITIES,
    )
    builtin_args = BuiltinSystemPromptArgs(
        KIMI_NOW="1970-01-01T00:00:00+00:00",
        KIMI_WORK_DIR=work_dir,
        KIMI_WORK_DIR_LS="Test ls content",
        KIMI_AGENTS_MD="Test agents content",
        KIMI_SKILLS="No skills found.",
        KIMI_ADDITIONAL_DIRS_INFO="",
        KIMI_OS="macOS",
        KIMI_SHELL="bash (`/bin/bash`)",
    )
    share_dir = Path(tempfile.mkdtemp())
    session = Session(
        id="test-work-dir",
        work_dir=work_dir,
        work_dir_meta=WorkDirMeta(path=str(work_dir), kaos=get_current_kaos().name),
        context_file=share_dir / "context.jsonl",
        wire_file=WireFile(path=share_dir / "wire.jsonl"),
        state=SessionState(),
        title="Test Session",
        updated_at=0.0,
        custom_data={},
        custom_config={},
    )
    notifications = NotificationManager(
        session.context_file.parent / "notifications", config.notifications
    )
    return Runtime(
        config=config,
        llm=llm,
        builtin_args=builtin_args,
        denwa_renji=None,  # type: ignore[arg-type]
        session=session,
        approval=Approval(yolo=True),
        labor_market=None,  # type: ignore[arg-type]
        environment=Environment(
            os_kind="Unix",
            os_arch="aarch64",
            os_version="1.0",
            shell_name="bash",
            shell_path=KaosPath("/bin/bash"),
        ),
        notifications=notifications,
        background_tasks=BackgroundTaskManager(
            session,
            config.background,
            notifications=notifications,
        ),
        skills={},
        oauth=None,  # type: ignore[arg-type]
        additional_dirs=[],
        skills_dirs=[],
        role="root",
    )


@pytest.fixture
def isolated_work_dir():
    """Yield (work_dir, cwd) where cwd differs from work_dir."""
    with tempfile.TemporaryDirectory() as work_dir_str:
        with tempfile.TemporaryDirectory() as cwd_str:
            original_cwd = Path.cwd()
            work_dir = KaosPath.unsafe_from_local_path(Path(work_dir_str).resolve())
            cwd = Path(cwd_str).resolve()
            os.chdir(cwd)
            token = set_current_kaos(LocalKaos())
            try:
                yield work_dir, cwd
            finally:
                os.chdir(original_cwd)


async def test_read_file_relative_to_work_dir(isolated_work_dir):
    """ReadFile resolves a relative path against Session.work_dir, not process cwd."""
    work_dir, cwd = isolated_work_dir
    # Create the file in work_dir, NOT in cwd.
    await (work_dir / "hello.txt").write_text("from work_dir\n")
    # Ensure the file does not exist in cwd.
    assert not (cwd / "hello.txt").exists()

    runtime = _make_runtime(work_dir)
    tool = ReadFile(runtime, runtime.session)
    result = await tool(ReadParams(path="hello.txt"))
    assert not result.is_error
    assert "from work_dir" in result.output


async def test_write_file_relative_to_work_dir(isolated_work_dir):
    """WriteFile resolves a relative path against Session.work_dir, not process cwd."""
    work_dir, cwd = isolated_work_dir
    runtime = _make_runtime(work_dir)
    tool = WriteFile(runtime, Approval(yolo=True), runtime.session)
    token = current_tool_call.set(
        ToolCall(id="test", function=ToolCall.FunctionBody(name="WriteFile", arguments=None))
    )
    try:
        result = await tool(WriteParams(path="created.txt", content="written to work_dir\n"))
    finally:
        current_tool_call.reset(token)
    assert not result.is_error

    assert await (work_dir / "created.txt").exists()
    assert not (cwd / "created.txt").exists()
    content = await (work_dir / "created.txt").read_text()
    assert content == "written to work_dir\n"


async def test_edit_file_relative_to_work_dir(isolated_work_dir):
    """EditFile resolves a relative path against Session.work_dir, not process cwd."""
    work_dir, cwd = isolated_work_dir
    await (work_dir / "edit.txt").write_text("hello world\n")
    assert not (cwd / "edit.txt").exists()

    runtime = _make_runtime(work_dir)
    tool = EditFile(runtime, Approval(yolo=True), runtime.session)
    token = current_tool_call.set(
        ToolCall(id="test", function=ToolCall.FunctionBody(name="EditFile", arguments=None))
    )
    try:
        result = await tool(
            EditParams(path="edit.txt", edit=Edit(old="world", new="work_dir"))
        )
    finally:
        current_tool_call.reset(token)
    assert not result.is_error

    assert await (work_dir / "edit.txt").read_text() == "hello work_dir\n"
    assert not (cwd / "edit.txt").exists()


async def test_glob_relative_to_work_dir(isolated_work_dir):
    """Glob resolves a relative pattern against Session.work_dir, not process cwd."""
    work_dir, cwd = isolated_work_dir
    await (work_dir / "find_me.py").write_text("# work_dir file")
    assert not (cwd / "find_me.py").exists()

    runtime = _make_runtime(work_dir)
    tool = Glob(runtime)
    result = await tool(GlobParams(pattern="*.py"))
    assert not result.is_error
    assert "find_me.py" in result.output


async def test_grep_relative_to_work_dir(isolated_work_dir):
    """Grep resolves a relative path against Session.work_dir, not process cwd."""
    work_dir, cwd = isolated_work_dir
    await (work_dir / "search.txt").write_text("needle in work_dir\n")
    assert not (cwd / "search.txt").exists()

    runtime = _make_runtime(work_dir)
    tool = Grep(runtime)
    # Force the Python fallback so the test does not depend on ripgrep.
    tool._rg_path = None
    tool._rg_path_task = None
    result = await tool(
        GrepParams(pattern="needle", path=".", output_mode="content")
    )
    assert not result.is_error
    assert "needle in work_dir" in result.output
