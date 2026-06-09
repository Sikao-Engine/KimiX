"""Transform PowerShell 7.x syntax to PowerShell 5.1 compatible syntax.

PowerShell 7 introduced several expression-level operators that do not exist in
PowerShell 5.1:

  * Ternary:          $cond ? $true_expr : $false_expr
  * Null-coalescing:  $a ?? $fallback
  * Null-assign:      $a ??= $default
  * Pipeline chains:  cmd1 && cmd2   /   cmd1 || cmd2
  * Null-conditional: $obj?.Property / $obj?[0]

This module performs a *source-to-source* transformation.  It operates on raw
text rather than an AST because the target environment (5.1) cannot parse the
new syntax in the first place.
"""

from __future__ import annotations

import re
from bisect import bisect_right


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

_VAR_CHARS = frozenset(".$_:")

_VAR_FIRST_CHARS = frozenset("$([\"'@0123456789")

_EXPR_STOP = frozenset("=;|&,")

_DEPTH_OPEN = frozenset("([{")
_DEPTH_CLOSE = frozenset(")]}")


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
    """Skip a block comment ``<# ... #>`` starting at *i*; return index after it."""
    depth = 1
    i += 2
    n = len(code)
    while i < n and depth:
        if code[i] == "<" and i + 1 < n and code[i + 1] == "#":
            depth += 1
            i += 2
        elif code[i] == "#" and i + 1 < n and code[i + 1] == ">":
            depth -= 1
            i += 2
        else:
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

def _find_regions(code: str, *, here_strings: bool = True) -> list[tuple[int, int]]:
    """Return intervals covering all string/comment regions in *code*.

    When *here_strings* is False, here-strings are not detected (used for
    line-level scanning where here-strings cannot be reliably identified).
    """
    regions: list[tuple[int, int]] = []
    i = 0
    n = len(code)
    while i < n:
        c = code[i]
        if c == "<" and i + 1 < n and code[i + 1] == "#":
            start = i
            i = _scan_block_comment(code, i)
            regions.append((start, i))
        elif c == "#":
            start = i
            if here_strings:
                while i < n and code[i] != "\n":
                    i += 1
            else:
                i = n  # rest of line is comment
            regions.append((start, i))
        elif here_strings and c == "@" and i + 1 < n and code[i + 1] in ("'", '"'):
            j = i + 2
            while j < n and code[j] in " \t\r":
                j += 1
            if j < n and code[j] != "\n":
                i += 1
                continue
            start = i
            quote_char = code[i + 1]
            i += 2
            while i < n:
                if code[i] == quote_char and i + 1 < n and code[i + 1] == "@":
                    line_start = code.rfind("\n", 0, i)
                    line_start = 0 if line_start == -1 else line_start + 1
                    if code[line_start:i].strip() == "":
                        i += 2
                        break
                i += 1
            regions.append((start, i))
        elif c == "'":
            start = i
            i = _scan_single_quoted(code, i)
            regions.append((start, i))
        elif c == '"':
            start = i
            i = _scan_double_quoted(code, i)
            regions.append((start, i))
        else:
            i += 1
    return regions


def _outside_regions(regions: list[tuple[int, int]], pos: int) -> bool:
    """Return ``True`` iff *pos* is not inside any of the supplied regions."""
    idx = bisect_right(regions, (pos, float("inf"))) - 1
    if idx >= 0:
        start, end = regions[idx]
        return not (start <= pos < end)
    return True


# ===========================================================================
# Depth tracking  (for matching ternary colon)
# ===========================================================================

def _compute_depths(line: str, regions: list[tuple[int, int]]) -> list[int]:
    """Return nesting depth of ``()``, ``{}``, ``[]`` before each character.

    Depths are now string-aware: brackets inside strings/comments are ignored.
    """
    depths: list[int] = []
    depth = 0
    for i, ch in enumerate(line):
        depths.append(depth)
        if _outside_regions(regions, i):
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
    """Collapse backtick line-continuations into single logical lines."""
    regions = _find_regions(code)
    result: list[str] = []
    i = 0
    n = len(code)
    while i < n:
        if code[i] == "`" and _outside_regions(regions, i):
            j = i + 1
            while j < n and code[j] in " \t\r":
                j += 1
            if j < n and code[j] == "\n":
                j += 1
                while j < n and code[j] in " \t\r":
                    j += 1
                result.append(" ")
                i = j
                continue
        result.append(code[i])
        i += 1
    return "".join(result)


