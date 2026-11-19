"""Tests for backup-provider failover in ``kimix.utils.prompt``.

Covers:
  - ``base.get_default_sub_providers_by_role`` (unit)
  - ``prompt._provider_key`` (unit)
  - ``config._normalize_sub_providers`` with multiple backup roles
  - ``prompt._run_single_prompt`` failover scenarios (mock-based)
"""
from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

import kimix.base as base
from kimix.utils import config as config_mod

prompt_mod = importlib.import_module("kimix.utils.prompt")

# Each provider gets 3 retries inside _run_prompt_attempts
_RETRIES = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeStatus:
    context_usage: float = 0.0
    context_tokens: int = 0


class FakeLLM:
    """Minimal stand-in for an LLM object."""

    def __init__(self, model_name: str = "fake-model") -> None:
        self.model_name = model_name
        self.chat_provider = MagicMock()
        self.chat_provider.aclose = AsyncMock()


class FakeRuntime:
    """Minimal stand-in for Runtime with a mutable ``llm`` field."""

    def __init__(self, llm: Any = None) -> None:
        self.llm = llm
        self.oauth = None


class FakeSoul:
    def __init__(self, runtime: FakeRuntime) -> None:
        self.runtime = runtime


class FakeCli:
    def __init__(self, soul: FakeSoul) -> None:
        self.soul = soul
        self.session = MagicMock()
        self.session.id = "fake-session-id"


def _exc_until(n: int, exc: Exception) -> Callable[[int], Exception | None]:
    """Return a callable that raises *exc* for ``call_count <= n``."""
    return lambda count: exc if count <= n else None


class FailoverFakeSession:
    """A fake session that supports provider failover.

    ``exc_fn`` is a callable ``(call_count) -> Exception | None``.  When it
    returns a non-None value, ``prompt()`` raises that exception; otherwise
    the prompt succeeds (empty async generator).
    """

    def __init__(
        self,
        exc_fn: Callable[[int], Exception | None] | None = None,
        llm: Any = None,
    ) -> None:
        self.status = FakeStatus()
        self._cancel_event = None
        self.cancelled_count = 0
        self._exc_fn = exc_fn
        self.call_count = 0
        self.prompts: list[str] = []

        self._runtime = FakeRuntime(llm or FakeLLM("primary-model"))
        self._soul = FakeSoul(self._runtime)
        self._cli = FakeCli(self._soul)
        self._custom_data: dict[str, Any] = {}
        self._custom_config: dict[str, Any] = {}

    def get_custom_data(self) -> dict[str, Any]:
        return self._custom_data

    def get_custom_config(self) -> dict[str, Any]:
        return self._custom_config

    async def prompt(self, prompt_str: str, *, merge_wire_messages: bool = False) -> Any:
        self.prompts.append(prompt_str)
        self.call_count += 1
        if self._exc_fn is not None:
            exc = self._exc_fn(self.call_count)
            if exc is not None:
                raise exc
        # success — empty async generator
        if False:  # pragma: no cover
            yield None

    def cancel(self) -> None:
        self.cancelled_count += 1


def _suppress_output(monkeypatch: Any) -> list[str]:
    """Suppress stream output and return captured colourful-print texts."""
    printed: list[str] = []

    def _capture(text: str, *args: Any, **kwargs: Any) -> None:
        printed.append(text)

    monkeypatch.setattr(prompt_mod.base._stream, "colorful_print_word", _capture)
    monkeypatch.setattr(prompt_mod.base._stream, "print_word", lambda *a, **k: None)
    monkeypatch.setattr(prompt_mod, "_print_usage", lambda *a, **k: None)
    return printed


def _suppress_print_agent_json(monkeypatch: Any) -> None:
    """Patch the stream-print helpers used inside _run_prompt_attempts."""
    monkeypatch.setattr(prompt_mod, "print_agent_json", AsyncMock())
    monkeypatch.setattr(prompt_mod, "print_agent_json_flush_text", lambda *a, **k: None)


def _mock_sleep(monkeypatch: Any) -> list[float]:
    """Replace asyncio.sleep with a no-op recorder."""
    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    return sleeps


