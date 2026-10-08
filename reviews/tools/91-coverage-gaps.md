# 91 — Coverage gaps

Source: `reviews/tools/gates/<ID>.json` (mechanical harness) and `00-metrics-baseline.json`.

`G4` requires a *named probe suite* per tool with a per-tool minimum probe count.
**No probe suite existed at review time for 33 of the 36 tools**, so every one of those
`G4_behavior` gates is red. Three suites were authored during this review:

| tool | probe file | required | present | state |
|---|---|---|---|---|
| C00 | `kimi-cli/tests/tools/test_tools_init_gate.py` | 10 | 0 | **missing** |
| C01 | `kimi-cli/tests/tools/test_agent_tool_gate.py` | 8 | 0 | **missing** |
| C02 | `kimi-cli/tests/tools/test_ask_user_gate.py` | 7 | 0 | **missing** |
| C03 | `kimi-cli/tests/tools/test_todo_gate.py` | 20 | 0 | **missing** |
| C04 | `kimi-cli/tests/tools/test_memory_retrieve_gate.py` | 6 | 0 | **missing** |
| C05 | `kimi-cli/tests/tools/test_read_gate.py` | 14 | 0 | **missing** |
| C06 | `kimi-cli/tests/tools/test_read_media_gate.py` | 10 | 0 | **missing** |
| C07 | `kimi-cli/tests/tools/test_glob_gate.py` | 12 | 0 | **missing** |
| C08 | `kimi-cli/tests/tools/test_grep_gate.py` | 15 | 0 | **missing** |
| C09 | `kimi-cli/tests/tools/test_write_gate.py` | 12 | 0 | **missing** |
| C10 | `kimi-cli/tests/tools/test_edit_gate.py` | 12 | 0 | **missing** |
| C11 | `kimi-cli/tests/tools/test_web_search_gate.py` | 10 | 0 | **missing** |
| C12 | `kimi-cli/tests/tools/test_fetch_url_gate.py` | 11 | 0 | **missing** |
| C13 | `kimi-cli/tests/tools/test_web_extract_gate.py` | 10 | 0 | **missing** |
| C14 | `kimi-cli/tests/tools/test_context_prune_gate.py` | 11 | 0 | **missing** |
| C15 | `kimi-cli/tests/tools/test_hash_line_gate.py` | 10 | 0 | **missing** |
| C16 | `kimi-cli/tests/tools/test_demo_tools.py` | 6 | 0 | **missing** |
| K00 | `tests/unit/tools/test_common_gate.py` | 16 | 0 | **missing** |
| K01 | `tests/kimix/test_agent_gate.py` | 14 | 0 | **missing** |
| K02 | `tests/kimix/test_agent_list_gate.py` | 5 | 0 | **missing** |
| K03 | `tests/kimix/test_agent_close_gate.py` | 5 | 0 | **missing** |
| K04 | `tests/unit/tools/test_bash_gate.py` | 15 | 0 | **missing** |
| K05 | `tests/unit/tools/test_pwsh_gate.py` | 10 | 0 | **missing** |
| K06 | `tests/unit/tools/test_run_gate.py` | 12 | 0 | **missing** |
| K07 | `tests/unit/tools/test_python_gate.py` | 12 | 0 | **missing** |
| K08 | `tests/unit/tools/test_job_output_gate.py` | 12 | 0 | **missing** |
| K09 | `tests/unit/tools/test_compact_gate.py` | 10 | 0 | **missing** |
| K10 | `tests/unit/tools/test_note_gate.py` | 5 | 0 | **missing** |
| K11 | `tests/unit/tools/test_note_gate.py` | 5 | 0 | **missing** |
| K12 | `tests/unit/tools/test_note_gate.py` | 6 | 0 | **missing** |
| K13 | `tests/unit/tools/test_swarm_gate.py` | 11 | 0 | **missing** |
| K14 | `tests/unit/tools/test_fetchurl_gate.py` | 8 | 0 | **missing** |
| K15 | `tests/unit/tools/test_parser_gate.py` | 10 | 0 | **missing** |
| K16 | `tests/unit/tools/test_find_str_gate.py` | 10 | 0 | **missing** |
| K17 | `tests/unit/tools/test_file_ops_gate.py` | 5 | 0 | **missing** |
| K18 | `tests/unit/tools/test_file_ops_gate.py` | 5 | 0 | **missing** |

