r"""Heredoc trailing-operator repair and the IR-side event model (Phase 7).

Part of the lossless tolerant-IR ``kimix_native.shell_compat`` implementation
package behind the stable ``_shell_compat`` facade.

Phase 7 of the ``_shell_compat`` refactor (plan §3.5 step 4 / §3.6, Phase 7
actions 1-4).  The heredoc repair functions ``_fix_heredoc_trailing_operators``
and ``_apply_heredoc_operator_move`` (and the trailing-operator set
``_HEREDOC_TRAILING_OPERATORS``) MOVED here byte-for-byte from
:mod:`kimix_native.shell_compat.bash_core`; ``bash_core`` keeps thin delegates
at the historical names because they are part of the 195-name
``_shell_compat`` facade ``__all__`` and the scanner call sites
(``_BashFixScanner._build_fixed_source``, ``fix_bash_command``) resolve through
them, exactly the Phase-3/4/5/6 ownership pattern.

Two shapes are exposed:

* **The moved repairs** — ``_apply_heredoc_operator_move`` (moves a control
  operator line following a heredoc terminator up onto the redirection line,
  including multi-line operator runs, backslash continuations, comment lines
  and the construct-closer "stay" guards) and ``_fix_heredoc_trailing_operators``
  (applies the repair to every ``(redir_line_end, terminator_end)`` event in
  reverse order; without events the source is rescanned through the
  compatibility scanner, which is the legacy fallback and stays byte-identical).
* **The IR-side event model** — ``heredoc_events(source)`` derives the scanner's
  ``(redir_line_end, terminator_end)`` event list from the lossless IR
  (``HeredocNode`` delimiter values, strip-tabs flags, expansion flags and
  terminator spans), reproducing the legacy scanner's claim rules exactly:
  a malformed (``None``) delimiter claims no event; a line whose redirection
  does not end on a newline claims nothing; multiple documents on ONE
  redirection line merge into a single event per document carrying the running
  maximum extent (the scanner's ``_skip_heredoc_bodies`` walks its pending list
  in one pass and appends one event per line-group); a document whose body
  never terminates claims the region end.  ``fix_heredoc_trailing_operators_ir``
  is the dispatcher-slot-8 renderer over the IR-derived events.

Import structure: this module imports NOTHING from ``bash_core`` at load time
(the event model needs the IR, and the IR needs ``bash_core`` — the event
functions import the parser lazily; the no-events legacy rescan imports the
scanner lazily, the Phase-3/4/5/6 pattern that keeps the cycle broken).
``bash_core`` imports the moved constant at load AFTER its own definitions and
delegates to the moved functions lazily inside delegate bodies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional, Tuple

if TYPE_CHECKING:  # pragma: no cover
    from kimix_native.shell_compat.models import HeredocNode, ScriptNode

_HEREDOC_TRAILING_OPERATORS = frozenset({"&&", "||", "|", "|&", ";", "&"})


def _apply_heredoc_operator_move(
    source: str, redir_line_end: int, terminator_end: int
) -> str:
    """Move a control-operator line following a heredoc terminator to the redirection line.

    Bash requires a control operator that continues a heredoc-delimited command
    to appear on the same line as the ``<<`` redirection.  A common model
    mistake is to place the operator on the line after the closing delimiter,
    which produces ``syntax error near unexpected token `&&'``.  This helper
    repairs that pattern while leaving the heredoc body and delimiter intact.
    """
    from kimix_native.shell_compat.bash_core import (
        _BashFixScanner,
        _read_shell_control_operator,
    )

    n = len(source)
    if redir_line_end < 0 or redir_line_end >= n or source[redir_line_end] != "\n":
        return source
    if terminator_end < 0 or terminator_end > n:
        return source

    i = terminator_end
    while i < n:
        ch = source[i]
        if ch in " \t\r":
            i += 1
            continue
        if ch == "\n":
            i += 1
            continue
        if ch == "#":
            nl = source.find("\n", i, n)
            i = n if nl < 0 else nl + 1
            continue
        break
    if i >= n:
        return source

    op, op_end = _read_shell_control_operator(source, i, n)
    if op not in _HEREDOC_TRAILING_OPERATORS:
        return source
    # ``&>`` / ``&>>`` are redirections, not list terminators.
    if op == "&" and op_end < n and source[op_end] == ">":
        return source

    move_start = i
    line_end = source.find("\n", i, n)
    if line_end < 0:
        line_end = n
        move_end = n
    else:
        move_end = line_end + 1

    rest = source[op_end:line_end].lstrip(" \t\r")
    if not rest or rest.startswith("#"):
        k = move_end
        while k < n:
            if source[k] in " \t\r":
                k += 1
                continue
            if source[k] == "\n":
                k += 1
                continue
            if source[k] == "#":
                nl = source.find("\n", k, n)
                k = n if nl < 0 else nl + 1
                continue
            break
        if k >= n:
            return source
        next_line_end = source.find("\n", k, n)
        if next_line_end < 0:
            move_end = n
        else:
            move_end = next_line_end + 1

    # A trailing backslash continues the logical command on the following
    # physical lines; the continuation must move with its line, or the
    # orphaned tail is left behind and read as heredoc body.
    while move_end < n:
        prev_nl = source.rfind("\n", move_start, move_end - 1)
        last_line = source[prev_nl + 1 : move_end - 1]
        if not _BashFixScanner._heredoc_line_continues(last_line):
            break
        next_end = source.find("\n", move_end, n)
        move_end = n if next_end < 0 else next_end + 1

    # A list may continue over SEVERAL physical operator lines (the model can
    # write ``&& echo A`` on one line and ``&& echo B`` on the next, both after
    # the closing delimiter).  The backslash loop above only carries a single
    # logical line; absorb each following operator-led line as well, or it is
    # left orphaned after the terminator and bash reports
    # ``syntax error near unexpected token `&&'``.
    while move_end < n:
        j = move_end
        while j < n:
            cj = source[j]
            if cj in " \t\r":
                j += 1
                continue
            if cj == "\n":
                j += 1
                continue
            if cj == "#":
                nl2 = source.find("\n", j, n)
                j = n if nl2 < 0 else nl2 + 1
                continue
            break
        if j >= n:
            break
        op2, op2_end = _read_shell_control_operator(source, j, n)
        if op2 not in _HEREDOC_TRAILING_OPERATORS:
            break
        if op2 == "&" and op2_end < n and source[op2_end] == ">":
            break
        cand_nl = source.find("\n", j, n)
        move_end = n if cand_nl < 0 else cand_nl + 1
        # carry this operator line's own backslash continuations too
        while move_end < n:
            pl = source.rfind("\n", j, move_end - 1)
            ll = source[pl + 1 : move_end - 1]
            if not _BashFixScanner._heredoc_line_continues(ll):
                break
            ne = source.find("\n", move_end, n)
            move_end = n if ne < 0 else ne + 1

    lines = source[move_start:move_end].splitlines()

    # Closing delimiters that belong to a construct opened BEFORE the
    # redirection line (a command substitution ``$(`` or backtick, a subshell
    # ``(``, a brace group ``{``) must NOT ride up onto the redirection line:
    # doing so closes the construct before the heredoc body, ejects the body
    # lines into the outer command list, and bash reports an unterminated
    # here-document.  The excess closers — and everything after them — stay
    # in place after the terminator (e.g. ``x=$(cat <<E | wc -l`` + body +
    # ``E`` + ``); echo q``).  The stay suffix may sit mid-line (``| wc -l);
    # echo q``) or after balanced groups inside the moved text (``| (a); b)``;
    # only the closers exceeding the moved text's own openers belong outside).
    # Guards are naive character counts; the prefix guard (an unclosed opener
    # before the redirection line) keeps ordinary operator lines such as
    # ``| (wc -l)`` moving whole.
    stay = ""
    if lines:
        moved_text = "\n".join(lines)
        prefix = source[:redir_line_end]
        stay_start: int | None = None
        paren_excess = moved_text.count(")") - moved_text.count("(")
        if paren_excess > 0 and prefix.count("(") > prefix.count(")"):
            # The suffix from the last excess ``)`` onward stays: it is the
            # construct closer (any ``)`` beyond the moved text's own openers
            # must close a prefix opener).
            target = moved_text.count("(") + paren_excess
            idx = -1
            for _ in range(target):
                idx = moved_text.find(")", idx + 1)
            if idx >= 0 and (stay_start is None or idx < stay_start):
                stay_start = idx
        if moved_text.count("`") % 2 == 1 and prefix.count("`") % 2 == 1:
            idx = moved_text.rfind("`")
            if idx >= 0 and (stay_start is None or idx < stay_start):
                stay_start = idx
        brace_excess = moved_text.count("}") - moved_text.count("{")
        if brace_excess > 0 and prefix.count("{") > prefix.count("}"):
            target = moved_text.count("{") + brace_excess
            idx = -1
            for _ in range(target):
                idx = moved_text.find("}", idx + 1)
            if idx >= 0 and (stay_start is None or idx < stay_start):
                stay_start = idx
        if stay_start is not None:
            stay = moved_text[stay_start:]
            lines = moved_text[:stay_start].splitlines() or [""]
            # If the first stay line is only construct closers and the next
            # stay line starts with a control operator, joining them with a
            # space is required: an operator-led physical line after a
            # completed command is a bash syntax error (``cmd \n && next``).
            stay_lines = stay.split("\n")
            if (
                len(stay_lines) > 1
                and not stay_lines[0].strip(" \t\r").strip(")`}")
                and stay_lines[1].lstrip(" \t\r")
            ):
                nxt = stay_lines[1]
                lead = len(nxt) - len(nxt.lstrip(" \t\r"))
                op2, _op2_end = _read_shell_control_operator(
                    nxt, lead, len(nxt)
                )
                if op2 in _HEREDOC_TRAILING_OPERATORS and not (
                    op2 == "&" and nxt[lead + len(op2) :].startswith(">")
                ):
                    stay = " ".join(
                        [stay_lines[0].rstrip(" \t\r")]
                        + [
                            ln.strip(" \t\r")
                            for ln in stay_lines[1:]
                            if ln.strip(" \t\r")
                        ]
                    )

    parts: list[str] = []
    if lines:
        parts.append(lines[0][len(op):].strip())
        parts.extend(line.strip() for line in lines[1:])
    # Re-join stripped parts, but keep a backslash line-continuation as a real
    # newline: splicing the next line on with a space would turn ``\`` + NL
    # into ``\ `` (an escaped space), corrupting the continued command.
    segments: list[str] = []
    for part in parts:
        if not part or part.startswith("#"):
            continue
        if segments:
            prev = segments[-1]
            trailing = len(prev) - len(prev.rstrip("\\"))
            segments.append(("\n" if trailing % 2 == 1 else " ") + part)
        else:
            segments.append(part)
    joined = "".join(segments)
    moved = f"{op} {joined}\n" if joined else f"{op}\n"

    return (
        source[:redir_line_end]
        + " "
        + moved
        + source[redir_line_end + 1 : move_start]
        + (stay + "\n" if stay else "")
        + source[move_end:]
    )


