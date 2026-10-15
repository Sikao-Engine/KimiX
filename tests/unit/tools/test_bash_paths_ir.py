"""Phase-4 IR tests: paths.py path / nul / cd primitives over IR nodes.

The ported path/null-device/``cd /d`` rules (Phase 4) live in
``kimix_native.shell_compat.paths``.  These tests drive the IR-driven pure
functions (``path_replacement_for_word`` / ``nul_replacement_for_redirection`` /
``cd_d_flag_span`` / ``cd_flag_span_after``) and the lifted pure primitives,
cross-checked against the pristine (HEAD) scanner outcomes whenever the
differential-oracle cache artifact is present.
"""
from __future__ import annotations

import importlib.util
import random
import sys
from pathlib import Path

import pytest

# Resolve the shim package the way every sibling suite does: kimi_cli.
# native_loader puts <repo>/bin on sys.path FIRST so `kimix_native.*` below
# imports the in-repo package (a stale installed wheel must not shadow it).
from kimi_cli.native_loader import get_compat as _get_compat

assert _get_compat("_shell_compat") is not None

from kimix_native.shell_compat import bash_core
from kimix_native.shell_compat import paths as paths_mod
from kimix_native.shell_compat.models import (
    CommandNode,
    RedirectionNode,
    WordNode,
    walk_nodes,
)
from kimix_native.shell_compat.parser import parse

REPO = Path(__file__).resolve().parents[3]
PRISTINE_PATH = REPO / ".kimix_cache" / "tmp_pristine_sc.py"


def _load_pristine():
    if not PRISTINE_PATH.exists():
        return None
    spec = importlib.util.spec_from_file_location("p4_pristine_sc", str(PRISTINE_PATH))
    if spec is None or spec.loader is None:
        return None
    m = importlib.util.module_from_spec(spec)
    sys.modules["p4_pristine_sc"] = m
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


def _cmd(source, want_word=None):
    sc = parse(source)
    for n in walk_nodes(sc):
        if isinstance(n, CommandNode):
            if want_word is None:
                return n
            cw = n.command_word
            if cw is not None and cw.span.text(source) == want_word:
                return n
    raise AssertionError("no CommandNode in %r" % source)


def _first_redir(source):
    sc = parse(source)
    for n in walk_nodes(sc):
        if isinstance(n, RedirectionNode) and n.target is not None:
            return n
    raise AssertionError("no redirection target in %r" % source)


def _target_word(source):
    return _first_redir(source).target


# ---------------------------------------------------------------------------
# 1. nul matrix through the NEW IR function.
# ---------------------------------------------------------------------------

NUL_REWRITE = [
    "echo hi > nul",
    "echo hi >NUL",
    "echo hi 2> nul",
    "echo hi &> nul",
    "echo hi >> nul",
    "echo hi 2>> nul",
    "echo hi>nul",
    "echo hi > " + chr(92) + "nul",           # backslash-escaped \nul
    "echo hi > n" + chr(92) + "ul",           # n\ul
    "echo hi > nu" + chr(92) + "l",           # nu\l
    "echo hi > $'nul'",
    "echo hi > $" + chr(39) + chr(92) + "156ul" + chr(39),
    "echo hi > nul''",
    "echo hi > ''nul",
    'echo hi > n""ul',
    "echo hi > n''ul",
    "echo hi >| nul",
    "echo hi <> nul",
    "echo hi >& nul",
    "echo hi>&nul",
    "echo hi 2>& nul",
    "echo hi 3> nul",
    "echo hi 10> nul",
    "echo hi >Nul",
    "echo hi > $'NuL'",
    "echo hi >nul",
    "echo hi >nul 2>&1",
    "cmd > NUL 2>&1",
    "echo a > nul; echo b > NUL",
]

NUL_PRESERVE = [
    "echo hi > 'nul'",        # whole-token quoted: intentional filename
    'echo hi > "nul"',
    "echo hi < nul",          # pure input redirection
    "echo hi 2< nul",
    "echo hi <<< nul",        # here-string is data
    "echo hi <& nul",         # input dup
    "cat << nul\nnul\n",      # heredoc delimiter
    "echo hi > null",         # different value
    "echo hi > nul.txt",
    "echo hi > nul2",
    "echo hi > $(echo nul)",  # runtime value
    "echo hi > `nul`",
    "echo hi > ${x}",
    "echo hi > $'nul'x",
    "echo hi 2>&1",           # fd dup to fd
    "echo hi >&2",            # fd dup to fd
    "echo hi >& nul.txt",     # write-capable but target not nul
]