def _patch_config_and_llm(monkeypatch: Any) -> dict[str, Any]:
    """Patch ``_create_config`` and ``create_llm`` for failover tests."""
    fake_cfg = MagicMock()
    fake_cfg.model = MagicMock()
    fake_cfg.provider = MagicMock()
    fake_cfg.max_tokens = None
    fake_cfg.temperature = None
    fake_cfg.top_p = None
    fake_cfg.top_k = None
    fake_cfg.thinking_effort = None

    create_config_mock = MagicMock(return_value=(fake_cfg, {}))
    created_llms: list[Any] = []

    def _fake_create_llm(*args: Any, **kwargs: Any) -> Any:
        llm = MagicMock()
        llm.chat_provider = MagicMock()
        llm.chat_provider.aclose = AsyncMock()
        created_llms.append(llm)
        return llm

    monkeypatch.setattr(config_mod, "_create_config", create_config_mock)
    monkeypatch.setattr("kimix.utils.config._create_config", create_config_mock)
    monkeypatch.setattr("kimi_cli.llm.create_llm", _fake_create_llm)

    return {"cfg": create_config_mock, "created_llms": created_llms}


def _make_backup(model: str, url: str) -> dict[str, Any]:
    return {
        "role": "backup",
        "model": model,
        "type": "openai_legacy",
        "url": url,
        "max_context_size": 128000,
        "api_key": "sk-backup",
    }


def _set_primary(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        base,
        "_default_provider",
        {"type": "kimi", "model": "primary", "url": "https://primary"},
    )


# ---------------------------------------------------------------------------
# Phase 4.1 — get_default_sub_providers_by_role  (unit)
# ---------------------------------------------------------------------------