def _fix_heredoc_trailing_operators(
    source: str, events: list[tuple[int, int]] | None = None
) -> str:
    """Repair heredoc commands whose trailing control operator is on the wrong line.

    *events* may carry the ``(redir_line_end, terminator_end)`` pairs already
    collected by a scan of *source*; when omitted the source is scanned here.
    """
    if events is None:
        try:
            from kimix_native.shell_compat.bash_core import _BashFixScanner

            scanner = _BashFixScanner(source)
            scanner._scan_range(0, scanner.n)
        except RecursionError:
            return source
        events = scanner.heredoc_events
    if not events:
        return source
    for redir_line_end, terminator_end in reversed(events):
        source = _apply_heredoc_operator_move(source, redir_line_end, terminator_end)
    return source


# ── IR-side heredoc event model (plan §3.2 HeredocNode / §3.5 slot 8) ─────────


def heredoc_event_for(source: str, doc: "HeredocNode") -> Optional[Tuple[int, int]]:
    """The scanner event ``(redir_line_end, terminator_end)`` of one HeredocNode.

    Reproduces the legacy ``_BashFixScanner._skip_heredoc_bodies`` claim rule
    from the IR data:
    * ``delimiter_value is None`` — a malformed delimiter: the scanner appends
      NO event for a document list whose every entry is malformed (and the IR
      never even reaches the terminator scan), so no event;
    * ``body_start - 1`` is the delimiter LINE's newline (the scanner computes
      ``redir_line_end = body_start - 1``); when the redirection line is not
      newline-terminated the body starts at the region end or the line simply
      has no newline — the scanner's claim then stops at the range end and
      yields an event only when it actually saw the newline, so a missing
      newline at ``body_start - 1`` claims nothing;
    * a terminator: the scanner's cursor ends just past the terminator LINE's
      newline — ``terminator_span.end`` plus one when a newline follows (or the
      range end when the terminator line is unterminated);
    * no terminator (``body_end`` reached by the scanner's line walk without a
      delimiter match): the cursor is the region end, exactly ``doc.body_end``
      for the IR's unterminated claim (which scans to the context end).
    """
    if doc.delimiter_value is None:
        return None
    n = len(source)
    redir_line_end = doc.body_start - 1
    if redir_line_end < 0 or source[redir_line_end] != "\n":
        return None
    if doc.terminator_span is not None:
        te = doc.terminator_span.end
        if te < n and source[te] == "\n":
            return (redir_line_end, te + 1)
        return (redir_line_end, n)
    return (redir_line_end, doc.body_end)


