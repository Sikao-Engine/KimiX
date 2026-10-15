r"""Lossless shell lexer: one O(n)-per-slice tokenizer for the shell IR.

Part of Phase 2 of the ``_shell_compat`` refactor (plan §3.3).  This module
consolidates the quote / escape / substitution scanners that are currently
DUPLICATED between ``fix_bash_command``'s scanner and ``_process_unquoted``
in :mod:`kimix_native.shell_compat.bash_core`.  The legacy copies stay in
bash_core untouched (they are retired in later phases).

Boundary oracles (plan §3.3 / task test 8): the module-level
:func:`find_dq_end`, :func:`find_ansi_c_end`, :func:`find_backtick_end` and
:func:`find_matching_paren` are **line-for-line ports** of the bash_core
module helpers (the ``_process_unquoted`` family), and the four
``_match_*`` scanner-family helpers are imported *directly* from
``bash_core`` (never re-implemented).  The lexer drives every region boundary
through these functions, so a quote/expansion token boundary can only ever be
a legacy boundary decision, and the tests compare token ends against the
oracles exactly.

Coverage (plan §3.3):

* single, double, ANSI-C (``$'``) and locale (``$"``) quotes;
* escaped characters and backslash line continuations;
* ``${ }``, ``$(( ))``, ``$( )`` and backtick regions, nesting-aware
  (``$( )`` closes at the ``)`` matched by :func:`find_matching_paren`,
  ``$(( ))`` at the ``))`` located by the imported
  ``bash_core._match_arithmetic_span``);
* comments (``#`` to end of line, never inside words/quotes — the legacy
  ``_comment_starts`` predicate decides);
* control operators (``&& || ; ;; ;& ;;& | |& ( )``) and redirection
  operators including fd prefixes (``2>``, ``>&2``, ``&>``, ``2>&1``,
  ``<<``, ``<<-``, ``<<<``) and word boundaries;
* ``<<`` / ``<<-`` marker operators: the delimiter is lexed as the following
  WORD token; body modelling is the parser's job;
* command substitutions inside double quotes: the double-quote walk records
  the ``$( )`` / backtick sub-spans it passes through as sibling parts, so
  the parser can build child contexts at any depth.

Complexity: :func:`lex` performs one pass over the range plus one pass per
region *entry* (nested ``$( )`` content is lexed when the parser descends
into it).  Python recursion is never used — every nesting decision resolves
through a flat loop — so a 2,000-level ``$( $( $( … ) ) )`` blob lexes in
bounded stack space and terminates (worst case quadratic like the legacy
matcher, which the depth-budget test asserts finishes).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

try:
    import regex as re
except ImportError:  # pragma: no cover
    import re  # stdlib: the used patterns are all stdlib-compatible

from kimix_native.shell_compat.bash_core import (
    _DQ_ESCAPED as _BC_DQ_ESCAPED,
)
from kimix_native.shell_compat.bash_core import (
    _find_ansi_c_end as _bc_find_ansi_c_end,
    _find_backtick_end as _bc_find_backtick_end,
    _find_dq_end as _bc_find_dq_end,
    _find_matching_paren as _bc_find_matching_paren,
)
from kimix_native.shell_compat.bash_core import (
    _match_arithmetic_span,
    _match_ansi_c_quote_span,
    _match_dquote_span,
    _match_parameter_braces,
)
from kimix_native.shell_compat.models import WordPartKind
from kimix_native.shell_compat.spans import Diagnostic, DiagnosticKind, SourceSpan

# ── lexical character classes (mirrors of the bash_core constants) ──────────

#: characters that terminate an unquoted word (operators + whitespace/newline)
_WORD_END_CHARS = frozenset(";&|()<>\n \t\r")
#: characters that can start a control operator
_CONTROL_OP_STARTS = frozenset(";&|()")
#: characters that can start a redirection operator
_REDIRECTION_START = frozenset("<>")
#: characters special inside a double-quoted region (bulk-skip pattern)
_DQ_SPECIAL_RE = re.compile(r'[\\"`$]')
#: characters that end a word or open a quoted/expanded region (bulk-skip)
_WORD_SPECIAL_RE = re.compile(r"[;&|()<>\n \t\r#\\'\"`$]")
#: longest-first redirection operator table (same ordering as bash_core's)
_REDIRECTION_RE = re.compile(r"&>>|&>|<<<|<<-|<<|>>|<>|>\||<&|>&|<|>")
#: horizontal whitespace run
_HSPACE_RE = re.compile(r"[ \t\r]+")

#: Redirection operators that create/truncate their target (the Phase 4 nul
#: predicate; ``>&`` with a file-word target also truncates — caller decides
#: from the target kind).
TRUNCATING_REDIRECT_OPERATORS = frozenset({">", ">|", ">>", "<>", "&>", "&>>"})
#: Redirection operators that never create a file.
INPUT_ONLY_REDIRECT_OPERATORS = frozenset({"<", "<<", "<<-", "<<<", "<&"})
#: Operators that consume a heredoc delimiter as the following word.
HEREDOC_OPERATORS = frozenset({"<<", "<<-"})

#: region kinds whose content is an executable command context
EXECUTABLE_REGIONS = frozenset(
    {
        WordPartKind.COMMAND_SUBSTITUTION,
        WordPartKind.BACKQUOTE_SUBSTITUTION,
        WordPartKind.PROCESS_SUBSTITUTION,
    }
)

__all__ = [
    "EXECUTABLE_REGIONS",
    "HEREDOC_OPERATORS",
    "INPUT_ONLY_REDIRECT_OPERATORS",
    "LexResult",
    "Token",
    "TokenKind",
    "TRUNCATING_REDIRECT_OPERATORS",
    "find_ansi_c_end",
    "find_backtick_end",
    "find_dq_end",
    "find_matching_paren",
    "lex",
    "match_ansi_c_quote_span",
    "match_arithmetic_span",
    "match_dquote_span",
    "match_parameter_braces",
    "read_control_operator",
    "read_redirection_operator",
    "redirection_after_fd",
]


class TokenKind(str, Enum):
    """Category of a lexed token (plan §3.2 ``TokenKind``)."""

    WORD = "word"
    OPERATOR = "operator"
    REDIRECTION = "redirection"
    COMMENT = "comment"
    NEWLINE = "newline"
    RAW = "raw"


#: ``(kind, span, inner)`` triple: a lexical region inside a word.  ``inner``
#: is the ``(content_start, content_end)`` range of the region's content
#: (opener/closer stripped) for closed regions; ``None`` when the region is
#: unterminated or its content is not addressable.
WordPart = Tuple[WordPartKind, SourceSpan, Optional[Tuple[int, int]]]


@dataclass(frozen=True, slots=True)
class Token:
    """A lexed source span.

    WORD tokens carry ``parts``: ``(kind, span, inner)`` triples in source
    order.  LITERAL / ESCAPED / LINE_CONTINUATION parts tile the word span
    without gaps; region parts (quotes, expansions, substitutions) span their
    whole region, and sub-regions discovered *directly inside* a double quote
    are appended as sibling parts immediately after the enclosing quote part
    (span containment identifies their parent, plan §3.3).
    """

    kind: TokenKind
    span: SourceSpan
    parts: Tuple[WordPart, ...] = ()
    #: operator text for OPERATOR/REDIRECTION tokens (``&&``, ``2>``, ``>&`` …)
    text: str = ""
    #: fd number for redirection tokens with an explicit fd prefix (``2>file``)
    fd: Optional[int] = None


@dataclass(frozen=True, slots=True)
class LexResult:
    """The tokens of one range plus the lexer's internal diagnostics."""

    tokens: Tuple[Token, ...] = ()
    diagnostics: Tuple[Diagnostic, ...] = ()


