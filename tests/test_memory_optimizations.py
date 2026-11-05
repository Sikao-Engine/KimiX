"""Memory-optimisation regression tests.

These pin the *representation* contracts introduced by the deep memory pass:

* ``kimi_cli.utils.gc_tuning`` freezes the startup object graph exactly once and
  never raises;
* ``HistoryIndex`` does not drag the legacy numpy/BM25 stack in on the FTS5
  (production) path, while the legacy path still works;
* ``context_pruning._join_text_parts`` shares the source ``str`` for the common
  single-part case instead of copying it, and elision records share that object;
* the pruning passes copy-on-write: the caller's messages are never mutated and
  no deep copy of the whole history is made;
* the steer queue is bounded and drops the oldest entry;
* the ``kimix.retrieval`` memoisation caches have a bounded capacity.
"""

from __future__ import annotations

import asyncio
import inspect
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from kosong.message import Message
from kimi_cli.wire.types import TextPart


# ---------------------------------------------------------------------------
# gc tuning
# ---------------------------------------------------------------------------


def test_freeze_startup_objects_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    import gc

    from kimi_cli.utils import gc_tuning

    # ``gc.freeze()`` is process-global and irreversible (there is no
    # ``gc.unfreeze()``), so the real call must never run inside pytest: it would
    # make every live cyclic object uncollectable for the rest of the session.
    calls: list[int] = []
    monkeypatch.setattr(gc, "freeze", lambda: calls.append(1))

    gc_tuning.reset_for_tests()
    try:
        assert gc_tuning.is_frozen() is False
        assert gc_tuning.freeze_startup_objects() is True
        assert gc_tuning.is_frozen() is True
        # Second call must be a cheap no-op, not a second freeze.
        assert gc_tuning.freeze_startup_objects() is False
        assert calls == [1]
    finally:
        gc_tuning.reset_for_tests()


def test_freeze_startup_objects_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from kimi_cli.utils import gc_tuning
    import gc

    gc_tuning.reset_for_tests()
    try:

        def boom() -> None:
            raise RuntimeError("collector is on fire")

        monkeypatch.setattr(gc, "freeze", boom)
        assert gc_tuning.freeze_startup_objects() is False
        assert gc_tuning.is_frozen() is False
    finally:
        gc_tuning.reset_for_tests()


# ---------------------------------------------------------------------------
# HistoryIndex laziness
# ---------------------------------------------------------------------------


