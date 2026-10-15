"""Phase-7 genuine IR per-command decision engine (plan 3.5, slots 4-8).

Part of the lossless tolerant-IR ``kimix_native.shell_compat`` implementation
package behind the stable ``_shell_compat`` facade.

Option A keeps the LIVE fix_bash_command orchestration in the engine reader (the
compatibility scanner): every rule DECISION the reader makes already delegates to
the IR modules (Phases 3-6), so the live path is byte-exact by construction and
is locked by the Phase-4/5/6 differentials (see translator.translate).  This
module is the INDEPENDENT IR-ownership proof: the 3.5 dispatcher slots 4-8, each
a genuine handler over a parsed CommandNode running the scanner's per-command
word-state machine from the node's OWN words (command word + arguments + embedded
owner words + redirections + heredocs + substitution child contexts), dispatching
every decision to the SAME ported functions the scanner delegates to (wrappers /
paths / heredoc / cmd_translator) - never a second rule implementation.

Slot partition (the 3.5 handler order; ONE shared EditPlan).  Reports are
bucketed by the slot that discovers them so the dispatcher (priority 4-8) over an
orchestrator's source-ordered command walk reproduces the live scanner's
per-command discovery order and global order byte-for-byte (the translate_ir
battery gate, .kimix_cache/p7_broad_tir.py: 511/511).

Every bash_core / wrappers / paths / heredoc / cmd_translator import is LAZY
(inside function bodies) - those modules import bash_core at load and bash_core
imports this module's dispatcher only at call time, so no load-time cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List, Optional, Tuple

if TYPE_CHECKING:  # pragma: no cover
    from kimix_native.shell_compat.models import CommandNode
    from kimix_native.shell_compat.translator import RuleResult, TranslationContext


# ── shared primitives (single definitions, delegated to the scanner) ─────────


def _decode(raw: str) -> Optional[str]:
    from kimix_native.shell_compat.bash_core import _BashFixScanner

    return _BashFixScanner._literal_word_value(raw)


def _reader(source: str) -> Any:
    from kimix_native.shell_compat.bash_core import _BashFixScanner

    return _BashFixScanner(source)


def _raw(word: Any, source: str) -> str:
    return source[word.span.start : word.span.end]


def _cmd_owned(raw_word: str) -> bool:
    # a command word a slot-1/2/3 cmd rule owns (cmd if/for keyword) - slots 4-8
    # must not double-fire on it.  Leading-@ words are handled by
    # decide_at_continuation (the strip re-dispatch) before this check.
    return raw_word in ("if", "for")


# bash control/compound keyword words the IR walk must not classify as a plain
# command word (the scanner leaves them to the keyword state machine; the tolerant
# parser tiles them as command words).
_KEYWORD_SKIP = frozenset(
    {
        "while", "until", "case", "select", "do", "done", "then", "else",
        "elif", "fi", "esac", "in", "function", "!", "{", "}",
    }
)


def _container_kinds() -> set:
    from kimix_native.shell_compat.models import NodeKind

    return {
        NodeKind.SCRIPT,
        NodeKind.COMMAND_LIST,
        NodeKind.PIPELINE,
        NodeKind.COMPOUND,
    }


def _stop_kinds() -> tuple:
    from kimix_native.shell_compat.models import NodeKind

    return (
        NodeKind.EMBEDDED,
        NodeKind.EXPANSION,
        NodeKind.HEREDOC,
        NodeKind.REDIRECTION,
    )


def _commands_in(node: Any) -> List[Any]:
    """The command nodes of a script/compound subtree in source order, stopping
    at embedded / substitution / heredoc / redirection edges (those contexts are
    recursed separately through the per-command pass)."""
    from kimix_native.shell_compat.models import CommandNode

    top = _container_kinds()
    stop = _stop_kinds()
    out: List[Any] = []

    def rec(current: Any) -> None:
        if current.kind in stop:
            return
        if isinstance(current, CommandNode):
            out.append(current)
            return
        if current.kind in top:
            kids: List[Any] = []
            for attr in ("children", "commands", "parts"):
                value = getattr(current, attr, None)
                if not value:
                    continue
                if attr == "parts":
                    kids.extend(child for _n, child in value)
                else:
                    kids.extend(value)
            for child in kids:
                rec(child)

    rec(node)
    return out


def outer_commands(source: str) -> List["CommandNode"]:
    """The scanner-view top-level command nodes of source, in source order."""
    from kimix_native.shell_compat.parser import parse

    return _commands_in(parse(source))


def _wrapper_key(raw: str, source: str) -> str:
    """The scanner's command-word table key: backslash-stripped unless a shell
    keyword, with the decoded-value fallback for quoted wrapper spellings."""
    from kimix_native.shell_compat.bash_core import (
        _COMMAND_WRAPPERS,
        _FALLBACK_COMMAND_WRAPPERS,
        _SHELL_KEYWORDS,
    )

    unescaped = (
        raw[1:] if (raw.startswith("\\") and raw[1:] not in _SHELL_KEYWORDS) else raw
    )
    wrapper_key = unescaped
    if (
        wrapper_key not in _FALLBACK_COMMAND_WRAPPERS
        and wrapper_key not in _COMMAND_WRAPPERS
    ):
        literal_unescaped: Optional[str] = None
        if raw != unescaped or raw[:1] in ("'", '"', "$"):
            decoded = _decode(raw)
            if decoded is not None and decoded not in _SHELL_KEYWORDS:
                literal_unescaped = decoded
        wrapper_key = literal_unescaped if literal_unescaped else wrapper_key
    return wrapper_key


# ── per-command decision accumulator (slot + report bucketed) ────────────────


class CommandDecision:
    """One command's IR decisions, bucketed by the 3.5 slot that discovers them.
    Each slot handler calls decide_command and commits ONLY its own bucket, so
    the dispatcher (priority 4-8) reproduces the live scanner's per-command
    event order and global order byte-for-byte."""

    __slots__ = (
        "names4",
        "unsupported4",
        "names5",
        "paths5",
        "shells5",
        "nuls5",
        "unsupported5",
        "cmd_notes5",
        "cmd_paths5",
        "paths6",
        "nuls7",
        "slot4_edits",
        "slot5_edits",
        "slot6_edits",
        "slot7_edits",
        "cmd_edits5",
        "cmd_consumed5",
        "heredoc_events",
        "consumed_end",
        "is_cmd_owned",
    )

    def __init__(self) -> None:
        self.names4: List[str] = []
        self.unsupported4: List[str] = []
        self.names5: List[str] = []
        self.paths5: List[str] = []
        self.shells5: List[str] = []
        self.nuls5: List[str] = []
        self.unsupported5: List[str] = []
        self.cmd_notes5: List[str] = []
        self.cmd_paths5: List[str] = []
        self.paths6: List[str] = []
        self.nuls7: List[str] = []
        self.slot4_edits: List[Tuple[int, int, str]] = []
        self.slot5_edits: List[Tuple[int, int, str]] = []
        self.slot6_edits: List[Tuple[int, int, str]] = []
        self.slot7_edits: List[Tuple[int, int, str]] = []
        self.cmd_edits5: List[Tuple[int, int, str]] = []
        self.cmd_consumed5: Optional[int] = None
        self.heredoc_events: List[Tuple[int, int]] = []
        self.consumed_end: int = 0
        self.is_cmd_owned: bool = False

    def absorb_inner(self, inner: Any) -> None:
        """Fold a scanned inner (nested script / substitution) scanner's reports
        into the slot-5 buckets by the LIVE merge rules."""
        self.names5.extend(n for n in inner.names if n not in self.names5)
        self.paths5.extend(inner.path_notes)
        self.shells5.extend(n for n in inner.shell_notes if n not in self.shells5)
        self.nuls5.extend(inner.nul_fixes)
        self.unsupported5.extend(
            n for n in inner.unsupported if n not in self.unsupported5
        )


# ── nested-script re-emit (engine reader; shared with the scanner inner scan) ─


def _scan_inner(script: str) -> Optional[Any]:
    try:
        inner = _reader(script)
        inner._scan_range(0, len(script))
    except RecursionError:
        return None
    return inner


def _inplace_fix_span(d: CommandDecision, source: str, ss: int, se: int) -> None:
    """slot 5: re-scan a located inline-script operand span and re-emit the
    repaired (single-quoted) script over the span - the scanner's
    _fix_shell_c_script_in_place / _inline_script_operand tail."""
    from kimix_native.shell_compat.fallback_data import _single_quote

    script = _decode(source[ss:se])
    if script is None:
        _harvest_span(d, source, ss, se)
        return
    inner = _scan_inner(script)
    if inner is None:
        return
    d.absorb_inner(inner)
    if inner.edits:
        d.slot5_edits.append((ss, se, _single_quote(inner._build_fixed_source())))


def _harvest_span(d: CommandDecision, source: str, ss: int, se: int) -> None:
    """The scanner's _harvest_nonliteral_inline_script: register statically
    visible fallbacks from an expansion-bearing operand (slot-5 names) with NO
    re-emit or path/nul metadata."""
    from kimix_native.shell_compat.bash_core import _opaque_inline_expansions

    inner_text = source[ss:se]
    if (
        len(inner_text) >= 2
        and inner_text[0] in "'\""
        and inner_text[-1] == inner_text[0]
    ):
        inner_text = inner_text[1:-1]
    sanitized, substitutions = _opaque_inline_expansions(inner_text)
    for src in (sanitized, *substitutions):
        if not src.strip():
            continue
        inner = _scan_inner(src)
        if inner is None:
            continue
        d.names5.extend(n for n in inner.names if n not in d.names5)
        d.shells5.extend(n for n in inner.shell_notes if n not in d.shells5)
        d.unsupported5.extend(
            n for n in inner.unsupported if n not in d.unsupported5
        )


# ── slot 6: path rules ────────────────────────────────────────────────────────


def _cmd_word_path(d: CommandDecision, source: str, command: "CommandNode") -> None:
    """slot 6: a command word can itself be a Windows / virtual path."""
    from kimix_native.shell_compat import paths as P, wrappers as W
    from kimix_native.shell_compat.bash_core import _COMMAND_WRAPPERS, _SHELL_WRAPPERS

    cw = command.command_word
    raw = _raw(cw, source)
    val = _decode(raw)
    wkey = _wrapper_key(raw, source)
    if W.wrapper_kind_of(wkey) is not None or wkey in _COMMAND_WRAPPERS:
        return
    if val in _SHELL_WRAPPERS:
        return
    pr = P.path_replacement(raw)
    if pr is not None:
        d.slot6_edits.append((cw.span.start, cw.span.end, pr))
        d.paths6.append(raw)


def _add_arg_path(d: CommandDecision, source: str, word: Any, slot: int) -> None:
    from kimix_native.shell_compat import paths as P

    araw = _raw(word, source)
    pr = P.path_replacement(araw)
    if pr is not None:
        (d.slot6_edits if slot == 6 else d.slot5_edits).append(
            (word.span.start, word.span.end, pr)
        )
        (d.paths6 if slot == 6 else d.paths5).append(araw)


def _walk_arguments(
    d: CommandDecision, source: str, command: "CommandNode", *, start_after: int = 0,
    pattern_word: Optional[str] = None,
) -> None:
    """slot 6: the argument-word path rule with the pattern-consumer suppression
    when the EFFECTIVE command word is a pattern program (grep/sed/awk/rg).
    ``pattern_word`` is the effective command-word literal value the scanner uses
    (the raw word for a plain command, the stripped word after an ``@`` strip,
    the post-unwrap word after a redundant shell); it defaults to the command
    node's own command word decoded."""
    from kimix_native.shell_compat import wrappers as W
    from kimix_native.shell_compat.bash_core import _PATTERN_PROGRAM_COMMANDS

    if pattern_word is None:
        cw = command.command_word
        pattern_word = _decode(_raw(cw, source)) if cw is not None else None
    pattern_family = (
        pattern_word
        if (pattern_word is not None and pattern_word in _PATTERN_PROGRAM_COMMANDS)
        else None
    )
    pending: Optional[str] = None
    bare = 0
    for a in command.arguments:
        if a.span.start < start_after:
            continue
        araw = _raw(a, source)
        path_ok = True
        if pattern_family is not None:
            value = _decode(araw)
            word = value if value is not None else araw
            if pending is not None:
                pend, pending = pending, None
                path_ok = pend == "path"
            elif word.startswith("-") and word != "-":
                option = W.classify_pattern_option(pattern_family, word)
                path_ok = False
                if option == "data-pending":
                    pending = "data"
                elif option == "path-pending":
                    pending = "path"
                elif option == "value-pending":
                    pending = "value"
            elif bare == 0:
                bare += 1
                path_ok = False
            else:
                bare += 1
                path_ok = True
        if path_ok:
            _add_arg_path(d, source, a, 6)


