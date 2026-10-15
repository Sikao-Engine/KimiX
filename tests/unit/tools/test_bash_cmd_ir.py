r"""Phase-6 IR tests: cmd_translator.py structural rules (cmd ``@``/``if``/
``for`` + jump refusals) — cross-checked against the LIVE scanner and the
pristine (HEAD) oracle.

The Phase-6 port lives in ``kimix_native.shell_compat.cmd_translator``; the
LIVE scanner (``kimix_native.shell_compat.bash_core``) keeps the historical
method names as thin lazy delegates to it (the Phase-4/5 pattern), so these
tests drive BOTH sides:

1. the read-only validate/commit split: ``validate_cmd_if``/``validate_cmd_for``
   are side-effect-free (a fresh reader stays empty; every bail produces
   ``None`` and NO edits) and the committed edit sets are pairwise disjoint by
   construction;
2. the IR structural nodes (``CmdIfNode``/``CmdForNode``) for every condition
   kind (exist / defined / errorlevel / glued == compare (incl. ``/i``) /
   spaced arithmetic), ``else if`` chains, final ``else``, nested ``@if``,
   ``for`` with nested ``if``, path and nul edits inside bodies — with every
   produced edit asserted disjoint (sorted, pairwise non-overlapping);
3. the five jump refusal forms (``goto``, glued ``:label``, ``call :label``,
   ``call *.bat|*.cmd``, ``exit /b``) record through the unsupported channel
   WITHOUT a text change;
4. the §3.5 RuleDispatcher slots 1-3 (``register_cmd_rules``) reproduce the
   LIVE notes/orderings and feed disjoint plans;
5. ``cmd_conditional_fixes`` note ordering equals source order; the warning
   text is the BashFix-locked prose;
6. every delegate on the LIVE scanner equals the cmd_translator function;
7. full-BashFix differential (extracted cmd-suite cases + seeded battery >=
   6000) against the pristine oracle — zero mismatches.
"""
from __future__ import annotations

import importlib.util
import random
import re
import sys
from pathlib import Path

import pytest

from kimi_cli.native_loader import get_compat as _get_compat

assert _get_compat("_shell_compat") is not None

from kimix_native._shell_compat import _BashFixScanner as LiveScanner
from kimix_native._shell_compat import fix_bash_command as live_fix
from kimix_native.shell_compat import bash_core
from kimix_native.shell_compat import cmd_translator as ct
from kimix_native.shell_compat import translator as tr
from kimix_native.shell_compat.models import CommandNode, walk_nodes
from kimix_native.shell_compat.parser import parse

REPO = Path(__file__).resolve().parents[3]
PRISTINE_PATH = REPO / ".kimix_cache" / "tmp_pristine_sc.py"


def _load_pristine():
    if not PRISTINE_PATH.exists():
        return None
    spec = importlib.util.spec_from_file_location(
        "p6_pristine_sc", str(PRISTINE_PATH)
    )
    if spec is None or spec.loader is None:
        return None
    m = importlib.util.module_from_spec(spec)
    sys.modules["p6_pristine_sc"] = m
    spec.loader.exec_module(m)
    return m


PRISTINE = _load_pristine()

pytestmark_oracle = pytest.mark.skipif(
    PRISTINE is None, reason="pristine oracle cache absent"
)


def full(f):
    return (
        f.command,
        f.replacements,
        f.path_changes,
        f.shell_wrappers,
        f.nul_fixes,
        f.unsupported,
        f.cmd_conditional_fixes,
    )


def _reader(source: str) -> LiveScanner:
    return LiveScanner(source)


def _assert_disjoint(edits) -> None:
    srt = sorted(edits)
    for k in range(len(srt) - 1):
        assert srt[k][1] <= srt[k + 1][0], (
            f"overlapping edits {srt[k]} / {srt[k + 1]}"
        )


def _live_edits(source: str) -> list[tuple[int, int, str]]:
    sc = LiveScanner(source)
    sc._scan_range(0, sc.n)
    _assert_disjoint(sc.edits)
    return list(sc.edits)


def _word_end(source: str, start: int) -> int:
    """The scanner's own command-word extent starting at *start*.

    Same terminator set the scan loop's ``_read_word`` uses at command
    position (the scanner's own boundary logic, via the same regex the live
    loop bulk-skips with), so test word spans are byte-identical to the live
    dispatch spans.
    """
    sc = LiveScanner(source)
    return sc._read_word(start, len(source), scan_substitutions=False)


def _first_cmd(source: str, want_raw: str | None = None) -> CommandNode | None:
    sc = parse(source)
    for n in walk_nodes(sc):
        if isinstance(n, CommandNode) and n.command_word is not None:
            if want_raw is None or (
                source[n.command_word.span.start:n.command_word.span.end]
                == want_raw
            ):
                return n
    return None


# ---------------------------------------------------------------------------
# 1. validate is side-effect-free; every bail returns None (no edits)
# ---------------------------------------------------------------------------

VALID_IFS = [
    ("if exist x (a)", "exist"),
    ("if not exist x (a) else (b)", "exist"),
    ('if exist "My Data" (a)', "exist"),
    (r"if exist D:\a (a) else (b)", "exist"),
    ("if defined V (a)", "defined"),
    ("if not defined PYTHONHOME (a) else (b)", "defined"),
    ("if errorlevel 1 (a)", "errorlevel"),
    ("if not errorlevel 2 (a) else (b)", "errorlevel"),
    ('if "%V%"=="x" (a)', "compare"),
    ("if X==Y (a)", "compare"),
    ("if %errorlevel%==0 (a)", "compare"),
    ('if /i "%V%"=="xy" (a) else (b)', "compare"),
    ('if not "%V%"=="x" (a)', "compare"),
    ("if 5 geq 3 (a)", "arithmetic"),
    ("if %errorlevel% neq 0 (a) else (b)", "arithmetic"),
    ("if not 5 lss 3 (a)", "arithmetic"),
    ("if exist A (x) else if defined B (y) else if errorlevel 1 (z) else (w)",
     "chain"),
]

INVALID_IFS = [
    "if exist x echo yes",
    "if exist x (a) else b",
    "if exist x\n(a)",
    "if exist x (a)\nelse (b)",
    "if exist x (a) else\n(b)",
    "if exist (a)",
    "if exist $(f) (a)",
    "if exists x (a)",
    "if EXIST x (a)",
    "if not x exist (a)",
    "if exist x (a)rev",
    'if exist x (a)"q"',
    "if exist x ()",
    "if exist x (  ) else (b)",
    "if exist x (a) else ()",
    "if exist x (echo #c)",
    "if exist x #c",
    r"if exist *.txt (a)",
    'if exist "D:\\My Dir" (a)',
    "if exist x (",
    "if exist x (a",
    "if exist x (a) else (",
    "if exist x (a) else",
    "if defined %V% (x)",
    "if defined 1 (x)",
    "if defined a-b (x)",
    "if errorlevel abc (x)",
    "if errorlevel 1.5 (x)",
    'if a=="a" (x)',
    'if "a"==b (x)',
    'if "a" == "b" (x)',
    "if !V!==1 (x)",
    'if "%V%"=="a*b" (x)',
    'if "%V%"=="a$b" (x)',
    'if %V%=="a b" (x)',
    "if 5 GEQ 3 (x)",
    "if not X geq Y (x)",
    "if 5 geq (x)",
    "if /i exist x (a)",
    "if /i defined V (a)",
    "if /i errorlevel 1 (a)",
    "IF DEFINED X (x)",
    "if Not defined x (x)",
    "if defined V (a)\nelse (b)",
    "if defined V\n(a)",
    "if defined V ()",
    "if defined V (a)#c",
    "if defined V (a)rev",
]


