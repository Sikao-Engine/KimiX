"""PowerShell 7.x -> 5.1 source transforms: region masks, multi-line
region handling, and the line-level ``??`` / ``?.`` / ``?:`` / operator-
chain transforms behind ``pwsh_transform``.

Moved byte-for-byte from ``bin/kimix_native/_shell_compat.py`` (the
process_pwsh.py section, former lines 6394-8268, including its own
``regex``-with-stdlib-fallback import block).  References to
``_PwshScanner`` inside the comments below are documentation-only
(the rule is mirrored there, not imported here; the pwsh_fix module
imports this module, so the reverse import would be circular).
``bin/kimix_native/_shell_compat.py`` is the stable compatibility FACADE over
this package — the implementation lives here, in
``kimix_native.shell_compat.pwsh_transform`` — and re-exports this surface.
"""

from __future__ import annotations

from typing import Callable

# ======================================================================
# process_pwsh.py (kimi-agent) - PS7 to PS5.1 source transform
# ======================================================================


try:
    import regex as re
except ImportError:  # pragma: no cover
    import re  # stdlib: the used patterns are all stdlib-compatible


# ===========================================================================
# Constants
# ===========================================================================

_PS_KEYWORDS = frozenset({
    "begin", "break", "catch", "class", "continue", "data", "define", "do",
    "dynamicparam", "else", "elseif", "end", "enum", "exit", "filter", "finally",
    "for", "foreach", "from", "function", "hidden", "if", "in", "param",
    "process", "return", "static", "switch", "throw", "trap", "try", "until",
    "using", "var", "while",
})



_EXPR_STOP = "=;|&,"

_DEPTH_OPEN = "([{"
_DEPTH_CLOSE = ")]}"


# ===========================================================================
# Low-level scanners — skip over strings, comments, subexpressions
# ===========================================================================

def _scan_single_quoted(code: str, i: int) -> int:
    """Skip a single-quoted string starting at *i*; return index after it."""
    i += 1
    n = len(code)
    while i < n:
        if code[i] == "'":
            if i + 1 < n and code[i + 1] == "'":
                i += 2          # escaped '' → literal single-quote
            else:
                return i + 1     # closing quote
        else:
            i += 1
    return i


def _scan_double_quoted(code: str, i: int) -> int:
    """Skip a double-quoted string starting at *i*; return index after it."""
    i += 1
    n = len(code)
    while i < n:
        ch = code[i]
        if ch == "`" and i + 1 < n:
            i += 2              # backtick-escaped char
        elif ch == '"':
            return i + 1         # closing quote
        elif ch == "$" and i + 1 < n and code[i + 1] == "(":
            i = _skip_subexpression(code, i)
        else:
            i += 1
    return i


def _scan_block_comment(code: str, i: int) -> int:
    """Skip a block comment ``<# ... #>`` starting at *i*; return index after it.

    PowerShell block comments do NOT nest: the comment ends at the first
    ``#>`` (matching ``_PwshScanner._skip_block`` and the real parser).  A
    nested-looking ``<#`` inside the comment is ordinary comment text.
    """
    i += 2
    n = len(code)
    while i < n:
        if code[i] == "#" and i + 1 < n and code[i + 1] == ">":
            return i + 2
        i += 1
    return i


def _skip_subexpression(code: str, start: int) -> int:
    """Skip past a ``$(...)`` sub-expression starting at *start*.

    Returns the index *after* the closing ``)``.
    """
    assert code[start] == "$"
    i = start + 2
    depth = 1
    n = len(code)
    while i < n and depth:
        c = code[i]
        if c == "(":
            depth += 1
            i += 1
        elif c == ")":
            depth -= 1
            i += 1
        elif c == "'":
            i = _scan_single_quoted(code, i)
        elif c == '"':
            i = _scan_double_quoted(code, i)
        elif c == "$" and i + 1 < n and code[i + 1] == "(":
            i = _skip_subexpression(code, i)
        else:
            i += 1
    return i


# ===========================================================================
# Region finders  (strings, comments, here-strings)
# ===========================================================================

def _scan_here_string(code: str, start: int) -> int:
    """Skip a here-string (``@'...'@`` or ``@"..."@``) starting at *start*.

    *start* points to the opening ``@``.
    Returns the index *after* the closing delimiter.
    """
    n = len(code)
    quote = code[start + 1]
    i = start + 2
    seen_newline = False
    line_begin = start + 2  # start of current line
    while i < n:
        if code[i] == "\n":
            seen_newline = True
            line_begin = i + 1
        elif (
            code[i] == quote
            and i + 1 < n
            and code[i + 1] == "@"
            and seen_newline
        ):
            # Closing delimiter must be at the beginning of its own line
            # (only whitespace may precede it on that line)
            if code[line_begin:i].strip() == "":
                return i + 2
        i += 1
    return i


_REGION_START_RE = re.compile(r"[#<@'\"]")


def _build_region_mask(code: str, *, here_strings: bool = True) -> bytearray:
    """Build a region mask for *code* in a single pass.

    Returns a bytearray where 1 means outside regions (code), 0 means inside
    (strings, comments, here-strings), and 2 means live code inside a ``$(...)``
    string interpolation (see :func:`_mask_double_quoted`).

    When *here_strings* is False, here-strings are not detected (used for
    line-level scanning where here-strings cannot be reliably identified).
    """
    n = len(code)
    mask = bytearray(b"\x01" * n)
    _mask_code_span(code, mask, 0, n, here_strings=here_strings)
    return mask


def _mask_code_span(code: str, mask: bytearray, lo: int, hi: int, *, here_strings: bool) -> None:
    """Mask region (0) characters within the code span ``[lo, hi)``.

    Byte 1 marks ordinary code, byte 2 marks interpolated code (inside a
    ``$(...)`` within a double-quoted string), byte 0 marks strings,
    comments and here-strings.  Scanners are clamped to *hi* so a region
    that (pathologically) extends past the span cannot corrupt neighbours.
    """
    find = _REGION_START_RE.search
    i = lo
    while i < hi:
        # Bulk-jump to the next region-opening character ('#', '<', '@',
        # quote): a C-level regex search replaces the per-character walk.
        m = find(code, i)
        if m is None or m.start() >= hi:
            break
        i = m.start()
        c = code[i]
        if c == "<" and i + 1 < hi and code[i + 1] == "#":
            start = i
            i = min(_scan_block_comment(code, i), hi)
            mask[start:i] = b"\x00" * (i - start)
        elif c == "#":
            # ``#`` opens a comment only at a token boundary: PowerShell treats
            # ``foo#c`` (a hash glued to an identifier character) as a single
            # bareword token, not as ``foo`` followed by a comment.  Without
            # this check the mask swallows everything after a glued ``#`` and
            # any ``??``/``?.``/``?:`` on the rest of the line survives into the
            # emitted PS 5.1 text (where it is a parse error).  This mirrors
            # ``_PwshScanner._at_token_start``.
            if i > lo and (code[i - 1].isalnum() or code[i - 1] == "_"):
                i += 1
                continue
            start = i
            newline = code.find("\n", i, hi)
            i = hi if newline < 0 else newline
            mask[start:i] = b"\x00" * (i - start)
        elif c == "@" and i + 1 < hi and code[i + 1] in ("'", '"'):
            if not here_strings:
                i += 1
                continue
            # PowerShell here-strings require the closing delimiter at the
            # beginning of its own line (only whitespace may precede it).
            j = i + 2
            while j < hi and code[j] in " \t\r":
                j += 1
            if j >= hi or code[j] != "\n":
                i += 1  # Not a here-string: @' or @" not followed by newline
                continue
            start = i
            i = min(_scan_here_string(code, i), hi)
            mask[start:i] = b"\x00" * (i - start)
        elif c == "'":
            start = i
            i = min(_scan_single_quoted(code, i), hi)
            mask[start:i] = b"\x00" * (i - start)
        elif c == '"':
            i = _mask_double_quoted(code, mask, i, hi, here_strings=here_strings)
        else:
            i += 1


