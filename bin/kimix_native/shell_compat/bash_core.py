"""Bash/Git Bash compatibility scanner: the ``BashFix`` result type, the
``_BashFixScanner`` engine reader and the ``fix_bash_command`` entry point, plus
the shared Bash constants and quote/substitution boundary scanners
(``_find_ansi_c_end`` / ``_find_backtick_end`` / ``_find_matching_paren`` /
``_find_dq_end`` and the ``_match_*`` family).

Part of the lossless tolerant-IR ``kimix_native.shell_compat`` implementation
package behind the stable ``_shell_compat`` facade.  This module owns the SCAN
FLOW only (Option A): every RULE decision is delegated to its own module —
paths (path / ``nul`` / ``cd /d``), wrappers (classification / fallback /
wrapper operand / reparse / unwrap), cmd_translator (cmd.exe ``@`` / ``if`` /
``for`` / jumps), heredoc (trailing-operator repair) and translator (the
``translate`` orchestration, the ``EditPlan`` render and the fallback prefix).
Already Git Bash-compatible input is returned byte-for-byte; only verified
compatibility cases produce an edit or a runtime-guarded fallback.

The ``_process_unquoted`` emitter moved to
``kimix_native.shell_compat.unquoted`` (Phase 3); this module keeps the
``_process_unquoted`` name as a thin delegate so intra-module callers and
the facade resolve unchanged.

Moved byte-for-byte from ``bin/kimix_native/_shell_compat.py`` (the
bash_fix.py scanner section and the bash_tool.py _process_unquoted
section, former lines 763-5878, minus the fallback data relocated to
kimix_native.shell_compat.fallback_data).  ``bin/kimix_native/_shell_compat.py``
is the stable compatibility FACADE over this package: it holds no scanner
implementation itself, it re-exports this surface.
"""

from __future__ import annotations

# ======================================================================
# bash_fix.py (kimi-agent) - Windows Git Bash compatibility scanner
# ======================================================================

import sys
import tempfile
from dataclasses import dataclass

try:
    import regex as re
except ImportError:  # pragma: no cover
    import re  # stdlib: the used patterns are all stdlib-compatible

# Runtime fallback data + helpers (defined in fallback_data.py).  These are
# read inside the scanner methods, so the import is needed for this module to
# work.  In the original single-file implementation both sections shared one
# module namespace; after the extraction the facade re-exports both namespaces
# into one module object, so late-bound lookups stay compatible with it.
from kimix_native.shell_compat.fallback_data import (
    _FALLBACKS,
    _FALLBACK_BODIES,
    _FALLBACK_COMMAND_WRAPPERS,
    _UNSUPPORTED_BODIES,
    _single_quote,
    _wrapper_runner,
)


_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\+)?=")
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Glued cmd label line (``:label`` as a whole command word).  ``:`` alone and
# ``: label`` (space) are valid bash no-ops and never match this.
_GLUED_LABEL_RE = re.compile(r"^:[A-Za-z_][A-Za-z0-9_]*$")
# cmd batch subroutine / child-script targets recognised after ``call``.
_CALL_BATCH_RE = re.compile(r"\.(?:bat|cmd)$", re.IGNORECASE)
# cmd ``for`` loop token: ``%name`` (interactive) or ``%%name`` (batch).
_FOR_LOOP_TOKEN_RE = re.compile(r"^(%%?)([A-Za-z_][A-Za-z0-9_]*)$")

_COMMAND_START_KEYWORDS = frozenset(
    {"!", "{", "if", "then", "elif", "else", "while", "until", "do"}
)
_COMMAND_END_KEYWORDS = frozenset({"fi", "done", "esac"})
_LIST_KEYWORDS = frozenset({"for", "select", "case"})
_SHELL_KEYWORDS = frozenset(
    {
        "!", "{", "}", "if", "then", "elif", "else", "while", "until",
        "do", "fi", "done", "esac", "for", "select", "case", "time",
        "coproc", "in", "function",
    }
)

_COMMAND_WRAPPERS = frozenset(
    {"builtin", "command", "coproc", "env", "exec", "nohup", "sudo", "time",
     "timeout", "stdbuf", "nice", "xargs"}
)

# Fallback names that behave like command wrappers too: the table itself
# (``_FALLBACK_COMMAND_WRAPPERS``, moved to fallback_data.py because it maps
# fallback names to wrapper kinds) is imported above.  ``gtimeout`` runs
# ``timeout "$@"`` (an executable that execs argv, so its command operand
# needs the standalone runner), while ``watch`` runs its command inside the
# same shell (its body uses ``eval "$*"``, so a same-shell function call
# suffices).  ``sudo`` maps to its own wrapper semantics (option table and
# executable operand); the fallback definition only engages on hosts
# without ``sudo.exe`` (pre-Windows 11), where it elevates through a UAC
# prompt.


# Wrappers that require a fixed number of plain operands (options excluded)
# before the command word.  GNU ``timeout`` takes exactly one DURATION operand
# (``timeout 5 rev``, ``timeout -s KILL 5s rev``) before COMMAND, so the
# duration is consumed without being mistaken for the wrapped command.
_WRAPPER_OPERAND_COUNTS = {"timeout": 1}

# Wrapper kinds whose command operand runs inside the current shell rather
# than being exec'd as a new process: the fallback function defined by the
# prefix is directly callable, so the source word is kept (no standalone
# runner rewrite is needed or possible).
_SAME_SHELL_WRAPPERS = frozenset({"builtin", "coproc", "time", "watch"})
# Builtins whose *operand words* are re-parsed as shell code at runtime, so a
# quoted operand is an inline script: ``eval`` concatenates and re-parses all
# of its operands, ``trap`` stores its first operand and re-parses it when the
# signal fires (the rest are signal specs — data).  Value -1 means "every
# operand"; a positive value counts the leading operands that are scripts.
# Scanning the operand's literal value as its own command context means the
# null-device rewrite (``> nul``) and the other compatibility fixes reach the
# code that actually runs.
_REPARSE_BUILTINS = {"eval": -1, "trap": 1}
# Command words that pass part of their argv to a *child process* for
# execution (``find -exec``, ``xargs``), or that re-parse their operands
# (``eval``).  Only for these is an argument-position shell word
# (``... sh -c '<script>'``) a real inline script whose redirections run; for
# any other command (``echo sh -c 'x > nul'``) the words are data and must
# stay untouched.
_EXEC_OPERAND_COMMANDS = frozenset({"find", "xargs", "eval", "ssh", "watch"})
# Commands whose leading operand is PATTERN/PROGRAM DATA, not a path: an
# unquoted drive-prefixed pattern (``grep -F C:\Users file``) has the bash
# value ``C:Users`` and genuinely matches; rewriting it "as a path" to
# ``C:/Users`` silently breaks the match.  The first bare operand (and the
# value of pattern-taking options like ``grep -e``) therefore suppresses the
# Windows-path rewrite; file-taking options (``grep -f``) and later operands
# keep it.
_PATTERN_PROGRAM_COMMANDS = frozenset(
    {"grep", "fgrep", "egrep", "rg", "sed", "awk", "gawk", "mawk"}
)
_GREP_PATTERN_FAMILY = frozenset({"grep", "fgrep", "egrep", "rg"})
# grep-family options whose value word is an arbitrary string (context
# lengths, device actions, ...): neither pattern nor path, but the value
# word must not be counted as the pattern operand.
_GREP_VALUE_OPTIONS = frozenset(
    {
        "-A",
        "-B",
        "-C",
        "-m",
        "-d",
        "-D",
        "--after-context",
        "--before-context",
        "--context",
        "--max-count",
        "--directories",
        "--devices",
    }
)

# Shell executables that agents frequently put in front of a command the Bash
# tool is already going to run (``bash cd /c/dev/x && ...``).  Git Bash can
# stand in for these POSIX-family shells, so a redundant leading invocation is
# unwrapped instead of letting bash try to open ``cd`` as a script file.  Only
# shells whose syntax is a subset of bash are listed; ``zsh``/``ksh``/``csh``
# scripts may use incompatible syntax and are deliberately left untouched.
_SHELL_WRAPPERS = frozenset({"bash", "sh", "dash", "ash"})

# Short-option clusters of the shell wrappers that take the next argument as
# an inline command string.  ``bash -c '...'`` is the canonical form;
# ``-lc``/``-cl`` add the (harmless here) login flag.  Clusters such as
# ``-ec``/``-ce``/``-xc`` that change errexit/xtrace semantics are left for
# bash to handle so unwrapping cannot alter the script's meaning.
_SHELL_C_OPTIONS = re.compile(r"^-c$|^-lc$|^-cl$")
# Option clusters whose *last* flag is ``c`` (``-c``, ``-lc``, ``-ec``,
# ``-ilc``): the next word is the shell's inline script.
_SHELL_C_SCRIPT_OPTION_RE = re.compile(r"^-[A-Za-z]*c$")
# Shell options that consume the NEXT word as their value (``bash -o pipefail
# -c '...'``, ``sh --rcfile /dev/null -c '...'``): the value is a plain
# operand, not a script, so the scan must skip it instead of bailing out.
# ``+o``/``+O`` do not start with ``-`` and are listed explicitly.
_SHELL_VALUE_OPTIONS = frozenset(
    {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}
)
_WRAPPER_OPTIONS_WITH_VALUE = {
    "env": frozenset(
        {
            "-u",
            "--unset",
            "-C",
            "--chdir",
            "-S",
            "--split-string",
        }
    ),
    "exec": frozenset({"-a"}),
    "sudo": frozenset(
        {
            "-C",
            "--close-from",
            "-D",
            "--chdir",
            "-g",
            "--group",
            "-h",
            "--host",
            "-p",
            "--prompt",
            "-R",
            "--chroot",
            "-r",
            "--role",
            "-t",
            "--type",
            "-T",
            "--command-timeout",
            "-u",
            "--user",
        }
    ),
    "time": frozenset({"-f", "--format", "-o", "--output"}),
    "timeout": frozenset(
        {
            "-k",
            "--kill-after",
            "-s",
            "--signal",
        }
    ),
    "stdbuf": frozenset(
        {
            "-o",
            "-e",
            "-i",
            "--output",
            "--error",
            "--input",
        }
    ),
    "nice": frozenset({"-n", "--adjustment"}),
    "xargs": frozenset(
        {
            "-I",
            "-n",
            "-L",
            "-P",
            "-s",
            "-S",
            "-a",
            "-d",
            "-E",
            "--arg-file",
            "--max-args",
            "--max-chars",
            "--max-procs",
            "--max-lines",
            "--replace",
            "--eof",
            "--delimiter",
        }
    ),
    "watch": frozenset({"-n", "--interval"}),
}

# Wrapper options whose value is a filesystem path rather than a name or
# number.  Windows backslash spellings of these values are rewritten for Git
# Bash just like ordinary argument paths; every other option value stays
# opaque.
_WRAPPER_PATH_OPTIONS = {
    "env": frozenset({"-C", "--chdir"}),
    "sudo": frozenset({"-D", "--chdir"}),
    "time": frozenset({"-o", "--output"}),
    "xargs": frozenset({"-a", "--arg-file"}),
}
_WRAPPER_PATH_OPTION_LONG = frozenset({"--chdir", "--output", "--arg-file"})

_OPERATOR_CHARS = frozenset(";&|()<>\n")
_REDIRECTION_START = frozenset("<>")

# Characters that terminate an unquoted word: operators plus horizontal
# whitespace.  A single frozenset membership test is cheaper than two.
_WORD_END_CHARS = _OPERATOR_CHARS | frozenset(" \t\r")

# Character classes for the C-accelerated skip loops.  ``str.translate`` with
# ``None`` mappings deletes the listed characters, turning a scan for "any
# special char present?" into a single C-level pass; the compiled regexes
# bulk-skip runs of ordinary characters inside quote-aware loops.
_LITERAL_SPECIAL_RE = re.compile(r'[\\\'"$`*?[{~]')
"""Chars whose presence forces the full quote-removal walk."""
_DQ_SPECIAL_RE = re.compile(r'[\\"`$]')
"""Chars that are special inside bash double quotes."""
_WORD_SPECIAL_RE = re.compile(r'[;&|()<>\n \t\r#\\\'"`$]')
"""Chars that can end a word or open a quoted/expanded region in ``_read_word``."""
_EXPANSION_SPECIAL_RE = re.compile(r'[\\\'"`$]')
"""Chars that matter while scanning a data region for substitutions."""
_HEREDOC_EXPANSION_SPECIAL_RE = re.compile(r'[\\`$]')
"""Chars that matter inside an expanding heredoc body (quotes are literal)."""
_CONDITIONAL_SPECIAL_RE = re.compile(r'[\\\]\'"`$]')
"""Chars that matter inside a ``[[ ... ]]`` conditional."""
_PARAMETER_SPECIAL_RE = re.compile(r'[\\$\'"{}]')
"""Chars that matter inside a ``${...}`` parameter expansion."""
_PARAMETER_LITERAL_SPECIAL_RE = re.compile(r'[\\\'"{}]')
"""Chars that matter inside a literal ``${...}`` (no substitution scan)."""
_ARITHMETIC_SPECIAL_RE = re.compile(r'[\\()\'"$]')
"""Chars that matter inside a ``$((...))`` arithmetic expansion."""
_BACKTICK_SPECIAL_RE = re.compile(r'[\\`]')
"""Chars that matter while finding a backtick region's end."""
_HSPACE_RE = re.compile(r'[ \t\r]+')
"""Horizontal whitespace run (skipped in one C-level match)."""
_CONTROL_OP_STARTS = frozenset(";&|()")
"""Characters a bash control operator can start with."""
_NAME_START_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_"
)
"""Characters a POSIX shell name can start with."""
_REDIRECTION_RE = re.compile(r"&>>|&>|<<<|<<-|<<|>>|<>|>\||<&|>&|<|>")
"""One C-level match against the longest-first redirection operator table."""

_UNDECODED = object()
"""Sentinel: "the caller did not pre-decode this word's literal value"."""

# Nesting deeper than this is never scanned: ``_find_matching`` and
# ``_scan_range`` recurse once per ``$( ... )`` level, so pathological input
# (e.g. a pasted blob with hundreds of nested substitutions) would otherwise
# cost O(depth * length).  Real commands stay far below this bound; content
# beyond it is simply left byte-for-byte for Bash to handle.
_MAX_NESTING_DEPTH = 1024

# ── Windows path recognition ────────────────────────────────────────────────
# Rewrites apply only to unquoted words that unambiguously look like Windows
# paths; everything else (quotes, expansions, short ambiguous words) is left
# byte-for-byte for Bash to handle.

_PATH_DRIVE_RE = re.compile(r"[A-Za-z]:[\\/].*")
"""Drive-absolute path such as ``D:\\foo`` or the drive root ``C:\\``."""

_PATH_SEGMENT_RE = re.compile(r"[A-Za-z0-9_.~\\-]+")
"""Decoded value of a plausible multi-segment relative path (no spaces)."""

_PATH_SAFE_CHARS = frozenset(
 "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_./:~@%+=-#,[]*?{}"
)
"""Characters that may appear unquoted in a normalized path word.

Glob metacharacters are included on purpose so ``D:/x/*.txt`` still performs
pathname expansion instead of being quoted into a literal name; brace
metacharacters too so ``/tmp/{a,b}`` still performs brace expansion.
"""

_PATH_GLOB_CHARS = frozenset("*?[]{}")
"""Expansion metacharacters that must stay OUTSIDE any quoting the path
rewrite adds.

A glob or brace metacharacter wrapped inside double quotes becomes a
literal filename character, silently destroying the pathname/brace
expansion the original unquoted word performed (``D:/x/*.txt`` must keep
expanding).  Quoting therefore breaks its span at these characters and
emits them verbatim.
"""

_ESCAPED_LITERAL_CHARS = frozenset(" \t&;|()<>'\"$`{}!")
"""Chars whose backslash form is a pure Bash escape, not a path separator.

``\\ `` (escaped space) is how a space inside an unquoted word is written;
normalizing it to ``/ `` would invent a directory level that does not exist.
The backslash is dropped and the character kept inside its segment.
"""


def _windows_temp_dir() -> str:
    """Return the real Windows temp directory as a forward-slash path.

    Git Bash's ``/tmp`` maps to this directory (``cygpath -w /tmp`` reports
    the same value), but Git Bash's ``TMP``/``TEMP`` environment variables are
    MSYS-style ``/tmp`` inside the shell, so they cannot be used to translate
    ``/tmp`` paths for native Windows executables.  The parent (kimi-agent)
    process environment holds the Windows-style spelling, which is what
    ``tempfile.gettempdir()`` returns.  On POSIX hosts this returns ``/tmp``
    (the identity for the rewrite), which keeps the scanner's platform gate
    the single source of truth for when the rewrite applies.
    """
    return tempfile.gettempdir().replace("\\", "/")



# ``_windows_temp_dir`` is the one name the test suite monkeypatches through
# the ``kimix_native._shell_compat`` facade handle (it patched the single
# flat namespace of the historical single-file implementation).  Split into
# ``bash_core`` that late-bound lookup would otherwise bypass the patch, so the
# virtual-path rewrite reads the temp dir through the facade dict when the facade
# is loaded (honoring a monkeypatch), else this module's own definition.  The
# result is byte-for-byte identical when nothing is patched, because the facade
# and this module bind the SAME function object.  The contract is locked by
# ``test_facade_module_level_patch_reaches_scanner`` in
# ``tests/unit/tools/test_shell_compat_facade.py``.
def _windows_temp_dir_from_namespace() -> str:
    _facade = sys.modules.get("kimix_native._shell_compat")
    if _facade is not None:
        _fn = _facade.__dict__.get("_windows_temp_dir")
        if _fn is not None and _fn is not _windows_temp_dir:
            return _fn()
    return _windows_temp_dir()

_VAR_NAME_START_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_"
)
"""Characters that may start a ``$name`` variable expansion."""

_VAR_NAME_CHARS = _VAR_NAME_START_CHARS | frozenset("0123456789")
"""Characters that may continue a ``$name`` variable expansion."""

_SPECIAL_PARAM_CHARS = frozenset("0123456789?-$!#*@")
"""Single-character special parameters (``$$``, ``$?``, ``$@``, ...)."""


