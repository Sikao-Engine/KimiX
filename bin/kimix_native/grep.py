"""kimix_native.grep -- GREP kernel: ripgrep-style multi-root file search.

Native implementation lives in ``runtime_py.grep`` (compiled grep_engine +
regex_lite, GIL released while scanning)::

    grep.run(pattern, roots, work_dir, include_glob="",
             mode="files_with_matches", ignore_case=False,
             ctx_before=0, ctx_after=0, head_limit=250) -> dict
      keys: ok(bool), status(str), message(str), total_matches(int),
            files(list[tuple[str,int]]), lines(list[str]),
            line_match(list[int])  # parallel to lines; 1 = match line
    grep.pattern_supported(pattern, ignore_case=False) -> bool

The compiled submodule is optional: a stale ``runtime_py`` build without the
``grep`` submodule (``getattr(_native, "grep", None) is None``) behaves like
``KIMIX_NATIVE_GREP=0``; :func:`native_active` reports the effective gate.

The pure-Python fallback below mirrors the engine semantics exactly
(authoritative sources: kimix-base ``src/builtin_tools/grep_engine.{h,cpp}``
and ``src/runtime/py/py_grep.cpp``; byte-for-byte parity against the compiled
module is pinned by ``kimi-cli/tests/native/test_grep_engine_parity.py``):

* walk *roots* in order (no dedup across roots; a missing root is silently
  skipped). Each root is a file or directory; a relative root joins
  *work_dir* the way ``std::filesystem::path`` ``operator/`` does — literal
  separator insertion, NO lexical normalisation and NO separator rewriting
  (the input spelling is preserved verbatim inside the reported walk
  paths), so a ``.`` root appears inside the walk paths and a ``/`` in the
  root stays ``/`` while appended children use the native separator.
  Directories are walked in OS
  enumeration order, pre-order DFS (a directory is descended the moment it
  is reached);
  hidden entries (name starting with ``.``) are skipped at every depth and
  hidden dirs are not descended (an explicitly-named hidden root IS walked);
  only regular files are scanned — a symlink to a file is scanned (status
  follows), a symlink to a dir is never descended.
* ``include_glob``: ``fnmatch_ascii`` (case-SENSITIVE) over the file NAME
  only; applied to explicit file roots as well.
* a file is skipped when its stat size exceeds 4 MiB, when the stat or the
  open/read fails, when the read buffer grows past 4 MiB, or when the first
  64 KiB of the buffer contains a NUL (a NUL past 64 KiB does not skip).
* files are matched as BYTES (regex_lite is byte-wise): the fallback views
  the buffer through a latin-1 decode and compiles the pattern's UTF-8 bytes
  with ``re.ASCII`` semantics; a line is the text up to ``\\n`` minus one
  trailing ``\\r``; a trailing partial line (no closing ``\\n``) is a line,
  a buffer ending in ``\\n`` has no extra empty line; matched LINES are
  counted, not occurrences.  Rendered text is decoded UTF-8 with
  ``surrogateescape`` (invalid bytes become ``\\udcXX``), exactly like the
  pybind str conversion of the native module.
* *ignore_case* folds ASCII ``A-Z`` only (both sides), never Unicode.
* rendering per *mode*: ``files_with_matches`` -> the path alone, capped at
  *head_limit* during collection while ``files`` stays complete;
  ``count_matches`` -> ``path:count``; ``content`` -> ``path:LN:text`` for
  hits, ``path-LN:text`` for context, context runs merged (a line is never
  emitted twice, no separator for adjacent runs), ``--`` between
  non-adjacent runs only, none before the first run. Content and count
  lines are NOT capped (returned whole; the caller truncates).
  ``line_match`` parallels every rendered line (1 = match, 0 = context or
  ``--``); ``head_limit <= 0`` means unlimited.
* ``message`` is ``"{N} match(es) in {M} file(s)"``; paths are the walk
  paths, never base-stripped.
* input validation parity: unsupported *mode* or negative context raises
  ``ValueError`` (like the binding layer); an invalid pattern returns
  ``status="invalid_input"`` (never raises).

Fallback pattern space is a superset of regex-lite (Python ``re`` accepts
look-around, back-references, ``(?i)`` …): callers must gate exotic patterns
through :func:`pattern_supported` before trusting native/fallback identity.

KNOWN COMPILED-ENGINE DEFECTS (staged runtime_py 1.3.0; the Python mirror is
unaffected — these are C++ bugs the kimix-base side must fix, and the reason
callers such as ``kimi_cli.tools.file.grep_local`` keep safety guards):

* backtracking stack overflow: one native frame per greedy repetition, so
  unbounded-quantifier patterns (``\\w+``, ``.*``, ``{m,}``) against a single
  line of ~24 KiB+ kill the process (0xC00000FD);
* non-ASCII walk paths are round-tripped through the narrow/ANSI fs::path
  spelling and come back garbled (not identity even for explicit file roots
  passed verbatim); the mirror keeps the Unicode spelling, so mirror/engine
  parity holds only for ASCII paths;
* embedded NUL marshalling in rendered content lines (a NUL past the 64 KiB
  binary sniff survives on the current build; behaviour is not pinned as the
  C++ fix may still change).
"""

