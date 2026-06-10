"""Tests for all language comment parsers."""

import json
from pathlib import Path

import pytest

from kimix.parser import (
    PythonParser,
    CParser,
    ShellParser,
    HtmlParser,
    PascalParser,
    LispParser,
    SqlParser,
    Comment,
    ParseResult,
    BaseParser,
)

EXAMPLES_DIR = Path(__file__).parent / "parser" / "examples"


def load_example(filename: str) -> str:
    """Read a sample source file from the examples directory."""
    path = EXAMPLES_DIR / filename
    return path.read_text(encoding="utf-8")


# ======================================================================
#  TestPythonParser
# ======================================================================


class TestPythonParser:
    """Tests for PythonParser."""

    parser = PythonParser()

    def test_line_comments(self):
        """Test that ``#`` line comments are extracted."""
        code = "a = 1\n# a comment\nb = 2\n"
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) == 1, f"Expected 1 comment, got {len(comments)}"
        assert "# a comment" in comments[0].content
        assert comments[0].kind == "line"
        assert comments[0].line == 2

    def test_docstring(self):
        """Test that triple-double-quoted docstrings are extracted with kind 'doc'."""
        code = '''"""A module docstring."""\n'''
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) == 1, f"Expected 1 docstring, got {len(comments)}"
        assert comments[0].kind == "doc"
        assert '"""A module docstring."""' in comments[0].content

    def test_single_quoted_docstring(self):
        """Test that triple-single-quoted docstrings are extracted with kind 'doc'."""
        code = "'''A single-quoted docstring.'''\n"
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) == 1, f"Expected 1 docstring, got {len(comments)}"
        assert comments[0].kind == "doc"
        assert "'''A single-quoted docstring.'''" in comments[0].content

    def test_comment_in_string(self):
        """Verify that ``#`` inside a regular string is NOT treated as a comment."""
        code = 'x = "# not a comment"\n'
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) == 0, (
            f"Expected 0 comments (hash inside string), got {len(comments)}: "
            f"{[c.content for c in comments]}"
        )

    def test_fstring_comment(self):
        """Verify that ``#`` inside an f-string expression is NOT a comment."""
        code = 'x = f"{1 + 1}"  # real comment\n'
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) == 1, (
            f"Expected 1 comment (the one outside the f-string), got {len(comments)}"
        )
        assert "# real comment" in comments[0].content

    def test_url_in_string(self):
        """Verify a URL with ``#`` inside a string is not a comment."""
        code = 'url = "http://example.com#fragment"\n'
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) == 0, (
            f"Expected 0 comments (# inside URL string), got {len(comments)}"
        )

    def test_sample_file(self):
        """Load ``sample.py`` and verify comment extraction."""
        code = load_example("sample.py")
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) >= 4, (
            f"Expected at least 4 comments in sample.py, got {len(comments)}"
        )

        kinds = {c.kind for c in comments}
        assert "line" in kinds, "Expected line comments in sample.py"
        assert "doc" in kinds, "Expected doc comments docstrings in sample.py"

        for c in comments:
            assert c.line >= 1, f"Invalid line number: {c.line}"

    def test_strip_comments(self):
        """Verify that ``code_without_comments`` has no ``#`` comments."""
        code = "# comment\na = 1\n# another\n"
        result = self.parser.parse(code)
        cleaned = result.code_without_comments
        # The cleaned code should preserve structure but no # should remain from comments
        lines = cleaned.splitlines()
        assert lines[0].strip() == "", "First line should be empty (comment replaced)"
        assert "a = 1" in cleaned, "Code body should be preserved"
        assert "another" not in cleaned, "Comment text should not remain"


# ======================================================================
#  TestCParser
# ======================================================================


class TestCParser:
    """Tests for CParser."""

    parser = CParser()

    def test_line_comments(self):
        """Test that ``//`` line comments are extracted."""
        code = "int x = 1;\n// A line comment\n"
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) == 1, f"Expected 1 comment, got {len(comments)}"
        assert "A line comment" in comments[0].content
        assert comments[0].kind == "line"

    def test_block_comments(self):
        """Test that ``/* */`` block comments are extracted."""
        code = "/* A block\ncomment */\n"
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) == 1, f"Expected 1 block comment, got {len(comments)}"
        assert comments[0].kind == "block"
        assert "A block" in comments[0].content

    def test_doc_comments(self):
        """Test that ``/** */`` doc comments are extracted with kind 'doc'."""
        code = "/** A doc comment */\n"
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) == 1, f"Expected 1 doc comment, got {len(comments)}"
        assert comments[0].kind == "doc"
        assert "A doc comment" in comments[0].content

    def test_url_in_string(self):
        """Verify ``//`` inside a URL string is NOT a comment."""
        code = 'char *url = "http://example.com/path";\n'
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) == 0, (
            f"Expected 0 comments (// inside URL string), got {len(comments)}"
        )

    def test_sample_file(self):
        """Load ``sample.c`` and verify comment extraction."""
        code = load_example("sample.c")
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) >= 3, (
            f"Expected at least 3 comments in sample.c, got {len(comments)}"
        )
        kinds = {c.kind for c in comments}
        assert "doc" in kinds, "Expected doc comment (/**/) in sample.c"
        assert "line" in kinds, "Expected line comment (//) in sample.c"

    def test_strip_comments(self):
        """Verify ``code_without_comments`` removes comment markers."""
        code = "// line\nint a; /* block */\n"
        result = self.parser.parse(code)
        cleaned = result.code_without_comments
        assert "//" not in cleaned, "Line comment markers should be removed"
        assert "/*" not in cleaned, "Block comment start markers should be removed"
        assert "int a;" in cleaned, "Code should be preserved"