def _claim_events(
    source: str, docs: "List[HeredocNode]"
) -> List[Tuple[int, int]]:
    """Merge per-document events into the scanner's per-redirection-line list.

    The legacy scanner walks its ``pending_heredocs`` list for ONE delimiter
    line inside a single ``_skip_heredoc_bodies`` call: every document starts
    where the previous one stopped, so each document's terminator scan operates
    on the RUNNING extent, and the list contributes exactly ONE event carrying
    the final cursor (the maximum over the group).  A group whose first
    document claims nothing (malformed delimiter / no newline) still produces
    the event if a LATER group member claims; the emitted cursor is the running
    maximum of the members' individual extents, one event per member beyond the
    first (the scanner's single event is duplicated per member only when the
    members are separate documents on the same line — verified against the
    scanner: ``cat <<E <<F`` yields TWO identical (rle, max) events per the
    outer/inner scan overlap).  Order follows the source-ordered document walk.
    """
    groups: dict = {}
    order: list = []
    for doc in docs:
        event = heredoc_event_for(source, doc)
        if event is None:
            continue
        rle = event[0]
        if rle not in groups:
            groups[rle] = []
            order.append(rle)
        groups[rle].append(event[1])
    out: List[Tuple[int, int]] = []
    for rle in order:
        ends = sorted(groups[rle])
        cursor = ends[0]
        for te in ends:
            cursor = max(cursor, te)
            out.append((rle, cursor))
    return out


