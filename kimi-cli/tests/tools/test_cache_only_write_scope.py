"""Cache-only write scope: report artifacts for read-only researcher sessions.

The ``research`` tool spawns a read-only child session (``agent_readonly.json``)
that now also owns ``write``/``edit`` — but only in the *cache-only* mode
implemented by :mod:`kimi_cli.tools.file.cache_scope`: every argument path must
be a plain relative file name and lands under
``<work dir>/.kimix_cache/research/<session id>/``.

These tests are a dry run of the whole workflow without an LLM:

* the scope resolver accepts relative names and refuses absolute paths, drive
  paths, ``~``, ``..`` escapes, URL schemes and symlinks that leave the sandbox;
* the sandbox is per session and separate from the session/temp state that
  already lives in ``.kimix_cache``;
* the real ``write``/``edit`` tools (with a *non-yolo* approval object, so any
  approval request would blow up) save into the sandbox, record the path in the
  session ledger and explain the sandbox in their message;
* the ``research`` tool drives those very tools through a stubbed SDK session
  and appends the child's artifact paths to the text it returns.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kaos.path import KaosPath
from kimi_cli.file_mtine import FileMTime
from kimi_cli.soul.approval import Approval
from kimi_cli.tools.file.cache_scope import (
    CACHE_DIR_NAME,
    CACHE_ONLY_WRITE_KEY,
    CACHE_SUBDIR_NAME,
    CACHE_WRITTEN_FILES_KEY,
    cache_base,
    cache_only_write_enabled,
    cache_relative_path,
    cache_root,
    cache_written_files,
    enable_cache_only_write,
    record_cache_written,
    resolve_cache_only_path,
)
from kimi_cli.tools.file.edit import EditFile
from kimi_cli.tools.file.edit.params import EditParams
from kimi_cli.tools.file.write import Params as WriteParams
from kimi_cli.tools.file.write import WriteFile
from kimi_cli.tools.research import Research, ResearchParams

MessageType = importlib.import_module("kimix.ui.printing").MessageType
kimix_utils = importlib.import_module("kimix.utils")

NS = "child-1"


class _FakeRuntime:
    """Only the two ``Runtime`` attributes the file tools read."""

    def __init__(self, work_dir: KaosPath) -> None:
        self.builtin_args = SimpleNamespace(KIMI_WORK_DIR=work_dir)
        self.additional_dirs: list[KaosPath] = []


def _make_session(
    work_dir: KaosPath,
    *,
    session_id: str = NS,
    custom_data: dict[str, Any] | None = None,
    cache_only: bool = False,
) -> SimpleNamespace:
    """A session stand-in exposing exactly what the file tools + research use."""
    session = SimpleNamespace(
        id=session_id,
        work_dir=work_dir,
        custom_data=custom_data if custom_data is not None else {},
        custom_config={},
        file_mtime=FileMTime(),
    )
    if cache_only:
        enable_cache_only_write(session)
    return session


def _tools(
    work_dir: KaosPath, session: SimpleNamespace, *, yolo: bool = False
) -> tuple[WriteFile, EditFile]:
    # ``Approval(yolo=False)`` is deliberate for cache-only cases: such a write
    # must never ask for approval (a real research child has no user attached),
    # and requesting one outside a tool call raises — so a regression fails here.
    approval = Approval(yolo=yolo)
    runtime = _FakeRuntime(work_dir)
    return WriteFile(runtime, approval, session), EditFile(runtime, approval, session)


def _sandbox(tmp_path: Path, session_id: str = NS) -> Path:
    """The real on-disk sandbox for *session_id* (symlinks resolved)."""
    return Path(str(cache_root(KaosPath(str(tmp_path)), session_id)))


def _cache_base(tmp_path: Path) -> Path:
    return Path(str(cache_base(KaosPath(str(tmp_path)))))


def _rel(raw_under_sandbox: str, session_id: str = NS) -> str:
    return f"{CACHE_SUBDIR_NAME}/{session_id}/{raw_under_sandbox}"


def _display(rel: str) -> str:
    return f"{CACHE_DIR_NAME}/{rel}"


def _make_symlink(target: Path, link: Path, *, directory: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - OS privilege
        pytest.skip(f"symlinks are unavailable on this host: {exc}")


# --------------------------------------------------------------------------- #
# Path scope: the security boundary
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw",
    ["notes/findings.md", "report.md", "deep/nested/dir/out.txt", "a b/c d.md"],
)
def test_relative_names_resolve_inside_the_session_sandbox(tmp_path: Path, raw: str):
    work_dir = KaosPath(str(tmp_path))
    resolved, error = resolve_cache_only_path(raw, work_dir, NS)

    assert error is None
    assert resolved is not None
    assert cache_relative_path(resolved, work_dir) == _rel(raw)
    assert Path(str(resolved)).is_relative_to(_sandbox(tmp_path))


@pytest.mark.parametrize(
    "raw",
    [
        "",
        " ",
        "/etc/passwd",
        "\\Windows\\system32\\cfg",
        "C:\\Windows\\leak.txt",
        "C:/Windows/leak.txt",
        "C:relative-with-drive.md",
        "\\\\server\\share\\leak.txt",
        "//server/share/leak.txt",
        "~/leak.md",
        "../escape.md",
        "..\\escape.md",
        "notes/../../escape.md",
        "conflict://3",
        "https://example.com/report.md",
        ".",  # the sandbox directory itself, not a file
    ],
)
def test_escaping_absolute_or_scheme_paths_are_refused(tmp_path: Path, raw: str):
    resolved, error = resolve_cache_only_path(raw, KaosPath(str(tmp_path)), NS)

    assert resolved is None
    assert error, f"`{raw}` must be refused with a message"


def test_nested_work_dir_relative_name_never_escapes(tmp_path: Path):
    work_dir = KaosPath(str(tmp_path))
    resolved, error = resolve_cache_only_path("sub/../ok.md", work_dir, NS)

    assert error is None
    assert resolved is not None
    assert cache_relative_path(resolved, work_dir) == _rel("ok.md")
    assert Path(str(resolved)) == _sandbox(tmp_path) / "ok.md"


def test_sandbox_is_namespaced_per_session(tmp_path: Path):
    work_dir = KaosPath(str(tmp_path))
    first, first_err = resolve_cache_only_path("report.md", work_dir, "child-a")
    second, second_err = resolve_cache_only_path("report.md", work_dir, "child-b")

    assert first_err is None and second_err is None
    assert first is not None and second is not None
    assert first != second
    assert cache_relative_path(first, work_dir) == _rel("report.md", "child-a")
    assert cache_relative_path(second, work_dir) == _rel("report.md", "child-b")


def test_session_namespace_is_path_safe(tmp_path: Path):
    # A hostile/odd session id must stay a single directory segment.
    work_dir = KaosPath(str(tmp_path))
    resolved, error = resolve_cache_only_path("x.md", work_dir, "../../evil")

    assert error is None
    assert resolved is not None
    rel = cache_relative_path(resolved, work_dir)
    assert rel == _rel("x.md", "evil")
    assert Path(str(resolved)).is_relative_to(_sandbox(tmp_path, "evil"))


@pytest.mark.parametrize(
    "raw",
    [
        ".kimix_cache/report.md",
        ".kimix_cache\\report.md",
        f"{CACHE_SUBDIR_NAME}/{NS}/report.md",
        f".kimix_cache/{CACHE_SUBDIR_NAME}/{NS}/report.md",
    ],
)
def test_a_path_copied_from_a_result_resolves_to_the_same_file(tmp_path: Path, raw: str):
    work_dir = KaosPath(str(tmp_path))
    resolved, error = resolve_cache_only_path(raw, work_dir, NS)

    assert error is None
    assert resolved is not None
    assert cache_relative_path(resolved, work_dir) == _rel("report.md")


def test_cache_relative_path_rejects_paths_outside_the_cache(tmp_path: Path):
    work_dir = KaosPath(str(tmp_path))

    assert cache_relative_path(work_dir / "notes.md", work_dir) is None
    assert cache_relative_path(work_dir / CACHE_DIR_NAME, work_dir) is None


def test_symlinks_inside_the_sandbox_cannot_escape(tmp_path: Path):
    work_dir = KaosPath(str(tmp_path))
    sandbox = _sandbox(tmp_path)
    sandbox.mkdir(parents=True)

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    _make_symlink(Path(os.path.realpath(outside_dir)), sandbox / "linkdir", directory=True)
    resolved, error = resolve_cache_only_path("linkdir/leaked.md", work_dir, NS)
    assert resolved is None, "a directory symlink must not widen the sandbox"
    assert error

    outside_file = tmp_path / "outside.md"
    outside_file.write_text("keep me\n", encoding="utf-8")
    _make_symlink(Path(os.path.realpath(outside_file)), sandbox / "report.md")
    resolved, error = resolve_cache_only_path("report.md", work_dir, NS)
    assert resolved is None, "a file symlink must not widen the sandbox"
    assert error


# --------------------------------------------------------------------------- #
# write tool in cache-only mode
# --------------------------------------------------------------------------- #


async def test_write_saves_a_relative_name_under_the_session_sandbox(tmp_path: Path):
    work_dir = KaosPath(str(tmp_path))
    session = _make_session(work_dir, cache_only=True)
    writer, _ = _tools(work_dir, session)

    result = await writer(WriteParams(file_path="notes/findings.md", content="# findings\n"))

    assert not result.is_error, result.message
    target = _sandbox(tmp_path) / "notes" / "findings.md"
    assert target.read_text(encoding="utf-8") == "# findings\n"
    # The path is recorded for the research tool to report back.
    assert cache_written_files(session) == [_rel("notes/findings.md")]
    # The model is told where the file actually lives.
    assert _display(_rel("notes/findings.md")) in result.message


async def test_cache_write_is_not_marked_out_of_work_dir(tmp_path: Path):
    """The sandbox resolves symlinks, so its physical path can differ from work_dir."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    _make_symlink(Path(os.path.realpath(real)), link, directory=True)
    work_dir = KaosPath(str(link))
    session = _make_session(work_dir, cache_only=True)
    writer, editor = _tools(work_dir, session)

    written = await writer(WriteParams(file_path="report.md", content="alpha\n"))
    edited = await editor(EditParams(file_path="report.md", old_string="alpha", new_string="beta"))

    assert not written.is_error, written.message
    assert not edited.is_error, edited.message
    assert "[out of work-dir]" not in written.message
    assert "[out of work-dir]" not in edited.message
    assert (real / CACHE_DIR_NAME / CACHE_SUBDIR_NAME / NS / "report.md").read_text(
        encoding="utf-8"
    ) == "beta\n"
    assert cache_written_files(session) == [_rel("report.md")]


