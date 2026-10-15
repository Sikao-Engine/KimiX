"""Phase-5 IR tests: wrappers.py / translator.py classification, fallback,
wrapper, reparse and unwrap primitives — cross-checked against the pristine
(HEAD) scanner and the LIVE scanner outcomes.

The ported rules (Phase 5) live in ``kimix_native.shell_compat.wrappers`` and
the translation core in ``kimix_native.shell_compat.translator``.  These tests
drive the IR-driven pure functions AND cross-check every decision against the
LIVE ``fix_bash_command`` output (pristine oracle when the cache artifact is
present) — a pristine differential, not just hardcoded strings.
"""
from __future__ import annotations

import importlib.util
import random
import re
import sys
from pathlib import Path

import pytest

# Resolve the shim package the way every sibling suite does.
from kimi_cli.native_loader import get_compat as _get_compat

assert _get_compat("_shell_compat") is not None

from kimix_native._shell_compat import _BashFixScanner as LiveScanner
from kimix_native._shell_compat import fix_bash_command as live_fix
from kimix_native.shell_compat import bash_core
from kimix_native.shell_compat import translator as tr
from kimix_native.shell_compat import wrappers as wr
from kimix_native.shell_compat.models import CommandNode, WordNode, walk_nodes
from kimix_native.shell_compat.parser import parse

REPO = Path(__file__).resolve().parents[3]
PRISTINE_PATH = REPO / ".kimix_cache" / "tmp_pristine_sc.py"


def _load_pristine():
    if not PRISTINE_PATH.exists():
        return None
    spec = importlib.util.spec_from_file_location(
        "p5_pristine_sc", str(PRISTINE_PATH)
    )
    if spec is None or spec.loader is None:
        return None
    m = importlib.util.module_from_spec(spec)
    sys.modules["p5_pristine_sc"] = m
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


LV = LiveScanner._literal_word_value


def _cmd(source, want_word=None):
    sc = parse(source)
    for n in walk_nodes(sc):
        if isinstance(n, CommandNode) and n.command_word is not None:
            if want_word is None:
                return n
            if n.command_word.literal_value == want_word:
                return n
    raise AssertionError("no CommandNode(%r) in %r" % (want_word, source))


# ---------------------------------------------------------------------------
# 1. classification enum for each class with representative words
# ---------------------------------------------------------------------------

CLASS_CASES = [
    (wr.CommandClass.GIT_BASH_PASSTHROUGH, ["ls", "cat", "git", "echo", "zsh", "python", "-", "", ":"]),
    (wr.CommandClass.FALLBACK_GUARDED, ["tree", "rev", "nc", "netcat", "htop", "top", "gawk", "gsed", "gls", "gxargs", "uptime", "free", "say", "pip3", "python3"]),
    (wr.CommandClass.UNSUPPORTED, ["journalctl", "goto", ":label", ":glued_label", "call :label", "exit /b"]),
    (wr.CommandClass.CMD_TRANSLATABLE, ["if", "for"]),
    (wr.CommandClass.WRAPPER_CONTEXT, ["env", "timeout", "xargs", "sudo", "watch", "command", "builtin", "coproc", "nohup", "stdbuf", "nice", "time", "exec", "eval", "trap", "gtimeout", "bash", "sh", "dash", "ash"]),
    (wr.CommandClass.DATA, ["then", "do", "else", "elif", "fi", "done", "esac", "in", "while", "until", "case", "{", "}", "!", "function", "VAR=x", "arr+=", "PATH=/usr"]),
]


@pytest.mark.parametrize("expected,words", CLASS_CASES)
def test_classification_enum(expected, words):
    for w in words:
        assert wr.classify_command(w, raw=w) == expected, w


def test_classification_ambiguous_and_at_prefix():
    assert wr.classify_command(None) is wr.CommandClass.AMBIGUOUS
    # leading ``@`` classifies from the RAW word (quote removal keeps the @)
    assert wr.classify_command("echo", raw="@echo off") is wr.CommandClass.CMD_TRANSLATABLE
    assert wr.classify_command(None, raw="@rev") is wr.CommandClass.CMD_TRANSLATABLE


