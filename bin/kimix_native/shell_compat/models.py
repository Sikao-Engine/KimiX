r"""Lossless, tolerant shell IR node types (plan §3.2).

Every node represents source *by reference*: a ``SourceSpan`` into the single
source string owned by the root :class:`ScriptNode`.  Nodes never copy or
normalize source text, so ``render_source(script)`` reproduces the input
byte-for-byte (Phase 2 invariant: recognition may only *add* structure).

Public names:

- ``Node``: base frozen record with a ``NodeKind`` discriminant and the node's
  source span.
- ``NodeKind``: Enum naming the concrete node classes.
- ``WordPartKind``: lexical anatomy of a word region (literal / quote / escape
  / expansion / substitution region).
- ``WordNode``: a token word: span, part list, optional decoded literal value.
- ``RedirectionNode``: operator span, optional fd, target word, ``can_truncate``.
- ``HeredocNode``: operator, delimiter, ``strip_tabs``/``expands`` flags, body
  and terminator spans.
- ``ExpansionNode``: ``$( )`` / ``$(())`` / ``${ }`` / backquote / ``<( )`` /
  ``>( )`` region, with an optional child ``ScriptNode`` when the content is
  executable and statically visible.
- ``CommandNode``: assignments, command word, arguments, redirections,
  heredocs, wrapper hint.
- ``PipelineNode`` / ``CommandListNode``: children joined by control operators,
  source order preserved.
- ``CompoundNode``: recognized function/case/if/for/while/until/select/subshell/
  brace-group constructs.
- ``EmbeddedScriptNode``: quoted runtime-reparsed operand (eval/trap/watch/
  shell ``-c``) with the quote kind needed to serialize back, plus its inner IR.
- ``RawNode``: opaque source — data, or too ambiguous to transform.
- ``NewlineNode``: a preserved logical line break.
- ``ScriptNode``: root — the single source string, top-level children, and the
  diagnostics tuple; ``reconstruct()`` (or ``render_source(script)``) returns
  the input byte-for-byte.
- ``render_source`` / ``walk_nodes`` / ``unglue``: rendering and traversal.
- ``decode_word_literal`` / ``decode_word_literal_partial``: static word-value
  decoders whose ``None`` contract matches the legacy ``_literal_word_value``.
"""

from __future__ import annotations

import dataclasses
from enum import Enum
from typing import Iterator, Optional, Tuple

from kimix_native.shell_compat.spans import Diagnostic, SourceSpan

try:
    import regex as re
except ImportError:  # pragma: no cover
    import re  # stdlib: the used patterns are all stdlib-compatible


class NodeKind(str, Enum):
    """Discriminant for the IR node tagged union."""

    SCRIPT = "script"
    PIPELINE = "pipeline"
    COMMAND_LIST = "command_list"
    COMMAND = "command"
    COMPOUND = "compound"
    WORD = "word"
    REDIRECTION = "redirection"
    HEREDOC = "heredoc"
    EXPANSION = "expansion"
    EMBEDDED = "embedded"
    RAW = "raw"
    NEWLINE = "newline"
    OPERATOR = "operator"


class WordPartKind(str, Enum):
    """Lexical anatomy of one region inside a word."""

    LITERAL = "literal"
    SINGLE_QUOTE = "single_quote"
    DOUBLE_QUOTE = "double_quote"
    ANSI_C_QUOTE = "ansi_c_quote"
    LOCALE_QUOTE = "locale_quote"
    PARAMETER_EXPANSION = "parameter_expansion"
    ARITHMETIC_EXPANSION = "arithmetic_expansion"
    COMMAND_SUBSTITUTION = "command_substitution"
    BACKQUOTE_SUBSTITUTION = "backquote_substitution"
    PROCESS_SUBSTITUTION = "process_substitution"
    ESCAPED_CHARACTER = "escaped_character"
    LINE_CONTINUATION = "line_continuation"
    RAW = "raw"


#: ``(kind, span)`` pair; parts appear in source order inside a word.
WordPart = Tuple[WordPartKind, SourceSpan]


@dataclasses.dataclass(frozen=True, slots=True)
class Node:
    """Base of every IR node: discriminant + source span."""

    kind: NodeKind
    span: SourceSpan


