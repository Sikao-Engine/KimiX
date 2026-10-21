"""Tests for ``{"json": "..."}`` sub-provider config links.

A ``sub_providers`` entry may be a pointer to another JSON config file
(``{"role": "sub_agent", "json": "subagent.json"}``) instead of an inline
provider dict. The referenced file is searched in the declaring file's
directory, then upward through its parents to the filesystem root; whatever it
contains is registered as sub-provider(s).

Covers:
  - ``config._resolve_linked_config`` -- upward search / absolute paths
  - ``config._expand_config_sub_provider_links`` -- expansion, nesting, cycles,
    overrides and failure modes
  - ``config._load_and_set_provider`` / ``config.init`` -- end-to-end
    registration into ``kimix.base`` globals
  - the ``_resolve_config_file`` / ``_load_config_file`` refactor
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import orjson
import pytest

import kimix.base as base
from kimix.utils import config as config_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_json(path: Path, data: Any) -> Path:
    """Write *data* as JSON to *path* (parents created) and return the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(orjson.dumps(data))
    return path


def _provider(
    model: str,
    role: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """A minimal but complete provider dict (all required keys present)."""
    entry: dict[str, Any] = {
        "model": model,
        "max_context_size": 100000,
        "url": f"https://api.example.com/{model}",
        "type": "openai_legacy",
        "api_key": f"sk-{model}",
    }
    if role is not None:
        entry["role"] = role
    entry.update(overrides)
    return entry


@pytest.fixture
def warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture ``print_warning`` messages emitted by the code under test."""
    captured: list[str] = []
    monkeypatch.setattr("kimix.ui.printing.print_warning", captured.append)
    return captured


# ---------------------------------------------------------------------------
# _resolve_linked_config
# ---------------------------------------------------------------------------


class TestResolveLinkedConfig:
    def test_same_directory(self, tmp_path: Path) -> None:
        target = _write_json(tmp_path / "linked-a.json", {"model": "m"})
        assert config_mod._resolve_linked_config("linked-a.json", tmp_path) == target.resolve()

    def test_ancestor_directory(self, tmp_path: Path) -> None:
        target = _write_json(tmp_path / "linked-b.json", {"model": "m"})
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        assert config_mod._resolve_linked_config("linked-b.json", deep) == target.resolve()

    def test_nearest_ancestor_wins(self, tmp_path: Path) -> None:
        _write_json(tmp_path / "linked-c.json", {"model": "far"})
        near = _write_json(tmp_path / "nested" / "linked-c.json", {"model": "near"})
        deep = tmp_path / "nested" / "deep"
        deep.mkdir(parents=True)
        assert config_mod._resolve_linked_config("linked-c.json", deep) == near.resolve()

    def test_subdirectory_reference(self, tmp_path: Path) -> None:
        target = _write_json(tmp_path / "shared" / "linked-d.json", {"model": "m"})
        assert (
            config_mod._resolve_linked_config("shared/linked-d.json", tmp_path) == target.resolve()
        )

    def test_absolute_path(self, tmp_path: Path) -> None:
        target = _write_json(tmp_path / "linked-e.json", {"model": "m"})
        assert config_mod._resolve_linked_config(str(target), tmp_path) == target.resolve()

    def test_absolute_path_missing(self, tmp_path: Path) -> None:
        assert config_mod._resolve_linked_config(str(tmp_path / "linked-f.json"), tmp_path) is None

    def test_missing_returns_none(self, tmp_path: Path) -> None:
        assert config_mod._resolve_linked_config("linked-absent-xyz.json", tmp_path) is None

    def test_directory_is_not_matched(self, tmp_path: Path) -> None:
        (tmp_path / "linked-dir-xyz.json").mkdir()
        assert config_mod._resolve_linked_config("linked-dir-xyz.json", tmp_path) is None


# ---------------------------------------------------------------------------
# _expand_config_sub_provider_links
# ---------------------------------------------------------------------------


class TestExpandSubProviderLinks:
    def test_root_fields_become_sub_provider(self, tmp_path: Path) -> None:
        _write_json(tmp_path / "subagent.json", _provider("sub-model"))
        config_data: dict[str, Any] = {
            "sub_providers": [{"role": "sub_agent", "json": "subagent.json"}]
        }

        config_mod._expand_config_sub_provider_links(config_data, tmp_path)

        assert config_data["sub_providers"] == [{**_provider("sub-model"), "role": "sub_agent"}]

    def test_linked_role_kept_when_link_omits_role(self, tmp_path: Path) -> None:
        _write_json(tmp_path / "subagent.json", _provider("sub-model", role="planner"))
        config_data: dict[str, Any] = {"sub_providers": [{"json": "subagent.json"}]}

        config_mod._expand_config_sub_provider_links(config_data, tmp_path)

        assert config_data["sub_providers"][0]["role"] == "planner"

    def test_link_role_overrides_linked_role(self, tmp_path: Path) -> None:
        _write_json(tmp_path / "subagent.json", _provider("sub-model", role="planner"))
        config_data: dict[str, Any] = {
            "sub_providers": [{"role": "sub_agent", "json": "subagent.json"}]
        }

        config_mod._expand_config_sub_provider_links(config_data, tmp_path)

        assert config_data["sub_providers"][0]["role"] == "sub_agent"

    def test_extra_link_keys_override_linked_fields(self, tmp_path: Path) -> None:
        _write_json(tmp_path / "subagent.json", _provider("sub-model"))
        config_data: dict[str, Any] = {
            "sub_providers": [
                {
                    "role": "sub_agent",
                    "json": "subagent.json",
                    "model": "override-model",
                    "api_key": "sk-override",
                }
            ]
        }

        config_mod._expand_config_sub_provider_links(config_data, tmp_path)

        entry = config_data["sub_providers"][0]
        assert entry["model"] == "override-model"
        assert entry["api_key"] == "sk-override"
        assert entry["url"] == "https://api.example.com/sub-model"

    def test_linked_nested_sub_providers_appended(self, tmp_path: Path) -> None:
        planner = _provider("planner-model", role="planner")
        _write_json(
            tmp_path / "subagent.json",
            {**_provider("sub-model"), "sub_providers": [planner]},
        )
        config_data: dict[str, Any] = {
            "sub_providers": [{"role": "sub_agent", "json": "subagent.json"}]
        }

        config_mod._expand_config_sub_provider_links(config_data, tmp_path)

        assert config_data["sub_providers"] == [
            {**_provider("sub-model"), "role": "sub_agent"},
            planner,
        ]

    def test_nested_link_resolved_from_linked_file_directory(self, tmp_path: Path) -> None:
        # subagent.json lives in tmp_path/shared and links "helper.json", which
        # only exists next to it -- not next to the declaring config.
        _write_json(tmp_path / "config.json", _provider("main-model"))
        helper = _provider("helper-model", role="planner")
        _write_json(tmp_path / "shared" / "helper.json", helper)
        _write_json(
            tmp_path / "shared" / "subagent.json",
            {**_provider("sub-model"), "sub_providers": [{"json": "helper.json"}]},
        )
        config_data: dict[str, Any] = {
            "sub_providers": [{"role": "sub_agent", "json": "shared/subagent.json"}]
        }

        config_mod._expand_config_sub_provider_links(config_data, tmp_path)

        assert config_data["sub_providers"] == [
            {**_provider("sub-model"), "role": "sub_agent"},
            helper,
        ]

    def test_inline_entries_untouched(self, tmp_path: Path) -> None:
        inline = _provider("inline-model", role="sub_agent")
        config_data: dict[str, Any] = {
            "sub_provider": _provider("single-model", role="backup"),
            "sub_providers": [inline],
        }

        config_mod._expand_config_sub_provider_links(config_data, tmp_path)

        assert config_data["sub_provider"] == [_provider("single-model", role="backup")]
        assert config_data["sub_providers"] == [inline]

    def test_singular_sub_provider_field_with_link(self, tmp_path: Path) -> None:
        _write_json(tmp_path / "subagent.json", _provider("sub-model", role="backup"))
        config_data: dict[str, Any] = {
            "sub_provider": {"json": "subagent.json"},
        }

        config_mod._expand_config_sub_provider_links(config_data, tmp_path)

        assert config_data["sub_provider"] == [_provider("sub-model", role="backup")]

    def test_absolute_link_path(self, tmp_path: Path) -> None:
        target = _write_json(tmp_path / "elsewhere" / "subagent.json", _provider("sub-model"))
        origin = tmp_path / "origin"
        origin.mkdir()
        config_data: dict[str, Any] = {
            "sub_providers": [{"role": "sub_agent", "json": str(target)}]
        }

        config_mod._expand_config_sub_provider_links(config_data, origin)

        assert config_data["sub_providers"] == [{**_provider("sub-model"), "role": "sub_agent"}]

    def test_missing_file_dropped_with_warning(self, tmp_path: Path, warnings: list[str]) -> None:
        config_data: dict[str, Any] = {
            "sub_providers": [{"role": "sub_agent", "json": "no-such-agent.json"}]
        }

        config_mod._expand_config_sub_provider_links(config_data, tmp_path)

        assert config_data["sub_providers"] == []
        assert any("no-such-agent.json" in w for w in warnings)

    @pytest.mark.parametrize("bad", [123, "", "   ", None, ["a.json"], {"p": 1}])
    def test_invalid_json_value_dropped(
        self, tmp_path: Path, warnings: list[str], bad: Any
    ) -> None:
        config_data: dict[str, Any] = {"sub_providers": [{"role": "sub_agent", "json": bad}]}

        config_mod._expand_config_sub_provider_links(config_data, tmp_path)

        assert config_data["sub_providers"] == []
        assert any("invalid" in w for w in warnings)

    def test_non_object_json_dropped(self, tmp_path: Path, warnings: list[str]) -> None:
        _write_json(tmp_path / "list.json", [1, 2, 3])
        config_data: dict[str, Any] = {"sub_providers": [{"json": "list.json"}]}

        config_mod._expand_config_sub_provider_links(config_data, tmp_path)

        assert config_data["sub_providers"] == []
        assert any("JSON object" in w for w in warnings)

    def test_invalid_json_syntax_dropped(self, tmp_path: Path, warnings: list[str]) -> None:
        (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
        config_data: dict[str, Any] = {"sub_providers": [{"json": "broken.json"}]}

        config_mod._expand_config_sub_provider_links(config_data, tmp_path)

        assert config_data["sub_providers"] == []
        assert any("Invalid JSON" in w for w in warnings)

    def test_self_link_ignored(self, tmp_path: Path, warnings: list[str]) -> None:
        _write_json(
            tmp_path / "self.json",
            {**_provider("self-model"), "sub_providers": [{"json": "self.json"}]},
        )
        config_data: dict[str, Any] = {"sub_providers": [{"json": "self.json"}]}

        config_mod._expand_config_sub_provider_links(config_data, tmp_path)

        assert config_data["sub_providers"] == [_provider("self-model")]
        assert any("Circular" in w for w in warnings)

    def test_two_file_cycle_terminates(self, tmp_path: Path, warnings: list[str]) -> None:
        _write_json(
            tmp_path / "cyc-a.json",
            {**_provider("a-model"), "sub_providers": [{"json": "cyc-b.json"}]},
        )
        _write_json(
            tmp_path / "cyc-b.json",
            {**_provider("b-model"), "sub_providers": [{"json": "cyc-a.json"}]},
        )
        config_data: dict[str, Any] = {"sub_providers": [{"json": "cyc-a.json"}]}

        config_mod._expand_config_sub_provider_links(config_data, tmp_path)

        assert [e["model"] for e in config_data["sub_providers"]] == ["a-model", "b-model"]
        assert any("Circular" in w for w in warnings)

    def test_depth_limit_stops_runaway_chain(self, tmp_path: Path, warnings: list[str]) -> None:
        depth = config_mod._MAX_SUB_PROVIDER_LINK_DEPTH
        total = depth + 4
        for i in range(total):
            entry: dict[str, Any] = _provider(f"chain-{i}")
            if i + 1 < total:
                entry["sub_providers"] = [{"json": f"chain-{i + 1}.json"}]
            _write_json(tmp_path / f"chain-{i}.json", entry)
        config_data: dict[str, Any] = {"sub_providers": [{"json": "chain-0.json"}]}

        config_mod._expand_config_sub_provider_links(config_data, tmp_path)

        models = [e["model"] for e in config_data["sub_providers"]]
        assert models == [f"chain-{i}" for i in range(depth)]
        assert any("nested too deep" in w for w in warnings)

    def test_non_dict_entries_preserved(self, tmp_path: Path) -> None:
        config_data: dict[str, Any] = {"sub_providers": ["junk", None, 7]}

        config_mod._expand_config_sub_provider_links(config_data, tmp_path)

        assert config_data["sub_providers"] == ["junk", None, 7]


# ---------------------------------------------------------------------------
# _load_and_set_provider / init integration
# ---------------------------------------------------------------------------


class TestLoadAndSetProviderWithLinks:
    @pytest.fixture(autouse=True)
    def _isolate_base_globals(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(base, "_default_provider", None)
        monkeypatch.setattr(base, "_default_sub_providers", [])

    def test_linked_provider_registered(self, tmp_path: Path) -> None:
        _write_json(tmp_path / "subagent.json", _provider("sub-model"))
        main = {
            **_provider("main-model"),
            "sub_providers": [{"role": "sub_agent", "json": "subagent.json"}],
        }

        config_mod._load_and_set_provider(main, tmp_path / "main.json")

        subs = base.get_default_sub_providers_by_role("sub_agent")
        assert len(subs) == 1
        assert subs[0]["model"] == "sub-model"
        assert subs[0]["type"] == "openai_legacy"
        assert subs[0]["role"] == "sub_agent"
        # Main provider is untouched by link expansion.
        assert base._default_provider["model"] == "main-model"

    def test_link_resolved_upward_from_config_subdirectory(self, tmp_path: Path) -> None:
        # subagent.json sits at the workspace root while the config that links
        # it lives in a nested directory.
        _write_json(tmp_path / "subagent.json", _provider("sub-model"))
        project = tmp_path / "project" / "configs"
        project.mkdir(parents=True)
        main = {
            **_provider("main-model"),
            "sub_providers": [{"role": "sub_agent", "json": "subagent.json"}],
        }

        config_mod._load_and_set_provider(main, project / "main.json")

        assert base.get_default_sub_provider("sub_agent")["model"] == "sub-model"

    def test_cwd_is_anchor_without_config_path(self, tmp_path: Path) -> None:
        _write_json(tmp_path / "subagent.json", _provider("sub-model"))
        main = {
            **_provider("main-model"),
            "sub_providers": [{"role": "sub_agent", "json": "subagent.json"}],
        }

        config_mod._load_and_set_provider(main, None)

        assert base.get_default_sub_provider("sub_agent")["model"] == "sub-model"

    def test_link_only_config_picks_linked_provider_as_main(self, tmp_path: Path) -> None:
        _write_json(tmp_path / "subagent.json", _provider("linked-model"))
        config_data: dict[str, Any] = {
            "sub_providers": [{"role": "sub_agent", "json": "subagent.json"}]
        }

        config_mod._load_and_set_provider(config_data, tmp_path / "main.json")

        assert base._default_provider["model"] == "linked-model"

    def test_inline_and_linked_entries_coexist(self, tmp_path: Path) -> None:
        _write_json(tmp_path / "subagent.json", _provider("linked-model"))
        inline = _provider("inline-model", role="planner")
        main = {
            **_provider("main-model"),
            "sub_providers": [
                inline,
                {"role": "sub_agent", "json": "subagent.json"},
                {"role": "backup", "json": "subagent.json"},
            ],
        }

        config_mod._load_and_set_provider(main, tmp_path / "main.json")

        assert base.get_default_sub_provider("planner") == inline
        assert base.get_default_sub_provider("sub_agent")["model"] == "linked-model"
        assert len(base.get_default_sub_providers_by_role("backup")) == 1

    def test_linked_missing_file_does_not_break_other_entries(
        self, tmp_path: Path, warnings: list[str]
    ) -> None:
        inline = _provider("inline-model", role="planner")
        main = {
            **_provider("main-model"),
            "sub_providers": [
                inline,
                {"role": "sub_agent", "json": "no-such-agent.json"},
            ],
        }

        config_mod._load_and_set_provider(main, tmp_path / "main.json")

        assert base.get_default_sub_provider("planner") == inline
        assert base.get_default_sub_provider("sub_agent") is None
        assert any("no-such-agent.json" in w for w in warnings)

    def test_init_registers_linked_sub_provider(self, tmp_path: Path) -> None:
        _write_json(tmp_path / "subagent.json", _provider("sub-model"))
        main = _write_json(
            tmp_path / "main.json",
            {
                **_provider("main-model"),
                "sub_providers": [{"role": "sub_agent", "json": "subagent.json"}],
            },
        )

        config_mod.init(config_path=main)

        assert base.get_default_sub_provider("sub_agent")["model"] == "sub-model"
        assert base._default_provider["model"] == "main-model"


# ---------------------------------------------------------------------------
# Regression: _resolve_config_file / _load_config_file
# ---------------------------------------------------------------------------


class TestConfigFileLoader:
    def test_resolve_returns_resolved_path(self, tmp_path: Path) -> None:
        target = _write_json(tmp_path / "prov.json", _provider("m"))
        assert config_mod._resolve_config_file(target) == target.resolve()

    def test_resolve_missing_returns_none(self, tmp_path: Path) -> None:
        assert config_mod._resolve_config_file(tmp_path / "prov-absent-xyz.json") is None

    def test_load_config_file_reads_object(self, tmp_path: Path) -> None:
        target = _write_json(tmp_path / "prov.json", {"model": "m", "url": "u"})
        assert config_mod._load_config_file(target) == {"model": "m", "url": "u"}

    def test_load_config_file_invalid_json_returns_empty(
        self, tmp_path: Path, warnings: list[str]
    ) -> None:
        broken = tmp_path / "prov.json"
        broken.write_text("{oops", encoding="utf-8")
        assert config_mod._load_config_file(broken) == {}
        assert any("Invalid JSON" in w for w in warnings)

    def test_load_config_file_missing_exits(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            config_mod._load_config_file(tmp_path / "prov-absent-xyz.json")


# ---------------------------------------------------------------------------
# Shipped docs example
# ---------------------------------------------------------------------------

_DOCS_DIR = Path(__file__).resolve().parents[1] / "docs"


@pytest.mark.skipif(
    not (_DOCS_DIR / "sub_provider_link.json").is_file(),
    reason="docs/sub_provider_link.json example not present",
)
def test_docs_sub_provider_link_example_resolves() -> None:
    """The shipped ``docs/sub_provider_link.json`` must expand to valid providers."""
    assert (_DOCS_DIR / "subagent.json").is_file(), "link target missing from docs/"

    config_data = orjson.loads((_DOCS_DIR / "sub_provider_link.json").read_bytes())
    config_mod._expand_config_sub_provider_links(config_data, _DOCS_DIR)

    entries = config_data["sub_providers"]
    # Link entry -> linked root (sub_agent) + its nested planner, then the inline backup.
    assert [e.get("role") for e in entries] == ["sub_agent", "planner", "backup"]
    for entry in entries:
        assert not (set(config_mod._REQUIRED_PROVIDER_KEYS) - set(entry)), entry
    assert entries[0]["model"] == "kimi-k2.6"
    assert entries[1]["type"] == "anthropic"

    # Every entry survives normalization.
    assert config_mod._normalize_sub_providers(None, entries) == entries
