"""Bash tool that executes commands via the system bash executable."""

import functools
import os
import queue
import shlex
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import kimi_cli
from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from pydantic import BaseModel, Field
from kimi_cli.session import Session
from kimi_cli.tools import SkipThisTool
from kimi_cli.tools.display import ShellDisplayBlock
from kimi_cli.share import get_share_dir

from kimix.tools.common import _maybe_export_output_async, ProcessTask

if TYPE_CHECKING:
    from kimi_agent_sdk import CallableTool2 as _CallableTool2
import platform

@functools.lru_cache(maxsize=1)
def find_bash() -> str | None:
    """Find the system bash executable.

    On Windows, prioritizes Git for Windows (msys) bash over WSL bash,
    because msys bash handles Windows paths more predictably.
    """
    if sys.platform == "win32":
        # Strategy 1: Find git location and derive the Git installation root.
        # git.exe typically resides in <GitRoot>/cmd/git.exe,
        # and bash.exe is in <GitRoot>/bin/bash.exe.
        git_path = shutil.which("git")
        if git_path:
            git_exe = Path(git_path).resolve()
            if git_exe.parent.name.lower() == "cmd":
                git_root = git_exe.parent.parent
            else:
                git_root = git_exe.parent
            for subpath in ("bin/bash.exe", "usr/bin/bash.exe"):
                bash_candidate = git_root / subpath
                if bash_candidate.exists():
                    return str(bash_candidate.resolve())
        # Strategy 2: Registry lookup for Git install location (most reliable)
        try:
            import winreg
            reg_paths = [
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Git_is1"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Git_is1"),
            ]
            for hkey, subkey in reg_paths:
                try:
                    with winreg.OpenKey(hkey, subkey) as key:
                        install_path, _ = winreg.QueryValueEx(key, "InstallLocation")
                        for subpath in ("bin/bash.exe", "usr/bin/bash.exe"):
                            bash_candidate = Path(install_path) / subpath
                            if bash_candidate.exists():
                                return str(bash_candidate.resolve())
                except FileNotFoundError:
                    pass
        except Exception:
            pass
        # Strategy 3: bash.exe via PATH (Git/bin often in PATH)
        bash_path = shutil.which("bash.exe")
        if bash_path:
            return str(Path(bash_path).resolve())

        # Strategy 4: where command
        try:
            import subprocess
            r = subprocess.run(
                ["where.exe", "bash.exe"],
                capture_output=True, text=True, check=True
            )
            return r.stdout.strip().splitlines()[0]
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

        # Strategy 5: Common paths fallback
        candidates = [
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
            r"C:\Git\bin\bash.exe",
        ]
        for candidate in candidates:
            if Path(candidate).exists():
                return str(Path(candidate).resolve())
        local_git = Path.home() / "AppData" / "Local" / "Programs" / "Git" / "bin" / "bash.exe"
        if local_git.exists():
            return str(local_git.resolve())
        scoop_git = Path.home() / "scoop" / "apps" / "git" / "current" / "bin" / "bash.exe"
        if scoop_git.exists():
            return str(scoop_git.resolve())

    if sys.platform == "darwin":
        # Strategy 1: Homebrew bash (Apple Silicon) – often newer than system bash
        candidate = Path("/opt/homebrew/bin/bash")
        if candidate.exists():
            return str(candidate.resolve())
        # Strategy 2: Homebrew bash (Intel Macs)
        candidate = Path("/usr/local/bin/bash")
        if candidate.exists():
            return str(candidate.resolve())
        # Strategy 3: MacPorts
        candidate = Path("/opt/local/bin/bash")
        if candidate.exists():
            return str(candidate.resolve())
        # Strategy 4: Git bash fallback (official Git installer for macOS)
        git_path = shutil.which("git")
        if git_path:
            git_exe = Path(git_path).resolve()
            if git_exe.parent.name.lower() == "bin":
                git_root = git_exe.parent
            else:
                git_root = git_exe.parent
            for subpath in ("bin/bash", "usr/bin/bash"):
                bash_candidate = git_root / subpath
                if bash_candidate.exists():
                    return str(bash_candidate.resolve())
        # Strategy 5: System bash (older, but guaranteed to exist)
        candidate = Path("/bin/bash")
        if candidate.exists():
            return str(candidate.resolve())

    bash = shutil.which("bash")
    if bash:
        return bash
    return None


# Characters for which a backslash escape must be preserved in bash.
# These are shell metacharacters and other special characters where
# converting \X to /X would change shell syntax or semantics.
_BASH_METACHARACTERS = frozenset("()|;&<>$\"`'*?[]{}~!#=% \t\n\r")

# In double quotes, \ only escapes these characters.
_DQ_ESCAPES = frozenset(('"', '\\'))