# ======================================================================
#  TestShellParser
# ======================================================================


class TestShellParser:
    """Tests for ShellParser."""

    parser = ShellParser()

    def test_line_comments(self):
        """Test that ``#`` comments are extracted."""
        code = "echo hi\n# a shell comment\n"
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) == 1, f"Expected 1 comment, got {len(comments)}"
        assert "# a shell comment" in comments[0].content
        assert comments[0].kind == "line"

    def test_shebang(self):
        """Verify the shebang ``#!/bin/bash`` is classified as kind 'doc'."""
        code = "#!/bin/bash\necho hi\n"
        result = self.parser.parse(code)
        comments = result.comments
        # The shebang is extracted as a doc comment
        shebangs = [c for c in comments if c.line == 1]
        assert len(shebangs) >= 1, "Expected a shebang comment on line 1"
        assert shebangs[0].kind == "doc", (
            f"Shebang should be kind 'doc', got '{shebangs[0].kind}'"
        )

    def test_url_in_string(self):
        """Verify ``#`` inside a double-quoted string is NOT treated as a comment."""
        code = 'echo "http://example.com#fragment"\n'
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) == 0, (
            f"Expected 0 comments (# inside double-quoted string), got {len(comments)}"
        )

    def test_sample_file(self):
        """Load ``sample.sh`` and verify comment extraction."""
        code = load_example("sample.sh")
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) >= 3, (
            f"Expected at least 3 comments in sample.sh, got {len(comments)}"
        )


# ======================================================================
#  TestHtmlParser
# ======================================================================


class TestHtmlParser:
    """Tests for HtmlParser."""

    parser = HtmlParser()

    def test_html_comments(self):
        """Test that ``<!-- -->`` comments are extracted."""
        code = "<!-- A comment -->\n"
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) == 1, f"Expected 1 comment, got {len(comments)}"
        assert "A comment" in comments[0].content
        assert comments[0].kind == "block"

    def test_processing_instructions(self):
        """Test that ``<? ?>`` processing instructions are extracted as kind 'doc'."""
        code = '<?xml version="1.0"?>\n'
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) == 1, f"Expected 1 PI, got {len(comments)}"
        assert comments[0].kind == "doc"

    def test_cdata(self):
        """Verify CDATA sections are NOT treated as comments."""
        code = "<![CDATA[some data]]>\n"
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) == 0, (
            f"Expected 0 comments for CDATA, got {len(comments)}"
        )

    def test_comment_in_attribute(self):
        """Verify ``<!--`` inside an attribute value is NOT a comment."""
        code = '<div data-test="a <!-- b --> c"></div>\n'
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) == 0, (
            f"Expected 0 comments (marker inside attribute), got {len(comments)}"
        )

    def test_sample_file(self):
        """Load ``sample.html`` and verify comment extraction."""
        code = load_example("sample.html")
        result = self.parser.parse(code)
        comments = result.comments
        # sample.html has 2 HTML comments plus 1 PI (<?xml?>)
        html_comments = [c for c in comments if c.kind == "block"]
        assert len(html_comments) >= 1, (
            f"Expected at least 1 HTML comment in sample.html, got {len(html_comments)}"
        )


# ======================================================================
#  TestPascalParser
# ======================================================================


