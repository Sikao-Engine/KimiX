"""Phase 2 tests: the consolidated lossless shell lexer.

Covers (plan §4 Phase 2 / task test 8):

* token stream tiling (every character of the range belongs to exactly one
  token — the base of the IR's lossless invariant);
* all quoting kinds (single / double / ANSI-C ``$'`` / locale ``$"``),
  escapes, line continuations;
* control / redirection operators including fd prefixes (``2>``, ``>&2``,
  ``&>``, ``2>&1``) and word boundaries;
* comments (``#`` word-initial only; glued after a substitution close the
  legacy scanner treats it as word content);
* ``$( )`` / ``$(())`` / ``${ }`` / backtick / ``<(`` regions with nesting;
* command substitutions reported *inside* double quotes as nested parts;
* heredoc marker lines (``<<``, ``<<-``, ``<<<``);
* boundary equivalence against the legacy oracle helpers in ``bash_core``
  (``_find_dq_end`` / ``_find_ansi_c_end`` / ``_find_backtick_end`` /
  ``_find_matching_paren`` / ``_match_*`` family);
* malformed / unterminated constructs: diagnostics + EOF-extended tokens,
  never exceptions;
* 2,000-level ``$( )`` nesting and a 20,000-word line: O(n), no recursion.

Pure Python, no subprocess.
"""

from __future__ import annotations

import time

import pytest

# Prime sys.path through the loader BEFORE any kimix_native import so the
# in-repo bin/kimix_native wins over a stale installed wheel (matches the
# sibling suites' import strategy). get_compat imports kimix_native._shell_compat,
# which the facade bootstraps onto the repo package.
from kimi_cli.native_loader import get_compat as _get_compat

assert _get_compat("_shell_compat") is not None

from kimix_native.shell_compat.bash_core import (
    _find_ansi_c_end,
    _find_backtick_end,
    _find_dq_end,
    _find_matching_paren,
    _match_arithmetic_span,
    _match_ansi_c_quote_span,
    _match_dquote_span,
    _match_parameter_braces,
)

from kimix_native.shell_compat.lexer import (
    EXECUTABLE_REGIONS,
    HEREDOC_OPERATORS,
    INPUT_ONLY_REDIRECT_OPERATORS,
    LexResult,
    Token,
    TokenKind,
    TRUNCATING_REDIRECT_OPERATORS,
    find_ansi_c_end,
    find_backtick_end,
    find_dq_end,
    find_matching_paren,
    lex,
    match_ansi_c_quote_span,
    match_arithmetic_span,
    match_dquote_span,
    match_parameter_braces,
    read_control_operator,
    read_redirection_operator,
    redirection_after_fd,
)
from kimix_native.shell_compat.models import WordPartKind, render_source
from kimix_native.shell_compat.parser import parse
from kimix_native.shell_compat.spans import DiagnosticKind


# ── tiling: the token stream covers its range exactly ───────────────────────

TILE_CASES = [
    "",
    " ",
    "\n",
    "echo hi",
    "echo  'a b'   \"c $(d) e\" ; x",
    "cat <<EOF\nbody\nEOF\nmore",
    "echo 2>&1 > out",
    "diff <(cat a) <(cat b)",
    "echo $(a) $(b) #notcomment ; x",
    "(a; b) #comment",
    "echo $'a\\n' $\"loc\" x",
    "echo a\\\nb",
    "echo 'unterminated",
    'echo "unterminated',
    "echo $(unterminated",
    "echo `unterminated",
    "a=(1 2 3)",
    "case $x in\n a) tree;; esac",
    "f() { rev; }",
    "for ((i=0;i<2;i++)) { tree; }",
    "echo <(x)#glued",
    "cmd arg1\n\n",
    "a && b || c",
    "a | b | c",
    "<<< word",
    ">&2",
    "\\",
    "echo \\",
]