from __future__ import annotations

import fnmatch
import os
import re
import sys

from . import use_native

__all__ = ["run", "pattern_supported", "native_active"]

_MODES = ("files_with_matches", "count_matches", "content")
_BINARY_SNIFF_BYTES = 64 * 1024
_MAX_FILE_BYTES = 4 * 1024 * 1024
# The engine's ignore_case is an ASCII-only A-Za-z fold, applied to both
# pattern and haystack.
_FOLD = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _native_grep():
    """The compiled ``runtime_py.grep`` submodule, or None when inactive.

    Gate (checked live so per-kernel env toggles apply without reloading):
    ``kimix_native.use_native("GREP")`` — covering ``KIMIX_NATIVE`` /
    ``KIMIX_NATIVE_GREP`` and the version gate — AND the submodule actually
    existing on the loaded extension: a stale ``runtime_py`` build without
    ``grep`` must not crash the shim.
    """
    if not use_native("GREP"):
        return None
    pkg = sys.modules.get(__package__)
    native = getattr(pkg, "_native", None) if pkg is not None else None
    if native is None:
        return None
    return getattr(native, "grep", None)


def native_active() -> bool:
    """True when the native engine (not the Python mirror) will run."""
    return _native_grep() is not None


def _empty_result(status: str, message: str) -> dict:
    return {
        "ok": status == "ok",
        "status": status,
        "message": message,
        "total_matches": 0,
        "files": [],
        "lines": [],
        "line_match": [],
    }


# ---------------------------------------------------------------------------
# fs::path-compatible handling (no lexical normalisation, spelling preserved)
# ---------------------------------------------------------------------------


if os.sep == "\\":

    def _is_abs(p: str) -> bool:
        return bool(p) and (
            p[0] in ("/", "\\")
            or (len(p) >= 2 and p[1:2] == ":" and p[2:3] in ("/", "\\"))
            or (len(p) >= 2 and p[0] in ("/\\") and p[1] in ("/\\"))
        )

else:

    def _is_abs(p: str) -> bool:
        return p.startswith("/")


def _filename(p: str) -> str:
    """std::filesystem filename(): the last non-separator component."""
    i = max(p.rfind("\\"), p.rfind("/"))
    return p[i + 1 :] if i >= 0 else p


# ---------------------------------------------------------------------------
# walking (collect_files parity)
# ---------------------------------------------------------------------------


