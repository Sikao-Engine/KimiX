import argparse
import sys
from pathlib import Path
from typing import Any

import orjson
from kaos.path import KaosPath

import kimix.base as base
from kimix.base import print_debug, print_error, print_warning

from . import constants, utils  # noqa: F401


def _load_project_mcp_config() -> dict[str, Any]:
    """Load project-level MCP config from ``.kimix/mcp.json``."""
    mcp_json_path = constants.curr_dir / ".kimix" / "mcp.json"
    if mcp_json_path.exists():
        try:
            config = orjson.loads(mcp_json_path.read_text(encoding="utf-8"))
            if isinstance(config, dict):
                print_debug(f"Loaded MCP config from {mcp_json_path}")
                return config
            print_warning(f"MCP config file {mcp_json_path} must contain a JSON object.")
        except orjson.JSONDecodeError as e:
            print_warning(f"Failed to parse MCP config file {mcp_json_path}: {e}")
        except Exception as e:
            print_warning(f"Failed to load MCP config file {mcp_json_path}: {e}")
    return {}


def set_arg() -> tuple[str | None, argparse.Namespace]:
    parser = argparse.ArgumentParser(description="Kimi Agent CLI")
    subparsers = parser.add_subparsers(dest="command", required=False)

    serve_parser = subparsers.add_parser("serve", description="Kimix HTTP server (opencode-style)")
    serve_parser.add_argument("--host", "--hostname", default="127.0.0.1", help="Host to bind to")
    serve_parser.add_argument("--port", type=int, default=4096, help="Port to bind to")

    sse_cli_parser = subparsers.add_parser("ssecli", description="Kimix SSE CLI for debug")
    sse_cli_parser.add_argument(
        "--host", default="127.0.0.1", help="Host to connect to (for ssecli)"
    )
    sse_cli_parser.add_argument(
        "--port", type=int, default=4096, help="Port to connect to (for ssecli)"
    )
    sse_cli_parser.add_argument(
        "--debug",
        action="store_true",
        help="Print all SSE stream details and save to sse_log_<date>.txt",
    )

    mcp_parser = subparsers.add_parser("mcp", description="MCP server management commands")
    mcp_subparsers = mcp_parser.add_subparsers(dest="mcp_command", required=True)

    mcp_serve_parser = mcp_subparsers.add_parser(
        "serve", description="Serve Kimix as an MCP server"
    )
    mcp_serve_parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="Transport for the MCP server",
    )
    mcp_serve_parser.add_argument(
        "--host", default="127.0.0.1", help="Host to bind to for HTTP transport"
    )
    mcp_serve_parser.add_argument(
        "--port", type=int, default=4097, help="Port to bind to for HTTP transport"
    )
    mcp_serve_parser.add_argument(
        "--work-dir", type=str, default=None, help="Workspace directory for resources and session"
    )
    mcp_serve_parser.add_argument(
        "--agent-file", type=str, default=None, help="Agent specification file to load"
    )
    mcp_serve_parser.add_argument(
        "--no-resource", action="store_true", help="Do not expose file resources"
    )
    mcp_serve_parser.add_argument("--no-prompt", action="store_true", help="Do not expose prompts")

    mcp_subparsers.add_parser("list", description="List configured MCP servers")

    mcp_test_parser = mcp_subparsers.add_parser(
        "test", description="Test connection to an MCP server"
    )
    mcp_test_parser.add_argument("server_name", help="Name of the MCP server to test")

    parser.add_argument("-c", "--clean", action="store_true", help="Delete cache file after quit")
    parser.add_argument(
        "-no_color", "--no_color", action="store_true", help="Disable colorful print"
    )
    parser.add_argument(
        "-no_think", "--no_think", action="store_true", help="Disable thinking mode"
    )
    parser.add_argument("-no_yolo", "--no_yolo", action="store_true", help="Disable YOLO mode")
    parser.add_argument("--manually-cot", action="store_true", help="Enable manually CoT mode")
    parser.add_argument(
        "-s",
        "--skill-dir",
        type=str,
        nargs="*",
        default=None,
        help="Specify custom skill directory(s)",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to a JSON config file to load as default provider",
    )
    parser.add_argument(
        "--ralph",
        nargs="?",
        const=1,
        type=int,
        default=None,
        help="Enable Ralph mode (unlimited iterations) or set to specific number",
    )
    args = parser.parse_args()
    args.mcp_config = _load_project_mcp_config()

    if args.command == "mcp":
        print_debug(f"Starting kimix mcp {args.mcp_command}.")
        return "mcp", args

    if args.command == "serve":
        print_debug("Starting kimix serve (opencode-style HTTP server).")
        return "serve", args

    if args.command == "ssecli":
        print_debug("Starting kimix SSE cli (opencode-style HTTP CLI for debugging).")
        return "ssecli", args

    if args.no_color:
        base._colorful_print = False

    constants.CLEAN_MODE = args.clean
    if constants.CLEAN_MODE:
        print_debug("Clean mode ON, delete cache file after quit.")

    if args.no_think:
        base.set_default_thinking(False)
        print_debug("Thinking OFF.")
    else:
        base.set_default_thinking(True)

    if args.no_yolo:
        base.set_default_yolo(False)
        print_debug("YOLO OFF.")
    else:
        base.set_default_yolo(True)

    if args.manually_cot:
        base.set_default_manually_cot(True)
        print_debug("Manually CoT mode ON.")

    # Read skill_dir from .kimix/config.json (string or array)
    config_json_path = Path(".kimix/config.json")
    if config_json_path.exists():
        print_debug(".kimix/config.json exists.")
    config_json_path = Path(".kimix/skill.json")
    if config_json_path.exists():
        print_debug(".kimix/skill.json exists.")
        try:
            config_json = orjson.loads(config_json_path.read_text(encoding="utf-8"))
            skill_dir_cfg = config_json.get("skill_dir")
            if skill_dir_cfg is not None:
                if isinstance(skill_dir_cfg, str):
                    skill_dir_cfg = [skill_dir_cfg]
                if isinstance(skill_dir_cfg, list):
                    skill_dirs_from_cfg = []
                    for sd in skill_dir_cfg:
                        if not isinstance(sd, str):
                            continue
                        sd_path = Path(sd)
                        if not sd_path.is_absolute():
                            sd_path = constants.curr_dir / sd_path
                        sd_path = sd_path.resolve()
                        if sd_path.exists() and sd_path.is_dir():
                            skill_dirs_from_cfg.append(KaosPath(str(sd_path)))
                            print_debug(f"Skill dir from config: {str(sd_path)}")
                        else:
                            print_warning(f"Skill dir from config not found: {str(sd_path)}")
                    if skill_dirs_from_cfg:
                        existing = list(base._default_skill_dirs)
                        base.set_default_skill_dirs(existing + skill_dirs_from_cfg)
        except (orjson.JSONDecodeError, Exception) as e:
            print_warning(f"Failed to read skill_dir from .kimix/skill.json: {e}")
    # Handle --config argument
    if args.config:
        config_path = Path(args.config)
        found = False
        if config_path.exists() and config_path.is_file():
            found = True
        else:
            # Search in parent directories of current work-dir recursively
            cwd = Path.cwd()
            for parent in [cwd, *cwd.parents]:
                candidate = parent / config_path.name
                if candidate.exists() and candidate.is_file():
                    config_path = candidate
                    found = True
                    break
            # Search in parent directories of __file__ recursively
            file_dir = Path(__file__).resolve().parent
            for parent in [file_dir, *file_dir.parents]:
                candidate = parent / config_path.name
                if candidate.exists() and candidate.is_file():
                    config_path = candidate
                    found = True
                    break
        if not found:
            # Check if config_path is inside environment var PATH
            import os

            for path_dir in os.environ.get("PATH", "").split(os.pathsep):
                path_dir = path_dir.strip()
                if not path_dir:
                    continue
                candidate = Path(path_dir) / config_path.name
                if candidate.exists() and candidate.is_file():
                    config_path = candidate
                    found = True
                    break
        if not found:
            print_error(f"Config file not found: {str(config_path)}")
            sys.exit(1)
        config_path = config_path.resolve()
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config_data = orjson.loads(f.read())
            sub_provider = config_data.pop("sub_provider", None)
            base.set_default_provider(config_data)
            print_debug(f"{str(config_path)} loaded.")
            print_debug(f"Provider model: {config_data.get('model', 'None')}")
            if sub_provider and isinstance(sub_provider, dict):
                base.set_default_sub_provider(sub_provider)
                print_debug(f"Sub-provider model: {sub_provider.get('model', 'None')}")

        except orjson.JSONDecodeError as e:
            print_warning(f"Invalid JSON in config file: {str(config_path)} ({e})")
        except Exception as e:
            print_warning(f"Failed to load config file: {str(config_path)} ({e})")
    else:
        default_config_path = Path(__file__).parent.parent / "default_config.json"
        if not default_config_path.exists():
            from . import init

            init.init(False)
        if default_config_path.exists():
            try:
                config_data = orjson.loads(default_config_path.read_text(encoding="utf-8"))
                sub_provider = config_data.pop("sub_provider", None)
                base.set_default_provider(config_data)
                if sub_provider and isinstance(sub_provider, dict):
                    base.set_default_sub_provider(sub_provider)
            except orjson.JSONDecodeError, Exception:
                pass

    if args.ralph is not None:
        base._default_ralph = args.ralph
        if base._default_provider is not None:
            if "loop_control" not in base._default_provider:
                base._default_provider["loop_control"] = {}
            base._default_provider["loop_control"]["max_ralph_iterations"] = args.ralph
        print_debug(f"Ralph mode set to {args.ralph}.")

    # Handle --skill-dir argument
    if args.skill_dir:
        skill_dirs = list(base._default_skill_dirs)
        for skill_dir in args.skill_dir:
            skill_dir_path = Path(skill_dir)
            if not skill_dir_path.is_absolute():
                skill_dir_path = constants.curr_dir / skill_dir_path
            # Normalize the path (resolve ., .., and symlinks)
            skill_dir_path = skill_dir_path.resolve()
            if skill_dir_path.exists() and skill_dir_path.is_dir():
                skill_dirs.append(KaosPath(str(skill_dir_path)))
                print_debug(f"Skill dir added: {str(skill_dir_path)}")
            else:
                print_warning(f"Skill dir not found: {str(skill_dir_path)}")
        base.set_default_skill_dirs(skill_dirs)
    return None, args