@dataclasses.dataclass(frozen=True, slots=True)
class WordNode(Node):
    """A token word: anatomy in ``parts``, static value in ``literal_value``.

    ``literal_value`` is the value produced *solely by quote removal* — the
    same contract as the legacy ``_BashFixScanner._literal_word_value``:
    ``None`` whenever a parameter/command/arithmetic expansion, a glob/brace/
    tilde expansion, or malformed quoting contributes to the value.  Runtime
    values are never guessed at parse time.
    """

    parts: Tuple[WordPart, ...] = ()
    literal_value: Optional[str] = None

    def __init__(
        self,
        span: SourceSpan,
        parts: Tuple[WordPart, ...] = (),
        literal_value: Optional[str] = None,
    ) -> None:
        Node.__init__(self, NodeKind.WORD, span)
        object.__setattr__(self, "parts", tuple(parts))
        object.__setattr__(self, "literal_value", literal_value)


@dataclasses.dataclass(frozen=True, slots=True)
class RedirectionNode(Node):
    """A redirection operator and target (heredocs are separate nodes).

    ``can_truncate`` says whether the operator can create or truncate the
    target: ``> >| >> <> &> &>>`` always; ``>&``/``&>`` only with a file-word
    target; pure input operators (``< << <<- <<< <&``) never.  Phase 4's
    null-device rule reads exactly this predicate.
    """

    operator_text: str = ""
    operator_span: SourceSpan = SourceSpan(0, 0)
    fd: Optional[int] = None
    target: Optional[WordNode] = None
    can_truncate: bool = False

    def __init__(
        self,
        span: SourceSpan,
        operator_text: str,
        operator_span: SourceSpan,
        fd: Optional[int] = None,
        target: Optional[WordNode] = None,
        can_truncate: bool = False,
    ) -> None:
        Node.__init__(self, NodeKind.REDIRECTION, span)
        object.__setattr__(self, "operator_text", operator_text)
        object.__setattr__(self, "operator_span", operator_span)
        object.__setattr__(self, "fd", fd)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "can_truncate", can_truncate)


# Operators whose target is opened for writing (may create/truncate a file).
_TRUNCATING_REDIRECT_OPERATORS = frozenset(
    {">", ">|", ">>", "<>", "&>", "&>>"}
)
# Operators that never create a file (pure input side).
_INPUT_ONLY_OPERATORS = frozenset({"<", "<<", "<<-", "<<<", "<&"})


@dataclasses.dataclass(frozen=True, slots=True)
class HeredocNode(Node):
    """A ``<<`` / ``<<-`` redirection resolved against its body.

    ``span`` covers the redirection line (operator + delimiter word); the
    BODY is data (never a command context) recorded via ``body_start`` /
    ``body_end`` / ``terminator_span``.  ``body_end is None`` means no
    terminator was found (unterminated heredoc; the body runs to the end of
    the source and a ``unterminated_heredoc`` diagnostic is attached).
    ``expands`` is False for a quoted delimiter; ``strip_tabs`` for ``<<-``.
    ``delimiter_value`` is ``None`` when the delimiter could not be matched
    (quoted ANSI-C spelling producing a non-representable byte, or malformed
    quoting): then no body is claimed and the region stays raw.
    """

    operator_text: str = ""
    operator_span: SourceSpan = SourceSpan(0, 0)
    delimiter_word: Optional[WordNode] = None
    delimiter_value: Optional[str] = None
    strip_tabs: bool = False
    expands: bool = True
    body_start: int = 0
    body_end: Optional[int] = None
    terminator_span: Optional[SourceSpan] = None

    def __init__(
        self,
        span: SourceSpan,
        operator_text: str,
        operator_span: SourceSpan,
        delimiter_word: Optional[WordNode] = None,
        delimiter_value: Optional[str] = None,
        strip_tabs: bool = False,
        expands: bool = True,
        body_start: int = 0,
        body_end: Optional[int] = None,
        terminator_span: Optional[SourceSpan] = None,
    ) -> None:
        Node.__init__(self, NodeKind.HEREDOC, span)
        object.__setattr__(self, "operator_text", operator_text)
        object.__setattr__(self, "operator_span", operator_span)
        object.__setattr__(self, "delimiter_word", delimiter_word)
        object.__setattr__(self, "delimiter_value", delimiter_value)
        object.__setattr__(self, "strip_tabs", strip_tabs)
        object.__setattr__(self, "expands", expands)
        object.__setattr__(self, "body_start", body_start)
        object.__setattr__(self, "body_end", body_end)
        object.__setattr__(self, "terminator_span", terminator_span)


