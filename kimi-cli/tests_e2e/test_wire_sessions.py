from __future__ import annotations

import json
from pathlib import Path

import xxhash
from inline_snapshot import snapshot
from tests_e2e.wire_helpers import (
    build_approval_response,
    build_shell_tool_call,
    collect_until_response,
    make_home_dir,
    make_work_dir,
    send_initialize,
    share_dir,
    start_wire,
    summarize_messages,
    write_scripted_config,
    write_shell_agent_file,
)


def _session_dir(home_dir: Path, work_dir: Path) -> Path:
    # Mirrors WorkDirMeta.sessions_dir (xxh64 of the work-dir path).
    digest = xxhash.xxh64(str(work_dir).encode("utf-8")).hexdigest()
    return share_dir(home_dir) / "sessions" / digest


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return len(path.read_text(encoding="utf-8").splitlines())


def _count_db_entries(db_path: Path) -> int:
    """Count logical context entries in a context.db (messages + system_prompt + checkpoints)."""
    if not db_path.exists():
        return 0
    import apsw

    conn = apsw.Connection(str(db_path))
    try:
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        total += conn.execute("SELECT COUNT(*) FROM system_prompt").fetchone()[0]
        total += conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
        return total
    finally:
        conn.close()


def _read_roles(db_path: Path) -> list[str]:
    """Read the logical message-role sequence from a context.db."""
    if not db_path.exists():
        return []
    import apsw

    conn = apsw.Connection(str(db_path))
    roles: list[str] = []
    try:
        if conn.execute("SELECT COUNT(*) FROM system_prompt").fetchone()[0]:
            roles.append("_system_prompt")
        if conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]:
            roles.append("_checkpoint")
        roles.extend(row[0] for row in conn.execute("SELECT role FROM messages ORDER BY rowid"))
    finally:
        conn.close()
    return roles


def test_session_files_created(tmp_path) -> None:
    config_path = write_scripted_config(tmp_path, ["text: hello"])
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)
    session_id = "e2e-session"

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        extra_args=["--session", session_id],
        yolo=True,
    )
    try:
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "hi"},
            }
        )
        resp, _ = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"
    finally:
        wire.close()

    session_dir = _session_dir(home_dir, work_dir) / session_id
    # New sessions store context in context.db (context.jsonl is the legacy path).
    context_file = session_dir / "context.db"
    wire_file = session_dir / "wire.jsonl"
    assert context_file.exists()
    assert wire_file.exists()
    assert context_file.stat().st_size > 0
    assert wire_file.stat().st_size > 0
    # Ignore the ephemeral per-process rtk cache dir (.kimix_cache).
    names = sorted(p.name for p in session_dir.iterdir() if p.name != ".kimix_cache")
    assert names == snapshot(["context.db", "history.db", "state.json", "wire.jsonl"])


def test_continue_session_appends(tmp_path) -> None:
    config_path = write_scripted_config(tmp_path, ["text: first", "text: second"])
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    # Anonymous (auto-id) sessions delete their context on close, so name the
    # session to test that --continue appends to persisted context.
    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        extra_args=["--session", "e2e-continue-session"],
        yolo=True,
    )
    try:
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "first"},
            }
        )
        resp, _ = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"
    finally:
        wire.close()

    session_root = _session_dir(home_dir, work_dir)
    session_ids = [p.name for p in session_root.iterdir() if p.is_dir()]
    assert len(session_ids) == 1
    session_id = session_ids[0]
    session_dir = session_root / session_id
    context_file = session_dir / "context.db"
    wire_file = session_dir / "wire.jsonl"
    context_before = _count_db_entries(context_file)
    wire_before = _count_lines(wire_file)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        extra_args=["--continue"],
        yolo=True,
    )
    try:
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-2",
                "method": "prompt",
                "params": {"user_input": "second"},
            }
        )
        resp, _ = collect_until_response(wire, "prompt-2")
        assert resp.get("result", {}).get("status") == "finished"
    finally:
        wire.close()

    context_after = _count_db_entries(context_file)
    wire_after = _count_lines(wire_file)
    assert context_after > context_before
    assert wire_after > wire_before
    assert {
        "context_before": context_before,
        "context_after": context_after,
        "wire_before": wire_before,
        "wire_after": wire_after,
    } == snapshot({"context_before": 3, "context_after": 5, "wire_before": 8, "wire_after": 14})
    assert _read_roles(context_file) == snapshot(
        [
            "_system_prompt", "user", "assistant", "user", "assistant",
        ]
    )


