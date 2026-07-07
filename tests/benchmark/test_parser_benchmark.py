"""Performance benchmarks for kimix.parser — all 7 parsers.

All timings are assert-based so the file doubles as a regression test.
"""

from __future__ import annotations

import time

import pytest

from kimix.parser import (
    PythonParser,
    CParser,
    ShellParser,
    HtmlParser,
    PascalParser,
    LispParser,
    SqlParser,
)

pytestmark = pytest.mark.slow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_python_source(lines: int, with_fstrings: bool = False) -> str:
    """Generate synthetic Python source code."""
    parts: list[str] = []
    parts.append("# Auto-generated Python source\n")
    parts.append("from __future__ import annotations\n\n")
    parts.append("import os\nimport sys\n\n")
    parts.append("def main() -> None:\n")
    for i in range(lines - 10):
        if with_fstrings and i % 3 == 0:
            parts.append(f"    name = f\"item_{i}_{i*2}\"\n")
            parts.append(f'    print(f"Processing {{name}}... " + str(i))\n')
        elif i % 5 == 0:
            parts.append(f"    # Process item {i}\n")
            parts.append(f"    value = calculate(i + {i})\n")
        else:
            parts.append(f"    x = {i} * 2\n")
    parts.append('    """This is a docstring for main."""\n')
    parts.append('    """A multi-line docstring that\n    spans several lines\n    for testing purposes.\n    """\n')
    parts.append("    return 0\n\n")
    parts.append("if __name__ == '__main__':\n")
    parts.append("    sys.exit(main())\n")
    return "".join(parts)


def _generate_c_source(lines: int) -> str:
    """Generate synthetic C source code."""
    parts: list[str] = []
    parts.append("/* Auto-generated C source */\n")
    parts.append("#include <stdio.h>\n")
    parts.append("#include <stdlib.h>\n\n")
    parts.append("int main(int argc, char *argv[]) {\n")
    for i in range(lines - 15):
        if i % 4 == 0:
            parts.append(f"    // Process iteration {i}\n")
            parts.append(f'    printf("Processing %d\\n", {i});\n')
        elif i % 5 == 0:
            parts.append("    /* Multi-line block\n")
            parts.append("     * comment for testing\n")
            parts.append("     */\n")
        else:
            parts.append(f"    int x = {i} * 2;\n")
    parts.append("    return 0;\n")
    parts.append("}\n")
    return "".join(parts)


def _generate_html_source(lines: int) -> str:
    """Generate synthetic HTML source code."""
    parts: list[str] = []
    parts.append("<!-- Auto-generated HTML source -->\n")
    parts.append("<!DOCTYPE html>\n")
    parts.append("<html>\n")
    parts.append("<head>\n")
    parts.append("    <title>Test Page</title>\n")
    parts.append("</head>\n")
    parts.append("<body>\n")
    for i in range(lines - 20):
        parts.append(f"    <!-- Section {i} comment -->\n")
        parts.append(f"    <div id=\"section-{i}\">\n")
        parts.append(f"        <p>Content for section {i}</p>\n")
        parts.append(f"        <?php echo 'processing {i}'; ?>\n")
        parts.append(f"    </div>\n")
    parts.append("</body>\n")
    parts.append("</html>\n")
    return "".join(parts)


def _generate_shell_source(lines: int) -> str:
    """Generate synthetic shell script source code."""
    parts: list[str] = []
    parts.append("# Auto-generated shell script\n\n")
    for i in range(lines - 5):
        if i % 3 == 0:
            parts.append(f"# Comment for step {i}\n")
            parts.append(f"echo 'Processing step {i}'\n")
        elif i % 5 == 0:
            parts.append(f"# Another comment\n")
            parts.append(f"result=$(({i} * 2))\n")
        else:
            parts.append(f"echo 'Line {i}'\n")
    return "".join(parts)


def _generate_sql_source(lines: int) -> str:
    """Generate synthetic SQL source code."""
    parts: list[str] = []
    parts.append("-- Auto-generated SQL source\n\n")
    for i in range(lines - 5):
        if i % 3 == 0:
            parts.append(f"-- Select query for table_{i}\n")
            parts.append(f"SELECT * FROM table_{i} WHERE id = {i};\n")
        elif i % 5 == 0:
            parts.append(f"# MySQL-style comment\n")
            parts.append(f"INSERT INTO log (id, msg) VALUES ({i}, 'test{i}');\n")
        else:
            parts.append(f"UPDATE config SET value = {i} WHERE key = 'item{i}';\n")
    return "".join(parts)


def _generate_lisp_source(lines: int) -> str:
    """Generate synthetic Lisp source code."""
    parts: list[str] = []
    parts.append(";; Auto-generated Lisp source\n\n")
    parts.append("(defpackage :test-pkg\n")
    parts.append("  (:use :cl))\n\n")
    parts.append("(in-package :test-pkg)\n\n")
    for i in range(lines - 15):
        if i % 3 == 0:
            parts.append(f";; Function to process {i}\n")
            parts.append(f"(defun process-{i} (x)\n")
            parts.append(f"  #| Block comment for {i} |#\n")
            parts.append(f"  (+ x {i}))\n\n")
        elif i % 4 == 0:
            parts.append(f"(defvar *var-{i}* {i})\n")
        else:
            parts.append(f"(format t \"Line ~a~%\" {i})\n")
    return "".join(parts)


