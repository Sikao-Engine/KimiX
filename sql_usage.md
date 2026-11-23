# SQLite usage after JSONL session storage

Audit baseline: commit `56025efb`; JSONL refactor implemented in the working tree.

## Runtime session storage

Session context and durable history no longer use SQLite. Root, subagent,
temporary, forked, copied, and SDK sessions use:

- `<session-dir>/context.jsonl` for the authoritative active context;
- `<session-dir>/history.jsonl` for the derived full-history archive/index.

Both files are dense UTF-8 JSONL. Every message/turn or control record occupies
one physical line. Context prompt changes, usage snapshots, checkpoints,
`_reset`, and `_revert` records are appended and replayed. History compaction
uses `_compacted` boundaries and explicit clears use `_reset`. Normal runtime
operations never prepend, truncate, or atomically rewrite these logs.

Replay maintains logical message IDs independently of physical line numbers.
Reset/revert hide obsolete records from the active view but do not securely
erase them. Physical session deletion remains the secure-erasure path. A crash
may leave a valid prefix or an incomplete final record; readers preserve valid
records and log malformed records rather than repairing the source file.

History load builds an in-memory inverted index once. Appends update only new
postings. Latin/code and CJK queries use BM25-style candidate ranking; short CJK
and unrepresentable literal queries use cached-memory substring search. Warm
searches perform no file I/O and the durable history has no 500-turn cap.

## Isolated legacy migration

`kimi-cli/src/kimi_cli/legacy_sqlite_migration.py` is the only session-storage
SQLite compatibility boundary. It uses `apsw` read-only connections before
runtime context/history storage is constructed. It reads only base `messages`,
`system_prompt`, `checkpoints`, `usage_snapshots`, or `turns` tables—never FTS
tables or triggers.

Migration writes a temporary JSONL file, flushes/fsyncs it, replays and verifies
it, then atomically activates it and renames the legacy DB to a non-active `.bak`
file. Existing canonical JSONL always wins and is never overwritten implicitly.
Context migration failure is visible and preserves the DB; derived-history
failure logs a warning. Existing subagent instances migrate during root startup.
The runtime modules `context.py`, `history_index.py`, and `kimisoul.py` do not
import the migrator or a SQLite driver.

## Intentionally retained SQLite surfaces

These components are unrelated to session context/history and are unchanged:

| Surface | Purpose |
|---|---|
| `kimi_cli/tools/broadcast/bus.py` | Cross-process broadcast bus in `.kimix_cache/_broadcast.db` |
| `kimi_cli/tools/file/read_sqlite.py` | Rich read/query support for arbitrary user-selected SQLite files |
| `bin/kimix_native/index.py` | Optional native SQLite compatibility wrapper; not used by JSONL history |
| Migration fixtures and dedicated tests | Seed/verify old DBs and approved retained surfaces |

`apsw` remains a project dependency for these surfaces and the isolated legacy
migrator. `aiosqlite` has been removed from `kimi-cli` dependencies.

## Benchmark evidence

Reference working-tree runs on Windows:

- 5,000 short turns: batch append/index **0.059 s**, cold JSONL load **0.073 s**,
  warm common-keyword search **0.00147 s**, **139.6 JSONL bytes/turn**.
- Approximately 900,000 estimated tokens (1,200 turns, 3.96 MB JSONL): append
  **1.13 s**, cold load **1.19 s**, warm Latin search **0.00723 s**, peak traced
  allocation approximately **48.7 MB**.
- `tools/mem_bench.py session_context --no-census`: 500 messages, cold restore
  **107.5 ms**, 834,227 JSONL bytes.
- `tools/mem_bench.py history_index_jsonl --no-census`: 500 larger messages,
  cold load **1.12 s**, warm Latin search **4.92 ms**, 734,175 JSONL bytes.

Benchmark timings are measurements, not cross-hardware guarantees. Test limits
are intentionally broader and cold rebuild is reported separately from warm
queries.
