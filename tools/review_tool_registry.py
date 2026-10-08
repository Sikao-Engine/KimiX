"""G-ORPHAN — tool-class registry / orphan detector (plan §3 G7).

Discovers every tool class (a class deriving from ``CallableTool`` /
``CallableTool2``) under the given source roots, then checks that each one is
reachable from an agent manifest:

* ``kimi-cli/src/kimi_cli/agents/**/*.yaml`` (``tools:`` entries)
* ``src/kimix/agent_*.json`` (``agent.tools`` entries)

Manifest entries are ``module:attr`` and are resolved the same way the runtime
does (``kimi_cli.tools.resolve_tool_class``):

1. ``attr`` names a symbol in ``module`` (following ``from .x import Y``
   re-exports and ``Alias = RealClass`` assignments);
2. otherwise ``attr`` matches the model-facing ``name`` of a class reachable in
   ``module``'s namespace (e.g. ``read`` -> ``ReadFile``).

A tool that is neither reachable from a manifest nor listed in
``tools/tool_registry_allowlist.txt`` (``module:Class  # rationale``) is an
ORPHAN.  Per plan §4.3 an orphan that is also untested is not an acceptable
terminal state — it must be registered, deleted, or allowlisted with a written
rationale and a follow-up.

Exit code: ``0`` when there are no unaccounted orphans, ``1`` otherwise.

Usage::

    uv run tools/review_tool_registry.py --sources kimi-cli/src/kimi_cli/tools src/kimix/tools
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

import regex as re

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ALLOWLIST = PROJECT_ROOT / "tools" / "tool_registry_allowlist.txt"

TOOL_BASES = {"CallableTool", "CallableTool2"}
SKIP_DIRS = {"__pycache__", ".venv", ".git"}

_REF_RE = re.compile(r'"([A-Za-z_][\w.]*):([A-Za-z_]\w*)"')
_YAML_REF_RE = re.compile(r'-\s*["\']?([A-Za-z_][\w.]*):([A-Za-z_]\w*)["\']?')


def _node_name(node: ast.expr | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _node_name(node.value)
    return ""


def _iter_py_files(roots: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in roots:
        p = Path(raw)
        if p.is_dir():
            files.extend(f for f in sorted(p.rglob("*.py")) if SKIP_DIRS.isdisjoint(f.parts))
        elif p.suffix == ".py":
            files.append(p)
    return files


def _module_of(file: str) -> str:
    """Map a source path to its dotted module name (source-root relative)."""
    norm = file.replace("\\", "/")
    idx = norm.find("kimi-cli/src/")
    if idx != -1:
        rel = norm[idx + len("kimi-cli/src/") :]
    else:
        idx = norm.find("src/")
        if idx == -1:
            rel = norm
        else:
            rel = norm[idx + len("src/") :]
    if rel.endswith(".py"):
        rel = rel[:-3]
    if rel.endswith("/__init__"):
        rel = rel[: -len("/__init__")]
    return rel.replace("/", ".")


def _is_package(file: str) -> bool:
    return file.replace("\\", "/").endswith("/__init__.py")


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


def _class_tool_name(cls: ast.ClassDef, consts: dict[str, str]) -> str:
    for stmt in cls.body:
        value: ast.expr | None = None
        target: ast.expr | None = None
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


class ModuleIndex:
    """Static module namespace index used to resolve manifest tool references."""

    def __init__(self, files: list[Path]) -> None:
        self.trees: dict[str, ast.Module] = {}
        self.paths: dict[str, str] = {}
        self.packages: set[str] = set()
        self.consts: dict[str, dict[str, str]] = {}
        self.bindings: dict[str, dict[str, tuple]] = {}
        self.tool_names: dict[tuple[str, str], str] = {}
        for path in files:
            raw = path.read_text(encoding="utf-8", errors="replace")
            try:
                tree = ast.parse(raw, filename=str(path))
            except SyntaxError:
                continue
            module = _module_of(str(path))
            self.trees[module] = tree
            self.paths[module] = str(path).replace("\\", "/")
            if _is_package(str(path)):
                self.packages.add(module)
            self.consts[module] = _module_constants(tree)
            self.bindings[module] = self._collect_bindings(module, tree)
            self._collect_tools(module, tree)

    # -- binding collection -------------------------------------------------
    def _collect_bindings(self, module: str, tree: ast.Module) -> dict[str, tuple]:
        bindings: dict[str, tuple] = {}
        for stmt in tree.body:
            if isinstance(stmt, ast.ClassDef):
                bindings[stmt.name] = ("class", stmt.name)
            elif isinstance(stmt, ast.ImportFrom):
                target = self._relative(module, stmt.level, stmt.module)
                for alias in stmt.names:
                    bindings[alias.asname or alias.name] = ("import", target, alias.name)
            elif (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and isinstance(stmt.value, ast.Name)
            ):
                bindings[stmt.targets[0].id] = ("alias", stmt.value.id)
        return bindings

    def _relative(self, module: str, level: int, modname: str | None) -> str:
        if level == 0:
            return modname or ""
        parts = module.split(".")
        base = parts if module in self.packages else parts[:-1]
        if level > 1:
            base = base[: len(base) - (level - 1)]
        if modname:
            base = [*base, *modname.split(".")]
        return ".".join(base)

    # -- tool class discovery ----------------------------------------------
    def _collect_tools(self, module: str, tree: ast.Module) -> None:
        consts = self.consts[module]
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if {_node_name(b) for b in node.bases}.isdisjoint(TOOL_BASES):
                continue
            self.tool_names[(module, node.name)] = _class_tool_name(node, consts)

    # -- resolution ---------------------------------------------------------
    def resolve(self, module: str, name: str, seen: frozenset[str] = frozenset()) -> str | None:
        binding = self.bindings.get(module, {}).get(name)
        if binding is None:
            return None
        if binding[0] == "class":
            fq = (module, binding[1])
            return f"{module}:{binding[1]}" if fq in self.tool_names else None
        target_mod = binding[1] if binding[0] == "import" else module
        target_name = binding[2] if binding[0] == "import" else binding[1]
        marker = f"{target_mod}:{target_name}"
        if marker in seen:
            return None
        return self.resolve(target_mod, target_name, seen | {marker})

    def namespace_classes(self, module: str) -> dict[str, str]:
        """Return {tool_name: fq} for every tool class reachable in *module*."""
        out: dict[str, str] = {}
        for name in self.bindings.get(module, {}):
            fq = self.resolve(module, name)
            if fq is None:
                continue
            mod, _, cls = fq.partition(":")
            tool_name = self.tool_names.get((mod, cls))
            if tool_name is not None:
                out[tool_name] = fq
        return out


def manifest_refs() -> set[str]:
    refs: set[str] = set()
    bases = [
        PROJECT_ROOT / "kimi-cli" / "src" / "kimi_cli" / "agents",
        PROJECT_ROOT / "src" / "kimix",
    ]
    for base in bases:
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.suffix not in (".yaml", ".yml", ".json") or "agent" not in path.name:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for mod, attr in _REF_RE.findall(text):
                refs.add(f"{mod}:{attr}")
            for mod, attr in _YAML_REF_RE.findall(text):
                refs.add(f"{mod}:{attr}")
    return refs


def load_allowlist(path: Path) -> set[str]:
    refs: set[str] = set()
    if not path.exists():
        return refs
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        body = line.partition("#")[0].strip()
        if body:
            refs.add(body)
    return refs


def registered_fqs(index: ModuleIndex, refs: set[str]) -> set[str]:
    """Resolve every manifest ``module:attr`` reference to a tool class fq."""
    out: set[str] = set()
    for ref in refs:
        module, _, attr = ref.partition(":")
        if not module or not attr:
            continue
        fq = index.resolve(module, attr)
        if fq is None:
            fq = index.namespace_classes(module).get(attr)
        if fq is not None:
            out.add(fq)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="G-ORPHAN tool registry / orphan detector.")
    parser.add_argument("--sources", nargs="+", required=True, help="source roots to scan")
    parser.add_argument("--allowlist", default=str(DEFAULT_ALLOWLIST))
    args = parser.parse_args(argv)

    index = ModuleIndex(_iter_py_files(args.sources))
    refs = manifest_refs()
    allow = load_allowlist(Path(args.allowlist))
    registered = registered_fqs(index, refs)

    rows: list[tuple[str, str, str, str]] = []
    for (module, cls), tool_name in sorted(index.tool_names.items()):
        fq = f"{module}:{cls}"
        if fq in registered:
            status = "registered"
        elif fq in allow:
            status = "allowlisted"
        else:
            status = "ORPHAN"
        rows.append((status, fq, tool_name, index.paths.get(module, "?")))

    orphans = [r for r in rows if r[0] == "ORPHAN"]
    print(f"# discovered tool classes: {len(rows)}")
    print(f"# registered in a manifest: {sum(1 for r in rows if r[0] == 'registered')}")
    print(f"# allowlisted (rationale recorded): {sum(1 for r in rows if r[0] == 'allowlisted')}")
    print(f"# ORPHANS: {len(orphans)}\n")
    for status, fq, tool_name, path in rows:
        print(f"{status:<11} {fq:<50} name={tool_name!r}  {path}")

    if orphans:
        print("\nORPHANS:", file=sys.stderr)
        for _s, fq, _n, path in orphans:
            print(f"  {fq}  ({path})", file=sys.stderr)
        print("G-ORPHAN: FAIL", file=sys.stderr)
        return 1
    print("\nG-ORPHAN: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
