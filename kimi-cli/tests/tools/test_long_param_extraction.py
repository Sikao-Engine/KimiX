"""Tests for the malformed long-param extraction gate.

``kimi_cli.tools.common._extract_and_save_long_param`` (called from
``Toolset.handle``) rejects a tool call and saves its long param to a temp file
when the value "looks malformed".  One of its heuristics rewrites values that
use ``\\n`` as a line separator, and that heuristic used to fire on any long
single-line value containing the two literal characters ``\\`` + ``n`` — which
made perfectly valid shell commands (``tr '\\n' ' '``, ``printf '%s\\n'``) fail
with "Parameters appear to be in the wrong format" without ever reaching bash.

Regression under test: a ~440 character one-line bash ``for`` loop driving test
executables through ``tr '\\n' ' '`` was rejected as a malformed parameter.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from kimi_cli.session import Session
from kimi_cli.soul.toolset import KimiToolset
from kimi_cli.tools import SkipThisTool
from kimi_cli.tools.common import (
    _count_unquoted_escaped_newlines,
    _extract_and_save_long_param,
    _extract_content_from_malformed,
    _looks_escaped_newlines,
    _looks_like_malformed_json_param,
)
from kimi_cli.tools.file.bash.bash_tool import Bash, find_bash
from kimi_cli.wire.types import ToolCall, ToolResult

# The command that regressed (abbreviated test list, otherwise byte-for-byte).
BASH_FOR_LOOP_WITH_TR = (
    'cd /c/dev/kimix-native && export PATH="/c/Program Files/Microsoft Visual Studio/'
    '2022/Community/VC/Tools/MSVC/14.44.35207/bin/HostX64/x64:$PATH"; for t in '
    "test_graph_db test_graph_db_serialize test_graph_db_file_store; do printf "
    '"%-32s " $t; timeout 300 ./bin/debug/$t.exe > /tmp/run_$t.log 2>&1; echo '
    "\"exit=$? :: $(grep -E -o 'all tests passed \\([0-9]+ asserts in [0-9]+ tests\\)"
    "|[0-9]+ failed|AddressSanitizer: [a-z-]+' /tmp/run_$t.log | head -2 | "
    "tr '\\n' ' ')\"; done"
)

# Same failure mode via ``printf '%s\n'``: one literal escape, one long line.
BASH_PRINTF_SCRIPT_WRITER = (
    "cd /c/dev/kimix-native && printf '%s\\n' '#!/bin/bash' 'export PATH=\""
    "/c/Program Files/Microsoft Visual Studio/2022/Community/VC/Tools/MSVC/"
    "14.44.35207/bin/HostX64/x64:$PATH\"' 'for t in test_graph_db test_graph_db_bulk; "
    "do' '  timeout 400 ./bin/debug/$t.exe > /tmp/run_$t.log 2>&1' 'done' > "
    "/tmp/runall.sh && chmod +x /tmp/runall.sh"
)


def _flatten(text: str) -> str:
    """Encoder-style flattening: real line breaks become ``\\n`` escapes."""
    return text.replace("\n", "\\n")


# A genuinely flattened payload: every line break is an escape sequence.
_REAL_BASH_SCRIPT = """#!/bin/bash
set -euo pipefail
export PATH=/usr/bin:/bin:$PATH
for t in test_a test_b; do
  echo "running $t"
  ./bin/debug/$t.exe > /tmp/run_$t.log 2>&1
  grep -E "all tests passed|failed" /tmp/run_$t.log | tail -3