class TestPascalParser:
    """Tests for PascalParser."""

    parser = PascalParser()

    def test_brace_comments(self):
        """Test that ``{ }`` comments are extracted."""
        code = "{ A brace comment }\n"
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) == 1, f"Expected 1 brace comment, got {len(comments)}"
        assert "A brace comment" in comments[0].content
        assert comments[0].kind == "block"

    def test_paren_star_comments(self):
        """Test that ``(* *)`` comments are extracted."""
        code = "(* An alt comment *)\n"
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) == 1, f"Expected 1 (* *) comment, got {len(comments)}"
        assert "An alt comment" in comments[0].content
        assert comments[0].kind == "block"

    def test_line_comments(self):
        """Test that ``//`` line comments are extracted."""
        code = "// A line comment\n"
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) == 1, f"Expected 1 // comment, got {len(comments)}"
        assert comments[0].kind == "line"
        assert "A line comment" in comments[0].content

    def test_comment_in_string(self):
        """Verify comment markers inside strings are NOT comments."""
        code = "s := '{ not a comment }';\n"
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) == 0, (
            f"Expected 0 comments (markers inside string), got {len(comments)}"
        )

    def test_sample_file(self):
        """Load ``sample.pas`` and verify comment extraction."""
        code = load_example("sample.pas")
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) >= 3, (
            f"Expected at least 3 comments in sample.pas, got {len(comments)}"
        )


# ======================================================================
#  TestLispParser
# ======================================================================


class TestLispParser:
    """Tests for LispParser."""

    parser = LispParser()

    def test_line_comments(self):
        """Test that ``;`` line comments are extracted."""
        code = "(defun x (y) y)\n; A lisp comment\n"
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) == 1, f"Expected 1 comment, got {len(comments)}"
        assert "; A lisp comment" in comments[0].content
        assert comments[0].kind == "line"

    def test_block_comments(self):
        """Test that ``#| |#`` block comments are extracted."""
        code = "#| A multi-line\nblock comment |#\n"
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) == 1, f"Expected 1 block comment, got {len(comments)}"
        assert "A multi-line" in comments[0].content
        assert comments[0].kind == "block"

    def test_comment_in_string(self):
        """Verify semicolons inside strings are NOT comments."""
        code = '(print "; not a comment")\n'
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) == 0, (
            f"Expected 0 comments (; inside string), got {len(comments)}"
        )

    def test_sample_file(self):
        """Load ``sample.lisp`` and verify comment extraction."""
        code = load_example("sample.lisp")
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) >= 3, (
            f"Expected at least 3 comments in sample.lisp, got {len(comments)}"
        )


# ======================================================================
#  TestSqlParser
# ======================================================================


class TestSqlParser:
    """Tests for SqlParser."""

    parser = SqlParser()

    def test_dash_line_comments(self):
        """Test that ``-- `` SQL line comments are extracted."""
        code = "SELECT 1;\n-- A SQL comment\n"
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) == 1, f"Expected 1 -- comment, got {len(comments)}"
        assert "A SQL comment" in comments[0].content
        assert comments[0].kind == "line"

    def test_hash_line_comments(self):
        """Test that ``#`` MySQL-style comments are extracted."""
        code = "SELECT 1;\n# MySQL comment\n"
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) == 1, f"Expected 1 # comment, got {len(comments)}"
        assert "MySQL comment" in comments[0].content

    def test_block_comments(self):
        """Test that ``/* */`` block comments are extracted."""
        code = "/* A block comment */\n"
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) == 1, f"Expected 1 block comment, got {len(comments)}"
        assert comments[0].kind == "block"
        assert "A block comment" in comments[0].content

    def test_comment_in_string(self):
        """Verify comment markers inside string literals are NOT comments."""
        code = "SELECT '-- not a comment' AS val;\n"
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) == 0, (
            f"Expected 0 comments (marker inside string), got {len(comments)}"
        )

    def test_sample_file(self):
        """Load ``sample.sql`` and verify comment extraction."""
        code = load_example("sample.sql")
        result = self.parser.parse(code)
        comments = result.comments
        assert len(comments) >= 3, (
            f"Expected at least 3 comments in sample.sql, got {len(comments)}"
        )


# ======================================================================
#  TestEdgeCases
# ======================================================================


