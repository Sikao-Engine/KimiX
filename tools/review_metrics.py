"""Per-file simplicity metrics for the built-in tools review (plan §3 G1).

For every target ``.py`` file this prints::

    loc                 total source lines in the file (== ``module_loc``)
    module_loc          alias of ``loc`` (plan wording)
    functions[]         {name, loc, complexity, branches, max_nesting}
    longest_fn          {name, loc} of the biggest function
    module_complexity   max function complexity in the file

Cyclomatic complexity is computed on the function body only (nested
functions/lambdas/classes are pruned so their decisions are attributed to
themselves, not to the enclosing function).  ``branches`` counts the decision
nodes; ``max_nesting`` is the deepest nesting of compound statements.

Exit code is ``1`` when ``--max-fn-loc`` or ``--max-complexity`` is breached
(per file), ``0`` otherwise.  With ``--json`` a machine-readable document is
emitted instead of the human table.

Usage::

    uv run tools/review_metrics.py <file-or-dir> [<file-or-dir> ...] --json
    uv run tools/review_metrics.py src --max-fn-loc 120 --max-complexity 12
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

import orjson

# Decision nodes that add a distinct path through a function body.
_BRANCH_NODES = (
    ast.If,
    ast.IfExp,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.ExceptHandler,
    ast.comprehension,
    ast.Match,
)

# Compound statements that increase structural nesting depth.
_NESTING_NODES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.Match,
)

_FUNC_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _iter_all(node: ast.AST):
    """Yield *node* and every descendant, without descending into nested scopes."""
    yield node
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
        return
    for child in ast.iter_child_nodes(node):
        yield from _iter_all(child)


def _iter_body_nodes(func: ast.AST):
    """Yield every node reachable in *func*'s body (nested scopes excluded)."""
    for stmt in getattr(func, "body", []):
        yield from _iter_all(stmt)


def _count_decisions(func: ast.AST) -> tuple[int, int]:
    """Return (complexity, branches) for a function body.

    ``branches`` counts decision nodes (so ``complexity == branches + 1`` plus
    boolean operators); ``complexity`` is the standard cyclomatic number.
    """
    branches = 0
    for node in _iter_body_nodes(func):
        if isinstance(node, ast.BoolOp):
            branches += len(node.values) - 1
        elif isinstance(node, ast.comprehension):
            branches += len(node.ifs)
        elif isinstance(node, ast.Match):
            branches += len(node.cases)
        elif isinstance(node, _BRANCH_NODES):
            branches += 1
    return branches + 1, branches


def _complexity(func: ast.AST) -> int:
    return _count_decisions(func)[0]


def _count_branches(func: ast.AST) -> int:
    return _count_decisions(func)[1]


def _max_nesting(func: ast.AST) -> int:
    def depth(node: ast.AST, current: int) -> int:
        best = current
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                continue
            step = 1 if isinstance(child, _NESTING_NODES) else 0
            best = max(best, depth(child, current + step))
        return best

    return depth(func, 0)


def analyze_file(path: Path) -> dict:
    source = path.read_text(encoding="utf-8", errors="replace")
    display = str(path).replace("\\", "/")
    lines = source.splitlines()
    module_loc = len(lines)
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:  # pragma: no cover - syntax_check owns this
        return {
            "path": display,
            "loc": module_loc,
            "module_loc": module_loc,
            "syntax_error": f"{exc.msg} (line {exc.lineno})",
            "functions": [],
            "longest_fn": None,
            "module_complexity": 0,
        }

    functions: list[dict] = []
    for node in ast.walk(tree):
        if isinstance(node, _FUNC_NODES):
            end = node.end_lineno or node.lineno
            functions.append(
                {
                    "name": node.name,
                    "lineno": node.lineno,
                    "loc": end - node.lineno + 1,
                    "complexity": _complexity(node),
                    "branches": _count_branches(node),
                    "max_nesting": _max_nesting(node),
                }
            )

    longest = max(functions, key=lambda f: f["loc"], default=None)
    module_complexity = max((f["complexity"] for f in functions), default=0)
    return {
        "path": display,
        "loc": module_loc,
        "module_loc": module_loc,
        "functions": functions,
        "longest_fn": (
            {"name": longest["name"], "loc": longest["loc"]} if longest else None
        ),
        "module_complexity": module_complexity,
    }


def _expand_targets(targets: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in targets:
        p = Path(raw)
        if p.is_dir():
            files.extend(sorted(p.rglob("*.py")))
        elif p.suffix == ".py":
            files.append(p)
        else:
            # Explicit file of any type is accepted (best effort).
            files.append(p)
    # De-duplicate while preserving order.
    seen: set[str] = set()
    unique: list[Path] = []
    for f in files:
        key = str(f)
        if key not in seen and "__pycache__" not in f.parts:
            seen.add(key)
            unique.append(f)
    return unique


def _collect_breaches(
    reports: list[dict], max_fn_loc: int | None, max_complexity: int | None
) -> list[str]:
    breaches: list[str] = []
    for rep in reports:
        for fn in rep["functions"]:
            if max_fn_loc is not None and fn["loc"] > max_fn_loc:
                breaches.append(f"{rep['path']}::{fn['name']} loc={fn['loc']} > {max_fn_loc}")
            if max_complexity is not None and fn["complexity"] > max_complexity:
                breaches.append(
                    f"{rep['path']}::{fn['name']} complexity={fn['complexity']}"
                    f" > {max_complexity}"
                )
    return breaches


def _print_table(reports: list[dict], top: int) -> None:
    for rep in reports:
        if "syntax_error" in rep:
            print(f"{rep['path']}: SYNTAX ERROR: {rep['syntax_error']}")
            continue
        fns = sorted(rep["functions"], key=lambda f: f["loc"], reverse=True)
        if top:
            fns = fns[:top]
        longest = rep["longest_fn"]
        longest_txt = f"{longest['name']} ({longest['loc']})" if longest else "-"
        print(
            f"{rep['path']}: loc={rep['loc']} fns={len(rep['functions'])} "
            f"longest_fn={longest_txt} max_complexity={rep['module_complexity']}"
        )
        for fn in fns:
            print(
                f"    {fn['name']:<40} loc={fn['loc']:<5} "
                f"complexity={fn['complexity']:<4} branches={fn['branches']:<4} "
                f"nesting={fn['max_nesting']}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Per-file simplicity metrics (plan §3 G1).")
    parser.add_argument("targets", nargs="+", help="Python files or directories")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    parser.add_argument("--max-fn-loc", type=int, default=None, help="hard max function LOC")
    parser.add_argument(
        "--max-complexity", type=int, default=None, help="hard max cyclomatic complexity"
    )
    parser.add_argument(
        "--top", type=int, default=0, help="in table mode show only the N largest functions"
    )
    args = parser.parse_args(argv)

    files = _expand_targets(args.targets)
    if not files:
        print("review_metrics: no files matched", file=sys.stderr)
        return 1

    reports = [analyze_file(f) for f in files]
    breaches = _collect_breaches(reports, args.max_fn_loc, args.max_complexity)

    if args.json:
        payload = {
            "files": reports,
            "totals": {
                "files": len(reports),
                "loc": sum(r["loc"] for r in reports),
                "functions": sum(len(r["functions"]) for r in reports),
            },
            "breaches": breaches,
        }
        print(orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode())
    else:
        _print_table(reports, args.top)
        if breaches:
            print("\nBREACHES:", file=sys.stderr)
            for b in breaches:
                print(f"  {b}", file=sys.stderr)

    return 1 if breaches else 0


if __name__ == "__main__":
    sys.exit(main())
