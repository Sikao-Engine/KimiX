r"""Compatibility classifier, fallback registration, wrapper, reparse and
redundant-shell-unwrap rules (Phase 5).

Phase 5 of the ``_shell_compat`` refactor (plan §3.4 "Compatibility
classification", Phase 5 actions 1-5).  The command classification,
fallback-registration, wrapper-operand, runtime-reparse and shell-unwrap
DECISIONS lived inline in :class:`kimix_native.shell_compat.bash_core._BashFixScanner`
(the main scan loop) and in its helper methods.  This module owns their
canonical implementation as pure, testable functions, and the LIVE scanner
delegates to them (thin lazy delegates at the historical names, exactly the
Phase-4 ``paths.py`` pattern), so there is ONE implementation per rule:

* ``classify_command`` — the explicit plan §3.4 classifier (``CommandClass``).
  The scanner keeps its own inline dispatch flow (recognition order is the
  scanning flow, which stays in ``bash_core``); this function is the IR-side
  statement of the same table memberships, built from the SAME tables.
* ``fallback_registration`` / ``unsupported_record`` / ``gnu_alias_target`` —
  the fallback/unsupported record lookups the scanner performs when a command
  word literal-resolves to a registered name (``netcat``/``htop`` are shared
  bodies; the ``g<tool>`` Homebrew aliases are materialised into the tables by
  ``fallback_data`` at import).
* ``wrapper_step`` — the ``_consume_wrapper_word`` state machine lifted to a
  pure function of ``(state, raw_word, decode)``: option operands
  (``_WRAPPER_OPTIONS_WITH_VALUE``), path-valued options
  (``_WRAPPER_PATH_OPTIONS`` / ``_WRAPPER_PATH_OPTION_LONG`` inline forms),
  ``env -S`` opaque handling, ``command -p``/``-v``, the GNU ``timeout``
  DURATION operand (``_WRAPPER_OPERAND_COUNTS``), ``xargs``/``sudo``/``watch``/
  ``nohup``/``stdbuf``/``nice``/``time``/``coproc``/``builtin``/``exec``.
* ``reparse_operand_index`` / ``shell_c_script_range`` — runtime-reparsed
  script detection: which operand word of ``eval``/``trap``/``watch``/shell
  ``-c`` (``bash``/``sh``/``dash``/``ash``, ``find -exec sh -c``,
  ``xargs sh -c``) carries the inline script, porting the scanner's option
  forms (``-lc``/``-cl`` clusters, ``-o VALUE -c``, ``--`` bail, trailing
  argv bail) byte-for-byte.
* ``unwrap_decision`` — the redundant-shell unwrap/keep decision porting the
  ``_handle_shell_wrapper`` rules (subshell/option/assignment forms
  preserved; kept ``-c`` scripts fixed in place; the expansion-bearing
  ``bash -c "rev $HOME"`` bail keeps the wrapper but harvests registrations).

The word-boundary primitives (``_read_word`` / ``_redirection_after_fd`` /
``_literal_word_value``) stay on the scanner; the span-based rules here are
parameterised over a reader object (the LIVE scanner passes ``self``) so the
IR wrappers (which build a scanner over the same source, exactly the Phase-4
``cd_flag_span_after`` pattern) and the live flow share one decision.

Data tables stay in ``fallback_data.py`` / ``bash_core.py`` (single source of
truth); this module imports and READS them, never copies.  ``bash_core``
imports this module LAZILY (inside the delegate bodies) to avoid a load-time
cycle, so this module imports ``bash_core`` eagerly at load, the safe
direction.
"""

from __future__ import annotations

import dataclasses
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Dict, FrozenSet, Optional, Tuple

# ── reused tables / helpers (canonical homes stay bash_core + fallback_data) ──
from kimix_native.shell_compat.bash_core import (
    _ASSIGNMENT_RE,
    _COMMAND_WRAPPERS,
    _EXEC_OPERAND_COMMANDS,
    _GLUED_LABEL_RE,
    _GREP_PATTERN_FAMILY,
    _GREP_VALUE_OPTIONS,
    _OPERATOR_CHARS,
    _PATTERN_PROGRAM_COMMANDS,
    _REPARSE_BUILTINS,
    _SHELL_C_OPTIONS,
    _SHELL_C_SCRIPT_OPTION_RE,
    _SHELL_VALUE_OPTIONS,
    _SHELL_WRAPPERS,
    _WRAPPER_OPERAND_COUNTS,
    _WRAPPER_OPTIONS_WITH_VALUE,
    _WRAPPER_PATH_OPTIONS,
    _WRAPPER_PATH_OPTION_LONG,
)
from kimix_native.shell_compat.fallback_data import (
    _FALLBACK_BODIES,
    _FALLBACK_COMMAND_WRAPPERS,
    _FALLBACKS,
    _UNSUPPORTED_BODIES,
)

if TYPE_CHECKING:  # pragma: no cover
    from kimix_native.shell_compat.models import CommandNode
    from kimix_native.shell_compat.spans import SourceSpan


# ── plan §3.4 command classification ──────────────────────────────────────────


