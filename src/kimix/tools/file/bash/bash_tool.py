"""Bash tool that executes commands via the system bash executable."""


import functools
import re
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

from kimix.tools.common import _maybe_export_output_async, ProcessTask, _DEFAULT_FORBIDDEN_COMMANDS

if TYPE_CHECKING:
    from kimi_agent_sdk import CallableTool2 as _CallableTool2

USE_SYSTEM_SHELL = True
USE_SYSTEM_PWSH_ON_WINDOWS = True


@functools.lru_cache(maxsize=1)
def find_bash() -> str | None:
    """Find the system bash executable."""
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
_BASH_METACHARACTERS = frozenset("()|;&<>$\"`'\"*?[]{}~!#=% \t\n\r")

# In double quotes, \ only escapes these characters.  $ and ` are included
# because \$, \` inside "..." are literal (the $ / ` is escaped, not triggering
# variable expansion or command substitution).
_DQ_ESCAPED = frozenset(('"', '\\', '$', '`'))

# Precompiled regex for finding the next special character in unquoted mode.
# Matches backslash, single quote, double quote, dollar, or backtick.
_UNQUOTED_SPECIAL_RE = re.compile(r'[\\\'"$`]')


def _find_ansi_c_end(cmd: str, start: int) -> int:
    """Return the index AFTER the closing ' of a ``$'...'`` region.

    ``start`` is the position right after the opening ``$'`` (i.e. the first
    character inside the region).  Returns ``-1`` if the region is
    unterminated.  Inside ``$'...'`` every ``\\X`` pair is treated as an
    escape (any character after \\ is skipped over).
    """
    i = start
    length = len(cmd)
    while i < length:
        c = cmd[i]
        if c == "\\" and i + 1 < length:
            i += 2
        elif c == "'":
            return i + 1
        else:
            i += 1
    return -1


def _find_backtick_end(cmd: str, start: int) -> int:
    """Return the index AFTER the closing `` ` `` of a backtick region.

    ``start`` is the position right after the opening `` ` ``.
    Returns ``-1`` if the region is unterminated.  ``\\` `` inside the
    region is an escaped backtick (literal `` ` ``).
    """
    i = start
    length = len(cmd)
    while i < length:
        c = cmd[i]
        if c == "\\" and i + 1 < length:
            i += 2  # skip escaped char (including \`)
        elif c == "`":
            return i + 1
        else:
            i += 1
    return -1


def _find_matching_paren(cmd: str, open_pos: int) -> int:
    """Return the index of the ``)`` matching the ``(`` at ``cmd[open_pos]``.

    Returns ``-1`` if no matching ``)`` is found.  Tracks nested ``$(...)``,
    single-quoted regions, double-quoted regions (including their own
    nested ``$(...)`` and backticks), and backtick regions.
    """
    assert cmd[open_pos] == "("
    depth = 1
    i = open_pos + 1
    length = len(cmd)
    while i < length:
        c = cmd[i]
        if c == "'":
            end = cmd.find("'", i + 1)
            if end == -1:
                return -1
            i = end + 1
        elif c == '"':
            i = _find_dq_end(cmd, i + 1)
            if i == -1:
                return -1
        elif c == "`":
            i = _find_backtick_end(cmd, i + 1)
            if i == -1:
                return -1
        elif c == "$" and i + 1 < length and cmd[i + 1] == "(":
            depth += 1
            i += 2
        elif c == "$" and i + 1 < length and cmd[i + 1] == "'":
            # $'...' ANSI-C quoted region — skip to its closing '
            end = _find_ansi_c_end(cmd, i + 2)
            if end == -1:
                return -1
            i = end
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
            i += 1
        else:
            i += 1
    return -1


def _find_dq_end(cmd: str, start: int) -> int:
    """Return the index AFTER the closing ``"`` of a double-quoted region.

    ``start`` is the position right after the opening ``"``.
    Returns ``-1`` if the region is unterminated.  Recognises ``\\X``
    escapes (``X`` in ``_DQ_ESCAPED``), nested ``$(...)``, ``$'...'``, and
    backtick command substitutions inside the region.
    """
    i = start
    length = len(cmd)
    while i < length:
        c = cmd[i]
        if c == "\\" and i + 1 < length and cmd[i + 1] in _DQ_ESCAPED:
            i += 2  # skip \X (X is escaped: ", \, $, `)
        elif c == '"':
            return i + 1
        elif c == "$" and i + 1 < length and cmd[i + 1] == "(":
            end = _find_matching_paren(cmd, i + 1)
            if end == -1:
                return -1
            i = end + 1
        elif c == "$" and i + 1 < length and cmd[i + 1] == "'":
            end = _find_ansi_c_end(cmd, i + 2)
            if end == -1:
                return -1
            # _find_ansi_c_end returns the index AFTER the closing '
            i = end
        elif c == "`":
            end = _find_backtick_end(cmd, i + 1)
            if end == -1:
                return -1
            # _find_backtick_end returns the index AFTER the closing `
            i = end
        else:
            i += 1
    return -1


