r"""Bash-quoting repair of a ``cmd /c`` command STRING (one word -> words).

Part of the lossless tolerant-IR ``kimix_native.shell_compat`` implementation
package behind the stable ``_shell_compat`` facade.

The failure this rule repairs
-----------------------------
Agents (and people) spell a multi-argument cmd.exe command string the way a
POSIX shell would: ONE double-quoted word whose inner quotes are backslash
escaped, and every Windows path survives the outer quotes verbatim::

    cmd //c "\"D:\Epic Games\UE_5.8\...\Build.bat\" Editor Win64 \
        -Project=\"D:\temp\HarnessDemo\HarnessDemo.uproject\" -WaitMutex"

Bash reads that correctly (``\"`` inside ``"..."`` is a literal ``"``): the
argv element is the whole command string, quotes included.  The MSYS runtime
then has to hand that argv element to a NATIVE program (``cmd.exe``), so it
re-quotes it for the Windows command line — and the only spelling it can emit
for a literal ``"`` is ``\"``::

    cmd.exe /c ""\"D:\Epic Games\...\Build.bat\" Editor Win64 ..."

cmd.exe is not a CRT program: a ``\`` is a literal character, ``\"`` is not a
quote.  It also strips the first and last quote character of a command string
that STARTS with a quote unless the string holds exactly two quotes, no
special characters between them and an executable file name in between — so
the escaped quotes and the outer pair are all lost and cmd.exe tries to run a
token like ``\"D:\Epic``::

    '\"D:\Epic Games\...\Build.bat\"' is not recognized as an internal or
    external command

and the build never starts.  (Verified on Git Bash 5.3 / Windows 11: a ``"``
in a Bash word NEVER reaches ``cmd.exe`` as a quote character, so no
re-spelling of the ONE word — doubling the quotes, ``""x""``, ``'...'``,
``\\"`` — can work.)

Rule
----
When the command string of a ``cmd``/``cmd.exe`` ``/c``/``/k`` node is exactly
ONE word, and that word is wholly quoted for bash (a single double-quoted or
single-quoted region) and its VALUE carries a real ``"`` (i.e. the author
escaped quotes inside it), the word is re-spelled as the cmd-level TOKENS it
always described::

    cmd /c call "D:\Epic Games\...\Build.bat" Editor Win64 \
        -Project="D:\temp\HarnessDemo\HarnessDemo.uproject" -WaitMutex

Bash then removes those quotes and the MSYS runtime quotes each argv element
for cmd.exe properly, so cmd.exe receives the command string the author meant.
Each token keeps the author's spelling: a quoted region stays quoted, a
backslash-bearing token is double-quoted (bash-only), and everything else is
emitted byte-for-byte.  Tokens are joined by the author's single spaces.

``call`` is prepended when the command string would START with a quoted token,
i.e. when the program word carries a space.  cmd.exe strips the first and last
quote of a command string that starts with a quote unless the string holds
exactly two quotes, no special characters between them, whitespace between them
and AN EXISTING executable file name in between — that last condition is a
filesystem fact a static rewriter cannot know (a mistyped path, or a target
created later in the same command line, silently mangles the token), and with
three or more quoted tokens the outer pair is always eaten.  ``call`` makes the
command string start with a word, so cmd.exe never strips anything.

Everything else stays byte-for-byte (the conservative pass-through policy):

* a command string that is already several words (the author split it), or
  that carries no ``"`` in its value (nothing bash-only to repair);
* a word that is not wholly quoted (``pre"..."``, ``a\ b``, ``$x``), or whose
  value carries an expansion (``$VAR``, ``$(...)``, backticks) or a newline —
  the command string the author meant is not knowable statically;
* a whitespace run other than the single spaces between tokens (bash cannot
  pass a gap between two argv elements), or an unbalanced quote;
* ``^`` outside a quoted region (cmd.exe's own escape character: quoting it
  for bash would turn ``^x`` into a literal ``^x``);
* embedded quoted scripts (``bash -c '...'``), heredoc bodies and raw regions
  are never entered — their spans are a different source string or pure data.

The rule runs as a post-pass in the ``_shell_compat`` facade over the shared
:class:`~kimix_native.shell_compat.bash_core.BashFix` value, BEFORE the
``cmd_script_paths`` path repair (that rule then sees plain words), and it is
Windows-only.  The compiled ``runtime_py.bash_fix`` kernel cannot express new
rules from this repository, so the facade is the single integration point that
keeps the compiled and pure-Python engines byte-equal by construction.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from kimix_native.shell_compat.cmd_script_paths import (
    CommandStringHandler,
    for_each_cmd_command_string,
)

__all__ = [
    "cmd_command_string_edits",
    "cmd_command_string_fix",
]

#: ``(start, end, replacement)`` splice in ORIGINAL-source coordinates.
Edit = Tuple[int, int, str]

#: Characters that must never appear in a rewritten command string: bash would
#: expand them at run time (``$VAR``, ``$(...)``, backticks) and a newline would
#: split the command, so the command string the author meant is not statically
#: knowable.  ``_shell_compat`` never re-emits an expansion it cannot prove.
_REFUSED_CHARS = frozenset("$`\n\r")

#: Characters bash treats specially in an UNQUOTED word.  A segment carrying
#: one of them is emitted inside double quotes, which neutralizes the whole set
#: (backslashes inside the quotes are handled by :func:`_dq_escape`).
_BASH_SPECIAL = frozenset("\\'\"$`;&|<>()*?[]{}!#~ \t\v\f")

#: Characters cmd.exe treats specially OUTSIDE a quoted region and whose
#: meaning would change if the segment were quoted for bash: ``^`` escapes the
#: next character (``^x`` -> ``x``), so quoting it would pass a literal ``^x``.
_UNQUOTED_REFUSED = frozenset("^")

#: A space: the only whitespace allowed between two command-string tokens.
_SPACE = " "

#: Characters that may sit immediately BEFORE or AFTER a word span without being
#: part of the same bash word.  Used to reject lossy tilings: the tolerant parser
#: can report a word span that omits a leading literal (``pre"..."``), and
#: re-spelling such a span would drop that literal from the command.
_WORD_BOUNDARY = frozenset(" \t\n\r;&|()<>")


# ── tokenizer ────────────────────────────────────────────────────────────────


def _split_tokens(value: str) -> Optional[List[str]]:
    """Split a command string VALUE into its cmd-level tokens.

    Whitespace outside a double-quoted region separates tokens; the quote
    characters themselves are kept in the token (they are re-emitted as bash
    quotes).  ``None`` when the value cannot be re-spelled faithfully: an
    unbalanced quote, leading/trailing whitespace, or a whitespace run other
    than a single space between two tokens (bash cannot re-emit a wider gap
    between two argv elements).
    """
    tokens: List[str] = []
    gaps: List[str] = []
    gap: List[str] = []
    current: List[str] = []
    in_quotes = False
    for char in value:
        if char == '"':
            if gap and not current:
                gaps.append("".join(gap))
                gap = []
            in_quotes = not in_quotes
            current.append(char)
            continue
        if char.isspace() and not in_quotes:
            if current:
                tokens.append("".join(current))
                current = []
            gap.append(char)
            continue
        if gap:
            if not current and not tokens:
                # Leading whitespace: cmd.exe would skip it, but a re-spelled
                # command line cannot reproduce it.
                return None
            gaps.append("".join(gap))
            gap = []
        current.append(char)
    if in_quotes or not current:
        return None
    tokens.append("".join(current))
    if len(gaps) != len(tokens) - 1:
        return None
    if any(one_gap != _SPACE for one_gap in gaps):
        return None
    return tokens


def _segments(token: str) -> List[Tuple[str, bool]]:
    """Split one token into ``(text, quoted)`` regions.

    ``quoted`` marks the regions the AUTHOR quoted inside the command string:
    their quotes are re-emitted (they are bash quotes on the way out and cmd
    quotes on the way in, which is what the author asked for).
    """
    segments: List[Tuple[str, bool]] = []
    buffer: List[str] = []
    in_quotes = False
    for char in token:
        if char == '"':
            segments.append(("".join(buffer), in_quotes))
            buffer = []
            in_quotes = not in_quotes
        else:
            buffer.append(char)
    segments.append(("".join(buffer), in_quotes))
    return segments


# ── emitter ──────────────────────────────────────────────────────────────────


def _dq_escape(text: str) -> Optional[str]:
    """Escape *text* for a bash double-quoted region, or ``None``.

    Inside ``"..."`` bash only treats ``\\``, ``\\"``, ``\\$``, ``\\` `` and a
    line continuation as escapes, so a lone backslash is safe — but a backslash
    that PRECEDES one of those characters (or ends the region) must be doubled
    or it would eat it.
    """
    out: List[str] = []
    for index, char in enumerate(text):
        if char in _REFUSED_CHARS:
            return None
        if char == "\\":
            following = text[index + 1 : index + 2]
            out.append("\\\\" if following in ("", "\\", '"', "$", "`") else "\\")
        else:
            out.append(char)
    return "".join(out)


def _needs_quoting(text: str) -> bool:
    """True when an unquoted *text* run carries a bash-special character."""
    return any(char in _BASH_SPECIAL for char in text)


def _render_token(token: str) -> Optional[str]:
    """Bash spelling of one cmd-level *token*, or ``None`` to refuse it.

    The emitted spelling yields exactly the token's text as ONE bash word, so
    the MSYS runtime re-quotes it for cmd.exe element by element.
    """
    out: List[str] = []
    for text, quoted in _segments(token):
        if not text:
            continue
        escaped = _dq_escape(text)
        if escaped is None:
            return None
        if not quoted and any(char in _UNQUOTED_REFUSED for char in text):
            # cmd.exe's ``^`` escape would change meaning inside bash quotes.
            return None
        out.append('"' + escaped + '"' if quoted or _needs_quoting(text) else escaped)
    return "".join(out)


def _quoted_for_bash(word: object) -> bool:
    """True when *word*'s span is wholly ONE bash quote region.

    A word with any other anatomy (``pre"..."``, ``a\\ b``, ``$x``) has no
    single value to re-spell, and a partially quoted word means the author's
    command string is already split.
    """
    from kimix_native.shell_compat.models import WordPartKind

    parts = tuple(getattr(word, "parts", ()) or ())
    if len(parts) != 1:
        return False
    return parts[0][0] in (WordPartKind.DOUBLE_QUOTE, WordPartKind.SINGLE_QUOTE)


def _standalone_span(source: str, start: int, end: int) -> bool:
    """True when ``source[start:end]`` is a whole bash word (not a partial span).

    The tolerant parser never re-normalizes, but it can report a span that
    starts INSIDE a word (``pre"..."`` is tiled from the quote on, dropping the
    literal prefix from the span while keeping it in the value).  Re-spelling
    such a span would silently drop the prefix, so a word is only rewritten
    when its span is bounded by word separators.
    """
    if start < 0 or end <= start or end > len(source):
        return False
    before = source[start - 1] if start else " "
    after = source[end] if end < len(source) else " "
    return before in _WORD_BOUNDARY and after in _WORD_BOUNDARY


def _raw_value(raw: str) -> Optional[str]:
    """The quote-removed value of one wholly quoted word *raw*, or ``None``.

    This is the reference decode of the SPAN TEXT: a wholly single-quoted word
    keeps its bytes, a wholly double-quoted word drops the outer quotes and
    applies bash's double-quote escapes.  ``None`` when *raw* is not one
    complete quote region or carries an expansion (``$``/backtick), where quote
    removal alone does not determine the value.
    """
    if len(raw) < 2 or raw[0] != raw[-1] or raw[0] not in "\"'":
        return None
    inner = raw[1:-1]
    if raw[0] == "'":
        return None if "'" in inner else inner
    out: List[str] = []
    index = 0
    while index < len(inner):
        char = inner[index]
        if char in ('"', "$", "`"):
            return None
        if char != "\\":
            out.append(char)
            index += 1
            continue
        following = inner[index + 1 : index + 2]
        if following in ("\\", '"', "$", "`"):
            out.append(following)
            index += 2
            continue
        if following == "\n":
            # Line continuation: removed by bash.
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _carries_space(token: str) -> bool:
    """True when *token* forces the MSYS runtime to quote the argument.

    A space makes the runtime wrap the element in quotes — which is what puts a
    ``"`` on cmd.exe's command line, and therefore what decides whether the
    command string starts with a quote (see the ``call`` rule below).
    """
    return any(char in " \t" for char in token)


def _program_label(token: str) -> str:
    """The report spelling of a command string's first token (quotes off)."""
    if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        return token[1:-1]
    return token


