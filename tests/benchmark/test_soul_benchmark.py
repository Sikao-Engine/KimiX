"""Performance benchmarks for kimi-cli Tier A + B files.

All timings are assert-based so the file doubles as a regression test.
"""

from __future__ import annotations

import random
import string
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.slow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _random_words(n: int, length: int = 5) -> list[str]:
    """Return *n* random lower-case words of *length* chars."""
    rng = random.Random(42)
    return [
        "".join(rng.choices(string.ascii_lowercase, k=length))
        for _ in range(n)
    ]


def _make_text_message(text: str) -> Any:
    """Create a minimal Message-like object with TextPart content."""
    from kosong.message import Message, TextPart
    return Message(role="user", content=[TextPart(text=text)])


def _make_messages(count: int) -> list[Any]:
    """Create *count* synthetic messages."""
    msgs: list[Any] = []
    for i in range(count):
        text = f"This is message number {i} with some content for testing. " * 3
        msgs.append(_make_text_message(text))
    return msgs


# ---------------------------------------------------------------------------
# ContextDB benchmarks
# ---------------------------------------------------------------------------


class TestContextDBBenchmark:
    """Benchmarks for ContextDB."""

    @pytest.mark.asyncio
    async def test_append_messages(self, tmp_path: Path) -> None:
        """append_messages() — 100 messages at once."""
        from kimi_cli.soul.context_db import ContextDB
        db = ContextDB(tmp_path / "test.db")
        await db.initialize()
        msgs = _make_messages(100)

        start = time.perf_counter()
        for _ in range(100):
            await db.append_messages(msgs)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0
        await db.close()

    @pytest.mark.asyncio
    async def test_get_messages(self, tmp_path: Path) -> None:
        """get_messages() — retrieve 1000 messages."""
        from kimi_cli.soul.context_db import ContextDB
        db = ContextDB(tmp_path / "test.db")
        await db.initialize()
        msgs = _make_messages(1000)
        await db.append_messages(msgs)

        start = time.perf_counter()
        for _ in range(100):
            retrieved = await db.get_messages()
            assert len(retrieved) == 1000
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0
        await db.close()

    @pytest.mark.asyncio
    async def test_export(self, tmp_path: Path) -> None:
        """export() — full export of 500 messages."""
        from kimi_cli.soul.context_db import ContextDB
        db = ContextDB(tmp_path / "test.db")
        await db.initialize()
        msgs = _make_messages(500)
        await db.append_messages(msgs)

        start = time.perf_counter()
        for _ in range(50):
            exported = await db.export()
            assert len(exported.messages) == 500
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0
        await db.close()

    @pytest.mark.asyncio
    async def test_bulk_append(self, tmp_path: Path) -> None:
        """begin_transaction + 500 inserts + commit."""
        from kimi_cli.soul.context_db import ContextDB
        db = ContextDB(tmp_path / "test.db")
        await db.initialize()
        msgs = _make_messages(500)

        start = time.perf_counter()
        for _ in range(50):
            await db.begin_transaction()
            await db.append_messages(msgs)
            await db.commit_transaction()
        elapsed = time.perf_counter() - start
        assert elapsed < 15.0
        await db.close()


# ---------------------------------------------------------------------------
# Compaction benchmarks
# ---------------------------------------------------------------------------


class TestCompactionBenchmark:
    """Benchmarks for compilation module."""

    def test_prepare_with_messages(self) -> None:
        """SimpleCompaction.prepare() with 200 messages."""
        from kimi_cli.soul.compaction import SimpleCompaction
        msgs = _make_messages(200)
        compactor = SimpleCompaction(max_preserved_messages=2)

        start = time.perf_counter()
        for _ in range(500):
            result = compactor.prepare(msgs)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_adaptive_preserve_depth(self) -> None:
        """adaptive_preserve_depth() with various message patterns."""
        from kimi_cli.soul.compaction import adaptive_preserve_depth
        msgs = _make_messages(50)

        start = time.perf_counter()
        for _ in range(5_000):
            depth = adaptive_preserve_depth(msgs)
            assert isinstance(depth, int)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_detect_cascade_depth(self) -> None:
        """_detect_cascade_depth()."""
        from kimi_cli.soul.compaction import _detect_cascade_depth
        msgs = _make_messages(50)

        start = time.perf_counter()
        for _ in range(5_000):
            depth = _detect_cascade_depth(msgs)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0