def _mask_double_quoted(
    code: str, mask: bytearray, start: int, hi: int, *, here_strings: bool
) -> int:
    """Mask a double-quoted string starting at *start*; return index after it.

    String text is masked 0.  ``$(...)`` interpolations are *live code* in
    PowerShell, so their contents are masked 2 ("interpolated code": truthy
    like ordinary code for the expression transforms, but distinguishable
    from plain code) with nested strings/comments masked 0.  Without this,
    operators inside an interpolation such as ``"$($a ?? 'd')"`` were
    invisible to every transform and survived into the emitted 5.1 text as
    parse errors.
    """
    n = min(len(code), hi)
    i = start + 1
    mask[start] = 0
    while i < n:
        ch = code[i]
        if ch == "`" and i + 1 < n:
            mask[i] = 0
            mask[i + 1] = 0
            i += 2
        elif ch == '"':
            mask[i] = 0
            return i + 1
        elif ch == "$" and i + 1 < n and code[i + 1] == "(":
            sub_end = min(_skip_subexpression(code, i), n)
            mask[i] = 2  # $(
            mask[i + 1] = 2
            if sub_end - 1 > i + 1:
                mask[sub_end - 1] = 2  # closing )
                _mask_code_span(
                    code, mask, i + 2, sub_end - 1, here_strings=here_strings
                )
                # Promote plain code (1) to interpolated code (2); nested
                # strings/comments stay 0, deeper interpolations stay 2.
                for k in range(i + 2, sub_end - 1):
                    if mask[k] == 1:
                        mask[k] = 2
            i = sub_end
        else:
            mask[i] = 0
            i += 1
    return i


def _line_mask(line: str) -> bytearray:
    """Return a region mask for *line* (here-strings disabled)."""
    return _build_region_mask(line, here_strings=False)


# ===========================================================================
# Depth tracking  (for matching ternary colon)
# ===========================================================================

def _compute_depths(line: str, mask: bytearray) -> list[int]:
    """Return nesting depth of ``()``, ``{}``, ``[]`` before each character."""
    depths: list[int] = []
    depth = 0
    for i, ch in enumerate(line):
        depths.append(depth)
        if mask[i]:
            if ch in _DEPTH_OPEN:
                depth += 1
            elif ch in _DEPTH_CLOSE:
                depth -= 1
    depths.append(depth)
    return depths


# ===========================================================================
# Pre-processing: backtick line continuation
# ===========================================================================

def _join_continuation_lines(code: str) -> str:
    """Collapse line-continuations into single logical lines.

    Two flavours continue a statement past a newline in PowerShell 7: an
    explicit backtick, and a trailing binary operator (``&&`` / ``||`` /
    ``??`` / ``??=``) at end of line.  Both are collapsed here so the
    per-line transforms see the whole statement.  Blank lines and comment
    lines between the operator and the operand are skipped (PowerShell
    ignores them while continuing).
    """
    has_op_cont = False
    if "&&" in code or "||" in code or "??" in code:
        has_op_cont = True
    if "`" not in code and not has_op_cont:
        return code
    if "`" in code:
        code = _join_backtick_continuations(code)
    if has_op_cont:
        code = _join_operator_continuations(code)
    return code


def _join_backtick_continuations(code: str) -> str:
    """Collapse explicit backtick line-continuations into logical lines."""
    if "`" not in code:
        return code
    mask = _build_region_mask(code)
    n = len(code)
    result: list[str] = []
    i = 0
    previous = 0
    find = code.find
    while True:
        # Bulk-jump to the next backtick (C-level find) instead of testing
        # every character; only a backtick in a code region can continue.
        i = find("`", i)
        while i != -1 and not mask[i]:
            i = find("`", i + 1)
        if i == -1:
            break
        j = i + 1
        while j < n and code[j] in " \t\r":
            j += 1
        if j < n and code[j] == "\n":
            j += 1
            while j < n and code[j] in " \t\r":
                j += 1
            result.append(code[previous:i])
            result.append(" ")
            previous = j
            i = j
            continue
        if j == i + 1 and j < n and code[j] == "`":
            # Escaped backtick (`` `` ``): a literal backtick character, NOT a
            # line continuation.  Skip the pair so the second backtick is not
            # re-examined and mistaken for a continuation when a newline
            # follows it (real PowerShell keeps the line break).
            i = j + 1
            continue
        i += 1
    result.append(code[previous:])
    return "".join(result)


def _join_operator_continuations(code: str) -> str:
    """Join a statement whose physical line ends with a binary operator.

    PowerShell 7 continues the logical line when a binary operator
    (``&&`` / ``||`` / ``??`` / ``??=``) is the last token of the line; the
    per-line transforms would otherwise see an operator with no operand and
    emit broken or wrongly-gated 5.1 code.  Only code-region tails qualify
    (a ``&&`` inside a string or comment is data).  Blank lines and full-
    comment lines between the operator and the operand are skipped, matching
    PowerShell's continuation.
    """
    mask = _build_region_mask(code)
    n = len(code)
    result: list[str] = []
    pos = 0
    while pos < n:
        nl = code.find("\n", pos)
        if nl == -1:
            result.append(code[pos:])
            break
        line = code[pos:nl]
        stripped = line.rstrip(" \t\r")
        join = False
        if stripped:
            tail_idx = pos + len(line) - len(stripped)
            if stripped.endswith("??=") and mask[tail_idx + len(stripped) - 3]:
                join = True
            elif stripped.endswith(("&&", "||", "??")) and mask[tail_idx + len(stripped) - 2]:
                join = True
        if not join:
            result.append(code[pos:nl + 1])
            pos = nl + 1
            continue
        # Skip blank / comment lines to find the continuation operand.
        j = nl + 1
        while j < n:
            nxt_nl = code.find("\n", j)
            if nxt_nl == -1:
                nxt_nl = n
            nxt = code[j:nxt_nl].strip(" \t\r")
            if not nxt or nxt.startswith("#"):
                j = nxt_nl + 1 if nxt_nl < n else n
                continue
            break
        if j >= n:
            # Nothing to continue onto: leave the newline in place.
            result.append(code[pos:nl + 1])
            pos = nl + 1
            continue
        result.append(code[pos:nl].rstrip(" \t\r") + " ")
        pos = j
    return "".join(result)
    return "".join(result)


# ===========================================================================
# Assignment detection
# ===========================================================================

_ASSIGN_RE = re.compile(r"(.*?)(\$\w+(?::\w+)?(?:\.\w+)*)\s*=\s*$")
_CHAIN_ASSIGN_RE = re.compile(
    r"^(\s*(?:\$\{[^}]+\}|\$\w+(?::\w+)?(?:\.\w+|\[[^\]]*\])*)\s*"
    r"(\+=|-=|\*=|/=|%=|=(?![=~])))\s*(.*)$"
)
"""Match a variable assignment at the START of a chain's left segment.

``$x = A && B`` assigns the COMBINED output of the executed segments in
PowerShell 7, so the sequential rewrite must place the assignment outside
the lifted value: ``$x = $(A; if ($?) { B })`` — assigning inside would
give ``$x`` only the left segment and leak the right one to the output
stream.  Comparison operators (``-eq`` …) never match; ``==``/``=~`` are
excluded by the negative lookahead.  Group 1 is the assignment incl. the
operator, group 3 the remainder of the left segment.
"""
_COMMAND_PREFIX_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*\s+")


def _match_assignment(before: str) -> tuple[str, str] | None:
    """Match an assignment prefix like ``$var = `` at the end of *before*."""
    m = _ASSIGN_RE.match(before.rstrip())
    if m:
        return m.group(1), m.group(2)
    return None


def _build_replacement(prefix: str, inner: str) -> str:
    """Build replacement string, preserving an assignment if one is detected."""
    assign = _match_assignment(prefix)
    if assign:
        p, var = assign
        return f"{p}{var} = {inner}"
    return f"{prefix}{inner}"


def _emit_replacement(prefix: str, inner: str) -> str:
    """Concatenate *prefix* with the generated *inner*, guarding keyword glue.

    ``return``/``throw``/command-name prefixes stripped from an operand leave
    the prefix ending in a word character; concatenating directly produces
    ``return$(...)``, which 5.1 parses as a command name.  Insert a space in
    that case (an assignment rewrite never ends in a word character, so the
    guard cannot misfire there).
    """
    if (
        prefix
        and (prefix[-1].isalnum() or prefix[-1] == "_")
        and _match_assignment(prefix) is None
    ):
        return f"{prefix} {inner}"
    return _build_replacement(prefix, inner)


def _strip_command_prefix(expr: str, start: int, *, check_keywords: bool = True) -> tuple[str, int]:
    """Strip a leading command name (e.g. ``Write-Output ``) from *expr*.

    Returns ``(stripped_expr, adjusted_start)``.
    When *check_keywords* is True (default), PowerShell keywords (if, foreach, …)
    are never stripped. When False, any command prefix is stripped.
    """
    m = _COMMAND_PREFIX_RE.match(expr)
    if m:
        cmd = m.group(0).strip().lower()
        if not check_keywords or cmd not in _PS_KEYWORDS:
            expr_part = expr[m.end():]
            if expr_part and expr_part[0] in '$(["\'@0123456789':
                return expr_part, start + m.end()
    return expr, start