# ── the rule ─────────────────────────────────────────────────────────────────


def _command_string_rewrite(
    words: List[object],
    source: str,
) -> Optional[Tuple[Edit, str]]:
    """Rewrite the ONE bash-quoted command-string word, or ``None``.

    *words* are the IR words after the ``/c``/``/k`` run switch; their spans
    index into *source*.  Returns the splice plus the report word — the command
    string's PROGRAM token, the one cmd.exe could not resolve before the
    repair (quote-stripped when the author quoted it).
    """
    if len(words) != 1:
        # Several words: the author already spelled the command string as cmd
        # arguments, so there is no bash-only quoting to undo.
        return None
    word = words[0]
    if not _quoted_for_bash(word):
        return None
    span = word.span
    if not _standalone_span(source, span.start, span.end):
        return None
    raw = source[span.start : span.end]
    value = _raw_value(raw)
    if value is None or value != getattr(word, "literal_value", None):
        # The span text must decode to the value the IR reports: an expansion,
        # or a tiling whose span dropped part of the word, is not re-spellable.
        return None
    if '"' not in value:
        # No escaped quote inside the word: nothing bash-only to repair, and
        # cmd.exe's own quoting is already what reaches it.
        return None
    if any(char in _REFUSED_CHARS for char in value):
        return None
    tokens = _split_tokens(value)
    if not tokens:
        return None
    rendered: List[str] = []
    for token in tokens:
        text = _render_token(token)
        if text is None:
            return None
        rendered.append(text)
    program = _program_label(tokens[0])
    if not program:
        # ``cmd /c "\"\" x"``: no program to run.
        return None
    command_string = " ".join(rendered)
    if _carries_space(tokens[0]):
        # cmd.exe strips the first and last quote of a command string that
        # starts with a quote: ``"D:\a b\x.exe" args`` becomes ``D:\a b\x.exe``
        # unless the "exactly two quotes + an existing executable between them"
        # preserve rule happens to apply, and that rule is a filesystem fact
        # (a path created later, a mistyped path or an associated extension all
        # break it).  ``call`` makes the command string start with a word, so
        # nothing is ever stripped and the quotes cmd.exe needs are kept.
        command_string = "call " + command_string
    return (span.start, span.end, command_string), program


