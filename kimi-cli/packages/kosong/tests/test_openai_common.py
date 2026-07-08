import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest

from kosong.chat_provider import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    ChatProviderError,
    openai_common,
)
from kosong.chat_provider.openai_common import (
    clamp_thinking_effort,
    convert_error,
    maybe_log_reasoning_content_error,
    reasoning_effort_to_thinking_effort,
    thinking_effort_to_reasoning_effort,
)
from kosong.contrib.chat_provider.openai_legacy import OpenAILegacy


class TestClampThinkingEffort:
    """Clamp thinking effort to a model's supported set."""

    @pytest.mark.parametrize(
        "effort,supported,expected",
        [
            ("off", None, "off"),
            ("off", {"low", "medium", "high"}, "off"),
            ("low", None, "low"),
            ("high", {"low", "medium", "high"}, "high"),
            ("max", {"low", "medium", "high"}, "high"),
            ("xhigh", {"low", "medium", "high"}, "high"),
            ("max", {"low", "medium", "high", "xhigh", "max"}, "max"),
            ("xhigh", {"low", "medium", "high", "xhigh", "max"}, "xhigh"),
            ("max", {"low", "medium", "high", "xhigh"}, "xhigh"),
            ("high", set(), "high"),
        ],
    )
    def test_clamp_thinking_effort(
        self, effort: str, supported: set[str] | None, expected: str
    ) -> None:
        assert (
            clamp_thinking_effort(
                effort,  # type: ignore[arg-type]
                supported,  # type: ignore[arg-type]
            )
            == expected
        )


class TestThinkingEffortMapping:
    """OpenAI's standard reasoning_effort accepts: none, minimal, low, medium,
    high, xhigh. Kosong's ThinkingEffort is: off, low, medium, high, xhigh, max.
    In this implementation OpenAI providers forward "max" verbatim (via
    extra_body) for backends that accept it, so the mapping preserves max and
    xhigh round-trips.
    """

    @pytest.mark.parametrize(
        "thinking_effort,expected_reasoning",
        [
            ("off", None),
            ("low", "low"),
            ("medium", "medium"),
            ("high", "high"),
            # xhigh must pass through — OpenAI supports it natively for
            # gpt-5.1-codex-max and later models.
            ("xhigh", "xhigh"),
            # max is forwarded as-is. The SDK's typed ReasoningEffort/Reasoning
            # models reject it, so providers send it through extra_body instead.
            ("max", "max"),
        ],
    )
    def test_thinking_to_reasoning(
        self, thinking_effort: str, expected_reasoning: str | None
    ) -> None:
        assert (
            thinking_effort_to_reasoning_effort(
                thinking_effort  # type: ignore[arg-type]
            )
            == expected_reasoning
        )

    @pytest.mark.parametrize(
        "reasoning_effort,expected_thinking",
        [
            (None, "off"),
            ("none", "off"),
            ("minimal", "low"),
            ("low", "low"),
            ("medium", "medium"),
            ("high", "high"),
            # xhigh is a valid kosong level and a valid OpenAI level — round-trip.
            ("xhigh", "xhigh"),
        ],
    )
    def test_reasoning_to_thinking(
        self, reasoning_effort: str | None, expected_thinking: str
    ) -> None:
        assert (
            reasoning_effort_to_thinking_effort(
                reasoning_effort  # type: ignore[arg-type]
            )
            == expected_thinking
        )


def test_create_openai_client_does_not_inject_max_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(openai_common, "AsyncOpenAI", FakeAsyncOpenAI)

    openai_common.create_openai_client(
        api_key="test-key",
        base_url="https://example.com/v1",
        client_kwargs={"timeout": 3},
    )

    assert captured["api_key"] == "test-key"
    assert captured["base_url"] == "https://example.com/v1"
    assert captured["timeout"] == 3
    assert "max_retries" not in captured


@pytest.mark.asyncio
async def test_retry_recovery_does_not_close_shared_http_client() -> None:
    http_client = httpx.AsyncClient()
    provider = OpenAILegacy(
        model="gpt-4.1",
        api_key="test-key",
        http_client=http_client,
    )

    provider.on_retryable_error(APIConnectionError("Connection error."))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert provider.client._client is http_client  # type: ignore[reportPrivateUsage]
    assert http_client.is_closed is False
    await http_client.aclose()


# ---------------------------------------------------------------------------
# convert_error: openai.APIError (base class) handling
# ---------------------------------------------------------------------------

_DUMMY_REQUEST = httpx.Request("POST", "https://api.test")