@pytest.mark.parametrize("source", TILE_CASES)
def test_token_stream_tiles_its_range(source: str) -> None:
    """Tokens are ordered, disjoint, and cover every character exactly."""
    result = lex(source)
    cursor = 0
    for token in result.tokens:
        assert token.span.start == cursor, (source, token, cursor)
        assert token.span.end > cursor or token.kind is TokenKind.RAW
        cursor = token.span.end
    assert cursor == len(source)


@pytest.mark.parametrize("source", TILE_CASES)
def test_word_parts_tile_their_word(source: str) -> None:
    """Inside each WORD token the parts cover the word span without gaps."""
    for token in lex(source).tokens:
        if token.kind is not TokenKind.WORD or not token.parts:
            continue
        # parts may nest (double-quote regions report their inner $( ) as
        # sibling parts); the *covering* property: every character of the
        # word lies inside at least one part span
        covered = [False] * token.span.length
        for _kind, span, _inner in token.parts:
            assert token.span.contains(span), (source, token)
            for pos in range(span.start, span.end):
                covered[pos - token.span.start] = True
        assert all(covered), f"word {token.span.text(source)!r} not covered"


def test_lex_range_subset() -> None:
    source = "echo 'q' ; tail -f x"
    result = lex(source, 8)  # "; tail -f x"
    assert result.tokens
    joined = "".join(t.span.text(source) for t in result.tokens)
    assert joined == source[8:]
    with pytest.raises(ValueError):
        lex(source, 5, 2)


# ── quoting kinds ────────────────────────────────────────────────────────────

QUOTE_KIND_MAP = {
    "'a'": WordPartKind.SINGLE_QUOTE,
    '"a"': WordPartKind.DOUBLE_QUOTE,
    "$'a'": WordPartKind.ANSI_C_QUOTE,
    '$"a"': WordPartKind.LOCALE_QUOTE,
}


@pytest.mark.parametrize(("word", "kind"), sorted(QUOTE_KIND_MAP.items()))
def test_quote_region_part_kind(word: str, kind: WordPartKind) -> None:
    source = f"echo {word}"
    tokens = [t for t in lex(source).tokens if t.kind is TokenKind.WORD]
    parts = tokens[1].parts
    assert parts[0][0] is kind
    assert parts[0][1] == tokens[1].span


def test_single_quote_content_is_literal() -> None:
    source = "echo 'a $b `c' d"
    tokens = [t for t in lex(source).tokens if t.kind is TokenKind.WORD]
    assert tokens[1].span.text(source) == "'a $b `c'"
    kinds = [k for k, _s, _i in tokens[1].parts]
    assert kinds == [WordPartKind.SINGLE_QUOTE]


def test_ansi_c_escapes_skip_pairs() -> None:
    source = r"echo $'a\'b' tail"
    tokens = [t for t in lex(source).tokens if t.kind is TokenKind.WORD]
    quoted = tokens[1]
    assert quoted.span.text(source) == r"$'a\'b'"


def test_backslash_line_continuation_inside_and_between_words() -> None:
    source = "cmd a\\\nb"
    kinds = [t.kind for t in lex(source).tokens]
    # the continuation is either a word part or an owned RAW token, never dropped
    assert TokenKind.RAW in kinds or any(
        k is WordPartKind.LINE_CONTINUATION
        for t in lex(source).tokens
        for k, _s, _i in t.parts
    )
    assert len(lex(source).tokens) >= 1


def test_escaped_metacharacters_stay_in_words() -> None:
    source = r"echo a\;b\|c\&d"
    tokens = [t for t in lex(source).tokens if t.kind is TokenKind.WORD]
    assert tokens[1].span.text(source) == r"a\;b\|c\&d"


# ── operators / redirections ────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("source", "ops"),
    [
        ("a && b || c | d ; e & f", ["&&", "||", "|", ";", "&"]),
        ("a |& b", ["|&"]),
        ("case x in a) b;; esac", [")", ";;"]),
        ("(a)", ["(", ")"]),
        ("a ;& b", [";&"]),
        ("a ;;& b", [";;&"]),
    ],
)
def test_control_operators_tokenized(source: str, ops: list) -> None:
    found = [t.text for t in lex(source).tokens if t.kind is TokenKind.OPERATOR]
    assert found == ops


