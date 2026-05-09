"""Tests for prompt_str utilities."""

import pytest
from unittest.mock import patch
from kimix.utils.prompt_str import (
    escape_file_paths,
    clean_text,
    remove_redundant_whitespace,
    normalize_encoding,
    remove_meaningless_symbols,
    normalize_case,
)


class TestEscapeFilePaths:
    """Tests for the merged escape_file_paths + sanitize_for_tokenizer function."""

    def test_no_paths_no_slashes(self):
        text = "hello world"
        assert escape_file_paths(text) == "hello world"

    def test_zero_width_chars_removed(self):
        text = "hello\u200bworld"
        assert escape_file_paths(text) == "helloworld"

    def test_zero_width_chars_removed_with_spaces(self):
        text = "hello \u200b world"
        assert escape_file_paths(text) == "hello  world"

    def test_surrogates_removed(self):
        text = "hello\ud800world"
        assert escape_file_paths(text) == "helloworld"

    def test_replacement_chars_removed(self):
        text = "hello\ufffdworld"
        assert escape_file_paths(text) == "helloworld"

    def test_pua_removed(self):
        text = "hello\ue000world"
        assert escape_file_paths(text) == "helloworld"

    def test_noncharacters_removed(self):
        text = "hello\ufdd0world"
        assert escape_file_paths(text) == "helloworld"

    def test_control_chars_removed(self):
        text = "hello\x00world"
        assert escape_file_paths(text) == "helloworld"

    def test_nfc_normalization(self):
        # e + combining acute -> é
        text = "hello\u0065\u0301world"
        assert escape_file_paths(text) == "hello\u00e9world"

    def test_dedupe_repeats(self):
        text = "A" * 200
        result = escape_file_paths(text, max_repeat=100)
        assert len(result) == 100
        assert result == "A" * 100

    def test_max_chars_truncate(self):
        text = "hello world"
        assert escape_file_paths(text, max_chars=5) == "hello"

    def test_max_chars_with_truncate_msg(self):
        text = "hello world"
        result = escape_file_paths(text, max_chars=10, truncate_msg="...")
        assert result == "hello w..."

    def test_strip_whitespace(self):
        text = "  hello world  \n"
        assert escape_file_paths(text) == "hello world"

    def test_non_string_input(self):
        assert escape_file_paths(123) == "123"

    @patch("kimix.utils.prompt_str.Path.exists", return_value=True)
    def test_escapes_real_path(self, mock_exists):
        text = "check src/kimix/utils.py for details"
        result = escape_file_paths(text)
        assert "`src/kimix/utils.py`" in result

    @patch("kimix.utils.prompt_str.Path.exists", return_value=False)
    def test_does_not_escape_nonexistent_path(self, mock_exists):
        text = "check /nonexistent/path.py for details"
        result = escape_file_paths(text)
        assert "`/nonexistent/path.py`" not in result
        assert result == "check /nonexistent/path.py for details"

    @patch("kimix.utils.prompt_str.Path.exists", return_value=True)
    def test_path_escaping_plus_sanitization(self, mock_exists):
        text = "check src/kimix/utils.py\u200b for details"
        result = escape_file_paths(text)
        assert "`src/kimix/utils.py`" in result
        assert "\u200b" not in result

    @patch("kimix.utils.prompt_str.Path.exists", return_value=True)
    def test_paths_in_quotes_unchanged(self, mock_exists):
        text = 'check "src/kimix/utils.py" for details'
        result = escape_file_paths(text)
        assert '`src/kimix/utils.py`' not in result
        assert '"src/kimix/utils.py"' in result

    def test_url_ignored(self):
        text = "visit https://example.com/path"
        assert escape_file_paths(text) == "visit https://example.com/path"

    def test_fraction_ignored(self):
        text = "the ratio is 3/4"
        assert escape_file_paths(text) == "the ratio is 3/4"

    def test_date_ignored(self):
        text = "today is 2024/01/15"
        assert escape_file_paths(text) == "today is 2024/01/15"

    def test_newlines_preserved(self):
        text = "line1\nline2"
        assert escape_file_paths(text) == "line1\nline2"

    def test_empty_string(self):
        assert escape_file_paths("") == ""


class TestCleanText:
    def test_remove_zero_width(self):
        text = "a\u200bb\u200cc\u200dd\ufeffe"
        assert clean_text(text) == "abcde"

    def test_keep_newlines(self):
        text = "a\nb\tc"
        assert clean_text(text, keep_newlines=True) == "a\nb\tc"

    def test_remove_newlines(self):
        text = "a\nb\tc"
        assert clean_text(text, keep_newlines=False) == "abc"


class TestRemoveRedundantWhitespace:
    def test_basic(self):
        text = "hello    world\n\n\nfoo"
        assert remove_redundant_whitespace(text) == "hello world foo"

    def test_inline_code_preserved(self):
        text = "hello `  world  ` foo"
        assert remove_redundant_whitespace(text) == "hello `  world  ` foo"


class TestNormalizeCase:
    def test_lower(self):
        text = "Hello World"
        assert normalize_case(text, mode="lower") == "hello world"

    def test_title(self):
        text = "hello world"
        assert normalize_case(text, mode="title") == "Hello World"


class TestRemoveMeaninglessSymbols:
    def test_remove_emoji(self):
        text = "hello 😀 world"
        assert remove_meaningless_symbols(text) == "hello  world"

    def test_dedupe_punctuation(self):
        text = "hello!!!???"
        result = remove_meaningless_symbols(text)
        assert "!!!" not in result
        assert "???" not in result