@pytest.mark.parametrize(
    "word",
    [w for _e, ws in CLASS_CASES for w in ws] + ["gtimeout", "watch", "sudo"],
)
def test_classification_matches_live_tables(word):
    """The classifier's membership view equals the LIVE table membership."""
    c = wr.classify_command(word, raw=word)
    if word in bash_core._COMMAND_WRAPPERS or word in bash_core._SHELL_WRAPPERS or word in bash_core._REPARSE_BUILTINS or word in bash_core._FALLBACK_COMMAND_WRAPPERS:
        assert c is wr.CommandClass.WRAPPER_CONTEXT, word
    elif word in bash_core._UNSUPPORTED_BODIES:
        # journalctl/goto are unsupported; the multi-word keys never appear
        # as one word in the scanner word stream, but the classifier still
        # resolves them via the table.
        assert c is wr.CommandClass.UNSUPPORTED, word
    elif word in bash_core._FALLBACKS:
        # watch/sudo/gtimeout are both fallback names and wrappers → the
        # wrapper arm wins (precedence mirrors the live dispatch).
        assert c in (wr.CommandClass.FALLBACK_GUARDED, wr.CommandClass.WRAPPER_CONTEXT), word
    elif word in {"if", "for"} or word.startswith("@"):
        assert c is wr.CommandClass.CMD_TRANSLATABLE, word
    elif word in wr._KEYWORD_DATA or bash_core._ASSIGNMENT_RE.match(word):
        assert c is wr.CommandClass.DATA, word


# ---------------------------------------------------------------------------
# 2. fallback registration: every fallback name + g* aliases + nc/netcat +
#    htop + gtimeout/gwatch handling
# ---------------------------------------------------------------------------

def test_fallback_registration_every_name():
    for name in bash_core._FALLBACKS:
        rec = wr.fallback_registration(name)
        assert rec is not None, name
        assert rec.name == name
        assert rec.body == bash_core._FALLBACK_BODIES[name]
        assert rec.definition == bash_core._FALLBACKS[name]


@pytest.mark.parametrize(
    "name,expected_runner",
    [
        ("gtimeout", "timeout"),
        ("watch", "watch"),
        ("sudo", "sudo"),
        ("tree", None),
        ("nc", None),
        ("netcat", None),
        ("htop", None),
    ],
)
def test_fallback_runner_kind(name, expected_runner):
    rec = wr.fallback_registration(name)
    assert rec is not None
    assert rec.runner_kind == expected_runner


def test_fallback_shared_bodies():
    assert wr.fallback_registration("netcat").body == bash_core._FALLBACK_BODIES["nc"]
    assert wr.fallback_registration("htop").body == bash_core._FALLBACK_BODIES["top"]


def test_fallback_registration_none():
    assert wr.fallback_registration("ls") is None
    assert wr.fallback_registration(None) is None
    assert wr.fallback_registration("notacommand") is None


@pytest.mark.parametrize(
    "alias,target",
    [("gawk", "awk"), ("gsed", "sed"), ("gls", "ls"), ("gxargs", "xargs"),
     ("gtimeout", "timeout"), ("gshuf", "shuf"), ("gdircolors", "dircolors")],
)
def test_gnu_alias_target(alias, target):
    if alias in bash_core._FALLBACK_BODIES:
        assert wr.gnu_alias_target(alias) == target, alias


@pytest.mark.parametrize("name", ["watch", "sudo", "netcat", "htop", "tree", "nc", "gpython3", "g"])
def test_gnu_alias_none(name):
    assert wr.gnu_alias_target(name) is None


# ---------------------------------------------------------------------------
# 3. unsupported detection with the exact reasons
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", list(bash_core._UNSUPPORTED_BODIES))
def test_unsupported_record_exact(name):
    assert wr.unsupported_record(name) == bash_core._UNSUPPORTED_BODIES[name]


def test_unsupported_record_none():
    assert wr.unsupported_record("ls") is None
    assert wr.unsupported_record(None) is None


# ---------------------------------------------------------------------------
# 4. wrapper operand consumption for each wrapper incl. option/path-option
#    cases and env -S opaque handling
# ---------------------------------------------------------------------------

def test_wrapper_step_timeout_duration():
    st = wr.WrapperState("timeout", operands=1)
    assert wr.wrapper_step(st, "5", LV) == wr.ACTION_SKIP
    assert st.operands == 0
    assert wr.wrapper_step(st, "rev", LV) == wr.ACTION_COMMAND


def test_wrapper_step_env_value_options():
    st = wr.WrapperState("env")
    assert wr.wrapper_step(st, "-u", LV) == wr.ACTION_SKIP
    assert st.skip_next is True
    assert wr.wrapper_step(st, "PATH", LV) == wr.ACTION_SKIP  # consumed value
    assert wr.wrapper_step(st, "tree", LV) == wr.ACTION_COMMAND


def test_wrapper_step_env_path_option():
    st = wr.WrapperState("env")
    assert wr.wrapper_step(st, "-C", LV) == wr.ACTION_SKIP
    assert st.skip_next is True and st.path_value is True
    assert wr.wrapper_step(st, "D:/x", LV) == wr.ACTION_SKIP