### Authored during the review

| tool | probe file | probes | result |
|---|---|---|---|
| C06 | `kimi-cli/tests/tools/test_read_media_gate.py` | 22 | pass |
| C10 | `kimi-cli/tests/tools/test_edit_gate.py` | 28 | pass |
| FP-08 | `kimi-cli/tests/tools/test_tools_integrity_gate.py` | 22 (+5 skip) | pass |

### Coverage (G3) result per tool

| tool | cov module | coverage line | floor | gate |
|---|---|---|---|---|
| C00 | `kimi_cli.tools` | `no line recorded` | 90 | fail |
| C01 | `kimi_cli.tools.agent` | `no line recorded` | 90 | pass |
| C02 | `kimi_cli.tools.ask_user` | `no line recorded` | 95 | pass |
| C03 | `kimi_cli.tools.todo` | `no line recorded` | 90 | pass |
| C04 | `kimi_cli.tools.memory` | `no line recorded` | 95 | pass |
| C05 | `kimi_cli.tools.file.read` | `no line recorded` | 85 | fail |
| C06 | `kimi_cli.tools.file.read_media` | `no line recorded` | 85 | pass |
| C07 | `kimi_cli.tools.file.glob` | `no line recorded` | 85 | pass |
| C08 | `kimi_cli.tools.file.grep_local` | `no line recorded` | 80 | pass |
| C09 | `kimi_cli.tools.file.write` | `no line recorded` | 85 | pass |
| C10 | `kimi_cli.tools.file.edit` | `no line recorded` | 85 | pass |
| C11 | `kimi_cli.tools.web.search` | `no line recorded` | 80 | pass |
| C12 | `kimi_cli.tools.web.fetch` | `no line recorded` | 90 | pass |
| C13 | `kimi_cli.tools.web.extract` | `no line recorded` | 90 | pass |
| C14 | `kimi_cli.tools.context_prune` | `no line recorded` | 90 | pass |
| C15 | `kimi_cli.tools.file.hash_line` | `no line recorded` | 85 | pass |
| C16 | `kimi_cli.tools.test` | `no line recorded` | 95 | not-run |
| K00 | `kimix.tools.common` | `no line recorded` | 80 | fail |
| K01 | `kimix.tools.agent` | `no line recorded` | 85 | pass |
| K02 | `kimix.tools.agent` | `no line recorded` | 85 | pass |
| K03 | `kimix.tools.agent` | `no line recorded` | 85 | pass |
| K04 | `kimix.tools.file.bash.bash_tool` | `no line recorded` | 80 | fail |
| K05 | `kimix.tools.file.bash.pwsh_tool` | `no line recorded` | 85 | pass |
| K06 | `kimix.tools.file.run` | `no line recorded` | 90 | pass |
| K07 | `kimix.tools.py` | `no line recorded` | 85 | pass |
| K08 | `kimix.tools.background` | `no line recorded` | 85 | pass |
| K09 | `kimix.tools.context` | `no line recorded` | 90 | pass |
| K10 | `kimix.tools.note` | `no line recorded` | 90 | pass |
| K11 | `kimix.tools.note` | `no line recorded` | 90 | pass |
| K12 | `kimix.tools.note` | `no line recorded` | 90 | pass |
| K13 | `kimix.tools.swarm` | `no line recorded` | 85 | pass |
| K14 | `kimix.tools.web.fetch_url` | `no line recorded` | 90 | pass |
| K15 | `kimix.tools.parser` | `no line recorded` | 85 | pass |
| K16 | `kimix.tools.file.find_str` | `no line recorded` | 85 | pass |
| K17 | `kimix.tools.file` | `no line recorded` | 90 | not-run |
| K18 | `kimix.tools.file` | `no line recorded` | 90 | not-run |

### Tests missing entirely

* **K17 (`Mkdir`), K18 (`Rm`)** — `tools/gen_inventory.py` declares no test glob for either; FP-07.
* **C16 (`plus`/`compare`/`panic`)** — no tests; the plan's decision gate applies (delete or keep+test).
* **K02/K03** — no dedicated test files; their behaviour is only covered indirectly through K01's tests.