class TestGetSubProvidersByRole:
    def test_returns_all_backups_in_order(self, monkeypatch: Any) -> None:
        backups = [
            {"role": "backup", "model": "a", "type": "kimi", "url": "u1"},
            {"role": "backup", "model": "b", "type": "openai", "url": "u2"},
            {"role": "sub_agent", "model": "c", "type": "kimi", "url": "u3"},
        ]
        monkeypatch.setattr(base, "_default_sub_providers", backups)
        result = base.get_default_sub_providers_by_role("backup")
        assert len(result) == 2
        assert result[0]["model"] == "a"
        assert result[1]["model"] == "b"

    def test_returns_empty_when_no_backup(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(
            base,
            "_default_sub_providers",
            [{"role": "sub_agent", "model": "c", "type": "kimi", "url": "u3"}],
        )
        assert base.get_default_sub_providers_by_role("backup") == []

    def test_does_not_return_other_roles(self, monkeypatch: Any) -> None:
        providers = [
            {"role": "planner", "model": "p", "type": "kimi", "url": "up"},
            {"role": "sub_agent", "model": "s", "type": "kimi", "url": "us"},
        ]
        monkeypatch.setattr(base, "_default_sub_providers", providers)
        assert base.get_default_sub_providers_by_role("backup") == []
        assert len(base.get_default_sub_providers_by_role("sub_agent")) == 1


# ---------------------------------------------------------------------------
# Phase 4.2 — _provider_key  (unit)
# ---------------------------------------------------------------------------


class TestProviderKey:
    def test_deterministic_key(self) -> None:
        d = {"type": "kimi", "model": "k2", "url": "https://x"}
        assert prompt_mod._provider_key(d) == ("kimi", "k2", "https://x")

    def test_url_fallback_to_base_url(self) -> None:
        d = {"type": "openai", "model": "gpt-4", "base_url": "https://y"}
        key = prompt_mod._provider_key(d)
        assert key[2] == "https://y"

    def test_none_values(self) -> None:
        assert prompt_mod._provider_key({}) == (None, None, None)


# ---------------------------------------------------------------------------
# Phase 4.3 — _normalize_sub_providers with multiple backups
# ---------------------------------------------------------------------------


class TestNormalizeSubProviders:
    def test_multiple_backups_kept_without_warning(self, monkeypatch: Any) -> None:
        debug_msgs: list[str] = []
        monkeypatch.setattr("kimix.ui.printing.print_debug", lambda m: debug_msgs.append(m))
        backups = [
            {"role": "backup", "model": "a", "type": "kimi", "url": "u1", "max_context_size": 100000},
            {"role": "backup", "model": "b", "type": "openai", "url": "u2", "max_context_size": 100000},
        ]
        result = config_mod._normalize_sub_providers(None, backups)
        assert len(result) == 2
        assert all(r["role"] == "backup" for r in result)
        assert not any("backup" in m for m in debug_msgs)

    def test_sub_agent_duplicate_warns_but_backup_does_not(self, monkeypatch: Any) -> None:
        debug_msgs: list[str] = []
        monkeypatch.setattr("kimix.ui.printing.print_debug", lambda m: debug_msgs.append(m))
        providers = [
            {"role": "sub_agent", "model": "a", "type": "kimi", "url": "u1", "max_context_size": 100000},
            {"role": "sub_agent", "model": "b", "type": "openai", "url": "u2", "max_context_size": 100000},
            {"role": "backup", "model": "c", "type": "openai", "url": "u3", "max_context_size": 100000},
            {"role": "backup", "model": "d", "type": "openai", "url": "u4", "max_context_size": 100000},
        ]
        result = config_mod._normalize_sub_providers(None, providers)
        assert len(result) == 4
        sub_agent_warnings = [m for m in debug_msgs if "sub_agent" in m]
        backup_warnings = [m for m in debug_msgs if "backup" in m]
        assert len(sub_agent_warnings) == 1
        assert len(backup_warnings) == 0


# ---------------------------------------------------------------------------
# Phase 4.4 — failover to backup succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failover_to_backup_succeeds(monkeypatch: Any) -> None:
    """Primary fails (all retries exhausted), backup succeeds."""
    _suppress_output(monkeypatch)
    monkeypatch.setattr(prompt_mod, "print_agent_json", AsyncMock())
    monkeypatch.setattr(prompt_mod, "print_agent_json_flush_text", lambda *a, **k: None)
    _mock_sleep(monkeypatch)

    monkeypatch.setattr(base, "_default_sub_providers", [_make_backup("backup-1", "https://b1")])
    _set_primary(monkeypatch)
    mocks = _patch_config_and_llm(monkeypatch)

    # Primary must fail all _RETRIES attempts (calls 1..3), backup succeeds (call 4+)
    session = FailoverFakeSession(exc_fn=_exc_until(_RETRIES, RuntimeError("primary down")))

    result = await prompt_mod._run_single_prompt(
        session, "hi", output_function=None, cancel_callable=None,
        merge_wire_messages=False, info_print=False,
    )
    assert result is True
    assert len(mocks["created_llms"]) == 1
    assert session._runtime.llm is mocks["created_llms"][0]
    assert session.get_custom_data()["_active_provider_dict"]["model"] == "backup-1"


# ---------------------------------------------------------------------------
# Phase 4.5 — no backups → primary error propagates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_backups_propagates_error(monkeypatch: Any) -> None:
    """When no backups are configured, the primary error propagates."""
    _suppress_output(monkeypatch)
    _suppress_print_agent_json(monkeypatch)
    _mock_sleep(monkeypatch)

    monkeypatch.setattr(base, "_default_sub_providers", [])

    from kosong.chat_provider import APIStatusError

    # APIStatusError raises immediately (no retries)
    session = FailoverFakeSession(exc_fn=lambda c: APIStatusError(500, "server error"))

    with pytest.raises(APIStatusError, match="server error"):
        await prompt_mod._run_single_prompt(
            session, "hi", output_function=None, cancel_callable=None,
            merge_wire_messages=False, info_print=False,
        )


# ---------------------------------------------------------------------------
# Phase 4.6 — all backups fail → last error raised
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_backups_fail_raises_last_error(monkeypatch: Any) -> None:
    """Primary + all backups fail → last error propagated."""
    _suppress_output(monkeypatch)
    _suppress_print_agent_json(monkeypatch)
    _mock_sleep(monkeypatch)

    backups = [_make_backup("backup-1", "https://b1"), _make_backup("backup-2", "https://b2")]
    monkeypatch.setattr(base, "_default_sub_providers", backups)
    _set_primary(monkeypatch)
    _patch_config_and_llm(monkeypatch)

    # Everything fails forever
    session = FailoverFakeSession(exc_fn=lambda c: RuntimeError("all down"))

    with pytest.raises(RuntimeError, match="all down"):
        await prompt_mod._run_single_prompt(
            session, "hi", output_function=None, cancel_callable=None,
            merge_wire_messages=False, info_print=False,
        )


# ---------------------------------------------------------------------------
# Phase 4.7 — multiple backup iteration order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_backup_iteration_order(monkeypatch: Any) -> None:
    """Primary fails, backup1 fails, backup2 succeeds."""
    _suppress_output(monkeypatch)
    _suppress_print_agent_json(monkeypatch)
    _mock_sleep(monkeypatch)

    backups = [_make_backup("backup-1", "https://b1"), _make_backup("backup-2", "https://b2")]
    monkeypatch.setattr(base, "_default_sub_providers", backups)
    _set_primary(monkeypatch)
    mocks = _patch_config_and_llm(monkeypatch)

    # Primary fails (calls 1-3), backup-1 fails (calls 4-6), backup-2 succeeds (call 7)
    total_fail = _RETRIES * 2  # primary + backup-1
    session = FailoverFakeSession(exc_fn=_exc_until(total_fail, RuntimeError("fail")))

    result = await prompt_mod._run_single_prompt(
        session, "hi", output_function=None, cancel_callable=None,
        merge_wire_messages=False, info_print=False,
    )
    assert result is True
    assert len(mocks["created_llms"]) == 2


# ---------------------------------------------------------------------------
# Config contract for `backup` role
# ---------------------------------------------------------------------------


class TestBackupConfigContract:
    def test_backup_in_sub_provider_roles_and_excluded_from_pick_priority(self) -> None:
        assert "backup" in config_mod.SUB_PROVIDER_ROLES
        assert "backup" not in config_mod._SUB_PROVIDER_PICK_PRIORITY

    def test_backup_never_picked_as_main_when_root_lacks_model(self) -> None:
        config_data: dict[str, Any] = {}
        sub_providers = [_make_backup("backup-only", "https://backup")]
        config_mod._pick_main_from_sub_providers(config_data, None, sub_providers)
        assert "model" not in config_data

    def test_get_backup_providers_returns_all_backups(self, monkeypatch: Any) -> None:
        backups = [_make_backup("b1", "https://b1"), _make_backup("b2", "https://b2")]
        monkeypatch.setattr(
            base,
            "_default_sub_providers",
            [{"role": "sub_agent", "model": "s", "type": "kimi", "url": "u"}, *backups],
        )
        assert [p["model"] for p in config_mod.get_backup_providers()] == ["b1", "b2"]

    def test_cross_type_backup_does_not_inherit_oauth_or_openai_settings(self) -> None:
        root = {
            "type": "kimi",
            "model": "kimi-for-coding",
            "url": "https://api.kimi.com/v1",
            "max_context_size": 256000,
            "oauth": {"storage": "file", "key": "oauth/kimi-code"},
            "openai_settings": {"reasoning_effort": "high"},
        }
        backup = {
            "role": "backup",
            "type": "openai_legacy",
            "model": "gpt-4o",
            "url": "https://api.openai.com/v1",
            "max_context_size": 128000,
            "api_key": "sk-oai",
        }
        config_mod._inherit_sub_provider_defaults(root, None, [backup])
        assert "oauth" not in backup
        assert "openai_settings" not in backup


# ---------------------------------------------------------------------------
# HTTP non-200 errors (400, 429, 5xx, etc.) trigger backup failover
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 403, 404, 422, 429, 500, 502, 503, 504, 529])
async def test_http_non_200_errors_switch_to_backup(
    monkeypatch: Any, status_code: int
) -> None:
    """Any non-200 APIStatusError from primary switches to the backup provider."""
    from kosong.chat_provider import APIStatusError

    printed = _suppress_output(monkeypatch)
    _suppress_print_agent_json(monkeypatch)
    _mock_sleep(monkeypatch)

    monkeypatch.setattr(base, "_default_sub_providers", [_make_backup("backup-1", "https://b1")])
    _set_primary(monkeypatch)
    mocks = _patch_config_and_llm(monkeypatch)

    # First call (primary) raises APIStatusError; second call (backup) succeeds.
    session = FailoverFakeSession(
        exc_fn=_exc_until(1, APIStatusError(status_code, f"http {status_code}"))
    )

    result = await prompt_mod._run_single_prompt(
        session, "hi", output_function=None, cancel_callable=None,
        merge_wire_messages=False, info_print=False,
    )
    assert result is True
    assert len(mocks["created_llms"]) == 1
    assert session.get_custom_data()["_active_provider_dict"]["model"] == "backup-1"
    assert any("Switching to backup provider: backup-1" in m for m in printed)


