"""Tests for prompt_str utilities.

The contract under test: this module removes *invalid characters* only.  It must
never rewrite the meaning of a prompt or a tool result, so case, Unicode width,
punctuation runs, whitespace, Traditional Chinese, and long character runs all
survive verbatim.
"""

from unittest.mock import patch

import pytest

from kimix.utils.prompt import prepare_prompt_text
from kimix.utils.prompt_str import (
    clean_text,
    escape_file_paths,
    prefilter_tool_result,
)


class TestPreparePromptText:
    """The prompt text that reaches the model: verbatim mode vs the tokenizer pass."""

    def test_verbatim_mode_keeps_the_text_byte_exact(self):
        # A corpus note quotes the exact strings an app renders.  NFKC rewrote
        # MICRO SIGN to GREEK SMALL LETTER MU, so the worker compared the note's
        # ``μm`` against a page that renders ``µm`` - a harness-made mismatch.
        note = 'PASS if [data-testid=err] reads "0.50 µm or less.", then click [data-testid=go]'
        assert prepare_prompt_text("  " + note + "  ", sanitize=False) == note

    def test_sanitize_mode_does_not_rewrite_the_wording(self):
        # Sanitizing may only drop tokenizer-hostile characters; width, case,
        # spacing and punctuation reach the model as typed.
        assert prepare_prompt_text("  ＡＩ  x!!!  ") == "ＡＩ  x!!!"

    def test_sanitize_mode_drops_invalid_characters(self):
        assert prepare_prompt_text("hello\x00\ud800\u200bworld") == "helloworld"

    def test_sanitize_mode_keeps_the_micro_sign(self):
        assert prepare_prompt_text("0.50 µm or less") == "0.50 µm or less"


class TestInvalidCharacterRemoval:
    """The only policy this module applies."""

    def test_surrogates_removed(self):
        assert escape_file_paths("hello\ud800world") == "helloworld"

    def test_noncharacters_removed(self):
        assert escape_file_paths("hello\ufdd0world") == "helloworld"
        assert escape_file_paths("hello\ufffeworld") == "helloworld"

    def test_control_chars_removed(self):
        assert escape_file_paths("hello\x00\x07world") == "helloworld"
        assert escape_file_paths("hello\x85world") == "helloworld"  # C1 control

    def test_zero_width_padding_removed(self):
        assert escape_file_paths("hello\u200bworld") == "helloworld"
        assert escape_file_paths("hello\ufeff\u2060world") == "helloworld"
        assert escape_file_paths("hello\u00adworld") == "helloworld"  # soft hyphen

    def test_zero_width_removal_does_not_squeeze_spaces(self):
        # Removing the invisible chars must not collapse the spaces around them.
        assert escape_file_paths("hello \u200b \u200b world") == "hello   world"

    def test_replacement_chars_preserved(self):
        text = "hello\ufffdworld"
        assert escape_file_paths(text) == text

    def test_pua_preserved(self):
        text = "hello\ue000world"
        assert escape_file_paths(text) == text

    def test_emoji_and_variation_selectors_preserved(self):
        text = "family 👨\u200d👩\u200d👧 keycap 1\ufe0f\u20e3 Powerline \ue0b0"
        assert escape_file_paths(text) == text

    def test_zwj_zwnj_preserved(self):
        text = "می\u200cخواهم a\u200db"
        assert escape_file_paths(text) == text

    def test_canonical_composition_from_clean_text(self):
        # clean_text NFC-composes; canonical equivalence keeps the meaning.
        text = "hello\u0065\u0301world"
        assert escape_file_paths(text) == "hello\u00e9world"

    def test_newlines_and_tabs_kept(self):
        assert escape_file_paths("a\tb\nc\r\nd") == "a\tb\nc\r\nd"

    def test_non_string_input(self):
        assert escape_file_paths(123) == "123"


class TestNoMeaningRewrites:
    """Content a prompt carries must reach the model byte-exact."""

    def test_repeated_punctuation_is_preserved(self):
        for text in (
            "stop!!!!! now",
            "really????????",
            "wait。。。。。。 next",
            "再想想，，，，，",
            "a!?!? b",
            "---",
            "=========",
        ):
            assert escape_file_paths(text) == text

    def test_mixed_punctuation_syntax_is_preserved(self):
        for text in (
            "select [data-testid='board-zone'], then extract [data-testid='count'].",
            'fields={"count": "[data-testid=station-count]", "first": "row"}, done',
            "|---|---|\n| a | b |",
            "path: C:\\dev\\x, next: `a`, then )]} stop",
        ):
            assert escape_file_paths(text) == text

    def test_full_width_forms_are_not_rewritten(self):
        # NFKC used to fold these; the model must see what the user typed.
        for text in ("ＡＩ hello", "price is １２３", "ＡＢＣ　ＤＥＦ", "ｈｅｌｌｏ"):
            assert escape_file_paths(text) == text

    def test_case_is_never_folded(self):
        for text in ("Hello World", "HELLO WORLD", "iPhone 15 Pro", "ß"):
            assert escape_file_paths(text) == text

    def test_traditional_chinese_is_not_converted(self):
        text = "繁體中文與臺灣"
        assert escape_file_paths(text) == text

    def test_long_character_runs_are_preserved(self):
        # Hashes, ASCII art, separators and base64 blobs are data, not noise.
        text = "A" * 200 + " " + "-" * 120
        assert escape_file_paths(text) == text

    def test_horizontal_whitespace_is_not_squeezed(self):
        for text in ("hello  world", "a\t\t\tb", "x   y", "1.  step"):
            assert escape_file_paths(text) == text

    def test_blank_line_runs_are_preserved(self):
        text = "line1\n\n\nline2"
        assert escape_file_paths(text) == text

    def test_fenced_code_block_verbatim(self):
        text = "```python\n  x = 1  #  HELLO\n```\nplain"
        assert escape_file_paths(text) == text

    def test_inline_code_verbatim(self):
        text = "hello `  WORLD  ` and `x!!!`"
        assert escape_file_paths(text) == text

    def test_lossy_parameters_are_gone(self):
        # The policy knobs were removed with the policies they controlled.
        with pytest.raises(TypeError):
            escape_file_paths("Hello World", case_mode="lower")  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            escape_file_paths("A" * 200, max_repeat=50)  # type: ignore[call-arg]