def test_clear_context(tmp_path) -> None:
    config_path = write_scripted_config(tmp_path, ["text: hello"])
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        extra_args=["--session", "e2e-clear-session"],
        yolo=True,
    )
    try:
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "hi"},
            }
        )
        resp, _ = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"

        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-2",
                "method": "prompt",
                "params": {"user_input": "/clear"},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-2")
        assert resp.get("result", {}).get("status") == "finished"
        assert summarize_messages(messages) == snapshot(
            [
                {"method": "event", "type": "TurnBegin", "payload": {"user_input": "/clear"}},
                {
                    "method": "event",
                    "type": "ContentPart",
                    "payload": {"type": "text", "text": "The context has been cleared."},
                },
                {
                    "method": "event",
                    "type": "StatusUpdate",
                    "payload": {
                        "context_usage": 0.0,
                        "context_tokens": 0,
                        "max_context_tokens": 100000,
                        "token_usage": None,
                        "message_id": None,
                        "mcp_status": None,
                    },
                },
                {"method": "event", "type": "TurnEnd", "payload": {}},
            ]
        )
    finally:
        wire.close()

    session_root = _session_dir(home_dir, work_dir)
    session_ids = [p.name for p in session_root.iterdir() if p.is_dir()]
    assert len(session_ids) == 1
    session_dir = session_root / session_ids[0]
    context_file = session_dir / "context.db"
    assert _read_roles(context_file) == snapshot(["_system_prompt"])


def test_manual_compact(tmp_path) -> None:
    scripts = [
        "text: hello",
        "text: compacted summary",
    ]
    config_path = write_scripted_config(tmp_path, scripts)
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=True,
    )
    try:
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "hi"},
            }
        )
        resp, _ = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"

        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-2",
                "method": "prompt",
                "params": {"user_input": "/compact"},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-2")
        assert resp.get("result", {}).get("status") == "finished"
        assert summarize_messages(messages) == snapshot(
            [
                {"method": "event", "type": "TurnBegin", "payload": {"user_input": "/compact"}},
                {"method": "event", "type": "CompactionBegin", "payload": {"compaction_id": "<uuid>", "trigger": "manual", "shadowed_tokens": None}},
                {"method": "event", "type": "CompactionEnd", "payload": {"compaction_id": "<uuid>", "trigger": "manual", "shadowed_tokens": 0, "estimated_token_count": 4133, "error": None}},
                {
                    "method": "event",
                    "type": "ContentPart",
                    "payload": {"type": "text", "text": "The context has been compacted."},
                },
                {
                    "method": "event",
                    "type": "StatusUpdate",
                    "payload": {
                        "context_usage": 0.04133,
                        "context_tokens": 4133,
                        "max_context_tokens": 100000,
                        "token_usage": None,
                        "message_id": None,
                        "mcp_status": None,
                    },
                },
                {"method": "event", "type": "TurnEnd", "payload": {}},
            ]
        )
    finally:
        wire.close()


