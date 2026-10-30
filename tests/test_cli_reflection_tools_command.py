"""Tests for the generic ``/reflection_<tool>:<prompt_file_path>`` commands.

The command family generalizes ``/reflection_bash`` to the other builtin tools
listed in ``src/kimix/agent_worker.json``.  These tests pin the contract that
matters: the spec table covers exactly the requested tools, every cited source
path exists, every generated document keeps the reflection_bash skeleton, the
recorded evidence is complete (including failures, folding and unparsable
arguments), and every registered command writes the file it reports.
"""

from __future__ import annotations

import dataclasses
import importlib
import re
from pathlib import Path
from types import SimpleNamespace

import orjson
import pytest
from kosong.message import Message, TextPart, ToolCall

import kimix.cli_impl.commands as commands

#: The exact tool set requested for the reflection prompt family.
EXPECTED_TOOLS = (
    'pwsh',
    'python',
    'job_output',
    'todo_list',
    'read',
    'read_image',
    'write',
    'edit',
    'subagent',
    'list_agents',
    'glob',
    'grep',
    'fetch_url',
    'web_search',
)

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── helpers ──────────────────────────────────────────────────────────────


def _call(call_id: str, name: str, arguments: str | None) -> ToolCall:
    return ToolCall(id=call_id, function=ToolCall.FunctionBody(name=name, arguments=arguments))


def _assistant(*calls: ToolCall) -> Message:
    return Message(role="assistant", content="", tool_calls=list(calls))


def _result(call_id: str, *texts: str) -> Message:
    return Message(role="tool", content=[TextPart(text=t) for t in texts], tool_call_id=call_id)


def _session(history: list[Message], session_id: str = 'sess-reflection-tools') -> SimpleNamespace:
    return SimpleNamespace(
        id=session_id,
        _cli=SimpleNamespace(soul=SimpleNamespace(context=SimpleNamespace(history=history))),
    )


def _json_block(text: str) -> list[dict]:
    match = re.search(r'(?P<fence>`{3,})json\n(?P<body>.*?)\n(?P=fence)', text, re.DOTALL)
    assert match is not None, 'the prompt must contain a fenced json code block'
    return orjson.loads(match.group('body'))


def _context(session: SimpleNamespace | None = None) -> commands._ReflectionContext:
    return commands._reflection_context(Path('out.md'), session)


def _build(spec: commands._ReflectionToolSpec, invocations: list = ()) -> str:
    return commands._build_tool_reflection_prompt(spec, _context(), list(invocations))


_ABS_PATH_RE = re.compile(r'`([A-Za-z]:\\[^`\n]*)`')


def _cited_paths(prompt: str) -> list[Path]:
    """Absolute Windows paths cited by a prompt (commas/args stripped)."""
    paths: list[Path] = []
    for raw in _ABS_PATH_RE.findall(prompt):
        for part in raw.split(', '):
            candidate = part.strip().split(' ')[0].rstrip('`.,')
            if not re.match(r'^[A-Za-z]:\\', candidate):
                continue
            if '_report_' in candidate:
                continue
            paths.append(Path(candidate))
    return paths


def _specs() -> list[commands._ReflectionToolSpec]:
    return [commands._REFLECTION_TOOL_SPECS[tool] for tool in EXPECTED_TOOLS]


# ── spec table ───────────────────────────────────────────────────────────


def test_spec_table_covers_exactly_the_requested_tools():
    assert tuple(commands._REFLECTION_TOOL_NAMES) == EXPECTED_TOOLS
    assert tuple(commands._REFLECTION_TOOL_SPECS) == EXPECTED_TOOLS
    assert set(commands._REFLECTION_TOOL_SPECS) == set(EXPECTED_TOOLS)