# ===========================================================================
# Assignment detection
# ===========================================================================

_ASSIGN_RE = re.compile(r"(.*?)(\$\w+(?::\w+)?(?:\.\w+)*)\s*=\s*$")
_COMMAND_PREFIX_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*\s+")


def _match_assignment(before: str) -> tuple[str, str] | None:
    """Match an assignment prefix like ``$var = `` at the end of *before*."""
    m = _ASSIGN_RE.match(before)
    if m:
        return m.group(1), m.group(2)
    return None


def _build_replacement(prefix: str, inner: str) -> str:
    """Build replacement string, preserving an assignment if one is detected."""
    assign = _match_assignment(prefix.rstrip())
    if assign:
        p, var = assign
        return f"{p}{var} = {inner}"
    return f"{prefix}{inner}"


def _strip_command_prefix(expr: str, start: int) -> tuple[str, int]:
    """Strip a leading command name (e.g. ``Write-Output ``) from *expr*.

    Returns ``(stripped_expr, adjusted_start)``.
    PowerShelle keywords (if, foreach, …) are never stripped.
    """
    m = _COMMAND_PREFIX_RE.match(expr)
    if m:
        cmd = m.group(0).strip().lower()
        if cmd not in _PS_KEYWORDS:
            expr_part = expr[m.end():]
            if expr_part and expr_part[0] in _VAR_FIRST_CHARS:
                return expr_part, start + m.end()
    return expr, start


# ===========================================================================
# $? adjacency check — shared by all transforms
# ===========================================================================

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
# Shared colon-context check  (used by _find_expr_start & _find_matching_colon)
# ===========================================================================

def _is_scope_colon(line: str, i: int) -> bool:
    """Return ``True`` if the colon at *i* belongs to a ``$scope:var`` prefix."""
    if i > 0 and (line[i - 1].isalnum() or line[i - 1] == "_"):
        j = i - 1
        while j > 0 and (line[j].isalnum() or line[j] == "_"):
            j -= 1
        if line[j] == "$":
            return True
    return False


# ===========================================================================
# Expression boundary helpers
# ===========================================================================

def _find_expr_start(line: str, end: int, regions: list[tuple[int, int]],
                     extra_stop: str = "") -> int:
    """Scan backwards from *end* to locate the start of the expression.

    *extra_stop* can contain additional delimiter characters (e.g. ``"?:"``
    for null-conditional base scanning).
    """
    depth = 0
    stop_set = _EXPR_STOP | frozenset(extra_stop) if extra_stop else _EXPR_STOP
    for i in range(end - 1, -1, -1):
        if not _outside_regions(regions, i):
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
                if i + 1 < len(line) and line[i + 1] == ".":
                    continue  # ?. is null-conditional
                if i > 0 and line[i - 1] == "$" and not (i > 1 and line[i - 2] == "$"):
                    continue  # $? auto var (but not $$)
            elif c == ":":
                if i + 1 < len(line) and line[i + 1] == ":":
                    continue  # :: static member
                if i > 0 and line[i - 1] == ":":
                    continue  # :: static member (right side)
                if _is_scope_colon(line, i):
                    continue  # $scope:var
            return i + 1
    return 0


def _find_expr_end(line: str, start: int, regions: list[tuple[int, int]]) -> int:
    """Scan forwards from *start* to locate the end of the expression."""
    depth = 0
    for i in range(start, len(line)):
        c = line[i]
        if not _outside_regions(regions, i):
            continue
        if c in _DEPTH_OPEN:
            depth += 1
        elif c in _DEPTH_CLOSE:
            depth -= 1
            if depth < 0:
                return i
        elif depth == 0:
            if c == "#":
                return i
            if c in _EXPR_STOP:
                return i
    return len(line)


def _expr_left(line: str, pos: int, regions: list[tuple[int, int]],
               extra_stop: str = "") -> tuple[int, int]:
    """Return (start, end) of the expression immediately left of *pos*."""
    end = pos
    while end > 0 and line[end - 1] == " ":
        end -= 1
    start = _find_expr_start(line, end, regions, extra_stop)
    return start, end