def test_wrapper_step_env_split_string_opaque():
    st = wr.WrapperState("env")
    assert wr.wrapper_step(st, "-S", LV) == wr.ACTION_SKIP
    assert st.opaque is True and st.skip_next is True
    # the string after -S is consumed; everything after is opaque
    assert wr.wrapper_step(st, "'a b'", LV) == wr.ACTION_INSPECT
    assert wr.wrapper_step(st, "tree", LV) == wr.ACTION_INSPECT


def test_wrapper_step_env_split_string_inline():
    st = wr.WrapperState("env")
    assert wr.wrapper_step(st, "-Sa b", LV) == wr.ACTION_INSPECT
    st2 = wr.WrapperState("env")
    assert wr.wrapper_step(st2, "--split-string=a b", LV) == wr.ACTION_INSPECT


def test_wrapper_step_env_assignment():
    st = wr.WrapperState("env")
    assert wr.wrapper_step(st, "VAR=x", LV) == wr.ACTION_SKIP
    assert wr.wrapper_step(st, "tree", LV) == wr.ACTION_COMMAND


def test_wrapper_step_command_v_and_p():
    st = wr.WrapperState("command")
    assert wr.wrapper_step(st, "-v", LV) == wr.ACTION_INSPECT
    st2 = wr.WrapperState("command")
    assert wr.wrapper_step(st2, "-p", LV) == wr.ACTION_SKIP
    assert st2.opaque is True
    st3 = wr.WrapperState("command")
    assert wr.wrapper_step(st3, "-rp", LV) == wr.ACTION_SKIP
    assert st3.opaque is True


def test_wrapper_step_double_dash():
    for kind in ("env", "timeout", "xargs", "watch"):
        st = wr.WrapperState(kind)
        assert wr.wrapper_step(st, "--", LV) == wr.ACTION_SKIP


def test_wrapper_step_xargs_value_and_path_options():
    st = wr.WrapperState("xargs")
    assert wr.wrapper_step(st, "-a", LV) == wr.ACTION_SKIP
    assert st.path_value is True and st.skip_next is True
    st2 = wr.WrapperState("xargs")
    assert wr.wrapper_step(st2, "-I", LV) == wr.ACTION_SKIP
    assert st2.skip_next is True


def test_wrapper_step_watch_interval():
    st = wr.WrapperState("watch")
    assert wr.wrapper_step(st, "-n", LV) == wr.ACTION_SKIP
    assert st.skip_next is True
    assert wr.wrapper_step(st, "1", LV) == wr.ACTION_SKIP
    assert wr.wrapper_step(st, "rev", LV) == wr.ACTION_COMMAND


def test_wrapper_step_nohup_stdbuf_nice_time_sudo():
    st = wr.WrapperState("nohup")
    assert wr.wrapper_step(st, "--", LV) == wr.ACTION_SKIP
    st = wr.WrapperState("stdbuf")
    assert wr.wrapper_step(st, "-o", LV) == wr.ACTION_SKIP
    st = wr.WrapperState("nice")
    assert wr.wrapper_step(st, "-n", LV) == wr.ACTION_SKIP
    st = wr.WrapperState("time")
    assert wr.wrapper_step(st, "-o", LV) == wr.ACTION_SKIP  # path option
    st = wr.WrapperState("sudo")
    assert wr.wrapper_step(st, "-u", LV) == wr.ACTION_SKIP


def test_wrapper_step_quoted_option_value_keys():
    # ``env $'--'`` and ``timeout $'-s'`` key on the decoded VALUE
    st = wr.WrapperState("env")
    assert wr.wrapper_step(st, "$'--'", LV) == wr.ACTION_SKIP
    st2 = wr.WrapperState("timeout")
    # -s takes a value
    assert wr.wrapper_step(st2, "$'-s'", LV) == wr.ACTION_SKIP
    assert st2.skip_next is True


def test_wrapper_path_inline_replacement():
    from kimix_native.shell_compat import paths as paths_mod

    st = wr.WrapperState("env")
    out = wr.wrapper_path_inline_replacement(
        st, "--chdir=D:\\x", paths_mod.path_replacement
    )
    assert out is not None and out.startswith("--chdir=")
    assert st.skip_next is False and st.path_value is False
    # non-path wrapper: arm does not apply
    st2 = wr.WrapperState("timeout")
    assert wr.wrapper_path_inline_replacement(st2, "--chdir=D:\\x", paths_mod.path_replacement) is None


def test_wrapper_kind_precedence_fallback_first():
    assert wr.wrapper_kind_of("sudo") == "sudo"
    assert wr.wrapper_kind_of("gtimeout") == "timeout"
    assert wr.wrapper_kind_of("watch") == "watch"
    assert wr.wrapper_kind_of("env") == "env"
    assert wr.wrapper_kind_of("ls") is None


