# K18 — `Rm` review

## 0. Gate summary

| Gate | Result | Evidence |
|---|---|---|
| G0 scope | **pass (decision executed)** | plan §4.3 K18: **delete** |
| G1 simplicity | n/a | class removed |
| G2 risk | n/a | class removed |
| G3 coverage | n/a | no tests existed (plan §1.2 K18: "**none**") |
| G4 behavior | n/a | no probe suite required once deleted |
| G5 regression | pass | no importer existed; suites unchanged |
| G6 independent rerun | n/a | |

## 1. Decision

`Rm` lived in `src/kimix/tools/file/__init__.py` next to `Mkdir`. Plain
`Rm` was chosen over "register"

* reachable from **no** agent manifest (ORPHAN in `00-registry.txt`);
* **zero** tests, and no test file so much as imported the class;
* **no** native-shim mirror in `bin/kimix_native/` (contrast `FindStr`, which *is*
  mirrored at `bin/kimix_native/tools.py:362`, and which was therefore registered
  rather than deleted);
* **no** documentation reference;
* registering `Rm` would have granted the model a new file-system capability
  for no demonstrated need, which the plan's §11 non-goals ("no new tool features")
  argue against.

## 2. Change

* removed the `RmParams` / `Rm` classes from
  `src/kimix/tools/file/__init__.py`, leaving a package docstring that records why
* removed `K18` from `tools/gen_inventory.py`, `tools/review_gates.py`,
  `tools/gen_tool_reports.py`
* deleted `reviews/tools/gates/K18.{txt,json}`

## 3. Verification

```
uv run tools/review_tool_registry.py --sources kimi-cli/src/kimi_cli/tools src/kimix/tools
  # ORPHANS 0 -> PASS
grep -rn "Mkdir\|Rm" --include=*.py src/kimix tests   # -> no live importer
```

## 4. JSON summary

```json
{
  "id": "K18",
  "tool": "Rm",
  "files": ["src/kimix/tools/file/__init__.py"],
  "verdict": "ok",
  "gates": {
    "G0_scope": "pass",
    "G1_simplicity": {"ruff": "not-run", "complexity_max": 0, "longest_fn_loc": 0,
                      "loc_regression": "none", "vulture_hits": 0, "vulture_waived": 0},
    "G2_risk": {"items_answered": 13, "open_high": 0, "open_medium": 0, "waived_medium": 0},
    "G3_coverage": {"module": "kimix.tools.file", "floor": 90, "actual": 0.0,
                    "waived_lines": 0, "waiver_budget": 25},
    "G4_behavior": {"probe_file": "n/a (deleted)", "probes_required": 0,
                    "probes_present": 0, "result": "not-run"},
    "G5_regression": {"root_suite": "pass", "cli_suite": "pass",
                      "syntax_check": "pass", "git_diff": "pass"},
    "G6_independent_rerun": "not-run"
  },
  "findings": [],
  "coverage": {"behaviors_mapped": 0, "covered": 0, "gaps": 0, "gap_list": []},
  "gate_evidence": "reviews/tools/93-orphans-and-removal.md",
  "git_sha": "a45f336be13df643142afb510f436885c92bfab7",
  "worktree_clean_at_start": true,
  "needs_fix_phase": false,
  "duplication_notes": []
}
```
