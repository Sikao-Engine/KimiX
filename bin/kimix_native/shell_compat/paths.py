r"""Path, null-device, and ``cd /d`` translation rules (Phase 4).

Part of the lossless tolerant-IR ``kimix_native.shell_compat`` implementation
package behind the stable ``_shell_compat`` facade.

Phase 4 of the ``_shell_compat`` refactor (plan section 3.1 / Phase 4 / section
3.5 rule 6 "path and ``cd /d`` normalization" and rule 7 "output-redirection
``nul`` normalization").  The Windows-path recognition, Git Bash virtual-path
rewrite, null-device redirection rule, and the cmd.exe ``cd /d`` flag drop lived
as methods on :class:`kimix_native.shell_compat.bash_core._BashFixScanner` and as
module helpers next to it.  This module owns their canonical implementation.

Two shapes are exposed on purpose:

* **Pure string primitives** (``windows_path_replacement``,
  ``git_bash_abs_path_replacement``, ``quote_virtual_path``,
  ``path_replacement``, ``decode_unquoted_word``, ``normalize_windows_path``,
  ``quote_path_word``, ``plausible_path_segments``, ``decode_virtual_path_span``,
  ``split_virtual_path_expansions``) — the legacy methods/functions lifted to
  free functions with byte-identical semantics.  ``bash_core`` delegates these,
  so the LIVE scanner keeps its byte-for-byte behavior while the implementation
  now lives here.  A pure rule is a function of ``(raw_word)`` because the
  legacy scanner already resolves the word's raw text before calling it.

* **IR-driven pure functions** (``path_replacement_for_word``,
  ``nul_replacement_for_redirection``, ``cd_d_flag_span``) — the same rules
  expressed over lossless IR nodes (``WordNode`` / ``RedirectionNode`` /
  ``CommandNode``) plus the source string, so Phase 7's translator can schedule
  edits from a parsed tree.  They are functions of ``(node, source)`` and return
  either a replacement string (the new word text) or a ``SourceSpan``/``None``;
  they NEVER mutate the source.  The redirection rule is read from the EFFECTIVE
  WORD VALUE (quote removal / ANSI-C expansion) exactly like the legacy
  ``_is_effective_nul_target`` + ``_literal_word_value`` pair, and the
  operator/fd decision mirrors the legacy ``redirect_op`` membership test
  byte-for-byte.

Monkeypatch contract (unchanged).  ``_windows_temp_dir`` is the ONE name the
test suite monkeypatches through the ``kimix_native._shell_compat`` facade
dict (it patched the single flat namespace of the historical single-file
implementation).  The
virtual-path ``/tmp`` rewrite reads the temp directory through
``bash_core._windows_temp_dir_from_namespace`` — the same late-binding resolver
the live scanner uses — so a facade patch is honored regardless of where the
rewrite function is defined.  This module never re-implements the temp-dir
access; it routes through that resolver (directly imported from ``bash_core``),
and the IR wrapper ``path_replacement_for_word`` accepts an *optional* explicit
``temp_dir`` for direct unit testing (when omitted it uses the same resolver).

Region boundaries and the quote/expansion matcher family are reused from
``bash_core`` (``_match_*``) — the single-source-of-truth contract the lexer
also relies on — never re-implemented here.  The ``$'...'`` ANSI-C decode used
by the null-device rule is the legacy scanner decoder (``_BashFixScanner._ansi_c_literal_span``),
imported here as a bound classmethod so an ANSI-C spelling of ``nul``
(``$'nul'``, ``$'\156ul'``) decodes to ``nul`` exactly like the live scanner
resolves it (the IR's :func:`~kimix_native.shell_compat.models.decode_word_literal`
deliberately returns ``None`` for ANSI-C because it only performs *static quote
removal*; the null-device rewrite is allowed to use the literal — not runtime —
value, which is why the legacy predicate decodes ANSI-C).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable, List, Optional, Tuple

try:
    import regex as re
except ImportError:  # pragma: no cover
    import re  # stdlib: the used patterns are all stdlib-compatible

if TYPE_CHECKING:  # pragma: no cover
    from kimix_native.shell_compat.models import CommandNode, RedirectionNode, WordNode
    from kimix_native.shell_compat.spans import SourceSpan

# ── reused constants / helpers (canonical home stays bash_core) ──────────────
# These are the literal values and the matchers/decoder that carry the legacy
# boundary and ANSI-C decode semantics; the path rules below read those same
# objects rather than duplicating them, so a regression in a constant or matcher
# cannot silently fork.  ``_windows_temp_dir_from_namespace`` is the late-binding
# resolver that honors a facade monkeypatch of ``_windows_temp_dir`` (the ONE
# patched name); this module never re-implements the temp-dir access.  The
# legacy scanner supplies the word/flag decoders (``_literal_word_value``,
# ``_is_effective_nul_target``, ``_read_word``, ``_redirection_after_fd``) the
# null-device and cd rules must mirror exactly; importing it is safe because
# bash_core does NOT import this module at load time.
from kimix_native.shell_compat.bash_core import (
    _ASSIGNMENT_RE,
    _ESCAPED_LITERAL_CHARS,
    _OPERATOR_CHARS,
    _PATH_DRIVE_RE,
    _PATH_GLOB_CHARS,
    _PATH_SAFE_CHARS,
    _PATH_SEGMENT_RE,
    _SPECIAL_PARAM_CHARS,
    _VAR_NAME_CHARS,
    _VAR_NAME_START_CHARS,
    _BashFixScanner,
    _match_ansi_c_quote_span,
    _match_arithmetic_span,
    _match_parameter_braces,
    _windows_temp_dir_from_namespace,
)

# The scanner instance used purely for the legacy word/flag decoders and cd-flag
# word boundary reading (constructed empty for the pure word decoders).
_LITERAL_WORD_VALUE: Callable[[str], Optional[str]] = _BashFixScanner._literal_word_value

# Operators that, in the legacy scanner's redirection handler, are NOT eligible
# for a null-device rewrite (pure input side).  Kept verbatim (a frozenset test
# of the captured ``redirect_op`` operator text, which the scanner reads AFTER
# consuming any leading fd digits -- so ``2< nul`` has op ``<`` and ``2<> nul``
# has op ``<>``).  The bare ``&`` (background) never reaches this code: the
# scanner only enters the redirection arm for ``&`` when followed by ``>``, so
# the captured text is always ``&>``/``&>>``/``>&``/``<&``/the truncating set.
# ``<>`` is intentionally absent: it opens its target for reading *and* writing,
# so ``<> nul`` does create the file and is rewritten.
_NUL_FORBIDDEN_OPERATORS = frozenset({"<", "<<", "<<-", "<<<", "<&"})

__all__ = [
    # pure string primitives (ported)
    "windows_path_replacement",
    "git_bash_abs_path_replacement",
    "quote_virtual_path",
    "path_replacement",
    "decode_unquoted_word",
    "normalize_windows_path",
    "quote_path_word",
    "plausible_path_segments",
    "decode_virtual_path_span",
    "split_virtual_path_expansions",
    "virtual_path_variable_references",
    "path_assignment_edit",
    "path_assignment_edits",
    "has_path_assignment_translation",
    "has_python_owned_path_translation",
    "is_effective_nul_word",
    # IR-driven pure functions (Phase 4 design surface)
    "path_replacement_for_word",
    "nul_replacement_for_redirection",
    "cd_d_flag_span",
    "cd_flag_span_after",
    "NUL_REPLACEMENT",
]


# ======================================================================
# module-level helpers (ported verbatim from the bash_core module functions
# _decode_virtual_path_span / _split_virtual_path_expansions, which remain
# re-exported from bash_core for the facade's 195-name __all__).
# ======================================================================


def decode_virtual_path_span(span: str) -> Optional[str]:
    """Decode Bash unquoted-word backslash escapes in a literal path span.

    Byte-for-byte identical to :func:`bash_core._decode_virtual_path_span`
    (that name stays re-exported from bash_core and delegates here through
    ``_split_virtual_path_expansions``' shared implementation).
    """
    out: List[str] = []
    i = 0
    n = len(span)
    while i < n:
        ch = span[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= n:
            return None
        nxt = span[i + 1]
        if nxt == "\n":
            i += 2  # line continuation: both characters drop from the value
            continue
        if nxt in "*?[{}":
            return None
        out.append(nxt)
        i += 2
    return "".join(out)


def split_virtual_path_expansions(text: str) -> Optional[List[Tuple[bool, str]]]:
    """Split a rewritten virtual path into literal and expansion spans.

    Byte-for-byte identical to :func:`bash_core._split_virtual_path_expansions`.
    Returns a list of ``(is_expansion, span)`` runs in source order, or ``None``
    when the word embeds a command substitution / ``$"..."`` / an unterminated
    expansion / a quote character.
    """
    spans: List[Tuple[bool, str]] = []
    literal_start = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            i += 2  # an escaped char stays in the literal span
            continue
        if ch in "'\"`":
            return None
        if ch != "$":
            i += 1
            continue
        nxt = text[i + 1] if i + 1 < n else ""
        if nxt == "(":
            if text.startswith("$((", i):
                end = _match_arithmetic_span(text, i + 3)
                if end is None:
                    return None
            else:
                return None
        elif nxt == '"':
            return None
        elif nxt == "{":
            end = _match_parameter_braces(text, i + 2)
            if end is None:
                return None
        elif nxt == "'":
            end = _match_ansi_c_quote_span(text, i + 2)
            if end is None:
                return None
        elif nxt in _VAR_NAME_START_CHARS:
            end = i + 2
            while end < n and text[end] in _VAR_NAME_CHARS:
                end += 1
        elif nxt in _SPECIAL_PARAM_CHARS:
            end = i + 2
        else:
            i += 1  # a ``$`` starting no expansion is a literal dollar sign
            continue
        if literal_start < i:
            spans.append((False, text[literal_start:i]))
        spans.append((True, text[i:end]))
        i = end
        literal_start = i
    if literal_start < n:
        spans.append((False, text[literal_start:]))
    return spans


# ======================================================================
# pure path primitives (ported from the scanner methods, semantics unchanged)
# ======================================================================


def plausible_path_segments(raw: str) -> bool:
    """Require at least one segment that looks like a real directory name.

    Byte-for-byte identical to ``_BashFixScanner._plausible_path_segments``.
    """
    return any(
        len(segment) >= 2 and segment[0].isalpha()
        for segment in raw.split("\\")
    )


def decode_unquoted_word(raw: str) -> str:
    """Return the word value after Bash quote removal (unquoted form).

    Byte-for-byte identical to ``_BashFixScanner._decode_unquoted_word``.
    """
    value: List[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\\" and i + 1 < len(raw):
            value.append(raw[i + 1])
            i += 2
        else:
            value.append(ch)
            i += 1
    return "".join(value)


def normalize_windows_path(raw: str) -> str:
    """Rewrite backslashes as the forward slashes Git Bash understands.

    Byte-for-byte identical to ``_BashFixScanner._normalize_windows_path``.
    """
    out: List[str] = []
    i = 0
    n = len(raw)
    if n >= 2 and raw.startswith("\\\\"):
        out.append("//")
        i = 2
    while i < n:
        ch = raw[i]
        if ch == "\\" and i + 1 < n:
            nxt = raw[i + 1]
            if nxt == "\\":
                out.append("/")
            elif nxt in _ESCAPED_LITERAL_CHARS:
                out.append(nxt)
            else:
                out.append("/")
                out.append(nxt)
            i += 2
        elif ch == "\\":
            out.append("/")
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def quote_path_word(normalized: str) -> str:
    """Quote a normalized path only when unquoted emission would break it.

    Byte-for-byte identical to ``_BashFixScanner._quote_path_word`` (which
    recursed through ``self``; here it recurses through the same function).
    """
    if all(ch in _PATH_SAFE_CHARS for ch in normalized):
        return normalized
    if normalized.startswith("~"):
        rest = normalized[1:]
        if rest.startswith("/"):
            return "~/" + quote_path_word(rest[1:])
        return "~" + quote_path_word(rest)
    out: List[str] = []
    buf: List[str] = []

    def _flush() -> None:
        if not buf:
            return
        span = "".join(buf)
        buf.clear()
        if all(ch in _PATH_SAFE_CHARS for ch in span):
            out.append(span)
            return
        escaped = (
            span.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("$", "\\$")
            .replace("`", "\\`")
        )
        out.append('"' + escaped + '"')

    for ch in normalized:
        if ch in _PATH_GLOB_CHARS:
            _flush()
            out.append(ch)
        else:
            buf.append(ch)
    _flush()
    return "".join(out)


def windows_path_replacement(raw: str) -> Optional[str]:
    """Return the Git Bash spelling of a Windows backslash path word.

    Byte-for-byte identical to ``_BashFixScanner._windows_path_replacement``.
    Only unquoted words that unambiguously look like a Windows path are
    rewritten (drive paths, UNC shares, root-/home-/dot-relative, multi-segment
    relative paths); quoted / expansion-bearing / ambiguous short words return
    ``None``.
    """
    if not raw or "\\" not in raw:
        return None
    backslashes = 0
    for ch in raw:
        if ch == "\\":
            backslashes += 1
        elif ch in "'\"`$\n\r":
            return None
    if _PATH_DRIVE_RE.fullmatch(raw):
        pass
    elif raw.startswith("\\\\") and len(raw) > 2:
        pass
    elif raw.startswith("\\") and not raw.startswith("\\\\") and backslashes >= 2:
        if not plausible_path_segments(raw):
            return None
    elif raw.startswith("~\\") or raw.startswith("~/"):
        pass
    elif (
        raw.startswith(".\\")
        or raw.startswith("./")
        or raw.startswith("..\\")
        or raw.startswith("../")
    ):
        pass
    elif backslashes >= 2:
        decoded = decode_unquoted_word(raw)
        if (
            len(decoded) < 2
            or not any(ch.isalnum() for ch in decoded)
            or not _PATH_SEGMENT_RE.fullmatch(decoded)
            or not plausible_path_segments(raw)
        ):
            return None
    else:
        return None
    return quote_path_word(normalize_windows_path(raw))


def quote_virtual_path(normalized: str) -> Optional[str]:
    """Quote a rewritten virtual path without disabling shell expansions.

    Byte-for-byte identical to ``_BashFixScanner._quote_virtual_path``.
    """
    spans = split_virtual_path_expansions(normalized)
    if spans is None:
        return None
    out: List[str] = []
    for is_expansion, span in spans:
        if is_expansion:
            out.append(span)
            continue
        decoded = decode_virtual_path_span(span)
        if decoded is None:
            return None
        out.append(quote_path_word(decoded))
    return "".join(out)


def _virtual_prefix_rewrite_is_safe(raw: str) -> bool:
    """Whether replacing only the virtual root preserves the word's value.

    ``..`` does not commute with a mount rewrite (``/tmp/..`` is the Git Bash
    root, not the parent of Windows TEMP).  A literal colon can join several
    path values (classpaths, host:guest pairs); rewriting only the first prefix
    would silently produce a half-translated list.  Expansion spans are opaque
    data here, so a parameter operator such as ``${x:-default}`` is not mistaken
    for a literal list separator.
    """
    spans = split_virtual_path_expansions(raw)
    if spans is None:
        return True  # the existing quoting validator will reject the word
    for is_expansion, span in spans:
        if is_expansion:
            continue
        decoded = decode_virtual_path_span(span)
        if decoded is None:
            return True  # preserve the existing conservative rejection path
        if ":" in decoded or any(
            segment == ".." for segment in decoded.split("/")
        ):
            return False
    return True


def git_bash_abs_path_replacement(
    raw: str, temp_dir: Optional[str] = None
) -> Optional[str]:
    """Return the native Windows spelling of a Git Bash virtual absolute path.

    Implements ``_BashFixScanner._git_bash_abs_path_replacement``
    (``/tmp/...`` -> the real Windows temp dir, ``/c/...`` -> ``C:/...``).
    Non-commuting ``..`` segments and literal colon-joined words are rejected
    before the prefix rewrite.  When an explicit ``temp_dir`` is
    passed it is used instead of the late-binding resolver.  When ``temp_dir`` is
    ``None`` (the live scanner path) the rewrite reads the temp dir through
    :func:`bash_core._windows_temp_dir_from_namespace`, honoring a facade
    monkeypatch of ``_windows_temp_dir`` exactly like the scanner did.
    """
    if not raw:
        return None
    if not _virtual_prefix_rewrite_is_safe(raw):
        return None
    if raw.startswith("/tmp"):
        if raw != "/tmp" and not raw.startswith("/tmp/"):
            return None
        base = temp_dir if temp_dir is not None else _windows_temp_dir_from_namespace()
        return quote_virtual_path(base + raw[4:])
    if len(raw) >= 3 and raw[0] == "/" and raw[1].isalpha() and raw[2] == "/":
        return quote_virtual_path(raw[1].upper() + ":" + raw[2:])
    return None


def path_replacement(raw: str, temp_dir: Optional[str] = None) -> Optional[str]:
    """Return the Git Bash spelling for a Windows path word.

    Covers the native backslash spelling and safe Git Bash virtual POSIX
    absolute spellings for ``_BashFixScanner._path_replacement``;
    ``temp_dir`` is threaded to :func:`git_bash_abs_path_replacement` for direct
    unit testing and is ``None`` on the live scanner path.
    """
    if not raw or ("\\" not in raw and raw[0] != "/"):
        return None
    replacement = windows_path_replacement(raw)
    if replacement is not None:
        return replacement
    return git_bash_abs_path_replacement(raw, temp_dir)


# ======================================================================
# usage-aware virtual-path assignments
# ======================================================================

# ``L=/c/dev/x; clang++ -I $L/include`` needs the same /c/ -> C:/ rewrite as a
# literal clang++ operand: native Windows programs do not understand Git Bash's
# virtual drive mounts.  Assignments remain data by default, however; the mere
# spelling ``x=/tmp/x`` is not proof that x will be consumed as a path.  The
# analysis below therefore activates the rule only for a LATER executable-word
# reference that appends a path suffix (``$L/...`` or ``${L}/...``).
_PLAIN_PATH_REFERENCE_RE = re.compile(
    r"(?<![\\$])\$([A-Za-z_][A-Za-z0-9_]*)(?=/)"
)
_BRACED_PATH_REFERENCE_RE = re.compile(
    r"(?<![\\$])\$\{([A-Za-z_][A-Za-z0-9_]*)\}(?=/)"
)
_BRACED_PARAMETER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}\Z")
_PLAIN_PARAMETER_AT_END_RE = re.compile(
    r"(?<![\\$])\$([A-Za-z_][A-Za-z0-9_]*)\Z"
)
_BRACED_PARAMETER_AT_END_RE = re.compile(
    r"(?<![\\$])\$\{([A-Za-z_][A-Za-z0-9_]*)\}\Z"
)
_VIRTUAL_ASSIGNMENT_GATE_RE = re.compile(r"=/[A-Za-z]/")
_MAX_PATH_WALK_DEPTH = 16


def _executable_commands(node: object, depth: int = 0):
    """Yield commands whose words and spans belong to the same source string.

    Command/process substitutions are executable child contexts and share the
    root coordinates.  Heredocs and runtime-reparsed quoted scripts are not
    entered: the former are data, while the latter's inner nodes index a
    different source string.  The explicit depth bound keeps malformed input a
    conservative no-op.
    """
    if depth > _MAX_PATH_WALK_DEPTH:
        return
    from kimix_native.shell_compat.models import NodeKind

    kind = node.kind
    if kind == NodeKind.COMMAND:
        yield node
        for expansion in node.expansions:
            yield from _executable_commands(expansion, depth + 1)
        return
    if kind == NodeKind.EXPANSION:
        child = node.child
        if child is not None:
            yield from _executable_commands(child, depth + 1)
        return
    if kind in (NodeKind.EMBEDDED, NodeKind.HEREDOC, NodeKind.RAW):
        return
    for attr in ("children", "commands"):
        for child in getattr(node, attr, ()) or ():
            yield from _executable_commands(child, depth + 1)
    if kind == NodeKind.COMPOUND:
        for _part_name, part in node.parts:
            yield from _executable_commands(part, depth + 1)


def _command_words(command: object):
    """Yield executable words, excluding heredoc delimiters and bodies."""
    yield from command.assignments
    if command.command_word is not None:
        yield command.command_word
    yield from command.arguments
    for redirection in command.redirections:
        if redirection.target is not None:
            yield redirection.target


def _record_word_path_references(
    word: object, source: str, references: dict[str, List[int]]
) -> None:
    """Collect simple variable expansions immediately followed by ``/``.

    Lexer parts provide the quote boundaries: single/ANSI-C quotes and escaped
    characters are never searched, while literal and double-quoted regions may
    contain expansions.  A braced parameter split from its suffix is handled
    from its exact source span.  More complex parameter operators are opaque.
    """
    from kimix_native.shell_compat.models import WordPartKind

    searchable = {
        WordPartKind.LITERAL,
        WordPartKind.DOUBLE_QUOTE,
        WordPartKind.LOCALE_QUOTE,
        WordPartKind.PARAMETER_EXPANSION,
    }
    for kind, span in word.parts:
        if kind not in searchable:
            continue
        text = span.text(source)
        for pattern in (_PLAIN_PATH_REFERENCE_RE, _BRACED_PATH_REFERENCE_RE):
            for match in pattern.finditer(text):
                name = match.group(1)
                references.setdefault(name, []).append(span.start + match.start())
        if kind == WordPartKind.PARAMETER_EXPANSION:
            match = _BRACED_PARAMETER_RE.fullmatch(text)
            if (
                match is not None
                and span.end < len(source)
                and source[span.end] == "/"
            ):
                references.setdefault(match.group(1), []).append(span.start)
        if (
            kind in (WordPartKind.DOUBLE_QUOTE, WordPartKind.LOCALE_QUOTE)
            and span.end < len(source)
            and source[span.end] == "/"
        ):
            # The suffix may follow the closing quote: both ``"$L"/include``
            # and ``"${L}"/include`` are executable path uses, just like
            # ``"$L/include"``.  Inspect only the expansion at the END of the
            # quoted region; an earlier variable is not the path-root segment
            # directly joined to the suffix.  Single/ANSI-C quotes never enter
            # this branch and remain data.
            prefix_len = 2 if kind == WordPartKind.LOCALE_QUOTE else 1
            prefix = '$"' if prefix_len == 2 else '"'
            if not (text.startswith(prefix) and text.endswith('"')):
                continue
            inner = text[prefix_len:-1]
            for pattern in (
                _PLAIN_PARAMETER_AT_END_RE,
                _BRACED_PARAMETER_AT_END_RE,
            ):
                match = pattern.search(inner)
                if match is not None:
                    position = span.start + prefix_len + match.start()
                    references.setdefault(match.group(1), []).append(position)


def _may_have_virtual_path_assignment(source: str) -> bool:
    """Cheap pre-parse gate; never decides whether a rewrite is legal."""
    return (
        "=" in source
        and "$" in source
        and (
            "=/tmp" in source
            or _VIRTUAL_ASSIGNMENT_GATE_RE.search(source) is not None
        )
    )


def _path_assignment_analysis(source: str):
    """Parse once and return ``(commands, later-path-reference positions)``."""
    if not _may_have_virtual_path_assignment(source):
        return (), {}
    try:
        from kimix_native.shell_compat.parser import parse

        commands = tuple(_executable_commands(parse(source)))
    except (ImportError, RecursionError):  # pragma: no cover - defensive fallback
        return (), {}
    references: dict[str, List[int]] = {}
    for command in commands:
        for word in _command_words(command):
            _record_word_path_references(word, source, references)
    return commands, references


def virtual_path_variable_references(source: str) -> dict[str, List[int]]:
    """Return executable ``$NAME/...`` / ``${NAME}/...`` reference positions."""
    return _path_assignment_analysis(source)[1]


def path_assignment_edit(
    raw: str,
    references: dict[str, List[int]],
    assignment_start: int = 0,
) -> Optional[Tuple[int, str, str]]:
    """Return ``(value_offset, original, replacement)`` for a proven path root.

    Only a plain, unquoted ``NAME=VALUE`` assignment is eligible (not ``+=``),
    VALUE must independently satisfy :func:`path_replacement`, and at least one
    path-suffix reference must occur AFTER the complete assignment word.  The
    caller splices only the value span, leaving the variable name and ``=``
    byte-for-byte intact.
    """
    match = _ASSIGNMENT_RE.match(raw)
    if match is None:
        return None
    name = raw[: match.end() - 1]
    if name.endswith("+"):
        return None
    assignment_end = assignment_start + len(raw)
    if not any(
        position >= assignment_end for position in references.get(name, ())
    ):
        return None
    value_offset = match.end()
    original = raw[value_offset:]
    replacement = path_replacement(original)
    if replacement is None:
        return None
    return value_offset, original, replacement


def path_assignment_edits(source: str) -> List[Tuple[int, int, str]]:
    """Return source-coordinate value edits for all proven path assignments."""
    commands, references = _path_assignment_analysis(source)
    edits: List[Tuple[int, int, str]] = []
    for command in commands:
        for word in command.assignments:
            raw = word.span.text(source)
            decision = path_assignment_edit(raw, references, word.span.start)
            if decision is None:
                continue
            offset, _original, replacement = decision
            edits.append((word.span.start + offset, word.span.end, replacement))
    return edits


def has_path_assignment_translation(source: str) -> bool:
    """Whether the source or an embedded script needs the Python-owned rule.

    Embedded scripts have independent source coordinates and are deliberately
    excluded from ``path_assignment_edits``.  Inspect their own source strings
    here so the native facade still selects the reference engine when a nested
    shell/eval operand would exercise the new rule.
    """
    if path_assignment_edits(source):
        return True
    if not _may_have_virtual_path_assignment(source):
        return False
    try:
        from kimix_native.shell_compat.models import EmbeddedScriptNode, walk_nodes
        from kimix_native.shell_compat.parser import parse

        for node in walk_nodes(parse(source)):
            if not isinstance(node, EmbeddedScriptNode) or node.inner is None:
                continue
            inner_source = node.inner.source
            if inner_source != source and path_assignment_edits(inner_source):
                return True
    except (ImportError, RecursionError):  # pragma: no cover - defensive fallback
        return False
    return False


# The compiled kernel predates command-role and non-commuting-path guards.  A
# cheap source predicate routes only potentially affected inputs through the
# reference implementation; quoted/data false positives merely lose acceleration,
# never change behavior.  Keeping this decision here avoids rule logic in the
# stable facade.
_CONTEXT_PATH_COMMAND_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:find|test|wsl(?:\.exe)?|wslpath(?:\.exe)?)"
    r"(?![A-Za-z0-9_])|\["
)
_VIRTUAL_PATH_CANDIDATE_RE = re.compile(
    r"/tmp(?![A-Za-z0-9_])|/[A-Za-z]/"
)


def has_python_owned_path_translation(source: str) -> bool:
    """Whether a source may exercise a rule absent from the native kernel."""
    if has_path_assignment_translation(source):
        return True
    has_virtual_path = _VIRTUAL_PATH_CANDIDATE_RE.search(source) is not None
    if _CONTEXT_PATH_COMMAND_RE.search(source) is not None and (
        has_virtual_path or "\\" in source
    ):
        return True
    return has_virtual_path and (":" in source or ".." in source)


# ======================================================================
# null-device rule (ported from _is_effective_nul_word / the redirection arm)
# ======================================================================



def is_effective_nul_word(raw: str) -> bool:
    """Return True when redirection target word *raw* resolves to ``nul``.

    Byte-for-byte identical to the legacy ``_BashFixScanner._is_effective_nul_target``
    (which now delegates here so the live scanner and the IR rule share ONE
    definition — the scanner method calls this function).  True for every
    spelling whose literal value (after Bash quote removal / ANSI-C expansion,
    decoded by ``_BashFixScanner._literal_word_value``) is exactly the word
    ``nul`` in any case; whole-token quoted ``'nul'``/``"nul"`` (no backslash) are
    intentional filenames (``False``); runtime-expansion words (``$var``,
    ``${...}``, ``$(...)``, backquote) are ``False``.
    """
    if not raw:
        return False
    if "$" in raw and not raw.startswith(("$'", '$"')):
        return False
    if "`" in raw:
        return False
    if raw[:1] in ("'", '"') and raw[-1:] == raw[:1] and len(raw) >= 2:
        inner = raw[1:-1]
        if inner.casefold() == "nul" and "\\" not in inner:
            return False
    value = _LITERAL_WORD_VALUE(raw)
    return value is not None and value.casefold() == "nul"


# The fixed replacement text the legacy scanner emits for a null-device target.
NUL_REPLACEMENT = "/dev/null"


# ======================================================================
# IR-driven pure functions (Phase 4 design surface)
# ======================================================================


def path_replacement_for_word(
    word: "WordNode", source: str, temp_dir: Optional[str] = None
) -> Optional[str]:
    """IR wrapper: the path replacement for a parsed ``WordNode``, or ``None``.

    Extracts the word's exact raw source text (``word.span.text(source)``) and
    applies :func:`path_replacement`.  Pure: never mutates the source.  This is
    the primitive Phase 7's translator will call from a ``CommandNode`` walk;
    the live scanner still drives the pure ``path_replacement`` directly.
    """
    raw = word.span.text(source)
    return path_replacement(raw, temp_dir)


def nul_replacement_for_redirection(
    redir: "RedirectionNode", source: str
) -> Optional[str]:
    """IR rule: the null-device replacement for a parsed ``RedirectionNode``.

    A pure function of the redirection target's EFFECTIVE WORD VALUE plus the
    captured operator text, mirroring the legacy scanner's redirection arm
    byte-for-byte (``bash_core`` lines: ``redirect_op not in {"<", "<<", "<<-",
    "<<<", "<&"} and self._is_effective_nul_target(raw_word)``):

    * returns ``"/dev/null"`` when the operator is a write-capable redirection
      (any operator not in :data:`_NUL_FORBIDDEN_OPERATORS`) whose target word
      (the whole raw token, after Bash quote removal / ANSI-C expansion) is the
      bare null device in any spelling: plain ``nul``/``NUL``, backslash-escaped
      (``\\nul``, ``n\\ul``, ``nu\\l``), ANSI-C (``$'nul'``), or
      empty-quote-concatenated (``nul''``, ``''nul``, ``n""ul``);
    * returns ``None`` (leave the target byte-for-byte) for a pure input
      redirection (``<``, ``<<``, ``<<-``, ``<<<``, ``<&`` — ``<>`` is NOT, it
      opens for reading *and* writing), for a whole-token quoted
      ``'nul'``/``"nul"`` (an intentional filename, documented policy), and for
      any target whose value only resolves at runtime (``$(...)``, backtick,
      ``${...}``, ``$var``) or is a different string;
    * fd handling matches the legacy: ``2> nul`` -> rewrite (``2>`` not in the
      forbidden set); ``2>&1`` / ``>&2`` -> None (the target is a fd digit so the
      effective value is not ``nul``); ``>> nul`` -> rewrite.

    ``redir.target`` is the parsed ``WordNode`` for the target; ``redir.span``
    covers operator + target and ``redir.operator_text`` is the captured
    operator token text (which the scanner reads as ``redirect_op``).
    """
    target = getattr(redir, "target", None)
    if target is None:
        return None
    raw = target.span.text(source)
    # Operator text: prefer the explicit captured operator; fall back to slicing
    # the redirection span up to the target start so the function still works on
    # a hand-built node without an operator_text field.
    op_text = getattr(redir, "operator_text", None)
    if op_text is None:
        redir_span = getattr(redir, "span", None)
        if redir_span is not None:
            op_text = source[redir_span.start : target.span.start]
        else:
            op_text = ""
    # The legacy scanner consumes leading fd digits (``2>``, ``10>&``, ...) and
    # captures the operator text WITHOUT them, so membership in the forbidden set
    # is tested on the digit-stripped core.  ``>&``/``&>`` keep their ``&``; the
    # fd digits are only ever a numeric prefix (the scanner's ``isdigit`` run).
    op_core = op_text.lstrip("0123456789")
    # The legacy scanner decides purely on the captured operator membership.
    if op_core in _NUL_FORBIDDEN_OPERATORS:
        return None
    if is_effective_nul_word(raw):
        return NUL_REPLACEMENT
    return None


def cd_flag_span_after(reader, source: str, start: int, end: int):
    """Core ``cd /d`` decision: the flag-word span, or ``None``.

    ``reader`` is anything exposing the legacy ``_read_word(i, end,
    scan_substitutions=...)`` and ``_redirection_after_fd(i, end)`` bound to
    ``source`` -- the live scanner passes ``self`` (so the word boundary is
    resolved against the scanner's own string, byte-identical to the historical
    ``_drop_cmd_cd_flag``); :func:`cd_d_flag_span` passes a fresh scanner over
    ``source``.  *start* is the index right after the ``cd`` command word and
    *end* is the scan-range end (the line / command boundary).  The returned
    span is the ``/d``/``/D`` flag word the caller would delete; ``None`` when
    the flag is not followed by a path operand.

    The deletion conditions are identical to the legacy method:

    1. scan past horizontal whitespace ``" \\t\\r"`` to the flag word start
       ``j``; if ``j`` reaches ``end`` (bare ``cd /d``), do nothing;
    2. read the flag word (``scan_substitutions=False``); if it is not exactly
       ``/d``/``/D``, do nothing;
    3. scan past whitespace to ``k``; if ``k`` reaches ``end`` or the next char
       is an operator char (``;&|()<>\\n``) or ``#`` (a comment), do nothing;
    4. if the next char is a digit that begins an fd-prefixed redirection
       (``cd /d 2>/dev/null``), do nothing (a redirection is not a path operand);
    5. otherwise the flag word span ``[j, flag_end)`` is returned.
    """
    from kimix_native.shell_compat.spans import SourceSpan

    s = source
    j = start
    while j < end and s[j] in " \t\r":
        j += 1
    if j >= end:
        return None
    flag_end = reader._read_word(j, end, scan_substitutions=False)
    if flag_end <= j or s[j:flag_end] not in {"/d", "/D"}:
        return None
    k = flag_end
    while k < end and s[k] in " \t\r":
        k += 1
    if k >= end or s[k] in _OPERATOR_CHARS or s[k] == "#":
        return None
    if s[k].isdigit() and reader._redirection_after_fd(k, end):
        return None
    return SourceSpan(j, flag_end)


def cd_d_flag_span(command: "CommandNode", source: str) -> Optional["SourceSpan"]:
    """IR rule: the span of a deletable cmd.exe ``cd /d`` flag, or ``None``.

    A pure check over a parsed ``CommandNode`` (whose ``command_word`` is the
    literal ``cd`` word) plus the source, delegating the decision to
    :func:`cd_flag_span_after` with a scanner bound to ``source``.  The scan
    range is the command node's own span (the command's text region), so the
    "path operand follows" test sees the same trailing characters the live
    scanner sees at its line boundary.  Returns ``None`` for a node without a
    command word (the caller must only invoke this for an identified ``cd``
    command word anyway).
    """
    command_word = getattr(command, "command_word", None)
    if command_word is None:
        return None
    start = command_word.span.end
    cmd_span = getattr(command, "span", None)
    end = cmd_span.end if cmd_span is not None else len(source)
    return cd_flag_span_after(_BashFixScanner(source), source, start, end)