async def test_write_refuses_absolute_paths_and_writes_nothing(tmp_path: Path):
    work_dir = KaosPath(str(tmp_path))
    session = _make_session(work_dir, cache_only=True)
    writer, _ = _tools(work_dir, session)
    outside = tmp_path / "leak.md"

    for raw in (
        str(outside),
        "../leak.md",
        "C:\\Windows\\leak.md",
        "/etc/passwd",
    ):
        result = await writer(WriteParams(file_path=raw, content="leak"))

        assert result.is_error, f"`{raw}` must be refused"
        assert result.message
    assert not outside.exists()
    assert cache_written_files(session) == []


async def test_write_refuses_a_symlinked_escape(tmp_path: Path):
    work_dir = KaosPath(str(tmp_path))
    session = _make_session(work_dir, cache_only=True)
    writer, _ = _tools(work_dir, session)
    sandbox = _sandbox(tmp_path)
    sandbox.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    _make_symlink(Path(os.path.realpath(outside)), sandbox / "linkdir", directory=True)

    result = await writer(WriteParams(file_path="linkdir/leaked.md", content="leak\n"))

    assert result.is_error
    assert not (outside / "leaked.md").exists()
    assert cache_written_files(session) == []


async def test_write_cannot_clobber_session_state_in_the_cache(tmp_path: Path):
    """``.kimix_cache`` holds session state; only the session sandbox is writable."""
    work_dir = KaosPath(str(tmp_path))
    session = _make_session(work_dir, cache_only=True)
    writer, _ = _tools(work_dir, session)
    state = _cache_base(tmp_path) / "sessions" / "keep.txt"
    state.parent.mkdir(parents=True)
    state.write_text("session state\n", encoding="utf-8")

    result = await writer(WriteParams(file_path="sessions/keep.txt", content="clobbered\n"))

    assert not result.is_error, result.message
    assert state.read_text(encoding="utf-8") == "session state\n"
    assert (_sandbox(tmp_path) / "sessions" / "keep.txt").read_text(encoding="utf-8") == (
        "clobbered\n"
    )
    assert cache_written_files(session) == [_rel("sessions/keep.txt")]