def collect_heredoc_events(script: "ScriptNode") -> List[Tuple[int, int]]:
    """The scanner-order heredoc event list of a parsed (sub)context source.

    Visits the IR the same way ``models.walk_nodes`` does (DFS, the scanner's
    source-discovery order for substitution regions, which share the outer
    string and get scanned inline by the scanner's own recursion) but STOPS at
    ``EmbeddedScriptNode.inner`` edges: a runtime-reparsed operand owns a
    STANDALONE coordinate string; the scanner scans it through a FRESH inner
    scanner whose ``heredoc_events`` never merge into the outer list (the LIVE
    merge blocks copy names/path/shell/nul/cmd/unsupported only), and the inner
    repair is applied to the inner string by ``translate_inner``.  The
    Phase-7 translator therefore translates embedded contexts recursively and
    collects their events for their own render pass — not here.
    """
    from kimix_native.shell_compat.models import NodeKind

    source = script.source
    docs = []
    stack = [script]
    while stack:
        current = stack.pop()
        if current.kind is NodeKind.HEREDOC:
            docs.append(current)
        children = []
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
            # owner word only — the inner script is a separate context
            if current.owner_word is not None:
                children.append(current.owner_word)
        elif kind is NodeKind.COMPOUND:
            children.extend(reversed([node for _name, node in current.parts]))
        stack.extend(children)
    return _claim_events(source, docs)


def fix_heredoc_trailing_operators_ir(
    source: str, script: "ScriptNode", events: Optional[List[Tuple[int, int]]]
) -> str:
    """The §3.5 slot-8 render step: repair with IR-derived events.

    ``events`` is the caller's already-derived event list (``None`` = derive
    from *script*).  With no events the source returns unchanged (byte-for-byte
    the legacy fast path); otherwise the moved repair applies in reverse event
    order exactly like ``_fix_heredoc_trailing_operators``.
    """
    if events is None:
        events = collect_heredoc_events(script)
    if not events:
        return source
    for redir_line_end, terminator_end in reversed(events):
        source = _apply_heredoc_operator_move(
            source, redir_line_end, terminator_end
        )
    return source


__all__ = [
    "_HEREDOC_TRAILING_OPERATORS",
    "_apply_heredoc_operator_move",
    "_fix_heredoc_trailing_operators",
    "collect_heredoc_events",
    "fix_heredoc_trailing_operators_ir",
    "heredoc_event_for",
]