# ── ported / re-exported boundary oracles (byte-compatible with bash_core) ──


def find_ansi_c_end(cmd: str, start: int) -> int:
    """Index AFTER the closing ``'`` of a ``$'…'`` region; ``-1`` if unterminated.

    Port of ``bash_core._find_ansi_c_end`` (delegates to the original — the
    single source of truth; the lexer never re-implements a boundary).
    """
    return _bc_find_ansi_c_end(cmd, start)


def find_backtick_end(cmd: str, start: int) -> int:
    """Index AFTER the closing backtick; ``-1`` if unterminated.

    Port of ``bash_core._find_backtick_end`` (delegates to the original).
    """
    return _bc_find_backtick_end(cmd, start)


def find_dq_end(cmd: str, start: int) -> int:
    """Index AFTER the closing ``"`` of a double-quoted region; ``-1`` if open.

    Port of ``bash_core._find_dq_end`` (delegates to the original), including
    its documented quirks: only ``\\X`` with ``X`` in ``_DQ_ESCAPED`` skips a
    pair; ``$(`` descends through the legacy matcher; ``$'`` inside ``"…"`` is
    literal text; a backtick closes via the legacy backtick scanner.
    """
    return _bc_find_dq_end(cmd, start)


def find_matching_paren(cmd: str, open_pos: int) -> int:
    """Index of the ``)`` matching the ``(`` at ``open_pos``; ``-1`` if none.

    Port of ``bash_core._find_matching_paren`` (delegates to the original):
    flat pass tracking nested ``$(…)``, single quotes, double quotes (with
    their own nested regions), backticks, ``$'…'`` regions, and plain
    subshell parentheses.
    """
    return _bc_find_matching_paren(cmd, open_pos)