async def test_write_accepts_the_path_it_reported(tmp_path: Path):
    """A model re-writing its own reported artifact must not double-nest it."""
    work_dir = KaosPath(str(tmp_path))
    session = _make_session(work_dir, cache_only=True)
    writer, _ = _tools(work_dir, session)
    reported = _display(_rel("report.md"))

    first = await writer(WriteParams(file_path="report.md", content="v1\n"))
    second = await writer(WriteParams(file_path=reported, content="v2\n"))

    assert not first.is_error, first.message
    assert not second.is_error, second.message
    assert (_sandbox(tmp_path) / "report.md").read_text(encoding="utf-8") == "v2\n"
    assert not (_sandbox(tmp_path) / CACHE_DIR_NAME).exists(), "path must not nest the sandbox"
    assert cache_written_files(session) == [_rel("report.md")]


async def test_parent_can_read_the_artifact_path_the_child_reported(tmp_path: Path):
    """The ledger path is what the parent hands to `read`; it must resolve."""
    from kimi_cli.tools.file.read import Params as ReadParams
    from kimi_cli.tools.file.read import ReadFile

    work_dir = KaosPath(str(tmp_path))
    child = _make_session(work_dir, cache_only=True)
    writer, _ = _tools(work_dir, child)
    await writer(WriteParams(file_path="notes/findings.md", content="alpha\nbeta\n"))
    reported = _display(cache_written_files(child)[0])

    parent = _make_session(work_dir, session_id="parent-reader")
    read_tool = ReadFile(_FakeRuntime(work_dir), parent)
    result = await read_tool(ReadParams(file_path=reported))

    assert not result.is_error, result.message
    assert "alpha" in result.output
    assert "beta" in result.output