def _match_ansi_c_quote_span(text: str, i: int) -> int | None:
    """Return the index after the ``'`` closing a ``$'`` region opened before *i*.

    Returns None when the region is unterminated.
    """
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
        elif ch == "'":
            return i + 1
        else:
            i += 1
    return None


def _match_dquote_span(text: str, i: int) -> int | None:
    """Return the index after the ``"`` closing a double-quoted region opened before *i*.

    Returns None when the region is unterminated.
    """
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
        elif ch == '"':
            return i + 1
        else:
            i += 1
    return None


def _match_parameter_braces(text: str, i: int) -> int | None:
    """Return the index after the ``}`` closing ``${`` (opened at ``i - 2``).

    Returns None when the expansion is unterminated or embeds a command
    substitution (``$(...)`` or backquote): those inner command contexts are
    scanned separately, and rewriting a word that contains them would clash
    with the edits recorded there.
    """
    depth = 1
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
        elif ch == "'":
            j = text.find("'", i + 1)
            if j < 0:
                return None
            i = j + 1
        elif ch == '"':
            j = _match_dquote_span(text, i + 1)
            if j is None:
                return None
            i = j
        elif ch == "`" or (ch == "$" and i + 1 < n and text[i + 1] == "("):
            return None
        elif ch == "{":
            depth += 1
            i += 1
        elif ch == "}":
            depth -= 1
            i += 1
            if depth == 0:
                return i
        else:
            i += 1
    return None


def _match_arithmetic_span(text: str, i: int) -> int | None:
    """Return the index after the ``))`` closing ``$((`` opened at ``i - 3``.

    Returns None when the expansion is unterminated or embeds a command
    substitution (``$(...)``), whose inner command context is scanned
    separately.
    """
    depth = 1
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "$" and i + 1 < n and text[i + 1] == "(":
            return None
        if ch == "(" and text.startswith("((", i):
            depth += 1
            i += 2
        elif ch == ")" and text.startswith("))", i):
            depth -= 1
            i += 2
            if depth == 0:
                return i
        elif ch == "\\":
            i += 2
        elif ch == "'":
            j = text.find("'", i + 1)
            if j < 0:
                return None
            i = j + 1
        elif ch == '"':
            j = _match_dquote_span(text, i + 1)
            if j is None:
                return None
            i = j
        else:
            i += 1
    return None


def _decode_virtual_path_span(span: str) -> str | None:
    """Decode Bash unquoted-word backslash escapes in a literal path span.

    Phase 4: the canonical implementation moved to
    :func:`kimix_native.shell_compat.paths.decode_virtual_path_span`; this
    module keeps the historical name (re-exported by the ``_shell_compat``
    facade's 195-name ``__all__``) as a thin delegate.  The import is lazy so
    bash_core does not import paths at module load (paths imports bash_core).
    """
    from kimix_native.shell_compat.paths import decode_virtual_path_span

    return decode_virtual_path_span(span)


def _split_virtual_path_expansions(text: str) -> list[tuple[bool, str]] | None:
    """Split a rewritten virtual path into literal and expansion spans.

    Phase 4 delegate to
    :func:`kimix_native.shell_compat.paths.split_virtual_path_expansions`
    (historical facade name preserved; see that function for the contract).
    """
    from kimix_native.shell_compat.paths import split_virtual_path_expansions

    return split_virtual_path_expansions(text)


def _opaque_inline_expansions(text: str) -> tuple[str, list[str]]:
    """Copy inline-script text with every runtime expansion made opaque.

    Returns ``(sanitized, substitutions)``: *sanitized* parses as the same
    command structure (the same command words in the same positions) with
    each ``$...``/backquote expansion replaced by a placeholder word, so it
    can be scanned as a command context without executing or misreading the
    expansion values; *substitutions* holds the raw body of every ``$(...)``
    or backquote region (which run in the current shell at command time) for
    the caller to scan separately as its own command context.

    Single-quoted regions are copied verbatim (``$`` inside them is literal
    at runtime); escaped characters are kept verbatim; a ``$`` starting no
    expansion is a literal dollar sign.  Unterminated expansions are copied
    verbatim so the sanitized text stays a faithful prefix of the script.
    """
    placeholder = "__kimix_operand__"
    n = len(text)
    out: list[str] = []
    substitutions: list[str] = []
    i = 0
    in_squote = False
    in_dquote = False
    while i < n:
        ch = text[i]
        if in_squote:
            out.append(ch)
            if ch == "'":
                in_squote = False
            i += 1
            continue
        if ch == "'":
            in_squote = True
            out.append(ch)
            i += 1
            continue
        if ch == "\\":
            # An escaped char is literal script text; keep it verbatim.
            out.append(ch)
            if i + 1 < n:
                out.append(text[i + 1])
                i += 2
            else:
                i += 1
            continue
        if ch == '"':
            in_dquote = not in_dquote
            out.append(ch)
            i += 1
            continue
        if ch == "`":
            end = _find_backtick_end(text, i + 1)
            if end < 0:
                out.append(text[i:])
                break
            substitutions.append(text[i + 1 : end - 1])
            out.append(placeholder)
            i = end
            continue
        if ch != "$" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = text[i + 1]
        if nxt == "(":
            if text.startswith("$((", i):
                end = _match_arithmetic_span(text, i + 3)
                if end is None:
                    out.append(text[i:])
                    break
            else:
                close = _find_matching_paren(text, i + 1)
                if close < 0:
                    out.append(text[i:])
                    break
                substitutions.append(text[i + 2 : close])
                end = close + 1
            out.append(placeholder)
            i = end
            continue
        if nxt == "{":
            end = _match_parameter_braces(text, i + 2)
            if end is None:
                out.append(text[i:])
                break
            out.append(placeholder)
            i = end
            continue
        if nxt == "'":
            end = _find_ansi_c_end(text, i + 2)
            if end < 0:
                out.append(text[i:])
                break
            out.append(placeholder)
            i = end
            continue
        if nxt == '"':
            end = _find_dq_end(text, i + 2)
            if end < 0:
                out.append(text[i:])
                break
            out.append(placeholder)
            i = end
            continue
        if nxt in _VAR_NAME_START_CHARS:
            j = i + 2
            while j < n and text[j] in _VAR_NAME_CHARS:
                j += 1
            out.append(placeholder)
            i = j
            continue
        if nxt in _SPECIAL_PARAM_CHARS:
            out.append(placeholder)
            i += 2
            continue
        # A ``$`` starting no expansion is a literal dollar sign.
        out.append(ch)
        i += 1
    return "".join(out), substitutions


@dataclass(frozen=True)
class BashFix:
    """Result of :func:`fix_bash_command`.

    ``replacements`` records each original command name in source order and
    ``path_changes`` each original argument or command word whose
    Windows-style backslashes, Git Bash virtual absolute path (``/tmp/x``,
    ``/c/x``), or cmd.exe ``/d`` flag were rewritten for Git Bash.
    ``shell_wrappers`` records each redundant ``bash``/``sh``
    invocation that was unwrapped (``bash <cmd> ...`` or ``bash -c <script>``)
    so the command runs directly in the Bash tool.  ``nul_fixes`` records each
    redirection target whose literal value is the bare word ``nul``/``NUL``
    (plain, backslash-escaped ``\\nul``/``n\\ul``, ANSI-C ``$'nul'``, or
    empty-quote-concatenated ``nul''``) that was rewritten to ``/dev/null`` —
    including inside runtime-reparsed strings (``eval``/``trap`` operands,
    ``watch`` and nested ``sh -c`` scripts) — because Git Bash treats ``nul``
    as an ordinary filename, silently creating an empty ``nul`` file instead
    of discarding output.  A whole-token quoted ``'nul'``/``"nul"`` target
    stays untouched (documented intentional-filename policy).
    ``unsupported`` records each command name that has no faithful Windows
    Git Bash equivalent (see ``_UNSUPPORTED_BODIES`` for the reasons); the
    command text is left byte-for-byte unchanged for those.  Empty tuples
    Empty tuples
    mean the command was returned byte-for-byte unchanged.  ``cmd_conditional_fixes``
    records the original keyword region of each cmd.exe control-flow construct
    that was translated to a bash equivalent by the scanner (see
    ``_try_cmd_if`` / ``_try_cmd_for`` / ``_try_at_prefix``): ``if [not] exist
    P``/``if [not] defined V``/``if [not] errorlevel N``/``if [not] X==Y``
    (and the arithmetic ``equ``/``neq``/``lss``/``leq``/``gtr``/``geq``
    variants, with ``/i`` case-folding) become bash ``if [[ ... ]]``/``if ((
    ... ))`` tests, ``for [%|%]name in (list) do body`` becomes a bash
    ``for name in list; do body; done`` loop, and ``@echo off``/``@echo on``
    becomes ``set +x``/``set -x``.  Only fully recognised, single-line, horizontally
    separated forms are rewritten; anything else stays byte-for-byte (and
    untranslatable jumps -- ``goto``/``:label``/``call x.bat``/``exit /b`` --
    ride the ``unsupported`` refusal channel instead).
    """
    command: str
    replacements: tuple[str, ...] = ()
    path_changes: tuple[str, ...] = ()
    shell_wrappers: tuple[str, ...] = ()
    nul_fixes: tuple[str, ...] = ()
    unsupported: tuple[str, ...] = ()
    cmd_conditional_fixes: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        """Return whether any compatibility replacement was made."""
        return (
            bool(self.replacements)
            or bool(self.path_changes)
            or bool(self.shell_wrappers)
            or bool(self.nul_fixes)
            or bool(self.cmd_conditional_fixes)
        )

    @property
    def warning(self) -> str:
        """Return a concise description of compatibility changes."""
        parts: list[str] = []
        if self.unsupported:
            details = "; ".join(
                f"`{name}` — {_UNSUPPORTED_BODIES[name]}" for name in self.unsupported
            )
            parts.append(
                "Command(s) with no Windows Git Bash equivalent: "
                f"{details}."
            )
        if self.replacements:
            names = ", ".join(f"`{name}`" for name in self.replacements)
            parts.append(
                f"Added Windows Git Bash fallback(s) for native command(s): {names}."
            )
        if self.path_changes:
            words = ", ".join(f"`{word}`" for word in self.path_changes)
            parts.append(
                "Rewrote Windows path(s) for Git Bash (backslashes to forward "
                f"slashes; Git Bash virtual paths to native spellings): {words}."
            )
        if self.shell_wrappers:
            names = ", ".join(f"`{name}`" for name in self.shell_wrappers)
            parts.append(
                f"Removed redundant shell wrapper(s): {names}."
            )
        if self.nul_fixes:
            words = ", ".join(f"`{word}`" for word in self.nul_fixes)
            parts.append(
                "Rewrote null-device redirection target(s) for Git Bash: "
                f"{words} -> `/dev/null` (an unquoted `nul` would otherwise "
                "create an empty file named `nul`)."
            )
        if self.cmd_conditional_fixes:
            words = ", ".join(
                f"`{word}`" for word in self.cmd_conditional_fixes
            )
            parts.append(
                "Rewrote cmd.exe control-flow construct(s) to bash equivalents "
                "(`if exist`/`if defined`/`if errorlevel`/`if ...==...` tests, "
                "`for %v in (...) do` loops, `@echo off|on` -> `set +x|-x`): "
                f"{words}."
            )
        return " ".join(parts)


@dataclass
class _BashWrapper:
    kind: str
    skip_next: bool = False
    opaque: bool = False
    path_value: bool = False
    operands: int = 0


@dataclass
class _BashHereDoc:
    delimiter: str | None
    strip_tabs: bool
    expands: bool


@dataclass(frozen=True)
class _CmdIfSegment:
    """One validated cmd.exe ``if``/``else if`` header and its then-branch.

    Spans into the scanner source: ``[header_start, op_start)`` is the
    keyword region (``if [not] exist`` resp. ``else if [not] exist``) that
    the H/EL edit replaces with the bash test header, ``[op_start, op_end)``
    the path operand (kept, possibly rewritten by the path machinery), and
    ``(open_pos, close_pos)`` the parenthesised branch body (parens deleted,
    body scanned as a command context).
    """

    header_start: int
    negated: bool
    op_start: int
    op_end: int
    open_pos: int
    close_pos: int
    # cmd condition kind: ``exist`` (landed default), ``defined``,
    # ``errorlevel``, ``compare`` (glued ``==``), ``arithmetic`` (spaced
    # ``equ``/``neq``/``lss``/``leq``/``gtr``/``geq``).  ``exist`` segments
    # keep the historical positional construction (defaults below), so
    # existing callers and tests are unaffected.
    kind: str = "exist"
    folded: bool = False
    # ``compare``/``arithmetic``: the already-built bash condition text that
    # replaces the ``[op_start, op_end)`` region (``CMP`` edit).  ``None`` for
    # the kept-verbatim ``exist``/``defined``/``errorlevel`` operands.
    cmp_text: str | None = None


@dataclass(frozen=True)
class _CmdIfMatch:
    """A fully validated cmd.exe ``if exist`` conditional.

    ``segments`` holds the ``if`` header plus every ``else if`` (``elif``)
    chain segment; ``else_open``/``else_close`` bound the optional final
    ``else (`` branch (``None``/``None`` for the single-branch form).
    Produced by a read-only validation pass; committing one is guaranteed
    to succeed.
    """

    segments: tuple[_CmdIfSegment, ...]
    else_open: int | None
    else_close: int | None


@dataclass(frozen=True)
class _CmdForMatch:
    r"""A fully validated cmd.exe ``for [%|%]name in (list) do body`` loop.

    Spans into the scanner source.  ``[tok_start, tok_end)`` is the loop
    token (``%name`` or ``%%name``) rewritten to the bare ``name``; the
    ``(`` at ``open_pos`` and the ``)`` at ``close_pos`` bound the item list
    (commas become spaces, Windows-path items rewritten); ``do`` ends at
    ``do_end`` and the body is the rest of the physical line,
    ``[do_end, body_end)``.  ``var_edits`` are the ``(word_start,
    word_end)`` spans of every whole-word ``%name``/``%%name`` occurrence in
    the body (each rewritten to ``$name``); ``comma_spans`` the single
    characters to turn into spaces; ``list_path_edits`` the
    ``(start, end, replacement)`` Windows-path rewrites already validated on
    the list region.  Produced by a read-only pass; committing one is
    guaranteed to succeed and every edit pair is disjoint by construction
    (design ``.kimix_cache/cmd_control_flow_design.md` \u00a73.2`).
    """

    for_start: int
    name: str
    tok_start: int
    tok_end: int
    open_pos: int
    close_pos: int
    do_end: int
    body_start: int
    body_end: int
    comma_spans: tuple[tuple[int, int], ...]
    list_path_edits: tuple[tuple[int, int, str], ...]
    var_edits: tuple[tuple[int, int], ...]


_CMD_IF_TAIL_SAFE_CHARS = frozenset(" \t\r\n;&|()<>")
"""Characters that may follow a conditional's closing ``)`` when the no-else
form appends ``; fi``: control and redirection characters cannot glue onto
``fi``; any word character could (``fi`` + ``x`` becomes the command ``fix``)."""

# cmd relational *words* (spaced operands, the only form cmd accepts for
# them -- ``5geq3`` is invalid cmd) mapped to their ``(( ))`` C operators.
_CMD_REL_OPS = {
    "equ": "==", "neq": "!=", "lss": "<",
    "leq": "<=", "gtr": ">", "geq": ">=",
}
# ``if errorlevel`` takes a plain (non-negative) decimal integer.
_ERRORLEVEL_RE = re.compile(r"^[0-9]{1,9}$")
# arithmetic compare operands are optionally-signed decimal integers.
_INT_RE = re.compile(r"^-?[0-9]+$")