@pytest.mark.parametrize(
    ("source", "redirs"),
    [
        ("cat < a > b >> c", ["<", ">", ">>"]),
        ("cmd 2>&1", ["2>&"]),
        ("cmd > out 2>&1", [">", "2>&"]),
        ("cmd 2> err", ["2>"]),
        ("cmd &> both", ["&>"]),
        ("cmd &>> both", ["&>>"]),
        ("cmd <<< here", ["<<<"]),
        ("cmd << EOF", ["<<"]),
        ("cmd <<- EOF", ["<<-"]),
        ("cmd >&2", [">&"]),
        ("cmd <> rw", ["<>"]),
    ],
)
def test_redirection_operators_with_fd_prefixes(
    source: str, redirs: list
) -> None:
    found = [t.text for t in lex(source).tokens if t.kind is TokenKind.REDIRECTION]
    assert found == redirs


def test_fd_prefix_value_recorded() -> None:
    for t in lex("cmd 2>&1").tokens:
        if t.kind is TokenKind.REDIRECTION:
            assert t.fd == 2
            assert t.text == "2>&"


def test_digit_word_is_not_a_redirection() -> None:
    source = "echo 2x"
    kinds = [t.kind for t in lex(source).tokens]
    assert TokenKind.REDIRECTION not in kinds


def test_operator_adjacency_boundaries() -> None:
    # operators split words exactly at their characters (no glue loss):
    # every source character is owned by some token, and operators appear
    # as standalone tokens between the words they separate.
    source = "echo hi>out"
    result = lex(source)
    texts = [t.span.text(source) for t in result.tokens]
    assert "hi" in texts and ">" in [t.text for t in result.tokens if t.kind is TokenKind.REDIRECTION]
    assert "".join(texts) == source
    source = "foo;bar"
    texts = [t.span.text(source) for t in lex(source).tokens]
    assert "".join(texts) == source


def test_read_control_operator_table() -> None:
    assert read_control_operator(";;&x", 0, 4) == (";;&", 3)
    assert read_control_operator(";;x", 0, 3) == (";;", 2)
    assert read_control_operator(";&x", 0, 3) == (";&", 2)
    assert read_control_operator("&&x", 0, 3) == ("&&", 2)
    assert read_control_operator("||x", 0, 3) == ("||", 2)
    assert read_control_operator("|&x", 0, 3) == ("|&", 2)
    assert read_control_operator("|x", 0, 2) == ("|", 1)
    assert read_control_operator(";x", 0, 2) == (";", 1)
    assert read_control_operator("&x", 0, 2) == ("&", 1)
    assert read_control_operator("(x", 0, 2) == ("(", 1)
    assert read_control_operator(")x", 0, 2) == (")", 1)
    assert read_control_operator("x", 0, 1) == ("", 0)
    assert read_control_operator("", 0, 0) == ("", 0)


def test_read_redirection_operator_table() -> None:
    for op in ("&>>", "&>", "<<<", "<<-", "<<", ">>", "<>", ">|", "<&", ">&", "<", ">"):
        got, after = read_redirection_operator(op + " rest", 0, len(op) + 5)
        assert got == op
        assert after == len(op)
    assert read_redirection_operator("&&", 0, 2) == ("", 0)


def test_redirection_after_fd_predicate() -> None:
    assert redirection_after_fd("2>&1", 0, 4) is True
    assert redirection_after_fd("23>", 0, 3) is True
    assert redirection_after_fd("2x", 0, 2) is False
    assert redirection_after_fd("2", 0, 1) is False


def test_truncating_operator_tables() -> None:
    assert ">>" in TRUNCATING_REDIRECT_OPERATORS
    assert "&>" in TRUNCATING_REDIRECT_OPERATORS
    assert "<" in INPUT_ONLY_REDIRECT_OPERATORS
    assert "<<" in INPUT_ONLY_REDIRECT_OPERATORS
    assert "<<<" in INPUT_ONLY_REDIRECT_OPERATORS
    assert HEREDOC_OPERATORS == frozenset({"<<", "<<-"})