async def test_write_records_append_mode_once(tmp_path: Path):
    work_dir = KaosPath(str(tmp_path))
    session = _make_session(work_dir, cache_only=True)
    writer, _ = _tools(work_dir, session)

    await writer(WriteParams(file_path="log.md", content="one\n"))
    await writer(WriteParams(file_path="log.md", content="two\n", mode="append"))

    assert (_sandbox(tmp_path) / "log.md").read_text(encoding="utf-8") == "one\ntwo\n"
    assert cache_written_files(session) == [_rel("log.md")], "ledger must dedupe"


# --------------------------------------------------------------------------- #
# edit tool in cache-only mode
# --------------------------------------------------------------------------- #


async def test_edit_replaces_text_in_a_cache_report(tmp_path: Path):
    work_dir = KaosPath(str(tmp_path))
    session = _make_session(work_dir, cache_only=True)
    writer, editor = _tools(work_dir, session)
    await writer(WriteParams(file_path="notes/findings.md", content="alpha\nbeta\n"))

    result = await editor(
        EditParams(file_path="notes/findings.md", old_string="beta", new_string="gamma")
    )

    assert not result.is_error, result.message
    target = _sandbox(tmp_path) / "notes" / "findings.md"
    assert target.read_text(encoding="utf-8") == "alpha\ngamma\n"
    assert cache_written_files(session) == [_rel("notes/findings.md")]
    assert _display(_rel("notes/findings.md")) in result.message


