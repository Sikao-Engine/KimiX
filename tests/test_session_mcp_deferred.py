import asyncio

from kaos.path import KaosPath

from kimix.utils.session import close_session, create_session


def test_sync_session_defers_mcp_loading_to_prompt_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "kimi_cli.app.discover_mcp_configs",
        lambda _work_dir: [{"mcpServers": {}}],
    )

    session = create_session(
        work_dir=KaosPath(tmp_path),
        resume=False,
        provider_dict={
            "model": "test-model",
            "max_context_size": 131072,
            "capabilities": [],
            "url": "http://127.0.0.1",
            "type": "openai_legacy",
            "api_key": "test-key",
        },
    )
    toolset = session._cli.soul.agent.toolset

    assert toolset._mcp_loading_task is None
    assert toolset._deferred_mcp_load is not None

    async def load_in_prompt_loop() -> None:
        assert await toolset.start_deferred_mcp_tool_loading()
        await toolset.wait_for_mcp_tools()

    try:
        asyncio.run(load_in_prompt_loop())
    finally:
        close_session(session)
