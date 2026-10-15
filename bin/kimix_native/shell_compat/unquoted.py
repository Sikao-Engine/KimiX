r"""``_process_unquoted``: the lexer-driven unquoted-backslash emitter.

Phase 3 of the ``_shell_compat`` refactor (plan section 3.1 / Phase 3).  The
legacy implementation and its quote/substitution scanners lived in
:mod:`kimix_native.shell_compat.bash_core` (duplicated from the
``fix_bash_command`` side).  This module owns the canonical implementation of
the emitter, driven by the consolidated boundary oracles of
:mod:`kimix_native.shell_compat.lexer`.

Behavior (identical to the legacy scanner, bit-for-bit -- see
``tests/unit/tools/test_bash_unquoted_ir.py`` for the differential gate):

* a backslash before a non-metacharacter becomes ``/`` (Windows path
  normalization for Git Bash);
* a backslash before a bash metacharacter is preserved as the escape pair;
* backslashes inside single quotes, double quotes and ANSI-C quote regions
  stay intact (the regions are copied verbatim -- a lone backslash inside a
  double-quoted region is literal bash text, never converted);
* ``$(...)`` and backtick command substitutions have their content recursed
  in unquoted mode.  At the top level a ``$(...)`` is consumed by the flat
  character walk (the legacy walker has no top-level command-substitution
  case: it emits the ``$`` as an ordinary character and keeps scanning), so
  its content converts with the whole-string null-device spans; inside a
  double-quoted region a ``$(...)`` recurses through the legacy
  ``_process_unquoted(slice)`` contract and its null-device spans are
  computed on the slice.  A top-level backtick likewise recurses on its
  content slice;
* escaped spellings of the Windows null device are preserved so
  ``fix_bash_command`` can rewrite the whole word to ``/dev/null`` --
  converting the backslashes first would turn the null device into the
  absolute path ``/nul``;
* unterminated quotes / regions copy the remainder verbatim.

Why the boundary oracles and not :func:`lexer.lex`: the legacy scanner walks
a single character stream where a region boundary is taken only when the
region is properly closed (an unterminated double quote, command
substitution, backtick or ANSI-C quote copies the remainder verbatim and
stops).  The shared lexer instead classifies every character (words,
operators, redirections, comments) and models unterminated regions as open
word-parts, and it emits double-quote sub-regions as sibling parts -- both
behaviours diverge from the legacy emitter on malformed input and would need
to be second-guessed here.  Reusing ``lex`` would therefore add risk, not
safety.  This module instead reuses exactly the primitives that carry the
legacy boundary semantics -- ``find_ansi_c_end``, ``find_backtick_end``,
``find_dq_end`` and ``find_matching_paren`` (the Phase-2 lexer wrappers of
the bash_core originals, verified position-equal in Phase 2) -- as its region
oracles, plus the constant values.  That is the same single-source-of-truth
for a region boundary contract the lexer itself relies on.

``_process_unquoted`` is a pure string transform and consumes none of the
runtime monkeypatch hooks the facade exposes (only ``_windows_temp_dir`` is
patched, on the bash_core side), so the bash_core delegation is plain and
late-bound.
"""

from __future__ import annotations

try:
    import regex as re
except ImportError:  # pragma: no cover
    import re  # stdlib: the used patterns are all stdlib-compatible

# Boundary oracles (Phase 2 lexer wrappers of the bash_core originals) and the
# character-class / null-device constants.  This module never re-implements a
# region boundary; bash_core remains the canonical home of the constant values
# (the facade re-exports them from there) and this module reads those same
# objects rather than duplicating them.
from kimix_native.shell_compat.bash_core import (
    _BASH_METACHARACTERS,
    _DQ_ESCAPED,
    _ESCAPED_NUL_RE,
)
from kimix_native.shell_compat.lexer import (
    find_ansi_c_end,
    find_backtick_end,
    find_dq_end,
    find_matching_paren,
)

__all__ = ["_process_unquoted"]


# Bulk-skip pattern for unquoted mode: the legacy scanner stopped only on
# ``\`` ``'`` ``"`` ``$`` `` `` ` (it treated ``#``, ``(``, ``)``,
# redirection operators and bare newlines as ordinary copy-through text).  The
# shared lexer's ``_WORD_SPECIAL_RE`` is broader and would change that, so
# this module keeps the legacy pattern verbatim.
_UNQUOTED_SPECIAL_RE = re.compile(r'[\\\'"$`]')


def _nul_spans(cmd: str):
    """Word spans that are escaped spellings of ``nul`` (legacy _ESCAPED_NUL_RE)."""
    if "\\" not in cmd:
        return ()
    return tuple(m.span() for m in _ESCAPED_NUL_RE.finditer(cmd))


def _in_nul(i: int, nul_spans) -> bool:
    """True when index ``i`` falls inside an escaped-nul word span."""
    for lo, hi in nul_spans:
        if lo <= i < hi:
            return True
    return False


