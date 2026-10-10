"""Unit tests for kimi_cli.tools.common.filter_output."""

from __future__ import annotations

import pytest

from kimi_cli.tools.common import filter_output


class TestFilterOutput:
    def test_remove_ansi_colors(self) -> None:
        assert filter_output("\x1B[31mHello\x1B[0m") == "Hello"

    def test_remove_ansi_cursor(self) -> None:
        # CSI cursor movement sequences should be stripped.
        assert filter_output("\x1B[2;4Htext\x1B[K") == "text"

    def test_remove_osc_title(self) -> None:
        assert filter_output("\x1B]0;window title\x07") == ""

    def test_remove_osc_hyperlink(self) -> None:
        raw = "\x1B]8;;https://example.com\x1B\\link\x1B]8;;\x1B\\"
        assert filter_output(raw) == "link"

    def test_remove_dcs_sequence(self) -> None:
        raw = "before\x1BP1;1|data\x1B\\after"
        assert filter_output(raw) == "beforeafter"

    def test_emoticon_preserved(self) -> None:
        assert filter_output("Hello 😀 World") == "Hello 😀 World"

    def test_zwj_emoji_preserved(self) -> None:
        # ZWJ family emoji is now preserved.
        assert filter_output("👨‍👩‍👧‍👦") == "👨‍👩‍👧‍👦"

    def test_keycap_sequence_preserved(self) -> None:
        # Keycap sequences are now preserved as-is.
        assert filter_output("1️⃣") == "1️⃣"
        assert filter_output("#️⃣") == "#️⃣"

    def test_crlf_normalization(self) -> None:
        assert filter_output("Line1\r\nLine2") == "Line1\nLine2"
        assert filter_output("Line1\rLine2") == "Line1\nLine2"

    def test_mixed_input(self) -> None:
        raw = "\x1B[31mHello\x1B[0m 😀\r\nLine2\x1B]0;title\x07"
        assert filter_output(raw) == "Hello 😀\nLine2"

    def test_plain_text_preserved(self) -> None:
        # ASCII / Unicode text that is not ANSI should be unchanged.
        assert filter_output("plain text") == "plain text"
        assert filter_output("Hello, world! 123") == "Hello, world! 123"
        assert filter_output("URL: https://example.com/path") == "URL: https://example.com/path"
        assert filter_output("Tabs\tand\nnewlines") == "Tabs\tand\nnewlines"
        assert filter_output("Math: ∫∂√") == "Math: ∫∂√"
        assert filter_output("Arrows: ←↑→↓") == "Arrows: ←↑→↓"

    def test_enclosed_alphanumerics_preserved(self) -> None:
        # Emoji-style symbols are now preserved.
        assert filter_output("Enclosed: ⓐ ⓩ") == "Enclosed: ⓐ ⓩ"

    def test_lone_cr_normalized(self) -> None:
        # Carriage returns used for overprinting become newlines.
        assert filter_output("a\rb\rc") == "a\nb\nc"

    def test_non_string_raises(self) -> None:
        with pytest.raises(TypeError):
            filter_output(b"bytes")  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            filter_output(123)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            filter_output(None)  # type: ignore[arg-type]