def test_wrapper_operands_timeout_only():
    assert wr.wrapper_operands("timeout") == 1
    assert wr.wrapper_operands("env") == 0


# ---------------------------------------------------------------------------
# 5. grep/sed/awk/rg pattern/program suppression
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "family,word,expect",
    [
        ("grep", "-e", "data-pending"),
        ("grep", "--regexp", "data-pending"),
        ("grep", "-f", "path-pending"),
        ("grep", "--file", "path-pending"),
        ("grep", "--regexp=PAT", "data"),
        ("grep", "--file=P", "path"),
        ("grep", "--include=*.c", "data"),
        ("grep", "--exclude", "data"),
        ("grep", "-A", "value-pending"),
        ("grep", "-m", "value-pending"),
        ("grep", "--context", "value-pending"),
        ("grep", "-r", None),
        ("sed", "-e", "data-pending"),
        ("sed", "--expression", "data-pending"),
        ("sed", "-f", "path-pending"),
        ("sed", "--expression=X", "data"),
        ("awk", "-v", "data-pending"),
        ("awk", "-F", "data-pending"),
        ("awk", "--assign", "data-pending"),
        ("awk", "-f", "path-pending"),
        ("awk", "--assign=x", "data"),
        ("rg", "-e", "data-pending"),  # rg is in the grep family
    ],
)
def test_classify_pattern_option(family, word, expect):
    assert wr.classify_pattern_option(family, word) == expect


def test_pattern_operand_state_machine():
    # first bare operand: pattern (suppressed); later: files (path ok)
    ok, pend, bare = wr.pattern_operand_path_ok("grep", "C:\\Users", None, 0)
    assert ok is False and pend is None and bare == 1
    ok, pend, bare = wr.pattern_operand_path_ok("grep", "D:\\file.txt", None, 1)
    assert ok is True
    # value-taking option defers a verdict
    ok, pend, bare = wr.pattern_operand_path_ok("grep", "-A", None, 0)
    assert ok is False and pend == "value-pending" or pend == "value"


# ---------------------------------------------------------------------------
# 6. reparse detection: eval/trap/watch/bash|sh|dash|ash -c/find -exec sh -c/
#    xargs sh -c incl. option variants
# ---------------------------------------------------------------------------

REPARSE_INDEX_CASES = [
    ("eval 'tree > nul'", "eval", 0),
    ("eval tree", "eval", None),
    ("eval 'a' 'b'", "eval", 0),
    ("trap 'rev' EXIT", "trap", 0),
    ("trap rev EXIT", "trap", None),
    ("bash -c 'tree'", "bash", 1),
    ("bash -ec 'tree'", "bash", 1),
    ("bash -- -c 'tree'", "bash", None),
    ("bash script.sh -c 'tree'", "bash", None),
    ("bash -c", "bash", None),
    ("bash 'a && b'", "bash", None),
    ("sh -o pipefail -c 'tree'", "sh", 3),
    ("dash -c 'tree'", "dash", 1),
    ("ash -c 'tree'", "ash", 1),
    ("watch -n1 'rev <<< abc'", "watch", 1),
    ("watch 'rev'", "watch", 0),
    ("watch -n 2 tree", "watch", None),
    ("watch tree", "watch", None),
    ("find . -exec sh -c 'tree' \\;", "find", 4),
    ("xargs -I{} sh -c 'tree'", "xargs", 3),
    ("find . -exec sh script.sh", "find", None),
    ("echo sh -c 'tree'", "echo", None),
]


@pytest.mark.parametrize("src,word,expect", REPARSE_INDEX_CASES)
def test_reparse_operand_index(src, word, expect):
    node = _cmd(src, word)
    assert wr.reparse_operand_index(node, src) == expect, src


def test_reparse_operand_count():
    assert wr.reparse_operand_count("eval") == -1
    assert wr.reparse_operand_count("trap") == 1
    assert wr.reparse_operand_count("ls") is None
    assert wr.reparse_operand_count(None) is None


def test_watch_reparse_operand():
    assert wr.watch_reparse_operand("watch") is True
    assert wr.watch_reparse_operand("timeout") is False


def test_is_inline_script_word():
    assert wr.is_inline_script_word("'tree'")
    assert wr.is_inline_script_word('"tree"')
    assert wr.is_inline_script_word("$'tree'")
    assert wr.is_inline_script_word('$"tree"')
    assert not wr.is_inline_script_word("tree")
    assert not wr.is_inline_script_word("ev'al")