class CommandClass(str, Enum):
    r"""Compatibility class of a command word (plan §3.4).

    ``GIT_BASH_PASSTHROUGH`` — known shell builtins / bundled Git Bash
    commands and ordinary command words: emit no change.
    ``FALLBACK_GUARDED`` — names in the fallback tables: record the fallback
    and prefix a guarded definition.
    ``UNSUPPORTED`` — names refused through the unsupported channel; the
    command text is preserved byte-for-byte.
      ``CMD_TRANSLATABLE`` — cmd.exe-shaped command words (the leading-``@``
      form, the ``if``/``for`` hook words) whose translation rules live in
      ``kimix_native.shell_compat.cmd_translator`` (Phase 6; the scanner cmd
      hooks delegate to it).
    ``WRAPPER_CONTEXT`` — names that open a wrapper/reparse operand context
    (``env``, ``timeout``, ``xargs``, ``sudo``, ``watch``, ``command``,
    ``builtin``, ``coproc``, ``eval``, ``trap``, shells, …).
    ``DATA`` — assignment words, reserved keywords, quoted data: never a
    command context.
    ``AMBIGUOUS`` — words whose class the classifier cannot decide from the
    name alone (runtime expansion, …): preserve byte-for-byte.
    """

    GIT_BASH_PASSTHROUGH = "git_bash_passthrough"
    FALLBACK_GUARDED = "fallback_guarded"
    UNSUPPORTED = "unsupported"
    CMD_TRANSLATABLE = "cmd_translatable"
    WRAPPER_CONTEXT = "wrapper_context"
    DATA = "data"
    AMBIGUOUS = "ambiguous"


#: Reserved shell keywords: at command position they are structure, not a
#: command word (``then``/``do``/``fi``/``done``/``esac``/``case``/``in``…).
#: ``time``/``coproc``/``!``/``{``/``funcname()`` stay in the wrapper/command
#: flow (they have a compatibility role), so they are NOT DATA here.  ``case``
#: and ``for``/``while``/``until`` open compound contexts: ``case`` patterns
#: are data, so the classifier names ``for`` the cmd hook word (CMD hook; see
#: ``_CMD_STRUCTURAL_WORDS``) and ``while``/``until``/``case`` are structural
#: openers that belong to the DATA-class family (never rewritten as commands).
_KEYWORD_DATA = frozenset(
    {"then", "do", "else", "elif", "fi", "done", "esac", "in",
     "while", "until", "case", "{", "}", "!", "function"}
)

#: cmd.exe structural hook words recognised at command position by the live
#: scanner (``_try_cmd_if`` / ``_try_cmd_for`` validate the operands; the
#: classifier names the class the plan §3.4 assigns to them).  ``if`` and
#: ``for`` are ALSO valid bash compound openers — the class records that
#: they are *eligible* for cmd translation, which the Phase-6 rules gate on
#: operand validation (a valid bash ``if`` simply falls through unchanged).
_CMD_STRUCTURAL_WORDS = frozenset({"if", "for"})


def classify_command(
    name: Optional[str],
    *,
    raw: Optional[str] = None,
    fallbacks: Dict[str, str] = _FALLBACKS,
    fallback_command_wrappers: Dict[str, str] = _FALLBACK_COMMAND_WRAPPERS,
    command_wrappers: FrozenSet[str] = _COMMAND_WRAPPERS,
    shell_wrappers: FrozenSet[str] = _SHELL_WRAPPERS,
    reparse_builtins: Dict[str, int] = _REPARSE_BUILTINS,
    unsupported: Dict[str, str] = _UNSUPPORTED_BODIES,
) -> CommandClass:
    """Classify a decoded command word *name* (plan §3.4).

    *name* is the word's literal value (``_literal_word_value`` result; pass
    ``None`` for a word with no statically decodable value — it is
    ``AMBIGUOUS``).  *raw* optionally carries the un-decoded source spelling;
    it enables the leading-``@`` cmd form (quote removal never strips ``@``,
    so the raw word is authoritative for that class).

    Recognition precedence mirrors the live scanner's command dispatch:
    wrapper-family names (command wrappers, fallback wrappers, shells, reparse
    builtins) are decided before the plain fallback-name lookup; unsupported
    names are refused only when the word is NOT a wrapper-family name (the
    scanner reaches ``_record_unsupported`` after the wrapper arms);
    assignment words are DATA; every other known or unknown word is
    ``GIT_BASH_PASSTHROUGH`` — the explicit "already Git Bash -> pass"
    statement the plan §2 requires.
    """
    if raw is not None and raw[:1] == "@":
        return CommandClass.CMD_TRANSLATABLE
    if name is None:
        return CommandClass.AMBIGUOUS
    if _GLUED_LABEL_RE.match(name):
        # ``:label`` glued word: refused as a cmd jump at command position
        # (``:`` alone and ``: label`` are valid bash no-ops and never match).
        return CommandClass.UNSUPPORTED
    if _ASSIGNMENT_RE.match(name):
        return CommandClass.DATA
    if name in _KEYWORD_DATA:
        return CommandClass.DATA
    if name in _CMD_STRUCTURAL_WORDS:
        return CommandClass.CMD_TRANSLATABLE
    if (
        name in command_wrappers
        or name in shell_wrappers
        or name in reparse_builtins
        or name in fallback_command_wrappers
    ):
        return CommandClass.WRAPPER_CONTEXT
    if name in unsupported:
        return CommandClass.UNSUPPORTED
    if name in fallbacks:
        return CommandClass.FALLBACK_GUARDED
    return CommandClass.GIT_BASH_PASSTHROUGH


# ── fallback registration ─────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class FallbackRecord:
    """A fallback registration record for one command-word name.

    ``name`` is the registered table key (appended to the scanner's ``names``
    in discovery order); ``body`` is the raw fallback command body
    (``_FALLBACK_BODIES``); ``definition`` is the runtime-guarded definition
    (``_FALLBACKS``) the prefix generator emits; ``runner_kind`` is the
    wrapper semantics key from ``_FALLBACK_COMMAND_WRAPPERS`` when the name
    behaves as a command wrapper too (``gtimeout`` -> ``timeout``, ``watch``
    -> ``watch``, ``sudo`` -> ``sudo``), else ``None``.
    """

    name: str
    body: str
    definition: str
    runner_kind: Optional[str] = None