class _BashFixScanner:
    """Conservative scanner for Bash executable command positions."""

    __slots__ = ("s", "n", "edits", "names", "path_notes", "shell_notes", "heredoc_events", "nest_depth", "nul_fixes", "unsupported", "cmd_conditional_notes")

    def __init__(self, command: str) -> None:
        self.s = command
        self.n = len(command)
        self.edits: list[tuple[int, int, str]] = []
        self.names: list[str] = []
        self.path_notes: list[str] = []
        self.shell_notes: list[str] = []
        self.heredoc_events: list[tuple[int, int]] = []
        self.nest_depth = 0
        self.nul_fixes: list[str] = []
        self.unsupported: list[str] = []
        self.cmd_conditional_notes: list[str] = []

    def fix(self) -> BashFix:
        try:
            self._scan_range(0, self.n)
        except RecursionError:
            # Malformed or adversarial nesting must never make the Bash tool
            # fail before Bash itself can report the syntax error.
            return BashFix(self.s)
        if (
            not self.names
            and not self.edits
            and not self.shell_notes
            and not self.nul_fixes
            and not self.unsupported
            and not self.cmd_conditional_notes
        ):
            return BashFix(self.s)
        # Phase 5: the fallback prefix construction (dedup in discovery
        # order via ``dict.fromkeys``, one guarded definition + one
        # conditional ``export -f`` line per unique name, emitted ONLY when
        # at least one fallback was registered) lives in
        # ``kimix_native.shell_compat.wrappers.fallback_prefix`` (shared
        # with the IR translator; re-exported by
        # ``kimix_native.shell_compat.translator``).  The lazy import keeps
        # the load-time cycle (wrappers -> bash_core) broken, and the
        # function is byte-for-byte the construction below used to inline.
        from kimix_native.shell_compat.translator import fallback_prefix

        prefix = fallback_prefix(self.names)
        source = self._build_source()
        source = self._build_fixed_source(source)
        return BashFix(
            prefix + source,
            tuple(self.names),
            tuple(self.path_notes),
            tuple(self.shell_notes),
            tuple(self.nul_fixes),
            tuple(self.unsupported),
            tuple(self.cmd_conditional_notes),
        )

    def _build_fixed_source(self, source: str | None = None) -> str:
        """Return the built source with heredoc trailing operators repaired.

        When no edits were recorded the build returns ``self.s`` itself, so the
        heredoc event positions this scan collected are still valid and are
        reused instead of scanning the same string a second time (the common
        case: e.g. fallback-command names alone produce no source edits).
        """
        if source is None:
            source = self._build_source()
        if source is self.s:
            return _fix_heredoc_trailing_operators(source, self.heredoc_events)
        return _fix_heredoc_trailing_operators(source)

    def _build_source(self) -> str:
        """Return the source with all recorded edits applied.

        Positions recorded by an inner scanner (a nested ``bash -c``/``eval``
        script) belong to the *inner* string and must not be interpreted as
        offsets into ``self.s``; callers splice the inner result themselves.
        """
        if not self.edits:
            return self.s
        return self._apply_edits(self.s, self.edits)

    @staticmethod
    def _apply_edits(s: str, edits: list[tuple[int, int, str]]) -> str:
        """Apply ``(start, end, replacement)`` edits to *s*.

        Phase 5: the render lives in
        :func:`kimix_native.shell_compat.translator.render_splices` (the
        single §3.5 splice renderer, shared with the IR ``EditPlan``; the
        empty-plan identity contract it preserves is what the
        ``_build_fixed_source`` heredoc-event reuse below depends on).
        """
        from kimix_native.shell_compat.translator import render_splices

        return render_splices(s, edits)

    @staticmethod
    def _literal_word_value(raw: str) -> str | None:
        """Return the word value produced solely by Bash quote removal.

        Bash permits literal words such as ``'rev'``, ``\rev`` and ``r""ev``.
        Only words whose value can be determined without any expansion are
        accepted; parameter/command/arithmetic expansions, globbing, and
        malformed quotes return ``None`` so the caller leaves them untouched.
        """
        # Fast path: a word with no quoting/expansion characters evaluates to
        # itself (quote removal is the identity), so the walk below is skipped.
        search = _LITERAL_SPECIAL_RE.search
        m = search(raw)
        if m is None:
            return raw
        value: list[str] = []
        i = 0
        while i < len(raw):
            # Bulk-append the plain run before the next special character.
            m = search(raw, i)
            if m is None:
                value.append(raw[i:])
                break
            if m.start() > i:
                value.append(raw[i : m.start()])
                i = m.start()
            ch = raw[i]
            if ch == "\\":
                if i + 1 >= len(raw):
                    return None
                if raw[i + 1] == "\n":
                    i += 2
                    continue
                value.append(raw[i + 1])
                i += 2
                continue
            if ch == "'":
                close = raw.find("'", i + 1)
                if close < 0:
                    return None
                value.append(raw[i + 1 : close])
                i = close + 1
                continue
            if ch == '"':
                i += 1
                while i < len(raw) and raw[i] != '"':
                    inner = raw[i]
                    if inner in "$`":
                        return None  # runtime expansion: not a literal word
                    if inner == "\\" and i + 1 < len(raw):
                        escaped = raw[i + 1]
                        if escaped in '$`"\\\n':
                            if escaped != "\n":
                                value.append(escaped)
                            i += 2
                            continue
                    value.append(inner)
                    i += 1
                if i >= len(raw):
                    return None
                i += 1
                continue
            if ch == "$" and raw.startswith("$'", i):
                literal = _BashFixScanner._ansi_c_literal_span(raw, i)
                if literal is None:
                    return None
                text, i = literal
                value.append(text)
                continue
            if ch == "$" and raw.startswith('$"', i):
                # ``$"..."`` is locale translation: on Git Bash (no message
                # catalog) the value is the plain double-quoted body.
                close = i + 2
                while close < len(raw):
                    if raw[close] == "\\" and close + 1 < len(raw):
                        close += 2
                        continue
                    if raw[close] == '"':
                        break
                    close += 1
                if close >= len(raw):
                    return None
                inner_literal = _BashFixScanner._literal_word_value(
                    raw[i + 1 : close + 1]
                )
                if inner_literal is None:
                    return None
                value.append(inner_literal)
                i = close + 1
                continue
            if ch in "$`*?[{~":
                return None
            value.append(ch)
            i += 1
        return "".join(value)

    @staticmethod
    def _ansi_c_literal_span(raw: str, i: int) -> tuple[str, int] | None:
        """Decode the ``$'...'`` ANSI-C quote starting at *i* in *raw*.

        Returns ``(value, index_after_closing_quote)``, or ``None`` when the
        quote is unterminated or holds an unsupported escape (unicode
        ``\\u``/``\\U`` and multibyte octal sequences are rejected rather than
        approximated).  Supported escapes mirror bash: ``\\a \\b \\e \\E \\f
        \\n \\r \\t \\v \\\\ \\' \\" \\? \\c<char>`` plus octal ``\\[0-7]{1,3}``
        and hex ``\\x{1,2}`` for single-byte values.
        """
        if not raw.startswith("$'", i):
            return None
        body_start = i + 2
        j = body_start
        out: list[str] = []
        # Bash strings are C strings: the VALUE truncates at the first NUL
        # (``$'tree\0x'`` is the 4-char word ``tree``).  The scan still runs
        # to the closing quote so the returned index is correct, but nothing
        # after the NUL is part of the value.
        nul_seen = False
        while j < len(raw):
            ch = raw[j]
            if ch == "'":
                return "".join(out), j + 1
            if ch != "\\":
                if not nul_seen:
                    out.append(ch)
                j += 1
                continue
            j += 1
            if j >= len(raw):
                return None
            esc = raw[j]
            simple = {
                "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f",
                "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\",
                "'": "'", '"': '"', "?": "?",
            }
            if esc in simple:
                if not nul_seen:
                    out.append(simple[esc])
                j += 1
                continue
            if esc == "c":
                if j + 1 >= len(raw):
                    return None
                ctrl = raw[j + 1]
                code = ord(ctrl.upper()) & 0x1F
                if code == 0:
                    nul_seen = True
                elif not nul_seen:
                    out.append(chr(code))
                j += 2
                continue
            if esc in "01234567":
                digits = ""
                k = j
                while k < len(raw) and len(digits) < 3 and raw[k] in "01234567":
                    digits += raw[k]
                    k += 1
                code = int(digits, 8)
                if code > 0x7F:
                    return None  # multibyte octal: unsupported, be safe
                if code == 0:
                    nul_seen = True
                elif not nul_seen:
                    out.append(chr(code))
                j = k
                continue
            if esc == "x":
                digits = ""
                k = j + 1
                while (
                    k < len(raw)
                    and len(digits) < 2
                    and raw[k] in "0123456789abcdefABCDEF"
                ):
                    digits += raw[k]
                    k += 1
                if not digits:
                    return None
                code = int(digits, 16)
                if code > 0x7F:
                    return None
                if code == 0:
                    nul_seen = True
                elif not nul_seen:
                    out.append(chr(code))
                j = k
                continue
            if esc in "uU":
                return None  # unicode escapes: not worth approximating
            return None  # unknown escape: bash copies it, but stay conservative
        return None  # unterminated

    @classmethod
    def _is_effective_nul_target(cls, raw: str) -> bool:
        """Return True when redirection target word *raw* resolves to ``nul``.

        Phase 4: the predicate lives in
        :func:`kimix_native.shell_compat.paths.is_effective_nul_word`, which the
        null-device IR rule shares so the live scanner and the IR decision are
        ONE definition.  This classmethod is kept as the scanner-side name (and
        the historical method dispatch point) and delegates there.
        """
        from kimix_native.shell_compat.paths import is_effective_nul_word

        return is_effective_nul_word(raw)

    @staticmethod
    def _literal_command_name(raw: str) -> str | None:
        """Return the fallback command name produced by Bash quote removal."""
        # Phase 5: decode then route through the wrappers-backed ``*_value``
        # delegate, so the membership decision has ONE implementation.
        return _BashFixScanner._literal_command_name_value(
            _BashFixScanner._literal_word_value(raw)
        )

    @staticmethod
    def _literal_unsupported_name(raw: str) -> str | None:
        """Return the unsupported command name produced by Bash quote removal."""
        return _BashFixScanner._literal_unsupported_name_value(
            _BashFixScanner._literal_word_value(raw)
        )

    @staticmethod
    def _reparse_builtin_name(raw: str) -> int | None:
        """Return the operand count when *raw* is a literal reparse builtin.

        ``eval`` (``\\eval``, ``'eval'`` and ``ev""al`` all resolve to it)
        re-parses every operand; ``trap`` re-parses only its first (the rest
        are signal specs).  Returns ``-1`` for "all operands" or a positive
        count, ``None`` for any other word.
        """
        return _BashFixScanner._reparse_builtin_name_value(
            _BashFixScanner._literal_word_value(raw)
        )

    @staticmethod
    def _shell_wrapper_name(raw: str) -> str | None:
        """Return the shell name when *raw* is a literal ``bash``/``sh`` word."""
        return _BashFixScanner._shell_wrapper_name_value(
            _BashFixScanner._literal_word_value(raw)
        )

    # The helpers above decode the raw word first; the ``*_value`` variants
    # below take an already-decoded literal value so a caller that decoded the
    # word once (the main scan loop) does not pay for the decode repeatedly.

    @staticmethod
    def _literal_command_name_value(name: str | None) -> str | None:
        # Phase 5: the fallback-registration decision lives in
        # ``kimix_native.shell_compat.wrappers.fallback_registration`` (the
        # ``name in _FALLBACKS`` membership test + record build, shared with
        # the IR classifier); this delegate returns the registered name or
        # ``None`` exactly as before.  Lazy import (wrappers -> bash_core).
        from kimix_native.shell_compat.wrappers import fallback_registration

        record = fallback_registration(name)
        return record.name if record is not None else None

    @staticmethod
    def _literal_unsupported_name_value(name: str | None) -> str | None:
        # Phase 5: the unsupported detection lives in
        # ``kimix_native.shell_compat.wrappers.unsupported_record`` (the
        # ``name in _UNSUPPORTED_BODIES`` test + reason lookup).  The scanner
        # needs the NAME, so it maps the record back to the key.
        from kimix_native.shell_compat.wrappers import unsupported_record

        return name if unsupported_record(name) is not None else None

    @staticmethod
    def _reparse_builtin_name_value(name: str | None) -> int | None:
        # Phase 5: the reparse-builtin operand-count lookup lives in
        # ``kimix_native.shell_compat.wrappers.reparse_operand_count``
        # (``_REPARSE_BUILTINS.get``).
        from kimix_native.shell_compat.wrappers import reparse_operand_count

        return reparse_operand_count(name)

    @staticmethod
    def _shell_wrapper_name_value(name: str | None) -> str | None:
        # ``_SHELL_WRAPPERS`` membership (the table is the classifier's
        # shell branch); kept inline because the scanner calls it per word
        # and the IR path reads the same table via
        # ``wrappers.classify_command`` (``WRAPPER_CONTEXT``).
        return name if name is not None and name in _SHELL_WRAPPERS else None

    @staticmethod
    def _plausible_script_file(raw: str) -> bool:
        """Return True when *raw* looks like a script path, not a command word.

        Phase 5: the decision lives in
        :func:`kimix_native.shell_compat.wrappers.plausible_script_file`
        (shared with the IR path; lazy import — wrappers imports bash_core
        at load).  Kept at the historical name because the ``*_value``
        delegate pattern and the tests reference it.
        """
        from kimix_native.shell_compat.wrappers import plausible_script_file

        return plausible_script_file(raw)

    def _handle_shell_wrapper(
        self,
        shell_name: str,
        word_start: int,
        i: int,
        end: int,
        *,
        assignment_prefix: bool = False,
        wrapped: bool = False,
    ) -> tuple[int, bool] | None:
        """Repair a redundant ``bash``/``sh`` invocation at command position.

        Returns ``(scan index, keep_wrapper)`` when the wrapper was rewritten,
        or ``None`` when the wrapper is left untouched (legitimate
        ``bash script.sh``, semantic options such as ``-e``/``-x``, stdin
        forms, scripts with trailing argv that ``$0``/``$1`` depend on, or an
        assignment prefix such as ``VAR=x bash ...`` whose scoping the rewrite
        could not preserve).  ``keep_wrapper`` asks the caller to leave any
        active command wrapper in place so the wrapped command keeps its
        executable context.

        Two shapes are repaired:
        * ``bash <command> ...`` — the shell word is redundant because the Bash
          tool already runs the whole string via bash, so it is removed
          (``bash cd /c/dev/x && grep ...`` becomes
          ``cd /c/dev/x && grep ...``); a bare ``bash`` would try to open
          ``cd`` as a script file and fail.
        * ``bash [-l] -c <script>`` — the wrapper is NOT inlined: the child
          shell's isolation (``exit``, assignments, ``cd``, traps) must be
          preserved, and the nested bash is harmless (it inherits the
          exported fallback functions).  Only the inline script is scanned
          and re-emitted in place (``bash -c 'cd C:\\x && rev'`` becomes
          ``bash -c 'cd C:/x && rev'``).  Only the ``-c``/``-lc``/``-cl``
          forms with the script as the final word of the segment are
          repaired this way; trailing argv, ``-ec``-style clusters and
          expansions are preserved for bash to handle.

        Under an active command wrapper (``env``/``nohup``/``timeout``/``sudo``
        ...) the shell word is an *operand* of that wrapper, so the repair
        must not move text out of its argv (``timeout 5 bash -c 'a && b'``
        rewritten to ``timeout 5 a && b`` would let ``&&`` split the list):
        both forms keep the wrapper's executable (standalone-runner) context
        — the ``-c`` form naturally, and the prefix form by leaving the
        wrapper active while only dropping the shell word.
        """
        from kimix_native.shell_compat.wrappers import unwrap_decision

        decision = unwrap_decision(
            self,
            self.s,
            i,
            end,
            assignment_prefix=assignment_prefix,
            wrapped=wrapped,
        )
        if decision.action == "keep":
            if decision.harvest and decision.script_span is not None:
                # Expansions inside the script: the operand text cannot be
                # re-emitted, but its statically visible command words (and
                # the commands inside its substitutions, which run in this
                # shell) still need their fallback definitions registered
                # for the nested shell that re-parses the expanded text.
                self._harvest_nonliteral_inline_script(
                    self.s[decision.script_span[0] : decision.script_span[1]]
                )
            return None
        if decision.action == "unwrap":
            j = decision.scan_index
            assert j is not None
            # ``bash <command> ...``: drop the redundant shell word itself.
            # The edit covers the shell word plus the whitespace that
            # separated it from the real command (``bash cd ...`` ->
            # ``cd ...``).  Under a command wrapper the wrapped command must
            # keep its executable context (``env bash rev`` ->
            # ``env <runner>``), so the caller leaves the active wrapper in
            # place.
            self.edits.append((word_start, j, ""))
            self.shell_notes.append(shell_name)
            return j, wrapped
        # ``fix_script``: the wrapper stays — ``bash -c 'script'`` runs the
        # script in a CHILD shell whose isolation (``exit``, variable
        # assignments, ``cd``, traps) inlining into the surrounding script
        # would silently destroy.  The nested bash inherits the exported
        # fallback functions, and the in-place repair below fixes the
        # script's paths and commands.  Under an active command wrapper the
        # wrapper additionally keeps its executable context.
        assert decision.script_span is not None
        assert decision.scan_index is not None
        k, script_end = decision.script_span
        script = self._literal_word_value(self.s[k:script_end])
        try:
            inner = _BashFixScanner(script)
            inner._scan_range(0, len(script))
        except RecursionError:
            return None
        fixed = inner._build_fixed_source()
        self.names.extend(n for n in inner.names if n not in self.names)
        self.path_notes.extend(inner.path_notes)
        self.shell_notes.extend(
            n for n in inner.shell_notes if n not in self.shell_notes
        )
        self.nul_fixes.extend(inner.nul_fixes)
        self.cmd_conditional_notes.extend(inner.cmd_conditional_notes)
        self.unsupported.extend(
            n for n in inner.unsupported if n not in self.unsupported
        )
        if inner.edits:
            self.edits.append((k, script_end, _single_quote(fixed)))
        return script_end, wrapped

    def _fix_shell_c_script_in_place(self, i: int, end: int) -> int | None:
        """Fix the ``-c`` inline script of a shell word seen as an *operand*.

        Covers shells invoked by another program (``find . -exec sh -c '...'
        \\;``, ``xargs -I{} sh -c '...'``) and command-position forms the
        unwrapper refuses to restructure (``bash -e -c '...'``, a script with
        trailing argv): the nested shell really runs, so the wrapper stays and
        only the quoted script operand is scanned and re-emitted in place —
        the same in-place repair the wrapped ``bash -c`` path performs.
        Phase 5: the option/script scan lives in
        :func:`kimix_native.shell_compat.wrappers.shell_c_script_range`
        (shared with the IR path); the import is lazy because wrappers
        imports bash_core at load.
        """
        from kimix_native.shell_compat.wrappers import shell_c_script_range

        found = shell_c_script_range(self, self.s, i, end)
        if found is None:
            return None
        script_start, script_end = found
        script_raw = self.s[script_start:script_end]
        script = self._literal_word_value(script_raw)
        if script is None:
            # Runtime expansions inside: no re-emit, but still register the
            # statically visible fallbacks (see _harvest_nonliteral_inline_script).
            self._harvest_nonliteral_inline_script(script_raw)
            return script_end
        try:
            inner = _BashFixScanner(script)
            inner._scan_range(0, len(script))
        except RecursionError:
            return script_end
        if (
            not inner.names
            and not inner.unsupported
            and not inner.nul_fixes
            and not inner.path_notes
            and not inner.cmd_conditional_notes
        ):
            return script_end
        # Merge the inner script's registrations exactly like the wrapped
        # ``bash -c`` branch above.  The fallback definitions emitted for this
        # shell are ``export -f``-ed, and a nested bash / msys ``sh`` inherits
        # exported functions from its environment (verified on Git Bash 5.3:
        # ``tree() { ...; }; export -f tree; bash -e -c 'tree'`` runs the
        # function).  Dropping ``inner.names`` here left every option-led
        # wrapper repaired in place but still failing at runtime — the nested
        # shell had no ``tree`` (``bash -e -c 'tree'``, ``find . -exec sh -c
        # 'tree' \;``, ``sh -c 'rev' a b``) — while the expansion-bearing
        # variant (``_harvest_nonliteral_inline_script``) already registered
        # them.
        self.names.extend(n for n in inner.names if n not in self.names)
        self.path_notes.extend(inner.path_notes)
        self.shell_notes.extend(
            n for n in inner.shell_notes if n not in self.shell_notes
        )
        self.nul_fixes.extend(inner.nul_fixes)
        self.cmd_conditional_notes.extend(inner.cmd_conditional_notes)
        self.unsupported.extend(
            n for n in inner.unsupported if n not in self.unsupported
        )
        if inner.edits:
            self.edits.append(
                (script_start, script_end, _single_quote(inner._build_fixed_source()))
            )
        return script_end

    def _inline_script_operand(
        self, word_start: int, word_end: int, raw: str
    ) -> bool:
        """Fix the inline script of a quoted runtime-reparsed operand.
            Shared by the constructs whose quoted operand is re-parsed as shell
            code at *runtime*: procps ``watch`` (executes its command line
            through ``sh -c``; the Git Bash fallback mirrors that with
            ``eval "$*"``), ``eval`` (concatenates its operands and re-parses
            them), and ``trap`` (stores its first operand and re-parses it when
            the signal fires).  A quoted operand (``watch -n1 'rev <<< abc'``,
            ``eval 'foo > nul'``, ``trap 'foo > nul' EXIT``) is therefore an
            inline script: its literal value is scanned as its own command
            context and re-emitted with the inner fixes applied, so fallback
            names, Windows paths, and ``> nul`` null-device redirections inside
            it work.  Unquoted operands need no special handling here — they
            flow through the normal word rules.

            Returns ``True`` when the word was consumed as an inline script (any
            quoted operand): the caller must not also treat it as a command word,
            or a script that *is* a single fallback name (``watch 'uptime'``)
            would be recorded twice.
            """
        if not raw.startswith(("'", '"', "$'", '$"')):
            return False
        script = self._literal_word_value(raw)
        if script is None:
            # Expansions inside the script: no re-emit, but still register
            # the statically visible fallbacks (see _harvest_nonliteral_inline_script).
            self._harvest_nonliteral_inline_script(raw)
            return True
        try:
            inner = _BashFixScanner(script)
            inner._scan_range(0, len(script))
        except RecursionError:
            return True
        fixed = inner._build_fixed_source()
        self.names.extend(n for n in inner.names if n not in self.names)
        self.path_notes.extend(inner.path_notes)
        self.shell_notes.extend(
            n for n in inner.shell_notes if n not in self.shell_notes
        )
        self.nul_fixes.extend(inner.nul_fixes)
        self.cmd_conditional_notes.extend(inner.cmd_conditional_notes)
        self.unsupported.extend(
            n for n in inner.unsupported if n not in self.unsupported
        )
        if inner.edits:
            self.edits.append((word_start, word_end, _single_quote(fixed)))
        return True

    def _harvest_nonliteral_inline_script(self, raw: str) -> None:
        """Register fallbacks from an inline-script operand that holds
        runtime expansions.

        ``bash -c "rev $HOME"`` / ``watch "rev <<< $x"`` operands cannot be
        re-emitted (any re-quoting would change the word's runtime value), so
        no edit is recorded — but the statically visible command words must
        still register their fallback definitions, and so must the commands
        inside the operand's ``$(...)`` / backtick substitutions: those run
        in *this* shell (they build the script string), and the child shell
        re-parses the expanded text with the exported fallback functions
        available.  Only registrations are merged; path/nul metadata is not,
        because the operand text stays byte-for-byte and such metadata would
        claim a fix that was never applied.
        """
        inner_text = raw
        if len(raw) >= 2 and raw[0] in "'\"" and raw[-1] == raw[0]:
            inner_text = raw[1:-1]
        sanitized, substitutions = _opaque_inline_expansions(inner_text)
        sources = [sanitized, *substitutions]
        for source in sources:
            if not source.strip():
                continue
            try:
                inner = _BashFixScanner(source)
                inner._scan_range(0, len(source))
            except RecursionError:
                continue
            self.names.extend(n for n in inner.names if n not in self.names)
            self.shell_notes.extend(
                n for n in inner.shell_notes if n not in self.shell_notes
            )
            self.unsupported.extend(
                n for n in inner.unsupported if n not in self.unsupported
            )


    def _try_at_prefix(
        self, word_start: int, word_end: int, end: int
    ) -> tuple[int | None, str | None]:
        r"""Handle a leading ``@`` at a Bash command position.

        A command word starting with ``@`` is a guaranteed error on this
        platform (no ``@*`` executable exists on the Git Bash PATH; the only
        ``@*`` files are non-executable system toast icons), so it may be
        rewritten.  Two forms:

        ``@echo off`` / ``@echo on`` (case-sensitive ``off``/``on``, glued or
        space-separated, ending on a statement boundary that is not a single
        backgrounding ``&``) translate to ``set +x`` / ``set -x`` -- the cmd
        echo-visibility switch's bash counterpart -- and the whole two-word
        region is replaced (``mode`` ``statement``).  This is the only form
        that reports in ``cmd_conditional_fixes``.

        Everything else strips the single leading ``@`` and re-dispatches the
        remainder as the command word (``mode`` ``strip``), so ``@if ...``,
        ``@for ...`` compose with the control-flow hooks and ``@dir``/``@rev``
        resolve normally.  Bails (no edit, byte-for-byte) on a bare ``@``,
        ``@@x``, a quoted-leading ``@`` (``"@"dir``), or the cmd
        ``@echo.<no newline>`` idiom (kept loud rather than becoming 127
        ``echo.``).

        Returns ``(index, mode)``; ``index`` is the position to continue the
        dispatch loop at (``word_start + 1`` for a strip, or just past the
        translated ``@echo`` region for a statement).  ``(None, None)`` when
        the word is not a recognised ``@`` form.

        Phase 6: the recognition/decision lives in
        :func:`kimix_native.shell_compat.cmd_translator.at_prefix_rule` (shared
        with the IR path; this is the byte-for-byte edit/note-apply wrapper
        passing ``self`` as the reader so the ``@echo`` word read and the
        ``_at_boundary_ok`` test are the scanner's own).  The import is lazy
        (cmd_translator imports bash_core at load).
        """
        from kimix_native.shell_compat.cmd_translator import at_prefix_rule

        decision = at_prefix_rule(self, self.s, word_start, word_end, end)
        if decision is None:
            return None, None
        self.edits.append(decision.edit)
        if decision.note is not None:
            self.cmd_conditional_notes.append(decision.note)
        return decision.index, decision.mode

    def _at_boundary_ok(self, pos: int, end: int) -> bool:
        """Return whether the char after an ``@echo off|on`` word ends the form.

        A statement boundary (newline, ``;``, ``&&``, ``|``, a redirection, or
        EOF) lets the two-word translation stand; a single backgrounding ``&``
        (cmd runs the tail inline per line, bash would background it) bails to
        the generic strip, as do glued words like ``@echo offline``.

        Phase 6: the test lives in
        :func:`kimix_native.shell_compat.cmd_translator.at_boundary_ok` (shared
        with the IR path).  Lazy import (cmd_translator imports bash_core at
        load).
        """
        from kimix_native.shell_compat.cmd_translator import at_boundary_ok

        return at_boundary_ok(self.s, pos, end)

    # ── cmd.exe ``if [not] exist`` conditional recognition ──────────────────────

    def _try_cmd_if(self, if_start: int, if_end: int, end: int) -> int | None:
        """Translate a cmd.exe ``if exist`` conditional at command position.

        *if_start*/*if_end* bound the ``if`` keyword word.  A cmd conditional
        written into a bash command line is a guaranteed Bash syntax error
        (a command word at pipeline head is never followed by a bare ``(``),
        so recognising the shape can never change the meaning of a command
        Bash would have accepted.  Returns the index just past the whole
        conditional after recording the header/paren edits and scanning each
        branch body normally (inner path/``nul``/fallback rewrites keep
        firing); returns ``None`` — with no edits recorded at all — unless
        :meth:`_match_cmd_if` validates the strict single-line parenthesised
        form, leaving the text byte-for-byte for Bash.

        Phase 6: the validation/commit split lives in
        :func:`kimix_native.shell_compat.cmd_translator.validate_cmd_if` /
        :func:`kimix_native.shell_compat.cmd_translator.commit_cmd_if` (shared
        with the IR path; ``self`` is the reader so every word boundary and the
        recursive branch-body scan are byte-identical).  The match object is
        produced by the read-only pass; committing it is guaranteed to succeed
        and every edit pair is disjoint by construction.
        """
        from kimix_native.shell_compat.cmd_translator import (
            commit_cmd_if,
            validate_cmd_if,
        )

        match = validate_cmd_if(self, self.s, if_start, if_end, end)
        if match is None:
            return None
        self.cmd_conditional_notes.append(
            self.s[if_start : match.segments[0].op_end]
        )
        return commit_cmd_if(self, self.s, match)

    @staticmethod
    def _split_top_eq(raw: str) -> tuple[str, str] | None:
        r"""Split a glued cmd ``LHS==RHS`` compare word on the first ``==``.

        cmd.exe toggles a double-quoted region on every ``"``, so a ``==``
        inside quotes is data, not the operator.  Returns ``(lhs_raw,
        rhs_raw)`` on the first *top-level* ``==`` (empty sides allowed and
        rejected later by the operand validator) or ``None`` when the word
        carries no top-level ``==``.  Only the glued single-word form is
        recognised: a spaced ``LHS == RHS`` breaks the single-word rule and
        is a deliberate v1 bail (cmd itself treats ``== RHS`` as junk
        arguments -- probed).

        Phase 6: the rule lives in
        :func:`kimix_native.shell_compat.cmd_translator.split_top_eq` (shared
        with the IR path).  Lazy import (cmd_translator imports bash_core).
        """
        from kimix_native.shell_compat.cmd_translator import split_top_eq

        return split_top_eq(raw)

    def _cmd_cmp_side(
        self, raw: str, *, folded: bool, numeric: bool
    ) -> tuple[str, bool] | None:
        r"""Return ``(bash_operand, was_cmd_quoted)`` for one compare side.

        Accepts only the forms cmd compares faithfully as a single token
        (design §3.1): an exact ``%IDENT%`` (mapped to ``${IDENT}``, or
        ``${IDENT,,}`` under ``/i``), ``%errorlevel%`` (case-insensitive, maps
        to ``$?``), an integer literal for ``numeric`` arithmetic, or a
        possibly double-quoted string literal whose decoded value contains no
        ``\ " ' % ! $ `` backtick, glob, or shell-control character.  The outer
        quotes are reported via ``was_cmd_quoted`` so the caller can enforce
        cmd's quote-as-data semantics (asymmetric quoting bails).  ``None``
        bails the whole conditional.

        Phase 6: the rule lives in
        :func:`kimix_native.shell_compat.cmd_translator.cmd_cmp_side` (shared
        with the IR path).  Lazy import (cmd_translator imports bash_core).
        """
        from kimix_native.shell_compat.cmd_translator import cmd_cmp_side

        return cmd_cmp_side(raw, folded=folded, numeric=numeric)

    def _build_cmd_cmp(
        self, lhs_raw: str, rhs_raw: str, op: str, *, folded: bool, numeric: bool
    ) -> str | None:
        """Build the bash condition text for a cmd compare/arithmetic pair.

        ``numeric`` emits the ``(( ))`` C operators from
        :data:`_CMD_REL_OPS`; the string form emits ``[[ LHS == "RHS" ]]``
        with the right-hand side always quoted (``[[ ]]`` treats an unquoted
        right side as a pattern) and both sides' cmd quoting symmetric (an
        asymmetric ``a=="a"`` is FALSE in cmd but TRUE under a naive ``==``
        -- design §3.1 -- so it bails).  ``/i`` folding applies ``,,`` to
        expansion sides and lowercases literal sides; it is inert for
        arithmetic (numbers compare the same either way).

        Phase 6: the rule lives in
        :func:`kimix_native.shell_compat.cmd_translator.build_cmd_cmp` (shared
        with the IR path).  Lazy import (cmd_translator imports bash_core).
        """
        from kimix_native.shell_compat.cmd_translator import build_cmd_cmp

        return build_cmd_cmp(
            lhs_raw, rhs_raw, op, folded=folded, numeric=numeric
        )


    def _match_cmd_if(
        self, if_start: int, pos: int, end: int
    ) -> _CmdIfMatch | None:
        """Validate the cmd conditional shape; strictly read-only.

        *if_start* is the start of the command-position ``if`` word and
        *pos* the index just past it.  The full shape and every documented
        bail are documented in
        :func:`kimix_native.shell_compat.cmd_translator.validate_cmd_if`
        (the canonical Phase-6 implementation, shared with the IR path):
        horizontal whitespace only, an optional literal ``not``/``/i``, the
        ``exist``/``defined``/``errorlevel``/glued ``==``/spaced relational
        condition kinds, a mandatory ``(`` anchor, a matched ``)``, and the
        optional ``else (``/``else if`` tail.  Returns ``None`` on any
        deviation.  Phase 6: this is a thin delegate passing ``self`` as the
        reader so every word boundary and ``_find_matching`` is byte-identical
        to the historical method.  Lazy import (cmd_translator imports
        bash_core at load).
        """
        from kimix_native.shell_compat.cmd_translator import validate_cmd_if

        return validate_cmd_if(self, self.s, if_start, pos, end)

    def _cmd_if_branch_ok(self, start: int, stop: int) -> bool:
        """Return whether the cmd branch body ``[start, stop)`` is translatable.

        Rejects (so the whole conditional stays byte-for-byte): bodies
        containing a newline (multi-line cmd conditional — out of scope, and
        a trailing comment or heredoc body would swallow the inserted
        ``;``/``; fi`` terminator), empty bodies (Bash rejects
        ``then ;``/``else ;``), and bodies holding a ``#`` that starts a
        Bash comment.  Phase 6: the rule lives in
        :func:`kimix_native.shell_compat.cmd_translator.cmd_if_branch_ok`
        (shared with the IR path).  Lazy import (cmd_translator imports
        bash_core at load).
        """
        from kimix_native.shell_compat.cmd_translator import cmd_if_branch_ok

        return cmd_if_branch_ok(self.s, start, stop)

    def _commit_cmd_if(self, match: _CmdIfMatch) -> int:
        """Emit the edits for a validated cmd conditional and scan its bodies.

        All edits are disjoint by construction (design §4.3): the H/EL
        header edit stops at the operand, the operand path rewrite stops at
        the operand end, the O edit spans operand-end..``)``-inclusive,
        branch-inner edits live strictly inside the parentheses, and the
        C/E close edits start at the closing ``)``.  Parens are deleted,
        never turned into subshells: a cmd group shares the interpreter
        context (``cd`` inside a branch persists), which plain
        ``then``/``else`` grouping preserves.  Phase 6: the commit lives in
        :func:`kimix_native.shell_compat.cmd_translator.commit_cmd_if` (shared
        with the IR path); ``self`` is the reader so the recursive branch-body
        ``_scan_range`` and the ``edits``/``path_notes`` appends target this
        scanner.  Lazy import (cmd_translator imports bash_core at load).
        """
        from kimix_native.shell_compat.cmd_translator import commit_cmd_if

        return commit_cmd_if(self, self.s, match)

    def _try_cmd_for(
        self, for_start: int, for_end: int, end: int
    ) -> int | None:
        r"""Translate a cmd.exe ``for [%|%]name in (list) do body`` loop.

        Recognised only as a guaranteed Bash syntax error at this position
        (a bare ``(`` never follows a command word), so the strict single-line
        parenthesised form may be translated to
        ``for name in list; do body; done`` while anything else falls through
        to today's behavior byte-for-byte.  Branch/loop bodies are scanned
        normally so inner path / ``nul`` / fallback rewrites and nested cmd
        control-flow keep composing.  Returns the index just past the whole
        loop after recording the structural edits, or ``None`` (no edits) when
        :meth:`_match_cmd_for` does not fully validate.

        Phase 6: the validation/commit split lives in
        :func:`kimix_native.shell_compat.cmd_translator.validate_cmd_for` /
        :func:`kimix_native.shell_compat.cmd_translator.commit_cmd_for` (shared
        with the IR path; ``self`` is the reader so every word boundary and the
        recursive body scan are byte-identical).
        """
        from kimix_native.shell_compat.cmd_translator import (
            commit_cmd_for,
            validate_cmd_for,
        )

        match = validate_cmd_for(self, self.s, for_start, for_end, end)
        if match is None:
            return None
        self.cmd_conditional_notes.append(
            self.s[for_start : match.close_pos + 1]
        )
        return commit_cmd_for(self, self.s, match)

    def _match_cmd_for(
        self, for_start: int, pos: int, end: int
    ) -> _CmdForMatch | None:
        r"""Validate the cmd ``for`` loop shape; strictly read-only.

        The full shape and every documented bail are in
        :func:`kimix_native.shell_compat.cmd_translator.validate_cmd_for`
        (the canonical Phase-6 implementation, shared with the IR path).
        Phase 6: thin delegate passing ``self`` as the reader.  Lazy import.
        """
        from kimix_native.shell_compat.cmd_translator import validate_cmd_for

        return validate_cmd_for(self, self.s, for_start, pos, end)

    def _match_cmd_for_list(
        self, i: int, close: int
    ) -> tuple[tuple[tuple[int, int], ...], tuple[tuple[int, int, str], ...]] | None:
        r"""Validate a cmd ``for`` item list and emit its data edits.

        Splits the region on whitespace and on cmd's comma item separator.
        Each item must decode to plain data (no leading/trailing quote, and no
        ``$``/backtick/``!``/``%``/``^``); bare glob items are allowed.  Every
        comma becomes a single-character space edit; every comma-free item gets
        the ordinary Windows path rewrite.  Returns ``(comma_spans,
        list_path_edits)`` or ``None`` to bail the whole loop.  Phase 6: the
        rule lives in
        :func:`kimix_native.shell_compat.cmd_translator.validate_cmd_for_list`
        (shared with the IR path).  Thin delegate passing ``self`` as the
        reader.  Lazy import.
        """
        from kimix_native.shell_compat.cmd_translator import validate_cmd_for_list

        return validate_cmd_for_list(self, self.s, i, close)

    @staticmethod
    def _cmd_for_item_ok(item: str) -> bool:
        """Return whether a cmd ``for`` list item is plain translatable data.

        Rejects quote-delimited items (``"a b"``, commas inside them are data
        in cmd) and any runtime/expansion or cmd-escape character.  Glob
        characters are explicitly allowed: cmd expands the set at run time and
        bash's ``for`` list expands its globs at run time too.  Phase 6: the
        rule lives in
        :func:`kimix_native.shell_compat.cmd_translator.cmd_for_item_ok`
        (shared with the IR path).  Lazy import.
        """
        from kimix_native.shell_compat.cmd_translator import cmd_for_item_ok

        return cmd_for_item_ok(item)

    def _match_cmd_for_body(
        self, start: int, stop: int, name: str
    ) -> tuple[tuple[int, int], ...] | None:
        """Validate a cmd ``for`` do-body and collect its loop-var word spans.

        Returns the ``(word_start, word_end)`` span of every whole-word
        ``%name``/``%%name`` occurrence (rewritten to ``$name``), or ``None``
        to bail.  Phase 6: the rule lives in
        :func:`kimix_native.shell_compat.cmd_translator.validate_cmd_for_body`
        (shared with the IR path).  Thin delegate passing ``self`` as the
        reader.  Lazy import.
        """
        from kimix_native.shell_compat.cmd_translator import validate_cmd_for_body

        return validate_cmd_for_body(self, self.s, start, stop, name)

    @staticmethod
    def _cmd_body_has_bare_ampersand(body: str) -> bool:
        """Return whether a cmd ``for`` body holds a single (backgrounding) ``&``.

        A run of one ``&`` outside quotes backgrounds the preceding command in
        bash but sequences it inline in cmd, so it bails; ``&&`` (and ``||``)
        run faithfully per iteration and is allowed.  Quote-aware.  Phase 6:
        the rule lives in
        :func:`kimix_native.shell_compat.cmd_translator.cmd_body_has_bare_ampersand`
        (shared with the IR path).  Lazy import.
        """
        from kimix_native.shell_compat.cmd_translator import (
            cmd_body_has_bare_ampersand,
        )

        return cmd_body_has_bare_ampersand(body)

    def _commit_cmd_for(self, match: _CmdForMatch) -> int:
        r"""Emit the edits for a validated cmd ``for`` loop and scan its body.

        All edits are disjoint by construction (design §3.2): the loop-name
        token, the two paren deletions, the comma -> space edits, the
        Windows-path rewrites on comma-free list items, the whole-word
        loop-variable -> ``$name`` rewrites in the body, and the zero-width
        ``; done`` insertion at the end of the physical line.  The body is then
        scanned as a command context so inner ``2>nul`` / path / fallback
        rewrites and nested cmd control-flow fire recursively.  Phase 6: the
        commit lives in
        :func:`kimix_native.shell_compat.cmd_translator.commit_cmd_for` (shared
        with the IR path); ``self`` is the reader.  Lazy import.
        """
        from kimix_native.shell_compat.cmd_translator import commit_cmd_for

        return commit_cmd_for(self, self.s, match)


    def _scan_range(self, start: int, end: int) -> None:
        """Scan *start..end* as a command context, bounding recursion depth.

        The wrapper exists so pathological nesting (hundreds of nested
        ``$( ... )`` levels) costs O(_MAX_NESTING_DEPTH * length) instead of
        O(depth * length); the innermost levels are left for Bash.  An empty
        range holds no commands, so it is skipped without touching the depth
        counter.
        """
        if start >= end:
            return
        if self.nest_depth >= _MAX_NESTING_DEPTH:
            return
        self.nest_depth += 1
        try:
            self._scan_range_inner(start, end)
        finally:
            self.nest_depth -= 1

    def _scan_range_inner(self, start: int, end: int) -> None:
        s = self.s
        i = start
        command_expected = True
        redirect_expected = False
        redirect_resume = True
        redirect_op: str | None = None
        wrapper: _BashWrapper | None = None
        heredoc_operator: str | None = None
        herestring_flag = False
        pending_heredocs: list[_BashHereDoc] = []
        case_stack: list[str] = []
        # Set right after a command-position ``for`` word so the immediately
        # following ``(( ))`` unit is recognised as the arithmetic loop header
        # (``for ((expr))``).  Bash 5 accepts a brace-group loop body without
        # ``do`` right after the header
        # (``for ((i=0;i<2;i++)) { tree; rev; }``); ``for_brace_body`` carries
        # that expectation to the next ``{`` word.
        for_arith_header = False
        for_brace_body = False
        function_name_expected = False
        function_body_expected = False
        # Set when an assignment prefix (``VAR=x``) precedes the command word on
        # this line; a following ``bash`` wrapper must keep its shell-process
        # scoping, so it is not unwrapped (``VAR=x bash -c 'echo $VAR'``).
        assignment_prefix = False
        # Non-zero right after a command-position reparse builtin (``eval``/
        # ``trap``): the following argument words are inline scripts.  -1
        # means every operand (``eval``); a positive count covers only the
        # leading operands (``trap``'s single script operand).
        reparse_operands = 0
        # True only for the FIRST operand word after a reparse builtin: bash
        # concatenates the operands and re-parses them, so the leading word
        # holds the command name (see the reparse branch below).
        reparse_leading = False
        # Literal value of the command word of the current simple command
        # (used to decide whether argument words can be executed by the
        # command, e.g. ``find -exec sh -c``; see _EXEC_OPERAND_COMMANDS).
        command_word: str | None = None
        # Pattern-consumer state for the current simple command (see
        # _PATTERN_PROGRAM_COMMANDS): the command family, the verdict
        # deferred to the next word by a value-taking option, and the count
        # of bare operands seen.  Resets wherever command_word resets.
        pattern_family: str | None = None
        pattern_pending: str | None = None
        pattern_bare = 0
        # One-shot suppression of the ``#``-comment check for the character
        # immediately after a command/process substitution's closing ``)``.
        # That ``)`` is word-internal in Bash (a substitution expands inside
        # a word), so a glued ``#`` continues the word and does NOT start a
        # comment — unlike a ``#`` after a control-operator ``)`` (subshell),
        # which genuinely comments out the rest of the line.
        subst_just_closed = False

        while i < end:
            ch = s[i]
            # Snapshot and clear the one-shot substitution-close flag: it
            # applies ONLY to the character directly after the ``)`` (this
            # iteration), never across whitespace or control operators.
            suppress_hash_comment = subst_just_closed
            subst_just_closed = False

            if ch in " \t\r":
                # A whitespace *run* (indentation, aligned columns) is skipped
                # in one C-level match; a single space just steps over.
                if i + 1 < end and s[i + 1] in " \t\r":
                    m = _HSPACE_RE.match(s, i, end)
                    i = m.end() if m is not None else i + 1
                else:
                    i += 1
                continue
            if ch == "\\" and i + 1 < end and s[i + 1] == "\n":
                i += 2
                continue
            if ch == "\n":
                i += 1
                if pending_heredocs:
                    i = self._skip_heredoc_bodies(i, end, pending_heredocs)
                    pending_heredocs.clear()
                command_expected = True
                redirect_expected = False
                redirect_op = None
                heredoc_operator = None
                herestring_flag = False
                wrapper = None
                assignment_prefix = False
                reparse_operands = 0
                command_word = None
                pattern_family = None
                pattern_pending = None
                pattern_bare = 0
                for_arith_header = False
                for_brace_body = False
                continue
            if (
                ch == "#"
                and self._comment_starts(i, start)
                and not suppress_hash_comment
            ):
                newline = s.find("\n", i + 1, end)
                i = end if newline < 0 else newline
                continue

            process_substitution = ch in _REDIRECTION_START and s[i + 1 : i + 2] == "("
            if not process_substitution and (
                ch in _REDIRECTION_START
                or (ch == "&" and s[i + 1 : i + 2] == ">")
                or (ch.isdigit() and self._redirection_after_fd(i, end))
            ):
                op_start = i
                if ch.isdigit():
                    while i < end and s[i].isdigit():
                        i += 1
                op, i = self._read_redirection(i, end)
                if op:
                    redirect_resume = command_expected
                    redirect_expected = True
                    redirect_op = op
                    herestring_flag = op == "<<<"
                    if op in {"<<", "<<-"}:
                        # The delimiter is captured when the following word is
                        # read; its body starts only after this command line.
                        pass
                    else:
                        op_start = -1
                    if op_start >= 0:
                        heredoc_operator = op
                    continue
                i = op_start

            if redirect_expected:
                if ch in _REDIRECTION_START and s[i + 1 : i + 2] == "(":
                    close = self._find_matching(i + 2, end, ")")
                    self._scan_range(i + 2, close if close < end else end)
                    word_end = close + 1 if close < end else end
                    # A ``#`` glued after the process-substitution ``)`` is
                    # word-internal, not a comment (see the main-loop check).
                    subst_just_closed = True
                else:
                    scan_substitutions = heredoc_operator not in {"<<", "<<-"}
                    word_end = self._read_word(
                        i, end, scan_substitutions=scan_substitutions
                    )
                if word_end <= i:
                    i += 1
                    continue
                if heredoc_operator in {"<<", "<<-"}:
                    heredoc = self._heredoc_delimiter(s[i:word_end])
                    if heredoc is not None:
                        delimiter, expands = heredoc
                        pending_heredocs.append(
                            _BashHereDoc(delimiter, heredoc_operator == "<<-", expands)
                        )
                elif not herestring_flag:
                    raw_word = s[i:word_end]
                    # ``> nul`` / ``2> NUL`` / ``&> nul`` / ``>> nul`` would
                    # silently create a file literally named ``nul`` in Git
                    # Bash (Windows-style null-device redirection).  Escape
                    # spellings whose value is exactly the word ``nul`` create
                    # the file too (verified on Git Bash: ``> \nul``,
                    # ``> n\ul``, ``> $'nul'`` all produce it), so they are
                    # rewritten as well.  Quoted ``'nul'``/``"nul"`` stays an
                    # intentional filename (documented policy), words with
                    # expansions (``> $(echo nul)``) keep their value only at
                    # runtime and are left untouched, and input redirections
                    # (``< nul``) never create a file.
                    if (
                        redirect_op is not None
                        # Pure input redirections (``<``/``<<``/``<<<``/``<&``)
                        # never create a file; ``<>`` opens its target for
                        # reading *and writing*, so ``<> nul`` does.
                        and redirect_op not in {"<", "<<", "<<-", "<<<", "<&"}
                        and self._is_effective_nul_target(raw_word)
                    ):
                        self.edits.append((i, word_end, "/dev/null"))
                        self.nul_fixes.append(raw_word)
                    else:
                        replacement = self._path_replacement(raw_word)
                        if replacement is not None:
                            self.edits.append((i, word_end, replacement))
                            self.path_notes.append(raw_word)
                i = word_end
                command_expected = redirect_resume
                redirect_expected = False
                redirect_op = None
                heredoc_operator = None
                continue

            if ch == "[" and s[i + 1 : i + 2] == "[":
                function_body_expected = False
                i = self._skip_conditional(i + 2, end)
                command_expected = False
                for_brace_body = False
                continue
            if ch == "(" and s[i + 1 : i + 2] == "(":
                function_body_expected = False
                i = self._skip_arithmetic(i + 2, end)
                # ``for ((expr))`` arithmetic header: bash 5 accepts a do-less
                # brace-group body right after the unit
                # (``for ((i=0;i<2;i++)) { tree; rev; }``); the ``{`` word then
                # opens the group in command position.  A bare simple command
                # there is a syntax error, so words stay arguments.
                command_expected = False
                for_brace_body = for_arith_header
                for_arith_header = False
                continue
            if ch == "$" and s[i + 1 : i + 2] == "(" and s[i + 2 : i + 3] != "(":
                close = self._find_matching(i + 2, end, ")")
                inner_end = close if close < end else end
                self._scan_range(i + 2, inner_end)
                i = close + 1 if close < end else end
                # A ``#`` glued after the substitution's ``)`` continues the
                # word in Bash — it must not start a comment here.
                subst_just_closed = True
                for_brace_body = False
                if case_stack and case_stack[-1] == "word":
                    # A substitution can be the case subject word itself
                    # (``case $(...) in ...``); it ends the header just like
                    # a plain subject word.
                    case_stack[-1] = "await-in"
                command_expected = False
                continue
            if ch == "`":
                close = self._find_backtick_end(i + 1, end)
                self._scan_range(i + 1, close)
                i = close + 1 if close < end else end
                for_brace_body = False
                if case_stack and case_stack[-1] == "word":
                    case_stack[-1] = "await-in"
                command_expected = False
                continue
            if ch in _REDIRECTION_START and s[i + 1 : i + 2] == "(":
                close = self._find_matching(i + 2, end, ")")
                self._scan_range(i + 2, close if close < end else end)
                i = close + 1 if close < end else end
                # A ``#`` glued after the process-substitution's ``)`` is
                # word-internal, not a comment (see the main-loop check).
                subst_just_closed = True
                for_brace_body = False
                if case_stack and case_stack[-1] == "word":
                    case_stack[-1] = "await-in"
                command_expected = False
                continue

            if ch in _CONTROL_OP_STARTS:
                op, op_end = self._read_control_operator(i, end)
            else:
                op, op_end = "", i
            if op:
                i = op_end
                for_brace_body = False
                if op == "(" and function_body_expected:
                    function_body_expected = False
                    command_expected = True
                elif op == "(":
                    command_expected = True
                elif op == ")":
                    if case_stack and case_stack[-1] == "patterns":
                        case_stack[-1] = "body"
                        command_expected = True
                    else:
                        command_expected = False
                elif op in {";;", ";&", ";;&"}:
                    if case_stack:
                        case_stack[-1] = "patterns"
                        command_expected = False
                    else:
                        command_expected = True
                else:
                    command_expected = True
                redirect_expected = False
                heredoc_operator = None
                wrapper = None
                assignment_prefix = False
                reparse_operands = 0
                command_word = None
                pattern_family = None
                pattern_pending = None
                pattern_bare = 0
                continue

            word_start = i
            scan_substitutions = heredoc_operator not in {"<<", "<<-"}
            word_end = self._read_word(i, end, scan_substitutions=scan_substitutions)
            if word_end <= i:
                i += 1
                continue
            raw = s[word_start:word_end]
            i = word_end
            # Any word token is a full token boundary: a pending ``for`` word
            # only pairs with an ``(( ))`` unit that follows it immediately.
            brace_body_open = for_brace_body and raw == "{"
            for_arith_header = False
            for_brace_body = False
            if function_name_expected:
                function_name_expected = False
                function_body_expected = True
                command_expected = False
                declaration_end = self._empty_parentheses_end(i, end)
                if declaration_end is not None:
                    i = declaration_end
                continue

            if function_body_expected:
                function_body_expected = False
                if raw == "{":
                    command_expected = True
                    continue
                # Brace-less body: a compound keyword (`f() if …`, `f() case
                # …`, `f() while …`) or a simple command (`f() tree;`).  Fall
                # through to the ordinary command-position handling below —
                # it re-arms command position for test keywords and sets up
                # the case/for state machine — instead of consuming the word
                # as data, which dropped the body's fallback registrations.
                command_expected = True

            if case_stack and case_stack[-1] == "word":
                case_stack[-1] = "await-in"
                command_expected = False
                continue
            if case_stack and case_stack[-1] == "await-in" and raw == "in":
                case_stack[-1] = "patterns"
                command_expected = False
                continue
            if case_stack and case_stack[-1] == "patterns":
                if raw == "esac":
                    case_stack.pop()
                command_expected = False
                continue

            if not command_expected:
                if (
                    command_word in _EXEC_OPERAND_COMMANDS
                    and self._shell_wrapper_name(raw) is not None
                ):
                    # A shell invoked as an *operand* of a program that
                    # executes its argv (``find . -exec sh -c '...' \;``,
                    # ``xargs sh -c``): the nested shell really runs, so fix
                    # its ``-c`` inline script in place.  Gated on the current
                    # command word so plain data uses (``echo sh -c '...'``)
                    # stay untouched.
                    script_end = self._fix_shell_c_script_in_place(i, end)
                    if script_end is not None:
                        i = script_end
                        continue
                if reparse_operands and raw not in _COMMAND_END_KEYWORDS:
                    # Argument word of a reparse builtin (``eval``/``trap``):
                    # its literal value is re-parsed as shell code at runtime,
                    # so scan it as its own command context (this is what
                    # fixes ``eval 'foo > nul'``).  Unquoted words are
                    # ordinary words in the current context (``eval foo >
                    # nul`` — the redirection is scanned here anyway); only
                    # quoted operands carry a whole script.
                    if reparse_operands > 0:
                        reparse_operands -= 1
                    if self._inline_script_operand(word_start, word_end, raw):
                        reparse_leading = False
                        continue
                    if reparse_leading:
                        # Unquoted leading operand of ``eval``/``trap``: bash
                        # concatenates and re-parses it as code in THIS shell
                        # (the prelude's fallback functions are in scope), so
                        # a literal command name it carries needs its fallback
                        # registered just like a quoted script's command word
                        # (``eval tree`` runs ``tree`` -> rc 127 without it).
                        # Only the LEADING word is command position; later
                        # operands are arguments (``eval echo tree``).
                        reparse_leading = False
                        operand = self._literal_word_value(raw)
                        if operand:
                            fallback_name = self._literal_command_name_value(
                                operand
                            )
                            if fallback_name is not None:
                                self.names.append(fallback_name)
                            else:
                                self._record_unsupported(raw, operand)
                if raw in {"then", "do", "else", "elif"}:
                    command_expected = True
                elif raw == "esac" and case_stack:
                    case_stack.pop()
                elif brace_body_open:
                    # do-less ``for ((expr)) { … }" body opens here
                    command_expected = True
                elif pattern_family is not None:
                    # Argument of a pattern-consuming command (grep/sed/awk):
                    # the leading operand is pattern/program DATA, so a
                    # drive-prefixed word keeps its runtime value instead of
                    # being "repaired" into a different string (see
                    # _PATTERN_PROGRAM_COMMANDS).
                    value = self._literal_word_value(raw)
                    word = value if value is not None else raw
                    if pattern_pending is not None:
                        pending, pattern_pending = pattern_pending, None
                        path_ok = pending == "path"
                    elif word.startswith("-") and word != "-":
                        option = self._classify_pattern_option(
                            pattern_family, word
                        )
                        # Option words themselves are never paths (they do
                        # not start with a drive letter, so the rewrite
                        # could not fire anyway); defer the verdict for
                        # value-taking options.
                        path_ok = False
                        if option == "data-pending":
                            pattern_pending = "data"
                        elif option == "path-pending":
                            pattern_pending = "path"
                        elif option == "value-pending":
                            pattern_pending = "value"
                    elif pattern_bare == 0:
                        # First bare operand: the pattern/program.
                        pattern_bare += 1
                        path_ok = False
                    else:
                        # Later bare operands: files.
                        pattern_bare += 1
                        path_ok = True
                    if path_ok:
                        replacement = self._path_replacement(raw)
                        if replacement is not None:
                            self.edits.append(
                                (word_start, word_end, replacement)
                            )
                            self.path_notes.append(raw)
                else:
                    replacement = self._path_replacement(raw)
                    if replacement is not None:
                        self.edits.append((word_start, word_end, replacement))
                        self.path_notes.append(raw)
                if (
                    "=" in raw
                    and _ASSIGNMENT_RE.match(raw)
                    and i < end
                    and s[i] == "("
                ):
                        # Array literal as a declaration-builtin argument
                        # (``declare -a arr=( ...)``): its elements are data
                        # words, scanned like the command-position form.
                        close = self._find_matching(i + 1, end, ")")
                        self._scan_array_words(
                            i + 1, close if close < end else end
                        )
                        i = close + 1 if close < end else end
                continue

            if raw[:1] == "@":
                # A leading ``@`` at a Bash command position is a guaranteed
                # error on this platform (no ``@*`` executable on PATH), so it
                # may be handled.  ``@echo off|on`` becomes ``set +x|-x``; any
                # other ``@word`` is stripped and re-dispatched as a command
                # word so ``@if``/``@for`` compose with the control-flow hooks
                # and ``@rev`` still gets its fallback prefix (design \u00a73.4).
                at_index, at_mode = self._try_at_prefix(word_start, word_end, end)
                if at_index is not None:
                    i = at_index
                    if at_mode == "statement":
                        command_expected = False
                    continue
            if raw == "function":
                function_name_expected = True
                command_expected = True
                continue
            declaration_end = self._function_declaration_end(raw, i, end)
            if declaration_end is not None:
                i = declaration_end
                function_body_expected = True
                command_expected = False
                continue
            if raw == "if":
                # cmd.exe `if [not] exist <path> (A) else (B)` conditional:
                # guaranteed to be a Bash syntax error at this position (a
                # command word is never followed by a bare `(`), so a full
                # match may be translated; anything else falls through to
                # the keyword handling below untouched (byte-for-byte).
                consumed = self._try_cmd_if(word_start, word_end, end)
                if consumed is not None:
                    i = consumed
                    command_expected = False  # the emitted `fi` completes it
                    continue
            if raw == "for":
                # cmd.exe `for [%|%]name in (list) do body` loop: also a
                # guaranteed Bash syntax error here (a bare `(` after `in`),
                # so a full match may be translated.  A non-matching `for`
                # falls through to the `_LIST_KEYWORDS` arm below (today's
                # behavior, `for_arith_header` flag consumed by the next word)
                # with the text left byte-for-byte.
                consumed = self._try_cmd_for(word_start, word_end, end)
                if consumed is not None:
                    i = consumed
                    command_expected = False  # the emitted `done` completes it
                    continue
            if raw in _COMMAND_START_KEYWORDS:
                command_expected = True
                continue
            if raw in _COMMAND_END_KEYWORDS:
                if raw == "esac" and case_stack:
                    case_stack.pop()
                command_expected = False
                continue
            if raw in _LIST_KEYWORDS:
                if raw == "case":
                    case_stack.append("word")
                elif raw == "for":
                    for_arith_header = True
                command_expected = False
                continue
            if "=" in raw and _ASSIGNMENT_RE.match(raw):
                if i < end and s[i] == "(":
                    close = self._find_matching(i + 1, end, ")")
                    self._scan_array_words(i + 1, close if close < end else end)
                    i = close + 1 if close < end else end
                command_expected = True
                assignment_prefix = True
                continue

            if raw == "cd":
                self._drop_cmd_cd_flag(i, end)

            executable_wrapper = (
                wrapper is not None and wrapper.kind not in _SAME_SHELL_WRAPPERS
            )
            if wrapper is not None and wrapper.kind == "coproc":
                if self._coproc_name_before_compound(raw, i, end):
                    wrapper = None
                    command_expected = True
                    continue
            inline_consumed = False
            if wrapper is not None and wrapper.kind in _WRAPPER_PATH_OPTIONS:
                for option in _WRAPPER_PATH_OPTION_LONG:
                    if raw.startswith(option + "="):
                        value = raw[len(option) + 1 :]
                        replacement = self._path_replacement(value)
                        if replacement is not None:
                            self.edits.append(
                                (word_start, word_end, option + "=" + replacement)
                            )
                            self.path_notes.append(raw)
                        # An inline option also fills a pending value slot
                        # (``env -C --chdir=D:\\x``) and leaves the wrapper
                        # itself active for the command that follows it.
                        wrapper.skip_next = False
                        wrapper.path_value = False
                        command_expected = True
                        inline_consumed = True
                        break
            if inline_consumed:
                continue
            if wrapper is not None:
                path_option_value = wrapper.path_value and wrapper.skip_next
                action = self._consume_wrapper_word(wrapper, raw)
                if action == "skip":
                    if path_option_value:
                        replacement = self._path_replacement(raw)
                        if replacement is not None:
                            self.edits.append((word_start, word_end, replacement))
                            self.path_notes.append(raw)
                    command_expected = True
                    continue
                if action == "inspect":
                    command_expected = False
                    wrapper = None
                    continue
                if wrapper is not None and wrapper.kind == "watch":
                    # ``watch`` re-executes its command in the current shell
                    # (``eval "$*"``, matching procps ``watch``'s ``sh -c``),
                    # so a quoted operand is an inline script: scan its
                    # literal value as its own command context so fallback
                    # names and Windows paths inside it are fixed too.  An
                    # unquoted operand falls through to the plain command-word
                    # rules below (``watch`` is a same-shell wrapper, so the
                    # source word resolves against the prefix functions).
                    # Phase 5: the same-shell gate is
                    # ``wrappers.watch_reparse_operand`` (one predicate).
                    from kimix_native.shell_compat.wrappers import (
                        watch_reparse_operand as _watch_gate,
                    )

                    if _watch_gate(wrapper.kind):
                        consumed = self._inline_script_operand(
                            word_start, word_end, raw
                        )
                        wrapper = None
                        if consumed:
                            command_expected = False
                            continue

            # A leading-backslash word (``\sudo tree``) only suppresses
            # alias lookup of the wrapper name; it invokes the same wrapper,
            # so the tables are keyed on the backslash-stripped word.  Shell
            # keywords (``time``, ``coproc``) are the exception: escaping
            # suppresses the KEYWORD, so the external command runs — and an
            # external command cannot invoke the registered shell functions
            # (the raw keyword can), making wrapper registration useless.
            unescaped = (
                raw[1:]
                if raw.startswith("\\") and raw[1:] not in _SHELL_KEYWORDS
                else raw
            )
            # Quoted spellings of the wrapper word itself (``$'command'``,
            # ``"env"``) decode to the same static name the tables key on;
            # without the fallback the wrapped command's operand is never
            # scanned (``$'command' $'tree'`` -> rc 127).  Shell keywords are
            # excluded for the same reason as the backslash form above: a
            # quoted/escaped keyword suppresses keyword recognition, so the
            # external command runs and cannot invoke the shell functions.
            literal_unescaped: str | None = None
            if raw != unescaped or raw[:1] in {"'", '"', "$"}:
                decoded = self._literal_word_value(raw)
                if decoded is not None and decoded not in _SHELL_KEYWORDS:
                    literal_unescaped = decoded
            wrapper_key = unescaped
            if wrapper_key not in _FALLBACK_COMMAND_WRAPPERS and (
                wrapper_key not in _COMMAND_WRAPPERS
            ):
                wrapper_key = literal_unescaped if literal_unescaped else wrapper_key
            # Fallback wrappers are checked first: ``sudo`` is both a plain
            # command wrapper (operand semantics, option tables) and a
            # fallback name whose definition must be recorded for hosts
            # without ``sudo.exe``.
            fallback_wrapper = _FALLBACK_COMMAND_WRAPPERS.get(wrapper_key)
            if fallback_wrapper is not None:
                # ``gtimeout 5 rev``/``watch -n1 rev``/``sudo rev``: the
                # wrapper word is itself a fallback (its definition is
                # recorded) and the command that follows its
                # options/operands is scanned like a wrapped command word.
                self.names.append(wrapper_key)
                if executable_wrapper:
                    # The wrapping executable (``xargs gtimeout ...``) cannot
                    # invoke shell functions: swap the word for the standalone
                    # runner.  The operand scan continues through the wrapper
                    # state so the names the wrapped command uses are still
                    # recorded; the prefix exports them, which the runner's
                    # nested bash inherits.
                    self.edits.append(
                        (word_start, word_end, _wrapper_runner(wrapper_key))
                    )
                wrapper = _BashWrapper(
                    fallback_wrapper,
                    operands=_WRAPPER_OPERAND_COUNTS.get(fallback_wrapper, 0),
                )
                command_expected = True
                continue
            if wrapper_key in _COMMAND_WRAPPERS:
                wrapper = _BashWrapper(
                    wrapper_key,
                    operands=_WRAPPER_OPERAND_COUNTS.get(wrapper_key, 0),
                )
                command_expected = True
                command_word = self._literal_word_value(raw) or raw
                continue

            # Decode the word's literal value ONCE: the shell-wrapper, reparse
            # builtin, fallback-name, unsupported-name and command_word checks
            # below all need it, and each used to re-run the quote-removal
            # walk on the same raw text.
            literal = self._literal_word_value(raw)

            # Redundant shell invocation (``bash cd ...``, ``bash -c '...'``):
            # only at a plain command position — after ``command``/``env``/
            # ``sudo`` the shell word is the wrapped command itself and is
            # consumed by the wrapper logic above, so it is left untouched.
            shell_name = self._shell_wrapper_name_value(literal)
            if shell_name is not None:
                handled = self._handle_shell_wrapper(
                    shell_name,
                    word_start,
                    i,
                    end,
                    assignment_prefix=assignment_prefix,
                    wrapped=wrapper is not None,
                )
                if handled is not None:
                    i, keep_wrapper = handled
                    command_expected = True
                    redirect_expected = False
                    heredoc_operator = None
                    if not keep_wrapper:
                        wrapper = None
                    assignment_prefix = False
                    continue

                # The unwrap was refused (semantic options such as ``-e``,
                # trailing argv, ...): the nested shell still runs, so fix a
                # ``-c`` inline script operand in place.
                script_end = self._fix_shell_c_script_in_place(i, end)
                if script_end is not None:
                    i = script_end
                    command_expected = False
                    wrapper = None
                    reparse_operands = 0
                    continue

            # Record the command word of this simple command so argument-
            # position decisions (e.g. ``find -exec sh -c '...'``) know what
            # program will consume the words that follow.
            command_word = literal if literal is not None else raw
            pattern_family = (
                command_word
                if command_word in _PATTERN_PROGRAM_COMMANDS
                else None
            )
            pattern_pending = None
            pattern_bare = 0

            reparse_count = self._reparse_builtin_name_value(literal)
            if reparse_count is not None:
                # ``eval``/``trap``: the operand words that follow are
                # re-parsed as shell code at runtime; scan the quoted ones as
                # inline scripts so ``> nul`` inside them is rewritten too.
                reparse_operands = reparse_count
                reparse_leading = True
                command_expected = False
                wrapper = None
                assignment_prefix = False
                continue

            fallback_name = self._literal_command_name_value(literal)
            if fallback_name is not None:
                self.names.append(fallback_name)
                if executable_wrapper:
                    self.edits.append(
                        (word_start, word_end, _wrapper_runner(fallback_name))
                    )
            elif self._record_cmd_jump(raw, literal, word_end, end):
                pass
            elif self._record_unsupported(raw, literal):
                pass
            else:
                # A command word can itself be a Windows executable path
                # (``C:\tools\rg.exe``) or a Git Bash virtual absolute path
                # (``/c/tools/rg.exe``); Bash quote removal would eat the
                # backslashes and lose the command, so rewrite it like an
                # argument path.
                replacement = self._path_replacement(raw)
                if replacement is not None:
                    self.edits.append((word_start, word_end, replacement))
                    self.path_notes.append(raw)
            command_expected = False
            wrapper = None

    def _record_unsupported(self, raw: str, name: object = _UNDECODED) -> bool:
        """Record a command word with no Windows Git Bash equivalent.

        Returns True when *raw* literal-resolved to an unsupported command
        name; the command text is left untouched so the app layer can surface
        ``_UNSUPPORTED_BODIES``' reason instead of executing a guaranteed
        "command not found".  *name* may carry the already-decoded literal
        value (``None`` included); the ``_UNDECODED`` sentinel decodes *raw*.
        """
        if name is _UNDECODED:
            name = self._literal_unsupported_name(raw)
        if name is None or name not in _UNSUPPORTED_BODIES:
            return False
        if name not in self.unsupported:
            self.unsupported.append(name)
        return True

    def _record_cmd_jump(
        self, raw: str, literal: object, word_end: int, end: int
    ) -> bool:
        """Record an untranslatable cmd.exe batch jump at command position.

        ``goto``, glued ``:label`` lines, ``call :label`` / ``call x.bat`` and
        ``exit /b`` have no faithful bash equivalent (design
        ``.kimix_cache/cmd_control_flow_design.md`` §3.3): arbitrary jumps
        would run linearly past the target, and a translated ``exit N``
        kills the persistent session / the interactive ``exec bash -i``
        startup.  They are refused through the existing unsupported channel
        (``_UNSUPPORTED_BODIES`` → ``BashFix.unsupported`` → the Bash tool
        refuses before spawning), so the command text stays byte-for-byte.
        Returns True when a jump was recorded; ``literal`` is the already
        decoded command-word value (``None`` allowed).

        Phase 6: the decision + refusal-key append lives in
        :func:`kimix_native.shell_compat.cmd_translator.record_cmd_jump` (shared
        with the IR path); ``self`` is the reader so ``_peek_literal_word`` is
        this scanner's own.  Lazy import (cmd_translator imports bash_core at
        load).
        """
        from kimix_native.shell_compat.cmd_translator import record_cmd_jump

        return record_cmd_jump(self, self.s, raw, literal, word_end, end)

    def _peek_literal_word(self, pos: int, end: int) -> str | None:
        """Return the decoded literal of the word at ``pos`` or ``None``.

        Horizontal whitespace only; read without recording substitution
        edits (validation-only peek).  Returns ``None`` on no word or a
        non-decodable (runtime-expansion) word.  Phase 6: the rule lives in
        :func:`kimix_native.shell_compat.cmd_translator.peek_literal_word`
        (shared with the IR path).  Thin delegate passing ``self`` as the
        reader.  Lazy import.
        """
        from kimix_native.shell_compat.cmd_translator import peek_literal_word

        return peek_literal_word(self, self.s, pos, end)

    def _read_word(
        self, start: int, end: int, *, scan_substitutions: bool = True
    ) -> int:
        s = self.s
        i = start
        search = _WORD_SPECIAL_RE.search
        while i < end:
            # Bulk-skip plain word characters (C-level regex search): only the
            # special set can end a word or open a quoted/expanded region.
            m = search(s, i, end)
            if m is None:
                return end
            i = m.start()
            ch = s[i]
            if ch in _WORD_END_CHARS:
                break
            if ch == "#":
                if i == start:
                    break
                i += 1
                continue
            if ch == "\\":
                i += 2 if i + 1 < end else 1
                continue
            if ch == "'":
                i = self._skip_single_quote(i + 1, end)
                continue
            if ch == '"':
                if scan_substitutions:
                    i = self._skip_double_quote(i + 1, end)
                else:
                    i = self._skip_double_quote_for_matching(i + 1, end)
                continue
            if ch == "`":
                close = self._find_backtick_end(i + 1, end)
                if scan_substitutions:
                    self._scan_range(i + 1, close)
                i = close + 1 if close < end else end
                continue
            # ch == "$"
            nxt = s[i + 1] if i + 1 < end else ""
            if nxt == "(":
                if s.startswith("$((", i):
                    i = self._skip_arithmetic(i + 3, end)
                    continue
                close = self._find_matching(i + 2, end, ")")
                if scan_substitutions:
                    self._scan_range(i + 2, close if close < end else end)
                i = close + 1 if close < end else end
                continue
            if nxt == "{":
                if scan_substitutions:
                    i = self._skip_parameter(i + 2, end)
                else:
                    i = self._skip_parameter_literal(i + 2, end)
                continue
            if nxt == "'":
                i = self._skip_ansi_quote(i + 2, end)
                continue
            i += 1
        return i

    def _skip_single_quote(self, i: int, end: int) -> int:
        close = self.s.find("'", i, end)
        return end if close < 0 else close + 1

    def _skip_ansi_quote(self, i: int, end: int) -> int:
        s = self.s
        while i < end:
            if s[i] == "\\":
                i += 2 if i + 1 < end else 1
            elif s[i] == "'":
                return i + 1
            else:
                i += 1
        return end

    def _skip_double_quote(self, i: int, end: int) -> int:
        s = self.s
        while i < end:
            # Bulk-skip plain content: only '\\', '"', '`' and '$' are special
            # inside double quotes, so a C-level regex search jumps over runs of
            # ordinary characters instead of testing them one at a time.
            m = _DQ_SPECIAL_RE.search(s, i, end)
            if m is None:
                return end
            i = m.start()
            ch = s[i]
            if ch == "\\":
                if i + 1 < end and s[i + 1] in '$`"\\\n':
                    i += 2
                else:
                    i += 1
            elif ch == '"':
                return i + 1
            elif ch == "`":
                close = self._find_backtick_end(i + 1, end)
                self._scan_range(i + 1, close)
                i = close + 1 if close < end else end
            elif s.startswith("$(", i) and not s.startswith("$((", i):
                close = self._find_matching(i + 2, end, ")")
                self._scan_range(i + 2, close if close < end else end)
                i = close + 1 if close < end else end
            elif s.startswith("${", i):
                i = self._skip_parameter(i + 2, end)
            else:
                i += 1
        return end

    def _scan_array_words(self, i: int, end: int) -> None:
        """Scan array literal elements as data words.

        Elements are data, not commands: substitutions inside them are
        scanned as their own command contexts (``_read_word`` handles
        ``$( ... )`` and backquotes), and unquoted words get the same
        Windows path rewrite as ordinary arguments — Bash quote removal
        would otherwise eat their backslashes (``arr=(D:\\x\\y)`` would
        store ``D:xy``).
        """
        s = self.s
        while i < end:
            ch = s[i]
            if ch in " \t\r\n":
                i += 1
                continue
            if ch == "\\" and i + 1 < end and s[i + 1] == "\n":
                i += 2
                continue
            if ch == "#" and self._comment_starts(i, 0):
                newline = s.find("\n", i + 1, end)
                i = end if newline < 0 else newline
                continue
            word_end = self._read_word(i, end)
            if word_end <= i:
                i += 1
                continue
            raw = s[i:word_end]
            replacement = self._path_replacement(raw)
            if replacement is not None:
                self.edits.append((i, word_end, replacement))
                self.path_notes.append(raw)
            i = word_end

    def _scan_expansions(self, i: int, end: int) -> None:
        """Scan executable substitutions in a region whose plain words are data."""
        s = self.s
        search = _EXPANSION_SPECIAL_RE.search
        while i < end:
            m = search(s, i, end)
            if m is None:
                return
            i = m.start()
            ch = s[i]
            if ch == "\\":
                i += 2 if i + 1 < end else 1
            elif ch == "$" and s.startswith("$'", i):
                i = self._skip_ansi_quote(i + 2, end)
            elif ch == "'":
                i = self._skip_single_quote(i + 1, end)
            elif ch == '"':
                i = self._skip_double_quote(i + 1, end)
            elif ch == "`":
                close = self._find_backtick_end(i + 1, end)
                self._scan_range(i + 1, close)
                i = close + 1 if close < end else end
            elif ch == "$" and s.startswith("$(", i) and not s.startswith("$((", i):
                close = self._find_matching(i + 2, end, ")")
                self._scan_range(i + 2, close if close < end else end)
                i = close + 1 if close < end else end
            elif ch == "$" and s.startswith("$((", i):
                i = self._skip_arithmetic(i + 3, end)
            elif ch == "$" and s.startswith("${", i):
                i = self._skip_parameter(i + 2, end)
            else:
                i += 1

    def _scan_heredoc_expansions(self, i: int, end: int) -> None:
        """Scan substitutions in an expanding heredoc body.

        Quote characters are literal in heredoc bodies; only a backslash can
        suppress the expansion introducers that Bash recognizes there.
        """
        s = self.s
        search = _HEREDOC_EXPANSION_SPECIAL_RE.search
        while i < end:
            m = search(s, i, end)
            if m is None:
                return
            i = m.start()
            ch = s[i]
            if ch == "\\":
                i += 2 if i + 1 < end else 1
            elif ch == "`":
                close = self._find_backtick_end(i + 1, end)
                self._scan_range(i + 1, close)
                i = close + 1 if close < end else end
            elif ch == "$" and s.startswith("$(", i) and not s.startswith("$((", i):
                close = self._find_matching(i + 2, end, ")")
                self._scan_range(i + 2, close if close < end else end)
                i = close + 1 if close < end else end
            elif ch == "$" and s.startswith("$((", i):
                i = self._skip_arithmetic(i + 3, end)
            elif ch == "$" and s.startswith("${", i):
                i = self._skip_parameter(i + 2, end)
            else:
                i += 1

    def _skip_conditional(self, i: int, end: int) -> int:
        """Skip a ``[[ ... ]]`` expression while scanning its substitutions."""
        s = self.s
        search = _CONDITIONAL_SPECIAL_RE.search
        while i < end:
            m = search(s, i, end)
            if m is None:
                return end
            i = m.start()
            ch = s[i]
            if ch == "]" and s.startswith("]]", i):
                return i + 2
            if ch == "\\":
                i += 2 if i + 1 < end else 1
            elif ch == "$" and s.startswith("$'", i):
                i = self._skip_ansi_quote(i + 2, end)
            elif ch == "'":
                i = self._skip_single_quote(i + 1, end)
            elif ch == '"':
                i = self._skip_double_quote(i + 1, end)
            elif ch == "`":
                close = self._find_backtick_end(i + 1, end)
                self._scan_range(i + 1, close)
                i = close + 1 if close < end else end
            elif ch == "$" and s.startswith("$(", i) and not s.startswith("$((", i):
                close = self._find_matching(i + 2, end, ")")
                self._scan_range(i + 2, close if close < end else end)
                i = close + 1 if close < end else end
            elif ch == "$" and s.startswith("$((", i):
                i = self._skip_arithmetic(i + 3, end)
            else:
                i += 1
        return end

    def _skip_parameter_literal(self, i: int, end: int) -> int:
        s = self.s
        depth = 1
        search = _PARAMETER_LITERAL_SPECIAL_RE.search
        while i < end:
            m = search(s, i, end)
            if m is None:
                return end
            i = m.start()
            if s[i] == "\\":
                i += 2 if i + 1 < end else 1
            elif s[i] == "'":
                i = self._skip_single_quote(i + 1, end)
            elif s[i] == '"':
                i = self._skip_double_quote_for_matching(i + 1, end)
            elif s[i] == "{":
                depth += 1
                i += 1
            elif s[i] == "}":
                depth -= 1
                i += 1
                if depth == 0:
                    return i
            else:
                i += 1
        return end

    def _skip_parameter(self, i: int, end: int) -> int:
        s = self.s
        depth = 1
        search = _PARAMETER_SPECIAL_RE.search
        while i < end:
            m = search(s, i, end)
            if m is None:
                return end
            i = m.start()
            ch = s[i]
            if ch == "\\":
                i += 2 if i + 1 < end else 1
            elif ch == "$" and s.startswith("$(", i) and not s.startswith("$((", i):
                close = self._find_matching(i + 2, end, ")")
                self._scan_range(i + 2, close if close < end else end)
                i = close + 1 if close < end else end
            elif ch == "'":
                i = self._skip_single_quote(i + 1, end)
            elif ch == '"':
                i = self._skip_double_quote(i + 1, end)
            elif ch == "{":
                depth += 1
                i += 1
            elif ch == "}":
                depth -= 1
                i += 1
                if depth == 0:
                    return i
            else:
                i += 1
        return end

    def _skip_arithmetic(self, i: int, end: int) -> int:
        s = self.s
        depth = 1
        search = _ARITHMETIC_SPECIAL_RE.search
        while i < end:
            m = search(s, i, end)
            if m is None:
                return end
            i = m.start()
            ch = s[i]
            if ch == "$" and s.startswith("$(", i) and not s.startswith("$((", i):
                close = self._find_matching(i + 2, end, ")")
                self._scan_range(i + 2, close if close < end else end)
                i = close + 1 if close < end else end
            elif ch == "(" and s.startswith("((", i):
                depth += 1
                i += 2
            elif ch == ")" and s.startswith("))", i):
                depth -= 1
                i += 2
                if depth == 0:
                    return i
            elif ch == "\\":
                i += 2 if i + 1 < end else 1
            elif ch == "'":
                i = self._skip_single_quote(i + 1, end)
            elif ch == '"':
                i = self._skip_double_quote(i + 1, end)
            else:
                i += 1
        return end

    def _find_backtick_end(self, i: int, end: int) -> int:
        s = self.s
        search = _BACKTICK_SPECIAL_RE.search
        while i < end:
            m = search(s, i, end)
            if m is None:
                return end
            i = m.start()
            if s[i] == "\\":
                i += 2 if i + 1 < end else 1
            else:
                return i
        return end

    def _find_matching(self, i: int, end: int, closing: str) -> int:
        """Find the position of the bracket matching the one at ``i - 2``.

        Recursion is bounded by :data:`_MAX_NESTING_DEPTH`; beyond it the
        region is treated as unmatched so the caller skips it for Bash.
        """
        if i >= end:
            return end
        if self.nest_depth >= _MAX_NESTING_DEPTH:
            return end
        self.nest_depth += 1
        try:
            return self._find_matching_inner(i, end, closing)
        finally:
            self.nest_depth -= 1

    def _find_matching_inner(self, i: int, end: int, closing: str) -> int:
        s = self.s
        depth = 0
        pending_heredocs: list[_BashHereDoc] = []
        case_stack: list[str] = []
        while i < end:
            ch = s[i]
            if ch == "\\":
                i += 2 if i + 1 < end else 1
            elif ch == "\n":
                i += 1
                if pending_heredocs:
                    i = self._skip_heredoc_bodies(
                        i, end, pending_heredocs, scan_expansions=False
                    )
                    pending_heredocs.clear()
            elif ch == "$" and s.startswith("$((", i):
                i = self._skip_arithmetic(i + 3, end)
                if case_stack and case_stack[-1] == "word":
                    # Arithmetic expansion can be the case subject word
                    # (``case $((...)) in ...``); it ends the header just
                    # like a plain subject word.
                    case_stack[-1] = "await-in"
            elif ch == "<" and s.startswith("<<", i) and not s.startswith("<<<", i):
                strip_tabs = s.startswith("<<-", i)
                delimiter_start = i + (3 if strip_tabs else 2)
                while delimiter_start < end and s[delimiter_start] in " \t\r":
                    delimiter_start += 1
                delimiter_end = self._read_word(
                    delimiter_start, end, scan_substitutions=False
                )
                heredoc = self._heredoc_delimiter(s[delimiter_start:delimiter_end])
                if heredoc is not None:
                    delimiter, expands = heredoc
                    pending_heredocs.append(_BashHereDoc(delimiter, strip_tabs, expands))
                i = delimiter_end if delimiter_end > delimiter_start else delimiter_start
            elif ch == "'":
                i = self._skip_single_quote(i + 1, end)
            elif ch == '"':
                i = self._skip_double_quote_for_matching(i + 1, end)
            elif ch == "`":
                close = self._find_backtick_end(i + 1, end)
                i = close + 1 if close < end else end
                if case_stack and case_stack[-1] == "word":
                    # A backquote substitution can be the case subject word
                    # (``case `...` in ...``); it ends the header just like
                    # a plain subject word.
                    case_stack[-1] = "await-in"
            elif ch == "#" and self._comment_starts(i, 0):
                newline = s.find("\n", i + 1, end)
                i = end if newline < 0 else newline
            elif ch == ";" and s.startswith(";;&", i):
                if case_stack:
                    case_stack[-1] = "patterns"
                i += 3
            elif ch == ";" and (s.startswith(";;", i) or s.startswith(";&", i)):
                if case_stack:
                    case_stack[-1] = "patterns"
                i += 2
            elif ch not in _WORD_END_CHARS:
                word_end = self._read_word(i, end, scan_substitutions=False)
                if word_end <= i:
                    i += 1
                    continue
                word = s[i:word_end]
                if word == "case":
                    case_stack.append("word")
                elif (
                    case_stack
                    and case_stack[-1] in {"patterns", "body"}
                    and word == "esac"
                ):
                    case_stack.pop()
                elif case_stack and case_stack[-1] == "word":
                    case_stack[-1] = "await-in"
                elif case_stack and case_stack[-1] == "await-in" and word == "in":
                    case_stack[-1] = "patterns"
                i = word_end
            elif ch == "(":
                depth += 1
                i += 1
            elif ch == closing:
                if case_stack and case_stack[-1] == "patterns":
                    case_stack[-1] = "body"
                    i += 1
                elif depth == 0:
                    return i
                else:
                    depth -= 1
                    i += 1
            else:
                i += 1
        return end

    def _skip_double_quote_for_matching(self, i: int, end: int) -> int:
        s = self.s
        search = _DQ_SPECIAL_RE.search
        while i < end:
            m = search(s, i, end)
            if m is None:
                return end
            i = m.start()
            ch = s[i]
            if ch == "\\":
                if i + 1 < end and s[i + 1] in '$`"\\\n':
                    i += 2
                else:
                    i += 1
            elif ch == '"':
                return i + 1
            elif ch == "`":
                close = self._find_backtick_end(i + 1, end)
                i = close + 1 if close < end else end
            elif s.startswith("$(", i) and not s.startswith("$((", i):
                close = self._find_matching(i + 2, end, ")")
                i = close + 1 if close < end else end
            else:
                i += 1
        return end

    def _read_control_operator(self, i: int, end: int) -> tuple[str, int]:
        s = self.s
        ch = s[i]
        nxt = s[i + 1 : i + 2]
        if ch == ";":
            if nxt == ";":
                if s[i + 2 : i + 3] == "&":
                    return ";;&", i + 3
                return ";;", i + 2
            if nxt == "&":
                return ";&", i + 2
            return ";", i + 1
        if ch == "&":
            if nxt == "&":
                return "&&", i + 2
            return "&", i + 1
        if ch == "|":
            if nxt == "|":
                return "||", i + 2
            if nxt == "&":
                return "|&", i + 2
            return "|", i + 1
        if ch == "(" or ch == ")":
            return ch, i + 1
        return "", i

    def _read_redirection(self, i: int, end: int) -> tuple[str, int]:
        m = _REDIRECTION_RE.match(self.s, i, end)
        if m is None:
            return "", i
        return m.group(), m.end()

    def _redirection_after_fd(self, i: int, end: int) -> bool:
        s = self.s
        while i < end and s[i].isdigit():
            i += 1
        return i < end and s[i] in _REDIRECTION_START

    def _comment_starts(self, i: int, range_start: int) -> bool:
        if i <= range_start:
            return True
        return self.s[i - 1] in " \t\r\n;&|()<>"

    def _empty_parentheses_end(self, i: int, end: int) -> int | None:
        s = self.s
        while i < end and s[i] in " \t\r":
            i += 1
        if i >= end or s[i] != "(":
            return None
        i += 1
        while i < end and s[i] in " \t\r":
            i += 1
        return i + 1 if i < end and s[i] == ")" else None

    def _function_declaration_end(self, raw: str, i: int, end: int) -> int | None:
        # ``_NAME_RE`` requires a leading letter/underscore; that one-char test
        # rejects most words without running the regex.
        if not raw or (raw[0] not in _NAME_START_CHARS):
            return None
        if not _NAME_RE.fullmatch(raw):
            return None
        return self._empty_parentheses_end(i, end)

    @staticmethod
    def _classify_pattern_option(family: str, word: str) -> str | None:
        """Classify an option word of a pattern-consuming command.

        Phase 5: the classification lives in
        :func:`kimix_native.shell_compat.wrappers.classify_pattern_option`
        (shared with the IR path; lazy import — wrappers imports bash_core
        at load).  Returns one of ``"data-pending"`` / ``"path-pending"`` /
        ``"value-pending"`` / ``"data"`` / ``"path"`` / ``None``.
        """
        from kimix_native.shell_compat.wrappers import classify_pattern_option

        return classify_pattern_option(family, word)

    def _consume_wrapper_word(self, wrapper: _BashWrapper, raw: str) -> str:
        """Consume one wrapper argument word (Phase 5: thin delegate).

        The state machine itself lives in
        :func:`kimix_native.shell_compat.wrappers.wrapper_step` (the LIVE and
        the IR path share ONE implementation).  ``_BashWrapper`` and the IR
        ``WrapperState`` expose the same attribute names, so the scanner
        passes ``wrapper`` directly; the word decoder is the scanner's own
        ``_literal_word_value``.  The import is lazy (wrappers imports
        bash_core at load; bash_core must NOT import wrappers at load).
        """
        from kimix_native.shell_compat.wrappers import wrapper_step

        return wrapper_step(wrapper, raw, _BashFixScanner._literal_word_value)

    def _coproc_name_before_compound(self, raw: str, i: int, end: int) -> bool:
        if not _NAME_RE.fullmatch(raw):
            return False
        while i < end and self.s[i] in " \t\r":
            i += 1
        if i >= end:
            return False
        if self.s.startswith(("{", "(", "[[", "(("), i):
            return True
        for keyword in ("case", "for", "if", "select", "until", "while"):
            keyword_end = i + len(keyword)
            if self.s.startswith(keyword, i) and (
                keyword_end >= end
                or self.s[keyword_end] in " \t\r\n;&|()<>{}"
            ):
                return True
        return False

    def _heredoc_delimiter(self, raw: str) -> tuple[str | None, bool] | None:
        if not raw:
            return None
        result: list[str] = []
        quoted = False
        matchable = True
        i = 0
        while i < len(raw):
            ch = raw[i]
            if raw.startswith("$'", i):
                quoted = True
                value, i, valid = self._read_ansi_c_delimiter(raw, i + 2)
                result.append(value)
                matchable = matchable and valid
            elif ch == "'":
                quoted = True
                close = raw.find("'", i + 1)
                if close < 0:
                    result.append(raw[i + 1 :])
                    i = len(raw)
                else:
                    result.append(raw[i + 1 : close])
                    i = close + 1
            elif ch == '"' or raw.startswith('$"', i):
                # A plain ``"..."`` run and the locale form ``$"..."`` both
                # quote the delimiter text (locale translation never changes
                # it on Git Bash); the ``$`` of the locale form is consumed
                # with the opener so the effective delimiter is not prefixed
                # with a stray ``$``.
                quoted = True
                i += 2 if ch != '"' else 1
                while i < len(raw) and raw[i] != '"':
                    if raw[i] == "\\" and i + 1 < len(raw):
                        escaped = raw[i + 1]
                        if escaped in '$`"\\\n':
                            if escaped != "\n":
                                result.append(escaped)
                            i += 2
                            continue
                    result.append(raw[i])
                    i += 1
                if i < len(raw):
                    i += 1
            elif ch == "\\" and i + 1 < len(raw):
                quoted = True
                result.append(raw[i + 1])
                i += 2
            else:
                result.append(ch)
                i += 1
        delimiter = "".join(result) if matchable else None
        return delimiter, not quoted

    def _read_ansi_c_delimiter(
        self, raw: str, i: int
    ) -> tuple[str, int, bool]:
        result: list[str] = []
        valid = True
        # Bash strings are C strings: the delimiter value truncates at the
        # first NUL escape (same rule as _ansi_c_literal_span).  The scan
        # still runs to the closing quote for the correct end index.
        nul_seen = False
        simple = {
            "a": "\a",
            "b": "\b",
            "e": "\x1b",
            "E": "\x1b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
            "v": "\v",
            "\\": "\\",
            "'": "'",
            '"': '"',
            "?": "?",
        }
        while i < len(raw):
            if raw[i] == "'":
                return "".join(result), i + 1, valid
            if raw[i] != "\\" or i + 1 >= len(raw):
                if not nul_seen:
                    result.append(raw[i])
                i += 1
                continue
            escape = raw[i + 1]
            if escape in simple:
                if not nul_seen:
                    result.append(simple[escape])
                i += 2
                continue
            if escape in "01234567":
                j = i + 1
                while j < len(raw) and j < i + 4 and raw[j] in "01234567":
                    j += 1
                if int(raw[i + 1 : j], 8) == 0:
                    nul_seen = True
                elif not nul_seen:
                    result.append(chr(int(raw[i + 1 : j], 8)))
                i = j
                continue
            if escape in "xXuU":
                widths = {"x": 2, "X": 2, "u": 4, "U": 8}
                j = i + 2
                limit = min(len(raw), j + widths[escape])
                while j < limit and raw[j] in "0123456789abcdefABCDEF":
                    j += 1
                if j > i + 2:
                    value = int(raw[i + 2 : j], 16)
                    if value == 0:
                        nul_seen = True
                    elif value <= 0x10FFFF and not 0xD800 <= value <= 0xDFFF:
                        if not nul_seen:
                            result.append(chr(value))
                    else:
                        # Bash accepts byte sequences outside Python's Unicode
                        # scalar range.  Python text cannot represent the same
                        # delimiter, so mark it unmatchable and conservatively
                        # keep all remaining source inside the heredoc.
                        valid = False
                        result.append(raw[i:j])
                    i = j
                    continue
            if not nul_seen:
                result.append("\\" + escape)
            i += 2
        return "".join(result), i, valid

    def _skip_heredoc_bodies(
        self,
        i: int,
        end: int,
        documents: list[_BashHereDoc],
        *,
        scan_expansions: bool = True,
    ) -> int:
        s = self.s
        redir_line_end = -1
        for document in documents:
            body_start = i
            if redir_line_end < 0:
                redir_line_end = body_start - 1
            logical_line = ""
            logical_start = i
            while i < end:
                newline = s.find("\n", i, end)
                line_end = end if newline < 0 else newline
                # A CRLF source leaves a trailing ``\r`` on every line; strip it
                # so the terminator line matches the delimiter byte-for-byte
                # (otherwise the closing ``EOF\r`` never ends the heredoc and
                # the trailing-operator repair never runs).  The stored body is
                # not mutated — this only feeds the delimiter/continuation test.
                line = s[i:line_end].rstrip("\r")
                compare = line.lstrip("\t") if document.strip_tabs else line
                if not logical_line:
                    logical_start = i
                if document.expands and self._heredoc_line_continues(compare):
                    logical_line += compare[:-1]
                    i = end if newline < 0 else newline + 1
                    continue
                logical_line += compare
                if (
                    document.delimiter is not None
                    and logical_line == document.delimiter
                ):
                    if scan_expansions and document.expands:
                        self._scan_heredoc_expansions(body_start, logical_start)
                    i = end if newline < 0 else newline + 1
                    break
                logical_line = ""
                i = end if newline < 0 else newline + 1
            else:
                if scan_expansions and document.expands:
                    self._scan_heredoc_expansions(body_start, end)
        if redir_line_end >= 0:
            self.heredoc_events.append((redir_line_end, i))
        return i

    @staticmethod
    def _heredoc_line_continues(line: str) -> bool:
        trailing = len(line) - len(line.rstrip("\\"))
        return trailing % 2 == 1

    # ── Windows path normalization for Git Bash ─────────────────────────────

    def _drop_cmd_cd_flag(self, i: int, end: int) -> None:
        """Drop the cmd.exe-only ``cd /d <path>`` flag form.

        Bash ``cd`` accepts a single argument, so ``cd /d D:\\x`` fails with
        "too many arguments".  The flag is deleted only when a path argument
        actually follows it on the same line; bare ``cd /d`` stays untouched.

        Phase 4: the decision (the ``/d`` flag-word span, or ``None``) lives in
        :func:`kimix_native.shell_compat.paths.cd_flag_span_after`, which this
        method calls with ``self`` as the word reader so the legacy
        ``_read_word`` / ``_redirection_after_fd`` boundaries are byte-identical;
        the edit + note are applied here.  The import is lazy (bash_core does
        not import paths at module load) to avoid a load-time cycle.
        """
        from kimix_native.shell_compat.paths import cd_flag_span_after

        span = cd_flag_span_after(self, self.s, i, end)
        if span is None:
            return
        self.edits.append((span.start, span.end, ""))
        self.path_notes.append("cd /d")

    def _windows_path_replacement(self, raw: str) -> str | None:
        """Return the Git Bash spelling of a Windows backslash path word.

        Phase 4: the rule lives in
        :func:`kimix_native.shell_compat.paths.windows_path_replacement`; this
        is a thin delegate that keeps the name on the live scanner (and the
        historical method dispatch).  See paths.py for the full contract.
        """
        from kimix_native.shell_compat.paths import windows_path_replacement

        return windows_path_replacement(raw)

    def _git_bash_abs_path_replacement(self, raw: str) -> str | None:
        """Return the native Windows spelling of a Git Bash virtual absolute path.

        Phase 4: the rule lives in
        :func:`kimix_native.shell_compat.paths.git_bash_abs_path_replacement`;
        the live scanner delegates here (temp dir ``None`` -> that function
        reads the temp directory through the same late-binding resolver
        :func:`_windows_temp_dir_from_namespace`, honoring a facade monkeypatch
        of ``_windows_temp_dir``).  See paths.py for the full contract.
        """
        from kimix_native.shell_compat.paths import git_bash_abs_path_replacement

        return git_bash_abs_path_replacement(raw)

    def _quote_virtual_path(self, normalized: str) -> str | None:
        """Quote a rewritten virtual path without disabling shell expansions.

        Phase 4 delegate to :func:`kimix_native.shell_compat.paths.quote_virtual_path`.
        """
        from kimix_native.shell_compat.paths import quote_virtual_path

        return quote_virtual_path(normalized)

    def _path_replacement(self, raw: str) -> str | None:
        """Return the Git Bash spelling for a Windows path word.

        Phase 4 delegate to :func:`kimix_native.shell_compat.paths.path_replacement`.
        """
        from kimix_native.shell_compat.paths import path_replacement

        return path_replacement(raw)

    @staticmethod
    def _plausible_path_segments(raw: str) -> bool:
        """Require at least one segment that looks like a real directory name.

        Phase 4 delegate to :func:`kimix_native.shell_compat.paths.plausible_path_segments`.
        """
        from kimix_native.shell_compat.paths import plausible_path_segments

        return plausible_path_segments(raw)

    @staticmethod
    def _decode_unquoted_word(raw: str) -> str:
        """Return the word value after Bash quote removal (unquoted form).

        Phase 4 delegate to :func:`kimix_native.shell_compat.paths.decode_unquoted_word`.
        """
        from kimix_native.shell_compat.paths import decode_unquoted_word

        return decode_unquoted_word(raw)

    @staticmethod
    def _normalize_windows_path(raw: str) -> str:
        """Rewrite backslashes as the forward slashes Git Bash understands.

        Phase 4 delegate to
        :func:`kimix_native.shell_compat.paths.normalize_windows_path`.
        """
        from kimix_native.shell_compat.paths import normalize_windows_path

        return normalize_windows_path(raw)

    def _quote_path_word(self, normalized: str) -> str:
        """Quote a normalized path only when unquoted emission would break it.

        Phase 4 delegate to
        :func:`kimix_native.shell_compat.paths.quote_path_word` (which keeps the
        same tilde-outside-quotes / glob-and-brace-outside-quotes policy).
        """
        from kimix_native.shell_compat.paths import quote_path_word

        return quote_path_word(normalized)


