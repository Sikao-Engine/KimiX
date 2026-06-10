"""Source code comment parsers for multiple programming languages."""

from kimix.parser.base import Comment, ParseResult, BaseParser
from kimix.parser.py_parser import PythonParser
from kimix.parser.c_parser import CParser
from kimix.parser.shell_parser import ShellParser
from kimix.parser.html_parser import HtmlParser
from kimix.parser.pascal_parser import PascalParser
from kimix.parser.lisp_parser import LispParser
from kimix.parser.sql_parser import SqlParser

__all__ = [
    "Comment", "ParseResult", "BaseParser",
    "PythonParser", "CParser", "ShellParser",
    "HtmlParser", "PascalParser", "LispParser", "SqlParser",
]