def _expr_right(line: str, pos: int, regions: list[tuple[int, int]]) -> tuple[int, int]:
    """Return (start, end) of the expression immediately right of *pos*."""
    start = pos
    while start < len(line) and line[start] == " ":
        start += 1
    end = _find_expr_end(line, start, regions)
    return start, end


# ===========================================================================
# Variable-name backward scanner  (shared by NCA and null-conditional)
# ===========================================================================

def _scan_var_backward(line: str, end: int) -> int:
    """Scan backward from *end* to find the start of a variable reference.

    Recognises ``$var``, ``$scope:var``, ``$obj.Prop``, ``$?``, ``$$``, ``$^``,
    ``${braced}`` (the caller must handle the braced case separately).
    Returns the index of the ``$`` or 0 if none found.
    """
    start = end
    while start > 0 and (line[start - 1].isalnum() or line[start - 1] in _VAR_CHARS):
        start -= 1
    while start > 0 and line[start - 1] == "?":
        start -= 1
    if start > 0 and line[start - 1] == "$":
        start -= 1
    return start


# ===========================================================================
# Helper: skip whitespace backward / forward
# ===========================================================================

def _skip_spaces_back(line: str, pos: int) -> int:
    """Return the index after skipping trailing spaces before *pos*."""
    while pos > 0 and line[pos - 1] == " ":
        pos -= 1
    return pos


def _skip_spaces_fwd(line: str, pos: int) -> int:
    """Return the index after skipping leading spaces from *pos*."""
    while pos < len(line) and line[pos] == " ":
        pos += 1
    return pos


# ===========================================================================
# Transform: null-coalescing assignment  (??=)
# ===========================================================================

def _transform_nca_line(line: str) -> tuple[str, list[str]]:
    """Rewrite null-coalescing assignment ``$var ??= value``.

    Returns ``(transformed_line, warnings)``.
    """
    warnings: list[str] = []
    while True:
        regions = _find_regions(line, here_strings=False)
        matched = False
        pos = 0
        while pos < len(line) - 2:
            if line[pos:pos + 3] != "??=" or not _outside_regions(regions, pos):
                pos += 1
                continue
            # Skip if $? before ??= (first ? belongs to $? auto-var)
            if _after_dollar_question(line, pos):
                pos += 1
                continue

            # Scan backward for the variable name.
            var_end = _skip_spaces_back(line, pos)
            var_start = var_end
            if var_start > 0 and line[var_start - 1] == "}":
                # Braced variable: ${global:var} → scan to matching ${
                bd = 1
                var_start -= 1
                while var_start > 0 and bd > 0:
                    if line[var_start - 1] == "}":
                        bd += 1
                    elif line[var_start - 1] == "{":
                        bd -= 1
                    var_start -= 1
                if var_start > 0 and line[var_start - 1] == "$":
                    var_start -= 1
            else:
                var_start = _scan_var_backward(line, var_end)

            var = line[var_start:var_end].strip()
            if not var:
                pos += 3
                continue

            val_start, val_end = _expr_right(line, pos + 3, regions)
            value = line[val_start:val_end].strip()
            before = line[:var_start].rstrip()
            prefix = f"{before} " if before else ""
            new_inner = f"if ($null -eq {var}) {{ {var} = {value} }}"
            warnings.append(
                f"null-coalescing assignment `{var} ??= {value}` "
                f"rewritten to `{new_inner}`"
            )
            line = f"{prefix}{new_inner}" + line[val_end:]
            matched = True
            break
        if not matched:
            break
    return line, warnings


# ===========================================================================
# Transform: null-coalescing  (??)
# ===========================================================================