done
echo all done
"""
ESCAPED_BASH_SCRIPT = _flatten(_REAL_BASH_SCRIPT)

# Few escapes, but acompanied by the other artifacts a JSON encoder emits.
ESCAPED_WITH_QUOTE_HINTS = (
    'cd /c/dev/repo && echo \\"building the whole test suite now\\" \\n '
    "cmake --build build --config RelWithDebInfo \\n ./build/tests/run_all.exe \\n"
)


def _assert_reaches_tool(command: str) -> None:
    """No malformed-param detection may fire for this (valid) value."""
    assert _looks_like_malformed_json_param(command) is False
    assert _extract_content_from_malformed(command) is None
    assert _extract_and_save_long_param({"command": command}, "bash") is None


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(BASH_FOR_LOOP_WITH_TR, id="for-loop-with-tr"),
        pytest.param(BASH_PRINTF_SCRIPT_WRITER, id="printf-escaped-newlines"),
    ],
)
def test_literal_escape_newline_in_one_liner_is_not_malformed(command: str) -> None:
    """A long one-line command that merely *contains* ``\\n`` is valid input."""
    assert len(command) > 200, "fixture must be long enough to reach the gate"
    assert "\\n" in command and "\n" not in command
    _assert_reaches_tool(command)


def test_tr_newline_command_is_eye_checked() -> None:
    """The escaped-newline predicate is the exact switch that regressed."""
    assert _looks_escaped_newlines(BASH_FOR_LOOP_WITH_TR) is False
    assert _looks_escaped_newlines(BASH_PRINTF_SCRIPT_WRITER) is False


def test_escaped_payload_still_extracted() -> None:
    """A flattened multi-line script is still recognised and saved."""
    assert _looks_escaped_newlines(ESCAPED_BASH_SCRIPT) is True
    extracted = _extract_content_from_malformed(ESCAPED_BASH_SCRIPT)
    assert extracted is not None
    assert "\n" in extracted
    assert "\\n" not in extracted
    assert extracted.startswith("#!/bin/bash\nset -euo pipefail\n")

    saved = _extract_and_save_long_param({"command": ESCAPED_BASH_SCRIPT}, "bash")
    assert saved is not None
    path = Path(saved["command"])
    assert path.read_text(encoding="utf-8") == extracted
    path.unlink()


def test_escaped_payload_with_quote_hints_extracted_at_low_count() -> None:
    """Few escapes plus other encoder artifacts still counts as a payload."""
    assert ESCAPED_WITH_QUOTE_HINTS.count("\\n") < 6
    assert _looks_escaped_newlines(ESCAPED_WITH_QUOTE_HINTS) is True
    assert _extract_content_from_malformed(ESCAPED_WITH_QUOTE_HINTS) is not None


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(r"one \n two", id="single-escape"),
        pytest.param(r"line one \n line two \n", id="two-escapes"),
    ],
)
def test_too_few_escapes_is_not_a_payload(value: str) -> None:
    """Below the minimum count (and without other hints) nothing is rewritten."""
    assert _looks_escaped_newlines(value) is False
    assert _extract_content_from_malformed(value) is None


def test_quoted_escape_newlines_are_data_not_separators() -> None:
    """Many ``\\n`` inside quotes are payload data, not escaped line breaks.

    Even above the "strong" count, a value whose escapes are all quoted cannot
    be a flattened document: line breaks are never inside quotes.
    """
    command = (
        "printf '%s\\n' 'line-one\\n' 'line-two\\n' 'line-three\\n' "
        "'line-four\\n' 'line-five\\n' > /c/dev/repo/gen.txt && cat /c/dev/repo/gen.txt"
    )
    assert command.count("\\n") >= 6
    assert _count_unquoted_escaped_newlines(command) == 0
    assert _looks_escaped_newlines(command) is False
    _assert_reaches_tool(command)


def test_flattened_payload_separators_are_unquoted() -> None:
    """A real flattened payload has escapes outside quotes."""
    assert _count_unquoted_escaped_newlines(ESCAPED_BASH_SCRIPT) > 0
    assert _count_unquoted_escaped_newlines(ESCAPED_BASH_SCRIPT) == (
        ESCAPED_BASH_SCRIPT.count("\\n")
    )


def test_windows_backslash_paths_are_not_a_payload() -> None:
    """``C:\\new``-style arguments are not encoder output.

    ``\\n``, ``\\t`` and ``\\u`` all occur inside ordinary Windows paths, so
    the predicate must not read them as escaped line breaks.
    """
    command = (
        "type C:\\notes\\a.txt C:\\new\\b.txt C:\\new\\c.txt "
        "> C:\\temp\\all.txt && type C:\\users\\public\\b.txt > C:\\new\\d.txt"
    )
    assert command.count("\\n") >= 3
    assert _looks_escaped_newlines(command) is False
    assert _extract_content_from_malformed(command) is None


def test_real_newlines_are_never_flagged() -> None:
    """Text that already has real line breaks is not an escaped payload."""
    value = 'echo "a\\nb"\nfor t in x y; do echo $t; done\n'
    assert _looks_escaped_newlines(value) is False
    assert _extract_content_from_malformed(value) is None


def test_short_command_with_literal_escape_newline_untouched() -> None:
    """Short values never reach the extraction path (length gate intact)."""
    command = "echo hi | tr '\\n' ' '"
    assert len(command) < 200
    _assert_reaches_tool(command)


def test_json_encoded_string_param_still_extracted() -> None:
    """Case 1 (a JSON-quoted string) keeps working unchanged."""
    inner = "x" * 220
    assert _looks_like_malformed_json_param(f'"{inner}"') is True
    extracted = _extract_content_from_malformed(f'"{inner}"')
    assert extracted == inner


def test_list_param_for_bash_still_extracted() -> None:
    """A list where bash expects a string is still malformed."""
    lines = ["cd /c/dev/kimix-native", "for t in a b; do echo $t; done"]
    saved = _extract_and_save_long_param({"command": lines}, "bash")
    assert saved is not None
    path = Path(saved["command"])
    assert path.read_text(encoding="utf-8") == "\n".join(lines)
    path.unlink()


def test_subagent_batch_prompts_still_pass_through() -> None:
    """``subagent`` list prompts are valid multi-dispatch input."""
    prompts = ["first sub-task", "second sub-task"]
    assert _extract_and_save_long_param({"prompt": prompts}, "subagent") is None


def test_write_content_with_escaped_newlines_still_extracted() -> None:
    """``write`` content flattened onto one line keeps being repaired."""
    real_source = """#include <stdio.h>