@pytest.mark.parametrize("source,kind", VALID_IFS)
def test_validate_cmd_if_kinds(source: str, kind: str) -> None:
    src = source
    reader = _reader(src)
    match = ct.validate_cmd_if(reader, src, 0, _word_end(src, 0), len(src))
    assert match is not None, kind
    # validation is strictly read-only: the reader recorded nothing.
    assert reader.edits == []
    assert reader.path_notes == []
    assert reader.cmd_conditional_notes == []
    assert reader.unsupported == []
    # the first segment kind is exactly the expected condition kind
    if kind != "chain":
        assert match.segments[0].kind == kind
    else:
        kinds = [s.kind for s in match.segments]
        assert kinds == ["exist", "defined", "errorlevel"]
        assert match.else_open is not None and match.else_close is not None
    # structural edits are pairwise disjoint by construction
    _assert_disjoint(ct.cmd_if_structural_edits(src, match))


@pytest.mark.parametrize("source", INVALID_IFS)
def test_validate_cmd_if_bails(source: str) -> None:
    src = source
    reader = _reader(src)
    assert ct.validate_cmd_if(reader, src, 0, _word_end(src, 0), len(src)) is None
    assert reader.edits == []  # strictly read-only


@pytest.mark.parametrize("source,kind", VALID_IFS)
def test_cmd_if_node_shape(source: str, kind: str) -> None:
    src = source
    node = ct.cmd_if_node(_reader(src), src, 0, _word_end(src, 0), len(src))
    assert node is not None
    assert node.span[0] == 0
    # the note is the keyword region — byte-identical to the LIVE note
    sc = LiveScanner(src)
    consumed = sc._try_cmd_if(0, _word_end(src, 0), sc.n)
    assert consumed is not None
    assert sc.cmd_conditional_notes == [node.note]
    assert src[node.if_start : node.match.segments[0].op_end] == node.note


def test_cmd_if_negated_folded_segment_fields() -> None:
    src = 'if /i not "%V%"=="xy" (a)'
    m = ct.validate_cmd_if(_reader(src), src, 0, 2, len(src))
    assert m is not None
    seg = m.segments[0]
    assert seg.negated is True
    assert seg.folded is True
    assert seg.kind == "compare"
    assert seg.cmp_text == '${V,,} == "xy"'
    src2 = "if not 5 lss 3 (a)"
    m2 = ct.validate_cmd_if(_reader(src2), src2, 0, 2, len(src2))
    assert m2 is not None
    assert m2.segments[0].kind == "arithmetic"
    assert m2.segments[0].negated is True
    assert m2.segments[0].cmp_text == "5 < 3"


def test_cmd_if_else_chain_and_final_else_spans() -> None:
    src = "if exist A (x) else if exist B (y) else (z)"
    m = ct.validate_cmd_if(_reader(src), src, 0, 2, len(src))
    assert m is not None
    assert len(m.segments) == 2
    assert m.segments[1].header_start == src.index("else if")
    assert src[m.else_open] == "(" and src[m.else_close] == ")"
    assert m.else_close == len(src) - 1
    # commit returns the index just past the final ')'
    sc = LiveScanner(src)
    out = sc._try_cmd_if(0, 2, sc.n)
    assert out == sc.n


def test_cmd_if_chain_two_segments_note_and_edits() -> None:
    src = "if exist A (echo 1) else if not exist B (echo 2)"
    m = ct.validate_cmd_if(_reader(src), src, 0, 2, len(src))
    assert m is not None
    assert len(m.segments) == 2
    assert m.segments[1].negated is True
    assert m.segments[1].header_start == src.index("else if")
    assert m.else_open is None and m.else_close is None
    edits = ct.cmd_if_structural_edits(src, m)
    _assert_disjoint(edits)
    rendered = tr.render_splices(src, edits)
    assert rendered == (
        "if [[ -e A ]]; then echo 1; elif [[ ! -e B ]]; then echo 2; fi"
    )


VALID_FOR = [
    "for %f in (a b) do echo %f",
    "for %%f in (a,b,c) do echo %%f",
    "for %f in (*.txt *.md) do echo %f",
    r"for %f in (D:\a D:\b\c.txt) do type %f",
    "for %i in (a) do %i",
    "for %f in (a) do echo %f\nnext",
    "for %f in (a) do del %f 2>nul",
    "for %f in (a b) do if exist %f (echo f) else (echo n)",
    "for %%i in (a) do echo %%i && echo x",
]

INVALID_FOR = [
    "for /l %i in (1,1,10) do echo %i",
    "for /d %i in (a) do echo %i",
    "for /r %i in (a) do echo %i",
    "for /f %i in (a) do echo %i",
    "for %f in (a) do echo %~nf",
    "for %f in (a) do echo %~dpf",
    "for %d in (1 2 3) do echo file_%d",
    "for %f in (a) do echo %OTHER%",
    "for %f in (%OS%) do echo %f",
    "for %f in ($HOME) do echo %f",
    "for %f in (a) do echo %f ^ tail",
    "for %f in (!x) do echo %f",
    'for %f in ("a b") do echo %f',
    'for %f in (a) do echo "%f"',
    "for %f in (a) do echo %f & echo x",
    "for %1 in (a) do echo %1",
    "for %a b in (c) do echo x",
    "for %f in a b do echo %f",
    "for %f in (a) do",
    "for %f in (a) do %f; # c",
    "for %f in (a) doe echo %f",
    "for %f in (a) do (echo %f)",
    "for %f in (a\nb) do echo %f",
    "for i in a b; do echo $i; done",
    "for ((i=0;i<3;i++)); do echo $i; done",
    "for %i in (a) do for %j in (b) do echo %i %j",
]


@pytest.mark.parametrize("source", VALID_FOR)
def test_validate_cmd_for_shapes(source: str) -> None:
    src = source
    reader = _reader(src)
    m = ct.validate_cmd_for(reader, src, 0, _word_end(src, 0), len(src))
    assert m is not None
    assert reader.edits == []  # read-only
    assert m.name == re.match(r"for %+([A-Za-z_]\w*)", src).group(1)
    # the loop token span is exactly the %name / %%name word
    assert src[m.tok_start:m.tok_end].lstrip("%") == m.name
    assert src[m.open_pos] == "(" and src[m.close_pos] == ")"
    assert src[m.do_end - 2:m.do_end] == "do"
    assert "\n" not in src[m.body_start:m.body_end]
    _assert_disjoint(ct.cmd_for_structural_edits(src, m))
    # every var edit is a whole-word loop token rewritten to $name
    for a, b in m.var_edits:
        assert src[a:b] in ("%" + m.name, "%%" + m.name)
    # every comma span is a single comma character
    for a, b in m.comma_spans:
        assert src[a:b] == "," and b - a == 1
    # every list path edit is a Windows path rewrite inside the list region
    for a, b, repl in m.list_path_edits:
        assert m.open_pos < a < b <= m.close_pos
        assert "\\" in src[a:b] and "\\" not in repl
    # the IR node carries the LIVE note text (a nested cmd construct in the
    # body records its own note after it, in source order)
    node = ct.cmd_for_node(reader, src, 0, _word_end(src, 0), len(src))
    assert node is not None
    sc = LiveScanner(src)
    assert sc._try_cmd_for(0, _word_end(src, 0), sc.n) is not None
    assert sc.cmd_conditional_notes[0] == node.note