@pytest.mark.parametrize("source", NUL_REWRITE)
def test_nul_ir_rewrites_output_targets(source):
    redir = _first_redir(source)
    assert paths_mod.nul_replacement_for_redirection(redir, source) == "/dev/null"


@pytest.mark.parametrize("source", NUL_PRESERVE)
def test_nul_ir_preserves_other_targets(source):
    sc = parse(source)
    decisions = [
        paths_mod.nul_replacement_for_redirection(node, source)
        for node in walk_nodes(sc)
        if isinstance(node, RedirectionNode) and node.target is not None
    ]
    assert all(d is None for d in decisions), (source, decisions)


@pytestmark_oracle
@pytest.mark.parametrize("source", NUL_REWRITE + NUL_PRESERVE)
def test_nul_ir_matches_legacy_scanner(source):
    expected = PRISTINE.fix_bash_command(source)
    sc = parse(source)
    n_ir = sum(
        paths_mod.nul_replacement_for_redirection(node, source) == "/dev/null"
        for node in walk_nodes(sc)
        if isinstance(node, RedirectionNode) and node.target is not None
    )
    assert n_ir == len(expected.nul_fixes), source
    assert full(bash_core.fix_bash_command(source)) == full(expected), source


@pytestmark_oracle
def test_nul_warning_and_fixes_order_match_baseline():
    for src in [
        "echo a > nul; echo b > NUL; echo c >nul",
        "echo hi > nul 2>&1",
        r"cd /d D:\x 2>nul >nul",
    ]:
        assert full(bash_core.fix_bash_command(src)) == full(PRISTINE.fix_bash_command(src)), src


# ---------------------------------------------------------------------------
# 2. path classes through the pure primitives + IR wrapper.
# ---------------------------------------------------------------------------

BS = chr(92)  # single backslash, avoids escape-sequence hazards in source


def test_path_ir_drive_unc_dotrelative():
    assert paths_mod.windows_path_replacement("D:" + BS + "repo" + BS + "src") == "D:/repo/src"
    assert paths_mod.windows_path_replacement(BS + BS + "server" + BS + "share") == "//server/share"
    assert paths_mod.windows_path_replacement("." + BS + "build" + BS + "dist") == "./build/dist"
    assert paths_mod.windows_path_replacement(".." + BS + "src" + BS + "lib") == "../src/lib"
    assert paths_mod.windows_path_replacement("~" + BS + "Desktop" + BS + "file.txt") == "~/Desktop/file.txt"
    assert paths_mod.windows_path_replacement("C:" + BS + BS) == "C:/"


def test_path_ir_ambiguous_untouched():
    assert paths_mod.windows_path_replacement("foo" + BS + "bar") is None
    assert paths_mod.windows_path_replacement("a" + BS + "nb") is None
    assert paths_mod.windows_path_replacement(BS + "a" + BS + "b") is None
    assert paths_mod.windows_path_replacement("x" + BS + "n" + BS + "t") is None
    assert paths_mod.windows_path_replacement(BS + "033" + BS + "015") is None


def test_path_ir_quoted_data_untouched():
    assert paths_mod.windows_path_replacement("'" + "D:" + BS + "x" + "'") is None
    assert paths_mod.windows_path_replacement('"D:' + BS + 'x"') is None
    assert paths_mod.windows_path_replacement("$'nul'") is None


def test_path_ir_temp_dir_patched_and_unpatched():
    real = bash_core._windows_temp_dir()
    assert paths_mod.git_bash_abs_path_replacement("/tmp/x") == real + "/x"
    assert paths_mod.git_bash_abs_path_replacement("/tmp/x", temp_dir="C:/T D") == '"C:/T D/x"'
    assert paths_mod.git_bash_abs_path_replacement("/tmp") == real
    assert paths_mod.git_bash_abs_path_replacement("/tmpfile") is None
    assert paths_mod.git_bash_abs_path_replacement("/c") is None
    assert paths_mod.git_bash_abs_path_replacement("/d") is None


def test_path_ir_gitbash_drive_mounts():
    assert paths_mod.git_bash_abs_path_replacement("/c/x") == "C:/x"
    assert paths_mod.git_bash_abs_path_replacement("/D/foo") == "D:/foo"


