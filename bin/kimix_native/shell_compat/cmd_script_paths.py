r"""cmd.exe command-string path repair (``cmd /c tools/build.bat`` -> ``cmd /c "tools\build.bat"``).

Part of the lossless tolerant-IR ``kimix_native.shell_compat`` implementation
package behind the stable ``_shell_compat`` facade.

The failure this rule repairs
-----------------------------
``cmd.exe`` splits its command STRING at every ``/``: the argument list

    cmd.exe /c .kimix_cache/tools/build.bat dxc

is parsed by cmd as the program ``.kimix_cache`` plus the switch
``/tools/build.bat``, so ``cmd`` reports

    '.kimix_cache' is not recognized as an internal or external command

and the batch script never runs.  Git Bash's own repairs make this worse in
BOTH directions, because they are written for bash, not for the cmd.exe command
string that receives the word verbatim (the tool environments disable MSYS argv
path conversion, so nothing is translated on the way out):

* a POSIX-style RELATIVE path (``tools/build.bat``, ``./tools/build.bat``)
  is not a drive path, so no Windows-path rule fires and cmd.exe still cannot
  resolve it;
* a Windows-style relative path (``.\tools\build.bat``) is rewritten to POSIX
  by ``_process_unquoted`` and by the scanner's Windows-path rule
  (``./tools/build.bat``) — right for bash, whose quote removal would eat the
  backslashes, but wrong for cmd.exe, which needs them.

Rule
----
A command word naming ``cmd``/``cmd.exe`` (path stem, ``.exe`` optional,
case-insensitive) followed by the run switch ``/c`` or ``/k`` (also the
``//c``/``//k`` and ``-c``/``-k`` workaround spellings) starts a cmd.exe
command STRING: the words after that switch are cmd syntax, not bash words.
Inside that string a word is rewritten to the QUOTED backslash spelling
(``tools/build.bat`` -> ``"tools\build.bat"``) only when cmd.exe has to
resolve it as a path:

* the FIRST word of the command string (cmd's program token), and
* an explicit relative path (``./x``, ``../x``), and
* a word whose last segment carries an executable/script extension
  (``.bat``, ``.cmd``, ``.exe``, ``.com``, ``.ps1``, ``.vbs``, ...), and
* an operand of a cmd builtin that consumes a path (``call``, ``cd``, ``dir``,
  ``type``, ``del``, ``copy``, ``move``, ``ren``, ``md``, ...).  The operand
  COUNT is per builtin (``copy``/``move``/``ren``/``xcopy``/``robocopy``/
  ``mklink`` take two), and switches may sit between the builtin and its
  operands.  ``start`` is excluded on purpose: cmd reads its FIRST quoted
  operand as the window TITLE, so quoting a path there would silently run
  nothing, and its operands therefore keep their spelling.

Everything else stays byte-for-byte:

* switches (``/s``, ``/q``, ``/e:on``) and any word starting with ``/`` — an
  ABSOLUTE path is rewritten by the Windows-path rules to its drive form
  (``C:/x``), which cmd.exe resolves fine, so nothing is left to do here;
* words that already carry a backslash (Windows spelling preserved), a
  ``%VAR%``/``!VAR!``/``$var`` expansion or a glob character (the value is only
  known at runtime, so a splice would change it);
* single-quoted words (cmd.exe has no single-quote quoting: the quotes reach it
  as literal characters, so the word cannot be repaired without re-quoting);
* every operand of ``start`` (title hazard, see above) and the operands of a
  program that is not a cmd builtin (``cmd /c echo a/b`` stays as written);
* a quoted command STRING (``cmd /c "tools\build.bat dxc"``) — that is already
  cmd syntax and is passed through untouched.  A double-quoted SINGLE-word
  operand (``cmd /c "./tools/build.bat"``) is rewritten inside its quotes,
  because double quotes are also cmd's quoting.

The rewrite is emitted inside DOUBLE quotes, never bare: an unquoted backslash
is an escape character to bash, so ``cmd /c sub\t.bat dxc`` reaches cmd.exe as
``subt.bat`` and still fails.  ``"sub\t.bat"`` is one word for bash (the quotes
are removed, the backslashes survive) and one quoted token for cmd.exe — the
only spelling both shells agree on.

Conservative pass-through: when nothing qualifies, :func:`cmd_script_path_fix`
returns ``None`` and the caller keeps the original string object, exactly like
every other rule in this package.

Integration
-----------
The rule runs as a post-pass over the shared
:class:`~kimix_native.shell_compat.bash_core.BashFix` value — the orchestration
lives in :func:`kimix_native.shell_compat.post_passes.windows_post_passes`, which
the ``_shell_compat`` facade calls AFTER either the compiled
``runtime_py.bash_fix`` kernel or the pure-Python reference scanner has run and
after the doubled-slash flag repair (so ``cmd //c`` has already become
``cmd /c``).  The compiled kernel in kimix-base cannot express new rules from
this repository, so the facade is the single integration point that keeps the
native and pure-Python paths byte-equal by construction — the same contract
:mod:`kimix_native.shell_compat.native_flags` follows.

The IR traversal mirrors ``native_flags._walk``: command contexts are visited
in source order, ``$(...)``/backquote/process-substitution child scripts are
entered (their spans index into the SAME source string), and quoted embedded
scripts and heredoc bodies are never entered (a different source string / pure
data).

One tolerant-parser quirk is handled explicitly: a ``case`` pattern's ``|``
leaks pipeline state, so a simple command that follows a ``case``/``esac``
construct is tiled as a ``PIPELINE``/``COMMAND_LIST`` of bare ``WORD`` nodes —
and the fallback prelude of every command that needed one contains a ``case``.
Those words are regrouped into command regions at the operator gaps between
their spans (the parser drops the operator from the child list), so
``rev C:/a && cmd /c z/w.bat`` is repaired inside its prelude-wrapped form too.
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

try:  # pragma: no cover - both spellings are stdlib-compatible
    import regex as re
except ImportError:  # pragma: no cover
    import re  # stdlib: the used patterns are all stdlib-compatible

#: Reserved words the tolerant parser tiles as a COMMAND word wrapping the real
#: command in the FIRST argument (``if cmd /c x.bat; then ...``), plus the
#: command-word -> tool-stem normalizer: shared with
#: :mod:`kimix_native.shell_compat.native_flags` (one table, one meaning).  That
#: module imports nothing from this package at load time, so the import is safe.
from kimix_native.shell_compat.native_flags import (
    _CONDITION_KEYWORDS,
    _tool_name,
)

#: ``(start, end, replacement)`` splice in ORIGINAL-source coordinates.
Edit = Tuple[int, int, str]

#: Cheap pre-gate: a ``cmd``/``cmd.exe`` command word.  The tolerant parser is
#: only run when this matches (the walk then decides precisely); it is
#: deliberately looser than the parser's word boundaries so quoted and full-path
#: spellings still reach the walk.
_CMD_GATE_RE = re.compile(r"(?i)(?:^|[\s;&|(){}\"'=])cmd(?:\.exe)?(?=$|[\s;&|(){}\"'])")

#: Recursion bound for the IR walk (pathological substitution nesting).
_MAX_WALK_DEPTH = 16

#: The switch that starts the cmd.exe command string: ``/c`` and ``/k`` (plus
#: the doubled-slash and dash workaround spellings).  ``/d``/``/q``/``/s``/
#: ``/e:on``/``/v:on``/``/a``/``/u``/``/f:on`` are ordinary switches that may
#: precede it.
_RUN_SWITCH_CORES = frozenset({"c", "k"})

#: Extensions that make a relative word cmd's program/batch target.
_EXECUTABLE_EXTENSIONS = (
    ".bat",
    ".cmd",
    ".exe",
    ".com",
    ".ps1",
    ".vbs",
    ".vbe",
    ".wsf",
    ".msi",
    ".lnk",
    ".pif",
)

#: cmd builtins whose operand is a filesystem path, with how many of the
#: following NON-SWITCH operands are paths: ``cmd /c call tools/run.cmd``,
#: ``cmd /c cd src/gen``, ``cmd /c copy /b a/b.txt c/d.txt``.  The operands of
#: any other builtin or program are data and keep their spelling.
_PATH_BUILTIN_OPERANDS = {
    "attrib": 1,
    "call": 1,
    "cd": 1,
    "certutil": 1,
    "chdir": 1,
    "compact": 1,
    "copy": 2,
    "del": 1,
    "dir": 1,
    "erase": 1,
    "findstr": 1,
    "icacls": 1,
    "md": 1,
    "mkdir": 1,
    "mklink": 2,
    "move": 2,
    "popd": 1,
    "pushd": 1,
    "rd": 1,
    "ren": 2,
    "rename": 2,
    "rmdir": 1,
    "robocopy": 2,
    "type": 1,
    "where": 1,
    "xcopy": 2,
}

#: ``start`` is deliberately NOT a path builtin: cmd treats its FIRST quoted
#: operand as the window TITLE, so quoting a rewritten path there would turn
#: ``start tools/run.exe`` into a title-only ``start "tools\\run.exe"`` and
#: silently run nothing.  Its operands are left byte-for-byte instead.
_QUOTE_UNSAFE_BUILTINS = frozenset({"start"})

#: A word that cannot be rewritten from a static splice: runtime expansions,
#: glob metacharacters and an already-Windows spelling stay untouched.
_UNSAFE_CHARS = frozenset("\\$`%!*?[]{}~\"'")

#: A gap between two words of a degraded tiling that still holds a command
#: separator: the tolerant parser drops the operator from the child list, so the
#: gap is where a new command region starts.
_REGION_BREAK_RE = re.compile(r"[\n;&|<>()]")

#: Drive-absolute spelling (``C:/x``): cmd.exe resolves forward slashes in a
#: drive-qualified path, so neither spelling needs a rewrite here.
_DRIVE_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[/\\]")

__all__ = [
    "cmd_script_path_edits",
    "cmd_script_path_fix",
    "path_changes_after_cmd_repair",
]


def _is_run_switch(text: Optional[str]) -> bool:
    """True when *text* is cmd's ``/c``/``/k`` run switch (any spelling)."""
    if not text or text[0] not in "-/":
        return False
    core = text.lstrip("-/")
    return len(core) == 1 and core.lower() in _RUN_SWITCH_CORES


