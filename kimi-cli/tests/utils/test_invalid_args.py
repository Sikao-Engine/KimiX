"""Tests for the invalid-arguments recorder utility."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from kosong.message import ToolCall
from kosong.tooling import ToolError, ToolResult
from kosong.tooling.error import ToolParseError, ToolValidateError
from pydantic import ValidationError

from kimi_cli.utils.invalid_args import InvalidArgRecord, InvalidArgsRecorder


# ═══════════════════════════════════════════════════════════════════════════
# Unit tests for the data model
# ═══════════════════════════════════════════════════════════════════════════


class TestInvalidArgRecordModel:
    """Verify the Pydantic model serialisation / deserialisation."""

    def test_valid_parse_error(self) -> None:
        record = InvalidArgRecord.model_validate(
            {
                "role": "_invalid_arg",
                "timestamp": 1234567890.0,
                "session_id": "sess_01",
                "tool_name": "read_file",
                "tool_call_id": "call_abc",
                "arguments": '{"path": "foo.txt"',
                "error_type": "parse_error",
                "error_message": "Error parsing JSON arguments: Expecting ',' delimiter",
            }
        )
        assert record.role == "_invalid_arg"
        assert record.tool_name == "read_file"
        assert record.error_type == "parse_error"
        assert record.turn_id is None
        assert record.step_no is None

    def test_valid_validate_error(self) -> None:
        record = InvalidArgRecord(
            role="_invalid_arg",
            timestamp=time.time(),
            session_id="sess_02",
            tool_name="edit_file",
            tool_call_id="call_def",
            arguments='{"path": 123}',  # wrong type for path
            error_type="validate_error",
            error_message="Error validating JSON arguments: 'path' should be a string",
            turn_id="turn_001",
            step_no=3,
        )
        assert record.role == "_invalid_arg"
        assert record.error_type == "validate_error"
        assert record.turn_id == "turn_001"
        assert record.step_no == 3

    def test_wrong_role(self) -> None:
        with pytest.raises(ValidationError):
            InvalidArgRecord.model_validate(
                {
                    "role": "_system_prompt",  # wrong literal
                    "timestamp": 1.0,
                    "session_id": "s",
                    "tool_name": "t",
                    "tool_call_id": "c",
                    "arguments": "{}",
                    "error_type": "parse_error",
                    "error_message": "msg",
                }
            )

    def test_wrong_error_type(self) -> None:
        with pytest.raises(ValidationError):
            InvalidArgRecord.model_validate(
                {
                    "role": "_invalid_arg",
                    "timestamp": 1.0,
                    "session_id": "s",
                    "tool_name": "t",
                    "tool_call_id": "c",
                    "arguments": "{}",
                    "error_type": "runtime_error",  # not allowed
                    "error_message": "msg",
                }
            )

    def test_serialization_exclude_none(self) -> None:
        """When turn_id/step_no are None they should be excluded from JSON."""
        record = InvalidArgRecord(
            role="_invalid_arg",
            timestamp=42.0,
            session_id="sess_99",
            tool_name="test_tool",
            tool_call_id="call_xyz",
            arguments='{"key": "val"}',
            error_type="parse_error",
            error_message="bad json",
        )
        dumped = record.model_dump_json(exclude_none=True)
        data = json.loads(dumped)
        assert data["role"] == "_invalid_arg"
        assert "turn_id" not in data
        assert "step_no" not in data
        assert data["timestamp"] == 42.0

    def test_optional_fields_present(self) -> None:
        """When turn_id/step_no are set they should appear in JSON."""
        record = InvalidArgRecord(
            role="_invalid_arg",
            timestamp=42.0,
            session_id="sess_99",
            tool_name="test_tool",
            tool_call_id="call_xyz",
            arguments="{}",
            error_type="validate_error",
            error_message="invalid",
            turn_id="turn_007",
            step_no=5,
        )
        dumped = record.model_dump_json(exclude_none=True)
        data = json.loads(dumped)
        assert data["turn_id"] == "turn_007"
        assert data["step_no"] == 5


# ═══════════════════════════════════════════════════════════════════════════
# Unit tests for the Recorder
# ═══════════════════════════════════════════════════════════════════════════


class TestInvalidArgsRecorder:
    """Verify the recorder's file I/O behaviour."""

    @pytest.fixture
    def work_dir(self, tmp_path: Path) -> Path:
        return tmp_path / "work_dir"

    @pytest.fixture
    def recorder(self, work_dir: Path) -> InvalidArgsRecorder:
        return InvalidArgsRecorder(work_dir)

    def test_target_path(self, recorder: InvalidArgsRecorder, work_dir: Path) -> None:
        """The target file should be under work-dir/.kimix_cache/log/."""
        expected = work_dir / ".kimix_cache" / "log" / "invalid_arguments.md"
        assert recorder.target_path == expected

    async def test_recorder_creates_file(self, recorder: InvalidArgsRecorder) -> None:
        """Recording should create the target Markdown file."""
        record = InvalidArgRecord(
            role="_invalid_arg",
            timestamp=100.0,
            session_id="sess_01",
            tool_name="read_file",
            tool_call_id="call_001",
            arguments='{"path": "x.txt"',
            error_type="parse_error",
            error_message="bad json",
        )
        await recorder.record(record)
        assert recorder.target_path.exists()
        text = recorder.target_path.read_text(encoding="utf-8")
        assert "# Invalid arguments log" in text
        assert "## Invalid argument — read_file (parse_error)" in text
        assert "bad json" in text

    async def test_recorder_appends_multiple_records(
        self, recorder: InvalidArgsRecorder
    ) -> None:
        """Multiple records should be appended as separate Markdown sections."""
        records = [
            InvalidArgRecord(
                role="_invalid_arg",
                timestamp=float(i),
                session_id="sess_01",
                tool_name=f"tool_{i}",
                tool_call_id=f"call_{i}",
                arguments="{}",
                error_type="parse_error",
                error_message=f"error_{i}",
            )
            for i in range(3)
        ]
        for r in records:
            await recorder.record(r)

        text = recorder.target_path.read_text(encoding="utf-8")
        # Only one document title.
        assert text.count("# Invalid arguments log") == 1
        # Three record headings.
        assert text.count("## Invalid argument") == 3
        for i in range(3):
            assert f"tool_{i}" in text
            assert f"error_{i}" in text

    async def test_recorder_creates_parent_dirs(
        self, work_dir: Path, recorder: InvalidArgsRecorder
    ) -> None:
        """The recorder should create the .kimix_cache/log/ directories."""
        record = InvalidArgRecord(
            role="_invalid_arg",
            timestamp=200.0,
            session_id="sess_02",
            tool_name="any_tool",
            tool_call_id="call_999",
            arguments="{}",
            error_type="validate_error",
            error_message="bad validation",
        )
        await recorder.record(record)
        assert recorder.target_path.parent == work_dir / ".kimix_cache" / "log"
        assert recorder.target_path.parent.exists()

    async def test_recorder_handles_missing_parent_dir(
        self, recorder: InvalidArgsRecorder
    ) -> None:
        """If the work directory has been deleted, the recorder should not crash."""
        # Delete the parent dir of the target file
        parent = recorder.target_path.parent
        if parent.exists():
            import shutil

            shutil.rmtree(parent)
        record = InvalidArgRecord(
            role="_invalid_arg",
            timestamp=300.0,
            session_id="sess_gone",
            tool_name="ghost_tool",
            tool_call_id="call_ghost",
            arguments="{}",
            error_type="parse_error",
            error_message="ghost",
        )
        # Should not raise
        await recorder.record(record)


