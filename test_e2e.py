#!/usr/bin/env python3
"""End-to-end test driver for the kimix agent workloop.

Steps:
  1. `uv run kimix --config=<cfg>`  (start the interactive CLI)
     - dry-run mode (default): appends `--dry-run`, the CLI validates the
       configs, prints the resolved plan and exits by itself. No LLM call,
       no network, no real agent workloop.
     - live mode (--live): feeds the test prompt to the REPL on stdin,
       followed by `/exit`, starting a *real* LLM agent workloop.
  2. Every byte the process prints (stdout+stderr) is recorded in
     `<generated uid>.log`; the final return value and the log path are
     printed at the end.

Usage:
  python test_e2e.py               # dry-run only (safe, offline)
  python test_e2e.py --live        # REAL agent workloop (needs a valid api_key)
  python test_e2e.py --live --timeout 1800
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = PROJECT_ROOT / "qwen_flash.json"
CONFIG_TEMPLATE = PROJECT_ROOT / "default_config.json"

DEFAULT_PROMPT = (
    "Testing current agent process, run all your tools with multiple "
    "different legal arguments, if the tool's behavior or output is not "
    "desired, write and update report to tool_report.md, "
    "if tool_report.md exists, read first, then update it."
)

EXIT_COMMAND = "/exit"  # REPL slash command: save session + exit (code 0)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def resolve_uv() -> str:
    """uv from PATH."""
    exe = shutil.which("uv")
    if exe:
        return exe
    sys.exit("error: uv not found on PATH - install it from https://docs.astral.sh/uv/")


def ensure_config(config_path: Path) -> None:
    """Create qwen_flash.json from default_config.json if it is missing."""
    if config_path.is_file():
        return
    if not CONFIG_TEMPLATE.is_file():
        sys.exit(f"error: {config_path.name} missing and no {CONFIG_TEMPLATE.name} to copy from")
    print(f"[test_e2e] WARNING: {config_path.name} not found - creating it from "
          f"{CONFIG_TEMPLATE.name} (fill in a valid api_key before --live)")
    shutil.copyfile(CONFIG_TEMPLATE, config_path)


class ProcessLogger:
    """Streams the child's merged stdout/stderr to both the log file and console."""

    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self._file = open(log_path, "w", encoding="utf-8", errors="replace", newline="")
        self.chars = 0

    def pump(self, pipe) -> None:
        for chunk in iter(lambda: pipe.read(4096), ""):
            self._file.write(chunk)
            self._file.flush()
            sys.stdout.write(chunk)
            sys.stdout.flush()
            self.chars += len(chunk)

    def close(self) -> None:
        self._file.close()


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill the process and its children (uv run wraps the kimix child)."""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:  # pragma: no cover - posix path
        proc.kill()


def run_step(cmd: list[str], log: ProcessLogger, *, input_text: str | None,
             timeout: float | None, live_stdin: bool) -> int:
    """Run one command, mirroring all output into the log. Returns exit code,
    or -9 if it had to be killed after the timeout."""
    print(f"\n[test_e2e] $ {' '.join(cmd)}")
    log._file.write(f"\n$ {' '.join(cmd)}\n")
    log._file.flush()

    proc = subprocess.Popen(
        cmd,
        cwd=PROJECT_ROOT,
        stdin=subprocess.PIPE if live_stdin else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    reader = threading.Thread(target=log.pump, args=(proc.stdout,), daemon=True)
    reader.start()

    def feed() -> None:
        try:
            proc.stdin.write(input_text)
            proc.stdin.flush()
            proc.stdin.close()  # REPL drains the queued lines, then hits EOF
        except (BrokenPipeError, OSError):
            pass

    feeder = threading.Thread(target=feed, daemon=True) if input_text else None
    if feeder:
        feeder.start()

    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"\n[test_e2e] ERROR: timed out after {timeout}s - killing process tree")
        kill_tree(proc)
        proc.wait()
        rc = -9
    reader.join(timeout=5)
    return rc


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="E2E driver for the kimix agent workloop.",
        epilog="Default (and only self-tested) mode is --dry-run: no LLM call is made. "
               "--live starts a real agent workloop against the configured provider.")
    parser.add_argument("--live", action="store_true",
                        help="run the REAL agent workloop (default: dry-run, offline)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help=f"config file for --config (default: {DEFAULT_CONFIG.name})")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT,
                        help="prompt fed to the REPL in --live mode")
    parser.add_argument("--timeout", type=float, default=900.0,
                        help="seconds before the CLI is killed (default: 900)")
    args = parser.parse_args()

    uv = resolve_uv()
    uid = uuid.uuid4().hex
    log_path = PROJECT_ROOT / f"{uid}.log"
    log = ProcessLogger(log_path)

    print(f"[test_e2e] log file : {log_path}")
    print(f"[test_e2e] mode     : {'LIVE (real LLM workloop)' if args.live else 'DRY-RUN (offline, no LLM call)'}")

    try:
        # ---- step 1: start the CLI and drive it ------------------------------
        ensure_config(args.config)
        cli_args = [uv, "run", "kimix", f"--{args.config.name}"]
        stdin_text = None
        if args.live:
            # Real workloop: the REPL reads prompt + /exit from stdin (piped REPL).
            stdin_text = args.prompt + "\n" + EXIT_COMMAND + "\n"
        else:
            cli_args.append("--dry-run")  # validate configs, print plan, exit
        rc = run_step(cli_args, log, input_text=stdin_text,
                      timeout=args.timeout, live_stdin=stdin_text is not None)

        result = "TIMEOUT (killed)" if rc == -9 else rc
        print("\n" + "=" * 60)
        print(f"[test_e2e] final return value : {result}")
        print(f"[test_e2e] log file           : {log_path}")
        print(f"[test_e2e] log size           : {log.chars} chars")
        print("=" * 60)
        return 0 if rc == 0 else 1
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
