"""Phase 2 tests: the lossless shell IR (spans, models, parser).

Covers (plan §4 Phase 2 exit gate):

1. lossless invariant — ``render_source(parse(source)) == source``
   byte-for-byte for plain commands, every quoting kind, comments, heredocs,
   nested substitutions, redirections, control operators, and malformed /
   unterminated constructs;
2. command-position classification (command word vs arguments vs assignments);
3. comments and heredoc bodies are DATA nodes (never command contexts);
4. command substitutions inside double quotes are child contexts;
5. malformed/unterminated constructs produce diagnostics + raw/partial
   trees, never exceptions;
6. 2,000-level ``$( )`` nesting terminates (bounded by _MAX_NESTING_DEPTH,
   explicit worklist, no RecursionError, no hang);
7. a 20,000-word command lexes/parses in linear-ish time (bounded, fast);
8. span/validation semantics of the primitives (SourceSpan, Edit, Diagnostic).

Pure Python, no subprocess, no Git Bash.
"""

from __future__ import annotations

import dataclasses
import time

import pytest

# Resolve the shim package the way every sibling suite does: kimix_cli.
# native_loader puts <repo>/bin on sys.path FIRST so `kimix_native.*` below
# imports the in-repo package (a stale installed wheel must not shadow it).
from kimi_cli.native_loader import get_compat as _get_compat

assert _get_compat("_shell_compat") is not None

from kimix_native.shell_compat.models import (
    CommandListNode,
    CommandNode,
    CompoundNode,
    EmbeddedScriptNode,
    ExpansionNode,
    HeredocNode,
    NewlineNode,
    NodeKind,
    OperatorNode,
    PipelineNode,
    RawNode,
    RedirectionNode,
    ScriptNode,
    WordNode,
    WordPartKind,
    decode_word_literal,
    decode_word_literal_partial,
    render_source,
    walk_nodes,
)
from kimix_native.shell_compat.parser import (
    classify_assignment,
    is_effective_nul_target,
    parse,
    parse_inner,
    word_to_node,
)
from kimix_native.shell_compat.spans import (
    Diagnostic,
    DiagnosticKind,
    Edit,
    ParseNote,
    SourceSpan,
)

# ── 1. lossless invariant ────────────────────────────────────────────────────