def gnu_alias_target(name: str) -> Optional[str]:
    r"""Return the GNU core tool a Homebrew ``g``-prefixed alias runs.

    The ``g<tool>`` aliases are materialised into ``_FALLBACK_BODIES`` by
    ``fallback_data`` at import (``awk`` … ``xargs``, each body exactly
    ``<tool> "$@"``); ``gtimeout`` is the explicitly spelled body
    (``timeout "$@"`` — ``timeout`` itself is a command wrapper, not a
    fallback body, but the alias body still names it).  Returns the target
    tool name when *name* is a ``g*`` fallback whose body is the ``"$@"``
    forwarding form, else ``None`` (``watch``/``sudo`` map to themselves with
    real bodies, ``netcat``/``htop`` share bodies, non-``g`` names return
    ``None``).
    """
    if not name.startswith("g") or len(name) < 2 or name not in _FALLBACK_BODIES:
        return None
    body = _FALLBACK_BODIES[name]
    suffix = ' "$@"'
    if not body.endswith(suffix):
        return None
    target = body[: -len(suffix)]
    if not target or target == name:
        return None
    return target


def fallback_registration(
    name: Optional[str],
    *,
    fallbacks: Dict[str, str] = _FALLBACKS,
    bodies: Dict[str, str] = _FALLBACK_BODIES,
    command_wrapper_kinds: Dict[str, str] = _FALLBACK_COMMAND_WRAPPERS,
) -> Optional[FallbackRecord]:
    """Return the guarded-definition record for the command-word name *name*.

    Exactly the scanner's ``_literal_command_name_value`` membership test
    (``name in _FALLBACKS``): every registered name — including the ``g*`` GNU
    aliases, ``netcat`` (shares ``nc``'s ``/dev/tcp`` body), ``htop`` (shares
    ``top``'s body) and ``gtimeout`` — yields a record; unknown names and
    non-decodable words (``None``) yield ``None``.
    """
    if name is None or name not in fallbacks:
        return None
    return FallbackRecord(
        name=name,
        body=bodies[name],
        definition=fallbacks[name],
        runner_kind=command_wrapper_kinds.get(name),
    )


def unsupported_record(
    name: Optional[str],
    *,
    unsupported: Dict[str, str] = _UNSUPPORTED_BODIES,
) -> Optional[str]:
    """Return the refusal reason when the command-word name is unsupported.

    Mirrors ``_literal_unsupported_name_value``: only exact table keys match
    (``journalctl``/``goto`` plus the structural multi-word keys
    ``"call :label"``/``"exit /b"`` which never appear as one word — the
    scanner's ``_record_cmd_jump`` records those keys after peeking the
    operand word).
    """
    if name is None or name not in unsupported:
        return None
    return unsupported[name]


# ── wrapper state machine ─────────────────────────────────────────────────────


@dataclasses.dataclass
class WrapperState:
    """Mirror of the scanner's ``_BashWrapper`` (same attribute names).

    ``kind`` is the wrapper name (table key), ``skip_next`` marks a pending
    option value, ``opaque`` marks opaque remainder consumption (``env -S``,
    ``command -p``), ``path_value`` marks the pending value as a filesystem
    path (rewritten by the path rule), ``operands`` counts plain operands
    still consumed as data before the wrapped command (GNU ``timeout``: 1
    DURATION).  The live scanner holds the historical ``_BashWrapper``
    dataclass; ``wrapper_step`` accepts either (duck-typed reader).
    """

    kind: str
    skip_next: bool = False
    opaque: bool = False
    path_value: bool = False
    operands: int = 0


#: ``wrapper_step`` actions (identical strings to the legacy
#: ``_consume_wrapper_word`` contract):
#: ``"skip"`` — word consumed as wrapper data; ``"inspect"`` — wrapper state
#: ends, the word is still worth decoding for its own sake (``command -v``);
#: ``"command"`` — the wrapped command word.
ACTION_SKIP = "skip"
ACTION_INSPECT = "inspect"
ACTION_COMMAND = "command"


def wrapper_kind_of(
    wrapper_key: str,
    *,
    command_wrappers: FrozenSet[str] = _COMMAND_WRAPPERS,
    fallback_wrapper_kinds: Dict[str, str] = _FALLBACK_COMMAND_WRAPPERS,
) -> Optional[str]:
    """Return the wrapper state ``kind`` a command word opens, or ``None``.

    The fallback-wrapper table is checked FIRST (``sudo`` is both a plain
    command wrapper and a fallback name whose semantics are its own;
    ``gtimeout`` opens the ``timeout`` state machine; ``watch`` opens the
    ``watch`` state machine), mirroring the scanner's order.  A plain command
    wrapper (``env``/``timeout``/``xargs``/``sudo``/…) opens its own kind.
    """
    kind = fallback_wrapper_kinds.get(wrapper_key)
    if kind is not None:
        return kind
    if wrapper_key in command_wrappers:
        return wrapper_key
    return None


def wrapper_operands(
    kind: str, operand_counts: Dict[str, int] = _WRAPPER_OPERAND_COUNTS
) -> int:
    """Plain-operand count the wrapper consumes before its command word."""
    return operand_counts.get(kind, 0)


