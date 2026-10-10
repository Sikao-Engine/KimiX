"""Tests for skill-dir auto-loading from .kimix/config.json (+ deprecated skill.json)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import kimix.base as base
from kimix.utils import config as config_mod


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    config_mod.clear_config_json_cache()
    monkeypatch.setattr(base, "_default_skill_dirs", [])
    yield
    config_mod.clear_config_json_cache()


def _write_kimix_file(name: str, content: str) -> None:
    kimix_dir = Path(".kimix")
    kimix_dir.mkdir(parents=True, exist_ok=True)
    (kimix_dir / name).write_text(content, encoding="utf-8")


def _skill_dirs() -> list[str]:
    return [str(p) for p in base._default_skill_dirs]


class TestSkillDirsFromConfigJson:
    def test_skill_dir_string_from_config_json(self) -> None:
        skill = Path("skills_a")
        skill.mkdir()
        _write_kimix_file("config.json", '{"skill_dir": "skills_a"}')

        config_mod._load_skill_dirs()

        assert _skill_dirs() == [str(skill.resolve())]

    def test_skill_dir_list_from_config_json(self) -> None:
        a = Path("skills_a")
        b = Path("skills_b")
        a.mkdir()
        b.mkdir()
        _write_kimix_file("config.json", '{"skill_dir": ["skills_a", "skills_b"]}')

        config_mod._load_skill_dirs()

        assert _skill_dirs() == [str(a.resolve()), str(b.resolve())]

    def test_missing_skill_dir_warns_but_continues(self) -> None:
        missing = Path("nope")
        _write_kimix_file("config.json", '{"skill_dir": "nope"}')

        config_mod._load_skill_dirs()

        assert _skill_dirs() == []
        assert not missing.exists()

    def test_non_string_entries_ignored(self) -> None:
        a = Path("skills_a")
        a.mkdir()
        _write_kimix_file("config.json", '{"skill_dir": [123, "skills_a", null]}')

        config_mod._load_skill_dirs()

        assert _skill_dirs() == [str(a.resolve())]

    def test_no_config_file_is_noop(self) -> None:
        config_mod._load_skill_dirs()
        assert _skill_dirs() == []


class TestDeprecatedSkillJson:
    def test_skill_json_warns_deprecated(self, capsys: pytest.CaptureFixture) -> None:
        a = Path("skills_legacy")
        a.mkdir()
        _write_kimix_file("skill.json", '{"skill_dir": "skills_legacy"}')

        warnings: list[str] = []
        import kimix.ui.printing as printing

        original = printing.print_warning
        printing.print_warning = lambda msg, *args, **kwargs: warnings.append(str(msg))
        try:
            config_mod._load_skill_dirs()
        finally:
            printing.print_warning = original

        assert any("deprecated" in w for w in warnings)
        assert _skill_dirs() == [str(a.resolve())]

    def test_both_sources_merged_and_deduped(self) -> None:
        a = Path("skills_a")
        b = Path("skills_b")
        a.mkdir()
        b.mkdir()
        _write_kimix_file("config.json", '{"skill_dir": ["skills_a", "skills_b"]}')
        _write_kimix_file("skill.json", '{"skill_dir": ["skills_b"]}')

        import kimix.ui.printing as printing

        original = printing.print_warning
        printing.print_warning = lambda msg, *args, **kwargs: None
        try:
            config_mod._load_skill_dirs()
        finally:
            printing.print_warning = original

        # skills_b appears in both files but only once in the result.
        assert _skill_dirs() == [str(a.resolve()), str(b.resolve())]

    def test_malformed_skill_json_warns(self) -> None:
        _write_kimix_file("skill.json", "not json")

        warnings: list[str] = []
        import kimix.ui.printing as printing

        original = printing.print_warning
        printing.print_warning = lambda msg, *args, **kwargs: warnings.append(str(msg))
        try:
            config_mod._load_skill_dirs()
        finally:
            printing.print_warning = original

        assert any("deprecated" in w for w in warnings)
        assert any("Failed to read skill_dir" in w for w in warnings)
        assert _skill_dirs() == []


class TestLoadConfigJsonCaching:
    def test_cache_hit_returns_same_object(self, tmp_path: Path) -> None:
        _write_kimix_file("config.json", '{"x": 1}')
        first = config_mod.load_config_json(Path.cwd())
        second = config_mod.load_config_json(Path.cwd())
        assert first is second
        assert first == {"x": 1}

    def test_clear_cache(self) -> None:
        _write_kimix_file("config.json", '{"x": 1}')
        first = config_mod.load_config_json(Path.cwd())
        config_mod.clear_config_json_cache()
        second = config_mod.load_config_json(Path.cwd())
        assert first is not second
        assert first == second

    def test_accepts_str_path(self) -> None:
        _write_kimix_file("config.json", '{"y": 2}')
        assert config_mod.load_config_json(str(Path.cwd())) == {"y": 2}

    def test_defaults_to_cwd(self) -> None:
        _write_kimix_file("config.json", '{"z": 3}')
        assert config_mod.load_config_json() == {"z": 3}