def _is_switch(text: str) -> bool:
    """True when *text* is a cmd switch (``/d``, ``/q``, ``/e:on``, ``-s``)."""
    if not text or text[0] not in "-/":
        return False
    core = text.lstrip("-/")
    if not core:
        return False
    # ``/x`` / ``/x:value`` / ``/x,y``: switches never contain a path separator.
    if "/" in core or "\\" in core:
        return False
    return core[0].isalnum()


def _command_string_start(arguments: object, source: str) -> Optional[int]:
    """Index of the first command-string word, or ``None``.

    Walks the argument words until the ``/c``/``/k`` run switch is found; any
    non-switch word before it means this is not a run form (``cmd /?``,
    ``cmd /d``), so the command is left alone.
    """
    for index, arg in enumerate(arguments):
        raw = source[arg.span.start : arg.span.end]
        value = arg.literal_value if arg.literal_value is not None else raw
        if _is_run_switch(value):
            return index + 1
        if not _is_switch(value):
            return None
    return None


def _split_quoted(raw: str) -> Optional[str]:
    """Return the inner text of a double-quoted single word, else ``None``.

    A double-quoted word is the only quoting cmd.exe shares with bash, so a
    path inside it may be rewritten and re-emitted with the same quotes.  The
    inner text must be one whitespace-free token (a quoted command STRING keeps
    its bytes) and free of quote characters.
    """
    if len(raw) >= 3 and raw[0] == '"' and raw[-1] == '"':
        inner = raw[1:-1]
        if '"' not in inner and not any(ch.isspace() for ch in inner):
            return inner
    return None