# ---------------------------------------------------------------------------
# RunCancelled during backup must not cascade to next backup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_cancelled_during_backup_returns_false_without_cascade(
    monkeypatch: Any,
) -> None:
    from kimi_cli.soul import RunCancelled
    from kosong.chat_provider import APIStatusError

    _suppress_output(monkeypatch)
    _suppress_print_agent_json(monkeypatch)
    _mock_sleep(monkeypatch)

    backups = [_make_backup("backup-1", "https://b1"), _make_backup("backup-2", "https://b2")]
    monkeypatch.setattr(base, "_default_sub_providers", backups)
    _set_primary(monkeypatch)
    mocks = _patch_config_and_llm(monkeypatch)

    def _exc_fn(count: int) -> Exception | None:
        if count == 1:
            return APIStatusError(503, "primary unavailable")
        return RunCancelled()

    session = FailoverFakeSession(exc_fn=_exc_fn)

    result = await prompt_mod._run_single_prompt(
        session, "hi", output_function=None, cancel_callable=None,
        merge_wire_messages=False, info_print=False,
    )
    assert result is False
    # Stopped after backup-1 was cancelled; never tried backup-2.
    assert len(mocks["created_llms"]) == 1


# ---------------------------------------------------------------------------
# Real Session._restart() resume with backup provider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_provider_restarts_and_resumes_real_sdk_session(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """When Session._restart exists, failover updates _create_kwargs and resumes."""
    from dataclasses import dataclass, field
    from pathlib import Path
    from kaos.path import KaosPath
    from kimi_cli.wire.types import TextPart
    from kosong.chat_provider import APIStatusError
    import kimi_agent_sdk._session as sdk_session_mod
    from kimi_agent_sdk._session import Session

    _suppress_output(monkeypatch)
    _suppress_print_agent_json(monkeypatch)
    _mock_sleep(monkeypatch)

    work_dir = KaosPath.unsafe_from_local_path(Path(tmp_path))
    session_dir = Path(tmp_path) / "sessions" / "s_failover"
    session_dir.mkdir(parents=True)
    context_file = session_dir / "context.jsonl"
    context_file.write_text('{"role": "user", "content": "prior context"}\n', encoding="utf-8")
    (session_dir / "state.json").write_text("{}", encoding="utf-8")

    @dataclass
    class _FakeWireFile:
        path: Path

    @dataclass
    class _FakeCLISession:
        work_dir: KaosPath
        session_id: str
        context_file: Path
        dir: Path
        wire_file: _FakeWireFile
        custom_data: dict[str, Any] = field(default_factory=dict)
        custom_config: dict[str, Any] = field(default_factory=dict)

        @property
        def id(self) -> str:
            return self.session_id

        async def close_context_db(self) -> None:
            pass

    @dataclass
    class _FakeAgent:
        toolset: Any = None

    @dataclass
    class _FakeRuntime:
        llm: Any
        oauth: Any = None

    @dataclass
    class _FakeSoul:
        agent: _FakeAgent
        _runtime: _FakeRuntime

        async def close(self) -> None:
            pass

    created_configs: list[Any] = []
    resumed_flags: list[bool] = []
    old_chat_provider = MagicMock()
    old_chat_provider.aclose = AsyncMock()
    old_llm = MagicMock()
    old_llm.chat_provider = old_chat_provider

    class _FakeCLI:
        calls: int = 0

        def __init__(self, cli_session: _FakeCLISession, llm: Any = None) -> None:
            self.session = cli_session
            self.soul = _FakeSoul(agent=_FakeAgent(), _runtime=_FakeRuntime(llm=llm or old_llm))

        async def run(self, user_input: Any, cancel_event: Any, *, merge_wire_messages: bool = False) -> Any:
            _FakeCLI.calls += 1
            # Fail all primary attempts (initial + max_restarts=3 => 4 calls),
            # then succeed once switched & resumed onto the backup provider.
            if _FakeCLI.calls <= 4:
                from kimi_cli.soul import SessionRestartRequired

                raise SessionRestartRequired("429 on primary", original_error=APIStatusError(429, "rate limit"))
            yield TextPart(text="resumed on backup")

    cli_session = _FakeCLISession(
        work_dir=work_dir,
        session_id="s_failover",
        context_file=context_file,
        dir=session_dir,
        wire_file=_FakeWireFile(session_dir / "wire.jsonl"),
        custom_config={"chat_provider": old_chat_provider, "provider_dict": {"model": "primary"}},
    )

    async def fake_find(wd: Any, sid: str, _sessions_dir: Any = None) -> _FakeCLISession:
        return cli_session

    async def fake_create(cs: _FakeCLISession, **kwargs: Any) -> _FakeCLI:
        created_configs.append(kwargs.get("config"))
        resumed_flags.append(bool(kwargs.get("resumed")))
        return _FakeCLI(cs, llm=kwargs.get("llm"))

    monkeypatch.setattr(sdk_session_mod.CliSession, "find", classmethod(lambda cls, *a, **kw: fake_find(*a, **kw)))
    monkeypatch.setattr(sdk_session_mod.KimiCLI, "create", classmethod(lambda cls, *a, **kw: fake_create(a[0], **kw)))

    primary_cfg = MagicMock()
    session = Session(_FakeCLI(cli_session))
    session._create_kwargs = {"config": primary_cfg, "model_name": "primary", "llm": None, "yolo": True}

    backup = _make_backup("backup-1", "https://b1")
    monkeypatch.setattr(base, "_default_sub_providers", [backup])
    _set_primary(monkeypatch)
    mocks = _patch_config_and_llm(monkeypatch)

    result = await prompt_mod._run_single_prompt(
        session, "continue", output_function=None, cancel_callable=None,
        merge_wire_messages=False, info_print=False,
    )

    assert result is True
    assert _FakeCLI.calls == 5
    # Session._restart was used to resume the session with resumed=True and backup config
    assert resumed_flags and all(resumed_flags)
    assert created_configs[-1] is mocks["cfg"].return_value[0]
    assert session._create_kwargs["config"] is mocks["cfg"].return_value[0]
    assert session._create_kwargs["llm"] is None
    assert session._create_kwargs["model_name"] is None
    assert session.get_custom_config()["provider_dict"]["model"] == "backup-1"
    assert "chat_provider" not in session.get_custom_config()
    assert session.get_custom_data()["_active_provider_dict"]["model"] == "backup-1"
    assert "prior context" in context_file.read_text(encoding="utf-8")
    old_chat_provider.aclose.assert_awaited()