def wrapper_step(state: Any, raw: str, decode: Callable[[str], Optional[str]]) -> str:
    """Consume one wrapper argument word; return the legacy action string.

    Byte-for-byte port of ``_BashFixScanner._consume_wrapper_word``: *state*
    is the active wrapper (a ``_BashWrapper`` from the live scanner or a
    ``WrapperState`` here — same attribute names), *raw* the source word,
    *decode* the word-value decoder (``_BashFixScanner._literal_word_value``).
    Mutates ``state.skip_next`` / ``state.opaque`` / ``state.path_value`` /
    ``state.operands`` exactly like the method does.
    """
    if state.skip_next:
        state.skip_next = False
        if state.opaque:
            return ACTION_INSPECT
        return ACTION_SKIP
    if state.opaque:
        return ACTION_INSPECT
    # Option matching keys on the word VALUE: quoted spellings
    # (``env $'--'``, ``timeout $'-s'``) are statically the same option
    # the raw-text checks look for, and mistaking ``$'-s'`` for a plain
    # operand derails the wrapper state machine.
    word = decode(raw)
    if word is None:
        word = raw
    if state.operands > 0 and not word.startswith("-"):
        # A plain operand before the command word (GNU ``timeout`` takes
        # exactly one DURATION operand) is wrapper data, not the wrapped
        # command; consume it without ending the wrapper.
        state.operands -= 1
        return ACTION_SKIP
    if state.kind == "command" and word in {"-v", "-V"}:
        return ACTION_INSPECT
    if state.kind == "command" and (
        word == "-p"
        or (
            word.startswith("-")
            and not word.startswith("--")
            and "p" in word[1:]
        )
    ):
        state.opaque = True
        return ACTION_SKIP
    if state.kind == "env" and word in {"-S", "--split-string"}:
        state.opaque = True
        state.skip_next = True
        return ACTION_SKIP
    if state.kind == "env" and (
        word.startswith("--split-string=")
        or (word.startswith("-S") and word != "-S")
    ):
        return ACTION_INSPECT
    if word == "--":
        return ACTION_SKIP
    if word in _WRAPPER_OPTIONS_WITH_VALUE.get(state.kind, ()):
        state.skip_next = True
        if word in _WRAPPER_PATH_OPTIONS.get(state.kind, ()):
            state.path_value = True
        return ACTION_SKIP
    if word.startswith("-"):
        return ACTION_SKIP
    if state.kind == "env" and _ASSIGNMENT_RE.match(raw):
        return ACTION_SKIP
    return ACTION_COMMAND


def wrapper_path_inline_replacement(
    state: Any, raw: str, path_replacement: Callable[[str], Optional[str]]
) -> Optional[str]:
    """Rewrite an inline ``--chdir=PATH``-style option value of a path wrapper.

    Port of the scanner's ``_WRAPPER_PATH_OPTIONS``/``_WRAPPER_PATH_OPTION_LONG``
    inline arm: when the active wrapper kind takes path-valued options and the
    word starts with ``<long-option>=``, the value part is path-rewritten and
    the whole word re-emitted as ``<option>=<replacement>``; the option also
    fills a pending value slot and leaves the wrapper active.  Returns the
    replacement text, ``""`` when the arm fired with no path change (the arm
    still consumed the word), or ``None`` when the arm does not apply.
    """
    if state.kind not in _WRAPPER_PATH_OPTIONS:
        return None
    for option in _WRAPPER_PATH_OPTION_LONG:
        if raw.startswith(option + "="):
            value = raw[len(option) + 1 :]
            replacement = path_replacement(value)
            state.skip_next = False
            state.path_value = False
            return option + "=" + replacement if replacement is not None else ""
    return None


# ── pattern/program operand suppression (grep/sed/awk/rg) ────────────────────


def pattern_family_of(command_word: Optional[str]) -> Optional[str]:
    """The pattern-consuming family of a command word (or ``None``)."""
    if command_word is not None and command_word in _PATTERN_PROGRAM_COMMANDS:
        return command_word
    return None


def classify_pattern_option(family: str, word: str) -> Optional[str]:
    """Byte-for-byte port of ``_BashFixScanner._classify_pattern_option``."""
    if family in _GREP_PATTERN_FAMILY:
        if word in {"-e", "--regexp"}:
            return "data-pending"
        if word in {"-f", "--file"}:
            return "path-pending"
        if word.startswith("--regexp="):
            return "data"
        if word.startswith("--file="):
            return "path"
        if word.startswith(("--include", "--exclude")):
            # Glob option (glued ``--include=GLOB`` or separate value).
            return "data"
        if word in _GREP_VALUE_OPTIONS:
            return "value-pending"
        return None
    if family == "sed":
        if word in {"-e", "--expression"}:
            return "data-pending"
        if word in {"-f", "--file"}:
            return "path-pending"
        if word.startswith("--expression="):
            return "data"
        if word.startswith("--file="):
            return "path"
        return None
    # awk family.
    if word in {"-v", "-F", "--assign"}:
        return "data-pending"
    if word in {"-f", "--file"}:
        return "path-pending"
    if word.startswith("--assign="):
        return "data"
    if word.startswith("--file="):
        return "path"
    return None


def pattern_operand_path_ok(
    family: str, word: str, pending: Optional[str], bare: int
) -> Tuple[bool, Optional[str], int]:
    """One step of the pattern-consumer state machine.

    Returns ``(path_rewrite_allowed, new_pending, new_bare)`` for the argument
    word *word* (already value-decoded; the caller substitutes the raw
    spelling when the value is not statically decodable, per the legacy rule),
    given the pending verdict from a value-taking option and the count of bare
    operands seen so far.  Mirrors the scanner arm exactly: a pending verdict
    decides this word and clears; option words are never paths and defer a
    verdict for value-taking options; the first bare operand is the
    pattern/program (suppressed); later bare operands are files.
    """
    if pending is not None:
        return (pending == "path", None, bare)
    if word.startswith("-") and word != "-":
        option = classify_pattern_option(family, word)
        new_pending = pending
        if option == "data-pending":
            new_pending = "data"
        elif option == "path-pending":
            new_pending = "path"
        elif option == "value-pending":
            new_pending = "value"
        return (False, new_pending, bare)
    if bare == 0:
        # First bare operand: the pattern/program.
        return (False, pending, bare + 1)
    # Later bare operands: files.
    return (True, pending, bare + 1)


# ── runtime-reparsed scripts ──────────────────────────────────────────────────


def reparse_operand_count(word_value: Optional[str]) -> Optional[int]:
    """Operand-script count when the command word is a reparse builtin.

    ``eval`` -> ``-1`` (every operand re-parsed), ``trap`` -> ``1`` (first
    operand only), ``None`` otherwise — the ``_REPARSE_BUILTINS`` lookup the
    scanner performs on the decoded command-word value.  ``watch`` is a
    fallback command wrapper whose body re-parses its command operand with
    ``eval "$*"``; the scanner reaches it through the fallback-wrapper arm,
    so this function covers the plain table exactly.
    """
    if word_value is None:
        return None
    return _REPARSE_BUILTINS.get(word_value)