@pytest.mark.parametrize("source", INVALID_FOR)
def test_validate_cmd_for_bails(source: str) -> None:
    src = source
    reader = _reader(src)
    assert ct.validate_cmd_for(reader, src, 0, _word_end(src, 0), len(src)) is None
    assert reader.edits == []


# ---------------------------------------------------------------------------
# 2. @ rule: decisions + documented bails
# ---------------------------------------------------------------------------

AT_STATEMENTS = ["@echo off", "@echo on", "@echo off >nul", "@echo off; dir",
                 "@echo off && dir", "@echo off\nfoo", "@echo off | cat",
                 "@echo  off", "@echo\toff"]
AT_STRIPS = ["@echo offline", "@echo off & dir", "@dir x", "@type foo.txt",
             "@rev", "@if exist P (A) else (B)", "@for %f in (a) do echo %f",
             "@ECHO OFF", "@echo hello"]
AT_BAILS = ["@", "@@x", '"@"dir', "'@'dir", "@echo.", "echo off", "x @ y",
            "echo a @ b", "$@"]


@pytest.mark.parametrize("source", AT_STATEMENTS)
def test_at_statement_mode(source: str) -> None:
    src = source
    reader = _reader(src)
    word_end = _word_end(src, 0)
    decision = ct.at_prefix_rule(reader, src, 0, word_end, len(src))
    assert decision is not None
    assert decision.mode == "statement"
    assert decision.note is not None and decision.note.startswith("@echo")
    a, b, repl = decision.edit
    assert (a, b) == (0, decision.index)
    assert repl in ("set +x", "set -x")
    # the LIVE delegate appends exactly this edit + note
    sc = LiveScanner(src)
    idx, mode = sc._try_at_prefix(0, word_end, sc.n)
    assert (idx, mode) == (decision.index, "statement")
    assert sc.edits == [decision.edit]
    assert sc.cmd_conditional_notes == [decision.note]


@pytest.mark.parametrize("source", AT_STRIPS)
def test_at_strip_mode(source: str) -> None:
    src = source
    reader = _reader(src)
    word_end = _word_end(src, 0)
    decision = ct.at_prefix_rule(reader, src, 0, word_end, len(src))
    assert decision is not None
    assert decision.mode == "strip"
    assert decision.edit == (0, 1, "")
    assert decision.index == 1
    assert decision.note is None


@pytest.mark.parametrize("source", AT_BAILS)
def test_at_bails(source: str) -> None:
    reader = _reader(source)
    word_end = _word_end(source, 0)
    if source[:1] not in ('"', "'") and word_end < len(source):
        # ``x @ y`` / ``echo a @ b`` / ``$@``: the first word is not an @-word
        # at all — the rule bails on ``raw[:1] != '@'``.
        pass
    assert ct.at_prefix_rule(reader, source, 0, word_end, len(source)) is None


def test_at_delegate_equals_pure_decision() -> None:
    """The LIVE _try_at_prefix appends exactly the decision's edit/note."""
    src = r"@if exist D:\a (dir D:\a 2>nul)"
    sc = LiveScanner(src)
    word_end = _word_end(src, 0)
    idx, mode = sc._try_at_prefix(0, word_end, sc.n)
    assert mode == "strip"
    decision = ct.at_prefix_rule(_reader(src), src, 0, word_end, len(src))
    assert decision.edit == sc.edits[0]
    assert decision.index == idx


# ---------------------------------------------------------------------------
# 3. commit reproduces the LIVE structural edits; the disjointness holds on
#    composed cases (nested @if, for-with-if, path + nul edits in bodies)
# ---------------------------------------------------------------------------

COMPOSED = [
    r"@if exist D:\a (dir D:\a 2>nul)",
    "for %f in (a b) do if exist %f (echo f) else (echo n)",
    "for %f in (a) do if exist %f (del %f 2>nul) else (echo no)",
    r"if defined V (dir D:\a 2>nul) else (if exist C:\p (echo y))",
    "if exist A (if exist B (echo x) else (echo y))",
    r"if exist D:\a (dir D:\a) else (echo n)",
    r"for %%f in (D:\a,D:\b) do type %%f 2>nul",
    r"@for %f in (D:\a) do echo %f > nul",
    "if %errorlevel% neq 0 (echo bad) else (cd C:\\x)",
    "if exist x (tree) else (rev) && goto :end",
]


@pytest.mark.parametrize("source", COMPOSED)
def test_composed_edits_disjoint_and_match_live(source: str) -> None:
    edits = _live_edits(source)  # also asserts LIVE disjointness
    result = live_fix(source)
    assert result.changed
    if source == "for %f in (a b) do if exist %f (echo f) else (echo n)":
        assert len(edits) >= 8  # header/token/parens/var + nested if headers
    # the FULL BashFix tuple matches the pristine oracle (when cached)
    if PRISTINE is not None:
        assert full(PRISTINE.fix_bash_command(source)) == full(result)


def test_commit_cmd_if_edit_stream_equals_live_method() -> None:
    """The IR-side commit on a fresh scanner emits the exact LIVE edit stream."""
    src = "if exist A (x) else if defined B (y) else (z)"
    live = LiveScanner(src)
    out_live = live._try_cmd_if(0, 2, live.n)
    ir = LiveScanner(src)  # a fresh reader for the IR-side commit
    match = ct.validate_cmd_if(ir, src, 0, 2, len(src))
    assert match is not None
    ir.cmd_conditional_notes.append(src[0 : match.segments[0].op_end])
    out_ir = ct.commit_cmd_if(ir, src, match)
    assert out_ir == out_live
    assert ir.edits == live.edits
    assert ir.path_notes == live.path_notes
    assert ir.cmd_conditional_notes == live.cmd_conditional_notes


def test_commit_cmd_for_edit_stream_equals_live_method() -> None:
    src = "for %f in (a) do if exist %f (echo f) else (echo n)"
    live = LiveScanner(src)
    out_live = live._try_cmd_for(0, 3, live.n)
    ir = LiveScanner(src)
    match = ct.validate_cmd_for(ir, src, 0, 3, len(src))
    assert match is not None
    ir.cmd_conditional_notes.append(src[0 : match.close_pos + 1])
    out_ir = ct.commit_cmd_for(ir, src, match)
    assert out_ir == out_live
    assert ir.edits == live.edits
    assert ir.cmd_conditional_notes == live.cmd_conditional_notes