def _has_executable_extension(candidate: str) -> bool:
    """True when *candidate*'s last path segment carries a program extension."""
    tail = candidate.rsplit("/", 1)[-1].lower()
    return any(tail.endswith(ext) for ext in _EXECUTABLE_EXTENSIONS)


def _needs_backslashes(
    candidate: str,
    *,
    is_program: bool,
    path_operand: bool,
) -> bool:
    """True when cmd.exe must resolve *candidate* (a POSIX-ish relative word).

    *candidate* is quote-free, expansion-free, backslash-free and contains at
    least one ``/``.  ``is_program`` marks cmd's own program token (the first
    word of the command string) and ``path_operand`` an operand of a path-taking
    cmd builtin.
    """
    if is_program:
        # cmd's program token: ``cmd /c tools/build.bat`` names one program.
        return True
    if candidate.startswith(("./", "../")):
        # An explicit relative path is never anything but a path.
        return True
    if _has_executable_extension(candidate):
        return True
    # ``cmd /c call tools/build.bat`` / ``cmd /c cd src/gen``.
    return path_operand


def _rewrite_word(
    raw: str,
    literal: Optional[str],
    *,
    is_program: bool,
    path_operand: bool,
) -> Optional[str]:
    """Return the quoted backslash spelling of *raw*, or ``None`` to leave it.

    The result is re-emitted inside DOUBLE quotes on purpose: an unquoted
    backslash is an escape character to bash, so ``cmd /c sub\\t.bat dxc``
    reaches cmd.exe as ``subt.bat`` and still fails.  ``"sub\\t.bat"`` is one
    word for bash (the quotes are removed, the backslashes survive) and one
    quoted token for cmd.exe — the only spelling both shells agree on.
    """
    if not raw or "/" not in raw or raw[0] in "-/":
        # Switches and absolute paths: the Windows-path rules own the latter
        # (``/c/x`` -> ``C:/x``, which cmd.exe resolves), the former must not
        # be touched.
        return None
    inner = _split_quoted(raw)
    if inner is not None:
        # Already double-quoted by the author: rewrite inside the quotes.
        if literal is None or literal != inner:
            return None
        converted = _rewrite_bare(
            inner, is_program=is_program, path_operand=path_operand
        )
        return None if converted is None else '"' + converted + '"'
    if raw[0] in "'" or literal is None or literal != raw:
        # Single-quoted words: cmd.exe has no single-quote quoting, so the
        # quotes would reach it as literal characters and the word cannot be
        # repaired without changing what cmd receives.  ``literal != raw``
        # means quoting/escaping/expansion that must not be re-emitted.
        return None
    converted = _rewrite_bare(raw, is_program=is_program, path_operand=path_operand)
    return None if converted is None else '"' + converted + '"'


