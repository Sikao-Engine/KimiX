"""G-DUP — divergent duplicate tool detector (plan §3 G7).

Scans the reviewed source roots for tool classes (``CallableTool`` /
``CallableTool2`` subclasses) and groups them by their model-facing ``name``.
A tool name with more than one implementation is a *duplicate*: either the
implementations are the same tool defined twice (divergence risk), or one is a
thin delegator whose forwarding is proven by a parity test.

Exit code: ``0`` when every tool name has exactly one implementation, or the
extra ones are waived via ``--allow-dup NAME`` / an allowlist file (each waiver
must cite the parity test that proves the delegator forwards exactly).
``1`` otherwise.

Usage::

    uv run tools/gate_dup.py kimi-cli/src/kimi_cli/tools src/kimix/tools
    uv run tools/gate_dup.py <roots...> --allow-dup fetch_url:parity test path
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ALLOWLIST = PROJECT_ROOT / "tools" / "gate_dup_allowlist.txt"

TOOL_BASES = {"CallableTool", "CallableTool2"}
SKIP_DIRS = {"__pycache__", ".venv", ".git"}


def _node_name(node: ast.expr | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _node_name(node.value)
    return ""


def _module_constants(tree: ast.Module) -> dict[str, str]:
    consts: dict[str, str] = {}
    for stmt in tree.body:
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            consts[stmt.targets[0].id] = stmt.value.value
    return consts


def _tool_name(cls: ast.ClassDef, consts: dict[str, str]) -> str:
    for stmt in cls.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            target, value = stmt.target, stmt.value
        if isinstance(target, ast.Name) and target.id == "name":
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value
            if isinstance(value, ast.Name) and value.id in consts:
                return consts[value.id]
    return cls.name


def discover(roots: list[str]) -> list[dict]:
    out: list[dict] = []
    for raw in roots:
        p = Path(raw)
        files = (
            [f for f in sorted(p.rglob("*.py")) if SKIP_DIRS.isdisjoint(f.parts)]
            if p.is_dir()
            else ([p] if p.suffix == ".py" else [])
        )
        for f in files:
            try:
                tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"), filename=str(f))
            except SyntaxError:
                continue
            consts = _module_constants(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                if {_node_name(b) for b in node.bases}.isdisjoint(TOOL_BASES):
                    continue
                out.append(
                    {
                        "name": _tool_name(node, consts),
                        "class": node.name,
                        "file": str(f).replace("\\", "/"),
                        "lineno": node.lineno,
                    }
                )
    return out


def load_allowlist(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    if not path.exists():
        return entries
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, _, rationale = line.partition("#")
        entries[name.strip()] = rationale.strip()
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="G-DUP duplicate tool detector.")
    parser.add_argument(
        "roots",
        nargs="*",
        default=["kimi-cli/src/kimi_cli/tools", "src/kimix/tools"],
        help="source roots to scan (default: both reviewed tool trees)",
    )
    parser.add_argument("--allowlist", default=str(DEFAULT_ALLOWLIST))
    parser.add_argument(
        "--allow-dup",
        action="append",
        default=[],
        metavar="NAME",
        help="waive a duplicate tool name (repeatable)",
    )
    args = parser.parse_args(argv)

    tools = discover(args.roots)
    by_name: dict[str, list[dict]] = {}
    for tool in tools:
        by_name.setdefault(tool["name"], []).append(tool)

    allowed = dict(load_allowlist(Path(args.allowlist)))
    for name in args.allow_dup:
        allowed.setdefault(name, "waived via --allow-dup")

    duplicates = {n: ts for n, ts in by_name.items() if len(ts) > 1}
    failing = {n: ts for n, ts in duplicates.items() if n not in allowed}

    print(f"# tool classes discovered: {len(tools)}")
    print(f"# distinct tool names: {len(by_name)}")
    print(f"# duplicated names: {len(duplicates)}")
    for name, ts in sorted(duplicates.items()):
        mark = "WAIVED" if name in allowed else "FAIL"
        print(f"\n[{mark}] {name} ({len(ts)} implementations)")
        for t in ts:
            print(f"    {t['class']:<28} {t['file']}:{t['lineno']}")
        if name in allowed:
            print(f"    rationale: {allowed[name]}")

    if failing:
        print("\nG-DUP: FAIL (unwaived divergent duplicates)", file=sys.stderr)
        return 1
    print("\nG-DUP: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
