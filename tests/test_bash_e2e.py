r"""End-to-end corpus tests: native POSIX command -> Git Bash command.

The parser under test is ``kimix.tools.file.bash.bash_fix.fix_bash_command``
(whose canonical scanner lives in ``bin/kimix_native/_shell_compat.py``).
It rewrites a native POSIX command string into its Git Bash compatible form:

* Windows backslash paths become forward-slash paths
  (``ls D:\repo\src`` -> ``ls D:/repo/src``), while bash escapes that are
  *not* path separators stay byte-for-byte (``ls my\ file.txt``,
  ``cat foo\bar``) -- the historical weak spot of this parser.
* Quoted data, heredoc bodies, comments, assignments and case patterns are
  never rewritten (``grep "C:\Users" f``).
* ``cd /d X`` loses its cmd.exe-only flag; ``nul``/``NUL`` redirection targets
  become ``/dev/null``; Git Bash virtual paths (``/c/x``, ``/tmp/x``) become
  native spellings (``C:/x``, the real temp directory).
* Missing commands (``rev``, ``tree``, ``uptime``...) get a fallback-function
  prelude; a redundant ``bash``/``bash -c`` wrapper is unwrapped and fixed in
  place.

A corpus file is a JSON array pairing each *source* native POSIX command
with the *destination* Git Bash command the parser must produce::

    [
      {
        "name": "drive-path-as-argument",
        "src": "ls D:\\repo\\src",
        "dest": "ls D:/repo/src",
        "note": "backslash separators become forward slashes",
        "expect": {"path_changes": ["D:\\repo\\src"]}
      },
      ["echo hi > D:\\out.txt", "echo hi > D:/out.txt"]
    ]

Object cases require ``src`` and ``dest`` (aliases: ``source``/``target``/
``expected``/``command``); ``name`` and ``note`` are optional; ``expect``
(option keys: ``replacements``, ``path_changes``, ``shell_wrappers``,
``nul_fixes``, ``unsupported``) asserts the structured change notes on the
``BashFix`` result.  A two-element array is shorthand for ``[src, dest]``.
Objects whose keys all start with ``_`` are ignored, so the file can carry
comments.

Token placeholders, expanded in ``src``, ``dest`` and ``expect`` before the
parser runs (keeps corpora portable across machines)::

    {{WIN_TEMP}}    the Windows temp directory in forward-slash form --
                    exactly what the parser substitutes for the Git Bash
                    virtual ``/tmp`` mount.

Every case runs with the Windows Git-Bash gate forced on (``sys.platform``
is patched to ``"win32"``), so a corpus behaves identically on Linux, macOS
and Windows hosts.

Usage::

    pytest tests/test_bash_e2e.py                     # bundled corpus
    pytest tests/test_bash_e2e.py --case=xx.json      # custom corpus
    python tests/test_bash_e2e.py --case=xx.json      # standalone runner
    python tests/test_bash_e2e.py --case=xx.json --record
        # refresh every ``dest``/``expect`` in the file from the parser's
        # current output (for recording new cases after reviewing them)
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass, field
from difflib import unified_diff
from pathlib import Path
from typing import Any
from unittest.mock import patch

try:
    import pytest
except ImportError:  # pragma: no cover - standalone use without pytest installed
    pytest = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASE_FILE = REPO_ROOT / "tests" / "bash" / "cases.json"

#: Portable stand-in for the machine-specific Windows temp directory.
WIN_TEMP_TOKEN = "{{WIN_TEMP}}"

_SRC_KEYS = ("src", "source", "command", "in")
_DEST_KEYS = ("dest", "destination", "target", "expected", "out")

#: Structured ``BashFix`` notes that ``expect`` may pin (name -> dataclass field).
_EXPECT_FIELDS = (
    "replacements",
    "path_changes",
    "shell_wrappers",
    "nul_fixes",
    "unsupported",
)


@dataclass(frozen=True)
class BashCase:
    """One source->destination transformation pair from a corpus file."""

    name: str
    src: str
    dest: str
    note: str = ""
    expect: dict[str, tuple[str, ...]] = field(default_factory=dict)
    index: int = -1


# ---------------------------------------------------------------------------
# corpus loading
# ---------------------------------------------------------------------------


def _win_temp_dir() -> str:
    """Return the host temp dir slash-normalized (mirrors the parser's rule)."""
    return tempfile.gettempdir().replace("\\", "/")


def expand_tokens(value: str) -> str:
    """Substitute placeholder tokens with their host-specific values."""
    return value.replace(WIN_TEMP_TOKEN, _win_temp_dir())


def contract_tokens(value: str) -> str:
    """Inverse of :func:`expand_tokens`, used when recording corpora."""
    temp = _win_temp_dir()
    return value.replace(temp, WIN_TEMP_TOKEN) if temp else value


def _first_string(obj: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = obj.get(key)
        if isinstance(value, str):
            return value
    return None


def load_cases(path: Path) -> list[BashCase]:
    """Parse a corpus file into cases, raising ``ValueError`` with context."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - missing file is a usage error
        raise ValueError(f"cannot read corpus file {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError(f"{path}: corpus must be a JSON array, got {type(data).__name__}")
    cases: list[BashCase] = []
    used_names: dict[str, int] = {}
    for i, entry in enumerate(data):
        if isinstance(entry, list):
            if len(entry) != 2 or not all(isinstance(part, str) for part in entry):
                raise ValueError(f"{path}[{i}]: pair form needs exactly [src, dest]")
            src, dest = entry
            name = ""
            note = ""
            expect: dict[str, tuple[str, ...]] = {}
        elif isinstance(entry, dict):
            if all(str(key).startswith("_") for key in entry):
                continue  # comment object
            src = _first_string(entry, _SRC_KEYS)
            dest = _first_string(entry, _DEST_KEYS)
            if src is None or dest is None:
                raise ValueError(
                    f"{path}[{i}]: object form needs string 'src' and 'dest' keys"
                )
            name = entry.get("name") or ""
            if not isinstance(name, str) or not isinstance(entry.get("note", ""), str):
                raise ValueError(f"{path}[{i}]: 'name'/'note' must be strings")
            note = entry.get("note", "")
            raw_expect = entry.get("expect", {})
            if not isinstance(raw_expect, dict):
                raise ValueError(f"{path}[{i}]: 'expect' must be an object")
            unknown = sorted(set(raw_expect) - set(_EXPECT_FIELDS))
            if unknown:
                raise ValueError(
                    f"{path}[{i}]: unknown 'expect' key(s) {unknown}; "
                    f"allowed: {list(_EXPECT_FIELDS)}"
                )
            expect = {}
            for key, values in raw_expect.items():
                if not isinstance(values, list) or not all(
                    isinstance(v, str) for v in values
                ):
                    raise ValueError(f"{path}[{i}]: expect.{key} must be a list of strings")
                expect[key] = tuple(values)
        else:
            raise ValueError(f"{path}[{i}]: case must be an object or [src, dest], got {type(entry).__name__}")
        if not name:
            name = f"{path.name}#{i}"
        if name in used_names:
            raise ValueError(f"{path}: duplicate case name {name!r} at [{i}] and [{used_names[name]}]")
        used_names[name] = i
        cases.append(
            BashCase(name=name, src=src, dest=dest, note=note, expect=expect, index=i)
        )
    return cases


# ---------------------------------------------------------------------------
# parser access
# ---------------------------------------------------------------------------


def _get_fixer():
    """Return ``bash_fix.fix_bash_command`` from the app layer."""
    from kimix.tools.file.bash.bash_fix import fix_bash_command

    return fix_bash_command


def run_parser(command: str):
    """Transform a native POSIX command as if running on Windows Git Bash."""
    fix_bash_command = _get_fixer()
    # The app-layer gate returns input unchanged off Windows; corpora encode
    # the Git Bash behavior, so the gate is forced open on every host.
    with patch("kimix.tools.file.bash.bash_fix.sys.platform", "win32"):
        return fix_bash_command(command)


# ---------------------------------------------------------------------------
# comparison helpers
# ---------------------------------------------------------------------------


def _visual(text: str) -> str:
    """Render a command with visible newlines so reprs stay single-line."""
    return repr(text)


def describe_failure(case: BashCase, expected: str, actual: str) -> str:
    """Build a readable failure report for one corpus case."""
    lines = [
        f"case [{case.index}] {case.name!r}",
        f"  note: {case.note}" if case.note else None,
        f"  src:      {_visual(case.src)}",
        f"  expected: {_visual(expected)}",
        f"  actual:   {_visual(actual)}",
    ]
    diff = list(
        unified_diff(
            expected.splitlines(),
            actual.splitlines(),
            fromfile="expected",
            tofile="actual",
            lineterm="",
        )
    )
    if diff:
        lines.append("  diff:")
        lines.extend(f"    {row}" for row in diff[:40])
    return "\n".join(line for line in lines if line is not None)


def check_case(case: BashCase) -> str | None:
    """Run one case; return ``None`` on success or a failure report."""
    src = expand_tokens(case.src)
    expected = expand_tokens(case.dest)
    result = run_parser(src)
    if result.command != expected:
        return describe_failure(case, expected, result.command)
    for key, values in case.expect.items():
        want = tuple(expand_tokens(v) for v in values)
        got = tuple(getattr(result, key))
        if got != want:
            return (
                f"case [{case.index}] {case.name!r}: expect.{key} mismatch\n"
                f"  expected: {want!r}\n"
                f"  actual:   {got!r}"
            )
    return None


# ---------------------------------------------------------------------------
# pytest integration
# ---------------------------------------------------------------------------


def _case_path(config: Any) -> Path:
    option = None
    if config is not None:
        option = config.getoption("case", default=None)
    return Path(option) if option else DEFAULT_CASE_FILE


def _case_id(case: BashCase) -> str:
    slug = "".join(ch if (ch.isalnum() or ch in "-_.") else "-" for ch in case.name)
    return slug[:80] or f"case-{case.index}"


def pytest_generate_tests(metafunc: Any) -> None:
    """Parametrize ``bash_case`` from the corpus selected by ``--case``."""
    if "bash_case" not in metafunc.fixturenames:
        return
    path = _case_path(metafunc.config)
    cases = load_cases(path)
    if not cases:
        cases = [BashCase(name="empty-corpus", src="", dest="", index=-1)]
        metafunc.parametrize(
            "bash_case",
            cases,
            ids=["empty-corpus"],
        )
        return
    metafunc.parametrize("bash_case", cases, ids=[_case_id(c) for c in cases])


def test_bash_case(bash_case: BashCase) -> None:
    """The parser must transform ``src`` (native POSIX) into ``dest`` (Git Bash)."""
    if bash_case.index < 0:  # synthetic empty-corpus placeholder
        if pytest is not None:
            pytest.skip("corpus file is empty")
        return
    report = check_case(bash_case)
    assert report is None, report


# ---------------------------------------------------------------------------
# harness self-tests: corpus loading/validation must itself be trustworthy
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "corpus.json"
    path.write_text(text, encoding="utf-8")
    return path


class TestCorpusLoading:
    def test_object_and_pair_forms_and_comments(self, tmp_path: Path) -> None:
        text = json.dumps(
            [
                {"_comment": "ignored"},
                {"name": "a", "src": "ls .\\x", "dest": "ls ./x"},
                ["cat foo\\bar", "cat foo\\bar"],
                {"src": "echo $HOME", "expected": "echo $HOME"},
            ]
        )
        cases = load_cases(_write(tmp_path, text))
        assert [c.name for c in cases] == ["a", "corpus.json#2", "corpus.json#3"]
        assert cases[1].src == r"cat foo\bar"
        assert cases[2].dest == "echo $HOME"

    def test_alias_keys(self, tmp_path: Path) -> None:
        text = json.dumps(
            [{"source": "ls .\\x", "target": "ls ./x", "note": "n"}]
        )
        cases = load_cases(_write(tmp_path, text))
        assert cases[0].src == r"ls .\x"
        assert cases[0].dest == "ls ./x"
        assert cases[0].note == "n"

    def test_win_temp_token_round_trip(self, tmp_path: Path) -> None:
        assert "{{WIN_TEMP}}/x" == contract_tokens(expand_tokens("{{WIN_TEMP}}/x"))
        assert expand_tokens("{{WIN_TEMP}}/a") == _win_temp_dir() + "/a"
        # The parser substitutes the same value for the virtual /tmp mount.
        result = run_parser("echo x > /tmp/a.txt")
        assert expand_tokens("echo x > {{WIN_TEMP}}/a.txt") == result.command

    def test_rejects_bad_shapes(self, tmp_path: Path) -> None:
        bad = {
            "not an array": '{"src": "x", "dest": "y"}',
            "missing dest": '[{"src": "x"}]',
            "src not a string": '[{"src": 1, "dest": "y"}]',
            "pair form arity": '[["a", "b", "c"]]',
            "duplicate names": '[{"name": "x", "src": "a", "dest": "b"},'
            ' {"name": "x", "src": "c", "dest": "d"}]',
            "unknown expect key": '[{"src": "a", "dest": "b", "expect": {"bogus": []}}]',
            "expect not list": '[{"src": "a", "dest": "b", "expect": {"nul_fixes": "nul"}}]',
            "invalid json": "[",
        }
        for label, text in bad.items():
            try:
                load_cases(_write(tmp_path, text))
            except ValueError:
                continue
            raise AssertionError(f"corpus error not raised for {label}")

    def test_failure_report_is_informative(self) -> None:
        case = BashCase(name="c", src=r"ls .\x", dest="wrong", index=0)
        report = check_case(case)
        assert report is not None
        assert "'c'" in report and "src:" in report and "diff:" in report


# ---------------------------------------------------------------------------
# standalone runner (no pytest needed)
# ---------------------------------------------------------------------------


def _record(raw: list[Any], cases: list[BashCase]) -> int:
    """Rewrite dest/expect entries in ``raw`` from the parser's output."""
    changed = 0
    fix_bash_command = _get_fixer()
    for case in cases:
        if case.index < 0:
            continue
        with patch("kimix.tools.file.bash.bash_fix.sys.platform", "win32"):
            result = fix_bash_command(expand_tokens(case.src))
        entry = raw[case.index]
        new_dest = contract_tokens(result.command)
        if isinstance(entry, list):
            if entry[1] != new_dest:
                entry[1] = new_dest
                changed += 1
            continue
        if entry.get("dest") != new_dest:
            for key in _DEST_KEYS:
                if key in entry:
                    entry[key] = new_dest
                    break
            else:
                entry["dest"] = new_dest
            changed += 1
        raw_expect = entry.get("expect")
        if isinstance(raw_expect, dict):
            for key in list(raw_expect):
                want = tuple(getattr(result, key))
                contracted = tuple(contract_tokens(v) for v in want)
                if raw_expect[key] != list(contracted):
                    raw_expect[key] = list(contracted)
                    changed += 1
    return changed


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="test_bash_e2e.py",
        description="Run (or record) a native-POSIX -> Git Bash corpus file.",
    )
    parser.add_argument(
        "--case",
        default=str(DEFAULT_CASE_FILE),
        metavar="PATH",
        help="corpus JSON file (default: tests/bash/cases.json)",
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help="rewrite each case's dest/expect from the parser's current output",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="only report failures"
    )
    args = parser.parse_args(argv)

    path = Path(args.case)
    try:
        cases = load_cases(path)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.record:
        raw = json.loads(path.read_text(encoding="utf-8"))
        changed = _record(raw, cases)
        path.write_text(
            json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"recorded {path}: {changed} field(s) updated")
        return 0

    failures = 0
    for case in cases:
        report = check_case(case)
        if report is None:
            if not args.quiet:
                print(f"PASS [{case.index}] {case.name}")
            continue
        failures += 1
        print(f"FAIL {report}\n")
    total = len(cases)
    print(f"{total - failures}/{total} cases passed ({path})")
    return 1 if failures else 0


if __name__ == "__main__":
    # Allow `python tests/test_bash_e2e.py` without an editable install.
    if "kimix" not in sys.modules:
        try:
            _get_fixer()
        except ImportError:
            sys.path.insert(0, str(REPO_ROOT / "src"))
            sys.path.insert(0, str(REPO_ROOT))
    raise SystemExit(main(sys.argv[1:]))
