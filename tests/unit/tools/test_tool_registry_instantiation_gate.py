"""Registry gate: every *registered* tool must be instantiable by the toolset.

`kimi_cli.soul.toolset` builds a tool by injecting **dependencies resolved from
`__init__` parameter annotations**. A parameter annotated with something it does
not know (or that cannot be resolved at all) aborts the whole session with
`ValueError: Tool dependency not found: ...`.

That is exactly what happened once `ParserTool` was registered (FP-03): its
`__init__(self, session: Any = None)` looked harmless while the class was an
orphan, but `Any` is not an injectable dependency, so
`tests/test_session_mcp_deferred.py` blew up with
`ValueError: Tool dependency not found: Any`.

These probes walk the agent manifests and pin, for every referenced tool class:

* the module imports and the class resolves;
* `typing.get_type_hints()` succeeds for its `__init__` (catches annotations
  whose names are no longer imported);
* no parameter is annotated `Any` (never injectable).
"""

from __future__ import annotations

import importlib
import inspect
import sys
import typing
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "tools"))

from review_tool_registry import ModuleIndex, manifest_refs  # noqa: E402

_SOURCES = ["kimi-cli/src/kimi_cli/tools", "src/kimix/tools"]
_INDEX = ModuleIndex(
    [p for root in _SOURCES for p in (REPO_ROOT / root).rglob("*.py") if "__pycache__" not in p.parts]
)


def _registered_tool_classes() -> list[tuple[str, str]]:
    """(ref, fq) for every manifest reference that resolves to a tool class."""
    out: list[tuple[str, str]] = []
    for ref in sorted(manifest_refs()):
        module_name, _, attr = ref.partition(":")
        if not module_name or not attr:
            continue
        fq = _INDEX.resolve(module_name, attr) or _INDEX.namespace_classes(module_name).get(attr)
        if fq:
            out.append((ref, fq))
    return out


REGISTERED = _registered_tool_classes()


def test_the_registry_finds_registered_tool_classes() -> None:
    assert len(REGISTERED) >= 25, f"only found {len(REGISTERED)} registered tool classes"


@pytest.mark.parametrize("ref,fq", REGISTERED, ids=[r for r, _ in REGISTERED])
def test_registered_tool_module_imports_and_class_exists(ref: str, fq: str) -> None:
    module_name, _, cls_name = fq.partition(":")
    module = importlib.import_module(module_name)
    assert isinstance(getattr(module, cls_name, None), type), f"{ref} -> {fq} is not a class"


@pytest.mark.parametrize("ref,fq", REGISTERED, ids=[r for r, _ in REGISTERED])
def test_registered_tool_init_hints_resolve(ref: str, fq: str) -> None:
    """`get_type_hints` must not raise: the toolset resolves deps that way."""
    module_name, _, cls_name = fq.partition(":")
    cls = getattr(importlib.import_module(module_name), cls_name)
    if "__init__" not in cls.__dict__:
        pytest.skip("uses the base __init__")
    try:
        typing.get_type_hints(cls.__init__)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"{fq}.__init__ annotations are unresolvable: {exc!r}")


@pytest.mark.parametrize("ref,fq", REGISTERED, ids=[r for r, _ in REGISTERED])
def test_registered_tool_init_has_no_any_parameter(ref: str, fq: str) -> None:
    """`Any` can never be injected, so it must not appear as a bare parameter."""
    module_name, _, cls_name = fq.partition(":")
    cls = getattr(importlib.import_module(module_name), cls_name)
    init = cls.__dict__.get("__init__")
    if init is None:
        pytest.skip("uses the base __init__")
    bad = [
        name
        for name, param in inspect.signature(init).parameters.items()
        if name != "self" and param.annotation is typing.Any
    ]
    assert bad == [], (
        f"{fq}.__init__ annotates {bad} as `Any`, which the toolset cannot inject"
    )