def _rewrite_bare(
    candidate: str,
    *,
    is_program: bool,
    path_operand: bool,
) -> Optional[str]:
    """Backslash spelling of an unquoted, expansion-free *candidate*."""
    if "/" not in candidate or any(ch in _UNSAFE_CHARS for ch in candidate):
        return None
    if _DRIVE_ABSOLUTE_RE.match(candidate) is not None:
        # ``cmd /c C:/dev/x.bat``: verified to run (cmd resolves forward
        # slashes in a drive-qualified path), so keep the author's spelling.
        return None
    if not _needs_backslashes(
        candidate, is_program=is_program, path_operand=path_operand
    ):
        return None
    if not candidate.strip("/"):
        return None
    return candidate.replace("/", "\\")


def _walk_command_string(words: object, source: str, edits: List[Edit]) -> None:
    """Apply the rule to the words of one cmd.exe command string.

    *words* are the IR words AFTER the ``/c``/``/k`` switch, in source order.
    """
    # Words left in the operand list of a path-taking cmd builtin (``copy a b``)
    # and the "this word follows ``start``" guard (its first quoted operand is a
    # window TITLE, so quoting a path there would change what runs).
    operands_left = 0
    after_start = False
    for position, word in enumerate(words):
        raw = source[word.span.start : word.span.end]
        literal = word.literal_value
        value = literal if literal is not None else raw
        lowered = value.lower()
        if _is_switch(value):
            # Switches may sit between a builtin and its operands
            # (``copy /b a b``): they neither consume nor cancel the operand
            # list.
            continue
        if after_start:
            # ``start`` operand: never re-quoted (see _QUOTE_UNSAFE_BUILTINS).
            after_start = False
            continue
        path_operand = operands_left > 0
        if operands_left > 0:
            operands_left -= 1
        elif lowered in _QUOTE_UNSAFE_BUILTINS:
            after_start = True
        elif lowered in _PATH_BUILTIN_OPERANDS:
            operands_left = _PATH_BUILTIN_OPERANDS[lowered]
        replacement = _rewrite_word(
            raw,
            literal,
            is_program=position == 0,
            path_operand=path_operand,
        )
        if replacement is not None:
            edits.append((word.span.start, word.span.end, replacement))