def _transform_nc_line(line: str) -> tuple[str, list[str]]:
    """Transform every ``??`` on *line* into PS 5.1 compatible ``if`` form.

    Returns ``(transformed_line, warnings)``.
    """
    warnings: list[str] = []
    while True:
        regions = _find_regions(line, here_strings=False)
        matched = False
        pos = 0
        while pos < len(line) - 1:
            idx = line.find("??", pos)
            if idx == -1:
                break
            if not _outside_regions(regions, idx):
                pos = idx + 2
                continue
            # Skip $?? — the first ? belongs to $? auto-var
            if _after_dollar_question(line, idx):
                pos = idx + 1  # +1 so we can still find ?? at the next position
                continue

            left_start, left_end = _expr_left(line, idx, regions)
            right_start, right_end = _expr_right(line, idx + 2, regions)
            left_expr = line[left_start:left_end].strip()
            left_expr, left_start = _strip_command_prefix(left_expr, left_start)
            right_expr = line[right_start:right_end].strip()
            if left_expr and right_expr:
                inner = (
                    f"if ($null -ne {left_expr}) "
                    f"{{ {left_expr} }} else {{ {right_expr} }}"
                )
                warnings.append(
                    f"null-coalescing `{left_expr} ?? {right_expr}` "
                    f"rewritten to `{inner}`"
                )
                line = _build_replacement(line[:left_start], inner) + line[right_end:]
                matched = True
                break
            pos = idx + 2
        if not matched:
            break
    return line, warnings


# ===========================================================================
# Transform: ternary  (? :)
# ===========================================================================

def _find_matching_colon(
    line: str, start: int, regions: list[tuple[int, int]], depth_arr: list[int]
) -> int:
    """Find the colon that separates the true/false branches of a ternary."""
    for i in range(start, len(line)):
        if line[i] != ":" or depth_arr[i] != 0 or not _outside_regions(regions, i):
            continue
        # Skip :: static member access
        if (i > 0 and line[i - 1] == ":") or (i + 1 < len(line) and line[i + 1] == ":"):
            continue
        # Skip $scope:var prefix
        if _is_scope_colon(line, i):
            continue
        return i
    return -1


def _transform_ternary_line(line: str) -> tuple[str, list[str]]:
    """Rewrite ternary ``$cond ? $true : $false`` into an ``if`` statement.

    Returns ``(transformed_line, warnings)``.
    """
    warnings: list[str] = []
    regions = _find_regions(line, here_strings=False)
    depth_arr = _compute_depths(line, regions)
    pos = 0
    while pos < len(line):
        if (
            line[pos] == "?"
            and _outside_regions(regions, pos)
            and not (pos > 0 and line[pos - 1] == "$")  # $? skip
        ):
            colon_pos = _find_matching_colon(line, pos + 1, regions, depth_arr)
            if colon_pos != -1:
                cond_start, cond_end = _expr_left(line, pos, regions)
                condition = line[cond_start:cond_end].strip()
                true_expr = line[pos + 1:colon_pos].strip()
                false_start, false_end = _expr_right(line, colon_pos + 1, regions)
                false_expr = line[false_start:false_end].strip()
                # Strip command prefix from condition when not in assignment
                if not _match_assignment(line[:cond_start].rstrip()) and condition:
                    m = _COMMAND_PREFIX_RE.match(condition)
                    if m:
                        expr_part = condition[m.end():]
                        if expr_part and expr_part[0] in _VAR_FIRST_CHARS:
                            cond_start += m.end()
                            condition = expr_part
                inner = f"if ({condition}) {{ {true_expr} }} else {{ {false_expr} }}"
                warnings.append(
                    f"ternary operator `{condition} ? {true_expr} : {false_expr}` "
                    f"rewritten to `{inner}`"
                )
                suffix = line[false_end:]
                line = _build_replacement(line[:cond_start], inner) + suffix
                regions = _find_regions(line, here_strings=False)
                depth_arr = _compute_depths(line, regions)
                pos = len(line) - len(suffix)
                continue
        pos += 1
    return line, warnings


# ===========================================================================
# Transform: pipeline chain operators  (&& / ||)
# ===========================================================================

def _transform_chain_line(line: str) -> tuple[str, list[str]]:
    """Rewrite pipeline chain operators ``&&`` and ``||``.

    Uses rightmost-first to maintain correct right-associative semantics.

    Returns ``(transformed_line, warnings)``.
    """
    warnings: list[str] = []
    while True:
        regions = _find_regions(line, here_strings=False)
        # Find rightmost && or || outside string/comment regions.
        best_pos, best_op = -1, ""
        for op in ("&&", "||"):
            idx = line.rfind(op)
            while idx != -1:
                if _outside_regions(regions, idx) and idx > best_pos:
                    best_pos, best_op = idx, op
                    break
                idx = line.rfind(op, 0, idx)
        if best_pos == -1:
            break
        condition = "$?" if best_op == "&&" else "-not $?"
        left = line[:best_pos].strip()
        right = line[best_pos + 2:].strip()
        new_line = f"{left}; if ({condition}) {{ {right} }}"
        warnings.append(
            f"pipeline chain `{left} {best_op} {right}` "
            f"rewritten to `{new_line}`"
        )
        line = new_line
    return line, warnings


