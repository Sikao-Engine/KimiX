from __future__ import annotations

import pytest

from kimi_cli.soul.jsonl_search import (
    InvertedSearchIndex,
    contains_cjk,
    extract_text_from_content,
    make_snippet,
    message_text,
    normalize_text,
    tokenize,
)
from kimi_cli.wire.types import TextPart
from kosong.message import Message


def _turn(turn_id: int, text: str, role: str = "user") -> dict:
    return {
        "turn_id": turn_id,
        "role": role,
        "text": text,
        "timestamp": 100.0 + turn_id,
        "is_compacted": False,
    }


def test_extract_text_from_all_supported_content_shapes() -> None:
    assert extract_text_from_content("hello") == "hello"
    assert extract_text_from_content([TextPart(text="a"), TextPart(text="b")]) == "a\nb"
    assert extract_text_from_content([{"type": "text", "text": "a"}, "b"]) == "a\nb"
    assert extract_text_from_content(["plain", {"type": "image", "url": "x"}, {"text": "tail"}]) == "plain\ntail"
    assert extract_text_from_content({"type": "image", "url": "x"}) == ""
    assert extract_text_from_content(None) == ""
    assert message_text(Message(role="user", content=[{"type": "text", "text": "question"}])) == "question"


def test_normalize_and_tokenize_latin_code_and_cjk() -> None:
    assert normalize_text("ＡＢＣ") == "abc"
    assert tokenize("Compile my_app 2.0") == ["compile", "my_app", "2", "0"]
    assert tokenize("关于Python安装") == ["关于", "python", "安装"]
    assert tokenize("日本語の検索") == ["日本", "本語", "語の", "の検", "検索"]
    assert contains_cjk("中文")
    assert not contains_cjk("plain")


def test_latin_query_uses_and_semantics_and_verbatim_results() -> None:
    index = InvertedSearchIndex()
    index.add_turn(_turn(0, "How do I compile Python?"))
    index.add_turn(_turn(1, "Use pyinstaller or cx_Freeze.", role="assistant"))
    results = index.search("compile Python", top_k=2)
    assert len(results) == 1
    assert results[0]["text"] == "How do I compile Python?"
    assert results[0]["score"] > 0
    assert results[0]["is_compacted"] is False


def test_bm25_ranks_relevant_documents_and_ties_deterministically() -> None:
    index = InvertedSearchIndex()
    index.add_turn(_turn(0, "python python python"))
    index.add_turn(_turn(1, "python with a much longer explanation about packaging and deployment"))
    index.add_turn(_turn(2, "python python python"))
    results = index.search("python", top_k=3)
    assert [r["turn_id"] for r in results] == [0, 2, 1]
    assert results[0]["score"] == pytest.approx(results[1]["score"])


def test_cjk_two_three_and_one_character_queries() -> None:
    index = InvertedSearchIndex()
    index.add_turn(_turn(0, "关于Python安装和配置"))
    index.add_turn(_turn(1, "日本語の検索テストです", role="assistant"))
    assert any("日本語" in r["text"] for r in index.search("日本語"))
    assert any("配置" in r["text"] for r in index.search("配置"))
    assert any("日本語" in r["text"] for r in index.search("日"))
    assert index.fallback_search_calls == 1


def test_short_mixed_cjk_uses_literal_fallback() -> None:
    index = InvertedSearchIndex()
    index.add_turn(_turn(0, "日本語x中"))
    index.add_turn(_turn(1, "日本語だけ"))
    assert [r["turn_id"] for r in index.search("日本語x中")] == [0]


@pytest.mark.parametrize(
    ("query", "matching", "other"),
    [
        ("50%", "progress is 50% done", "progress is 50 done"),
        ('"hello"', 'say "hello" exactly', "say hello exactly"),
        ("a_b", "path a_b literal", "path a-b instead"),
        ("AND", "hello AND goodbye", "hello or goodbye"),
        ("TODO: fix", "TODO: fix this", "TODO fix this"),
    ],
)
def test_punctuation_and_boolean_words_are_literal(query: str, matching: str, other: str) -> None:
    index = InvertedSearchIndex()
    index.add_turn(_turn(0, other))
    index.add_turn(_turn(1, matching))
    results = index.search(query, top_k=3)
    assert [r["turn_id"] for r in results] == [1]


def test_empty_queries_and_nonpositive_top_k() -> None:
    index = InvertedSearchIndex()
    index.add_turn(_turn(0, "hello"))
    assert index.search("") == []
    assert index.search("   ") == []
    assert index.search("hello", top_k=0) == []
    assert index.search("hello", top_k=-1) == []


def test_compaction_and_incremental_updates() -> None:
    index = InvertedSearchIndex()
    index.add_turn(_turn(0, "first python"))
    index.mark_compacted(0)
    index.add_turn(_turn(1, "second python"))
    results = index.search("python", top_k=2)
    assert [r["is_compacted"] for r in results] == [True, False]
    assert index.add_turn_calls == 2


def test_make_snippet_marks_match_and_bounds_context() -> None:
    snippet = make_snippet("prefix " + "x" * 100 + " NEEDLE " + "y" * 100, "needle")
    assert ">>>needle<<<" in snippet
    assert snippet.startswith("...")
    assert snippet.endswith("...")
    assert make_snippet("short", "missing") == "short"
