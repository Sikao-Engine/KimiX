"""Vendored pure-Python mirrors of the kimi-agent bash/pwsh scanners.

Copied (with attribution) from the kimi-agent repo's kimi-cli/src/kimi_cli/tools/file/bash/
so the shim has a bit-exact fallback when the reference checkout is not
importable. Only the self-contained scanner code is vendored (no tool I/O).

Reference files: bash_fix.py (the _Scanner + fallback data), bash_tool.py
(_process_unquoted + helpers), pwsh_fix.py, process_pwsh.py.

THIS MODULE IS A STABLE COMPATIBILITY FACADE over the
``kimix_native.shell_compat`` package, which now holds the
implementation: models / spans / lexer / parser (the lossless tolerant shell
IR), fallback_data (guarded fallback command bodies and unsupported reasons),
paths / wrappers / cmd_translator / heredoc (the rule modules), translator (the
single ``translate`` orchestration), ir_command (the per-command IR decision
engine), bash_core (the Bash/Git Bash scanner and its constants), unquoted
(``_process_unquoted``), pwsh_fix (PowerShell quoting repair) and pwsh_transform
(PS7->PS5.1 source transforms).  See ``shell_compat/__init__.py`` for the layout
and the conservative pass-through policy (already Git Bash-compatible commands
pass byte-for-byte; only verified compatibility cases produce edits or guarded
fallbacks).  This file re-exports the complete historical module-level surface
(explicit imports, no ``*``), so both ``import kimix_native._shell_compat`` and
loading this exact file by path with
importlib.util.spec_from_file_location keep working unchanged.
"""

from __future__ import annotations

# ======================================================================
# bash_fix.py (kimi-agent) - Windows Git Bash compatibility scanner
# ======================================================================

# Standalone support: this file is ALSO loaded directly by path
# (importlib.util.spec_from_file_location in tests/test_bash.py) and run as
# a script (the CLI block below).  The package re-exports below need
# ``kimix_native.shell_compat`` to resolve; when it does not (no ``bin`` on
# sys.path, or an older installed ``kimix_native`` wheel without this
# package), load the in-repo package rooted next to this file.  In a normal
# package import the first try succeeds and nothing runs here.
import importlib as _imp
import importlib.util as _imp_util
import os as _os
import sys as _sys
import types as _types

try:
    _imp.import_module("kimix_native.shell_compat.bash_core")
except ImportError:  # pragma: no cover - standalone/old-wheel fallback

    def _load_sibling_package() -> None:
        _here = _os.path.dirname(_os.path.abspath(__file__))
        if _os.path.basename(_here) != "kimix_native":
            return
        _sc = _os.path.join(_here, "shell_compat")
        if not _os.path.isdir(_sc):
            return
        _pkg = _sys.modules.get("kimix_native")
        if isinstance(_pkg, _types.ModuleType) and getattr(_pkg, "__path__", None):
            if _here not in list(_pkg.__path__):
                _pkg.__path__.append(_here)
        elif _pkg is None:
            _pkg = _imp_util.module_from_spec(
                _imp_util.spec_from_file_location(
                    "kimix_native", _os.path.join(_here, "__init__.py")
                )
            )
            _sys.modules["kimix_native"] = _pkg
            _imp.import_module("kimix_native")
        _pkg = _sys.modules["kimix_native"]
        _sub_name = _pkg.__name__ + ".shell_compat"
        if _sub_name not in _sys.modules:
            _sub = _imp_util.module_from_spec(
                _imp_util.spec_from_file_location(
                    _sub_name,
                    _os.path.join(_sc, "__init__.py"),
                    submodule_search_locations=[_sc],
                )
            )
            _sys.modules[_sub_name] = _sub
        _imp.import_module(_sub_name + ".bash_core")

    _load_sibling_package()
    del _load_sibling_package

del _imp, _imp_util, _os, _sys, _types

