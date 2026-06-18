from pathlib import Path
from typing import Any

import kimix.base as base
from kimix.base import (
    print_debug,
    print_error,
    print_info,
    print_success,
    print_warning,
    sync_all,
)
from kimix.cot import cot_prompt
from kimix.utils import _create_default_session, get_default_session, prompt

from . import constants, utils
from .args import set_arg
from .commands import _cmd_unknown, _command_map
from .utils import _input

exec_ctx: dict[str, Any] = {}


def _enable_line_editing() -> None:
    rl = utils._ensure_readline()
    if rl is None:
        import warnings
        warnings.warn(
            "On Windows, install pyreadline3 for Tab completion: pip install pyreadline3",
            stacklevel=2,
        )

def _client_cli() -> None:
    global exec_ctx
    _enable_line_editing()
    input_str = None
    _create_default_session(False)
    assert get_default_session()
    text_arr: list[str] = []

    while True:
        try:
            input_str = _input(
                "\n>>>>>>>>> Enter your prompt or command:\n", text_arr, use_completion=True)
        except KeyboardInterrupt as e:
            print_success('\nbye.')
            break
        except EOFError as e:
            print_success('\nbye.')
            break
        try:
            if len(input_str) == 0:
                continue
            if input_str is not None and input_str[0] == '/':
                task = input_str[1:]
                split_idx = task.strip().find(':')
                if split_idx >= 0:
                    task_split = [task[:split_idx], task[split_idx+1:]]
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
                        with open(path, 'r', encoding='utf-8', errors='replace') as f:
                            s = f.read()
                        suffix = path.suffix
                        if suffix == '.py':
                            print_info(
                                f'Executing {path.name}', end='\n\n')
                            try:
                                exec_ctx['__file__'] = str(path)
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
                            print_debug('File not executable, consider as prompt.')
                            input_str = s
                    except KeyboardInterrupt as e:
                        print_warning('Keyboard Interrupt.')
                        input_str = None
                    except Exception as e:
                        print_error(str(e))
                        input_str = None
                if input_str is not None and len(input_str) > 0:
                    try:
                        if base._default_manually_cot:
                            print_info('Manually CoT mode enabled: may use multiple sessions and extra tokens.')
                            cot_prompt(input_str)
                        else:
                            prompt(prompt_str=input_str,
                                   session=get_default_session())
                    except KeyboardInterrupt as e:
                        print_warning('Keyboard Interrupt.')
        except KeyboardInterrupt as e:
            print_success('\nbye.')
            break
        except Exception as e:
            import traceback
            traceback.print_exc()
            print_error(str(e))


def _run_cli() -> None:
    global exec_ctx
    exec_ctx = {'__name__': '__main__'}

    subcmd, args = set_arg()

    if subcmd == "serve":
        from kimix.server.serve import serve_cli
        serve_cli(args)
        return

    if subcmd == "ssecli":
        print_debug('Launching SSE CLI debugger.')
        from .sse_cli import run_sse_cli
        run_sse_cli(host=args.host, port=args.port, debug=getattr(args, 'debug', False))
        return

    _client_cli()