def watch_reparse_operand(word_value: Optional[str]) -> bool:
    """True when the wrapper kind re-executes its operand in this shell.

    ``watch`` (procps semantics: ``sh -c``; the Git Bash fallback mirrors it
    with ``eval "$*"``) is the same-shell wrapper whose QUOTED operand is an
    inline script — the scanner's ``wrapper.kind == "watch"`` arm.
    """
    return word_value == "watch"


def is_inline_script_word(raw: str) -> bool:
    """True when a word is a quoted inline-script operand (leading quote).

    The scanner's ``_inline_script_operand`` gate: a quoted operand
    (``watch -n1 'rev <<< abc'``, ``eval 'foo > nul'``, ``trap 'foo' EXIT``)
    carries a whole script as its literal value; unquoted operands flow
    through the ordinary word rules.
    """
    return raw.startswith(("'", '"', "$'", '$"'))


def eval_concat_fallback_name(operand_value: Optional[str]) -> Optional[str]:
    """Fallback name carried by the LEADING unquoted operand of eval/trap.

    Bash concatenates eval/trap operands and re-parses them; the leading word
    holds the command name, so a literal fallback name (``eval tree``) needs
    its definition registered (``None`` for later operands / unknown words —
    exactly the scanner arm, which tests truthiness of the decoded value).
    """
    if not operand_value:
        return None
    if operand_value in _FALLBACKS:
        return operand_value
    return None


def shell_c_script_range(
    reader: Any, source: str, start: int, end: int
) -> Optional[Tuple[int, int]]:
    r"""Locate the inline ``-c`` script operand of a shell word.

    Byte-for-byte port of ``_BashFixScanner._fix_shell_c_script_in_place``'s
    scan (the merge/edit tail stays in the scanner — it schedules outer
    actions).  *reader* must expose ``_read_word`` and ``_literal_word_value``
    (the live scanner passes ``self``; IR wrappers build a scanner over the
    same source — the Phase-4 reader pattern).  Returns ``(script_start,
    script_end)`` when an option-validated ``-c``-family script word follows
    (``bash -e -c '...'``, ``bash -o pipefail -c '...'``, ``sh '-c' '...'``,
    ``find -exec sh -c '...' \;``, ``xargs -I{} sh -c '...'``), else ``None``
    (``--`` end-of-options, a plain script operand before any ``-c``, an
    undecodable word, or no script word).
    """
    k = start
    decode = reader._literal_word_value
    script_start = -1
    script_end = -1
    expect_script = False
    while k < end:
        while k < end and source[k] in " \t\r":
            k += 1
        if k >= end or source[k] in _OPERATOR_CHARS or source[k] == "#":
            return None
        word_end = reader._read_word(k, end, scan_substitutions=False)
        if word_end <= k:
            return None
        if expect_script:
            # The word right after the ``-c`` option is the inline script.
            script_start, script_end = k, word_end
            break
        raw = source[k:word_end]
        # Key on the word VALUE so quoted option spellings (``sh '-c'``,
        # ``bash \-c``) reach the script scan the same way raw ones do.
        opt = decode(raw)
        if opt is None:
            return None  # runtime expansion: not an option word we know
        if _SHELL_C_SCRIPT_OPTION_RE.match(opt):
            expect_script = True
        elif opt in _SHELL_VALUE_OPTIONS:
            # Value-taking option: skip its value word and keep looking
            # for the ``-c`` pair (``bash -o pipefail -c '...'``).
            k = word_end
            while k < end and source[k] in " \t\r":
                k += 1
            if k >= end or source[k] in _OPERATOR_CHARS or source[k] == "#":
                return None
            value_end = reader._read_word(k, end, scan_substitutions=False)
            if value_end <= k:
                return None
            k = value_end
            continue
        elif opt == "--":
            # End of options: the next word is a script PATH, not an
            # inline ``-c`` script.
            return None
        elif opt.startswith(("-", "+")):
            pass  # another option (``-e``, ``-l``): keep looking
        else:
            # A plain operand before any ``-c`` (``sh script.sh``): not
            # an inline-script form.
            return None
        k = word_end
    if script_start < 0:
        return None
    return (script_start, script_end)


def exec_operand_shell_word(
    command_word: Optional[str], word_value: Optional[str]
) -> bool:
    """True when a shell word in ARGUMENT position is an executed inline shell.

    The scanner gate for ``find -exec sh -c``/``xargs sh -c``: only when the
    current command word passes argv to a child (``_EXEC_OPERAND_COMMANDS``)
    and the argument word is a shell (``_SHELL_WRAPPERS``) does its ``-c``
    operand really run.  ``echo sh -c '...'`` stays data.
    """
    return (
        command_word in _EXEC_OPERAND_COMMANDS
        and word_value is not None
        and word_value in _SHELL_WRAPPERS
    )


# ── redundant-shell unwrap decision ──────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class UnwrapDecision:
    """Decision for a command-position shell wrapper word.

    ``action`` is one of:

    ``"keep"`` — leave the wrapper byte-for-byte (bare ``bash``, semantic
    options, script file, assignment prefix, trailing argv, …).  When
    ``harvest`` is True and ``script_span`` is set, the refused shape is
    ``bash -c "script with $expansions"``: the wrapper stays, but the
    statically visible fallback names inside the non-decodable script word
    must still be registered (the scanner harvests via
    ``_harvest_nonliteral_inline_script``) — and the general ``keep`` fall
    through still attempts the in-place ``-c`` repair.
    ``"unwrap"`` — drop the redundant shell word (the edit covers the shell
    word plus its separating whitespace).  ``keep_wrapper`` asks the caller
    to leave an active command wrapper in place (the wrapped command keeps
    its executable context).  ``scan_index`` is the index of the real command
    word (the LIVE ``return j, wrapped`` value).
    ``"fix_script"`` — the ``[-l] -c script`` shape with a decodable script:
    the wrapper STAYS and only the inline script region (``script_span``) is
    repaired in place; ``keep_wrapper`` as above; ``scan_index`` is the index
    after the script word (the LIVE ``return script_end, wrapped`` value).
    """

    action: str
    keep_wrapper: bool = False
    script_span: Optional[Tuple[int, int]] = None
    harvest: bool = False
    scan_index: Optional[int] = None


