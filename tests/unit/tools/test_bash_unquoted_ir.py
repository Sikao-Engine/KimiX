r"""Behavior contract for the lexer-driven ``_process_unquoted`` emitter.

:func:`kimix_native.shell_compat.unquoted._process_unquoted` is the canonical
pre-pass used by ``bash_tool._prepare_bash_cmd``.  This suite pins its rule:

* a backslash is a separator only inside a **Windows path word** recognized by
  ``paths.windows_path_replacement`` (drive, UNC, root-relative, ``~\\``,
  ``.\\`` / ``..\\``, or a multi-segment relative path); only those become
  ``/``;
* every other unquoted backslash is a POSIX escape and is preserved with the
  character it escapes (``foo\\bar`` is ``foobar``, ``\\a\\b`` is ``ab``,
  ``x\\n\\t`` is ``xnt``) -- converting it would silently change the word
  bash computes.  This is the historical weak spot fixed on 2026-11-04;
  the previous table pinned the aggressive conversion;
* backslashes inside single / double / ANSI-C quotes stay intact;
* command substitutions (incl. nested plain-paren subshell groups) and
  backticks (incl. nested/escaped) recurse into unquoted mode, including
  when they sit inside double quotes;
* escaped null-device spellings are NOT converted into an absolute /nul
  path; a ``nul`` embedded in a longer word with an escape prefix stays an
  escape;
* unterminated quotes / regions copy the remainder verbatim;
* UNC paths and line continuations.

The bash_core delegate and the facade re-export must resolve to the same
emitter.  Pure Python, no subprocess.
"""

from __future__ import annotations

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


# Behavior-contract (input, expected) pairs distilled from tests/test_bash.py
# coverage + the category battery.  Rows whose expected value is the input
# itself are POSIX escape words that must pass through byte-for-byte.
FROZEN_CASES = [
    ('C:\\path\\to',
     'C:/path/to'),
    ('dir\\file',
     'dir\\file'),
    ('ls C:\\Users\\me',
     'ls C:/Users/me'),
    ('rm -rf ./dist\\build',
     'rm -rf ./dist/build'),
    ('cd sub\\dir && make',
     'cd sub\\dir && make'),
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
     'echo \\\\ \\"'),
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
     'echo "$( (ls) ; cat x\\y )"'),
    ('echo `cat C:\\a\\b`',
     'echo `cat C:/a/b`'),
    ('echo `echo \\\\`date\\\\``',
     'echo `echo \\\\`date\\\\``'),
    ('echo `echo C:\\a`',
     'echo `echo C:/a`'),
    ('echo ``',
     'echo ``'),
    ('echo `a\\\\b`',
     'echo `a\\\\b`'),
    ('echo $(cat C:\\a\\b)',
     'echo $(cat C:/a/b)'),
    ('x=$(echo a\\b)',
     'x=$(echo a\\b)'),
    ("echo $(echo 'a\\b') $(n\\ul)",
     "echo $(echo 'a\\b') $(n\\ul)"),
    ('echo "$(cat C:\\a\\b)"',
     'echo "$(cat C:/a/b)"'),
    ('echo "a$(b\\c)d"',
     'echo "a$(b\\c)d"'),
    ('echo "x $(y $z) w"',
     'echo "x $(y $z) w"'),
    ('\\"a$\'b$(c\\\\d)e\'f\\"',
     '\\"a$\'b$(c\\\\d)e\'f\\"'),
    ('"p$\\\'q$(echo a\\\\b)\\\'r"',
     '"p$\\\'q$(echo a\\\\b)\\\'r"'),
    ('"x$\\\'$(echo p\\\\q)\\\'"',
     '"x$\\\'$(echo p\\\\q)\\\'"'),
    ('"a$\'b`c\\d`e\'f"',
     '"a$\'b`c\\d`e\'f"'),
    ('echo "cost $\'50\' $(echo C:\\b)"',
     'echo "cost $\'50\' $(echo C:/b)"'),
    ('echo "$\'x $(echo C:\\b)"',
     'echo "$\'x $(echo C:/b)"'),
    ('echo "x $(echo C:\\b)"',
     'echo "x $(echo C:/b)"'),
    ('echo "$( `cat C:\\a` )" x\\b',
     'echo "$( `cat C:/a` )" x\\b'),
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
     'echo \\nulx'),
    ('echo a\\nul',
     'echo a\\nul'),
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
     'echo $(unterminated \\a'),
    ("echo 'a\\b' x\\y '",
     "echo 'a\\b' x\\y '"),
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
     'echo a\\\nb\\c'),
    ('echo \\\n C:\\x',
     'echo \\\n C:/x'),
    ('"a\\\nb"',
     '"a\\\nb"'),
    ('',
     ''),
    ('\\',
     '\\'),
    ('\\\\',
     '\\\\'),
    ("echo $'nul' > $'nul'",
     "echo $'nul' > $'nul'"),
    ('echo a\\\\b',
     'echo a\\\\b'),
    ('echo ${var\\path}',
     'echo ${var\\path}'),
    ('echo $((1+\\a))',
     'echo $((1+\\a))'),
    ('echo <(l\\s) >(m\\t)',
     'echo <(l\\s) >(m\\t)'),
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
        == "echo " + DQ + "a$(b" + BS + "c)d" + DQ
    )


def test_literal_ansi_c_inside_dq_keeps_substitution_active():
    # regression (tests/test_bash.py): a literal $-quote inside "..." is
    # ordinary text, but a real $(...) after it must still be walked.  The
    # substitution's word ``c\\d`` is a POSIX escape (``cd``), not a path, so
    # its backslash is preserved.
    cmd = DQ + "a$" + SQ + "b$(c" + BS + "d)e" + SQ + "f" + DQ
    assert _process(cmd) == cmd


def test_escaped_nul_spellings_are_not_converted():
    for spelling in [BS + "nul", "n" + BS + "ul", "nu" + BS + "l", BS + "NUL"]:
        out = _process("echo > " + spelling)
        assert out == "echo > " + spelling
    # the nul escapes must survive intact so fix_bash_command can rewrite
    assert _process("echo > " + BS + "nul") == "echo > " + BS + "nul"
    assert _process("echo > n" + BS + "ul") == "echo > n" + BS + "ul"
    assert _process("echo > nu" + BS + "l") == "echo > nu" + BS + "l"


def test_nul_inside_longer_word_is_a_posix_escape():
    # ``C:\\nul`` is a drive path, so its separator converts; ``\\nulx`` is
    # the escape ``\n`` followed by ``ulx``, not the null device, so the
    # backslash stays and bash still computes the same word.
    assert _process("cat C:" + BS + "nul") == "cat C:/nul"
    assert _process("echo " + BS + "nulx") == "echo " + BS + "nulx"


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
    # a lone backslash inside a double-quoted region stays literal; the
    # backtick content is walked as an unquoted command, but ``r\\s`` is a
    # POSIX escape (``rs``), not a path, so it is preserved too.
    cmd = "echo " + DQ + "p" + BS + "q `r" + BS + "s` t" + DQ
    assert _process(cmd) == cmd