# ---------------------------------------------------------------------------
# SessionManager.prompt backup failover (kimix serve)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_manager_prompt_switches_to_backup_and_resumes(
    tmp_path: Any, monkeypatch: Any
) -> None:
    from kimi_cli.wire.types import TextPart
    from kosong.chat_provider import APIStatusError
    from kimix.server.session_manager import SessionManager

    monkeypatch.chdir(tmp_path)
    backup = _make_backup("backup-srv", "https://b-srv")
    monkeypatch.setattr(base, "_default_sub_providers", [backup])
    _set_primary(monkeypatch)
    mocks = _patch_config_and_llm(monkeypatch)

    calls = 0

    class _ServerFakeSession(FailoverFakeSession):
        async def prompt(self, prompt_str: str, *, merge_wire_messages: bool = False) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise APIStatusError(429, "server rate limited")
            yield TextPart(text="recovered via backup")

    sdk_session = _ServerFakeSession()
    monkeypatch.setattr("kimix.server.session_manager._create_session_async", AsyncMock(return_value=sdk_session))
    manager = SessionManager()
    info = await manager.create_session(title="Failover test")

    result = await manager.prompt(info.id, "hello")

    assert calls == 2
    assert len(mocks["created_llms"]) == 1
    assert sdk_session.get_custom_data()["_active_provider_dict"]["model"] == "backup-srv"
    text_parts = [p["text"] for p in result["parts"] if p.get("type") == "text"]
    assert any("recovered via backup" in t for t in text_parts)
    assert sdk_session._runtime.llm is mocks["created_llms"][-1]


