"""Repo-wide guardrails for the todo-tool merge.

The merge was a hard cutover: the two retired planning tools are gone from
code, configuration, prompts, manifests, docs and tests. These tests keep it
that way, and pin the structural invariants of the surviving tool.

The retired spellings are rebuilt from ``RETIRED_TODO_TOOL_NAMES`` so this file
does not contain a retired name either.
"""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

from pydantic import BaseModel

from kimi_cli.tools import RETIRED_TODO_TOOL_NAMES
from kimi_cli.tools.todo import Params, Todo, TodoList
from kosong.tooling import CallableTool, CallableTool2

RETIRED = list(RETIRED_TODO_TOOL_NAMES)
GUARD_FILE = Path(__file__).name

# Every planning-tool spelling that may not appear in tracked text: the two
# retired names themselves.
TEXT_SUFFIXES = {
    ".py", ".pyi", ".md", ".rst", ".txt", ".yaml", ".yml", ".json", ".jsonc",
    ".toml", ".cfg", ".ini", ".ts", ".tsx", ".js", ".jsx", ".html", ".css", ".rs",
}
# Recorded history and generated artefacts may legitimately mention the old
# names; source, config, docs and tests may not.
ALLOWED_NAMES = {"todo_plan.md", GUARD_FILE}
ALLOWED_STEMS = {"uv.lock", "poetry.lock", "package-lock"}
ALLOWED_PARTS = {".venv", "node_modules", "__pycache__", ".git", ".kimix_cache"}

REPO_ROOT = next(
    parent for parent in Path(__file__).resolve().parents if (parent / ".git").exists()
)


def _candidate_files() -> list[Path]:
    """Tracked plus untracked-but-not-ignored files, narrowed to text."""
    listing = subprocess.run(  # noqa: S603
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    ).stdout
    files: list[Path] = []
    for raw in listing.split(b"\0"):
        if not raw:
            continue
        relative = Path(raw.decode("utf-8", "replace"))
        if relative.name in ALLOWED_NAMES or relative.stem in ALLOWED_STEMS:
            continue
        if relative.suffix not in TEXT_SUFFIXES:
            continue
        if set(relative.parts) & ALLOWED_PARTS:
            continue
        path = REPO_ROOT / relative
        if path.is_file():
            files.append(path)
    return files


class TestNoRetiredNames:
    def test_repo_has_no_retired_todo_tool_literals(self) -> None:
        offenders: list[str] = []
        for path in _candidate_files():
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            hits = [retired for retired in RETIRED if retired in text]
            if hits:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {', '.join(hits)}")
        assert offenders == [], (
            "retired todo tool names are still present:\n" + "\n".join(offenders)
        )

    def test_the_scan_is_not_trivially_empty(self) -> None:
        # guards against an over-broad exclusion list silently passing the test
        assert len(_candidate_files()) > 500


class TestSingleToolShape:
    def test_exactly_one_todo_tool_is_exported(self) -> None:
        module = importlib.import_module("kimi_cli.tools.todo")
        tools = [
            value
            for name, value in vars(module).items()
            if isinstance(value, type)
            and not name.startswith("_")
            and issubclass(value, (CallableTool, CallableTool2))
            and value not in (CallableTool, CallableTool2)
        ]
        unique = {id(tool): tool for tool in tools}.values()
        assert [tool.__name__ for tool in unique] == ["TodoList"]
        assert {tool.name for tool in unique} == {TodoList.name}
        assert TodoList.name == "todo_list"
        # the lowercase module alias keeps ``module:todo_list`` manifest entries working
        module = importlib.import_module("kimi_cli.tools.todo")
        assert module.todo_list is TodoList

    def test_one_params_model_and_one_item_model(self) -> None:
        module = importlib.import_module("kimi_cli.tools.todo")
        models = {
            name: value
            for name, value in vars(module).items()
            if isinstance(value, type)
            and issubclass(value, BaseModel)
            and value is not BaseModel
        }
        assert {"Params", "Todo"} <= set(models), sorted(models)
        # no second params model survived the merge (the retired item/batch models are gone)
        assert [name for name in models if name.endswith("Params")] == ["Params"]
        assert [name for name in models if name == "TodoUpdateParams" or name == "TodoUpdateItem"] == []

    def test_item_canonical_keys(self) -> None:
        assert set(Todo.model_fields) == {
            "title",
            "status",
            "notes",
            "children",
            "parent",
            "rename_to",
            "complete",
            "fuzzy",
            "force",
        }
        assert set(Params.model_fields) == {
            "todos",
            "mode",
            "scope",
            "on_conflict",
            "fuzzy",
            "force",
            "auto_fix",
        }

    def test_no_retired_module_attributes(self) -> None:
        module = importlib.import_module("kimi_cli.tools.todo")
        for retired in RETIRED:
            camel: str = "".join(str(part).capitalize() for part in retired.split("_"))
            assert not hasattr(module, camel), camel
            assert not hasattr(module, retired), retired

    def test_the_wiring_target_is_the_surviving_class(self) -> None:
        from kimi_cli.tools import _MERGED_TOOL_NAME_TARGETS, resolve_tool_class  # noqa: PLC2701

        targets = _MERGED_TOOL_NAME_TARGETS["kimi_cli.tools.todo"]
        assert set(targets) == set(RETIRED)
        assert set(targets.values()) == {"TodoList"}
        module = importlib.import_module("kimi_cli.tools.todo")
        for name in sorted(set(targets) | {TodoList.name, "TodoList"}):
            assert resolve_tool_class(module, name) is TodoList, name