def plausible_script_file(raw: str) -> bool:
    """Byte-for-byte port of ``_BashFixScanner._plausible_script_file``."""
    if raw.startswith("./") or raw.startswith("../"):
        return True
    if raw.startswith(".\\") or raw.startswith("..\\"):
        return True
    if "/" in raw or "\\" in raw:
        return True
    lower = raw.lower()
    return any(
        lower.endswith(ext)
        for ext in (".sh", ".bash", ".zsh", ".ksh", ".dash", ".ash", ".bats")
    )


def unwrap_decision(
    reader: Any,
    source: str,
    start: int,
    end: int,
    *,
    assignment_prefix: bool = False,
    wrapped: bool = False,
) -> UnwrapDecision:
    """Decide the redundant ``bash``/``sh`` unwrap at command position.

    Byte-for-byte port of ``_BashFixScanner._handle_shell_wrapper``'s
    decision (the edit/note/merge tail stays in the scanner).  *reader*
    exposes ``_read_word`` / ``_literal_word_value`` /
    ``_redirection_after_fd`` (the live scanner passes ``self``; the IR
    wrapper builds a scanner over the same source).  The decision maps to
    the LIVE returns as follows:

    * ``unwrap``        — ``return j, wrapped`` (drop the shell word region
      ``[word_start, j)``; ``script_span`` records ``j`` so the caller can
      schedule the identical edit).
    * ``fix_script``    — ``return script_end, wrapped`` (repair the quoted
      script at ``script_span`` in place; wrapper stays).
    * ``keep``          — ``return None`` (the caller falls through to the
      in-place ``-c`` repair exactly like the LIVE main loop does).  The
      expansion-bearing script bail is the one ``keep`` that carries
      ``harvest=True`` + ``script_span``: the LIVE method itself harvests
      registrations from that word and returns ``None`` (the subsequent
      in-place retry re-harvests — dedup makes it idempotent — and consumes
      the script span), so the caller must invoke
      ``_harvest_nonliteral_inline_script`` for ``harvest`` before returning
      ``None``.
    """
    if assignment_prefix:
        # ``VAR=x bash -c 'echo $VAR'``: the assignment is scoped to the
        # shell *process*, so unwrapping would make the outer shell expand
        # ``$VAR`` before the assignment takes effect.  Keep the wrapper.
        return UnwrapDecision("keep")
    s = source
    j = start
    while j < end and s[j] in " \t\r":
        j += 1
    if j >= end or s[j] in _OPERATOR_CHARS or s[j] == "#":
        # Bare ``bash`` (or ``bash && ...`` / ``bash`` at EOF): bash would
        # start a nested shell; that is not a compatibility problem.
        return UnwrapDecision("keep")
    next_end = reader._read_word(j, end, scan_substitutions=False)
    if next_end <= j:
        return UnwrapDecision("keep")
    next_raw = s[j:next_end]
    # The option/script-file decisions below must key on the word VALUE
    # (bash quote removal), not the raw source text: a quoted option
    # (``bash '-c' 'echo hi'``) or quoted script file (``bash "deploy.sh"``)
    # has a raw spelling that hides its role, and deleting the shell word in
    # front of it leaves the option/script word as the command name (real
    # bash: rc 0 -> 127).  A word whose value is not statically decodable
    # (``bash "$S"``) may open a script file relative to cwd, so the unwrap
    # is refused as well.
    next_val = reader._literal_word_value(next_raw)
    if next_val is None:
        return UnwrapDecision("keep")
    if next_val.startswith("-"):
        # Optional leading login flag, then the ``-c`` family.  Any other
        # option (``-e``, ``-x``, ``-i``, ``--norc``, ...) changes shell
        # behaviour and is left for bash to handle.
        opt_end = next_end
        opt = next_val
        if opt in ("-l", "-L", "--login"):
            cursor = opt_end
            while cursor < end and s[cursor] in " \t\r":
                cursor += 1
            if cursor >= end or s[cursor] in _OPERATOR_CHARS or s[cursor] == "#":
                return UnwrapDecision("keep")
            opt_end = reader._read_word(cursor, end, scan_substitutions=False)
            if opt_end <= cursor:
                return UnwrapDecision("keep")
            opt_raw = s[cursor:opt_end]
            opt_val = reader._literal_word_value(opt_raw)
            if opt_val is None:
                return UnwrapDecision("keep")
            opt = opt_val
        if not _SHELL_C_OPTIONS.match(opt):
            return UnwrapDecision("keep")
        # The word after ``-c`` is the inline script.
        k = opt_end
        while k < end and s[k] in " \t\r":
            k += 1
        if k >= end or s[k] in _OPERATOR_CHARS or s[k] == "#":
            return UnwrapDecision("keep")  # ``bash -c`` with no script
        script_end = reader._read_word(k, end, scan_substitutions=False)
        if script_end <= k:
            return UnwrapDecision("keep")
        m = script_end
        while m < end and s[m] in " \t\r":
            m += 1
        if m < end and s[m] not in _OPERATOR_CHARS and s[m] != "#":
            # A digit run followed by a redirection operator
            # (``bash -c 'tree' 2>/dev/null``, ``1>&2``) closes the wrapper
            # segment exactly like ``;``/``&&``: the fd redirect applies to
            # the wrapper itself, it is NOT script argv.
            if not (s[m].isdigit() and reader._redirection_after_fd(m, end)):
                return UnwrapDecision("keep")  # trailing script argv
        script_raw = s[k:script_end]
        script = reader._literal_word_value(script_raw)
        if script is None:
            # Expansions inside the script: the operand text cannot be
            # re-emitted, but its statically visible command words still
            # need their fallback definitions registered for the nested
            # shell that re-parses the expanded text.
            return UnwrapDecision(
                "keep", script_span=(k, script_end), harvest=True
            )
        # The wrapper stays: ``bash -c 'script'`` runs the script in a CHILD
        # shell whose isolation (``exit``, assignments, ``cd``, traps)
        # inlining into the surrounding script would silently destroy.  Only
        # the inline script is repaired in place.
        return UnwrapDecision(
            "fix_script",
            keep_wrapper=wrapped,
            script_span=(k, script_end),
            scan_index=script_end,
        )
    if plausible_script_file(next_val):
        return UnwrapDecision("keep")
    # ``bash <command> ...``: drop the redundant shell word itself (plus the
    # whitespace run that separated it from the real command).  The caller
    # supplies the shell word's own start for the edit span and uses
    # ``scan_index`` as the resume position.
    return UnwrapDecision(
        "unwrap", keep_wrapper=wrapped, script_span=None, scan_index=j
    )