def bash_compatibility_prelude() -> str:
    """Return exported fallback definitions for a persistent Git Bash shell.

    Interactive input can be an incomplete Bash fragment (for example a
    heredoc body or the second half of a quote), so it must never be scanned
    and prefixed independently.  The interactive shell instead executes this
    prelude once and exports the fallback functions across ``exec bash -i``.
    """
    if sys.platform != "win32":
        return ""
    definitions = "\n".join(_FALLBACKS.values())
    exports = "\n".join(
        f"if declare -F {name} >/dev/null; then export -f {name}; fi"
        for name in _FALLBACKS
    )
    return definitions + "\n" + exports


def _read_shell_control_operator(s: str, i: int, n: int) -> tuple[str, int]:
    """Return the control operator at position *i* and the index after it."""
    if i >= n:
        return "", i
    ch = s[i]
    if ch == ";":
        if s.startswith(";;&", i):
            return ";;&", i + 3
        if s.startswith(";;", i):
            return ";;", i + 2
        if s.startswith(";&", i):
            return ";&", i + 2
        return ";", i + 1
    if ch == "&":
        if s.startswith("&&", i):
            return "&&", i + 2
        return "&", i + 1
    if ch == "|":
        if s.startswith("||", i):
            return "||", i + 2
        if s.startswith("|&", i):
            return "|&", i + 2
        return "|", i + 1
    return "", i