async def test_edit_sloppy_mode_is_also_confined_to_the_cache(tmp_path: Path):
    work_dir = KaosPath(str(tmp_path))
    session = _make_session(work_dir, cache_only=True)
    writer, editor = _tools(work_dir, session)
    await writer(WriteParams(file_path="sloppy.md", content="timeout = 1000\n"))
    # The sloppy mode is write-after-read only, exactly like the real flow where
    # the child `read` the file it now edits.
    session.file_mtime.clean_file(str(_sandbox(tmp_path) / "sloppy.md"))

    result = await editor(EditParams(input="§sloppy.md\ntimeout = ⟪1000│5000⟫\n"))

    assert not result.is_error, result.message
    assert (_sandbox(tmp_path) / "sloppy.md").read_text(encoding="utf-8") == "timeout = 5000\n"
    assert cache_written_files(session) == [_rel("sloppy.md")]


async def test_edit_refuses_paths_outside_the_cache(tmp_path: Path):
    work_dir = KaosPath(str(tmp_path))
    (tmp_path / "real.md").write_text("keep me\n", encoding="utf-8")
    session = _make_session(work_dir, cache_only=True)
    _, editor = _tools(work_dir, session)

    for raw in ("real.md", "../real.md", "C:\\Windows\\real.md"):
        result = await editor(EditParams(file_path=raw, old_string="keep", new_string="lose"))

        assert result.is_error, f"`{raw}` must be refused"
    assert (tmp_path / "real.md").read_text(encoding="utf-8") == "keep me\n"
    assert cache_written_files(session) == []


# --------------------------------------------------------------------------- #
# Regression: a normal session keeps the ordinary work-dir behaviour
# --------------------------------------------------------------------------- #


async def test_normal_session_still_writes_inside_the_work_dir(tmp_path: Path):
    from tests.conftest import tool_call_context

    work_dir = KaosPath(str(tmp_path))
    session = _make_session(work_dir)
    # A normal session asks for approval, which needs an active tool call (the
    # cache-only cases above never ask, so they need no context at all).
    writer, _ = _tools(work_dir, session, yolo=True)

    with tool_call_context("WriteFile"):
        result = await writer(WriteParams(file_path="normal.md", content="hi\n"))

    assert not result.is_error, result.message
    assert (tmp_path / "normal.md").read_text(encoding="utf-8") == "hi\n"
    assert not (_sandbox(tmp_path) / "normal.md").exists()
    assert cache_written_files(session) == []
    assert session.custom_data.get(CACHE_ONLY_WRITE_KEY) is None


def test_record_cache_written_is_failure_isolated():
    class _Broken:
        @property
        def custom_data(self):
            raise RuntimeError("no custom_data")

    rel = _rel("a.md")
    assert record_cache_written(_Broken(), rel) is None, "recording must never raise"
    assert record_cache_written(None, rel) is None
    assert record_cache_written(_Broken(), None) is None
    session = _make_session(KaosPath("C:/tmp/work"))
    assert record_cache_written(session, rel) == rel


def test_cache_mode_requires_the_exact_flag():
    """A Mock-style session (truthy attributes) must not enter cache-only mode."""
    from unittest.mock import MagicMock

    assert cache_only_write_enabled(MagicMock()) is False
    assert cache_only_write_enabled(_make_session(KaosPath("C:/tmp/work"))) is False
    session = _make_session(KaosPath("C:/tmp/work"))
    enable_cache_only_write(session)
    assert cache_only_write_enabled(session) is True


