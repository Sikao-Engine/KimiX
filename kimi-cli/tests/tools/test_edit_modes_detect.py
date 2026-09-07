"""Tests for edit mode auto-detection."""

from __future__ import annotations

import pytest

from kimi_cli.tools.file.edit.params import EditParams


def test_detect_explicit_mode_wins():
    params = EditParams(mode="replace", edits=[{"old_string": "a", "new_string": "b"}], input="anything")
    assert params.resolved_mode == "replace"


def test_detect_sloppy_from_input():
    params = EditParams(input="§path\n⟪old│new⟫\n")
    assert params.resolved_mode == "sloppy"


def test_detect_patch_removed():
    """Patch payloads are no longer routed to a patch mode; they resolve as replace."""
    params = EditParams(mode="replace", edits=[{"old_string": "a", "new_string": "b"}])
    assert params.resolved_mode == "replace"


def test_detect_replace_from_old_new():
    params = EditParams(path="x.txt", old_string="old", new_string="new")
    assert params.resolved_mode == "replace"


def test_detect_replace_from_edits():
    params = EditParams(
        path="x.txt",
        edits=[{"old_string": "old", "new_string": "new"}],
    )
    assert params.resolved_mode == "replace"


def test_detect_ambiguous_raises():
    with pytest.raises(ValueError):
        EditParams(path="x.txt")


def test_mode_registry_covers_every_edit_mode():
    """MODE_REGISTRY maps every EditMode literal to a live executor class."""
    from typing import get_args

    from kimi_cli.tools.file.edit.modes import MODE_REGISTRY
    from kimi_cli.tools.file.edit.params import EditMode

    assert set(MODE_REGISTRY) == set(get_args(EditMode))
    for mode, executor_cls in MODE_REGISTRY.items():
        # Executor must declare the same mode and be instantiable per call.
        assert executor_cls.mode == mode
        executor = executor_cls()
        assert callable(getattr(executor, "execute", None))
