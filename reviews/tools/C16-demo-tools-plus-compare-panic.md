# C16 — demo tools `plus` / `compare` / `panic` review

## 0. Gate summary

| Gate | Result | Evidence |
|---|---|---|
| G0 scope | **pass (decision executed)** | plan §4.3 C16 option **A: delete** |
| G1 simplicity | n/a | file removed |
| G2 risk | n/a | file removed |
| G3 coverage | n/a | no tests existed (plan §1.1 C16: "**none**") |
| G4 behavior | n/a | no probe suite required once deleted |
| G5 regression | pass | `kimi-cli/tests/utils/test_pyinstaller_utils.py` updated; full CLI suite unchanged |
| G6 independent rerun | n/a | |

## 1. Decision

The plan's C16 gate offers exactly two terminal states: **(A) delete** the file and
prove `grep -rn "kimi_cli.tools.test"` is empty, or **(B) keep + test** it with a
>= 6-probe suite. Option **A** was chosen:

* the module contained three debug/demo tools (`plus` adds two floats, `compare`
  compares two floats, `panic` sleeps 2 s and raises) with **no tests, no
  registration in any agent manifest, and no documentation**;
* `uv run tools/review_tool_registry.py` reported all three as ORPHANs, and
  `grep -rn "kimi_cli.tools.test"` found exactly one live reference — a
  pyinstaller `hiddenimports` string list;
* keeping them under option B would have required a 95 %-coverage probe suite for
  code with no consumer.

## 2. Change

* deleted `kimi-cli/src/kimi_cli/tools/test.py`
* removed `"kimi_cli.tools.test",` from the hiddenimports list in
  `kimi-cli/tests/utils/test_pyinstaller_utils.py:77`
* removed `C16` from the review inventory (`tools/gen_inventory.py`), the coverage
  manifest (`tools/review_gates.py`) and the probe manifest
  (`tools/gen_tool_reports.py`)
* deleted `reviews/tools/gates/C16.{txt,json}`

## 3. Verification

```
grep -rn "kimi_cli.tools.test" kimi-cli/src src tests tools   # -> no hits
uv run tools/review_tool_registry.py --sources kimi-cli/src/kimi_cli/tools src/kimix/tools
  # discovered 32   registered 29   allowlisted 3   ORPHANS 0   -> PASS
uv run pytest kimi-cli/tests/utils/test_pyinstaller_utils.py -q   # pass
```

## 4. JSON summary

```json
{
  "id": "C16",
  "tool": "demo tools plus / compare / panic",
  "files": ["kimi-cli/src/kimi_cli/tools/test.py"],
  "verdict": "ok",
  "gates": {
    "G0_scope": "pass",
    "G1_simplicity": {"ruff": "not-run", "complexity_max": 0, "longest_fn_loc": 0,
                      "loc_regression": "none", "vulture_hits": 0, "vulture_waived": 0},
    "G2_risk": {"items_answered": 13, "open_high": 0, "open_medium": 0, "waived_medium": 0},
    "G3_coverage": {"module": "kimi_cli.tools.test", "floor": 95, "actual": 0.0,
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
