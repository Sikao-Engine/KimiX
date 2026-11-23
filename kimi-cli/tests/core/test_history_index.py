"""Append-only JSONL HistoryIndex correctness and instrumentation tests."""

from __future__ import annotations

from pathlib import Path

import orjson
from kosong.message import Message

from kimi_cli.soul.history_index import HistoryIndex
from kimi_cli.soul import jsonl_search
from kimi_cli.wire.types import TextPart


def _msg(role: str, text: str) -> Message:
    return Message(role=role, content=[TextPart(text=text)])


def _records(path: Path) -> list[dict]:
    return [orjson.loads(line) for line in path.read_bytes().splitlines() if line.strip()]


def test_in_memory_search_and_role_filtering() -> None:
    idx = HistoryIndex()
    assert idx.search("anything") == []
    idx.index_messages(
        [
            Message(role="system", content=[TextPart(text="not indexed")]),
            _msg("user", "How do I compile Python?"),
            _msg("assistant", "Use pyinstaller."),
            _msg("tool", "compiler finished"),
        ]
    )
    assert [turn["role"] for turn in idx._turns] == ["user", "assistant", "tool"]
    results = idx.search("compile Python", top_k=2)
    assert len(results) == 1
    assert results[0]["role"] == "user"
    idx.close()


def test_empty_text_is_not_indexed() -> None:
    idx = HistoryIndex()
    idx.index_messages([_msg("user", " "), _msg("assistant", "")])
    assert idx._turns == []