def _walk_arguments_after(d: CommandDecision, source: str, command: "CommandNode", consumed_to: int) -> None:
    """slot 6: the words after the wrapped command keep the path rule."""
    for a in command.arguments:
        if a.span.start < consumed_to:
            continue
        _add_arg_path(d, source, a, 6)


def _walk_redirections(d: CommandDecision, source: str, command: "CommandNode") -> None:
    """slots 6/7: redirection-target decisions (nul -> /dev/null is slot 7; else
    the slot-6 path rule)."""
    from kimix_native.shell_compat import paths as P

    for r in command.redirections:
        if r.target is None:
            continue
        repl = P.nul_replacement_for_redirection(r, source)
        if repl is not None:
            d.slot7_edits.append((r.target.span.start, r.target.span.end, repl))
            d.nuls7.append(_raw(r.target, source))
        else:
            pr = P.path_replacement_for_word(r.target, source)
            if pr is not None:
                d.slot6_edits.append((r.target.span.start, r.target.span.end, pr))
                d.paths6.append(_raw(r.target, source))


# ── slot 5: wrapper / unwrap / reparse ────────────────────────────────────────


def _consume_wrapper(d: CommandDecision, source: str, command: "CommandNode", *, wrapper_key: str) -> None:
    """slot 5: drive the wrapper operand state machine over the IR argument
    words; the wrapped command word is registered (slot5 name) and runner-swapped
    when the wrapping executable cannot invoke shell functions."""
    from kimix_native.shell_compat import paths as P, wrappers as W
    from kimix_native.shell_compat.bash_core import (
        _FALLBACK_COMMAND_WRAPPERS,
        _SAME_SHELL_WRAPPERS,
        _SHELL_WRAPPERS,
        _WRAPPER_OPERAND_COUNTS,
        _WRAPPER_PATH_OPTIONS,
        _WRAPPER_PATH_OPTION_LONG,
    )
    from kimix_native.shell_compat.fallback_data import _wrapper_runner

    kind = _FALLBACK_COMMAND_WRAPPERS.get(wrapper_key) or wrapper_key
    executable_wrapper = kind not in _SAME_SHELL_WRAPPERS
    state = W.WrapperState(kind, operands=_WRAPPER_OPERAND_COUNTS.get(kind, 0))
    args = command.arguments
    consumed_to = command.command_word.span.end
    wrapped_done = False
    idx = 0
    while idx < len(args) and not wrapped_done:
        a = args[idx]
        araw = _raw(a, source)
        if kind in _WRAPPER_PATH_OPTIONS:
            inline = None
            for option in _WRAPPER_PATH_OPTION_LONG:
                if araw.startswith(option + "="):
                    value = araw[len(option) + 1 :]
                    pr = P.path_replacement(value)
                    inline = option + "=" + pr if pr is not None else None
                    break
            if inline is not None:
                if inline != araw:
                    d.slot6_edits.append((a.span.start, a.span.end, inline))
                d.paths6.append(araw)
                state.skip_next = False
                state.path_value = False
                consumed_to = a.span.end
                idx += 1
                continue
        path_option_value = state.path_value and state.skip_next
        action = W.wrapper_step(state, araw, _decode)
        if action == W.ACTION_SKIP:
            if path_option_value:
                pr = P.path_replacement(araw)
                if pr is not None:
                    d.slot6_edits.append((a.span.start, a.span.end, pr))
                    d.paths6.append(araw)
            consumed_to = a.span.end
            idx += 1
            continue
        if action == W.ACTION_INSPECT:
            # command -v / env -S style: the wrapper's option consumes the shell
            # context; the scanner drops the wrapper (wrapper=None) so the
            # REMAINING words are ordinary arguments (path rule only, never a
            # new wrapped-command classification).
            consumed_to = a.span.end
            _walk_arguments_after(d, source, command, consumed_to)
            return
        # ACTION_COMMAND: the wrapped command word.
        wval = _decode(araw)
        rec = W.fallback_registration(wval)
        if rec is not None:
            d.names5.append(rec.name)
            if executable_wrapper:
                d.slot5_edits.append((a.span.start, a.span.end, _wrapper_runner(rec.name)))
        else:
            ur = W.unsupported_record(wval)
            if ur is not None:
                if wval not in d.unsupported5:
                    d.unsupported5.append(wval)
            elif wval in _SHELL_WRAPPERS:
                if not _shell_operand(d, source, command, a):
                    _add_arg_path(d, source, a, 6)
            elif W.wrapper_kind_of(wval) is None:
                _add_arg_path(d, source, a, 6)
        consumed_to = a.span.end
        idx += 1
        wrapped_done = True
    if wrapped_done:
        _walk_arguments_after(d, source, command, consumed_to)