def cmd_command_string_edits(source: str) -> List[Edit]:
    """Return ``(start, end, replacement)`` splices for command-string words.

    Edits are returned in source order and never overlap (word spans are
    disjoint).  An empty list means the source needs no repair — the caller
    must then keep the original string object.
    """
    return [edit for edit, _program in _command_string_rewrites(source)]


def _command_string_rewrites(source: str) -> List[Tuple[Edit, str]]:
    """Every ``(splice, program)`` rewrite of *source*, in source order."""
    if not source or '"' not in source:
        # A repair always re-emits at least one quote character (a quoted
        # region, or the double quotes of the bash spelling).
        return []
    rewrites: List[Tuple[Edit, str]] = []

    def handler(words: List[object], src: str) -> None:
        rewrite = _command_string_rewrite(words, src)
        if rewrite is not None:
            rewrites.append(rewrite)

    visit: CommandStringHandler = handler
    for_each_cmd_command_string(source, visit)
    return rewrites


def cmd_command_string_fix(
    source: str,
) -> Optional[Tuple[str, Tuple[str, ...]]]:
    """Apply :func:`cmd_command_string_edits` to *source*.

    Returns ``(fixed_source, original_programs)`` when at least one command
    string was re-spelled, else ``None`` (the caller then keeps the original
    string object, preserving the byte-for-byte pass-through contract).  Each
    reported word is the command string's PROGRAM token — the one cmd.exe could
    not resolve before the repair.
    """
    rewrites = _command_string_rewrites(source)
    if not rewrites:
        return None
    fixed = source
    # Word spans are disjoint, so reverse-order splicing keeps earlier offsets
    # valid while replacing each span in place.
    for start, end, replacement in sorted(
        (edit for edit, _program in rewrites), reverse=True
    ):
        fixed = fixed[:start] + replacement + fixed[end:]
    return fixed, tuple(program for _edit, program in rewrites)
