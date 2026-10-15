"""PowerShell quoting validator/repair: ``PwshFix``, ``_PwshScanner``,
the warning constants, the ``nul`` redirection rewrite, and
``fix_pwsh_command``.

Moved byte-for-byte from ``bin/kimix_native/_shell_compat.py`` (the
pwsh_fix.py section, former lines 5879-6391).  ``_build_region_mask`` is
imported from :mod:`kimix_native.shell_compat.pwsh_transform` (the two
sections lived in one module; there is no code dependency in the
reverse direction).  ``kimix_native/_shell_compat.py`` is the stable
compatibility facade that re-exports this surface.
"""

from __future__ import annotations

from dataclasses import dataclass

try:
    import regex as re
except ImportError:  # pragma: no cover
    import re  # stdlib: the used patterns are all stdlib-compatible

# The code-region mask is the same scanner the PS7->PS5.1 transformer uses
# (its section lived in this module; keep the reference explicit here).
# It is read inside _rewrite_pwsh_nul_redirections, so it is needed for the
# module to work standalone; the facade shares one namespace across sections.
from kimix_native.shell_compat.pwsh_transform import _build_region_mask


# pwsh_fix.py (kimi-agent) - PowerShell quoting validator/repair
# ======================================================================


_PWSH_NORMAL_RE = re.compile(r'''["'`#<@\-$]''')
"""Chars that can open a PowerShell region in normal (unquoted) mode.

Everything else in normal mode is plain code, so the tokenizer can bulk-skip
runs of it with one C-level regex search instead of a per-character dispatch.
``-`` is included for the ``--%`` stop-parsing marker, ``#``/``@`` for the
token-start comment / here-string checks, and ``$`` for ``$(...)``.
"""

_NORMAL = "normal"
_DQ = "double-quoted"
_SQ = "single-quoted"
_HDQ = "here-double"
_HSQ = "here-single"
_COMMENT = "line-comment"
_BLOCK = "block-comment"

_W_UNCLOSED_DQ = (
    "The command has an unclosed double-quoted string; "
    'appended a closing `"` at the end to make it a legal PowerShell command.'
)
_W_UNCLOSED_SQ = (
    "The command has an unclosed single-quoted string; "
    "appended a closing `'` at the end to make it a legal PowerShell command."
)
_W_UNCLOSED_HDQ = (
    "The command has an unclosed double-quoted here-string; "
    'appended a newline and `"@` at the end to close it.'
)
_W_UNCLOSED_HSQ = (
    "The command has an unclosed single-quoted here-string; "
    "appended a newline and `'@` at the end to close it."
)
_W_UNCLOSED_BLOCK = (
    "The command has an unclosed block comment `<#`; "
    "appended `#>` at the end to close it."
)
_W_TRAILING_COMMENT = (
    "The command ends with a line comment; "
    "appended a newline so the trailing comment does not swallow the "
    "try/catch wrapper used to execute the command."
)
_W_STOP_PARSING = (
    "The command ends with the `--%` stop-parsing marker; "
    "appended a newline so the wrapper is not passed literally to the "
    "native command."
)
_W_COMMENT_ONLY = (
    "The command contains only comments; appended a newline and a no-op "
    "`$null` statement so the try/catch wrapper has a statement to execute."
)
_W_TRAILING_CONTINUATION = (
    "The command ends with a backtick line-continuation; "
    "appended a newline so the continuation does not join with the "
    "try/catch wrapper used to execute the command."
)
_W_NUL_REDIRECT = (
    "Rewrote Windows-style null-device redirection target(s) `nul` to "
    "`$null` so PowerShell discards the output instead of creating a file "
    "named `nul`."
)