_RETURN_THROW_OPERAND_RE = re.compile(r"^(return|throw)(\s+)(.*)$",
                                      re.IGNORECASE | re.DOTALL)

_REGION_PLACEHOLDER_RE = re.compile(r"__kimixrgn\d+x\d+__")


def _lift_region_placeholders(expr: str) -> tuple[str, list[str]]:
    """Split opaque multi-line-region placeholder words out of *expr*.

    ``pwsh_transform`` splices placeholder words onto lines where a multi-line
    string/comment/here-string region opens or closes mid-line.  A placeholder
    stands for region text (e.g. a block comment) and must survive as a
    standalone token — it must never be folded into a generated ``if (...)``
    operand, or the substitution step would re-inject the region's literal
    text (``#>``) inside the generated expression (a 5.1 parse error).

    Returns ``(clean_expr, placeholders)``.  When the operand consists of
    nothing but placeholders (e.g. a here-string literal used directly as
    the ``??`` value) the placeholders are kept in place — the substitution
    re-injects a valid literal — and an empty ``clean_expr`` is reported so
    the caller does not misread the operand as missing.
    """
    found = _REGION_PLACEHOLDER_RE.findall(expr)
    if not found:
        return expr, []
    cleaned = _REGION_PLACEHOLDER_RE.sub(" ", expr).strip()
    if not cleaned:
        return expr, []
    return cleaned, found


def _keeps_prefix(keeps: list[str]) -> str:
    """Render lifted placeholders as a prefix segment (or "")."""
    return (" " + " ".join(keeps) + " ") if keeps else ""


def _strip_keyword_operand(expr: str, start: int) -> tuple[str, int]:
    """Strip a leading ``return``/``throw`` keyword from *expr*.

    The keyword remains in the prefix (``return $(if ...)``) and only its
    operand is transformed.  Without this the keyword is folded into the
    coalesced operand (``if ($null -ne return $a)`` — a 5.1 parse error).
    ``return``/``throw`` are PowerShell keywords, so
    :func:`_strip_command_prefix` deliberately leaves them in place.
    """
    m = _RETURN_THROW_OPERAND_RE.match(expr)
    if m:
        return m.group(3), start + len(m.group(1)) + len(m.group(2))
    return expr, start


# ===========================================================================
# $? adjacency check — shared by all transforms
# ===========================================================================

def _separate_trailing_comment(line: str, start: int, mask: bytearray
                         ) -> tuple[int, str]:
    """Split off a trailing line comment starting at *start*, if present.

    Returns ``(content_end, comment_str)`` where *content_end* is the index
    at which the content before the comment ends, and *comment_str* is the
    comment text (including the ``#``) or ``""`` if none.
    """
    for ri in range(max(start, 1), len(line)):
        if mask[ri] == 0 and mask[ri - 1] == 1 and line[ri] == "#":
            return ri, line[ri:]
    return len(line), ""


def _after_dollar_question(line: str, op_idx: int) -> bool:
    """Return True if *op_idx* is immediately after ``$?``.

    When True the first ``?`` of the operator at *op_idx* is actually the
    ``?`` of the ``$?`` automatic variable, so the operator should be skipped.

    Does NOT match ``$$?.`` — ``$$`` is a separate automatic variable.
    """
    return (
        op_idx > 0
        and line[op_idx - 1] == "$"
        and not (op_idx > 1 and line[op_idx - 2] == "$")
    )


# ===========================================================================
# Shared colon-context check
# ===========================================================================

def _is_scope_colon(line: str, i: int) -> bool:
    """Return ``True`` if the colon at *i* belongs to a ``$scope:var`` prefix."""
    # Scan backwards from the colon to find the preceding ``$`` variable prefix.
    j = i - 1
    while j >= 0 and (line[j].isalnum() or line[j] == "_"):
        j -= 1
    return j >= 0 and line[j] == "$"


def _is_drive_colon(line: str, i: int) -> bool:
    """Return ``True`` if the colon at *i* is a drive-reference colon.

    A single ASCII letter directly before the colon (itself not part of a
    longer word or a ``$`` reference) followed by ``\\``/``/`` or the end of
    the expression is a drive reference (``C:\\Temp``, ``C:/x``, ``C:``),
    not the branch separator of a ternary — ``?`` is also the Where-Object
    alias, so a line like ``gci | ? {...} ; Write-Output C:\\Temp`` must not
    be mistaken for a ternary whose separator colon is the drive letter.
    """
    if i < 1 or not (line[i - 1].isascii() and line[i - 1].isalpha()):
        return False
    j = i - 2
    if j >= 0 and (line[j].isalnum() or line[j] in "_$"):
        return False
    return i + 1 >= len(line) or line[i + 1] in "\\/ \t')},;|&"


_VALUE_EXPR_TAIL_RE = re.compile(
    r"(?:"
    r"\$[A-Za-z_][\w:]*|\$\?|\$\$|\$\^|\$\{[^}]+\}"  # variable references
    r"|@[A-Za-z_][\w:]*"  # splatting / array literals (@obj, @{...} ends with })
    r"|[)\]}]"  # closed groups / indexers / braces
    r"|'[^']*'|\"[^\"]*\""  # string literals
    r"|[0-9][0-9.]*"  # numeric literals
    r"|\[[\w.]+\]::\w+"  # static member access ([Math]::PI)
    r")(?:\.\w+|\[[^\]]*\]|--|\+\+)*$"
)
"""Matches an expression that ends like a value: a variable reference, string
or numeric literal, closed group, or static member access, with optional
member/index chains (``$obj.Name``, ``$a[0]``, ``'x'``, ``[Math]::PI``)."""


def _looks_like_value_expr(expr: str) -> bool:
    """Return ``True`` when *expr* ends like a value expression.

    The null-conditional (``?.``/``?[``) and null-coalescing (``??``)
    transforms only apply to value expressions; a bareword left operand is
    5.1 command text whose ``?`` characters are filename wildcards
    (``rm f?.bak``, ``Write-Host a??b``) and must be left untouched.
    """
    expr = expr.rstrip()
    if not expr:
        return False
    if expr[-1] in ")]}":
        return True
    return _VALUE_EXPR_TAIL_RE.search(expr) is not None


# ===========================================================================
# Expression boundary helpers
# ===========================================================================

def _is_null_conditional_qmark(line: str, i: int) -> bool:
    """Return True if ``?`` at *i* is part of a null-conditional operator.

    Both flavours count: ``?.`` (member) and ``?[`` (index).  Expression
    scans use this guard to pass through a null-conditional instead of
    treating its ``?`` as a stop character, so a mixed chain such as
    ``$a?[$i]?.b`` keeps its full base (``$a?[$i]``) for the ``?.`` pass.
    """
    return i + 1 < len(line) and line[i + 1] in ".["


def _is_double_colon(line: str, i: int) -> bool:
    """Return True if ``:`` at *i* is part of ``::`` (static member access)."""
    return (i + 1 < len(line) and line[i + 1] == ":") or (i > 0 and line[i - 1] == ":")


def _find_expr_start(line: str, end: int, mask: bytearray,
                     extra_stop: str = "") -> int:
    """Scan backwards from *end* to locate the start of the expression.

    *extra_stop* can contain additional delimiter characters (e.g. ``"?:``
    for null-conditional base scanning).
    """
    depth = 0
    stop_set = _EXPR_STOP + extra_stop if extra_stop else _EXPR_STOP
    for i in range(end - 1, -1, -1):
        if not mask[i]:
            continue
        c = line[i]
        if c in _DEPTH_CLOSE:
            depth += 1
        elif c in _DEPTH_OPEN:
            depth -= 1
            if depth < 0:
                return i + 1
        elif depth == 0 and c in stop_set:
            if c == "?":
                if _is_null_conditional_qmark(line, i):
                    continue
                if _after_dollar_question(line, i):
                    continue
            elif c == ":":
                if _is_double_colon(line, i) or _is_scope_colon(line, i):
                    continue
            return i + 1
    return 0


