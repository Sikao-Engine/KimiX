from __future__ import annotations

import asyncio
from pathlib import Path

import orjson

from kimi_cli.soul.jsonl_io import (
    JsonlAppender,
    encode_jsonl_record,
    iter_jsonl_records,
    read_jsonl_records,
)


def test_encode_jsonl_record_is_dense_utf8_with_one_lf() -> None:
    encoded = encode_jsonl_record({"role": "user", "content": "中文\n\t "})
    assert encoded == '{"role":"user","content":"中文\\n\\t "}\n'.encode()
    assert orjson.loads(encoded)["content"] == "中文\n\t "


def test_appender_batches_independent_lines_and_preserves_prefix(tmp_path: Path) -> None:
    path = tmp_path / "records.jsonl"
    appender = JsonlAppender(path)
    appender.append({"id": 1, "text": "first"})
    prefix = path.read_bytes()
    appender.append_many([{"id": 2, "text": "第二"}, {"id": 3, "text": "third"}])
    appender.save()
    appender.close()
    appender.close()

    data = path.read_bytes()
    assert data.startswith(prefix)
    assert data.splitlines() == [
        b'{"id":1,"text":"first"}',
        '{"id":2,"text":"第二"}'.encode(),
        b'{"id":3,"text":"third"}',
    ]
    assert [r["id"] for r in read_jsonl_records(path)] == [1, 2, 3]


def test_appender_separates_an_incomplete_existing_tail(tmp_path: Path) -> None:
    path = tmp_path / "records.jsonl"
    original = b'{"id":1}\n{"id":'
    path.write_bytes(original)
    appender = JsonlAppender(path)
    appender.append({"id": 2})
    appender.close()

    assert path.read_bytes().startswith(original)
    assert [r["id"] for r in read_jsonl_records(path)] == [1, 2]


def test_reader_skips_malformed_interior_and_supports_relaxed_legacy(
    tmp_path: Path,
) -> None:
    path = tmp_path / "records.jsonl"
    path.write_bytes(
        b'{"id":1}\n'
        b'not json\n'
        b'{"id":2,}\n'
        + '{"id":3,"text":"中"}\n'.encode()
        + b'{"id":4,"text":"\xe4\xb8'
    )
    records = list(iter_jsonl_records(path))
    assert [(line, record.get("id")) for line, record in records] == [(1, 1), (3, 2), (4, 3)]


def test_reader_accepts_truncated_utf8_without_touching_file(tmp_path: Path) -> None:
    path = tmp_path / "records.jsonl"
    original = b'{"id":1}\n{"id":2,"text":"\xe4\xb8\n'
    path.write_bytes(original)
    assert [record["id"] for record in read_jsonl_records(path)] == [1]
    assert path.read_bytes() == original


def test_appender_can_be_reused_across_successive_event_loops(tmp_path: Path) -> None:
    path = tmp_path / "records.jsonl"
    appender = JsonlAppender(path)

    async def write(value: int) -> None:
        await asyncio.to_thread(appender.append, {"id": value})

    asyncio.run(write(1))
    asyncio.run(write(2))
    appender.close()
    assert [record["id"] for record in read_jsonl_records(path)] == [1, 2]


def test_copy_reader_tolerates_only_an_incomplete_final_record(tmp_path: Path) -> None:
    from kimi_cli.soul.jsonl_io import read_jsonl_records_for_copy

    path = tmp_path / "copy.jsonl"
    path.write_bytes(b'{"id":1}\n{"id":')
    assert read_jsonl_records_for_copy(path) == [{"id": 1}]

    path.write_bytes(b'{"id":1}\nnot json\n{"id":2}\n')
    try:
        read_jsonl_records_for_copy(path)
    except ValueError as exc:
        assert "line 2" in str(exc)
    else:
        raise AssertionError("malformed interior record was silently accepted")


def test_copy_reader_rejects_a_malformed_complete_final_record(tmp_path: Path) -> None:
    from kimi_cli.soul.jsonl_io import read_jsonl_records_for_copy

    path = tmp_path / "copy.jsonl"
    path.write_bytes(b'{"id":1}\n{"id":2} trailing')
    try:
        read_jsonl_records_for_copy(path)
    except ValueError:
        pass
    else:
        raise AssertionError("malformed complete final record was accepted")