class TestConvertErrorBaseAPIError:
    """openai.APIError (the base class, NOT APIConnectionError) must be
    correctly mapped when the error message indicates a network issue.

    This guards against the bug where streaming mid-flight disconnections
    raise ``openai.APIError("Network connection lost.")`` instead of
    ``openai.APIConnectionError``, and the converter falls through to
    the generic ``ChatProviderError`` — bypassing all retry/recovery logic.
    """

    @pytest.mark.parametrize(
        ("message", "expected_type"),
        [
            ("Network connection lost.", APIConnectionError),
            ("Connection error.", APIConnectionError),
            ("network error", APIConnectionError),
            ("disconnected from server", APIConnectionError),
            ("connection reset by peer", APIConnectionError),
            ("connection closed unexpectedly", APIConnectionError),
            ("Request timed out.", APITimeoutError),
            ("timed out", APITimeoutError),
            # Timeout must take priority over network when both patterns match.
            ("connection timed out", APITimeoutError),
            ("Something completely unrelated", ChatProviderError),
            ("Internal server error", ChatProviderError),
            # Bare "reset"/"closed" must NOT match — they are too broad
            # and could appear in non-network server messages.
            ("Your session has been reset", ChatProviderError),
            ("Stream closed by server due to policy violation", ChatProviderError),
        ],
        ids=[
            "network_connection_lost",
            "connection_error",
            "network_error",
            "disconnected",
            "connection_reset_by_peer",
            "connection_closed_unexpectedly",
            "request_timed_out",
            "timed_out",
            "connection_timed_out_timeout_priority",
            "unrelated_error",
            "internal_server_error",
            "bare_reset_no_match",
            "bare_closed_no_match",
        ],
    )
    def test_base_api_error_mapping(
        self, message: str, expected_type: type[ChatProviderError]
    ) -> None:
        err = openai.APIError(message=message, request=_DUMMY_REQUEST, body=None)
        result = convert_error(err)
        assert type(result) is expected_type, (
            f"Expected {expected_type.__name__} for message={message!r}, "
            f"got {type(result).__name__}"
        )

    def test_subclass_errors_still_match_first(self) -> None:
        """Existing specific error types must still be matched before
        the new base APIError branch."""
        # APIConnectionError should still match its own case
        conn_err = openai.APIConnectionError(request=_DUMMY_REQUEST)
        result = convert_error(conn_err)
        assert type(result) is APIConnectionError

        # APITimeoutError should still match its own case
        timeout_err = openai.APITimeoutError(request=_DUMMY_REQUEST)
        result = convert_error(timeout_err)
        assert type(result) is APITimeoutError

    def test_api_error_with_body_skips_heuristic(self) -> None:
        """SSE error events carry a body dict — they must NOT be
        heuristically reclassified, even if the message contains
        network keywords."""
        err = openai.APIError(
            message="Connection limit exceeded",
            request=_DUMMY_REQUEST,
            body={"error": {"message": "Connection limit exceeded", "type": "server_error"}},
        )
        result = convert_error(err)
        assert type(result) is ChatProviderError

    def test_api_response_validation_error_falls_through(self) -> None:
        """APIResponseValidationError has a body and must not be
        heuristically reclassified even if message contains keywords."""
        resp = httpx.Response(200, request=_DUMMY_REQUEST)
        err = openai.APIResponseValidationError(
            response=resp,
            body=None,
            message="connection field missing in response",
        )
        # APIResponseValidationError sets body from the response parsing,
        # but even with body=None the guard only applies to exact APIError;
        # however APIResponseValidationError IS an APIError subclass.
        # The key point: it should become ChatProviderError, not APIConnectionError.
        result = convert_error(err)
        assert type(result) is ChatProviderError

    def test_api_status_error_preserves_response_headers(self) -> None:
        """openai.APIStatusError must propagate response headers so the soul
        layer can read ``Retry-After``."""
        response = httpx.Response(
            429,
            request=_DUMMY_REQUEST,
            headers={"retry-after": "7", "x-request-id": "req-openai"},
        )
        err = openai.APIStatusError(
            response=response,
            body={"error": {"message": "rate limited"}},
            message="rate limited",
        )
        result = convert_error(err)
        assert type(result) is APIStatusError
        assert result.status_code == 429
        assert result.request_id == "req-openai"
        assert result.headers == response.headers
        assert result.retry_after == 7


# ---------------------------------------------------------------------------
# missing reasoning_content 400 logging
# ---------------------------------------------------------------------------


