"""Tests for the ``/reflection_bash:<prompt_file_path>`` command."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import orjson
import pytest
from kosong.message import Message, TextPart, ToolCall

import kimix.cli_impl.commands as commands

# ── helpers ──────────────────────────────────────────────────────────────


def _call(call_id: str, name: str, arguments: str | None) -> ToolCall:
    return ToolCall(id=call_id, function=ToolCall.FunctionBody(name=name, arguments=arguments))


def _assistant(*calls: ToolCall) -> Message:
    return Message(role="assistant", content="", tool_calls=list(calls))


def _result(call_id: str, *texts: str) -> Message:
    return Message(role="tool", content=[TextPart(text=t) for t in texts], tool_call_id=call_id)


def _session(history: list[Message]) -> SimpleNamespace:
    return SimpleNamespace(
        id="sess-reflection-bash",
        _cli=SimpleNamespace(soul=SimpleNamespace(context=SimpleNamespace(history=history, token_count=42))),
        status=SimpleNamespace(context_usage=0.5, context_tokens=42),
    )


def _history() -> list[Message]:
    return [
        Message(role="user", content="please list the dirs"),
        _assistant(
            _call("c1", "bash", '{"cmd": "ls D:\\\\repo\\\\src", "mode": "execute", "timeout": 120}'),
            _call("c2", "read", '{"path": "src/main.py"}'),
            _call("c3", "bash", '{"command": "cat /tmp/x > nul"}'),
            _call("c4", "bash", '{"cmd": "echo unterminated"}'),
        ),
        _result("c1", "<system>success</system>", "main.py"),
        _result("c2", "<system>success</system>", "print(1)"),
        _result(
            "c3",
            "<system>ERROR: failed Hint: nul: No such file</system>",
            "bash: line 1: nul: No such file or directory",
        ),
    ]


def _json_block(text: str) -> list[dict]:
    """Extract and parse the fenced JSON array of the prompt."""
    match = re.search(r'(?P<fence>`{3,})json\n(?P<body>.*?)\n(?P=fence)', text, re.DOTALL)
    assert match is not None, 'the prompt must contain a fenced json code block'
    return orjson.loads(match.group('body'))


def _norm(text: str) -> str:
    return text.replace('\\', '/').lower()


# ── prompt content ───────────────────────────────────────────────────────


def test_build_prompt_includes_fixer_path_and_architecture():
    session = _session(_history())
    invocations = commands.collect_tool_invocations(session, tool_names=commands._BASH_TOOL_NAMES)
    prompt = commands._build_bash_reflection_prompt(session, invocations, prompt_path=Path('out.md'))
    norm = _norm(prompt)

    # The bash fixer file path (the primary review target) and its package.
    assert 'bin/kimix_native/_shell_compat.py' in norm
    assert 'bin/kimix_native/shell_compat' in norm
    for module in (
        'models.py',
        'spans.py',
        'lexer.py',
        'parser.py',
        'translator.py',
        'ir_command.py',
        'bash_core.py',
        'paths.py',
        'wrappers.py',
        'cmd_translator.py',
        'heredoc.py',
        'fallback_data.py',
        'unquoted.py',
        'native_flags.py',
    ):
        assert module in prompt, f'architecture map is missing {module}'

    # Consumers of the fixer.
    assert 'kimi_cli/tools/file/bash/bash_fix.py' in norm
    assert 'kimi_cli/tools/file/bash/shell_common.py' in norm
    assert 'kimi_cli/tools/file/bash/bash_tool.py' in norm

    # Architecture + workflow vocabulary.
    for marker in (
        'fix_bash_command()',
        '_process_unquoted',
        'inspect_bash_command',
        '_prepare_bash_cmd',
        'translate()',
        'EditPlan',
        'RawNode',
        'unsupported',
        'byte-for-byte',
        'Workflow',
    ):
        assert marker in prompt, f'prompt is missing {marker!r}'

    # It is a task prompt: it must ask for a review of the IR parser and tests.
    assert '## 2. Architecture' in prompt
    assert '## 3. Workflow of one' in prompt
    assert 'regression' in prompt.lower()


def test_prompt_lists_every_bash_call_with_arguments_and_result():
    session = _session(_history())
    invocations = commands.collect_tool_invocations(session, tool_names=commands._BASH_TOOL_NAMES)
    prompt = commands._build_bash_reflection_prompt(session, invocations)
    entries = _json_block(prompt)

    assert [entry['call_id'] for entry in entries] == ['c1', 'c3', 'c4']
    assert [entry['index'] for entry in entries] == [1, 2, 3]
    assert [entry['status'] for entry in entries] == ['ok', 'failed', 'no_result']

    ok, failed, unanswered = entries
    # Full arguments are preserved (cmd/command alias + the other params).
    assert ok['arguments'] == {'cmd': 'ls D:\\repo\\src', 'mode': 'execute', 'timeout': 120}
    assert ok['command'] == 'ls D:\\repo\\src'
    assert ok['message'] == 'success'
    assert ok['output'] == 'main.py'

    # Failures are marked and keep message + output separately.
    assert failed['arguments'] == {'command': 'cat /tmp/x > nul'}
    assert failed['message'] == 'failed Hint: nul: No such file'
    assert failed['output'] == 'bash: line 1: nul: No such file or directory'

    # An unanswered call is listed instead of silently dropped.
    assert unanswered['message'] is None
    assert unanswered['output'] is None

    # The non-bash call is not part of the JSON listing.
    assert 'src/main.py' not in prompt

    # Each command carries what the fixer currently produces for it.
    assert all('ir_fix' in entry for entry in entries)
    assert all('command' in entry['ir_fix'] for entry in entries)
    assert ok['ir_fix']['platform'] in {'win32', 'linux', 'darwin'}


def test_prompt_marks_failed_calls_in_the_quick_index():
    session = _session(_history())
    invocations = commands.collect_tool_invocations(session, tool_names=commands._BASH_TOOL_NAMES)
    prompt = commands._build_bash_reflection_prompt(session, invocations)

    index_section = prompt.split('## Failed / unanswered invocations', 1)[1].split('## 1.', 1)[0]
    assert 'invocation #2' in index_section
    assert 'failed Hint: nul: No such file' in index_section
    assert 'invocation #3' in index_section
    # The successful invocation is not part of the failure index.
    assert 'invocation #1' not in index_section
    # Counts are reported in the context block.
    assert 'Recorded bash invocations: 3 (2 not `ok`)' in prompt


def test_prompt_without_bash_calls_says_so():
    session = _session([Message(role="user", content="hi")])
    prompt = commands._build_bash_reflection_prompt(session, [])
    assert 'Recorded bash invocations: 0 (0 not `ok`)' in prompt
    assert 'every recorded bash call succeeded' in prompt
    assert _json_block(prompt) == []


def test_prompt_reports_missing_result_messages_and_truncation():
    long_output = 'y' * (commands._BASH_OUTPUT_LIMIT * 2)
    history = [
        _assistant(_call("c1", "bash", '{"cmd": "make"}')),
        _result("c1", "<system>ERROR: failed</system>", long_output),
    ]
    entries = _json_block(
        commands._build_bash_reflection_prompt(
            _session(history),
            commands.collect_tool_invocations(_session(history), tool_names=commands._BASH_TOOL_NAMES),
        )
    )
    (entry,) = entries
    assert entry['output_truncated'] is True
    assert entry['output_characters'] == len(long_output)
    assert 'characters omitted' in entry['output']
    assert len(entry['output']) < len(long_output)


# ── helpers ──────────────────────────────────────────────────────────────


def test_fold_text_keeps_head_and_tail():
    text = 'HEAD' + ('x' * 100) + 'TAIL'
    folded, truncated = commands._fold_text(text, limit=20, head=8)
    assert truncated is True
    assert folded.startswith('HEADxxxx')
    assert folded.endswith('TAIL')
    assert 'characters omitted' in folded

    short, truncated = commands._fold_text('short', limit=20, head=8)
    assert (short, truncated) == ('short', False)


def test_code_block_escalates_the_fence():
    assert commands._code_block('plain', 'json').startswith('```json\n')
    fenced = commands._code_block('```\ninner\n```', 'json')
    assert fenced.startswith('````json\n')
    assert fenced.endswith('\n````')


def test_bash_ir_fix_is_isolated_from_fixer_failures(monkeypatch):
    import kimi_cli.tools.file.bash.shell_common as shell_common

    def _boom(command: str):
        raise RuntimeError('fixer exploded')

    monkeypatch.setattr(shell_common, 'inspect_bash_command', _boom)
    assert commands._bash_ir_fix('ls') == {'error': 'RuntimeError: fixer exploded'}


# ── command handler ──────────────────────────────────────────────────────


def test_cmd_reflection_bash_writes_the_prompt_file(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(commands, 'get_default_session', lambda: _session(_history()))
    target = tmp_path / 'nested' / 'prompt.md'

    assert commands._cmd_reflection_bash(['reflection_bash', str(target)], []) == (None, False)
    assert target.is_file()

    content = target.read_text(encoding='utf-8')
    assert content.startswith('# Bash IR Reflection Task')
    assert str(target) in content
    assert 'sess-reflection-bash' in content
    assert [entry['call_id'] for entry in _json_block(content)] == ['c1', 'c3', 'c4']

    out = capsys.readouterr().out
    assert 'Wrote 3 bash invocation(s) (2 not ok)' in out
    assert f'/file:{target}' in out


def test_cmd_reflection_bash_relative_path_uses_curr_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(commands, 'get_default_session', lambda: _session(_history()))
    monkeypatch.setattr(commands.constants, 'curr_dir', tmp_path)

    assert commands._cmd_reflection_bash(['reflection_bash', 'out/prompt.md'], []) == (None, False)
    assert (tmp_path / 'out' / 'prompt.md').is_file()


def test_cmd_reflection_bash_requires_a_path(monkeypatch, capsys):
    monkeypatch.setattr(commands, 'get_default_session', lambda: _session(_history()))
    assert commands._cmd_reflection_bash(['reflection_bash'], []) == (None, False)
    assert commands._cmd_reflection_bash(['reflection_bash', '   '], []) == (None, False)
    assert 'Command must be /reflection_bash:<prompt_file_path>' in capsys.readouterr().out


def test_cmd_reflection_bash_requires_an_active_session(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(commands, 'get_default_session', lambda: None)
    target = tmp_path / 'prompt.md'
    assert commands._cmd_reflection_bash(['reflection_bash', str(target)], []) == (None, False)
    assert 'No active session' in capsys.readouterr().out
    assert not target.exists()


def test_cmd_reflection_bash_survives_a_broken_context(monkeypatch, tmp_path, capsys):
    broken = SimpleNamespace(
        id='broken',
        _cli=SimpleNamespace(soul=SimpleNamespace(context='not-a-context')),
        status=SimpleNamespace(context_usage=0.0, context_tokens=0),
    )
    monkeypatch.setattr(commands, 'get_default_session', lambda: broken)
    target = tmp_path / 'prompt.md'

    assert commands._cmd_reflection_bash(['reflection_bash', str(target)], []) == (None, False)
    assert target.is_file()
    out = capsys.readouterr().out
    assert 'Wrote 0 bash invocation(s)' in out
    assert 'No bash tool call was recorded' in out


def test_cmd_reflection_bash_reports_write_failures(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(commands, 'get_default_session', lambda: _session(_history()))
    # A directory is not a writable prompt file.
    target = tmp_path / 'is_a_dir'
    target.mkdir()

    assert commands._cmd_reflection_bash(['reflection_bash', str(target)], []) == (None, False)
    assert 'Failed to write' in capsys.readouterr().out


@pytest.mark.parametrize('name', ['reflection_bash'])
def test_command_is_registered(name):
    assert commands._command_map[name] is commands._cmd_reflection_bash
    assert commands._command_arg_types[name] == 'file'
    # The dispatcher splits "/reflection_bash:<path>" on the first ':'.
    task = 'reflection_bash:C:\\tmp\\prompt.md'
    split_idx = task.find(':')
    assert [task[:split_idx], task[split_idx + 1:]] == ['reflection_bash', 'C:\\tmp\\prompt.md']


def test_help_text_documents_the_command():
    from kimix.cli_impl import constants

    assert '/reflection_bash:<path>' in constants.HELP_STR
