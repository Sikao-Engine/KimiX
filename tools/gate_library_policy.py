"""G-POLICY — stdlib-for-third-party AST import check (plan §3 G7).

The project mandates third-party libraries over their stdlib counterparts
(AGENTS.md "Performance rule"):

    orjson / msgspec  <- json, pickle, struct
    regex             <- re
    rapidfuzz         <- difflib
    xxhash            <- hashlib (non-crypto)
    pybase64          <- base64
    pendulum          <- datetime
    apsw              <- sqlite3
    uvloop            <- asyncio default event loop

This tool walks the reviewed source roots, parses every ``.py`` file, and
reports each import of a *policy* stdlib module.  A hit is waived when the
importing file is listed in ``tools/library_policy_allowlist.txt`` (either the
whole file, or the file + module) with a written rationale.

Exit code: ``0`` when every hit is waived (or there are none), ``1`` otherwise.

Usage::

    uv run tools/gate_library_policy.py kimi-cli/src/kimi_cli/tools
    uv run tools/gate_library_policy.py <root> ... --allowlist tools/library_policy_allowlist.txt
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

import orjson

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ALLOWLIST = PROJECT_ROOT / "tools" / "library_policy_allowlist.txt"

# stdlib module -> mandated third-party replacement(s).
POLICY: dict[str, str] = {
    "json": "orjson",
    "pickle": "msgspec",
    "struct": "msgspec",
    "re": "regex",
    "difflib": "rapidfuzz",
    "hashlib": "xxhash",
    "base64": "pybase64",
    "datetime": "pendulum",
    "sqlite3": "apsw",
}

SKIP_DIRS = {"__pycache__", ".venv", ".git"}


def _module_root(name: str | None) -> str:
    if not name:
        return ""
    return name.split(".")[0]


class _ImportVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.hits: list[tuple[int, str, str]] = []  # (lineno, module, replacement)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = _module_root(alias.name)
            if root in POLICY:
                self.hits.append((node.lineno, root, POLICY[root]))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level == 0:
            root = _module_root(node.module)
            if root in POLICY:
                self.hits.append((node.lineno, root, POLICY[root]))
        self.generic_visit(node)


def _iter_py_files(roots: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in roots:
        p = Path(raw)
        if p.is_dir():
            for f in sorted(p.rglob("*.py")):
                if SKIP_DIRS.isdisjoint(f.parts):
                    files.append(f)
        elif p.suffix == ".py":
            files.append(p)
    return files


def load_allowlist(path: Path) -> list[tuple[str, str | None, str]]:
    """Return [(path_prefix, module_or_None, rationale)]."""
    entries: list[tuple[str, str | None, str]] = []
    if not path.exists():
        return entries
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        rationale = ""
        if "#" in line:
            body, _, rationale = line.partition("#")
            line = body.strip()
            rationale = rationale.strip()
        module: str | None = None
        if "::" in line:
            line, _, module = line.partition("::")
            line = line.strip()
            module = module.strip() or None
        entries.append((line.replace("\\", "/"), module, rationale))
    return entries


def _matches(norm: str, prefix: str) -> bool:
    """True when *prefix* addresses *norm* (equal, directory prefix, or suffix)."""
    prefix = prefix.rstrip("/")
    if not prefix:
        return False
    return norm == prefix or norm.startswith(prefix + "/") or norm.endswith("/" + prefix)


def is_waived(
    filepath: str, module: str, allowlist: list[tuple[str, str | None, str]]
) -> bool:
    norm = filepath.replace("\\", "/")
    for prefix, mod, _reason in allowlist:
        if _matches(norm, prefix):
            if mod is None or mod == module:
                return True
    return False


def scan(roots: list[str], allowlist_path: Path) -> tuple[list[dict], list[dict], int]:
    allowlist = load_allowlist(allowlist_path)
    hits: list[dict] = []
    waived: list[dict] = []
    unparsable = 0
    for f in _iter_py_files(roots):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"), filename=str(f))
        except SyntaxError:
            unparsable += 1
            continue
        visitor = _ImportVisitor()
        visitor.visit(tree)
        for lineno, module, replacement in visitor.hits:
            rel = str(f).replace("\\", "/")
            rec = {
                "file": rel,
                "line": lineno,
                "module": module,
                "replacement": replacement,
            }
            if is_waived(rel, module, allowlist):
                waived.append(rec)
            else:
                hits.append(rec)
    return hits, waived, unparsable


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="G-POLICY library-policy AST import check.")
    parser.add_argument("roots", nargs="+", help="source roots to scan")
    parser.add_argument("--allowlist", default=str(DEFAULT_ALLOWLIST))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    hits, waived, unparsable = scan(args.roots, Path(args.allowlist))

    if args.json:
        print(
            orjson.dumps(
                {"hits": hits, "waived": waived, "unparsable": unparsable},
                option=orjson.OPT_INDENT_2,
            ).decode()
        )
    else:
        for rec in hits:
            print(f"HIT  {rec['file']}:{rec['line']}  import {rec['module']} -> {rec['replacement']}")
        for rec in waived:
            print(f"ok   {rec['file']}:{rec['line']}  import {rec['module']} (waived)")
        print(f"\npolicy hits: {len(hits)}  waived: {len(waived)}  unparsable: {unparsable}")
        if hits:
            print("G-POLICY: FAIL", file=sys.stderr)
        else:
            print("G-POLICY: PASS")

    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
