r"""Tolerant command-context parser for the lossless shell IR (plan §3.3).

Part of the lossless tolerant-IR ``kimix_native.shell_compat`` implementation
package behind the stable ``_shell_compat`` facade.

Builds the :mod:`kimix_native.shell_compat.models` IR from the flat token
stream of :mod:`kimix_native.shell_compat.lexer`.  This is deliberately NOT
a POSIX grammar: structure is recognized only where recognition is safe —
command word vs arguments vs assignments, redirections, heredocs, comments,
nested executable substitutions, wrapper hints.  Anything ambiguous stays
:data:`RawNode` and the parser NEVER raises on malformed input.

Tiling invariant (design decision)
-----------------------------------
Every region's top-level segments TILE it: segment spans are in source
order, pairwise non-overlapping, and each one is either a *data* segment
(comment / heredoc body / newline / operator marker / reserved word — which
renders as its own span) or a *term* (command / pipeline / list / compound /
expansion / embedded).  For every term the following identity is enforced by
construction:

    span.text(source) == concatenation(children spans in source order) + gaps

(terms whose children do NOT tile their span — e.g. a command whose
redirection span glues to the operator and would double the fd digits — are
emitted as ``RawNode`` data segments instead, preserving the source).  This
makes ``render_source`` byte-for-byte: a top-level segment renders as its
span slice, except ``ScriptNode`` which renders recursively over its own
tiling children.  Grouped nodes (pipeline/list folds) cover contiguous source
including the operator gaps, so their identity holds trivially.

Segmentation + fold model
-------------------------
Stage 1 (flat segmentation): tokens are walked once per context.  Simple
commands accumulate their words/redirections/heredocs/embedded scripts;
operators, newlines, comments, heredoc bodies, nested expansions and groups
become segments.  Stage 2 (:func:`_fold_segments`): adjacent term segments
separated by a single control-operator segment fold into ``PipelineNode``
(``|``/``|&``) or ``CommandListNode`` (every other operator) with the
operator texts recorded.

Heredoc model: ``<<``/``<<-`` record a pending document with its delimiter
word; the body is claimed when the delimiter LINE ends (legacy terminator
rule: whole-line equality after ``\r``-strip and ``<<-`` tab-strip).  The
body + terminator line become a DATA segment (test 3) and the line's
``HeredocNode`` records ``body_start``/``body_end``/``terminator_span``
while its span stays the operator+delimiter line (so it can stay a command
field without breaking tiling).  Missing terminator → body to the region
end + ``unterminated_heredoc`` diagnostic (region still owned by a data
segment).

Embedded scripts: a whole-quoted operand of a reparse builtin (``eval`` =
all operands, ``trap`` = the first) or of a shell ``-c`` script (after an
exec-operand command word like ``find``/``xargs``) becomes an
``EmbeddedScriptNode``: the operand's literal value is parsed as its OWN
standalone source string (the shell re-parses it at runtime), with the
outer quote kind recorded for serialization.

Depth model (test 6): nested command contexts are queued as worklist tasks
processed by a WHILE loop — descent adds NO Python frames, so arbitrarily
deep input cannot raise ``RecursionError``.  Beyond
``bash_core._MAX_NESTING_DEPTH`` (1024) the region is not parsed: a
``depth_limit`` diagnostic is recorded and its characters are owned by a
``RawNode`` data segment (never an exception).

Hints only: wrapper / shell / reparse recognition imports the legacy tables
from ``bash_core``; Phase 2 changes no public behavior.

Public names: ``parse``, ``parse_command_context``, ``parse_inner``,
``classify_assignment``, ``word_to_node``, ``is_effective_nul_target``,
``COMMAND_POSITION_OPERATORS``, ``DECLARATION_BUILTINS``,
``PIPELINE_OPERATORS``.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from kimix_native.shell_compat.bash_core import (
    _ASSIGNMENT_RE,
    _BashFixScanner,
    _COMMAND_WRAPPERS,
    _EXEC_OPERAND_COMMANDS,
    _MAX_NESTING_DEPTH,
    _REPARSE_BUILTINS,
    _SAME_SHELL_WRAPPERS,
    _SHELL_C_OPTIONS,
    _SHELL_WRAPPERS,
    _find_matching_paren,
)
from kimix_native.shell_compat.lexer import (
    EXECUTABLE_REGIONS,
    HEREDOC_OPERATORS,
    INPUT_ONLY_REDIRECT_OPERATORS,
    LexResult,
    Token,
    TokenKind,
    TRUNCATING_REDIRECT_OPERATORS,
    lex,
)
from kimix_native.shell_compat.models import (
    CommandListNode,
    CommandNode,
    CompoundNode,
    EmbeddedScriptNode,
    ExpansionNode,
    HeredocNode,
    NewlineNode,
    Node,
    NodeKind,
    PipelineNode,
    RawNode,
    RedirectionNode,
    ScriptNode,
    WordNode,
    WordPartKind,
    decode_word_literal,
)
from kimix_native.shell_compat.spans import Diagnostic, DiagnosticKind, SourceSpan

#: Control operators after which the following word is in command position
#: (legacy scanner behaviour: every list/pipeline operator and the opening
#: subshell ``(``; ``)`` closes a group instead).
COMMAND_POSITION_OPERATORS = frozenset(
    {"|", "|&", "&&", "||", ";", ";&", ";;&", "("}
)
#: Declaration builtins whose ``NAME=value`` operands are assignments.
DECLARATION_BUILTINS = frozenset(
    {"declare", "typeset", "local", "export", "readonly"}
)
#: Operators that group terms into pipelines (tighter than list operators).
PIPELINE_OPERATORS = frozenset({"|", "|&"})
#: Reserved words that (re-)open command position inside compounds.
_COMMAND_REOPENERS = frozenset({"then", "do", "else", "elif", "in", "{"})
#: Reserved words that close recognized compound constructs.
_COMMAND_CLOSERS = frozenset({"fi", "done", "esac", "}"})
#: Compound keywords recognized in command position (Phase 2: state tracked,
#: bodies stay command contexts without a folded compound node).
_COMPOUND_KEYWORDS = frozenset({"if", "while", "until", "for", "select", "case"})

__all__ = [
    "COMMAND_POSITION_OPERATORS",
    "DECLARATION_BUILTINS",
    "PIPELINE_OPERATORS",
    "classify_assignment",
    "is_effective_nul_target",
    "keyword_of",
    "parse",
    "parse_command_context",
    "parse_inner",
    "word_to_node",
]

#: Zero-length scanner used ONLY for its stateless delimiter decoder.
_DECODER = _BashFixScanner("")


def classify_assignment(raw_word: str) -> Optional[str]:
    """Return the variable name when ``raw_word`` is a ``NAME[+]=…`` assignment.

    Same predicate as the legacy ``_ASSIGNMENT_RE`` (imported, never copied);
    ``None`` for any other word.
    """
    match = _ASSIGNMENT_RE.match(raw_word)
    if match is None:
        return None
    return raw_word[: match.end() - 1]


def word_to_node(token: Token, source: str) -> WordNode:
    """Build a ``WordNode`` from a WORD token: anatomy + static literal."""
    raw = token.span.text(source)
    return WordNode(
        token.span,
        tuple((kind, span) for kind, span, _inner in token.parts),
        decode_word_literal(raw),
    )


def is_effective_nul_target(word: WordNode, source: str) -> Optional[bool]:
    """Legacy null-device predicate lifted to a parsed redirection target.

    ``True``/``False`` when the value is statically decidable — the legacy
    ``_is_effective_nul_target`` contract: exactly ``nul`` in any case after
    quote removal / ANSI-C expansion; whole-token-quoted ``'nul'``/``"nul"``
    are intentional filenames (``False``).  ``None`` when runtime expansions
    keep the value open — Phase 4 leaves such targets byte-for-byte.
    """
    raw = word.span.text(source)
    if "$" in raw and not raw.startswith(("$'", '$"')):
        return None
    if "`" in raw:
        return None
    value = word.literal_value
    if value is None:
        return None
    return value.casefold() == "nul"


def keyword_of(raw: str, literal: Optional[str]) -> Optional[str]:
    """The reserved-WORD value of a token, or ``None`` when it is not plain.

    Bash recognizes reserved words only in their plain spelling: a quoted
    (``"then"``) or expansion-bearing (``$(x)``, ``{`` brace) word is data.
    When ``literal`` decodes it equals the raw text for a plain word, so the
    keyword is ``literal`` only when it matches ``raw`` exactly.  When
    ``literal`` is ``None`` the word carried quote/expansion characters --
    except operator-shaped keywords like ``{`` / ``}`` which decode to None
    (brace expansion) yet are real reserved words when spelled bare.
    """
    if literal is not None:
        return literal if literal == raw else None
    # bare operator/keyword tokens that decode_word_literal rejects because
    # they contain brace/glob characters but are still reserved words:
    if raw in _BARE_KEYWORD_TOKENS:
        return raw
    return None


#: Words that decode_word_literal maps to None (expansion characters) yet
#: are reserved words in their plain spelling only.  Anything else with
#: quote/expansion characters stays data.
_BARE_KEYWORD_TOKENS = frozenset(
    {"{", "}", "in", "then", "do", "else", "elif", "fi", "done", "esac", "if",
     "while", "until", "for", "case", "select", "time", "coproc", "function"}
)


def _heredoc_delimiter_of(raw: str) -> Optional[Tuple[Optional[str], bool]]:
    """Decode a heredoc delimiter word with the LEGACY scanner method.

    Returns ``(delimiter | None, expands)``; ``None`` for a malformed
    delimiter (bash consumes the rest of the line there: no body claimed).
    """
    return _DECODER._heredoc_delimiter(raw)


def _setattr(node: object, name: str, value: object) -> None:
    """Construction-time field write on a frozen IR node (module-internal).

    Public nodes are immutable; the parser refines heredoc bookkeeping on
    nodes it is still constructing (the same node object is then embedded in
    the tree).  A translator-phase edit planner never sees these writes.
    """
    object.__setattr__(node, name, value)


class _Part:
    """Mutable accumulation of one simple command (parser-internal)."""

    __slots__ = (
        "start",
        "end",
        "assignments",
        "command_word",
        "arguments",
        "redirections",
        "heredocs",
        "embedded",
        "expansions",
        "wrapper",
        "wrapper_hint",
        "command_value",
        "reparse_remaining",
        "shell_c_pending",
        "shell_exec_operand",
        "array_data",
    )

    def __init__(self, start: int) -> None:
        self.start = start
        self.end = start
        self.assignments: List[Node] = []
        self.command_word: Optional[Node] = None
        self.arguments: List[Node] = []
        self.redirections: List[RedirectionNode] = []
        self.heredocs: List[HeredocNode] = []
        self.embedded: List[EmbeddedScriptNode] = []
        self.expansions: List[Node] = []
        self.wrapper: Optional[str] = None
        self.wrapper_hint: Optional[str] = None
        self.command_value: Optional[str] = None
        self.reparse_remaining = 0
        self.shell_c_pending = False
        self.shell_exec_operand = False
        self.array_data = False


class _Task:
    """Queued nested-context job (worklist: iterative, bounded descent)."""

    __slots__ = ("slot", "region", "depth", "standalone", "source_text", "parent_ctx")

    def __init__(
        self,
        slot: "_Slot",
        region: SourceSpan,
        depth: int,
        standalone: bool,
        source_text: str,
        parent_ctx: "_Ctx",
    ) -> None:
        self.slot = slot
        self.region = region
        self.depth = depth
        self.standalone = standalone
        self.source_text = source_text
        self.parent_ctx = parent_ctx


class _Slot:
    """Install point for a parsed child ScriptNode (construction-time)."""

    __slots__ = ("kind", "owner")

    def __init__(self, kind: str, owner: object) -> None:
        self.kind = kind  # "expansion" | "embedded" | "group_body"
        self.owner = owner

    def fill(self, script: ScriptNode) -> None:
        owner = self.owner
        if self.kind == "expansion":
            _setattr(owner, "child", script)
        elif self.kind == "embedded":
            _setattr(owner, "inner", script)
        elif self.kind == "group_body":
            # the placeholder ScriptNode object IS the compound body part:
            # fill it in place so the parent node keeps its identity.
            _setattr(owner, "children", script.children)
            _setattr(owner, "diagnostics", script.diagnostics)


class _Ctx:
    """Segmentation state for ONE command context (root, child, or inner)."""

    __slots__ = (
        "source",
        "region",
        "depth",
        "tokens",
        "diagnostics",
        "segments",
        "parts",
        "pending_docs",
        "command_expected",
        "case_state",
        "compound_state",
        "tasks",
        "index",
        "doc_claim_until",
    )

    def __init__(
        self,
        source: str,
        region: SourceSpan,
        tokens: Tuple[Token, ...],
        depth: int,
        diagnostics: List[Diagnostic],
    ) -> None:
        self.source = source
        self.region = region
        self.depth = depth
        self.tokens = tokens
        self.diagnostics = diagnostics
        self.segments: List[Node] = []
        self.parts: Optional[_Part] = None
        self.pending_docs: List[_Doc] = []
        self.command_expected = True
        self.case_state: Optional[str] = None
        self.compound_state: List[str] = []
        self.tasks: List[_Task] = []
        self.index = 0
        self.doc_claim_until = -1

    # ── segment emission ────────────────────────────────────────────────────

    def _emit(self, node: Node) -> None:
        self.segments.append(node)

    def _flush(self, until: int) -> None:
        p = self.parts
        if p is None:
            return
        self.parts = None
        if not (
            p.assignments
            or p.command_word is not None
            or p.arguments
            or p.redirections
            or p.heredocs
            or p.embedded
            or p.expansions
            or p.array_data
        ):
            return
        end = max(until, p.end)
        self._emit_command(p, end)

    def _emit_command(self, p: _Part, end: int) -> None:
        span = SourceSpan(p.start, end)
        node = CommandNode(
            span,
            tuple(p.assignments),
            p.command_word,
            tuple(p.arguments),
            tuple(p.redirections),
            tuple(p.heredocs),
            tuple(p.embedded),
            p.wrapper,
            p.wrapper_hint,
            tuple(p.expansions),
        )
        if _tiles(node):
            self._emit(node)
        else:
            # ambiguous/non-tiling command shape: preserve source as data
            self._emit(RawNode(span, "non-tiling command shape"))

    # ── driver ──────────────────────────────────────────────────────────────

    def run(self) -> List[Node]:
        tokens = self.tokens
        index = 0
        while index < len(tokens):
            token = tokens[index]
            index += 1
            self.index = index
            kind = token.kind
            if kind is TokenKind.RAW:
                continue
            if self.pending_docs and self._doc_boundary(token, index):
                index = self._claim_docs(token, index)
                if index >= len(tokens):
                    break
                token = tokens[index - 1]
                kind = token.kind
            if kind is TokenKind.NEWLINE:
                self._flush(token.span.start)
                self._emit(NewlineNode(token.span))
                self.command_expected = True
                continue
            if kind is TokenKind.COMMENT:
                # comments are DATA nodes, never command contexts (test 3)
                self._flush(token.span.start)
                self._emit(RawNode(token.span, "comment"))
                continue
            if kind is TokenKind.REDIRECTION:
                index = self._handle_redirection(token, index)
                continue
            if kind is TokenKind.OPERATOR:
                index = self._handle_operator(token, index)
                continue
            index = self._handle_word(token, index)
        self._flush(self.region.end)
        if self.pending_docs:
            self._claim_docs_at(self.region.end, len(tokens))
        return self.segments

    def _doc_boundary(self, token: Token, index: int) -> bool:
        """True when the pending heredoc's delimiter line ends now."""
        if token.kind is TokenKind.NEWLINE:
            return True
        prev_end = self.region.start
        for j in range(index - 2, -1, -1):
            prev = self.tokens[j]
            if prev.kind is TokenKind.RAW:
                continue
            prev_end = prev.span.end
            break
        return "\n" in self.source[prev_end : token.span.start]

    # ── operators ───────────────────────────────────────────────────────────

    def _handle_operator(self, token: Token, index: int) -> int:
        op = token.text
        if op == "(":
            self._flush(token.span.start)
            index = self._parse_group(token, index)
            self.command_expected = True
            return index
        if op == ")":
            self._flush(token.span.start)
            if self.case_state == "patterns":
                # case arm: `)` closes the pattern list, opens the arm body
                self.case_state = "body"
                self._emit(RawNode(token.span, "case-body-start"))
                self.command_expected = True
                return index
            self._emit(RawNode(token.span, "unexpected )"))
            self.diagnostics.append(
                Diagnostic(
                    DiagnosticKind.UNEXPECTED_TOKEN, token.span, "unexpected )"
                )
            )
            self.command_expected = False
            return index
        if op in (";;", ";&", ";;&"):
            self._flush(token.span.start)
            if self.case_state is not None:
                self._emit(RawNode(token.span, f"case-arm-end:{op}"))
                self.case_state = "patterns"
                self.command_expected = True
            else:
                self._emit(RawNode(token.span, f"unexpected {op}"))
                self.diagnostics.append(
                    Diagnostic(
                        DiagnosticKind.UNEXPECTED_TOKEN,
                        token.span,
                        f"unexpected {op} outside a case construct",
                    )
                )
                self.command_expected = True
            return index
        self._flush(token.span.start)
        self._emit(RawNode(token.span, f"op:{op}"))
        self.command_expected = True
        return index

    def _parse_group(self, token: Token, index: int) -> int:
        """Recognize a subshell group ``(`` … ``)`` (flat oracle matcher)."""
        close = _find_matching_paren(self.source, token.span.start)
        if close == -1 or close >= self.region.end:
            span = SourceSpan(token.span.start, self.region.end)
            self.diagnostics.append(
                Diagnostic(
                    DiagnosticKind.UNTERMINATED_SUBST,
                    span,
                    "unmatched ( in command position",
                )
            )
            self._emit(RawNode(span, "unmatched ("))
            return len(self.tokens)
        span = SourceSpan(token.span.start, close + 1)
        body_region = SourceSpan(token.span.end, close)
        if self.depth + 1 >= _MAX_NESTING_DEPTH:
            self.diagnostics.append(
                Diagnostic(
                    DiagnosticKind.DEPTH_LIMIT,
                    span,
                    f"nesting depth limit {_MAX_NESTING_DEPTH} reached; group preserved",
                )
            )
            self._emit(RawNode(span, "depth limit: group"))
        else:
            placeholder = ScriptNode(
                body_region, self.source, body_region, (), (), ()
            )
            self._emit(CompoundNode(span, "subshell", (("body", placeholder),)))
            self.tasks.append(
                _Task(
                    _Slot("group_body", placeholder),
                    body_region,
                    self.depth + 1,
                    standalone=False,
                    source_text=self.source,
                    parent_ctx=self,
                )
            )
        # tokens strictly inside the group belong to the child context: skip.
        while index < len(self.tokens) and self.tokens[index].span.start <= close:
            index += 1
        return index

    # ── redirections / heredocs ─────────────────────────────────────────────

    def _handle_redirection(self, token: Token, index: int) -> int:
        p = self._ensure(token.span.start)
        text = token.span.text(self.source)
        op_core = text.lstrip("0123456789")
        op_start = token.span.start + (len(text) - len(op_core))
        op_span = SourceSpan(op_start, token.span.end)
        p.end = max(p.end, op_span.end)
        target = self._peek_target(token, index)
        if op_core in HEREDOC_OPERATORS:
            return self._record_heredoc(
                p, op_core, op_span, token, target, self._target_index(token, index)
            )
        if op_core == "<<<":
            # herestring target is DATA (the legacy scanner leaves it alone)
            if target is not None:
                word = word_to_node(target, self.source)
                span = SourceSpan(token.span.start, word.span.end)
                node = RedirectionNode(
                    span, text, op_span, token.fd, word, False
                )
                p.redirections.append(node)
                p.end = max(p.end, span.end)
                index = self._target_index(token, index)
                return index
            node = RedirectionNode(token.span, text, op_span, token.fd, None, False)
            p.redirections.append(node)
            return index
        word_node = None
        if target is not None:
            word_node = word_to_node(target, self.source)
            self._collect_regions(target, p)
        span = (
            token.span
            if word_node is None
            else SourceSpan(token.span.start, word_node.span.end)
        )
        node = RedirectionNode(
            span,
            text,
            op_span,
            token.fd,
            word_node,
            _redirection_truncates(op_core, word_node),
        )
        p.redirections.append(node)
        p.end = max(p.end, span.end)
        if target is not None:
            index = self._target_index(token, index)
        return index

    def _peek_target(self, token: Token, index: int) -> Optional[Token]:
        """The redirection's target word: next token if a word on the line."""
        if index >= len(self.tokens):
            return None
        candidate = self.tokens[index]
        if candidate.kind is TokenKind.RAW:
            if index + 1 >= len(self.tokens):
                return None
            candidate = self.tokens[index + 1]
        if candidate.kind is not TokenKind.WORD:
            return None
        if "\n" in self.source[token.span.end : candidate.span.start]:
            return None
        return candidate

    def _target_index(self, token: Token, index: int) -> int:
        """Token index AFTER the redirection's target (past RAW + word)."""
        target = self._peek_target(token, index)
        if target is None:
            return index
        while index < len(self.tokens) and self.tokens[index] is not target:
            index += 1
        return index + 1

    def _record_heredoc(
        self,
        p: _Part,
        op_core: str,
        op_span: SourceSpan,
        token: Token,
        delimiter_token: Optional[Token],
        index: int,
    ) -> int:
        delimiter_word: Optional[WordNode] = None
        delimiter: Optional[str] = None
        expands = True
        line_end = op_span.end
        if delimiter_token is not None:
            scanned = _heredoc_delimiter_of(
                delimiter_token.span.text(self.source)
            )
            if scanned is None:
                self.diagnostics.append(
                    Diagnostic(
                        DiagnosticKind.AMBIGUOUS_REGION,
                        delimiter_token.span,
                        "malformed heredoc delimiter: no body claimed",
                    )
                )
                p.arguments.append(word_to_node(delimiter_token, self.source))
                line_end = delimiter_token.span.end
            else:
                delimiter, expands = scanned
                delimiter_word = word_to_node(delimiter_token, self.source)
                line_end = delimiter_token.span.end
        node = HeredocNode(
            SourceSpan(token.span.start, line_end),
            op_core,
            op_span,
            delimiter_word,
            delimiter,
            op_core == "<<-",
            expands,
            line_end,
            None,
            None,
        )
        p.heredocs.append(node)
        self.pending_docs.append(
            _Doc(op_span, op_core, delimiter_word, delimiter, op_core == "<<-",
                 expands, node)
        )
        p.end = max(p.end, line_end)
        return index

    def _claim_docs(self, token: Token, index: int) -> int:
        """Resolve pending heredoc bodies; returns the new token index."""
        if token.kind is TokenKind.NEWLINE:
            self._flush(token.span.start)
            body_start = token.span.end
            next_index = index
        else:
            self._flush(token.span.start)
            body_start = token.span.start
            next_index = index - 1
        return self._claim_docs_at(body_start, next_index)

    def _claim_docs_at(self, body_start: int, next_index: int) -> int:
        docs = self.pending_docs
        self.pending_docs = []
        source = self.source
        n = self.region.end
        end_for_tokens = body_start
        for doc in docs:
            claimed = self._claim_one_doc(doc, body_start, source, n)
            end_for_tokens = max(end_for_tokens, claimed)
        # body tokens are data: skip them (they belong to the doc segments)
        tokens = self.tokens
        while next_index < len(tokens) and tokens[next_index].span.start < end_for_tokens:
            next_index += 1
        self.index = next_index
        return next_index

    def _claim_one_doc(self, doc: _Doc, body_start: int, source: str, n: int) -> int:
        """Resolve one heredoc body; returns the region end it owns."""
        terminator_start: Optional[int] = None
        terminator_end: Optional[int] = None
        if doc.delimiter is not None:
            scan = body_start
            while scan < n:
                newline = source.find("\n", scan, n)
                line_end = n if newline < 0 else newline
                line = source[scan:line_end].rstrip("\r")
                compare = line.lstrip("\t") if doc.strip_tabs else line
                if compare == doc.delimiter:
                    terminator_start = scan
                    terminator_end = line_end
                    break
                scan = n if newline < 0 else newline + 1
        if terminator_start is None:
            _setattr(doc.node, "body_start", body_start)
            _setattr(doc.node, "body_end", n)
            self.diagnostics.append(
                Diagnostic(
                    DiagnosticKind.UNTERMINATED_HEREDOC,
                    SourceSpan(body_start, n),
                    "unterminated heredoc body",
                )
            )
            self._emit(RawNode(SourceSpan(body_start, n), "heredoc-body: unterminated"))
            return n
        # claim through the terminator line's newline
        nl = source.find("\n", terminator_end or n, n)
        after = n if nl < 0 else nl + 1
        _setattr(doc.node, "body_start", body_start)
        _setattr(doc.node, "body_end", terminator_end)
        _setattr(
            doc.node,
            "terminator_span",
            SourceSpan(terminator_start, terminator_end),
        )
        if after > body_start:
            self._emit(RawNode(SourceSpan(body_start, after), "heredoc-body"))
        return after

    # ── words ───────────────────────────────────────────────────────────────

    def _handle_word(self, token: Token, index: int) -> int:
        raw = token.span.text(self.source)
        word = word_to_node(token, self.source)
        literal = word.literal_value
        keyword = keyword_of(raw, literal)

        # case header state machine (subject word / `in` / patterns are data)
        if self.case_state == "word":
            self.case_state = "await-in"
            self._flush(token.span.end)
            self._emit(word)  # subject word: data segment
            return index
        if self.case_state == "await-in" and keyword == "in":
            self.case_state = "patterns"
            self._flush(token.span.end)
            self._emit(word)
            return index
        if self.case_state == "patterns":
            self._emit(word)  # pattern words: data segments
            return index

        p = self.parts if self.parts is not None else self._ensure(token.span.start)
        self._collect_regions(token, p)

        if self.command_expected and self.parts.command_word is None:
            if keyword in _COMMAND_REOPENERS or keyword in _COMMAND_CLOSERS:
                # reserved word at command position: marker segment (data),
                # then re-open (``then``/``do``/``else``/``{``) or close
                # (``fi``/``done``/``}`` …) the command context — the legacy
                # scanner's keyword table behaviour.
                self._flush(token.span.end)
                self._emit(RawNode(token.span, f"reserved:{literal}"))
                self.command_expected = keyword in _COMMAND_REOPENERS
                if keyword in ("}", "fi", "done", "esac"):
                    if self.compound_state:
                        self.compound_state.pop()
                    self.command_expected = False
                return index
            if classify_assignment(raw) is not None:
                self.parts.assignments.append(word)
                self.parts.end = max(self.parts.end, token.span.end)
                return index
            self.parts.command_word = word
            self.parts.command_value = literal
            self._record_wrapper_hints(self.parts, literal)
            self.parts.end = max(self.parts.end, token.span.end)
            self.command_expected = False
            if keyword == "case":
                self.case_state = "word"
            elif keyword in ("for", "select", "while", "until", "if"):
                self.compound_state.append(keyword)
            return index

        # argument/data position
        if keyword in _COMMAND_REOPENERS:
            self._flush(token.span.end)
            self._emit(RawNode(token.span, f"reserved:{keyword}"))
            self.command_expected = True
            return index
        if keyword in _COMMAND_CLOSERS:
            self._flush(token.span.start)
            self._emit(RawNode(token.span, f"reserved:{keyword}"))
            if self.compound_state and keyword in ("fi", "done", "esac"):
                self.compound_state.pop()
            self.command_expected = False
            return index
        p = self._ensure(token.span.start)
        if (
            p.command_value in DECLARATION_BUILTINS
            and classify_assignment(raw) is not None
        ):
            p.assignments.append(word)
            p.end = max(p.end, token.span.end)
            return index
        if (
            literal is not None
            and _SHELL_C_OPTIONS.match(literal)
            and (p.command_value in _SHELL_WRAPPERS or p.shell_exec_operand)
        ):
            p.shell_c_pending = True
            p.arguments.append(word)
            p.end = max(p.end, token.span.end)
            return index
        if (
            p.command_value in _EXEC_OPERAND_COMMANDS or p.shell_exec_operand
        ) and literal in _SHELL_WRAPPERS:
            # operand-position shell (`find -exec sh -c '<script>'`,
            # `xargs sh -c`): its -c operand is an inline script — arm the
            # same embedding rule as a top-level shell invocation.
            p.shell_exec_operand = True
            p.arguments.append(word)
            p.end = max(p.end, token.span.end)
            return index
        if p.shell_c_pending and _looks_whole_quoted(word):
            p.shell_c_pending = False
            p.embedded.append(_make_embedded(word, self.depth, self.tasks, self))
            p.end = max(p.end, token.span.end)
            return index
        if p.reparse_remaining != 0 and _looks_whole_quoted(word):
            p.embedded.append(_make_embedded(word, self.depth, self.tasks, self))
            if p.reparse_remaining > 0:
                p.reparse_remaining -= 1
            p.end = max(p.end, token.span.end)
            return index
        p.arguments.append(word)
        p.end = max(p.end, token.span.end)
        return index

    def _ensure(self, start: int) -> _Part:
        if self.parts is None:
            self.parts = _Part(start)
        return self.parts

    def _record_wrapper_hints(self, p: _Part, literal: Optional[str]) -> None:
        """Tag the command with wrapper / shell / reparse HINTS (no action).

        Table membership is read from the legacy scanner tables (imported,
        never copied): ``_SHELL_WRAPPERS`` -> "shell", ``_COMMAND_WRAPPERS``
        -> "command", ``_SAME_SHELL_WRAPPERS`` -> "same-shell",
        ``_REPARSE_BUILTINS`` -> "reparse" (operand scripts),
        ``_EXEC_OPERAND_COMMANDS`` -> "exec-operand" (argv-passing commands).
        ``watch`` re-executes its command in the current shell (legacy
        ``eval "$*"``), so its first quoted operand gets a reparse count.
        """
        if literal is None:
            return
        if literal in _SHELL_WRAPPERS:
            p.wrapper, p.wrapper_hint = literal, "shell"
        elif literal in _COMMAND_WRAPPERS:
            p.wrapper, p.wrapper_hint = literal, "command"
        if literal in _SAME_SHELL_WRAPPERS and p.wrapper is None:
            p.wrapper, p.wrapper_hint = literal, "same-shell"
        if literal in _REPARSE_BUILTINS:
            p.wrapper, p.wrapper_hint = literal, "reparse"
            p.reparse_remaining = _REPARSE_BUILTINS[literal]
        if literal == "watch":
            p.wrapper, p.wrapper_hint = literal, "same-shell"
            p.reparse_remaining = 1
        if literal in _EXEC_OPERAND_COMMANDS and p.wrapper is None:
            p.wrapper, p.wrapper_hint = literal, "exec-operand"

    def _collect_regions(self, token: Token, p: _Part) -> None:
        """Build child contexts for executable substitution regions.

        Covers top-level ``$( )``/backtick/``<( )`` regions and the ones the
        lexer reports inside double quotes (test 4).  Beyond the depth bound
        the region is NOT descended: ``depth_limit`` diagnostic and the
        content stays preserved as data inside the word (test 6).  A word
        whose substitution is itself the command word keeps the WordNode
        shape (the ExpansionNode rides in ``arguments`` for tiling).
        """
        for kind, span, inner in token.parts:
            if kind not in EXECUTABLE_REGIONS or inner is None:
                continue
            region = SourceSpan(inner[0], inner[1])
            if self.depth + 1 >= _MAX_NESTING_DEPTH:
                self.diagnostics.append(
                    Diagnostic(
                        DiagnosticKind.DEPTH_LIMIT,
                        span,
                        f"nesting depth limit {_MAX_NESTING_DEPTH} reached; "
                        "region preserved as data",
                    )
                )
                continue
            placeholder = ExpansionNode(span, kind, None)
            p.expansions.append(placeholder)
            self.tasks.append(
                _Task(
                    _Slot("expansion", placeholder),
                    region,
                    self.depth + 1,
                    standalone=False,
                    source_text=self.source,
                    parent_ctx=self,
                )
            )

    # ── array-literal detection ─────────────────────────────────────────────

    def note_array(self, span: SourceSpan) -> None:
        """Mark the open command as carrying array-literal DATA (element
        words between ``(`` and ``)`` are data, not command context)."""
        p = self._ensure(span.start)
        p.array_data = True
        p.end = max(p.end, span.end)


class _Doc:
    """Internal record for a pending heredoc document."""

    __slots__ = (
        "op_span",
        "op_text",
        "delimiter_word",
        "delimiter",
        "strip_tabs",
        "expands",
        "node",
    )

    def __init__(
        self,
        op_span: SourceSpan,
        op_text: str,
        delimiter_word: Optional[WordNode],
        delimiter: Optional[str],
        strip_tabs: bool,
        expands: bool,
        node: HeredocNode,
    ) -> None:
        self.op_span = op_span
        self.op_text = op_text
        self.delimiter_word = delimiter_word
        self.delimiter = delimiter
        self.strip_tabs = strip_tabs
        self.expands = expands
        self.node = node


def _redirection_truncates(op_core: str, target: Optional[WordNode]) -> bool:
    """Whether the operator can create/truncate its target (Phase 4 rule)."""
    if op_core in TRUNCATING_REDIRECT_OPERATORS:
        return True
    if op_core == ">&":
        if target is not None:
            value = target.literal_value
            if value is not None and value.isdigit():
                return False  # fd dup never creates a file
            return True
        return False
    return False


def _looks_whole_quoted(word: WordNode) -> bool:
    """True when the word is exactly one statically-decodable quote region."""
    if word.literal_value is None or not word.parts:
        return False
    kind, span = word.parts[0]
    if kind not in (
        WordPartKind.SINGLE_QUOTE,
        WordPartKind.DOUBLE_QUOTE,
        WordPartKind.ANSI_C_QUOTE,
        WordPartKind.LOCALE_QUOTE,
    ):
        return False
    return len(word.parts) == 1 and span == word.span


def _make_embedded(
    word: WordNode, depth: int, tasks: List[_Task], ctx: "_Ctx"
) -> EmbeddedScriptNode:
    kind, _span = word.parts[0]
    script = word.literal_value or ""
    node = EmbeddedScriptNode(word.span, word, kind, None)
    if script:
        region = SourceSpan(0, len(script))
        if depth + 1 < _MAX_NESTING_DEPTH:
            placeholder = ScriptNode(region, script, region, (), (), ())
            tasks.append(
                _Task(
                    _Slot("embedded", node),
                    region,
                    depth + 1,
                    standalone=True,
                    source_text=script,
                    parent_ctx=ctx,
                )
            )
            _setattr(node, "inner", placeholder)
        else:
            ctx.diagnostics.append(
                Diagnostic(
                    DiagnosticKind.DEPTH_LIMIT,
                    word.span,
                    "embedded script not descended: depth limit",
                )
            )
    return node


# ── tiling predicate + folding (stage 2) ───────────────────────────────────


def _tiles(node: Node) -> bool:
    """True when a node's own children tile its span (render identity)."""
    if node.kind is NodeKind.COMMAND:
        child_spans: List[SourceSpan] = []
        child_spans.extend(word.span for word in node.assignments)
        if node.command_word is not None:
            child_spans.append(node.command_word.span)
        child_spans.extend(child.span for child in node.arguments)
        child_spans.extend(red.span for red in node.redirections)
        child_spans.extend(doc.span for doc in node.heredocs)
        child_spans.extend(emb.span for emb in node.embedded)
        # expansion nodes ride inside a word's parts (overlapping spans); they
        # are reachable through CommandNode.expansions / walk_nodes, not as
        # separate tiling children, so they are excluded here.
        return _spans_tile(node.span, child_spans)
    if node.kind is NodeKind.PIPELINE:
        return _spans_tile(node.span, [c.span for c in node.commands])
    if node.kind is NodeKind.COMMAND_LIST:
        return _spans_tile(node.span, [c.span for c in node.children])
    if node.kind is NodeKind.REDIRECTION:
        if node.target is None:
            return True
        # operator+target tile iff the target touches the operator end
        return node.target.span.start == node.operator_span.end and (
            node.span.end == node.target.span.end
        )
    if node.kind is NodeKind.EMBEDDED:
        return node.owner_word is None or node.owner_word.span == node.span
    if node.kind is NodeKind.COMPOUND:
        # compound bodies own interior text; the group span renders whole
        return True
    return True  # words/raw/newlines/leaf nodes are self-tiling


def _spans_tile(span: SourceSpan, children: List[SourceSpan]) -> bool:
    """True when ordered children fit inside ``span`` with no overlap."""
    cursor = span.start
    for child in sorted(children, key=lambda c: c.start):
        if child.start < cursor:
            return False
        if child.end > span.end:
            return False
        cursor = child.end
    return True


def _fold_segments(segments: List[Node], source: str) -> Tuple[Node, ...]:
    """Fold term/operator segment runs into pipeline/list nodes (lossless).

    A term run joined by single-operator data segments (``op:`` markers)
    folds into one ``PipelineNode`` (all operators pipeline-level) or one
    ``CommandListNode`` (mixed/list operators); the group span is contiguous
    source (terms + operators + gaps), so the render identity holds
    trivially.  Any other data segment (newline, comment, heredoc body,
    reserved word, case marker) flushes the run and stays its own child.
    """
    folded: List[Node] = []
    run: List[Node] = []
    ops: List[Tuple[Node, str]] = []

    def flush_run() -> None:
        nonlocal run, ops
        if not run:
            return
        if len(run) == 1:
            folded.append(run[0])
        else:
            start = run[0].span.start
            end = run[-1].span.end
            texts = [op for _marker, op in ops]
            if all(op in PIPELINE_OPERATORS for op in texts):
                folded.append(
                    PipelineNode(SourceSpan(start, end), tuple(run), tuple(texts))
                )
            else:
                folded.append(
                    CommandListNode(SourceSpan(start, end), tuple(run), tuple(texts))
                )
        run = []
        ops = []

    i = 0
    n = len(segments)
    while i < n:
        seg = segments[i]
        if seg.kind is NodeKind.RAW and seg.reason.startswith("op:"):
            op = seg.reason[3:]
            # operator joins only if the NEXT segment is a term and the run
            # already has a term (operator between two terms)
            if run and i + 1 < n and segments[i + 1].kind is not NodeKind.RAW:
                ops.append((seg, op))
                i += 1
                continue
            # operator without a following term: it owned the run boundary;
            # flush the run and keep the operator as its own data child.
            flush_run()
            folded.append(seg)
            i += 1
            continue
        if seg.kind is NodeKind.RAW and seg.reason.startswith("case-"):
            flush_run()
            folded.append(seg)
            i += 1
            continue
        if seg.kind in (NodeKind.NEWLINE,):
            flush_run()
            folded.append(seg)
            i += 1
            continue
        if seg.kind is NodeKind.RAW and (
            seg.reason.startswith("comment")
            or seg.reason.startswith("heredoc-body")
            or seg.reason.startswith("reserved:")
            or seg.reason.startswith("unexpected")
            or seg.reason.startswith("unmatched")
            or seg.reason.startswith("depth limit")
            or seg.reason.startswith("non-tiling")
        ):
            flush_run()
            folded.append(seg)
            i += 1
            continue
        # a term segment
        run.append(seg)
        i += 1
    flush_run()
    return tuple(folded)


# ── context driver ───────────────────────────────────────────────────────────


def _build_context(
    source: str,
    region: SourceSpan,
    tokens: Tuple[Token, ...],
    depth: int,
    diagnostics: List[Diagnostic],
) -> Tuple[ScriptNode, List[_Task]]:
    ctx = _Ctx(source, region, tokens, depth, diagnostics)
    segments = ctx.run()
    children = _fold_segments(segments, source)
    script = ScriptNode(
        region, source, region, children, (), tuple(diagnostics)
    )
    return script, ctx.tasks


def _drain_tasks(root_source: str, tasks: List[_Task]) -> None:
    """Iteratively parse queued nested contexts (bounded; no recursion).

    Each task lexes+segments its region and fills its ``_Slot`` (expansion
    child / embedded inner / group body).  Children queue their own tasks
    onto the same list: the loop walks any nesting count while the Python
    call stack stays O(1).
    """
    queue: List[_Task] = list(tasks)
    while queue:
        task = queue.pop(0)
        region = task.region
        if region.length == 0:
            if task.slot.kind != "embedded":
                task.slot.fill(
                    ScriptNode(region, task.source_text, region, (), (), ())
                )
            else:
                task.slot.fill(
                    ScriptNode(region, task.source_text, region, (), (), ())
                )
            continue
        text = task.source_text if task.standalone else root_source
        result: LexResult = lex(text, region.start, region.end)
        diagnostics = list(result.diagnostics)
        script, child_tasks = _build_context(
            text, region, result.tokens, task.depth, diagnostics
        )
        task.slot.fill(script)
        queue.extend(child_tasks)


def parse(source: str) -> ScriptNode:
    """Parse ``source`` into the lossless tolerant shell IR (root ScriptNode).

    Never raises on malformed, incomplete, adversarial, or deeply nested
    input: unrecognized structure stays byte-for-byte (data segments) and
    reasons are recorded as internal diagnostics; nesting beyond
    ``_MAX_NESTING_DEPTH`` stops descending (``depth_limit``).  The
    lossless invariant ``render_source(parse(src)) == src`` holds via
    segment tiling.
    """
    if not source:
        return ScriptNode(SourceSpan(0, 0), "", SourceSpan(0, 0), (), (), ())
    region = SourceSpan(0, len(source))
    result: LexResult = lex(source, 0, len(source))
    diagnostics = list(result.diagnostics)
    script, tasks = _build_context(
        source, region, result.tokens, 0, diagnostics
    )
    _drain_tasks(source, tasks)
    # children were installed into placeholders via slots: rebuild the
    # diagnostic tuple order (source order) now that nested diagnostics
    # merged during the drain.
    return script


def parse_inner(source: str) -> ScriptNode:
    """Parse a standalone (runtime-reparsed) script into its own IR.

    The inner ``ScriptNode`` owns the standalone string with local
    coordinates: the quoted operand of ``eval``/``trap``/``watch``/``sh -c``
    is re-parsed by the shell at runtime and is not a slice of the outer
    source.  ``EmbeddedScriptNode.quote_kind`` records the quoting so later
    phases can serialize inner edits back into the outer word.
    """
    if not source:
        return ScriptNode(SourceSpan(0, 0), "", SourceSpan(0, 0), (), (), ())
    region = SourceSpan(0, len(source))
    result: LexResult = lex(source, 0, len(source))
    diagnostics = list(result.diagnostics)
    script, tasks = _build_context(
        source, region, result.tokens, 0, diagnostics
    )
    _drain_tasks(source, tasks)
    return script


parse_command_context = parse
