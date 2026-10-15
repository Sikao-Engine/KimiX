r"""Phase 3 tests: the lexer-driven ``_process_unquoted`` emitter.

Plan section 4 / Phase 3, task test 3.  This suite locks the behavior of
:func:`kimix_native.shell_compat.unquoted._process_unquoted` after its
migration off the duplicated bash_core quote scanners.  The reference
expectations were frozen from the PRISTINE pre-refactor implementation
(``git show HEAD:bin/kimix_native/_shell_compat.py``), so every case here
is a byte-exact regression contract for the migration.

Coverage (brief + the ``_process_unquoted`` cases distilled from
``tests/test_bash.py``):

* plain Windows-path backslash conversion (backslash -> forward slash);

* backslashes inside single / double / ANSI-C quotes stay intact;
* command substitutions (incl. nested plain-paren subshell groups) and
  backticks (incl. nested/escaped) recurse into unquoted mode, including
  when they sit inside double quotes;
* escaped null-device spellings are NOT converted into an absolute /nul
  path, while a nul embedded in a longer word keeps normal conversion;
* unterminated quotes / regions copy the remainder verbatim;
* UNC paths and line continuations.

The bash_core delegate and the facade re-export must resolve to the same
emitter, and (when the pristine oracle file is present) a randomized
differential sweep runs against it.  Pure Python, no subprocess.
"""

from __future__ import annotations

import os

import pytest

# Prime sys.path through the loader BEFORE any kimix_native import so the
# in-repo bin/kimix_native wins over a stale installed wheel (matches the
# sibling suites' import strategy).
from kimi_cli.native_loader import get_compat as _get_compat

assert _get_compat("_shell_compat") is not None

from kimix_native.shell_compat.bash_core import _process_unquoted as _bc_process
from kimix_native.shell_compat.unquoted import _process_unquoted as _process

# The facade is the consumer-facing entry point (parse.py / bash_tool.py
# call through it); its re-export must resolve to the same implementation.
_shell_compat = _get_compat("_shell_compat")