def test_if_exist_operand_path_edit_recorded_and_disjoint() -> None:
    src = r"if exist D:\a (dir D:\a)"
    m = ct.validate_cmd_if(_reader(src), src, 0, 2, len(src))
    assert m is not None
    edits = ct.cmd_if_structural_edits(src, m)
    # the operand got the Windows-path rewrite inside the structural set
    assert any(s == m.segments[0].op_start and r == "D:/a" for s, e, r in edits)
    _assert_disjoint(edits)


def test_render_structural_set_equals_live_structure() -> None:
    """Rendering ONLY the structural edits yields the structural rewrites;
    the recursive body edits are the live scanner's own (composition test)."""
    src = r"if exist D:\a (dir D:\a)"
    m = ct.validate_cmd_if(_reader(src), src, 0, 2, len(src))
    assert m is not None
    rendered = tr.render_splices(src, ct.cmd_if_structural_edits(src, m))
    assert rendered == "if [[ -e D:/a ]]; then dir D:\\a; fi"
    live = live_fix(src)
    # LIVE additionally rewrites the body path
    assert live.command == "if [[ -e D:/a ]]; then dir D:/a; fi"


def test_nested_at_if_disjoint_full_pipeline() -> None:
    """``@if ...``: the strip edit and the header edits are disjoint and the
    FULL pipeline (live scanner) renders the exact command."""
    src = r"@if exist D:\a (dir D:\a 2>nul)"
    sc = LiveScanner(src)
    sc._scan_range(0, sc.n)
    _assert_disjoint(sc.edits)
    strip = ct.at_prefix_rule(_reader(src), src, 0, _word_end(src, 0), len(src))
    assert strip.mode == "strip"
    # the scanner's first recorded edit IS the strip splice
    assert strip.edit in sc.edits
    # and the structural if-set tiles the rest without overlap: feed both
    # into the shared EditPlan together with the live body edits — all
    # pairwise disjoint (the live edit list is already disjoint, and the
    # structural set is a subset of the LIVE header/close edits):
    m = ct.validate_cmd_if(_reader(src), src, 1, 3, len(src))
    assert m is not None  # validate after the strip position
    plan = tr.EditPlan()
    for a, b, r in sc.edits:
        plan.add(a, b, r)
    assert plan.render(src) == live_fix(src).command


def test_for_with_nested_if_renders_through_shared_plan() -> None:
    """for + nested if: the two structural edit sets tile into ONE EditPlan
    and render the exact LIVE command (the body path/nul edits ride the LIVE
    scanner's recursive scan; cross-checked disjoint here)."""
    src = "for %f in (a b) do if exist %f (echo f) else (echo n)"
    fm = ct.validate_cmd_for(_reader(src), src, 0, 3, len(src))
    assert fm is not None
    if_start = src.index("if")
    im = ct.validate_cmd_if(_reader(src), src, if_start, if_start + 2, len(src))
    assert im is not None
    plan = tr.EditPlan()
    for a, b, r in ct.cmd_for_structural_edits(src, fm):
        plan.add(a, b, r)
    for a, b, r in ct.cmd_if_structural_edits(src, im):
        plan.add(a, b, r)
    assert plan.render(src) == (
        "for f in a b; do if [[ -e $f ]]; then echo f; else echo n; fi; done"
    )
    assert plan.render(src) == live_fix(src).command


# ---------------------------------------------------------------------------
# 4. jump refusals: five forms, unsupported channel, byte-for-byte text
# ---------------------------------------------------------------------------

JUMP_FORMS = [
    ("goto :end", "goto"),
    (":label", "goto"),
    ("call :sub", "call :label"),
    ("call a.BAT", "call :label"),
    ("call x.cmd", "call :label"),
    ("exit /b", "exit /b"),
    ("exit /B 2", "exit /b"),
]


@pytest.mark.parametrize("command,key", JUMP_FORMS)
def test_jump_record_and_live_equal(command: str, key: str) -> None:
    reader = _reader(command)
    word_end = _word_end(command, 0)
    raw = command[:word_end]
    recorded = ct.cmd_jump_record(reader, command, raw, raw, word_end, len(command))
    assert recorded == key
    # refusal-only: the live fix keeps the text byte-for-byte
    result = live_fix(command)
    assert result.command == command
    assert key in result.unsupported
    assert not result.changed


def test_record_cmd_jump_delegate() -> None:
    sc = LiveScanner("call :sub")
    assert sc._record_cmd_jump("call", "call", 4, sc.n) is True
    assert sc.unsupported == ["call :label"]
    # dedup first-wins
    assert sc._record_cmd_jump("call", "call", 4, sc.n) is True
    assert sc.unsupported == ["call :label"]


@pytest.mark.parametrize(
    "command", ["call prog.exe", "call foo", "call", "exit", "exit 3", ":",
                ": label", "echo goto"]
)
def test_non_jumps_not_recorded(command: str) -> None:
    reader = _reader(command)
    word_end = _word_end(command, 0)
    raw = command[:word_end]
    assert ct.cmd_jump_record(reader, command, raw, raw, word_end, len(command)) is None


def test_peek_literal_word_delegate() -> None:
    sc = LiveScanner("call a.bat")
    assert sc._peek_literal_word(4, sc.n) == "a.bat"
    assert LiveScanner("call")._peek_literal_word(4, 4) is None


# ---------------------------------------------------------------------------
# 5. note ordering == source order + warning text locked by BashFix
# ---------------------------------------------------------------------------

def test_note_ordering_is_source_order() -> None:
    src = (
        r"if exist D:\a (for %f in (x) do echo %f) else "
        r"(if defined V (tree 2>nul) else (goto :end))"
    )
    result = live_fix(src)
    # outer if first, then the for, then the inner if (all in source order)
    assert result.cmd_conditional_fixes == (
        r"if exist D:\a",
        "for %f in (x)",
        "if defined V",
    )
    assert "goto" in result.unsupported
    _live_edits(src)


def test_note_ordering_matches_list_of_own_positions() -> None:
    src = "if exist a (x) && if not exist b (y) && for %f in (c) do echo %f"
    result = live_fix(src)
    notes = list(result.cmd_conditional_fixes)
    assert notes == ["if exist a", "if not exist b", "for %f in (c)"]
    # source order: every note occurs strictly after the previous one
    pos = 0
    for note in notes:
        idx = src.index(note, pos)
        pos = idx + len(note)


def test_warning_text_locked() -> None:
    r1 = live_fix("if exist x (a) else (b)")
    assert "cmd.exe control-flow construct" in r1.warning
    assert "`if exist x`" in r1.warning
    r2 = live_fix("@echo off")
    assert "`@echo off`" in r2.warning
    r3 = live_fix("for %f in (a) do echo %f")
    assert "`for %f in (a)`" in r3.warning
    # refusal-only: no cmd-warning prose, unsupported names the reason
    r4 = live_fix("goto :eof")
    assert "goto" in r4.warning
    assert "control-flow construct" not in r4.warning


# ---------------------------------------------------------------------------
# 6. §3.5 dispatcher slots 1-3 (IR-side handlers)
# ---------------------------------------------------------------------------