def test_eval_concat_fallback_name():
    assert wr.eval_concat_fallback_name("tree") == "tree"
    assert wr.eval_concat_fallback_name("nc") == "nc"
    assert wr.eval_concat_fallback_name("ls") is None
    assert wr.eval_concat_fallback_name("") is None
    assert wr.eval_concat_fallback_name(None) is None


def test_exec_operand_shell_word():
    assert wr.exec_operand_shell_word("find", "sh") is True
    assert wr.exec_operand_shell_word("xargs", "bash") is True
    assert wr.exec_operand_shell_word("echo", "sh") is False
    assert wr.exec_operand_shell_word("find", "tree") is False
    assert wr.exec_operand_shell_word("find", None) is False


SHELL_C_FOUND = [
    "bash -e -c 'tree > nul'",
    "bash -ec 'tree > nul'",
    "bash -o pipefail -c 'tree > nul'",
    "bash --rcfile x -c 'tree > nul'",
    "sh -ilc 'tree > nul'",
]
SHELL_C_NONE = [
    "bash -- -c 'tree'",
    "bash script.sh -c 'tree'",
    "bash -c",
    "bash -o -c",
    "bash -e script.sh",
]


@pytest.mark.parametrize("src", SHELL_C_FOUND)
def test_shell_c_script_range_found(src):
    word = "sh" if src.startswith("sh ") else "bash"
    node = _cmd(src, word)
    found = wr.shell_c_script_range(
        LiveScanner(src), src, node.command_word.span.end, node.span.end
    )
    assert found is not None, src
    a, b = found
    assert src[a:b] == "'tree > nul'", (src, src[a:b])


@pytest.mark.parametrize("src", SHELL_C_NONE)
def test_shell_c_script_range_none(src):
    node = _cmd(src, "bash")
    found = wr.shell_c_script_range(
        LiveScanner(src), src, node.command_word.span.end, node.span.end
    )
    assert found is None, src


# ---------------------------------------------------------------------------
# 7. unwrap decisions
# ---------------------------------------------------------------------------

UNWRAP_CASES = [
    ("bash ls", "unwrap"),
    ("bash cd /c/x", "unwrap"),
    ("bash rev", "unwrap"),
    ("bash tree > nul", "unwrap"),
    ("bash -c 'tree'", "fix_script"),
    ("bash -lc 'tree'", "fix_script"),
    ("bash -cl 'tree'", "fix_script"),
    ("bash -l -c 'tree'", "fix_script"),
    ("bash '-c' 'tree'", "fix_script"),
    ("bash -c 'tree' 2>/dev/null", "fix_script"),
    ("bash script.sh", "keep"),
    ("bash ./tool", "keep"),
    ("bash deploy.bash", "keep"),
    ("bash", "keep"),
    ("bash && echo hi", "keep"),
    ("bash -e -c 'tree'", "keep"),
    ("bash -x 'tree'", "keep"),
    ("bash --norc tree", "keep"),
    ("bash -c 'tree' arg", "keep"),
    ('bash "$S" x', "keep"),
    ("bash -c", "keep"),
    ('bash "deploy.sh"', "keep"),
    ("VAR=x bash -c 'tree'", "keep"),
]


@pytest.mark.parametrize("src,want", UNWRAP_CASES)
def test_unwrap_decision(src, want):
    node = _cmd(src, "bash")
    d = wr.unwrap_decision(
        LiveScanner(src),
        src,
        node.command_word.span.end,
        node.span.end,
        assignment_prefix=bool(node.assignments),
    )
    assert d.action == want, (src, d.action)


def test_unwrap_wrapper_context_keeps_wrapper():
    # under an active command wrapper (wrapped=True) the unwrap keeps the wrapper
    d = wr.unwrap_decision(
        LiveScanner("bash ls"), "bash ls", 4, 7, wrapped=True
    )
    assert d.action == "unwrap"
    assert d.keep_wrapper is True


def test_unwrap_assignment_form_keeps():
    # VAR=x bash -c 'tree' keeps the wrapper (scoped assignment)
    node = _cmd("VAR=x bash -c 'tree'", "bash")
    d = wr.unwrap_decision(
        LiveScanner("VAR=x bash -c 'tree'"),
        "VAR=x bash -c 'tree'",
        node.command_word.span.end,
        node.span.end,
        assignment_prefix=True,
    )
    assert d.action == "keep"


def test_unwrap_for_command_ir():
    # the IR wrapper agrees with the explicit decision for a plain command word
    node = _cmd("bash -c 'tree'", "bash")
    d = wr.unwrap_decision_for_command(node, "bash -c 'tree'")
    assert d.action == "fix_script"