def _prepare_bash_cmd(cmd: str) -> str:
    r"""Prepare a command string for safe use with bash -c.

    On Windows, bash consumes backslashes as escape sequences outside of
    quotes, mangling Windows paths like ``src\kimix\tools\...`` into
    ``srckimixtools...``.  This function converts unquoted backslashes to
    forward slashes so that paths work correctly while preserving backslash
    escapes inside quoted strings (single quotes, double quotes, and ``$'…'``)
    and before bash metacharacters (e.g. ``\(``, ``\)``, ``\|``).

    On non-Windows platforms, returns the command unchanged to preserve
    existing behavior.
    """
    if sys.platform != "win32":
        return cmd

    result: list[str] = []
    i = 0
    length = len(cmd)

    while i < length:
        # ---- find the next special character ----
        # Use C-accelerated str.find (4 calls) to bulk-skip non-special chars.
        nxt = length
        pos = cmd.find('\\', i)
        if pos != -1:
            nxt = pos
        pos = cmd.find("'", i)
        if pos != -1 and pos < nxt:
            nxt = pos
        pos = cmd.find('"', i)
        if pos != -1 and pos < nxt:
            nxt = pos
        pos = cmd.find('$', i)
        if pos != -1 and pos < nxt:
            nxt = pos

        if nxt > i:
            result.append(cmd[i:nxt])
            i = nxt

        if i >= length:
            break

        char = cmd[i]

        if char == "'":
            # Single-quoted region — copy literally until closing '
            end = cmd.find("'", i + 1)
            if end == -1:
                result.append(cmd[i:])
                break
            result.append(cmd[i : end + 1])
            i = end + 1

        elif char == '"':
            # Double-quoted region — copy literally until closing "
            # In bash double quotes, \ only escapes $, `, ", \, and newline.
            # We handle \" (escaped quote) and \\ (escaped backslash) to
            # correctly find the region boundary.
            j = i + 1
            while j < length:
                # Bulk-skip to next \ or " inside the region
                nxt2 = length
                pos = cmd.find('\\', j)
                if pos != -1:
                    nxt2 = pos
                pos = cmd.find('"', j)
                if pos != -1 and pos < nxt2:
                    nxt2 = pos
                if nxt2 > j:
                    j = nxt2
                if j >= length:
                    break
                if cmd[j] == "\\" and j + 1 < length and cmd[j + 1] in _DQ_ESCAPES:
                    # Escaped quote or escaped backslash inside double quotes
                    j += 2
                elif cmd[j] == '"':
                    break
                else:
                    j += 1  # regular char (or lone backslash), advance
            if j < length:
                result.append(cmd[i : j + 1])
                i = j + 1
            else:
                result.append(cmd[i:])
                break

        elif char == "$" and i + 1 < length and cmd[i + 1] == "'":
            # $'...' ANSI-C quoted region
            j = i + 2
            while j < length:
                # Bulk-skip to next \ or ' inside the region
                nxt2 = length
                pos = cmd.find('\\', j)
                if pos != -1:
                    nxt2 = pos
                pos = cmd.find("'", j)
                if pos != -1 and pos < nxt2:
                    nxt2 = pos
                if nxt2 > j:
                    j = nxt2
                if j >= length:
                    break
                if cmd[j] == "\\" and j + 1 < length:
                    j += 2  # skip escaped char
                elif cmd[j] == "'":
                    break
                else:
                    j += 1  # regular char, advance
            if j < length:
                result.append(cmd[i : j + 1])
                i = j + 1
            else:
                result.append(cmd[i:])
                break

        elif char == "\\":
            if i + 1 < length and cmd[i + 1] in _BASH_METACHARACTERS:
                # Backslash is escaping a bash metacharacter — preserve both.
                # Append atomically so the metacharacter (e.g. ' " $) is not
                # re-processed as a quote-start or ANSI-C region on the next
                # iteration.
                result.append("\\")
                result.append(cmd[i + 1])
                i += 2
            else:
                # Unquoted backslash in a path-like context — convert to /
                result.append("/")
                i += 1

        else:
            # Should not reach here — nxt always points to a special char
            result.append(char)
            i += 1

    return "".join(result)


class BashParams(BaseModel):
    """Parameters for the Bash tool — execute a bash command via the system bash."""

    cmd: str = Field(description="Bash command.")
    timeout: int = Field(
        default=10,
        ge=3,
        le=900,
        description="Timeout in seconds."
    )
    output_path: str | None = Field(
        default=None,
        description="Output file path."
    )
    cwd: str | None = Field(
        default=None,
        description="Working directory."
    )


class Bash(CallableTool2[BashParams]):
    """Execute a bash command via the system bash, with background task support."""

    name: str = "Bash"
    description: str = "Execute a bash command via the system bash."
    params: type[BashParams] = BashParams

    def __init__(self, session: Session):
        super().__init__()
        self._session = session
        self._bash = find_bash()
        if not self._bash:
            raise SkipThisTool()

    async def __call__(self, params: BashParams) -> ToolReturnValue:
        """Execute the bash command via the system bash executable.

        Args:
            params: The parameters specifying the command and its arguments.

        Returns:
            ToolOk on success, ToolError on failure or timeout.
        """
        from kimix.tools.background.utils import remove_task_id

        if not params.cmd:
            return ToolError(
                output="Empty command.",
                message="No command specified.",
                brief="Empty command",
            )


        # Build the command line to pass to bash -c
        # On Windows, escape backslashes so bash preserves them in paths.
        safe_cmd = _prepare_bash_cmd(params.cmd)
        process_task = ProcessTask(self._bash, ["-c", safe_cmd], params.cwd, None)
        task_id = await process_task.start(self._session, "bash")

        await process_task.wait(params.timeout)

        if await process_task.thread_is_alive():
            output = await process_task.stream.get_output() if process_task.stream else ""
            return ToolError(
                output=output or f"Running in background. task_id: `{task_id}`. use `TaskOutput` or `Input`",
                message="Process timeout",
                brief=f"Timeout: {params.cmd}",
            )

        remove_task_id(self._session, task_id)

        output = await process_task.stream.pop_output() if process_task.stream else ""
        success = await process_task.stream.success() if process_task.stream else False

        # Handle output_path
        if params.output_path:
            import anyio
            async with await anyio.open_file(params.output_path, 'w', encoding='utf-8', errors='replace') as f:
                await f.write(output)
            display_path = params.output_path.replace("\\", "/")
            output = f'saved to file `{display_path}`'

        if not success:
            return ToolError(output=output, message="Command execution failed", brief=params.cmd)

        output = await _maybe_export_output_async(output)
        return ToolOk(
            output=output,
            brief="Command executed successfully",
            display_block=ShellDisplayBlock(language="shell", command=params.cmd),
        )
