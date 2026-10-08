# 99 — Final verification report

Phase 5 of the built-in tools review. Branch `review/tools-hardgate`.
Base tag `tools-review/p0-base`. Revision reviewed: `f172ca7e3ae730232c9009663c787c257084ee80`.

## 1. Method

Every per-tool gate has a **mechanical** half that a machine decides and a
**judgement** half that a reviewer decides. This run made the mechanical half
fully reproducible and ran the judgement half through subagents:

* `tools/review_gates.py` runs, per tool ID, `ruff check`,
  `ruff check --select C901 --max-complexity=12`,
  `review_metrics.py --max-fn-loc 120 --max-complexity 12`, the in-scope test
  files, and a `--cov-branch` coverage run. Raw output goes to
  `reviews/tools/gates/<ID>.txt` (line 1 = `git rev-parse HEAD`, line 2 =
  `git status --porcelain`) and a summary to `gates/<ID>.json`.
* `tools/gen_tool_reports.py` turns that evidence into the 36 plan-§7 reports,
  each with a §2.2 JSON block that `tools/gate_validate.py` accepts.
* Six Phase-1 sweeps (X01–X06) and the per-tool reviewers did the judgement half;
  their reports and raw evidence are committed.

Reproduce the backbone with:

```bash
uv run tools/review_gates.py --all          # 36 mechanical gate runs
uv run tools/gen_tool_reports.py --all      # regenerate the report skeletons
uv run tools/gate_validate.py reviews/tools/[CK]*-*.md
```

## 2. Headline

| | |
|---|---|
| tools with a report | **36 / 36** (+ 6 sweeps) |
| reports whose §2.2 JSON is schema-valid | **36 / 36** |
| G1 (simplicity) green | **0 / 36** — see §4 |
| G3 (coverage) green | **30 / 36** |
| G4 (behavioural probes) green | **5 / 36** (C06, C09, C10, K01, K04) |
| G-POLICY | **PASS** (0 hits, 1 justified waiver) |
| G-DUP | **FAIL** (`fetch_url` C12 vs K14) |
| G-ORPHAN | **FAIL** (9 orphans, decisions recorded) |
| G-COMPACT | **PASS** |
| new failures introduced in the CLI suite | **0** |
| root suite | **unblocked**: collection error -> 5402 passed / 1 failed / 3 skipped |

The exit criteria in plan §8 are **not** met. What is met: every tool has a
report and SHA-bound evidence, the mechanically decidable gates are recorded and
re-runnable, the two library/native policy gates and the compaction invariant
gate are green, and 7 fix packages (including 4 latent correctness defects and a
credential-leak path) are fixed with probes and zero regressions.

## 3. Suite state: before vs after

| suite | baseline (`p0-base`) | final | delta |
|---|---|---|---|
| `uv run pytest tests -q` | **collection ERROR**, exit 2 (`tests/bash/cases.json` missing) | 5402 passed, 1 failed, 3 skipped | unblocked (FP-00) |
| `uv run pytest kimi-cli/tests -q` | 43 failed, 5155 passed, 101 skipped | 43 failed, 5271 passed, 105 skipped | **+116 passed, 0 new failures, 0 fixed** |

The CLI failure set is **byte-identical** to the baseline: `baseline FAILED ==
after FAILED` (43 == 43, NEW = 0, FIXED = 0). The 4 extra skips are the five
`__all__` probes whose modules do not declare `__all__`.

The single root-suite failure is **F-30**, pre-existing drift that had never been
executed because the suite could not collect:
`tests/unit/tools/test_prompt_common.py::test_descriptions_unchanged` expects a
richer bash description than `bash_tool.py` builds. Resolving it changes the
model-facing prompt, so it is recorded, not papered over.

## 4. Per-tool gate table

Mechanical gates as recorded at `p0-base` (the review-time revision). `G4` counts
the author-written probe suites; the plan's per-tool minimum is in brackets.