def _shell_operand(d: CommandDecision, source: str, command: "CommandNode", shell_word: Any) -> bool:
    """slot 5: a shell invoked as a wrapper operand (env bash -c) or by an
    exec-operand command (find -exec sh -c): in-place -c repair."""
    from kimix_native.shell_compat import wrappers as W

    found = W.shell_c_script_range(_reader(source), source, shell_word.span.end, len(source))
    if found is None:
        return False
    _inplace_fix_span(d, source, found[0], found[1])
    d.consumed_end = max(d.consumed_end, found[1])
    return True


def _reparse_operand_words(d: CommandDecision, source: str, command: "CommandNode", *, count: int) -> None:
    """slot 5: eval (count=-1, every operand) / trap (count=1, first operand)
    inline-script operands - re-scan + re-emit the quoted operand, and register
    the unquoted LEADING word's fallback (mirrors the scanner reparse branch)."""
    from kimix_native.shell_compat import wrappers as W

    remaining = count
    leading = True
    for a in command.arguments:
        araw = _raw(a, source)
        if remaining == 0:
            break
        if W.is_inline_script_word(araw):
            if remaining > 0:
                remaining -= 1
            _inplace_fix_span(d, source, a.span.start, a.span.end)
            leading = False
            continue
        if leading:
            leading = False
            operand = _decode(araw)
            if operand:
                rec = W.fallback_registration(operand)
                if rec is not None:
                    d.names5.append(rec.name)
                else:
                    ur = W.unsupported_record(operand)
                    if ur is not None and operand not in d.unsupported5:
                        d.unsupported5.append(operand)
        if remaining > 0:
            remaining -= 1


