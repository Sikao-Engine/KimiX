r"""cmd.exe structural translation and refusal rules (Phase 6, plan §3.5).

Phase 6 of the ``_shell_compat`` refactor (plan §3.5 handler priorities 1-3:
cmd leading ``@``, cmd ``if``/``for`` structural rules, and the unsupported
jump refusals).  The recognition, validation and commit logic for the cmd.exe
control-flow constructs lived inline as methods on
:class:`kimix_native.shell_compat.bash_core._BashFixScanner` (the ``_try_at_prefix``
/ ``_match_cmd_if`` / ``_commit_cmd_if`` / ``_try_cmd_for`` / ``_match_cmd_for`` /
``_commit_cmd_for`` / ``_record_cmd_jump`` family and their helpers).  This module
owns their canonical implementation as pure, testable functions, and the LIVE
scanner delegates to them (thin lazy delegates at the historical names, exactly
the Phase-4 ``paths.py`` / Phase-5 ``wrappers.py`` pattern), so there is ONE
implementation per rule, byte-for-byte.

Two shapes are exposed on purpose:

* **Reader-mutating core functions** (``at_prefix_rule``, ``validate_cmd_if`` /
  ``commit_cmd_if``, ``validate_cmd_for`` / ``commit_cmd_for``,
  ``record_cmd_jump``) — the byte-for-byte port of the legacy scanner method
  bodies.  They are parameterised over a *reader* object (the live scanner passes
  ``self`` so the word boundaries, the ``@echo``/``else``/``do`` word reads, and
  the recursive branch-body ``_scan_range`` are byte-identical to the historical
  methods) plus the source string and the index spans.  ``validate_*`` are a
  strictly read-only pass that produces the match object; ``commit_*`` is the
  guaranteed-to-succeed edit/scan pass whose every edit pair is disjoint by
  construction (design ``.kimix_cache/cmd_control_flow_design.md`` §3.2/§4.3).
* **IR-side pure predicates + dispatcher handlers** (``cmd_if_node``,
  ``cmd_for_node``, ``cmd_at_decision``, ``cmd_jump_refusal``,
  ``rule_cmd_at`` / ``rule_cmd_control`` / ``rule_cmd_jump``) — the §3.5
  ``RuleDispatcher`` slot 1-3 handlers.  They build the validated
  :class:`CmdIfNode` / :class:`CmdForNode` (the IR statement of the same match
  the scanner produces, sharing the read-only pass) and emit report events
  through the :class:`translator.RuleResult` / :class:`translator.EditPlan`
  contract, so Phase 7's translator can schedule the structural edits from a
  parsed tree without running the scanner.  The IR wrappers build a
  ``_BashFixScanner(source)`` reader (the Phase-4 ``cd_d_flag_span`` / Phase-5
  ``unwrap_decision_for_command`` pattern) so the read-only decision is the SAME
  function the live scanner runs.

Dataclass / constant ownership (lower-churn option): the frozen
``_CmdIfSegment`` / ``_CmdIfMatch`` / ``_CmdForMatch`` match records, the
matcher constants (``_CMD_IF_TAIL_SAFE_CHARS`` / ``_CMD_REL_OPS``) and the
compiled regexes (``_ERRORLEVEL_RE`` / ``_INT_RE`` / ``_GLUED_LABEL_RE`` /
``_CALL_BATCH_RE`` / ``_FOR_LOOP_TOKEN_RE``) STAY canonical in ``bash_core``
(the 195-name facade and the intra-module lookups therefore resolve with zero
churn); this module IMPORTS them, so both sides share one object per record /
pattern.  The Phase-6 IR-side additions (``AtPrefixDecision``, ``CmdIfNode``,
``CmdForNode``) are defined here.

Import structure: this module imports ``bash_core`` (constants + tables) and
``paths`` (the Windows-path rewrite used by ``if exist``/``for`` list operands)
eagerly at load — safe because neither imports this module at load; ``bash_core``
imports this module LAZILY inside the delegate bodies.  The IR wrappers import
``_BashFixScanner`` lazily inside their bodies to avoid a load-time self-cycle.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, List, Optional, Tuple

# ── reused tables / constants / helpers (canonical homes stay bash_core + paths) ─
# The frozen ``_CmdIfSegment`` / ``_CmdIfMatch`` / ``_CmdForMatch`` match records,
# the matcher constants (``_CMD_IF_TAIL_SAFE_CHARS`` / ``_CMD_REL_OPS``) and the
# compiled regexes are imported from ``bash_core`` (the LOWER-CHURN option: they
# stay canonical there, so the ``_shell_compat`` facade's 195-name ``__all__`` and
# the intra-module lookups resolve unchanged and no re-export wiring is needed).
# The IR path and the live scanner share the SAME objects — a change in a canonical
# constant/pattern cannot fork here.  ``dataclasses`` is imported only for the IR
# ``CmdIfNode``/``CmdForNode``/``AtPrefixDecision`` definitions added here.
from kimix_native.shell_compat.bash_core import (
    _CALL_BATCH_RE,
    _CMD_IF_TAIL_SAFE_CHARS,
    _CMD_REL_OPS,
    _CmdForMatch,
    _CmdIfMatch,
    _CmdIfSegment,
    _ERRORLEVEL_RE,
    _FOR_LOOP_TOKEN_RE,
    _GLUED_LABEL_RE,
    _HSPACE_RE,
    _INT_RE,
    _NAME_RE,
)
from kimix_native.shell_compat.paths import path_replacement

if TYPE_CHECKING:  # pragma: no cover
    from kimix_native.shell_compat.models import CommandNode
    from kimix_native.shell_compat.translator import RuleResult, TranslationContext


# ── leading ``@`` rule (plan §3.5 priority 1) ─────────────────────────────────


@dataclasses.dataclass(frozen=True)
class AtPrefixDecision:
    """The decision a command-position ``@`` word produced.

    ``index`` is the position the dispatch loop continues at
    (``word_start + 1`` for a strip, or just past the translated ``@echo``
    region for a statement); ``mode`` is ``"statement"`` (the two-word
    ``@echo off|on`` translation, reported in ``cmd_conditional_fixes``) or
    ``"strip"`` (the single leading ``@`` deleted and the remainder
    re-dispatched).  ``edit`` is the ``(start, end, replacement)`` splice to
    append; ``note`` is the ``cmd_conditional_fixes`` keyword region (only the
    ``statement`` mode records one).
    """

    index: int
    mode: str
    edit: Tuple[int, int, str]
    note: Optional[str]


def at_prefix_rule(
    reader: Any, source: str, word_start: int, word_end: int, end: int
) -> Optional[AtPrefixDecision]:
    r"""Decide the leading-``@`` command-word form; strictly read-only.

    Byte-for-byte port of the decision part of ``_BashFixScanner._try_at_prefix``
    (the edit/note appending stays at the call site, exactly the Phase-5
    ``unwrap_decision`` split).  ``reader`` must expose ``_literal_word_value``,
    ``_read_word`` and ``_at_boundary_ok`` (the live scanner passes ``self`` so the
    ``@echo off|on`` word read and boundary test are byte-identical).  Returns
    ``None`` on every documented bail (a bare ``@``, a non-decodable word,
    ``@@x``/``@ x``/``@``-then-operator, the cmd ``@echo.`` blank-line idiom, or a
    quoted-leading ``@``); otherwise an :class:`AtPrefixDecision`.
    """
    s = source
    raw = s[word_start:word_end]
    if raw[:1] != "@":
        return None
    literal = reader._literal_word_value(raw)
    if literal is None or len(literal) < 2:
        return None  # bare ``@`` or a non-decodable word
    nxt = literal[1]
    if nxt in "@ \t\r" or nxt in "&|<>()":
        return None  # ``@@x``, ``@ x``, an operator after ``@``
    if literal.lower().startswith("@echo."):
        # cmd ``@echo.`` (echo a blank line): the dot idiom has no bash
        # spelling, so keep it a loud error instead of stripping to 127
        # ``echo.`` (design §3.4 strip-bail list).
        return None
    if literal.lower() == "@echo" and raw[0] == "@":
        qm = _HSPACE_RE.match(s, word_end, end)
        if qm is not None:
            off_start = qm.end()
            off_end = reader._read_word(off_start, end, scan_substitutions=False)
            if off_end > off_start:
                off_lit = reader._literal_word_value(s[off_start:off_end])
                if off_lit in ("off", "on") and reader._at_boundary_ok(
                    off_end, end
                ):
                    repl = "set +x" if off_lit == "off" else "set -x"
                    return AtPrefixDecision(
                        off_end,
                        "statement",
                        (word_start, off_end, repl),
                        s[word_start:off_end],
                    )
        if literal.lower().startswith("@echo."):
            return None  # cmd ``@echo.`` idiom: keep it loud
    if raw[0] != "@":
        return None  # quoted-leading @ bails v1 (landed doctrine)
    return AtPrefixDecision(
        word_start + 1, "strip", (word_start, word_start + 1, ""), None
    )


def at_boundary_ok(source: str, pos: int, end: int) -> bool:
    """Return whether the char after an ``@echo off|on`` word ends the form.

    Byte-for-byte port of ``_BashFixScanner._at_boundary_ok`` (a pure function
    of the source).  A statement boundary (newline, ``;``, ``&&``, ``|``, a
    redirection, or EOF) lets the two-word translation stand; a single
    backgrounding ``&`` (cmd runs the tail inline per line, bash would
    background it) bails to the generic strip, as do glued words like
    ``@echo offline``.
    """
    s = source
    while pos < end and s[pos] in " \t\r":
        pos += 1
    if pos >= end:
        return True
    ch = s[pos]
    if ch == "&":
        return pos + 1 < end and s[pos + 1] == "&"  # ``&&`` ok, ``&`` not
    return ch in "\n;|<>"


# ── ``if`` comparison-side primitives (read-only pure functions) ──────────────


def split_top_eq(raw: str) -> Optional[Tuple[str, str]]:
    r"""Split a glued cmd ``LHS==RHS`` compare word on the first ``==``.

    Byte-for-byte port of ``_BashFixScanner._split_top_eq``.  cmd.exe toggles a
    double-quoted region on every ``"``, so a ``==`` inside quotes is data, not
    the operator.  Returns ``(lhs_raw, rhs_raw)`` on the first *top-level*
    ``==`` (empty sides allowed and rejected later by the operand validator) or
    ``None`` when the word carries no top-level ``==``.  Only the glued
    single-word form is recognised: a spaced ``LHS == RHS`` breaks the
    single-word rule and is a deliberate v1 bail (cmd itself treats ``== RHS``
    as junk arguments -- probed).
    """
    in_quote = False
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == '"':
            in_quote = not in_quote
        elif not in_quote and ch == "=" and i + 1 < n and raw[i + 1] == "=":
            return raw[:i], raw[i + 2:]
        i += 1
    return None


def cmd_cmp_side(
    raw: str, *, folded: bool, numeric: bool
) -> Optional[Tuple[str, bool]]:
    r"""Return ``(bash_operand, was_cmd_quoted)`` for one compare side.

    Byte-for-byte port of ``_BashFixScanner._cmd_cmp_side``.  Accepts only the
    forms cmd compares faithfully as a single token (design §3.1): an exact
    ``%IDENT%`` (mapped to ``${IDENT}``, or ``${IDENT,,}`` under ``/i``),
    ``%errorlevel%`` (case-insensitive, maps to ``$?``), an integer literal for
    ``numeric`` arithmetic, or a possibly double-quoted string literal whose
    decoded value contains no ``\ " ' % ! $ `` backtick, glob, or shell-control
    character.  The outer quotes are reported via ``was_cmd_quoted`` so the
    caller can enforce cmd's quote-as-data semantics (asymmetric quoting
    bails).  ``None`` bails the whole conditional.
    """
    quoted = False
    core = raw
    if (
        len(raw) >= 2
        and raw[0] == '"'
        and raw[-1] == '"'
        and '"' not in raw[1:-1]
    ):
        quoted = True
        core = raw[1:-1]
    if (
        len(core) >= 2
        and core[0] == "%"
        and core[-1] == "%"
        and core.count("%") == 2
    ):
        name = core[1:-1]
        if name.lower() == "errorlevel":
            return "$?", quoted
        if _NAME_RE.match(name):
            if numeric:
                return "${" + name + "}", quoted
            if folded:
                return "${" + name + ",,}", quoted
            return "${" + name + "}", quoted
        return None  # %partial%, %~ni%, %%..%
    if numeric:
        return (core, quoted) if _INT_RE.match(core) else None
    if core == "":
        return None  # an empty cmd operand is not supported here
    if any(
        ch in core
        for ch in "\\\"'!$`()*?[]{}~<>;%&|"  # value must be plain text
    ):
        return None
    value = core.lower() if folded else core
    return '"' + value + '"', quoted


def build_cmd_cmp(
    lhs_raw: str, rhs_raw: str, op: str, *, folded: bool, numeric: bool
) -> Optional[str]:
    """Build the bash condition text for a cmd compare/arithmetic pair.

    Byte-for-byte port of ``_BashFixScanner._build_cmd_cmp``.  ``numeric``
    emits the ``(( ))`` C operators from :data:`_CMD_REL_OPS`; the string form
    emits ``[[ LHS == "RHS" ]]`` with the right-hand side always quoted
    (``[[ ]]`` treats an unquoted right side as a pattern) and both sides' cmd
    quoting symmetric (an asymmetric ``a=="a"`` is FALSE in cmd but TRUE under
    a naive ``==`` -- design §3.1 -- so it bails).  ``/i`` folding applies ``,,``
    to expansion sides and lowercases literal sides; it is inert for arithmetic
    (numbers compare the same either way).
    """
    lhs = cmd_cmp_side(lhs_raw, folded=folded, numeric=numeric)
    rhs = cmd_cmp_side(rhs_raw, folded=folded, numeric=numeric)
    if lhs is None or rhs is None:
        return None
    lhs_tok, lhs_quoted = lhs
    rhs_tok, rhs_quoted = rhs
    if not numeric and lhs_quoted != rhs_quoted:
        return None  # cmd compares the quote characters themselves
    if numeric:
        return f"{lhs_tok} {op} {rhs_tok}"
    rhs_str = rhs_tok if rhs_tok.startswith('"') else '"' + rhs_tok + '"'
    return f"{lhs_tok} == {rhs_str}"


def cmd_if_branch_ok(source: str, start: int, stop: int) -> bool:
    """Return whether the cmd branch body ``[start, stop)`` is translatable.

    Byte-for-byte port of ``_BashFixScanner._cmd_if_branch_ok``.  Rejects (so
    the whole conditional stays byte-for-byte): bodies containing a newline
    (multi-line cmd conditional — out of scope, and a trailing comment or
    heredoc body would swallow the inserted ``;``/``; fi`` terminator), empty
    bodies (Bash rejects ``then ;``/``else ;``), and bodies holding a ``#`` that
    starts a Bash comment (cmd.exe has no ``#`` comments, so a genuine cmd
    branch never hits this; the scan tracks quotes and backslashes, and errs
    conservative inside nested substitutions).
    """
    body = source[start:stop]
    if "\n" in body or not body.strip():
        return False
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "'":
            close = body.find("'", i + 1)
            if close < 0:
                return False
            i = close + 1
            continue
        if ch == '"':
            i += 1
            while i < n and body[i] != '"':
                if body[i] == "\\":
                    i += 1
                i += 1
            i += 1
            continue
        if ch == "#" and (i == 0 or body[i - 1] in " \t;&|()<>"):
            return False
        i += 1
    return True


# ── cmd ``if`` validation + commit (plan §3.5 priority 2) ─────────────────────


def validate_cmd_if(
    reader: Any, source: str, if_start: int, pos: int, end: int
) -> Optional[_CmdIfMatch]:
    """Validate the cmd conditional shape; strictly read-only.

    Byte-for-byte port of ``_BashFixScanner._match_cmd_if``.  *if_start* is the
    start of the command-position ``if`` word and *pos* the index just past it.
    The shape (design ``.kimix_cache/cmd_if_exist_design.md`` §2): horizontal
    whitespace only between every token, an optional literal ``not``, the
    literal word ``exist``, exactly one operand decoding to a non-empty literal,
    a mandatory ``(`` anchor (the guaranteed-error marker), a matched ``)``, and
    an optional ``else`` tail that is either ``else (`` (final branch) or
    ``else if`` (an ``elif`` chain segment, parsed iteratively here so chain
    length costs no recursion depth).  A newline before the ``(`` or hiding an
    ``else`` means a multi-line cmd conditional (out of scope): bail.  A bare
    command after ``else``, an empty branch, or a branch whose single line
    carries a Bash comment (it would swallow the inserted ``; fi``) also bail.
    Every word read uses ``scan_substitutions=False`` so a bail can never have
    recorded (or duplicated) edits inside ``$(...)``/backtick regions.  Returns
    ``None`` on any deviation.  ``reader`` must expose ``_read_word``,
    ``_literal_word_value`` and ``_find_matching`` (the live scanner passes
    ``self``; the IR wrappers build a scanner over the same source).
    """
    s = source

    def space(p: int) -> int:
        m = _HSPACE_RE.match(s, p, end)
        return m.end() if m is not None else p

    def word(p: int) -> Tuple[Optional[str], int, int]:
        q = space(p)
        r = reader._read_word(q, end, scan_substitutions=False)
        if r <= q:
            return None, q, q
        return reader._literal_word_value(s[q:r]), q, r

    segments: List[_CmdIfSegment] = []
    header_start = if_start
    while True:
        negated = False
        folded = False
        # Optional ``not`` and ``/i`` flags in either order (``/i`` is
        # only meaningful for compare/arithmetic; before exist/defined/
        # errorlevel it is invalid cmd and bails below, same as the old
        # single-keyword matcher).
        kw_lit, kw_start, kw_end = word(pos)
        for _ in range(2):
            if kw_lit == "not":
                if negated:
                    return None
                negated = True
                kw_lit, kw_start, kw_end = word(kw_end)
            elif kw_lit == "/i":
                if folded:
                    return None
                folded = True
                kw_lit, kw_start, kw_end = word(kw_end)
            else:
                break
        if kw_lit is None:
            return None
        kind = "exist"
        cmp_text: Optional[str] = None
        if kw_lit == "exist":
            if folded:
                return None  # ``if /i exist`` is invalid cmd
            op_start = space(kw_end)
            op_end = reader._read_word(op_start, end, scan_substitutions=False)
            if op_end <= op_start:
                return None
            operand_raw = s[op_start:op_end]
            if reader._literal_word_value(operand_raw) in (None, ""):
                return None
            # cmd ``if exist`` matches wildcards; ``[[ -e ]]`` tests the
            # literal name (``[[ ]]`` performs no pathname expansion), so
            # an operand holding ``*``/``?``/``[`` cannot be translated
            # faithfully: leave the whole conditional byte-for-byte.
            if any(ch in operand_raw for ch in "*?["):
                return None
            # A quoted operand that still contains backslashes: quotes are
            # meaningless to cmd.exe (every ``\`` is a path separator
            # there) but make the word literal data for Bash, and the
            # scanner never rewrites quoted words.  ``[[ -e "D:\My Dir" ]]``
            # would then silently test the wrong name; better to leave the
            # loud syntax error untouched than to take the wrong branch.
            if "\\" in operand_raw and operand_raw[:1] in {"'", '"'}:
                return None
        elif kw_lit == "defined":
            if folded:
                return None
            op_start = space(kw_end)
            op_end = reader._read_word(op_start, end, scan_substitutions=False)
            if op_end <= op_start or not _NAME_RE.match(
                s[op_start:op_end]
            ):
                return None  # a defined operand must be a bare identifier
            kind = "defined"
        elif kw_lit == "errorlevel":
            if folded:
                return None
            op_start = space(kw_end)
            op_end = reader._read_word(op_start, end, scan_substitutions=False)
            if op_end <= op_start or not _ERRORLEVEL_RE.match(
                s[op_start:op_end]
            ):
                return None  # an errorlevel operand must be a plain integer
            kind = "errorlevel"
        else:
            # compare / arithmetic: the condition is the token(s) starting
            # at ``kw_start``.  String compare is a glued single word
            # ``LHS==RHS``; arithmetic uses the spaced cmd relational
            # words ``LHS rel RHS`` (equ/neq/lss/leq/gtr/geq -- glued
            # ``5geq3`` is invalid cmd).  Every operand restriction and
            # bail lives in :func:`build_cmd_cmp`.
            op_start = kw_start
            cond_raw = s[kw_start:kw_end]
            eq_split = split_top_eq(cond_raw)
            if eq_split is not None:
                cmp_text = build_cmd_cmp(
                    eq_split[0], eq_split[1], "==",
                    folded=folded, numeric=False,
                )
                if cmp_text is None:
                    return None
                op_end = kw_end
                kind = "compare"
            else:
                rel_lit, _, rel_end = word(kw_end)
                cmd_op = _CMD_REL_OPS.get(rel_lit or "")
                if cmd_op is None:
                    return None  # neither glued == nor a relational word
                rhs_lit, rhs_start, rhs_end = word(rel_end)
                if rhs_lit is None:
                    return None
                cmp_text = build_cmd_cmp(
                    cond_raw, s[rhs_start:rhs_end], cmd_op,
                    folded=folded, numeric=True,
                )
                if cmp_text is None:
                    return None
                op_end = rhs_end
                kind = "arithmetic"
        open_pos = space(op_end)
        if open_pos >= end or s[open_pos] != "(":
            return None
        close_pos = reader._find_matching(open_pos + 1, end, ")")
        if close_pos >= end:
            return None
        if not cmd_if_branch_ok(s, open_pos + 1, close_pos):
            return None
        segments.append(
            _CmdIfSegment(
                header_start, negated, op_start, op_end,
                open_pos, close_pos, kind, folded, cmp_text,
            )
        )
        # Optional ``else`` tail.  The next word is probed across
        # whitespace *including* newlines: a keyword behind a newline
        # marks a multi-line cmd conditional (out of scope, bail rather
        # than half-translate); otherwise an ``else`` continues the
        # chain and anything else (control operator, EOF, another word)
        # closes the conditional in the single-branch form.
        q = close_pos + 1
        crossed_newline = False
        while q < end and s[q] in " \t\r\n":
            if s[q] == "\n":
                crossed_newline = True
            q += 1
        lit, else_start, else_end = word(q)
        if lit != "else":
            if close_pos + 1 < end and s[close_pos + 1] not in _CMD_IF_TAIL_SAFE_CHARS:
                return None  # a word glued to `)` would corrupt the `; fi`
            return _CmdIfMatch(tuple(segments), None, None)
        if crossed_newline:
            return None
        nxt = space(else_end)
        if nxt >= end:
            return None  # dangling ``else``
        if s[nxt] == "(":
            else_close = reader._find_matching(nxt + 1, end, ")")
            if else_close >= end:
                return None
            if not cmd_if_branch_ok(s, nxt + 1, else_close):
                return None
            if (else_close + 1 < end
                    and s[else_close + 1] not in _CMD_IF_TAIL_SAFE_CHARS):
                return None
            return _CmdIfMatch(tuple(segments), nxt, else_close)
        lit, _, if_end2 = word(nxt)
        if lit != "if":
            return None  # bare command branch: extent is ambiguous
        header_start = else_start
        pos = if_end2


def commit_cmd_if(reader: Any, source: str, match: _CmdIfMatch) -> int:
    """Emit the edits for a validated cmd conditional and scan its bodies.

    Byte-for-byte port of ``_BashFixScanner._commit_cmd_if``.  All edits are
    disjoint by construction (design §4.3): the H/EL header edit stops at the
    operand, the operand path rewrite stops at the operand end, the O edit
    spans operand-end..``)``-inclusive, branch-inner edits live strictly inside
    the parentheses, and the C/E close edits start at the closing ``)``.  Parens
    are deleted, never turned into subshells: a cmd group shares the interpreter
    context (``cd`` inside a branch persists), which plain ``then``/``else``
    grouping preserves.  ``reader`` is the live scanner (``self``): the commit
    appends ``reader.edits``/``reader.path_notes`` and recursively scans each
    branch body with ``reader._scan_range`` (the scan FLOW stays in the
    scanner).  Returns the index just past the whole conditional.
    """
    s = source
    last = len(match.segments) - 1
    for index, seg in enumerate(match.segments):
        keyword = "if" if index == 0 else "elif"
        neg = "! " if seg.negated else ""
        if seg.kind == "defined":
            reader.edits.append(
                (seg.header_start, seg.op_start, f"{keyword} [[ {neg}-n ${{")
            )
            reader.edits.append(
                (seg.op_end, seg.open_pos + 1, "+x} ]]; then ")
            )
        elif seg.kind == "errorlevel":
            head = "$? < " if seg.negated else "$? >= "
            reader.edits.append(
                (seg.header_start, seg.op_start, f"{keyword} (( {head}")
            )
            reader.edits.append(
                (seg.op_end, seg.open_pos + 1, " )); then ")
            )
        elif seg.kind in ("compare", "arithmetic"):
            assert seg.cmp_text is not None
            if seg.kind == "compare":
                reader.edits.append(
                    (seg.header_start, seg.op_start, f"{keyword} [[ {neg}")
                )
                reader.edits.append(
                    (seg.op_end, seg.open_pos + 1, " ]]; then ")
                )
            else:
                if seg.negated:
                    reader.edits.append(
                        (seg.header_start, seg.op_start,
                         f"{keyword} (( ! (")
                    )
                    reader.edits.append(
                        (seg.op_end, seg.open_pos + 1, " ) )); then ")
                    )
                else:
                    reader.edits.append(
                        (seg.header_start, seg.op_start, f"{keyword} (( ")
                    )
                    reader.edits.append(
                        (seg.op_end, seg.open_pos + 1, " )); then ")
                    )
            reader.edits.append((seg.op_start, seg.op_end, seg.cmp_text))
        else:  # ``exist`` (the landed default)
            test = (
                f"{keyword} [[ ! -e "
                if seg.negated
                else f"{keyword} [[ -e "
            )
            reader.edits.append((seg.header_start, seg.op_start, test))
            operand = s[seg.op_start:seg.op_end]
            replacement = path_replacement(operand)
            if replacement is not None:
                reader.edits.append((seg.op_start, seg.op_end, replacement))
                reader.path_notes.append(operand)
            reader.edits.append((seg.op_end, seg.open_pos + 1, " ]]; then "))
        reader._scan_range(seg.open_pos + 1, seg.close_pos)
        if match.else_open is None and index == last:
            reader.edits.append((seg.close_pos, seg.close_pos + 1, "; fi"))
        else:
            reader.edits.append((seg.close_pos, seg.close_pos + 1, ";"))
    if match.else_open is None:
        return match.segments[last].close_pos + 1
    else_close = match.else_close
    assert else_close is not None
    # Delete the final ``else (`` paren, keeping the (valid bash) ``else``
    # keyword; glue a space in when the source had ``else(``.
    spacer = "" if s[match.else_open - 1] in " \t\r" else " "
    reader.edits.append((match.else_open, match.else_open + 1, spacer))
    reader._scan_range(match.else_open + 1, else_close)
    reader.edits.append((else_close, else_close + 1, "; fi"))
    return else_close + 1


# ── cmd ``for`` validation + commit (plan §3.5 priority 2) ────────────────────


def cmd_for_item_ok(item: str) -> bool:
    """Return whether a cmd ``for`` list item is plain translatable data.

    Byte-for-byte port of ``_BashFixScanner._cmd_for_item_ok``.  Rejects
    quote-delimited items (``"a b"``, commas inside them are data in cmd) and
    any runtime/expansion or cmd-escape character.  Glob characters are
    explicitly allowed: cmd expands the set at run time and bash's ``for`` list
    expands its globs at run time too.
    """
    if item[:1] in {"'", '"'} or item[-1:] in {"'", '"'}:
        return False
    return not any(ch in item for ch in "$`!%^")


def cmd_body_has_bare_ampersand(body: str) -> bool:
    """Return whether a cmd ``for`` body holds a single (backgrounding) ``&``.

    Byte-for-byte port of ``_BashFixScanner._cmd_body_has_bare_ampersand``.  A
    run of one ``&`` outside quotes backgrounds the preceding command in bash
    but sequences it inline in cmd, so it bails; ``&&`` (and ``||``) run
    faithfully per iteration and is allowed.  Quote-aware.
    """
    i = 0
    n = len(body)
    in_single = False
    while i < n:
        ch = body[i]
        if ch == "\\" and not in_single:
            i += 2
            continue
        if ch == "'":
            in_single = not in_single
            i += 1
            continue
        if in_single or ch != "&":
            i += 1
            continue
        run = 1
        while i + run < n and body[i + run] == "&":
            run += 1
        if run == 1:
            return True
        i += run
    return False


def validate_cmd_for_list(
    reader: Any, source: str, i: int, close: int
) -> Optional[Tuple[Tuple[Tuple[int, int], ...], Tuple[Tuple[int, int, str], ...]]]:
    r"""Validate a cmd ``for`` item list and emit its data edits.

    Byte-for-byte port of ``_BashFixScanner._match_cmd_for_list``.  Splits the
    region on whitespace and on cmd's comma item separator.  Each item must
    decode to plain data (no leading/trailing quote, and no
    ``$``/backtick/``!``/``%``/``^``); bare glob items are allowed (cmd and bash
    both expand at run time).  Every comma becomes a single-character space
    edit; every comma-free item gets the ordinary Windows path rewrite.
    Returns ``(comma_spans, list_path_edits)`` (path edits never overlap the
    comma spans, keeping all committed edits disjoint) or ``None`` to bail the
    whole loop.  ``reader`` must expose ``_read_word``.
    """
    s = source
    commas: List[Tuple[int, int]] = []
    paths: List[Tuple[int, int, str]] = []
    while i < close:
        ch = s[i]
        if ch in " \t\r":
            i += 1
            continue
        word_end = reader._read_word(i, close, scan_substitutions=False)
        if word_end <= i:
            i += 1
            continue
        seg_start = i
        k = i
        while k < word_end:
            if s[k] == "," or k == word_end - 1:
                item_end = k if s[k] == "," else k + 1
                item = s[seg_start:item_end]
                if item and not cmd_for_item_ok(item):
                    return None
                if item:
                    repl = path_replacement(item)
                    if repl is not None:
                        paths.append((seg_start, item_end, repl))
                if s[k] == ",":
                    commas.append((k, k + 1))
                    seg_start = k + 1
                k += 1
                continue
            k += 1
        i = word_end
    return tuple(commas), tuple(paths)


def validate_cmd_for_body(
    reader: Any, source: str, start: int, stop: int, name: str
) -> Optional[Tuple[Tuple[int, int], ...]]:
    """Validate a cmd ``for`` do-body and collect its loop-var word spans.

    Byte-for-byte port of ``_BashFixScanner._match_cmd_for_body``.  Returns the
    ``(word_start, word_end)`` span of every whole-word ``%name``/``%%name``
    occurrence (rewritten to ``$name``), or ``None`` to bail: the body must
    contain no ``^`` and no unquoted single ``&`` (``&&``/``|``/``||``/
    redirections run per iteration faithfully in both cmd and bash -- a bare
    ``&`` backgrounds in bash), and every ``%`` must belong to a whole-word
    token equal to the loop variable (so ``%~ni`` modifiers, ``%ENVVAR%``
    references and glued ``file_%name`` partials bail rather than silently
    mis-translating).  ``reader`` must expose ``_read_word``.
    """
    s = source
    body = s[start:stop]
    if "^" in body:
        return None
    if cmd_body_has_bare_ampersand(body):
        return None
    var_edits: List[Tuple[int, int]] = []
    i = start
    while i < stop:
        word_end = reader._read_word(i, stop, scan_substitutions=False)
        if word_end <= i:
            i += 1
            continue
        if "%" in s[i:word_end]:
            if s[i:word_end] not in ("%" + name, "%%" + name):
                return None
            var_edits.append((i, word_end))
        i = word_end
    return tuple(var_edits)


def validate_cmd_for(
    reader: Any, source: str, for_start: int, pos: int, end: int
) -> Optional[_CmdForMatch]:
    r"""Validate the cmd ``for`` loop shape; strictly read-only.

    Byte-for-byte port of ``_BashFixScanner._match_cmd_for``.  *for_start* is
    the start of the command-position ``for`` word and *pos* the index just
    past it.  Horizontal whitespace only between every token (single line), a
    ``%name``/``%%name`` loop token, the literal ``in``, a matched
    parenthesised list that closes before the first newline, whitespace then
    the literal ``do``, and a one-line body.  The list items must be plain data
    (no ``$``/backtick/``!``/``%``/``^`` or quote-delimited items); bare glob
    items are allowed (cmd and bash both expand at run time).  Every ``%`` in
    the body must be a whole-word occurrence of the loop token (other ``%``
    forms -- ``%~ni`` modifiers, ``%ENVVAR%``, glued ``file_%i`` -- bail the
    whole loop), and the body must hold no unquoted single ``&`` and no ``^``.
    Commas are cmd item separators and become single-character space edits.
    Returns ``None`` on any deviation.  ``reader`` must expose ``_read_word``,
    ``_literal_word_value`` and ``_find_matching``.
    """
    s = source

    def space(p: int) -> int:
        m = _HSPACE_RE.match(s, p, end)
        return m.end() if m is not None else p

    def word(p: int) -> Tuple[Optional[str], int, int]:
        q = space(p)
        r = reader._read_word(q, end, scan_substitutions=False)
        if r <= q:
            return None, q, q
        return reader._literal_word_value(s[q:r]), q, r

    # 1. loop token
    _, tok_start, tok_end = word(pos)
    if tok_end <= tok_start:
        return None
    token = _FOR_LOOP_TOKEN_RE.match(s[tok_start:tok_end])
    if token is None:
        return None
    name = token.group(2)

    # 2. ``in``
    lit, in_start, in_end = word(tok_end)
    if lit != "in":
        return None

    # 3. ``(`` .. matching ``)`` closing before the first newline
    open_pos = space(in_end)
    if open_pos >= end or s[open_pos] != "(":
        return None
    close_pos = reader._find_matching(open_pos + 1, end, ")")
    if close_pos >= end:
        return None
    newline = s.find("\n", open_pos + 1, close_pos + 1)
    if newline >= 0:
        return None  # a list paren must not span lines

    scanned = validate_cmd_for_list(reader, s, open_pos + 1, close_pos)
    if scanned is None:
        return None
    comma_spans, list_path_edits = scanned

    # 4. whitespace then ``do`` (a glued ``)do`` is cmd-legal but bails v1)
    do_lit, do_start, do_end = word(close_pos + 1)
    if do_lit != "do" or do_start <= close_pos + 1:
        return None
    # 5. body = rest of the physical line
    line_end = s.find("\n", do_end, end)
    if line_end < 0:
        line_end = end
    if line_end <= do_end:
        return None  # empty body
    # A parenthesised do-body ``do ( ... )`` runs the cmd group once per
    # item; turning it into a bash subshell would drop the shared
    # interpreter context, so it bails (design §4).
    if s[space(do_end):end][:1] == "(":
        return None
    if not cmd_if_branch_ok(s, do_end, line_end):
        return None
    var_edits = validate_cmd_for_body(reader, s, do_end, line_end, name)
    if var_edits is None:
        return None

    return _CmdForMatch(
        for_start, name, tok_start, tok_end,
        open_pos, close_pos, do_end, do_end, line_end,
        comma_spans, list_path_edits, var_edits,
    )


def commit_cmd_for(reader: Any, source: str, match: _CmdForMatch) -> int:
    r"""Emit the edits for a validated cmd ``for`` loop and scan its body.

    Byte-for-byte port of ``_BashFixScanner._commit_cmd_for``.  All edits are
    disjoint by construction (design §3.2): the loop-name token, the two paren
    deletions (``(`` -- deleted, ``)`` -- turned into the ``;`` that separates
    the list from ``do``), the comma ``\u2192`` space edits, the Windows-path
    rewrites on comma-free list items, the whole-word loop-variable ``\u2192``
    ``$name`` rewrites in the body, and the zero-width ``; done`` insertion at
    the end of the physical line.  The body is then scanned as a command
    context so inner ``2>nul`` / path / fallback rewrites and nested cmd
    control-flow fire recursively; the list region was already validated by the
    matcher (its path edits recorded here), so it is not re-scanned and no edit
    can clash with a comma span.  ``reader`` is the live scanner (``self``).
    Returns the index just past the whole loop.
    """
    s = source
    reader.edits.append((match.tok_start, match.tok_end, match.name))
    reader.edits.append((match.open_pos, match.open_pos + 1, ""))
    reader.edits.append((match.close_pos, match.close_pos + 1, ";"))
    for a, b in match.comma_spans:
        reader.edits.append((a, b, " "))
    for a, b, repl in match.list_path_edits:
        reader.edits.append((a, b, repl))
        reader.path_notes.append(s[a:b])
    for a, b in match.var_edits:
        reader.edits.append((a, b, "$" + match.name))
    reader.edits.append((match.body_end, match.body_end, "; done"))
    reader._scan_range(match.body_start, match.body_end)
    return match.body_end


# ── unsupported cmd jump refusal (plan §3.5 priority 3) ───────────────────────


def peek_literal_word(
    reader: Any, source: str, pos: int, end: int
) -> Optional[str]:
    """Return the decoded literal of the word at ``pos`` or ``None``.

    Byte-for-byte port of ``_BashFixScanner._peek_literal_word``.  Horizontal
    whitespace only; read without recording substitution edits
    (validation-only peek).  Returns ``None`` on no word or a non-decodable
    (runtime-expansion) word.  ``reader`` must expose ``_read_word`` and
    ``_literal_word_value``.
    """
    s = source
    m = _HSPACE_RE.match(s, pos, end)
    q = m.end() if m is not None else pos
    r = reader._read_word(q, end, scan_substitutions=False)
    if r <= q:
        return None
    return reader._literal_word_value(s[q:r])


def cmd_jump_record(
    reader: Any, source: str, raw: str, literal: object, word_end: int, end: int
) -> Optional[str]:
    """Return the refusal key for an untranslatable cmd batch jump, or ``None``.

    Byte-for-byte port of the DECISION part of
    ``_BashFixScanner._record_cmd_jump`` (the unsupported-append stays at the
    call site / reader).  ``goto``, glued ``:label`` lines, ``call :label`` /
    ``call x.bat`` and ``exit /b`` have no faithful bash equivalent (design
    ``.kimix_cache/cmd_control_flow_design.md`` §3.3): arbitrary jumps would run
    linearly past the target, and a translated ``exit N`` kills the persistent
    session / the interactive ``exec bash -i`` startup.  They are refused
    through the existing unsupported channel (``_UNSUPPORTED_BODIES`` ->
    ``BashFix.unsupported`` -> the Bash tool refuses before spawning), so the
    command text stays byte-for-byte.  ``literal`` is the already decoded
    command-word value (``None`` allowed).
    """
    name = literal if isinstance(literal, str) else raw
    if name == "goto" or _GLUED_LABEL_RE.match(name):
        return "goto"
    if name == "call":
        nxt = peek_literal_word(reader, source, word_end, end)
        if nxt is not None and (
            nxt.startswith(":") or _CALL_BATCH_RE.search(nxt)
        ):
            return "call :label"
        return None
    if name == "exit":
        nxt = peek_literal_word(reader, source, word_end, end)
        if nxt is not None and nxt in ("/b", "/B"):
            return "exit /b"
        return None
    return None


def record_cmd_jump(
    reader: Any, source: str, raw: str, literal: object, word_end: int, end: int
) -> bool:
    """Record an untranslatable cmd.exe batch jump at command position.

    Byte-for-byte port of ``_BashFixScanner._record_cmd_jump`` (the thin
    wrapper over :func:`cmd_jump_record` that appends the refusal key to the
    scanner's ``unsupported`` list, dedup-first-wins).  Returns True when a jump
    was recorded; the command text is left untouched.
    """
    key = cmd_jump_record(reader, source, raw, literal, word_end, end)
    if key is None:
        return False
    if key not in reader.unsupported:
        reader.unsupported.append(key)
    return True


# ── IR-side validated structural nodes (plan §3.2 / §3.5 priority 1-2) ────────
#
# ``CmdIfNode`` / ``CmdForNode`` are the IR statement of the SAME validated match
# the scanner's read-only pass produces.  They wrap the ``_CmdIfMatch`` /
# ``_CmdForMatch`` span records with the node kind + source span the §3.5
# dispatcher walks.  They are produced by a read-only pass over
# ``(reader, source, spans)``; committing them is the SAME
# :func:`commit_cmd_if` / :func:`commit_cmd_for` the live scanner runs (so the
# IR and the live scanner share one validate + one commit), and the produced
# edit pairs are disjoint by construction (design §3.2 / §4.3).


@dataclasses.dataclass(frozen=True)
class CmdIfNode:
    """The IR view of a validated cmd.exe conditional (plan §3.5 priority 2).

    ``match`` is the read-only :class:`_CmdIfMatch` validation result;
    ``source``/``if_start``/``if_end``/``end`` record the spans so a
    dispatcher/translator can commit the structural edits (via
    :func:`commit_cmd_if`) or render them into an :class:`EditPlan` without the
    scanner.  A validated ``CmdIfNode`` is *always* translatable: building one
    (via :func:`cmd_if_node`) ran the same read-only validation pass the live
    scanner runs and only returns non-``None`` when the strict single-line
    parenthesised form fully matched.
    """

    match: _CmdIfMatch
    source: str
    if_start: int
    if_end: int
    end: int

    @property
    def span(self) -> Tuple[int, int]:
        return (self.if_start, self.end)

    @property
    def note(self) -> str:
        """The ``cmd_conditional_fixes`` keyword region (== LIVE note text)."""
        return self.source[self.if_start:self.match.segments[0].op_end]


@dataclasses.dataclass(frozen=True)
class CmdForNode:
    """The IR view of a validated cmd.exe ``for`` loop (plan §3.5 priority 2).

    ``match`` is the read-only :class:`_CmdForMatch` validation result.  See
    :class:`CmdIfNode` for the read-only/commit split contract.
    """

    match: _CmdForMatch
    source: str
    for_start: int
    for_end: int
    end: int

    @property
    def span(self) -> Tuple[int, int]:
        return (self.for_start, self.end)

    @property
    def note(self) -> str:
        """The ``cmd_conditional_fixes`` region (== LIVE note text)."""
        return self.source[self.for_start:self.match.close_pos + 1]


def cmd_if_node(
    reader: Any, source: str, if_start: int, if_end: int, end: int
) -> Optional[CmdIfNode]:
    """Build a validated :class:`CmdIfNode` from a command-position ``if`` word.

    Runs the read-only :func:`validate_cmd_if` pass; returns ``None`` (and
    records nothing) unless the strict single-line parenthesised form fully
    validates.  ``reader`` is the live scanner or a scanner built over the same
    source (the IR wrappers build ``_BashFixScanner(source)``).
    """
    match = validate_cmd_if(reader, source, if_start, if_end, end)
    if match is None:
        return None
    return CmdIfNode(match, source, if_start, if_end, end)


def cmd_for_node(
    reader: Any, source: str, for_start: int, for_end: int, end: int
) -> Optional[CmdForNode]:
    """Build a validated :class:`CmdForNode` from a command-position ``for`` word.

    Runs the read-only :func:`validate_cmd_for` pass; returns ``None`` (and
    records nothing) unless the strict single-line parenthesised loop fully
    validates.
    """
    match = validate_cmd_for(reader, source, for_start, for_end, end)
    if match is None:
        return None
    return CmdForNode(match, source, for_start, for_end, end)


def cmd_at_decision(
    reader: Any, source: str, word_start: int, word_end: int, end: int
) -> Optional[AtPrefixDecision]:
    """Alias for :func:`at_prefix_rule` (the IR-side leading-``@`` predicate)."""
    return at_prefix_rule(reader, source, word_start, word_end, end)


def cmd_jump_refusal(
    reader: Any, source: str, raw: str, literal: object, word_end: int, end: int
) -> Optional[str]:
    """Alias for :func:`cmd_jump_record` (the IR-side jump refusal predicate)."""
    return cmd_jump_record(reader, source, raw, literal, word_end, end)


# ── structural edit splices as a flat list (the disjoint-by-construction set) ─
#
# These render the validated match's *outer* structural edits WITHOUT recursing
# into the bodies (the bodies share the scanner's recursive _scan_range in the
# LIVE path and the IR translator's child-node walk in Phase 7).  They are the
# set a dispatcher feeds into an :class:`EditPlan` so the single overlap check
# validates the structural invariant on every case.


def cmd_if_structural_edits(
    source: str, match: _CmdIfMatch
) -> List[Tuple[int, int, str]]:
    """The outer ``if`` header/close/else edits (no body recursion) of a match.

    Produces exactly the edits :func:`commit_cmd_if` would append *for the
    conditional structure itself* (header splices, ``exist`` operand path
    rewrites, the ``)``->``;``/``; fi`` close edits, the ``else (`` paren
    delete) -- the branch-body recursive scan is omitted, so the returned list
    is a flat, pairwise-disjoint set over the conditional's own spans.
    """
    s = source
    edits: List[Tuple[int, int, str]] = []
    last = len(match.segments) - 1
    for index, seg in enumerate(match.segments):
        keyword = "if" if index == 0 else "elif"
        neg = "! " if seg.negated else ""
        if seg.kind == "defined":
            edits.append((seg.header_start, seg.op_start, f"{keyword} [[ {neg}-n ${{"))
            edits.append((seg.op_end, seg.open_pos + 1, "+x} ]]; then "))
        elif seg.kind == "errorlevel":
            head = "$? < " if seg.negated else "$? >= "
            edits.append((seg.header_start, seg.op_start, f"{keyword} (( {head}"))
            edits.append((seg.op_end, seg.open_pos + 1, " )); then "))
        elif seg.kind in ("compare", "arithmetic"):
            assert seg.cmp_text is not None
            if seg.kind == "compare":
                edits.append((seg.header_start, seg.op_start, f"{keyword} [[ {neg}"))
                edits.append((seg.op_end, seg.open_pos + 1, " ]]; then "))
            else:
                if seg.negated:
                    edits.append((seg.header_start, seg.op_start, f"{keyword} (( ! ("))
                    edits.append((seg.op_end, seg.open_pos + 1, " ) )); then "))
                else:
                    edits.append((seg.header_start, seg.op_start, f"{keyword} (( "))
                    edits.append((seg.op_end, seg.open_pos + 1, " )); then "))
            edits.append((seg.op_start, seg.op_end, seg.cmp_text))
        else:  # ``exist``
            test = f"{keyword} [[ ! -e " if seg.negated else f"{keyword} [[ -e "
            edits.append((seg.header_start, seg.op_start, test))
            operand = s[seg.op_start:seg.op_end]
            replacement = path_replacement(operand)
            if replacement is not None:
                edits.append((seg.op_start, seg.op_end, replacement))
            edits.append((seg.op_end, seg.open_pos + 1, " ]]; then "))
        if match.else_open is None and index == last:
            edits.append((seg.close_pos, seg.close_pos + 1, "; fi"))
        else:
            edits.append((seg.close_pos, seg.close_pos + 1, ";"))
    if match.else_open is not None:
        assert match.else_close is not None
        spacer = "" if s[match.else_open - 1] in " \t\r" else " "
        edits.append((match.else_open, match.else_open + 1, spacer))
        edits.append((match.else_close, match.else_close + 1, "; fi"))
    return edits


def cmd_for_structural_edits(
    source: str, match: _CmdForMatch
) -> List[Tuple[int, int, str]]:
    """The outer ``for`` loop edits (no body recursion) of a match.

    The flat pairwise-disjoint set over the loop's own spans: the loop-name
    token, the two paren edits, the comma->space edits, the list path edits, the
    whole-word loop-variable ``$name`` rewrites, and the zero-width ``; done``
    insertion.  The body's recursive scan (inner ``2>nul``/path/nested
    control-flow edits) is omitted.
    """
    edits: List[Tuple[int, int, str]] = []
    edits.append((match.tok_start, match.tok_end, match.name))
    edits.append((match.open_pos, match.open_pos + 1, ""))
    edits.append((match.close_pos, match.close_pos + 1, ";"))
    for a, b in match.comma_spans:
        edits.append((a, b, " "))
    for a, b, repl in match.list_path_edits:
        edits.append((a, b, repl))
    for a, b in match.var_edits:
        edits.append((a, b, "$" + match.name))
    edits.append((match.body_end, match.body_end, "; done"))
    return edits


# ── §3.5 RuleDispatcher slot handlers (priorities 1-3) ────────────────────────


def rule_cmd_at(
    command: "CommandNode", source: str, context: "TranslationContext"
) -> Optional["RuleResult"]:
    """§3.5 priority-1 handler: the leading-``@`` command-word rule.

    Inspects a parsed ``CommandNode``'s command word at its source span.  When
    the word carries a leading ``@`` it runs :func:`at_prefix_rule` (building a
    reader over the same source — the Phase-4/5 IR-wrapper pattern) and returns a
    :class:`RuleResult` carrying the structural edit + the ``cmd_conditional``
    note (statement mode only).  A strip records no note (the re-dispatch of the
    remainder is the Phase-7 orchestrator's job; the edit is returned so the
    dispatcher validates it into the shared plan).  Returns ``None`` for any word
    that is not a recognised ``@`` form (every documented bail).
    """
    from kimix_native.shell_compat.translator import RuleResult

    cw = command.command_word
    if cw is None:
        return None
    word_start, word_end = cw.span.start, cw.span.end
    from kimix_native.shell_compat.bash_core import _BashFixScanner

    decision = at_prefix_rule(
        _BashFixScanner(source), source, word_start, word_end, len(source)
    )
    if decision is None:
        return None
    events: List[Tuple[str, str]] = []
    if decision.note is not None:
        events.append(("record_cmd_conditional", decision.note))
    return RuleResult(edit=decision.edit, events=tuple(events))


def rule_cmd_control(
    command: "CommandNode", source: str, context: "TranslationContext"
) -> Optional["RuleResult"]:
    """§3.5 priority-2 handler: the cmd ``if``/``for`` structural rule.

    Recognises a command word that is the literal ``if`` or ``for`` keyword, runs
    the read-only validate pass (:func:`cmd_if_node` / :func:`cmd_for_node`) and,
    on success, returns the structural note as a report event.  The structural
    edit splices are returned in ``extra_edits`` (a tuple of ``(start, end,
    replacement)``) so the dispatcher feeds them all into the single overlap
    check.  Bails (``None``) on every non-matching form, exactly like the live
    scanner.  Branch-body recursion (path/``nul``/nested control-flow) is the
    Phase-7 translator's child-node walk; the structural edit set this handler
    returns is the match's own disjoint spans (see
    :func:`cmd_if_structural_edits` / :func:`cmd_for_structural_edits`).
    """
    from kimix_native.shell_compat.translator import RuleResult
    from kimix_native.shell_compat.bash_core import _BashFixScanner

    cw = command.command_word
    if cw is None:
        return None
    # Key on the RAW word text, exactly like the live scanner's
    # ``if raw == "if"`` / ``if raw == "for"`` arms: a quoted ``"if"`` word
    # decodes to ``if`` but never triggers the cmd hook (byte-for-byte).
    raw = source[cw.span.start:cw.span.end]
    if raw not in ("if", "for"):
        return None
    word_start, word_end = cw.span.start, cw.span.end
    reader = _BashFixScanner(source)
    if raw == "if":
        node = cmd_if_node(reader, source, word_start, word_end, len(source))
        if node is None:
            return None
        return RuleResult(
            edit=None,
            events=(("record_cmd_conditional", node.note),),
            extra_edits=tuple(cmd_if_structural_edits(source, node.match)),
        )
    node = cmd_for_node(reader, source, word_start, word_end, len(source))
    if node is None:
        return None
    return RuleResult(
        edit=None,
        events=(("record_cmd_conditional", node.note),),
        extra_edits=tuple(cmd_for_structural_edits(source, node.match)),
    )


def rule_cmd_jump(
    command: "CommandNode", source: str, context: "TranslationContext"
) -> Optional["RuleResult"]:
    """§3.5 priority-3 handler: the untranslatable cmd jump refusal.

    Records ``goto``/glued ``:label``/``call :label``/``call *.bat|*.cmd``/
    ``exit /b`` through the ``unsupported`` channel WITHOUT a text change (the
    refusal-only result is not a rewrite — ``BashFix.changed`` excludes
    ``unsupported``).  Returns a :class:`RuleResult` carrying only the
    ``record_unsupported`` event; the command text stays byte-for-byte.
    """
    from kimix_native.shell_compat.translator import RuleResult
    from kimix_native.shell_compat.bash_core import _BashFixScanner

    cw = command.command_word
    if cw is None:
        return None
    raw = source[cw.span.start:cw.span.end]
    reader = _BashFixScanner(source)
    key = cmd_jump_record(
        reader, source, raw, cw.literal_value, cw.span.end, len(source)
    )
    if key is None:
        return None
    return RuleResult(events=(("record_unsupported", key),))


__all__ = [
    "AtPrefixDecision",
    "CmdForNode",
    "CmdIfNode",
    "_CmdForMatch",
    "_CmdIfMatch",
    "_CmdIfSegment",
    "_CALL_BATCH_RE",
    "_CMD_IF_TAIL_SAFE_CHARS",
    "_CMD_REL_OPS",
    "_ERRORLEVEL_RE",
    "_FOR_LOOP_TOKEN_RE",
    "_GLUED_LABEL_RE",
    "_INT_RE",
    "at_boundary_ok",
    "at_prefix_rule",
    "build_cmd_cmp",
    "cmd_at_decision",
    "cmd_body_has_bare_ampersand",
    "cmd_cmp_side",
    "cmd_for_item_ok",
    "cmd_for_node",
    "cmd_if_branch_ok",
    "cmd_if_node",
    "cmd_if_structural_edits",
    "cmd_for_structural_edits",
    "cmd_jump_record",
    "cmd_jump_refusal",
    "commit_cmd_for",
    "commit_cmd_if",
    "peek_literal_word",
    "record_cmd_jump",
    "rule_cmd_at",
    "rule_cmd_control",
    "rule_cmd_jump",
    "split_top_eq",
    "validate_cmd_for",
    "validate_cmd_for_body",
    "validate_cmd_for_list",
    "validate_cmd_if",
]
