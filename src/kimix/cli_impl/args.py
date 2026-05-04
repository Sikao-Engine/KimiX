import kimix.base as base
from kaos.path import KaosPath
from pathlib import Path
from . import constants
from kimix.base import print_debug, print_warning
from . import utils

import argparse
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
    parser.add_argument('-s', '--skill-dir', type=str, nargs='*', default=None,
                        help='Specify custom skill directory(s)')
    parser.add_argument('--config', type=str, default=None,
                        help='Path to a JSON config file to load as default provider')
    parser.add_argument('--ralph', action='store_true',
                        help='Enable Ralph mode (unlimited iterations)')
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

    if args.ralph:
        base._default_ralph = True
        if base._default_provider is not None:
            if 'loop_control' not in base._default_provider:
                base._default_provider['loop_control'] = {}
            base._default_provider['loop_control']['max_ralph_iterations'] = -1
        print_debug('Ralph mode ON.')

    # Handle --config argument
    if args.config:
        import json
        config_path = Path(args.config)
        if not config_path.is_absolute():
            abs_path = constants.curr_dir / config_path
            if not (abs_path.exists() and abs_path.is_file()):
                config_path = Path(__file__).parent.parent / config_path
            else:
                config_path = abs_path
        config_path = config_path.resolve()
        if config_path.exists() and config_path.is_file():
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    base.set_default_provider(json.load(f))
                print_debug(f'{str(config_path)} loaded')
            except json.JSONDecodeError as e:
                print_warning(
                    f'Invalid JSON in config file: {str(config_path)} ({e})')
            except Exception as e:
                print_warning(
                    f'Failed to load config file: {str(config_path)} ({e})')
        else:
            print_warning(f'Config file not found: {str(config_path)}')

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
