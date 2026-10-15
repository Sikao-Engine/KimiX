"""Spans, edits, and diagnostics — the coordinate layer of the lossless shell IR.

Part of Phase 2 of the ``_shell_compat`` refactor (plan §3.2) and of the lossless
tolerant-IR ``kimix_native.shell_compat`` implementation package behind the stable
``_shell_compat`` facade: every IR node references the single source string
through these primitives; nothing here copies, normalizes, or re-encodes
source text.

Public names:

- ``SourceSpan``: immutable ``[start, end)`` coordinates into the original
  source string, with ``text(source)``, ``length``, adjacency/overlap helpers
  and ``union``/``join`` for merging spans.
- ``Edit``: an immutable ``(start, end, replacement)`` splice in
  original-source coordinates (the unit later phases collect before applying).
- ``DiagnosticKind`` / ``Diagnostic``: internal parse notes (unterminated
  construct, depth limit, ambiguous region).  Diagnostics are *data*: they are
  collected on the tree and are never raised as exceptions to callers.

The module is dependency-free (stdlib dataclasses only) and fully slotted so
the IR stays cheap on large inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


@dataclass(frozen=True, slots=True)
class SourceSpan:
    """A half-open ``[start, end)`` region of the original source string."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0:
            raise ValueError(f"span start must be >= 0, got {self.start}")
        if self.end < self.start:
            raise ValueError(
                f"span end must be >= start, got start={self.start} end={self.end}"
            )

    @property
    def length(self) -> int:
        """Number of source characters covered by the span."""
        return self.end - self.start

    def text(self, source: str) -> str:
        """The exact source substring this span refers to (byte-for-byte)."""
        return source[self.start : self.end]

    def empty(self) -> bool:
        """True when the span covers no characters."""
        return self.start == self.end

    def adjacent_to(self, other: SourceSpan) -> bool:
        """True when ``other`` starts exactly where this span ends (or vice versa)."""
        return self.end == other.start or other.end == self.start

    def overlaps(self, other: SourceSpan) -> bool:
        """True when the two spans share at least one character position."""
        return self.start < other.end and other.start < self.end

    def contains(self, other: SourceSpan) -> bool:
        """True when ``other`` lies entirely inside this span."""
        return self.start <= other.start and other.end <= self.end

    def union(self, other: SourceSpan) -> SourceSpan:
        """The smallest span covering both spans (fills any gap between them)."""
        return SourceSpan(min(self.start, other.start), max(self.end, other.end))

    def join(self, other: SourceSpan) -> SourceSpan:
        """Union of two *adjacent or overlapping* spans; rejects disjoint input.

        Adjacency is the common IR case (a word's quote region glued to its
        literal run); a silent gap-filling union there would let a caller
        fabricate a span covering text it never lexed.
        """
        if self.end < other.start or other.end < self.start:
            raise ValueError(
                f"join requires touching spans: {self} / {other} are disjoint"
            )
        return self.union(other)


@dataclass(frozen=True, slots=True)
class Edit:
    """A replacement splice ``source[start:end] -> replacement``.

    Coordinates are in the ORIGINAL source; later phases apply disjoint edits
    bottom-up so every span stays meaningful while the plan is built.
    """

    start: int
    end: int
    replacement: str = ""

    def __post_init__(self) -> None:
        if self.start < 0:
            raise ValueError(f"edit start must be >= 0, got {self.start}")
        if self.end < self.start:
            raise ValueError(
                f"edit end must be >= start, got start={self.start} end={self.end}"
            )

    @property
    def span(self) -> SourceSpan:
        """The region this edit replaces."""
        return SourceSpan(self.start, self.end)

    def adjacent_to(self, other: Edit) -> bool:
        """True when the two edits touch without overlapping."""
        return self.end == other.start or other.end == self.start

    def overlaps(self, other: Edit) -> bool:
        """True when the two edits share at least one source position."""
        return self.start < other.end and other.start < self.end

    def sorted_disjoint(self, edits: list[Edit]) -> list[Edit]:
        """Return ``edits`` sorted by start, rejecting overlaps/descending spans.

        ``ValueError`` here is a *programming* error (a malformed plan), not a
        parse outcome: callers building plans from IR diagnostics must
        deduplicate or split first.  Rendering an empty plan returns the
        source unchanged, so a plan that fails this check is never rendered.
        """
        ordered = sorted(edits, key=lambda e: (e.start, e.end))
        previous: Optional[Edit] = None
        for edit in ordered:
            if previous is not None and edit.start < previous.end:
                raise ValueError(
                    f"overlapping edits: {previous.span} / {edit.span}"
                )
            previous = edit
        return ordered


class DiagnosticKind(str, Enum):
    """Why the parser stopped recognizing structure at a position.

    Every kind is a *preserve-source* signal: the region stays byte-for-byte
    (as a ``RawNode`` or a partially built node) for Bash to interpret.
    """

    UNTERMINATED_QUOTE = "unterminated_quote"
    UNTERMINATED_SUBST = "unterminated_subst"
    UNTERMINATED_HEREDOC = "unterminated_heredoc"
    DEPTH_LIMIT = "depth_limit"
    AMBIGUOUS_REGION = "ambiguous_region"
    UNEXPECTED_TOKEN = "unexpected_token"


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """An internal parse note; never raised, never surfaced as a failure."""

    kind: DiagnosticKind
    span: SourceSpan
    message: str = ""


# Compatibility alias mentioned by the refactor plan: a "parse note" is just
# an internal diagnostic.
ParseNote = Diagnostic