def _process_unquoted(cmd: str) -> str:
    """Convert unquoted backslashes to forward slashes in ``cmd``.

    Walks the string in *unquoted mode* (the same rules that apply at the
    top level of a bash command): a bare ``\\`` followed by a non-metachar
    is converted to ``/``, while ``\\`` followed by a bash metacharacter,
    or ``\\`` inside single / double / ANSI-C quotes, is preserved.

    The function also descends into ``$(...)`` and backtick command
    substitutions, processing their *content* in unquoted mode as well
    (because bash runs the content of ``$(...)`` and `` ` ` `` in a
    subshell where it is parsed unquoted — even when the substitution is
    itself nested inside ``"..."``).
    """
    result: list[str] = []
    i = 0
    length = len(cmd)

    while i < length:
        # ---- find the next special character ----
        # Use a single regex search (C-accelerated) to bulk-skip non-special chars.
        m = _UNQUOTED_SPECIAL_RE.search(cmd, i)
        if m:
            nxt = m.start()
            if nxt > i:
                result.append(cmd[i:nxt])
                i = nxt
        else:
            # No more special characters — append the remaining suffix and finish.
            result.append(cmd[i:])
            break

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
            # Double-quoted region.  First find the end of the region,
            # then walk through it and convert the *content* of any
            # $(...) and `...` sub-regions using unquoted-mode rules
            # (bash runs command substitutions in a subshell where the
            # content is parsed unquoted, so backslashes inside must be
            # converted to '/' just like at the top level).
            dq_end = _find_dq_end(cmd, i + 1)
            if dq_end == -1:
                # Unterminated — copy the rest verbatim
                result.append(cmd[i:])
                break
            j = i + 1
            chunk_start = i
            while j < dq_end:
                # Bulk-skip to the next interesting character inside DQ:
                # backslash, dollar, or backtick.
                m2 = _UNQUOTED_SPECIAL_RE.search(cmd, j, dq_end)
                if m2:
                    nxt2 = m2.start()
                    if nxt2 > j:
                        j = nxt2
                else:
                    # No more special chars inside DQ — rest is verbatim
                    j = dq_end
                    break

                c = cmd[j]
                if c == "\\" and j + 1 < dq_end and cmd[j + 1] in _DQ_ESCAPED:
                    # \X inside DQ: X is escaped.  Skip the pair; it will
                    # be included in the next emitted chunk.
                    j += 2
                elif c == "$" and j + 1 < dq_end and cmd[j + 1] == "(":
                    # $(...) command substitution — process content
                    paren_end = _find_matching_paren(cmd, j + 1)
                    if paren_end == -1 or paren_end >= dq_end:
                        # Unterminated or mismatched — treat rest as verbatim
                        j = dq_end
                        break
                    result.append(cmd[chunk_start:j])
                    result.append("$(")
                    result.append(_process_unquoted(cmd[j + 2 : paren_end]))
                    result.append(")")
                    j = paren_end + 1
                    chunk_start = j
                elif c == "$" and j + 1 < dq_end and cmd[j + 1] == "'":
                    # $'...' ANSI-C region — skip through it (copied
                    # verbatim as part of the next chunk).
                    ac_end = _find_ansi_c_end(cmd, j + 2)
                    if ac_end == -1 or ac_end > dq_end:
                        # Unterminated or extends beyond DQ — treat rest as verbatim
                        j = dq_end
                        break
                    j = ac_end
                elif c == "`":
                    # Backtick command substitution — process content
                    bt_end = _find_backtick_end(cmd, j + 1)
                    if bt_end == -1 or bt_end > dq_end:
                        # Unterminated or extends beyond DQ — treat rest as verbatim
                        j = dq_end
                        break
                    result.append(cmd[chunk_start:j])
                    result.append("`")
                    result.append(_process_unquoted(cmd[j + 1 : bt_end - 1]))
                    result.append("`")
                    j = bt_end
                    chunk_start = j
                else:
                    # Should not reach here — char is not one we handle in DQ
                    j += 1
            # Emit the final chunk (up to and including the closing ")
            result.append(cmd[chunk_start:dq_end])
            i = dq_end

        elif char == "$" and i + 1 < length and cmd[i + 1] == "'":
            # $'...' ANSI-C quoted region at top level — copy literally
            ac_end = _find_ansi_c_end(cmd, i + 2)
            if ac_end == -1:
                result.append(cmd[i:])
                break
            result.append(cmd[i:ac_end])
            i = ac_end

        elif char == "`":
            # Backtick command substitution at top level — process content
            bt_end = _find_backtick_end(cmd, i + 1)
            if bt_end == -1:
                result.append(cmd[i:])
                break
            result.append("`")
            result.append(_process_unquoted(cmd[i + 1 : bt_end - 1]))
            result.append("`")
            i = bt_end

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
            # Defensive: nxt should always point to a special char we handle.
            result.append(char)
            i += 1

    return "".join(result)