def _handle_shell_command_word(d: CommandDecision, source: str, command: "CommandNode", cw: Any, val: str) -> None:
    """slot 5: a command-position redundant shell - unwrap (drop the shell word)
    or repair the -c inline script in place, dispatching to the SAME
    unwrap_decision / shell_c_script_range ported functions the scanner calls."""
    from kimix_native.shell_compat import wrappers as W

    decision = W.unwrap_decision(
        _reader(source),
        source,
        cw.span.end,
        len(source),
        assignment_prefix=bool(command.assignments),
        wrapped=False,
    )
    if decision.action == "unwrap" and decision.scan_index is not None:
        d.slot5_edits.append((cw.span.start, decision.scan_index, ""))
        d.shells5.append(val)
        d.consumed_end = max(d.consumed_end, decision.scan_index)
        unwrapped_cmd = None
        after_cmd = decision.scan_index
        for a in command.arguments:
            if a.span.start < decision.scan_index:
                continue
            aval = _decode(_raw(a, source))
            unwrapped_cmd = aval
            after_cmd = a.span.end  # the unwrapped word is the new COMMAND word
            rec = W.fallback_registration(aval)
            if rec is not None:
                d.names5.append(rec.name)
            else:
                ur = W.unsupported_record(aval)
                if ur is not None and aval not in d.unsupported5:
                    d.unsupported5.append(aval)
            break
        # the words after the unwrapped command word are its ARGUMENTS: the
        # unwrapped command drives the pattern suppression (bash rg /tmp/z ->
        # /tmp/z is the first bare operand = the pattern, NOT a path).
        _walk_arguments(
            d, source, command, start_after=after_cmd,
            pattern_word=unwrapped_cmd,
        )
        return
    if decision.action == "fix_script" and decision.script_span is not None:
        k, se = decision.script_span
        _inplace_fix_span(d, source, k, se)
        d.consumed_end = max(d.consumed_end, se)
        return
    # keep (with optional harvest): the scanner harvests then retries the
    # in-place -c repair for expansion-bearing / refused-unwrap forms.
    if decision.harvest and decision.script_span is not None:
        _harvest_span(d, source, decision.script_span[0], decision.script_span[1])
    found = W.shell_c_script_range(_reader(source), source, cw.span.end, len(source))
    if found is not None:
        _inplace_fix_span(d, source, found[0], found[1])
        d.consumed_end = max(d.consumed_end, found[1])