# ── fallback prefix / export generation ───────────────────────────────────────


def fallback_prefix(names: List[str]) -> str:
    """Generate the guarded-definition + conditional-export prefix.

    Byte-for-byte port of ``_BashFixScanner.fix``'s prefix construction:
    de-duplicate in discovery order (``dict.fromkeys``), emit one guarded
    definition line per unique name, then one conditional ``export -f`` line
    per unique name (``declare -F`` gated so a fallback that found a real
    executable installs no export noise); the prefix is emitted ONLY when at
    least one fallback was registered (empty otherwise — plan §3.5), and the
    whole block is terminated by a newline.
    """
    unique_names = list(dict.fromkeys(names))
    if not unique_names:
        return ""
    definitions = "\n".join(_FALLBACKS[name] for name in unique_names)
    exports = "\n".join(
        f"if declare -F {name} >/dev/null; then export -f {name}; fi"
        for name in unique_names
    )
    return definitions + "\n" + exports + "\n"


def export_lines(names: List[str]) -> Tuple[str, ...]:
    """The conditional ``export -f`` lines for the registered fallback names."""
    return tuple(
        f"if declare -F {name} >/dev/null; then export -f {name}; fi"
        for name in dict.fromkeys(names)
    )


# ── IR wrappers (pure functions of (node, source)) ───────────────────────────


def classify_command_node(command: "CommandNode", source: str) -> CommandClass:
    """Classify a parsed command's command word (plan §3.4 over IR)."""
    cw = command.command_word
    if cw is None:
        return CommandClass.AMBIGUOUS
    raw = cw.span.text(source)
    return classify_command(cw.literal_value, raw=raw)


def _command_value(command: "CommandNode") -> Optional[str]:
    cw = command.command_word
    return cw.literal_value if cw is not None else None




def _command_operands(command: "CommandNode", source: str) -> list:
    """The command's operand words in the scanner's source-word order.

    The IR records a whole-quoted runtime-reparsed operand as an
    :class:`EmbeddedScriptNode` (in ``command.embedded``, whose ``owner_word``
    is the same word) instead of duplicating it into ``command.arguments``;
    the LIVE scanner however sees ONE flat word stream (arguments + script
    operands interleaved by position).  This helper reconstructs the scanner
    view: every argument word plus every embedded owner word, sorted by span
    start (the embedded node occupies exactly the operand's span, so the
    merge is lossless and stable).
    """
    words = list(command.arguments)
    for emb in command.embedded:
        if emb.owner_word is not None:
            words.append(emb.owner_word)
    words.sort(key=lambda w: w.span.start)
    return words


def reparse_operand_index(
    command: "CommandNode", source: str
) -> Optional[int]:
    """Index into the command's scanner word stream of the runtime script word.

    The word stream is :func:`_command_operands` (arguments + embedded owner
    words in source order — the same sequence the LIVE scanner iterates).
    Detects, porting the scanner's rules:

    * ``eval`` (all operands) / ``trap`` (first operand): the first
      whole-quoted operand of a reparse builtin command.
    * ``watch`` (fallback command wrapper): the first operand after the
      wrapper's option words are consumed by the state machine, when that
      operand is whole-quoted (``watch -n1 'rev <<< abc'``); the ``-n VALUE``
      option value is consumed as an option and is NOT the script.
    * command-position shell ``-c`` (``bash``/``sh``/``dash``/``ash``): the
      script word right after the first ``-c``-family option (clusters like
      ``-ec`` included; ``--`` and plain script-path operands bail; value
      options like ``-o`` consume their value word).
    * ``find -exec sh -c '...'`` / ``xargs sh -c '...'`` (exec-operand
      commands): the script word after an operand-position shell's ``-c``.

    Returns the index, or ``None`` when no runtime-reparsed script operand is
    present.
    """
    command_value = _command_value(command)
    words = _command_operands(command, source)
    args = [w.span.text(source) for w in words]
    # eval / trap: the first quoted operand (within the count) is a script.
    count = reparse_operand_count(command_value)
    if count is not None:
        for index, word in enumerate(args):
            if count == 0:
                break
            if is_inline_script_word(word):
                return index
            if count > 0:
                count -= 1
        return None
    # watch (fallback command wrapper with same-shell reparse): consume the
    # option words with the wrapper state machine; the wrapped command word,
    # when quoted, is the script.
    if command_value is not None:
        kind = _FALLBACK_COMMAND_WRAPPERS.get(command_value)
        if kind == "watch":
            state = WrapperState("watch", operands=wrapper_operands("watch"))
            decode = _decode_word
            for index, word in enumerate(args):
                action = wrapper_step(state, word, decode)
                if action == ACTION_COMMAND:
                    if is_inline_script_word(word):
                        return index
                    return None
            return None
    # command-position shell -c (bash/sh/dash/ash): the LIVE in-place scan
    # (``shell_c_script_range``) is authoritative — it implements the exact
    # bail set (``--`` end-of-options, a plain script-path operand before any
    # ``-c``, undecodable option words) and the broader ``-[A-Za-z]*c``
    # cluster rule the parser's embedded arming does not model permissively
    # (the parser only arms on the ``-c``/``-lc``/``-cl`` unwrap family; the
    # scan additionally reaches ``-ec``-style clusters, matching the LIVE
    # fall-through after a refused unwrap).
    if command_value in _SHELL_WRAPPERS:
        return _shell_c_index_by_scan(command, source, words)
    # exec-operand commands (find/xargs/eval/ssh/watch/coproc): an
    # argument-position shell word's -c script is reparsed.  ``eval`` is
    # handled above; the remaining names gate on an in-argv shell word.
    if command_value in _EXEC_OPERAND_COMMANDS:
        values = [
            w.literal_value if w.literal_value is not None else args[i]
            for i, w in enumerate(words)
        ]
        for index, value in enumerate(values):
            if exec_operand_shell_word(command_value, value):
                located = _shell_c_index_by_scan(
                    command, source, words, after=index
                )
                if located is not None:
                    return located
        return None
    return None


