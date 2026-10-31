import os
import sys
from pathlib import Path
from typing import Any

from kimi_cli import native_loader

import kimix.base as base
import kimix.utils._globals as _globals
from kimix.base import sync_all
from kimix.cot import cot_prompt
from kimix.ui.printing import (
    print_debug,
    print_error,
    print_info,
    print_success,
    print_warning,
)
from kimix.utils import _create_default_session, close_session, get_default_session, prompt

from . import constants
from .args import set_arg
from .commands import _cmd_unknown, _command_map
from .ipc import CLIIPCServer, discover_servers, send_messages
from .utils import _input, _set_active_ipc_server

exec_ctx: dict[str, Any] = {}


def _check_native() -> None:
    """Detect the optional native acceleration path and log its status.

    The native library (``runtime_py.pyd`` on Windows / ``runtime_py.so`` on
    Linux & macOS, wrapped by the ``kimix_native`` shim) is an OPTIONAL
    acceleration. When it loads, an info
    log confirms it is enabled; when the binary is missing or the library is
    invalid, ``kimi_cli.native_loader`` falls back to pure Python and a concise
    warning is logged through ``kimix.ui.printing`` so the fallback does not
    go unnoticed. ``KIMIX_NATIVE=0`` (explicit opt-out) prints nothing.
    """
    if native_loader.NATIVE_AVAILABLE:
        print_debug("Native acceleration enabled.")
        return
    if os.environ.get("KIMIX_NATIVE") == "0":
        return  # Explicit opt-out; the user asked for pure Python.
    print_warning("Native acceleration unavailable, falling back to pure-Python.")


def _enable_line_editing() -> None:
    pass


def _client_cli() -> None:
    cache_dir = constants.curr_dir / ".kimix_cache"
    with CLIIPCServer(cache_dir, constants.curr_dir) as ipc_server:
        # Reap stale registrations and identify other CLIs in this project.
        discover_servers(
            cache_dir,
            constants.curr_dir,
            exclude_pid=ipc_server.pid,
        )
        _set_active_ipc_server(ipc_server)
        try:
            _client_cli_loop(ipc_server)
        finally:
            _set_active_ipc_server(None)


def _client_cli_loop(ipc_server: CLIIPCServer) -> None:
    global exec_ctx
    _enable_line_editing()
    input_str = None
    _create_default_session(False)
    assert get_default_session()
    text_arr: list[str] = []

    while True:
        try:
            input_str = _input(
                "\n>>>>>>>>> Enter your prompt or command:\n",
                text_arr,
                ipc_server,
            )
        except KeyboardInterrupt:
            print_success("\nbye.")
            break
        except EOFError:
            print_success("\nbye.")
            break
        try:
            if len(input_str) == 0:
                continue
            if input_str is not None and input_str[0] == "/":
                task = input_str[1:]
                split_idx = task.strip().find(":")
                if split_idx >= 0:
                    task_split = [task[:split_idx], task[split_idx + 1 :]]
                else:
                    task_split = [task]
                handler = _command_map.get(task_split[0], _cmd_unknown)
                new_input_str, should_break = handler(task_split, text_arr)
                if should_break:
                    break
                if new_input_str is not None:
                    input_str = new_input_str
                else:
                    continue
            elif len(input_str) > 0:
                # Test if is file path
                path = Path(input_str)
                if not path.is_absolute():
                    path = constants.curr_dir / path
                if path.is_file():
                    try:
                        with open(path, "r", encoding="utf-8", errors="replace") as f:
                            s = f.read()
                        suffix = path.suffix
                        if suffix == ".py":
                            print_info(f"Executing {path.name}", end="\n\n")
                            try:
                                exec_ctx["__file__"] = str(path)
                                exec(s, exec_ctx)
                            except KeyboardInterrupt as e:
                                raise e
                            except Exception as e:
                                import traceback

                                print_error(str(e))
                                print_error(traceback.format_exc())
                            finally:
                                sync_all()
                            input_str = None
                        else:
                            print_debug("File not executable, consider as prompt.")
                            input_str = s
                    except KeyboardInterrupt:
                        print_warning("Keyboard Interrupt.")
                        input_str = None
                    except Exception as e:
                        print_error(str(e))
                        input_str = None
                if input_str is not None and len(input_str) > 0:
                    try:
                        if base._default_manually_cot:
                            print_info(
                                "Manually CoT mode enabled: may use multiple sessions and extra tokens."
                            )
                            cot_prompt(input_str)
                        else:
                            prompt(
                                prompt_str=input_str,
                                session=get_default_session(),
                                format_output=True,
                            )
                    except KeyboardInterrupt:
                        print_warning("Keyboard Interrupt.")
        except KeyboardInterrupt:
            print_success("\nbye.")
            break
        except Exception as e:
            import traceback

            traceback.print_exc()
            print_error(str(e))

    # Clean up the session's resources (aiosqlite background thread, etc.)
    # so they do not block Python's threading._shutdown() on interpreter exit.
    session = get_default_session()
    if session is not None:
        # Save final session state to _cli_sessions before closing
        cli_sess = session._cli.session
        _globals._add_cli_session(
            cli_sess.id,
            cli_sess.title,
            cli_sess.updated_at,
            context_usage=session.status.context_usage,
            context_tokens=session.status.context_tokens,
        )
        close_session(session)


def _connect_cli(connect: str) -> None:
    """Forward this process's stdin to an existing CLI, without starting an agent."""
    requested_pid: int | None = None
    if connect:
        try:
            requested_pid = int(connect)
            if requested_pid <= 0:
                raise ValueError
        except ValueError:
            print_error(f"Invalid process ID for --connect: {connect}")
            return

    endpoints = discover_servers(
        constants.curr_dir / ".kimix_cache",
        constants.curr_dir,
        requested_pid=requested_pid,
    )
    if not endpoints:
        target = f"PID {requested_pid}" if requested_pid is not None else "any running CLI"
        print_error(f"Could not connect to {target} in {constants.curr_dir}.")
        return
    endpoint = endpoints[0]

    try:
        if sys.stdin.isatty():
            while True:
                try:
                    message = input(">>>>>>>> Message for CLI server (Ctrl+C to quit):\n")
                except (EOFError, KeyboardInterrupt):
                    print_success("\nbye.")
                    break
                send_messages(endpoint, [message])
        else:
            content = sys.stdin.read()
            if content:
                # One request carries the complete input as a list; the server
                # splits LF and CRLF into individual sentences for its main loop.
                send_messages(endpoint, [content])
    except (OSError, ConnectionError, ValueError) as error:
        print_error(f"Failed to send input to CLI server {endpoint.pid}: {error}")


def _run_cli() -> None:
    global exec_ctx
    exec_ctx = {"__name__": "__main__"}

    _check_native()

    subcmd, args = set_arg()

    if subcmd == "connect":
        _connect_cli(args.connect)
        return

    if subcmd == "serve":
        from kimix.server.serve import serve_cli

        serve_cli(args)
        return

    if subcmd == "gui":
        from .gui_cmd import run_gui

        run_gui(args)
        return

    if subcmd == "ssecli":
        print_debug("Launching SSE CLI debugger.")
        from .sse_cli import run_sse_cli

        run_sse_cli(host=args.host, port=args.port, debug=getattr(args, "debug", False))
        return

    if subcmd == "mcp":
        from .mcp_cmd import run_mcp_subcommand

        run_mcp_subcommand(args)
        return

    _client_cli()
