import contextlib
from pathlib import Path
from typing import Annotated, Any, Literal

import orjson
import typer

from kimi_cli.mcp.config import (
    get_global_mcp_config_file,
    load_global_mcp_config,
    load_project_mcp_config,
    merge_mcp_configs,
)

cli = typer.Typer(help="Manage MCP server configurations.")


def _effective_work_dir(work_dir: Path | None) -> Path:
    """Return the explicit work_dir or the process current directory."""
    return work_dir if work_dir is not None else Path.cwd()


def _load_mcp_config(work_dir: Path | None = None) -> dict[str, Any]:
    """Load MCP config from global and project mcp config files."""
    from fastmcp.mcp_config import MCPConfig
    from pydantic import ValidationError

    global_config = load_global_mcp_config()
    project_config = load_project_mcp_config(_effective_work_dir(work_dir))
    merged = merge_mcp_configs(global_config, project_config)

    try:
        MCPConfig.model_validate(merged)
    except ValidationError as e:
        config_file = get_global_mcp_config_file()
        raise typer.BadParameter(f"Invalid MCP config in '{config_file}': {e}") from e

    return merged


def _save_mcp_config(config: dict[str, Any]) -> None:
    """Save MCP config to default file."""
    mcp_file = get_global_mcp_config_file()
    mcp_file.write_text(
        orjson.dumps(config, option=orjson.OPT_INDENT_2).decode("utf-8"),
        encoding="utf-8",
    )


def _get_mcp_server(
    name: str, *, require_remote: bool = False, work_dir: Path | None = None
) -> dict[str, Any]:
    """Get MCP server config by name."""
    config = _load_mcp_config(_effective_work_dir(work_dir))
    servers = config.get("mcpServers", {})
    if name not in servers:
        typer.echo(f"MCP server '{name}' not found.", err=True)
        raise typer.Exit(code=1)
    server = servers[name]
    if require_remote and "url" not in server:
        typer.echo(f"MCP server '{name}' is not a remote server.", err=True)
        raise typer.Exit(code=1)
    return server


def _parse_key_value_pairs(
    items: list[str], option_name: str, *, separator: str = "=", strip_whitespace: bool = False
) -> dict[str, str]:
    """Parse key/value pairs from CLI options."""
    parsed: dict[str, str] = {}
    for item in items:
        if separator not in item:
            typer.echo(
                f"Invalid {option_name} format: {item} (expected KEY{separator}VALUE).",
                err=True,
            )
            raise typer.Exit(code=1)
        key, value = item.split(separator, 1)
        if strip_whitespace:
            key, value = key.strip(), value.strip()
        if not key:
            typer.echo(f"Invalid {option_name} format: {item} (empty key).", err=True)
            raise typer.Exit(code=1)
        parsed[key] = value
    return parsed


Transport = Literal["stdio", "http"]


@cli.command(
    "add",
    epilog="""
    Examples:\n
      \n
      # Add streamable HTTP server:\n
      kimi mcp add --transport http context7 https://mcp.context7.com/mcp --header "CONTEXT7_API_KEY: ctx7sk-your-key"\n
      \n
      # Add streamable HTTP server with OAuth authorization:\n
      kimi mcp add --transport http --auth oauth linear https://mcp.linear.app/mcp\n
      \n
      # Add stdio server:\n
      kimi mcp add --transport stdio chrome-devtools -- npx chrome-devtools-mcp@latest
    """.strip(),  # noqa: E501
)
def mcp_add(
    name: Annotated[
        str,
        typer.Argument(help="Name of the MCP server to add."),
    ],
    server_args: Annotated[
        list[str] | None,
        typer.Argument(
            metavar="TARGET_OR_COMMAND...",
            help="For http: server URL. For stdio: command to run (prefix with `--`).",
        ),
    ] = None,
    transport: Annotated[
        Transport,
        typer.Option(
            "--transport",
            "-t",
            help="Transport type for the MCP server. Default: stdio.",
        ),
    ] = "stdio",
    env: Annotated[
        list[str] | None,
        typer.Option(
            "--env",
            "-e",
            help="Environment variables in KEY=VALUE format. Can be specified multiple times.",
        ),
    ] = None,
    header: Annotated[
        list[str] | None,
        typer.Option(
            "--header",
            "-H",
            help="HTTP headers in KEY:VALUE format. Can be specified multiple times.",
        ),
    ] = None,
    auth: Annotated[
        str | None,
        typer.Option(
            "--auth",
            "-a",
            help="Authorization type (e.g., 'oauth').",
        ),
    ] = None,
    work_dir: Annotated[
        Path | None,
        typer.Option(
            "--work-dir",
            help="Project working directory for .kimix/mcp.json discovery.",
        ),
    ] = None,
):
    """Add an MCP server."""
    config = _load_mcp_config(_effective_work_dir(work_dir))
    server_args = server_args or []

    if transport not in {"stdio", "http"}:
        typer.echo(f"Unsupported transport: {transport}.", err=True)
        raise typer.Exit(code=1)

    if transport == "stdio":
        if not server_args:
            typer.echo(
                "For stdio transport, provide the command to start the MCP server after `--`.",
                err=True,
            )
            raise typer.Exit(code=1)
        if header:
            typer.echo("--header is only valid for http transport.", err=True)
            raise typer.Exit(code=1)
        if auth:
            typer.echo("--auth is only valid for http transport.", err=True)
            raise typer.Exit(code=1)
        command, *command_args = server_args
        server_config: dict[str, Any] = {"command": command, "args": command_args}
        if env:
            server_config["env"] = _parse_key_value_pairs(env, "env")
    else:
        if env:
            typer.echo("--env is only supported for stdio transport.", err=True)
            raise typer.Exit(code=1)
        if not server_args:
            typer.echo("URL is required for http transport.", err=True)
            raise typer.Exit(code=1)
        if len(server_args) > 1:
            typer.echo(
                "Multiple targets provided. Supply a single URL for http transport.",
                err=True,
            )
            raise typer.Exit(code=1)
        server_config = {"url": server_args[0], "transport": "http"}
        if header:
            server_config["headers"] = _parse_key_value_pairs(
                header, "header", separator=":", strip_whitespace=True
            )
        if auth:
            server_config["auth"] = auth

    if "mcpServers" not in config:
        config["mcpServers"] = {}
    config["mcpServers"][name] = server_config
    _save_mcp_config(config)
    typer.echo(f"Added MCP server '{name}' to {get_global_mcp_config_file()}.")