def _find_expr_end(line: str, start: int, mask: bytearray,
                   extra_stop: str = "") -> int:
    """Scan forwards from *start* to locate the end of the expression.

    *extra_stop* can contain additional delimiter characters (e.g. ``"?:"``
    so a ``??`` operand never swallows a co-located ternary's ``?``/``:``).
    The same context guards as :func:`_find_expr_start` apply: null-conditional
    ``?.``, the ``$?`` automatic variable, ``::`` static access and scope /
    drive colons do not end an expression.
    """
    depth = 0
    stop_set = _EXPR_STOP + extra_stop if extra_stop else _EXPR_STOP
    # Track mask value at the previous position to detect region boundaries.
    prev_mask = 1 if start == 0 else mask[start - 1]
    for i in range(start, len(line)):
        c = line[i]
        cur_mask = mask[i]
        if not cur_mask:
            # If we just stepped from outside (mask=1) into a region and the
            # character is '#', it is the start of a line comment — stop here.
            if prev_mask and c == "#":
                return i
            prev_mask = cur_mask
            continue
        prev_mask = cur_mask
        if c in _DEPTH_OPEN:
            depth += 1
        elif c in _DEPTH_CLOSE:
            depth -= 1
            if depth < 0:
                return i
        elif depth == 0 and c in stop_set:
            if c == "?":
                if _is_null_conditional_qmark(line, i):
                    continue
                if i > 0 and line[i - 1] == "$":
                    continue  # the $? automatic variable
            elif c == ":":
                if (
                    _is_double_colon(line, i)
                    or _is_scope_colon(line, i)
                    or _is_drive_colon(line, i)
                ):
                    continue
            return i
    return len(line)


def _expr_left(line: str, pos: int, mask: bytearray,
               extra_stop: str = "") -> tuple[int, int]:
    """Return (start, end) of the expression immediately left of *pos*."""
    end = pos
    while end > 0 and line[end - 1] == " ":
        end -= 1
    start = _find_expr_start(line, end, mask, extra_stop)
    return start, end


def _expr_right(line: str, pos: int, mask: bytearray,
                extra_stop: str = "") -> tuple[int, int]:
    """Return (start, end) of the expression immediately right of *pos*."""
    start = pos
    while start < len(line) and line[start] == " ":
        start += 1
    end = _find_expr_end(line, start, mask, extra_stop)
    return start, end


def _swallow_comma_operands(line: str, end: int, mask: bytearray) -> int:
    """Extend *end* over ``, <expr>`` continuations of a ``??`` right operand.

    PowerShell's comma operator binds tighter than ``??``, so ``$a ?? $b, 3``
    is ``$a ?? ($b, 3)`` in PowerShell 7.  Two comma flavours must NOT be
    swallowed: commas separating COMMAND ARGUMENTS (``f($a ?? 1, $b)`` — the
    enclosing opener belongs to a command/method call) and commas following a
    completed group (``Write-Output ($a ?? 1), 2`` — the operand already ended
    at the closer, so the char before the comma sits at a deeper level).
    """
    depth_arr = _compute_depths(line, mask)
    j = end
    while True:
        p = j - 1
        while p >= 0 and line[p] in " \t":
            p -= 1
        k = j
        while k < len(line) and line[k] in " \t":
            k += 1
        if (
            p < 0
            or k >= len(line)
            or line[k] != ","
            or not mask[k]
            or depth_arr[k] != depth_arr[p]
        ):
            return j
        d = depth_arr[k]
        if d > 0:
            # Identify the enclosing opener: walk left past nested groups.
            o = k - 1
            nested = 0
            opener = ""
            opener_pos = -1
            while o >= 0:
                if mask[o]:
                    ch = line[o]
                    if ch in ")]}":
                        nested += 1
                    elif ch in "([{":
                        if nested:
                            nested -= 1
                        elif depth_arr[o] == d - 1:
                            opener = ch
                            opener_pos = o
                            break
                o -= 1
            # A word/member directly before the opener marks a command or
            # method argument list: commas there separate arguments.
            if opener and opener_pos > 0 and (
                line[opener_pos - 1].isalnum() or line[opener_pos - 1] in "_)}]"
            ):
                return j
        nstart = k + 1
        while nstart < len(line) and line[nstart] in " \t":
            nstart += 1
        if nstart >= len(line):
            return j
        nend = _find_expr_end(line, nstart, mask)
        if nend <= nstart:
            return j
        j = nend


# ===========================================================================
# Generic operator transform engine
# ===========================================================================

def _find_next_op(line: str, op: str, mask: bytearray,
                  skip_dollar_q: bool = False, start: int = 0,
                  reverse: bool = False, plain_code_only: bool = False) -> int:
    """Find the next (or rightmost when *reverse* is True) occurrence of *op*
    in *line* that is outside regions.

    If *skip_dollar_q* is True, skip positions immediately after ``$?``.
    *start* restricts the search to indices >= *start* (ignored when *reverse*).
    When *plain_code_only* is True, only mask value 1 (ordinary code) matches:
    interpolated code inside ``"$(...)"`` (mask 2) is skipped — the chain
    transform handles interpolation operators in a dedicated second phase
    with region edges as hard boundaries.
    """
    search = line.rfind if reverse else line.find
    idx = search(op, start if not reverse else None)
    while idx != -1:
        if mask[idx] and (not plain_code_only or mask[idx] == 1) \
                and (not skip_dollar_q or not _after_dollar_question(line, idx)):
            return idx
        idx = search(op, idx + 1) if not reverse else line.rfind(op, 0, idx)
    return -1


def _transform_operator(
    line: str,
    op: str,
    builder: Callable,
    *,
    skip_dollar_q: bool = True,
    require_value_left: bool = False,
    operand_extra_stop: str = "",
    swallow_commas: bool = False,
    deep_ternary: bool = False,
) -> tuple[str, list[str]]:
    """Generic single-operator line transformer.

    *op* — the operator string to search for (e.g. ``"??"``).
    *builder* — callable(left_expr, right_expr, right_extra) → inner_str.
    *skip_dollar_q* — when True, skip positions immediately after ``$?``.
    *require_value_left* — when True, only transform when the left operand
      ends like a value expression (see :func:`_looks_like_value_expr`);
      a bareword left operand is 5.1 command text with wildcard characters.
    *operand_extra_stop* — extra delimiter characters for the operand scans
      (e.g. ``"?:"`` so a ``??`` never swallows a co-located ternary).
    *swallow_commas* — extend the right operand over same-depth ``, expr``
      continuations (comma binds tighter than ``??`` in PowerShell 7).
    *deep_ternary* — pre-transform ternaries inside the operands (a
      parenthesized ``( $c ? 1 : 2 )`` operand would otherwise be copied
      verbatim into the generated braces, where the ternary pass — which
      refuses ``?`` inside ``{ }`` — can never reach it).

    Returns ``(transformed_line, warnings)``.
    """
    warnings: list[str] = []
    op_len = len(op)
    if op not in line:
        return line, warnings
    search = 0
    while True:
        mask = _line_mask(line)
        idx = _find_next_op(line, op, mask, skip_dollar_q, search)
        if idx == -1:
            break

        left_start, left_end = _expr_left(line, idx, mask, operand_extra_stop)
        left_expr = line[left_start:left_end].strip()
        left_expr, left_start = _strip_command_prefix(left_expr, left_start)
        left_expr, left_start = _strip_keyword_operand(left_expr, left_start)

        right_start, right_end = _expr_right(
            line, idx + op_len, mask, operand_extra_stop
        )
        if swallow_commas:
            right_end = _swallow_comma_operands(line, right_end, mask)
        right_expr = line[right_start:right_end].strip()
        right_extra = None

        # Region placeholders (spliced onto mid-line region boundaries by
        # ``pwsh_transform``) are opaque tokens: keep them out of the operands
        # so the substitution step never re-injects region text (``#>``)
        # inside the generated expression.
        keeps: list[str] = []
        left_expr, found = _lift_region_placeholders(left_expr)
        keeps.extend(found)
        right_expr, found = _lift_region_placeholders(right_expr)
        keeps.extend(found)

        # Parenthesized ternaries inside an operand would be copied verbatim
        # into the generated ``{ }`` braces, where the ternary pass can never
        # reach them (its brace guard) — pre-transform them here.
        if deep_ternary:
            if "?" in left_expr:
                left_expr, wl = _transform_ternary_line(left_expr)
                warnings.extend(wl)
            if "?" in right_expr:
                right_expr, wl = _transform_ternary_line(right_expr)
                warnings.extend(wl)

        if not left_expr or not right_expr:
            search = idx + 1
            continue
        if require_value_left and not _looks_like_value_expr(left_expr):
            search = idx + 1
            continue

        inner = builder(left_expr, right_expr, right_extra)
        # ``builder`` returns an ``if (...) {...} else {...}`` *statement*.  A
        # bare statement is only legal at pipeline/statement position (or as a
        # full ``$var = ...`` right-hand side); wherever the operator sits in an
        # *expression* position — inside ``(...)``/``[...]``/``{...}``, as a
        # command argument, or directly before a ``|``/``&&``/redirection — the
        # statement must be lifted into a value with ``$( ... )``.  Wrapping
        # unconditionally is semantically identical at statement position (the
        # sub-expression still emits the branch result) and fixes every
        # expression position, e.g. ``(if ...)`` which is a parse error.
        inner = f"$({inner})"
        warnings.append(f"{op} operator `{left_expr} {op} {right_expr}` rewritten to `{inner}`")
        line = _emit_replacement(
            line[:left_start] + _keeps_prefix(keeps), inner
        ) + line[right_end:]
        search = left_start
    return line, warnings