# Phase 7: the heredoc trailing-operator constant lives in
# ``kimix_native.shell_compat.heredoc`` (moved BYTE-FOR-BYTE together with the
# two repair functions; the historical names below are thin lazy delegates kept
# on ``bash_core`` because they are part of the 195-name ``_shell_compat``
# facade ``__all__`` and the scanner call sites).  This import sits AFTER
# ``_read_shell_control_operator`` is defined: the moved
# ``_apply_heredoc_operator_move`` resolves that helper (and the scanner class
# for the no-events legacy rescan) LAZILY from ``bash_core`` inside its body,
# so whichever module loads first, both sides resolve at call time — the
# Phase-3/4/5/6 lazy-delegate cycle pattern.
from kimix_native.shell_compat.heredoc import (  # noqa: E402
    _HEREDOC_TRAILING_OPERATORS,
)


# Phase 7: the heredoc trailing-operator constants and repairs live in
# ``kimix_native.shell_compat.heredoc`` (moved BYTE-FOR-BYTE); these are thin
# delegates that keep the historical names on ``bash_core`` — they are part of
# the 195-name ``_shell_compat`` facade ``__all__`` and every intra-module call
# site (the scanner's ``_build_fixed_source``, ``fix_bash_command``).  The lazy
# function-body import keeps the load-time cycle (bash_core -> heredoc ->
# bash_core) broken, exactly the Phase-3/4/5/6 pattern.
def _apply_heredoc_operator_move(
    source: str, redir_line_end: int, terminator_end: int
) -> str:
    """Phase 7 delegate to :func:`kimix_native.shell_compat.heredoc._apply_heredoc_operator_move`."""
    from kimix_native.shell_compat.heredoc import (
        _apply_heredoc_operator_move as _impl,
    )

    return _impl(source, redir_line_end, terminator_end)