# Redirection operators that can create a file in PowerShell: ``>``, ``>>``,
# ``*>`` (all streams) and an fd/stream prefix such as ``2>`` / ``2>>``.  A
# target that is the unquoted word ``nul`` is the Windows null-device spelling
# (cmd.exe ``> NUL``); PowerShell treats it as an ordinary filename and would
# create an empty ``nul`` file, so it is rewritten to the ``$null`` sink.
_PWSH_NUL_REDIRECT_RE = re.compile(
    r"(?P<op>(?:[0-9]|\*)?>+)[ \t]*(?P<target>nul)(?![A-Za-z0-9_.])",
    re.IGNORECASE,
)


def _rewrite_pwsh_nul_redirections(cmd: str) -> tuple[str, str]:
    """Rewrite unquoted ``> nul``-style redirections to the ``$null`` sink.

    Returns ``(rewritten_command, warning)`` where *warning* is
    ``_W_NUL_REDIRECT`` when at least one target was rewritten, else ``""``.

    Quoting/comment/here-string awareness comes from :func:`_build_region_mask`
    (the same region scanner the PS7->PS5.1 transformer uses), so a literal
    ``nul`` inside a string/comment is never touched.  After ``--%`` the rest
    of the line is passed literally to the native executable (where ``> NUL``
    is valid cmd.exe syntax), so those regions are carved out of the mask too.
    Append redirections (``>> nul`` / ``2>> nul``) collapse to a single
    ``>``/``2>`` because appending to ``$null`` is meaningless.
    """
    if "nul" not in cmd.casefold() or ">" not in cmd:
        return cmd, ""
    mask = _build_region_mask(cmd)
    n = len(cmd)
    # ``--%`` stop-parsing: the rest of that line is literal for the native
    # command (cmd.exe ``> NUL`` is valid there and must not be rewritten).
    i = 0
    while i < n:
        if cmd[i] == "-" and cmd.startswith("--%", i) and mask[i]:
            if i == 0 or not (cmd[i - 1].isalnum() or cmd[i - 1] == "_"):
                line_end = cmd.find("\n", i + 3)
                end = n if line_end < 0 else line_end
                mask[i:end] = b"\x00" * (end - i)
                i = end
                continue
        i += 1

    out: list[str] = []
    previous = 0
    changed = False
    for m in _PWSH_NUL_REDIRECT_RE.finditer(cmd):
        op_start, op_end = m.span("op")
        target_start, target_end = m.span("target")
        # Only code-region matches are rewritten (strings/comments are 0).
        if not (mask[op_start] and mask[target_end - 1]):
            continue
        # Ensure every byte of the op+whitespace+target run is code.  A quoted
        # target (``> 'nul'``) has mask 0 at the quote, so the target_end-1
        # check above already preserves it.
        op_text = m.group("op")
        prefix = op_text.rstrip(">")  # preserve fd/stream prefix: 2>, *>
        between = cmd[op_end:target_start]
        space = " " if between else ""
        replacement = prefix + ">" + space + "$null"
        out.append(cmd[previous:op_start])
        out.append(replacement)
        previous = target_end
        changed = True
    if not changed:
        return cmd, ""
    out.append(cmd[previous:])
    return "".join(out), _W_NUL_REDIRECT


@dataclass(frozen=True)
class PwshFix:
    """Result of :func:`fix_pwsh_command`.

    ``command`` is the (possibly repaired) command to execute and ``warning``
    is a human-readable note describing any modification, or ``""`` when the
    command was already valid and left unchanged.
    """

    command: str
    warning: str = ""

    @property
    def changed(self) -> bool:
        """True when the command text differs from the input."""
        return bool(self.warning)