# ── the shared command-word classification (plain word + @-strip continuation) ─


def _classify_command_word(
    d: CommandDecision, command: "CommandNode", source: str, cw: Any, val: Optional[str]
) -> None:
    """Run the scanner's per-command word machine for one command word (slot 4-8
    buckets).  Shared by the plain command word and the leading-@ strip's
    continuation."""
    from kimix_native.shell_compat import paths as P, wrappers as W
    from kimix_native.shell_compat.bash_core import (
        _COMMAND_WRAPPERS,
        _SHELL_WRAPPERS,
    )

    raw = _raw(cw, source)
    wkey = _wrapper_key(raw, source)
    is_shell = val in _SHELL_WRAPPERS
    wkind = None if is_shell else W.wrapper_kind_of(wkey)
    is_wrapper = wkind is not None or (not is_shell and wkey in _COMMAND_WRAPPERS)
    # slot 4: the command-word fallback registration / unsupported-name refusal.
    # A command word that is BOTH a table fallback and a fallback-wrapper
    # (gtimeout/sudo/watch) registers its own name here; a plain command wrapper
    # (env/timeout/xargs/nohup) is not a table fallback (rec is None) and
    # registers nothing for its own word; a shell word is never a fallback; an
    # unsupported name is refused only for a plain word.
    rec = W.fallback_registration(val) if (val is not None and not is_shell) else None
    if rec is not None:
        d.names4.append(rec.name)
    elif not is_shell and not is_wrapper:
        ur = W.unsupported_record(val) if val is not None else None
        if ur is not None and val not in d.unsupported4:
            d.unsupported4.append(val)
    consumed_word = rec is not None or (val is not None and val in d.unsupported4)
    # slot 6: cd /d flag deletion (the cd command word).
    if val == "cd":
        span = P.cd_d_flag_span(command, source)
        if span is not None:
            d.slot6_edits.append((span.start, span.end, ""))
            d.paths6.append("cd /d")
    # slot 6: command-word path when NOT a fallback / unsupported / wrapper /
    # shell / cd.
    if not consumed_word and not is_shell and not is_wrapper and val != "cd":
        _cmd_word_path(d, source, command)

    if is_shell:
        _handle_shell_command_word(d, source, command, cw, val)
    elif is_wrapper:
        _consume_wrapper(d, source, command, wrapper_key=wkey)
    else:
        _reparse = W.reparse_operand_count(val) if val is not None else None
        if _reparse is not None:
            _reparse_operand_words(d, source, command, count=_reparse)
        else:
            # the EFFECTIVE command word is ``val`` (the @-strip / plain word);
            # it drives the pattern-program suppression for the arguments.
            _walk_arguments(d, source, command, pattern_word=val)
    _walk_redirections(d, source, command)
    _emit_embedded(d, source, command)
    _walk_expansions(d, source, command)
    _recurse_expansions(d, source, command)