def _fix_heredoc_trailing_operators(
    source: str, events: list[tuple[int, int]] | None = None
) -> str:
    """Phase 7 delegate to :func:`kimix_native.shell_compat.heredoc._fix_heredoc_trailing_operators`."""
    from kimix_native.shell_compat.heredoc import (
        _fix_heredoc_trailing_operators as _impl,
    )

    return _impl(source, events)


def fix_bash_command(command: str) -> BashFix:
    """Rewrite selected native POSIX commands for Windows Git Bash.

    Phase 7 (Option A): the orchestration is the IR-owned :func:`translate`
    entry (plan §3.5).  This is a thin wrapper: ``translate`` builds the engine
    reader (the :class:`_BashFixScanner` compatibility adapter), drives the scan
    flow whose every rule DECISION already delegates to the IR modules
    (paths/wrappers/cmd_translator/heredoc), renders through the shared
    ``EditPlan``/``render_splices``/``fallback_prefix`` and applies the heredoc
    trailing-operator repair LAST via ``heredoc.py``.  Empty input returns
    ``BashFix(command)`` byte-for-byte; malformed input and depth overflow never
    raise.  The Windows-platform gate lives in the app layer
    (``kimi_cli.tools.file.bash.bash_fix.fix_bash_command``), which also does not
    gate on platform inside this kernel.
    """
    from kimix_native.shell_compat.translator import translate as _translate

    return _translate(command)


