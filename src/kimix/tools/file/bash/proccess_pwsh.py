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

The public entry point is ``pwsh_transform(code)``.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right


# ---------------------------------------------------------------------------
# Low-level string / comment scanners
# ---------------------------------------------------------------------------

def _scan_single_quoted(code: str, i: int) -> int:
    """Skip a single-quoted string starting at *i*; return index after it."""
    i += 1
    n = len(code)
    while i < n:
        if code[i] == "'":
            if i + 1 < n and code[i + 1] == "'":
                i += 2
            else:
                return i + 1
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
            i += 2
        elif ch == '"':
            return i + 1
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


# ---------------------------------------------------------------------------
# Region finders  (strings, comments, here-strings)
# ---------------------------------------------------------------------------

def _find_string_regions(code: str) -> list[tuple[int, int]]:
    """Return intervals covering all string/comment regions in *code*."""
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
            while i < n and code[i] != "\n":
                i += 1
            regions.append((start, i))
        elif c == "@" and i + 1 < n and code[i + 1] in ("'", '"'):
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


def _find_line_regions(line: str) -> list[tuple[int, int]]:
    """Line-level variant of ``_find_string_regions`` (here-strings omitted)."""
    regions: list[tuple[int, int]] = []
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if c == "<" and i + 1 < n and line[i + 1] == "#":
            start = i
            i = _scan_block_comment(line, i)
            regions.append((start, i))
        elif c == "#":
            regions.append((i, n))
            break
        elif c == "'":
            start = i
            i = _scan_single_quoted(line, i)
            regions.append((start, i))
        elif c == '"':
            start = i
            i = _scan_double_quoted(line, i)
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


# ---------------------------------------------------------------------------
# Depth tracking
# ---------------------------------------------------------------------------

def _compute_depths(line: str, regions: list[tuple[int, int]]) -> list[int]:
    """Return nesting depth of ``()``, ``{}``, ``[]`` before each character."""
    depths: list[int] = []
    depth = 0
    for i, ch in enumerate(line):
        depths.append(depth)
        if _outside_regions(regions, i):
            if ch in "([{":
                depth += 1
            elif ch in ")}]":
                depth -= 1
    depths.append(depth)
    return depths


# ---------------------------------------------------------------------------
# Pre-processing
# ---------------------------------------------------------------------------

def _join_continuation_lines(code: str) -> str:
    """Collapse backtick line-continuations into single logical lines."""
    regions = _find_string_regions(code)
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


# ---------------------------------------------------------------------------
# Assignment detection
# ---------------------------------------------------------------------------

_ASSIGN_RE = re.compile(r"(.*?)(\$\w+(?::\w+)?(?:\.\w+)*)\s*=\s*$")
_COMMAND_PREFIX_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*\s+")
_PS_KEYWORDS = {
    "begin", "break", "catch", "class", "continue", "data", "define", "do",
    "dynamicparam", "else", "elseif", "end", "enum", "exit", "filter", "finally",
    "for", "foreach", "from", "function", "hidden", "if", "in", "param",
    "process", "return", "static", "switch", "throw", "trap", "try", "until",
    "using", "var", "while",
}


def _match_assignment(before: str) -> tuple[str, str] | None:
    """Match an assignment prefix like ``$var = `` at the end of *before*."""
    m = _ASSIGN_RE.match(before)
    if m:
        return m.group(1), m.group(2)
    return None


def _build_replacement(prefix: str, inner: str) -> str:
    """Build a replacement string with optional assignment detection."""
    assign = _match_assignment(prefix.rstrip())
    if assign:
        p, var = assign
        return f"{p}{var} = {inner}"
    return f"{prefix}{inner}"


def _strip_command_prefix(expr: str, start: int) -> tuple[str, int]:
    """Strip a leading command name (e.g. ``Write-Output ``) from *expr*.

    Returns the stripped expression and the adjusted start index.
    """
    m = _COMMAND_PREFIX_RE.match(expr)
    if m:
        cmd = m.group(0).strip().lower()
        if cmd not in _PS_KEYWORDS:
            expr_part = expr[m.end():]
            if expr_part and expr_part[0] in "$([\"'@0123456789":
                return expr_part, start + m.end()
    return expr, start