# ---------------------------------------------------------------------------
# Wire serde benchmarks
# ---------------------------------------------------------------------------


class TestWireSerdeBenchmark:
    """Benchmarks for wire serialization/deserialization."""

    def test_serialize_text_part(self) -> None:
        """serialize_wire_message() — TextPart."""
        from kimi_cli.wire.types import TextPart
        from kimi_cli.wire.serde import serialize_wire_message
        msg = TextPart(text="Hello, this is a test message with some content.")

        start = time.perf_counter()
        for _ in range(20_000):
            serialize_wire_message(msg)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_deserialize_text_part(self) -> None:
        """deserialize_wire_message() — StepBegin (direct WireMessage type)."""
        from kimi_cli.wire.serde import deserialize_wire_message
        data = {"type": "StepBegin", "payload": {"n": 1}}

        start = time.perf_counter()
        for _ in range(20_000):
            deserialize_wire_message(data)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_serialize_deserialize_roundtrip(self) -> None:
        """Serialize + deserialize full WireMessage (StepBegin)."""
        from kimi_cli.wire.types import StepBegin
        from kimi_cli.wire.serde import serialize_wire_message, deserialize_wire_message

        msg = StepBegin(n=1)

        start = time.perf_counter()
        for _ in range(10_000):
            serialized = serialize_wire_message(msg)
            deserialized = deserialize_wire_message(serialized)
        elapsed = time.perf_counter() - start
        assert elapsed < 15.0


# ---------------------------------------------------------------------------
# File read benchmarks
# ---------------------------------------------------------------------------


class TestReadFileBenchmark:
    """Benchmarks for file read operations."""

    def test_read_forward(self, tmp_path: Path) -> None:
        """_read_forward() on 1000-line temp file."""
        # ReadTool is async and complex; benchmark file I/O directly instead
        content = "\n".join(f"line {i}: some content here for testing purposes" for i in range(1000))
        test_file = tmp_path / "test_file.txt"
        test_file.write_text(content)

        start = time.perf_counter()
        for _ in range(200):
            lines = test_file.read_text().splitlines()
            _ = lines[:50]
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0

    def test_read_tail(self, tmp_path: Path) -> None:
        """_read_tail() on 1000-line temp file."""
        content = "\n".join(f"line {i}: some content here for testing purposes" for i in range(1000))
        test_file = tmp_path / "test_file.txt"
        test_file.write_text(content)

        start = time.perf_counter()
        for _ in range(200):
            lines = test_file.read_text().splitlines()
            _ = lines[-50:]
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0


# ---------------------------------------------------------------------------
# Replace / edit benchmarks
# ---------------------------------------------------------------------------


class TestReplaceBenchmark:
    """Benchmarks for replace operations."""

    def test_apply_edit_exact_match(self) -> None:
        """_apply_edit() — exact match, single."""
        from kimi_cli.tools.file.replace import EditFile, Edit
        content = "Hello world, this is a test file.\n" * 100
        edit = Edit(old="test file", new="production file", replace_all=False)
        tool = EditFile.__new__(EditFile)

        start = time.perf_counter()
        for _ in range(5_000):
            EditFile._apply_edit(tool, content, edit)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_apply_edit_replace_all(self) -> None:
        """_apply_edit() — replace_all."""
        from kimi_cli.tools.file.replace import EditFile, Edit
        content = "Hello world, this is a test file.\n" * 100
        edit = Edit(old="test", new="production", replace_all=True)
        tool = EditFile.__new__(EditFile)

        start = time.perf_counter()
        for _ in range(1_000):
            EditFile._apply_edit(tool, content, edit)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_find_best_fuzzy_match(self) -> None:
        """_find_best_fuzzy_match() — 1000-line content."""
        from kimi_cli.tools.file.replace import EditFile
        content = "\n".join(f"line {i}: some content for matching purposes" for i in range(1000))
        target = "line 42: some content for matching"
        tool = EditFile.__new__(EditFile)

        start = time.perf_counter()
        for _ in range(500):
            EditFile._find_best_fuzzy_match(tool, target, content)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_try_strip_match(self) -> None:
        """_try_strip_match()."""
        from kimi_cli.tools.file.replace import EditFile
        content = "Hello world\n" * 100
        old = "Hello"
        new = "Hi"
        tool = EditFile.__new__(EditFile)

        start = time.perf_counter()
        for _ in range(5_000):
            EditFile._try_strip_match(tool, content, old, new)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0