def _iter_dir_dfs(root: str, include_glob: str):
    """Walk paths of files under dir *root*, in recursive_directory_iterator
    order: OS enumeration order, pre-order DFS (descend when reached), hidden
    entries skipped without descending, only regular files emitted
    (symlink-to-file counts, symlink-to-dir is never descended),
    include-glob (case-sensitive fnmatch) over the file NAME. An unreadable
    directory ends that directory's walk silently."""
    try:
        entries = list(os.scandir(root))
    except OSError:
        return
    globs = include_glob
    for entry in entries:
        name = entry.name
        if name.startswith("."):
            continue
        path = os.path.join(root, name)
        try:
            is_file = entry.is_file(follow_symlinks=True)
            is_dir = False if is_file else entry.is_dir(follow_symlinks=False)
        except OSError:
            continue
        if is_file:
            if not globs or fnmatch.fnmatchcase(name, globs):
                yield path
        elif is_dir:
            yield from _iter_dir_dfs(path, globs)


def _collect_files(root: str, work_dir: str, include_glob: str) -> list[str]:
    """Walk paths for one root (file or dir), mirroring collect_files()."""
    rp = root
    if not _is_abs(rp) and work_dir:
        rp = os.path.join(work_dir, rp)
    if not os.path.exists(rp):
        return []
    if os.path.isfile(rp):  # std is_regular_file follows symlinks
        if include_glob and not fnmatch.fnmatchcase(_filename(rp), include_glob):
            return []
        return [rp]
    return list(_iter_dir_dfs(rp, include_glob))


# ---------------------------------------------------------------------------
# per-file scan (scan_file parity)
# ---------------------------------------------------------------------------


def _read_lines(path: str) -> list[str] | None:
    """Byte-view lines (latin-1 decode) or None when the file is skipped.

    Skip: stat size (follows symlinks like fs::file_size) > 4 MiB, stat or
    open/read failure, a read buffer growing past 4 MiB, or a NUL within the
    first 64 KiB of the buffer. Line rules match for_each_line().
    """
    try:
        size = os.stat(path).st_size
    except OSError:
        return None  # failed stat reads as (uintmax)-1 -> skipped
    if size > _MAX_FILE_BYTES:
        return None
    try:
        with open(path, "rb") as fh:
            data = fh.read(_MAX_FILE_BYTES + 1)
    except OSError:
        return None
    if len(data) > _MAX_FILE_BYTES:
        return None
    if b"\x00" in data[:_BINARY_SNIFF_BYTES]:
        return None
    text = data.decode("latin-1")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # buffer ends with '\n': no extra empty line
    return [ln[:-1] if ln.endswith("\r") else ln for ln in lines]


def _compile(pattern: str, ignore_case: bool):
    """Byte-space regex for *pattern*; None when it does not compile.

    The engine matches the UTF-8 pattern bytes against byte lines: encode
    the pattern and view it latin-1 so classes stay byte-wise; ``re.ASCII``
    keeps ``\\w``/``\\d``/``\\s`` ASCII-only like regex_lite. ignore_case is
    folded by hand (ASCII only) instead of ``re.IGNORECASE`` (which folds
    Unicode).
    """
    try:
        pat = pattern.encode("utf-8", "surrogateescape").decode("latin-1")
    except UnicodeError:
        return None
    if ignore_case:
        pat = pat.translate(_FOLD)
    try:
        return re.compile(pat, re.ASCII)
    except re.error:
        return None


def _render_text(line: str) -> str:
    """pybind-str parity: utf-8 decode with surrogateescape of the byte view."""
    return line.encode("latin-1").decode("utf-8", "surrogateescape")


