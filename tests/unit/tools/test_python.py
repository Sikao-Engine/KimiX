"""Tests for Defects 2.1-2.3: Python tool improvements."""
from __future__ import annotations

import os
import warnings
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from kimix.tools.py import Params as PythonParams, Python


# ── Defect 2.1: code vs file split ───────────────────────────────────────


class TestPythonCodeFileSplit:
    def test_code_only_accepted(self) -> None:
        params = PythonParams(code="print(1+1)")
        assert params.code == "print(1+1)"
        assert params.file is None

    def test_file_only_accepted(self) -> None:
        params = PythonParams(file="script.py")
        assert params.code == ""
        assert params.file == "script.py"

    def test_both_code_and_file_rejected(self) -> None:
        with pytest.raises(ValidationError, match="not both"):
            PythonParams(code="print(1)", file="script.py")

    def test_neither_rejected_unless_interactive(self) -> None:
        with pytest.raises(ValidationError, match="Either.*code.*or.*file"):
            PythonParams()

    def test_neither_ok_when_interactive(self) -> None:
        params = PythonParams(interactive=True)
        assert params.code == ""
        assert params.file is None


# ── Defect 2.2: Unified mode parameter ──────────────────────────────────


class TestPythonUnifiedMode:
    @pytest.mark.parametrize("mode", ["run", "background", "interactive"])
    def test_all_modes_accepted(self, mode: str) -> None:
        params = PythonParams(code="print(1)", mode=mode)
        assert params.mode == mode

    def test_legacy_interactive_bool_still_works(self) -> None:
        params = PythonParams(interactive=True)
        assert params.mode == "interactive"

    def test_legacy_run_in_background_bool_still_works(self) -> None:
        params = PythonParams(code="print(1)", run_in_background=True)
        assert params.mode == "background"

    def test_both_legacy_bools_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Cannot set both"):
            PythonParams(code="print(1)", interactive=True, run_in_background=True)


# ── Defect 2.3: Venv support ────────────────────────────────────────────


class TestPythonVenvSupport:
    def test_venv_parameter_accepted(self) -> None:
        params = PythonParams(code="print(1)", venv="/some/venv")
        assert params.venv == "/some/venv"

    def test_pip_install_parameter_accepted(self) -> None:
        params = PythonParams(code="import requests", pip_install=["requests"])
        assert params.pip_install == ["requests"]

    def test_pip_install_default_none(self) -> None:
        params = PythonParams(code="print(1)")
        assert params.pip_install is None

    def test_venv_default_none(self) -> None:
        params = PythonParams(code="print(1)")
        assert params.venv is None

    # Integration-style: verify _resolve_python raises on missing venv
    def test_resolve_python_raises_on_missing_venv(self, mock_session: MagicMock) -> None:
        tool = Python(session=mock_session)
        params = PythonParams(code="print(1)", venv="/nonexistent/venv")
        with pytest.raises(ValueError, match="Venv python not found"):
            tool._resolve_python(params)