def decide_at_continuation(d: CommandDecision, command: "CommandNode", source: str) -> bool:
    """Classify the remainder after a leading-@ command word (the 3.5 slot-1 strip
    re-dispatch).  The strip edit itself is slot 1's job (cmd_translator
    rule_cmd_at); this folds the STRIPPED word's command-position decisions into
    the buckets, exactly the scanner's re-dispatch at word_start + 1.  Returns
    False for a @echo off|on statement or a non-strip form (slot 1 owns those
    wholly)."""
    from kimix_native.shell_compat.cmd_translator import at_prefix_rule
    from kimix_native.shell_compat.bash_core import _BashFixScanner

    cw = command.command_word
    raw = _raw(cw, source)
    decision = at_prefix_rule(
        _BashFixScanner(source), source, cw.span.start, cw.span.end, len(source)
    )
    if decision is None:
        # a documented bail (bare @, @@, @echo., quoted-leading @): slot 1
        # records nothing, the engine records nothing -> byte-for-byte passthrough.
        return False
    if decision.mode != "strip":
        # @echo off/on statement: slot 1 emits the whole edit + note.
        return False
    stripped_raw = raw[1:]
    # A stripped cmd if/for keyword is owned by slot 2, but the tolerant parser
    # tiles the command word as the whole @if / @for (so rule_cmd_control's raw
    # == "if"/"for" arm never fires).  Re-dispatch the structural rule from the
    # keyword's own start index and fold its structural edits + note + the
    # exist-operand path into the buckets, so the @-prefix composes byte-for-byte
    # with the cmd hook (the scanner: strip @, re-read "if", _try_cmd_if).
    if stripped_raw in ("if", "for"):
        return _at_cmd_control(d, command, source, stripped_raw, cw.span.start + 1)
    val = _decode(stripped_raw)
    _classify_command_word(d, command, source, cw, val)
    return True


def _at_cmd_control(d: CommandDecision, command: "CommandNode", source: str,
                    keyword: str, kw_start: int) -> bool:
    """Fold a stripped cmd if/for's structural edits + note (+ the exist
    operand's path) into the slot-5 buckets - the slot-2 rule re-dispatched at
    the keyword's own start index (the @-continuation)."""
    from kimix_native.shell_compat import cmd_translator as CT
    from kimix_native.shell_compat.bash_core import _BashFixScanner

    reader = _BashFixScanner(source)
    kw_end = kw_start + len(keyword)
    if keyword == "if":
        node = CT.cmd_if_node(reader, source, kw_start, kw_end, len(source))
        if node is None:
            # @if not a valid cmd conditional: the scanner falls through to the
            # "if" keyword arm (no edit); the engine records nothing.
            _walk_redirections(d, source, command)
            _walk_expansions(d, source, command)
            _recurse_expansions(d, source, command)
            return True
        d.cmd_notes5.append(node.note)
        seg0 = node.match.segments[0]
        for a, b, r in CT.cmd_if_structural_edits(source, node.match):
            d.cmd_edits5.append((a, b, r))
        if seg0.kind == "exist":
            from kimix_native.shell_compat.paths import path_replacement

            operand = source[seg0.op_start:seg0.op_end]
            if path_replacement(operand) is not None:
                d.cmd_paths5.append(operand)
        d.cmd_consumed5 = seg0.op_end
        return True
    node = CT.cmd_for_node(reader, source, kw_start, kw_end, len(source))
    if node is None:
        _walk_redirections(d, source, command)
        _walk_expansions(d, source, command)
        _recurse_expansions(d, source, command)
        return True
    d.cmd_notes5.append(node.note)
    for a, b, r in CT.cmd_for_structural_edits(source, node.match):
        d.cmd_edits5.append((a, b, r))
    for _lpa, _lpb, _lpr in node.match.list_path_edits:
        d.cmd_paths5.append(source[_lpa:_lpb])
    return True


def decide_command(command: "CommandNode", source: str) -> CommandDecision:
    """Compute command's IR decisions (the scanner's per-command word machine)
    bucketed across 3.5 slots 4-8.  Pure and deterministic: the five slot
    handlers each call this and commit only their bucket, so the dispatcher
    (priority order 4->8) over an orchestrator's source-ordered command walk
    reproduces the live scanner's per-command event order and global order
    byte-for-byte (the translate_ir battery gate).  A command word owned by a
    slot-1/2/3 cmd rule (cmd if/for keyword, @echo off statement) is skipped
    (is_cmd_owned); a leading-@ STRIP re-dispatches its remainder here (the 3.5
    RuleResult.dispatch continuation).  Substitution child contexts recurse (they
    share the OUTER coordinate string)."""
    d = CommandDecision()
    cw = command.command_word
    if cw is None:
        _walk_redirections(d, source, command)
        _walk_expansions(d, source, command)
        _recurse_expansions(d, source, command)
        return d
    raw = _raw(cw, source)
    if raw[:1] == "@":
        if not decide_at_continuation(d, command, source):
            d.is_cmd_owned = True
        return d
    if _cmd_owned(raw):
        d.is_cmd_owned = True
        return d
    val = _decode(raw)
    if raw in _KEYWORD_SKIP:
        # a bash control/compound keyword tiled as a command word: no word
        # decisions here (the scanner leaves it to the keyword state machine);
        # still walk redirections / substitutions so a command-substitution word
        # and a redirection target are fixed.
        _walk_redirections(d, source, command)
        _walk_expansions(d, source, command)
        _recurse_expansions(d, source, command)
        return d
    _classify_command_word(d, command, source, cw, val)
    return d