int main(void) {
    printf("hello\\n");
    for (int i = 0; i < 3; i++) {
        printf("%d\\n", i);
    }
    fflush(stdout);
    fprintf(stderr, "done\\n");
    return 0;
}
"""
    content = _flatten(real_source)
    assert len(content) > 200 and "\n" not in content
    assert _looks_escaped_newlines(content) is True
    saved = _extract_and_save_long_param({"content": content}, "write")
    assert saved is not None
    saved_content = Path(saved["content"]).read_text(encoding="utf-8")
    # The decode is best-effort: every ``\n`` becomes a real newline, including
    # the ones that were the payload's *string literals*, so the saved text has
    # the right line structure but splits inside those literals.
    assert saved_content == real_source.replace("\\n", "\n")
    assert saved_content.startswith("#include <stdio.h>\n\nint main(void) {\n")
    Path(saved["content"]).unlink()


def _output_text(return_value: Any) -> str:
    output = return_value.output
    if isinstance(output, str):
        return output
    parts: list[str] = []
    for part in output if isinstance(output, list) else [output]:
        text = getattr(part, "text", None) or getattr(part, "think", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


@pytest.mark.skipif(find_bash() is None, reason="bash not found on this platform")
async def test_bash_call_with_literal_escape_newline_reaches_the_shell(
    session: Session,
) -> None:
    """End-to-end: the toolset must hand the command to bash, not reject it.

    This is the regression as the user saw it — ``ToolOk`` with the command's
    output instead of a ``Malformed parameter`` tool error.
    """
    try:
        tool = Bash(session)
    except SkipThisTool:
        pytest.skip("bash tool disabled on this platform")

    toolset = KimiToolset()
    toolset.add(tool)
    command = "echo marker-ok | tr '\\n' ' '   # padding " + "x" * 220
    assert len(command) > 200

    result = toolset.handle(
        ToolCall(
            id="tc-regression",
            function=ToolCall.FunctionBody(name="bash", arguments=json.dumps({"command": command})),
        )
    )
    assert isinstance(result, asyncio.Task)
    tool_result: ToolResult = await result

    assert not tool_result.return_value.is_error, _output_text(tool_result.return_value)
    text = _output_text(tool_result.return_value)
    assert "malformed parameter" not in text.lower()
    assert "marker-ok" in text