@dataclasses.dataclass(frozen=True, slots=True)
class ExpansionNode(Node):
    """A substitution/expansion region with optional executable child.

    ``expansion_kind`` is one of the ``WordPartKind`` expansion/substitution
    members.  ``child`` is a ``ScriptNode`` only when Bash executes the
    region's content as a command context AND the region is statically
    closed (``$( )``, backquotes, ``<( )``, ``>( )``); parameter/arithmetic
    expansions and unterminated regions keep ``child = None``.
    """

    expansion_kind: WordPartKind = WordPartKind.RAW
    operator_text: str = ""
    child: Optional[ScriptNode] = None

    def __init__(
        self,
        span: SourceSpan,
        expansion_kind: WordPartKind,
        child: Optional[ScriptNode] = None,
        operator_text: str = "",
    ) -> None:
        Node.__init__(self, NodeKind.EXPANSION, span)
        object.__setattr__(self, "expansion_kind", expansion_kind)
        object.__setattr__(self, "child", child)
        object.__setattr__(self, "operator_text", operator_text)


@dataclasses.dataclass(frozen=True, slots=True)
class EmbeddedScriptNode(Node):
    """A quoted operand that the shell re-parses at RUNTIME (an inline script).

    Produced for ``eval``/``trap`` operands, ``watch`` command operands, and
    shell ``-c`` scripts (``sh -c '...'``, ``find -exec sh -c '...'``) whose
    quoting is statically decodable.  ``owner_word`` is the quoted word span,
    ``quote_kind`` records the outer quoting so a later phase can serialize a
    modified script back into the same context, ``inner`` is the IR built from
    the operand's literal value (spans refer to the *inner* text, not the
    outer source — ``inner.source`` is that standalone string).
    """

    owner_word: Optional[WordNode] = None
    quote_kind: Optional[WordPartKind] = None
    inner: Optional[ScriptNode] = None

    def __init__(
        self,
        span: SourceSpan,
        owner_word: Optional[WordNode] = None,
        quote_kind: Optional[WordPartKind] = None,
        inner: Optional[ScriptNode] = None,
    ) -> None:
        Node.__init__(self, NodeKind.EMBEDDED, span)
        object.__setattr__(self, "owner_word", owner_word)
        object.__setattr__(self, "quote_kind", quote_kind)
        object.__setattr__(self, "inner", inner)


@dataclasses.dataclass(frozen=True, slots=True)
class CommandNode(Node):
    """A simple command: ``[assignments] word arguments [redirections]``.

    ``wrapper`` / ``wrapper_hint`` record (the decoded name and the role:
    ``"command"``, ``"shell"``, ``"reparse"``, ``"fallback"``) when the
    command word matches one of the legacy scanner hint tables.  These are
    hints ONLY in Phase 2 — recognition never rewrites anything.
    """

    assignments: Tuple[WordNode, ...] = ()
    command_word: Optional[WordNode] = None
    arguments: Tuple[WordNode, ...] = ()
    redirections: Tuple[RedirectionNode, ...] = ()
    heredocs: Tuple[HeredocNode, ...] = ()
    embedded: Tuple[EmbeddedScriptNode, ...] = ()
    expansions: Tuple[Node, ...] = ()
    wrapper: Optional[str] = None
    wrapper_hint: Optional[str] = None

    def __init__(
        self,
        span: SourceSpan,
        assignments: Tuple[WordNode, ...] = (),
        command_word: Optional[WordNode] = None,
        arguments: Tuple[WordNode, ...] = (),
        redirections: Tuple[RedirectionNode, ...] = (),
        heredocs: Tuple[HeredocNode, ...] = (),
        embedded: Tuple[EmbeddedScriptNode, ...] = (),
        wrapper: Optional[str] = None,
        wrapper_hint: Optional[str] = None,
        expansions: Tuple[Node, ...] = (),
    ) -> None:
        Node.__init__(self, NodeKind.COMMAND, span)
        object.__setattr__(self, "assignments", tuple(assignments))
        object.__setattr__(self, "command_word", command_word)
        object.__setattr__(self, "arguments", tuple(arguments))
        object.__setattr__(self, "redirections", tuple(redirections))
        object.__setattr__(self, "heredocs", tuple(heredocs))
        object.__setattr__(self, "embedded", tuple(embedded))
        object.__setattr__(self, "wrapper", wrapper)
        object.__setattr__(self, "wrapper_hint", wrapper_hint)
        object.__setattr__(self, "expansions", tuple(expansions))