def test_no_500_turn_cap_in_memory_or_durable(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    idx = HistoryIndex(jsonl_path=path)
    assert idx.load() is False
    idx.index_messages([_msg("user", f"Message {i} about sqlite") for i in range(510)])
    assert len(idx._turns) == 510
    assert len(idx.search("sqlite", top_k=3)) == 3
    idx.close()

    reopened = HistoryIndex(jsonl_path=path)
    assert reopened.load() is True
    assert len(reopened._turns) == 510
    assert reopened.search("Message 0", top_k=1)[0]["turn_id"] == 0
    reopened.close()


def test_dense_records_and_prefix_survive_save_close(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    idx = HistoryIndex(persist_path=path)
    idx.index_messages([_msg("user", "中文\nCaseSensitive"), _msg("assistant", "answer")])
    prefix = path.read_bytes()
    idx.save()
    idx.close()
    idx.close()
    assert path.read_bytes() == prefix
    lines = path.read_bytes().splitlines()
    assert len(lines) == 2
    for line in lines:
        assert line == orjson.dumps(orjson.loads(line))
        record = orjson.loads(line)
        assert set(record) == {"turn_id", "role", "text", "timestamp", "is_compacted"}
    assert lines[0].find("中文".encode()) >= 0


def test_compaction_markers_replay_before_and_after_new_appends(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    idx = HistoryIndex(jsonl_path=path)
    idx.index_messages([_msg("user", "first python")])
    idx.mark_compacted()
    first_boundary = path.read_bytes()
    idx.index_messages([_msg("assistant", "second python")])
    idx.mark_compacted()
    idx.index_messages([_msg("tool", "third python")])
    idx.close()
    assert path.read_bytes().startswith(first_boundary)

    reopened = HistoryIndex(jsonl_path=path)
    reopened.load()
    assert [turn["is_compacted"] for turn in reopened._turns] == [True, True, False]
    assert [r["is_compacted"] for r in reopened.search("python", top_k=3)] == [True, True, False]
    assert [r["role"] for r in _records(path) if r["role"] == "_compacted"] == ["_compacted", "_compacted"]
    reopened.close()


def test_clear_appends_reset_and_allows_repeated_ids_across_generations(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    idx = HistoryIndex(jsonl_path=path)
    idx.index_messages([_msg("user", "old")])
    original = path.read_bytes()
    idx.clear()
    assert path.exists()
    assert path.read_bytes() == original + b'{"role":"_reset"}\n'
    assert idx._turns == []
    idx.index_messages([_msg("user", "fresh")])
    assert idx._turns[0]["turn_id"] == 0
    idx.close()

    reopened = HistoryIndex(jsonl_path=path)
    reopened.load()
    assert len(reopened._turns) == 1
    assert reopened._turns[0]["text"] == "fresh"
    assert reopened._turns[0]["turn_id"] == 0
    assert reopened.search("old") == []
    reopened.clear()
    reopened.index_messages([_msg("user", "third")])
    reopened.close()
    third = HistoryIndex(jsonl_path=path)
    third.load()
    assert [turn["text"] for turn in third._turns] == ["third"]
    third.close()


def test_legacy_whole_file_json_migration_preserves_turns(tmp_path: Path) -> None:
    legacy = tmp_path / "history_index" / "sess.json"
    legacy.parent.mkdir(parents=True)
    turns = [
        {"turn_id": 4, "role": "user", "text": "Old question", "timestamp": 100.0, "is_compacted": False},
        {"turn_id": 7, "role": "assistant", "text": "Old answer", "timestamp": 101.0, "is_compacted": True},
    ]
    legacy.write_bytes(orjson.dumps({"doc_id_counter": 8, "turns": turns}))
    path = tmp_path / "history.jsonl"
    idx = HistoryIndex(jsonl_path=path, legacy_json_path=legacy)
    assert idx.load() is True
    assert idx._turns == turns
    assert not legacy.exists()
    assert legacy.with_suffix(".json.bak").exists()
    assert idx.search("old question", top_k=1)[0]["turn_id"] == 4
    idx.index_messages([_msg("user", "next")])
    assert idx._turns[-1]["turn_id"] == 8
    idx.close()


def test_legacy_empty_json_migration(tmp_path: Path) -> None:
    legacy = tmp_path / "old.json"
    legacy.write_bytes(orjson.dumps({"turns": []}))
    path = tmp_path / "history.jsonl"
    idx = HistoryIndex(jsonl_path=path, legacy_json_path=legacy)
    assert idx.load() is True
    assert path.exists()
    assert idx._turns == []
    idx.close()


def test_existing_jsonl_takes_precedence_over_legacy_json(tmp_path: Path) -> None:
    legacy = tmp_path / "old.json"
    legacy.write_bytes(orjson.dumps({"turns": [{"turn_id": 9, "role": "user", "text": "old", "timestamp": 1.0}]}))
    path = tmp_path / "history.jsonl"
    path.write_bytes(orjson.dumps({"turn_id": 0, "role": "user", "text": "canonical", "timestamp": 2.0, "is_compacted": False}, option=orjson.OPT_APPEND_NEWLINE))
    idx = HistoryIndex(jsonl_path=path, legacy_json_path=legacy)
    assert idx.load() is True
    assert [turn["text"] for turn in idx._turns] == ["canonical"]
    assert legacy.exists()
    idx.close()


def test_malformed_tail_and_interior_records_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    path.write_bytes(
        orjson.dumps({"turn_id": 0, "role": "user", "text": "before", "timestamp": 1.0, "is_compacted": False}, option=orjson.OPT_APPEND_NEWLINE)
        + b'not json\n'
        + orjson.dumps({"turn_id": 1, "role": "assistant", "text": "after", "timestamp": 2.0, "is_compacted": False}, option=orjson.OPT_APPEND_NEWLINE)
        + b'{"turn_id":2,"role":"user","text":'
    )
    original = path.read_bytes()
    idx = HistoryIndex(jsonl_path=path)
    assert idx.load() is True
    assert [turn["text"] for turn in idx._turns] == ["before", "after"]
    assert path.read_bytes() == original
    idx.index_messages([_msg("user", "new")])
    assert path.read_bytes().startswith(original)
    assert idx._turns[-1]["turn_id"] == 2
    idx.close()


def test_verbatim_case_and_cjk_short_literal_search(tmp_path: Path) -> None:
    idx = HistoryIndex(jsonl_path=tmp_path / "history.jsonl")
    idx.index_messages([_msg("user", "The Exact Original 50% 日本語"), _msg("assistant", "other")])
    assert idx.search("exact original")[0]["text"] == "The Exact Original 50% 日本語"
    assert idx.search("日本語")[0]["turn_id"] == 0
    assert idx.search("日")[0]["turn_id"] == 0
    assert idx.search("50%")[0]["turn_id"] == 0
    idx.close()


def test_warm_search_performs_no_file_open_or_read(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "history.jsonl"
    idx = HistoryIndex(jsonl_path=path)
    idx.index_messages([_msg("user", "warm python search")])
    idx.load()

    def forbidden(*args, **kwargs):
        raise AssertionError("warm search touched the filesystem")

    monkeypatch.setattr(Path, "open", forbidden)
    assert idx.search("python", top_k=1)[0]["text"] == "warm python search"
    assert idx.search("p", top_k=1) == []  # Latin terms remain exact token matches
    idx.close()


def test_incremental_append_does_not_rebuild_old_postings(tmp_path: Path, monkeypatch) -> None:
    idx = HistoryIndex(jsonl_path=tmp_path / "history.jsonl")
    idx.index_messages([_msg("user", "old python"), _msg("assistant", "other answer")])
    old_postings = idx._engine._postings["python"]
    calls: list[str] = []
    real_tokenize = jsonl_search.tokenize

    def recording_tokenize(text: str) -> list[str]:
        calls.append(text)
        return real_tokenize(text)

    monkeypatch.setattr(jsonl_search, "tokenize", recording_tokenize)
    idx.index_messages([_msg("user", "new python")])
    assert calls == ["new python"]
    assert idx._engine._postings["python"] is old_postings
    assert old_postings[0] == 1
    assert idx._engine.add_turn_calls == 3
    idx.close()


def test_missing_file_contract_and_in_memory_clear() -> None:
    idx = HistoryIndex()
    assert idx.load() is False
    idx.index_messages([_msg("user", "temporary")])
    idx.clear()
    assert idx._turns == []
    idx.index_messages([_msg("user", "fresh")])
    assert idx._turns[0]["turn_id"] == 0
