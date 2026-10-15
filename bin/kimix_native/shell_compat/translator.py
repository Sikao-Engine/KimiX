r"""Ordered Git Bash compatibility translator skeleton (plan §3.5).

Phase 5 wires the FALLBACK / WRAPPER / REPARSE / UNWRAP rules (ported to
:mod:`kimix_native.shell_compat.wrappers`) behind the §3.5 source-order rule
dispatcher skeleton; the rules not yet ported (cmd ``if``/``for``/``@`` —
Phase 6 — and heredoc repair — Phase 7) stay in the live scanner.  The LIVE
scanner does NOT run its orchestration through this module yet (Phase 7
switches it); what this phase establishes is the single translation core the
scanner's rendering/prefix builders delegate to, so there is ONE
implementation of:

* the ``EditPlan`` contract of plan §3.5 — store ``(start, end, replacement)``
  in original-source coordinates, reject overlaps or descending spans
  immediately (``EditPlan.add``), render once by sorted splice
  (``render_splices``), and return the ORIGINAL string object unchanged when
  no edit exists — the live scanner depends on the object identity
  (``source is self.s``) to reuse its heredoc-event positions, so the
  delegation must keep returning the input object itself (byte-identical
  behaviour, locked by the Phase-5 differential);
* the fallback prefix contract — ``wrappers.fallback_prefix`` (definitions +
  conditional ``export -f`` lines) emitted ONLY when at least one fallback
  was registered, in source discovery order (deduplicated with
  ``dict.fromkeys``);
* the ``TranslationContext`` report model — names/path_notes/shell_notes/
  nul_fixes/unsupported/cmd_conditional_notes recorded in source-discovery
  order, merged from nested script translations by the LIVE merge rules
  (names/shell_notes/unsupported deduplicated against the OUTER list at
  merge time; path/nul/cmd notes extended verbatim), so a rendered ``BashFix``
  keeps the byte-exact tuple orderings the suites lock;
* the source-order rule dispatcher skeleton (``RuleDispatcher``) with the
  plan §3.5 handler-priority slots; Phase 5 registers the fallback /
  wrapper / reparse / unwrap rules against IR command nodes (see
  ``wrappers.classify_command_node`` / ``reparse_operand_index`` /
  ``unwrap_decision_for_command``); Phases 6-7 fill the cmd and heredoc
  slots and switch the orchestration.

Import structure: this module imports ``wrappers`` (and through it
``bash_core``) at load; ``bash_core`` imports this module LAZILY inside the
delegate bodies (Phase-3/4/5 pattern), so no load-time cycle exists.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, Callable, List, Optional, Tuple

from kimix_native.shell_compat.fallback_data import (
    _FALLBACKS,
    _single_quote,
)

if TYPE_CHECKING:  # pragma: no cover
    from kimix_native.shell_compat.models import CommandNode
    from kimix_native.shell_compat.spans import SourceSpan
    from kimix_native.shell_compat.wrappers import CommandClass


class OverlappingEdits(ValueError):
    """A plan edit overlaps another or runs descending — never rendered.

    ``ValueError`` here is a programming error (the translator scheduled two
    conflicting splices), not a parse outcome: malformed input never reaches
    it because every scheduled span comes from a validated word boundary.
    """


@dataclasses.dataclass(frozen=True, slots=True)
class PlanEdit:
    """One ``(start, end, replacement)`` splice in original-source coordinates."""

    start: int
    end: int
    replacement: str = ""


def render_splices(source: str, edits: List[Tuple[int, int, str]]) -> str:
    """Apply ``(start, end, replacement)`` splices to *source* once, sorted.

    Byte-for-byte the legacy ``_BashFixScanner._apply_edits`` behaviour,
    including the identity contract: with no edits the input string object
    itself is returned (``is``-identity, not just equality) so callers can
    reuse positions collected against the original source.
    """
    if not edits:
        return source
    pieces: List[str] = []
    previous = 0
    for start, end, replacement in sorted(edits):
        pieces.extend((source[previous:start], replacement))
        previous = end
    pieces.append(source[previous:])
    return "".join(pieces)


class EditPlan:
    """Central edit collector with the plan §3.5 properties.

    * rejects overlaps and descending spans immediately (``add``);
    * renders once by sorted splice (``render`` — the shared
      ``render_splices``);
    * returns the ORIGINAL string object unchanged when no edit exists;
    * records nothing else: report events live on the TranslationContext.
    """

    __slots__ = ("_edits",)

    def __init__(self) -> None:
        self._edits: List[Tuple[int, int, str]] = []

    def __len__(self) -> int:
        return len(self._edits)

    @property
    def edits(self) -> Tuple[Tuple[int, int, str], ...]:
        """The recorded splices in recording order (a snapshot tuple)."""
        return tuple(self._edits)

    def add(self, start: int, end: int, replacement: str) -> None:
        """Record a splice, rejecting overlaps and descending spans.

        The overlap test compares against every recorded splice (insertion
        order irrelevant): a late-discovered earlier edit must not slip past
        an already-scheduled overlapping splice.  Adjacency is allowed
        (``end == next start``); equal spans overlap and are rejected — the
        translator must dedupe or split before scheduling.
        """
        if end < start:
            raise OverlappingEdits(f"descending edit span ({start}, {end})")
        for prev_start, prev_end, _ in self._edits:
            if start < prev_end and prev_start < end:
                raise OverlappingEdits(
                    f"overlapping edits: ({prev_start}, {prev_end}) / "
                    f"({start}, {end})"
                )
        self._edits.append((start, end, replacement))

    def add_span(self, span: "SourceSpan", replacement: str) -> None:
        """``add`` over a ``SourceSpan`` with ``.start``/``.end``."""
        self.add(span.start, span.end, replacement)

    def render(self, source: str) -> str:
        """Apply all splices by sorted splice; unchanged input returns itself."""
        return render_splices(source, self._edits)


class TranslationContext:
    """Ordered report collector + edit plan for one translation pass.

    Mirrors the scanner's public report attributes (``names``,
    ``path_notes``, ``shell_notes``, ``nul_fixes``, ``unsupported``,
    ``cmd_conditional_notes``) so the LIVE scanner's thin delegates and the
    future IR pass feed the SAME tuple orderings ``BashFix`` exposes.  Events
    are appended in source-discovery order; ``merge_inner`` implements the
    nested-script merge rules the LIVE scanner uses at its three reparse
    sites; ``fallback_prefix`` generates the §3.5 prefix (via the single
    ``wrappers.fallback_prefix`` implementation).
    """

    __slots__ = (
        "names",
        "path_notes",
        "shell_notes",
        "nul_fixes",
        "unsupported",
        "cmd_conditional_notes",
        "plan",
    )

    def __init__(self) -> None:
        self.names: List[str] = []
        self.path_notes: List[str] = []
        self.shell_notes: List[str] = []
        self.nul_fixes: List[str] = []
        self.unsupported: List[str] = []
        self.cmd_conditional_notes: List[str] = []
        self.plan = EditPlan()

    # ── recording (source order preserved by construction) ────────────────

    def record_name(self, name: str) -> None:
        """Register a fallback name.

        Duplicate names are legal here: the LIVE ``names`` tuple keeps every
        discovery (replacements reports one entry per occurrence), while the
        prefix generator deduplicates — exactly the scanner contract.
        """
        self.names.append(name)

    def record_path(self, raw: str) -> None:
        self.path_notes.append(raw)

    def record_shell(self, name: str) -> None:
        self.shell_notes.append(name)

    def record_nul(self, raw: str) -> None:
        self.nul_fixes.append(raw)

    def record_unsupported(self, name: str) -> None:
        """Unsupported refusals dedupe in the LIVE scanner — first wins."""
        if name not in self.unsupported:
            self.unsupported.append(name)

    def record_cmd_conditional(self, note: str) -> None:
        self.cmd_conditional_notes.append(note)

    # ── nested-script merge (LIVE rules) ──────────────────────────────────

    def merge_inner(self, inner: "TranslationContext") -> None:
        """Merge a nested script translation's events exactly like the LIVE
        scanner merge blocks (``_inline_script_operand`` /
        ``_fix_shell_c_script_in_place`` / ``_handle_shell_wrapper``).

        The LIVE dedupe predicate is ``n not in self.<list>`` (tested against
        the OUTER list at merge time, so repeated inner names collapse even
        when the outer list grows during the loop); path/nul/cmd notes
        extend verbatim.
        """
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

    # ── fallback prefix (plan §3.5: only when >= 1 fallback registered) ───

    def fallback_prefix(self) -> str:
        """Definitions + conditional exports (single shared implementation)."""
        from kimix_native.shell_compat.wrappers import fallback_prefix

        return fallback_prefix(self.names)

    def has_activity(self) -> bool:
        """The LIVE ``fix()`` early-out predicate (nothing recorded → the
        source returns byte-for-byte with no prefix)."""
        return bool(
            self.names
            or len(self.plan)
            or self.shell_notes
            or self.nul_fixes
            or self.unsupported
            or self.cmd_conditional_notes
        )


@dataclasses.dataclass(frozen=True)
class RuleResult:
    """What one source-order rule handler returned for the dispatcher.

    ``edit`` is an optional splice the dispatcher validates into the plan;
    ``extra_edits`` is the tuple of additional splices the same rule schedules
    (Phase 6: the cmd ``if``/``for`` structural rules emit a whole disjoint
    edit SET per validated match — header, operand, paren-close, comma,
    loop-var and ``; done`` splices — so a single ``edit`` field cannot carry
    them; every entry crosses the SAME overlap-validating collector);
    ``events`` are report records to append (each an ``(method_name,
    argument)`` pair against the context ``record_*`` API); ``dispatch``
    names a re-dispatch the dispatcher must honour (``None`` normally;
    reserved for the Phase-6/7 rule completions, e.g. the ``@``-strip
    re-dispatch).
    """

    edit: Optional[Tuple[int, int, str]] = None
    events: Tuple[Tuple[str, str], ...] = ()
    dispatch: Optional[str] = None
    extra_edits: Tuple[Tuple[int, int, str], ...] = ()


class RuleDispatcher:
    """Minimal §3.5 source-order dispatcher skeleton.

    Rules register under an integer priority = the plan's handler order
    (1 cmd ``@`` / 2 cmd if/for / 3 unsupported jumps / 4 fallback
    registration / 5 wrapper & runtime-reparse / 6 path & cd / 7 nul /
    8 heredoc).  Phase 5 populates slots 4-5 (fallback registration and the
    wrapper/reparse/unwrap rules — exercised by
    ``tests/unit/tools/test_bash_wrappers_ir.py`` against the LIVE scanner
    outcomes); Phases 6-7 fill the remaining slots and switch the scanner
    orchestration onto this dispatcher.  A handler is ``f(command, source,
    context) -> RuleResult | None``; the dispatcher walks the caller's
    source-ordered command nodes and feeds the shared context, so every
    edit crosses the single overlap-validating collector.
    """

    #: Handler order from plan §3.5 (the phases fill slots as they port).
    PRIORITY_CMD_AT = 1
    PRIORITY_CMD_CONTROL = 2
    PRIORITY_UNSUPPORTED = 3
    PRIORITY_FALLBACK = 4
    PRIORITY_WRAPPER = 5
    PRIORITY_PATH = 6
    PRIORITY_NUL = 7
    PRIORITY_HEREDOC = 8

    def __init__(self) -> None:
        self._rules: List[Tuple[int, str, Callable[..., Optional[RuleResult]]]] = []

    def register(
        self,
        priority: int,
        name: str,
        handler: Callable[..., Optional[RuleResult]],
    ) -> None:
        self._rules.append((priority, name, handler))
        self._rules.sort(key=lambda item: item[0])

    @property
    def rules(self) -> Tuple[Tuple[int, str], ...]:
        """Registered ``(priority, name)`` pairs in dispatch order."""
        return tuple((priority, name) for priority, name, _ in self._rules)

    def dispatch(
        self, command: "CommandNode", source: str, context: TranslationContext
    ) -> None:
        """Run every registered rule for *command* in priority order.

        A rule returning an edit (or extra edits) gets them validated into the
        plan (overlap rejection at the single collecting point); event tuples
        are applied against the context record API in rule order.
        """
        for _priority, _name, handler in self._rules:
            result = handler(command, source, context)
            if result is None:
                continue
            if result.edit is not None:
                context.plan.add(*result.edit)
            for splice in result.extra_edits:
                context.plan.add(*splice)
            for method, argument in result.events:
                getattr(context, method)(argument)


def register_cmd_rules(dispatcher: RuleDispatcher) -> RuleDispatcher:
    """Fill the §3.5 priority slots 1-3 with the Phase-6 cmd handlers.

    Registers ``cmd_translator.rule_cmd_at`` (leading ``@``),
    ``cmd_translator.rule_cmd_control`` (cmd ``if``/``for`` structural rules)
    and ``cmd_translator.rule_cmd_jump`` (unsupported jump refusals) on
    *dispatcher* under the plan's handler priorities 1/2/3.  The import is lazy
    (``cmd_translator`` imports ``bash_core``, which imports ``translator``
    lazily — no load-time cycle).  Returns the dispatcher for chaining.
    """
    from kimix_native.shell_compat.cmd_translator import (
        rule_cmd_at,
        rule_cmd_control,
        rule_cmd_jump,
    )

    dispatcher.register(
        RuleDispatcher.PRIORITY_CMD_AT, "cmd_at", rule_cmd_at
    )
    dispatcher.register(
        RuleDispatcher.PRIORITY_CMD_CONTROL, "cmd_control", rule_cmd_control
    )
    dispatcher.register(
        RuleDispatcher.PRIORITY_UNSUPPORTED, "cmd_jump", rule_cmd_jump
    )
    return dispatcher


# ── shared nested-script repair helpers (used by the dispatcher rules) ──────


def translate_inner(inner: Any) -> Tuple[str, bool]:
    """Return ``(fixed_source, has_edits)`` for a scanned inner scanner.

    ``inner`` exposes ``_build_fixed_source()`` and ``edits`` (the live
    scanner object).  Recursion protection already ran in the caller's scan
    step (the LIVE sites catch ``RecursionError`` around the inner
    ``_scan_range`` before calling this).
    """
    fixed = inner._build_fixed_source()
    return fixed, bool(inner.edits)


def single_quote_script(fixed: str) -> str:
    """Serialize a repaired embedded script back as one single-quoted word.

    The LIVE re-emit spelling (``_single_quote``): the modified script text
    is re-quoted with the legacy literal-quoting rule so the outer word's
    runtime value is exactly the fixed script.
    """
    return _single_quote(fixed)


def merge_inner_events(context: TranslationContext, inner: Any) -> None:
    """Merge a scanned inner scanner's events into *context* by the LIVE rules.

    ``inner`` is a scanned live scanner object; its report lists are wrapped
    into a throwaway context and merged with :meth:`TranslationContext.merge_inner`.
    """
    nested = TranslationContext()
    nested.names = list(inner.names)
    nested.path_notes = list(inner.path_notes)
    nested.shell_notes = list(inner.shell_notes)
    nested.nul_fixes = list(inner.nul_fixes)
    nested.unsupported = list(inner.unsupported)
    nested.cmd_conditional_notes = list(inner.cmd_conditional_notes)
    context.merge_inner(nested)


#: Re-exported so the prefix implementation has one import surface (the
#: generator itself lives in ``wrappers.fallback_prefix`` — the scanner,
#: the context and the tests all call the same function).
from kimix_native.shell_compat.wrappers import fallback_prefix  # noqa: E402

__all__ = [
    "EditPlan",
    "OverlappingEdits",
    "PlanEdit",
    "RuleDispatcher",
    "RuleResult",
    "TranslationContext",
    "fallback_prefix",
    "merge_inner_events",
    "register_cmd_rules",
    "render_splices",
    "single_quote_script",
    "translate_inner",
]