# ---------------------------------------------------------------------------
# Expression boundary helpers
# ---------------------------------------------------------------------------

def _find_expr_start(line: str, end: int, regions: list[tuple[int, int]],
                     extra_stop: str = "") -> int:
    """Scan backwards from *end* to locate the start of the expression.

    *extra_stop* can contain additional delimiter characters (e.g. "?:"
    for null-conditional base scanning).
    """
    depth = 0
    stop_set = "=;|&," + extra_stop
    for i in range(end - 1, -1, -1):
        if not _outside_regions(regions, i):
            continue
        c = line[i]
        if c in ")}]":
            depth += 1
        elif c in "([{":
            depth -= 1
            if depth < 0:
                return i + 1
        elif depth == 0 and c in stop_set:
            # For ?/: in extra_stop, skip $? / :: / $scope: / ?. contexts.
            if c == "?":
                if i + 1 < len(line) and line[i + 1] == ".":
                    continue  # ?. null-conditional, not ternary ?
                if i > 0 and line[i - 1] == "$":
                    continue  # $? automatic variable
            elif c == ":":
                if i + 1 < len(line) and line[i + 1] == ":":
                    continue  # :: static member access
                # Check for $scope: prefix
                if i > 0 and (line[i - 1].isalnum() or line[i - 1] == "_"):
                    j = i - 1
                    while j > 0 and (line[j].isalnum() or line[j] == "_"):
                        j -= 1
                    if line[j] == "$":
                        continue  # $scope:var
            return i + 1
    return 0


def _find_expr_end(line: str, start: int, regions: list[tuple[int, int]]) -> int:
    """Scan forwards from *start* to locate the end of the expression."""
    depth = 0
    for i in range(start, len(line)):
        c = line[i]
        if c == "#":
            if i > 0 and line[i - 1] == "<":
                # Part of <# block-comment start; handled below by _outside_regions.
                pass
            elif _outside_regions(regions, i):
                return i  # Bare # outside strings starts a line comment.
            else:
                # Inside a region — if this # starts that region, it is a boundary.
                idx = bisect_right(regions, (i, float("inf"))) - 1
                if idx >= 0 and regions[idx][0] == i:
                    return i
        if not _outside_regions(regions, i):
            continue
        if c in "([{":
            depth += 1
        elif c in ")}]":
            depth -= 1
            if depth < 0:
                return i
        elif depth == 0 and c in ";|&,":
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


# ---------------------------------------------------------------------------
# Null-coalescing assignment  (??=)
# ---------------------------------------------------------------------------

def _transform_nca_line(line: str) -> str:
    """Rewrite null-coalescing assignment ``$var ??= value``."""
    while True:
        regions = _find_line_regions(line)
        found = False
        pos = 0
        while pos < len(line) - 2:
            if line[pos : pos + 3] == "??=" and _outside_regions(regions, pos):
                var_end = pos
                while var_end > 0 and line[var_end - 1] == " ":
                    var_end -= 1
                var_start = var_end
                # Handle braced variables: ${global:var} ??= value
                if var_start > 0 and line[var_start - 1] == "}":
                    # Scan backward to find matching ${ ... }
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
                    while var_start > 0 and (line[var_start - 1].isalnum() or line[var_start - 1] in ".$_:"):
                        var_start -= 1
                    if var_start > 0 and line[var_start - 1] == "$":
                        var_start -= 1
                var = line[var_start:var_end].strip()
                if not var:
                    pos += 3
                    continue
                val_start, val_end = _expr_right(line, pos + 3, regions)
                value = line[val_start:val_end].strip()
                before = line[:var_start].rstrip()
                prefix = f"{before} " if before else ""
                line = f"{prefix}if ($null -eq {var}) {{ {var} = {value} }}" + line[val_end:]
                found = True
                break
            pos += 1
        if not found:
            break
    return line


# ---------------------------------------------------------------------------
# Null-coalescing  (??)
# ---------------------------------------------------------------------------