# ---------------------------------------------------------------------------
# Phase 4.8 — session resume across prompts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_resume_across_prompts(monkeypatch: Any) -> None:
    """After failover, the second prompt uses the backup as primary."""
    _suppress_output(monkeypatch)
    _suppress_print_agent_json(monkeypatch)
    _mock_sleep(monkeypatch)

    monkeypatch.setattr(base, "_default_sub_providers", [_make_backup("backup-1", "https://b1")])
    _set_primary(monkeypatch)
    mocks = _patch_config_and_llm(monkeypatch)

    # First prompt: primary fails (calls 1-3), backup succeeds (call 4)
    session = FailoverFakeSession(exc_fn=_exc_until(_RETRIES, RuntimeError("primary down")))

    result1 = await prompt_mod._run_single_prompt(
        session, "first", output_function=None, cancel_callable=None,
        merge_wire_messages=False, info_print=False,
    )
    assert result1 is True

    active = prompt_mod._get_active_provider_dict(session)
    assert active is not None
    assert active["model"] == "backup-1"

    created_before = len(mocks["created_llms"])

    # Second prompt: succeeds immediately on the current provider
    result2 = await prompt_mod._run_single_prompt(
        session, "second", output_function=None, cancel_callable=None,
        merge_wire_messages=False, info_print=False,
    )
    assert result2 is True
    assert len(mocks["created_llms"]) == created_before