@dataclasses.dataclass(frozen=True, slots=True)
class PipelineNode(Node):
    """Commands joined by single pipeline operators ``|`` ``|&``.

    ``operators[i]`` joins ``commands[i]`` and ``commands[i + 1]``.
    """

    commands: Tuple[Node, ...] = ()
    operators: Tuple[str, ...] = ()

    def __init__(
        self,
        span: SourceSpan,
        commands: Tuple[Node, ...] = (),
        operators: Tuple[str, ...] = (),
    ) -> None:
        Node.__init__(self, NodeKind.PIPELINE, span)
        object.__setattr__(self, "commands", tuple(commands))
        object.__setattr__(self, "operators", tuple(operators))


@dataclasses.dataclass(frozen=True, slots=True)
class CommandListNode(Node):
    """Terms joined by list/control operators (``;`` ``;;`` ``&&`` ``||`` …).

    ``operators[i]`` joins ``children[i]`` and ``children[i + 1]``.  When an
    ``&&``/``||``/``;``/``&`` operator was *terminated by a newline*, the
    operator text is exactly ``""``: the newline (a ``NewlineNode`` child)
    is what separated the terms, which keeps every character owned by one
    node and the top-level tiling lossless.
    """

    children: Tuple[Node, ...] = ()
    operators: Tuple[str, ...] = ()

    def __init__(
        self,
        span: SourceSpan,
        children: Tuple[Node, ...] = (),
        operators: Tuple[str, ...] = (),
    ) -> None:
        Node.__init__(self, NodeKind.COMMAND_LIST, span)
        object.__setattr__(self, "children", tuple(children))
        object.__setattr__(self, "operators", tuple(operators))


#: Compound kinds the tolerant parser recognizes (plan §3.2).
COMPOUND_KINDS = frozenset(
    {
        "subshell",
        "brace_group",
        "function",
        "if",
        "for",
        "while",
        "until",
        "select",
        "case",
    }
)


@dataclasses.dataclass(frozen=True, slots=True)
class CompoundNode(Node):
    """A recognized compound construct (function/case/if/for/group/…).

    ``compound_kind`` is one of :data:`COMPOUND_KINDS`; ``parts`` holds the
    confidently recognized named components (``("body", node)`` pairs, in
    source order).  Constructs that could not be recognized stay ``RawNode``.
    """

    compound_kind: str = ""
    parts: Tuple[Tuple[str, Node], ...] = ()

    def __init__(
        self,
        span: SourceSpan,
        compound_kind: str,
        parts: Tuple[Tuple[str, Node], ...] = (),
    ) -> None:
        Node.__init__(self, NodeKind.COMPOUND, span)
        object.__setattr__(self, "compound_kind", compound_kind)
        object.__setattr__(self, "parts", tuple(parts))

    def part(self, name: str) -> Optional[Node]:
        """The first part recorded under ``name`` (``None`` when absent)."""
        for key, node in self.parts:
            if key == name:
                return node
        return None


@dataclasses.dataclass(frozen=True, slots=True)
class RawNode(Node):
    """Opaque source: data or a region too ambiguous to transform."""

    reason: str = ""

    def __init__(self, span: SourceSpan, reason: str = "") -> None:
        Node.__init__(self, NodeKind.RAW, span)
        object.__setattr__(self, "reason", reason)


@dataclasses.dataclass(frozen=True, slots=True)
class NewlineNode(Node):
    """A preserved logical line break (``\n`` / ``\r\n``)."""

    def __init__(self, span: SourceSpan) -> None:
        Node.__init__(self, NodeKind.NEWLINE, span)


@dataclasses.dataclass(frozen=True, slots=True)
class OperatorNode(Node):
    """A control-operator token kept as a tiling child between commands.

    Grouping nodes (``PipelineNode``/``CommandListNode``) reference operator
    TEXT and own the operator's source inside their span; the operator node
    itself is what makes the flat item stream tile the region exactly.
    """

    operator_text: str = ""

    def __init__(self, span: SourceSpan, operator_text: str = "") -> None:
        Node.__init__(self, NodeKind.OPERATOR, span)
        object.__setattr__(self, "operator_text", operator_text)