try:
    import regex as re
except ImportError:  # pragma: no cover
    import re  # stdlib: the used patterns are all stdlib-compatible

from kimix_native.shell_compat.fallback_data import (  # noqa: F401
    _COLUMN_PERL,
    _FALLBACKS,
    _FALLBACK_BODIES,
    _FALLBACK_COMMAND_WRAPPERS,
    _KILLALL_PS,
    _NATIVE_DELEGATE,
    _PGREP_PS_FULL,
    _PGREP_PS_NAME,
    _PIDOF_PS,
    _PKILL_PS_FULL,
    _PKILL_PS_NAME,
    _POWERSHELL_PASTE,
    _REV_PERL,
    _STUB_AWARE_FALLBACKS,
    _SYSTEMINFO_PS,
    _TASKKILL_PS,
    _TASKLIST_PS,
    _TREE_PERL,
    _UNSUPPORTED_BODIES,
    _ZIP_PS,
    _fallback_definition,
    _single_quote,
    _wrapper_runner,
)

from kimix_native.shell_compat.bash_core import (  # noqa: F401
    BashFix,
    _ARITHMETIC_SPECIAL_RE,
    _ASSIGNMENT_RE,
    _BACKTICK_SPECIAL_RE,
    _BASH_METACHARACTERS,
    _BashFixScanner,
    _BashHereDoc,
    _BashWrapper,
    _CALL_BATCH_RE,
    _CMD_IF_TAIL_SAFE_CHARS,
    _CMD_REL_OPS,
    _COMMAND_END_KEYWORDS,
    _COMMAND_START_KEYWORDS,
    _COMMAND_WRAPPERS,
    _CONDITIONAL_SPECIAL_RE,
    _CONTROL_OP_STARTS,
    _CmdForMatch,
    _CmdIfMatch,
    _CmdIfSegment,
    _DQ_ESCAPED,
    _DQ_SPECIAL_RE,
    _ERRORLEVEL_RE,
    _ESCAPED_LITERAL_CHARS,
    _ESCAPED_NUL_RE,
    _EXEC_OPERAND_COMMANDS,
    _EXPANSION_SPECIAL_RE,
    _FOR_LOOP_TOKEN_RE,
    _GLUED_LABEL_RE,
    _GREP_PATTERN_FAMILY,
    _GREP_VALUE_OPTIONS,
    _HEREDOC_EXPANSION_SPECIAL_RE,
    _HEREDOC_TRAILING_OPERATORS,
    _HSPACE_RE,
    _INT_RE,
    _LIST_KEYWORDS,
    _LITERAL_SPECIAL_RE,
    _MAX_NESTING_DEPTH,
    _NAME_RE,
    _NAME_START_CHARS,
    _OPERATOR_CHARS,
    _PARAMETER_LITERAL_SPECIAL_RE,
    _PARAMETER_SPECIAL_RE,
    _PATH_DRIVE_RE,
    _PATH_GLOB_CHARS,
    _PATH_SAFE_CHARS,
    _PATH_SEGMENT_RE,
    _PATTERN_PROGRAM_COMMANDS,
    _REDIRECTION_RE,
    _REDIRECTION_START,
    _REPARSE_BUILTINS,
    _SAME_SHELL_WRAPPERS,
    _SHELL_C_OPTIONS,
    _SHELL_C_SCRIPT_OPTION_RE,
    _SHELL_KEYWORDS,
    _SHELL_VALUE_OPTIONS,
    _SHELL_WRAPPERS,
    _SPECIAL_PARAM_CHARS,
    _UNDECODED,
    _UNQUOTED_SPECIAL_RE,
    _VAR_NAME_CHARS,
    _VAR_NAME_START_CHARS,
    _WORD_END_CHARS,
    _WORD_SPECIAL_RE,
    _WRAPPER_OPERAND_COUNTS,
    _WRAPPER_OPTIONS_WITH_VALUE,
    _WRAPPER_PATH_OPTIONS,
    _WRAPPER_PATH_OPTION_LONG,
    _apply_heredoc_operator_move,
    _decode_virtual_path_span,
    _find_ansi_c_end,
    _find_backtick_end,
    _find_dq_end,
    _find_matching_paren,
    _fix_heredoc_trailing_operators,
    _match_ansi_c_quote_span,
    _match_arithmetic_span,
    _match_dquote_span,
    _match_parameter_braces,
    _opaque_inline_expansions,
    _process_unquoted,
    _read_shell_control_operator,
    _split_virtual_path_expansions,
    _windows_temp_dir,
    bash_compatibility_prelude,
    fix_bash_command,
)