def _command_edits(
    command_word: object,
    arguments: object,
    source: str,
    edits: List[Edit],
) -> None:
    """Emit the path splices of one cmd.exe command node."""
    raw = source[command_word.span.start : command_word.span.end]
    name = _tool_name(raw, command_word.literal_value)
    if name != "cmd":
        return
    start = _command_string_start(arguments, source)
    if start is None:
        return
    _walk_command_string(list(arguments)[start:], source, edits)


def _handle_command(command: object, source: str, edits: List[Edit]) -> None:
    """Apply the rule to one COMMAND node (plain or keyword-wrapped shape)."""
    word = getattr(command, "command_word", None)
    if word is None:
        return
    _command_edits(word, command.arguments, source, edits)
    raw = source[word.span.start : word.span.end]
    name = _tool_name(raw, word.literal_value)
    if name in _CONDITION_KEYWORDS and command.arguments:
        # ``if cmd /c x.bat; then ...`` is tiled as word ``if`` + arguments,
        # the first of which is the real command word.
        first = command.arguments[0]
        _command_edits(first, command.arguments[1:], source, edits)


def _degraded_words(node: object) -> Optional[List[object]]:
    """Bare WORD children of a degraded command container, else ``None``.

    A ``case`` pattern's ``|`` leaks parser pipeline state, so a following simple
    command is tiled as a ``PIPELINE``/``COMMAND_LIST`` of bare ``WORD`` nodes
    instead of ``COMMAND`` nodes (see ``native_flags._walk`` for the same
    quirk).  The operator between two such commands is dropped from the child
    list, which is why the caller splits the words on the SOURCE GAPS.
    """
    from kimix_native.shell_compat.models import NodeKind

    # ``PIPELINE`` stores its children in ``commands``, ``COMMAND_LIST`` in
    # ``children`` — a degraded tiling can appear in either shape.
    children = list(getattr(node, "children", ()) or ())
    words = children if children else list(getattr(node, "commands", ()) or ())
    if len(words) < 2:
        return None
    if any(getattr(word, "kind", None) != NodeKind.WORD for word in words):
        return None
    return words


def _degraded_regions(words: List[object], source: str) -> List[List[object]]:
    """Split degraded container *words* at the operator gaps between them."""
    regions: List[List[object]] = []
    current: List[object] = [words[0]]
    for previous, word in zip(words, words[1:]):
        gap = source[previous.span.end : word.span.start]
        if _REGION_BREAK_RE.search(gap) is not None:
            regions.append(current)
            current = []
        current.append(word)
    regions.append(current)
    return regions


def _walk_degraded(words: List[object], source: str, edits: List[Edit]) -> None:
    """Apply the rule to a degraded (bare-WORD) command container."""
    for region in _degraded_regions(words, source):
        if not region:
            continue
        command_word, arguments = region[0], region[1:]
        raw = source[command_word.span.start : command_word.span.end]
        name = _tool_name(raw, command_word.literal_value)
        if name != "cmd":
            continue
        start = _command_string_start(arguments, source)
        if start is not None:
            _walk_command_string(arguments[start:], source, edits)