def _process_unquoted(cmd: str) -> str:
    r"""Convert unquoted backslashes to forward slashes in ``cmd``.

    Lexer-driven reimplementation of the legacy bash_core scanner with the
    exact same behavior; see the module docstring for the rule list.  The
    emitter walks ``cmd`` in unquoted mode; a region boundary is taken only
    when the region is properly closed (every ``_find_*`` oracle returns
    ``-1`` on unterminated input, in which case the copy runs to the end of
    ``cmd`` verbatim, matching the legacy).  Command/backtick substitutions
    and double-quoted regions are described in the module docstring.
    """
    result: list[str] = []
    length = len(cmd)
    # Word spans that are escaped spellings of ``nul`` (legacy _ESCAPED_NUL_RE);
    # backslashes inside them are preserved, not converted to ``/``.  The
    # legacy computes these on the WHOLE ``cmd`` once per call (nested
    # ``_process_unquoted`` slices recompute on their own slice — reproduced
    # below where a recursion passes a fresh slice).
    nul = _nul_spans(cmd)
    i = 0
    while i < length:
        # ---- find the next special character ----
        m = _UNQUOTED_SPECIAL_RE.search(cmd, i)
        if m:
            nxt = m.start()
            if nxt > i:
                result.append(cmd[i:nxt])
                i = nxt
        else:
            result.append(cmd[i:])
            break
        if i >= length:
            break
        char = cmd[i]
        if char == "'":
            end = cmd.find("'", i + 1)
            if end == -1:
                result.append(cmd[i:])
                break
            result.append(cmd[i : end + 1])
            i = end + 1
        elif char == '"':
            dq_end = find_dq_end(cmd, i + 1)
            if dq_end == -1:
                result.append(cmd[i:])
                break
            result.append(_dq_region(cmd, i, dq_end, nul))
            i = dq_end
        elif char == "$" and i + 1 < length and cmd[i + 1] == "'":
            ac_end = find_ansi_c_end(cmd, i + 2)
            if ac_end == -1:
                result.append(cmd[i:])
                break
            result.append(cmd[i:ac_end])
            i = ac_end
        elif char == "`":
            bt_end = find_backtick_end(cmd, i + 1)
            if bt_end == -1:
                result.append(cmd[i:])
                break
            result.append("`")
            # Recurse on the content slice (legacy _process_unquoted(slice)):
            # the slice's own null-device spans are computed inside the call.
            result.append(_process_unquoted(cmd[i + 1 : bt_end - 1]))
            result.append("`")
            i = bt_end
        elif char == "\\":
            if i + 1 < length and cmd[i + 1] in _BASH_METACHARACTERS:
                result.append("\\")
                result.append(cmd[i + 1])
                i += 2
            elif nul and _in_nul(i, nul):
                result.append("\\")
                result.append(cmd[i + 1])
                i += 2
            else:
                result.append("/")
                i += 1
        else:
            # ``$`` not followed by ``'`` (covers top-level ``$(…)``, whose
            # content the legacy walker consumes by flat continuation: the
            # backslashes convert with the whole-string nul spans).  Emit the
            # single character and advance one.
            result.append(char)
            i += 1
    return "".join(result)


def _dq_region(cmd: str, dq_start: int, dq_end: int, nul) -> str:
    r"""Process the closed double-quoted region ``cmd[dq_start:dq_end]``.

    Mirrors the legacy DQ walker: ``dq_start`` is the opening ``"`` and
    ``dq_end`` is one past the closing ``"`` (both included verbatim in the
    output except where a substitution is recursed).  Within the content the
    walker bulk-copies literal text, preserves ``\\X`` pairs (``X`` in
    ``_DQ_ESCAPED``), treats a literal ``$'`` as ordinary text, and *recurses
    on the slice* of any ``$(…)`` / backtick substitution (the legacy calls
    ``_process_unquoted`` on the slice, so each recursion recomputes its own
    null-device spans).  A substitution the legacy deems unterminated or
    out-of-region makes it copy the rest of the region verbatim.  ``nul`` is
    the caller's whole-string span list — the legacy top loop uses it for the
    surrounding backslashes, but inside the DQ content only ``\\X`` pairs in
    ``_DQ_ESCAPED`` and the recursed substitutions occur, none of which uses
    ``nul`` directly (kept for signature clarity).
    """
    out: list[str] = [cmd[dq_start]]  # opening "
    j = dq_start + 1
    chunk_start = j
    while j < dq_end:
        m2 = _UNQUOTED_SPECIAL_RE.search(cmd, j, dq_end)
        if m2:
            nxt2 = m2.start()
            if nxt2 > j:
                j = nxt2
        else:
            j = dq_end
            break
        c = cmd[j]
        if c == "\\" and j + 1 < dq_end and cmd[j + 1] in _DQ_ESCAPED:
            j += 2
        elif c == "$" and j + 1 < dq_end and cmd[j + 1] == "(":
            paren_end = find_matching_paren(cmd, j + 1)
            if paren_end == -1 or paren_end >= dq_end:
                j = dq_end
                break
            out.append(cmd[chunk_start:j])
            out.append("$(")
            out.append(_process_unquoted(cmd[j + 2 : paren_end]))
            out.append(")")
            j = paren_end + 1
            chunk_start = j
        elif c == "$" and j + 1 < dq_end and cmd[j + 1] == "'":
            # Literal ``$'`` inside ``"..."``: advance one so both ``$`` and
            # the following ``'`` stay in the chunk, keeping a *real* ``$(…)``
            # / backtick between them active.
            j += 1
        elif c == "`":
            bt_end = find_backtick_end(cmd, j + 1)
            if bt_end == -1 or bt_end > dq_end:
                j = dq_end
                break
            out.append(cmd[chunk_start:j])
            out.append("`")
            out.append(_process_unquoted(cmd[j + 1 : bt_end - 1]))
            out.append("`")
            j = bt_end
            chunk_start = j
        else:
            # bare ``'`` or ``"`` inside a closed region, or a ``$`` / ``\``
            # that did not start a handled pair: copy as ordinary text.
            j += 1
    out.append(cmd[chunk_start:dq_end])  # trailing text + closing "
    return "".join(out)