def _shell_c_index_by_scan(
    command: "CommandNode", source: str, words: list, after: int = -1
) -> Optional[int]:
    """Run the LIVE in-place ``-c`` scan over the operand region and map the
    returned script span back onto the scanner word stream ``words``.

    The region starts after the shell word (``after``: the index of the shell
    word in ``words``; -1 means the command word itself, i.e. the scan starts
    at the first operand — exactly the LIVE call sites which pass the scan
    index right after the shell word).  A fresh scanner over the same source
    reproduces the word boundaries byte-for-byte (Phase-4 reader pattern).
    """
    from kimix_native.shell_compat.bash_core import _BashFixScanner

    if not words:
        return None
    region_start = (
        words[after].span.end if after >= 0 else words[0].span.start
    )
    region_end = command.span.end
    if region_start >= region_end:
        return None
    reader = _BashFixScanner(source)
    found = shell_c_script_range(reader, source, region_start, region_end)
    if found is None:
        return None
    start, end = found
    for index, word in enumerate(words):
        if word.span.start == start and word.span.end == end:
            return index
    return None


def _embedded_script_index(
    command: "CommandNode", source: str, words: list
) -> Optional[int]:
    """Index of the command's first embedded script owner word in ``words``.

    ``words`` is the scanner word stream (:func:`_command_operands`).  Returns
    ``None`` when the command has no embedded script operand.
    """
    if not command.embedded:
        return None
    target = command.embedded[0].owner_word
    if target is None:
        return None
    for index, word in enumerate(words):
        if word.span.start == target.span.start and word.span.end == target.span.end:
            return index
    return None


def _decode_word(raw: str) -> Optional[str]:
    from kimix_native.shell_compat.bash_core import _BashFixScanner

    return _BashFixScanner._literal_word_value(raw)




def unwrap_decision_for_command(
    command: "CommandNode", source: str
) -> UnwrapDecision:
    """IR wrapper of :func:`unwrap_decision` for a parsed shell command.

    Builds a scanner over the same source as the reader (Phase-4 pattern) so
    the word boundaries are byte-identical to the live call; ``wrapped`` is
    False at a plain command position (no active command wrapper is tracked
    in the IR yet — the Phase-7 translator threads it in the context).  The
    command's assignment words set ``assignment_prefix`` exactly like the
    scanner's flag (``VAR=x bash …``).
    """
    cw = command.command_word
    if cw is None:
        return UnwrapDecision("keep")
    from kimix_native.shell_compat.bash_core import _BashFixScanner

    return unwrap_decision(
        _BashFixScanner(source),
        source,
        cw.span.end,
        command.span.end,
        assignment_prefix=bool(command.assignments),
        wrapped=False,
    )


def wrapper_state_for_command(command: "CommandNode") -> Optional[WrapperState]:
    """Initial wrapper state a parsed command opens, or ``None``.

    Uses the decoded command-word value against the fallback-wrapper table
    first, then the plain command-wrapper table — the same precedence as the
    live dispatch.  The scanner's raw-vs-decoded ``wrapper_key`` refinement
    (backslash/quote spellings of the wrapper word) is part of the scanning
    flow and stays there; this IR wrapper covers the plain command word.
    """
    value = _command_value(command)
    if value is None:
        return None
    kind = wrapper_kind_of(value)
    if kind is None:
        return None
    return WrapperState(kind, operands=wrapper_operands(kind))


__all__ = [
    "ACTION_COMMAND",
    "ACTION_INSPECT",
    "ACTION_SKIP",
    "CommandClass",
    "FallbackRecord",
    "UnwrapDecision",
    "WrapperState",
    "classify_command",
    "classify_command_node",
    "classify_pattern_option",
    "eval_concat_fallback_name",
    "exec_operand_shell_word",
    "export_lines",
    "fallback_prefix",
    "fallback_registration",
    "gnu_alias_target",
    "is_inline_script_word",
    "pattern_family_of",
    "pattern_operand_path_ok",
    "plausible_script_file",
    "reparse_operand_count",
    "reparse_operand_index",
    "shell_c_script_range",
    "unsupported_record",
    "unwrap_decision",
    "unwrap_decision_for_command",
    "watch_reparse_operand",
    "wrapper_kind_of",
    "wrapper_operands",
    "wrapper_path_inline_replacement",
    "wrapper_state_for_command",
    "wrapper_step",
]