# ===========================================================================
# Transform: null-coalescing assignment  (??=)
# ===========================================================================

def _transform_nca_line(line: str) -> tuple[str, list[str]]:
    """Rewrite null-coalescing assignment ``$var ??= value``.

    Uses the generic expression scanner so braced variables (``${foo}``),
    property chains (``$obj.Name``) and indexed targets (``$arr[0]``) are
    all handled correctly.
    """
    warnings: list[str] = []
    if "??=" not in line:
        return line, warnings
    search = 0
    while True:
        mask = _line_mask(line)
        idx = _find_next_op(line, "??=", mask, True, search)
        if idx == -1:
            break

        left_start, left_end = _expr_left(line, idx, mask)
        var = line[left_start:left_end].strip()
        if not var:
            search = idx + 1
            continue

        val_start, val_end = _expr_right(line, idx + 3, mask)
        value = line[val_start:val_end].strip()
        # A chained coalesce in the value (``$x ??= $y ??= 3`` /
        # ``$x ??= $y ?? 3``) is right-associative in PowerShell 7.  The
        # plain value scan stops at the inner ``??=``'s ``=``, so extend the
        # value over the inner assignment first.
        while (
            value.endswith("??")
            and val_end < len(line)
            and line[val_end] == "="
        ):
            val_end = _find_expr_end(line, val_end + 1, mask)
            value = line[val_start:val_end].strip()
        # Transform the inner operators and lift the result into a ``$()``
        # value (an ``if`` statement emits nothing when assigned directly).
        yield_suffix = ""
        if "??" in value:
            vmask = _line_mask(value)
            inner_idx = _find_next_op(value, "??=", vmask, True)
            if inner_idx != -1:
                is_, ie_ = _expr_left(value, inner_idx, vmask)
                yield_var = value[is_:ie_].strip()
                value, w = _transform_nca_line(value)
                warnings.extend(w)
                # The chained assignment's statement emits nothing; re-yield
                # the assigned variable so the outer ??= assigns its value.
                yield_suffix = f"; {yield_var}"
            value, w = _transform_nc_line(value)
            warnings.extend(w)
            new_inner = (
                f"if ($null -eq {var}) {{ {var} = $({value}{yield_suffix}) }}"
            )
        else:
            new_inner = f"if ($null -eq {var}) {{ {var} = {value} }}"
        warnings.append(
            f"null-coalescing assignment `{var} ??= ...` "
            f"rewritten to `{new_inner}`"
        )
        line = _emit_replacement(line[:left_start].rstrip(), new_inner) + line[val_end:]
        search = left_start
    return line, warnings


# ===========================================================================
# Transform: null-coalescing  (??)
# ===========================================================================

def _transform_nc_line(line: str) -> tuple[str, list[str]]:
    """Transform every ``??`` on *line* into PS 5.1 compatible ``if`` form."""

    def _builder(left: str, right: str, _extra: None) -> str:
        return f"if ($null -ne {left}) {{ {left} }} else {{ {right} }}"

    return _transform_operator(
        line,
        "??",
        _builder,
        require_value_left=True,
        # ``??`` binds tighter than the ternary and the comma operator, so
        # its operands stop at a co-located ternary's ``?``/``:`` and swallow
        # same-level ``, expr`` continuations; parenthesized ternaries inside
        # an operand are pre-transformed (deep_ternary) so no raw ``? :``
        # survives inside the generated braces (a 5.1 parse error there).
        operand_extra_stop="?:",
        swallow_commas=True,
        deep_ternary=True,
    )


# ===========================================================================
# Transform: ternary  (? :)
# ===========================================================================

def _find_matching_colon(line: str, start: int, mask: bytearray,
                         depth_arr: list[int], ref_depth: int = 0) -> int:
    """Find the colon that separates the true/false branches of a ternary.

    The separator colon sits at the *same* bracket-nesting level as the ternary
    ``?`` (``ref_depth``): ``0`` for a statement-level ternary, ``> 0`` when the
    whole ternary is wrapped in parentheses such as ``($c ? $a : $b)``.  Once the
    depth drops below *ref_depth* the enclosing group has closed without a
    separator colon, so there is no matching ternary and we stop.
    """
    for i in range(start, len(line)):
        if not mask[i]:
            continue
        if depth_arr[i] < ref_depth:
            # The ternary's own group (e.g. the wrapping parenthesis) closed
            # before any separator colon appeared -- not a ternary.
            break
        if line[i] != ":" or depth_arr[i] != ref_depth:
            continue
        if _is_double_colon(line, i) or _is_scope_colon(line, i):
            continue
        if _is_drive_colon(line, i):
            continue
        return i
    return -1


def _brace_open_depths(line: str, mask: bytearray) -> list[int]:
    """Number of unclosed ``{`` before each character of *line*.

    A ``:`` nested inside ``{ ... }`` may belong to hashtable / switch /
    script-block syntax rather than a ternary separator, so the caller skips
    ternary detection when the ``?`` sits at a non-zero brace depth.  Characters
    inside strings / comments (``mask`` == 0) never open or close a brace.
    """
    depths = [0] * len(line)
    bc = 0
    for i, ch in enumerate(line):
        depths[i] = bc
        if mask[i]:
            if ch == "{":
                bc += 1
            elif ch == "}":
                bc -= 1
    return depths