def _walk(node: object, source: str, edits: List[Edit], depth: int) -> None:
    """Collect cmd.exe path splices from *node*'s command contexts.

    Mirrors ``native_flags._walk``: COMMAND nodes are handled, ``EXPANSION``
    child scripts are entered (same source string), embedded quoted scripts /
    heredoc bodies / raw regions are data or a different source and are never
    entered.  A container tiled as bare ``WORD`` children (the ``case``-pattern
    quirk) is regrouped by :func:`_walk_degraded` instead of being skipped.
    """
    if depth > _MAX_WALK_DEPTH:
        return
    from kimix_native.shell_compat.models import NodeKind

    kind = node.kind
    if kind == NodeKind.COMMAND:
        _handle_command(node, source, edits)
        for expansion in getattr(node, "expansions", ()) or ():
            _walk(expansion, source, edits, depth + 1)
        return
    if kind in (NodeKind.PIPELINE, NodeKind.COMMAND_LIST):
        words = _degraded_words(node)
        if words is not None:
            _walk_degraded(words, source, edits)
            return
    if kind == NodeKind.EXPANSION:
        child = getattr(node, "child", None)
        if child is not None:
            _walk(child, source, edits, depth + 1)
        return
    if kind in (NodeKind.EMBEDDED, NodeKind.HEREDOC, NodeKind.RAW):
        # Embedded quoted scripts index a DIFFERENT (standalone) source string;
        # heredoc bodies and raw regions are data, not commands.
        return
    for attr in ("children", "commands"):
        for child in getattr(node, attr, ()) or ():
            _walk(child, source, edits, depth + 1)
    if kind == NodeKind.COMPOUND:
        for _part_name, part in getattr(node, "parts", ()) or ():
            _walk(part, source, edits, depth + 1)


def cmd_script_path_edits(source: str) -> List[Edit]:
    """Return ``(start, end, replacement)`` splices for cmd.exe path words.

    Edits are returned in source order and never overlap (word spans are
    disjoint).  An empty list means the source needs no repair — the caller
    must then keep the original string object.
    """
    if not source or "cmd" not in source.lower():
        return []
    if _CMD_GATE_RE.search(source) is None:
        return []
    try:
        from kimix_native.shell_compat.parser import parse
    except ImportError:  # pragma: no cover - package half-installed
        return []
    script = parse(source)
    edits: List[Edit] = []
    _walk(script, source, edits, 0)
    return edits


def cmd_script_path_fix(source: str) -> Optional[Tuple[str, Tuple[str, ...]]]:
    """Apply :func:`cmd_script_path_edits` to *source*.

    Returns ``(fixed_source, original_words)`` when at least one cmd.exe path
    word was rewritten to backslash form, else ``None`` (the caller then keeps
    the original string object, preserving the byte-for-byte pass-through
    contract).
    """
    edits = cmd_script_path_edits(source)
    if not edits:
        return None
    originals = tuple(source[start:end] for start, end, _repl in edits)
    fixed = source
    # Word spans are disjoint, so reverse-order splicing keeps earlier offsets
    # valid while replacing each span in place.
    for start, end, replacement in sorted(edits, reverse=True):
        fixed = fixed[:start] + replacement + fixed[end:]
    return fixed, originals


def path_changes_after_cmd_repair(
    path_changes: Sequence[str],
    cmd_words: Sequence[str],
) -> Tuple[str, ...]:
    """Return *path_changes* minus the notes this repair made false.

    ``_process_unquoted`` and the scanner rewrite a Windows-style relative path
    (``.\\tools\\build.bat``) to its POSIX spelling, recording a "backslashes
    to forward slashes" note; the cmd.exe command string rule then converts the
    word BACK to the (quoted) backslash spelling, so that note no longer
    describes the command.  The word is still reported in ``cmd_path_fixes``
    (quoting it is a real, necessary change — bash would otherwise eat the
    backslashes), only the false path note is dropped.

    A note is matched separator-insensitively and case-insensitively (Windows
    paths are case-insensitive).
    """
    changes = tuple(path_changes)
    words = tuple(cmd_words)
    if not changes or not words:
        return changes
    reverted = {word.replace("/", "\\").casefold() for word in words}
    return tuple(
        word for word in changes if word.replace("/", "\\").casefold() not in reverted
    )