def test_path_ir_spaces_are_quoted():
    assert paths_mod.windows_path_replacement(BS + "Program" + BS + " Files" + BS + "x") == '"/Program Files/x"'
    assert paths_mod.windows_path_replacement("C:" + BS + "a b" + BS + "x") == '"C:/a b/x"'


def test_path_ir_globs_stay_outside_quotes():
    assert paths_mod.windows_path_replacement("D:" + BS + "x" + BS + "*.txt") == "D:/x/*.txt"
    assert paths_mod.windows_path_replacement("D:" + BS + "a b" + BS + "*.txt") == '"D:/a b/"*.txt'


def test_path_ir_braced_segments_preserved():
    real = bash_core._windows_temp_dir()
    assert paths_mod.git_bash_abs_path_replacement("/tmp/{a,b}") == real + "/{a,b}"


def test_path_ir_runtime_expansions_preserved_verbatim():
    real = bash_core._windows_temp_dir()
    assert paths_mod.git_bash_abs_path_replacement("/tmp/$var/x") == real + "/$var/x"
    assert paths_mod.git_bash_abs_path_replacement("/tmp/${x}") == real + "/${x}"
    assert paths_mod.git_bash_abs_path_replacement("/tmp/x$'t'y") == real + "/x$'t'y"


def test_path_ir_command_substitution_left_untouched():
    assert paths_mod.git_bash_abs_path_replacement("/tmp/x$(y)") is None
    assert paths_mod.git_bash_abs_path_replacement("/tmp/x`y`") is None


def test_path_ir_ansi_c_escapes_in_target():
    real = bash_core._windows_temp_dir()
    assert paths_mod.git_bash_abs_path_replacement("/tmp/a" + BS + " b") == '"%s/a b"' % real


def test_path_for_word_ir_wrapper_uses_target_span():
    source = "echo hi > D:" + BS + "repo" + BS + "src"
    word = _target_word(source)
    assert paths_mod.path_replacement_for_word(word, source) == "D:/repo/src"
    word2 = _target_word("cat > /tmp/out.txt")
    assert paths_mod.path_replacement_for_word(word2, source="cat > /tmp/out.txt") == bash_core._windows_temp_dir() + "/out.txt"


def test_path_for_word_respects_explicit_temp_dir():
    source = "echo /tmp/x"
    sc = parse(source)
    word = next(
        n for n in walk_nodes(sc)
        if isinstance(n, WordNode) and n.span.text(source) == "/tmp/x"
    )
    assert paths_mod.path_replacement_for_word(word, source, temp_dir="C:/My Temp") == '"C:/My Temp/x"'


def test_path_ir_escaped_glob_untouched():
    # an escaped glob metacharacter in the literal span makes the whole word
    # return None (re-emitting it unquoted would re-enable pathname expansion
    # the escape suppressed).
    assert paths_mod.git_bash_abs_path_replacement("/tmp/a" + BS + "*b") is None
    assert paths_mod.git_bash_abs_path_replacement("/tmp/a" + BS + "{b") is None


def test_path_ir_escaped_backslash_value_decoded_then_quoted():
    # /tmp/a\b  (raw: one literal backslash escape pair) decodes to a\b then
    # gets quoted; it must match the legacy scanner output exactly.
    expected = bash_core._BashFixScanner("/tmp/a" + BS + "\\\\b")._git_bash_abs_path_replacement(
        "/tmp/a" + BS + "\\\\b"
    )
    assert paths_mod.git_bash_abs_path_replacement("/tmp/a" + BS + "\\\\b") == expected


# ---------------------------------------------------------------------------
# 3. cd /d cases.
# ---------------------------------------------------------------------------

CD_DROP = [
    "cd /d D:" + BS + "x",
    "cd /D C:" + BS + "Users" + BS + "me",
    "cd /d 'D:" + BS + "x'",
    "cd /d /d/foo",
    "cd  /d  y",
    "cd /d" + chr(9) + "x",
    "cd /d " + BS + "d",
]

CD_NO_DROP = [
    "cd /d",
    "cd /d && echo x",
    "cd /d # comment",
    "cd /d ; x",
    "cd /dd x",
    "cd /dx",
    "cd x /d y",
    "cd /d 2>/dev/null",
    "cd /d\nfoo",
]