@cli.command("remove")
def mcp_remove(
    name: Annotated[
        str,
        typer.Argument(help="Name of the MCP server to remove."),
    ],
    work_dir: Annotated[
        Path | None,
        typer.Option(
            "--work-dir",
            help="Project working directory for .kimix/mcp.json discovery.",
        ),
    ] = None,
):
    """Remove an MCP server."""
    _get_mcp_server(name, work_dir=_effective_work_dir(work_dir))
    config = _load_mcp_config(_effective_work_dir(work_dir))
    del config["mcpServers"][name]
    _save_mcp_config(config)
    typer.echo(f"Removed MCP server '{name}' from {get_global_mcp_config_file()}.")


async def _has_oauth_tokens(server_url: str) -> bool:
    """Check if OAuth tokens exist for the server."""
    from kimi_cli.mcp_oauth import has_mcp_oauth_tokens

    return await has_mcp_oauth_tokens(server_url)


@cli.command("list")
def mcp_list(
    work_dir: Annotated[
        Path | None,
        typer.Option(
            "--work-dir",
            help="Project working directory for .kimix/mcp.json discovery.",
        ),
    ] = None,
):
    """List all MCP servers."""
    config_file = get_global_mcp_config_file()
    config = _load_mcp_config(work_dir)
    servers: dict[str, Any] = config.get("mcpServers", {})

    typer.echo(f"MCP config file: {config_file}")
    if not servers:
        typer.echo("No MCP servers configured.")
        return

    async def _list() -> None:
        for name, server in servers.items():
            if "command" in server:
                cmd = server["command"]
                cmd_args = " ".join(server.get("args", []))
                line = f"{name} (stdio): {cmd} {cmd_args}".rstrip()
            elif "url" in server:
                transport = server.get("transport") or "http"
                if transport == "streamable-http":
                    transport = "http"
                line = f"{name} ({transport}): {server['url']}"
                if server.get("auth") == "oauth" and not await _has_oauth_tokens(server["url"]):
                    line += " [authorization required - run: kimi mcp auth " + name + "]"
            else:
                line = f"{name}: {server}"
            typer.echo(f"  {line}")

    import asyncio

    asyncio.run(_list())


@cli.command("auth")
def mcp_auth(
    name: Annotated[
        str,
        typer.Argument(help="Name of the MCP server to authorize."),
    ],
    work_dir: Annotated[
        Path | None,
        typer.Option(
            "--work-dir",
            help="Project working directory for .kimix/mcp.json discovery.",
        ),
    ] = None,
):
    """Authorize with an OAuth-enabled MCP server."""
    import asyncio

    server = _get_mcp_server(name, require_remote=True, work_dir=_effective_work_dir(work_dir))
    if server.get("auth") != "oauth":
        typer.echo(f"MCP server '{name}' does not use OAuth. Add with --auth oauth.", err=True)
        raise typer.Exit(code=1)

    async def _auth() -> None:
        import fastmcp

        from kimi_cli.mcp_oauth import prepare_mcp_server_config

        typer.echo(f"Authorizing with '{name}'...")
        typer.echo("A browser window will open for authorization.")

        client = fastmcp.Client({"mcpServers": {name: prepare_mcp_server_config(server)}})
        try:
            async with client:
                tools = await client.list_tools()
                typer.echo(f"Successfully authorized with '{name}'.")
                typer.echo(f"Available tools: {len(tools)}")
        except Exception as e:
            typer.echo(f"Authorization failed: {type(e).__name__}: {e}", err=True)
            raise typer.Exit(code=1) from None

    asyncio.run(_auth())


