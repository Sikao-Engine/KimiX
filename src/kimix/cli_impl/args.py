import kimix.base as base
from kaos.path import KaosPath
from pathlib import Path
from . import constants
from kimix.base import print_debug, print_warning, print_error
from . import utils

import argparse
import json
import sys

def set_arg() -> tuple[bool, argparse.Namespace]:
    parser = argparse.ArgumentParser(description='Kimi Agent CLI')
    subparsers = parser.add_subparsers(dest='command', required=False)

    serve_parser = subparsers.add_parser('serve', description='Kimix HTTP server (opencode-style)')
    serve_parser.add_argument("--host", "--hostname", default="127.0.0.1", help="Host to bind to")
    serve_parser.add_argument("--port", type=int, default=4096, help="Port to bind to")

    sse_cli_parser = subparsers.add_parser("ssecli", description="Kimix SSE CLI for debug")
    sse_cli_parser.add_argument('--host', default='127.0.0.1', help='Host to connect to (for ssecli)')
    sse_cli_parser.add_argument('--port', type=int, default=4096, help='Port to connect to (for ssecli)')
    sse_cli_parser.add_argument('--debug', action='store_true',
                                help='Print all SSE stream details and save to sse_log_<date>.txt')

    parser.add_argument('-c', '--clean', action='store_true',
                        help='Delete cache file after quit')
    parser.add_argument('-no_color', '--no_color', action='store_true',
                        help='Disable colorful print')
    parser.add_argument('-no_think', '--no_think', action='store_true',
                        help='Disable thinking mode')
    parser.add_argument('-no_yolo', '--no_yolo', action='store_true',
                        help='Disable YOLO mode')
    parser.add_argument('--manually-cot', action='store_true',
                        help='Enable manually CoT mode')
    parser.add_argument('-s', '--skill-dir', type=str, nargs='*', default=None,
                        help='Specify custom skill directory(s)')
    parser.add_argument('--config', type=str, default=None,
                        help='Path to a JSON config file to load as default provider')
    parser.add_argument('--second_config', type=str, default=None,
                        help='Path to a JSON config file to load as default sub-provider')
    parser.add_argument('--ralph', nargs='?', const=-1, type=int, default=None,
                        help='Enable Ralph mode (unlimited iterations) or set to specific number')
    args = parser.parse_args()

    if args.command == 'serve':
        print_debug('Starting kimix serve (opencode-style HTTP server).')
        return "serve", args

    if args.command == 'ssecli':
        print_debug('Starting kimix SSE cli (opencode-style HTTP CLI for debugging).')
        return "ssecli", args

    if args.no_color:
        base._colorful_print = False

    constants.CLEAN_MODE = args.clean
    if constants.CLEAN_MODE:
        print_debug('Clean mode ON, delete cache file after quit.')

    if args.no_think:
        base.set_default_thinking(False)
        print_debug('Thinking OFF.')
    else:
        base.set_default_thinking(True)

    if args.no_yolo:
        base.set_default_yolo(False)
        print_debug('YOLO OFF.')
    else:
        base.set_default_yolo(True)

    if args.manually_cot:
        base.set_default_manually_cot(True)
        print_debug('Manually CoT mode ON.')

    if args.ralph is not None:
        base._default_ralph = args.ralph
        if base._default_provider is not None:
            if 'loop_control' not in base._default_provider:
                base._default_provider['loop_control'] = {}
            base._default_provider['loop_control']['max_ralph_iterations'] = args.ralph
        print_debug(f'Ralph mode set to {args.ralph}.')

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
            for path_dir in os.environ.get('PATH', '').split(os.pathsep):
                path_dir = path_dir.strip()
                if not path_dir:
                    continue
                candidate = Path(path_dir) / config_path.name
                if candidate.exists() and candidate.is_file():
                    config_path = candidate
                    found = True
                    break
        if not found:
            print_error(f'Config file not found: {str(config_path)}')
            sys.exit(1)
        config_path = config_path.resolve()
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config_data = json.load(f)
            base.set_default_provider(config_data)
            print_debug(f'{str(config_path)} loaded')
        except json.JSONDecodeError as e:
            print_warning(
                f'Invalid JSON in config file: {str(config_path)} ({e})')
        except Exception as e:
            print_warning(
                f'Failed to load config file: {str(config_path)} ({e})')
    else:
        default_config_path = Path(__file__).parent.parent / "default_config.json"
        if not default_config_path.exists():
            from . import init
            init.init(False)
        if default_config_path.exists():
            try:
                config_data = json.loads(default_config_path.read_text(encoding='utf-8'))
                base.set_default_provider(config_data)
            except (json.JSONDecodeError, Exception):
                pass
    # Handle --second_config argument
    if args.second_config:
        second_config_path = Path(args.second_config)
        found = False
        if second_config_path.exists() and second_config_path.is_file():
            found = True
        else:
            # Search in parent directories of __file__ recursively
            file_dir = Path(__file__).resolve().parent
            for parent in [file_dir, *file_dir.parents]:
                candidate = parent / second_config_path.name
                if candidate.exists() and candidate.is_file():
                    second_config_path = candidate
                    found = True
                    break
        if not found:
            # Check if second_config_path is inside environment var PATH
            import os
            for path_dir in os.environ.get('PATH', '').split(os.pathsep):
                path_dir = path_dir.strip()
                if not path_dir:
                    continue
                candidate = Path(path_dir) / second_config_path.name
                if candidate.exists() and candidate.is_file():
                    second_config_path = candidate
                    found = True
                    break
        if not found:
            print_error(f'Second config file not found: {str(second_config_path)}')
            sys.exit(1)
        second_config_path = second_config_path.resolve()
        try:
            with open(second_config_path, 'r', encoding='utf-8') as f:
                second_config_data = json.load(f)
            base.set_default_sub_provider(second_config_data)
            print_debug(f'Sub-provider config loaded from {str(second_config_path)}')
        except json.JSONDecodeError as e:
            print_warning(
                f'Invalid JSON in second config file: {str(second_config_path)} ({e})')
        except Exception as e:
            print_warning(
                f'Failed to load second config file: {str(second_config_path)} ({e})')
    # Load default second_config.json if no sub-provider has been set yet
    if base._default_sub_provider is None:
        default_second_config_path = Path(__file__).parent.parent / "second_config.json"
        if default_second_config_path.exists():
            try:
                with open(default_second_config_path, 'r', encoding='utf-8') as f:
                    second_config_data = json.load(f)
                base.set_default_sub_provider(second_config_data)
                print_debug(f'Default sub-provider config loaded from {str(default_second_config_path)}')
            except (json.JSONDecodeError, Exception):
                pass
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
                print_debug(f'Skill dir added: {str(skill_dir_path)}')
            else:
                print_warning(f'Skill dir not found: {str(skill_dir_path)}')
        base.set_default_skill_dirs(skill_dirs)
    return None, args