class TestMaybeLogReasoningContentError:
    """Debug logging for the Moonshot/Kimi 400 that occurs when thinking-mode
    reasoning_content is not passed back to the API.
    """

    _DUMMY_REQUEST = httpx.Request("POST", "https://api.test")

    @pytest.fixture
    def log_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        monkeypatch.chdir(tmp_path)
        return tmp_path

    def _make_api_status_error(
        self,
        status_code: int = 400,
        message: str = "The reasoning_content in the thinking mode must be passed back to the API.",
        body: dict[str, Any] | None = None,
    ) -> openai.APIStatusError:
        response = httpx.Response(status_code, request=self._DUMMY_REQUEST)
        return openai.APIStatusError(
            response=response,
            body=body
            or {
                "error": {
                    "message": message,
                    "type": "invalid_request_error",
                    "code": "400001",
                }
            },
            message=message,
        )

    def test_logs_target_400_error(self, log_dir: Path) -> None:
        err = self._make_api_status_error()
        maybe_log_reasoning_content_error(
            err,
            provider_name="openai",
            model="gpt-test",
            messages=[{"role": "user", "content": "hi"}],
            generation_kwargs={"temperature": 0.5},
        )
        log_path = log_dir / "error.log"
        assert log_path.exists()
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["provider"] == "openai"
        assert entry["model"] == "gpt-test"
        assert entry["error"]["status_code"] == 400
        assert entry["messages"] == [{"role": "user", "content": "hi"}]
        assert entry["generation_kwargs"] == {"temperature": 0.5}

    @pytest.mark.parametrize(
        "message",
        [
            "The reasoning_content in the thinking mode must be passed back to the API.",
            "The `reasoning_content` in the thinking mode must be passed back to the API.",
        ],
    )
    def test_matches_various_message_forms(self, log_dir: Path, message: str) -> None:
        err = self._make_api_status_error(message=message)
        maybe_log_reasoning_content_error(
            err,
            provider_name="openai",
            model="gpt-test",
            messages=[],
            generation_kwargs={},
        )
        assert (log_dir / "error.log").exists()

    def test_does_not_log_other_400_errors(self, log_dir: Path) -> None:
        err = self._make_api_status_error(
            message="Bad request",
            body={"error": {"message": "Bad request", "type": "invalid_request_error"}},
        )
        maybe_log_reasoning_content_error(
            err,
            provider_name="openai",
            model="gpt-test",
            messages=[],
            generation_kwargs={},
        )
        assert not (log_dir / "error.log").exists()

    def test_does_not_log_non_400_status(self, log_dir: Path) -> None:
        err = self._make_api_status_error(
            status_code=401,
            message="The reasoning_content in the thinking mode must be passed back to the API.",
        )
        maybe_log_reasoning_content_error(
            err,
            provider_name="openai",
            model="gpt-test",
            messages=[],
            generation_kwargs={},
        )
        assert not (log_dir / "error.log").exists()

    def test_does_not_log_non_api_status_error(self, log_dir: Path) -> None:
        err = openai.APIConnectionError(request=self._DUMMY_REQUEST)
        maybe_log_reasoning_content_error(
            err,
            provider_name="openai",
            model="gpt-test",
            messages=[],
            generation_kwargs={},
        )
        assert not (log_dir / "error.log").exists()


# ---------------------------------------------------------------------------
# Streaming error propagation (integration)
# ---------------------------------------------------------------------------


class TestOpenAIStreamingErrorPropagation:
    """When openai.APIError is raised during OpenAI stream consumption,
    _convert_stream_response must convert it to the correct kosong error type.

    This is the exact scenario from the bug: streaming for ~33 minutes,
    then the SSE connection drops and the SDK raises
    openai.APIError("Network connection lost.") — which must become
    APIConnectionError so that retry/recovery logic triggers.
    """

    async def test_base_api_error_becomes_connection_error(self) -> None:
        """openai.APIError("Network connection lost.") during streaming
        must surface as kosong APIConnectionError."""
        from kosong.contrib.chat_provider.openai_legacy import OpenAILegacyStreamedMessage

        async def _failing_stream() -> Any:
            raise openai.APIError(
                message="Network connection lost.",
                request=_DUMMY_REQUEST,
                body=None,
            )
            yield  # make this an async generator  # noqa: RUF027

        msg = OpenAILegacyStreamedMessage(_failing_stream(), reasoning_key=None)  # type: ignore[arg-type]
        with pytest.raises(APIConnectionError, match="Network connection lost"):
            async for _ in msg:
                pass


@pytest.mark.asyncio
async def test_openai_compatible_provider_aclose_closes_http_client() -> None:
    provider = OpenAILegacy(
        model="gpt-4.1",
        api_key="test-key",
        base_url="https://example.com/v1",
    )
    http_client = provider.client._client
    assert isinstance(http_client, httpx.AsyncClient)
    assert http_client.is_closed is False

    await provider.aclose()

    assert http_client.is_closed is True


@pytest.mark.asyncio
async def test_openai_compatible_provider_aclose_swallows_event_loop_closed() -> None:
    class FakeClient:
        async def close(self) -> None:
            raise RuntimeError("Event loop is closed")

    provider = OpenAILegacy(
        model="gpt-4.1",
        api_key="test-key",
        base_url="https://example.com/v1",
    )
    provider.client = FakeClient()  # type: ignore[assignment]

    # Should not raise.
    await provider.aclose()


@pytest.mark.asyncio
async def test_openai_compatible_provider_aclose_swallows_cancelled_error() -> None:
    class FakeClient:
        async def close(self) -> None:
            raise asyncio.CancelledError()

    provider = OpenAILegacy(
        model="gpt-4.1",
        api_key="test-key",
        base_url="https://example.com/v1",
    )
    provider.client = FakeClient()  # type: ignore[assignment]

    # Should not raise.
    await provider.aclose()