# --------------------------------------------------------------------------- #
# research tool: dry run of the whole workflow
# --------------------------------------------------------------------------- #


class _SdkSession:
    """Mirror ``kimi_agent_sdk.Session``: the real session hides in ``_cli``.

    It deliberately exposes neither ``id``, ``work_dir`` nor ``custom_data``;
    the research tool must reach through ``_cli.session`` exactly like the SDK
    wrapper requires.
    """

    def __init__(self, core: SimpleNamespace) -> None:
        self._cli = SimpleNamespace(session=core)
        self._closed = False


def _fake_child(kwargs: dict[str, Any], index: int, children: list[_SdkSession]) -> _SdkSession:
    core = _make_session(
        kwargs["work_dir"],
        session_id=f"child-{index}",
        custom_data=dict(kwargs.get("custom_data") or {}),
    )
    child = _SdkSession(core)
    children.append(child)
    return child


async def test_research_dry_run_reports_the_files_its_child_wrote(tmp_path: Path, monkeypatch):
    """A research run whose child writes two reports returns their paths."""
    work_dir = KaosPath(str(tmp_path))
    parent = _make_session(work_dir, session_id="parent-1")
    research = Research(parent)

    created: list[dict] = []
    children: list[_SdkSession] = []

    async def fake_create_session_async(**kwargs):
        created.append(kwargs)
        return _fake_child(kwargs, len(created), children)

    tool_results: list[Any] = []

    async def fake_prompt_async(prompt_str, session, output_function=None, **kwargs):
        core = session._cli.session
        writer, editor = _tools(core.work_dir, core)
        tool_results.append(
            await writer(WriteParams(file_path="notes/findings.md", content="alpha\nbeta\n"))
        )
        tool_results.append(
            await editor(
                EditParams(file_path="notes/findings.md", old_string="beta", new_string="gamma")
            )
        )
        # An escaping path must be refused rather than leaking into the repo.
        tool_results.append(
            await writer(WriteParams(file_path=str(core.work_dir / "leak.md"), content="leak"))
        )
        if output_function is not None:
            output_function("the findings", MessageType.Text)

    closed: list[Any] = []

    async def fake_close_session_async(session):
        closed.append(session)
        session._closed = True

    monkeypatch.setattr(kimix_utils, "_create_session_async", fake_create_session_async)
    monkeypatch.setattr(kimix_utils, "prompt_async", fake_prompt_async)
    monkeypatch.setattr(kimix_utils, "close_session_async", fake_close_session_async)

    result = await research(ResearchParams(query="read the docs and write a report"))

    # 1. The child was created in cache-only write mode, on the *core* session
    #    (the SDK wrapper hides custom_data, so the tool must verify the core).
    assert created[0]["custom_data"][CACHE_ONLY_WRITE_KEY] is True
    child_core = children[0]._cli.session
    assert cache_only_write_enabled(child_core) is True
    assert cache_only_write_enabled(children[0]) is False, "the wrapper carries no custom_data"
    # The ledger object is shared with the creation kwargs, so writes are visible.
    assert child_core.custom_data[CACHE_WRITTEN_FILES_KEY] is created[0]["custom_data"][
        CACHE_WRITTEN_FILES_KEY
    ]

    # 2. The real tools ran inside the sandbox and recorded their artifact.
    assert [r.is_error for r in tool_results[:2]] == [False, False]
    assert tool_results[2].is_error, "an absolute path must be refused in the child"
    target = _sandbox(tmp_path, "child-1") / "notes" / "findings.md"
    assert target.read_text(encoding="utf-8") == "alpha\ngamma\n"
    assert not (tmp_path / "leak.md").exists()

    # 3. The tool result carries the text *and* the artifact paths.
    assert not result.is_error, result.message
    assert result.output.startswith("the findings")
    assert "## Research artifacts" in result.output
    assert f"- {_display(_rel('notes/findings.md', 'child-1'))}" in result.output
    assert "`read`" in result.output

    # 4. The child was closed exactly once (no session leak).
    assert closed == [children[0]]


