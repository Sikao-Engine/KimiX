"""Tests for Defects 13.1-13.3: FetchURL improvements."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from kimi_cli.tools.web.fetch import Params as FetchURLParams


class TestFetchURLParams:
    def test_timeout_default_is_30(self) -> None:
        params = FetchURLParams(url="https://example.com")
        assert params.timeout == 30.0

    def test_timeout_bounds_enforced(self) -> None:
        with pytest.raises(ValidationError):
            FetchURLParams(url="https://example.com", timeout=0.5)  # below ge=1.0
        with pytest.raises(ValidationError):
            FetchURLParams(url="https://example.com", timeout=301)   # above le=300.0

    def test_method_default_get(self) -> None:
        params = FetchURLParams(url="https://example.com")
        assert params.method == "GET"

    def test_method_post_accepted(self) -> None:
        params = FetchURLParams(url="https://example.com", method="POST")
        assert params.method == "POST"

    def test_headers_accepted(self) -> None:
        params = FetchURLParams(
            url="https://api.example.com",
            headers={"Authorization": "Bearer token123"},
        )
        assert params.headers == {"Authorization": "Bearer token123"}

    def test_body_accepted(self) -> None:
        params = FetchURLParams(
            url="https://api.example.com",
            method="POST",
            body='{"key": "value"}',
        )
        assert params.body == '{"key": "value"}'

    def test_follow_redirects_default_true(self) -> None:
        params = FetchURLParams(url="https://example.com")
        assert params.follow_redirects is True

    def test_max_redirects_default_5(self) -> None:
        params = FetchURLParams(url="https://example.com")
        assert params.max_redirects == 5

    def test_max_redirects_bounds(self) -> None:
        with pytest.raises(ValidationError):
            FetchURLParams(url="https://example.com", max_redirects=25)