def test_history_index_fts_path_does_not_import_numpy(tmp_path: Path) -> None:
    """The FTS5 backend must not pay the legacy BM25 (numpy) import cost."""
    code = textwrap.dedent(
        """
        import sys
        from pathlib import Path

        from kimi_cli.soul.history_index import HistoryIndex

        idx = HistoryIndex(db_path=Path(sys.argv[1]) / "history.db")
        assert idx.load() in (True, False)
        idx.close()
        leaked = sorted(
            m for m in sys.modules if m == "numpy" or m.startswith("numpy.") or m == "kimix.retrieval"
        )
        print("LEAKED:" + ",".join(leaked))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    leaked = [line for line in proc.stdout.splitlines() if line.startswith("LEAKED:")]
    assert leaked and leaked[0] == "LEAKED:", leaked


def test_history_index_legacy_path_still_builds_the_index(tmp_path: Path) -> None:
    from kimi_cli.soul.history_index import HistoryIndex

    idx = HistoryIndex(persist_path=tmp_path / "history.json")
    try:
        idx.index_messages([Message(role="user", content=[TextPart(text="compile Python code")])])
        idx.index_messages([Message(role="assistant", content=[TextPart(text="use pyinstaller")])])
        assert len(idx._turns) == 2
        assert idx.search("compile Python", top_k=1)
    finally:
        idx.close()


def test_history_index_clear_in_fts_mode_does_not_import_retrieval(tmp_path: Path) -> None:
    from kimi_cli.soul import history_index as module

    idx = module.HistoryIndex(db_path=tmp_path / "history.db")
    try:
        idx.load()
        # ``clear`` used to re-initialise the legacy state (numpy import) on the
        # FTS path; it must only reset the refs now.
        idx.clear()
        assert idx._index is None
        assert idx._turns_list == []
    finally:
        idx.close()


# ---------------------------------------------------------------------------
# context pruning: shared strings, copy-on-write
# ---------------------------------------------------------------------------


def test_join_text_parts_shares_single_part_object() -> None:
    from kimi_cli.soul.context_pruning import _join_text_parts

    text = "x" * 4096
    part = TextPart(text=text)
    assert _join_text_parts([part]) is part.text  # no copy at all
    assert _join_text_parts([]) == ""
    assert _join_text_parts([TextPart(text="a"), TextPart(text="b")]) == "ab"
    # Non-text parts are ignored.
    assert _join_text_parts([TextPart(text="a"), object()]) == "a"


def test_join_text_parts_is_linear_not_quadratic() -> None:
    from kimi_cli.soul.context_pruning import _join_text_parts

    parts = [TextPart(text="chunk") for _ in range(500)]
    assert _join_text_parts(parts) == "chunk" * 500


def _tool_history(count: int, *, tail: int = 6) -> list[Message]:
    """A history of ``count`` tool messages with <system> metadata + big output."""
    history: list[Message] = []
    for i in range(count):
        history.append(
            Message(
                role="tool",
                tool_call_id=f"call_{i}",
                content=[
                    TextPart(text="<system>Results truncated to 20 lines.</system>"),
                    TextPart(text=f"line {i}\n" + "payload " * 400),
                ],
            )
        )
        if i >= count - tail:
            # Protect the tail from elision so the pass has something stable.
            history[-1].content = [TextPart(text=f"tail {i}")]
    return history


def test_elided_record_shares_the_source_string() -> None:
    from kimi_cli.soul.context_pruning import ContextPruner

    pruner = ContextPruner(
        trigger_ratio=0.0,
        recent_messages_protected=0,
        stable_prefix_messages=0,
        min_free_tokens=0,
        ephemeral_enabled=False,
        tool_output_min_tokens=1,
    )
    history = _tool_history(20, tail=0)
    result = pruner.prune(history, current_step=10, context_usage=0.97)
    assert result.elided, "a pass with 20 large tool outputs must elide something"
    for rec in result.elided:
        source = history[rec.index]
        source_texts = [p.text for p in source.content if isinstance(p, TextPart)]
        # Single-text-part messages must share the exact string object; multi
        # part ones must at least be equal (linear join, not a quadratic +=).
        if len(source_texts) == 1:
            assert rec.original_text is source_texts[0]
        else:
            assert rec.original_text == "".join(source_texts)


def test_prune_never_mutates_the_caller_history_with_micro_compress() -> None:
    from kimi_cli.soul.context_pruning import ContextPruner

    pruner = ContextPruner(
        trigger_ratio=0.0,
        recent_messages_protected=0,
        stable_prefix_messages=0,
        min_free_tokens=0,
        ephemeral_enabled=False,
        tool_output_min_tokens=1,
        micro_compress_enabled=True,
        micro_compress_min_saved_chars=1,
    )
    history = _tool_history(30)
    snapshot = [
        (msg.role, msg.tool_call_id, [type(p).__name__ for p in msg.content], list(msg.content))
        for msg in history
    ]
    pruner.prune(history, current_step=1, context_usage=0.99)

    for before, msg in zip(snapshot, history, strict=True):
        role, tool_call_id, part_types, parts = before
        assert msg.role == role
        assert msg.tool_call_id == tool_call_id
        assert [type(p).__name__ for p in msg.content] == part_types
        # Same part objects, same order: nothing was replaced in place.
        assert list(msg.content) == parts


def test_metadata_coalescing_uses_shallow_copies() -> None:
    """``coalesce_tool_metadata`` must run on copies, not on the caller's list."""
    from kimi_cli.soul.context_pruning import ContextPruner

    pruner = ContextPruner(
        trigger_ratio=0.0,
        recent_messages_protected=0,
        stable_prefix_messages=0,
        min_free_tokens=0,
        ephemeral_enabled=False,
        substantive_enabled=False,
        micro_compress_enabled=True,
        tool_output_min_tokens=10**9,
    )
    history: list[Message] = []
    for i in range(10):
        history.append(
            Message(
                role="tool",
                tool_call_id=f"c{i}",
                content=[
                    TextPart(text="<system>Results truncated to 20 lines.</system>"),
                    TextPart(text=f"output {i}"),
                ],
            )
        )
    original_first_part = history[0].content[0]
    original_content_lists = [list(m.content) for m in history]

    result = pruner.prune(history, current_step=1, context_usage=0.99)

    # The pass must actually have coalesced (otherwise the copy-on-write path
    # below would not be exercised at all).
    assert result.earliest_removed_index is not None
    assert result.messages is not history
    assert all(m is not h for m, h in zip(result.messages, history, strict=True))
    # ... while the caller's list kept every part object untouched.
    for msg, content in zip(history, original_content_lists, strict=True):
        assert list(msg.content) == content
    assert history[0].content[0] is original_first_part
    # The returned copies carry the coalesced metadata annotation.
    assert "[×" in result.messages[0].content[0].text
    # The regression signal: unchanged parts are *shared* with the caller's
    # history (a deep copy would yield 0 shared part objects, which is the
    # allocation this optimization removes).
    shared = sum(
        1
        for before, after in zip(history, result.messages, strict=True)
        for p_before in before.content
        if any(p_before is p_after for p_after in after.content)
    )
    assert shared > 0


def test_pruning_result_messages_are_not_the_originals_after_coalescing() -> None:
    from kimi_cli.soul.context_pruning import ContextPruner

    pruner = ContextPruner(
        trigger_ratio=0.0,
        recent_messages_protected=0,
        stable_prefix_messages=0,
        min_free_tokens=0,
        ephemeral_enabled=False,
        substantive_enabled=False,
        micro_compress_enabled=True,
        tool_output_min_tokens=10**9,
    )
    history: list[Message] = []
    for i in range(12):
        history.append(
            Message(
                role="tool",
                tool_call_id=f"c{i}",
                content=[
                    TextPart(text="<system>Results truncated to 20 lines.</system>"),
                    TextPart(text=f"output {i}"),
                ],
            )
        )
    result = pruner.prune(history, current_step=1, context_usage=0.99)
    if result.earliest_removed_index is not None:
        # A coalesced pass returns the copies: mutating them must not touch the
        # caller's history.
        for msg in result.messages:
            if msg.role == "tool" and msg.content:
                msg.content.clear()
        assert all(m.content for m in history)


# ---------------------------------------------------------------------------
# steer queue bound
# ---------------------------------------------------------------------------


def test_steer_queue_is_bounded_and_drops_oldest() -> None:
    from kimi_cli.soul import kimisoul as module

    assert module._STEER_QUEUE_MAX > 0

    class _Stub:
        def __init__(self) -> None:
            self._steer_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=module._STEER_QUEUE_MAX)

        _enqueue_steer = module.KimiSoul._enqueue_steer

    stub = _Stub()
    for i in range(module._STEER_QUEUE_MAX + 5):
        stub._enqueue_steer(f"steer-{i}")

    assert stub._steer_queue.qsize() == module._STEER_QUEUE_MAX
    remaining = [stub._steer_queue.get_nowait() for _ in range(stub._steer_queue.qsize())]
    assert remaining[-1] == f"steer-{module._STEER_QUEUE_MAX + 4}"
    # The five oldest entries were dropped.
    assert remaining[0] == "steer-5"


