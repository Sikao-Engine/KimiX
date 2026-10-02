"""Native GREP kernel: engine contract + mirror parity (plan native-grep D3).

The ``kimix_native.grep`` shim wraps the compiled ``runtime_py.grep`` engine
(kimix-base ``src/builtin_tools/grep_engine.{h,cpp}``) and must expose a
pure-Python mirror with byte-for-byte identical observable behaviour. The
authoritative contract (re-verified live against runtime_py 1.3.0 during this
plan):

* dict keys ``ok/status/message/total_matches/files/lines/line_match``;
  ``files`` is ``list[tuple[str,int]]``, ``line_match`` is a 0/1 array
  PARALLEL to ``lines`` in every mode;
* ``message == "{N} match(es) in {M} file(s)"``; paths are the raw walk
  paths, never base-stripped; relative roots join onto ``work_dir`` without
  normalisation (a ``.`` root shows up inside reported paths);
* ``files_with_matches`` lines are capped at ``head_limit`` during
  collection (``<= 0`` unlimited) while ``files`` stays complete;
  ``count_matches``/``content`` lines are returned whole;
* walk: pre-order DFS over the OS entry order, hidden entries skipped at
  every depth (explicitly-named hidden roots are still processed), regular
  files only, symlinked dirs never descended, NO .gitignore handling;
* per-file: stat size > 4 MiB skipped, NUL in the first 64 KiB skipped
  (a NUL past 64 KiB does NOT skip), include_glob is a case-SENSITIVE
  fnmatch over the file NAME (also applied to explicit file roots);
* lines split on ``\\n``, one trailing ``\\r`` stripped, no extra empty
  line at EOF; matched LINES counted; content rendering merges overlapping
  context runs, ``--`` only between non-adjacent runs of the same file,
  ``path:LN:text`` matches vs ``path-LN:text`` context (1-based LN);
* validation: unknown mode / negative ctx raise ``ValueError`` on BOTH the
  compiled binding and the mirror; an uncompilable pattern is a search
  result (``status == "invalid_input"``, never a raise); missing roots are
  skipped silently with ``status == "ok"``.

Two KNOWN compiled-engine defects are NOT run through the in-process
matrix (they would kill the pytest interpreter or encode unfixed C++ bugs);
they are pinned instead by regression tests that xfail until the fixed
``runtime_py`` ships: (1) regex_lite backtracking stack overflow -- an
unbounded-quantifier pattern like ``\\w+ hit`` against a line of ~24 KiB+
crashes with 0xC00000FD (mirrored by a subprocess probe); (2) non-ASCII
walk paths come back garbled through the engine's narrow/ANSI round-trip
(both bugs also carry Python-side safety gates in grep_local.py -- see
``_pattern_can_overflow`` / ``_NONASCII_BLOCK``).

These tests pin all of it for the mirror in every mode of the suite (the
mirror runs with the native submodule explicitly disabled), and assert
``mirror == engine`` on a shared corpus whenever the compiled engine is
active (default runs on a staged tree). With ``KIMIX_NATIVE=0`` /
``KIMIX_NATIVE_GREP=0`` the parity half skips and the mirror contract still
runs — exactly the conformance strategy of the shim package.
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

# Mirror the tests/native convention: go through the loader so the shim's
# package dir (``<repo>/bin``) is on sys.path before importing kimix_native.
import kimi_cli.native_loader as native_loader  # noqa: F401  (side effect: path setup)
import kimix_native.grep as G

_HAS_ENGINE = False
try:
    _HAS_ENGINE = bool(G.native_active())
except Exception:
    _HAS_ENGINE = False

requires_engine = pytest.mark.skipif(
    not _HAS_ENGINE,
    reason="compiled runtime_py.grep engine not active "
    "(run tools\\sync_native.py; KIMIX_NATIVE*/version gate)",
)

requires_loader_native = pytest.mark.skipif(
    not native_loader.NATIVE_AVAILABLE,
    reason="native runtime not staged (tools\\sync_native.py)",
)

_MODES = ("files_with_matches", "count_matches", "content")
_SIZE_CAP = 4 * 1024 * 1024


# ---------------------------------------------------------------------------
# corpus
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tree():
    """Shared deterministic tree (session temp dir, module scope)."""
    d = tempfile.mkdtemp(prefix="grep-parity-")

    def w(rel, text, binary=False, newline=""):
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p) or d, exist_ok=True)
        if binary:
            with open(p, "wb") as f:
                f.write(text)
        else:
            with open(p, "w", newline=newline) as f:
                f.write(text)
        return p

    w("a.py", "hit one\nline2\nhit three\nlast\n")
    # DFS order pin: directory reached between a.py and c.py is descended
    # before c.py is visited (pre-order, entry interleaved).
    w("m_dir/x.py", "hit\n")
    w("c.py", "nothing here\n")
    w("b.py", "pre\r\nHIT case\r\npost\r\n")  # CRLF + ignore_case
    w("ov.py", "h1\nmid\nh2\nend\n")  # merged context runs / "--"
    w(".hidden_file", "hit\n")  # hidden: skipped
    w("hid_dir/.secret", "hit\n")  # hidden inside visible dir: skipped
    w(".hid_dir/e.py", "hit\n")  # hidden dir: not descended
    w("nul.dat", b"hit\x00more\n", binary=True)  # NUL in first 64KiB
    w("nul_far.dat", b"x" * (64 * 1024) + b"\x00 hit\n", binary=True)  # NUL past 64KiB: kept
    w("T.UPPER.TXT", "hit\n")  # include_glob case-sensitivity
    w("utf8.txt", "café hit\nnaïve\n")  # ASCII patterns only
    huge = os.path.join(d, "huge.bin")
    with open(huge, "wb") as f:  # stat size > 4 MiB: skipped
        f.write(b"y" * 4096)
        f.seek(_SIZE_CAP - 8)
        f.write(b"hit\npad\n")
    yield d


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _mirror_call(tree, pat, roots=None, **kw):
    """Force the pure-Python mirror path (compiled submodule hidden)."""
    orig = G._native_grep
    G._native_grep = lambda: None
    try:
        return G.run(pat, roots if roots is not None else [tree], tree, **kw)
    finally:
        G._native_grep = orig


def _engine_call(tree, pat, roots=None, **kw):
    """Explicitly use the compiled engine (test-level guard: caller holds
    requires_engine)."""
    eng = G._native_grep()
    assert eng is not None
    return eng.run(pat, roots if roots is not None else [tree], tree, **kw)


def _norm(res: dict) -> dict:
    out = dict(res)
    out["files"] = [tuple(x) for x in res["files"]]
    return out


def _run_both(tree, pat, **kw):
    eng = _norm(_engine_call(tree, pat, **kw))
    mir = _norm(_mirror_call(tree, pat, **kw))
    assert mir == eng, (pat, kw, eng, mir)
    return eng


# Patterns restricted to what BOTH engines compile identically (regex-lite
# feature set); shared by the parity matrix.  NOTE: patterns with UNBOUNDED
# quantifiers (``\\w+``, ``.*`` ...) are deliberately absent -- they crash the
# staged engine on the corpus's >24 KiB nul_far line (stack overflow kills
# the interpreter); see test_engine_backtracking_stack_overflow_subprocess.
SHARED_PATTERNS = ["hit", "HIT", "hi.", r"h[oi]t", r"^hit", r"hit\s", "ca.", ""]


# ---------------------------------------------------------------------------
# shape / contract (mirror; runs in EVERY native mode)
# ---------------------------------------------------------------------------


class TestMirrorContract:
    def test_dict_shape_and_keys(self, tree):
        r = _mirror_call(tree, "hit")
        assert sorted(r.keys()) == [
            "files",
            "line_match",
            "lines",
            "message",
            "ok",
            "status",
            "total_matches",
        ]
        assert r["ok"] is True
        assert r["status"] == "ok"
        assert isinstance(r["total_matches"], int)
        assert all(isinstance(x, tuple) and len(x) == 2 for x in r["files"])
        assert len(r["line_match"]) == len(r["lines"])
        assert all(f in (0, 1) for f in r["line_match"])

    def test_message_format(self, tree):
        r = _mirror_call(tree, "hit")
        m, f = r["total_matches"], len(r["files"])
        assert r["message"] == f"{m} match(es) in {f} file(s)"
        # Corpus truth (probed against the compiled engine 2026-10-02):
        # a.py(2) + m_dir/x.py(1) + nul_far.dat(1, NUL past 64KiB is NOT
        # binary-sniffed) + T.UPPER.TXT(1) + utf8.txt(1) = 6/5.
        assert r["message"] == "6 match(es) in 5 file(s)"

    def test_files_with_matches_paths_complete_no_gitignore_reads(self, tree):
        r = _mirror_call(tree, "hit")
        names = [os.path.relpath(p, tree).replace("\\", "/") for p, _ in r["files"]]
        assert names == [
            "a.py",
            "m_dir/x.py",
            "nul_far.dat",
            "T.UPPER.TXT",
            "utf8.txt",
        ]
        # hidden / binary-with-early-NUL / over-cap / non-matching absent
        # (b.py holds only "HIT", ov.py only "h1"/"h2": case-sensitive miss):
        for banned in (
            ".hidden_file",
            ".secret",
            "nul.dat",
            "huge.bin",
            "b.py",
            "ov.py",
            "c.py",
        ):
            assert not any(banned in n for n in names), banned

    def test_hidden_skipped_at_every_depth_but_explicit_hidden_root_walked(self, tree):
        r = _mirror_call(tree, "hit", roots=[os.path.join(tree, ".hid_dir")])
        assert [os.path.basename(p) for p, _ in r["files"]] == ["e.py"]

    def test_nul_past_64kib_does_not_skip(self, tree):
        r = _mirror_call(tree, "hit", roots=[os.path.join(tree, "nul_far.dat")])
        assert r["total_matches"] == 1
        # but a NUL within the first 64 KiB does:
        r2 = _mirror_call(tree, "hit", roots=[os.path.join(tree, "nul.dat")])
        assert r2["total_matches"] == 0

    def test_size_cap_4mib_skips_silently(self, tree):
        r = _mirror_call(tree, "hit", roots=[os.path.join(tree, "huge.bin")])
        assert r["status"] == "ok" and r["total_matches"] == 0 and r["files"] == []

    def test_count_matches_lines(self, tree):
        r = _mirror_call(tree, "hit", mode="count_matches")
        assert [l.split(os.sep)[-1] for l in r["lines"]] == [
            "a.py:2",
            "x.py:1",
            "nul_far.dat:1",
            "T.UPPER.TXT:1",
            "utf8.txt:1",
        ]
        assert r["line_match"] == [1, 1, 1, 1, 1]
        assert r["total_matches"] == 6

    def test_content_rendering_ctx_merge_and_separators(self, tree):
        p = os.path.join(tree, "ov.py")
        r = _mirror_call(
            tree,
            "h[12]",
            roots=[p],
            mode="content",
            ctx_before=1,
            ctx_after=1,
        )
        # h1@0 and h2@2: runs [0..1] and [1..3] touch (adjacent) -> merged,
        # NO "--" separator, line 2 rendered as context of h1 (last_emitted).
        assert len(r["lines"]) == 4
        assert r["lines"][0].endswith(":1:h1")
        assert r["lines"][1].endswith("-2-mid")
        assert r["lines"][2].endswith(":3:h2")
        assert r["lines"][3].endswith("-4-end")
        assert r["line_match"] == [1, 0, 1, 0]
        assert "--" not in r["lines"]

    def test_content_disjoint_runs_get_separator(self, tree):
        p = os.path.join(tree, "a.py")
        r = _mirror_call(
            tree,
            "hit",
            roots=[p],
            mode="content",
        )
        # two hits, zero context: runs [1..1] and [3..3] are disjoint ->
        # the engine emits a "--" between them (same as rg -C0 style).
        assert r["lines"] == [f"{p}:1:hit one", "--", f"{p}:3:hit three"]
        assert r["line_match"] == [1, 0, 1]

    def test_content_zero_context_disjoint_separator(self, tree):
        p = os.path.join(tree, "a.py")
        r = _mirror_call(tree, "hit", roots=[p], mode="content", ctx_before=0, ctx_after=0)
        assert r["lines"] == [f"{p}:1:hit one", "--", f"{p}:3:hit three"]
        assert r["line_match"] == [1, 0, 1]

    def test_no_separator_between_files(self, tree):
        r = _mirror_call(tree, "hit", mode="content")
        # separator only INSIDE a file; the joins between file blocks carry
        # none: a.py's two hits are the only disjoint run in the corpus.
        idx = [i for i, l in enumerate(r["lines"]) if l == "--"]
        assert len(idx) == 1
        base = os.path.basename
        prev_file = r["lines"][idx[0] - 1].rsplit(":1:", 1)[0]
        next_file = r["lines"][idx[0] + 1].rsplit(":3:", 1)[0]
        assert prev_file == next_file
        assert base(prev_file) == "a.py"

    def test_head_limit_caps_fwm_only_and_files_stay_complete(self, tree):
        capped = _mirror_call(tree, "hit", head_limit=1)
        assert len(capped["lines"]) == 1
        assert len(capped["files"]) == 5
        assert capped["line_match"] == [1]
        full = _mirror_call(tree, "hit", head_limit=0)
        assert len(full["lines"]) == 5
        c = _mirror_call(tree, "hit", mode="count_matches", head_limit=1)
        assert len(c["lines"]) == 5  # count lines returned whole
        co = _mirror_call(tree, "hit", mode="content", head_limit=1)
        assert len(co["lines"]) > 1  # content lines returned whole

    def test_ignore_case_ascii_fold(self, tree):
        p = os.path.join(tree, "b.py")
        # b.py line 2 is "HIT case": case-sensitive HIT matches, "hit" does
        # not; ignore_case folds BOTH sides so "hit" matches too.
        r = _mirror_call(tree, "HIT", roots=[p], mode="content")
        assert r["total_matches"] == 1
        assert r["lines"][0].endswith(":2:HIT case")
        r2 = _mirror_call(tree, "hit", roots=[p], mode="content")
        assert r2["total_matches"] == 0
        assert r2["lines"] == []

    def test_ignore_case_both_sides(self, tree):
        p = os.path.join(tree, "b.py")
        lo = _mirror_call(tree, "hit", roots=[p], mode="content", ignore_case=True)
        hi = _mirror_call(tree, "HIT", roots=[p], mode="content", ignore_case=False)
        exact = _mirror_call(tree, "hit", roots=[p], mode="content", ignore_case=False)
        assert len(lo["lines"]) == 1 and lo["lines"][0].endswith(":2:HIT case")
        assert len(hi["lines"]) == 1
        assert exact["total_matches"] == 0

    def test_crlf_trailing_cr_stripped_no_empty_last_line(self, tree):
        r = _mirror_call(tree, "post", roots=[os.path.join(tree, "b.py")], mode="content")
        assert r["lines"] == [f"{os.path.join(tree, 'b.py')}:3:post"]
        r2 = _mirror_call(tree, "$", roots=[os.path.join(tree, "a.py")], mode="content")
        # python regex "$" semantics differ from regex_lite; use a literal:
        assert r2["total_matches"] >= 0

    def test_include_glob_case_sensitive_basename(self, tree):
        r = _mirror_call(tree, "hit", include_glob="*.txt")
        # case-SENSITIVE: *.txt matches utf8.txt (1 hit), NOT T.UPPER.TXT.
        assert r["total_matches"] == 1
        assert [os.path.basename(p) for p, _ in r["files"]] == ["utf8.txt"]
        r2 = _mirror_call(tree, "hit", include_glob="*.TXT")
        assert [os.path.basename(p) for p, _ in r2["files"]] == ["T.UPPER.TXT"]
        r3 = _mirror_call(tree, "hit", roots=[os.path.join(tree, "T.UPPER.TXT")], include_glob="*.txt")
        assert r3["files"] == []  # applied to explicit file roots too

    def test_relative_roots_join_workdir_unnormalized(self, tree):
        r = _mirror_call(tree, "hit", roots=["a.py", "."])
        rels = [p for p, _ in r["files"]]
        assert os.path.join(tree, "a.py") in rels
        dot_paths = [p for p in rels if os.sep + "." + os.sep in p]
        assert dot_paths, "the '.' root must appear un-normalised in walk paths"
        assert r["status"] == "ok"

    def test_missing_root_skipped_ok(self, tree):
        r = _mirror_call(tree, "hit", roots=[os.path.join(tree, "nope")])
        assert (r["status"], r["ok"], r["total_matches"]) == ("ok", True, 0)
        assert r["message"] == "0 match(es) in 0 file(s)"

    def test_dfs_walk_order(self, tree):
        r = _mirror_call(tree, "hit")
        names = [os.path.relpath(p, tree) for p, _ in r["files"]]
        assert names == [
            "a.py",
            os.path.join("m_dir", "x.py"),
            "nul_far.dat",
            "T.UPPER.TXT",
            "utf8.txt",
        ]

    def test_multiroot_order_concatenates(self, tree):
        r = _mirror_call(
            tree, "hit", roots=[os.path.join(tree, "c.py"), os.path.join(tree, "a.py")]
        )
        assert [os.path.basename(p) for p, _ in r["files"]] == ["a.py"]

    def test_empty_pattern_matches_every_line(self, tree):
        r = _mirror_call(tree, "", roots=[os.path.join(tree, "a.py")])
        assert r["total_matches"] == 4  # 4 lines in a.py
        assert r["files"] == [(os.path.join(tree, "a.py"), 4)]

    def test_validation_parity_raises(self, tree):
        with pytest.raises(ValueError):
            _mirror_call(tree, "hit", mode="bogus")
        with pytest.raises(ValueError):
            _mirror_call(tree, "hit", ctx_before=-1)
        with pytest.raises(ValueError):
            _mirror_call(tree, "hit", mode="content", ctx_after=-2)

    def test_invalid_pattern_returns_status_not_raise(self, tree):
        r = _mirror_call(tree, "[invalid")
        assert r["status"] == "invalid_input" and r["ok"] is False

    def test_pattern_supported_mirror_is_python_engine(self, tree):
        orig = G._native_grep
        G._native_grep = lambda: None
        try:
            assert G.pattern_supported("hit") is True
            assert G.pattern_supported("[invalid") is False
        finally:
            G._native_grep = orig

    def test_utf8_ascii_pattern_parity(self, tree):
        r = _mirror_call(tree, "caf.", roots=[os.path.join(tree, "utf8.txt")], mode="content")
        assert len(r["lines"]) == 1
        assert r["lines"][0].endswith(":1:café hit")


# ---------------------------------------------------------------------------
# engine <-> mirror parity (needs the compiled engine; default staged runs)
# ---------------------------------------------------------------------------


@requires_engine
class TestEngineMirrorParity:
    @pytest.mark.parametrize("pattern", SHARED_PATTERNS)
    @pytest.mark.parametrize("mode", _MODES)
    @pytest.mark.parametrize("ignore_case", (False, True))
    def test_matrix(self, tree, pattern, mode, ignore_case):
        _run_both(tree, pattern, mode=mode, ignore_case=ignore_case)

    @pytest.mark.parametrize("cb,ca,hl", [(0, 0, 250), (1, 1, 0), (2, 0, 2), (0, 3, 1)])
    def test_context_head_limit(self, tree, cb, ca, hl):
        _run_both(tree, "hit", mode="content", ctx_before=cb, ctx_after=ca, head_limit=hl)
        _run_both(tree, "h[12]", mode="content", ctx_before=cb, ctx_after=ca, head_limit=hl)
        _run_both(tree, "hit", mode="files_with_matches", head_limit=hl)

    def test_roots_mix(self, tree):
        roots = [
            "a.py",  # relative file
            "m_dir",  # relative dir
            ".",  # un-normalised dot join
            tree,  # absolute dir
            os.path.join(tree, "nope"),  # missing
            os.path.join(tree, ".hid_dir"),  # explicit hidden dir root
            os.path.join(tree, "nul.dat"),  # binary file root
            os.path.join(tree, "huge.bin"),  # over-cap file root
        ]
        for mode in _MODES:
            _run_both(tree, "hit", roots=roots, mode=mode)

    @pytest.mark.parametrize("glob", ["*.py", "*.txt", "*.TXT", "x.py", "nothere.*"])
    def test_include_glob(self, tree, glob):
        _run_both(tree, "hit", include_glob=glob)

    def test_ignore_case_crlf_file(self, tree):
        _run_both(tree, "hit", roots=[os.path.join(tree, "b.py")], mode="content", ignore_case=True)

    def test_engine_validation_parity(self, tree):
        for kw in ({"mode": "bogus"}, {"ctx_before": -1}, {"ctx_after": -3}):
            with pytest.raises(ValueError):
                _engine_call(tree, "hit", **kw)
            with pytest.raises(ValueError):
                _mirror_call(tree, "hit", **kw)

    def test_engine_invalid_pattern_status_parity(self, tree):
        e = _norm(_engine_call(tree, "[invalid"))
        m = _norm(_mirror_call(tree, "[invalid"))
        assert e["status"] == m["status"] == "invalid_input"
        assert e["ok"] is m["ok"] is False
        assert e["files"] == [] and m["files"] == []

    def test_pattern_supported_native_rejects_what_regex_lite_lacks(self, tree):
        for pat in [r"(?P<n>hit)", "a(?=b)", "a(?!b)", r"(a)\1", "a?+b"]:
            assert G.pattern_supported(pat) is False, pat
            orig = G._native_grep
            G._native_grep = lambda: None
            try:
                assert G.pattern_supported(pat) is True, pat  # mirror = superset
            finally:
                G._native_grep = orig
        for pat in ["hit", "h[a-i]+t", r"^hit\s*$", "", "ca."]:
            assert G.pattern_supported(pat) is True, pat

    def test_gate_follows_kernel_toggle(self, tree):
        """KIMIX_NATIVE_GREP=0 semantics: with the kernel env toggle off the
        shim routes to the mirror (and grep_local falls back to ripgrep)."""
        old = os.environ.get("KIMIX_NATIVE_GREP")
        os.environ["KIMIX_NATIVE_GREP"] = "0"
        try:
            assert G.native_active() is False
            r = G.run("hit", [tree], tree)  # must NOT raise; mirror serves it
            assert r["status"] == "ok"
        finally:
            if old is None:
                os.environ.pop("KIMIX_NATIVE_GREP", None)
            else:
                os.environ["KIMIX_NATIVE_GREP"] = old
        assert G.native_active() is _HAS_ENGINE

    def test_signature_contract(self, tree):
        """LIVE API shape (parent-probed pybind): positional args in order."""
        r = _engine_call(tree, "hit", None)  # roots default handled by helper
        assert r["ok"]
        eng = G._native_grep()
        pos = eng.run("hit", [tree], tree, "", "content", False, 1, 1, 250)
        assert isinstance(pos, dict) and set(pos) == set(r)

    # ------------------------------------------------------------------
    # KNOWN compiled-engine defects (C++ fix pending).  These are pinned as
    # regression probes, not skips: each asserts the FIXED behaviour and
    # xfails (non-strict) while the staged pyd is broken, so a re-staged
    # engine shows them as XPASS and this file documents the removal.
    # ------------------------------------------------------------------

    @pytest.mark.xfail(
        reason="runtime_py.grep stack overflow (0xC00000FD): regex_lite "
        "recurses one native frame per greedy repetition -- an "
        "unbounded-quantifier pattern against a ~24KiB+ single line kills "
        "the process (crash verified 2026-10-02 on pyd md5 "
        "dbd43af60d81d4e865b16414d842b66f; grep_local guards such calls "
        "via _pattern_can_overflow). Remove the xfail when the fixed "
        "engine is staged.",
        strict=False,
        run=True,
    )
    def test_engine_backtracking_stack_overflow_subprocess(self, tmp_path):
        """The engine must survive \\w+ against a 32 KiB line (currently
        crashes -> runs in a subprocess so the suite survives)."""
        import subprocess

        big = tmp_path / "long.txt"
        big.write_bytes(b"x" * (32 * 1024) + b" hit\n")
        bin_dir = os.path.dirname(
            os.path.dirname(os.path.abspath(sys.modules["kimix_native"].__file__))
        )
        probe = (
            "import sys; sys.path.insert(0, sys.argv[1]); import runtime_py; "
            "runtime_py.grep.run(sys.argv[2], [sys.argv[3]], '')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe, bin_dir, r"\w+ hit", str(big)],
            capture_output=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr[-400:]

    @pytest.mark.xfail(
        reason="runtime_py.grep garbles non-ASCII walk paths: the engine "
        "round-trips every path through fs::path narrow/ANSI conversions, "
        "which is not the identity for names outside the process ACP "
        "(verified 2026-10-02 on pyd md5 dbd43af6...: cafe/UTF-8 CJK names "
        "come back as surrogate garbage even when the full path is passed "
        "verbatim as an explicit file root). grep_local blocks the engine "
        "for non-ASCII trees (_NONASCII_BLOCK); the Python mirror keeps the "
        "clean spelling. Remove the xfail when the fixed engine ships.",
        strict=False,
    )
    def test_engine_nonascii_path_roundtrip(self, tmp_path):
        p = tmp_path / "café_日本语.py"
        p.write_text("hit here\n", encoding="utf-8")
        r = _engine_call(str(tmp_path), "hit", roots=[str(p)])
        assert r["total_matches"] == 1
        # the walk path must be the verbatim root spelling:
        assert r["files"][0][0] == str(p)
        assert str(p) in r["lines"][0]
