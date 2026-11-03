"""Manifest registration probes: every tool entry in ``src/kimix/agent_*.json``
must be valid JSON and resolve to an importable tool class.

The manifests are the source of truth for which tools an agent gets; these
tests pin that entries never dangle and that the planner can retrieve
background research runs (``job_output`` + ``research``).
"""
from __future__ import annotations

import importlib
from pathlib import Path

import orjson
from kimi_cli.tools import resolve_tool_class

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFESTS = (
    'agent_worker.json',
    'agent_planner.json',
    'agent_boss.json',
    'agent_subagent.json',
    'agent_readonly.json',
)


def _entries(manifest: str) -> list[str]:
    data = orjson.loads((REPO_ROOT / 'src' / 'kimix' / manifest).read_bytes())
    return data['agent']['tools']


def test_all_manifest_entries_resolve_to_tool_classes():
    for manifest in MANIFESTS:
        entries = _entries(manifest)
        assert entries, f'{manifest}: empty tool list'
        for entry in entries:
            module_name, _, attr = entry.partition(':')
            module = importlib.import_module(module_name)
            tool_cls = resolve_tool_class(module, attr)
            assert tool_cls is not None, f'{manifest}: entry {entry} does not resolve'


def test_worker_and_planner_have_research():
    for manifest in ('agent_worker.json', 'agent_planner.json'):
        names = {entry.partition(':')[2] for entry in _entries(manifest)}
        assert 'research' in names, f'{manifest}: missing kimi_cli.tools.research:research'


def test_planner_has_job_output_to_retrieve_background_research():
    names = {entry.partition(':')[2] for entry in _entries('agent_planner.json')}
    assert 'job_output' in names