def _transform_ternary_line(line: str) -> tuple[str, list[str]]:
    """Rewrite ternary ``$cond ? $true : $false`` into an ``if`` statement."""
    warnings: list[str] = []
    if "?" not in line:
        return line, warnings
    # Positions already tried and found to be incomplete ternaries (guards
    # passed, separator colon found, but an operand came out empty).  They
    # must not be re-selected on the next restart or the loop would spin.
    rejected: set[int] = set()
    while True:
        mask = _line_mask(line)
        depth_arr = _compute_depths(line, mask)
        # Number of unclosed ``{`` before each character.  A ``:`` inside ``{ ... }``
        # may belong to hashtable / switch / script-block syntax rather than a
        # ternary, so a ternary whose ``?`` sits inside an open brace is left
        # untouched (colons nested in parentheses are unambiguous ternary markers).
        brace_open = _brace_open_depths(line, mask)
        # Rewrite the RIGHTMOST eligible ternary first.  Its branch arms may
        # contain nested ternaries; rewriting inner-to-outer keeps each nested
        # ``? :`` out of the emitted ``{ ... }`` arm blocks, where the brace
        # guard would leave them raw (a 5.1 parse error).  Sequential ternaries
        # on one line are unaffected: every restart re-finds the new rightmost.
        pos = len(line) - 1
        colon_pos = -1
        while pos >= 0:
            if pos in rejected:
                pos -= 1
                continue
            if (
                line[pos] == "?"
                and mask[pos]
                and not _after_dollar_question(line, pos)
            ):
                # ``?`` in command position (the Where-Object alias) is not a
                # ternary: it follows a pipe/operator or starts the statement,
                # so there is no condition expression to its left.
                k = pos - 1
                while k >= 0 and line[k] == " ":
                    k -= 1
                if k < 0 or line[k] in "|&;,":
                    pos -= 1
                    continue
                # Inside an open ``{`` the colon is ambiguous (hashtable /
                # switch / script-block): skip, matching the documented
                # brace-depth limitation — with two unambiguous exceptions:
                # a ternary nested inside ``(...)`` within the braces (colons
                # in parentheses are always ternary separators), and a ternary
                # in ``return``/``throw`` operand position (the keyword
                # precedes the condition, so no hashtable/switch colon can
                # be involved).
                if brace_open[pos] > 0 and depth_arr[pos] - brace_open[pos] == 0:
                    cs, ce = _expr_left(line, pos, mask)
                    # ``return``/``throw`` precede the condition INSIDE the
                    # condition text (``return $c ? 1 : 2``); a keyword-led
                    # condition is the unambiguous operand case.
                    if not re.match(
                        r"(?i)^(?:return|throw)[ \t]", line[cs:ce].lstrip()
                    ):
                        pos -= 1
                        continue
                colon_pos = _find_matching_colon(
                    line, pos + 1, mask, depth_arr, depth_arr[pos]
                )
                if colon_pos == -1:
                    pos -= 1
                    continue
                break
            pos -= 1
        if pos < 0:
            break
        cond_start, cond_end = _expr_left(line, pos, mask, extra_stop=":")
        condition = line[cond_start:cond_end].strip()
        true_expr = line[pos + 1:colon_pos].strip()
        false_start, false_end = _expr_right(line, colon_pos + 1, mask)
        # PowerShell's comma operator binds tighter than the ternary, so the
        # false branch swallows comma-separated values at the same nesting
        # level: ``@($c ? 7 : 8, 5)`` is ``@($c ? 7 : (8, 5))`` in PS 7 (ONE
        # array element when the condition is true).  _find_expr_end tracks
        # depth relative to its own scan start, so such a comma would
        # otherwise cut the false arm short and change the emitted semantics.
        ref_depth = depth_arr[colon_pos]
        j = false_end
        while j < len(line):
            k = j
            while k < len(line) and line[k] in " \t":
                k += 1
            if not (
                k < len(line)
                and line[k] == ","
                and mask[k]
                and depth_arr[k] == ref_depth
            ):
                break
            nstart = k + 1
            while nstart < len(line) and line[nstart] in " \t":
                nstart += 1
            if nstart >= len(line):
                break
            nend = _find_expr_end(line, nstart, mask)
            if nend <= nstart:
                break
            j = nend
        false_end = j
        false_expr = line[false_start:false_end].strip()
        suffix = line[false_end:]
        if not _match_assignment(line[:cond_start]):
            condition, cond_start = _strip_command_prefix(condition, cond_start, check_keywords=False)
        condition, cond_start = _strip_keyword_operand(condition, cond_start)
        # Region placeholders (mid-line region boundaries spliced by
        # ``pwsh_transform``) are opaque tokens and must not be folded into
        # the emitted arms — re-injecting region text (``#>``) inside the
        # generated ``if`` is a 5.1 parse error.
        keeps: list[str] = []
        condition, found = _lift_region_placeholders(condition)
        keeps.extend(found)
        true_expr, found = _lift_region_placeholders(true_expr)
        keeps.extend(found)
        false_expr, found = _lift_region_placeholders(false_expr)
        keeps.extend(found)
        if not condition or not true_expr or not false_expr:
            # Incomplete ternary (e.g. a bare ``?`` alias invocation that
            # slipped past the position guard): leave it and keep looking
            # for eligible ternaries further left.
            rejected.add(pos)
            continue
        inner = f"if ({condition}) {{ {true_expr} }} else {{ {false_expr} }}"
        if depth_arr[pos] > 0 or suffix.lstrip()[:1] in ("|", "&", ">"):
            # Either the ternary sits in an *expression* position
            # (inside ``(...)``/``[...]``, so ``depth_arr[pos] > 0``) or
            # its value is piped / chained / redirected.  A bare ``if``
            # statement is illegal there: ``(if ...)`` is a parse error
            # in 5.1.  Lift it into a value with ``$( ... )``.
            inner = f"$({inner})"
        warnings.append(
            f"ternary operator `{condition} ? {true_expr} : {false_expr}` "
            f"rewritten to `{inner}`"
        )
        line = _emit_replacement(
            line[:cond_start] + _keeps_prefix(keeps), inner
        ) + suffix
        rejected.clear()  # positions shifted; re-evaluate everything
    return line, warnings


# ===========================================================================
# Transform: pipeline chain operators  (&& / ||)
# ===========================================================================

def _enclosing_group_opener(
    line: str, pos: int, mask: bytearray, depths: list[int], depth: int
) -> str:
    """Return the opener char of the bracket group nesting *pos* at *depth*.

    Returns "" at the top level (``depth`` <= 0) or when no opener is found.
    Used to tell expression positions (``( ... )`` / ``[ ... ]``) from
    scriptblock bodies (``{ ... }``): a chain inside the former must be
    lifted into ``$( ... )`` (a statement sequence is a 5.1 parse error
    inside plain parens), while a chain inside a scriptblock is rewritten in
    place (statements are legal there).
    """
    if depth <= 0:
        return ""
    j = pos - 1
    nested = 0
    while j >= 0:
        if mask[j]:
            ch = line[j]
            if ch in ")]}":
                nested += 1
            elif ch in "([{":
                if nested:
                    nested -= 1
                elif depths[j] == depth - 1:
                    return ch
        j -= 1
    return ""


def _find_interp_op(line: str, op: str, mask: bytearray) -> int:
    """Find the next *op* inside interpolated code (mask value 2)."""
    idx = line.find(op)
    while idx != -1:
        if mask[idx] == 2:
            return idx
        idx = line.find(op, idx + 1)
    return -1


def _mask0_span(line: str, mask: bytearray, k: int) -> tuple[int, int]:
    """Return the maximal mask-0 span (start, end) containing index *k*."""
    s = k
    while s > 0 and not mask[s - 1]:
        s -= 1
    e = k + 1
    n = len(line)
    while e < n and not mask[e]:
        e += 1
    return s, e


def _interp_edge_or_literal(line: str, mask: bytearray, k: int) -> str:
    """Classify the mask-0 character at *k* inside an interpolated region.

    The interpolation's own ``$(`` / ``)`` boundary characters are mask 2, so
    any mask-0 hit while scanning inside one is either a nested string
    literal (starts with ``'``/``"`` — transparent for boundary scans; the
    operand continues past it) or a comment/region edge (a hard boundary).
    Returns ``"literal"`` or ``"edge"``.
    """
    s, _e = _mask0_span(line, mask, k)
    return "literal" if line[s] in "'\"" else "edge"