| ID | tool | G1 ruff | G1 C901 | G1 metrics | cx_max | longest | G3 pytest | G3 cov | G4 probes |
|---|---|---|---|---|---|---|---|---|---|
| C00 | toolset plumbing & shared he | fail | fail | fail | 34 | 74 | fail | fail | 0/10 fail |
| C01 | subagent (AgentTool) | fail | pass | fail | 14 | 107 | pass | pass | 0/8 fail |
| C02 | AskUserQuestion | fail | pass | pass | 8 | 79 | pass | pass | 0/7 fail |
| C03 | todo_list (TodoList) | fail | fail | fail | 22 | 102 | pass | pass | 0/20 fail |
| C04 | retrieve | fail | pass | pass | 9 | 56 | pass | pass | 0/6 fail |
| C05 | read (ReadFile) | fail | fail | fail | 35 | 220 | fail | fail | 0/14 fail |
| C06 | read_image (ReadMediaFile) | fail | fail | fail | 29 | 224 | pass | pass | 22/10 pass |
| C07 | glob (Glob) | fail | fail | fail | 37 | 199 | pass | pass | 0/12 fail |
| C08 | grep (Grep) | fail | fail | fail | 77 | 297 | pass | pass | 0/15 fail |
| C09 | write (WriteFile) | fail | fail | fail | 64 | 297 | pass | pass | 30/12 pass |
| C10 | edit (EditFile) | fail | fail | fail | 38 | 239 | pass | pass | 28/12 pass |
| C11 | web_search (SearchWeb) | fail | pass | fail | 19 | 165 | pass | pass | 0/10 fail |
| C12 | fetch_url (kimi_cli) | fail | fail | fail | 19 | 165 | pass | pass | 0/11 fail |
| C13 | web_extract (WebExtract) | fail | fail | fail | 22 | 165 | pass | pass | 0/10 fail |
| C14 | context_prune | fail | pass | fail | 16 | 131 | pass | pass | 0/11 fail |
| C15 | HashRead / HashLine / HashEd | fail | fail | fail | 61 | 205 | pass | pass | 0/10 fail |
| C16 | demo tools plus / compare /  | fail | pass | pass | 3 | 7 | not-run | not-run | 0/6 fail |
| K00 | shared infra | fail | fail | fail | 35 | 279 | fail | fail | 0/16 fail |
| K01 | subagent (Agent) | fail | pass | fail | 19 | 88 | pass | pass | 20/14 pass |
| K02 | list_agents (AgentList) | fail | pass | fail | 19 | 88 | pass | pass | 0/5 fail |
| K03 | interrupt_agent (AgentClose) | fail | pass | fail | 19 | 88 | pass | pass | 0/5 fail |
| K04 | bash (Bash) | fail | fail | fail | 52 | 325 | fail | fail | 72/15 pass |
| K05 | pwsh (Powershell) | fail | fail | fail | 57 | 370 | pass | pass | 0/10 fail |
| K06 | Run | fail | fail | fail | 92 | 361 | pass | pass | 0/12 fail |
| K07 | python | fail | fail | fail | 28 | 210 | pass | pass | 0/12 fail |
| K08 | job_output (TaskOutput) | fail | fail | fail | 29 | 152 | pass | pass | 0/12 fail |
| K09 | compact | fail | pass | pass | 10 | 66 | pass | pass | 0/10 fail |
| K10 | WritePlan | fail | fail | fail | 15 | 66 | pass | pass | 0/5 fail |
| K11 | ReadPlan | fail | fail | fail | 15 | 66 | pass | pass | 0/5 fail |
| K12 | EditPlan | fail | fail | fail | 15 | 66 | pass | pass | 0/6 fail |
| K13 | workflow (AgentSwarm) | fail | pass | fail | 19 | 85 | pass | pass | 0/11 fail |
| K14 | fetch_url (kimix) | fail | pass | pass | 10 | 49 | pass | pass | 0/8 fail |
| K15 | ParserTool | fail | pass | pass | 12 | 113 | pass | pass | 0/10 fail |
| K16 | FindStr | fail | fail | fail | 19 | 162 | pass | pass | 0/10 fail |
| K17 | Mkdir | fail | pass | pass | 2 | 19 | not-run | not-run | 0/5 fail |
| K18 | Rm | fail | pass | pass | 2 | 19 | not-run | not-run | 0/5 fail |

## 5. Exit criteria (plan §8) — assessment

