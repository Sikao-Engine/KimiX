"""Tests for the comprehensive error-log snapshots (``kimi_cli.soul.error_log``).

Every point of the session-restart failure path (step retries exhausted →
``SessionRestartRequired`` → automatic session restart) must write a full
session snapshot into ``<work dir>/.kimix_cache/error_log/`` without leaking
secrets and without ever breaking the error path itself.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Self

import pytest
from pydantic import SecretStr

from kimi_cli.config import LLMProvider
from kimi_cli.llm import LLM
from kimi_cli.soul import SessionRestartRequired, run_soul
from kimi_cli.soul.agent import Agent, Runtime
from kimi_cli.soul.context import Context
from kimi_cli.soul.error_log import (
    ERROR_LOG_DIR_NAME,
    LATEST_POINTER_NAME,
    PHASE_STEP_RETRIES_EXHAUSTED,
    _mask_key,
    error_log_dir,
    record_session_error,
)
from kimi_cli.soul.kimisoul import KimiSoul
from kimi_cli.utils.aioqueue import QueueShutDown
from kimi_cli.wire import Wire
from kosong.chat_provider import (
    APIConnectionError,
    APIStatusError,
    StreamedMessagePart,
    ThinkingEffort,
    TokenUsage,
)
from kosong.message import Message, TextPart
from kosong.tooling.simple import SimpleToolset

# ── helpers ──────────────────────────────────────────────────────────────────


class AlwaysConnectionErrorProvider:
    """Provider whose every ``generate`` raises ``APIConnectionError``."""

    name = "always-connection-error"

    def __init__(self) -> None:
        self.generate_attempts = 0
        self.recovery_calls = 0

    @property
    def model_name(self) -> str:
        return "always-connection-error"

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return None

    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[object],
        history: Sequence[Message],
    ) -> StaticStreamedMessage:
        self.generate_attempts += 1
        raise APIConnectionError("Connection error.")

    def on_retryable_error(self, error: BaseException) -> bool:
        self.recovery_calls += 1
        return True

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


class StaticStreamedMessage:
    def __init__(self, parts: Sequence[StreamedMessagePart]) -> None:
        self._parts = parts

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> StreamedMessagePart:
        raise StopAsyncIteration

    @property
    def id(self) -> str | None:
        return None

    @property
    def usage(self) -> TokenUsage | None:
        return None


def _runtime_with_llm(runtime: Runtime, llm: LLM) -> Runtime:
    return Runtime(
        config=runtime.config,
        llm=llm,
        session=runtime.session,
        builtin_args=runtime.builtin_args,
        denwa_renji=runtime.denwa_renji,
        approval=runtime.approval,
        labor_market=runtime.labor_market,
        environment=runtime.environment,
        notifications=runtime.notifications,
        background_tasks=runtime.background_tasks,
        skills=runtime.skills,
        oauth=runtime.oauth,
        additional_dirs=runtime.additional_dirs,
        skills_dirs=runtime.skills_dirs,
        role=runtime.role,
    )


async def _drain_ui_messages(wire: Wire) -> None:
    wire_ui = wire.ui_side(merge=True)
    while True:
        try:
            await wire_ui.receive()
        except QueueShutDown:
            return


class _SecretProvider:
    """Provider carrying secrets in every shape the snapshot may traverse."""

    name = "kimi"

    def __init__(self) -> None:
        self.model_name = "kimi-for-coding"
        self.thinking_effort = "high"
        self.base_url = "https://api.moonshot.ai/v1"
        self.api_key = SecretStr("sk-provider-raw")
        self.default_headers = {
            "Authorization": "Bearer sk-header-secret",
            "X-Request-Id": "req-123",
        }
        self._generation_kwargs = {"temperature": 0.7, "max_tokens": 32_000}


def _make_soul(runtime: Runtime, llm: LLM, tmp_path: Path) -> KimiSoul:
    agent = Agent(
        name="Error Log Test Agent",
        system_prompt="Error log test prompt.",
        toolset=SimpleToolset(),
        runtime=_runtime_with_llm(runtime, llm),
    )
    context = Context(file_backend=tmp_path / "history.jsonl")
    return KimiSoul(agent, context=context)


# ── directory resolution ─────────────────────────────────────────────────────


def test_error_log_dir_defaults_to_work_dir(tmp_path: Path) -> None:
    assert error_log_dir(tmp_path) == tmp_path / ".kimix_cache" / ERROR_LOG_DIR_NAME


def test_error_log_dir_falls_back_to_cwd(tmp_path: Path) -> None:
    assert error_log_dir(None) == Path.cwd() / ".kimix_cache" / ERROR_LOG_DIR_NAME


def test_error_log_dir_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    override = tmp_path / "custom" / "errors"
    monkeypatch.setenv("KIMIX_ERROR_LOG_DIR", str(override))
    assert error_log_dir(None) == override
    # The override wins over an explicit work dir as well.
    assert error_log_dir(tmp_path / "work") == override


# ── snapshot content ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_exhaustion_writes_comprehensive_snapshot(
    runtime: Runtime, tmp_path: Path
) -> None:
    """Exhausted step retries must leave a full snapshot in the work dir."""
    runtime.config.loop_control.max_retries_per_step = 2
    provider = AlwaysConnectionErrorProvider()
    llm = LLM(chat_provider=provider, max_context_size=100_000, capabilities=set())
    soul = _make_soul(runtime, llm, tmp_path)
    work_dir = Path(str(runtime.session.work_dir))

    with pytest.raises(SessionRestartRequired):
        await run_soul(
            soul, "trigger connection failure", _drain_ui_messages, asyncio.Event()
        )

    log_dir = work_dir / ".kimix_cache" / ERROR_LOG_DIR_NAME
    files = [p for p in log_dir.glob("*.json") if p.name != LATEST_POINTER_NAME]
    assert len(files) == 1, f"expected exactly one snapshot, got {files}"
    snapshot = json.loads(files[0].read_text(encoding="utf-8"))

    # The snapshot name encodes phase + error type + session id.
    assert PHASE_STEP_RETRIES_EXHAUSTED in files[0].name
    assert "APIConnectionError" in files[0].name
    assert str(runtime.session.id) in files[0].name

    # Error section: full detail incl. traceback and classification.
    error = snapshot["error"]
    assert error["type"] == "APIConnectionError"
    assert error["chat_provider_error"] is True
    assert error["classification"] == "network"
    assert "APIConnectionError" in error["traceback"]
    assert error["message"] == "Connection error."

    # Restart bookkeeping mirrors the loop-control config.
    assert snapshot["restart"]["max_restarts"] == (
        runtime.config.loop_control.max_session_restarts
    )
    assert snapshot["restart"]["auto_restart_enabled"] is True

    # LLM state.
    assert snapshot["llm"]["model_name"] == "always-connection-error"
    assert snapshot["llm"]["chat_provider_class"] == "AlwaysConnectionErrorProvider"
    assert snapshot["llm"]["max_context_size"] == 100_000

    # Session + context state (the user prompt must be part of the history).
    assert snapshot["session"]["id"] == runtime.session.id
    assert snapshot["session"]["work_dir"] == str(work_dir)
    assert snapshot["context"]["message_count"] >= 1
    serialized = json.dumps(snapshot["context"]["messages"])
    assert "trigger connection failure" in serialized

    # Phase-specific extras.
    assert snapshot["extra"]["step_attempt"] == 2
    assert snapshot["extra"]["connection_recovery_exhausted"] is True
    assert snapshot["extra"]["max_retries_per_step"] == 2

    # The latest pointer names the snapshot.
    pointer = json.loads((log_dir / LATEST_POINTER_NAME).read_text(encoding="utf-8"))
    assert pointer["latest"] == files[0].name
    assert pointer["phase"] == PHASE_STEP_RETRIES_EXHAUSTED

    # The reason string is the restart reason verbatim.
    assert "retries exhausted, restarting session" in snapshot["reason"]
    assert "connection recovery exhausted" in snapshot["reason"]


def test_snapshot_masks_all_secrets(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provider config, headers and env secrets must never reach the snapshot."""
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-env-secret")
    provider = _SecretProvider()
    provider_config = LLMProvider(
        type="kimi",
        base_url="https://api.moonshot.ai/v1",
        api_key=SecretStr("sk-config-secret"),
    )
    llm = LLM(
        chat_provider=provider,
        max_context_size=100_000,
        capabilities=set(),
        provider_config=provider_config,
    )
    soul = _make_soul(runtime, llm, tmp_path)

    path = record_session_error(
        phase="connection_error",
        error=APIConnectionError("boom"),
        soul=soul,
    )
    assert path is not None
    text = path.read_text(encoding="utf-8")

    for secret in ("sk-provider-raw", "sk-config-secret", "sk-header-secret", "sk-env-secret"):
        assert secret not in text, f"secret leaked into snapshot: {secret}"
    assert "Bearer sk" not in text

    snapshot = json.loads(text)
    llm_section = snapshot["llm"]
    assert llm_section["provider_api_key_set"] is True
    assert llm_section["provider_default_headers"]["Authorization"]["masked"] is True
    assert llm_section["provider_default_headers"]["Authorization"]["set"] is True
    assert llm_section["provider_default_headers"]["X-Request-Id"] == "req-123"
    assert llm_section["provider_config"]["api_key"]["masked"] is True
    assert llm_section["provider_base_url"] == "https://api.moonshot.ai/v1"
    # Token *counters* are not credentials and must stay readable.
    assert llm_section["generation_kwargs"] == {"temperature": 0.7, "max_tokens": 32_000}
    env = snapshot["environment"]
    assert env["MOONSHOT_API_KEY"] == {"masked": True, "set": True, "length": 13}


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        # Token/key *counters* stay readable for debugging.
        ("max_tokens", False),
        ("max_completion_tokens", False),
        ("input_tokens", False),
        ("prompt_tokens", False),
        ("cached_tokens", False),
        ("token_count", False),
        ("tokens_used", False),
        # Credential-shaped keys are always masked.
        ("api_key", True),
        ("Authorization", True),
        ("refresh_token", True),
        ("access_token", True),
        ("HF_TOKEN", True),
        ("GITHUB_TOKEN", True),
        ("client_secret", True),
        ("x_goog_api_key", True),
        ("session_token", True),
    ],
)
def test_mask_key_classification(key: str, expected: bool) -> None:
    assert _mask_key(key) is expected