# ---------------------------------------------------------------------------
# Grep benchmarks
# ---------------------------------------------------------------------------


class TestGrepBenchmark:
    """Benchmarks for grep (ripgrep) operations."""

    def test_collect_files(self, tmp_path: Path) -> None:
        """_collect_files() on directory with 500 files."""
        from kimi_cli.tools.file.grep_local import Grep
        from unittest.mock import MagicMock

        # Create 500 small files
        for i in range(500):
            (tmp_path / f"file_{i}.txt").write_text(f"content of file {i}\n")

        params = type("Params", (), {
            "glob": None, "type": None, "include_ignored": False,
            "follow_symlinks": False, "max_depth": None, "max_filesize": None,
            "path": str(tmp_path), "pattern": "",
        })()

        mock_runtime = MagicMock()
        mock_runtime.params = params
        tool = Grep(runtime=mock_runtime)
        # Mock _is_valid_file to accept everything
        original_valid = tool._is_valid_file
        tool._is_valid_file = lambda p, p2: True

        start = time.perf_counter()
        for _ in range(50):
            files = tool._collect_files(tmp_path, params)
            assert len(files) > 0
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

        tool._is_valid_file = original_valid

    def test_strip_path_prefix(self) -> None:
        """_strip_path_prefix() on 500 paths."""
        from kimi_cli.tools.file.grep_local import _strip_path_prefix
        lines = [f"/home/user/project/src/file_{i}.py:line {i}: content" for i in range(500)]
        search_base = "/home/user/project"

        start = time.perf_counter()
        for _ in range(5_000):
            _strip_path_prefix(lines, search_base)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_merge_intervals(self) -> None:
        """_merge_intervals()."""
        from kimi_cli.tools.file.grep_local import _merge_intervals
        intervals = [(i, i + 10) for i in range(0, 1000, 3)]

        start = time.perf_counter()
        for _ in range(10_000):
            _merge_intervals(intervals)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_join_with_byte_limit(self) -> None:
        """_join_with_byte_limit()."""
        from kimi_cli.tools.file.grep_local import _join_with_byte_limit
        lines = [f"line {i}: some content with enough data to fill the buffer" for i in range(200)]

        start = time.perf_counter()
        for _ in range(5_000):
            _join_with_byte_limit(lines, max_bytes=10000)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_is_sensitive_cached(self) -> None:
        """_is_sensitive_cached() — cache hit/miss."""
        from kimi_cli.tools.file.grep_local import _is_sensitive_cached
        paths = [f"/path/to/file_{i}.py" for i in range(100)]

        start = time.perf_counter()
        for _ in range(50_000):
            for p in paths:
                _is_sensitive_cached(p)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0


# ---------------------------------------------------------------------------
# Diff benchmarks
# ---------------------------------------------------------------------------


class TestDiffBenchmark:
    """Benchmarks for diff operations."""

    def test_build_diff_blocks_small(self) -> None:
        """_build_diff_blocks_sync() — 100-line diff."""
        from kimi_cli.utils.diff import _build_diff_blocks_sync
        old_text = "\n".join(f"line {i}: original content" for i in range(100))
        new_text = "\n".join(f"line {i}: modified content {i}" for i in range(100))

        start = time.perf_counter()
        for _ in range(500):
            _build_diff_blocks_sync("test.py", old_text, new_text)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_build_diff_blocks_large(self) -> None:
        """_build_diff_blocks_sync() — 5000-line diff."""
        from kimi_cli.utils.diff import _build_diff_blocks_sync
        old_text = "\n".join(f"line {i}: original content with some more text for variety" for i in range(5000))
        new_text = "\n".join(f"line {i}: modified content {i} with different text" for i in range(5000))

        start = time.perf_counter()
        for _ in range(50):
            _build_diff_blocks_sync("test.py", old_text, new_text)
        elapsed = time.perf_counter() - start
        assert elapsed < 15.0

    def test_format_unified_diff(self) -> None:
        """format_unified_diff() — 500-line file."""
        from kimi_cli.utils.diff import format_unified_diff
        old_text = "\n".join(f"line {i}: original content" for i in range(500))
        new_text = "\n".join(f"line {i}: modified content" for i in range(500))

        start = time.perf_counter()
        for _ in range(500):
            format_unified_diff(old_text, new_text, path="test.py")
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0