def test_specs_only_cover_tools_defined_in_the_worker_manifest():
    manifest = orjson.loads((REPO_ROOT / 'src' / 'kimix' / 'agent_worker.json').read_bytes())
    entries = manifest['agent']['tools']

    from kimi_cli.tools import resolve_tool_class

    worker_tools: set[str] = set()
    for entry in entries:
        module_name, _, attr = entry.partition(':')
        module = importlib.import_module(module_name)
        tool_cls = resolve_tool_class(module, attr)
        assert tool_cls is not None, f'manifest entry {entry} does not resolve'
        worker_tools.add(tool_cls.name)

    assert set(EXPECTED_TOOLS) <= worker_tools
    # Exactly the requested subset: no extra spec sneaks in.
    assert set(commands._REFLECTION_TOOL_SPECS) == set(EXPECTED_TOOLS)


@pytest.mark.parametrize('spec', _specs(), ids=[spec.tool for spec in _specs()])
def test_spec_entry_resolves_to_the_same_tool(spec):
    module_name, _, class_name = spec.entry.partition(':')
    assert module_name and class_name

    module = importlib.import_module(module_name)
    tool_cls = getattr(module, class_name)
    assert getattr(tool_cls, 'name', None) == spec.tool

    # The entry module must live under the repo's kimi_cli tree.
    module_path = Path(module.__file__).resolve()
    assert commands._reflection_context(None).tools_src.parent in module_path.parents


@pytest.mark.parametrize('spec', _specs(), ids=[spec.tool for spec in _specs()])
def test_spec_command_name_is_a_dispatcher_safe_token(spec):
    assert spec.command == f'reflection_{spec.tool}'
    assert ':' not in spec.command
    assert spec.slug == spec.command
    assert spec.title


# ── registration ─────────────────────────────────────────────────────────


@pytest.mark.parametrize('spec', _specs(), ids=[spec.tool for spec in _specs()])
def test_command_is_registered(spec):
    handler = commands._command_map[spec.command]
    assert handler is commands._REFLECTION_COMMANDS[spec.command]
    assert callable(handler)
    assert handler.__name__ == f'_cmd_{spec.command}'
    assert spec.tool in (handler.__doc__ or '')
    assert commands._command_arg_types[spec.command] == 'file'


def test_registration_does_not_shadow_existing_commands():
    existing = {
        'help', 'clear', 'exit', 'context', 'cmd', 'fix', 'txt', 'file', 'plan',
        'compact', 'continue', 'export', 'resume', 'store', 'load', 'sessions',
        'reflection', 'reflection_bash', 'supervisor', 'swarm', 'init', 'todo', 'code',
    }
    assert set(commands._REFLECTION_COMMANDS).isdisjoint(existing)
    assert commands._command_map['reflection_bash'] is commands._cmd_reflection_bash

    # Every registration is also discoverable through the command-key set.
    for name in commands._REFLECTION_COMMANDS:
        assert name in commands._command_map_keys


# ── generated document ───────────────────────────────────────────────────


@pytest.mark.parametrize('spec', _specs(), ids=[spec.tool for spec in _specs()])
def test_prompt_has_the_reflection_bash_skeleton(spec):
    ctx = _context()
    prompt = commands._build_tool_reflection_prompt(spec, ctx, [])

    assert prompt.startswith(f'# {spec.title}')
    for section in (
        '## Context',
        '## Failed / unanswered invocations (quick index)',
        '## 1. Source of truth',
        '## 2. Architecture',
        '## 3. Workflow of one',
        '## 4. What to review',
        f'## 5. Observed `{spec.tool}` invocations',
        '## 6. How to reproduce and verify',
        '## 7. Deliverables',
    ):
        assert section in prompt, f'{spec.tool}: missing section {section!r}'

    assert f'- Recorded `{spec.tool}` invocations: 0 (0 not `ok`)' in prompt
    assert spec.entry in prompt
    assert spec.slug in prompt
    assert str(ctx.report(spec.slug)) in prompt
    assert 'uv run tools/syntax_check.py' in prompt
    assert 'regression' in prompt.lower()
    assert _json_block(prompt) == []


@pytest.mark.parametrize('spec', _specs(), ids=[spec.tool for spec in _specs()])
def test_prompt_cites_only_existing_paths(spec):
    prompt = _build(spec)
    paths = _cited_paths(prompt)
    assert paths, f'{spec.tool}: the prompt cites no source path at all'
    missing = [str(path) for path in paths if not path.exists()]
    assert missing == [], f'{spec.tool}: cites nonexistent paths {missing}'