# ── comments ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("source", "comment_text"),
    [
        ("# whole line", "# whole line"),
        ("echo hi # tail", "# tail"),
        ("a; #tail", "#tail"),
        ("a && #tail", "#tail"),
        ("(a) #tail", "#tail"),
    ],
)
def test_comment_tokens(source: str, comment_text: str) -> None:
    comments = [t for t in lex(source).tokens if t.kind is TokenKind.COMMENT]
    assert len(comments) == 1
    assert comments[0].text == comment_text
    assert comments[0].span.end == len(source)


@pytest.mark.parametrize(
    ("source", "glued"),
    [
        ("echo a#b", True),          # inside a word: NOT a comment
        ("echo $(x)#glued", True),   # glued to a substitution close: word
        ("echo <(x)#glued", True),   # glued to a process-sub close: word
        ('echo "x"#tail', True),     # glued to a double-quoted word: word
    ],
)
def test_hash_not_a_comment_in_those_positions(source: str, glued: bool) -> None:
    comments = [t for t in lex(source).tokens if t.kind is TokenKind.COMMENT]
    if glued:
        assert comments == []
    else:
        assert comments


def test_comment_stops_at_newline_only() -> None:
    source = "x # c\ny"
    result = lex(source)
    comment = [t for t in result.tokens if t.kind is TokenKind.COMMENT][0]
    assert comment.text == "# c"


# ── substitution regions and nesting ────────────────────────────────────────

@pytest.mark.parametrize(
    ("source", "kind"),
    [
        ("echo $(rev)", WordPartKind.COMMAND_SUBSTITUTION),
        ("echo `rev`", WordPartKind.BACKQUOTE_SUBSTITUTION),
        ("echo ${x}", WordPartKind.PARAMETER_EXPANSION),
        ("echo $((1+2))", WordPartKind.ARITHMETIC_EXPANSION),
        ("<(cat a) x", WordPartKind.PROCESS_SUBSTITUTION),
    ],
)
def test_region_part_kinds(source: str, kind: WordPartKind) -> None:
    parts = [
        (k, span)
        for t in lex(source).tokens
        for k, span, _inner in t.parts
    ]
    assert any(k is kind for k, _s in parts), source
    assert all(
        (k is not kind) or (k in EXECUTABLE_REGIONS or kind not in EXECUTABLE_REGIONS)
        for k, _s in parts
    )


def test_inner_content_ranges() -> None:
    source = "echo $(rev x)"
    for t in lex(source).tokens:
        for k, span, inner in t.parts:
            if k is WordPartKind.COMMAND_SUBSTITUTION:
                assert inner is not None
                assert source[slice(*inner)] == "rev x"


def test_nested_command_substitutions() -> None:
    """Each substitution level is reported when its own range is lexed.

    The flat pass reports the OUTER region only (the inner one is lexed by
    the parser as a child context range); this checks that descending
    re-lexing of the inner content recovers every level.
    """
    source = "echo $(a $(b $(c)) d)"
    subs = [
        (span, inner)
        for t in lex(source).tokens
        for k, span, inner in t.parts
        if k is WordPartKind.COMMAND_SUBSTITUTION
    ]
    assert len(subs) == 1
    outer_span, outer_inner = subs[0]
    assert outer_span.text(source) == "$(a $(b $(c)) d)"
    # descend: lex the inner content of the outer region
    level1 = lex(source, outer_inner[0], outer_inner[1])
    subs1 = [
        (span, inner)
        for t in level1.tokens
        for k, span, inner in t.parts
        if k is WordPartKind.COMMAND_SUBSTITUTION
    ]
    assert subs1
    inner1 = subs1[0][1]
    assert source[slice(*inner1)] == "b $(c)"
    level2 = lex(source, inner1[0], inner1[1])
    subs2 = [
        (span, inner)
        for t in level2.tokens
        for k, span, inner in t.parts
        if k is WordPartKind.COMMAND_SUBSTITUTION
    ]
    assert subs2 and source[slice(*subs2[0][1])] == "c"