# ---------------------------------------------------------------------------
# Phase 4.9 — KeyboardInterrupt during backup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keyboard_interrupt_during_backup_returns_false(monkeypatch: Any) -> None:
    """Primary fails, backup raises KeyboardInterrupt → returns False."""
    _suppress_output(monkeypatch)
    _suppress_print_agent_json(monkeypatch)
    _mock_sleep(monkeypatch)

    monkeypatch.setattr(base, "_default_sub_providers", [_make_backup("backup-1", "https://b1")])
    _set_primary(monkeypatch)
    _patch_config_and_llm(monkeypatch)

    # Primary fails (calls 1-3), backup raises KeyboardInterrupt (call 4)
    def _exc_fn(count: int) -> Exception | None:
        if count <= _RETRIES:
            return RuntimeError("primary down")
        return KeyboardInterrupt()

    session = FailoverFakeSession(exc_fn=_exc_fn)

    result = await prompt_mod._run_single_prompt(
        session, "hi", output_function=None, cancel_callable=None,
        merge_wire_messages=False, info_print=False,
    )
    assert result is False


# ---------------------------------------------------------------------------
# Bonus — skip active provider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_active_backup(monkeypatch: Any) -> None:
    """If the active provider matches a backup key, that backup is skipped."""
    _suppress_output(monkeypatch)
    _suppress_print_agent_json(monkeypatch)
    _mock_sleep(monkeypatch)

    backups = [_make_backup("backup-1", "https://b1"), _make_backup("backup-2", "https://b2")]
    monkeypatch.setattr(base, "_default_sub_providers", backups)
    _set_primary(monkeypatch)
    mocks = _patch_config_and_llm(monkeypatch)

    # Primary fails, backup-1 fails, backup-2 succeeds
    total_fail = _RETRIES * 2
    session = FailoverFakeSession(exc_fn=_exc_until(total_fail, RuntimeError("fail")))

    result = await prompt_mod._run_single_prompt(
        session, "hi", output_function=None, cancel_callable=None,
        merge_wire_messages=False, info_print=False,
    )
    assert result is True
    assert len(mocks["created_llms"]) == 2