class TestEscapeFilePaths:
    """Path detection and backtick wrapping."""

    def test_no_paths_no_slashes(self):
        assert escape_file_paths("hello world") == "hello world"

    def test_empty_string(self):
        assert escape_file_paths("") == ""

    def test_strip_whitespace(self):
        assert escape_file_paths("  hello world  \n") == "hello world"

    @patch("kimix.utils.prompt_str.Path.exists", return_value=True)
    def test_escapes_real_path(self, mock_exists):
        text = "check src/kimix/utils.py for details"
        assert escape_file_paths(text) == "check `src/kimix/utils.py` for details"

    @patch("kimix.utils.prompt_str.Path.exists", return_value=False)
    def test_does_not_escape_nonexistent_path(self, mock_exists):
        text = "check /nonexistent/path.py for details"
        assert escape_file_paths(text) == text

    @patch("kimix.utils.prompt_str.Path.exists", return_value=True)
    def test_path_escaping_plus_character_removal(self, mock_exists):
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

    @patch("kimix.utils.prompt_str.Path.exists", return_value=True)
    def test_paths_in_fenced_code_unchanged(self, mock_exists):
        text = "```\nsrc/kimix/utils.py\n```"
        assert escape_file_paths(text) == text

    @patch("kimix.utils.prompt_str.Path.exists", return_value=True)
    def test_path_layout_is_preserved(self, mock_exists):
        text = "1.check src/kimix/utils.py\n2.for details"
        result = escape_file_paths(text)
        assert result == "1.check `src/kimix/utils.py`\n2.for details"

    def test_url_ignored(self):
        text = "visit https://example.com/path"
        assert escape_file_paths(text) == text

    def test_fraction_ignored(self):
        assert escape_file_paths("the ratio is 3/4") == "the ratio is 3/4"

    def test_date_ignored(self):
        assert escape_file_paths("today is 2024/01/15") == "today is 2024/01/15"


class TestTruncation:
    """Size limiting happens only when the caller asks for it."""

    def test_no_implicit_limit(self):
        text = "A" * 5000
        assert escape_file_paths(text) == text

    def test_max_chars_truncate(self):
        assert escape_file_paths("hello world", max_chars=5) == "hello"

    def test_max_chars_with_truncate_msg(self):
        assert escape_file_paths("hello world", max_chars=10, truncate_msg="...") == "hello w..."

    def test_truncate_msg_longer_than_limit_is_ignored(self):
        assert escape_file_paths("hello world", max_chars=2, truncate_msg="...") == "he"


class TestCleanText:
    def test_remove_invisible_padding_but_preserve_joiners(self):
        text = "a\u200bb\u200cc\u200dd\ufeffe"
        assert clean_text(text) == "ab\u200cc\u200dde"

    def test_keep_newlines(self):
        text = "a\nb\tc"
        assert clean_text(text, keep_newlines=True) == "a\nb\tc"

    def test_remove_newlines(self):
        text = "a\nb\tc"
        assert clean_text(text, keep_newlines=False) == "abc"


class TestToolResultPrefilter:
    @pytest.mark.parametrize(
        "tool_name",
        ("bash", "pwsh", "PowerShell", "python", "Run", "job_output", "sh", "shell"),
    )
    def test_process_tools_strip_tokenizer_hostile_unicode(self, tool_name):
        text = "  before\x00\ud800\ufdd0\ue000\u0378\u061c\ufffd😀 after\t\n"

        result = prefilter_tool_result(text, tool_name=tool_name)

        # Only NUL, the surrogate, and the noncharacter are removed.  All
        # valid/special content, including emoji and PUA glyphs, survives.
        assert result == "  before\ue000\u0378\u061c\ufffd😀 after\t\n"
        result.encode("utf-8")

    def test_non_process_tools_are_unchanged(self):
        text = "keep 😀 \u0378 \ud800 \x00 exactly"
        assert prefilter_tool_result(text, tool_name="read") == text

    def test_process_text_layout_and_normal_unicode_are_preserved(self):
        text = "  中文\tＡＩ\nsecond line\r\n"
        assert prefilter_tool_result(text, tool_name="bash") == text

    def test_process_prefilter_is_idempotent(self):
        once = prefilter_tool_result("a😀\u0378b\x00", tool_name="python")
        assert prefilter_tool_result(once, tool_name="python") == once == "a😀\u0378b"