def fix_pwsh_command(cmd: str) -> PwshFix | None:
    """Validate *cmd* with PowerShell quoting rules and repair it if possible.

    Returns a :class:`PwshFix` (``command`` may equal *cmd* when the command
    is already legal), or ``None`` when the command cannot be repaired.

    Null-device redirections (``> nul`` etc.) are rewritten both before and
    after the quote-repair scanner: the first pass catches plain commands that
    would otherwise skip the scanner's fast path, and the second pass catches
    any target newly exposed when the scanner appends a missing closing quote
    or newline.
    """
    if not cmd or not cmd.strip():
        return None
    first, nul_warning = _rewrite_pwsh_nul_redirections(cmd)
    if not first.strip():
        return None
    result = _PwshScanner(first).fix()
    if result is None:
        return None
    second, nul_warning_after = _rewrite_pwsh_nul_redirections(result.command)
    warnings = [w for w in (nul_warning, result.warning, nul_warning_after) if w]
    return PwshFix(second, "\n".join(warnings))


class _PwshScanner:
    """Single-pass tokenizer implementing PowerShell's quoting rules."""

    __slots__ = ("s", "n")

    def __init__(self, s: str) -> None:
        self.s = s
        self.n = len(s)

    # -- helpers used while skipping $(...) sub-expressions -----------------

    def _skip_sq(self, start: int) -> int:
        """Skip a single-quoted string starting at *start*; index after it."""
        s, n = self.s, self.n
        i = start + 1
        while i < n:
            if s[i] == "'":
                if i + 1 < n and s[i + 1] == "'":
                    i += 2  # '' -> literal single quote
                else:
                    return i + 1  # closing quote
            else:
                i += 1
        return i

    def _skip_dq(self, start: int) -> int:
        """Skip a double-quoted string starting at *start*; index after it."""
        s, n = self.s, self.n
        i = start + 1
        while i < n:
            ch = s[i]
            if ch == "`":
                i += 2 if i + 1 < n else 1
            elif ch == '"':
                if i + 1 < n and s[i + 1] == '"':
                    i += 2  # "" -> literal double quote
                else:
                    return i + 1  # closing quote
            elif ch == "$" and i + 1 < n and s[i + 1] == "(":
                i = self._skip_subexpr(i)
            else:
                i += 1
        return i

    def _skip_block(self, start: int) -> int:
        """Skip a block comment starting at *start*; index after it.

        PowerShell closes block comments at the first ``#>`` — they do not
        nest (verified empirically with pwsh 7.6.2).
        """
        s, n = self.s, self.n
        i = start + 2
        while i < n:
            if s[i] == "#" and i + 1 < n and s[i + 1] == ">":
                return i + 2
            i += 1
        return i

    def _skip_subexpr(self, start: int) -> int:
        """Skip a ``$( ... )`` sub-expression starting at *start*.

        Iterative (no recursion) so deeply nested ``$(...)`` cannot hit the
        interpreter recursion limit.  Nested ``$(...)`` are handled by the
        paren-depth counter; strings and comments are skipped before the
        parens are counted, so a ``)`` inside a string is never mistaken for
        the closing paren.

        Returns the index *after* the matching ``)`` (or ``n`` when the
        sub-expression never closes — the caller then treats the enclosing
        string as unclosed).
        """
        s, n = self.s, self.n
        i = start + 2
        depth = 1
        while i < n and depth:
            ch = s[i]
            if ch == "(":
                depth += 1
                i += 1
            elif ch == ")":
                depth -= 1
                i += 1
            elif ch == "'":
                i = self._skip_sq(i)
            elif ch == '"':
                i = self._skip_dq(i)
            elif ch == "`":
                i += 2 if i + 1 < n else 1
            elif ch == "#":
                if self._at_token_start(i):
                    while i < n and s[i] != "\n":
                        i += 1
                else:
                    i += 1
            elif ch == "<" and i + 1 < n and s[i + 1] == "#":
                i = self._skip_block(i)
            else:
                i += 1
        return i

    # -- token-boundary predicate -------------------------------------------

    def _at_token_start(self, i: int) -> bool:
        """True when *i* starts a fresh token (not glued to a word/identifier)."""
        return i == 0 or not (self.s[i - 1].isalnum() or self.s[i - 1] == "_")

    # -- EOF repair ----------------------------------------------------------

    def _dq_closer(self) -> str:
        """Closing quote for an unclosed double-quoted string.

        A trailing backtick would escape a single appended ``"``, so an odd
        run of trailing backticks needs two quotes (one escaped, one closing).
        """
        k = 0
        for ch in reversed(self.s):
            if ch == "`":
                k += 1
            else:
                break
        return '""' if k % 2 == 1 else '"'

    # -- main scan -----------------------------------------------------------

    def fix(self) -> PwshFix | None:
        s, n = self.s, self.n
        mode = _NORMAL
        here_quote = ""   # opening quote of the current here-string
        line_start = 0    # start of the current line inside a here-string
        saw_code = False  # any real statement code seen (not comments/whitespace)
        last_cont_target = -1  # index after the last backtick-newline continuation
        i = 0
        search = _PWSH_NORMAL_RE.search
        while i < n:
            if mode == _NORMAL:
                # Bulk-skip a run of characters that cannot open a quoted,
                # comment, here-string, stop-parsing or sub-expression region.
                # They are all code (set ``saw_code``); only whitespace is
                # handled separately so it does not flip the flag.
                m = search(s, i)
                if m is None:
                    tail = s[i:]
                    if tail.strip():
                        saw_code = True
                    i = n
                    continue
                run = s[i:m.start()]
                if run:
                    if run.strip():
                        saw_code = True
                    i = m.start()
                ch = s[i]
                if ch == '"':
                    saw_code = True
                    mode = _DQ
                    i += 1
                elif ch == "'":
                    saw_code = True
                    mode = _SQ
                    i += 1
                elif ch == "`":
                    if i + 1 < n:
                        saw_code = True
                        if s[i + 1] == "\n":
                            last_cont_target = i + 2
                        i += 2  # escaped char (or line continuation)
                    else:
                        # Dangling line-continuation backtick: PowerShell
                        # rejects a backtick with nothing after it.
                        return None
                elif ch == "#" and self._at_token_start(i):
                    mode = _COMMENT
                    i += 1
                elif ch == "<" and i + 1 < n and s[i + 1] == "#":
                    mode = _BLOCK
                    i += 2
                elif (
                    ch == "@"
                    and i + 1 < n
                    and s[i + 1] in ("'", '"')
                    and self._at_token_start(i)
                ):
                    # Here-string opener: @' or @" followed by only
                    # whitespace until end-of-line (or end of input).
                    j = i + 2
                    while j < n and s[j] in " \t\r":
                        j += 1
                    if j == n or s[j] == "\n":
                        saw_code = True
                        here_quote = s[i + 1]
                        mode = _HDQ if here_quote == '"' else _HSQ
                        line_start = n if j == n else j + 1
                        i = line_start
                        continue
                    saw_code = True
                    i += 1  # @' / @" with content on the same line: not a here-string
                elif (
                    ch == "-"
                    and s.startswith("--%", i)
                    and self._at_token_start(i)
                ):
                    # --% stop-parsing: the rest of the line is literal.
                    if not saw_code:
                        # `--%` with no command before it is a PowerShell
                        # parse error ("Missing expression after unary
                        # operator '--'") — nothing to repair.
                        return None
                    nl = s.find("\n", i)
                    if nl == -1:
                        # The literal region reaches EOF and would swallow the
                        # try/catch wrapper — terminate the line explicitly.
                        return PwshFix(s + "\n", _W_STOP_PARSING)
                    i = nl + 1
                elif ch == "$" and i + 1 < n and s[i + 1] == "(":
                    saw_code = True
                    i = self._skip_subexpr(i)
                elif ch.isspace():
                    i += 1
                else:
                    saw_code = True
                    i += 1
            elif mode == _DQ:
                ch = s[i]
                if ch == "`":
                    i += 2 if i + 1 < n else 1
                elif ch == '"':
                    if i + 1 < n and s[i + 1] == '"':
                        i += 2  # "" -> literal double quote
                    else:
                        mode = _NORMAL
                        i += 1
                elif ch == "$" and i + 1 < n and s[i + 1] == "(":
                    i = self._skip_subexpr(i)
                else:
                    i += 1
            elif mode == _SQ:
                # Bulk-skip: only a single quote is special inside '...'.
                close = s.find("'", i, n)
                if close < 0:
                    i = n
                    continue
                if close + 1 < n and s[close + 1] == "'":
                    i = close + 2  # '' -> literal single quote
                else:
                    mode = _NORMAL
                    i = close + 1
            elif mode in (_HDQ, _HSQ):
                ch = s[i]
                if ch == "\n":
                    line_start = i + 1
                    i += 1
                elif (
                    ch == here_quote
                    and i + 1 < n
                    and s[i + 1] == "@"
                    and s[line_start:i].strip() == ""
                ):
                    mode = _NORMAL
                    i += 2
                else:
                    i += 1
            elif mode == _COMMENT:
                # Bulk-skip to the end of the line.
                nl = s.find("\n", i, n)
                if nl < 0:
                    i = n
                else:
                    mode = _NORMAL
                    i = nl + 1
            elif mode == _BLOCK:
                # Bulk-skip to the closing ``#>``.
                close = s.find("#>", i, n)
                if close < 0:
                    i = n
                else:
                    mode = _NORMAL
                    i = close + 2

        # -- end of input -----------------------------------------------------
        # A line-continuation backtick whose target line runs to the end of
        # the command would join with the try/catch wrapper added by the tool,
        # silently corrupting the command.  Append a newline so the
        # continuation ends on an empty line instead.
        needs_cont_nl = last_cont_target != -1 and s.rfind("\n") < last_cont_target
        if mode == _NORMAL:
            if saw_code:
                if needs_cont_nl:
                    return PwshFix(s + "\n", _W_TRAILING_CONTINUATION)
                return PwshFix(s, "")
            # Only comments/whitespace: give the wrapper a statement to run.
            return PwshFix(s + "\n$null", _W_COMMENT_ONLY)
        if mode == _DQ:
            fixed = s + self._dq_closer()
            warning = _W_UNCLOSED_DQ
            if needs_cont_nl:
                fixed += "\n"
                warning += "\n" + _W_TRAILING_CONTINUATION
            return PwshFix(fixed, warning)
        if mode == _SQ:
            fixed = s + "'"
            warning = _W_UNCLOSED_SQ
            if needs_cont_nl:
                fixed += "\n"
                warning += "\n" + _W_TRAILING_CONTINUATION
            return PwshFix(fixed, warning)
        if mode == _HDQ:
            fixed = s + '\n"@'
            warning = _W_UNCLOSED_HDQ
            if needs_cont_nl:
                fixed += "\n"
                warning += "\n" + _W_TRAILING_CONTINUATION
            return PwshFix(fixed, warning)
        if mode == _HSQ:
            fixed = s + "\n'@"
            warning = _W_UNCLOSED_HSQ
            if needs_cont_nl:
                fixed += "\n"
                warning += "\n" + _W_TRAILING_CONTINUATION
            return PwshFix(fixed, warning)
        if mode == _COMMENT:
            if saw_code:
                return PwshFix(s + "\n", _W_TRAILING_COMMENT)
            return PwshFix(s + "\n$null", _W_COMMENT_ONLY)
        if mode == _BLOCK:
            if saw_code:
                fixed = s + "#>"
                warning = _W_UNCLOSED_BLOCK
            else:
                fixed = s + "#>\n$null"
                warning = _W_COMMENT_ONLY
            if needs_cont_nl:
                fixed += "\n"
                warning += "\n" + _W_TRAILING_CONTINUATION
            return PwshFix(fixed, warning)
        return None  # pragma: no cover - unreachable
