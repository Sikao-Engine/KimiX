"""shell_compat — implementation package behind the ``_shell_compat`` facade.

Modules:
  fallback_data   guarded fallback command bodies / unsupported reasons /
                  runtime delegates and their helper constructors.
  bash_core       BashFix, _BashFixScanner, fix_bash_command and the
                  remaining Bash constants (the ``_process_unquoted``
                  emitter is canonical in ``unquoted``; bash_core delegates).
  lexer           shared O(n) tokenizer + boundary oracles (Phase 2).
  unquoted        ``_process_unquoted`` lexer-driven emitter (Phase 3).
  pwsh_fix        PwshFix, _PwshScanner, fix_pwsh_command.
  pwsh_transform  PS7 -> PS5.1 source transforms and pwsh_transform.

External consumers should keep importing kimix_native._shell_compat (the
stable facade). This __init__ re-exports the public entry points for
convenience; it is intentionally NOT the full historical surface.
"""

from __future__ import annotations

from kimix_native.shell_compat.bash_core import (  # noqa: F401
    BashFix,
    _apply_heredoc_operator_move,
    _BashFixScanner,
    _BashHereDoc,
    _BashWrapper,
    _fix_heredoc_trailing_operators,
    _process_unquoted,
    _read_shell_control_operator,
    bash_compatibility_prelude,
    fix_bash_command,
)
from kimix_native.shell_compat.fallback_data import (  # noqa: F401
    _FALLBACK_BODIES,
    _FALLBACK_COMMAND_WRAPPERS,
    _FALLBACKS,
    _UNSUPPORTED_BODIES,
    _fallback_definition,
    _single_quote,
    _wrapper_runner,
)
from kimix_native.shell_compat.pwsh_fix import (  # noqa: F401
    PwshFix,
    _PwshScanner,
    fix_pwsh_command,
)
from kimix_native.shell_compat.pwsh_transform import (  # noqa: F401
    _build_region_mask,
    pwsh_transform,
)