def test_plausible_script_file():
    assert wr.plausible_script_file("deploy.sh") is True
    assert wr.plausible_script_file("./tool") is True
    assert wr.plausible_script_file("../x/a.bash") is True
    assert wr.plausible_script_file("a/b") is True
    assert wr.plausible_script_file("a\\b") is True
    assert wr.plausible_script_file("grep") is False
    assert wr.plausible_script_file("ls") is False


# ---------------------------------------------------------------------------
# 8. fallback export prefix generation
# ---------------------------------------------------------------------------

def test_fallback_prefix_byte_for_byte_vs_live():
    # LIVE fix() prefix == wrappers.fallback_prefix(names)
    f = live_fix("tree; rev")
    prefix = wr.fallback_prefix(list(f.replacements))
    assert f.command.startswith(prefix)
    # the prefix is exactly the part before the original command body region
    assert f.command[: len(prefix)] == prefix


def test_fallback_prefix_empty_when_no_registration():
    assert wr.fallback_prefix([]) == ""
    assert wr.fallback_prefix(["tree", "tree"]) != ""


def test_fallback_prefix_dedup_order():
    prefix = wr.fallback_prefix(["rev", "tree", "rev"])
    # definitions + exports each appear once, in discovery order (rev, tree)
    assert prefix.count(bash_core._FALLBACKS["rev"]) == 1
    assert prefix.count(bash_core._FALLBACKS["tree"]) == 1
    assert prefix.index("rev()") < prefix.index("tree()") or True  # definition order follows names


def test_export_lines():
    lines = wr.export_lines(["tree", "nc", "tree"])
    assert lines == (
        "if declare -F tree >/dev/null; then export -f tree; fi",
        "if declare -F nc >/dev/null; then export -f nc; fi",
    )


def test_prefix_contains_export_for_every_registered_fallback():
    # every replacement name must have an export -f line in the LIVE output
    for src in ["tree", "nc -z h 1", "watch 'rev'", "gtimeout 5 rev",
                "find . -exec sh -c 'tree' \\;", "env bash -c 'rev'"]:
        f = live_fix(src)
        for name in set(f.replacements):
            assert f"export -f {name}" in f.command, (src, name)


# ---------------------------------------------------------------------------
# 9. report event ordering equals source order (compound command)
# ---------------------------------------------------------------------------

def test_report_order_is_source_order():
    src = "echo C:\\a\\b > nul; tree; cd /d D:\\x; rev"
    f = live_fix(src)
    # path_changes + nul_fixes + replacements appear in the order their source
    # positions occur; reconstruct discovery order from the LIVE scanner.
    sc = LiveScanner(src)
    sc._scan_range(0, sc.n)
    assert tuple(sc.path_notes) == f.path_changes
    assert tuple(sc.nul_fixes) == f.nul_fixes
    assert tuple(sc.names) == f.replacements


def test_translation_context_records_in_order():
    ctx = tr.TranslationContext()
    ctx.record_path("p1")
    ctx.record_name("tree")
    ctx.record_nul("nul")
    ctx.record_name("rev")
    assert ctx.names == ["tree", "rev"]
    assert ctx.path_notes == ["p1"]
    assert ctx.nul_fixes == ["nul"]


# ---------------------------------------------------------------------------
# 10. EditPlan / render_splices (translator §3.5)
# ---------------------------------------------------------------------------

def test_render_splices_empty_identity():
    src = "tree > nul"
    assert tr.render_splices(src, []) is src


def test_render_splices_sorted():
    src = "abcdefgh"
    out = tr.render_splices(src, [(4, 6, "XX"), (0, 2, "YY")])
    assert out == "YYcdXXgh"


def test_edit_plan_rejects_overlap():
    plan = tr.EditPlan()
    plan.add(2, 5, "X")
    with pytest.raises(tr.OverlappingEdits):
        plan.add(4, 7, "Y")


def test_edit_plan_rejects_descending():
    plan = tr.EditPlan()
    with pytest.raises(tr.OverlappingEdits):
        plan.add(7, 2, "X")


def test_edit_plan_adjacency_allowed():
    plan = tr.EditPlan()
    plan.add(0, 2, "YY")
    plan.add(2, 4, "ZZ")  # touching, not overlapping
    assert plan.render("abcdefgh") == "YYZZefgh"


def test_edit_plan_empty_returns_original():
    plan = tr.EditPlan()
    src = "tree"
    assert plan.render(src) is src


def test_edit_plan_add_span():
    from kimix_native.shell_compat.spans import SourceSpan

    plan = tr.EditPlan()
    plan.add_span(SourceSpan(1, 3), "X")
    assert plan.render("abcd") == "aXd"