class TestEdgeCases:
    """Edge-case tests across all parsers."""

    @pytest.mark.parametrize(
        "parser_cls",
        [PythonParser, CParser, ShellParser, HtmlParser, PascalParser, LispParser, SqlParser],
        ids=["python", "c", "shell", "html", "pascal", "lisp", "sql"],
    )
    def test_empty_code(self, parser_cls):
        """Empty string returns a valid result with no comments."""
        parser = parser_cls()
        result = parser.parse("")
        assert isinstance(result, ParseResult), "parse() must return a ParseResult"
        assert result.comments == [], f"Expected empty comments list, got {result.comments}"
        assert result.code_without_comments == ""

    @pytest.mark.parametrize(
        ("parser_cls", "code"),
        [
            (PythonParser, "# only"),
            (CParser, "// only"),
            (ShellParser, "# only"),
            (PascalParser, "{ only }"),
            (LispParser, "; only"),
            (SqlParser, "-- only"),
        ],
        ids=["python", "c", "shell", "pascal", "lisp", "sql"],
    )
    def test_only_comments(self, parser_cls, code):
        """Code that is only comments still produces valid output."""
        parser = parser_cls()
        result = parser.parse(code)
        assert len(result.comments) >= 1, (
            f"Expected at least 1 comment for '{code}', got {len(result.comments)}"
        )

    @pytest.mark.parametrize(
        "parser_cls",
        [PythonParser, CParser, ShellParser, HtmlParser, PascalParser, LispParser, SqlParser],
        ids=["python", "c", "shell", "html", "pascal", "lisp", "sql"],
    )
    def test_code_with_no_comments(self, parser_cls):
        """Code with no comments returns an empty comments list."""
        code = "x = 1\ny = 2\n"
        parser = parser_cls()
        result = parser.parse(code)
        assert result.comments == [], (
            f"Expected empty comments, got {len(result.comments)}"
        )

    def test_unclosed_block_comment(self):
        """Unterminated ``/*`` in C parser doesn't crash and still produces a comment."""
        code = "int a; /* unclosed block comment"
        result = CParser().parse(code)
        assert len(result.comments) >= 1, (
            "Expected at least 1 comment for unterminated /*"
        )
        assert result.comments[0].kind == "block"

    def test_unclosed_string(self):
        """Unterminated string in Python parser doesn't crash."""
        code = 'x = "unclosed string\n'
        result = PythonParser().parse(code)
        # Should not raise; returns whatever it could parse
        assert isinstance(result, ParseResult)


# ======================================================================
#  TestParserTool
# ======================================================================


class TestParserTool:
    """Tests for the ParserTool wrapper."""

    @pytest.mark.asyncio
    async def test_extract_mode(self):
        """Test extracting comments via the tool."""
        from kimix.tools.parser import ParserTool, Params

        tool = ParserTool()
        params = Params(
            language="python",
            source_code="# hello\nx = 1\n",
            mode="extract",
        )
        result = await tool(params)
        assert not result.is_error, f"Tool returned error: {result.message}"
        data = json.loads(result.output)
        assert isinstance(data, list), "Extract mode should return a list"
        assert len(data) == 1, f"Expected 1 comment, got {len(data)}"

    @pytest.mark.asyncio
    async def test_strip_mode(self):
        """Test stripping comments via the tool."""
        from kimix.tools.parser import ParserTool, Params

        tool = ParserTool()
        params = Params(
            language="python",
            source_code="# hello\nx = 1\n",
            mode="strip",
        )
        result = await tool(params)
        assert not result.is_error, f"Tool returned error: {result.message}"
        assert "# hello" not in result.output, "Comment should be stripped"
        assert "x = 1" in result.output, "Code should be preserved"

    @pytest.mark.asyncio
    async def test_both_mode(self):
        """Test 'both' mode returns comments and stripped code."""
        from kimix.tools.parser import ParserTool, Params

        tool = ParserTool()
        params = Params(
            language="python",
            source_code="# hello\nx = 1\n",
            mode="both",
        )
        result = await tool(params)
        assert not result.is_error, f"Tool returned error: {result.message}"
        data = json.loads(result.output)
        assert "comments" in data, "'both' mode should include 'comments'"
        assert "code_without_comments" in data, "'both' mode should include 'code_without_comments'"
        assert len(data["comments"]) == 1
        assert "# hello" not in data["code_without_comments"]

    @pytest.mark.asyncio
    async def test_invalid_language(self):
        """Returns error for unknown language."""
        from kimix.tools.parser import ParserTool, Params

        tool = ParserTool()
        params = Params(
            language="brainfuck",
            source_code="x",
        )
        result = await tool(params)
        assert result.is_error, "Expected an error for unknown language"
        assert "Unsupported language" in result.message

    @pytest.mark.asyncio
    async def test_missing_input(self):
        """Returns error when neither source_code nor file_path is provided."""
        from kimix.tools.parser import ParserTool, Params

        tool = ParserTool()
        params = Params(language="python")
        result = await tool(params)
        assert result.is_error, "Expected an error for missing input"
        assert "source_code" in result.message.lower() or "file_path" in result.message.lower()

    @pytest.mark.asyncio
    async def test_file_path(self):
        """Test parsing via file_path parameter using sample files."""
        from kimix.tools.parser import ParserTool, Params

        tool = ParserTool()
        sample = str(EXAMPLES_DIR / "sample.py")
        params = Params(language="python", file_path=sample, mode="extract")
        result = await tool(params)
        assert not result.is_error, f"Tool returned error: {result.message}"
        data = json.loads(result.output)
        assert len(data) >= 4, f"Expected at least 4 comments, got {len(data)}"