def test_register_cmd_rules_fills_slots_1_to_3() -> None:
    d = tr.register_cmd_rules(tr.RuleDispatcher())
    assert d.rules == (
        (tr.RuleDispatcher.PRIORITY_CMD_AT, "cmd_at"),
        (tr.RuleDispatcher.PRIORITY_CMD_CONTROL, "cmd_control"),
        (tr.RuleDispatcher.PRIORITY_UNSUPPORTED, "cmd_jump"),
    )


def _dispatch_outer(src: str) -> tr.TranslationContext:
    """Dispatch the parsed tree's nodes against slots 1-3.

    A cmd conditional's validated match ALREADY covers the whole chain
    (``else if`` segments + final ``else`` branch), so the second ``if`` word
    the tolerant parser tiles would re-validate the same spans: those
    re-dispatches are dropped (the dispatcher's single overlap check would
    reject a same-region double schedule; Phase 7's orchestrator honours the
    scanner's consumed extent).  The returned context holds the first
    non-overlapping schedule — always the outermost match.
    """
    d = tr.register_cmd_rules(tr.RuleDispatcher())
    ctx = tr.TranslationContext()
    nodes = [n for n in walk_nodes(parse(src))
             if isinstance(n, CommandNode) and n.command_word is not None]
    for n in nodes:
        try:
            d.dispatch(n, src, ctx)
        except tr.OverlappingEdits:
            # a nested keyword the tolerant parser tiles INSIDE an already
            # matched cmd construct re-validates the same region (the live
            # scanner never reaches it: its consumed extent skips the whole
            # conditional).  Phase 7's orchestrator honours the extent; here
            # the overlapping re-dispatch is dropped.
            continue
    return ctx


def test_dispatcher_slot2_if_structural_render_equals_live() -> None:
    """IR slot-2 handler: structural edits + notes match the LIVE scanner's
    cmd-structural edits exactly (the body's inner path/nul edits are the
    scanner's recursive-scan additions, excluded here)."""
    src = r"if exist D:\a (dir D:\a 2>nul) else (echo no)"
    ctx = _dispatch_outer(src)
    _assert_disjoint(ctx.plan.edits)
    assert ctx.cmd_conditional_notes == [r"if exist D:\a"]
    sc = LiveScanner(src)
    sc._scan_range(0, sc.n)
    # LIVE edit set filtered to the conditional's structural splices (the
    # same ``(start, end)`` regions the pure structural render emits — the
    # body-internal ``2>nul``/path edits live strictly inside the parens):
    m = ct.validate_cmd_if(_reader(src), src, 0, 2, len(src))
    assert m is not None
    seg = m.segments[0]
    structural = {
        e for e in sc.edits
        if not (seg.open_pos < e[0] and e[1] <= seg.close_pos)
    }
    assert sorted(ctx.plan.edits) == sorted(structural)
    # and the structural render is the live command modulo body edits
    assert ctx.plan.render(src).startswith("if [[ -e D:/a ]]; then dir D:\\a")


def test_dispatcher_slot2_renders_full_live_command_without_body_edits() -> None:
    """A pure ``if`` (no inner path/nul edits) renders EXACTLY the live
    command from the IR handler's edit set alone."""
    src = "for %f in (a b) do if exist %f (echo f) else (echo n)"
    d = tr.register_cmd_rules(tr.RuleDispatcher())
    ctx = tr.TranslationContext()
    node = _first_cmd(src, "for")
    d.dispatch(node, src, ctx)
    # the nested cmd-if inside the body is dispatched from the RAW ``if``
    # word position the for-match recorded (the live scanner reaches it via
    # the recursive body scan; the IR walks the parsed tree in Phase 7).
    if_start = src.index("if")
    match = ct.validate_cmd_if(_reader(src), src, if_start, if_start + 2, len(src))
    assert match is not None
    for a, b, r in ct.cmd_if_structural_edits(src, match):
        ctx.plan.add(a, b, r)
    ctx.record_cmd_conditional(src[if_start:match.segments[0].op_end])
    assert ctx.plan.render(src) == live_fix(src).command
    assert ctx.cmd_conditional_notes == list(live_fix(src).cmd_conditional_fixes)
    _assert_disjoint(ctx.plan.edits)


def test_dispatcher_slot2_for_body_edits_compose_disjointly() -> None:
    """Path/nul edits inside branch bodies: the LIVE scanner composes them;
    the structural set fed into the shared EditPlan stays disjoint with the
    body edits the scanner records (cross-checked via .edits)."""
    src = r"if defined V (dir D:\a 2>nul) else (echo no)"
    result = live_fix(src)
    assert result.nul_fixes == ("nul",)
    assert result.path_changes == (r"D:\a",)
    _live_edits(src)
    # IR side: validate + structural render + live body edits are consistent
    m = ct.validate_cmd_if(_reader(src), src, 0, 2, len(src))
    assert m is not None
    rendered = tr.render_splices(src, ct.cmd_if_structural_edits(src, m))
    assert rendered.startswith(
        "if [[ -n ${V+x} ]]; then dir D:\\a 2>nul; else echo no; fi"
    )


def test_dispatcher_slot1_at_and_slot3_jump() -> None:
    d = tr.register_cmd_rules(tr.RuleDispatcher())
    ctx = tr.TranslationContext()
    src = "@echo off"
    node = _first_cmd(src, "@echo")
    assert node is not None
    d.dispatch(node, src, ctx)
    assert ctx.plan.render(src) == "set +x"
    assert ctx.cmd_conditional_notes == ["@echo off"]

    ctx2 = tr.TranslationContext()
    node2 = _first_cmd("goto :end", "goto")
    d.dispatch(node2, "goto :end", ctx2)
    assert ctx2.unsupported == ["goto"]
    assert ctx2.plan.edits == ()  # refusal: NO text change


def test_dispatcher_bails_record_nothing() -> None:
    d = tr.register_cmd_rules(tr.RuleDispatcher())
    for src, word in [
        ("if exists x (a)", "if"),
        ("for %f in (a) do echo %~nf", "for"),
        ("call foo", "call"),
        ("exit 3", "exit"),
    ]:
        ctx = tr.TranslationContext()
        node = _first_cmd(src, word)
        if node is None:
            continue
        d.dispatch(node, src, ctx)
        assert ctx.plan.edits == (), src
        assert ctx.cmd_conditional_notes == [], src
        assert ctx.unsupported == [], src


@pytest.mark.parametrize("source,kind", VALID_IFS)
def test_dispatcher_slot2_if_feeds_plan_and_notes(source: str, kind: str) -> None:
    src = source
    d = tr.register_cmd_rules(tr.RuleDispatcher())
    ctx = tr.TranslationContext()
    node = _first_cmd(src, "if")
    if node is None:  # the tolerant parser may not tile every exotic form
        pytest.skip("no tiling command node for " + src)
    d.dispatch(node, src, ctx)
    live = live_fix(src)
    if kind == "chain":
        # an else-if chain parses as multiple command nodes for the IR
        # dispatcher (only the outermost header is dispatched here); assert
        # the outer note matches and edits are disjoint.
        assert ctx.cmd_conditional_notes == list(live.cmd_conditional_fixes[:1])
    else:
        assert ctx.cmd_conditional_notes == list(live.cmd_conditional_fixes)
    _assert_disjoint(ctx.plan.edits)
    if ctx.plan:
        # the structural edit set, rendered WITHOUT body recursion, still
        # contains the bash test header the LIVE output has (negation
        # included: ``[[ ! -e`` / ``[[ ! -n`` / ``(( ! (``)
        rendered = ctx.plan.render(src)
        assert (" -e" in rendered or " -n" in rendered
                or "((" in rendered or '== "' in rendered
                or "$?" in rendered), rendered
        assert rendered == live.command, (rendered, live.command)