# ═══════════════════════════════════════════════════════════════════════════
# Integration tests — detecting invalid args from ToolError subclasses
# ═══════════════════════════════════════════════════════════════════════════


def _make_tool_result(
    tool_call_id: str, return_value: ToolError
) -> ToolResult:
    return ToolResult(tool_call_id=tool_call_id, return_value=return_value)


def _make_tool_call(
    call_id: str, name: str = "test_tool", arguments: str = "{}"
) -> ToolCall:
    return ToolCall(
        id=call_id,
        function=ToolCall.FunctionBody(name=name, arguments=arguments),
    )


class TestInvalidArgDetection:
    """Verify that ToolParseError and ToolValidateError are correctly detected."""

    def test_tool_parse_error_brief(self) -> None:
        """ToolParseError should have brief='Invalid arguments'."""
        err = ToolParseError("bad json")
        assert err.is_error is True
        assert err.brief == "Invalid arguments"
        assert type(err).__name__ == "ToolParseError"

    def test_tool_validate_error_brief(self) -> None:
        """ToolValidateError should have brief='Invalid arguments'."""
        err = ToolValidateError("schema violation")
        assert err.is_error is True
        assert err.brief == "Invalid arguments"
        assert "validat" in err.message.lower()

    def test_other_tool_error_not_detected(self) -> None:
        """Other ToolErrors (e.g., ToolRuntimeError) should NOT match."""
        err = ToolError(message="Something broke", brief="Runtime error")
        assert err.is_error is True
        assert err.brief != "Invalid arguments"

    def test_record_creation_from_parse_error(self) -> None:
        """Simulate creating a record from a ToolParseError + ToolCall."""
        tc = _make_tool_call("call_parse", "read_file", '{"path": "x"')
        err = ToolParseError("Expecting ',' delimiter")
        error_type = "parse_error" if type(err).__name__ == "ToolParseError" else "validate_error"

        record = InvalidArgRecord(
            role="_invalid_arg",
            timestamp=42.0,
            session_id="sess_int",
            tool_name=tc.function.name,
            tool_call_id=tc.id,
            arguments=tc.function.arguments or "",
            error_type=error_type,  # type: ignore[arg-type]
            error_message=err.message,
            turn_id="turn_int",
            step_no=1,
        )
        assert record.error_type == "parse_error"
        assert record.tool_name == "read_file"
        assert record.arguments == '{"path": "x"'

    def test_record_creation_from_validate_error(self) -> None:
        """Simulate creating a record from a ToolValidateError + ToolCall."""
        tc = _make_tool_call("call_val", "edit_file", '{"path": 123}')
        err = ToolValidateError("'path' should be a string")
        error_type = "parse_error" if type(err).__name__ == "ToolParseError" else "validate_error"

        record = InvalidArgRecord(
            role="_invalid_arg",
            timestamp=42.0,
            session_id="sess_int",
            tool_name=tc.function.name,
            tool_call_id=tc.id,
            arguments=tc.function.arguments or "",
            error_type=error_type,  # type: ignore[arg-type]
            error_message=err.message,
        )
        assert record.error_type == "validate_error"
        assert record.tool_name == "edit_file"