LOSSLESS_CASES = [
    # plain commands and whitespace shapes
    "",
    "   ",
    "\n\n",
    "rev",
    "echo hi",
    "foo;bar",
    "cmd arg1\n\n",
    "tail -f a\nhead b",
    "a && b || c && d",
    "a | b | c",
    "a | b && c ; d",
    # quoting of every kind
    "echo 'a b' \"c $(d) e\"",
    "echo $'a\\'b' x",
    'echo $"locale" x',
    "echo a\\\nb",
    "echo $x $@ ${y:-z} $((1+2))",
    "echo ${a:-$(b)}",
    'echo "hi $name $(whoami)"',
    'echo "a `b` c"',
    # comments (whole line and trailing)
    "#whole line comment",
    "echo hi # trailing comment",
    "echo $(a) $(b) #notcomment ; x",
    "echo <(x)#glued",
    "echo $(y)#z; rev",
    "(a; b) #comment",
    # heredocs (all shapes incl. operators on the delimiter line)
    "cat <<EOF\nbody\nEOF\nmore",
    "cat <<EOF\nEOF",
    "cat <<EOF",
    "cat <<",
    "cat <<EOF | grep x",
    "cat <<-EOF\n\tbody\n\tEOF\nafter",
    "cat <<'QEOF'\n$(x)\nQEOF",
    'cat <<"E O F"\nb\nE O F',
    "cat <<A\nx\nA\ncat <<B\ny\nB\nz",
    "cat <<EOF; rev",
    "cat <<'E'\n$(not executed)\nE\nrev",
    # nested substitutions
    "echo $( $(nested) )",
    "echo \"a $(b $(c)) d\"",
    "echo $(rev <<< abc)",
    "diff <(cat a) <(cat b)",
    "echo `date`",
    "echo $(a; b) #notcomment ; x",
    # redirections and control operators
    "x=1 tree a > out 2>&1",
    "echo 2>&1 > out",
    "echo hi>out",
    "sort < a > b >> c",
    "cmd >&2",
    "echo |& cat",
    "echo foo;; bar",
    "<<< word",
    ">",
    "&",
    "|",
    ";;",
    ") x",
    # compound constructs
    "case $x in\n a) tree;; esac",
    "case `date` in a) tree;; esac",
    "if tree; then rev; fi",
    "while read -r l; do echo $l; done",
    "for ((i=0;i<2;i++)) { tree; }",
    "f() { rev; }",
    "f(){ rev; }",
    "function f { rev; }",
    "(ls; pwd) | grep x",
    # wrappers / reparse builtins / -c scripts
    "eval \"tree\"",
    "trap 'rm -f x' EXIT",
    "watch -n1 'rev <<< abc'",
    "bash -c 'cd /tmp && rev'",
    r"find . -exec sh -c 'rev' \;",
    "a=(1 2 3)",
    "x=1 y=2 cmd arg",
    'declare -a arr=("C:\\x")',
    "time ls | wc -l",
    "nice -n 5 rev f",
    "timeout 5 rev",
    "env A=1 B=2 tree",
    "sudo rev f",
    # malformed / unterminated
    "echo 'unterminated",
    'echo "unterminated',
    "echo $(unterminated",
    "echo `unterminated",
    "echo $'unterminated",
    "echo ${unterminated",
    "echo $((unterminated",
    "echo <(unterminated",
    "(unterminated",
    "cat <<EOF\nbody never ends",
    "cat <<$\x27malformed",
    "a &&",
    "a ||",
    "a |",
    "a ;&",
    "a ;;",
    "a ;;&",
    "\\",
    "echo \\",
]


@pytest.mark.parametrize("source", LOSSLESS_CASES)
def test_lossless_render_source_roundtrip(source: str) -> None:
    """parse + render_source reproduces the input byte-for-byte."""
    script = parse(source)
    assert render_source(script) == source


def test_reconstruct_method_matches_function() -> None:
    source = "echo hi > out\n# c\n"
    script = parse(source)
    assert script.reconstruct() == render_source(script) == source


def test_lossless_on_crlf_source() -> None:
    source = "echo hi\r\nif x; then\r\nfi\r\n"
    assert render_source(parse(source)) == source


def test_lossless_nested_diagnostics_survive_roundtrip() -> None:
    source = "echo $( \"a $(b) c\" ) tail"
    script = parse(source)
    assert render_source(script) == source


# ── 2. command-position classification ───────────────────────────────────────


def _first_command(source: str) -> CommandNode:
    script = parse(source)
    for node in walk_nodes(script):
        if isinstance(node, CommandNode) and node.command_word is not None:
            return node
    raise AssertionError(f"no command node in {source!r}: {script.children}")


def test_first_word_after_start_is_command_word() -> None:
    cmd = _first_command("tree arg1 arg2")
    src = "tree arg1 arg2"
    assert cmd.command_word.span.text(src) == "tree"
    assert [a.span.text(src) for a in cmd.arguments] == ["arg1", "arg2"]
    assert cmd.assignments == ()


def _command_words(source: str) -> list:
    """All command-word texts found in ``source`` (source order)."""
    script = parse(source)
    out = []
    for node in walk_nodes(script):
        if isinstance(node, CommandNode) and node.command_word is not None:
            out.append(node.command_word.span.text(source))
    return out