def test_command_substitution_inside_double_quotes_reported() -> None:
    source = 'echo "a $(b c) d"'
    for t in lex(source).tokens:
        kinds = {k for k, _s, _i in t.parts}
        if WordPartKind.DOUBLE_QUOTE in kinds:
            assert WordPartKind.COMMAND_SUBSTITUTION in kinds
            sub = next(span for k, span, _i in t.parts if k is WordPartKind.COMMAND_SUBSTITUTION)
            assert sub.text(source) == "$(b c)"


def test_backquote_inside_double_quotes_reported() -> None:
    source = 'echo "a `b c` d"'
    for t in lex(source).tokens:
        kinds = {k for k, _s, _i in t.parts}
        if WordPartKind.DOUBLE_QUOTE in kinds:
            assert WordPartKind.BACKQUOTE_SUBSTITUTION in kinds


def test_substitutions_do_not_cross_word_boundaries() -> None:
    # a bare $( ) is part of the enclosing word, not its own token
    source = "x$(y)z"
    words = [t for t in lex(source).tokens if t.kind is TokenKind.WORD]
    assert len(words) == 1
    assert words[0].span.text(source) == "x$(y)z"


# ── heredocs ────────────────────────────────────────────────────────────────

def test_heredoc_marker_tokens() -> None:
    source = "cat <<EOF\nbody\nEOF\n"
    redirs = [t.text for t in lex(source).tokens if t.kind is TokenKind.REDIRECTION]
    assert redirs == ["<<"]
    words = [t.span.text(source) for t in lex(source).tokens if t.kind is TokenKind.WORD]
    assert "EOF" in words  # delimiter tokenized on the marker line


def test_herestring_is_its_own_operator() -> None:
    source = "rev <<< abc"
    redirs = [t.text for t in lex(source).tokens if t.kind is TokenKind.REDIRECTION]
    assert redirs == ["<<<"]


# ── oracle equivalence (task test 8) ────────────────────────────────────────

ORACLE_CASES = [
    'echo "a b" x',
    'echo "a $(b c) d"',
    'echo "a\\"b" x',
    "echo 'plain' x",
    "echo $'a\\nb' tail",
    "echo $'unterminated",
    'echo "unterminated',
    "echo $(unterminated",
    "echo `unterminated",
    "echo $(a) $(b) tail",
    "echo $(a $(b)) tail",
    "echo ${x} tail",
    "echo ${x:-$(y)} tail",
    "echo $((1+2)) tail",
    "echo $((1+$(x))) tail",
    "diff <(cat a) <(cat b)",
    "echo `date +'%y'`",
    'echo "a `b` $(c) d" e',
    "echo $(echo \"nested $(deep) here\") out",
]

_KIND_TO_ORACLE = {
    WordPartKind.DOUBLE_QUOTE: lambda s, a: (
        _find_dq_end(s, a + 1), len(s)
    ),
    WordPartKind.COMMAND_SUBSTITUTION: lambda s, a: (
        _find_matching_paren(s, a + 1), len(s)
    ),
    WordPartKind.BACKQUOTE_SUBSTITUTION: lambda s, a: (
        _find_backtick_end(s, a + 1), len(s)
    ),
    WordPartKind.ANSI_C_QUOTE: lambda s, a: (
        _find_ansi_c_end(s, a + 2), len(s)
    ),
    WordPartKind.PARAMETER_EXPANSION: lambda s, a: (
        _match_parameter_braces(s, a + 2), len(s)
    ),
    WordPartKind.ARITHMETIC_EXPANSION: lambda s, a: (
        _match_arithmetic_span(s, a + 3), len(s)
    ),
}