@cli.command("reset-auth")
def mcp_reset_auth(
    name: Annotated[
        str,
        typer.Argument(help="Name of the MCP server to reset authorization."),
    ],
    work_dir: Annotated[
        Path | None,
        typer.Option(
            "--work-dir",
            help="Project working directory for .kimix/mcp.json discovery.",
        ),
    ] = None,
):
    """Reset OAuth authorization for an MCP server (clear cached tokens)."""
    import asyncio

    server = _get_mcp_server(name, require_remote=True, work_dir=_effective_work_dir(work_dir))

    async def _reset_auth() -> None:
        from kimi_cli.mcp_oauth import create_mcp_oauth_token_storage

        storage = create_mcp_oauth_token_storage(server["url"])
        await storage.clear()

    try:
        asyncio.run(_reset_auth())
        typer.echo(f"OAuth tokens cleared for '{name}'.")
    except ImportError:
        typer.echo("OAuth support not available.", err=True)
        raise typer.Exit(code=1) from None
    except Exception as e:
        typer.echo(f"Failed to clear tokens: {type(e).__name__}: {e}", err=True)
        raise typer.Exit(code=1) from None


@cli.command("test")
def mcp_test(
    name: Annotated[
        str,
        typer.Argument(help="Name of the MCP server to test."),
    ],
    work_dir: Annotated[
        Path | None,
        typer.Option(
            "--work-dir",
            help="Project working directory for .kimix/mcp.json discovery.",
        ),
    ] = None,
):
    """Test connection to an MCP server and list available tools."""
    import asyncio

    server = _get_mcp_server(name, work_dir=_effective_work_dir(work_dir))

    async def _test() -> None:
        import fastmcp

        from kimi_cli.mcp_oauth import prepare_mcp_server_config

        typer.echo(f"Testing connection to '{name}'...")
        client = fastmcp.Client({"mcpServers": {name: prepare_mcp_server_config(server)}})

        try:
            async with client:
                tools = await client.list_tools()
                typer.echo(f"✓ Connected to '{name}'")
                typer.echo(f"  Available tools: {len(tools)}")
                if tools:
                    typer.echo("  Tools:")
                    for tool in tools:
                        desc = tool.description or ""
                        if len(desc) > 50:
                            desc = desc[:47] + "..."
                        typer.echo(f"    - {tool.name}: {desc}")
        except Exception as e:
            typer.echo(f"✗ Connection failed: {type(e).__name__}: {e}", err=True)
            raise typer.Exit(code=1) from None

    asyncio.run(_test())


@cli.command("serve")
def mcp_serve(
    transport: Annotated[
        Transport,
        typer.Option(
            "--transport",
            "-t",
            help="Transport for the MCP server. Default: stdio.",
        ),
    ] = "stdio",
    host: Annotated[
        str,
        typer.Option("--host", help="Host to bind to for HTTP transport."),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option("--port", help="Port to bind to for HTTP transport."),
    ] = 4097,
    work_dir: Annotated[
        Path | None,
        typer.Option("--work-dir", help="Workspace directory for resources and session."),
    ] = None,
    agent_file: Annotated[
        Path | None,
        typer.Option("--agent-file", help="Agent specification file to load."),
    ] = None,
    no_resource: Annotated[
        bool,
        typer.Option("--no-resource", help="Do not expose file resources."),
    ] = False,
    no_prompt: Annotated[
        bool,
        typer.Option("--no-prompt", help="Do not expose prompts."),
    ] = False,
):
    """Serve Kimix as an MCP server."""
    import asyncio

    from kaos.path import KaosPath

    from kimi_cli.app import KimiCLI
    from kimi_cli.mcp.server import serve_http, serve_stdio
    from kimi_cli.session import Session

    async def _run() -> None:
        resolved_work_dir = KaosPath(str(work_dir or Path.cwd()))
        cli_session = await Session.create(resolved_work_dir)
        cli = await KimiCLI.create(
            cli_session,
            agent_file=agent_file,
        )
        runtime = cli.soul.agent.runtime
        options = {
            "agent": cli.soul.agent,
            "include_resources": not no_resource,
            "include_prompts": not no_prompt,
        }
        if transport == "stdio":
            await serve_stdio(runtime, **options)
        else:
            await serve_http(runtime, host=host, port=port, **options)

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run())