def test_manual_compact_with_usage(tmp_path) -> None:
    """Compaction with enough messages to trigger an actual LLM call that returns usage."""
    # The compaction guard rejects summaries that are not smaller than the
    # shadowed content, so the conversation text must be substantially longer
    # than the scripted "compacted summary".
    long_text = "lorem ipsum dolor sit amet " * 120
    scripts = [
        f"text: {long_text}\nusage: input_other=10 output=5",
        f"text: {long_text}\nusage: input_other=30 output=8",
        "text: compacted summary\nusage: input_other=50 output=20",
    ]
    config_path = write_scripted_config(tmp_path, scripts)
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=True,
    )
    try:
        send_initialize(wire)

        # Two rounds of conversation to build up context beyond max_preserved_messages=2
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "hi"},
            }
        )
        resp, _ = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"

        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-2",
                "method": "prompt",
                "params": {"user_input": "how are you"},
            }
        )
        resp, _ = collect_until_response(wire, "prompt-2")
        assert resp.get("result", {}).get("status") == "finished"

        # Now compact — this triggers a real compaction LLM call (script 3)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-3",
                "method": "prompt",
                "params": {"user_input": "/compact"},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-3")
        assert resp.get("result", {}).get("status") == "finished"

        # Verify context_usage is non-zero (usage.output=20 + preserved text estimate)
        status_msg = [m for m in messages if m.get("params", {}).get("type") == "StatusUpdate"]
        assert len(status_msg) == 1
        context_usage = status_msg[0]["params"]["payload"]["context_usage"]
        assert context_usage > 0, "context_usage should be non-zero after compaction with usage"
    finally:
        wire.close()


def test_replay_streams_wire_history(tmp_path) -> None:
    scripts = [
        "\n".join(
            [
                "text: step1",
                build_shell_tool_call("tc-1", "echo ok"),
            ]
        ),
        "text: done",
    ]
    config_path = write_scripted_config(tmp_path, scripts)
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        agent_file=write_shell_agent_file(tmp_path),
        extra_args=["--session", "replay-session"],
        yolo=False,
    )
    try:
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "run shell"},
            }
        )
        resp, _ = collect_until_response(
            wire,
            "prompt-1",
            request_handler=lambda msg: build_approval_response(msg, "approve"),
        )
        assert resp.get("result", {}).get("status") == "finished"

        wire.send_json({"jsonrpc": "2.0", "id": "replay-1", "method": "replay"})
        resp, messages = collect_until_response(wire, "replay-1")
        assert resp.get("result") == snapshot(
            {
                "status": "finished",
                "events": 10,
                "requests": 0,
            }
        )
        assert summarize_messages(messages) == snapshot(
            [
                {
                    "method": "event",
                    "type": "TurnBegin",
                    "payload": {"user_input": "run shell"},
                },
                {"method": "event", "type": "StepBegin", "payload": {"n": 1}},
                {
                    "method": "event",
                    "type": "ContentPart",
                    "payload": {"type": "text", "text": "step1"},
                },
                {
                    "method": "event",
                    "type": "ToolCall",
                    "payload": {
                        "type": "function",
                        "id": "tc-1",
                        "function": {"name": "bash", "arguments": '{"command": "echo ok"}'},
                        "extras": None,
                    },
                },
                {
                    "method": "event",
                    "type": "StatusUpdate",
                    "payload": {
                        "context_usage": None,
                        "context_tokens": None,
                        "max_context_tokens": None,
                        "token_usage": None,
                        "message_id": None,
                        "mcp_status": None,
                    },
                },
                {
                    "method": "event",
                    "type": "ToolResult",
                    "payload": {
                        "tool_call_id": "tc-1",
                        "return_value": {
                            "is_error": False,
                            "output": """\
task_id: bash
status: completed
exit_code: 0
exit_code_meaning: null
failure_hint: null
output: |
  ok
output_truncated: false
output_path: null
wait_matched: null
elapsed_seconds: null
original_path: <rtk_tmp>/0.txt\
""",
                            "message": "success [original saved to <rtk_tmp>/0.txt]",
                            "display": [{"type": "shell", "language": "shell"}],
                            "extras": None,
                        },
                    },
                },
                {"method": "event", "type": "StepBegin", "payload": {"n": 2}},
                {
                    "method": "event",
                    "type": "ContentPart",
                    "payload": {"type": "text", "text": "done"},
                },
                {
                    "method": "event",
                    "type": "StatusUpdate",
                    "payload": {
                        "context_usage": None,
                        "context_tokens": None,
                        "max_context_tokens": None,
                        "token_usage": None,
                        "message_id": None,
                        "mcp_status": None,
                    },
                },
                {"method": "event", "type": "TurnEnd", "payload": {}},
            ]
        )
    finally:
        wire.close()
