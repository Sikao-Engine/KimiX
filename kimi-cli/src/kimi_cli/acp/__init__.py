def acp_main() -> None:
    """Entry point for the multi-session ACP server."""
    import asyncio

    import acp

    from kimi_cli.acp.server import ACPServer
    from kimi_cli.app import enable_logging
    from kimi_cli.utils.asyncio_noise import suppress_proactor_reset_noise
    from kimi_cli.utils.logging import logger

    suppress_proactor_reset_noise()
    enable_logging()
    logger.info("Starting ACP server on stdio")
    asyncio.run(acp.run_agent(ACPServer(), use_unstable_protocol=True))