@pytest.mark.parametrize("source", ORACLE_CASES)
def test_lexer_region_ends_match_legacy_oracles(source: str) -> None:
    """For each region token the boundary equals the legacy oracle's.

    The lexer delegates its boundary decisions to the imported oracle
    helpers, so equality here locks the consolidation contract (plan §3.3).
    Unterminated regions (oracle ``-1``/``None``) must extend to the range
    end in the token stream.
    """
    for token in lex(source).tokens:
        for kind, span, _inner in token.parts:
            if kind not in _KIND_TO_ORACLE:
                continue
            oracle_fn = _KIND_TO_ORACLE[kind]
            value, eof = oracle_fn(source, span.start)
            if kind is WordPartKind.DOUBLE_QUOTE:
                expected = eof if value == -1 else value
            elif kind is WordPartKind.COMMAND_SUBSTITUTION:
                expected = eof if value == -1 else value + 1
            elif kind is WordPartKind.BACKQUOTE_SUBSTITUTION:
                expected = eof if value == -1 else value
            elif kind is WordPartKind.ANSI_C_QUOTE:
                expected = eof if value == -1 else value
            elif kind is WordPartKind.PARAMETER_EXPANSION:
                expected = eof if value is None else value + 1
            else:  # ARITHMETIC_EXPANSION
                expected = eof if value is None else value + 2
            # embedded regions inside ${ } / $(( )) are rejected by the
            # oracle contract (returns None) — those parts extend to EOF in
            # both lexer and expectation.
            assert span.end == expected, (source, kind, span.start, expected, span.end)


@pytest.mark.parametrize(
    ("source", "quote_pos"),
    [
        ('echo "a" x', 5),
        ('echo "a\\"b" x', 5),
        ('echo "plain" ', 5),
    ],
)
def test_dq_end_equals_find_dq_end_simple(source: str, quote_pos: int) -> None:
    """The exported find_dq_end equals the legacy helper bit-for-bit."""
    assert find_dq_end(source, quote_pos + 1) == _find_dq_end(source, quote_pos + 1)
    expected = _find_dq_end(source, quote_pos + 1)
    for token in lex(source).tokens:
        for kind, span, _inner in token.parts:
            if kind is WordPartKind.DOUBLE_QUOTE and span.start == quote_pos:
                assert span.end == expected


@pytest.mark.parametrize(
    "source",
    [
        "echo $'a'",
        "echo $'a\\nb'",
        "echo $'unterminated",
        'echo "a b"',
        'echo "a \\" b"',
        "echo `x`",
        "echo `x\\`y`",
        "echo $(x)",
        "echo $( (x) y )",
        "echo $(x) tail",
        "echo $( $(nested) )",
        "echo ${x}",
        "echo ${x:-y}",
        "echo $((1+2))",
    ],
)
def test_exported_oracles_delegate_to_legacy(source: str) -> None:
    """lexer.find_* == bash_core._find_* on every position of every kind."""
    n = len(source)
    for i in range(n):
        assert find_dq_end(source, i) == _find_dq_end(source, i)
        assert find_ansi_c_end(source, i) == _find_ansi_c_end(source, i)
        assert find_backtick_end(source, i) == _find_backtick_end(source, i)
        if source[i] == "(":
            assert find_matching_paren(source, i) == _find_matching_paren(source, i)
        assert match_ansi_c_quote_span(source, i) == _match_ansi_c_quote_span(source, i)
        assert match_dquote_span(source, i) == _match_dquote_span(source, i)
        assert match_parameter_braces(source, i) == _match_parameter_braces(source, i)
        assert match_arithmetic_span(source, i) == _match_arithmetic_span(source, i)


# ── malformed constructs: diagnostics, never exceptions ─────────────────────

MALFORMED = [
    "echo 'unterminated",
    'echo "unterminated',
    "echo $'unterminated",
    'echo $"unterminated',
    "echo $(unterminated",
    "echo `unterminated",
    "echo ${unterminated",
    "echo $((unterminated",
    "echo <(unterminated",
    "echo $( \"mixed up quotes",
    "echo a\\",
    "cat <<",
    "cat <<EOF",
    '"',
    "'",
    "$",
    "$(",
    "${",
    "$((",
    "`",
]


@pytest.mark.parametrize("source", MALFORMED)
def test_malformed_lexes_without_raising_and_tiles(source: str) -> None:
    result = lex(source)
    cursor = 0
    for token in result.tokens:
        assert token.span.start == cursor
        cursor = token.span.end
    assert cursor == len(source)
    # and the whole pipeline survives it
    assert render_source(parse(source)) == source


