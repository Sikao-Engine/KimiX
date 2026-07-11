from typing import Any
import os
from pathlib import Path
import orjson
import kimix.base as base
from kimi_cli.config import BackgroundConfig, LoopControl, SecretStr, NotificationConfig, MCPConfig, OAuthRef, OpenAISettings  # type: ignore[attr-defined]
from kimi_agent_sdk import Config
from . import _globals


def _create_config(provider_dict: dict[str, Any] | None = None) -> tuple[Config, dict[str, Any] | None]:
    from kimi_cli.config import LLMModel, LLMProvider
    from kimix.base import print_debug, print_warning

    provider_dict = provider_dict if provider_dict is not None else base._default_provider
    cfg = Config()

    if provider_dict is None:
        try:
            provider_dict = orjson.loads(
                (Path(__file__).parent.parent / 'default_config.json').read_text(encoding='utf-8', errors='replace'))
            if type(provider_dict) != dict:
                provider_dict = None
        except:
            pass
    if provider_dict is not None:
        model = provider_dict.get('model')
        max_context_size = provider_dict.get('max_context_size')
        capabilities = set(provider_dict.get('capabilities', set()))
        url = provider_dict.get('url')
        provider_type = provider_dict.get("type")
        assert provider_type is not None, "`provider_type` must be provided in config"
        assert max_context_size is not None, "`max_context_size` must be provided in  config"
        assert type(model) == str, "model(str) must be provided in config"
        assert url is not None, "url must be provided in config"

        env: dict | None =  provider_dict.get('env')
        if env is not None:
            for k, v in env.items():
                os.environ[k] = v
        max_context_size = int(max_context_size)
        api_key = provider_dict.get('api_key', None)
        if not api_key:
            api_key = os.environ.get("KIMI_API_KEY")
        if not api_key:
            api_key = os.environ.get("KIMIX_API_KEY")
        if not api_key:
            print_warning(
                'api_key not found. May config in JSON, or set to env `KIMI_API_KEY` or `KIMIX_API_KEY`')
            api_key = ''
        oath_dict = provider_dict.get('oauth')
        oath : OAuthRef | None = None
        if isinstance(oath_dict, dict):
            oath = OAuthRef(key=oath_dict.get('key', ''))
            oath.storage = oath_dict.get('storage', 'file')
            assert isinstance(oath.storage, str), 'oath.storage must be str'
            assert isinstance(oath.key, str), 'oath.key must be str'
        else:
            oath = None
        openai_settings_dict = provider_dict.get('openai_settings')
        openai_settings: OpenAISettings | None = None
        if isinstance(openai_settings_dict, dict):
            openai_settings = OpenAISettings(**openai_settings_dict)
        cfg.provider = LLMProvider(
            type=provider_type,
            # example: "https://api.minimaxi.com/anthropic"
            base_url=url,
            api_key=SecretStr(api_key),
            custom_headers=provider_dict.get('custom_headers'),
            oauth=oath,
            openai_settings=openai_settings,
        )
        cfg.model = LLMModel(
            model=model,
            max_context_size=max_context_size,
            capabilities=capabilities,
        )
        # Set loop control
        loop_control = provider_dict.get('loop_control')
        lc = LoopControl()
        if loop_control and isinstance(loop_control, dict):
            for key, value in loop_control.items():
                if hasattr(lc, key):
                    setattr(lc, key, value)
        if base._default_ralph is not None and 'max_ralph_iterations' not in (loop_control or {}): # override
            lc.max_ralph_iterations = base._default_ralph
        cfg.loop_control = lc
        def set_val(name: str, type_var: type) -> None:
            v = provider_dict.get(name)
            if v is not None:
                setattr(cfg, name, type_var(v))
        set_val('show_thinking_stream', bool)
        # Set notifications
        notifications = provider_dict.get('notifications')
        if notifications and isinstance(notifications, dict):
            nc = NotificationConfig()
            for key, value in notifications.items():
                if hasattr(nc, key):
                    setattr(nc, key, value)
            cfg.notifications = nc
        # Set mcp
        mcp = provider_dict.get('mcp')
        if mcp and isinstance(mcp, dict):
            mc = MCPConfig()
            for key, value in mcp.items():
                if hasattr(mc, key):
                    setattr(mc, key, value)
            cfg.mcp = mc
        # Set LLM override settings
        set_val('max_tokens', int)
        set_val('thinking_effort', str)
        set_val('temperature', float)
        set_val('top_p', float)
        set_val('top_k', int)
        # Set background
        background = provider_dict.get('background')
        if background and isinstance(background, dict):
            bc = BackgroundConfig()
            for key, value in background.items():
                if hasattr(bc, key):
                    setattr(bc, key, value)
            cfg.background = bc
        # The provider_dict contract no longer includes model_name/name.
        provider_dict = {k: v for k, v in provider_dict.items() if k not in ("model_name", "name")}
    return cfg, provider_dict