def _transform_chain_line(line: str) -> tuple[str, list[str]]:
    """Rewrite pipeline chain operators ``&&`` and ``||``.

    Chain operators are LEFT-associative in PowerShell 7
    (``A || B && C`` is ``(A || B) && C``), so each operator is rewritten as
    a *sequential* ``if ($?)`` / ``if (-not $?)`` statement — never nested
    inside a sibling operator's block, which would wrongly drop later
    segments when an earlier condition short-circuits.  Operands are bounded
    by the nearest statement boundary at the operator's nesting depth: a
    ``;`` separator, the opener/closer of the brace/paren group the operator
    sits in, the next chain operator at the same depth, or a trailing
    comment.  Operators inside scriptblocks (``{ ... }``) are rewritten in
    place at their own depth, so a trailing ``&&`` can never leak into a
    scriptblock body.

    Two positions need the sequence lifted into ``$( ... )`` instead:
    expression positions — a chain inside ``( ... )`` / ``[ ... ]`` (its
    combined output is the group's value in PS 7, and bare statements are a
    5.1 parse error there) — and the right-hand side of an assignment (the
    chained segments' combined output is the assigned value).  Chains inside
    interpolated sub-expressions (``"$( A && B )"``) are rewritten with
    region edges as hard boundaries: the statement sequence is valid there.
    """
    warnings: list[str] = []
    if "&&" not in line and "||" not in line:
        return line, warnings

    def _emit(
        prefix: str,
        left_seg: str,
        right_seg: str,
        best_op: str,
        wrap_value: bool,
        suffix: str,
        assign: "re.Match[str] | None" = None,
    ) -> str:
        condition = "$?" if best_op == "&&" else "-not $?"
        if prefix and not prefix[-1].isspace():
            prefix += " "
        if assign is not None:
            # ``$x = A && B`` -> ``$x = $(A; if ($?) { B })``: the variable
            # receives the combined output of the executed segments.
            rest = assign.group(3).strip()
            head = f"{rest}; " if rest else "; "
            return (
                f"{prefix}{assign.group(1)} "
                f"$({head}if ({condition}) {{ {right_seg} }}){suffix}"
            )
        head = f"{left_seg}; " if left_seg else "; "
        inner = f"{head}if ({condition}) {{ {right_seg} }}"
        if wrap_value:
            inner = f"$({inner})"
        return f"{prefix}{inner}{suffix}"

    def _rewrite_segments(left_seg: str, right_seg: str) -> tuple[str, str]:
        # A segment may still contain a chain operator at a DEEPER nesting
        # level (e.g. the ``||`` inside ``A && { B || C }``); rewrite it
        # recursively so no raw operator survives into the 5.1 output.
        if "&&" in left_seg or "||" in left_seg:
            left_seg, wl = _transform_chain_line(left_seg)
            warnings.extend(wl)
        if "&&" in right_seg or "||" in right_seg:
            right_seg, wl = _transform_chain_line(right_seg)
            warnings.extend(wl)
        return left_seg, right_seg

    # Phase 1 — chains in ordinary code (mask 1).  Region characters
    # (mask 0) are transparent for boundary scans.
    while True:
        mask = _line_mask(line)
        and_pos = _find_next_op(line, "&&", mask, plain_code_only=True)
        or_pos = _find_next_op(line, "||", mask, plain_code_only=True)
        if and_pos == -1 and or_pos == -1:
            break
        if and_pos == -1:
            best_pos, best_op = or_pos, "||"
        elif or_pos == -1:
            best_pos, best_op = and_pos, "&&"
        elif and_pos < or_pos:
            best_pos, best_op = and_pos, "&&"
        else:
            best_pos, best_op = or_pos, "||"

        depths = _compute_depths(line, mask)
        depth = depths[best_pos]

        # Left boundary: the nearest ``;`` at the operator's depth, or the
        # opener that established this depth level (its content starts after
        # it).  String/region characters (mask 0) are transparent.
        lo = best_pos
        while lo > 0:
            k = lo - 1
            if mask[k]:
                if depths[k] < depth:
                    break
                if depths[k] == depth and line[k] == ";":
                    break
            lo = k

        # Right boundary: scan forward at the operator's depth, stopping at
        # a statement separator (``;``), the closer ending this depth level,
        # the next chain operator at the same depth, or a trailing comment
        # (which must stay OUTSIDE the generated ``{ }`` block — a ``#``
        # inside braces would comment out the closing ``}``).
        hi = best_pos + 2
        prev_code = True  # the operator itself precedes the right operand
        while hi < len(line):
            c = line[hi]
            cur = mask[hi]
            if not cur:
                if prev_code and c == "#":
                    break
                prev_code = False
                hi += 1
                continue
            prev_code = True
            if depths[hi] < depth:
                break
            if depths[hi] == depth:
                if c == ";" or c in ")]}":
                    break
                if line.startswith("&&", hi) or line.startswith("||", hi):
                    break
            hi += 1

        left_seg = line[lo:best_pos].strip()
        right_seg = line[best_pos + 2:hi].strip()
        left_seg, right_seg = _rewrite_segments(left_seg, right_seg)

        # Expression position: a chain inside ``( ... )`` / ``[ ... ]`` must
        # become a value (statements are a 5.1 parse error inside plain
        # parens; the combined segment output is the group's value in PS 7).
        # An assignment left segment (``$x = A && B``) likewise: the assigned
        # value is the combined output of the executed segments.
        opener = _enclosing_group_opener(line, best_pos, mask, depths, depth)
        # NOTE: ``opener in "(["`` is not enough — "" is a substring of any
        # string, so the empty (top-level) opener must be excluded explicitly.
        wrap_value = bool(opener) and opener in "(["
        assign_split = None
        if not opener and depth == 0:
            m = _CHAIN_ASSIGN_RE.match(left_seg)
            if m and m.group(3).strip():
                assign_split = m

        prefix = line[:lo]
        new_line = _emit(
            prefix, left_seg, right_seg, best_op, wrap_value, line[hi:],
            assign=assign_split,
        )
        warnings.append(
            f"pipeline chain `{left_seg} {best_op} {right_seg}` "
            f"rewritten to `{new_line}`"
        )
        line = new_line

    # Phase 2 — chains inside interpolated sub-expressions ("$( ... )" code,
    # mask 2).  The sequential-statement rewrite IS valid there (verified
    # against 5.1), but region edges are hard boundaries: mask-0 characters
    # end the boundary scans instead of being transparent.
    while True:
        mask = _line_mask(line)
        and_pos = _find_interp_op(line, "&&", mask)
        or_pos = _find_interp_op(line, "||", mask)
        if and_pos == -1 and or_pos == -1:
            break
        if and_pos == -1:
            best_pos, best_op = or_pos, "||"
        elif or_pos == -1:
            best_pos, best_op = and_pos, "&&"
        elif and_pos < or_pos:
            best_pos, best_op = and_pos, "&&"
        else:
            best_pos, best_op = or_pos, "||"

        depths = _compute_depths(line, mask)
        depth = depths[best_pos]

        lo = best_pos
        while lo > 0:
            k = lo - 1
            if not mask[k]:
                # A nested string literal (``$a && 'x'``) is transparent;
                # anything else is the region edge (the interpolation's own
                # ``"$(`` opens here).
                if _interp_edge_or_literal(line, mask, k) == "literal":
                    lo = _mask0_span(line, mask, k)[0]
                    continue
                break
            if depths[k] < depth:
                break
            if depths[k] == depth and line[k] == ";":
                break
            lo = k

        hi = best_pos + 2
        while hi < len(line):
            cur = mask[hi]
            if not cur:
                # A nested string literal (``$a && 'x'``) extends the
                # operand; anything else is the region edge (the
                # interpolation's own ``)" closes here).
                if _interp_edge_or_literal(line, mask, hi) == "literal":
                    hi = _mask0_span(line, mask, hi)[1]
                    continue
                break
            if depths[hi] < depth:
                break
            if depths[hi] == depth:
                if line[hi] == ";" or line[hi] in ")]}":
                    break
                if line.startswith("&&", hi) or line.startswith("||", hi):
                    break
            hi += 1

        left_seg = line[lo:best_pos].strip()
        right_seg = line[best_pos + 2:hi].strip()
        left_seg, right_seg = _rewrite_segments(left_seg, right_seg)
        opener = _enclosing_group_opener(line, best_pos, mask, depths, depth)
        wrap_value = bool(opener) and opener in "(["

        prefix = line[:lo]
        new_line = _emit(
            prefix, left_seg, right_seg, best_op, wrap_value, line[hi:]
        )
        warnings.append(
            f"pipeline chain `{left_seg} {best_op} {right_seg}` "
            f"rewritten to `{new_line}`"
        )
        line = new_line
    return line, warnings


# ===========================================================================
# Null-conditional helpers
# ===========================================================================

def _scan_member_name(line: str, ms: int, mask: bytearray) -> int:
    """Scan a member name starting at *ms*; return the index after it."""
    if ms >= len(line):
        return ms
    c0 = line[ms]
    if c0 == "'":
        return _scan_single_quoted(line, ms)
    if c0 == '"':
        return _scan_double_quoted(line, ms)
    if c0 != "$":
        me = ms
        while me < len(line) and (line[me].isalnum() or line[me] == "_"):
            me += 1
        return me
    # Variable member ($var, ${var}, $? etc.)
    me = ms + 1
    if me >= len(line):
        return me
    ch = line[me]
    if ch == "{":
        bd = 1
        me += 1
        while me < len(line) and bd > 0:
            if line[me] == "{":
                bd += 1
            elif line[me] == "}":
                bd -= 1
            me += 1
    elif ch in "?$^":
        me += 1  # single-char automatic variables ($? $$ $^)
    else:
        while me < len(line) and (line[me].isalnum() or line[me] in "_:"):
            me += 1
    return me


def _scan_method_args(line: str, start: int, mask: bytearray) -> tuple[str, int]:
    """If *start* points to ``(``, scan the method argument list.

    Returns ``(args_string, index_after_closing_paren)``.
    """
    j = start
    while j < len(line) and line[j] == " ":
        j += 1
    if j >= len(line) or line[j] != "(":
        return "", start
    d = 1
    k = j + 1
    while k < len(line) and d > 0:
        if mask[k]:
            if line[k] == "(":
                d += 1
            elif line[k] == ")":
                d -= 1
        k += 1
    return line[j:k], k


# ===========================================================================
# Transform: null-conditional  (?. and ?[)
# ===========================================================================

def _transform_null_conditional_dot(line: str) -> tuple[str, list[str]]:
    """Rewrite null-conditional member access (``?.``)."""
    return _transform_null_conditional_line(line, "?.")


def _transform_null_conditional_bracket(line: str) -> tuple[str, list[str]]:
    """Rewrite null-conditional index access (``?[``)."""
    return _transform_null_conditional_line(line, "?[")