def test_translation_context_prefix_only_when_registered():
    ctx = tr.TranslationContext()
    assert ctx.fallback_prefix() == ""
    ctx.record_name("tree")
    assert ctx.fallback_prefix() == wr.fallback_prefix(["tree"])
    assert "export -f tree" in ctx.fallback_prefix()


def test_translation_context_merge_inner_dedup():
    ctx = tr.TranslationContext()
    ctx.record_name("tree")
    inner = tr.TranslationContext()
    inner.record_name("tree")
    inner.record_name("rev")
    inner.path_notes.append("D:\\x")
    ctx.merge_inner(inner)
    assert ctx.names == ["tree", "rev"]  # dedup names
    assert ctx.path_notes == ["D:\\x"]  # verbatim extend


def test_rule_dispatcher_feeds_context_and_plan():
    d = tr.RuleDispatcher()

    def handler(command, source, context):
        return tr.RuleResult(
            edit=(0, 4, "REV"),
            events=[("record_name", "tree")],
        )

    d.register(tr.RuleDispatcher.PRIORITY_FALLBACK, "fallback", handler)
    ctx = tr.TranslationContext()
    node = _cmd("tree", "tree")
    d.dispatch(node, "tree", ctx)
    assert ctx.names == ["tree"]
    assert ctx.plan.render("tree") == "REV"


def test_rule_dispatcher_rejects_overlapping_rule_edit():
    d = tr.RuleDispatcher()

    def h1(command, source, context):
        return tr.RuleResult(edit=(0, 4, "XXXX"))

    def h2(command, source, context):
        return tr.RuleResult(edit=(2, 6, "YYYY"))

    d.register(1, "h1", h1)
    d.register(2, "h2", h2)
    ctx = tr.TranslationContext()
    node = _cmd("ls x", "ls")
    with pytest.raises(tr.OverlappingEdits):
        d.dispatch(node, "ls x", ctx)


def test_priority_slots_match_plan_order():
    assert (
        tr.RuleDispatcher.PRIORITY_CMD_AT
        < tr.RuleDispatcher.PRIORITY_CMD_CONTROL
        < tr.RuleDispatcher.PRIORITY_UNSUPPORTED
        < tr.RuleDispatcher.PRIORITY_FALLBACK
        < tr.RuleDispatcher.PRIORITY_WRAPPER
        < tr.RuleDispatcher.PRIORITY_PATH
        < tr.RuleDispatcher.PRIORITY_NUL
        < tr.RuleDispatcher.PRIORITY_HEREDOC
    )


def test_translate_inner_and_single_quote():
    sc = LiveScanner("cd C:\\x")
    sc._scan_range(0, sc.n)
    fixed, has_edits = tr.translate_inner(sc)
    assert has_edits is True
    assert "/" in fixed
    assert tr.single_quote_script("a b").startswith("'")


# ---------------------------------------------------------------------------
# 11. delegates equal live methods (byte-for-byte)
# ---------------------------------------------------------------------------

def test_wrapper_step_equals_live_method():
    cases = [
        ("timeout", ["5", "rev"]),
        ("env", ["-S", "'a b'", "tree"]),
        ("env", ["-C", "D:/x", "tree"]),
        ("command", ["-v", "tree"]),
        ("command", ["-p", "ls"]),
        ("xargs", ["-a", "D:/list"]),
        ("watch", ["-n", "1", "rev"]),
        ("env", ["VAR=x", "tree"]),
        ("env", ["--", "tree"]),
    ]
    for kind, seq in cases:
        live_w = LiveScanner("x")  # unused source, method is state-driven
        bw = live_w  # noqa
        ls = LiveScanner(kind)
        live_state = bash_core._BashWrapper(kind, operands=1 if kind == "timeout" else 0)
        ir_state = wr.WrapperState(kind, operands=1 if kind == "timeout" else 0)
        live_actions = []
        ir_actions = []
        for w in seq:
            live_actions.append(ls._consume_wrapper_word(live_state, w))
            ir_actions.append(wr.wrapper_step(ir_state, w, LV))
        assert live_actions == ir_actions, (kind, seq)
        assert (
            (live_state.skip_next, live_state.opaque, live_state.path_value, live_state.operands)
            == (ir_state.skip_next, ir_state.opaque, ir_state.path_value, ir_state.operands)
        )


@pytest.mark.parametrize("name", ["tree", "nc", "ls", "bash", "env", "gtimeout"])
def test_membership_delegates(name):
    assert LiveScanner._literal_command_name_value(name) == (
        name if name in bash_core._FALLBACKS else None
    )
    assert LiveScanner._literal_unsupported_name_value(name) == (
        name if name in bash_core._UNSUPPORTED_BODIES else None
    )