def _emit_embedded(d: CommandDecision, source: str, command: "CommandNode") -> None:
    """slot 5: re-emit the runtime-reparsed operand owner words the shell/wrapper
    /arg walks did NOT already claim (dedup by span)."""
    claimed = {(e[0], e[1]) for e in d.slot5_edits}
    for e in command.embedded:
        ow = e.owner_word
        if ow is None:
            continue
        if (ow.span.start, ow.span.end) in claimed:
            continue
        _inplace_fix_span(d, source, ow.span.start, ow.span.end)


def _walk_expansions(d: CommandDecision, source: str, command: "CommandNode") -> None:
    """slot 8: collect the command's own IR heredocs' events."""
    from kimix_native.shell_compat.heredoc import heredoc_event_for

    for h in command.heredocs:
        event = heredoc_event_for(source, h)
        if event is not None:
            d.heredoc_events.append(event)


def _recurse_expansions(d: CommandDecision, source: str, command: "CommandNode") -> None:
    """slot 4-8 recursion into command/process-substitution child contexts
    (echo $(tree C:\\a > nul)): the child shares the OUTER coordinate string and
    is scanned inline by the scanner, so its fixes fold into the same buckets."""
    for e in command.expansions:
        child = getattr(e, "child", None)
        if child is None:
            continue
        for node in _commands_in(child):
            _fold(d, decide_command(node, source))


def _fold(d: CommandDecision, child: CommandDecision) -> None:
    d.names4.extend(n for n in child.names4 if n not in d.names4 and n not in d.names5)
    d.unsupported4.extend(u for u in child.unsupported4 if u not in d.unsupported4)
    d.names5.extend(n for n in child.names5 if n not in d.names5)
    d.paths5.extend(child.paths5)
    d.shells5.extend(s for s in child.shells5 if s not in d.shells5)
    d.nuls5.extend(child.nuls5)
    d.unsupported5.extend(u for u in child.unsupported5 if u not in d.unsupported5)
    d.cmd_notes5.extend(child.cmd_notes5)
    d.cmd_paths5.extend(child.cmd_paths5)
    d.paths6.extend(child.paths6)
    d.nuls7.extend(child.nuls7)
    d.slot4_edits.extend(child.slot4_edits)
    d.slot5_edits.extend(child.slot5_edits)
    d.slot6_edits.extend(child.slot6_edits)
    d.slot7_edits.extend(child.slot7_edits)
    d.cmd_edits5.extend(child.cmd_edits5)
    d.heredoc_events.extend(child.heredoc_events)


# ── 3.5 slot 4-8 handlers (genuine IR handlers over a CommandNode) ────────────


def rule_fallback(command: "CommandNode", source: str, context: "TranslationContext") -> Optional["RuleResult"]:
    """3.5 priority-4 handler: command-word fallback registration / unsupported
    refusal (slot 4; no edit)."""
    from kimix_native.shell_compat.translator import RuleResult

    d = decide_command(command, source)
    if d.is_cmd_owned or (not d.names4 and not d.unsupported4):
        return None
    events: List[Tuple[str, str]] = []
    events.extend(("record_name", name) for name in d.names4)
    events.extend(("record_unsupported", name) for name in d.unsupported4)
    return RuleResult(events=tuple(events))


def rule_wrapper(command: "CommandNode", source: str, context: "TranslationContext") -> Optional["RuleResult"]:
    """3.5 priority-5 handler: wrapper / unwrap / runtime-reparse / embedded
    script re-emit (slot 5), incl. the @-strip cmd if/for continuation folded into
    the cmd buckets."""
    from kimix_native.shell_compat.translator import RuleResult

    d = decide_command(command, source)
    if d.is_cmd_owned:
        return None
    events: List[Tuple[str, str]] = []
    events.extend(("record_name", name) for name in d.names5)
    events.extend(("record_path", name) for name in d.paths5)
    events.extend(("record_shell", name) for name in d.shells5)
    events.extend(("record_nul", name) for name in d.nuls5)
    events.extend(("record_unsupported", name) for name in d.unsupported5)
    events.extend(("record_cmd_conditional", note) for note in d.cmd_notes5)
    events.extend(("record_path", p) for p in d.cmd_paths5)
    edits = list(d.slot5_edits) + list(d.cmd_edits5)
    if not edits and not events:
        return None
    consumed = max(d.consumed_end, d.cmd_consumed5 or 0) or None
    return RuleResult(
        extra_edits=tuple(edits),
        events=tuple(events),
        consumed_end=consumed,
    )