def match_ansi_c_quote_span(text: str, i: int) -> Optional[int]:
    """Index after the ``'`` closing a ``$'`` region opened before ``i``.

    Re-export of ``bash_core._match_ansi_c_quote_span`` (scanner family).
    """
    return _match_ansi_c_quote_span(text, i)


def match_dquote_span(text: str, i: int) -> Optional[int]:
    """Index after the closing ``"`` of a region opened before ``i``.

    Re-export of ``bash_core._match_dquote_span``.  NOTE the documented
    difference from :func:`find_dq_end`: this matcher skips every ``\\X``
    pair and does not descend into regions; for expansion-free double quotes
    both agree, which the oracle test asserts.
    """
    return _match_dquote_span(text, i)


def match_parameter_braces(text: str, i: int) -> Optional[int]:
    """Index of the ``}`` closing ``${`` (opened at ``i - 2``); ``None`` open.

    Re-export of ``bash_core._match_parameter_braces``; ``None`` also when a
    ``$(…)``/backtick is embedded (legacy contract: inner command contexts
    are located separately).
    """
    return _match_parameter_braces(text, i)


def match_arithmetic_span(text: str, i: int) -> Optional[int]:
    """Index of the first ``)`` of the ``))`` closing ``$((``; ``None`` open.

    Re-export of ``bash_core._match_arithmetic_span``; ``None`` also when a
    ``$(…)`` command substitution is embedded.
    """
    return _match_arithmetic_span(text, i)


# ── operator helpers ─────────────────────────────────────────────────────────


def read_control_operator(source: str, i: int, end: int) -> Tuple[str, int]:
    """Return the control operator at ``i`` and the index after it.

    Same table and longest-first order as the legacy
    ``bash_core._read_shell_control_operator`` (``;;& | ;; | ;& | ; | && | &
    | || | |& | |``), extended with the subshell parentheses as in the
    scanner's own ``_read_control_operator``.
    """
    if i >= end:
        return "", i
    ch = source[i]
    if ch == ";":
        if source.startswith(";;&", i):
            return ";;&", i + 3
        if source.startswith(";;", i):
            return ";;", i + 2
        if source.startswith(";&", i):
            return ";&", i + 2
        return ";", i + 1
    if ch == "&":
        if source.startswith("&&", i):
            return "&&", i + 2
        return "&", i + 1
    if ch == "|":
        if source.startswith("||", i):
            return "||", i + 2
        if source.startswith("|&", i):
            return "|&", i + 2
        return "|", i + 1
    if ch == "(" or ch == ")":
        return ch, i + 1
    return "", i


def read_redirection_operator(source: str, i: int, end: int) -> Tuple[str, int]:
    """Return the redirection operator at ``i`` (longest match) and index after."""
    m = _REDIRECTION_RE.match(source, i, end)
    if m is None:
        return "", i
    return m.group(), m.end()


def redirection_after_fd(source: str, i: int, end: int) -> bool:
    """True when the digit run at ``i`` is followed by a redirection character."""
    while i < end and source[i].isdigit():
        i += 1
    return i < end and source[i] in _REDIRECTION_START


def _comment_starts(source: str, i: int, range_start: int) -> bool:
    """Legacy comment predicate (``_BashFixScanner._comment_starts``)."""
    if i <= range_start:
        return True
    return source[i - 1] in " \t\r\n;&|()<>"


# ── word scanning (the consolidated quote/escape scanner) ───────────────────


