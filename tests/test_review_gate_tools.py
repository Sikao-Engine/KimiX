"""Tests for the Phase-0 tools-review gate helpers (plan §3).

Covers `tools/review_metrics.py`, `tools/gate_library_policy.py`,
`tools/review_tool_registry.py`, `tools/gate_dup.py` and
`tools/gate_validate.py` with adversarial / boundary inputs, not smoke tests.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from tools import gate_dup, gate_library_policy, gate_validate, review_metrics, review_tool_registry


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# review_metrics
# --------------------------------------------------------------------------


def test_review_metrics_reports_loc_complexity_and_longest_fn(tmp_path: Path) -> None:
    src = _write(
        tmp_path / "m.py",
        """
        def simple(a):
            return a

        def branchy(a):
            if a:
                for _ in range(3):
                    if _:
                        return 1
            return 0
        """,
    )
    rep = review_metrics.analyze_file(src)
    fns = {f["name"]: f for f in rep["functions"]}
    assert set(fns) == {"simple", "branchy"}
    assert fns["simple"]["complexity"] == 1
    # if + for + if == 3 decisions -> complexity 4
    assert fns["branchy"]["complexity"] == 4
    assert fns["branchy"]["branches"] == 3
    assert rep["longest_fn"]["name"] == "branchy"
    assert rep["module_loc"] == rep["loc"] == len(src.read_text(encoding="utf-8").splitlines())


def test_review_metrics_prunes_nested_function_decisions(tmp_path: Path) -> None:
    src = _write(
        tmp_path / "n.py",
        """
        def outer(a):
            def inner(b):
                if b:
                    return 1
                return 2
            return inner(a)
        """,
    )
    fns = {f["name"]: f for f in review_metrics.analyze_file(src)["functions"]}
    assert fns["outer"]["complexity"] == 1, "inner's `if` must not count toward outer"
    assert fns["inner"]["complexity"] == 2


def test_review_metrics_flags_syntax_error_without_raising(tmp_path: Path) -> None:
    src = _write(tmp_path / "bad.py", "def x(:\n")
    rep = review_metrics.analyze_file(src)
    assert "syntax_error" in rep
    assert rep["functions"] == []


def test_review_metrics_breach_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    src = _write(
        tmp_path / "big.py",
        """
        def long_one(a):
            if a: return 1
            return 0
        """,
    )
    assert review_metrics.main([str(src), "--max-fn-loc", "2"]) == 1
    assert review_metrics.main([str(src), "--max-complexity", "1"]) == 1
    assert review_metrics.main([str(src), "--max-fn-loc", "2", "--max-complexity", "1", "--json"]) == 1
    out = capsys.readouterr().out
    assert '"breaches"' in out


def test_review_metrics_skips_pycache(tmp_path: Path) -> None:
    _write(tmp_path / "pkg" / "__pycache__" / "junk.py", "def x(): return 1\n")
    _write(tmp_path / "pkg" / "real.py", "def x(): return 1\n")
    files = review_metrics._expand_targets([str(tmp_path)])
    assert [f.name for f in files] == ["real.py"]


# --------------------------------------------------------------------------
# gate_library_policy
# --------------------------------------------------------------------------


def test_library_policy_flags_every_mapped_module(tmp_path: Path) -> None:
    _write(
        tmp_path / "p.py",
        """
        import json
        import re

        from datetime import datetime
        from difflib import SequenceMatcher
        """,
    )
    hits, waived, unparsable = gate_library_policy.scan(
        [str(tmp_path)], tmp_path / "missing-allowlist.txt"
    )
    assert unparsable == 0
    assert waived == []
    modules = sorted(h["module"] for h in hits)
    assert modules == ["datetime", "difflib", "json", "re"]
    repl = {h["module"]: h["replacement"] for h in hits}
    assert repl["json"] == "orjson"
    assert repl["re"] == "regex"
    assert repl["datetime"] == "pendulum"
    assert repl["difflib"] == "rapidfuzz"


def test_library_policy_ignores_relative_and_clean_imports(tmp_path: Path) -> None:
    _write(
        tmp_path / "p.py",
        """
        from . import json
        import orjson
        import os
        """,
    )
    hits, _waived, _u = gate_library_policy.scan([str(tmp_path)], tmp_path / "none.txt")
    assert hits == []


def test_library_policy_waiver_by_file_and_by_module(tmp_path: Path) -> None:
    _write(tmp_path / "a.py", "import json\nimport re\n")
    allow = tmp_path / "allow.txt"
    allow.write_text("a.py::re  # re is fine here\n", encoding="utf-8")
    hits, waived, _u = gate_library_policy.scan([str(tmp_path)], allow)
    assert [h["module"] for h in hits] == ["json"]
    assert [w["module"] for w in waived] == ["re"]


def test_library_policy_exit_code_and_json(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _write(tmp_path / "a.py", "import json\n")
    assert gate_library_policy.main([str(tmp_path), "--allowlist", str(tmp_path / "n.txt")]) == 1
    assert gate_library_policy.main(
        [str(tmp_path), "--allowlist", str(tmp_path / "n.txt"), "--json"]
    ) == 1
    assert '"hits"' in capsys.readouterr().out


# --------------------------------------------------------------------------
# review_tool_registry
# --------------------------------------------------------------------------


def test_registry_resolves_class_and_model_name(tmp_path: Path) -> None:
    _write(
        tmp_path / "src" / "pkg" / "__init__.py",
        """
        from .impl import Thing
        Alias = Thing
        """,
    )
    _write(
        tmp_path / "src" / "pkg" / "impl.py",
        """
        class CallableTool2:
            pass

        class Thing(CallableTool2):
            name: str = "thing"
        """,
    )
    index = review_tool_registry.ModuleIndex(sorted((tmp_path / "src" / "pkg").rglob("*.py")))
    assert index.resolve("pkg", "Thing") == "pkg.impl:Thing"
    assert index.resolve("pkg", "Alias") == "pkg.impl:Thing"
    assert index.namespace_classes("pkg") == {"thing": "pkg.impl:Thing"}


def test_registry_module_constant_tool_name(tmp_path: Path) -> None:
    _write(
        tmp_path / "src" / "pkg" / "t.py",
        """
        NAME = "subagent"

        class CallableTool2:
            pass

        class AgentTool(CallableTool2):
            name: str = NAME
        """,
    )
    index = review_tool_registry.ModuleIndex(sorted((tmp_path / "src" / "pkg").rglob("*.py")))
    assert index.tool_names == {("pkg.t", "AgentTool"): "subagent"}


def test_registry_relative_import_levels(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "pkg" / "__init__.py", "")
    _write(tmp_path / "src" / "pkg" / "sub" / "__init__.py", "")
    _write(tmp_path / "src" / "pkg" / "sub" / "leaf.py", "X = 1\n")
    index = review_tool_registry.ModuleIndex(sorted((tmp_path / "src").rglob("*.py")))
    assert index._relative("pkg.sub.leaf", 1, "other") == "pkg.sub.other"
    assert index._relative("pkg.sub", 2, "other") == "pkg.other"
    assert index._relative("pkg.sub.leaf", 1, None) == "pkg.sub"
    assert index._relative("pkg.sub.leaf", 0, "a.b") == "a.b"


def test_registry_report_rows_and_orphans(tmp_path: Path) -> None:
    _write(
        tmp_path / "src" / "m.py",
        """
        class CallableTool2:
            pass

        class Solo(CallableTool2):
            name: str = "solo"
        """,
    )
    index = review_tool_registry.ModuleIndex(sorted((tmp_path / "src").rglob("*.py")))
    assert list(index.tool_names.values()) == ["solo"]

    allow = tmp_path / "allow.txt"
    allow.write_text("# comment\n\nm:Solo  # keep for now\n", encoding="utf-8")
    assert review_tool_registry.load_allowlist(allow) == {"m:Solo"}


# --------------------------------------------------------------------------
# gate_dup
# --------------------------------------------------------------------------


def test_gate_dup_detects_and_waives(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _write(
        tmp_path / "a.py",
        """
        class CallableTool2:
            pass

        class One(CallableTool2):
            name: str = "dup"

        class Two(CallableTool2):
            name: str = "dup"

        class Three(CallableTool2):
            name: str = "unique"
        """,
    )
    tools = gate_dup.discover([str(tmp_path)])
    assert sorted(t["name"] for t in tools) == ["dup", "dup", "unique"]

    assert gate_dup.main([str(tmp_path), "--allowlist", str(tmp_path / "none.txt")]) == 1
    assert (
        gate_dup.main(
            [
                str(tmp_path),
                "--allowlist",
                str(tmp_path / "none.txt"),
                "--allow-dup",
                "dup",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "WAIVED" in out


def test_gate_dup_allowlist_file(tmp_path: Path) -> None:
    allow = tmp_path / "allow.txt"
    allow.write_text("dup # intentional\n", encoding="utf-8")
    assert gate_dup.load_allowlist(allow) == {"dup": "intentional"}


# --------------------------------------------------------------------------
# gate_validate
# --------------------------------------------------------------------------

_VALID_SUMMARY = {
    "id": "C99",
    "tool": "demo",
    "files": ["a.py"],
    "verdict": "ok",
    "gates": {
        "G0_scope": "pass",
        "G1_simplicity": {
            "ruff": "pass",
            "complexity_max": 3,
            "longest_fn_loc": 10,
            "loc_regression": "none",
            "vulture_hits": 0,
            "vulture_waived": 0,
        },
        "G2_risk": {"items_answered": 13, "open_high": 0, "open_medium": 0, "waived_medium": 0},
        "G3_coverage": {
            "module": "a",
            "floor": 80,
            "actual": 90.0,
            "waived_lines": 0,
            "waiver_budget": 25,
        },
        "G4_behavior": {
            "probe_file": "t.py",
            "probes_required": 1,
            "probes_present": 1,
            "result": "pass",
        },
        "G5_regression": {
            "root_suite": "pass",
            "cli_suite": "pass",
            "syntax_check": "pass",
            "git_diff": "pass",
        },
        "G6_independent_rerun": "pass",
    },
    "findings": [],
    "coverage": {"behaviors_mapped": 1, "covered": 1, "gaps": 0, "gap_list": []},
    "gate_evidence": "gates/C99.txt",
    "git_sha": "abc123",
    "worktree_clean_at_start": True,
    "needs_fix_phase": False,
    "duplication_notes": [],
}


def test_gate_validate_accepts_valid_summary() -> None:
    assert gate_validate.validate(dict(_VALID_SUMMARY)) == []


def test_gate_validate_rejects_bad_verdict_and_missing_keys() -> None:
    bad = dict(_VALID_SUMMARY)
    bad["verdict"] = "maybe"
    del bad["git_sha"]
    errors = gate_validate.validate(bad)
    assert any("verdict" in e for e in errors)
    assert any("git_sha" in e for e in errors)


def test_gate_validate_rejects_bad_gate_subtype() -> None:
    bad = dict(_VALID_SUMMARY)
    bad["gates"] = dict(bad["gates"])
    bad["gates"]["G3_coverage"] = {"module": "a", "floor": "eighty"}
    errors = gate_validate.validate(bad)
    assert any("waived_lines" in e for e in errors)
    assert any("expected" in e for e in errors)


def test_gate_validate_findings_require_all_keys() -> None:
    bad = dict(_VALID_SUMMARY)
    bad["findings"] = [{"severity": "high"}]
    errors = gate_validate.validate(bad)
    assert sum("findings[0]" in e for e in errors) == len(gate_validate.FINDING_KEYS) - 1


def test_gate_validate_extracts_last_summary_block(tmp_path: Path) -> None:
    doc = tmp_path / "r.md"
    doc.write_text(
        "# report\n\n```json\n{\"not\": \"a summary\"}\n```\n\n"
        "```json\n"
        '{"id": "C99", "gates": {"G0_scope": "pass"}}\n'
        "```\n",
        encoding="utf-8",
    )
    data, err = gate_validate.find_summary(doc.read_text(encoding="utf-8"))
    assert err is None
    assert data == {"id": "C99", "gates": {"G0_scope": "pass"}}


def test_gate_validate_missing_block_reports_error(tmp_path: Path) -> None:
    doc = tmp_path / "r.md"
    doc.write_text("# no json here\n", encoding="utf-8")
    data, err = gate_validate.find_summary(doc.read_text(encoding="utf-8"))
    assert data is None
    assert err and "no fenced" in err


def test_gate_validate_main_exit_codes(tmp_path: Path) -> None:
    missing = tmp_path / "absent.md"
    assert gate_validate.main([str(missing)]) == 1
