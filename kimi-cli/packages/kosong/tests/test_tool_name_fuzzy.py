"""Unit tests for tool-name fuzzy matching and resolution.

Covers ``normalize_tool_name``, ``fuzzy_match_tool_name``, ``resolve_tool_name``
and the ``ToolNameResolution`` result type in ``kosong.tooling``.
"""

import pytest

from kosong.tooling import (
    ToolNameResolution,
    fuzzy_match_tool_name,
    normalize_tool_name,
    resolve_tool_name,
)

# Canonical CamelCase builtins used across the tests.
VALID = [
    "WriteFile",
    "ReadFile",
    "ReadMediaFile",
    "SearchWeb",
    "Grep",
    "Shell",
    "LongNamedTool",
]


# ---------------------------------------------------------------------------
# normalize_tool_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spelling",
    [
        "write_file",
        "write-file",
        "WRITE_FILE",
        "Write-File",
        "Write_File",
        "WriteFile",
        "writefile",
    ],
)
def test_normalize_collapses_separators_and_case(spelling: str):
    assert normalize_tool_name(spelling) == "writefile"


def test_normalize_multi_word():
    assert normalize_tool_name("read_media_file") == "readmediafile"
    assert normalize_tool_name("READ-MEDIA-FILE") == "readmediafile"
    assert normalize_tool_name("ReadMediaFile") == "readmediafile"


# ---------------------------------------------------------------------------
# fuzzy_match_tool_name — separator-insensitive normalized exact
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sent,expected",
    [
        ("write_file", "WriteFile"),
        ("write-file", "WriteFile"),
        ("WRITE_FILE", "WriteFile"),
        ("Write_File", "WriteFile"),
        ("write-File", "WriteFile"),
        ("read_media_file", "ReadMediaFile"),
        ("search-web", "SearchWeb"),
    ],
)
def test_fuzzy_normalized_exact(sent: str, expected: str):
    # Even with the restrictive auto-correct cutoff, a normalized-exact match
    # is returned because it is high confidence (independent of *cutoff*).
    assert fuzzy_match_tool_name(sent, VALID, n=1, cutoff=0.75) == [expected]


# ---------------------------------------------------------------------------
# fuzzy_match_tool_name — case-insensitive exact
# ---------------------------------------------------------------------------


def test_fuzzy_case_insensitive_exact_concatenated():
    assert fuzzy_match_tool_name("writefile", VALID, n=1) == ["WriteFile"]


@pytest.mark.parametrize("sent", ["shell", "SHELL", "Shell"])
def test_fuzzy_case_insensitive_exact_single_word(sent: str):
    assert fuzzy_match_tool_name(sent, VALID, n=1) == ["Shell"]


# ---------------------------------------------------------------------------
# fuzzy_match_tool_name — normalized fuzzy (separator + typo)
# ---------------------------------------------------------------------------


def test_fuzzy_snake_typo_autocorrect_cutoff():
    # normalize("wrte_file") == "wrtefile"  (~0.94 vs "writefile")
    assert fuzzy_match_tool_name("wrte_file", VALID, n=1, cutoff=0.75) == ["WriteFile"]


def test_fuzzy_plain_typo_still_matches():
    # Normalization is a no-op for already-CamelCase input, so plain typos
    # continue to resolve through the same fuzzy path.
    assert fuzzy_match_tool_name("LongNamedTol", VALID, n=1, cutoff=0.75) == ["LongNamedTool"]


# ---------------------------------------------------------------------------
# fuzzy_match_tool_name — guards and non-matches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("short", ["rm", "ls", "x", ""])
def test_fuzzy_short_name_guard(short: str):
    assert fuzzy_match_tool_name(short, VALID) == []


def test_fuzzy_min_length_configurable():
    # "grep" (len 4) resolves by default (min_length=3) via case-insensitive exact.
    assert fuzzy_match_tool_name("grep", VALID, n=1) == ["Grep"]
    # Raising min_length above the query length suppresses matching entirely.
    assert fuzzy_match_tool_name("grep", VALID, n=1, min_length=5) == []


def test_fuzzy_no_match_returns_empty():
    assert fuzzy_match_tool_name("xyzzy", VALID) == []


def test_fuzzy_distant_snake_returns_empty():
    # Every normalized ratio for "totallyunrelated" is well below 0.5.
    assert fuzzy_match_tool_name("totally_unrelated", VALID) == []


# ---------------------------------------------------------------------------
# fuzzy_match_tool_name — determinism / ambiguity
# ---------------------------------------------------------------------------