def _scan_double_quote(
    source: str, start: int, end: int, diagnostics: list
) -> Tuple[int, Tuple[WordPart, ...]]:
    """Close the ``"`` region opened at ``start``; collect nested regions.

    The walk is the legacy ``_find_dq_end`` decision sequence (the token end
    must equal ``find_dq_end(source, start + 1)`` — asserted by the oracle
    test), extended only by *recording* the ``$( )`` / backtick sub-spans it
    passes through (and the closing ``"``'s inner range).  Sub-regions that
    the legacy oracle itself cannot close (embedded ``$(`` unterminated) end
    the quote scan exactly like the oracle: unterminated region to ``end``.
    """
    nested: list = []
    i = start + 1
    search = _DQ_SPECIAL_RE.search
    while i < end:
        m = search(source, i, end)
        if m is None:
            break
        i = m.start()
        ch = source[i]
        if ch == "\\" and i + 1 < end and source[i + 1] in _BC_DQ_ESCAPED:
            i += 2
            continue
        if ch == '"':
            return i + 1, tuple(nested)
        if ch == "$" and i + 1 < end and source[i + 1] == "(":
            close = find_matching_paren(source, i + 1)
            if close == -1 or close >= end:
                # the oracle returns -1 here: the quote never closes within
                # the slice.  Record the open region and stop at `end`.
                nested.append(
                    (
                        WordPartKind.COMMAND_SUBSTITUTION,
                        SourceSpan(i, end),
                        None,
                    )
                )
                diagnostics.append(
                    Diagnostic(
                        DiagnosticKind.UNTERMINATED_SUBST,
                        SourceSpan(i, end),
                        "unterminated command substitution in double quotes",
                    )
                )
                diagnostics.append(
                    Diagnostic(
                        DiagnosticKind.UNTERMINATED_QUOTE,
                        SourceSpan(start, end),
                        "unterminated double quote",
                    )
                )
                return end, tuple(nested)
            nested.append(
                (
                    WordPartKind.COMMAND_SUBSTITUTION,
                    SourceSpan(i, close + 1),
                    (i + 2, close),
                )
            )
            i = close + 1
            continue
        if ch == "$" and i + 1 < end and source[i + 1] == "'":
            i += 1  # literal '$' inside "..." (legacy documented behaviour)
            continue
        if ch == "`":
            close = find_backtick_end(source, i + 1)
            if close == -1 or close > end:
                nested.append(
                    (
                        WordPartKind.BACKQUOTE_SUBSTITUTION,
                        SourceSpan(i, end),
                        None,
                    )
                )
                diagnostics.append(
                    Diagnostic(
                        DiagnosticKind.UNTERMINATED_SUBST,
                        SourceSpan(i, end),
                        "unterminated backquote in double quotes",
                    )
                )
                diagnostics.append(
                    Diagnostic(
                        DiagnosticKind.UNTERMINATED_QUOTE,
                        SourceSpan(start, end),
                        "unterminated double quote",
                    )
                )
                return end, tuple(nested)
            nested.append(
                (
                    WordPartKind.BACKQUOTE_SUBSTITUTION,
                    SourceSpan(i, close),
                    (i + 1, close - 1),
                )
            )
            i = close
            continue
        i += 1
    diagnostics.append(
        Diagnostic(
            DiagnosticKind.UNTERMINATED_QUOTE,
            SourceSpan(start, end),
            "unterminated double quote",
        )
    )
    return end, tuple(nested)