@pytest.mark.parametrize("source", CD_DROP)
def test_cd_d_flag_dropped(source):
    cmd = _cmd(source, "cd")
    span = paths_mod.cd_d_flag_span(cmd, source)
    assert span is not None, source
    assert source[span.start:span.end] in {"/d", "/D"}, source


@pytest.mark.parametrize("source", CD_NO_DROP)
def test_cd_d_flag_not_dropped(source):
    sc = parse(source)
    irdrop = any(
        paths_mod.cd_d_flag_span(n, source) is not None
        for n in walk_nodes(sc)
        if isinstance(n, CommandNode)
        and n.command_word is not None
        and n.command_word.span.text(source) == "cd"
    )
    assert irdrop is False, source


@pytestmark_oracle
@pytest.mark.parametrize(
    "source",
    CD_DROP + CD_NO_DROP + [
        "cd /d 10",
        "cd /d D:" + BS + "x 2>nul",
        "cd /d D:" + BS + "x && cd /d D:" + BS + "y",
    ],
)
def test_cd_d_ir_matches_legacy_drop_decision(source):
    sc = parse(source)
    irdrop = any(
        paths_mod.cd_d_flag_span(n, source) is not None
        for n in walk_nodes(sc)
        if isinstance(n, CommandNode)
        and n.command_word is not None
        and n.command_word.span.text(source) == "cd"
    )
    legacydrop = "cd /d" in PRISTINE.fix_bash_command(source).path_changes
    assert irdrop == legacydrop, source
    assert full(bash_core.fix_bash_command(source)) == full(PRISTINE.fix_bash_command(source))


# ---------------------------------------------------------------------------
# 4. edit-span disjointness for compound commands (several path + nul edits).
# ---------------------------------------------------------------------------

def _all_edit_spans(source):
    spans = []
    sc = parse(source)
    for n in walk_nodes(sc):
        if isinstance(n, RedirectionNode) and n.target is not None:
            nul = paths_mod.nul_replacement_for_redirection(n, source)
            if nul is not None:
                spans.append((n.target.span.start, n.target.span.end, nul))
            else:
                opcore = n.operator_text.lstrip("0123456789")
                if opcore != "<<<" and opcore not in {"<<", "<<-"}:
                    rep = paths_mod.path_replacement_for_word(n.target, source)
                    if rep is not None:
                        spans.append((n.target.span.start, n.target.span.end, rep))
        if isinstance(n, CommandNode) and n.command_word is not None:
            cw = n.command_word.span.text(source)
            if cw == "cd":
                sp = paths_mod.cd_d_flag_span(n, source)
                if sp is not None:
                    spans.append((sp.start, sp.end, ""))
            for arg in n.arguments:
                rep = paths_mod.path_replacement_for_word(arg, source)
                if rep is not None:
                    spans.append((arg.span.start, arg.span.end, rep))
    return spans


COMPOUNDS = [
    "cd /d D:" + BS + "x && echo hi > nul; cat /tmp/a | rev > nul",
    "cp D:" + BS + "a" + BS + "b.txt E:" + BS + "dest" + BS + " > nul 2>&1",
    "echo /tmp/x > D:" + BS + "out.txt; cd /d C:" + BS + "y && printf hi >> nul",
    "rm -f /tmp/a /tmp/b 2> nul; ls D:" + BS + "x" + BS + "*.txt > nul",
]


@pytest.mark.parametrize("source", COMPOUNDS)
def test_edit_spans_sorted_and_pairwise_disjoint(source):
    spans = _all_edit_spans(source)
    assert spans, source
    ordered = sorted(spans)
    prev_end = -1
    for start, end, _rep in ordered:
        assert start <= end
        assert start >= prev_end, (source, spans)
        prev_end = end


@pytestmark_oracle
@pytest.mark.parametrize("source", COMPOUNDS)
def test_compound_commands_match_baseline(source):
    assert full(bash_core.fix_bash_command(source)) == full(PRISTINE.fix_bash_command(source))


# ---------------------------------------------------------------------------
# 5. seeded differential: fix_bash_command over a generated path/nul/cd battery.
# ---------------------------------------------------------------------------