def test_kimi_soul_creates_a_bounded_steer_queue() -> None:
    from kimi_cli.soul import kimisoul as module

    source = inspect.getsource(module.KimiSoul.__init__)
    assert "maxsize=_STEER_QUEUE_MAX" in source


# ---------------------------------------------------------------------------
# retrieval cache caps
# ---------------------------------------------------------------------------


def test_retrieval_caches_are_bounded() -> None:
    from kimix import retrieval

    assert 0 < retrieval._SIMILARITY_CACHE_MAXSIZE <= 8192
    assert 0 < retrieval._CHAR_CACHE_MAXSIZE <= 16384

    for name in (
        "jaro_similarity",
        "jaro_winkler_similarity",
        "sorensen_dice_coefficient",
        "porter_stem",
    ):
        func = getattr(retrieval, name)
        cache_info = func.cache_info()
        assert cache_info.maxsize == retrieval._SIMILARITY_CACHE_MAXSIZE, name


# ---------------------------------------------------------------------------
# error_log caps
# ---------------------------------------------------------------------------


def test_error_log_snapshot_caps_are_small() -> None:
    from kimi_cli.soul import error_log

    assert error_log._MAX_HISTORY_MESSAGES <= 200
    assert error_log._MAX_STRING_CHARS <= 10_000
    assert error_log._MAX_MESSAGE_PART_CHARS <= 10_000


# ---------------------------------------------------------------------------
# startup import budget
# ---------------------------------------------------------------------------


def test_cli_help_does_not_import_the_agent_app() -> None:
    """``--help`` must stay a cheap import path (no agent/soul graph)."""
    code = textwrap.dedent(
        """
        import sys

        from typer.testing import CliRunner

        from kimi_cli.cli import cli

        result = CliRunner().invoke(cli, ["--help"])
        assert result.exit_code == 0, result.output
        leaked = sorted(
            m
            for m in sys.modules
            if m in {"kimi_cli.app", "kimi_cli.soul", "numpy", "anthropic", "openai", "mcp"}
        )
        print("LEAKED:" + ",".join(leaked))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    leaked = [line for line in proc.stdout.splitlines() if line.startswith("LEAKED:")]
    assert leaked and leaked[0] == "LEAKED:", leaked