def _scan_word(
    source: str, start: int, end: int, diagnostics: list
) -> Tuple[int, Tuple[WordPart, ...]]:
    """Scan a WORD at ``start``; returns ``(word_end, parts)``.

    Parts tile ``[start, word_end)`` with no gaps, in source order
    (literal runs + region parts + nested double-quote sub-regions).  A
    mid-word ``#`` is ordinary content (legacy rule); ``#`` at word start
    never reaches here (the caller emits a COMMENT token).
    """
    parts: list = []
    literal_start = start
    i = start
    while i < end:
        m = _WORD_SPECIAL_RE.search(source, i, end)
        if i > literal_start and (m is None or m.start() > literal_start):
            flush_to = end if m is None else m.start()
            if flush_to > literal_start:
                parts.append(
                    (WordPartKind.LITERAL, SourceSpan(literal_start, flush_to), None)
                )
                literal_start = flush_to
        if m is None:
            i = end
            break
        i = m.start()
        ch = source[i]
        if ch in _WORD_END_CHARS:
            break
        if ch == "#":
            if i == start:
                break
            i += 1
            continue
        if ch == "\\":
            if i + 1 < end and source[i + 1] == "\n":
                # line continuation: flush the run, own the pair, keep going
                if i > literal_start:
                    parts.append(
                        (WordPartKind.LITERAL, SourceSpan(literal_start, i), None)
                    )
                parts.append(
                    (WordPartKind.LINE_CONTINUATION, SourceSpan(i, i + 2), None)
                )
                i += 2
                literal_start = i
                continue
            stop = i + 2 if i + 1 < end else i + 1
            if i > literal_start:
                parts.append(
                    (WordPartKind.LITERAL, SourceSpan(literal_start, i), None)
                )
            parts.append((WordPartKind.ESCAPED_CHARACTER, SourceSpan(i, stop), None))
            i = stop
            literal_start = i
            continue
        if ch == "'":
            close = source.find("'", i + 1, end)
            if close < 0:
                diagnostics.append(
                    Diagnostic(
                        DiagnosticKind.UNTERMINATED_QUOTE,
                        SourceSpan(i, end),
                        "unterminated single quote",
                    )
                )
                parts.append((WordPartKind.SINGLE_QUOTE, SourceSpan(i, end), None))
                i = end
                literal_start = i
                continue
            parts.append(
                (
                    WordPartKind.SINGLE_QUOTE,
                    SourceSpan(i, close + 1),
                    (i + 1, close),
                )
            )
            i = close + 1
            literal_start = i
            continue
        if ch == '"':
            stop, nested = _scan_double_quote(source, i, end, diagnostics)
            inner = (i + 1, stop - 1) if stop < end or source[stop - 1 : stop] == '"' else None
            if stop <= end and stop > i and source[stop - 1 : stop] == '"':
                inner = (i + 1, stop - 1)
            else:
                inner = None
            parts.append((WordPartKind.DOUBLE_QUOTE, SourceSpan(i, stop), inner))
            parts.extend(nested)
            i = stop
            literal_start = i
            continue
        if ch == "$" and i + 1 < end:
            nxt = source[i + 1]
            if nxt == "'":
                stop = find_ansi_c_end(source, i + 2)
                if stop == -1 or stop > end:
                    diagnostics.append(
                        Diagnostic(
                            DiagnosticKind.UNTERMINATED_QUOTE,
                            SourceSpan(i, end),
                            "unterminated ANSI-C quote",
                        )
                    )
                    parts.append(
                        (WordPartKind.ANSI_C_QUOTE, SourceSpan(i, end), None)
                    )
                    i = end
                    literal_start = i
                    continue
                parts.append(
                    (
                        WordPartKind.ANSI_C_QUOTE,
                        SourceSpan(i, stop),
                        (i + 2, stop - 1),
                    )
                )
                i = stop
                literal_start = i
                continue
            if nxt == '"':
                # locale quote $"...": body is data (identity translation on
                # Git Bash); boundary = closing " with every \X pair skipped
                # (the legacy _literal_word_value walk of the same region).
                j = i + 2
                while j < end:
                    if source[j] == "\\" and j + 1 < end:
                        j += 2
                        continue
                    if source[j] == '"':
                        break
                    j += 1
                if j >= end:
                    diagnostics.append(
                        Diagnostic(
                            DiagnosticKind.UNTERMINATED_QUOTE,
                            SourceSpan(i, end),
                            "unterminated locale quote",
                        )
                    )
                    parts.append((WordPartKind.LOCALE_QUOTE, SourceSpan(i, end), None))
                    i = end
                    literal_start = i
                    continue
                parts.append(
                    (
                        WordPartKind.LOCALE_QUOTE,
                        SourceSpan(i, j + 1),
                        (i + 2, j),
                    )
                )
                i = j + 1
                literal_start = i
                continue
            if nxt == "(":
                if source.startswith("$((", i):
                    stop = _match_arithmetic_span(source, i + 3)
                    if stop is None:
                        diagnostics.append(
                            Diagnostic(
                                DiagnosticKind.UNTERMINATED_SUBST,
                                SourceSpan(i, end),
                                "unterminated arithmetic expansion",
                            )
                        )
                        parts.append(
                            (WordPartKind.ARITHMETIC_EXPANSION, SourceSpan(i, end), None)
                        )
                        i = end
                        literal_start = i
                        continue
                    end_after = stop + 2  # consume the '))'
                    parts.append(
                        (
                            WordPartKind.ARITHMETIC_EXPANSION,
                            SourceSpan(i, end_after),
                            (i + 3, stop),
                        )
                    )
                    i = end_after
                    literal_start = i
                    continue
                close = find_matching_paren(source, i + 1)
                if close == -1 or close >= end:
                    diagnostics.append(
                        Diagnostic(
                            DiagnosticKind.UNTERMINATED_SUBST,
                            SourceSpan(i, end),
                            "unterminated command substitution",
                        )
                    )
                    parts.append(
                        (WordPartKind.COMMAND_SUBSTITUTION, SourceSpan(i, end), None)
                    )
                    i = end
                    literal_start = i
                    continue
                parts.append(
                    (
                        WordPartKind.COMMAND_SUBSTITUTION,
                        SourceSpan(i, close + 1),
                        (i + 2, close),
                    )
                )
                i = close + 1
                literal_start = i
                continue
            if nxt == "{":
                stop = _match_parameter_braces(source, i + 2)
                if stop is None:
                    diagnostics.append(
                        Diagnostic(
                            DiagnosticKind.UNTERMINATED_SUBST,
                            SourceSpan(i, end),
                            "unterminated parameter expansion",
                        )
                    )
                    parts.append(
                        (WordPartKind.PARAMETER_EXPANSION, SourceSpan(i, end), None)
                    )
                    i = end
                    literal_start = i
                    continue
                parts.append(
                    (
                        WordPartKind.PARAMETER_EXPANSION,
                        SourceSpan(i, stop + 1),
                        (i + 2, stop),
                    )
                )
                i = stop + 1
                literal_start = i
                continue
            # plain '$' is ordinary word content
            i += 1
            continue
        if ch == "`":
            close = find_backtick_end(source, i + 1)
            if close == -1 or close > end:
                diagnostics.append(
                    Diagnostic(
                        DiagnosticKind.UNTERMINATED_SUBST,
                        SourceSpan(i, end),
                        "unterminated backquote substitution",
                    )
                )
                parts.append((WordPartKind.BACKQUOTE_SUBSTITUTION, SourceSpan(i, end), None))
                i = end
                literal_start = i
                continue
            parts.append(
                (
                    WordPartKind.BACKQUOTE_SUBSTITUTION,
                    SourceSpan(i, close),
                    (i + 1, close - 1),
                )
            )
            i = close
            literal_start = i
            continue
        i += 1
    if i > literal_start:
        parts.append((WordPartKind.LITERAL, SourceSpan(literal_start, i), None))
    return i, tuple(parts)


