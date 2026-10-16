from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from kaos.path import KaosPath

from kimi_cli.tools import extract_key_argument


class TestExtractKeyArgument:
    """Tests for extract_key_argument with string inputs."""

    @pytest.fixture(autouse=True)
    def _work_dir(self, tmp_path: Path):
        self.work_dir = KaosPath(str(tmp_path))

    def test_fetchurl(self):
        result = extract_key_argument(
            '{"url": "https://example.com/a/b/c"}', "fetch_url", self.work_dir
        )
        assert result is not None
        assert "example.com" in result

    def test_readfile(self):
        result = extract_key_argument(
            '{"path": "foo/bar.py"}', "read", self.work_dir
        )
        assert result is not None
        assert "foo/bar.py" in result

    def test_grep(self):
        result = extract_key_argument('{"pattern": "hello"}', "grep", self.work_dir)
        assert result == "hello"

    def test_invalid_json(self):
        result = extract_key_argument("invalid", "Agent", self.work_dir)
        assert result is None

    def test_empty_json_object(self):
        result = extract_key_argument("{}", "Agent", self.work_dir)
        assert result is None

    def test_long_content_truncated(self):
        long_url = "https://example.com/" + "a" * 200
        result = extract_key_argument(
            f'{{"url": "{long_url}"}}', "fetch_url", self.work_dir
        )
        assert result is not None
        # shorten_middle(text, width=50) -> text[:25] + "..." + text[-25:]  => length 53
        assert len(result) <= 53

    def test_unknown_tool_returns_raw_content(self):
        result = extract_key_argument('{"a": 1}', "UnknownTool", self.work_dir)
        assert result is not None
        assert result == '{"a": 1}'

    def test_readfile_absolute_path_stripped(self):
        """Absolute paths under work_dir should be shortened to relative."""
        absolute = str(Path(str(self.work_dir)) / "foo" / "bar.py").replace("\\", "/")
        result = extract_key_argument(
            f'{{"path": "{absolute}"}}', "read", self.work_dir
        )
        assert result is not None
        assert "foo/bar.py" in result.replace("\\", "/")

    def test_subagent_string_prompt_subtitle(self):
        """The removed ``description`` param is replaced by a prompt-derived label."""
        result = extract_key_argument(
            '{"prompt": "Fix the parser bug in foo.py and run tests"}',
            "subagent",
            self.work_dir,
        )
        assert result is not None
        assert result.startswith("Fix the parser bug")

    def test_subagent_task_alias_subtitle(self):
        result = extract_key_argument('{"task": "do thing now"}', "subagent", self.work_dir)
        assert result == "do thing now"

    def test_subagent_multiline_prompt_first_line(self):
        result = extract_key_argument(
            '{"prompt": "First line\\nsecond line"}', "subagent", self.work_dir
        )
        assert result == "First line"

    def test_subagent_list_prompt_count_label(self):
        result = extract_key_argument(
            '{"prompt": ["a", "b", "c"]}', "subagent", self.work_dir
        )
        assert result == "3 tasks"

    def test_subagent_absent_prompt_returns_none(self):
        result = extract_key_argument('{"session_id": "s"}', "subagent", self.work_dir)
        assert result is None

    def test_subagent_empty_list_returns_none(self):
        result = extract_key_argument('{"prompt": []}', "subagent", self.work_dir)
        assert result is None

    def test_subagent_long_string_truncated(self):
        result = extract_key_argument(
            json.dumps({"prompt": "word " * 200}), "subagent", self.work_dir
        )
        assert result is not None
        assert len(result) <= 60

    def test_json_decode_error_returns_none(self):
        """loads_relaxed may raise stdlib json.JSONDecodeError for some malformed
        inputs; extract_key_argument must treat it like any other parse failure
        and return None instead of leaking the exception."""
        with patch(
            "kosong.utils.jsonx.loads_relaxed",
            side_effect=json.JSONDecodeError("Extra data", "doc", 0),
        ):
            result = extract_key_argument('{"url": "https://example.com"}', "fetch_url", self.work_dir)
        assert result is None