from kimix_native.shell_compat.pwsh_fix import (  # noqa: F401
    PwshFix,
    _BLOCK,
    _COMMENT,
    _DQ,
    _HDQ,
    _HSQ,
    _NORMAL,
    _PWSH_NORMAL_RE,
    _PWSH_NUL_REDIRECT_RE,
    _PwshScanner,
    _SQ,
    _W_COMMENT_ONLY,
    _W_NUL_REDIRECT,
    _W_STOP_PARSING,
    _W_TRAILING_COMMENT,
    _W_TRAILING_CONTINUATION,
    _W_UNCLOSED_BLOCK,
    _W_UNCLOSED_DQ,
    _W_UNCLOSED_HDQ,
    _W_UNCLOSED_HSQ,
    _W_UNCLOSED_SQ,
    _rewrite_pwsh_nul_redirections,
    fix_pwsh_command,
)

from kimix_native.shell_compat.pwsh_transform import (  # noqa: F401
    _ASSIGN_RE,
    _CHAIN_ASSIGN_RE,
    _COMMAND_PREFIX_RE,
    _DEPTH_CLOSE,
    _DEPTH_OPEN,
    _EXPR_STOP,
    _PS_KEYWORDS,
    _REGION_PLACEHOLDER_RE,
    _REGION_START_RE,
    _RETURN_THROW_OPERAND_RE,
    _TRANSFORMS,
    _VALUE_EXPR_TAIL_RE,
    _after_dollar_question,
    _brace_open_depths,
    _build_region_mask,
    _build_replacement,
    _compute_depths,
    _emit_replacement,
    _enclosing_group_opener,
    _expr_left,
    _expr_right,
    _find_expr_end,
    _find_expr_start,
    _find_interp_op,
    _find_matching_colon,
    _find_multiline_region_open_lines,
    _find_multiline_regions,
    _find_next_op,
    _interp_edge_or_literal,
    _is_double_colon,
    _is_drive_colon,
    _is_null_conditional_qmark,
    _is_scope_colon,
    _join_backtick_continuations,
    _join_continuation_lines,
    _join_operator_continuations,
    _keeps_prefix,
    _lift_region_placeholders,
    _line_mask,
    _looks_like_value_expr,
    _mask0_span,
    _mask_code_span,
    _mask_double_quoted,
    _match_assignment,
    _scan_block_comment,
    _scan_double_quoted,
    _scan_here_string,
    _scan_member_name,
    _scan_method_args,
    _scan_single_quoted,
    _separate_trailing_comment,
    _skip_subexpression,
    _strip_command_prefix,
    _strip_keyword_operand,
    _swallow_comma_operands,
    _transform_chain_line,
    _transform_nc_line,
    _transform_nca_line,
    _transform_null_conditional_bracket,
    _transform_null_conditional_dot,
    _transform_null_conditional_line,
    _transform_operator,
    _transform_ternary_line,
    pwsh_transform,
)