@pytest.mark.parametrize('spec', _specs(), ids=[spec.tool for spec in _specs()])
def test_prompt_leaves_no_unrendered_placeholders(spec):
    prompt = _build(spec)
    for marker in ('{ctx', '{spec', '{tool', '{body', '{now'):
        assert marker not in prompt, f'{spec.tool}: unrendered placeholder {marker!r}'


@pytest.mark.parametrize('spec', _specs(), ids=[spec.tool for spec in _specs()])
def test_prompt_classifies_that_tools_invocations_only(monkeypatch, tmp_path, spec):
    seen: list[tuple[str, ...]] = []

    def fake_collect(session, *, tool_names=None, **kwargs):
        seen.append(tuple(tool_names or ()))
        return []

    monkeypatch.setattr(commands, 'get_default_session', lambda: _session([]))
    monkeypatch.setattr(commands, 'collect_tool_invocations', fake_collect)

    handler = commands._REFLECTION_COMMANDS[spec.command]
    assert handler([spec.command, str(tmp_path / 'prompt.md')], []) == (None, False)
    assert seen == [(spec.tool,)]


# ── evidence payload ─────────────────────────────────────────────────────


def _read_history() -> list[Message]:
    return [
        Message(role="user", content="please inspect the sources"),
        _assistant(
            _call("r1", "read", '{"path": "src/main.py", "offset": 1}'),
            _call("r2", "read", '{"file_path": ["a.py", "b.py"], "limit": 3}'),
            _call("r3", "read", '{"file_path": "missing.py"}'),
            _call("r4", "read", 'not json at all'),
        ),
        _result("r1", "<system>success</system>", "     1\tprint(1)"),
        _result("r2", "<system>success</system>", "two files"),
        _result("r3", "<system>ERROR: path not found</system>", "No such file"),
    ]


def test_prompt_records_every_invocation_with_extras():
    session = _session(_read_history())
    spec = commands._REFLECTION_TOOL_SPECS['read']
    invocations = commands.collect_tool_invocations(session, tool_names=('read',))
    prompt = commands._build_tool_reflection_prompt(spec, _context(session), invocations)
    entries = _json_block(prompt)

    assert [entry['call_id'] for entry in entries] == ['r1', 'r2', 'r3', 'r4']
    assert [entry['status'] for entry in entries] == ['ok', 'ok', 'failed', 'no_result']

    ok, multi, failed, unanswered = entries
    assert ok['target'] == 'src/main.py'
    assert ok['arguments'] == {'path': 'src/main.py', 'offset': 1}
    assert ok['files_requested'] == 1
    assert ok['message'] == 'success'
    assert ok['output'] == '     1\tprint(1)'

    assert multi['target'] == 'a.py, b.py'
    assert multi['files_requested'] == 2

    assert failed['message'] == 'path not found'
    assert failed['output'] == 'No such file'
    assert failed['files_requested'] == 1

    assert unanswered['message'] is None
    assert unanswered['output'] is None
    # The unparsable call keeps its raw payload for the reviewer.
    assert unanswered['arguments'] is None
    assert unanswered['arguments_raw'] == 'not json at all'
    assert 'arguments_error' in unanswered

    # Only failed/unanswered calls are in the quick index.
    index = prompt.split('## Failed / unanswered invocations', 1)[1].split('## 1.', 1)[0]
    assert 'invocation #3' in index
    assert 'invocation #4' in index
    assert 'path not found' in index
    assert 'invocation #1' not in index
    assert 'invocation #2' not in index


def test_prompt_folds_long_output_and_reports_the_original_size():
    long_output = 'z' * (commands._BASH_OUTPUT_LIMIT * 2)
    history = [
        _assistant(_call("c1", "grep", '{"pattern": "abc"}')),
        _result("c1", "<system>success</system>", long_output),
    ]
    session = _session(history)
    spec = commands._REFLECTION_TOOL_SPECS['grep']
    invocations = commands.collect_tool_invocations(session, tool_names=('grep',))
    prompt = commands._build_tool_reflection_prompt(spec, _context(session), invocations)
    (entry,) = _json_block(prompt)

    assert entry['output_truncated'] is True
    assert entry['output_characters'] == len(long_output)
    assert 'characters omitted' in entry['output']
    assert len(entry['output']) < len(long_output)