def test_snapshot_captures_status_error_fields(tmp_path: Path) -> None:
    """``APIStatusError`` must expose status code, request id and Retry-After."""
    error = APIStatusError(
        503,
        "Service Unavailable",
        request_id="req-abc",
        headers={"retry-after": "12", "x-request-id": "req-abc"},
    )
    path = record_session_error(
        phase="connection_error",
        error=error,
        session=_FakeSession(tmp_path),
        restart_attempt=2,
        max_restarts=3,
        reason="Step 9: APIStatusError (status=503) — retries exhausted",
    )
    assert path is not None
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    err = snapshot["error"]
    assert err["status_code"] == 503
    assert err["request_id"] == "req-abc"
    assert err["retry_after"] == 12
    assert err["headers"]["retry-after"] == "12"
    assert err["classification"] == "5xx_server:503"
    assert snapshot["restart"]["attempt"] == 2
    assert snapshot["restart"]["max_restarts"] == 3
    assert snapshot["restart"]["restartable"] is True


def test_snapshot_captures_cause_chain_and_secondary_errors(tmp_path: Path) -> None:
    cause = APIConnectionError("socket closed")
    restart = SessionRestartRequired("restart me", original_error=cause)
    compaction = RuntimeError("compaction exploded")

    path = record_session_error(
        phase="overflow_recovery_failed",
        error=restart,
        session=_FakeSession(tmp_path),
        secondary_errors={"overflow_compaction": compaction},
    )
    assert path is not None
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    err = snapshot["error"]
    assert err["type"] == "SessionRestartRequired"
    assert err["original_error"]["type"] == "APIConnectionError"
    assert err["original_error"]["message"] == "socket closed"
    assert err["related"]["overflow_compaction"]["type"] == "RuntimeError"