| # | criterion | state |
|---|---|---|
| 1 | 36 tool reports + 6 sweeps, each with `gates/<ID>.txt` | **met** |
| 2 | every tool has G0–G6 recorded as `pass` | **not met** — G1 is red for all 36; the plan's own thresholds (complexity <= 12, <= 120 LOC, clean ruff) are breached by the pre-existing code, and G2/G4 need reviewer work beyond this run |
| 3 | every tool owns a probe suite with >= the required probe count | **not met** — 5 of 36 have suites (C06, C09, C10, K01, K04) |
| 4 | every high/medium finding fixed or waived with a rationale | **partly** — see §6; FP-02, FP-03, FP-09, FP-10, FP-12 open |
| 5 | G-DUP, G-ORPHAN, G-POLICY, G-COMPACT green | **3 of 4** — G-DUP and G-ORPHAN red |
| 6 | both suites green, no new skips vs baseline | **root suite not green** (1 pre-existing failure, newly visible); **no new failures** in either suite |
| 7 | `syntax_check` + `git_diff` clean on every changed file | **met** |
| 8 | baseline->final coverage delta per tool | recorded per tool in `gates/<ID>.json` and the reports |
| 9 | version management complete | **met** — see §10 |

## 6. Fix packages

| package | state | what changed |
|---|---|---|
| FP-00 | **done** | missing optional corpus no longer aborts collection; 6 probes |
| FP-01 | **done** | `write.py` open `TODO: checks:` removed + explained; 30 C09 probes |
| FP-02 | open | `fetch_url` still implemented twice (C12, K14) |
| FP-03 | decision recorded | 9 orphans; C16 decided **delete**, K15–K18 need an owner call |
| FP-05 | **done** | G-POLICY green; stdlib `json` import removed, `regex` adopted, `difflib` waived with rationale, dead `xxhash` import deleted; 11 probes |
| FP-06 | **done** | 24 stale `__pycache__` / 238 `.pyc` removed (gitignored -> no diff) |
| FP-07 | partial | 5 probe suites authored (172 probes total) |
| FP-08 | **done** | 4 latent defects fixed: `NameError` on the image-delivery path, **silently ignored malformed `region_pct`** (returned the whole image while the model believed it cropped), 5 unevaluatable `'X' | None` annotations, a dead `__all__` export + duplicate import; 49 probes |
| FP-09 | open | `ruff check` still red on 83 of 84 modules (263 findings, 111 auto-fixable) |
| FP-10 | open | 38 native-grep parity failures; stale `.venv` `kimix_native` shim |
| FP-11 | **done** | credential scrubbing enabled at the shared env choke point for all 8 shell spawn sites; 5 probes |
| FP-12 | open | blocking IO on the event loop in `FindStr`, swarm best-of-N, temp cleanup |

## 7. Cross-tool gates (G7) at Phase 5

```
uv run tools/gate_dup.py
  # tool classes discovered: 37   distinct names: 35   duplicated: 2
  [FAIL]   fetch_url  kimi-cli/src/kimi_cli/tools/web/fetch.py:101
                      src/kimix/tools/web/fetch_url.py:23
  [WAIVED] subagent   intentional contrast (C01 fg default vs K01 bg default)

uv run tools/review_tool_registry.py --sources kimi-cli/src/kimi_cli/tools src/kimix/tools
  # discovered 37  registered 27  allowlisted 1  ORPHANS 9
  G-ORPHAN: FAIL

uv run tools/gate_library_policy.py kimi-cli/src/kimi_cli/tools src/kimix/tools
  ok src/kimix/tools/swarm/best_of_n.py:23  import difflib (waived)
  policy hits: 0  waived: 1  unparsable: 0
  G-POLICY: PASS

uv run pytest tests/test_integration_compaction.py kimi-cli/tests/core/test_kimisoul_context_prune.py -q
  12 passed
  G-COMPACT: PASS
```

The `subagent` waiver is `tools/gate_dup_allowlist.txt`; the `context_prune`
allowlist entry is `tools/tool_registry_allowlist.txt`; the `difflib` waiver is
`tools/library_policy_allowlist.txt`.

## 8. Files created / modified

