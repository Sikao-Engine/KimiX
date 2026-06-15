"""Tests for Session.export() on the CLI Session dataclass."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from kaos.path import KaosPath

from kimi_cli.session import Session
from kimi_cli.soul.context_records import ExportedContext
from kimi_cli.wire.types import TextPart


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


def _write_context_records(context_file: Path, *records: dict[str, object]) -> None:
    context_file.parent.mkdir(parents=True, exist_ok=True)
    context_file.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


async def test_export_empty_context(isolated_share_dir: Path, work_dir: KaosPath) -> None:
    session = await Session.create(work_dir)
    result = await session.export()

    assert isinstance(result, ExportedContext)
    assert result.system_prompt is None
    assert result.messages == []
    assert result.checkpoints == []
    assert result.usages == []


async def test_export_mixed_records(isolated_share_dir: Path, work_dir: KaosPath) -> None:
    session = await Session.create(work_dir)
    _write_context_records(
        session.context_file,
        {"role": "_system_prompt", "content": "You are a helpful assistant."},
        {"role": "user", "content": [TextPart(text="Hello").model_dump()]},
        {"role": "assistant", "content": [TextPart(text="Hi there!").model_dump()]},
        {"role": "_usage", "token_count": 42},
        {"role": "_checkpoint", "id": 0},
        {"role": "user", "content": [TextPart(text="How are you?").model_dump()]},
    )

    result = await session.export()

    assert isinstance(result, ExportedContext)
    assert result.system_prompt == "You are a helpful assistant."
    assert len(result.messages) == 3
    assert result.messages[0].role == "user"
    assert result.messages[0].extract_text() == "Hello"
    assert result.messages[1].role == "assistant"
    assert result.messages[1].extract_text() == "Hi there!"
    assert result.messages[2].role == "user"
    assert result.messages[2].extract_text() == "How are you?"
    assert result.usages == [42]
    assert result.checkpoints == [0]


async def test_export_skips_invalid_lines(isolated_share_dir: Path, work_dir: KaosPath) -> None:
    session = await Session.create(work_dir)
    session.context_file.write_text(
        '{"role": "_system_prompt", "content": "valid"}\n'
        "not json\n"
        '{"role": "_usage", "token_count": 10}\n'
        '{"invalid": "no role"}\n'
        '{"role": "_checkpoint", "id": 1}\n',
        encoding="utf-8",
    )

    result = await session.export()

    assert result.system_prompt == "valid"
    assert result.usages == [10]
    assert result.checkpoints == [1]
    assert result.messages == []


async def test_export_skips_invalid_record_shapes(isolated_share_dir: Path, work_dir: KaosPath) -> None:
    session = await Session.create(work_dir)
    _write_context_records(
        session.context_file,
        {"role": "_system_prompt", "content": "valid prompt"},
        {"role": "_system_prompt"},
        {"role": "_usage", "token_count": 5},
        {"role": "_usage", "token_count": "not an int"},
        {"role": "_checkpoint", "id": 0},
        {"role": "_checkpoint", "id": "not an int"},
    )

    result = await session.export()

    assert result.system_prompt == "valid prompt"
    assert result.usages == [5]
    assert result.checkpoints == [0]


async def test_export_on_found_session(isolated_share_dir: Path, work_dir: KaosPath) -> None:
    session = await Session.create(work_dir)
    _write_context_records(
        session.context_file,
        {"role": "_system_prompt", "content": "found session"},
        {"role": "user", "content": [TextPart(text="hello").model_dump()]},
    )

    found = await Session.find(work_dir, session.id)
    assert found is not None
    result = await found.export()

    assert result.system_prompt == "found session"
    assert len(result.messages) == 1
