"""Tests for Google GenAI chat-provider error conversion."""

from __future__ import annotations

import pytest

pytest.importorskip("google.genai", reason="Optional contrib dependency not installed")

from google.genai import errors as genai_errors

from kosong.chat_provider import APIStatusError
from kosong.contrib.chat_provider.google_genai import _convert_error


def test_convert_error_preserves_status_and_headers_fallback() -> None:
    """Google GenAI errors must be converted to APIStatusError even when the
    SDK does not expose raw response headers.
    """
    err = genai_errors.ClientError(429, "rate limited")
    result = _convert_error(err)
    assert type(result) is APIStatusError
    assert result.status_code == 429
    assert "rate limited" in str(result)
    assert result.headers is None
    assert result.retry_after is None