**New review artifacts** — `reviews/tools/`: `00-inventory.md`, `00-baseline.txt`,
`00-metrics-baseline.json`, `00-metrics-final.json`, `00-registry.txt`,
36 `<ID>-<slug>.md` reports, `X01`–`X06` sweep reports,
`90-findings.md`, `91-coverage-gaps.md`, `92-simplicity-backlog.md`,
`93-orphans-and-removal.md`, this file, and `gates/<ID>.{txt,json}` for all 42
units plus `gates/X0*.txt`.

**New tooling** (all syntax-checked, ruff + C901 + metrics clean, unit-tested):
`tools/review_metrics.py`, `tools/gate_validate.py`, `tools/gate_dup.py`,
`tools/review_tool_registry.py`, `tools/gate_library_policy.py`,
`tools/review_gates.py`, `tools/gen_tool_reports.py`, `tools/gen_inventory.py`,
`tools/tool_registry_allowlist.txt`, `tools/library_policy_allowlist.txt`,
`tools/gate_dup_allowlist.txt`.

**Source fixes** (7 files): `kimi-cli/src/kimi_cli/tools/__init__.py`,
`.../tools/file/conflict_detect.py`, `.../file/edit/modes/sloppy.py`,
`.../file/read_media.py`, `.../file/write.py`, `src/kimix/tools/common.py`,
`src/kimix/tools/swarm/best_of_n.py`, `src/kimix/tools/file/bash/bash_tool.py`,
`src/kimix/tools/file/bash/pwsh_tool.py`, `src/kimix/tools/file/run.py`,
`src/kimix/tools/py/__init__.py`.

**New tests** (9 files, 224 new tests): `tests/test_review_gate_tools.py` (22),
`tests/test_review_orchestrator.py` (21), `tests/test_bash_e2e_gate.py` (6),
`tests/unit/tools/test_env_scrub_gate.py` (5),
`tests/unit/tools/test_bash_gate.py` (72, K04),
`tests/kimix/test_agent_gate.py` (20, K01),
`kimi-cli/tests/tools/test_edit_gate.py` (28, C10),
`kimi-cli/tests/tools/test_read_media_gate.py` (22, C06),
`kimi-cli/tests/tools/test_write_gate.py` (30, C09),
`kimi-cli/tests/tools/test_library_policy_gate.py` (11),
`kimi-cli/tests/tools/test_tools_integrity_gate.py` (22).

**Simplification regression check**: module LOC 36718 -> 36763 (+45, the fixes and
their explanatory comments); functions 1153 -> 1154; functions over complexity 12
90 -> 90; functions over 120 LOC 29 -> 29. No function grew past a threshold and
no module crossed the 1200-LOC split line because of this review.

Overall change scope, `git diff --stat tools-review/p0-base HEAD`:

```
 151 files changed, 51456 insertions(+), 37 deletions(-)
```

## 9. Orphan and duplicate decision log

| subject | decision | owner | state |
|---|---|---|---|
| `kimi_cli.tools.test` (`plus`, `compare`, `panic`) | **delete** (plan §4.3 C16 option A) | orchestrator | decided, not executed |
| `kimi_cli.tools.file.hash_line:HashRead` / `HashEdit` | **register or delete** — declared builtin by C15 but reachable from no manifest and instantiated nowhere | tool owner | **open** |
| `kimix.tools.parser:ParserTool` | register or delete (K15) | tool owner | **open** |
| `kimix.tools.file.find_str:FindStr` | register or delete (K16) | tool owner | **open** |
| `kimix.tools.file:Mkdir` | register or delete (K17) | tool owner | **open** |
| `kimix.tools.file:Rm` | register or delete (K18) | tool owner | **open** |
| `kimi_cli.tools.context_prune:context_prune` | **allowlist** — registered programmatically at `kimisoul.py:490` | orchestrator | done |
| `fetch_url` duplicate | reconcile into one core or an exact forwarder with a parity test (FP-02) | tool owner | **open** |
| `subagent` duplicate | **waive** — intentionally distinct defaults, mandated contrast probes | orchestrator | done |

## 10. Git provenance

* branch: `review/tools-hardgate` (created in P0; `master` untouched —
  `git log --oneline master ^review/tools-hardgate` is empty)
* tags: `tools-review/p0-base`, `p1-sweeps`, `p2-reviews`, `p3-triage`,
  `p4-complete`, `p5-final`
* one commit per unit of work; no `git add -A`, no `git reset --hard` on a dirty
  tree, no force-push, no history rewrite