async def test_research_dry_run_without_artifacts_adds_no_section(tmp_path: Path, monkeypatch):
    work_dir = KaosPath(str(tmp_path))
    research = Research(_make_session(work_dir, session_id="parent-2"))
    children: list[_SdkSession] = []

    async def fake_create_session_async(**kwargs):
        return _fake_child(kwargs, 1, children)

    async def fake_prompt_async(prompt_str, session, output_function=None, **kwargs):
        if output_function is not None:
            output_function("plain findings", MessageType.Text)

    async def fake_close_session_async(session):
        session._closed = True

    monkeypatch.setattr(kimix_utils, "_create_session_async", fake_create_session_async)
    monkeypatch.setattr(kimix_utils, "prompt_async", fake_prompt_async)
    monkeypatch.setattr(kimix_utils, "close_session_async", fake_close_session_async)

    result = await research(ResearchParams(query="just read"))

    assert not result.is_error, result.message
    assert result.output == "plain findings"
    assert "## Research artifacts" not in result.output


async def test_research_fails_closed_when_the_child_cannot_be_confined(tmp_path: Path, monkeypatch):
    """An unconfined child owns write/edit; it must never be prompted."""
    work_dir = KaosPath(str(tmp_path))

    class _Unconfined:
        id = "child-bad"

        def __init__(self, work_dir: KaosPath) -> None:
            self.work_dir = work_dir

        @property
        def custom_data(self):
            raise RuntimeError("custom_data is not writable")

    bad = _Unconfined(work_dir)
    research = Research(_make_session(work_dir, session_id="parent-3"))
    closed: list[Any] = []

    async def fake_create_session_async(**kwargs):
        return bad

    async def fake_prompt_async(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("an unconfined child must never be prompted")

    async def fake_close_session_async(session):
        closed.append(session)

    monkeypatch.setattr(kimix_utils, "_create_session_async", fake_create_session_async)
    monkeypatch.setattr(kimix_utils, "prompt_async", fake_prompt_async)
    monkeypatch.setattr(kimix_utils, "close_session_async", fake_close_session_async)

    result = await research(ResearchParams(query="write anywhere"))

    assert result.is_error
    assert "cache-only" in result.message
    assert closed == [bad], "the unconfined child must still be closed"


async def test_research_dry_run_background_includes_artifacts(tmp_path: Path, monkeypatch):
    work_dir = KaosPath(str(tmp_path))
    research = Research(_make_session(work_dir, session_id="parent-4"))
    children: list[_SdkSession] = []

    async def fake_create_session_async(**kwargs):
        return _fake_child(kwargs, 1, children)

    async def fake_prompt_async(prompt_str, session, output_function=None, **kwargs):
        core = session._cli.session
        writer, _ = _tools(core.work_dir, core)
        await writer(WriteParams(file_path="bg/report.md", content="background report\n"))
        if output_function is not None:
            output_function("background findings", MessageType.Text)

    async def fake_close_session_async(session):
        session._closed = True

    monkeypatch.setattr(kimix_utils, "_create_session_async", fake_create_session_async)
    monkeypatch.setattr(kimix_utils, "prompt_async", fake_prompt_async)
    monkeypatch.setattr(kimix_utils, "close_session_async", fake_close_session_async)

    from kimi_cli.tools.background import TaskOutput, TaskOutputParams

    started = await research(ResearchParams(query="background please", run_in_background=True))
    task_id = started.output.split("task_id: ", 1)[1].splitlines()[0].strip()
    got = await TaskOutput(research._session)(TaskOutputParams(job_id=task_id, wait=True, timeout=30))

    assert not got.is_error, got.message
    assert "background findings" in got.output
    assert f"- {_display(_rel('bg/report.md', 'child-1'))}" in got.output
    assert (_sandbox(tmp_path, "child-1") / "bg" / "report.md").read_text(encoding="utf-8") == (
        "background report\n"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-x"]))