def _pristine():
    # Load the pristine pre-refactor module for the optional differential
    # sweep, or return None when the cache artifact is not present (clean
    # checkout / CI): the frozen table below is then the whole contract.
    path = os.path.join(".kimix_cache", "tmp_pristine_sc.py")
    if not os.path.isfile(path):
        return None
    import importlib.util
    import sys as _sys

    spec = importlib.util.spec_from_file_location("_pristine_unq_p3", path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    _sys.modules["_pristine_unq_p3"] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:  # pragma: no cover - defensive: cache unusable
        return None
    return mod


# Frozen (input, expected) pairs distilled from tests/test_bash.py coverage
# + the Phase-3 category battery; expected values are byte-exact from the
# pristine legacy implementation.
FROZEN_CASES = [
    ('C:\\path\\to',
     'C:/path/to'),
    ('dir\\file',
     'dir/file'),
    ('ls C:\\Users\\me',
     'ls C:/Users/me'),
    ('rm -rf ./dist\\build',
     'rm -rf ./dist/build'),
    ('cd sub\\dir && make',
     'cd sub/dir && make'),
    ('echo a\\ b',
     'echo a\\ b'),
    ('echo \\(x\\)',
     'echo \\(x\\)'),
    ('echo \\|',
     'echo \\|'),
    ('echo \\&',
     'echo \\&'),
    ('echo \\;',
     'echo \\;'),
    ('echo \\<',
     'echo \\<'),
    ('echo \\>',
     'echo \\>'),
    ('echo \\*',
     'echo \\*'),
    ('echo \\?',
     'echo \\?'),
    ('echo \\[a\\]',
     'echo \\[a\\]'),
    ('echo \\{x\\}',
     'echo \\{x\\}'),
    ('echo \\$',
     'echo \\$'),
    ('echo \\\\ \\"',
     'echo /\\ \\"'),
    ('echo \\$HOME',
     'echo \\$HOME'),
    ('echo a\\*b',
     'echo a\\*b'),
    ('echo a\\?b',
     'echo a\\?b'),
    ('echo a\\[b\\]',
     'echo a\\[b\\]'),
    ('echo a\\{b\\}',
     'echo a\\{b\\}'),
    ('echo a\\~b',
     'echo a\\~b'),
    ('echo a\\!b',
     'echo a\\!b'),
    ('echo a\\#b',
     'echo a\\#b'),
    ('echo a\\=b',
     'echo a\\=b'),
    ('echo a\\%b',
     'echo a\\%b'),
    ("'quoted\\path'",
     "'quoted\\path'"),
    ("echo 'a\\b'",
     "echo 'a\\b'"),
    ("echo a'x\\y'b",
     "echo a'x\\y'b"),
    ('echo "a\\b"',
     'echo "a\\b"'),
    ('echo "C:\\Users\\me"',
     'echo "C:\\Users\\me"'),
    ('echo "a\\$b"',
     'echo "a\\$b"'),
    ('echo "a`b"',
     'echo "a`b"'),
    ('echo "\\\\"',
     'echo "\\\\"'),
    ('echo "a\\"',
     'echo "a\\"'),
    ("printf %s $'a\\tb'",
     "printf %s $'a\\tb'"),
    ("echo $'nul'",
     "echo $'nul'"),
    ("echo $'C:\\path\\to'",
     "echo $'C:\\path\\to'"),
    ('echo "$( (true) ; cat C:\\path\\to )"',
     'echo "$( (true) ; cat C:/path/to )"'),
    ('echo "$(true ; cat C:\\path\\to )"',
     'echo "$(true ; cat C:/path/to )"'),
    ('echo $(echo $(echo "C:\\a\\b"))',
     'echo $(echo $(echo "C:\\a\\b"))'),
    ('echo "$(($(echo C:\\a)))"',
     'echo "$(($(echo C:/a)))"'),
    ('echo "$( (ls) ; cat x\\y )"',
     'echo "$( (ls) ; cat x/y )"'),
    ('echo `cat C:\\a\\b`',
     'echo `cat C:/a/b`'),
    ('echo `echo \\\\`date\\\\``',
     'echo `echo //`date/\\``'),
    ('echo `echo C:\\a`',
     'echo `echo C:/a`'),
    ('echo ``',
     'echo ``'),
    ('echo `a\\\\b`',
     'echo `a//b`'),
    ('echo $(cat C:\\a\\b)',
     'echo $(cat C:/a/b)'),
    ('x=$(echo a\\b)',
     'x=$(echo a/b)'),
    ("echo $(echo 'a\\b') $(n\\ul)",
     "echo $(echo 'a\\b') $(n\\ul)"),
    ('echo "$(cat C:\\a\\b)"',
     'echo "$(cat C:/a/b)"'),
    ('echo "a$(b\\c)d"',
     'echo "a$(b/c)d"'),
    ('echo "x $(y $z) w"',
     'echo "x $(y $z) w"'),
    ('\\"a$\'b$(c\\\\d)e\'f\\"',
     '\\"a$\'b$(c\\\\d)e\'f\\"'),
    ('"p$\\\'q$(echo a\\\\b)\\\'r"',
     '"p$\\\'q$(echo a//b)\\\'r"'),
    ('"x$\\\'$(echo p\\\\q)\\\'"',
     '"x$\\\'$(echo p//q)\\\'"'),
    ('"a$\'b`c\\d`e\'f"',
     '"a$\'b`c/d`e\'f"'),
    ('echo "cost $\'50\' $(echo C:\\b)"',
     'echo "cost $\'50\' $(echo C:/b)"'),
    ('echo "$\'x $(echo C:\\b)"',
     'echo "$\'x $(echo C:/b)"'),
    ('echo "x $(echo C:\\b)"',
     'echo "x $(echo C:/b)"'),
    ('echo "$( `cat C:\\a` )" x\\b',
     'echo "$( `cat C:/a` )" x/b'),
    ('echo "$(echo "$(cat C:\\a\\b)")"',
     'echo "$(echo "$(cat C:/a/b)")"'),
    ('echo > \\nul',
     'echo > \\nul'),
    ('echo > n\\ul',
     'echo > n\\ul'),
    ('echo > nu\\l',
     'echo > nu\\l'),
    ('echo > \\nul 2> n\\ul',
     'echo > \\nul 2> n\\ul'),
    ('echo \\NUL',
     'echo \\NUL'),
    ('cat C:\\nul',
     'cat C:/nul'),
    ('echo \\nulx',
     'echo /nulx'),
    ('echo a\\nul',
     'echo a/nul'),
    ('echo \\\\nul',
     'echo //nul'),
    ('echo (\\nul)',
     'echo (\\nul)'),
    ('echo ;\\nul',
     'echo ;\\nul'),
    ('echo |\\nul',
     'echo |\\nul'),
    ('echo $(cat \\nul)',
     'echo $(cat \\nul)'),
    ('echo "n\\ul"',
     'echo "n\\ul"'),
    ("echo 'n\\ul'",
     "echo 'n\\ul'"),
    ("echo $'n\\ul'",
     "echo $'n\\ul'"),
    ('echo `n\\ul`',
     'echo `n\\ul`'),
    ("echo unterminated 'quote",
     "echo unterminated 'quote"),
    ('echo "unterminated',
     'echo "unterminated'),
    ("echo $'unterminated",
     "echo $'unterminated"),
    ('echo `unterminated \\a',
     'echo `unterminated \\a'),
    ('echo $(unterminated \\a',
     'echo $(unterminated /a'),
    ("echo 'a\\b' x\\y '",
     "echo 'a\\b' x/y '"),
    ('echo "a\\b c\\d',
     'echo "a\\b c\\d'),
    ('echo "${x',
     'echo "${x'),
    ('echo "$( (a\\b',
     'echo "$( (a\\b'),
    ('\\\\server\\share',
     '//server/share'),
    ('cat \\\\server\\share\\x.txt',
     'cat //server/share/x.txt'),
    ('echo "\\\\server\\share"',
     'echo "\\\\server\\share"'),
    ('echo a\\\nb\\c',
     'echo a\\\nb/c'),
    ('echo \\\n C:\\x',
     'echo \\\n C:/x'),
    ('"a\\\nb"',
     '"a\\\nb"'),
    ('',
     ''),
    ('\\',
     '/'),
    ('\\\\',
     '//'),
    ("echo $'nul' > $'nul'",
     "echo $'nul' > $'nul'"),
    ('echo a\\\\b',
     'echo a//b'),
    ('echo ${var\\path}',
     'echo ${var/path}'),
    ('echo $((1+\\a))',
     'echo $((1+/a))'),
    ('echo <(l\\s) >(m\\t)',
     'echo <(l/s) >(m/t)'),
    ("find . -exec sh -c 'echo {} > \\nul' \\;",
     "find . -exec sh -c 'echo {} > \\nul' \\;"),
    ("sh -c 'cd /d C:\\x && ls'",
     "sh -c 'cd /d C:\\x && ls'"),
    ('echo "a\\nul b"',
     'echo "a\\nul b"'),
    ('echo \\"nul',
     'echo \\"nul'),
    ('grep -r pattern C:\\src',
     'grep -r pattern C:/src'),
    ('C:\\Windows\\System32\\tasklist.exe',
     'C:/Windows/System32/tasklist.exe'),
    ('cat < \\nul',
     'cat < \\nul'),
    ('cat <<EOF\\nx\\y\\nEOF',
     'cat <<EOF/nx/y/nEOF'),
    ("trap 'echo t' EXIT; chdir C:\\x",
     "trap 'echo t' EXIT; chdir C:/x"),
    ('echo "$(echo \\nul)"',
     'echo "$(echo \\nul)"'),
    ('echo "`nu\\l`"',
     'echo "`nu\\l`"'),
    ('$(a)\\nul',
     '$(a)\\nul'),
    ('`nu\\l`',
     '`nu\\l`'),
    ('a `nu\\l` b',
     'a `nu\\l` b'),]


@pytest.mark.parametrize("command, expected", FROZEN_CASES)
def test_frozen_process_unquoted(command, expected):
    assert _process(command) == expected


def test_case_count():
    # Locks the breadth of the frozen differential table (>= 100 cases).
    assert len(FROZEN_CASES) >= 100


@pytest.mark.parametrize("command, expected", FROZEN_CASES)
def test_bash_core_delegate_matches(command, expected):
    # bash_core keeps the name (intra-module callers + facade re-export)
    # and must resolve to the migrated emitter.
    assert _bc_process(command) == expected


@pytest.mark.parametrize("command, expected", FROZEN_CASES)
def test_facade_reexport_matches(command, expected):
    assert _shell_compat._process_unquoted(command) == expected


# Behavior invariants named in the brief (the FROZEN_CASES table above
# carries the exhaustive byte-exact set).

BS = chr(92)
SQ = chr(39)
DQ = chr(34)





def test_backslashes_inside_quotes_are_intact():
    # single quotes
    assert _process(SQ + "a" + BS + "b" + SQ) == SQ + "a" + BS + "b" + SQ
    # double quotes (lone backslash is literal bash text, never converted)
    assert _process(DQ + "a" + BS + "b" + DQ) == DQ + "a" + BS + "b" + DQ
    assert _process(DQ + "a" + BS + "bc" + DQ) == DQ + "a" + BS + "bc" + DQ
    assert (
        _process(DQ + "C:" + BS + "Users" + BS + "me" + DQ)
        == DQ + "C:" + BS + "Users" + BS + "me" + DQ
    )
    # ANSI-C quotes
    assert _process("$" + SQ + "a" + BS + "tb" + SQ) == "$" + SQ + "a" + BS + "tb" + SQ


def test_command_substitution_recurses_unquoted():
    assert (
        _process("echo $(cat C:" + BS + "a" + BS + "b)")
        == "echo $(cat C:/a/b)"
    )
    # top-level $(...) descends through the flat walk; backslashes convert
    assert (
        _process("echo $( (true) ; cat C:" + BS + "path" + BS + "to )")
        == "echo $( (true) ; cat C:/path/to )"
    )


def test_backtick_recurses_unquoted():
    assert (
        _process("echo `cat C:" + BS + "a" + BS + "b`")
        == "echo `cat C:/a/b`"
    )


def test_dollar_substitution_inside_double_quotes_recurses():
    assert (
        _process(DQ + "$(cat C:" + BS + "a" + BS + "b)" + DQ)
        == DQ + "$(cat C:/a/b)" + DQ
    )
    assert (
        _process("echo " + DQ + "a$(b" + BS + "c)d" + DQ)
        == "echo " + DQ + "a$(b/c)d" + DQ
    )


def test_literal_ansi_c_inside_dq_keeps_substitution_active():
    # regression (tests/test_bash.py): a literal $-quote inside "..." is
    # ordinary text, but a real $(...) after it must still be walked (its
    # backslashes convert).
    cmd = DQ + "a$" + SQ + "b$(c" + BS + "d)e" + SQ + "f" + DQ
    expected = DQ + "a$" + SQ + "b$(c/d)e" + SQ + "f" + DQ
    assert _process(cmd) == expected


def test_escaped_nul_spellings_are_not_converted():
    for spelling in [BS + "nul", "n" + BS + "ul", "nu" + BS + "l", BS + "NUL"]:
        out = _process("echo > " + spelling)
        assert out == "echo > " + spelling
    # the nul escapes must survive intact so fix_bash_command can rewrite
    assert _process("echo > " + BS + "nul") == "echo > " + BS + "nul"
    assert _process("echo > n" + BS + "ul") == "echo > n" + BS + "ul"
    assert _process("echo > nu" + BS + "l") == "echo > nu" + BS + "l"


def test_nul_inside_longer_word_converts_normally():
    # ``C:/nul`` is a real path (the escaped-nul matcher requires a complete
    # word), and ``/nulx`` extends the word -> normal backslash conversion.
    assert _process("cat C:" + BS + "nul") == "cat C:/nul"
    assert _process("echo " + BS + "nulx") == "echo /nulx"


def test_unterminated_forms_copy_remainder_verbatim():
    assert _process("echo unterminated " + SQ + "quote") == "echo unterminated " + SQ + "quote"
    assert _process("echo " + DQ + "unterminated") == "echo " + DQ + "unterminated"
    assert _process("$" + SQ + "unterminated") == "$" + SQ + "unterminated"


def test_unc_paths_match_delegate():
    # UNC leading-double-backslash semantics: migrated emitter == the
    # bash_core delegate (the byte-exact pristine expectation lives in the
    # FROZEN_CASES table).
    unc = BS + BS + "server" + BS + "share"
    assert _process(unc) == _bc_process(unc)


def test_line_continuation_preserved():
    # backslash-newline: the newline is a metachar -> both chars preserved.
    cmd = "a" + BS + "\n" + "b"
    assert _process(cmd) == cmd


def test_backslashes_inside_dq_around_active_backtick():
    # a lone backslash inside a double-quoted region adjacent to a walked
    # backtick substitution: the lone backslash stays literal, the inner
    # substitution backslashes convert.
    assert (
        _process("echo " + DQ + "p" + BS + "q `r" + BS + "s` t" + DQ)
        == "echo " + DQ + "p" + BS + "q `r/s` t" + DQ
    )


# ── optional randomized differential sweep vs the pristine oracle ────────────

PRISTINE = _pristine()


@pytest.mark.skipif(PRISTINE is None, reason="pristine oracle not available")
@pytest.mark.parametrize("command, expected", FROZEN_CASES)
def test_pristine_matches_frozen(command, expected):
    assert PRISTINE._process_unquoted(command) == expected


@pytest.mark.skipif(PRISTINE is None, reason="pristine oracle not available")
def test_pristine_random_sweep():
    import random

    fragments = [
        "C:" + BS + "a", "x", BS + "srv" + BS + "sh", BS + "nul",
        "n" + BS + "ul", "nu" + BS + "l", SQ, DQ, "$", "`", "(", ")",
        "$(", "$" + SQ, "&&", ";", "|", BS + BS, BS + "\n",
        "$" + SQ + "a" + BS + "tb" + SQ, SQ + "sq" + BS + "x" + SQ,
        DQ + "dq" + BS + BS + "x" + DQ, "`bt" + BS + "y`", " ", "\t",
        BS, "#", "*", "${x}", "$((1+2))", "<(l" + BS + "s)", ">", "2>",
    ]
    rnd = random.Random(0x9959F20E)
    for _ in range(3000):
        cmd = "".join(rnd.choice(fragments) for _ in range(rnd.randint(0, 7)))
        assert _process(cmd) == PRISTINE._process_unquoted(cmd), repr(cmd)
