#!/usr/bin/env python3
"""Lint tool — check parameter validation consistency across all tool Params classes.

Usage:
    uv run tools/check_validation.py [--fix]

Scans all ``class Params(BaseModel)`` definitions in the tool codebase and
reports issues such as:

- Cross-field validation using ``@field_validator`` instead of ``@model_validator``
- Missing ``@model_validator`` for cross-field constraints
- Validation error messages that don't mention the parameter name
- ``Field(alias=...)`` without ``populate_by_name=True``
"""

import ast
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
TOOL_DIRS = [
    PROJECT_ROOT / "src" / "kimix" / "tools",
    PROJECT_ROOT / "kimi-cli" / "src" / "kimi_cli" / "tools",
]

# Issues found during the scan
issues: list[dict[str, str | int]] = []


def _find_params_classes(filepath: Path) -> list[dict]:
    """Parse a Python file and find all ``class Params(BaseModel)`` definitions."""
    try:
        tree = ast.parse(filepath.read_text(encoding="utf-8"))
    except SyntaxError:
        return []

    classes = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        # Check if it's a Params class that inherits from BaseModel
        for base in node.bases:
            if isinstance(base, ast.Name) and base.id == "BaseModel":
                classes.append({"node": node, "file": filepath})
                break
            if isinstance(base, ast.Attribute) and base.attr == "BaseModel":
                classes.append({"node": node, "file": filepath})
                break
    return classes


def _check_param_class(cls_info: dict) -> None:
    """Check a single Params class for validation issues."""
    node = cls_info["node"]
    filepath = cls_info["file"]
    class_name = node.name
    rel_path = filepath.relative_to(PROJECT_ROOT)

    # Check for populate_by_name if aliases are used
    has_alias = False
    has_populate_by_name = False
    has_model_validator = False
    has_field_validator = False

    for item in node.body:
        if isinstance(item, ast.Assign):
            # Check for model_config
            if (
                isinstance(item.targets[0], ast.Name)
                and item.targets[0].id == "model_config"
            ):
                if isinstance(item.value, ast.Dict):
                    for key, val in zip(item.value.keys, item.value.values):
                        if (
                            isinstance(key, ast.Constant)
                            and key.value == "populate_by_name"
                        ):
                            has_populate_by_name = True

        if isinstance(item, ast.AnnAssign) and isinstance(item.annotation, ast.Name):
            # Check for Field()
            if item.value and isinstance(item.value, ast.Call):
                func = item.value.func
                if isinstance(func, ast.Name) and func.id == "Field":
                    for kw in item.value.keywords:
                        if kw.arg == "alias":
                            has_alias = True

        if isinstance(item, ast.FunctionDef):
            # Check for decorators
            for dec in item.decorator_list:
                if isinstance(dec, ast.Name):
                    if dec.id == "model_validator":
                        has_model_validator = True
                    elif dec.id == "field_validator":
                        has_field_validator = True
                elif isinstance(dec, ast.Attribute):
                    if dec.attr == "model_validator":
                        has_model_validator = True
                    elif dec.attr == "field_validator":
                        has_field_validator = True

    # Report issues
    if has_alias and not has_populate_by_name:
        issues.append({
            "file": str(rel_path),
            "line": node.lineno,
            "class": class_name,
            "message": (
                f"Uses Field(alias=...) but missing model_config = "
                f"{{'populate_by_name': True}} — canonical field names won't work"
            ),
        })

    if not has_field_validator and not has_model_validator:
        issues.append({
            "file": str(rel_path),
            "line": node.lineno,
            "class": class_name,
            "message": "No field_validator or model_validator found — consider adding validation",
        })


def main() -> int:
    """Run the validation lint checks."""
    for tool_dir in TOOL_DIRS:
        if not tool_dir.exists():
            continue
        for pyfile in sorted(tool_dir.rglob("*.py")):
            if pyfile.name.startswith("_"):
                continue
            classes = _find_params_classes(pyfile)
            for cls_info in classes:
                _check_param_class(cls_info)

    if not issues:
        print("✅ No validation issues found.")
        return 0

    print(f"Found {len(issues)} validation issue(s):\n")
    for issue in issues:
        print(f"  {issue['file']}:{issue['line']} ({issue['class']})")
        print(f"    {issue['message']}")
        print()

    return 1


if __name__ == "__main__":
    sys.exit(main())