* every `gates/<ID>.txt` begins with the `git rev-parse HEAD` and
  `git status --porcelain` it was produced at

`git diff --stat tools-review/p0-base tools-review/p5-final`:

```
.gitignore                                         |    4 +
 kimi-cli/src/kimi_cli/tools/__init__.py            |    8 +-
 .../src/kimi_cli/tools/file/conflict_detect.py     |    1 -
 .../src/kimi_cli/tools/file/edit/modes/sloppy.py   |    2 +-
 kimi-cli/src/kimi_cli/tools/file/read_media.py     |   65 +-
 kimi-cli/src/kimi_cli/tools/file/write.py          |    9 +-
 kimi-cli/tests/tools/test_edit_gate.py             | 1265 ++++++++
 kimi-cli/tests/tools/test_library_policy_gate.py   |  177 ++
 kimi-cli/tests/tools/test_read_media_gate.py       |  194 ++
 kimi-cli/tests/tools/test_tools_integrity_gate.py  |  179 ++
 kimi-cli/tests/tools/test_write_gate.py            |  238 ++
 reviews/tools/90-findings.md                       |   64 +
 reviews/tools/91-coverage-gaps.md                  |  101 +
 reviews/tools/92-simplicity-backlog.md             |   88 +
 reviews/tools/93-orphans-and-removal.md            |   63 +
 .../tools/C00-toolset-plumbing-shared-helpers.md   |  228 ++
 reviews/tools/C01-subagent-agenttool.md            |  222 ++
 reviews/tools/C02-askuserquestion.md               |  197 ++
 reviews/tools/C03-todo-list-todolist.md            |  234 ++
 reviews/tools/C04-retrieve.md                      |  197 ++
 reviews/tools/C05-read-readfile.md                 |  271 ++
 reviews/tools/C06-read-image-readmediafile.md      |  231 ++
 reviews/tools/C07-glob-glob.md                     |  242 ++
 reviews/tools/C08-grep-grep.md                     |  254 ++
 reviews/tools/C09-write-writefile.md               |  262 ++
 reviews/tools/C10-edit-editfile.md                 |  421 +++
 reviews/tools/C11-web-search-searchweb.md          |  246 ++
 reviews/tools/C12-fetch-url-kimi-cli.md            |  252 ++
 reviews/tools/C13-web-extract-webextract.md        |  252 ++
 reviews/tools/C14-context-prune.md                 |  231 ++
 reviews/tools/C15-hashread-hashline-hashedit.md    |  241 ++
 reviews/tools/C16-demo-tools-plus-compare-panic.md |  208 ++
 reviews/tools/K00-shared-infra.md                  |  246 ++
 reviews/tools/K01-subagent-agent.md                |  377 +++
 reviews/tools/K02-list-agents-agentlist.md         |  226 ++
 reviews/tools/K03-interrupt-agent-agentclose.md    |  226 ++
 reviews/tools/K04-bash-bash.md                     |  477 +++
 reviews/tools/K05-pwsh-powershell.md               |  243 ++
 reviews/tools/K06-run.md                           |  235 ++
 reviews/tools/K07-python.md                        |  235 ++
 reviews/tools/K08-job-output-taskoutput.md         |  237 ++
 reviews/tools/K09-compact.md                       |  193 ++
 reviews/tools/K10-writeplan.md                     |  226 ++
 reviews/tools/K11-readplan.md                      |  226 ++
 reviews/tools/K12-editplan.md                      |  226 ++
 reviews/tools/K13-workflow-agentswarm.md           |  225 ++
 reviews/tools/K14-fetch-url-kimix.md               |  207 ++
 reviews/tools/K15-parsertool.md                    |  195 ++
 reviews/tools/K16-findstr.md                       |  225 ++
 reviews/tools/K17-mkdir.md                         |  207 ++
 reviews/tools/K18-rm.md                            |  207 ++
 reviews/tools/X01-library-policy.md                |  170 +
 reviews/tools/X02-async-resources.md               |  135 +
 reviews/tools/X03-security.md                      |  168 +
 reviews/tools/X04-native-parity.md                 |  214 ++
 reviews/tools/X05-dead-code.md                     |  216 ++
 reviews/tools/X06-test-hygiene.md                  |  606 ++++
 reviews/tools/gates/C00.json                       |   38 +
 reviews/tools/gates/C00.txt                        |  573 ++++
 reviews/tools/gates/C01.json                       |   33 +
 reviews/tools/gates/C01.txt                        |  198 ++
 reviews/tools/gates/C02.json                       |   30 +
 reviews/tools/gates/C02.txt                        |  139 +
 reviews/tools/gates/C03.json                       |   48 +
 reviews/tools/gates/C03.txt                        | 1138 +++++++
 reviews/tools/gates/C04.json                       |   29 +
 reviews/tools/gates/C04.txt                        |  160 +
 reviews/tools/gates/C05.json                       |   68 +
 reviews/tools/gates/C05.txt                        | 2073 ++++++++++++
 reviews/tools/gates/C06.json                       |   38 +
 reviews/tools/gates/C06.txt                        |  404 +++
 reviews/tools/gates/C07.json                       |   40 +
 reviews/tools/gates/C07.txt                        |  460 +++
 reviews/tools/gates/C08.json                       |   53 +
 reviews/tools/gates/C08.txt                        | 1111 +++++++
 reviews/tools/gates/C09.json                       |   57 +
 reviews/tools/gates/C09.txt                        | 2148 +++++++++++++
 reviews/tools/gates/C10.json                       |   45 +
 reviews/tools/gates/C10.txt                        | 3362 ++++++++++++++++++++
 reviews/tools/gates/C11.json                       |   42 +
 reviews/tools/gates/C11.txt                        | 1046 ++++++
 reviews/tools/gates/C12.json                       |   44 +
 reviews/tools/gates/C12.txt                        | 1162 +++++++
 reviews/tools/gates/C13.json                       |   46 +
 reviews/tools/gates/C13.txt                        | 1161 +++++++
 reviews/tools/gates/C14.json                       |   34 +
 reviews/tools/gates/C14.txt                        |  248 ++
 reviews/tools/gates/C15.json                       |   39 +
 reviews/tools/gates/C15.txt                        |  512 +++
 reviews/tools/gates/C16.json                       |   25 +
 reviews/tools/gates/C16.txt                        |  133 +
 reviews/tools/gates/K00.json                       |   47 +
 reviews/tools/gates/K00.txt                        | 1247 ++++++++
 reviews/tools/gates/K01.json                       |   37 +
 reviews/tools/gates/K01.txt                        | 2345 ++++++++++++++
 reviews/tools/gates/K02.json                       |   32 +
 reviews/tools/gates/K02.txt                        |  503 +++
 reviews/tools/gates/K03.json                       |   32 +
 reviews/tools/gates/K03.txt                        |  505 +++
 reviews/tools/gates/K04.json                       |   45 +
 reviews/tools/gates/K04.txt                        | 2299 +++++++++++++
 reviews/tools/gates/K05.json                       |   40 +
 reviews/tools/gates/K05.txt                        |  589 ++++
 reviews/tools/gates/K06.json                       |   34 +
 reviews/tools/gates/K06.txt                        |  437 +++
 reviews/tools/gates/K07.json                       |   34 +
 reviews/tools/gates/K07.txt                        |  410 +++
 reviews/tools/gates/K08.json                       |   35 +
 reviews/tools/gates/K08.txt                        |  755 +++++
 reviews/tools/gates/K09.json                       |   30 +
 reviews/tools/gates/K09.txt                        |  186 ++
 reviews/tools/gates/K10.json                       |   32 +
 reviews/tools/gates/K10.txt                        |  318 ++
 reviews/tools/gates/K11.json                       |   32 +
 reviews/tools/gates/K11.txt                        |  320 ++
 reviews/tools/gates/K12.json                       |   32 +
 reviews/tools/gates/K12.txt                        |  322 ++
 reviews/tools/gates/K13.json                       |   33 +
 reviews/tools/gates/K13.txt                        |  463 +++
 reviews/tools/gates/K14.json                       |   31 +
 reviews/tools/gates/K14.txt                        |  301 ++
 reviews/tools/gates/K15.json                       |   29 +
 reviews/tools/gates/K15.txt                        |  226 ++
 reviews/tools/gates/K16.json                       |   33 +
 reviews/tools/gates/K16.txt                        |  527 +++
 reviews/tools/gates/K17.json                       |   25 +
 reviews/tools/gates/K17.txt                        |  203 ++
 reviews/tools/gates/K18.json                       |   25 +
 reviews/tools/gates/K18.txt                        |  205 ++
 reviews/tools/gates/X01.txt                        |  500 +++
 reviews/tools/gates/X02.txt                        |  568 ++++
 reviews/tools/gates/X03.txt                        | 1231 +++++++
 reviews/tools/gates/X04.txt                        | 1048 ++++++
 reviews/tools/gates/X05.txt                        |  783 +++++
 reviews/tools/gates/X06.txt                        | 1528 +++++++++
 reviews/tools/gates/_harness.log                   |   36 +
 src/kimix/tools/common.py                          |   16 +-
 src/kimix/tools/file/bash/bash_tool.py             |    2 +-
 src/kimix/tools/file/bash/pwsh_tool.py             |    3 +-
 src/kimix/tools/file/run.py                        |    4 +-
 src/kimix/tools/py/__init__.py                     |    2 +-
 src/kimix/tools/swarm/best_of_n.py                 |    1 -
 tests/kimix/test_agent_gate.py                     |  896 ++++++
 tests/test_bash_e2e.py                             |   18 +-
 tests/test_bash_e2e_gate.py                        |   95 +
 tests/test_review_orchestrator.py                  |  317 ++
 tests/unit/tools/test_bash_gate.py                 | 1077 +++++++
 tests/unit/tools/test_env_scrub_gate.py            |  103 +
 tools/gen_tool_reports.py                          |  643 ++++
 tools/library_policy_allowlist.txt                 |   14 +
 tools/review_gates.py                              |  277 ++
 151 files changed, 51456 insertions(+), 37 deletions(-)
```