def test_snapshot_truncates_long_message_text(tmp_path: Path) -> None:
    session = _FakeSession(tmp_path)
    long_text = "x" * 100_000
    path = record_session_error(
        phase="connection_error",
        error=APIConnectionError("boom"),
        session=session,
        extra={"history": [Message(role="user", content=[TextPart(text=long_text)])]},
    )
    assert path is not None
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    stored = snapshot["extra"]["history"][0]["content"]
    assert isinstance(stored, str)
    assert len(stored) < 100_000
    assert stored.endswith("chars]")
    assert "truncated" in stored


def test_snapshot_survives_broken_soul(tmp_path: Path) -> None:
    class _Boom:
        def __getattr__(self, name: str) -> object:
            raise RuntimeError(f"no attribute {name}")

    session = _FakeSession(tmp_path)
    path = record_session_error(
        phase="connection_error",
        error=APIConnectionError("boom"),
        soul=_Boom(),
        session=session,
    )
    assert path is not None, "a broken soul must not prevent the snapshot"
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    assert snapshot["session"]["id"] == "sess-1"
    # Section failures are recorded, not swallowed silently.
    assert snapshot["collection_warnings"]


def test_disabled_by_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIMIX_ERROR_LOG_ENABLED", "0")
    assert (
        record_session_error(
            phase="connection_error",
            error=APIConnectionError("boom"),
            session=_FakeSession(tmp_path),
        )
        is None
    )
    assert not (tmp_path / ".kimix_cache" / ERROR_LOG_DIR_NAME).exists()