@pytest.mark.parametrize(
    ("source", "command_text"),
    [
        ("a ; tree", "tree"),
        ("a && tree", "tree"),
        ("a || tree", "tree"),
        ("a | tree", "tree"),
        ("( tree )", "tree"),
        ("{ tree ; }", "tree"),
        ("x=1 tree", "tree"),
    ],
)
def test_command_position_after_each_operator(
    source: str, command_text: str
) -> None:
    # the word after each list/pipeline/group operator is a command word
    # (``tree`` appears as a command word in every form)
    assert command_text in _command_words(source)


def test_assignments_before_command_word() -> None:
    source = "A=1 B=2 tree arg"
    cmd = _first_command(source)
    src = source
    assert [a.span.text(src) for a in cmd.assignments] == ["A=1", "B=2"]
    assert cmd.command_word.span.text(src) == "tree"
    assert [a.span.text(src) for a in cmd.arguments] == ["arg"]


def test_words_after_command_word_are_arguments() -> None:
    source = "cat -n file.txt"
    cmd = _first_command(source)
    assert cmd.command_word.span.text(source) == "cat"
    assert [a.span.text(source) for a in cmd.arguments] == ["-n", "file.txt"]


def test_declaration_arguments_are_assignments() -> None:
    source = "declare x=1"
    cmd = _first_command(source)
    assert cmd.command_word.span.text(source) == "declare"
    # an assignment-shaped operand of a declaration builtin is recorded on
    # the assignments side even in argument position
    assert any(a.span.text(source).startswith("x=") for a in cmd.assignments)


def test_classify_assignment_predicate() -> None:
    assert classify_assignment("x=1") == "x"
    # ``+=`` is an append assignment: the legacy regex keeps the ``+`` in the
    # captured name (only the ``=`` is stripped).
    assert classify_assignment("FOO+=1") == "FOO+"
    assert classify_assignment("_a1=v") == "_a1"
    assert classify_assignment("1x=v") is None
    assert classify_assignment("=v") is None
    assert classify_assignment("plain") is None


def test_quoted_command_word_still_classified() -> None:
    source = "'tree' arg"
    cmd = _first_command(source)
    assert cmd.command_word.span.text(source) == "'tree'"
    assert cmd.command_word.literal_value == "tree"


def test_wrapper_hints_recorded_from_legacy_tables() -> None:
    cmd = _first_command("env A=1 tree")
    assert cmd.wrapper == "env"
    assert cmd.wrapper_hint == "command"
    cmd = _first_command("bash -c 'rev'")
    assert cmd.wrapper == "bash"
    assert cmd.wrapper_hint == "shell"
    cmd = _first_command("eval 'rev'")
    assert cmd.wrapper == "eval"
    assert cmd.wrapper_hint == "reparse"


def test_redirection_fd_and_truncate_predicate() -> None:
    source = "tree > out 2>&1"
    cmd = _first_command(source)
    by_op = {r.operator_text: r for r in cmd.redirections}
    gt = by_op[">"]
    assert gt.can_truncate is True
    assert gt.fd is None
    assert gt.target is not None and gt.target.span.text(source) == "out"
    dup = by_op["2>&"]
    assert dup.fd == 2
    assert dup.can_truncate is False  # dup-to-fd never creates a file


def test_redirection_truncates_input_vs_output() -> None:
    cmd = _first_command("sort < in")
    redir = cmd.redirections[0]
    assert redir.can_truncate is False
    cmd = _first_command("sort > out")
    assert cmd.redirections[0].can_truncate is True
    cmd = _first_command("sort <> both")
    assert cmd.redirections[0].can_truncate is True


def test_is_effective_nul_target_matches_legacy_semantics() -> None:
    def value_for(source_word: str):
        source = f"cmd > {source_word}"
        cmd_node = _first_command(source)
        target = cmd_node.redirections[0].target
        assert target is not None, source
        return is_effective_nul_target(target, source)

    assert value_for("nul") is True
    assert value_for("NUL") is True
    assert value_for("out.txt") is False
    assert value_for("$(echo nul)") is None  # runtime: undecidable