@dataclasses.dataclass(frozen=True, slots=True)
class ScriptNode(Node):
    """Root of a parse: the source string + top-level children + diagnostics.

    ``source`` is THE single string every descendant span indexes into;
    ``region`` marks the substring that was parsed as this context (the whole
    string at the root; the inner text for an embedded script, whose
    ``source`` is that standalone string).  Top-level ``children`` must TILE
    the region: every non-whitespace character belongs to some node, which is
    what makes :func:`render_source` byte-for-byte.  ``pending_heredocs``
    keeps the HeredocNodes whose bodies live in the *enclosing* line context.
    Diagnostics are internal notes — never raised, never surfaced as errors.
    """

    source: str = ""
    region: SourceSpan = SourceSpan(0, 0)
    children: Tuple[Node, ...] = ()
    pending_heredocs: Tuple[HeredocNode, ...] = ()
    diagnostics: Tuple[Diagnostic, ...] = ()

    def __init__(
        self,
        span: SourceSpan,
        source: str,
        region: Optional[SourceSpan] = None,
        children: Tuple[Node, ...] = (),
        pending_heredocs: Tuple[HeredocNode, ...] = (),
        diagnostics: Tuple[Diagnostic, ...] = (),
    ) -> None:
        Node.__init__(self, NodeKind.SCRIPT, span)
        object.__setattr__(self, "source", source)
        object.__setattr__(
            self, "region", region if region is not None else span
        )
        object.__setattr__(self, "children", tuple(children))
        object.__setattr__(self, "pending_heredocs", tuple(pending_heredocs))
        object.__setattr__(self, "diagnostics", tuple(diagnostics))

    def reconstruct(self) -> str:
        """Return the region rebuilt from top-level children (lossless)."""
        return render_source(self)


def render_source(node: Node, source: Optional[str] = None) -> str:
    """Render a ScriptNode's region byte-for-byte from its children.

    The walk is the *gap render*: for each top-level child, the whitespace
    gap before it is emitted literally, then the child's own span slice
    (recursive for nested ``ScriptNode`` regions, which re-apply the same
    rule on their own range).  No node is ever dropped: a child starting
    before the running cursor cannot happen (the parser tiles in source
    order) but is skipped defensively rather than duplicating text.  For
    non-script nodes the function returns the node's source slice directly,
    which is trivially lossless.
    """
    if not isinstance(node, ScriptNode):
        if not isinstance(node, Node):
            raise TypeError(f"render_source expects a Node, got {type(node)!r}")
        if source is None:
            raise ValueError(
                "render_source of a non-script node requires the source string"
            )
        return node.span.text(source)
    source = node.source
    out: list[str] = []
    cursor = node.region.start
    for child in node.children:
        start = child.span.start
        if start > cursor:
            out.append(source[cursor:start])
        elif start < cursor:
            continue  # defensive: never duplicate overlapping text
        if child.kind is NodeKind.SCRIPT:
            out.append(render_source(child))
        else:
            out.append(source[child.span.start:child.span.end])
        cursor = child.span.end
    if cursor < node.region.end:
        out.append(source[cursor:node.region.end])
    return "".join(out)


def unglue(source: str, node: Node) -> str:
    """Return the exact source text a node stands for (debug/inspection)."""
    return node.span.text(source)


def walk_nodes(node: Node) -> Iterator[Node]:
    """Yield ``node`` and every descendant in deterministic source order.

    Recursion is explicit (a worklist) so arbitrarily deep trees traverse
    without touching Python's recursion limit.  ``ScriptNode`` children are
    traversed after their parent; embedded-script inner sources are included.
    """
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        children: list[Node] = []
        kind = current.kind
        if kind is NodeKind.SCRIPT:
            children.extend(reversed(current.children))
            children.extend(reversed(current.pending_heredocs))
        elif kind is NodeKind.COMMAND_LIST:
            children.extend(reversed(current.children))
        elif kind is NodeKind.PIPELINE:
            children.extend(reversed(current.commands))
        elif kind is NodeKind.COMMAND:
            children.extend(reversed(current.assignments))
            if current.command_word is not None:
                children.append(current.command_word)
            children.extend(reversed(current.arguments))
            children.extend(reversed(current.redirections))
            children.extend(reversed(current.heredocs))
            children.extend(reversed(current.embedded))
            children.extend(reversed(current.expansions))
        elif kind is NodeKind.REDIRECTION:
            if current.target is not None:
                children.append(current.target)
        elif kind is NodeKind.HEREDOC:
            if current.delimiter_word is not None:
                children.append(current.delimiter_word)
        elif kind is NodeKind.EXPANSION:
            if current.child is not None:
                children.append(current.child)
        elif kind is NodeKind.EMBEDDED:
            if current.owner_word is not None:
                children.append(current.owner_word)
            if current.inner is not None:
                children.append(current.inner)
        elif kind is NodeKind.COMPOUND:
            children.extend(reversed([n for _k, n in current.parts]))
        stack.extend(children)