def _transform_nc_line(line: str) -> str:
    """Transform every ``??`` on *line* into PS 5.1 compatible ``if`` form."""
    while True:
        regions = _find_line_regions(line)
        rewritten = False
        pos = 0
        while pos < len(line) - 1:
            idx = line.find("??", pos)
            if idx == -1:
                break
            if _outside_regions(regions, idx):
                left_start, left_end = _expr_left(line, idx, regions)
                right_start, right_end = _expr_right(line, idx + 2, regions)
                left_expr = line[left_start:left_end].strip()
                left_expr, left_start = _strip_command_prefix(left_expr, left_start)
                right_expr = line[right_start:right_end].strip()
                if left_expr and right_expr:
                    inner = f"if ($null -ne {left_expr}) {{ {left_expr} }} else {{ {right_expr} }}"
                    line = _build_replacement(line[:left_start], inner) + line[right_end:]
                    rewritten = True
                    break
            pos = idx + 2
        if not rewritten:
            break
    return line


# ---------------------------------------------------------------------------
# Ternary  (? :)
# ---------------------------------------------------------------------------

def _find_matching_colon(
    line: str, start: int, regions: list[tuple[int, int]], depth_arr: list[int]
) -> int:
    """Find the colon that separates the true/false branches of a ternary."""
    for i in range(start, len(line)):
        if line[i] == ":" and depth_arr[i] == 0 and _outside_regions(regions, i):
            if i > 0 and line[i - 1] == ":":
                continue
            if i + 1 < len(line) and line[i + 1] == ":":
                continue
            return i
    return -1


def _transform_ternary_line(line: str) -> str:
    """Rewrite ternary ``$cond ? $true : $false`` into an ``if`` statement."""
    regions = _find_line_regions(line)
    depth_arr = _compute_depths(line, regions)
    pos = 0
    while pos < len(line):
        if (
            line[pos] == "?"
            and _outside_regions(regions, pos)
            and not (pos > 0 and line[pos - 1] == "$")
        ):
            colon_pos = _find_matching_colon(line, pos + 1, regions, depth_arr)
            if colon_pos != -1:
                cond_start, cond_end = _expr_left(line, pos, regions)
                condition = line[cond_start:cond_end].strip()
                true_expr = line[pos + 1:colon_pos].strip()
                false_start, false_end = _expr_right(line, colon_pos + 1, regions)
                false_expr = line[false_start:false_end].strip()
                if not _match_assignment(line[:cond_start].rstrip()) and condition:
                    m = _COMMAND_PREFIX_RE.match(condition)
                    if m:
                        expr_part = condition[m.end():]
                        if expr_part and expr_part[0] in "$([\"'@0123456789":
                            cond_start += m.end()
                            condition = expr_part
                inner = f"if ({condition}) {{ {true_expr} }} else {{ {false_expr} }}"
                suffix = line[false_end:]
                line = _build_replacement(line[:cond_start], inner) + suffix
                regions = _find_line_regions(line)
                depth_arr = _compute_depths(line, regions)
                pos = len(line) - len(suffix)
                continue
        pos += 1
    return line


# ---------------------------------------------------------------------------
# Pipeline chain operators  (&& / ||)
# ---------------------------------------------------------------------------

def _transform_chain_line(line: str) -> str:
    """Rewrite pipeline chain operators ``&&`` and ``||``."""
    while True:
        regions = _find_line_regions(line)
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
        line = f"{left}; if ({condition}) {{ {right} }}"
    return line


# ---------------------------------------------------------------------------
# Null-conditional member access  (?.)
# ---------------------------------------------------------------------------