# ── 3. comments and heredoc bodies are data nodes ────────────────────────────


def test_comment_is_raw_data_node() -> None:
    script = parse("rev  # trailing")
    kinds = [n.kind for n in script.children]
    assert NodeKind.RAW in kinds
    comment = next(
        n for n in script.children if n.kind is NodeKind.RAW and n.reason == "comment"
    )
    src = "rev  # trailing"
    assert comment.span.text(src) == "# trailing"


def test_heredoc_body_is_data_never_command_context() -> None:
    source = "cat <<EOF\nrev not-a-command\n$(x) not-a-context\nEOF\n"
    script = parse(source)
    body = next(
        n
        for n in walk_nodes(script)
        if isinstance(n, RawNode) and n.reason == "heredoc-body"
    )
    assert "rev not-a-command" in body.span.text(source)
    # and crucially: 'rev' inside the body never became a command word
    for node in walk_nodes(script):
        if isinstance(node, CommandNode) and node.command_word is not None:
            text = node.command_word.span.text(source)
            assert text != "rev", f"heredoc body leaked into context: {text!r}"
            assert text != "$(x)"


def test_heredoc_node_flags() -> None:
    source = "cat <<-EOF\n\tbody\n\tEOF\n"
    script = parse(source)
    doc = next(n for n in walk_nodes(script) if isinstance(n, HeredocNode))
    assert doc.strip_tabs is True
    assert doc.delimiter_value == "EOF"
    assert doc.expands is True
    assert doc.body_start < doc.body_end
    assert doc.terminator_span is not None
    src = source
    assert doc.terminator_span.text(src) == "\tEOF"


def test_quoted_delimiter_disables_expansion() -> None:
    source = "cat <<'EOF'\n$(x)\nEOF\n"
    script = parse(source)
    doc = next(n for n in walk_nodes(script) if isinstance(n, HeredocNode))
    assert doc.expands is False
    assert doc.delimiter_value == "EOF"


# ── 4. substitutions inside double quotes are child contexts ─────────────────


def test_command_substitution_inside_double_quotes_is_child_context() -> None:
    source = 'echo "a $(rev <<< abc) d"'
    script = parse(source)
    expansions = [
        n for n in walk_nodes(script) if isinstance(n, ExpansionNode)
    ]
    assert expansions, "no expansion child built"
    exp = expansions[0]
    assert exp.expansion_kind is WordPartKind.COMMAND_SUBSTITUTION
    assert exp.child is not None
    inner = render_source(exp.child)
    assert inner == "rev <<< abc"


def test_nested_dquote_substitution_children_chain() -> None:
    source = 'echo "a $(b "c $(d)" e)" f"'
    script = parse(source)
    exps = [n for n in walk_nodes(script) if isinstance(n, ExpansionNode)]
    regions = sorted(
        render_source(e.child) for e in exps if e.child is not None
    )
    assert 'b "c $(d)" e' in regions


def test_process_substitution_child_context() -> None:
    source = "diff <(cat a) <(cat b)"
    script = parse(source)
    exps = [
        n
        for n in walk_nodes(script)
        if isinstance(n, ExpansionNode)
        and n.expansion_kind is WordPartKind.PROCESS_SUBSTITUTION
    ]
    assert len(exps) == 2
    assert render_source(exps[0].child) == "cat a"


def test_backquote_child_context() -> None:
    source = "echo `date`"
    script = parse(source)
    exps = [n for n in walk_nodes(script) if isinstance(n, ExpansionNode)]
    assert exps[0].expansion_kind is WordPartKind.BACKQUOTE_SUBSTITUTION
    assert render_source(exps[0].child) == "date"