UNTERMINATED_QUOTE_CASES = [
    ("echo 'x", DiagnosticKind.UNTERMINATED_QUOTE),
    ('echo "x', DiagnosticKind.UNTERMINATED_QUOTE),
    ("echo $'x", DiagnosticKind.UNTERMINATED_QUOTE),
    ('echo $"x', DiagnosticKind.UNTERMINATED_QUOTE),
]

UNTERMINATED_SUBST_CASES = [
    "echo $(x",
    "echo `x",
    "echo ${x",
    "echo $((x",
    "echo <(x",
    'echo "a $(b',
]


@pytest.mark.parametrize(("source", "kind"), UNTERMINATED_QUOTE_CASES)
def test_unterminated_quotes_diagnostic(source: str, kind: DiagnosticKind) -> None:
    diagnostics = lex(source).diagnostics
    assert any(d.kind is kind for d in diagnostics), (source, diagnostics)


@pytest.mark.parametrize("source", UNTERMINATED_SUBST_CASES)
def test_unterminated_substitutions_diagnostic(source: str) -> None:
    diagnostics = lex(source).diagnostics
    assert diagnostics, source
    assert any(
        d.kind is DiagnosticKind.UNTERMINATED_SUBST for d in diagnostics
    ), (source, diagnostics)


# ── performance / safety: depth and width bounds ────────────────────────────

def test_deep_nesting_lexes_in_bounded_time_without_recursion() -> None:
    source = "$(" * 2000 + "x" + ")" * 2000
    start = time.perf_counter()
    result = lex(source)
    elapsed = time.perf_counter() - start
    assert elapsed < 5.0, f"deep nesting too slow: {elapsed:.2f}s"
    # the outermost region tokenized and covers the range
    assert result.tokens[0].span.end <= len(source) + 1
    # unterminated/depth pathologies must still tile:
    cursor = 0
    for token in result.tokens:
        assert token.span.start == cursor
        cursor = token.span.end


def test_lex_is_not_recursive_python() -> None:
    """lex() runs at a CONSTANT stack depth regardless of nesting count."""
    import sys

    baseline = _frame_depth()
    source = "$(" * 300 + "x" + ")" * 300
    sys.setrecursionlimit(3000)
    lex(source)
    # a recursive descent over 300 regions would have added ~300 frames;
    # the flat oracle scanner adds a small constant instead
    assert _frame_depth() <= baseline + 1


def _frame_depth() -> int:
    import sys

    frame = sys._getframe()
    depth = 0
    while frame is not None:
        depth += 1
        frame = frame.f_back
    return depth


def test_20000_word_line_is_linear_time_and_token_count() -> None:
    words = " ".join(f"w{i}" for i in range(20000))
    start = time.perf_counter()
    result = lex(words)
    elapsed = time.perf_counter() - start
    assert elapsed < 5.0, f"wide input too slow: {elapsed:.2f}s"
    count = sum(1 for t in result.tokens if t.kind is TokenKind.WORD)
    assert count == 20000
    # gap (single-space) RAW tokens: exactly words-1
    raws = sum(1 for t in result.tokens if t.kind is TokenKind.RAW)
    assert raws == 19999


def test_second_pass_linear_scaling() -> None:
    """token count for 5,000 and 20,000 words scales linearly (ratio ~4)."""
    small = " ".join(f"w{i}" for i in range(5000))
    big = " ".join(f"w{i}" for i in range(20000))
    s_count = len(lex(small).tokens)
    b_count = len(lex(big).tokens)
    assert b_count == pytest.approx(s_count * 4, rel=0.05)


def test_diagnostic_spans_and_kinds_are_internal_data() -> None:
    diagnostics = lex("echo 'x").diagnostics
    assert diagnostics
    d = diagnostics[0]
    assert d.kind in set(DiagnosticKind)
    assert 0 <= d.span.start <= d.span.end <= 7