def _hash_glued_after_substitution(tokens: list, hash_pos: int) -> bool:
    """True when the previous WORD ends exactly at ``hash_pos`` with a
    command/process substitution region as its last part.

    Mirrors the legacy scanner's ``subst_just_closed`` flag: only a '#' at
    the character directly after a substitution close is word-internal.
    """
    if not tokens:
        return False
    prev = tokens[-1]
    if prev.kind is not TokenKind.WORD or prev.span.end != hash_pos:
        return False
    if not prev.parts:
        return False
    last_kind, last_span, _inner = prev.parts[-1]
    if last_span.end != hash_pos:
        return False
    return last_kind in (
        WordPartKind.COMMAND_SUBSTITUTION,
        WordPartKind.PROCESS_SUBSTITUTION,
    )


# ── the public tokenizer ─────────────────────────────────────────────────────


def lex(source: str, start: int = 0, end: Optional[int] = None) -> LexResult:
    """Tokenize ``source[start:end]`` into a flat, source-ordered token list.

    Every region boundary comes from an oracle helper (module docstring); no
    Python recursion is used at any depth, so pathologically nested input
    terminates.  Token spans tile the range exactly — whitespace runs become
    RAW tokens — which is the base of the lossless invariant.  Diagnostics
    (unterminated quotes/regions) are collected, never raised: malformed
    input still yields a tiling stream whose spans cover the range.
    """
    if end is None:
        end = len(source)
    if not (0 <= start <= end <= len(source)):
        raise ValueError(
            f"bad lex range: start={start} end={end} len={len(source)}"
        )
    tokens: list = []
    diagnostics: list = []
    i = start
    while i < end:
        ch = source[i]
        if ch in " \t\r":
            m = _HSPACE_RE.match(source, i, end)
            stop = m.end() if m is not None else i + 1
            tokens.append(
                Token(TokenKind.RAW, SourceSpan(i, stop), text=source[i:stop])
            )
            i = stop
            continue
        if ch == "\\" and i + 1 < end and source[i + 1] == "\n":
            tokens.append(Token(TokenKind.RAW, SourceSpan(i, i + 2), text="\\\n"))
            i += 2
            continue
        if ch == "\n":
            tokens.append(Token(TokenKind.NEWLINE, SourceSpan(i, i + 1)))
            i += 1
            continue
        if ch == "#" and _comment_starts(source, i, start):
            # Legacy rule (the scanner's one-shot ``subst_just_closed``):
            # a '#' glued DIRECTLY after a ``$(…)`` / ``<(`` / ``>(`` close
            # continues the word (substitutions expand inside words) and
            # does NOT start a comment — unlike '#' after a control-
            # operator ')' (subshell group), which genuinely comments.
            if not _hash_glued_after_substitution(tokens, i):
                newline = source.find("\n", i + 1, end)
                stop = end if newline < 0 else newline
                tokens.append(
                    Token(TokenKind.COMMENT, SourceSpan(i, stop), text=source[i:stop])
                )
                i = stop
                continue
        # process substitution: <( or >( — word-like region, not a redirection
        if ch in _REDIRECTION_START and source[i + 1 : i + 2] == "(":
            close = find_matching_paren(source, i + 1)
            if close == -1 or close >= end:
                diagnostics.append(
                    Diagnostic(
                        DiagnosticKind.UNTERMINATED_SUBST,
                        SourceSpan(i, end),
                        "unterminated process substitution",
                    )
                )
                tokens.append(
                    Token(
                        TokenKind.WORD,
                        SourceSpan(i, end),
                        parts=(
                            (WordPartKind.PROCESS_SUBSTITUTION, SourceSpan(i, end), None),
                        ),
                    )
                )
                i = end
                continue
            tokens.append(
                Token(
                    TokenKind.WORD,
                    SourceSpan(i, close + 1),
                    parts=(
                        (
                            WordPartKind.PROCESS_SUBSTITUTION,
                            SourceSpan(i, close + 1),
                            (i + 2, close),
                        ),
                    ),
                )
            )
            i = close + 1
            continue
        # redirection operators, including fd prefixes (2>, >&2, &>, 2>&1)
        j = i
        fd_start = -1
        if ch.isdigit() and redirection_after_fd(source, i, end):
            fd_start = i
            while j < end and source[j].isdigit():
                j += 1
            ch = source[j] if j < end else ""
        if ch in _REDIRECTION_START or (ch == "&" and source[j + 1 : j + 2] == ">"):
            op, after = read_redirection_operator(source, j, end)
            if op:
                span_start = fd_start if fd_start >= 0 else i
                fd_value = int(source[fd_start:j]) if fd_start >= 0 else None
                tokens.append(
                    Token(
                        TokenKind.REDIRECTION,
                        SourceSpan(span_start, after),
                        text=source[span_start:after],
                        fd=fd_value,
                    )
                )
                i = after
                continue
        # control operators / subshell parentheses
        if ch in _CONTROL_OP_STARTS and ch not in _REDIRECTION_START:
            op, after = read_control_operator(source, i, end)
            if op:
                tokens.append(
                    Token(TokenKind.OPERATOR, SourceSpan(i, after), text=op)
                )
                i = after
                continue
        if ch == "#":
            # glued '#' after a substitution close: word content, NOT a
            # comment (``_scan_word`` would break on a leading '#').
            word_end, parts = _scan_word(source, i + 1, end, diagnostics)
            parts = (
                (WordPartKind.LITERAL, SourceSpan(i, i + 1), None),
            ) + tuple(parts)
            span_end = max(word_end, i + 1)
            tokens.append(Token(TokenKind.WORD, SourceSpan(i, span_end), parts=parts))
            i = span_end
            continue
        word_end, parts = _scan_word(source, i, end, diagnostics)
        if word_end <= i:
            # nothing consumed (defensive): one raw char keeps progress
            tokens.append(Token(TokenKind.RAW, SourceSpan(i, i + 1), text=source[i]))
            i += 1
            continue
        tokens.append(Token(TokenKind.WORD, SourceSpan(i, word_end), parts=parts))
        i = word_end
    return LexResult(tuple(tokens), tuple(diagnostics))