def test_fuzzy_normalized_collision_is_deterministic():
    # "ReadFile" and "read_file" both normalize to "readfile" (a genuine
    # collision). "Read-File" is *not* a case-insensitive exact match to
    # either (it has a "-"), so resolution goes through the normalized-exact
    # branch and must rank deterministically.
    collide = ["ReadFile", "read_file"]
    result_a = fuzzy_match_tool_name("Read-File", collide, n=3, cutoff=0.75)
    result_b = fuzzy_match_tool_name("Read-File", list(reversed(collide)), n=3, cutoff=0.75)
    assert result_a == result_b == ["ReadFile", "read_file"]
    # The n=1 (auto-correct) call yields a stable single winner.
    assert fuzzy_match_tool_name("Read-File", collide, n=1, cutoff=0.75) == ["ReadFile"]


# ---------------------------------------------------------------------------
# fuzzy_match_tool_name — n limit and cutoff boundary
# ---------------------------------------------------------------------------


def test_fuzzy_n_limit():
    # "TollX" is ~0.6 similar to both ToolA and ToolB.
    names = ["ToolA", "ToolB"]
    assert fuzzy_match_tool_name("TollX", names, n=1, cutoff=0.5) == ["ToolA"]
    assert fuzzy_match_tool_name("TollX", names, n=3, cutoff=0.5) == ["ToolA", "ToolB"]


def test_fuzzy_cutoff_boundary():
    names = ["ToolA", "ToolB"]
    # ratio == 0.6: included when cutoff <= 0.6, excluded when cutoff > 0.6.
    assert fuzzy_match_tool_name("TollX", names, n=3, cutoff=0.6) == ["ToolA", "ToolB"]
    assert fuzzy_match_tool_name("TollX", names, n=3, cutoff=0.61) == []


# ---------------------------------------------------------------------------
# resolve_tool_name / ToolNameResolution
# ---------------------------------------------------------------------------


def test_resolve_exact_hit_no_correction():
    r = resolve_tool_name("WriteFile", VALID)
    assert isinstance(r, ToolNameResolution)
    assert r.name == "WriteFile"
    assert r.original == "WriteFile"
    assert r.corrected is False
    assert r.suggestions == []


@pytest.mark.parametrize(
    "sent,expected",
    [
        ("write_file", "WriteFile"),
        ("write-file", "WriteFile"),
        ("WRITE_FILE", "WriteFile"),
        ("Write_File", "WriteFile"),
        ("read_media_file", "ReadMediaFile"),
        ("search-web", "SearchWeb"),
    ],
)
def test_resolve_autocorrects_separator_styles(sent: str, expected: str):
    r = resolve_tool_name(sent, VALID)
    assert r.name == expected
    assert r.original == sent
    assert r.corrected is True
    assert r.suggestions == []


def test_resolve_autocorrects_typo():
    r = resolve_tool_name("wrte_file", VALID)
    assert r.name == "WriteFile"
    assert r.corrected is True
    assert r.suggestions == []


def test_resolve_not_found_populates_suggestions():
    r = resolve_tool_name("TollX", ["ToolA", "ToolB"])
    assert r.name is None
    assert r.corrected is False
    assert set(r.suggestions) == {"ToolA", "ToolB"}


def test_resolve_not_found_no_suggestions_when_distant():
    r = resolve_tool_name("xyzzy", VALID)
    assert r.name is None
    assert r.corrected is False
    assert r.suggestions == []


def test_resolve_short_name_guard():
    r = resolve_tool_name("rm", VALID)
    assert r.name is None
    assert r.suggestions == []


def test_resolve_accepts_dict_keys_and_generators():
    # dict.keys() is an Iterable, not a Sequence — must be materialized safely.
    tool_dict = {name: object() for name in VALID}
    r = resolve_tool_name("write_file", tool_dict.keys())
    assert r.name == "WriteFile"
    assert r.corrected is True

    # A one-shot generator must not be exhausted by the internal ``in`` test.
    gen = (name for name in VALID)
    r2 = resolve_tool_name("read_file", gen)
    assert r2.name == "ReadFile"
    assert r2.corrected is True


def test_resolve_custom_cutoffs():
    # Lowering the auto-correct cutoff turns a ~0.6 suggestion into a correction.
    names = ["ToolA", "ToolB"]
    r = resolve_tool_name("TollX", names, auto_correct_cutoff=0.55)
    assert r.name == "ToolA"
    assert r.corrected is True