def test_pruning_keeps_newest_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIMIX_ERROR_LOG_MAX_FILES", "2")
    log_dir = error_log_dir(tmp_path)
    for i in range(4):
        record_session_error(
            phase="connection_error",
            error=APIConnectionError(f"boom {i}"),
            session=_FakeSession(tmp_path),
        )
    files = [p for p in log_dir.glob("*.json") if p.name != LATEST_POINTER_NAME]
    assert len(files) == 2
    # The pointer always references an existing snapshot.
    pointer = json.loads((log_dir / LATEST_POINTER_NAME).read_text(encoding="utf-8"))
    assert (log_dir / pointer["latest"]).exists()


class _FakeSession:
    """Minimal session stand-in carrying only the paths the recorder needs."""

    def __init__(self, work_dir: Path) -> None:
        self.id = "sess-1"
        self.work_dir = work_dir
        self.title = "Test Session"
        self.state = {"yolo": False}
        self.custom_data: dict[str, object] = {}


# ── restart-section defaults ─────────────────────────────────────────────────


def test_restart_section_reflects_soul_loop_control(runtime: Runtime, tmp_path: Path) -> None:
    runtime.config.loop_control.max_session_restarts = 5
    soul = _make_soul(
        runtime,
        LLM(
            chat_provider=AlwaysConnectionErrorProvider(),
            max_context_size=100_000,
            capabilities=set(),
        ),
        tmp_path,
    )
    path = record_session_error(
        phase="connection_error",
        error=APIConnectionError("boom"),
        soul=soul,
    )
    assert path is not None
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    restart = snapshot["restart"]
    assert restart["config_max_session_restarts"] == 5
    assert restart["max_restarts"] == 5
    assert restart["auto_restart_enabled"] is True
    # The soul context itself is captured.
    assert snapshot["soul"]["class"] == "KimiSoul"
    assert snapshot["soul"]["current_step_no"] is not None