def _transform_null_conditional_line(line: str, op: str) -> tuple[str, list[str]]:
    """Rewrite null-conditional member access (``?.``) or index access (``?[``)."""
    warnings: list[str] = []
    is_dot = op == "?."
    op_len = len(op)
    if op not in line:
        return line, warnings
    search = 0
    while True:
        mask = _line_mask(line)
        idx = _find_next_op(line, op, mask, True, search)
        if idx == -1:
            return line, warnings

        expr_start, expr_end = _expr_left(line, idx, mask, "?:")
        base = line[expr_start:expr_end].strip()
        base, expr_start = _strip_command_prefix(base, expr_start)
        base, expr_start = _strip_keyword_operand(base, expr_start)
        keeps: list[str] = []
        base, found = _lift_region_placeholders(base)
        keeps.extend(found)
        if not base or not _looks_like_value_expr(base):
            # A bareword "base" is 5.1 command text whose ``?`` is a
            # filename wildcard (``rm f?.bak``), not a null-conditional.
            search = idx + op_len
            continue

        if is_dot and "?[" in base:
            # Mixed chain (``$a?[$i]?.b``): the index null-conditional is
            # part of the dot base.  Transform it here so no raw ``?[`` is
            # left inside the generated condition, where the later bracket
            # pass would misread its base (the scan would absorb the
            # ``$null -ne`` operator text).
            base, wl = _transform_null_conditional_line(base, "?[")
            warnings.extend(wl)
            if not base:
                search = idx + op_len
                continue

        if is_dot:
            segments: list[tuple[str, int]] = []  # (member_expr, end_pos)
            prefixes = [base]  # accumulated dotted prefixes
            cur = idx
            while cur < len(line) - 1 and line[cur:cur + 2] == "?.":
                ms = cur + 2
                while ms < len(line) and line[ms] == " ":
                    ms += 1
                me = _scan_member_name(line, ms, mask)
                if me == ms:
                    break
                mem = line[ms:me]
                args, me = _scan_method_args(line, me, mask)
                segments.append((f".{mem}{args}", me))
                prefixes.append(prefixes[-1] + segments[-1][0])
                cur = me
            if not segments:
                search = idx + op_len
                continue
            # Build nested if chain from innermost to outermost
            full_expr = prefixes[-1]  # full dotted expression
            for pfx in reversed(prefixes[:-1]):
                full_expr = f"if ($null -ne {pfx}) {{ {full_expr} }}"
            inner = f"$({full_expr})"
            end_pos = segments[-1][1]
            orig_expr = base + "?." + "?.".join(seg[0][1:] for seg in segments)
        else:
            bracket_depth = 1
            bracket_end = idx + 2
            while bracket_end < len(line) and bracket_depth > 0:
                c = line[bracket_end]
                if mask[bracket_end]:
                    if c == "[":
                        bracket_depth += 1
                    elif c == "]":
                        bracket_depth -= 1
                bracket_end += 1
            index_expr = line[idx + 2:bracket_end - 1]
            inner = f"$(if ($null -ne {base}) {{ {base}[{index_expr}] }})"
            end_pos = bracket_end
            orig_expr = f"{base}?[{index_expr}]"

        kind = "member access" if is_dot else "index"
        warnings.append(
            f"null-conditional {kind} `{orig_expr}` "
            f"rewritten to `{inner}`"
        )
        line = _emit_replacement(
            line[:expr_start] + _keeps_prefix(keeps), inner
        ) + line[end_pos:]
        search = 0


# ===========================================================================
# Transform dispatch
# ===========================================================================

_TRANSFORMS = (
    _transform_nca_line,
    _transform_null_conditional_dot,
    _transform_null_conditional_bracket,
    _transform_nc_line,
    _transform_ternary_line,
    _transform_chain_line,
)


# ===========================================================================
# Public API
# ===========================================================================

def pwsh_transform(code: str) -> tuple[str, list[str]]:
    """Transform PowerShell 7.x syntax into PowerShell 5.1 compatible syntax.

    Returns ``(transformed_code, warnings)`` where *warnings* is a list of
    human-readable messages describing each transformation that was applied.
    """
    code = _join_continuation_lines(code)
    lines = code.split("\n")
    mask = _build_region_mask(code)
    multi = _find_multiline_regions(code, mask, lines)
    region_open = _find_multiline_region_open_lines(code, mask)
    # Byte offset where each line starts within *code* (for slicing the
    # full-code mask per line).
    line_starts: list[int] = []
    off = 0
    for ln in lines:
        line_starts.append(off)
        off += len(ln) + 1

    result: list[str] = []
    all_warnings: list[str] = []

    for i, line in enumerate(lines):
        if i in multi:
            # The line is inside a multi-line string/comment/here-string
            # region.  Such regions can open or close *mid-line*, leaving
            # live code before/after them (e.g. a here-string closing ``"@``
            # followed by ``; $y = $a ?? "d"``): a whole-line skip would leave
            # operators in that code raw (5.1 parse errors).  Replace each
            # region span with an opaque placeholder word, run the normal
            # pipeline on the placeholder line (whose mask is then derivable
            # per-line), and substitute the region text back afterwards.
            base = line_starts[i]
            nline = len(line)
            if i in region_open:
                # This line *opens* a multi-line region (e.g. the opening
                # here-string header ``@'`` at the end of ``$a ?? @'``).  The
                # region header would otherwise be folded into a generated
                # construct as an operand — the emitted
                # ``$(if ($null -ne $a) { $a } else { @' })`` splits the here-
                # string across the generated braces (its body and ``'@`` stay
                # on the following lines) and is no longer parseable.  Only
                # regions that *close* mid-line (``"@; $y = $a ?? "d"``) carry
                # live code that can be safely transformed.
                result.append(line)
                continue
            runs: list[tuple[int, int]] = []
            j = 0
            while j < nline:
                if not mask[base + j]:
                    k = j
                    while k < nline and not mask[base + k]:
                        k += 1
                    runs.append((j, k))
                    j = k
                else:
                    j += 1
            if runs == [(0, nline)]:
                # Wholly inside the region — nothing live on this line.
                result.append(line)
                continue
            if runs:
                replacements: list[tuple[str, str]] = []
                parts: list[str] = []
                prev = 0
                for ridx, (s, e) in enumerate(runs):
                    ph = f"__kimixrgn{i}x{ridx}__"
                    replacements.append((ph, line[s:e]))
                    parts.append(line[prev:s])
                    parts.append(ph)
                    prev = e
                parts.append(line[prev:])
                pline = "".join(parts)
                for xform in _TRANSFORMS:
                    pline, w = xform(pline)
                    if w:
                        all_warnings.extend(f"Line {i + 1}: {msg}" for msg in w)
                for ph, orig_text in replacements:
                    pline = pline.replace(ph, orig_text)
                result.append(pline)
                continue
        for xform in _TRANSFORMS:
            line, w = xform(line)
            if w:
                all_warnings.extend(f"Line {i + 1}: {msg}" for msg in w)
        result.append(line)

    return "\n".join(result), all_warnings


def _find_multiline_regions(code: str, mask: bytearray, lines: list[str]) -> set[int]:
    """Return a set of line indices that are inside multi-line regions.

    Multi-line regions are strings, comments, or here-strings that span
    across two or more lines. Lines wholly inside such regions must be
    skipped by line-level transforms.
    """
    multi: set[int] = set()
    i = 0
    n = len(code)
    line_idx = 0
    while i < n:
        if mask[i] == 0:
            start_line = line_idx
            while i < n and mask[i] == 0:
                if code[i] == "\n":
                    line_idx += 1
                i += 1
            if line_idx > start_line:
                multi.update(range(start_line, line_idx + 1))
        else:
            if code[i] == "\n":
                line_idx += 1
            i += 1
    return multi


def _find_multiline_region_open_lines(code: str, mask: bytearray) -> set[int]:
    """Return the line indices that *open* a multi-line region.

    A line opens a region when the first character of a masked span that
    runs past the end of the line sits on it.  Such a line may still carry
    live code before the region header (``$a ?? @'``), but that code must
    not be rewritten: lifting the region header into a generated construct
    (``$(if (...) { ... } else { @' })``) tears the here-string apart across
    the generated braces and emits invalid PowerShell.  Lines that merely
    *close* a region mid-line (``"@; $y = $a ?? "d"``) are deliberately not
    included — their trailing live code can be transformed safely.
    """
    opens: set[int] = set()
    i = 0
    n = len(code)
    line_idx = 0
    while i < n:
        if mask[i] == 0:
            start_line = line_idx
            while i < n and mask[i] == 0:
                if code[i] == "\n":
                    line_idx += 1
                i += 1
            if line_idx > start_line:
                opens.add(start_line)
        else:
            if code[i] == "\n":
                line_idx += 1
            i += 1
    return opens