# ======================================================================
# bash_tool.py (kimi-agent) - _process_unquoted + helpers
# ======================================================================
_BASH_METACHARACTERS = frozenset("()|;&<>$\"`'\"*?[]{}~!#=% \t\n\r")

# In double quotes, \ only escapes these characters.  $ and ` are included
# because \$, \` inside "..." are literal (the $ / ` is escaped, not triggering
# variable expansion or command substitution).
_DQ_ESCAPED = frozenset(('"', '\\', '$', '`'))

# Precompiled regex for finding the next special character in unquoted mode.
# Matches backslash, single quote, double quote, dollar, or backtick.
_UNQUOTED_SPECIAL_RE = re.compile(r'[\\\'"$`]')
# A word that is an escaped/interleaved spelling of the Windows null device
# (``\nul``, ``n\ul``, ``nu\l``, any case): the backslash(s) are quote
# removal, so the word's value is exactly ``nul``.  ``_process_unquoted``
# preserves the escape pairs so ``fix_bash_command`` can see the effective
# word and rewrite the whole token to ``/dev/null``; converting the
# backslashes to ``/`` first would corrupt the word (``/nul`` is an absolute
# path, not the null device).
_ESCAPED_NUL_RE = re.compile(
    # The escaped word must be a *complete* word: it starts at a word
    # boundary and ends at one, so a real Windows path (``C:\\nul``) or a
    # longer escaped word (``a\\nul\\b``) keeps its normal backslash
    # conversion.
    r"(?:(?<=^)|(?<=[\s;|&<>(){}]))"
    r"\\?[nN]\\?[uU]\\?[lL]"
    r"(?![A-Za-z0-9_.\\])"
)