# ===========================================================================
# Null-conditional helpers
# ===========================================================================

def _scan_member_name(line: str, ms: int, regions: list[tuple[int, int]]) -> int:
    """Scan a member name starting at *ms*; return the index after it.

    Handles plain identifiers, ``$property``, ``${braced}``, ``'quoted'``,
    and ``"quoted"`` member names.
    """
    if ms >= len(line):
        return ms
    c0 = line[ms]
    if c0 == "$":
        me = ms + 1
        if me < len(line) and line[me] == "{":
            bd = 1
            me += 1
            while me < len(line) and bd > 0:
                if line[me] == "{":
                    bd += 1
                elif line[me] == "}":
                    bd -= 1
                me += 1
        else:
            while me < len(line) and (line[me].isalnum() or line[me] in "_:"):
                me += 1
        return me
    if c0 == "'":
        return _scan_single_quoted(line, ms)
    if c0 == '"':
        return _scan_double_quoted(line, ms)
    # Plain identifier
    me = ms
    while me < len(line) and (line[me].isalnum() or line[me] == "_"):
        me += 1
    return me


def _scan_method_args(line: str, start: int, regions: list[tuple[int, int]]) -> tuple[str, int]:
    """If *start* points to ``(``, scan the method argument list.

    Returns ``(args_string, index_after_closing_paren)``.
    If no ``(`` at *start*, returns ``("", start)``.
    """
    j = _skip_spaces_fwd(line, start)
    if j >= len(line) or line[j] != "(":
        return "", start
    d = 1
    k = j + 1
    while k < len(line) and d > 0:
        if _outside_regions(regions, k):
            if line[k] == "(":
                d += 1
            elif line[k] == ")":
                d -= 1
        k += 1
    return line[j:k], k


# ===========================================================================
# Transform: null-conditional member access  (?.)
# ===========================================================================

def _transform_null_conditional_dot_line(line: str) -> tuple[str, list[str]]:
    """Rewrite null-conditional member access ``$obj?.Member``.

    Returns ``(transformed_line, warnings)``.
    """
    warnings: list[str] = []
    while True:
        regions = _find_regions(line, here_strings=False)
        matched = False
        pos = 0
        while pos < len(line) - 1:
            idx = line.find("?.", pos)
            if idx == -1:
                break
            if not _outside_regions(regions, idx):
                pos = idx + 2
                continue
            # Skip $?. — the ? belongs to $? auto-var
            if _after_dollar_question(line, idx):
                pos = idx + 1  # +1 so we can still find ?. at the next position
                continue

            expr_start, expr_end = _expr_left(line, idx, regions, "?:")
            base = line[expr_start:expr_end].strip()
            base, expr_start = _strip_command_prefix(base, expr_start)
            if not base:
                pos = idx + 2
                continue

            # Collect the chain of ?.member segments.
            chain: list[tuple[str, str, int]] = []
            cur = idx
            while cur < len(line) - 1 and line[cur:cur + 2] == "?.":
                ms = _skip_spaces_fwd(line, cur + 2)
                me = _scan_member_name(line, ms, regions)
                if me == ms:
                    break
                mem = line[ms:me]
                args, me = _scan_method_args(line, me, regions)
                chain.append((mem, args, me))
                cur = me
            if not chain:
                pos = idx + 2
                continue

            # Build nested if-null checks from the inside out.
            paths = [base]
            orig_parts = [base]
            for mem, args, _ in chain:
                paths.append(f"{paths[-1]}.{mem}{args}")
                orig_parts.append(f"{mem}{args}")
            inner = paths[-1]
            for p in reversed(paths[:-1]):
                inner = f"if ($null -ne {p}) {{ {inner} }}"
            inner = f"$({inner})"

            orig_expr = "?.".join(orig_parts)
            warnings.append(
                f"null-conditional member access `{orig_expr}` "
                f"rewritten to `{inner}`"
            )
            line = _build_replacement(line[:expr_start], inner) + line[chain[-1][2]:]
            matched = True
            break
        if not matched:
            break
    return line, warnings