def test_embedded_script_operand_is_inner_context() -> None:
    source = "eval 'tree > nul'"
    script = parse(source)
    emb = next(n for n in walk_nodes(script) if isinstance(n, EmbeddedScriptNode))
    assert emb.quote_kind is WordPartKind.SINGLE_QUOTE
    assert emb.owner_word is not None
    assert emb.owner_word.span.text(source) == "'tree > nul'"
    assert emb.inner is not None
    assert render_source(emb.inner) == "tree > nul"
    # and the inner script parsed its own command context
    inner_cmd = emb.inner.children[0]
    assert isinstance(inner_cmd, CommandNode)
    assert inner_cmd.command_word.span.text("tree > nul") == "tree"


def test_shell_dash_c_embedded_script() -> None:
    source = "bash -c 'cd /tmp && rev'"
    script = parse(source)
    emb = next(n for n in walk_nodes(script) if isinstance(n, EmbeddedScriptNode))
    assert emb.inner is not None
    assert render_source(emb.inner) == "cd /tmp && rev"


def test_find_exec_sh_c_embedded_script() -> None:
    source = "find . -exec sh -c 'rev' \\;"
    script = parse(source)
    emb = next(n for n in walk_nodes(script) if isinstance(n, EmbeddedScriptNode))
    assert render_source(emb.inner) == "rev"


# ── 5. malformed / unterminated constructs never raise ───────────────────────


MALFORMED = [
    "echo 'unterminated",
    'echo "unterminated',
    "echo $'unterminated",
    "echo $(unterminated",
    "echo `unterminated",
    "echo ${unterminated",
    "echo $((unterminated",
    "echo <(unterminated",
    "(unterminated",
    "cat <<EOF\nno terminator here",
    "cat <<",
    "a &&",
    "a |",
    ";;",
    ") x",
    "\\",
    "echo \\",
    'echo "a $(b',
    "case $x in",
    "if",
    "then",
    "fi",
    "done",
    "esac",
    "{",
    "echo \"$( \"nested unterminated",
    "cat <<EOF\nbody\nEOFtail",
]


@pytest.mark.parametrize("source", MALFORMED)
def test_malformed_input_never_raises_and_stays_lossless(source: str) -> None:
    script = parse(source)  # must not raise
    assert render_source(script) == source
    assert isinstance(script, ScriptNode)


@pytest.mark.parametrize(
    "source",
    [
        "echo 'unterminated",
        'echo "unterminated',
        "echo $'unterminated",
    ],
)
def test_unterminated_quotes_produce_diagnostics(source: str) -> None:
    script = parse(source)
    kinds = {d.kind for node in walk_nodes(script) for d in _script_diags(node)}
    assert DiagnosticKind.UNTERMINATED_QUOTE in kinds


def _script_diags(node) -> tuple:
    return getattr(node, "diagnostics", ())


def test_unterminated_substitution_produces_diagnostics() -> None:
    script = parse("echo $(oops")
    diags = [d for n in walk_nodes(script) for d in _script_diags(n)]
    assert any(d.kind is DiagnosticKind.UNTERMINATED_SUBST for d in diags)


def test_unterminated_heredoc_produces_diagnostic() -> None:
    script = parse("cat <<EOF\nbody with no terminator")
    diags = [d for n in walk_nodes(script) for d in _script_diags(n)]
    assert any(d.kind is DiagnosticKind.UNTERMINATED_HEREDOC for d in diags)


def test_malformed_input_yields_raw_or_partial_not_missing() -> None:
    # every character of a malformed source stays owned by *some* top-level
    # item (covered by the tiling) — verified indirectly by lossless render.
    for src in ["echo 'x", "(a", "a |", ";;"]:
        script = parse(src)
        assert render_source(script) == src
        assert script.children, src


# ── 6. deep nesting terminates (bounded, no RecursionError) ─────────────────

def test_2000_level_command_substitution_terminates() -> None:
    source = "$(" * 2000 + "x" + ")" * 2000
    start = time.perf_counter()
    script = parse(source)
    elapsed = time.perf_counter() - start
    assert render_source(script) == source
    assert elapsed < 5.0, f"deep nesting too slow: {elapsed:.2f}s"