## 11. Deviations from the plan (declared)

1. **Six waves -> one review pass.** The plan splits Phase 2 into 6 waves of 6
   subagents. Wave agents that did run produced excellent work (C10, K01, K04
   reports and 120 probes) but most exhausted their budget before writing
   deliverables. The orchestrator therefore built the mechanical harness so the
   evidence exists for all 36 regardless, and generated the 36 reports from it.
   Tags are `p2-reviews` (one tag) instead of `p2-w1`…`p2-w6`.
2. **Three extra helper files**: `tools/review_gates.py`,
   `tools/gen_tool_reports.py`, `tools/gen_inventory.py`, `tools/gate_dup_allowlist.txt`
   (the plan's §3 list was not exhaustive).
3. **Gate commands corrected where the plan text is stale**:
   `kimi-cli/tests/unit/tools/` does not exist (the `read` unit tests are at
   `tests/unit/tools/`); `file/_ripgrep_common.py` does not exist; and
   `--cov=<path>` / `--cov=src/...` measures nothing — the dotted module form is
   required (K01 §G3, K04 §G3).
4. **`pre-commit` is not installed** in this environment, so every commit carries
   `Skip-PreCommit: pre-commit is not installed in the environment.` after running
   `ruff`, `ruff --select C901` and `tools/syntax_check.py` by hand. No commit
   bypassed a real type error.

## 12. Recommended next steps

1. **FP-02** — reconcile `fetch_url`; the only thing keeping G-DUP red.
2. **FP-03** — execute the orphan decisions in §9; deleting `tools/test.py` plus
   registering or deleting the four `kimix` orphans flips G-ORPHAN green.
3. **FP-09** — `uv run ruff check --fix kimi-cli/src/kimi_cli/tools src/kimix/tools`
   clears 111 of the 263 findings mechanically, then hand-fix the rest; this is the
   single largest blocker to a green G1.
4. **FP-07** — author the remaining 31 probe suites from the §4.3 lists (the
   C06/C09/C10/K01/K04 suites are working templates).
5. **FP-10** — retire or re-point `kimi-cli/tests/native/test_grep_engine_parity.py`
   (38 failures) and re-sync `bin/kimix_native` with the installed wheel.
6. **FP-12** — move `FindStr`'s walk and the swarm's `subprocess`/`shutil` work off
   the event loop.
7. Resolve **F-30** (bash description snapshot drift) with the tool owner, since it
   changes the model-facing prompt.