PATH_FRAGS = [
    "nul", "NUL", "nul.txt", "/dev/null", "out.txt", "/tmp", "/tmp/x",
    "/c/x", "/d/foo", "C:" + BS + "a", "D:" + BS + "repo" + BS + "src",
    BS + BS + "server" + BS + "share", BS + "Users" + BS + "foo",
    "~" + BS + "Desktop" + BS + "file.txt", "." + BS + "build", "foo" + BS + "bar",
    "a" + BS + "nb", "D:" + BS + "*" + ".txt", "C:" + BS + "with space",
    "$'nul'", "nul''", "''nul", "n" + BS + "ul", "$(echo nul)", "${x}",
    "/tmp/$var/x", "D:" + BS + "x" + BS + "*.txt", "/tmp/{a,b}",
]
OPS = [">", ">>", "2>", "&>", "<>", ">|", "<", "<<<", "<&", ">&", "2>&"]
CMDS = ["echo hi", "tree", "cat", "rev", "cd", "printf x"]


@pytestmark_oracle
def test_seeded_generated_battery_zero_mismatches():
    rng = random.Random(0xC0FFEE ^ 0x9959F20E)
    cases = list(PATH_FRAGS)
    for _ in range(6000):
        op = rng.choice(OPS)
        frag = rng.choice(PATH_FRAGS)
        cmd = rng.choice(CMDS)
        sep = rng.choice(["", " "])
        cases.append("%s %s%s%s" % (cmd, op, sep, frag))
        cases.append("cd /d %s %s %s" % (frag, op, frag))
        k = rng.randint(1, 4)
        cases.append(" ".join(rng.choice(PATH_FRAGS + OPS + ["&&", ";", "|"]) for _ in range(k)))
    mism = 0
    for src in dict.fromkeys(cases):
        a = full(PRISTINE.fix_bash_command(src))
        b = full(bash_core.fix_bash_command(src))
        if a != b:
            mism += 1
    assert mism == 0, "%d mismatches" % mism


# ---------------------------------------------------------------------------
# 6. monkeypatch contract still routes through the facade for IR path calls.
# ---------------------------------------------------------------------------

def test_windows_temp_dir_monkeypatch_contract(monkeypatch):
    import kimix_native._shell_compat as facade

    original = facade._windows_temp_dir
    assert bash_core._windows_temp_dir is original
    monkeypatch.setattr(facade, "_windows_temp_dir", lambda: "C:/S P/T")
    assert paths_mod.git_bash_abs_path_replacement("/tmp/x") == '"C:/S P/T/x"'
    assert paths_mod.path_replacement("/tmp/x") == '"C:/S P/T/x"'
    monkeypatch.setattr(facade, "_windows_temp_dir", original)
    assert paths_mod.git_bash_abs_path_replacement("/tmp/x") == bash_core._windows_temp_dir() + "/x"


# ---------------------------------------------------------------------------
# 7. facade re-exports / __all__ stability for the ported names.
# ---------------------------------------------------------------------------

def test_paths_public_surface():
    assert bash_core._decode_virtual_path_span("a" + BS + " b") == "a b"
    assert bash_core._split_virtual_path_expansions("x$y") == [(False, "x"), (True, "$y")]
    for name in [
        "windows_path_replacement", "git_bash_abs_path_replacement",
        "quote_virtual_path", "path_replacement", "decode_unquoted_word",
        "normalize_windows_path", "quote_path_word", "plausible_path_segments",
        "decode_virtual_path_span", "split_virtual_path_expansions",
        "is_effective_nul_word", "path_replacement_for_word",
        "nul_replacement_for_redirection", "cd_d_flag_span", "cd_flag_span_after",
    ]:
        assert hasattr(paths_mod, name), name


def test_paths_delegate_equals_live_scanner_methods():
    sc = bash_core._BashFixScanner("echo D:" + BS + "a" + BS + "b")
    assert sc._windows_path_replacement("D:" + BS + "a" + BS + "b") == paths_mod.windows_path_replacement("D:" + BS + "a" + BS + "b")
    assert sc._path_replacement("/tmp/x") == paths_mod.path_replacement("/tmp/x")
    assert sc._quote_path_word("C:" + BS + "a b") == paths_mod.quote_path_word("C:" + BS + "a b")
    assert sc._normalize_windows_path("C:" + BS + "a" + BS + "b") == paths_mod.normalize_windows_path("C:" + BS + "a" + BS + "b")
    assert sc._decode_unquoted_word("a" + BS + " b") == paths_mod.decode_unquoted_word("a" + BS + " b")
    assert sc._plausible_path_segments("Users" + BS + "x") == paths_mod.plausible_path_segments("Users" + BS + "x")
    assert sc._is_effective_nul_target("nul") is True
    assert sc._is_effective_nul_target("'nul'") is False