# ---------------------------------------------------------------------------
# 7. delegates equal the pure functions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw", ["a==b", '"a"=="b"', '"a"=="b"==c', 'a=="b"', '""==""', "x",
            '"x', "a=b==c", '"a"b=="c"', "==x", "a=="]
)
def test_split_top_eq_delegate(raw: str) -> None:
    assert LiveScanner._split_top_eq(raw) == ct.split_top_eq(raw)


@pytest.mark.parametrize(
    "raw,folded,numeric",
    [("5", False, True), ("a", False, False), ('"a"', False, False),
     ("%V%", False, False), ("%V%", True, False), ("%V%", False, True),
     ("%errorlevel%", False, False), ("%errorlevel%", True, True),
     ("%~ni%", False, False), ("", False, False), ("a b", False, False),
     ("$x", False, False), ("a*b", False, False), ("-5", False, True),
     ("a;b", False, False)],
)
def test_cmd_cmp_side_delegate(raw: str, folded: bool, numeric: bool) -> None:
    sc = LiveScanner("x")
    assert sc._cmd_cmp_side(raw, folded=folded, numeric=numeric) == (
        ct.cmd_cmp_side(raw, folded=folded, numeric=numeric)
    )


@pytest.mark.parametrize(
    "lhs,rhs,folded,numeric",
    [("%V%", "x", False, False), ("5", "3", False, True),
     ("a", "b", False, False), ("%V%", "3", True, True)],
)
def test_build_cmd_cmp_delegate(lhs, rhs, folded, numeric) -> None:
    sc = LiveScanner("x")
    assert sc._build_cmd_cmp(lhs, rhs, "==", folded=folded, numeric=numeric) == (
        ct.build_cmd_cmp(lhs, rhs, "==", folded=folded, numeric=numeric)
    )


@pytest.mark.parametrize(
    "body", ["a", "a b", "", "  ", "x #c", "'q # c'", '"q # c"', "a\\b",
             "a\nb", "$(", "(", "'", '"', "a'b", 'a"b', "#a", "& #c"]
)
def test_cmd_if_branch_ok_delegate(body: str) -> None:
    sc = LiveScanner(body)
    assert sc._cmd_if_branch_ok(0, len(body)) == ct.cmd_if_branch_ok(
        body, 0, len(body)
    )


@pytest.mark.parametrize(
    "body,pos",
    [("", 0), ("x", 0), (" &x", 1), ("&&x", 1), (";x", 1), ("|x", 1),
     (">x", 1), ("\nx", 1), ("xx", 1), (" ", 0), ("&", 0)],
)
def test_at_boundary_ok_delegate(body: str, pos: int) -> None:
    sc = LiveScanner(body)
    assert sc._at_boundary_ok(pos, len(body)) == ct.at_boundary_ok(
        body, pos, len(body)
    )


@pytest.mark.parametrize(
    "item", ["a", "a,b", '"a b"', "$x", "`x", "!x", "%x", "^x", "*.txt", "'"]
)
def test_cmd_for_item_ok_delegate(item: str) -> None:
    assert LiveScanner._cmd_for_item_ok(item) == ct.cmd_for_item_ok(item)


@pytest.mark.parametrize(
    "body", ["a & b", "a && b", "'a & b'", '"a & b"', "a\\&b", "&", "a || b"]
)
def test_cmd_body_ampersand_delegate(body: str) -> None:
    assert LiveScanner._cmd_body_has_bare_ampersand(body) == (
        ct.cmd_body_has_bare_ampersand(body)
    )


def test_match_records_shared_objects() -> None:
    """The IR and the live scanner share ONE dataclass + constant objects
    (lower-churn ownership: canonical homes stay bash_core)."""
    assert ct._CmdIfSegment is bash_core._CmdIfSegment
    assert ct._CmdIfMatch is bash_core._CmdIfMatch
    assert ct._CmdForMatch is bash_core._CmdForMatch
    assert ct._CMD_IF_TAIL_SAFE_CHARS is bash_core._CMD_IF_TAIL_SAFE_CHARS
    assert ct._CMD_REL_OPS is bash_core._CMD_REL_OPS
    assert ct._ERRORLEVEL_RE is bash_core._ERRORLEVEL_RE
    assert ct._INT_RE is bash_core._INT_RE
    assert ct._GLUED_LABEL_RE is bash_core._GLUED_LABEL_RE
    assert ct._CALL_BATCH_RE is bash_core._CALL_BATCH_RE
    assert ct._FOR_LOOP_TOKEN_RE is bash_core._FOR_LOOP_TOKEN_RE


def test_validate_cmd_if_for_list_and_body_delegates() -> None:
    src = r"for %f in (D:\a,x) do echo %f"
    sc = LiveScanner(src)
    m = ct.validate_cmd_for(sc, src, 0, 3, len(src))
    assert m is not None
    commas, paths = m.comma_spans, m.list_path_edits
    sc2 = LiveScanner(src)
    got = sc2._match_cmd_for_list(m.open_pos + 1, m.close_pos)
    assert got == (commas, paths)
    ve = sc2._match_cmd_for_body(m.do_end, m.body_end, m.name)
    assert ve == m.var_edits


# ---------------------------------------------------------------------------
# 8. full-BashFix differential vs the pristine (HEAD) scanner
# ---------------------------------------------------------------------------