# ── static word-value decoding ──────────────────────────────────────────────

_LITERAL_SPECIAL_RE = re.compile(r"[\\'\"$`*?[{~]")


def decode_word_literal(raw: str) -> Optional[str]:
    """Return the word value produced solely by bash quote removal.

    The contract is the legacy scanner's (``_BashFixScanner._literal_word_value``):
    ``None`` when a parameter/command/arithmetic expansion, a backquote
    substitution, glob/brace/tilde expansion, or malformed quoting can
    contribute to the value — only statically decodable words get a literal.
    """
    m = _LITERAL_SPECIAL_RE.search(raw)
    if m is None:
        return raw
    value: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        m = _LITERAL_SPECIAL_RE.search(raw, i)
        if m is None:
            value.append(raw[i:])
            break
        if m.start() > i:
            value.append(raw[i : m.start()])
            i = m.start()
        ch = raw[i]
        if ch == "\\":
            if i + 1 >= n:
                return None  # trailing lone backslash
            if raw[i + 1] == "\n":  # line continuation joins the value
                i += 2
                continue
            value.append(raw[i + 1])
            i += 2
            continue
        if ch == "'":
            close = raw.find("'", i + 1)
            if close < 0:
                return None  # unterminated single quote
            value.append(raw[i + 1 : close])
            i = close + 1
            continue
        if ch == '"':
            i += 1
            while i < n and raw[i] != '"':
                inner = raw[i]
                if inner in "$`*?[":
                    return None  # runtime expansion inside the region
                if inner == "\\":
                    if i + 1 >= n:
                        return None
                    nxt = raw[i + 1]
                    if nxt in ('"', "\\", "$", "`"):
                        value.append(nxt)
                        i += 2
                        continue
                    if nxt == "\n":
                        i += 2  # escaped newline: line continuation
                        continue
                    value.append("\\")
                    i += 1
                    continue
                value.append(inner)
                i += 1
            if i >= n:
                return None  # unterminated double quote
            i += 1
            continue
        if ch == "$":
            nxt = raw[i + 1 : i + 2]
            if nxt in ("(", "{", "`", "'"):
                return None  # $( ${ $(( $' expansions are runtime values
            value.append("$")
            i += 1
            continue
        if ch == "`":
            return None  # backquote substitution: runtime value
        if ch in "*?[{~":
            return None  # glob / brace / tilde expansion: runtime value
        value.append(ch)
        i += 1
    return "".join(value)


def decode_word_literal_partial(raw: str) -> str:
    """Best-effort decode: static quotes removed, expansions kept verbatim.

    Used for *hint recording* inside words whose full value is runtime-dependent
    (``"rev $x"`` decodes ``rev $x``).  No rewrite decision may rely on this
    value — :func:`decode_word_literal` gates every transformation.
    """
    value: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == "\\":
            if i + 1 < n and raw[i + 1] != "\n":
                value.append(raw[i + 1])
                i += 2
            else:
                i += 1
            continue
        if ch == "'":
            close = raw.find("'", i + 1)
            if close < 0:
                value.append(raw[i:])
                break
            value.append(raw[i + 1 : close])
            i = close + 1
            continue
        if ch == '"':
            close = _find_quote_run_end(raw, i + 1, '"')
            if close < 0:
                value.append(raw[i:])
                break
            value.append(raw[i + 1 : close])
            i = close + 1
            continue
        value.append(ch)
        i += 1
    return "".join(value)


def _find_quote_run_end(raw: str, i: int, quote: str) -> int:
    """Index of the closing ``quote`` after position ``i``, or ``-1``."""
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == quote:
            return i
        i += 1
    return -1


__all__ = [
    "COMPOUND_KINDS",
    "OperatorNode",
    "CommandListNode",
    "CommandNode",
    "CompoundNode",
    "EmbeddedScriptNode",
    "ExpansionNode",
    "HeredocNode",
    "NewlineNode",
    "Node",
    "NodeKind",
    "PipelineNode",
    "RawNode",
    "RedirectionNode",
    "ScriptNode",
    "WordNode",
    "WordPart",
    "WordPartKind",
    "decode_word_literal",
    "decode_word_literal_partial",
    "render_source",
    "unglue",
    "walk_nodes",
]