def _render_content(
    path: str,
    lines: list[str],
    hits: list[int],
    ctx_before: int,
    ctx_after: int,
    out_lines: list[str],
    out_match: list[int],
) -> None:
    """Content rendering, mirroring ge::render_content() control flow."""
    n = len(lines)
    last_emitted = -1000
    for li in hits:
        lo = max(0, li - ctx_before)
        hi = min(n - 1, li + ctx_after)
        if lo > last_emitted + 1 and last_emitted > -999:
            out_lines.append("--")
            out_match.append(0)
        for l in range(lo, hi + 1):
            if l <= last_emitted:
                continue
            sep = ":" if l == li else "-"
            out_lines.append(f"{path}{sep}{l + 1}{sep}{_render_text(lines[l])}")
            out_match.append(1 if l == li else 0)
            last_emitted = l
        last_emitted = max(last_emitted, hi)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def run(
    pattern: str,
    roots: list[str],
    work_dir: str,
    include_glob: str = "",
    mode: str = "files_with_matches",
    ignore_case: bool = False,
    ctx_before: int = 0,
    ctx_after: int = 0,
    head_limit: int = 250,
) -> dict:
    """Search *pattern* (regex, engine semantics) under *roots*.

    Returns the pinned dict contract (see module docstring). Routes to the
    compiled engine when :func:`native_active`, else to the pure-Python
    mirror. Raises ``ValueError`` on an unsupported *mode* or negative
    context, exactly like the binding layer.
    """
    native = _native_grep()
    if native is not None:
        return native.run(
            pattern, [str(r) for r in roots], work_dir, include_glob, mode,
            bool(ignore_case), int(ctx_before), int(ctx_after), int(head_limit),
        )
    if mode not in _MODES:
        raise ValueError(
            "mode must be one of files_with_matches|count_matches|content"
        )
    if ctx_before < 0 or ctx_after < 0:
        raise ValueError("ctx_before/ctx_after must be >= 0")
    ctx_before = int(ctx_before)
    ctx_after = int(ctx_after)
    head_limit = int(head_limit)

    rx = _compile(pattern, ignore_case)
    if rx is None:
        return _empty_result(
            "invalid_input", f"invalid pattern: Python could not compile {pattern!r}"
        )
    # The engine folds ASCII A-Z on BOTH sides at match time; the fold is
    # applied to the haystack line views below as well (rendering always
    # uses the unfolded line).
    files: list[tuple[str, int]] = []
    out_lines: list[str] = []
    out_match: list[int] = []
    total = 0

    for root in roots:
        for path in _collect_files(str(root), work_dir, include_glob):
            lines = _read_lines(path)
            if lines is None:
                continue
            # ignore_case: the engine folds BOTH sides (ASCII A-Za-z only);
            # the pattern was already folded in _compile, fold the haystack
            # line views here (rendering still uses the unfolded bytes).
            if ignore_case:
                hits = [i for i, ln in enumerate(lines)
                        if rx.search(ln.translate(_FOLD))]
            else:
                hits = [i for i, ln in enumerate(lines) if rx.search(ln)]
            count = len(hits)
            if not count:
                continue
            total += count
            files.append((path, count))
            if mode == "files_with_matches":
                # capped DURING collection; files[] stays complete
                if head_limit <= 0 or len(out_lines) < head_limit:
                    out_lines.append(path)
                    out_match.append(1)
            elif mode == "count_matches":
                out_lines.append(f"{path}:{count}")
                out_match.append(1)
            else:
                _render_content(path, lines, hits, ctx_before, ctx_after,
                                out_lines, out_match)

    return {
        "ok": True,
        "status": "ok",
        "message": f"{total} match(es) in {len(files)} file(s)",
        "total_matches": total,
        "files": files,
        "lines": out_lines,
        "line_match": out_match,
    }


def pattern_supported(pattern: str, ignore_case: bool = False) -> bool:
    """True when the ACTIVE engine accepts *pattern*.

    Native: the regex-lite probe (no back-references, look-around,
    possessive quantifiers or named groups). Fallback: the local Python
    engine's (strictly larger) pattern space — callers that need native
    identity must call this while the native engine is active.
    """
    native = _native_grep()
    if native is not None:
        return bool(native.pattern_supported(pattern, bool(ignore_case)))
    rx = _compile(pattern, ignore_case)
    return rx is not None
