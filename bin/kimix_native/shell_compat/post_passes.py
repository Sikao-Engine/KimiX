r"""Windows-only post-passes over the shared ``BashFix`` value.

Part of the lossless tolerant-IR ``kimix_native.shell_compat`` implementation
package behind the stable ``_shell_compat`` facade.

Why a post-pass at all
----------------------
The compatibility kernel also exists as a compiled C++ module
(``runtime_py.bash_fix``) that this repository cannot extend.  Every rule added
after that port is therefore applied to the SHARED :class:`BashFix` value by
the facade, exactly once, AFTER whichever engine produced the result::

    native.fix_bash_command(...)  ->  windows_post_passes(...)
    _py_fix_bash_command(...)     ->  windows_post_passes(...)

so the compiled kernel and the pure-Python reference stay byte-equal by
construction.  The two rules that live here are the ones the kernel cannot
express:

``native_flags``
    doubled-slash switches of native Windows tools (``msiexec //a`` ->
    ``msiexec /a``).
``cmd_command_string``
    a ``cmd``/``cmd.exe`` command string spelled as ONE bash-quoted word with
    ``\"`` escapes (``cmd /c "\"prog\" args"``) is re-split into the cmd-level
    words it described (``cmd /c "prog" args``): the shell hands a ``"``
    inside a Bash word to ``cmd.exe`` as a literal backslash + quote, so
    cmd.exe could never resolve the program.  Runs BEFORE the path rule so the
    path rule sees plain words.
``cmd_script_paths``
    relative paths inside a ``cmd``/``cmd.exe`` ``/c``/``/k`` command string
    (``cmd /c tools/build.bat`` -> ``cmd /c "tools\build.bat"``), which cmd.exe
    would otherwise split at ``/`` and read as a switch.  The rewrite is quoted
    because bash removes bare backslashes as escapes before cmd.exe runs.

Order matters: the doubled-slash repair runs FIRST so the classic Git Bash
workaround spelling ``cmd //c ...`` has become ``cmd /c ...`` before the
command-string rules look for the run switch (they accept both spellings, but
the normalized form is the one users see in the final command), and the
bash-quoting repair runs before the path repair so the path rule sees the
words the author meant instead of one opaque command string.

Both passes are pure functions of the command text and preserve the
byte-for-byte contract: when a pass changes nothing the SAME ``BashFix`` object
is returned, and the stale "backslashes to forward slashes" path note is dropped
for a word the cmd.exe repair converted back to a backslash spelling
(``cmd /c .\tools\build.bat``).
"""

from __future__ import annotations

import sys
from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from kimix_native.shell_compat.bash_core import BashFix

__all__ = ["windows_post_passes"]


def windows_post_passes(
    result: "BashFix",
    *,
    platform: str | None = None,
) -> "BashFix":
    """Apply the Windows-only post-passes to *result* and return the new value.

    ``platform`` defaults to :data:`sys.platform`; the parameter exists so the
    Windows-only behaviour is unit-testable on any host.  On a non-Windows
    platform the value is returned unchanged (the same object).
    """
    from kimix_native.shell_compat.cmd_command_string import (
        cmd_command_string_fix,
    )
    from kimix_native.shell_compat.cmd_script_paths import (
        cmd_script_path_fix,
        path_changes_after_cmd_repair,
    )
    from kimix_native.shell_compat.native_flags import native_flag_fix

    if (sys.platform if platform is None else platform) != "win32":
        return result
    if "//" in result.command:
        flag_fix = native_flag_fix(result.command)
        if flag_fix is not None:
            fixed, words = flag_fix
            result = replace(
                result,
                command=fixed,
                native_flag_fixes=result.native_flag_fixes + words,
            )
    if "cmd" in result.command.lower() and '"' in result.command:
        string_fix = cmd_command_string_fix(result.command)
        if string_fix is not None:
            fixed, programs = string_fix
            result = replace(
                result,
                command=fixed,
                cmd_command_string_fixes=(
                    result.cmd_command_string_fixes + programs
                ),
            )
    if "cmd" in result.command.lower():
        cmd_fix = cmd_script_path_fix(result.command)
        if cmd_fix is not None:
            fixed, words = cmd_fix
            # A word this repair converted back to the author's spelling makes
            # the earlier "backslashes to forward slashes" note false: drop the
            # note (the word is still a cmd.exe fix — quoting it is real).
            result = replace(
                result,
                command=fixed,
                path_changes=path_changes_after_cmd_repair(
                    result.path_changes, words
                ),
                cmd_path_fixes=result.cmd_path_fixes + words,
            )
    return result