EXTRACTED = [
    # -- at-prefix suite
    "@echo off", "@echo on", "@echo off >nul", "@echo off>nul",
    "@echo off; dir", "@echo off && dir", "@echo off\nfoo", "@echo off | cat",
    "@echo offline", "@echo off & dir", "@dir x", "@type foo.txt",
    "@ping 1.1.1.1", r"@if exist P (A) else (B)", "@for %f in (a) do echo %f",
    "@rev", r"@if exist D:\a (dir D:\a 2>nul)",
    "@", "@@x", '"@"dir', "'@'dir", "@echo.", "echo off", "echo on", "x @ y",
    "echo a @ b", "$@", 'echo "${arr[@]}"', "arr=(@foo)", "git add @file",
    "cd @dir", "; @dir", "&& @dir",
    # -- conditional suite (representative, incl. REPRODUCED shape)
    "if exist x (echo a) else (echo b)", "if exist x (echo a)",
    "if not exist x (echo a) else (echo b)",
    "if exist A (echo 1) else if exist B (echo 2) else (echo 3)",
    "if exist A (echo 1) else if exist B (echo 2)",
    "if exist A (echo 1) else if not exist B (echo 2)",
    "if exist x (a) && echo t", "if exist x (a); echo t", "if exist x (a)|grep b",
    "time if exist x (a)", "! if exist x (a)", "echo hi; if exist x (a) else (b)",
    "if exist A (if exist B (echo x) else (echo y))",
    r"if exist P (dir P\sub) else (echo no P)", 'if exist "My Data" (echo ok)',
    "x=$(if exist P (a) else (b))", r"if exist x (cd C:\y)",
    "if exist x (a) else (b)\n", "if  exist  x  ( a )  ",
    "if exist LuisaCompute (dir LuisaCompute | more +5) "
    "else (echo no LuisaCompute dir) && echo "
    '"---"'
    " && "
    r"if exist D:\LuisaCompute "
    r"(dir D:\LuisaCompute\include\luisa\backends\ext 2>nul | findstr vk) "
    r"else (echo no D:\LuisaCompute)",
    'eval "if exist x (a) else (b)"', "if exist a (x) && if not exist b (y)",
    "if exist; then echo t; fi", "if exist foo; then bar; fi",
    "if [ -e x ]; then echo t; fi", "if [[ -e x ]]; then echo t; fi",
    "if test -e x; then echo t; fi", 'echo "if exist x (y)"',
    "echo 'if exist x (y)'", "if exist x echo yes", "if exist x (a) else b",
    "if exist x\n(a)", "if exist x\n(a)\nelse\n(b)", "if exist x (a)\nelse (b)",
    "if exist x (a) else\n(b)", "if exist (a)", "if exist $(f) (a)",
    "if exists x (a)", "if EXIST x (a)", "if not x exist (a)",
    "if exist x (a)rev", 'if exist x (a)"q"', "if exist x ()",
    "if exist x (  ) else (b)", "if exist x (a) else ()",
    "if exist x (echo #c)", "if exist x (a)#c", "if exist x #c",
    r"if exist *.txt (echo a)", 'if exist "D:\\My Dir" (echo a) else (echo b)',
    r"if not exist 'C:\Program Files' (echo a)", "if exist x (", "if exist x (a",
    "if exist x (a) else (", "if exist x (a) else",
    r"if exist x D:\y (a)", r"if exist D:\a\* (echo a)",
    "if exist somefile; then echo y; fi",
    # -- if variants
    "if defined V (echo set)", "if defined V (echo set) else (echo unset)",
    "if not defined PYTHONHOME (echo unset)",
    "if defined A (x) else if defined B (y)",
    "if errorlevel 1 (echo failed) else (echo ok)",
    "if errorlevel 1 (echo failed)", "if not errorlevel 2 (echo fine) else (echo bad)",
    "false && if errorlevel 1 (echo failed) else (echo ok)",
    'if "%V%"=="x" (echo a) else (echo b)',
    'if "%BUILD%"=="release" (echo rel) else (echo dbg)', "if X==Y (a)",
    "if %errorlevel%==0 (echo ok)", 'if not "%V%"=="x" (a)',
    'if /i "%V%"=="xy" (echo eq) else (echo ne)', 'if /i not "a"=="b" (a)',
    "if 5 geq 3 (echo big) else (echo small)", "if %errorlevel% neq 0 (echo bad)",
    "if 1 equ 1 (echo same)", "if 2 lss 3 (a) else (b)", "if 2 leq 3 (a)",
    "if 4 gtr 3 (a)", "if not 5 lss 3 (a)", "if %V% geq 3 (a)",
    "if defined OUT (mkdir OUT) else if errorlevel 1 (echo failed)",
    "if exist A (x) else if defined B (y) else if errorlevel 1 (z) else (w)",
    r"if defined V (dir D:\a 2>nul) else (if exist C:\p (echo y))",
    "if not defined PYTHONHOME (cat *.tmp)",
    "if defined %V% (x)", "if defined 1 (x)", "if defined a-b (x)",
    "if errorlevel abc (x)", "if errorlevel 1.5 (x)", 'if a=="a" (x)',
    'if "a"==b (x)', 'if "a" == "b" (x)', "if !V!==1 (x)", 'if "%V%"=="a*b" (x)',
    'if "%V%"=="a$b" (x)', 'if %V%=="a b" (x)', "if 5 GEQ 3 (x)",
    "if not X geq Y (x)", "if 5 geq (x)", "if X==Y geq 3 (x)",
    "if /i exist x (a)", "if /i defined V (a)", "if /i errorlevel 1 (a)",
    "IF DEFINED X (x)", "if Not defined x (x)",
    "if defined V (a)\nelse (b)", "if defined V\n(a)", "if defined V ()",
    "if defined V (a)#c", "if defined V (a)rev", "if defined; then :; fi",
    "if [ -v VAR ]; then :; fi", "if errorlevel; then :; fi",
    'echo "if defined V (x)"', "x=if; echo $x",
    r"if defined V (dir D:\a 2>nul) else (echo no)",
    "if defined ZZ (cd sub) && pwd",
    # -- for suite
    "for %f in (a b) do echo %f", "for %%f in (a b) do echo %%f",
    "for %%f in (a,b,c) do echo %%f", "for %f in (*.txt *.md) do echo %f",
    "for %f in (a) do echo %f %f", "for %i in (a) do %i",
    "for %%i in (a) do echo %%i && echo x", "for %%i in (a) do echo %%i | grep x",
    "for %f in (a) do echo %f\nnext", "x=$(for %f in (a b) do echo %f)",
    "bash -c 'for %f in (a) do echo %f'",
    r"for %f in (D:\a D:\b\c.txt) do type %f", "for %f in (a) do del %f 2>nul",
    "for %f in (a b) do if exist %f (echo f) else (echo n)",
    "for %i in (a) do if exist %i (x) else (y)",
    "for %f in (a) do if exist %f (del %f 2>nul) else (echo no)",
    "for %%d in (a,b , c) do echo %%d", "for %%f in (a) do echo %%f",
    "for /l %i in (1,1,10) do echo %i", "for /d %i in (a) do echo %i",
    "for /r %i in (a) do echo %i", "for /f %i in (a) do echo %i",
    "for %f in (a) do echo %~nf", "for %f in (a) do echo %~dpf",
    "for %d in (1 2 3) do echo file_%d", "for %f in (a) do echo %OTHER%",
    "for %f in (%OS%) do echo %f", "for %f in ($HOME) do echo %f",
    "for %f in (a) do echo %f ^ tail", "for %f in (!x) do echo %f",
    'for %f in ("a b") do echo %f', 'for %f in (a) do echo "%f"',
    "for %f in (a) do echo %f & echo x", "for %1 in (a) do echo %1",
    "for %a b in (c) do echo x", "for %f in a b do echo %f", "for %f in (a) do",
    "for %f in (a) do %f; # c", "for %f in (a) doe echo %f",
    "for %f in (a) do (echo %f)", "for %f in (a\nb) do echo %f",
    'echo "for %f in (a) do echo %f"', "x=for", "for i in a b; do echo $i; done",
    "for ((i=0;i<3;i++)); do echo $i; done",
    "for %i in (a) do for %j in (b) do echo %i %j",
    # -- refusals
    "goto :end", "goto end", "goto :eof", ":label", "echo a; goto :end",
    ":start\necho hi\ngoto :start", "call :sub", "call a.BAT", "call x.cmd",
    'call "some path\\a.bat"', "exit /b", "exit /B 2", "exit /b 0",
    r"if %errorlevel% neq 0 (exit /b 1)", "x=$(goto :end)",
    "bash -c 'echo a; exit /b 1'", "exit", "exit 3", "exit $n", ":", ": label",
    ": this is a comment", "call prog.exe", "call foo", "call", "echo goto",
    "echo :label", "rg foo :bar", "echo 'goto :x'", "for f in a; do echo $f; done",
    "set -- 1 2",
]