def test_2000_level_nesting_emits_depth_diagnostic() -> None:
    source = "$(" * 2000 + "x" + ")" * 2000
    script = parse(source)
    diags = [d for n in walk_nodes(script) for d in _script_diags(n)]
    assert any(d.kind is DiagnosticKind.DEPTH_LIMIT for d in diags)


def test_nesting_bounded_at_max_depth() -> None:
    """The child chain descends but never beyond the imported legacy bound.

    walk_nodes is explicit-stack (safe on the deep tree); count ScriptNodes
    and the longest expansion->child chain through it.
    """
    from kimix_native.shell_compat.parser import _MAX_NESTING_DEPTH

    source = "$(" * 2000 + "x" + ")" * 2000
    script = parse(source)

    scripts = [n for n in walk_nodes(script) if isinstance(n, ScriptNode)]
    expansions = [n for n in walk_nodes(script) if isinstance(n, ExpansionNode)]
    assert expansions, "deep input must still build child expansions"
    # every child ScriptNode region is nested no deeper than the bound:
    # regions are strictly decreasing source slices; count them as a proxy
    # for chain length (one ScriptNode per descent level).
    assert len(scripts) <= _MAX_NESTING_DEPTH + 2
    assert len(scripts) >= 50  # real descent happened (not truncated at 0)


def test_walk_nodes_survives_deep_tree_without_recursion_error() -> None:
    source = "$(" * 1500 + "x" + ")" * 1500
    script = parse(source)
    count = sum(1 for _ in walk_nodes(script))
    assert count > 10


def test_deep_backtick_nesting_terminates() -> None:
    source = "echo " + "`" * 1000 + "x" + "`" * 1000
    start = time.perf_counter()
    script = parse(source)
    assert render_source(script) == source
    assert time.perf_counter() - start < 5.0


# ── 7. linear scaling for wide input ─────────────────────────────────────────


def test_20000_word_command_is_linear_and_bounded() -> None:
    source = " ".join(f"w{i}" for i in range(20000))
    start = time.perf_counter()
    script = parse(source)
    elapsed = time.perf_counter() - start
    assert render_source(script) == source
    assert elapsed < 5.0, f"wide command too slow: {elapsed:.2f}s"
    cmd = script.children[0]
    assert isinstance(cmd, CommandNode)
    assert cmd.command_word.span.text(source) == "w0"
    assert len(cmd.arguments) == 19999


def test_many_words_token_count_is_linear() -> None:
    from kimix_native.shell_compat.lexer import lex

    for n in (5000, 20000):
        source = " ".join(f"w{i}" for i in range(n))
        result = lex(source)
        words = sum(1 for t in result.tokens if t.kind.value == "word")
        raws = sum(1 for t in result.tokens if t.kind.value == "raw")
        assert words == n
        assert raws == n - 1  # single-space gaps


# ── 8. spans/models primitives ───────────────────────────────────────────────


def test_sourcespan_validation_and_helpers() -> None:
    span = SourceSpan(2, 5)
    assert span.length == 3
    assert span.text("abcdef") == "cde"
    with pytest.raises(ValueError):
        SourceSpan(-1, 2)
    with pytest.raises(ValueError):
        SourceSpan(3, 2)
    assert SourceSpan(0, 3).adjacent_to(SourceSpan(3, 6))
    assert not SourceSpan(0, 3).adjacent_to(SourceSpan(4, 6))
    assert SourceSpan(0, 3).overlaps(SourceSpan(2, 5))
    assert not SourceSpan(0, 3).overlaps(SourceSpan(3, 5))
    assert SourceSpan(0, 3).union(SourceSpan(5, 6)) == SourceSpan(0, 6)
    assert SourceSpan(0, 3).join(SourceSpan(3, 6)) == SourceSpan(0, 6)
    with pytest.raises(ValueError):
        SourceSpan(0, 2).join(SourceSpan(5, 7))


