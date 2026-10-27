r"""Doubled-slash flag repair for native Windows tools (``msiexec //a`` -> ``msiexec /a``).

Part of the lossless tolerant-IR ``kimix_native.shell_compat`` implementation
package behind the stable ``_shell_compat`` facade.

Background: the Bash/Pwsh tool environments on Windows deliberately disable
MSYS argv path conversion (``MSYS_NO_PATHCONV`` / ``MSYS2_ARG_CONV_EXCL``) so
that native Windows tools receive ``/flag`` switches verbatim instead of
having them rewritten into ``C:/.../Git/flag`` paths.  A side effect is that
the classic Git Bash workaround of doubling the slash — ``msiexec //a``,
meant to collapse back to ``/a`` — no longer collapses: the ``//a`` spelling
reaches the native tool verbatim, and tools like ``msiexec``/``cmd`` reject
it as an invalid switch (msiexec exits non-zero, classically 103 for an
unparseable command line, or 1639 "invalid command line").

This module repairs that one spelling, conservatively:

- Only commands whose command word names a KNOWN slash-flag native Windows
  tool are considered (``msiexec``, ``cmd``, ``tasklist``, ``schtasks``,
  ``xcopy``, ...).  POSIX tools (``find``, ``sed``, ...) are never touched,
  so data spellings like ``sed 's,a//,x'`` pass through byte-for-byte.
- Only unquoted argument words that decode to themselves and match
  ``^//[A-Za-z][A-Za-z0-9_-]*$`` are rewritten (``//a``, ``//qb``,
  ``//norestart``).  Quoted spellings (``'//a'``), escaped characters,
  expansions and UNC-looking multi-segment words (``//server/share``) are
  left untouched.
- Command words may use full paths and an ``.exe`` suffix
  (``C:/Windows/System32/msiexec.exe``); the tool name is the path stem.
- The walk recurses into pipelines, lists, compound bodies and ``$(...)`` /
  backquote / process-substitution child scripts (their spans index into the
  SAME source string).  Runtime-reparsed quoted scripts (``bash -c '...'``,
  ``eval '...'``) and heredoc bodies are NOT entered: their inner spans
  refer to a different (standalone) source string or are pure data, so they
  stay byte-for-byte (the conservative pass-through policy).
- Commands under an executable wrapper (``env msiexec ...``, ``timeout 5
  msiexec ...``) are not rewritten in this version: the wrapper word, not
  the tool, is the command word.  They pass through untouched.

The rule runs as a post-pass in the ``_shell_compat`` facade over the shared
:class:`~kimix_native.shell_compat.bash_core.BashFix` value, AFTER either the
compiled ``runtime_py.bash_fix`` kernel or the pure-Python reference scanner
has run, so both execution paths apply it exactly once and stay byte-equal
with each other.  The compiled kernel in kimix-base cannot express new rules
from this repository; the facade is the single integration point that keeps
native and pure-Python behavior identical by construction.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

try:
    import regex as re
except ImportError:  # pragma: no cover
    import re  # stdlib: the patterns are stdlib-compatible

#: Native Windows tools whose command line uses single-slash switches and
#: which agents commonly drive from the Bash tool (the ``MSYS_NO_PATHCONV``
#: environment makes doubled-slash ``//flag`` spellings reach them verbatim,
#: where they are rejected as invalid switches).  Only command words whose
#: path stem matches one of these (case-insensitively, optional ``.exe``)
#: have their ``//flag`` arguments rewritten.  Deliberately excludes POSIX
#: userland names and tools without slash-flag syntax (``net``, ``netsh``,
#: ``sc`` use bare subcommands; ``ping`` is ambiguous).
_NATIVE_SLASH_TOOLS = frozenset(
    {
        "msiexec",
        "cmd",
        "tasklist",
        "taskkill",
        "schtasks",
        "wmic",
        "xcopy",
        "robocopy",
        "reg",
        "regsvr32",
        "shutdown",
        "ipconfig",
        "tracert",
        "pathping",
        "powercfg",
        "bcdedit",
        "diskpart",
        "sfc",
        "chkdsk",
        "attrib",
        "icacls",
        "cacls",
        "takeown",
        "control",
        "rundll32",
        "findstr",
        "driverquery",
        "systeminfo",
        "assoc",
        "ftype",
        "chcp",
        "compact",
        "fsutil",
        "vssadmin",
        "dism",
    }
)

#: A doubled-slash switch worth repairing: ``//`` + a letter + switch
#: characters, nothing else.  Excludes UNC-looking words such as
#: ``//server/share`` (a further ``/``) and glob/expansion-bearing words
#: (``//L*v``) which are left for the shell/tool to interpret.
_DOUBLE_SLASH_FLAG_RE = re.compile(r"^//[A-Za-z][A-Za-z0-9_-]*$")

#: Recursion bound for the IR walk (pathological substitution nesting).
_MAX_WALK_DEPTH = 16

Edit = Tuple[int, int, str]


def _tool_name(raw: str, literal: Optional[str]) -> Optional[str]:
    """Normalize a command word to its lowercase tool stem.

    ``literal`` is the quote-removed value (``None`` when the word carries
    expansions); when unavailable the raw spelling is used.  Directory
    components and a trailing ``.exe`` are dropped so
    ``C:/Windows/System32/msiexec.exe`` resolves to ``msiexec``.
    """
    candidate = literal if literal is not None else raw
    stem = candidate.replace("\\", "/").rsplit("/", 1)[-1]
    if not stem:
        return None
    if stem.lower().endswith(".exe"):
        stem = stem[:-4]
    return stem.lower() if stem else None


#: Reserved words that the tolerant parser tiles as a COMMAND word wrapping
#: the real command in the FIRST argument (``if msiexec //a x; then ...`` is
#: tiled as word ``if`` + args ``msiexec //a x``).  The argument after the
#: keyword is itself a command word, so its ``//flag`` arguments qualify.
_CONDITION_KEYWORDS = frozenset({"if", "elif", "while", "until", "then", "do", "else", "!"})


def _handle_command_words(command: object, source: str, edits: List[Edit]) -> None:
    """Apply the flag rule to one COMMAND node's words.

    Handles both the plain shape (word = tool) and the keyword-wrapped shape
    (word = ``if``/``while``/``!``/..., first argument = tool).
    """
    word = command.command_word
    if word is None:
        return
    raw = source[word.span.start : word.span.end]
    name = _tool_name(raw, word.literal_value)
    if name in _NATIVE_SLASH_TOOLS:
        _rewrite_args(name, command.arguments, source, edits)
        return
    if name in _CONDITION_KEYWORDS and command.arguments:
        first = command.arguments[0]
        first_raw = source[first.span.start : first.span.end]
        inner = _tool_name(first_raw, first.literal_value)
        if inner is not None and inner in _NATIVE_SLASH_TOOLS:
            _rewrite_args(inner, command.arguments[1:], source, edits)


def _rewrite_args(name: str, arguments: object, source: str, edits: List[Edit]) -> None:
    for arg in arguments:
        arg_raw = source[arg.span.start : arg.span.end]
        # Only an UNQUOTED, UNESCAPED, EXPANSION-FREE word whose value is
        # exactly its spelling is rewritten: quoting or expansions would
        # make a span splice change the value.
        if (
            arg.literal_value == arg_raw
            and len(arg_raw) >= 3
            and arg_raw.startswith("//")
            and _DOUBLE_SLASH_FLAG_RE.match(arg_raw) is not None
        ):
            edits.append((arg.span.start, arg.span.end, "/" + arg_raw[2:]))


#: A degraded ``PipelineNode`` (parser quirk: a ``case`` pattern's ``|`` leaks
#: pipeline state, so a following simple command is tiled as a pipeline of
#: bare WORDS instead of command nodes) is only trusted when its span is
#: unambiguously ONE simple command — no newline or control/operator char.
_DEGRADED_PIPELINE_FORBIDDEN = frozenset("\n;&|<>()")


def _walk(node: object, source: str, edits: List[Edit], depth: int) -> None:
    """Collect ``//flag`` edits from *node*'s command contexts.

    Mirrors the slot-handler stop rules: embedded quoted scripts and heredoc
    bodies are never entered; ``$(...)``/backquote/process-substitution child
    scripts are (their spans share the root source string).
    """
    if depth > _MAX_WALK_DEPTH:
        return
    from kimix_native.shell_compat.models import NodeKind

    kind = node.kind
    if kind == NodeKind.COMMAND:
        command = node
        _handle_command_words(command, source, edits)
        for expansion in command.expansions:
            _walk(expansion, source, edits, depth + 1)
        return
    if kind == NodeKind.PIPELINE:
        commands = node.commands
        if commands and all(
            getattr(child, "kind", None) == NodeKind.WORD for child in commands
        ):
            # Degraded tiling: one simple command stored as bare words.  Only
            # treat it as a command when no operator/newline inside the span
            # could join two logical commands (else a `//flag` belonging to a
            # LATER command could be wrongly rewritten).
            span_text = source[node.span.start : node.span.end]
            if not any(ch in _DEGRADED_PIPELINE_FORBIDDEN for ch in span_text):
                first = commands[0]
                first_raw = source[first.span.start : first.span.end]
                name = _tool_name(first_raw, first.literal_value)
                if name in _NATIVE_SLASH_TOOLS:
                    _rewrite_args(name, commands[1:], source, edits)
                elif name in _CONDITION_KEYWORDS and len(commands) > 1:
                    second = commands[1]
                    second_raw = source[second.span.start : second.span.end]
                    inner = _tool_name(second_raw, second.literal_value)
                    if inner is not None and inner in _NATIVE_SLASH_TOOLS:
                        _rewrite_args(inner, commands[2:], source, edits)
            return
        for child in commands:
            _walk(child, source, edits, depth + 1)
        return
    if kind == NodeKind.EXPANSION:
        child = node.child
        if child is not None:
            _walk(child, source, edits, depth + 1)
        return
    if kind in (NodeKind.EMBEDDED, NodeKind.HEREDOC, NodeKind.RAW):
        # Embedded quoted scripts index a DIFFERENT (standalone) source
        # string; heredoc bodies and raw regions are data, not commands.
        return
    for attr in ("children", "commands"):
        for child in getattr(node, attr, ()) or ():
            _walk(child, source, edits, depth + 1)
    if kind == NodeKind.COMPOUND:
        if _is_array_literal_compound(node, source):
            # `declare -a x=(msiexec //a)` is tiled as a subshell COMPOUND,
            # but the parenthesised words are ARRAY DATA, not a command:
            # rewriting them would corrupt the array values.
            return
        for _part_name, part in node.parts:
            _walk(part, source, edits, depth + 1)


def _is_array_literal_compound(node: object, source: str) -> bool:
    """True when a subshell-shaped COMPOUND directly follows ``=`` (array data).

    ``x=(a b)`` / ``arr[i]=(a b)`` are array assignments; the tolerant parser
    tiles the parenthesised body as a subshell compound, but it is data.  A
    ``=`` as the last non-space character before the span is unambiguous:
    a real subshell command never directly follows ``=`` (command
    substitution ``$(...)`` is an EXPANSION node, not a COMPOUND).
    """
    i = node.span.start - 1
    while i >= 0 and source[i] in " \t":
        i -= 1
    return i >= 0 and source[i] == "="


def native_flag_edits(source: str) -> List[Edit]:
    """Return ``(start, end, replacement)`` edits for doubled-slash flags.

    Cheap substring gates run before the tolerant parser: without ``//`` or
    any known tool name in the source there is nothing to repair.  Edits are
    returned in source order and never overlap (word spans are disjoint).
    """
    if "//" not in source:
        return []
    lowered = source.lower()
    if not any(name in lowered for name in _NATIVE_SLASH_TOOLS):
        return []
    try:
        from kimix_native.shell_compat.models import NodeKind  # noqa: F401
        from kimix_native.shell_compat.parser import parse
    except ImportError:  # pragma: no cover - package half-installed
        return []
    script = parse(source)
    edits: List[Edit] = []
    _walk(script, source, edits, 0)
    return edits


def native_flag_fix(source: str) -> Optional[Tuple[str, Tuple[str, ...]]]:
    """Apply :func:`native_flag_edits` to *source*.

    Returns ``(fixed_source, original_words)`` when at least one doubled-slash
    flag was repaired, else ``None`` (the caller then keeps the original
    string object, preserving the byte-for-byte pass-through contract).
    """
    edits = native_flag_edits(source)
    if not edits:
        return None
    originals = tuple(source[start:end] for start, end, _repl in edits)
    fixed = source
    # Word spans are disjoint, so reverse-order splicing keeps earlier
    # offsets valid while replacing each span in place.
    for start, end, replacement in sorted(edits, reverse=True):
        fixed = fixed[:start] + replacement + fixed[end:]
    return fixed, originals