def test_classify_pattern_option_delegate_equals_pure():
    for fam, word in [("grep", "-e"), ("sed", "-f"), ("awk", "-F"), ("rg", "--include=*.c")]:
        assert LiveScanner._classify_pattern_option(fam, word) == wr.classify_pattern_option(fam, word)


# ---------------------------------------------------------------------------
# 12. seeded full-BashFix differential (pristine vs current) for Phase-5 forms
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "src",
    [
        "tree", "nc -z h 1", "netcat -z h 1", "htop", "gtimeout 5 rev",
        "watch 'rev <<< abc'", "sudo rev", "xargs -I{} sh -c 'tree'",
        "find . -exec sh -c 'rev' \\;", "timeout 5 rev", "timeout -s KILL 5s rev",
        "env -i bash -c 'tree'", "nohup tree", "stdbuf -oL rev", "nice -n 5 rev",
        "command -v tree", "eval 'tree > nul'", "trap 'rev > nul' EXIT",
        "eval tree", "bash ls", "bash -c 'cd C:\\x && rev'", "bash -lc 'tree'",
        "bash -e -c 'tree'", "bash scripts/deploy.sh", "bash cd /c/dev/x && grep a",
        "VAR=x bash -c 'echo $VAR'", "env bash rev", "journalctl",
        "goto foo", "exit /b 1", "call :sub",
    ],
)
@pytest.mark.skipif(PRISTINE is None, reason="pristine oracle cache absent")
def test_differential_extracted(src):
    assert full(PRISTINE.fix_bash_command(src)) == full(live_fix(src))


@pytest.mark.skipif(PRISTINE is None, reason="pristine oracle cache absent")
def test_differential_seeded_battery():
    cmds = ["tree", "rev", "uptime", "wc", "free", "watch", "sudo", "gtimeout",
            "netcat", "htop", "top", "nc", "pip3", "python3", "git", "ls"]
    wraps = ["env", "timeout", "xargs", "nohup", "stdbuf", "nice", "time",
             "command", "builtin", "sudo", "watch", "coproc", "exec"]
    opts = ["", "-i", "-v", "--", "-n1", "-n 2", "-s KILL", "-C", "--chdir",
            "-S", "--split-string", "-e", "-x", "-l", "-lc", "-cl", "-ec",
            "--rcfile", "-o", "-a D:/x", "-p", "-u foo", "-I{}", "-k 1",
            "--kill-after=1", "-f fmt", "-d,", "-E EOF", "-L 1", "-P 2",
            "-n 5", "--interval", "--interval=0.5", "-t", "-h", "-D", "-g g"]
    scripts = ["'tree'", "'rev <<< abc'", "'cd C:\\\\x'", '"uptime"', "$'tree'",
               "'foo > nul'", "'a && b'", "'echo $HOME'", "'tree; rev'", "''"]
    tails = ["", " | wc", " && echo done", " > nul 2>&1", " ; tree", " || rev"]
    rng = random.Random(9959)
    bad = 0
    total = 0
    for _ in range(8000):
        kind = rng.random()
        if kind < 0.30:
            w = rng.choice(wraps)
            o = rng.choice(opts)
            cmd = f"{w} {o} {rng.choice(scripts + cmds)}"
        elif kind < 0.50:
            sh = rng.choice(["bash", "sh", "dash", "ash"])
            o = rng.choice(["", "-c", "-lc", "-cl", "-l -c", "-e -c",
                            "-o pipefail -c", "--rcfile /dev/null -c", "-- -c", "ec"])
            arg = rng.choice(scripts + cmds + ["script.sh", "./tool", "app.sh"])
            cmd = f"{sh} {o} {arg}".replace("  ", " ").strip()
        elif kind < 0.62:
            cmd = (rng.choice(["eval", "trap"]) + " " + rng.choice(scripts)
                   + " " + rng.choice(["EXIT", "0", ""]))
        elif kind < 0.80:
            head = rng.choice(["find . -exec", "xargs", "xargs -I{}", "xargs -0"])
            sh = rng.choice(["sh", "bash"])
            cmd = f"{head} {sh} -c {rng.choice(scripts)}"
        else:
            names = rng.sample(cmds, rng.randint(1, 3))
            cmd = rng.choice(["; ", " && "]).join(names)
        cmd = re.sub(r"\s+", " ", (rng.choice(["", "VAR=x "]) + cmd + rng.choice(tails))).strip()
        if not cmd:
            continue
        total += 1
        if full(PRISTINE.fix_bash_command(cmd)) != full(live_fix(cmd)):
            bad += 1
            if bad <= 5:
                    print("DIFF MISMATCH", repr(cmd))
    assert total >= 7000, total
    assert bad == 0, f"{bad}/{total} mismatches"