@pytest.mark.parametrize(
    ('tool', 'arguments', 'expected'),
    [
        ('write', {'file_path': 'a.py', 'content': 'x' * 7}, {'content_characters': 7, 'mode': 'overwrite'}),
        ('edit', {'file_path': 'a.py', 'edit': [{}, {}]}, {'edit_items': 2, 'mode': 'auto'}),
        ('edit', {'file_path': 'a.py', 'old_string': 'x', 'new_string': 'y'}, {'edit_items': 1}),
        ('subagent', {'prompt': ['a', 'b']}, {'prompts': 2, 'return_history': False}),
        ('todo_list', {'todos': [{'title': 'a'}]}, {'todo_items': 1, 'mode': 'merge'}),
        ('job_output', {'job_id': 'j1', 'action': 'kill'}, {'action': 'kill'}),
        ('pwsh', {'command': 'Get-Item .'}, {'mode': 'execute'}),
        ('fetch_url', {'url': 'https://example.com'}, {'markdown_characters': 0}),
    ],
)
def test_extras_are_derived_per_tool(tool, arguments, expected):
    invocation = SimpleNamespace(
        index=0,
        status='ok',
        tool_name=tool,
        call_id='c1',
        arguments=arguments,
        arguments_raw=None,
        arguments_error=None,
        message='success',
        output='',
        command=None,
    )
    entry = commands._reflection_invocation_payload(invocation, commands._REFLECTION_EXTRAS.get(tool))
    for key, value in expected.items():
        assert entry[key] == value


def test_extras_failures_never_break_the_prompt():
    def boom(arguments, output):  # pragma: no cover - executed, must be isolated
        raise RuntimeError('extras exploded')

    spec = dataclasses.replace(commands._REFLECTION_TOOL_SPECS['read'], extras=boom)
    invocation = SimpleNamespace(
        index=0,
        status='ok',
        tool_name='read',
        call_id='c1',
        arguments={'file_path': 'a.py'},
        arguments_raw=None,
        arguments_error=None,
        message='success',
        output='line',
        command=None,
    )
    prompt = _build(spec, [invocation])
    (entry,) = _json_block(prompt)
    assert entry['extras_error'] is True
    assert entry['output'] == 'line'


def test_target_falls_back_to_the_shell_command_key():
    invocation = SimpleNamespace(
        index=0,
        status='ok',
        tool_name='pwsh',
        call_id='c1',
        arguments={'cmd': 'Get-ChildItem'},
        command='Get-ChildItem',
    )
    assert commands._reflection_target(invocation) == 'Get-ChildItem'


def test_target_is_none_without_usable_arguments():
    invocation = SimpleNamespace(arguments=None, command=None)
    assert commands._reflection_target(invocation) is None


# ── command handler ──────────────────────────────────────────────────────


@pytest.mark.parametrize('spec', _specs(), ids=[spec.tool for spec in _specs()])
def test_cmd_writes_the_prompt_file(monkeypatch, tmp_path, capsys, spec):
    session = _session(_read_history())
    monkeypatch.setattr(commands, 'get_default_session', lambda: session)
    target = tmp_path / 'nested' / 'prompt.md'

    handler = commands._REFLECTION_COMMANDS[spec.command]
    assert handler([spec.command, str(target)], []) == (None, False)
    assert target.is_file()

    content = target.read_text(encoding='utf-8')
    assert content.startswith(f'# {spec.title}')
    assert session.id in content

    out = capsys.readouterr().out
    assert f'{spec.tool} invocation(s)' in out
    assert f'/file:{target}' in out


