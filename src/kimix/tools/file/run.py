"""run tool for executing a process from a path."""
import anyio
import asyncio
from pathlib import Path
import shlex
import sys
import tempfile
from kimi_cli.tools import SkipThisTool
from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from pydantic import BaseModel, Field
from kimi_cli.session import Session
from kimix.tools.common import _maybe_export_output_async, _export_to_temp_file_async, ProcessTask
from kimi_cli.tools.display import ShellDisplayBlock
from kimi_cli.share import get_share_dir
import functools
import shlex
import shutil
_HUGE_CMD_THRESHOLD = 10000
"""Character count above which command display is culled to only the path."""

_DEFAULT_FORBIDDEN_COMMANDS = [
    "taskkill",
    "kill",
    "killall",
    "pkill",
    "xkill",
    "rd",
    "format",
    "fdisk",
    "dd",
    "mkfs",
    "shutdown",
    "reboot",
    "poweroff",
    "halt",
    "reg",
    "regedit",
    "regedt32",
]


@functools.lru_cache(maxsize=1)
def find_bash() -> str | None:
    """Find the system bash executable.

    On Windows, prioritizes Git for Windows (msys) bash over WSL bash,
    because msys bash handles Windows paths more predictably.
    """
    if sys.platform == "win32":
        # Strategy 1: read cached git address from share directory
        git_cache = get_share_dir() / "git"
        if git_cache.is_dir():
            # Git was installed directly into the share directory
            for subpath in ("bin/bash.exe", "usr/bin/bash.exe"):
                bash_candidate = git_cache / subpath
                if bash_candidate.exists():
                    return str(bash_candidate.resolve())
        elif git_cache.is_file():
            # Read cached git.exe path from file
            git_exe_path = git_cache.read_text().strip()
            if git_exe_path:
                git_exe = Path(git_exe_path).resolve()
                if git_exe.parent.name.lower() == "cmd":
                    git_root = git_exe.parent.parent
                else:
                    git_root = git_exe.parent
                for subpath in ("bin/bash.exe", "usr/bin/bash.exe"):
                    bash_candidate = git_root / subpath
                    if bash_candidate.exists():
                        return str(bash_candidate.resolve())
        # Strategy 2: Find git location and derive the Git installation root.
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
        # Strategy 3: Registry lookup for Git install location
        try:
            import winreg
            reg_paths = [
                (winreg.HKEY_LOCAL_MACHINE,
                 r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Git_is1"),
                (winreg.HKEY_LOCAL_MACHINE,
                 r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Git_is1"),
            ]
            for hkey, subkey in reg_paths:
                try:
                    with winreg.OpenKey(hkey, subkey) as key:
                        install_path, _ = winreg.QueryValueEx(
                            key, "InstallLocation")
                        for subpath in ("bin/bash.exe", "usr/bin/bash.exe"):
                            bash_candidate = Path(install_path) / subpath
                            if bash_candidate.exists():
                                return str(bash_candidate.resolve())
                except FileNotFoundError:
                    pass
        except Exception:
            pass
        # Strategy 4: bash.exe via PATH (Git/bin often in PATH)
        bash_path = shutil.which("bash.exe")
        if bash_path:
            return str(Path(bash_path).resolve())

        # Strategy 5: where command
        try:
            import subprocess
            r = subprocess.run(
                ["where.exe", "bash.exe"],
                capture_output=True, text=True, check=True
            )
            return r.stdout.strip().splitlines()[0]
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

        # Strategy 6: Common paths fallback
        candidates = [
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
            r"C:\Git\bin\bash.exe",
        ]
        for candidate in candidates:
            if Path(candidate).exists():
                return str(Path(candidate).resolve())
        local_git = Path.home() / "AppData" / "Local" / \
            "Programs" / "Git" / "bin" / "bash.exe"
        if local_git.exists():
            return str(local_git.resolve())
        scoop_git = Path.home() / "scoop" / "apps" / "git" / \
            "current" / "bin" / "bash.exe"
        if scoop_git.exists():
            return str(scoop_git.resolve())
        # Strategy 7: Download and install PortableGit to the share directory
        try:
            from kimix.tools.file.bash.install_git import install_git
            install_dir = str(get_share_dir() / "git")
            if install_git(install_dir=install_dir):
                # Retry strategy 1 after successful install
                for subpath in ("bin/bash.exe", "usr/bin/bash.exe"):
                    bash_candidate = Path(install_dir) / subpath
                    if bash_candidate.exists():
                        return str(bash_candidate.resolve())
        except Exception:
            pass

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


class RunParams(BaseModel):
    command: str = Field(
        description=(
            "Executable command line. Only real executables / processes are accepted — "
            "No shell syntax (pipes, redirects, &&, ||, variables, etc.). "
            "Example: `python -c \"print(1)\"` or `git status`."
        )
    )
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
    env: str | list[str] | None = Field(
        default=None,
        description="Environment variables to set for the subprocess."
    )
    run_in_background: bool = Field(
        default=False,
        description="Run the process in the background and return immediately."
    )


class Run(CallableTool2[RunParams]):
    name: str = "Run"
    description: str = "Run an executable or bash command."
    params: type[RunParams] = RunParams

    def __init__(self, session: Session):
        import os
        os.environ['PYTHONIOENCODING'] = 'utf-8'
        super().__init__()
        if find_bash() is not None:
            raise SkipThisTool()
        self._session = session
        self._semaphore = asyncio.Semaphore(8)
        self.use_posix = sys.platform != "win32"

        # Pre-normalize forbidden commands once at init time for O(1) per-call lookup.
        raw_forbidden = _DEFAULT_FORBIDDEN_COMMANDS + \
            self._session.custom_config.get(
                "config_json", {}).get("forbidden_commands", [])
        self._forbidden_tokens: list[list[str]] = []
        for cmd in raw_forbidden:
            if not isinstance(cmd, str) or not cmd:
                continue
            self._forbidden_tokens.append(" ".join(cmd.split()).split())

    async def __call__(self, params: RunParams) -> ToolReturnValue:
        import os
        script_path: str | None = None
        use_posix = self.use_posix

        # Split the full command into parts.
        # Use posix=False on Windows to preserve backslashes in paths;
        # on POSIX systems use posix=True for correct quoting/escaping.
        # double quotes are NOT stripped in posix=False mode, so we strip them below.
        cmd_parts: list[str] = shlex.split(params.command, posix=use_posix)
        if not cmd_parts:
            return ToolError(
                output="",
                message="Empty command.",
                brief="Empty command",
            )

        # -- Resolve the executable: progressive prefix lookup for paths with spaces --
        # First, strip outer double quotes from the first element if present.
        # (Only needed when posix=False; posix=True already strips shell quotes.)
        first = cmd_parts[0]
        if not use_posix and first.startswith('"') and first.endswith('"'):
            first = first[1:-1]
        executable = first
        args_raw: list[str] = cmd_parts[1:]

        if len(cmd_parts) > 1:
            # Try progressively longer prefixes to find an existing file,
            # so unquoted paths with spaces are handled correctly.
            for i in range(2, len(cmd_parts) + 1):
                candidate = " ".join(cmd_parts[:i])
                # Strip outer double quotes if present on the candidate
                if not use_posix and candidate.startswith('"') and candidate.endswith('"'):
                    candidate = candidate[1:-1]
                try:
                    if Path(candidate).is_file():
                        executable = candidate
                        args_raw = cmd_parts[i:]
                        break
                except OSError:
                    pass

        # Strip surrounding double quotes from each arg.
        # Only needed when posix=False, because posix=True already strips them.
        if not use_posix:
            args_list: list[str] = []
            for arg in args_raw:
                if arg.startswith('"') and arg.endswith('"'):
                    args_list.append(arg[1:-1])
                else:
                    args_list.append(arg)
        else:
            args_list = list(args_raw)

        try:
            display_args = [
                arg[:100] + '...' if len(arg) > 100 else arg for arg in args_list]
            cmd_str = shlex.join([executable] + display_args)
            display_cmd = executable if len(
                cmd_str) > _HUGE_CMD_THRESHOLD else cmd_str

            # Check forbidden commands (pre-normalized in __init__)
            if self._forbidden_tokens:
                full_cmd = params.command
                cmd_tokens = " ".join(full_cmd.split()).split()
                for forbidden_tokens in self._forbidden_tokens:
                    if len(forbidden_tokens) > len(cmd_tokens):
                        continue
                    if cmd_tokens[:len(forbidden_tokens)] == forbidden_tokens:
                        return ToolError(
                            output="",
                            message=f"Command `{full_cmd}` is forbidden by config rule.",
                            brief=display_cmd,
                        )

            # Check if executable is a valid process (in PATH or existing file),
            # then fall back to bash built-in commands.
            import shutil

            is_process = False
            is_py = False
            if (executable == 'python' and (shutil.which('python') is None) and (not Path('./python').exists())) or (executable == 'python.exe' and (shutil.which('python.exe') is None) and (not Path('./python.exe').exists())):
                executable = sys.executable
                is_process = True
                is_py = True
            elif os.sep in executable or "/" in executable:
                # Contains path separator - check if it's an existing file
                is_process = Path(executable).is_file()
            else:
                # Bare command name - check if it's in PATH
                is_process = shutil.which(executable) is not None

            if not is_process:
                # Not a real process - check if it's a bash built-in command.
                error_msg = " This tool does not support shell commands; use `Bash` tool."
                return ToolError(
                    output='',
                    message=error_msg,
                    brief='Bash not supported.'
                )

            # Handle extremely long python -c scripts via temp file (Windows CreateProcessW ~32767 limit)
            if is_py:
                c_idx = next((i for i, a in enumerate(
                    args_list) if a == '-c'), None)
                if c_idx is not None and c_idx + 1 < len(args_list) and len(args_list[c_idx + 1]) > 30000:
                    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as f:
                        f.write(args_list[c_idx + 1])
                        script_path = f.name
                    # Replace -c <code> with <script_path>, preserving leading options and trailing args
                    args_list = args_list[:c_idx] + \
                        [script_path] + args_list[c_idx + 2:]

            async with self._semaphore:
                env_dict: dict[str, str] | None = None
                if isinstance(params.env, str):
                    tokens = shlex.split(params.env)
                    env_items = []
                    i = 0
                    while i < len(tokens):
                        if i + 2 < len(tokens) and tokens[i + 1] == '=' and '=' not in tokens[i]:
                            env_items.append(f"{tokens[i]}={tokens[i + 2]}")
                            i += 3
                        else:
                            env_items.append(tokens[i])
                            i += 1
                else:
                    env_items = params.env
                if env_items:
                    env_dict = {}
                    for item in env_items:
                        if '=' in item:
                            key, value = item.split('=', 1)
                            env_dict[key] = value
                        else:
                            env_dict[item] = '1'
                task = ProcessTask(executable, args_list, params.cwd, env_dict)
                task_id = await task.start(self._session, "run", Path(executable).stem)

                if params.run_in_background:
                    return ToolOk(
                        output=f"Running in background. task_id: `{task_id}`. Use `TaskOutput` tool to retrieve output.",
                        brief="Background task started",
                        display_block=ShellDisplayBlock(
                            language="shell", command=display_cmd),
                    )

                # Wait for completion with timeout (allow a small buffer for cleanup)
                wait_timeout = params.timeout
                await task.wait(wait_timeout)

                if await task.thread_is_alive():
                    output = await task.stream.get_output() if task.stream else ""
                    return ToolError(
                        output=output,
                        message=f"Running in background. task_id: `{task_id}`. use `TaskOutput` or `Input`",
                        brief=f"Timeout: {display_cmd}",
                    )
                # Clean up foreground task registration
                from kimix.tools.background.utils import remove_task_id
                remove_task_id(self._session, task_id)

                # Get output
                output = await task.stream.pop_output() if task.stream else ""

                # Handle output export if needed
                if params.output_path:
                    async with await anyio.open_file(params.output_path, 'w', encoding='utf-8', errors='replace') as f:
                        await f.write(output)
                    display_path = params.output_path.replace("\\", "/")
                    output = f'saved to file `{display_path}`'

                # Check success
                success = await task.stream.success() if task.stream else False

                if not success:
                    if output and not params.output_path:
                        temp_path, _ = await _export_to_temp_file_async(key=None, content=output, ext='.txt')
                        display_temp_path = temp_path.replace("\\", "/")
                        output = f'saved to file `{display_temp_path}`'
                    return ToolError(
                        output=output,
                        message="Command execution failed",
                        brief=display_cmd,
                    )

                output = await _maybe_export_output_async(output)
                return ToolOk(
                    output=output,
                    brief="Command executed successfully",
                    display_block=ShellDisplayBlock(
                        language="shell", command=display_cmd),
                )
        except Exception as e:
            return ToolError(
                output='',
                message='Internal error, quit current session now.',
                brief='Internal error'
            )
        finally:
            if script_path is not None:
                try:
                    os.remove(script_path)
                except Exception:
                    pass