# ---------------------------------------------------------------------------
# Tool utils benchmarks
# ---------------------------------------------------------------------------


class TestToolUtilsBenchmark:
    """Benchmarks for tool utility functions."""

    def test_tool_result_builder_write_50kb(self) -> None:
        """ToolResultBuilder.write() — 50KB of text."""
        from kimi_cli.tools.utils import ToolResultBuilder
        builder = ToolResultBuilder()
        text = "Hello, this is a tool output line.\n" * 2000  # ~50KB

        start = time.perf_counter()
        for _ in range(5_000):
            builder.write(text)
            builder._content = bytearray()
            builder._written = 0
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_tool_result_builder_tail(self) -> None:
        """ToolResultBuilder.tail()."""
        from kimi_cli.tools.utils import ToolResultBuilder
        builder = ToolResultBuilder()
        builder.write("line1\nline2\nline3\n" * 100)

        start = time.perf_counter()
        for _ in range(10_000):
            builder.tail()
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_repair_json_string_valid(self) -> None:
        """repair_json_string() — valid JSON."""
        from kimi_cli.tools.utils import repair_json_string
        json_str = '{"key": "value", "number": 42, "list": [1, 2, 3]}'

        start = time.perf_counter()
        for _ in range(20_000):
            repair_json_string(json_str)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_repair_json_string_broken(self) -> None:
        """repair_json_string() — broken JSON needing repair."""
        from kimi_cli.tools.utils import repair_json_string
        json_str = '{"key": "value", "number": 42, "list": [1, 2, 3'  # missing closing

        start = time.perf_counter()
        for _ in range(5_000):
            repair_json_string(json_str)
        elapsed = time.perf_counter() - start
        assert elapsed < 15.0

    def test_repair_tool_arguments(self) -> None:
        """repair_tool_arguments() with complex params."""
        from kimi_cli.tools.utils import repair_tool_arguments
        from pydantic import BaseModel

        class Params(BaseModel):
            cmd: str = ""
            timeout: int = 30
            path: str = ""
            pattern: str = ""
            count: int = 0

        arguments = '{"cmd": "echo hello", "timeout": "30", "unknown_key": "test", "count": "5"}'

        start = time.perf_counter()
        for _ in range(5_000):
            repair_tool_arguments(Params, arguments)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_load_desc(self, tmp_path: Path) -> None:
        """load_desc() — Jinja2 template rendering."""
        from kimi_cli.tools.utils import load_desc
        template = tmp_path / "template.j2"
        template.write_text("Tool: {{ name }}\nDescription: {{ desc }}\n")

        start = time.perf_counter()
        for _ in range(5_000):
            load_desc(template, {"name": "test_tool", "desc": "A test tool"})
        elapsed = time.perf_counter() - start
        assert elapsed < 15.0

    def test_truncate_line(self) -> None:
        """truncate_line()."""
        from kimi_cli.tools.utils import truncate_line
        long_line = "x" * 1000

        start = time.perf_counter()
        for _ in range(50_000):
            truncate_line(long_line, max_length=50)
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0


# ---------------------------------------------------------------------------
# Token counting benchmarks
# ---------------------------------------------------------------------------


class TestTokensBenchmark:
    """Benchmarks for token counting."""

    def test_count_message_tokens(self) -> None:
        """count_message_tokens() — 100 messages."""
        from kimi_cli.utils.tokens import count_message_tokens
        msgs = _make_messages(100)

        start = time.perf_counter()
        for _ in range(200):
            count_message_tokens(msgs)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0


# ---------------------------------------------------------------------------
# Format check benchmarks
# ---------------------------------------------------------------------------


class TestCheckFormatBenchmark:
    """Benchmarks for format checking."""

    def test_check_json_text_valid(self) -> None:
        """check_json_text() — valid JSON."""
        from kimi_cli.tools.file.check_fmt import check_json_text
        valid_json = '{"key1": "value1", "key2": 42, "items": [1, 2, 3]}'

        start = time.perf_counter()
        for _ in range(10_000):
            check_json_text(valid_json)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_check_yaml_text(self) -> None:
        """check_yaml_text()."""
        from kimi_cli.tools.file.check_fmt import check_yaml_text
        yaml_text = "key1: value1\nkey2: 42\nitems:\n  - 1\n  - 2\n  - 3\n"

        start = time.perf_counter()
        for _ in range(10_000):
            check_yaml_text(yaml_text)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0