def _prepare_bash_cmd(cmd: str) -> str:
    r"""Prepare a command string for safe use with bash -c.

    On Windows, bash consumes backslashes as escape sequences outside of
    quotes, mangling Windows paths like ``src\kimix\tools\...`` into
    ``srckimixtools...``.  This function converts unquoted backslashes to
    forward slashes so that paths work correctly while preserving backslash
    escapes inside quoted strings (single quotes, double quotes, and ``$'…'``)
    and before bash metacharacters (e.g. ``\(``, ``\)``, ``\|``).

    It also descends into ``$(...)`` and backtick command substitutions
    (including those nested inside double quotes), converting backslashes
    in their content, because bash runs the content of a command
    substitution in a subshell where it is parsed unquoted.

    On non-Windows platforms, returns the command unchanged to preserve
    existing behavior.
    """
    if sys.platform != "win32":
        return cmd
    return _process_unquoted(cmd)


class BashParams(BaseModel):
    """Parameters for the Bash tool — execute a bash command."""

    cmd: str = Field(description="Bash command.")
    timeout: int = Field(
        default=10,
        ge=3,
        le=900,
        description="Timeout in seconds."
    )


class Bash(CallableTool2[BashParams]):
    """Execute a bash command via the system bash, with background task support."""

    name: str = "Bash"
    description: str = "Execute a bash command. Supports Unix-style / POSIX bash syntax."
    params: type[BashParams] = BashParams

    def __init__(self, session: Session):
        super().__init__()
        if not USE_SYSTEM_SHELL:
            raise SkipThisTool()
        self._session = session
        if sys.platform == "win32" and USE_SYSTEM_PWSH_ON_WINDOWS:
            raise SkipThisTool()
        self._bash = find_bash()
        if not self._bash:
            raise SkipThisTool()

        # Pre-normalize forbidden commands once at init time for O(1) per-call lookup.
        raw_forbidden = _DEFAULT_FORBIDDEN_COMMANDS + self._session.custom_config.get("config_json", {}).get("forbidden_commands", [])
        self._forbidden_keywords: list[str] = []
        seen: set[str] = set()
        for cmd in raw_forbidden:
            if not isinstance(cmd, str) or not cmd:
                continue
            normalized = " ".join(cmd.split())
            if normalized not in seen:
                seen.add(normalized)
                self._forbidden_keywords.append(normalized)

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

        # Check forbidden commands (pre-normalized in __init__)
        if self._forbidden_keywords:
            full_cmd = params.cmd
            normalized_cmd = " ".join(full_cmd.split())
            for keyword in self._forbidden_keywords:
                if keyword in normalized_cmd:
                    return ToolError(
                        output="",
                        message=f"`{full_cmd}` is forbidden by config rule.",
                        brief="Forbidden command",
                    )

        # Build the command line to pass to bash -c
        # On Windows, escape backslashes so bash preserves them in paths.
        safe_cmd = _prepare_bash_cmd(params.cmd)
        process_task = ProcessTask(self._bash, ["-c", safe_cmd], None, None)
        task_id = await process_task.start(self._session, "bash")

        await process_task.wait(params.timeout)

        if await process_task.thread_is_alive():
            output = await process_task.stream.get_output() if process_task.stream else ""
            return ToolError(
                output=output,
                message=f"`{params.cmd}` Running in background. task_id: `{task_id}`. use `TaskOutput` or `Input`",
                brief="Timeout",
            )

        remove_task_id(self._session, task_id)

        output = await process_task.stream.pop_output() if process_task.stream else ""
        success = await process_task.stream.success() if process_task.stream else False

        if not success:
            return ToolError(output=output, message=f"`{params.cmd}` failed", brief="Command execution failed")

        output = await _maybe_export_output_async(output)
        return ToolOk(
            output=output,
            message=f'`{params.cmd}` success',
            brief="Command executed successfully",
            display_block=ShellDisplayBlock(language="shell", command=params.cmd),
        )
