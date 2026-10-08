"""Tests for the orchestrator gate harness + report generator.

Covers `tools/review_gates.py` and `tools/gen_tool_reports.py`, including the
cross-check that a generated §2.2 summary actually passes `gate_validate`.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from tools import gate_validate, gen_inventory, gen_tool_reports, review_gates


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# inventory / harness manifest consistency
# --------------------------------------------------------------------------


def test_every_inventory_id_has_harness_manifests() -> None:
    ids = set(gen_inventory.INVENTORY)
    for label, table in (
        ("COV_MODULE", review_gates.COV_MODULE),
        ("PROBE_FILE", gen_tool_reports.PROBE_FILE),
        ("PROBES_REQUIRED", gen_tool_reports.PROBES_REQUIRED),
        ("FLOOR", gen_tool_reports.FLOOR),
    ):
        missing = sorted(ids - set(table))
        extra = sorted(set(table) - ids)
        assert not missing, f"{label} missing ids: {missing}"
        assert not extra, f"{label} has unknown ids: {extra}"


def test_inventory_source_entries_are_declared_facts() -> None:
    for uid, (tool, sources, tests) in gen_inventory.INVENTORY.items():
        assert tool, uid
        assert sources, uid
        assert isinstance(tests, list), uid


# --------------------------------------------------------------------------
# review_gates
# --------------------------------------------------------------------------


def test_expand_globs_dedups_and_drops_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(review_gates, "ROOT", tmp_path)
    _write(tmp_path / "a" / "x.py", "x = 1\n")
    _write(tmp_path / "a" / "y.py", "y = 1\n")
    out = review_gates._expand(["a/*.py", "a/x.py", "does/not/exist.py"])
    assert out == ["a/x.py", "a/y.py"]


def test_run_reports_returncode_and_output() -> None:
    rc, out = review_gates._run(
        ["python", "-c", "import sys; print('hello'); sys.exit(3)"]
    )
    assert "hello" in out
    assert "rc=3" in out


def test_summarize_metrics_aggregates() -> None:
    summary: dict = {}
    metrics = {
        "files": [
            {"loc": 100, "functions": [
                {"name": "big", "loc": 130, "complexity": 20},
                {"name": "ok", "loc": 10, "complexity": 3},
            ]},
            {"loc": 50, "functions": [{"name": "mid", "loc": 121, "complexity": 13}]},
        ],
        "breaches": ["b1"],
    }
    review_gates._summarize_metrics(metrics, 1, summary)
    assert summary["module_loc"] == 150
    assert summary["complexity_max"] == 20
    assert summary["longest_fn_loc"] == 130
    assert summary["longest_fn"] == "big"
    assert summary["functions_over_120_loc"] == 2
    assert summary["functions_over_12_cx"] == 2
    assert summary["metrics"] == "fail"
    assert summary["metrics_breaches"] == ["b1"]


def test_summarize_metrics_empty_module() -> None:
    summary: dict = {}
    review_gates._summarize_metrics({"files": [], "breaches": []}, 0, summary)
    assert summary["complexity_max"] == 0
    assert summary["longest_fn"] is None
    assert summary["metrics"] == "pass"


def test_emit_writes_marked_section(tmp_path: Path) -> None:
    p = tmp_path / "log.txt"
    with p.open("w", encoding="utf-8") as fh:
        review_gates._emit(fh, "cmd --flag", "out\n")
    text = p.read_text(encoding="utf-8")
    assert "=== cmd --flag ===" in text
    assert text.endswith("out\n")


# --------------------------------------------------------------------------
# gen_tool_reports — AST helpers
# --------------------------------------------------------------------------

_TOOL_MODULE = '''
class CallableTool2:
    pass


class Params:
    pass


class Widget(CallableTool2[Params]):
    name: str = "widget"

    class Params2:
        pass

    async def __call__(self, params):
        if params is None:
            return ToolError(message="bad", brief="bad")
        return ToolOk(output="ok")
'''


def test_tool_classes_detects_bases_params_and_async_call(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(gen_tool_reports, "ROOT", tmp_path)
    _write(tmp_path / "src" / "pkg" / "m.py", _TOOL_MODULE)
    found = gen_tool_reports.tool_classes(["src/pkg/m.py"])
    assert len(found) == 1
    cls = found[0]
    assert cls["class"] == "Widget"
    assert cls["bases"] == ["CallableTool2"]
    assert cls["params_models"] == ["Params2"]
    assert cls["async_call"] is True
    assert cls["lineno"] == 10


def test_tool_classes_ignores_non_tool_classes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(gen_tool_reports, "ROOT", tmp_path)
    _write(tmp_path / "m.py", "class Plain:\n    pass\n")
    assert gen_tool_reports.tool_classes(["m.py"]) == []


def test_error_contract_counts_returns_and_raises(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(gen_tool_reports, "ROOT", tmp_path)
    _write(
        tmp_path / "m.py",
        '''
        def a():
            if x:
                return ToolError(message="m")
            return ToolOk(output="o")

        def b():
            raise ToolError("x") from None

        def c():
            raise SkipThisTool("y")

        def d():
            raise ValueError("z")
        ''',
    )
    counts = gen_tool_reports.error_contract(["m.py"])
    assert counts["tool_error_returns"] == 1
    assert counts["tool_ok_returns"] == 1
    assert counts["raises"] == 2, "only ToolError/SkipThisTool raises count"


def test_count_probes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(gen_tool_reports, "ROOT", tmp_path)
    _write(
        tmp_path / "t.py",
        """
        def test_one():
            assert 1

        async def test_two():
            assert 2

        def helper():
            return 1
        """,
    )
    assert gen_tool_reports.count_probes("t.py") == 2
    assert gen_tool_reports.count_probes("missing.py") == 0


def test_load_registry_parses_status_rows(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(gen_tool_reports, "ROOT", tmp_path)
    _write(
        tmp_path / "reviews" / "tools" / "00-registry.txt",
        """
        # discovered tool classes: 2
        registered  a.b:One name='one'  a/b.py
        ORPHAN a.c:Two name='two'  a/c.py
        allowlisted a.d:Three name='three'  a/d.py
        """,
    )
    reg = gen_tool_reports.load_registry()
    assert reg == {"a.b:One": "registered", "a.c:Two": "ORPHAN", "a.d:Three": "allowlisted"}


def test_slugify() -> None:
    assert gen_tool_reports.slugify("read_image (ReadMediaFile)") == "read-image-readmediafile"
    assert gen_tool_reports.slugify("!!!") == ""


def test_verdict_mapping() -> None:
    assert gen_tool_reports._verdict([]) == "ok"
    assert gen_tool_reports._verdict([{"severity": "low"}]) == "minor"
    assert gen_tool_reports._verdict([{"severity": "medium"}]) == "minor"
    assert gen_tool_reports._verdict([{"severity": "high"}]) == "major"


# --------------------------------------------------------------------------
# gen_tool_reports — findings + generated summary schema
# --------------------------------------------------------------------------


def _facts(**overrides) -> dict:
    base = {
        "id": "X99",
        "tool": "demo",
        "sources": ["a.py"],
        "test_pats": ["t.py"],
        "gate": {
            "ruff": "pass", "c901": "pass", "metrics": "pass",
            "complexity_max": 3, "longest_fn_loc": 10, "longest_fn": "f",
            "module_loc": 20, "functions_over_120_loc": 0, "functions_over_12_cx": 0,
            "metrics_breaches": [], "tests": ["t.py"], "files": ["a.py"],
            "cov_module": "a", "cov_line": "a.py 90 10 90%", "cov": "pass",
            "pytest": "pass", "pytest_rc": 0, "sha": "deadbeef", "id": "X99",
        },
        "files": ["a.py"],
        "tests": ["t.py"],
        "probe": "probe_gate.py",
        "probes_present": 3,
        "probes_required": 3,
        "floor": 80,
        "metrics": [],
        "fns": [],
        "by_cx": [],
        "classes": [],
        "contract": {"tool_error_returns": 1, "tool_ok_returns": 1, "raises": 0},
        "module_loc": 20,
        "g1": "pass",
        "g3": "pass",
        "g5": "pass",
        "g4": "pass",
    }
    base.update(overrides)
    return base


def test_findings_of_clean_facts_is_empty() -> None:
    assert gen_tool_reports.findings_of(_facts()) == []


def test_findings_of_flags_probe_gap_and_missing_tests() -> None:
    f = _facts(probes_present=1, probes_required=5, tests=[], g4="fail")
    titles = [x["title"] for x in gen_tool_reports.findings_of(f)]
    assert any("probe suite missing" in t for t in titles)
    assert any("no tests in scope" in t for t in titles)


def test_findings_of_flags_lint_and_complexity() -> None:
    f = _facts()
    f["gate"]["ruff"] = "fail"
    f["gate"]["ruff_rc"] = 1
    f["gate"]["functions_over_12_cx"] = 4
    f["gate"]["functions_over_120_loc"] = 2
    titles = [x["title"] for x in gen_tool_reports.findings_of(f)]
    assert any("ruff" in t for t in titles)
    assert any("complexity 12" in t for t in titles)
    assert any("120 LOC" in t for t in titles)


def test_generated_summary_passes_gate_validate() -> None:
    f = _facts()
    findings = gen_tool_reports.findings_of(f)
    summary = gen_tool_reports.summary_of(f, findings)
    assert gate_validate.validate(summary) == [], gate_validate.validate(summary)


def test_generated_summary_extracts_coverage_percent() -> None:
    f = _facts()
    f["gate"]["cov_line"] = "kimi_cli/x.py 100 20 32 80%"
    summary = gen_tool_reports.summary_of(f, [])
    assert summary["gates"]["G3_coverage"]["actual"] == 80.0


def test_summary_verdict_and_high_counts() -> None:
    f = _facts(probes_present=0, probes_required=5, tests=[])
    findings = gen_tool_reports.findings_of(f)
    summary = gen_tool_reports.summary_of(f, findings)
    assert summary["verdict"] == "major"
    assert summary["gates"]["G2_risk"]["open_high"] >= 1
    assert summary["needs_fix_phase"] is True


def test_collect_requires_gate_json(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(gen_tool_reports, "GATES", tmp_path / "gates")
    (tmp_path / "gates").mkdir()
    with pytest.raises(FileNotFoundError):
        gen_tool_reports.collect("C08")