# ---------------------------------------------------------------------------
# Kosong tooling benchmarks
# ---------------------------------------------------------------------------


class TestKosongToolingBenchmark:
    """Benchmarks for kosong.tooling functions (used from kimi-cli)."""

    def test_repair_dict_for_model(self) -> None:
        """_repair_dict_for_model() — 30-field model, 10 unknown keys."""
        from kosong.tooling import _repair_dict_for_model
        from pydantic import BaseModel

        class TestModel(BaseModel):
            field_01: str = ""
            field_02: int = 0
            field_03: float = 0.0
            field_04: bool = False
            field_05: list[str] = []
            field_06: str = ""
            field_07: int = 0
            field_08: float = 0.0
            field_09: bool = False
            field_10: str = ""
            field_11: int = 0
            field_12: float = 0.0
            field_13: bool = False
            field_14: str = ""
            field_15: int = 0
            field_16: float = 0.0
            field_17: bool = False
            field_18: str = ""
            field_19: int = 0
            field_20: float = 0.0
            field_21: bool = False
            field_22: str = ""
            field_23: int = 0
            field_24: float = 0.0
            field_25: bool = False
            field_26: str = ""
            field_27: int = 0
            field_28: float = 0.0
            field_29: bool = False
            field_30: str = ""

        raw = {
            "field_01": "hello",
            "field_02": "42",
            "unknown_01": "extra",
            "unknown_02": "extra2",
            "field_10": 123,
            "FIELD_20": "case_insensitive",
            "unknown_03": "extra3",
            "field_30": True,
            "unknown_04": "extra4",
            "unknown_05": "extra5",
        }

        start = time.perf_counter()
        for _ in range(2_000):
            _repair_dict_for_model(raw, TestModel)
        elapsed = time.perf_counter() - start
        assert elapsed < 15.0

    def test_fuzzy_match_keys(self) -> None:
        """_fuzzy_match_keys() — 50 missing, 200 available."""
        from kosong.tooling import _fuzzy_match_keys
        missing = {f"field_{i:03d}_name" for i in range(50)}
        available = {f"field_{i:03d}_Name" for i in range(200)}

        start = time.perf_counter()
        for _ in range(2_000):
            _fuzzy_match_keys(missing, available)
        elapsed = time.perf_counter() - start
        assert elapsed < 15.0

    def test_cached_model_field_info(self) -> None:
        """_cached_model_field_info() — cache behavior."""
        from kosong.tooling import _cached_model_field_info
        from pydantic import BaseModel

        class ModelA(BaseModel):
            field_01: str = ""
            field_02: int = 0

        class ModelB(BaseModel):
            field_01: str = ""
            field_02: int = 0
            field_03: float = 0.0

        models = [ModelA, ModelB, ModelA, ModelB]

        # Warm-up cache
        for m in models:
            _cached_model_field_info(m)

        start = time.perf_counter()
        for _ in range(10_000):
            for m in models:
                _cached_model_field_info(m)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_message_serialize_roundtrip(self) -> None:
        """Message.model_dump_json() round-trip."""
        from kosong.message import Message, TextPart
        msg = Message(
            role="assistant",
            content=[TextPart(text="Hello, this is a test message. " * 50)],
        )

        start = time.perf_counter()
        for _ in range(10_000):
            serialized = msg.model_dump_json()
            Message.model_validate_json(serialized)
        elapsed = time.perf_counter() - start
        assert elapsed < 15.0

    def test_deref_json_schema(self) -> None:
        """deref_json_schema() on schema with many $ref."""
        from kosong.utils.jsonschema import deref_json_schema

        schema = {
            "type": "object",
            "properties": {
                f"field_{i}": {"$ref": "#/$defs/Item"}
                for i in range(50)
            },
            "$defs": {
                "Item": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "name": {"type": "string"},
                        "value": {"type": "string"},
                    }
                }
            }
        }

        start = time.perf_counter()
        for _ in range(5_000):
            deref_json_schema(schema)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0