def test_edit_validation() -> None:
    edit = Edit(1, 3, "xy")
    assert edit.start == 1 and edit.end == 3 and edit.replacement == "xy"
    with pytest.raises(ValueError):
        Edit(2, 1, "")
    with pytest.raises(ValueError):
        Edit(-1, 2, "")
    assert edit.span == SourceSpan(1, 3)
    assert Edit(0, 1, "a").overlaps(Edit(1, 2, "b")) is False
    assert Edit(0, 2, "a").overlaps(Edit(1, 3, "b")) is True


def test_diagnostic_is_data_never_raised() -> None:
    diag = Diagnostic(DiagnosticKind.AMBIGUOUS_REGION, SourceSpan(0, 3), "why")
    assert ParseNote is Diagnostic
    assert isinstance(diag.kind, DiagnosticKind)
    assert diag.span.text("abcdef") == "abc"
    assert dataclasses.is_dataclass(diag)
    # frozen and slotted
    with pytest.raises(dataclasses.FrozenInstanceError):
        diag.kind = DiagnosticKind.DEPTH_LIMIT  # type: ignore[misc]


def test_nodes_are_frozen_and_slotted() -> None:
    node = WordNode(SourceSpan(0, 1), (WordPartKind.LITERAL, SourceSpan(0, 1)), "a")
    with pytest.raises(dataclasses.FrozenInstanceError):
        node.literal_value = "b"  # type: ignore[misc]
    with pytest.raises(TypeError):
        node.anything = 1  # slots: unknown attribute rejected


def test_decode_word_literal_contract() -> None:
    assert decode_word_literal("plain") == "plain"
    assert decode_word_literal("'a b'") == "a b"
    assert decode_word_literal('a"b"c') == "abc"
    assert decode_word_literal("$(x)") is None
    assert decode_word_literal("$((x))") is None
    assert decode_word_literal("${x}") is None
    assert decode_word_literal("$'nul'") is None
    assert decode_word_literal("a\\b") == "ab"
    assert decode_word_literal("*") is None
    assert decode_word_literal("'unterminated") is None
    assert decode_word_literal("nul") == "nul"


def test_decode_word_literal_partial_never_fails() -> None:
    assert decode_word_literal_partial('a"b $x"c') == "ab $xc"
    # an unterminated quote is preserved verbatim by the partial decoder
    assert decode_word_literal_partial("'x") == "'x"


def test_walk_nodes_visits_nested_regions() -> None:
    source = "echo $(rev <<< abc) tail"
    script = parse(source)
    seen_kinds = {n.kind for n in walk_nodes(script)}
    assert NodeKind.SCRIPT in seen_kinds
    assert NodeKind.COMMAND in seen_kinds
    assert NodeKind.EXPANSION in seen_kinds


def test_operator_and_newline_nodes_present() -> None:
    script = parse("a; b\nc")
    kinds = [n.kind for n in script.children]
    assert NodeKind.NEWLINE in kinds or any(
        isinstance(n, CommandListNode) for n in script.children
    )
    # the semicolon survives somewhere
    assert render_source(script) == "a; b\nc"


def test_parse_inner_is_standalone_source() -> None:
    inner = parse_inner("tree > nul")
    assert isinstance(inner, ScriptNode)
    assert inner.source == "tree > nul"
    assert render_source(inner) == "tree > nul"
    cmd = inner.children[0]
    assert isinstance(cmd, CommandNode)
    assert cmd.command_word.span.text("tree > nul") == "tree"


def test_word_to_node_carries_parts_and_literal() -> None:
    from kimix_native.shell_compat.lexer import lex

    source = "echo 'a b'"
    tokens = [t for t in lex(source).tokens if t.kind.value == "word"]
    word = word_to_node(tokens[1], source)
    assert word.literal_value == "a b"
    assert word.parts[0][0] is WordPartKind.SINGLE_QUOTE