def _transform_null_conditional_dot_line(line: str) -> str:
    """Rewrite null-conditional member access ``$obj?.Member``."""
    while True:
        regions = _find_line_regions(line)
        matched = False
        pos = 0
        while pos < len(line) - 1:
            idx = line.find("?.", pos)
            if idx == -1:
                break
            if not _outside_regions(regions, idx):
                pos = idx + 2
                continue
            expr_start, expr_end = _expr_left(line, idx, regions, "?:")
            base = line[expr_start:expr_end].strip()
            base, expr_start = _strip_command_prefix(base, expr_start)
            if not base:
                pos = idx + 2
                continue
            chain: list[tuple[str, str, int]] = []
            pos = idx
            while pos < len(line) - 1 and line[pos : pos + 2] == "?.":
                ms = pos + 2
                while ms < len(line) and line[ms] == " ":
                    ms += 1
                me = ms
                # Variable / quoted / plain identifier member name scanning.
                c0 = line[ms] if ms < len(line) else ""
                if c0 == "$":
                    # Variable property name: $property, ${var}, $global:prop, etc.
                    me = ms + 1
                    if me < len(line) and line[me] == "{":
                        # ${braced var}
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
                elif c0 == "'":
                    # Single-quoted member name: 'prop-name'
                    me = _scan_single_quoted(line, ms)
                elif c0 == '"':
                    # Double-quoted member name: "prop-name"
                    me = _scan_double_quoted(line, ms)
                else:
                    while me < len(line) and (line[me].isalnum() or line[me] == "_"):
                        me += 1
                if me == ms:
                    break
                mem = line[ms:me]
                args = ""
                j = me
                while j < len(line) and line[j] == " ":
                    j += 1
                if j < len(line) and line[j] == "(":
                    d = 1
                    k = j + 1
                    while k < len(line) and d > 0:
                        if _outside_regions(regions, k):
                            if line[k] == "(":
                                d += 1
                            elif line[k] == ")":
                                d -= 1
                        k += 1
                    args = line[j:k]
                    me = k
                chain.append((mem, args, me))
                pos = me
            if not chain:
                pos = idx + 2
                continue
            paths = [base]
            for mem, args, _ in chain:
                paths.append(f"{paths[-1]}.{mem}{args}")
            inner = paths[-1]
            for p in reversed(paths[:-1]):
                inner = f"if ($null -ne {p}) {{ {inner} }}"
            inner = f"$({inner})"
            line = _build_replacement(line[:expr_start], inner) + line[chain[-1][2]:]
            matched = True
            break
        if not matched:
            break
    return line


# ---------------------------------------------------------------------------
# Null-conditional index access  (?[)
# ---------------------------------------------------------------------------

def _transform_null_conditional_bracket_line(line: str) -> str:
    """Rewrite null-conditional index access ``$obj?[index]``."""
    while True:
        regions = _find_line_regions(line)
        matched = False
        pos = 0
        while pos < len(line) - 1:
            idx = line.find("?[", pos)
            if idx == -1:
                break
            if not _outside_regions(regions, idx):
                pos = idx + 2
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
            index_expr = line[idx + 2 : bracket_end - 1]
            inner = f"$(if ($null -ne {expr}) {{ {expr}[{index_expr}] }})"
            line = _build_replacement(line[:expr_start], inner) + line[bracket_end:]
            matched = True
            break
        if not matched:
            break
    return line


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def pwsh_transform(code: str) -> str:
    """Transform PowerShell 7.x syntax into PowerShell 5.1 compatible syntax."""
    code = _join_continuation_lines(code)
    lines = code.split("\n")
    regions = _find_string_regions(code)

    line_offsets = [0]
    for ln in lines[:-1]:
        line_offsets.append(line_offsets[-1] + len(ln) + 1)

    multi: set[int] = set()
    for s, e in regions:
        if "\n" not in code[s:e]:
            continue
        first = bisect_right(line_offsets, s) - 1
        last = bisect_left(line_offsets, e) - 1
        multi.update(range(first, last + 1))

    result: list[str] = []
    for i, line in enumerate(lines):
        if i in multi:
            result.append(line)
            continue
        line = _transform_nca_line(line)
        line = _transform_null_conditional_dot_line(line)
        line = _transform_null_conditional_bracket_line(line)
        line = _transform_nc_line(line)
        line = _transform_ternary_line(line)
        line = _transform_chain_line(line)
        result.append(line)

    return "\n".join(result)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        text = " ".join(sys.argv[1:])
    else:
        text = sys.stdin.read()
    result = pwsh_transform(text)
    print(result)