@pytest.mark.parametrize("src", EXTRACTED)
@pytest.mark.skipif(PRISTINE is None, reason="pristine oracle cache absent")
def test_differential_extracted(src: str) -> None:
    assert full(PRISTINE.fix_bash_command(src)) == full(live_fix(src))


@pytest.mark.skipif(PRISTINE is None, reason="pristine oracle cache absent")
def test_differential_seeded_battery() -> None:
    """Seeded battery of cmd @/if/for forms x kinds x bodies (fallbacks,
    paths, nul, nested if/for, wrappers) + near-miss bails; >= 6000 cases."""
    rng = random.Random(20251114)

    exist_ops = ["x", "P", r"C:\dir\file", r"D:\a", '"My Data"', "%V%", "f"]
    defined_ops = ["V", "PYTHONHOME", "PATH", "OUT"]
    err_ops = ["0", "1", "2", "99"]
    cmps = ['"%V%"=="x"', "X==Y", "%errorlevel%==0", 'not "%V%"=="x"',
            '/i "%V%"=="xy"', '/i "a"=="b"']
    cmp_bails = ['a=="a"', '"a"==b', "5 GEQ 3", "not X geq Y", "!V!==1"]
    ariths = ["5 geq 3", "1 equ 1", "2 lss 3", "%errorlevel% neq 0",
              "%V% geq 3", "4 gtr 3", "2 leq 3"]
    bodies = ["(echo a)", "(dir)", r"(dir D:\a 2>nul)", "(cat *.tmp)",
              "(cd sub)", '(echo "q")', "(del %f 2>nul)", "(tree)", "(rev)"]
    elses = ["", " else (echo b)", r" else (dir D:\b)",
             " else if exist z (echo z) else (echo w)"]
    tails = ["", " && echo t", "; echo t", " | cat", "\nnext", " >nul"]
    for_toks = ["%f", "%%f", "%i", "%%i"]
    lists = ["(a b)", "(a,b,c)", r"(D:\a D:\b\c.txt)", "(*.txt *.md)", "(x)"]
    bad_lists = ["($HOME)", "(%OS%)", '("a b")', "(a^b)", "()", "(a\nb)"]
    dos = ["echo %s", "type %s 2>nul", "%s", "if exist %s (echo f) else (echo n)",
           "del %s && echo x", r"dir D:\%s", "echo %s | grep x"]

    def gen_if(depth: int) -> str:
        r = rng.random()
        if r < 0.30:
            head = (f"if {rng.choice(['', 'not '])}exist "
                    f"{rng.choice(exist_ops)}")
        elif r < 0.42:
            head = (f"if {rng.choice(['', 'not '])}defined "
                    f"{rng.choice(defined_ops)}")
        elif r < 0.50:
            head = (f"if {rng.choice(['', 'not '])}errorlevel "
                    f"{rng.choice(err_ops)}")
        elif r < 0.68:
            head = "if " + rng.choice(cmps + cmp_bails)
        elif r < 0.84:
            head = f"if {rng.choice(['', 'not ', '/i '])}{rng.choice(ariths)}"
        else:
            head = rng.choice(["if exists x", "if EXIST x", "if", "if exist",
                               "if /i", "if not not exist x"])
        body = rng.choice(bodies)
        if depth < 2 and rng.random() < 0.20:
            body = f"(if exist {rng.choice(exist_ops)} (n){rng.choice(elses)})"
        elif depth < 2 and rng.random() < 0.12:
            tok = rng.choice(for_toks)
            body = f"(for {tok} in (a) do {rng.choice(dos).replace('%s', tok)})"
        return f"{head} {body}{rng.choice(elses)}{rng.choice(tails)}".strip()

    def gen_for() -> str:
        tok = rng.choice(for_toks)
        pre = rng.choice(["", "", "", "@", "for /l ", "for /f "])
        lst = rng.choice(lists + bad_lists)
        body = rng.choice(dos).replace("%s", tok)
        if rng.random() < 0.08:
            body = "(echo grp)"
        if rng.random() < 0.08:
            body = f"if exist {rng.choice(exist_ops)} (x) else (y)"
        return (f"{pre}for {tok} in {lst} do {body}").strip()

    def gen_at() -> str:
        r = rng.random()
        if r < 0.25:
            return (rng.choice(["@echo off", "@echo on", "@echo OFF",
                                "@echo offline", "@echo.", "@echo",
                                "@echo  off"])
                    + rng.choice(["", " & dir", "; dir", " >nul", "\nf", "x"]))
        if r < 0.50:
            return rng.choice(["@", "@@x", '"@"dir', "'@'dir", "@ x", "@dir x",
                               "@rev", "@type f"])
        if r < 0.72:
            return "@" + gen_if(0)
        if r < 0.85:
            return "@" + gen_for()
        return gen_if(1)

    def gen_shell() -> str:
        inner = rng.choice([gen_if(0), gen_for(), gen_at()])
        r = rng.random()
        if r < 0.20:
            q = inner.replace("'", "'\\''")
            return f"bash -c '{q}'"
        if r < 0.35:
            q = inner.replace("'", "'\\''")
            return f"eval '{q}'"
        if r < 0.50:
            return f"x=$({inner})"
        if r < 0.60:
            q = inner.replace("'", "'\\''")
            return f"sh -c '{q}'"
        if r < 0.68:
            return f"timeout 5 {inner}"
        if r < 0.74:
            return f"env A=1 {inner}"
        if r < 0.80:
            q = inner.replace("'", "")
            return f"watch '{q}'"
        if r < 0.88:
            other = rng.choice([gen_if(0), gen_for()])
            return inner + rng.choice([" && ", "; ", " || "]) + other
        return inner + " " + rng.choice(["goto :end", "exit /b", "call :s"])

    total = 0
    bad = 0
    first: list = []
    for _ in range(6600):
        r = rng.random()
        if r < 0.30:
            cmd = gen_if(rng.randint(0, 2))
        elif r < 0.55:
            cmd = gen_for()
        elif r < 0.72:
            cmd = gen_at()
        else:
            cmd = gen_shell()
        cmd = re.sub(r"[\x00-\x08\x0b-\x1f]", " ", cmd).strip()
        if not cmd:
            continue
        total += 1
        a = full(PRISTINE.fix_bash_command(cmd))
        b = full(live_fix(cmd))
        if a != b:
            bad += 1
            if len(first) < 5:
                first.append((cmd, a, b))
    assert total >= 6000, total
    assert bad == 0, first
