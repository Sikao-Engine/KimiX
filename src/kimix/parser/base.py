"""Base parser class and data models for source code comment parsers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar


@dataclass
class Comment:
    """Represents a single comment found in source code."""
    content: str
    line: int
    column: int
    kind: str  # "line", "block", "doc"


@dataclass
class ParseResult:
    """Result of parsing source code for comments."""
    language: str
    comments: list[Comment] = field(default_factory=list)
    code_without_comments: str = ""

    @property
    def total_comments(self) -> int:
        return len(self.comments)

    @property
    def comment_lines(self) -> int:
        return sum(1 for c in self.comments for _ in c.content.splitlines() if _)

    def get_comments_by_kind(self, kind: str) -> list[Comment]:
        return [c for c in self.comments if c.kind == kind]


class BaseParser(ABC):
    """Abstract base class for all language parsers."""

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""

    @abstractmethod
    def parse(self, source_code: str) -> ParseResult:
        """Parse source code and extract comments.

        Args:
            source_code: The source code string to parse.

        Returns:
            ParseResult containing extracted comments and code without comments.
        """
        ...

    def parse_file(self, file_path: str, encoding: str = "utf-8") -> ParseResult:
        """Parse a source file and extract comments.

        Args:
            file_path: Path to the source file.
            encoding: File encoding (default: utf-8).

        Returns:
            ParseResult containing extracted comments and code without comments.
        """
        with open(file_path, encoding=encoding) as f:
            source_code = f.read()
        return self.parse(source_code)

    def _build_result(self, language: str, source_code: str, comments: list[Comment]) -> ParseResult:
        """Build a ParseResult with code stripped of comments."""
        lines = source_code.splitlines(keepends=True)
        # Sort comments by line (and column) in reverse to avoid offset issues
        sorted_comments = sorted(comments, key=lambda c: (c.line, c.column), reverse=True)

        for comment in sorted_comments:
            if 1 <= comment.line <= len(lines):
                line = lines[comment.line - 1]
                # Replace the comment portion with whitespace
                col = comment.column - 1  # convert to 0-based
                end_col = min(col + len(comment.content), len(line))
                if col < len(line):
                    # Preserve indentation structure by replacing with spaces
                    replacement = " " * len(comment.content)
                    lines[comment.line - 1] = line[:col] + replacement + line[end_col:]

        code_without = "".join(lines)
        return ParseResult(
            language=language,
            comments=sorted(comments, key=lambda c: (c.line, c.column)),
            code_without_comments=code_without,
        )