__all__ = [
    "BashFix",
    "PwshFix",
    "_ARITHMETIC_SPECIAL_RE",
    "_ASSIGNMENT_RE",
    "_ASSIGN_RE",
    "_BACKTICK_SPECIAL_RE",
    "_BASH_METACHARACTERS",
    "_BLOCK",
    "_BashFixScanner",
    "_BashHereDoc",
    "_BashWrapper",
    "_CALL_BATCH_RE",
    "_CHAIN_ASSIGN_RE",
    "_CMD_IF_TAIL_SAFE_CHARS",
    "_CMD_REL_OPS",
    "_COLUMN_PERL",
    "_COMMAND_END_KEYWORDS",
    "_COMMAND_PREFIX_RE",
    "_COMMAND_START_KEYWORDS",
    "_COMMAND_WRAPPERS",
    "_COMMENT",
    "_CONDITIONAL_SPECIAL_RE",
    "_CONTROL_OP_STARTS",
    "_CmdForMatch",
    "_CmdIfMatch",
    "_CmdIfSegment",
    "_DEPTH_CLOSE",
    "_DEPTH_OPEN",
    "_DQ",
    "_DQ_ESCAPED",
    "_DQ_SPECIAL_RE",
    "_ERRORLEVEL_RE",
    "_ESCAPED_LITERAL_CHARS",
    "_ESCAPED_NUL_RE",
    "_EXEC_OPERAND_COMMANDS",
    "_EXPANSION_SPECIAL_RE",
    "_EXPR_STOP",
    "_FALLBACKS",
    "_FALLBACK_BODIES",
    "_FALLBACK_COMMAND_WRAPPERS",
    "_FOR_LOOP_TOKEN_RE",
    "_GLUED_LABEL_RE",
    "_GREP_PATTERN_FAMILY",
    "_GREP_VALUE_OPTIONS",
    "_HDQ",
    "_HEREDOC_EXPANSION_SPECIAL_RE",
    "_HEREDOC_TRAILING_OPERATORS",
    "_HSPACE_RE",
    "_HSQ",
    "_INT_RE",
    "_KILLALL_PS",
    "_LIST_KEYWORDS",
    "_LITERAL_SPECIAL_RE",
    "_MAX_NESTING_DEPTH",
    "_NAME_RE",
    "_NAME_START_CHARS",
    "_NATIVE_DELEGATE",
    "_NORMAL",
    "_OPERATOR_CHARS",
    "_PARAMETER_LITERAL_SPECIAL_RE",
    "_PARAMETER_SPECIAL_RE",
    "_PATH_DRIVE_RE",
    "_PATH_GLOB_CHARS",
    "_PATH_SAFE_CHARS",
    "_PATH_SEGMENT_RE",
    "_PATTERN_PROGRAM_COMMANDS",
    "_PGREP_PS_FULL",
    "_PGREP_PS_NAME",
    "_PIDOF_PS",
    "_PKILL_PS_FULL",
    "_PKILL_PS_NAME",
    "_POWERSHELL_PASTE",
    "_PS_KEYWORDS",
    "_PWSH_NORMAL_RE",
    "_PWSH_NUL_REDIRECT_RE",
    "_PwshScanner",
    "_REDIRECTION_RE",
    "_REDIRECTION_START",
    "_REGION_PLACEHOLDER_RE",
    "_REGION_START_RE",
    "_REPARSE_BUILTINS",
    "_RETURN_THROW_OPERAND_RE",
    "_REV_PERL",
    "_SAME_SHELL_WRAPPERS",
    "_SHELL_C_OPTIONS",
    "_SHELL_C_SCRIPT_OPTION_RE",
    "_SHELL_KEYWORDS",
    "_SHELL_VALUE_OPTIONS",
    "_SHELL_WRAPPERS",
    "_SPECIAL_PARAM_CHARS",
    "_SQ",
    "_STUB_AWARE_FALLBACKS",
    "_SYSTEMINFO_PS",
    "_TASKKILL_PS",
    "_TASKLIST_PS",
    "_TRANSFORMS",
    "_TREE_PERL",
    "_UNDECODED",
    "_UNQUOTED_SPECIAL_RE",
    "_UNSUPPORTED_BODIES",
    "_VALUE_EXPR_TAIL_RE",
    "_VAR_NAME_CHARS",
    "_VAR_NAME_START_CHARS",
    "_WORD_END_CHARS",
    "_WORD_SPECIAL_RE",
    "_WRAPPER_OPERAND_COUNTS",
    "_WRAPPER_OPTIONS_WITH_VALUE",
    "_WRAPPER_PATH_OPTIONS",
    "_WRAPPER_PATH_OPTION_LONG",
    "_W_COMMENT_ONLY",
    "_W_NUL_REDIRECT",
    "_W_STOP_PARSING",
    "_W_TRAILING_COMMENT",
    "_W_TRAILING_CONTINUATION",
    "_W_UNCLOSED_BLOCK",
    "_W_UNCLOSED_DQ",
    "_W_UNCLOSED_HDQ",
    "_W_UNCLOSED_HSQ",
    "_W_UNCLOSED_SQ",
    "_ZIP_PS",
    "_after_dollar_question",
    "_apply_heredoc_operator_move",
    "_brace_open_depths",
    "_build_region_mask",
    "_build_replacement",
    "_compute_depths",
    "_decode_virtual_path_span",
    "_emit_replacement",
    "_enclosing_group_opener",
    "_expr_left",
    "_expr_right",
    "_fallback_definition",
    "_find_ansi_c_end",
    "_find_backtick_end",
    "_find_dq_end",
    "_find_expr_end",
    "_find_expr_start",
    "_find_interp_op",
    "_find_matching_colon",
    "_find_matching_paren",
    "_find_multiline_region_open_lines",
    "_find_multiline_regions",
    "_find_next_op",
    "_fix_heredoc_trailing_operators",
    "_interp_edge_or_literal",
    "_is_double_colon",
    "_is_drive_colon",
    "_is_null_conditional_qmark",
    "_is_scope_colon",
    "_join_backtick_continuations",
    "_join_continuation_lines",
    "_join_operator_continuations",
    "_keeps_prefix",
    "_lift_region_placeholders",
    "_line_mask",
    "_looks_like_value_expr",
    "_mask0_span",
    "_mask_code_span",
    "_mask_double_quoted",
    "_match_ansi_c_quote_span",
    "_match_arithmetic_span",
    "_match_assignment",
    "_match_dquote_span",
    "_match_parameter_braces",
    "_opaque_inline_expansions",
    "_process_unquoted",
    "_read_shell_control_operator",
    "_rewrite_pwsh_nul_redirections",
    "_scan_block_comment",
    "_scan_double_quoted",
    "_scan_here_string",
    "_scan_member_name",
    "_scan_method_args",
    "_scan_single_quoted",
    "_separate_trailing_comment",
    "_single_quote",
    "_skip_subexpression",
    "_split_virtual_path_expansions",
    "_strip_command_prefix",
    "_strip_keyword_operand",
    "_swallow_comma_operands",
    "_transform_chain_line",
    "_transform_nc_line",
    "_transform_nca_line",
    "_transform_null_conditional_bracket",
    "_transform_null_conditional_dot",
    "_transform_null_conditional_line",
    "_transform_operator",
    "_transform_ternary_line",
    "_windows_temp_dir",
    "_wrapper_runner",
    "bash_compatibility_prelude",
    "fix_bash_command",
    "fix_pwsh_command",
    "pwsh_transform",
]


if __name__ == "__main__":
    import sys

    from kimix_native.shell_compat.pwsh_transform import pwsh_transform

    if len(sys.argv) > 1:
        text = " ".join(sys.argv[1:])
    else:
        text = sys.stdin.read()
    result, warnings = pwsh_transform(text)
    for w in warnings:
        print(f"[WARNING] {w}", file=sys.stderr)
    print(result)