@pytest.mark.parametrize('spec', _specs(), ids=[spec.tool for spec in _specs()])
def test_cmd_requires_a_path(monkeypatch, capsys, spec):
    monkeypatch.setattr(commands, 'get_default_session', lambda: _session([]))
    handler = commands._REFLECTION_COMMANDS[spec.command]

    assert handler([spec.command], []) == (None, False)
    assert handler([spec.command, '   '], []) == (None, False)
    assert f'Command must be /{spec.command}:<prompt_file_path>' in capsys.readouterr().out


@pytest.mark.parametrize('spec', _specs(), ids=[spec.tool for spec in _specs()])
def test_cmd_requires_an_active_session(monkeypatch, tmp_path, capsys, spec):
    monkeypatch.setattr(commands, 'get_default_session', lambda: None)
    target = tmp_path / 'prompt.md'

    handler = commands._REFLECTION_COMMANDS[spec.command]
    assert handler([spec.command, str(target)], []) == (None, False)
    assert 'No active session' in capsys.readouterr().out
    assert not target.exists()


@pytest.mark.parametrize('spec', _specs(), ids=[spec.tool for spec in _specs()])
def test_cmd_relative_path_uses_curr_dir(monkeypatch, tmp_path, spec):
    monkeypatch.setattr(commands, 'get_default_session', lambda: _session([]))
    monkeypatch.setattr(commands.constants, 'curr_dir', tmp_path)

    handler = commands._REFLECTION_COMMANDS[spec.command]
    assert handler([spec.command, 'out/prompt.md'], []) == (None, False)
    assert (tmp_path / 'out' / 'prompt.md').is_file()


def test_cmd_survives_a_broken_context(monkeypatch, tmp_path, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError('context exploded')

    monkeypatch.setattr(commands, 'get_default_session', lambda: _session([]))
    monkeypatch.setattr(commands, 'collect_tool_invocations', boom)
    target = tmp_path / 'prompt.md'

    handler = commands._REFLECTION_COMMANDS['reflection_read']
    assert handler(['reflection_read', str(target)], []) == (None, False)
    assert 'Failed to read the session context' in capsys.readouterr().out
    assert not target.exists()


def test_cmd_reports_write_failures(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(commands, 'get_default_session', lambda: _session([]))
    target = tmp_path / 'is_a_dir'
    target.mkdir()

    handler = commands._REFLECTION_COMMANDS['reflection_read']
    assert handler(['reflection_read', str(target)], []) == (None, False)
    assert 'Failed to write' in capsys.readouterr().out


def test_cmd_warns_when_no_invocation_was_recorded(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(commands, 'get_default_session', lambda: _session([]))
    target = tmp_path / 'prompt.md'

    handler = commands._REFLECTION_COMMANDS['reflection_todo_list']
    assert handler(['reflection_todo_list', str(target)], []) == (None, False)
    out = capsys.readouterr().out
    assert 'Wrote 0 todo_list invocation(s)' in out
    assert 'No todo_list tool call was recorded' in out


# ── context / report path ────────────────────────────────────────────────


def test_report_path_uses_a_sortable_timestamp_and_the_slug():
    ctx = _context(_session([]))
    report = ctx.report('reflection_read')
    assert report.parent == ctx.repo_root / 'docs'
    assert report.name.startswith('reflection_read_report_')
    assert report.suffix == '.md'
    assert re.search(r'_report_\d{8}_\d{6}\.md$', report.name)


def test_context_paths_point_into_the_repository():
    ctx = _context()
    assert ctx.agent_src == REPO_ROOT / 'src' / 'kimix'
    assert ctx.tools_src == REPO_ROOT / 'kimi-cli' / 'src' / 'kimi_cli' / 'tools'
    assert ctx.tool('file', 'read.py').is_file()
    assert ctx.test('test_cli_reflection_tools_command.py').is_file()
    assert ctx.cli_test('tools', 'test_glob.py').is_file()
    assert ctx.python_path


def test_help_text_documents_the_command_family():
    from kimix.cli_impl import constants

    assert '/reflection_<tool>:<path>' in constants.HELP_STR
    for tool in EXPECTED_TOOLS:
        assert tool in constants.HELP_STR