def _generate_pascal_source(lines: int) -> str:
    """Generate synthetic Pascal source code."""
    parts: list[str] = []
    parts.append("{ Auto-generated Pascal source }\n\n")
    parts.append("program TestProgram;\n\n")
    parts.append("begin\n")
    for i in range(lines - 10):
        if i % 3 == 0:
            parts.append(f"    (* Comment for iteration {i} *)\n")
            parts.append(f"    writeln('Processing {i}');\n")
        elif i % 5 == 0:
            parts.append("    { Another comment }\n")
            parts.append(f"    x := {i} * 2;\n")
        else:
            parts.append(f"    y := {i};\n")
    parts.append("end.\n")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Python parser benchmarks
# ---------------------------------------------------------------------------


class TestPythonParserBenchmark:
    """Benchmarks for PythonParser."""

    def test_small(self) -> None:
        """100 LOC Python file."""
        parser = PythonParser()
        code = _generate_python_source(100)
        start = time.perf_counter()
        for _ in range(1000):
            parser.parse(code)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_medium(self) -> None:
        """1000 LOC Python file."""
        parser = PythonParser()
        code = _generate_python_source(1000)
        start = time.perf_counter()
        for _ in range(100):
            parser.parse(code)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_large(self) -> None:
        """10000 LOC Python file."""
        parser = PythonParser()
        code = _generate_python_source(10000)
        start = time.perf_counter()
        for _ in range(10):
            parser.parse(code)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_fstrings(self) -> None:
        """Heavy f-string usage."""
        parser = PythonParser()
        code = _generate_python_source(500, with_fstrings=True)
        start = time.perf_counter()
        for _ in range(500):
            parser.parse(code)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0


# ---------------------------------------------------------------------------
# C parser benchmarks
# ---------------------------------------------------------------------------


class TestCParserBenchmark:
    """Benchmarks for CParser."""

    def test_medium(self) -> None:
        """1000 LOC C file."""
        parser = CParser()
        code = _generate_c_source(1000)
        start = time.perf_counter()
        for _ in range(100):
            parser.parse(code)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0


# ---------------------------------------------------------------------------
# HTML parser benchmarks
# ---------------------------------------------------------------------------


class TestHtmlParserBenchmark:
    """Benchmarks for HtmlParser."""

    def test_large(self) -> None:
        """10000 LOC HTML."""
        parser = HtmlParser()
        code = _generate_html_source(10000)
        start = time.perf_counter()
        for _ in range(10):
            parser.parse(code)
        elapsed = time.perf_counter() - start
        assert elapsed < 15.0


# ---------------------------------------------------------------------------
# Shell parser benchmarks
# ---------------------------------------------------------------------------


class TestShellParserBenchmark:
    """Benchmarks for ShellParser."""

    def test_medium(self) -> None:
        """1000 LOC shell."""
        parser = ShellParser()
        code = _generate_shell_source(1000)
        start = time.perf_counter()
        for _ in range(100):
            parser.parse(code)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0


# ---------------------------------------------------------------------------
# SQL parser benchmarks
# ---------------------------------------------------------------------------


class TestSqlParserBenchmark:
    """Benchmarks for SqlParser."""

    def test_medium(self) -> None:
        """1000 LOC SQL."""
        parser = SqlParser()
        code = _generate_sql_source(1000)
        start = time.perf_counter()
        for _ in range(100):
            parser.parse(code)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0


# ---------------------------------------------------------------------------
# Lisp parser benchmarks
# ---------------------------------------------------------------------------


class TestLispParserBenchmark:
    """Benchmarks for LispParser."""

    def test_medium(self) -> None:
        """1000 LOC Lisp."""
        parser = LispParser()
        code = _generate_lisp_source(1000)
        start = time.perf_counter()
        for _ in range(100):
            parser.parse(code)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0


# ---------------------------------------------------------------------------
# Pascal parser benchmarks
# ---------------------------------------------------------------------------


class TestPascalParserBenchmark:
    """Benchmarks for PascalParser."""

    def test_medium(self) -> None:
        """1000 LOC Pascal."""
        parser = PascalParser()
        code = _generate_pascal_source(1000)
        start = time.perf_counter()
        for _ in range(100):
            parser.parse(code)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0


# ---------------------------------------------------------------------------
# All-parser comparison benchmark
# ---------------------------------------------------------------------------


class TestAllParsersComparison:
    """Same 500 LOC file in all 7 languages — compare relative performance."""

    def test_all_parsers(self) -> None:
        """Parse 500 LOC in each language."""
        parsers: list[tuple[str, object, str]] = [
            ("Python", PythonParser(), _generate_python_source(500)),
            ("C", CParser(), _generate_c_source(500)),
            ("HTML", HtmlParser(), _generate_html_source(500)),
            ("Shell", ShellParser(), _generate_shell_source(500)),
            ("SQL", SqlParser(), _generate_sql_source(500)),
            ("Lisp", LispParser(), _generate_lisp_source(500)),
            ("Pascal", PascalParser(), _generate_pascal_source(500)),
        ]

        times: dict[str, float] = {}
        for name, parser, code in parsers:
            start = time.perf_counter()
            for _ in range(50):
                parser.parse(code)
            elapsed = time.perf_counter() - start
            times[name] = elapsed

        # All should complete within 10 seconds each
        for name, elapsed in times.items():
            assert elapsed < 10.0, f"{name} parser too slow: {elapsed:.4f}s"