# ===========================================================================
# Transform: null-conditional index access  (?[)
# ===========================================================================

def _transform_null_conditional_bracket_line(line: str) -> tuple[str, list[str]]:
    """Rewrite null-conditional index access ``$obj?[index]``.

    Returns ``(transformed_line, warnings)``.
    """
    warnings: list[str] = []
    while True:
        regions = _find_regions(line, here_strings=False)
        matched = False
        pos = 0
        while pos < len(line) - 1:
            idx = line.find("?[", pos)
            if idx == -1:
                break
            if not _outside_regions(regions, idx):
                pos = idx + 2
                continue
            # Skip $?[ — the ? belongs to $? auto-var
            if _after_dollar_question(line, idx):
                pos = idx + 1  # +1 so we can still find ?[ at the next position
                continue

            expr_start, expr_end = _expr_left(line, idx, regions, "?:")
            expr = line[expr_start:expr_end].strip()
            expr, expr_start = _strip_command_prefix(expr, expr_start)
            if not expr:
                pos = idx + 2
                continue

            bracket_depth = 1
            bracket_end = idx + 2
            while bracket_end < len(line) and bracket_depth > 0:
                c = line[bracket_end]
                if _outside_regions(regions, bracket_end):
                    if c == "[":
                        bracket_depth += 1
                    elif c == "]":
                        bracket_depth -= 1
                bracket_end += 1
            index_expr = line[idx + 2:bracket_end - 1]
            inner = f"$(if ($null -ne {expr}) {{ {expr}[{index_expr}] }})"
            warnings.append(
                f"null-conditional index `{expr}?[{index_expr}]` "
                f"rewritten to `{inner}`"
            )
            line = _build_replacement(line[:expr_start], inner) + line[bracket_end:]
            matched = True
            break
        if not matched:
            break
    return line, warnings


# ===========================================================================
# Public API
# ===========================================================================

def _collect_line_warnings(
    all_warnings: list[str], line_idx: int, line_warnings: list[str]
) -> None:
    """Prepend a line number prefix to each warning and append to *all_warnings*."""
    for w in line_warnings:
        all_warnings.append(f"Line {line_idx + 1}: {w}")


def pwsh_transform(code: str) -> tuple[str, list[str]]:
    """Transform PowerShell 7.x syntax into PowerShell 5.1 compatible syntax.

    Returns ``(transformed_code, warnings)`` where *warnings* is a list of
    human-readable messages describing each transformation that was applied.
    """
    code = _join_continuation_lines(code)
    lines = code.split("\n")
    regions = _find_regions(code)

    # Compute line offsets for multi-line region detection.
    line_offsets = [0]
    for ln in lines[:-1]:
        line_offsets.append(line_offsets[-1] + len(ln) + 1)

    multi: set[int] = set()
    for s, e in regions:
        if "\n" not in code[s:e]:
            continue
        first = bisect_right(line_offsets, s) - 1
        last = bisect_right(line_offsets, e) - 1
        multi.update(range(first, last + 1))

    result: list[str] = []
    all_warnings: list[str] = []

    # Order matters: ??= before ?? (so ??= isn't partially matched),
    # ?. before ?[ before ?? (so ?? doesn't consume ?. output), etc.
    _TRANSFORMS = (
        _transform_nca_line,
        _transform_null_conditional_dot_line,
        _transform_null_conditional_bracket_line,
        _transform_nc_line,
        _transform_ternary_line,
        _transform_chain_line,
    )

    for i, line in enumerate(lines):
        if i in multi:
            result.append(line)
            continue
        for xform in _TRANSFORMS:
            line, w = xform(line)
            _collect_line_warnings(all_warnings, i, w)
        result.append(line)

    return "\n".join(result), all_warnings


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        text = " ".join(sys.argv[1:])
    else:
        text = sys.stdin.read()
    result, warnings = pwsh_transform(text)
    for w in warnings:
        print(f"[WARNING] {w}", file=sys.stderr)
    print(result)