def rule_path(command: "CommandNode", source: str, context: "TranslationContext") -> Optional["RuleResult"]:
    """3.5 priority-6 handler: Windows / virtual-absolute path rewrites + the
    cmd cd /d flag deletion (slot 6)."""
    from kimix_native.shell_compat.translator import RuleResult

    d = decide_command(command, source)
    if d.is_cmd_owned or not d.slot6_edits:
        return None
    return RuleResult(
        extra_edits=tuple(d.slot6_edits),
        events=tuple(("record_path", p) for p in d.paths6),
    )


def rule_nul(command: "CommandNode", source: str, context: "TranslationContext") -> Optional["RuleResult"]:
    """3.5 priority-7 handler: null-device redirections (slot 7)."""
    from kimix_native.shell_compat.translator import RuleResult

    d = decide_command(command, source)
    if d.is_cmd_owned or not d.slot7_edits:
        return None
    return RuleResult(
        extra_edits=tuple(d.slot7_edits),
        events=tuple(("record_nul", n) for n in d.nuls7),
    )


def rule_heredoc(command: "CommandNode", source: str, context: "TranslationContext") -> Optional["RuleResult"]:
    """3.5 priority-8 handler: heredoc event collection (slot 8)."""
    from kimix_native.shell_compat.translator import RuleResult

    d = decide_command(command, source)
    if d.is_cmd_owned or not d.heredoc_events:
        return None
    return RuleResult(heredoc_events=tuple(d.heredoc_events))


def register_default_rules(dispatcher: "Any") -> "Any":
    """Fill the 3.5 priority slots 4-8 with the Phase-7 IR handlers.  Slots 1-3
    are filled by translator.register_cmd_rules (the cmd @ / if/for / jump
    handlers); call both to fill the whole 3.5 table.  Returns the dispatcher."""
    from kimix_native.shell_compat.translator import RuleDispatcher

    dispatcher.register(RuleDispatcher.PRIORITY_FALLBACK, "fallback", rule_fallback)
    dispatcher.register(RuleDispatcher.PRIORITY_WRAPPER, "wrapper", rule_wrapper)
    dispatcher.register(RuleDispatcher.PRIORITY_PATH, "path", rule_path)
    dispatcher.register(RuleDispatcher.PRIORITY_NUL, "nul", rule_nul)
    dispatcher.register(RuleDispatcher.PRIORITY_HEREDOC, "heredoc", rule_heredoc)
    return dispatcher


# ── the independent IR translate (byte-equality proof battery entry) ──────────


def translate_ir(command: str) -> Any:
    """The independent IR-ownership orchestration (proof, not the live path).
    Walks the outer command nodes in source order, dispatches each through the
    3.5 rule chain (slots 1-8), honours the @-strip re-dispatch and the outer
    match's consumed extent (TranslationContext.consumed_end / OverlappingEdits
    skip - a nested else-if / loop keyword the tolerant parser re-tiles is
    dropped, per phase6_report 5), renders through ONE shared EditPlan, prepends
    the fallback prefix and applies the heredoc trailing-operator repair LAST.
    Byte-for-byte translator.translate for the covered battery; the LIVE
    fix_bash_command still uses translate (Option A)."""
    from kimix_native.shell_compat.bash_core import BashFix
    from kimix_native.shell_compat.heredoc import _fix_heredoc_trailing_operators
    from kimix_native.shell_compat.translator import (
        OverlappingEdits,
        RuleDispatcher,
        TranslationContext,
        register_cmd_rules,
    )

    if not command:
        return BashFix(command)
    dispatcher = register_default_rules(register_cmd_rules(RuleDispatcher()))
    context = TranslationContext()
    for node in outer_commands(command):
        if node.span.start < context.consumed_end:
            # a nested keyword re-tiled INSIDE an already consumed construct
            # (else-if chain / loop body): the LIVE consumed extent skips it.
            continue
        try:
            dispatcher.dispatch(node, command, context)
        except OverlappingEdits:
            # the tolerant parser re-tiled a construct the outer match consumed;
            # drop the redundant re-dispatch (honours the consumed extent).
            continue

    if not context.has_activity():
        # byte-for-byte fast path: only the heredoc trailing-operator repair can
        # change the source (mirrors fix_bash_command's reuse of collected events
        # when nothing was edited).
        fixed = _fix_heredoc_trailing_operators(command, context.heredoc_events)
        return BashFix(fixed)
    rendered = context.plan.render(command)
    fixed = _fix_heredoc_trailing_operators(rendered, context.heredoc_events or None)
    prefix = context.fallback_prefix()
    return BashFix(
        prefix + fixed,
        tuple(context.names),
        tuple(context.path_notes),
        tuple(context.shell_notes),
        tuple(context.nul_fixes),
        tuple(context.unsupported),
        tuple(context.cmd_conditional_notes),
    )


__all__ = [
    "CommandDecision",
    "decide_at_continuation",
    "decide_command",
    "outer_commands",
    "register_default_rules",
    "rule_fallback",
    "rule_heredoc",
    "rule_nul",
    "rule_path",
    "rule_wrapper",
    "translate_ir",
]
