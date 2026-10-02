"""Native GREP kernel integration in the Grep TOOL (plan native-grep D4).

These tests exercise the REAL compiled engine (``runtime_py.grep``, gated via
``kimix_native.grep.native_active``) through ``Grep.__call__`` and pin:

* R1 — with the native kernel active the ripgrep machinery (lookup, ensure,
  download, spawn) and the rtk machinery are NEVER touched, and the tool
  instance has no rg/rtk ensure tasks;
* R2 — every supported Params combination maps onto one engine run in an
  executor with a ``params.timeout`` bound (spied via ``_native_grep_job``);
* R3 — every combination the engine cannot reproduce (type/multiline/
  selectors/archives/multi-entry/unsupported pattern/empty-pattern/
  broken-pattern/timeout/exception/synthetic status, plus the safety gates
  for the two KNOWN ENGINE BUGS) routes to ``backup_grep`` — never to rg;
* R4 — engine output flows through the same ``_postprocess`` pipeline as the
  ripgrep flow: prefix strip (native separators preserved), sensitive
  filter + warning, recorder note, dedup/fold/pagination, grouped output,
  honest (rtk-free) command display;
* gitignore parity (``_build_rg_args`` ran rg's default ``--hidden`` +
  in-repo ``.gitignore`` behavior, which the engine never reads itself) is
  proven end-to-end on a fake repo, including the repo-root-rules-apply-to-
  subdir-search precedence case.

The whole file skips when the kernel is not active (KIMIX_NATIVE=0,
KIMIX_NATIVE_GREP=0, stale pyd without the submodule): the rg flow is then
byte-identical to the pre-integration behaviour, covered by
``tests/tools/test_grep.py``.

Mirror/engine contract tests live in ``test_grep_engine_parity.py``.
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

import kimi_cli.tools.file.grep_local as grep_local
from kimi_cli.soul.agent import Runtime
from kimi_cli.tools.file.grep_local import Grep, Params

_HAS_NATIVE_GREP = grep_local._grep_kernel() is not None

requires_native_grep = pytest.mark.skipif(
    not _HAS_NATIVE_GREP,
    reason="native GREP kernel not active (runtime_py.grep missing or "
    "disabled by KIMIX_NATIVE / KIMIX_NATIVE_GREP)",
)

pytestmark = [requires_native_grep]

_SENTINEL = object()


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


@pytest.fixture
def rg_trips(monkeypatch) -> list[str]:
    """Bomb every ripgrep/rtk mechanism; record (and raise) on any touch."""
    trips: list[str] = []

    def bomb(name: str):
        def _b(*args, **kwargs):
            trips.append(name)
            raise AssertionError(f"ripgrep/rtk machinery touched: {name}")

        return _b

    for fn in (
        "_find_existing_rg",
        "_download_and_install_rg",
        "_ensure_rg_path",
        "_find_existing_rtk",
        "_download_and_install_rtk",
    ):
        monkeypatch.setattr(grep_local, fn, bomb(fn), raising=True)
    for meth in ("_run_rg_subprocess", "_resolve_rtk_path", "_rich_call"):
        monkeypatch.setattr(Grep, meth, bomb(meth), raising=True)
    return trips


@pytest.fixture
def backup_calls(monkeypatch) -> list[Params]:
    """Spy backup_grep (the ORIGINAL still runs; the call is recorded)."""
    calls: list[Params] = []
    orig = Grep.backup_grep

    async def spy(self, params: Params):
        calls.append(params)
        return await orig(self, params)

    monkeypatch.setattr(Grep, "backup_grep", spy, raising=True)
    return calls


@pytest.fixture
def engine_jobs(monkeypatch) -> list[dict]:
    """Spy the executor job: records the engine result dict per run."""
    seen: list[dict] = []
    orig = grep_local._native_grep_job

    def spy(kernel, search_path: Path, params: Params) -> dict:
        res = orig(kernel, search_path, params)
        seen.append(res)
        return res

    monkeypatch.setattr(grep_local, "_native_grep_job", spy, raising=True)
    return seen


@pytest.fixture
def tool(runtime: Runtime, rg_trips: list[str]) -> Grep:
    """R1: constructing the tool with the native kernel must not create the
    rg/rtk ensure tasks (the bombs would have fired already)."""
    assert grep_local._grep_kernel() is not None
    t = Grep(runtime)
    assert t._rg_path is None
    assert t._rg_path_task is None
    assert t._rtk_path is None
    assert t._rtk_path_task is None
    assert rg_trips == []
    return t


def fwd(s: str) -> str:
    """Slash-normalize for assertions (paths stay in OS form by design)."""
    return s.replace("\\", "/")


def make_tree(root: Path, *, env: bool = True) -> Path:
    """ASCII, small-file tree (the engine-bug gates need short lines).

    No line carries a run of consecutive spaces: the tool pipeline runs
    micro-compress (collapses whitespace runs) on BOTH the native and the rg
    flows, while backup_grep never did (pre-existing asymmetry); the parity
    fixtures stay invariant under that stage.

    * ``a.py``       two foo hits (lines 1 and 4, non-adjacent)
    * ``sub/b.py``   one hit on line 2, a second line
    * ``notes.txt``  one hit
    * ``.hidden/f.py`` hidden-dir hit (rg ``--hidden`` parity: SEARCHED)
    * ``cr.txt``     CRLF hit
    * ``node_modules/m.py``  pruned dir (_IGNORED_DIRS)
    * ``.env``       sensitive name (filtered + warned by the pipeline)
    """
    files = {
        "a.py": "def foo():\nx = 1\nnoise\nreturn foo\n",
        "sub/b.py": "start\nfoo bar\nthird line\n",
        "notes.txt": "foo txt\nplain\n",
        os.path.join(".hidden", "f.py"): "foo hidden\n",
        "cr.txt": "foo cr\r\nsecond\r\n",
        os.path.join("node_modules", "m.py"): "foo nm\n",
    }
    if env:
        files[".env"] = "TOKEN=foo\n"
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
    return root


# ---------------------------------------------------------------------------
# T1 — native serves supported searches end to end, rg machinery untouched
# ---------------------------------------------------------------------------


class TestNativeServes:
    async def test_files_with_matches(self, tool: Grep, tmp_path, rg_trips,
                                      backup_calls, engine_jobs):
        tree = make_tree(tmp_path / "t")
        r = await tool(Params(pattern="foo", path=str(tree)))
        assert not r.is_error
        assert rg_trips == [] and backup_calls == [] and len(engine_jobs) == 1
        assert engine_jobs[0]["status"] == "ok"
        out = fwd(r.output)
        for want in ("a.py", "sub/b.py", "notes.txt", ".hidden/f.py", "cr.txt"):
            assert want in out, want
        assert "node_modules" not in out
        assert ".env" not in out  # sensitive-filtered
        # 5 files listed (6 matched, .env filtered) + warning + recorder note
        assert "Found 5 files matching 'foo'." in r.message
        assert ".env" in r.message  # sensitive warning names it
        assert "Recorded" in r.message and "matched file(s)" in r.message

    async def test_content_mode(self, tool: Grep, tmp_path, rg_trips,
                                backup_calls, engine_jobs):
        tree = make_tree(tmp_path / "t")
        r = await tool(
            Params(pattern="foo", path=str(tree), output_mode="content")
        )
        assert not r.is_error
        assert rg_trips == [] and backup_calls == []
        out = fwd(r.output)
        assert "a.py:1:def foo():" in out
        assert "a.py:4:return foo" in out
        assert "sub/b.py:2:foo bar" in out
        assert "cr.txt:1:foo cr" in out  # one trailing CR stripped
        # disjoint runs of a.py carry the rg-style "--" separator:
        assert "--" in r.output.splitlines()

    async def test_count_mode(self, tool: Grep, tmp_path, rg_trips):
        tree = make_tree(tmp_path / "t")
        r = await tool(
            Params(pattern="foo", path=str(tree), output_mode="count_matches")
        )
        assert not r.is_error
        assert rg_trips == []
        out = fwd(r.output)
        assert "a.py:2" in out and "notes.txt:1" in out
        # 7 occurrences in 6 files scanned; .env filtered -> 6 across 5
        assert "Found 6 total occurrences across 5 files." in r.message

    async def test_ignore_case_and_context(self, tool: Grep, tmp_path, rg_trips):
        tree = make_tree(tmp_path / "t")
        r = await tool(
            Params.model_validate(
                {
                    "pattern": "FOO",
                    "path": str(tree),
                    "output_mode": "content",
                    "-i": True,
                    "-B": 1,
                    "-A": 1,
                }
            )
        )
        assert not r.is_error
        assert rg_trips == []
        out = fwd(r.output)
        assert "a.py:1:def foo():" in out  # folded match (ASCII fold)
        assert "a.py-2-x = 1" in out  # -A context
        assert "a.py-3-noise" in out
        assert "a.py:4:return foo" in out
        assert "cr.txt:1:foo cr" in out  # CRLF file folded too

    async def test_head_limit_pagination(self, tool: Grep, tmp_path, rg_trips):
        tree = make_tree(tmp_path / "t", env=False)
        r = await tool(
            Params(pattern="foo", path=str(tree), head_limit=2)
        )
        assert not r.is_error
        assert rg_trips == []
        assert len(r.output.splitlines()) == 2
        # fwm summary counts the mtime-prefetched page (like the rg flow);
        # the honest grand total rides in the truncation note:
        assert "Found 2 files matching 'foo'." in r.message
        assert "Results truncated to 2 lines (total: 5)." in r.message

    async def test_include_glob(self, tool: Grep, tmp_path, rg_trips,
                                backup_calls):
        tree = make_tree(tmp_path / "t")
        r = await tool(Params(pattern="foo", path=str(tree), include="*.py"))
        assert not r.is_error
        assert rg_trips == [] and backup_calls == []
        out = fwd(r.output)
        assert "a.py" in out and ".hidden/f.py" in out
        assert "notes.txt" not in out and "cr.txt" not in out
        assert ".env" not in out

    async def test_grouped_output(self, tool: Grep, tmp_path, rg_trips):
        tree = make_tree(tmp_path / "t")
        r = await tool(
            Params(
                pattern="foo",
                path=str(tree),
                output_mode="content",
                grouped=True,
            )
        )
        assert not r.is_error
        assert rg_trips == []
        assert "# " in r.output  # grouped file headers
        assert "def foo" in r.output

    async def test_line_number_off_rebuilds_stream(self, tool: Grep, tmp_path,
                                                   rg_trips):
        tree = make_tree(tmp_path / "t")
        r = await tool(
            Params.model_validate(
                {
                    "pattern": "foo",
                    "path": str(tree),
                    "output_mode": "content",
                    "-n": False,
                }
            )
        )
        assert not r.is_error
        assert rg_trips == []
        out = fwd(r.output)
        assert "a.py:def foo():" in out  # no line numbers, rg --no-line-number
        assert "a.py:return foo" in out  # match line keeps the ":" separator
        assert "a.py-second" not in out  # (no context here anyway)

    async def test_brief_honest_no_wrapper(self, tool: Grep, tmp_path,
                                            rg_trips):
        tree = make_tree(tmp_path / "t")
        r = await tool(Params(pattern="foo", path=str(tree)))
        assert not r.is_error
        brief = r.brief or ""
        assert brief.startswith("rg ")  # rg-equivalent display
        # no rtk wrapper invocation (argv would start "<abs rtk> rg" or
        # the normalized "rtk rg"): the args after "rg" must follow.
        assert not brief.startswith("rtk ")
        assert " rtk " not in brief.replace(str(tmp_path), "TMP")

    async def test_single_file_target(self, tool: Grep, tmp_path, rg_trips,
                                      backup_calls, engine_jobs):
        tree = make_tree(tmp_path / "t")
        r = await tool(
            Params(pattern="foo", path=str(tree / "a.py"),
                   output_mode="content")
        )
        assert not r.is_error
        assert rg_trips == [] and backup_calls == []
        assert engine_jobs[0]["status"] == "ok"
        # single-file strip makes paths relative to the PARENT dir:
        assert "a.py:1:def foo():" in fwd(r.output)


# ---------------------------------------------------------------------------
# gitignore / hidden emulation (pin (d)): must mirror _build_rg_args —
# rg ran with --hidden and default in-repo .gitignore honouring
# ---------------------------------------------------------------------------


class TestSelectionEmulation:
    async def test_repo_gitignore_and_hidden_parity(self, tool: Grep, tmp_path,
                                                    rg_trips, engine_jobs,
                                                    backup_calls):
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / ".gitignore").write_text("ignored.py\nbuild/\n", encoding="utf-8")
        (repo / "tracked.py").write_text("foo t\n", encoding="utf-8")
        (repo / "ignored.py").write_text("foo i\n", encoding="utf-8")
        (repo / "build").mkdir()
        (repo / "build" / "keep.py").write_text("foo k\n", encoding="utf-8")
        (repo / ".dotfile").write_text("foo d\n", encoding="utf-8")
        r = await tool(Params(pattern="foo", path=str(repo)))
        assert not r.is_error
        assert rg_trips == []
        assert backup_calls == []
        assert len(engine_jobs) == 1, engine_jobs
        assert engine_jobs[0]["status"] == "ok"
        out = fwd(r.output)
        assert "tracked.py" in out
        assert ".dotfile" in out  # rg ran with --hidden
        assert "ignored.py" not in out  # gitignored
        assert "build" not in out  # dir-only rule pruned (no re-include)

    async def test_ancestor_rules_apply_to_subdir_search(self, tool: Grep,
                                                         tmp_path, rg_trips,
                                                         engine_jobs,
                                                         backup_calls):
        """Regression: the repo-root .gitignore MUST apply when the search
        path is a SUBDIRECTORY of the repo (the _ancestor_rulesets chain used
        to drop the repo root and instead re-read the search dir itself)."""
        repo = tmp_path / "repo2"
        (repo / ".git").mkdir(parents=True)
        (repo / ".gitignore").write_text("x.py\n", encoding="utf-8")
        deep = repo / "deep" / "deeper"
        deep.mkdir(parents=True)
        (deep / "x.py").write_text("foo x\n", encoding="utf-8")
        (deep / "ok.py").write_text("foo o\n", encoding="utf-8")
        r = await tool(Params(pattern="foo", path=str(deep)))
        assert not r.is_error
        assert rg_trips == []
        assert backup_calls == []
        assert len(engine_jobs) == 1, engine_jobs
        assert engine_jobs[0]["status"] == "ok"

        out = fwd(r.output)
        assert "ok.py" in out
        assert "x.py" not in out  # root rule anchored from the repo root

    async def test_negation_rule(self, tool: Grep, tmp_path, rg_trips,
                                 engine_jobs, backup_calls):
        repo = tmp_path / "repo3"
        (repo / ".git").mkdir(parents=True)
        (repo / ".gitignore").write_text("*.log\n!keep.log\n", encoding="utf-8")
        (repo / "a.log").write_text("foo a\n", encoding="utf-8")
        (repo / "keep.log").write_text("foo k\n", encoding="utf-8")
        r = await tool(Params(pattern="foo", path=str(repo)))
        assert not r.is_error
        assert backup_calls == [] and len(engine_jobs) == 1
        out = fwd(r.output)
        assert "keep.log" in out
        assert "a.log" not in out

    async def test_include_ignored_disables_gitignore(self, tool: Grep,
                                                      tmp_path, rg_trips,
                                                      engine_jobs,
                                                      backup_calls):
        repo = tmp_path / "repo4"
        (repo / ".git").mkdir(parents=True)
        (repo / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
        (repo / "ignored.py").write_text("foo i\n", encoding="utf-8")
        r = await tool(
            Params(pattern="foo", path=str(repo), include_ignored=True)
        )
        assert not r.is_error
        assert backup_calls == [] and len(engine_jobs) == 1
        assert "ignored.py" in fwd(r.output)  # --no-ignore parity


# ---------------------------------------------------------------------------
# T2 — routing: unsupported combos go to backup_grep, never to rg
# ---------------------------------------------------------------------------


class TestRouting:
    async def _expect_backup(self, tool, backup_calls, rg_trips, params,
                             *, want_error: str | None = None):
        before = len(backup_calls)
        r = await tool(params)
        assert rg_trips == []
        assert len(backup_calls) == before + 1, f"backup not reached: {params}"
        if want_error is not None:
            assert r.is_error
            assert want_error in r.message
        else:
            assert not r.is_error, r.message
        return r

    async def test_type_filter(self, tool, backup_calls, rg_trips, tmp_path):
        tree = make_tree(tmp_path / "t")
        await self._expect_backup(
            tool, backup_calls, rg_trips,
            Params(pattern="foo", path=str(tree), type="py"),
        )

    async def test_multiline_flag(self, tool, backup_calls, rg_trips, tmp_path):
        tree = make_tree(tmp_path / "t")
        await self._expect_backup(
            tool, backup_calls, rg_trips,
            Params(pattern="foo.*start", path=str(tree), multiline=True,
                   output_mode="content"),
        )

    async def test_newline_escape_pattern(self, tool, backup_calls, rg_trips,
                                          tmp_path):
        tree = make_tree(tmp_path / "t")
        await self._expect_backup(
            tool, backup_calls, rg_trips,
            Params.model_validate(
                {"pattern": "foo\\nnoise", "path": str(tree),
                 "output_mode": "content"}
            ),
        )

    async def test_lookaround_pattern(self, tool, backup_calls, rg_trips,
                                      tmp_path):
        tree = make_tree(tmp_path / "t")
        await self._expect_backup(
            tool, backup_calls, rg_trips,
            Params(pattern="(?=foo)f", path=str(tree), output_mode="content"),
        )

    async def test_broken_pattern_is_error_not_backup(self, tool, backup_calls,
                                                      rg_trips, tmp_path):
        tree = make_tree(tmp_path / "t")
        r = await tool(Params(pattern="[unclosed", path=str(tree)))
        assert r.is_error
        assert "Invalid regex pattern" in r.message
        assert rg_trips == []
        assert backup_calls == []  # rg flow errored too; no silent Python run

    async def test_selector_entry(self, tool, backup_calls, rg_trips, tmp_path):
        tree = make_tree(tmp_path / "t")
        await self._expect_backup(
            tool, backup_calls, rg_trips,
            Params(pattern="foo", path=f"{tree / 'a.py'}:1-3",
                   output_mode="content"),
        )

    async def test_archive_member(self, tool, backup_calls, rg_trips, tmp_path):
        tree = make_tree(tmp_path / "t")
        zp = tree / "bundle.zip"
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr("inner.txt", "foo zip\n")
        await self._expect_backup(
            tool, backup_calls, rg_trips,
            Params(pattern="foo", path=f"{zp}:inner.txt"),
        )

    async def test_multi_entry(self, tool, backup_calls, rg_trips, tmp_path):
        tree = make_tree(tmp_path / "t")
        await self._expect_backup(
            tool, backup_calls, rg_trips,
            Params(pattern="foo", path=[str(tree / "a.py"),
                                        str(tree / "notes.txt")]),
        )

    async def test_context_combining(self, tool, backup_calls, rg_trips,
                                     tmp_path):
        tree = make_tree(tmp_path / "t")
        await self._expect_backup(
            tool, backup_calls, rg_trips,
            Params.model_validate(
                {"pattern": "foo", "path": str(tree),
                 "output_mode": "content", "-C": 1, "-B": 2}
            ),
        )

    async def test_empty_pattern(self, tool, backup_calls, rg_trips, tmp_path):
        tree = make_tree(tmp_path / "t")
        await self._expect_backup(
            tool, backup_calls, rg_trips,
            Params(pattern="", path=str(tree)),
            want_error="Pattern cannot be empty",
        )

    async def test_non_ascii_pattern(self, tool, backup_calls, rg_trips,
                                     tmp_path):
        tree = make_tree(tmp_path / "t")
        (tree / "u8.txt").write_text("café foo\n", encoding="utf-8")
        await self._expect_backup(
            tool, backup_calls, rg_trips,
            Params(pattern="café", path=str(tree)),
        )

    async def test_missing_path(self, tool, backup_calls, rg_trips, tmp_path):
        await self._expect_backup(
            tool, backup_calls, rg_trips,
            Params(pattern="foo", path=str(tmp_path / "nope")),
            want_error="does not exist",
        )

    async def test_outside_workspace_error_native(self, tool, backup_calls,
                                                  rg_trips, runtime):
        # A RELATIVE escape: the native branch owns this error (like the rg
        # flow did); backup must not be reached, rg must not be touched.
        r = await tool(Params(pattern="foo", path="../escape-me"))
        assert r.is_error
        assert "outside the workspace" in r.message
        assert rg_trips == [] and backup_calls == []

    async def test_reserved_device_name(self, tool, backup_calls, rg_trips):
        r = await tool(Params(pattern="foo", path="nul"))
        assert r.is_error
        assert "reserved device name" in r.message
        assert rg_trips == [] and backup_calls == []

    async def test_engine_exception(self, tool, backup_calls, rg_trips,
                                    tmp_path, monkeypatch):
        tree = make_tree(tmp_path / "t")

        def raiser(kernel, search_path, params):
            raise RuntimeError("boom")

        monkeypatch.setattr(grep_local, "_native_grep_job", raiser)
        await self._expect_backup(
            tool, backup_calls, rg_trips,
            Params(pattern="foo", path=str(tree)),
        )

    async def test_engine_timeout(self, tool, backup_calls, rg_trips, tmp_path,
                                  monkeypatch):
        tree = make_tree(tmp_path / "t")

        def hang(kernel, search_path, params):
            raise TimeoutError()

        monkeypatch.setattr(grep_local, "_native_grep_job", hang)
        await self._expect_backup(
            tool, backup_calls, rg_trips,
            Params(pattern="foo", path=str(tree)),
        )


# ---------------------------------------------------------------------------
# engine-bug safety gates (see _pattern_can_overflow / _NONASCII_BLOCK)
# ---------------------------------------------------------------------------


class TestEngineBugGates:
    async def test_nonascii_tree_blocked(self, tool, backup_calls, rg_trips,
                                         tmp_path, engine_jobs):
        tree = tmp_path / "uni"
        (tree / "café").mkdir(parents=True)
        (tree / "café" / "a.py").write_text("foo\n", encoding="utf-8")
        r = await tool(Params(pattern="foo", path=str(tree)))
        assert not r.is_error
        assert rg_trips == []
        assert len(backup_calls) == 1  # blocked -> backup
        assert engine_jobs[0]["status"] == "unsupported"
        assert "non-ASCII" in engine_jobs[0]["message"]

    async def test_backtrack_guard_triggers(self, tool, backup_calls,
                                            rg_trips, tmp_path, engine_jobs):
        tree = tmp_path / "long"
        tree.mkdir()
        (tree / "big.txt").write_bytes(b"x" * (32 * 1024) + b" hit\n")
        r = await tool(Params(pattern=r"\w+ hit", path=str(tree)))
        assert not r.is_error
        assert rg_trips == []
        assert len(backup_calls) == 1
        assert engine_jobs[0]["status"] == "unsupported"
        assert "quantifier" in engine_jobs[0]["message"]
        # the backup answered correctly:
        assert "big.txt" in fwd(r.output)

    async def test_backtrack_guard_precise_long_file_safe_pattern(
        self, tool, backup_calls, rg_trips, tmp_path, engine_jobs
    ):
        tree = tmp_path / "long2"
        tree.mkdir()
        (tree / "big.txt").write_bytes(b"x" * (32 * 1024) + b" hit\n")
        r = await tool(Params(pattern="h[oi]t", path=str(tree)))
        assert not r.is_error
        assert rg_trips == [] and backup_calls == []  # served natively
        assert engine_jobs[0]["status"] == "ok"

    async def test_backtrack_guard_precise_quantifier_short_files(
        self, tool, backup_calls, rg_trips, tmp_path, engine_jobs
    ):
        tree = make_tree(tmp_path / "t")
        r = await tool(Params(pattern=r"\w+ foo", path=str(tree),
                              output_mode="content"))
        assert not r.is_error
        assert rg_trips == [] and backup_calls == []  # served natively
        assert engine_jobs[0]["status"] == "ok"
        # "\w+ foo" needs word-chars + space + foo: a.py lines 1 and 4
        assert "a.py:1:def foo():" in fwd(r.output)

    @pytest.mark.parametrize(
        "pattern,risky",
        [
            (r"\w+ hit", True),
            ("a.*b", True),
            ("x{2,}", True),
            ("x{5000}", True),
            ("x{2,9999}", True),
            ("hit", False),
            (r"a\+b", False),
            ("[+*]", False),
            ("x{2,500}", False),
            ("^hit\\s*$", True),
            ("h[oi]t", False),
            ("a?b", False),
        ],
    )
    def test_pattern_can_overflow(self, pattern: str, risky: bool):
        assert grep_local._pattern_can_overflow(pattern) is risky


# ---------------------------------------------------------------------------
# T3 — native vs backup_grep parity on the same fixture tree
# ---------------------------------------------------------------------------


def _norm(res) -> tuple[list[str], str]:
    """(sorted lines, message) with the recorder note normalized away.

    Ordering: engine walk order, os.walk order and rg order are all
    "deterministic but different"; the multiset of lines and every summary
    count must still be identical. The recorder NOTE is a native/rg-flow
    message that backup_grep (a distinct legacy flow) does not write — it is
    pinned by test_files_with_matches instead; here record=False removes it.
    """
    return sorted(res.output.splitlines()), res.message


PARITY_PATTERNS = ["foo", "line", r"^f", "cr", r"\w+ f", "noise"]
# NB: no "o."-style pattern -- backup_grep counts OCCURRENCES in count mode
# while the engine (and rg --count-matches) count matched LINES; a pattern
# with repeated in-line hits would compare against a backup quirk, not
# against the rg semantics the native path mirrors.  Likewise adjacent
# matches are avoided in the context case (see test_context_parity).


class TestNativeVsBackupParity:
    @pytest.mark.parametrize("pattern", PARITY_PATTERNS)
    @pytest.mark.parametrize("mode", ["files_with_matches", "count_matches",
                                      "content"])
    async def test_modes(self, tool, backup_calls, rg_trips, tmp_path,
                         pattern, mode):
        tree = make_tree(tmp_path / "t")
        params = Params(pattern=pattern, path=str(tree), output_mode=mode,
                        record=False)
        expected = await tool.backup_grep(params)
        n_backup_before = len(backup_calls)
        actual = await tool(params)
        assert not actual.is_error and not expected.is_error
        assert rg_trips == []
        # native-served (no extra backup hop) unless a gate routed it:
        assert len(backup_calls) == n_backup_before
        assert _norm(actual) == _norm(expected), (pattern, mode)

    async def test_ignore_case_parity(self, tool, backup_calls, rg_trips,
                                      tmp_path):
        tree = make_tree(tmp_path / "t")
        params = Params.model_validate(
            {"pattern": "FOO", "path": str(tree), "record": False}
        )
        expected = await tool.backup_grep(params)
        expected = await tool.backup_grep(params)
        n = len(backup_calls)
        actual = await tool(params)
        assert len(backup_calls) == n
        assert _norm(actual) == _norm(expected)

    async def test_context_parity(self, tool, backup_calls, rg_trips, tmp_path):
        """Note: adjacent-match context rendering intentionally EXCLUDES
        adjacent hits (see report): regex_lite renders a hit swallowed by a
        preceding hit's context run as a CONTEXT line, while both rg and
        backup_grep re-render it as a match line. Non-adjacent hits (the
        fixture here: a.py lines 1 and 4) are identical."""
        tree = make_tree(tmp_path / "t")
        params = Params.model_validate(
            {"pattern": "foo", "path": str(tree), "output_mode": "content",
             "-B": 1, "-A": 1, "record": False}
        )
        expected = await tool.backup_grep(params)
        n = len(backup_calls)
        actual = await tool(params)
        assert len(backup_calls) == n
        assert _norm(actual) == _norm(expected)

    async def test_include_parity(self, tool, backup_calls, rg_trips, tmp_path):
        tree = make_tree(tmp_path / "t")
        params = Params(pattern="foo", path=str(tree), include="*.txt",
                        record=False)
        expected = await tool.backup_grep(params)
        n = len(backup_calls)
        actual = await tool(params)
        assert len(backup_calls) == n
        assert _norm(actual) == _norm(expected)

    async def test_invalid_utf8_parity(self, tool, backup_calls, rg_trips,
                                       tmp_path):
        tree = tmp_path / "bad8"
        tree.mkdir()
        (tree / "b.txt").write_bytes(b"fo\xffo bad\nsecond bad\n")
        params = Params(pattern="bad", path=str(tree), output_mode="content",
                        record=False)
        expected = await tool.backup_grep(params)
        n = len(backup_calls)
        actual = await tool(params)
        assert len(backup_calls) == n
        assert _norm(actual) == _norm(expected)
        assert "bad" in actual.output

    async def test_crlf_and_hidden_parity(self, tool, backup_calls, rg_trips,
                                          tmp_path):
        tree = make_tree(tmp_path / "t")
        params = Params(pattern="foo", path=str(tree), output_mode="content",
                        record=False)
        expected = await tool.backup_grep(params)
        n = len(backup_calls)
        actual = await tool(params)
        assert len(backup_calls) == n
        assert _norm(actual) == _norm(expected)
        assert "cr.txt:1:foo cr" in fwd(actual.output)
        assert "f.py:1:foo hidden" in fwd(actual.output)