def _find_ansi_c_end(cmd: str, start: int) -> int:
    """Return the index AFTER the closing ' of a ``$'...'`` region.

    ``start`` is the position right after the opening ``$'`` (i.e. the first
    character inside the region).  Returns ``-1`` if the region is
    unterminated.  Inside ``$'...'`` every ``\\X`` pair is treated as an
    escape (any character after \\ is skipped over).
    """
    i = start
    length = len(cmd)
    while i < length:
        c = cmd[i]
        if c == "\\" and i + 1 < length:
            i += 2
        elif c == "'":
            return i + 1
        else:
            i += 1
    return -1


def _find_backtick_end(cmd: str, start: int) -> int:
    """Return the index AFTER the closing `` ` `` of a backtick region.

    ``start`` is the position right after the opening `` ` ``.
    Returns ``-1`` if the region is unterminated.  ``\\` `` inside the
    region is an escaped backtick (literal `` ` ``).
    """
    i = start
    length = len(cmd)
    while i < length:
        c = cmd[i]
        if c == "\\" and i + 1 < length:
            i += 2  # skip escaped char (including \`)
        elif c == "`":
            return i + 1
        else:
            i += 1
    return -1


def _find_matching_paren(cmd: str, open_pos: int) -> int:
    """Return the index of the ``)`` matching the ``(`` at ``cmd[open_pos]``.

    Returns ``-1`` if no matching ``)`` is found.  Tracks nested ``$(...)``,
    single-quoted regions, double-quoted regions (including their own
    nested ``$(...)`` and backticks), and backtick regions.
    """
    assert cmd[open_pos] == "("
    depth = 1
    i = open_pos + 1
    length = len(cmd)
    while i < length:
        c = cmd[i]
        if c == "'":
            end = cmd.find("'", i + 1)
            if end == -1:
                return -1
            i = end + 1
        elif c == '"':
            i = _find_dq_end(cmd, i + 1)
            if i == -1:
                return -1
        elif c == "`":
            i = _find_backtick_end(cmd, i + 1)
            if i == -1:
                return -1
        elif c == "$" and i + 1 < length and cmd[i + 1] == "(":
            depth += 1
            i += 2
        elif c == "$" and i + 1 < length and cmd[i + 1] == "'":
            # $'...' ANSI-C quoted region — skip to its closing '
            end = _find_ansi_c_end(cmd, i + 2)
            if end == -1:
                return -1
            i = end
        elif c == "(":
            # A plain parenthesis opens a subshell group: it must be balanced
            # by its own ``)`` before the substitution can close (``$( (ls)
            # ; cat x )``).  Without this the matcher closed the substitution
            # at the group's ``)`` and everything after the group was dropped
            # from the scan.
            depth += 1
            i += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
            i += 1
        else:
            i += 1
    return -1


def _find_dq_end(cmd: str, start: int) -> int:
    """Return the index AFTER the closing ``"`` of a double-quoted region.

    ``start`` is the position right after the opening ``"``.
    Returns ``-1`` if the region is unterminated.  Recognises ``\\X``
    escapes (``X`` in ``_DQ_ESCAPED``), nested ``$(...)``, ``$'...'``, and
    backtick command substitutions inside the region.
    """
    i = start
    length = len(cmd)
    while i < length:
        c = cmd[i]
        if c == "\\" and i + 1 < length and cmd[i + 1] in _DQ_ESCAPED:
            i += 2  # skip \X (X is escaped: ", \, $, `)
        elif c == '"':
            return i + 1
        elif c == "$" and i + 1 < length and cmd[i + 1] == "(":
            end = _find_matching_paren(cmd, i + 1)
            if end == -1:
                return -1
            i = end + 1
        elif c == "$" and i + 1 < length and cmd[i + 1] == "'":
            # Inside "..." bash does NOT start an ANSI-C quoting region at
            # $' (ANSI-C only applies outside double quotes): both characters
            # are ordinary literals here.  Treat the $ as ordinary so a stray
            # or unbalanced ' cannot derail the closing-quote search (which
            # would previously discard the whole region verbatim and lose
            # required rewrites of a real $(...) inside it).
            i += 1
        elif c == "`":
            end = _find_backtick_end(cmd, i + 1)
            if end == -1:
                return -1
            # _find_backtick_end returns the index AFTER the closing `
            i = end
        else:
            i += 1
    return -1


def _process_unquoted(cmd: str) -> str:
    """Convert unquoted backslashes to forward slashes in ``cmd``.

    Phase 3 migrated the canonical implementation to
    :mod:`kimix_native.shell_compat.unquoted` (lexer-driven emitter).  This
    module keeps the name in the bash_core namespace for intra-module
    callers and the ``_shell_compat`` facade; the import is resolved
    lazily to avoid a bash_core -> unquoted -> lexer -> bash_core cycle.
    """
    from kimix_native.shell_compat.unquoted import _process_unquoted as _impl

    return _impl(cmd)


