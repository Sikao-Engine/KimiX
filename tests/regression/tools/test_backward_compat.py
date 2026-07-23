"""Backward compatibility regression tests.

Every deprecated parameter name / behavior must keep working.
This test suite gates removal of any backward-compat shim.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

# ── Parameter aliases ─────────────────────────────────────────────────────


class TestBackwardCompatibility:
    """Bash: both 'cmd' and 'command' work."""

    def test_bash_cmd_alias(self) -> None:
        from kimix.tools.file.bash.bash_tool import BashParams
        p1 = BashParams(cmd="echo a")
        p2 = BashParams(command="echo a")
        assert p1.cmd == p2.cmd

    def test_run_command_alias(self) -> None:
        from kimix.tools.file.run import RunParams
        p1 = RunParams(command="git status")
        p2 = RunParams(cmd="git status")
        assert p1.command == p2.command

    def test_taskoutput_block_alias(self) -> None:
        from kimix.tools.background import TaskOutputParams
        p1 = TaskOutputParams(task_id="t1", block=True)
        p2 = TaskOutputParams(task_id="t1", wait=True)
        assert p1.wait == p2.wait

    def test_bash_token_kill_alias(self) -> None:
        from kimix.tools.file.bash.bash_tool import BashParams
        p1 = BashParams(cmd="test", token_kill=False)
        p2 = BashParams(cmd="test", deduplicate_output=False)
        assert p1.deduplicate_output == p2.deduplicate_output

    def test_python_interactive_legacy_bool(self) -> None:
        from kimix.tools.py import Params as PythonParams
        params = PythonParams(interactive=True)
        assert params.mode == "interactive"

    def test_python_run_in_background_legacy_bool(self) -> None:
        from kimix.tools.py import Params as PythonParams
        params = PythonParams(code="print(1)", run_in_background=True)
        assert params.mode == "background"

    def test_editfile_single_edit_auto_wrap(self) -> None:
        from kimi_cli.tools.file.replace import Params as EditFileParams
        params = EditFileParams(path="f.txt", edit={"old": "a", "new": "b"})
        assert isinstance(params.edit, list)
        assert len(params.edit) == 1

    def test_todolist_append_mode_still_default(self) -> None:
        from kimi_cli.tools.todo import Params as TodoListParams, Todo
        params = TodoListParams(todos=[Todo(title="task", status="pending")])
        assert params.mode == "append"

    def test_readfile_path_alias_files(self) -> None:
        """'files' and 'paths' aliases should still work via field_aliases."""
        # These are handled by the tool's field_aliases, not Pydantic
        from kimi_cli.tools.file.read import ReadFile
        assert "files" in ReadFile.field_aliases
        assert ReadFile.field_aliases["files"] == "path"

    def test_grep_cli_aliases_in_field_aliases(self) -> None:
        """-A, -B, -C, -n, -i aliases must be registered."""
        from kimi_cli.tools.file.grep_local import Grep
        assert "-A" in Grep.field_aliases
        assert "-B" in Grep.field_aliases
        assert "-C" in Grep.field_aliases
        assert "-n" in Grep.field_aliases
        assert "-i" in Grep.field_aliases
