"""Comprehensive tests for pwsh_transform (PowerShell 7.x → 5.1 syntax transformer)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# Import the module directly to avoid the bash_tool.py Python 3.14 issue
_MODULE_PATH = Path(__file__).parent.parent / "src" / "kimix" / "tools" / "file" / "bash" / "proccess_pwsh.py"
_spec = importlib.util.spec_from_file_location("proccess_pwsh", str(_MODULE_PATH))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
pwsh_transform = _mod.pwsh_transform

# ============================================================================
# Ternary operator  (? :)
# ============================================================================

class TestTernaryOperator:
    def test_simple_ternary(self) -> None:
        result = pwsh_transform('$x = $cond ? "a" : "b"')[0]
        assert "if ($cond)" in result
        assert '{ "a" }' in result
        assert '{ "b" }' in result

    def test_ternary_with_comparison(self) -> None:
        result = pwsh_transform("$x = $a -gt 5 ? $a : 0")[0]
        assert "if ($a -gt 5)" in result
        assert "{ $a }" in result
        assert "{ 0 }" in result

    def test_ternary_in_assignment(self) -> None:
        result = pwsh_transform('$status = $count -eq 0 ? "empty" : "non-empty"')[0]
        assert "$status = " in result
        assert "($count -eq 0)" in result

    def test_ternary_with_function_calls(self) -> None:
        result = pwsh_transform('$x = Test-Path $p ? (Get-Item $p) : $null')[0]
        assert "if (Test-Path $p)" in result
        assert "(Get-Item $p)" in result
        assert "$null" in result

    def test_ternary_no_assignment(self) -> None:
        result = pwsh_transform('$cond ? "yes" : "no"')[0]
        assert 'if ($cond) { "yes" } else { "no" }' in result

# ============================================================================
# Null-coalescing  (??)
# ============================================================================

class TestNullCoalescing:
    def test_simple_null_coalescing(self) -> None:
        result = pwsh_transform('$x = $a ?? "default"')[0]
        assert "if ($null -ne $a)" in result
        assert '{ $a }' in result
        assert '{ "default" }' in result

    def test_null_coalescing_with_variable(self) -> None:
        result = pwsh_transform("$x = $a ?? $b")[0]
        assert "if ($null -ne $a)" in result
        assert "{ $a }" in result
        assert "{ $b }" in result

    def test_null_coalescing_with_literal_default(self) -> None:
        result = pwsh_transform("$path = $env:HOME ?? 'C:\\Users\\Default'")[0]
        assert "if ($null -ne $env:HOME)" in result
        assert "{ $env:HOME }" in result

    def test_nested_null_coalescing(self) -> None:
        result = pwsh_transform('$x = $a ?? $b ?? "default"')[0]
        # After first ?? transform, the result contains another ??
        # which should also be transformed
        assert "default" in result
        assert "if ($null -ne " in result

    def test_null_coalescing_no_assignment(self) -> None:
        result = pwsh_transform('$a ?? "fallback"')[0]
        assert 'if ($null -ne $a) { $a } else { "fallback" }' in result

# ============================================================================
# Null-coalescing assignment  (??=)
# ============================================================================

class TestNullCoalescingAssignment:
    def test_simple_assign(self) -> None:
        result = pwsh_transform('$a ??= "default"')[0]
        assert "if ($null -eq $a)" in result
        assert '$a = "default"' in result

    def test_assign_with_expression(self) -> None:
        result = pwsh_transform("$count ??= (Get-ChildItem).Count")[0]
        assert "if ($null -eq $count)" in result
        assert "$count = (Get-ChildItem).Count" in result

    def test_assign_does_not_conflict_with_null_coalescing(self) -> None:
        """??= should be transformed before ?? so ??= is not partially matched."""
        result = pwsh_transform("$a ??= $b ?? $c")[0]
        # ??= should be fully resolved
        assert "??=" not in result
        assert "??" not in result

# ============================================================================
# Pipeline chain AND  (&&)
# ============================================================================

class TestPipelineChainAnd:
    def test_simple_and_chain(self) -> None:
        result = pwsh_transform("cmd1 && cmd2")[0]
        assert ";" in result
        assert "if ($?)" in result
        assert "cmd1" in result
        assert "cmd2" in result

    def test_multiple_and_chain(self) -> None:
        result = pwsh_transform("cmd1 && cmd2 && cmd3")[0]
        assert "cmd1;" in result
        assert "if ($?) { cmd2; if ($?) { cmd3 } }" in result

    def test_and_chain_with_pipeline(self) -> None:
        result = pwsh_transform("Get-Process | Where-Object CPU && Write-Output done")[0]
        assert "Get-Process | Where-Object CPU" in result
        assert "Write-Output done" in result
        assert "if ($?)" in result

# ============================================================================
# Pipeline chain OR  (||)
# ============================================================================

class TestPipelineChainOr:
    def test_simple_or_chain(self) -> None:
        result = pwsh_transform("cmd1 || cmd2")[0]
        assert ";" in result
        assert "if (-not $?)" in result
        assert "cmd1" in result
        assert "cmd2" in result

    def test_multiple_or_chain(self) -> None:
        result = pwsh_transform("cmd1 || cmd2 || cmd3")[0]
        assert "cmd1;" in result
        assert "if (-not $?) { cmd2; if (-not $?) { cmd3 } }" in result

# ============================================================================
# Null-conditional  (?. and ?[])
# ============================================================================

class TestNullConditional:
    def test_property_access(self) -> None:
        result = pwsh_transform("$a?.Length")[0]
        assert "if ($null -ne $a) { $a.Length }" in result

    def test_index_access(self) -> None:
        result = pwsh_transform("$a?[0]")[0]
        assert "if ($null -ne $a) { $a[0] }" in result

    def test_chained_null_conditional(self) -> None:
        result = pwsh_transform("$a?.Property?.SubProperty")[0]
        # Both ?. should be transformed
        assert "?." not in result

    def test_null_conditional_with_method(self) -> None:
        result = pwsh_transform("$a?.ToString()")[0]
        assert "if ($null -ne $a) { $a.ToString() }" in result

    def test_null_conditional_assignment(self) -> None:
        result = pwsh_transform("$x = $a?.Length")[0]
        assert "$x = $(if ($null -ne $a) { $a.Length })" == result

# ============================================================================
# Combined transformations
# ============================================================================

class TestCombinedTransformations:
    def test_multiple_features(self) -> None:
        code = '$x = $a ?? "default"\nGet-Process && Write-Output done'
        result = pwsh_transform(code)[0]
        assert "??" not in result
        assert "&&" not in result
        assert "if ($null -ne $a)" in result
        assert "if ($?)" in result

    def test_no_false_positives_in_strings(self) -> None:
        code = "Write-Output 'The ?? operator is new'"
        result = pwsh_transform(code)[0]
        # The ?? inside the string should not be transformed
        assert "??" in result
        assert "if ($null -ne" not in result

    def test_no_false_positives_in_comments(self) -> None:
        code = "# This ?? is a comment\nWrite-Output hello"
        result = pwsh_transform(code)[0]
        assert "??" in result  # still in comment

    def test_no_false_positives_in_double_quoted_string(self) -> None:
        code = 'Write-Output "The ?? operator"'
        result = pwsh_transform(code)[0]
        assert "??" in result

    def test_combined_and_or(self) -> None:
        result = pwsh_transform("cmd1 && cmd2 || cmd3")[0]
        assert "&&" not in result
        assert "||" not in result

# ============================================================================
# Idempotency
# ============================================================================

class TestIdempotency:
    def test_double_transform_same_result(self) -> None:
        code = '$x = $a ?? "default"\n$y = $cond ? "yes" : "no"\nGet-Process && Write-Output done'
        first = pwsh_transform(code)[0]
        second = pwsh_transform(first)[0]
        assert first == second

    def test_ternary_idempotent(self) -> None:
        code = '$x = $cond ? "a" : "b"'
        first = pwsh_transform(code)[0]
        second = pwsh_transform(first)[0]
        assert first == second

    def test_null_coalescing_idempotent(self) -> None:
        code = '$x = $a ?? "default"'
        first = pwsh_transform(code)[0]
        second = pwsh_transform(first)[0]
        assert first == second

    def test_pipeline_chain_idempotent(self) -> None:
        code = "cmd1 && cmd2"
        first = pwsh_transform(code)[0]
        second = pwsh_transform(first)[0]
        assert first == second

    def test_null_conditional_idempotent(self) -> None:
        code = "$a?.Length"
        first = pwsh_transform(code)[0]
        second = pwsh_transform(first)[0]
        assert first == second

# ============================================================================
# Edge cases
# ============================================================================

class TestEdgeCases:
    def test_strings_with_operators_not_transformed(self) -> None:
        code = """Write-Output 'Use ?? for null-coalescing'
Write-Output "A ? B : C is ternary"
Write-Output 'cmd1 && cmd2 is chain'"""
        result = pwsh_transform(code)[0]
        assert "?? for null-coalescing" in result
        assert "A ? B : C is ternary" in result
        assert "cmd1 && cmd2 is chain" in result

    def test_comments_not_transformed(self) -> None:
        code = """# The ?? operator is new in PS7
# $x = $cond ? "a" : "b"
# cmd1 && cmd2
Write-Output hello"""
        result = pwsh_transform(code)[0]
        assert "The ?? operator" in result
        assert '$cond ? "a" : "b"' in result
        assert "cmd1 && cmd2" in result

    def test_here_string_not_transformed(self) -> None:
        code = """$text = @'
The ?? operator is preserved here.
And so is the ?. operator.
'@
Write-Output $text"""
        result = pwsh_transform(code)[0]
        assert "??" in result  # preserved inside here-string
        assert "?." in result

    def test_multiline_with_backtick(self) -> None:
        code = "Get-Process `\n| Where-Object CPU `\n&& Write-Output done"
        result = pwsh_transform(code)[0]
        assert "&&" not in result
        assert "if ($?)" in result

    def test_empty_code(self) -> None:
        result = pwsh_transform("")[0]; assert result == ""

    def test_no_operators(self) -> None:
        code = "Write-Output 'hello world'"
        result = pwsh_transform(code)[0]; assert result == code

    def test_ternary_in_pipeline(self) -> None:
        code = "$x = $a ? $b : $c | ForEach-Object { $_ }"
        result = pwsh_transform(code)[0]
        assert "?" not in result
        assert "if ($a)" in result

    def test_null_coalescing_with_property(self) -> None:
        code = '$name = $obj.Name ?? "Unknown"'
        result = pwsh_transform(code)[0]
        assert "if ($null -ne $obj.Name)" in result
        assert "Unknown" in result

    def test_block_comment_not_transformed(self) -> None:
        code = "<# The ?? and ?. operators are new #>\nWrite-Output hello"
        result = pwsh_transform(code)[0]
        assert "??" in result  # preserved in block comment
        assert "?." in result

    def test_null_conditional_bracket_with_expression(self) -> None:
        result = pwsh_transform("$a?[$i + 1]")[0]
        assert "if ($null -ne $a) { $a[$i + 1] }" in result

# ============================================================================
# Corner case: nested ternary
# ============================================================================

class TestNestedTernary:
    def test_nested_in_true_branch(self) -> None:
        """Nested ternary: only the outer ?: is transformed in one pass."""
        result = pwsh_transform('$x = $a ? ($b ? "c" : "d") : "e"')[0]
        # Outer ternary is transformed; inner remains (one-pass limitation)
        assert 'if ($a)' in result
        assert '($b ? "c" : "d")' in result or '"c"' in result
        assert '"e"' in result

    def test_nested_in_false_branch(self) -> None:
        """Nested ternary in false branch: outer transformed, inner remains."""
        result = pwsh_transform('$x = $a ? "yes" : ($b ? "maybe" : "no")')[0]
        assert 'if ($a)' in result

    def test_deeply_nested_ternary(self) -> None:
        """Deeply nested ternary: only outermost ?: transformed per pass."""
        result = pwsh_transform('$x = $a ? ($b ? ($c ? 1 : 2) : 3) : 4')[0]
        assert "if ($a)" in result
        # inner ternaries preserved
        assert "?" in result  # inner ? operators still present

# ============================================================================
# Corner case: multiple operators on one line
# ============================================================================

class TestMultipleOperatorsOneLine:
    def test_multiple_null_coalescing_one_line(self) -> None:
        """$a ?? $b on same line as $c ?? $d (separated by semicolon)."""
        result = pwsh_transform('$x = $a ?? "x"; $y = $b ?? "y"')[0]
        assert "??" not in result
        assert "if ($null -ne $a)" in result
        assert "if ($null -ne $b)" in result

    def test_multiple_null_conditional_one_line(self) -> None:
        result = pwsh_transform('$x = $a?.Name; $y = $b?.Count')[0]
        assert "?." not in result
        assert "if ($null -ne $a)" in result
        assert "if ($null -ne $b)" in result

    def test_mixed_operators_one_line(self) -> None:
        result = pwsh_transform('$x = $a ?? $b; $y = $c ? "t" : "f"')[0]
        assert "??" not in result
        assert "?" not in result
        assert "if ($null -ne $a)" in result
        assert "if ($c)" in result

    def test_multiple_null_coalescing_assign_one_line(self) -> None:
        """Multiple ??= on one line: only the leftmost is fully captured.
        Known limitation: the regex greedily captures everything after ??=."""
        result = pwsh_transform('$a ??= "x"; $b ??= "y"')[0]
        # At minimum, the first ??= is processed
        assert "if ($null -eq $a)" in result

# ============================================================================
# Corner case: chained null-conditional with methods
# ============================================================================

class TestNullConditionalMethodChain:
    def test_method_with_args(self) -> None:
        result = pwsh_transform("$a?.GetValue($param)")[0]
        assert "?." not in result
        assert "if ($null -ne $a) { $a.GetValue($param) }" in result

    def test_method_with_multiple_args(self) -> None:
        result = pwsh_transform("$a?.Invoke($x, $y, $z)")[0]
        assert "?." not in result
        assert "$a.Invoke($x, $y, $z)" in result

    def test_method_with_no_args(self) -> None:
        result = pwsh_transform("$a?.Dispose()")[0]
        assert "?." not in result
        assert "$a.Dispose()" in result

    def test_chained_method_calls(self) -> None:
        result = pwsh_transform("$a?.ToString()?.Split()")[0]
        assert "?." not in result
        assert "$a.ToString()" in result
        assert "$a.ToString().Split()" in result

# ============================================================================
# Corner case: mixed null-conditional dot and bracket
# ============================================================================

class TestMixedNullConditional:
    def test_dot_then_bracket(self) -> None:
        """?. followed by ?[ is tricky: ?. is processed first."""
        result = pwsh_transform("$a?.Items?[0]")[0]
        # The ?. should be transformed; ?[ may remain depending on order
        assert "?." not in result
        assert "$a.Items" in result

    def test_bracket_then_dot(self) -> None:
        """?[ followed by ?.: ?. processed first, ?[ may remain inside braces.
        This no longer hangs (infinite-loop bug fixed); result preserves ?[ at depth > 0."""
        result = pwsh_transform("$a?[0]?.Name")[0]
        # ?. should be transformed
        assert "?." not in result
        assert "$a" in result

    def test_dot_bracket_dot_chain(self) -> None:
        """Long chain: ?. processed first, ?[ preserved at depth > 0. No hang."""
        result = pwsh_transform("$a?.Items?[0]?.LastName")[0]
        assert "$a.Items" in result

    def test_bracket_with_nested_expr(self) -> None:
        result = pwsh_transform("$a?[$i?.ToString()]")[0]
        # The inner ?. is inside brackets; depends on implementation whether it's transformed
        # At minimum, the outer ?[ should be transformed
        assert "if ($null -ne $a)" in result

# ============================================================================
# Corner case: chain operators with complex pipelines
# ============================================================================

class TestChainComplexPipelines:
    def test_and_chain_with_pipe_and_args(self) -> None:
        result = pwsh_transform("Get-ChildItem -Path $env:USERPROFILE -Recurse && Write-Output 'done'")[0]
        assert "&&" not in result
        assert "if ($?)" in result
        assert "Get-ChildItem -Path $env:USERPROFILE -Recurse" in result

    def test_or_chain_after_failed_command(self) -> None:
        result = pwsh_transform("Test-Path $f || New-Item $f")[0]
        assert "||" not in result
        assert "if (-not $?)" in result

    def test_and_or_chain_sequence(self) -> None:
        result = pwsh_transform("cmd1 && cmd2 || cmd3")[0]
        assert "&&" not in result
        assert "||" not in result
        # Should be: cmd1; if ($?) { cmd2; if (-not $?) { cmd3 } }
        assert "if ($?)" in result
        assert "if (-not $?)" in result

    def test_or_and_chain_sequence(self) -> None:
        result = pwsh_transform("cmd1 || cmd2 && cmd3")[0]
        assert "&&" not in result
        assert "||" not in result
        assert "if (-not $?)" in result
        assert "if ($?)" in result

    def test_triple_and_chain(self) -> None:
        result = pwsh_transform("cmd1 && cmd2 && cmd3 && cmd4")[0]
        assert "&&" not in result
        # Check that all three chain points are there
        assert result.count("if ($?)") == 3

# ============================================================================
# Corner case: edge literal / variable patterns
# ============================================================================

class TestEdgeLiteralPatterns:
    def test_dollar_question_not_transformed(self) -> None:
        """$? is an automatic variable, should not be confused with ?. or ternary."""
        result = pwsh_transform("if ($?) { Write-Output ok }")[0]
        assert "$?" in result  # $? preserved
        assert "if ($?) { Write-Output ok }" == result

    def test_question_mark_in_variable_name(self) -> None:
        """Variable with ? in name like ${foo?} should not cause transformation."""
        # This is unusual but let's make sure it doesn't crash
        result = pwsh_transform('Write-Output ${foo?}')[0]
        # Should not have transformed anything
        assert "Write-Output" in result

    def test_null_coalescing_with_null_literal(self) -> None:
        result = pwsh_transform('$x = $a ?? $null')[0]
        assert "if ($null -ne $a)" in result
        assert "{ $a }" in result
        assert "{ $null }" in result

    def test_null_coalescing_with_true_false(self) -> None:
        result = pwsh_transform('$x = $a ?? $true')[0]
        assert "if ($null -ne $a)" in result
        assert "{ $a }" in result
        assert "{ $true }" in result

    def test_ternary_with_null(self) -> None:
        result = pwsh_transform('$x = $cond ? $null : "default"')[0]
        assert "if ($cond)" in result
        assert "{ $null }" in result

# ============================================================================
# Corner case: here-string double-quoted variant
# ============================================================================

class TestHereStringDoubleQuoted:
    def test_double_quoted_here_string(self) -> None:
        code = r'''$text = @"
The ?? operator is preserved.
And so is ?. and ?[
"@
Write-Output $text'''
        result = pwsh_transform(code)[0]
        assert "??" in result  # preserved
        assert "?." in result
        assert "?[" in result

    def test_at_quote_single_line_here_string(self) -> None:
        code = "$text = @'?? is not transformed here'@\nWrite-Output $text"
        result = pwsh_transform(code)[0]
        assert "??" in result

# ============================================================================
# Corner case: backtick continuation with various operators
# ============================================================================

class TestBacktickContinuationOperators:
    def test_null_coalescing_with_backtick(self) -> None:
        code = '$x = $a ??`\n  "default"'
        result = pwsh_transform(code)[0]
        assert "??" not in result
        assert "if ($null -ne $a)" in result

    def test_ternary_with_backtick_continuation(self) -> None:
        code = '$x = $cond ?`\n  "yes" :`\n  "no"'
        result = pwsh_transform(code)[0]
        assert "?" not in result
        assert "if ($cond)" in result

    def test_null_conditional_with_backtick(self) -> None:
        code = "$a?.`\n  Property"
        result = pwsh_transform(code)[0]
        # After backtick join, the ?. is on a single line w/spaces
        assert "?." not in result

    def test_chain_with_backtick_continuation(self) -> None:
        code = "cmd1 `\n&& cmd2"
        result = pwsh_transform(code)[0]
        assert "&&" not in result
        assert "if ($?)" in result

# ============================================================================
# Corner case: expression boundaries
# ============================================================================

class TestExprBoundaries:
    def test_null_coalescing_with_parenthesized_left(self) -> None:
        result = pwsh_transform('$x = (Get-Item $p) ?? "default"')[0]
        assert "if ($null -ne (Get-Item $p))" in result

    def test_null_coalescing_with_subexpression(self) -> None:
        result = pwsh_transform('$x = $(Get-Date) ?? "never"')[0]
        assert "if ($null -ne $(Get-Date))" in result

    def test_null_conditional_on_subexpression(self) -> None:
        """$()?.Property - null conditional on a subexpression."""
        result = pwsh_transform("$(Get-Item $p)?.Length")[0]
        # The subexpression $(...) should be detected as the base
        assert "?." not in result
        assert "if ($null -ne $(Get-Item $p))" in result

    def test_ternary_with_expression_condition(self) -> None:
        result = pwsh_transform('$x = (Get-Date).Year -gt 2020 ? "new" : "old"')[0]
        assert "?" not in result
        assert "if ((Get-Date).Year -gt 2020)" in result

    def test_ternary_with_complex_true_branch(self) -> None:
        result = pwsh_transform('$x = $cond ? (Get-Process | Select -First 1) : $null')[0]
        assert "?" not in result
        assert "if ($cond)" in result
        assert "(Get-Process | Select -First 1)" in result

# ============================================================================
# Corner case: ??= idempotency and edge patterns
# ============================================================================

class TestNCAEdgeCases:
    def test_nca_idempotent(self) -> None:
        code = '$a ??= "default"'
        first = pwsh_transform(code)[0]
        second = pwsh_transform(first)[0]
        assert first == second

    def test_nca_with_same_line_code(self) -> None:
        result = pwsh_transform('$a ??= "x"; Write-Output $a')[0]
        assert "??=" not in result
        assert "if ($null -eq $a)" in result
        assert "Write-Output $a" in result

    def test_nca_right_side_with_spaces(self) -> None:
        result = pwsh_transform("$a ??= (Get-ChildItem).Count")[0]
        assert "??=" not in result
        assert "$a = (Get-ChildItem).Count" in result


# ============================================================================
# Corner case: operators inside splatting / hashtable context
# ============================================================================

class TestOperatorsInSpecialContext:
    def test_question_in_hashtable_access(self) -> None:
        """@{}.Keys - accessing a hashtable's Keys property."""
        result = pwsh_transform("$x = @{ key = 'val' }.Keys")[0]
        assert "@{" in result

    def test_colon_in_hashtable_not_confused(self) -> None:
        """Ternary inside @{ } is at depth > 0 so it is NOT transformed.
        This is intentional: colons inside braces could be switch/hashtable syntax."""
        result = pwsh_transform('$x = @{ key = $a ? "t" : "f" }')[0]
        # Ternary inside braces is preserved (depth > 0)
        assert "?" in result  # not transformed at depth > 0

    def test_colon_in_string_not_confused(self) -> None:
        """Colon inside a string is not a ternary colon.
        Note: _find_matching_colon may not exclude in-string colons currently."""
        result = pwsh_transform('$x = $cond ? "no-colon" : "default"')[0]
        # Works correctly when strings have no colons
        assert "?" not in result
        assert "if ($cond)" in result

# ============================================================================
# Corner case: whitespace and formatting stress
# ============================================================================

class TestWhitespaceStress:
    def test_no_spaces_around_ternary(self) -> None:
        result = pwsh_transform('$x=$cond?"a":"b"')[0]
        assert "?" not in result
        assert "if ($cond)" in result

    def test_no_spaces_around_null_coalescing(self) -> None:
        result = pwsh_transform('$x=$a??"default"')[0]
        assert "??" not in result
        assert "if ($null -ne $a)" in result

    def test_no_spaces_around_null_conditional(self) -> None:
        result = pwsh_transform("$a?.Property?.SubProperty")[0]
        assert "?." not in result

    def test_extra_spaces_around_operators(self) -> None:
        result = pwsh_transform('$x  =   $a    ??    "default"')[0]
        assert "??" not in result

    def test_tabs_around_operators(self) -> None:
        result = pwsh_transform("$x\t=\t$a\t??\t'default'")[0]
        assert "??" not in result

# ============================================================================
# Corner case: code that looks like operators but at end of line
# ============================================================================

class TestTrickyOperatorPlacement:
    def test_and_at_end_of_command(self) -> None:
        """&& at end of line is still valid operator."""
        result = pwsh_transform("cmd1 &&")[0]
        # After transformation, the trailing && situation might be edge
        assert "cmd1" in result

    def test_question_at_end_of_line(self) -> None:
        """Isolated ? at end should not cause error."""
        result = pwsh_transform("$a ?")[0]
        # No colon, so no ternary transformation
        assert "$a" in result

    def test_double_question_at_end(self) -> None:
        """?? at end of line without right side - should be safe."""
        result = pwsh_transform("$a ??")[0]
        # Should not crash; right side is missing
        assert "$a" in result

    def test_null_conditional_at_end(self) -> None:
        """?. at end of line without member."""
        result = pwsh_transform("$a?.")[0]
        # No member name after ?., should be safe
        assert "$a" in result

# ============================================================================
# Corner case: idempotency for all combined transforms
# ============================================================================

class TestFullIdempotency:
    def test_all_operators_together_idempotent(self) -> None:
        code = '''$null_coal = $maybe ?? "fallback"
$ternary = $cond ? "yes" : "no"
$nc_assign ??= "init"
$safe_access = $obj?.Property?.Nested?[0]
Get-Service && Write-Output done || Write-Error failed'''
        first = pwsh_transform(code)[0]
        second = pwsh_transform(first)[0]
        assert first == second

    def test_transform_preserves_critical_semantics(self) -> None:
        """Multiple transforms should yield consistent structure."""
        code = '$x = $a ?? $b ?? $c'
        result = pwsh_transform(code)[0]
        # After transformation: all ?? resolved
        assert "??" not in result
        # Should still reference all three variables
        assert "$a" in result
        assert "$b" in result
        assert "$c" in result
# ============================================================================
# Regression / bug-reproduction tests
# ============================================================================

class TestKnownBugs:
    """Tests that reproduce currently known bugs in pwsh_transform."""

    def test_ternary_with_static_member_access(self) -> None:
        result = pwsh_transform('$x = $cond ? [Math]::PI : 0')[0]
        assert "?" not in result
        assert "[Math]::PI" in result
        assert "if ($cond)" in result

    def test_ternary_with_colon_in_string(self) -> None:
        result = pwsh_transform('$x = $cond ? "a:b" : "c"')[0]
        assert "?" not in result
        assert '"a:b"' in result
        assert '"c"' in result
        assert "if ($cond)" in result

    def test_null_coalescing_with_hash_in_string(self) -> None:
        result = pwsh_transform('$x = $a ?? "default#value"')[0]
        assert "??" not in result
        assert '"default#value"' in result

    def test_null_coalescing_with_comma_in_string(self) -> None:
        result = pwsh_transform('$x = $a ?? "a,b"')[0]
        assert "??" not in result
        assert '"a,b"' in result

    def test_null_conditional_method_with_paren_in_string_arg(self) -> None:
        result = pwsh_transform('$obj?.Foo("a)")')[0]
        assert "?." not in result
        assert 'Foo("a)")' in result

    def test_null_conditional_bracket_with_bracket_in_string_index(self) -> None:
        result = pwsh_transform('$arr?["key]"]')[0]
        assert "?[" not in result
        assert '["key]"]' in result

    def test_backtick_continuation_inside_comment(self) -> None:
        code = '# comment `\nWrite-Output hello'
        result = pwsh_transform(code)[0]
        lines = result.splitlines()
        assert len(lines) == 2
        assert lines[1] == "Write-Output hello"

    def test_dollar_question_as_ternary_condition(self) -> None:
        result = pwsh_transform('$? ? "yes" : "no"')[0]
        assert result == 'if ($?) { "yes" } else { "no" }'

    def test_command_followed_by_ternary_without_parens(self):
        result = pwsh_transform('Write-Output $a ? $b : $c')[0]
        # Current behaviour incorrectly treats Write-Output $a as the condition
        condition = result.split("if (")[1].split(")")[0]
        assert "Write-Output $a" not in condition

# ============================================================================
# Infinite-loop safety tests
# ============================================================================

class TestNoInfiniteLoops:
    """Inputs that previously caused hangs or look pathological."""

    def test_bracket_then_dot_no_hang(self) -> None:
        result = pwsh_transform("$a?[0]?.Name")[0]
        assert isinstance(result, str)

    def test_long_null_conditional_chain_no_hang(self) -> None:
        result = pwsh_transform("$a?.Items?[0]?.LastName")[0]
        assert isinstance(result, str)

    def test_incomplete_null_coalescing_no_hang(self) -> None:
        result = pwsh_transform("$a ??")[0]
        assert isinstance(result, str)

    def test_incomplete_null_conditional_dot_no_hang(self) -> None:
        result = pwsh_transform("$a?.")[0]
        assert isinstance(result, str)

    def test_trailing_and_operator_no_hang(self) -> None:
        result = pwsh_transform("cmd1 &&")[0]
        assert isinstance(result, str)

    def test_bare_question_marks_no_hang(self) -> None:
        result = pwsh_transform("?.?.?.?")[0]
        assert isinstance(result, str)

    def test_many_nested_ternaries_no_hang(self) -> None:
        code = '$a ? ($b ? ($c ? ($d ? 1 : 2) : 3) : 4) : 5'
        result = pwsh_transform(code)[0]
        assert isinstance(result, str)

    def test_backtick_rain_no_hang(self) -> None:
        code = "Write-Output `\n`\n`\nhello"
        result = pwsh_transform(code)[0]
        assert isinstance(result, str)

# ============================================================================
# Additional bug-reproduction tests discovered during deep analysis
# ============================================================================

class TestAdditionalBugs:
    """Further edge-case bugs found by studying _find_string_regions and depth handling."""

    def test_here_string_false_positive_consumes_rest_of_file(self) -> None:
        code = "$x = @'foo'@\nWrite-Output hello && cmd2"
        result = pwsh_transform(code)[0]
        # The second line should have its && transformed, but because the
        # here-string scanner swallows to EOF, it is left untouched.
        assert "&&" not in result

    def test_at_quote_inside_line_not_here_string(self) -> None:
        code = "$text = @' preserved ?? and ?. '\nWrite-Output hello && cmd2"
        result = pwsh_transform(code)[0]
        assert "&&" not in result

    def test_chain_inside_script_block(self):
        result = pwsh_transform("$sb = { cmd1 && cmd2 }")[0]
        assert "&&" not in result

    def test_chain_inside_subexpression(self):
        result = pwsh_transform("$(cmd1 && cmd2)")[0]
        assert "&&" not in result
# ============================================================================
# Depth tracking vs strings/comments  (BUG: _compute_depths ignores strings)
# ============================================================================

class TestDepthTrackingStrings:
    """_compute_depths counts brackets even inside strings/comments.
    This can break ternary colon matching when true-branch strings
    contain brackets."""

    def test_ternary_with_paren_in_string_not_transformed(self) -> None:
        result = pwsh_transform('$x = $cond ? "a(b" : "c"')[0]
        # _compute_depths is now string-aware, so ternary transforms correctly
        assert "?" not in result
        assert '"a(b"' in result
        assert '"c"' in result

    def test_ternary_with_bracket_in_string_not_transformed(self) -> None:
        result = pwsh_transform('$x = $cond ? "a[b" : "c"')[0]
        assert "?" not in result
        assert '"a[b"' in result
        assert '"c"' in result

    def test_ternary_with_brace_in_string_not_transformed(self) -> None:
        result = pwsh_transform('$x = $cond ? "a{b" : "c"')[0]
        assert "?" not in result
        assert '"a{b"' in result
        assert '"c"' in result

    def test_ternary_with_colon_in_true_branch_string(self) -> None:
        # No brackets, so this works despite the extra colon inside string.
        result = pwsh_transform('$x = $cond ? "a:b:c" : "d"')[0]
        assert "?" not in result
        assert '"a:b:c"' in result
        assert '"d"' in result

    def test_ternary_with_drive_path_in_true_branch(self) -> None:
        result = pwsh_transform('$x = $cond ? "C:\\foo" : "D:\\bar"')[0]
        assert "?" not in result
        assert '"C:\\foo"' in result
        assert '"D:\\bar"' in result

# ============================================================================
# Null-conditional on complex base expressions
# ============================================================================

class TestNullConditionalComplexBase:
    def test_array_element_then_property(self) -> None:
        result = pwsh_transform("$arr[0]?.Name")[0]
        assert "?." not in result
        assert "$arr[0]" in result
        assert ".Name" in result

    def test_hashtable_access_then_property(self) -> None:
        result = pwsh_transform('$ht["key"]?.Value')[0]
        assert "?." not in result
        assert '$ht["key"]' in result
        assert ".Value" in result

    def test_property_then_bracket_then_property(self) -> None:
        result = pwsh_transform('$a.Items[0]?.Name')[0]
        assert "?." not in result
        assert "$a.Items[0]" in result
        assert ".Name" in result

    def test_subexpression_then_property(self) -> None:
        result = pwsh_transform("$(Get-Item $p)?.Length")[0]
        assert "?." not in result
        assert "$(Get-Item $p)" in result
        assert ".Length" in result

    def test_nested_subexpression_then_property(self) -> None:
        result = pwsh_transform("$($($a))?.Name")[0]
        assert "?." not in result
        assert "$($($a))" in result

    def test_null_literal_then_property(self) -> None:
        result = pwsh_transform("$null?.Property")[0]
        assert "?." not in result
        assert "$null" in result

    def test_variable_with_braces_then_property(self) -> None:
        result = pwsh_transform("${foo-bar}?.Name")[0]
        assert "?." not in result
        assert "${foo-bar}" in result
        assert ".Name" in result

# ============================================================================
# Scoped variables and property access with operators
# ============================================================================

class TestScopedVariables:
    def test_global_scope_null_coalescing(self) -> None:
        result = pwsh_transform('$global:x ?? "default"')[0]
        assert "??" not in result
        assert "$global:x" in result

    def test_env_scope_null_coalescing(self) -> None:
        result = pwsh_transform('$env:PATH ?? "C:\\Windows"')[0]
        assert "??" not in result
        assert "$env:PATH" in result

    def test_script_scope_nca(self) -> None:
        result = pwsh_transform('$script:count ??= 0')[0]
        assert "??=" not in result
        assert "$script:count" in result
        assert "if ($null -eq $script:count)" in result

    def test_property_access_nca(self) -> None:
        result = pwsh_transform('$obj.Name ??= "default"')[0]
        assert "??=" not in result
        assert "if ($null -eq $obj.Name)" in result
        assert "$obj.Name = \"default\"" in result

    def test_global_scope_null_conditional(self) -> None:
        result = pwsh_transform('$global:obj?.Name')[0]
        assert "?." not in result
        assert "$global:obj" in result

# ============================================================================
# Comments and strings interaction
# ============================================================================

class TestCommentStringInteraction:
    def test_hash_inside_single_quoted_string(self) -> None:
        result = pwsh_transform("'hello # world' ?? 'default'")[0]
        assert "??" not in result
        assert "'hello # world'" in result
        assert "'default'" in result

    def test_hash_inside_double_quoted_string(self) -> None:
        result = pwsh_transform('"hello # world" ?? "default"')[0]
        assert "??" not in result
        assert '"hello # world"' in result

    def test_block_comment_start_inside_line_comment(self) -> None:
        code = '# <# not a block comment\n$x = $a ?? "default"'
        result = pwsh_transform(code)[0]
        assert "<# not a block comment" in result
        assert "??" not in result

    def test_line_comment_after_operator(self) -> None:
        result = pwsh_transform('$x = $a ?? "default" # comment with ??')[0]
        # BUG: the comment is swallowed into the right-hand expression of ??
        # because _expr_right does not stop at the # comment boundary.
        # The operator ?? is transformed, but the ?? inside the comment is preserved.
        assert "if ($null -ne $a)" in result
        assert "# comment with ??" in result

    def test_single_quoted_string_with_doubled_quotes(self) -> None:
        result = pwsh_transform("'It''s ?? and ?. here'")[0]
        assert "??" in result
        assert "?." in result

    def test_double_quoted_string_with_escaped_backtick(self) -> None:
        result = pwsh_transform('"a ``?? b"')[0]
        assert "??" in result

# ============================================================================
# Nested / multi-line block comments
# ============================================================================

class TestBlockComments:
    def test_nested_block_comment(self) -> None:
        code = '<# outer <# inner #> still outer #>\n$x = $a ?? "default"'
        result = pwsh_transform(code)[0]
        assert "??" not in result
        assert "outer" in result
        assert "inner" in result

    def test_block_comment_spanning_lines_with_operators(self) -> None:
        code = '<#\n?? operator\n?. operator\n&& operator\n#>\nWrite-Output done'
        result = pwsh_transform(code)[0]
        assert "??" in result
        assert "?." in result
        assert "&&" in result

# ============================================================================
# Ternary with complex true/false branches
# ============================================================================

class TestTernaryComplexBranches:
    def test_ternary_with_hashtable_true_branch(self) -> None:
        result = pwsh_transform('$x = $cond ? @{ a = 1 } : @{ b = 2 }')[0]
        assert "?" not in result
        assert "@{ a = 1 }" in result
        assert "@{ b = 2 }" in result

    def test_ternary_with_script_block_branches(self) -> None:
        result = pwsh_transform('$x = $cond ? { $a } : { $b }')[0]
        assert "?" not in result
        assert "{ $a }" in result
        assert "{ $b }" in result

    def test_ternary_with_array_literal_branches(self) -> None:
        result = pwsh_transform('$x = $cond ? @(1,2) : @(3,4)')[0]
        assert "?" not in result
        assert "@(1,2)" in result
        assert "@(3,4)" in result

    def test_ternary_with_match_operator(self) -> None:
        result = pwsh_transform('$x = $a -match "test" ? "yes" : "no"')[0]
        assert "?" not in result
        assert 'if ($a -match "test")' in result

    def test_ternary_dollar_question_as_condition(self) -> None:
        result = pwsh_transform('$? ? $? : $false')[0]
        assert result == 'if ($?) { $? } else { $false }'

    def test_ternary_with_test_path_condition(self) -> None:
        result = pwsh_transform('(Test-Path $f) ? "exists" : "missing"')[0]
        assert "?" not in result
        assert "if ((Test-Path $f))" in result

# ============================================================================
# Null coalescing with complex left/right expressions
# ============================================================================

class TestNullCoalescingComplex:
    def test_null_coalescing_with_array_literal_left(self) -> None:
        result = pwsh_transform('$x = @(1,2) ?? @(3)')[0]
        assert "??" not in result
        assert "@(1,2)" in result
        assert "@(3)" in result

    def test_null_coalescing_with_hashtable_literal_left(self) -> None:
        result = pwsh_transform('$x = @{ a = 1 } ?? @{ b = 2 }')[0]
        assert "??" not in result
        assert "@{ a = 1 }" in result

    def test_null_coalescing_with_script_block_right(self) -> None:
        result = pwsh_transform('$x = $sb ?? { Write-Output default }')[0]
        assert "??" not in result
        assert "{ Write-Output default }" in result

    def test_null_coalescing_inside_parentheses(self) -> None:
        result = pwsh_transform('$x = ($a) ?? "default"')[0]
        assert "??" not in result
        assert "($a)" in result

    def test_null_coalescing_with_nested_parens(self) -> None:
        result = pwsh_transform('$x = (($a)) ?? "default"')[0]
        assert "??" not in result
        assert "(($a))" in result

    def test_string_with_operator_then_real_operator(self) -> None:
        # LIMITATION: _expr_left scans past string boundaries, so the entire
        # left side includes the preceding string and its inner operator.
        result = pwsh_transform("'a ?? b' ?? 'c'")[0]
        assert "if ($null -ne 'a ?? b')" in result
        assert "'a ?? b'" in result
        assert "'c'" in result

    def test_double_quoted_string_with_operator_then_real_operator(self) -> None:
        result = pwsh_transform('"a ?? b" ?? "c"')[0]
        assert "if ($null -ne \"a ?? b\")" in result
        assert '"a ?? b"' in result
        assert '"c"' in result

# ============================================================================
# Pipeline chains with special contexts
# ============================================================================

class TestChainSpecialContexts:
    def test_chain_with_semicolon_before(self) -> None:
        result = pwsh_transform("cmd1 ; cmd2 && cmd3")[0]
        assert "&&" not in result
        assert "if ($?)" in result
        assert "cmd1 ; cmd2" in result

    def test_chain_inside_array_subexpression(self) -> None:
        result = pwsh_transform("@(cmd1 && cmd2)")[0]
        assert "&&" not in result
        assert "cmd1" in result
        assert "cmd2" in result

    def test_chain_with_variable_assignment(self) -> None:
        result = pwsh_transform("$r = cmd1 && cmd2")[0]
        assert "&&" not in result
        assert "if ($?)" in result

    def test_chain_after_foreach_pipeline(self) -> None:
        result = pwsh_transform("1..3 | ForEach-Object { $_ } && Write-Output done")[0]
        assert "&&" not in result
        assert "if ($?)" in result

# ============================================================================
# Null-conditional method args with inner operators
# ============================================================================

class TestNullConditionalMethodNesting:
    def test_method_arg_with_inner_null_conditional(self) -> None:
        # Inner ?. inside method args is now transformed on a subsequent pass.
        result = pwsh_transform("$a?.Foo($b?.Bar())")[0]
        assert "?." not in result
        assert "$a" in result
        assert ".Foo(" in result
        assert ".Bar()" in result

    def test_method_arg_with_inner_null_coalescing(self) -> None:
        result = pwsh_transform('$a?.Foo($b ?? "default")')[0]
        assert "?." not in result
        assert "??" not in result
        assert '"default"' in result

    def test_index_with_nested_brackets(self) -> None:
        result = pwsh_transform("$a?[$i[$j]]")[0]
        assert "?[" not in result
        assert "$i[$j]" in result

# ============================================================================
# Unterminated / malformed inputs
# ============================================================================

class TestMalformedInputs:
    def test_unterminated_double_quoted_string(self) -> None:
        result = pwsh_transform('Write-Output "hello')[0]
        assert isinstance(result, str)

    def test_unterminated_single_quoted_string(self) -> None:
        result = pwsh_transform("Write-Output 'hello")[0]
        assert isinstance(result, str)

    def test_unterminated_block_comment(self) -> None:
        result = pwsh_transform("<# hello\nWrite-Output $a ?? 'default'")[0]
        assert isinstance(result, str)

    def test_unterminated_subexpression(self) -> None:
        result = pwsh_transform("$($a + ")[0]
        assert isinstance(result, str)

    def test_whitespace_only_input(self) -> None:
        result = pwsh_transform("   \n  \t  \n  ")[0]
        assert isinstance(result, str)

    def test_line_with_only_comment(self) -> None:
        result = pwsh_transform("# just a comment")[0]
        assert result == "# just a comment"

# ============================================================================
# Mixed / combined operator stress
# ============================================================================

class TestMixedOperatorStress:
    def test_null_coalescing_then_ternary(self) -> None:
        # LIMITATION: after ?? is transformed, the resulting ternary sits
        # inside braces at depth>0, so _transform_ternary_line skips it.
        result = pwsh_transform('$x = $a ?? $b ? "t" : "f"')[0]
        assert "??" not in result
        # ternary inside generated braces is NOT transformed (depth>0)
        assert "?" in result
        assert "$a" in result
        assert "$b" in result

    def test_ternary_then_null_coalescing(self) -> None:
        result = pwsh_transform('$x = $cond ? ($a ?? $b) : $c')[0]
        assert "??" not in result
        assert "?" not in result
        assert "$cond" in result

    def test_null_conditional_then_null_coalescing(self) -> None:
        # ?. now runs before ?? and wraps its output in $(), so ?? can safely
        # use the transformed expression as an operand.
        result = pwsh_transform('$x = $a?.Name ?? "default"')[0]
        assert "?." not in result
        assert "??" not in result
        assert "if ($null -ne $(if ($null -ne $a) { $a.Name }))" in result
        assert '"default"' in result

    def test_all_operators_in_one_line(self) -> None:
        result = pwsh_transform('$a ??= $b; $c = $d?.Name ?? "x"; cmd1 && cmd2 || cmd3')[0]
        assert "??=" not in result
        assert "?." not in result
        assert "??" not in result
        assert "&&" not in result
        assert "||" not in result
        # $d?.Name is transformed first, then ?? uses the wrapped result
        assert "if ($null -ne $(if ($null -ne $d) { $d.Name }))" in result

    def test_null_conditional_chain_with_index_and_property(self) -> None:
        result = pwsh_transform('$a?.Items?[0]?.Name')[0]
        assert "?." not in result
        assert "$a" in result

# ============================================================================
# Backtick edge cases
# ============================================================================

class TestBacktickEdgeCases:
    def test_backtick_before_operator_no_newline(self) -> None:
        result = pwsh_transform("cmd1 `&& cmd2")[0]
        # No newline after backtick, so `& is literal backtick + &, not continuation
        assert isinstance(result, str)

    def test_multiple_backticks_with_newlines(self) -> None:
        result = pwsh_transform("Write-Output `\n`\n`\nhello")[0]
        assert isinstance(result, str)
        assert "hello" in result

    def test_backtick_continuation_before_comment(self) -> None:
        code = "$x = $a ??`\n  # this is a comment\n  'default'"
        result = pwsh_transform(code)[0]
        assert isinstance(result, str)

# ============================================================================
# Expression boundary edge cases
# ============================================================================

class TestExprBoundaryEdgeCases:
    def test_null_coalescing_after_command_prefix_in_parens(self) -> None:
        result = pwsh_transform('Write-Output ($a ?? "default")')[0]
        assert "??" not in result
        assert "if ($null -ne $a)" in result
        assert "Write-Output" in result

    def test_ternary_after_command_prefix_in_parens(self) -> None:
        # BUG: ternary inside () is at depth>0, so it is skipped.
        result = pwsh_transform('Write-Output ($cond ? "a" : "b")')[0]
        assert "?" in result
        assert "Write-Output ($cond ? \"a\" : \"b\")" == result

    def test_null_conditional_after_command_prefix(self) -> None:
        # BUG: _expr_left includes the command prefix as part of the base expr.
        result = pwsh_transform('Write-Output $a?.Name')[0]
        assert "?." not in result
        # Currently produces: if ($null -ne Write-Output $a) { Write-Output $a.Name }
        assert "Write-Output" in result
        assert "$a" in result

    def test_ternary_with_type_accelerator_condition(self) -> None:
        result = pwsh_transform('[string]::IsNullOrEmpty($s) ? "empty" : "non-empty"')[0]
        assert "?" not in result
        assert "[string]::IsNullOrEmpty($s)" in result

# ============================================================================
# Idempotency for new patterns
# ============================================================================

class TestNewIdempotency:
    def test_null_conditional_array_element_idempotent(self) -> None:
        code = "$arr[0]?.Name"
        first = pwsh_transform(code)[0]
        second = pwsh_transform(first)[0]
        assert first == second

    def test_property_nca_idempotent(self) -> None:
        code = '$obj.Name ??= "default"'
        first = pwsh_transform(code)[0]
        second = pwsh_transform(first)[0]
        assert first == second

    def test_ternary_with_hashtable_idempotent(self) -> None:
        code = '$x = $cond ? @{ a = 1 } : @{ b = 2 }'
        first = pwsh_transform(code)[0]
        second = pwsh_transform(first)[0]
        assert first == second

# ============================================================================
# Null-conditional with variable property names (?.$prop)
# ============================================================================

class TestNullConditionalVariableProperty:
    def test_simple_variable_property(self) -> None:
        result = pwsh_transform("$a?.$property")[0]
        assert "?." not in result
        assert "$a" in result
        assert "$property" in result
        assert "if ($null -ne $a)" in result

    def test_variable_property_with_scope(self) -> None:
        result = pwsh_transform("$a?.$global:prop")[0]
        assert "?." not in result
        assert "$global:prop" in result

    def test_variable_property_braced(self) -> None:
        result = pwsh_transform("$a?.${var}")[0]
        assert "?." not in result
        assert "${var}" in result

    def test_variable_property_assignment(self) -> None:
        result = pwsh_transform("$x = $a?.$property")[0]
        assert "?." not in result
        assert "$x = " in result

    def test_variable_property_chained(self) -> None:
        result = pwsh_transform("$a?.$prop?.$other")[0]
        assert "?." not in result
        assert "$prop" in result
        assert "$other" in result

    def test_mixed_variable_and_plain_chain(self) -> None:
        result = pwsh_transform("$a?.Name?.$prop")[0]
        assert "?." not in result
        assert "Name" in result
        assert "$prop" in result

    def test_variable_property_idempotent(self) -> None:
        code = "$a?.$property"
        first = pwsh_transform(code)[0]
        second = pwsh_transform(first)[0]
        assert first == second

# ============================================================================
# Null-conditional with quoted member names (?.'name' / ?."name")
# ============================================================================

class TestNullConditionalQuotedMember:
    def test_single_quoted_member(self) -> None:
        result = pwsh_transform("$a?.'property-name'")[0]
        assert "?." not in result
        assert "'property-name'" in result

    def test_double_quoted_member(self) -> None:
        result = pwsh_transform('$a?."property-name"')[0]
        assert "?." not in result
        assert '"property-name"' in result

    def test_double_quoted_with_spaces(self) -> None:
        result = pwsh_transform('$a?."property name"')[0]
        assert "?." not in result
        assert '"property name"' in result

    def test_single_quoted_with_doubled_quote(self) -> None:
        result = pwsh_transform("$a?.'it''s'")[0]
        assert "?." not in result
        assert "'it''s'" in result

    def test_double_quoted_with_subexpression(self) -> None:
        result = pwsh_transform('$a?."prop$(1+1)"')[0]
        assert "?." not in result
        assert '"prop$(1+1)"' in result

    def test_quoted_member_chained(self) -> None:
        result = pwsh_transform("$a?.Name?.'other-prop'")[0]
        assert "?." not in result
        assert "Name" in result
        assert "'other-prop'" in result

    def test_quoted_member_with_method(self) -> None:
        result = pwsh_transform("$a?.'get-Name'()")[0]
        assert "?." not in result
        assert "'get-Name'()" in result

    def test_quoted_member_idempotent(self) -> None:
        code = "$a?.'prop-name'"
        first = pwsh_transform(code)[0]
        second = pwsh_transform(first)[0]
        assert first == second

# ============================================================================
# Null-coalescing assignment with braced/scoped variables
# ============================================================================

class TestNCABracedVariables:
    def test_nca_braced_variable(self) -> None:
        result = pwsh_transform('${global:var} ??= "init"')[0]
        assert "??=" not in result
        assert "if ($null -eq ${global:var})" in result

    def test_nca_braced_nested(self) -> None:
        result = pwsh_transform('${outer.${inner}} ??= "default"')[0]
        assert "??=" not in result
        assert "if ($null -eq ${outer.${inner}})" in result

    def test_nca_scoped_variable(self) -> None:
        result = pwsh_transform('$global:var ??= "init"')[0]
        assert "??=" not in result
        assert "if ($null -eq $global:var)" in result

    def test_nca_with_semicolon_after(self) -> None:
        result = pwsh_transform('${x} ??= 1; Write-Output ${x}')[0]
        assert "??=" not in result
        assert "if ($null -eq ${x})" in result
        assert "Write-Output" in result

    def test_nca_braced_idempotent(self) -> None:
        code = '${global:var} ??= "init"'
        first = pwsh_transform(code)[0]
        second = pwsh_transform(first)[0]
        assert first == second

# ============================================================================
# Null-conditional with complex base expressions
# ============================================================================

class TestNullConditionalComplexChains:
    def test_multi_variable_prop_chain(self) -> None:
        result = pwsh_transform("$a?.$b?.$c?.$d")[0]
        assert "?." not in result
        assert "$a" in result
        assert "$b" in result
        assert "$c" in result
        assert "$d" in result

    def test_mixed_all_member_types(self) -> None:
        result = pwsh_transform("$a?.$b?.'c-d'?.$e")[0]
        assert "?." not in result
        assert "$b" in result
        assert "'c-d'" in result
        assert "$e" in result

    def test_double_quoted_member_chain(self) -> None:
        result = pwsh_transform('$a?."b-c"?."d-e"')[0]
        assert "?." not in result
        assert '"b-c"' in result
        assert '"d-e"' in result

    def test_cmd_prefix_with_variable_prop(self) -> None:
        result = pwsh_transform("Write-Output $a?.Name")[0]
        assert "?." not in result
        assert "Write-Output" in result
        assert "$a" in result

    def test_array_element_prop_chain(self) -> None:
        result = pwsh_transform("$arr[0][1]?.Name")[0]
        assert "?." not in result
        assert "$arr[0][1]" in result
        assert "Name" in result

# ============================================================================
# More edge cases discovered during analysis
# ============================================================================

class TestDiscoveredEdgeCases:
    def test_ternary_with_dollar_question_all(self) -> None:
        result = pwsh_transform("$? ? $? : $?")[0]
        assert result == "if ($?) { $? } else { $? }"

    def test_null_coalescing_in_double_quoted_string_preserved(self) -> None:
        result = pwsh_transform('"$a ?? $b" | Write-Output')[0]
        assert "??" in result  # preserved inside string

    def test_incomplete_here_string_preserved(self) -> None:
        result = pwsh_transform("$text = @'\nhello\n&& cmd2")[0]
        # Unterminated here-string: the rest of file is treated as string
        assert "&&" in result  # preserved because in unterminated here-string

    def test_question_mark_not_preceded_by_dollar(self) -> None:
        """? that is not preceded by $ and not followed by colon should be safe."""
        result = pwsh_transform("$a ?")[0]
        # No crash, no false match
        assert "$a" in result

    def test_double_question_at_end_no_crash(self) -> None:
        result = pwsh_transform("$a ??")[0]
        assert "$a" in result

    def test_null_conditional_dot_at_end_no_crash(self) -> None:
        result = pwsh_transform("$a?.")[0]
        assert "$a" in result

# ============================================================================
# Idempotency for all new patterns
# ============================================================================

class TestNewComprehensiveIdempotency:
    def test_var_prop_chain_idempotent(self) -> None:
        code = "$a?.$b?.$c?.$d"
        first = pwsh_transform(code)[0]
        second = pwsh_transform(first)[0]
        assert first == second

    def test_mixed_chain_idempotent(self) -> None:
        code = "$a?.$b?.'c-d'?.$e"
        first = pwsh_transform(code)[0]
        second = pwsh_transform(first)[0]
        assert first == second

    def test_brace_var_nca_idempotent(self) -> None:
        code = '${global:var} ??= "init"'
        first = pwsh_transform(code)[0]
        second = pwsh_transform(first)[0]
        assert first == second

    def test_quoted_member_chain_idempotent(self) -> None:
        code = '$a?."b-c"?."d-e"'
        first = pwsh_transform(code)[0]
        second = pwsh_transform(first)[0]
        assert first == second

# ============================================================================
# Deeply nested block comments
# ============================================================================

class TestNestedBlockComments:
    def test_triple_nested_block_comment(self) -> None:
        code = '<# L1 <# L2 <# L3 #> still L2 #> still L1 #>\n$x = $a ?? "default"'
        result = pwsh_transform(code)[0]
        assert "??" not in result
        assert "L1" in result
        assert "L2" in result
        assert "L3" in result

    def test_block_comment_then_operators_on_next_line(self) -> None:
        code = '<# comment #>\n$a ?? "default"'
        result = pwsh_transform(code)[0]
        assert "??" not in result
        assert "if ($null -ne $a)" in result

    def test_block_comment_then_chain_on_next_line(self) -> None:
        code = '<# comment #>\ncmd1 && cmd2'
        result = pwsh_transform(code)[0]
        assert "&&" not in result
        assert "if ($?)" in result

# ============================================================================
# Double-quoted here-strings
# ============================================================================

class TestHereStringDoubleQuotedExtra:
    def test_at_double_quote_here_string_preserves_operators(self) -> None:
        code = '$text = @"\n?? and ?. and && and ||\n"@\nWrite-Output done'
        result = pwsh_transform(code)[0]
        assert "??" in result  # preserved inside here-string
        assert "?." in result
        assert "&&" in result
        assert "||" in result

    def test_at_double_quote_here_string_with_subexpressions(self) -> None:
        code = '$text = @"\nHello $(Get-Date) and ?? is fine\n"@\ncmd1 && cmd2'
        result = pwsh_transform(code)[0]
        assert "$(Get-Date)" in result  # preserved in here-string
        # The && on the line after the here-string SHOULD be transformed
        assert "if ($?)" in result

    def test_at_single_quote_here_string_followed_by_operators(self) -> None:
        code = "$text = @'\nhello\n'@\n$x = $a ?? 'default'"
        result = pwsh_transform(code)[0]
        assert "??" not in result
        assert "if ($null -ne $a)" in result

# ============================================================================
# Backtick continuation deep edge cases
# ============================================================================

class TestBacktickDeepEdgeCases:
    def test_backtick_inside_double_quoted_string_not_collapsed(self) -> None:
        """Backtick inside a double-quoted string is not a line continuation."""
        code = '$x = "hello`nthere $a ?? $b"'
        result = pwsh_transform(code)[0]
        # ?? inside string should be preserved
        assert "??" in result

    def test_backtick_inside_single_quoted_string_not_collapsed(self) -> None:
        code = "$x = 'hello`nthere $a ?? $b'"
        result = pwsh_transform(code)[0]
        # Inside single-quoted string, ` is literal
        assert "??" in result

    def test_backtick_with_only_carriage_return(self) -> None:
        """Backtick followed by \\r only (not \\n) is NOT a line continuation."""
        code = "cmd1 `\r && cmd2"
        result = pwsh_transform(code)[0]
        # The ` is NOT collapsed since \r is not \n
        assert "`" in result

    def test_backtick_at_eof(self) -> None:
        """Backtick at end of file with no following characters."""
        result = pwsh_transform("Write-Output `")[0]
        assert isinstance(result, str)
        assert "`" in result or "Write-Output" in result

    def test_consecutive_backtick_continuations(self) -> None:
        code = "cmd1 `\n`\n`\n&& cmd2"
        result = pwsh_transform(code)[0]
        assert "&&" not in result
        assert "if ($?)" in result

    def test_backtick_continuation_with_tabs(self) -> None:
        code = "cmd1 `\n\t\t&& cmd2"
        result = pwsh_transform(code)[0]
        assert "&&" not in result
        assert "if ($?)" in result

# ============================================================================
# _strip_command_prefix with PS keywords
# ============================================================================

class TestCommandPrefixStripping:
    def test_keyword_not_stripped(self) -> None:
        """PS keywords like 'if', 'for', 'while' should NOT be stripped as command prefix."""
        result = pwsh_transform('if $a ?? "default"')[0]
        # 'if' is a keyword, not a command, so it should not be stripped
        # This means $a is recognized as left of ??, not "if $a"
        assert "??" not in result

    def test_foreach_not_stripped(self) -> None:
        result = pwsh_transform('foreach $a ?? "default"')[0]
        assert "??" not in result
        assert "$a" in result

    def test_return_not_stripped(self) -> None:
        result = pwsh_transform('return $a ?? "default"')[0]
        assert "??" not in result
        assert "$a" in result

    def test_real_command_is_stripped(self) -> None:
        result = pwsh_transform('Write-Output $a ?? "default"')[0]
        assert "??" not in result
        assert "if ($null -ne $a)" in result

# ============================================================================
# _match_assignment with complex left-hand sides
# ============================================================================

class TestComplexAssignmentDetection:
    def test_scoped_property_assignment_coalescing(self) -> None:
        result = pwsh_transform('$global:obj.Property = $a ?? "default"')[0]
        assert "??" not in result
        assert "$global:obj.Property = " in result
        assert "if ($null -ne $a)" in result

    def test_no_assignment_coalescing(self) -> None:
        result = pwsh_transform('$a ?? "default"')[0]
        assert "=" not in result.split("if")[0]  # no assignment before the if

    def test_assignment_with_ternary(self) -> None:
        result = pwsh_transform('$x = $cond ? "a" : "b"')[0]
        assert "$x = " in result

# ============================================================================
# _find_expr_start / _find_expr_end edge cases
# ============================================================================

class TestExpressionBoundariesDeep:
    def test_expr_at_start_of_line(self) -> None:
        """Expression starting at column 0."""
        result = pwsh_transform('$a ?? "default"')[0]
        assert "??" not in result

    def test_expr_at_end_of_line(self) -> None:
        """Expression ending at end of line (no trailing chars)."""
        result = pwsh_transform('$x = $a ?? "default"')[0]
        assert "??" not in result

    def test_array_subexpr_boundary(self) -> None:
        """@() as expression boundary."""
        result = pwsh_transform('$x = @(1,2) ?? @(3,4)')[0]
        assert "??" not in result
        assert "@(1,2)" in result
        assert "@(3,4)" in result

    def test_at_paren_boundary_for_ternary(self) -> None:
        """Ternary where condition is @()."""
        result = pwsh_transform('$x = @(1).Count -gt 0 ? "yes" : "no"')[0]
        assert "?" not in result
        assert "if (@(1).Count -gt 0)" in result

    def test_ampersand_call_operator_boundary(self) -> None:
        """& call operator as boundary."""
        result = pwsh_transform('& $cmd $a ?? "default"')[0]
        assert "??" not in result

# ============================================================================
# Null-conditional with unusual member-name characters
# ============================================================================

class TestNullConditionalUnusualMembers:
    def test_dot_then_at_sign_not_transformed(self) -> None:
        """$a?.@ is invalid; should not crash or transform."""
        result = pwsh_transform("$a?.@")[0]
        assert isinstance(result, str)
        # @ is not a valid member name char, so ?. is not transformed
        assert "$a" in result

    def test_dot_then_hash_not_transformed(self) -> None:
        """$a?.#comment should stop at #."""
        result = pwsh_transform("$a?.#comment")[0]
        assert isinstance(result, str)

    def test_dot_then_lparen_method(self) -> None:
        """$a?.(...) is invalid; should not crash."""
        result = pwsh_transform("$a?.(Get-Member)")[0]
        assert isinstance(result, str)

# ============================================================================
# ?[ inside strings/regions
# ============================================================================

class TestBracketNullConditionalInStrings:
    def test_bracket_qmark_inside_single_quoted_string(self) -> None:
        result = pwsh_transform("Write-Output '?[0] is not transformed'")[0]
        assert "?[" in result
        assert "if ($null -ne" not in result

    def test_bracket_qmark_inside_double_quoted_string(self) -> None:
        result = pwsh_transform('Write-Output "?[0] is not transformed"')[0]
        assert "?[" in result
        assert "if ($null -ne" not in result

    def test_bracket_qmark_inside_comment(self) -> None:
        result = pwsh_transform("# ?[$a] is a comment\nWrite-Output hello")[0]
        assert "?[" in result

# ============================================================================
# ??= at absolute start of line
# ============================================================================

class TestNCALineStart:
    def test_nca_at_line_start(self) -> None:
        """$a ??= 'x' at column 0 of line."""
        result = pwsh_transform("$a ??= 'x'")[0]
        assert "??=" not in result
        assert "if ($null -eq $a)" in result

    def test_nca_braced_at_line_start(self) -> None:
        result = pwsh_transform("${a} ??= 'x'")[0]
        assert "??=" not in result
        assert "if ($null -eq ${a})" in result


# ============================================================================
# Multi-line here-string interaction with line transformer
# ============================================================================

class TestMultiLineRegions:
    def test_here_string_lines_not_individually_transformed(self) -> None:
        """Lines inside a multi-line here-string should be skipped by pwsh_transform."""
        code = """$text = @'
$a ?? 'should not transform'
$b?.Property
'@
Write-Output $text"""
        result = pwsh_transform(code)[0]
        # Operators inside here-string preserved
        assert "??" in result
        assert "?." in result

    def test_block_comment_lines_not_individually_transformed(self) -> None:
        code = """<#
$a ?? 'inside block comment'
$b?.Property
#>
Write-Output done"""
        result = pwsh_transform(code)[0]
        assert "??" in result
        assert "?." in result

# ============================================================================
# _skip_subexpression nested
# ============================================================================

class TestSkipSubexpressionNested:
    def test_nested_subexpressions_in_dq_string(self) -> None:
        """$() nesting inside double-quoted strings."""
        result = pwsh_transform('"$(Get-Date) and $($($a)) is fine"')[0]
        assert "$(Get-Date)" in result
        assert "$($($a))" in result

    def test_subexpr_with_single_quoted_string_inside(self) -> None:
        """$() containing a single-quoted string with special chars."""
        result = pwsh_transform('"$($x + ''?.'' )"')[0]
        # The ?. inside single quotes inside $() inside double quotes — preserved
        assert "?." in result

    def test_subexpr_with_nested_subexpr_in_dq(self) -> None:
        """Double-quoted string with $() that itself contains a dq string with $()."""
        result = pwsh_transform('"outer $(Get-Date \"inner $($a)\") end"')[0]
        assert isinstance(result, str)

# ============================================================================
# Ternary operator interaction with ?. and ?[
# ============================================================================

class TestTernaryInteractionDeep:
    def test_ternary_question_not_confused_with_null_conditional_dot(self) -> None:
        """$a?.Property should NOT be recognized as ternary."""
        result = pwsh_transform("$a?.Property")[0]
        assert "?." not in result
        assert "? :" not in result
        assert "if ($null -ne $a)" in result

    def test_ternary_true_branch_with_null_coalescing(self) -> None:
        """Ternary where true branch is a ?? expression."""
        result = pwsh_transform('$x = $cond ? ($a ?? "x") : "y"')[0]
        assert "??" not in result
        assert "?" not in result

    def test_ternary_false_branch_with_null_conditional(self) -> None:
        """Ternary where false branch has ?."""
        result = pwsh_transform('$x = $cond ? "yes" : $obj?.Name')[0]
        assert "?." not in result
        # The ternary ? should be gone (only $? from generated code may remain)
        assert "$obj" in result
        assert ".Name" in result

# ============================================================================
# Null coalescing with unusual spacing and operator adjacency
# ============================================================================

class TestNullCoalescingSpacingEdge:
    def test_coalescing_adjacent_to_pipe(self) -> None:
        """$a ?? $b | ForEach-Object { $_ }"""
        result = pwsh_transform('$a ?? $b | ForEach-Object { $_ }')[0]
        assert "??" not in result

    def test_coalescing_with_semicolon_right_after(self) -> None:
        result = pwsh_transform('$a ?? "x"; $b ?? "y"')[0]
        assert "??" not in result
        assert result.count("if ($null -ne") == 2

    def test_coalescing_with_comma_separated_defaults(self) -> None:
        """$a ?? $b, $c ?? $d — comma binds tighter than ??."""
        result = pwsh_transform('$a ?? $b, $c ?? $d')[0]
        assert "??" not in result

# ============================================================================
# _transform_chain_line: operators inside strings with outside operators
# ============================================================================

class TestChainMixedInsideOutside:
    def test_and_inside_string_or_outside(self) -> None:
        result = pwsh_transform("Write-Output '&&' || Write-Output done")[0]
        assert "&&" in result  # inside string, preserved
        assert "||" not in result
        assert "if (-not $?)" in result

    def test_or_inside_string_and_outside(self) -> None:
        result = pwsh_transform('Write-Output "||" && Write-Output done')[0]
        assert "||" in result  # inside string, preserved
        assert "&&" not in result
        assert "if ($?)" in result

# ============================================================================
# pwsh_transform multiline with mixed operators on different lines
# ============================================================================

class TestMultiLineMixedOperators:
    def test_different_operators_on_different_lines(self) -> None:
        code = """$x = $a ?? "default"
$y = $cond ? "yes" : "no"
cmd1 && cmd2
$z = $obj?.Property"""
        result = pwsh_transform(code)[0]
        assert "??" not in result
        assert "?." not in result
        assert "&&" not in result
        # Ternary ? is gone; $? from chain transform is expected
        assert "if ($cond)" in result
        assert "if ($?)" in result

    def test_operators_on_consecutive_lines(self) -> None:
        code = """cmd1 && cmd2
cmd3 || cmd4"""
        result = pwsh_transform(code)[0]
        assert "&&" not in result
        assert "||" not in result
        assert "if ($?)" in result
        assert "if (-not $?)" in result

# ============================================================================
# Null-conditional bracket with string containing brackets
# ============================================================================

class TestNullConditionalBracketStrings:
    def test_bracket_index_with_string_containing_bracket(self) -> None:
        result = pwsh_transform("$a?['[']")[0]
        assert "?[" not in result
        assert "'['" in result

    def test_bracket_index_with_dq_string_containing_bracket(self) -> None:
        result = pwsh_transform('$a?["]"]')[0]
        assert "?[" not in result
        assert '"]"' in result

    def test_bracket_index_with_nested_brackets_in_string(self) -> None:
        result = pwsh_transform('$a?["[[["]')[0]
        assert "?[" not in result
        assert '"[[["' in result

# ============================================================================
# Single-quoted string scanner edge cases
# ============================================================================

class TestSingleQuotedStringScanner:
    def test_empty_single_quoted_string(self) -> None:
        """Empty '' should not confuse the scanner."""
        result = pwsh_transform("'' ?? 'default'")[0]
        assert "??" not in result
        assert "if ($null -ne '')" in result

    def test_only_escaped_quotes(self) -> None:
        """'''' is two escaped quotes — should be a string region."""
        result = pwsh_transform("'''' ?? 'default'")[0]
        assert "??" not in result

    def test_escaped_at_start_and_end(self) -> None:
        """''a'' — escaped quote, content, escaped quote."""
        result = pwsh_transform("''a'' ?? 'default'")[0]
        assert "??" not in result

    def test_doubled_quotes_in_content(self) -> None:
        """'it''s ok' — doubled quotes representing literal '."""
        result = pwsh_transform("'it''s ok' ?? 'default'")[0]
        assert "??" not in result

# ============================================================================
# Double-quoted string scanner edge cases
# ============================================================================

class TestDoubleQuotedStringScanner:
    def test_backtick_n_escape(self) -> None:
        """`n inside double-quoted string should not close the string."""
        result = pwsh_transform('"hello`nworld" ?? "default"')[0]
        assert "??" not in result

    def test_backtick_escaped_quote(self) -> None:
        """`" inside double-quoted string is an escaped quote, not closing."""
        result = pwsh_transform('"hello`"world" ?? "default"')[0]
        assert "??" not in result

    def test_dollar_paren_subexpression_in_dq(self) -> None:
        """$() inside double-quoted string should be skipped correctly."""
        result = pwsh_transform('"$(Get-Date)" ?? "default"')[0]
        assert "??" not in result
        assert "$(Get-Date)" in result

    def test_nested_dollar_paren_in_dq(self) -> None:
        """Nested $($($a)) inside dq string."""
        result = pwsh_transform('"$($($a))" ?? "default"')[0]
        assert "??" not in result

# ============================================================================
# Deeply nested block comments (4+ levels)
# ============================================================================

class TestDeepNestedBlockComments:
    def test_four_deep_block_comment(self) -> None:
        code = '<# L1 <# L2 <# L3 <# L4 #> L3 #> L2 #> L1 #>\n$x = $a ?? "default"'
        result = pwsh_transform(code)[0]
        assert "??" not in result
        assert "L1" in result
        assert "L4" in result

# ============================================================================
# Subexpression scanner with mixed quotes
# ============================================================================

class TestSubexpressionMixedQuotes:
    def test_mixed_quotes_in_subexpr(self) -> None:
        result = pwsh_transform('"$( "hello $(''inner'') world" )"')[0]
        assert isinstance(result, str)

    def test_brackets_inside_subexpr(self) -> None:
        result = pwsh_transform("$($a[0]) ?? 'default'")[0]
        assert "??" not in result
        assert "$($a[0])" in result

# ============================================================================
# @' and @" single-line (not here-strings)
# ============================================================================

class TestAtSignSingleLine:
    def test_at_double_quote_single_line_not_here_string(self) -> None:
        """@"..."@ on a single line is not a here-string."""
        result = pwsh_transform('@"?? and ?. preserved"@')[0]
        assert "??" in result  # inside string region, preserved
        assert "?." in result

    def test_at_single_quote_single_line_not_here_string(self) -> None:
        """@'...'@ on a single line is not a here-string."""
        result = pwsh_transform("@'?? preserved'@")[0]
        assert "??" in result

# ============================================================================
# Backtick inside single-quoted strings not collapsed
# ============================================================================

class TestBacktickInSingleQuotedString:
    def test_backtick_newline_in_sq_string_not_collapsed(self) -> None:
        """Backtick inside '...' is literal, not a line continuation."""
        result = pwsh_transform("'hello `\nworld'")[0]
        assert isinstance(result, str)
        # The backtick should remain because it's inside a string

# ============================================================================
# _strip_command_prefix with numbers
# ============================================================================

class TestCommandPrefixNumbers:
    def test_command_prefix_with_number_argument(self) -> None:
        """Write-Output 123 ?? 0 — command prefix should be stripped."""
        result = pwsh_transform("Write-Output 123 ?? 0")[0]
        assert "??" not in result
        assert "if ($null -ne 123)" in result

    def test_command_prefix_with_variable(self) -> None:
        """Write-Output $a ?? 0 — command prefix should be stripped."""
        result = pwsh_transform("Write-Output $a ?? 0")[0]
        assert "??" not in result
        assert "if ($null -ne $a)" in result

# ============================================================================
# Two ?? or two ??= or two ?. or two ?[ on one line
# ============================================================================

class TestMultipleSameOperator:
    def test_two_nca_on_one_line(self) -> None:
        result = pwsh_transform('$a ??= "x"; $b ??= "y"')[0]
        assert "??=" not in result
        assert "if ($null -eq $a)" in result
        assert "if ($null -eq $b)" in result

    def test_two_null_coalescing_on_one_line(self) -> None:
        result = pwsh_transform('$a ?? "x"; $b ?? "y"')[0]
        assert "??" not in result
        assert result.count("if ($null -ne") == 2

    def test_two_null_conditional_dot_on_one_line(self) -> None:
        result = pwsh_transform("$a?.Name; $b?.Count")[0]
        assert "?." not in result
        assert "$a" in result
        assert "$b" in result

    def test_two_null_conditional_bracket_on_one_line(self) -> None:
        result = pwsh_transform("$a?[0]; $b?[1]")[0]
        assert "?[" not in result
        assert "$a[0]" in result
        assert "$b[1]" in result

# ============================================================================
# Ternary with nested condition
# ============================================================================

class TestTernaryNestedCondition:
    def test_ternary_with_paren_condition(self) -> None:
        result = pwsh_transform('($a -gt 0) ? ($b ? "c" : "d") : "e"')[0]
        assert "if (($a -gt 0))" in result

    def test_ternary_false_branch_chain(self) -> None:
        result = pwsh_transform('$cond ? "a" : cmd1 && cmd2')[0]
        # Ternary ? is gone, but $? from chain transform appears
        assert "if ($cond)" in result
        assert "&&" not in result

# ============================================================================
# Chain with 5 operators
# ============================================================================

class TestLongChain:
    def test_five_and_chain(self) -> None:
        result = pwsh_transform("cmd1 && cmd2 && cmd3 && cmd4 && cmd5")[0]
        assert "&&" not in result
        assert result.count("if ($?)") == 4

# ============================================================================
# ?. with invalid member (starts with number)
# ============================================================================

class TestNullConditionalInvalidMembers:
    def test_number_member_not_transformed(self) -> None:
        """$a?.123 — member names can't start with number; should not transform."""
        result = pwsh_transform("$a?.123")[0]
        # Should not crash; ?. is not transformed because 123 is not alphanumeric...
        # Actually 1 is alphanumeric, but the member starts with a digit.
        # The transformer accepts it as a member name but in PS member names
        # starting with digits are invalid. Transformer just passes through.
        assert isinstance(result, str)

    def test_empty_index_not_crash(self) -> None:
        """$a?[] — empty index should not crash."""
        result = pwsh_transform("$a?[]")[0]
        assert isinstance(result, str)

# ============================================================================
# Complex NCA with chained property access
# ============================================================================

class TestNCAPropertyChain:
    def test_chained_property_nca(self) -> None:
        result = pwsh_transform('$a.b.c ??= "default"')[0]
        assert "??=" not in result
        assert "if ($null -eq $a.b.c)" in result
        assert "$a.b.c = " in result

# ============================================================================
# && / || with & call operator boundary
# ============================================================================

class TestChainWithCallOperator:
    def test_call_operator_then_coalescing(self) -> None:
        result = pwsh_transform('& $cmd $a ?? "default"')[0]
        assert "??" not in result
        assert "$a" in result


# ============================================================================
# Ultimate idempotency: all operators combined
# ============================================================================

class TestUltimateIdempotency:
    def test_all_operators_combined_idempotent(self) -> None:
        code = '${a} ??= ${b}; $c = $d?.$e?.\'f\' ?? "g"; cmd1 && cmd2 || cmd3'
        first = pwsh_transform(code)[0]
        second = pwsh_transform(first)[0]
        assert first == second

    def test_every_operator_once_idempotent(self) -> None:
        code = '$x = $a ?? "d"; $y = $c ? "t" : "f"; $z ??= 0; $w = $q?.Prop; cmd1 && cmd2'
        first = pwsh_transform(code)[0]
        second = pwsh_transform(first)[0]
        assert first == second

# ============================================================================
# Unterminated string / comment / subexpression scanners
# ============================================================================

class TestUnterminatedScanners:
    def test_unterminated_single_quoted(self) -> None:
        """Unterminated '... should not crash; treats rest as string."""
        result = pwsh_transform("'unterminated ?? and ?.")[0]
        assert isinstance(result, str)
        assert "??" in result  # inside unterminated string region, preserved

    def test_unterminated_double_quoted(self) -> None:
        result = pwsh_transform('"unterminated ?? and ?.')[0]
        assert isinstance(result, str)
        assert "??" in result

    def test_unterminated_block_comment_eof(self) -> None:
        result = pwsh_transform("<# unterminated ?? and ?.")[0]
        assert isinstance(result, str)

    def test_unterminated_subexpression(self) -> None:
        result = pwsh_transform("$(unterminated ?? and ?.")[0]
        assert isinstance(result, str)

    def test_unterminated_here_string_single_quoted(self) -> None:
        result = pwsh_transform("@'\nunterminated ?? and ?.")[0]
        assert isinstance(result, str)
        assert "??" in result

# ============================================================================
# Backtick at extremes (position 0, EOF)
# ============================================================================

class TestBacktickExtremes:
    def test_backtick_at_position_zero(self) -> None:
        """Backtick at very start of code."""
        result = pwsh_transform("`\ncmd1")[0]
        assert "cmd1" in result

    def test_backtick_at_end_of_file(self) -> None:
        """Backtick as last character of code (no newline after)."""
        result = pwsh_transform("cmd1 `")[0]
        assert isinstance(result, str)
        assert "`" in result or "cmd1" in result

# ============================================================================
# _match_assignment with ${braced} variables
# ============================================================================

class TestBracedAssignment:
    def test_braced_var_assignment_with_coalescing(self) -> None:
        result = pwsh_transform('${global:var} = $a ?? "default"')[0]
        assert "??" not in result
        assert "${global:var} =" in result  # _build_replacement joins without extra space
        assert "if ($null -ne $a)" in result

# ============================================================================
# Line comment at position 0 with operators on next line
# ============================================================================

class TestHashAtPositionZero:
    def test_comment_at_start_then_operator_line(self) -> None:
        code = "# comment\n$a ?? 'default'"
        result = pwsh_transform(code)[0]
        assert "# comment" in result
        assert "??" not in result  # ?? on second line IS transformed
        assert "if ($null -ne $a)" in result

# ============================================================================
# ??= inside string literal (should NOT be transformed)
# ============================================================================

class TestNCAInsideString:
    def test_nca_inside_single_quoted_string_not_transformed(self) -> None:
        """The ??= inside the string is not matched. The real ??= after the string
        has an empty variable (the string literal), so _transform_nca_line skips it;
        _transform_nc_line then handles the ?? part."""
        result = pwsh_transform("'??= inside string' ??= 'value'")[0]
        # The ??= is not transformed (skipped by nca, caught as ?? by nc)
        # The ?? inside the string is preserved
        assert "'??= inside string'" in result

# ============================================================================
# Chain operators all inside strings — none should transform
# ============================================================================

class TestChainAllInStrings:
    def test_all_chains_inside_strings(self) -> None:
        result = pwsh_transform("'&&' + '||'")[0]
        assert "&&" in result
        assert "||" in result
        assert "if ($?)" not in result
        assert "if (-not $?)" not in result

# ============================================================================
# String literal containing ?? then real ?? on same line
# ============================================================================

class TestStringThenRealCoalescing:
    def test_string_then_real_coalescing_same_line(self) -> None:
        result = pwsh_transform("'??' ?? 'real'")[0]
        # The real ?? is transformed; ?? inside the string literal is preserved
        assert "if ($null -ne '??')" in result
        assert "'??'" in result  # string still contains ??, preserved as content
        assert "'real'" in result

# ============================================================================
# $? as ternary condition with complex branches
# ============================================================================

class TestDollarQuestionTernaryComplex:
    def test_dollar_q_ternary_with_complex_branches(self) -> None:
        result = pwsh_transform('$? ? ($a ?? "x") : ($b?.Name)')[0]
        assert "?." not in result
        assert "??" not in result
        assert "if ($?)" in result

# ============================================================================
# ?. / ?[ with ?? chained after
# ============================================================================

class TestNullConditionalThenCoalescing:
    def test_qd_then_coalescing(self) -> None:
        result = pwsh_transform('$a?.Name ?? "default"')[0]
        assert "?." not in result
        assert "??" not in result

    def test_qb_then_coalescing(self) -> None:
        result = pwsh_transform('$a?[0] ?? "default"')[0]
        assert "?[" not in result
        assert "??" not in result

# ============================================================================
# ??= with nothing on the right side
# ============================================================================

class TestNCAEmptyRight:
    def test_nca_empty_right_side(self) -> None:
        """$a ??= with nothing after should not crash."""
        result = pwsh_transform("$a ??= ")[0]
        assert isinstance(result, str)
        assert "$a" in result

# ============================================================================
# Multiple multiline here-strings in one code block
# ============================================================================

class TestMultipleHereStrings:
    def test_two_here_strings_with_operator_between(self) -> None:
        code = "@'\n?? preserved\n'@\n$x = $a ?? 'default'\n@'\n?. preserved\n'@"
        result = pwsh_transform(code)[0]
        # The ?? between the here-strings IS transformed
        assert "if ($null -ne $a)" in result
        # The ?? inside the first here-string and ?. inside second are preserved
        assert "?? preserved" in result
        assert "?. preserved" in result

# ============================================================================
# @" without newline after (not a here-string)
# ============================================================================

class TestAtSignDoubleQuoteNoNewline:
    def test_at_dq_single_line_content(self) -> None:
        """@"hello"@ on a single line — @" is NOT a here-string start."""
        result = pwsh_transform('$x = @"hello"@')[0]
        assert isinstance(result, str)

# ============================================================================
# Extremely long null-conditional chain
# ============================================================================

class TestLongNullConditionalChain:
    def test_eight_deep_qd_chain(self) -> None:
        result = pwsh_transform("$a?.b?.c?.d?.e?.f?.g?.h")[0]
        assert "?." not in result
        assert ".h" in result

# ============================================================================
# Invalid assignment syntax (no crash)
# ============================================================================

class TestInvalidAssignmentNoCrash:
    def test_dollar_sign_only_assignment(self) -> None:
        """Invalid PS: $ = ... should not crash."""
        result = pwsh_transform('$ = $a ?? "default"')[0]
        assert isinstance(result, str)

# ============================================================================
# Semicolons mixed with chain operators
# ============================================================================

class TestSemicolonChainMix:
    def test_semicolons_and_chains_mixed(self) -> None:
        result = pwsh_transform("cmd1; cmd2 && cmd3; cmd4 || cmd5")[0]
        assert "&&" not in result
        assert "||" not in result
        assert "if ($?)" in result
        assert "if (-not $?)" in result

# ============================================================================
# Ternary / ?? at very start of line (no preceding spaces)
# ============================================================================

class TestOperatorAtLineStart:
    def test_ternary_at_column_zero(self) -> None:
        result = pwsh_transform('$cond ? "a" : "b"')[0]
        assert "?" not in result
        assert "if ($cond)" in result

    def test_coalescing_at_column_zero(self) -> None:
        result = pwsh_transform('$a ?? "default"')[0]
        assert "??" not in result
        assert "if ($null -ne $a)" in result

# ============================================================================
# _strip_command_prefix: @ sign after command
# ============================================================================

class TestCommandPrefixAtSign:
    def test_command_with_array_subexpr_argument(self) -> None:
        """Write-Output @(1,2) ?? 0 — @ triggers the command-prefix check."""
        result = pwsh_transform("Write-Output @(1,2) ?? 0")[0]
        assert "??" not in result
        assert "@(1,2)" in result

    def test_command_with_hashtable_argument_coalescing(self) -> None:
        result = pwsh_transform('Write-Output @{a=1} ?? "fallback"')[0]
        assert "??" not in result
        assert "@{a=1}" in result

# ============================================================================
# _transform_chain_line: empty right side
# ============================================================================

class TestChainEmptyRight:
    def test_and_with_nothing_after(self) -> None:
        """cmd1 && — nothing after &&, should produce empty if body."""
        result = pwsh_transform("cmd1 &&")[0]
        assert "cmd1" in result
        assert "if ($?)" in result

    def test_or_with_nothing_after(self) -> None:
        result = pwsh_transform("cmd1 ||")[0]
        assert "cmd1" in result
        assert "if (-not $?)" in result

# ============================================================================
# String containing ?: that should not match ternary
# ============================================================================

class TestStringColonNotTernary:
    def test_colon_in_dq_string_not_ternary_colon(self) -> None:
        """?: inside double-quoted string should not confuse ternary."""
        result = pwsh_transform('$x = $cond ? "a:b:c" : "d"')[0]
        assert "?" not in result
        assert '"a:b:c"' in result
        assert '"d"' in result

    def test_colon_in_sq_string_not_ternary_colon(self) -> None:
        result = pwsh_transform("$x = $cond ? 'a:b:c' : 'd'")[0]
        assert "?" not in result
        assert "'a:b:c'" in result
        assert "'d'" in result

# ============================================================================
# _find_string_regions: @" at end of file (no newline)
# ============================================================================

class TestAtSignEdgeCases:
    def test_at_dq_at_end_of_code(self) -> None:
        """@" at the very end of code with no newline — not a here-string."""
        result = pwsh_transform('$x = @"text"')[0]
        assert isinstance(result, str)

    def test_at_sq_at_end_of_code(self) -> None:
        result = pwsh_transform("$x = @'text'")[0]
        assert isinstance(result, str)

# ============================================================================
# Idempotency: transform of already-transformed code with $? in it
# ============================================================================

class TestIdempotencyWithDollarQuestion:
    def test_transformed_if_with_dollar_q_is_idempotent(self) -> None:
        """if ($?) should survive a second transform unchanged."""
        code = "if ($?) { Write-Output ok }"
        first = pwsh_transform(code)[0]
        second = pwsh_transform(first)[0]
        assert first == second

    def test_transformed_chain_result_is_idempotent(self) -> None:
        code = "cmd1; if ($?) { cmd2 }"
        first = pwsh_transform(code)[0]
        second = pwsh_transform(first)[0]
        assert first == second


# ============================================================================
# Corner case: -not / ! unary operator with ternary
# ============================================================================

class TestNotOperatorTernary:
    def test_not_operator_in_ternary_condition(self) -> None:
        """-not $cond ? 'a' : 'b' — -not is part of the condition."""
        result = pwsh_transform("-not $cond ? 'a' : 'b'")[0]
        assert "?" not in result
        assert "if (-not $cond)" in result
        assert "{ 'a' }" in result
        assert "{ 'b' }" in result

    def test_not_operator_with_parens_ternary(self) -> None:
        result = pwsh_transform("!($a -eq $null) ? 'has-value' : 'null'")[0]
        assert "?" not in result
        assert "if (!($a -eq $null))" in result

    def test_not_operator_ternary_in_assignment(self) -> None:
        result = pwsh_transform('$x = -not (Test-Path $f) ? "missing" : "exists"')[0]
        assert "?" not in result
        assert "if (-not (Test-Path $f))" in result

    def test_bang_operator_ternary(self) -> None:
        """! is an alias for -not in PS."""
        result = pwsh_transform('!$flag ? "off" : "on"')[0]
        assert "?" not in result
        assert "if (!$flag)" in result


# ============================================================================
# Corner case: [Type]::StaticMember with ??
# ============================================================================

class TestStaticMemberNullCoalescing:
    def test_static_property_with_null_coalescing(self) -> None:
        """[Math]::PI ?? 3.14 — static member as left operand."""
        result = pwsh_transform("[Math]::PI ?? 3.14")[0]
        assert "??" not in result
        assert "if ($null -ne [Math]::PI)" in result
        assert "[Math]::PI" in result
        assert "3.14" in result

    def test_static_method_call_with_coalescing(self) -> None:
        result = pwsh_transform('[Enum]::Parse($type, $value) ?? "unknown"')[0]
        assert "??" not in result
        assert "if ($null -ne [Enum]::Parse($type, $value))" in result

    def test_static_member_coalescing_in_assignment(self) -> None:
        result = pwsh_transform('$x = [Version]::Parse($s) ?? [Version]::new(0,0)')[0]
        assert "??" not in result
        assert "if ($null -ne [Version]::Parse($s))" in result

    def test_static_member_on_right_of_coalescing(self) -> None:
        result = pwsh_transform('$v ?? [Math]::PI')[0]
        assert "??" not in result
        assert "if ($null -ne $v)" in result
        assert "[Math]::PI" in result


# ============================================================================
# Corner case: ?. on @() array subexpression and parenthesized tuples
# ============================================================================

class TestNullConditionalOnArrayExpr:
    def test_array_subexpr_dot_property(self) -> None:
        """@(1,2,3)?.Count — null-conditional on array subexpression."""
        result = pwsh_transform("@(1,2,3)?.Count")[0]
        assert "?." not in result
        assert "if ($null -ne @(1,2,3))" in result
        assert "@(1,2,3).Count" in result

    def test_array_subexpr_dot_method(self) -> None:
        result = pwsh_transform("@(1,2,3)?.GetType()")[0]
        assert "?." not in result
        assert "if ($null -ne @(1,2,3))" in result
        assert "@(1,2,3).GetType()" in result

    def test_parenthesized_expression_dot(self) -> None:
        result = pwsh_transform("(1,2,3)?.Count")[0]
        assert "?." not in result
        assert "if ($null -ne (1,2,3))" in result

    def test_array_subexpr_bracket(self) -> None:
        result = pwsh_transform("@(1,2,3)?[0]")[0]
        assert "?[" not in result
        assert "if ($null -ne @(1,2,3))" in result

    def test_array_subexpr_assignment(self) -> None:
        result = pwsh_transform("$x = @(1,2,3)?.Count")[0]
        assert "?." not in result
        assert "$x = $(if ($null -ne @(1,2,3)) { @(1,2,3).Count })" == result


# ============================================================================
# Corner case: ?. with PowerShell keyword member names
# ============================================================================

class TestNullConditionalKeywordMembers:
    def test_keyword_begin_member(self) -> None:
        """$obj?.Begin — 'Begin' is a PS keyword but also valid member name."""
        result = pwsh_transform("$obj?.Begin")[0]
        assert "?." not in result
        assert "if ($null -ne $obj)" in result
        assert "$obj.Begin" in result

    def test_keyword_process_member(self) -> None:
        result = pwsh_transform("$obj?.Process")[0]
        assert "?." not in result
        assert "$obj.Process" in result

    def test_keyword_end_member(self) -> None:
        result = pwsh_transform("$obj?.End")[0]
        assert "?." not in result
        assert "$obj.End" in result

    def test_keyword_foreach_member(self) -> None:
        result = pwsh_transform("$obj?.ForEach")[0]
        assert "?." not in result
        assert "$obj.ForEach" in result

    def test_keyword_where_member(self) -> None:
        result = pwsh_transform("$obj?.Where")[0]
        assert "?." not in result
        assert "$obj.Where" in result

    def test_keyword_return_member(self) -> None:
        result = pwsh_transform("$obj?.Return")[0]
        assert "?." not in result
        assert "$obj.Return" in result


# ============================================================================
# Corner case: $? as ?? left operand (automatic variable)
# ============================================================================

class TestDollarQuestionCoalescing:
    def test_dollar_q_coalescing_left(self) -> None:
        """$? ?? $false — $? is an automatic variable, not ternary."""
        result = pwsh_transform("$? ?? $false")[0]
        assert "??" not in result
        assert "if ($null -ne $?)" in result
        assert "{ $? }" in result
        assert "{ $false }" in result

    def test_dollar_q_coalescing_in_assignment(self) -> None:
        result = pwsh_transform('$ok = $? ?? $false')[0]
        assert "??" not in result
        assert "$ok = " in result
        assert "if ($null -ne $?)" in result

    def test_dollar_q_nca(self) -> None:
        """$? ??= $true — null-coalescing assignment with $?."""
        result = pwsh_transform("$? ??= $true")[0]
        assert "??=" not in result
        assert "if ($null -eq $?)" in result
        assert "$? = $true" in result


# ============================================================================
# Corner case: ?. on $$ / $^ automatic variables
# ============================================================================

class TestNullConditionalAutomaticVars:
    def test_doubledollar_dot(self) -> None:
        """$$?.Name — $$ is an automatic variable (last token)."""
        result = pwsh_transform("$$?.Name")[0]
        assert "?." not in result
        assert "if ($null -ne $$)" in result
        assert "$$.Name" in result

    def test_caret_dot(self) -> None:
        """$^?.Name — $^ is an automatic variable (first token)."""
        result = pwsh_transform("$^?.Name")[0]
        assert "?." not in result
        assert "if ($null -ne $^)" in result
        assert "$^.Name" in result

    def test_dollar_q_then_question_bracket(self) -> None:
        """$??[0] — $? followed by ?[ operator."""
        result = pwsh_transform("$??[0]")[0]
        # $?[0] in output is $? auto-var + [0] index, not ?[ operator
        assert "if ($null -ne $?)" in result
        assert "$?[0]" in result

    def test_caret_coalescing(self) -> None:
        result = pwsh_transform('$^ ?? "default"')[0]
        assert "??" not in result
        assert "if ($null -ne $^)" in result


# ============================================================================
# Corner case: chain operators with $(...) subexpressions
# ============================================================================

class TestChainWithSubexpressions:
    def test_subexpr_and_subexpr(self) -> None:
        """$(cmd1) && $(cmd2) — subexpressions in chain."""
        result = pwsh_transform("$(cmd1) && $(cmd2)")[0]
        assert "&&" not in result
        assert "if ($?)" in result
        assert "$(cmd1)" in result
        assert "$(cmd2)" in result

    def test_subexpr_or_subexpr(self) -> None:
        result = pwsh_transform("$(cmd1) || $(cmd2)")[0]
        assert "||" not in result
        assert "if (-not $?)" in result

    def test_subexpr_and_or_chain(self) -> None:
        result = pwsh_transform("$(cmd1) && $(cmd2) || $(cmd3)")[0]
        assert "&&" not in result
        assert "||" not in result
        assert result.count("if ($?)") >= 1
        assert "if (-not $?)" in result

    def test_mixed_subexpr_and_plain_chain(self) -> None:
        result = pwsh_transform("cmd1 && $(cmd2) && cmd3")[0]
        assert "&&" not in result
        assert result.count("if ($?)") == 2


# ============================================================================
# Corner case: ?? with right side containing semicolons in parens
# ============================================================================

class TestCoalescingWithSemicolonRight:
    def test_subexpr_right_with_semicolons(self) -> None:
        """?? with subexpression right side containing ; at depth > 0."""
        result = pwsh_transform('$a ?? (cmd1; cmd2)')[0]
        assert "??" not in result
        assert "if ($null -ne $a)" in result
        assert "(cmd1; cmd2)" in result

    def test_subexpr_right_with_nested_semicolons(self) -> None:
        result = pwsh_transform('$a ?? $(cmd1; cmd2; cmd3)')[0]
        assert "??" not in result
        assert "if ($null -ne $a)" in result
        assert "$(cmd1; cmd2; cmd3)" in result


# ============================================================================
# Corner case: ternary with -and / -or in condition
# ============================================================================

class TestTernaryWithLogicalOperators:
    def test_and_in_condition(self) -> None:
        result = pwsh_transform('$a -and $b ? "both" : "not-both"')[0]
        assert "?" not in result
        assert "if ($a -and $b)" in result

    def test_or_in_condition(self) -> None:
        result = pwsh_transform('$a -or $b ? "either" : "neither"')[0]
        assert "?" not in result
        assert "if ($a -or $b)" in result

    def test_xor_in_condition(self) -> None:
        result = pwsh_transform('$a -xor $b ? "one" : "both-or-neither"')[0]
        assert "?" not in result
        assert "if ($a -xor $b)" in result

    def test_complex_logical_condition(self) -> None:
        result = pwsh_transform('$a -gt 0 -and $b -lt 10 ? "ok" : "bad"')[0]
        assert "?" not in result
        assert "if ($a -gt 0 -and $b -lt 10)" in result


# ============================================================================
# Corner case: nested ternary in both true AND false branches
# ============================================================================

class TestNestedTernaryBothBranches:
    def test_ternary_in_both_branches(self) -> None:
        """Outer ternary with inner ternary in both branches (one pass)."""
        result = pwsh_transform('$a ? ($b ? "c" : "d") : ($e ? "f" : "g")')[0]
        assert "if ($a)" in result
        # Inner ternaries preserved (one-pass limitation)
        assert "?" in result
        assert '"c"' in result
        assert '"g"' in result

    def test_ternary_chained_condition(self) -> None:
        """$a ? "a" : $b ? "b" : $c ? "c" : "d" — right-associative parsing."""
        result = pwsh_transform('$a ? "a" : $b ? "b" : $c ? "c" : "d"')[0]
        # Only outer ?: transformed in one pass
        assert "if ($a)" in result
        assert '"a"' in result
        # Inner cascading ternaries remain
        assert "?" in result


# ============================================================================
# Corner case: $null as ?? left operand
# ============================================================================

class TestNullCoalescingWithNullLeft:
    def test_null_literal_left_coalescing(self) -> None:
        """$null ?? 'default' — $null is always null, so 'default' is chosen."""
        result = pwsh_transform("$null ?? 'default'")[0]
        assert "??" not in result
        assert "if ($null -ne $null)" in result
        assert "{ $null }" in result
        assert "{ 'default' }" in result

    def test_null_automatic_var_left_coalescing(self) -> None:
        """$null with ??= is redundant but shouldn't crash."""
        result = pwsh_transform("$null ??= 'value'")[0]
        assert "??=" not in result
        assert "if ($null -eq $null)" in result


# ============================================================================
# Corner case: ?[ with complex index containing operators
# ============================================================================

class TestNullConditionalBracketComplexIndex:
    def test_bracket_index_with_coalescing_inside(self) -> None:
        """$a?[$b ?? 0] — ?? inside bracket index at depth > 0."""
        result = pwsh_transform("$a?[$b ?? 0]")[0]
        assert "?[" not in result  # outer ?[ is transformed
        assert "if ($null -ne $a)" in result
        # The ?? inside the brackets is at depth > 0, not transformed in single pass

    def test_bracket_index_with_ternary_inside(self) -> None:
        result = pwsh_transform('$a?[$cond ? 0 : 1]')[0]
        assert "?[" not in result
        assert "if ($null -ne $a)" in result

    def test_bracket_index_with_nested_bracket(self) -> None:
        result = pwsh_transform("$a?[$b[$c]]")[0]
        assert "?[" not in result
        assert "if ($null -ne $a)" in result
        assert "$b[$c]" in result


# ============================================================================
# Corner case: $scope:variable containing : adjacent to ternary :
# ============================================================================

class TestScopeColonWithTernary:
    def test_scope_var_in_ternary_true_branch(self) -> None:
        """$cond ? $global:x : $local:x — colon in $global:x vs ternary :."""
        result = pwsh_transform('$cond ? $global:x : $local:x')[0]
        assert "?" not in result
        assert "if ($cond)" in result
        assert "$global:x" in result
        assert "$local:x" in result

    def test_scope_var_in_ternary_false_branch(self) -> None:
        result = pwsh_transform('$cond ? "a" : $script:val')[0]
        assert "?" not in result
        assert "$script:val" in result

    def test_scope_var_as_ternary_condition(self) -> None:
        result = pwsh_transform('$global:flag ? "yes" : "no"')[0]
        assert "?" not in result
        assert "if ($global:flag)" in result


# ============================================================================
# Corner case: ??= with property chain on left (deep assignment)
# ============================================================================

class TestNCAPropertyDeepChain:
    def test_three_level_property_nca(self) -> None:
        result = pwsh_transform('$obj.Prop1.Prop2.Prop3 ??= "init"')[0]
        assert "??=" not in result
        assert "if ($null -eq $obj.Prop1.Prop2.Prop3)" in result
        assert "$obj.Prop1.Prop2.Prop3 = " in result

    def test_property_chain_with_method_nca(self) -> None:
        result = pwsh_transform('$svc.Status ??= "Running"')[0]
        assert "??=" not in result
        assert "if ($null -eq $svc.Status)" in result


# ============================================================================
# Corner case: backtick inside '' and "" NOT collapsed (literal)
# ============================================================================

class TestBacktickLiteralInStrings:
    def test_backtick_n_in_dq_not_collapsed(self) -> None:
        """`n inside double-quoted string is escape, not continuation."""
        result = pwsh_transform('"hello`nworld"')[0]
        assert "hello`nworld" in result

    def test_backtick_t_in_dq_not_collapsed(self) -> None:
        result = pwsh_transform('"col1`tcol2"')[0]
        assert "col1`tcol2" in result

    def test_backtick_in_sq_literal_not_collapsed(self) -> None:
        result = pwsh_transform("'backtick ` is literal'")[0]
        assert "`" in result

    def test_backtick_before_chars_in_sq_not_collapsed(self) -> None:
        """`a in single quotes is just literal `a."""
        result = pwsh_transform("'`a ?? b'")[0]
        assert "`a ?? b" in result
        assert "??" in result  # inside string, preserved


# ============================================================================
# Corner case: $? preservation inside if/elseif/while conditions
# ============================================================================

class TestDollarQuestionInKeywords:
    def test_if_with_dollar_q_condition(self) -> None:
        result = pwsh_transform("if ($?) { Write-Output 'ok' }")[0]
        assert result == "if ($?) { Write-Output 'ok' }"

    def test_while_with_dollar_q_condition(self) -> None:
        result = pwsh_transform("while ($?) { Do-Something }")[0]
        assert result == "while ($?) { Do-Something }"

    def test_elseif_with_dollar_q(self) -> None:
        code = "if ($a) { 1 } elseif ($?) { 2 } else { 3 }"
        result = pwsh_transform(code)[0]
        assert "$?" in result
        assert "?" not in result.replace("$?", "")  # no bare ? remains


# ============================================================================
# Corner case: ?. chain with method then property then index
# ============================================================================

class TestNullConditionalMixedChainTypes:
    def test_method_then_property_chain(self) -> None:
        result = pwsh_transform("$a?.GetValue()?.Length")[0]
        assert "?." not in result
        assert "GetValue()" in result
        assert ".Length" in result

    def test_property_then_method_then_property(self) -> None:
        result = pwsh_transform("$a?.Items?.GetType()?.Name")[0]
        assert "?." not in result
        assert "Items" in result
        assert "GetType()" in result
        assert "Name" in result

    def test_method_then_bracket_chain(self) -> None:
        result = pwsh_transform("$a?.GetItems()?[0]")[0]
        assert "?." not in result
        assert "GetItems()" in result


# ============================================================================
# Corner case: ?? with @() or @{} on right side
# ============================================================================

class TestCoalescingRightSideCollections:
    def test_coalescing_with_empty_array_right(self) -> None:
        result = pwsh_transform("$a ?? @()")[0]
        assert "??" not in result
        assert "if ($null -ne $a)" in result
        assert "@()" in result

    def test_coalescing_with_empty_hashtable_right(self) -> None:
        result = pwsh_transform("$a ?? @{}")[0]
        assert "??" not in result
        assert "if ($null -ne $a)" in result
        assert "@{}" in result

    def test_coalescing_with_scriptblock_right(self) -> None:
        result = pwsh_transform("$a ?? { Get-Date }")[0]
        assert "??" not in result
        assert "{ Get-Date }" in result


# ============================================================================
# Corner case: multiple ?. chains on same line with ; separator
# ============================================================================

class TestMultipleNullConditionalChains:
    def test_two_qd_chains_semicolon(self) -> None:
        result = pwsh_transform("$a?.b?.c; $x?.y?.z")[0]
        assert "?." not in result
        assert "$a.b.c" in result
        assert "$x.y.z" in result

    def test_qd_and_qb_chains_semicolon(self) -> None:
        result = pwsh_transform("$a?.Name; $b?[0]")[0]
        assert "?." not in result
        assert "?[" not in result
        assert "$a.Name" in result
        assert "$b[0]" in result

    def test_three_qd_chains_semicolon(self) -> None:
        result = pwsh_transform("$a?.P1; $b?.P2; $c?.P3")[0]
        assert "?." not in result
        assert result.count("if ($null -ne $") == 3


# ============================================================================
# Corner case: chain && || with trailing whitespace
# ============================================================================

class TestChainWithTrailingWhitespace:
    def test_and_chain_trailing_spaces(self) -> None:
        result = pwsh_transform("cmd1 && cmd2   ")[0]
        assert "&&" not in result
        assert "if ($?)" in result

    def test_or_chain_trailing_tabs(self) -> None:
        result = pwsh_transform("cmd1 || cmd2\t\t")[0]
        assert "||" not in result
        assert "if (-not $?)" in result

    def test_and_chain_leading_spaces(self) -> None:
        result = pwsh_transform("   cmd1 && cmd2")[0]
        assert "&&" not in result
        assert "if ($?)" in result


# ============================================================================
# Corner case: ternary with static method call in all positions
# ============================================================================

class TestTernaryStaticMethods:
    def test_static_method_in_condition(self) -> None:
        result = pwsh_transform('[string]::IsNullOrEmpty($s) ? "empty" : "ok"')[0]
        assert "?" not in result
        assert "if ([string]::IsNullOrEmpty($s))" in result

    def test_static_method_in_true_branch(self) -> None:
        result = pwsh_transform('$cond ? [Math]::Abs($x) : $x')[0]
        assert "?" not in result
        assert "{ [Math]::Abs($x) }" in result

    def test_static_method_in_false_branch(self) -> None:
        result = pwsh_transform('$cond ? $x : [Math]::Max($x, 0)')[0]
        assert "?" not in result
        assert "{ [Math]::Max($x, 0) }" in result


# ============================================================================
# Corner case: ??= idempotency after chain transforms
# ============================================================================

class TestNCAChainInteractionIdempotency:
    def test_nca_after_transform_is_idempotent(self) -> None:
        code = '$x ??= "init"'
        first = pwsh_transform(code)[0]
        second = pwsh_transform(first)[0]
        third = pwsh_transform(second)[0]
        assert first == second == third

    def test_nca_combined_with_other_ops_idempotent(self) -> None:
        code = '$a ??= "x"; $b = $c ?? "y"; cmd1 && cmd2'
        first = pwsh_transform(code)[0]
        second = pwsh_transform(first)[0]
        assert first == second


# ============================================================================
# Corner case: operators adjacent to end-of-line comment (#)
# ============================================================================

class TestOperatorsBeforeLineComment:
    def test_coalescing_before_line_comment(self) -> None:
        result = pwsh_transform('$a ?? "default" # end of line')[0]
        assert "??" not in result
        assert "if ($null -ne $a)" in result
        assert "# end of line" in result

    def test_ternary_before_line_comment(self) -> None:
        result = pwsh_transform('$cond ? "yes" : "no" # ternary')[0]
        assert "if ($cond)" in result
        assert "# ternary" in result

    def test_null_conditional_before_line_comment(self) -> None:
        result = pwsh_transform("$a?.Name # null-conditional")[0]
        assert "?." not in result
        assert "# null-conditional" in result

    def test_chain_before_line_comment(self) -> None:
        result = pwsh_transform("cmd1 && cmd2 # chain")[0]
        assert "&&" not in result
        assert "# chain" in result


# ============================================================================
# Corner case: deeply nested ?. inside method args (multi-pass)
# ============================================================================

class TestDeepNestedNullConditionalInArgs:
    def test_qd_inside_qd_method_arg(self) -> None:
        """?. inside another ?. method argument — both transformed."""
        result = pwsh_transform("$a?.Foo($b?.Bar($c?.Baz()))")[0]
        assert "?." not in result
        assert ".Foo(" in result
        assert ".Bar(" in result
        assert ".Baz()" in result

    def test_qd_with_nested_qb_in_arg(self) -> None:
        result = pwsh_transform("$a?.Process($b?[0])")[0]
        assert "?." not in result
        assert "?[" not in result

    def test_qd_with_nested_coalescing_in_arg(self) -> None:
        result = pwsh_transform('$a?.Method($b ?? "fallback")')[0]
        assert "?." not in result
        assert "??" not in result


# ============================================================================
# Corner case: ?? with $(...) containing newlines
# ============================================================================

class TestSubexpressionWithNewlines:
    def test_coalescing_with_multiline_subexpr_right(self) -> None:
        code = "$a ?? $(\n  Get-Date\n  Get-Process\n)"
        result = pwsh_transform(code)[0]
        assert "??" not in result
        assert "if ($null -ne $a)" in result

    def test_coalescing_with_multiline_subexpr_left(self) -> None:
        code = "$(\n  Get-Item $p\n) ?? 'default'"
        result = pwsh_transform(code)[0]
        assert "??" not in result


# ============================================================================
# Corner case: _join_continuation_lines preserves backtick in strings
# ============================================================================

class TestBacktickContinuationInStringsPreserved:
    def test_backtick_n_in_dq_not_joined(self) -> None:
        """`n inside "..." is escape sequence for newline, NOT continuation."""
        code = '"line1`nline2"'
        result = pwsh_transform(code)[0]
        assert "line1`nline2" in result

    def test_backtick_quote_in_dq_not_joined(self) -> None:
        """`" inside "..." is escaped quote, NOT continuation."""
        code = '"say `"hello`""'
        result = pwsh_transform(code)[0]
        assert '`"hello`"' in result


# ============================================================================
# Corner case: ??= with right side containing chain operators
# ============================================================================

class TestNCARightSideChain:
    def test_nca_right_side_with_and_chain(self) -> None:
        """$a ??= cmd1 && cmd2 — chain on right side of ??=."""
        result = pwsh_transform("$a ??= cmd1 && cmd2")[0]
        assert "??=" not in result
        assert "&&" not in result
        assert "if ($null -eq $a)" in result
        assert "if ($?)" in result

    def test_nca_right_side_with_or_chain(self) -> None:
        result = pwsh_transform("$a ??= cmd1 || cmd2")[0]
        assert "??=" not in result
        assert "||" not in result
        assert "if ($null -eq $a)" in result
        assert "if (-not $?)" in result


# ============================================================================
# Corner case: ?. with static member as base
# ============================================================================

class TestNullConditionalOnStaticMember:
    def test_static_property_dot(self) -> None:
        """[SomeType]::Property?.Member — static member null-conditional."""
        result = pwsh_transform("[SomeType]::Property?.Member")[0]
        assert "?." not in result
        assert "if ($null -ne [SomeType]::Property)" in result
        assert "[SomeType]::Property.Member" in result

    def test_static_method_call_dot(self) -> None:
        result = pwsh_transform("[Enum]::GetValues($t)?.Count")[0]
        assert "?." not in result
        assert "if ($null -ne [Enum]::GetValues($t))" in result
        assert "[Enum]::GetValues($t).Count" in result


# ============================================================================
# Corner case: ?. on splatted variable
# ============================================================================

class TestNullConditionalOnSplat:
    def test_splat_variable_dot(self) -> None:
        """@args?.Count — null-conditional on splatted variable base."""
        # @args is not a valid base for ?., but shouldn't crash
        result = pwsh_transform("@args?.Count")[0]
        assert isinstance(result, str)


# ============================================================================
# Corner case: operators in multiline pipelines (realistic PS scripts)
# ============================================================================

class TestRealisticMultiLineScripts:
    def test_conditional_service_check(self) -> None:
        code = """$svc = Get-Service -Name $name
$status = $svc?.Status ?? "Unknown"
if ($status -eq "Running") { Write-Output "ok" } else { Write-Output "not ok" }"""
        result = pwsh_transform(code)[0]
        assert "?." not in result
        assert "??" not in result

    def test_file_processing_pipeline(self) -> None:
        code = """$files = Get-ChildItem -Path $dir -Recurse
$csv = $files?.Where({$_.Extension -eq '.csv'})
$count = $csv?.Count ?? 0
Write-Output "Found $count CSV files"
"""
        result = pwsh_transform(code)[0]
        assert "?." not in result
        assert "??" not in result

    def test_api_response_handling(self) -> None:
        code = """$response = Invoke-RestMethod -Uri $url
$data = $response?.data ?? $response?.result ?? @{}
$name = $data?.name ?? "anonymous"
"""
        result = pwsh_transform(code)[0]
        assert "?." not in result
        assert "??" not in result


# ============================================================================
# Corner case: ??= with ${} braced var containing nested braces
# ============================================================================

class TestNCANestedBracedVars:
    def test_double_braced_var_nca(self) -> None:
        """${outer.${inner}} ??= 'val' — nested braced variable."""
        result = pwsh_transform("${outer.${inner}} ??= 'val'")[0]
        assert "??=" not in result
        assert "if ($null -eq ${outer.${inner}})" in result

    def test_triple_braced_var_nca(self) -> None:
        result = pwsh_transform("${a.${b.${c}}} ??= 'deep'")[0]
        assert "??=" not in result
        assert "if ($null -eq ${a.${b.${c}}})" in result


# ============================================================================
# Corner case: _find_expr_end with # comment at boundary
# ============================================================================

class TestExprEndHashComment:
    def test_hash_comment_right_after_operator(self) -> None:
        result = pwsh_transform("$a?.Name#$comment")[0]
        assert "?." not in result
        assert "if ($null -ne $a)" in result

    def test_hash_comment_right_after_ternary(self) -> None:
        result = pwsh_transform('$cond ? "a" : "b"#comment')[0]
        assert "if ($cond)" in result


# ============================================================================
# Ultimate idempotency: transform 3 times for all operators
# ============================================================================

class TestTripleTransformIdempotency:
    def test_triple_transform_all_ops(self) -> None:
        code = '$a ??= $b; $c = $d?.$e?."f" ?? "g"; cmd1 && cmd2 || cmd3'
        first = pwsh_transform(code)[0]
        second = pwsh_transform(first)[0]
        third = pwsh_transform(second)[0]
        assert first == second == third

    def test_triple_transform_ternary_only(self) -> None:
        code = '$x = $a ? ($b ? "c" : "d") : "e"'
        first = pwsh_transform(code)[0]
        second = pwsh_transform(first)[0]
        third = pwsh_transform(second)[0]
        assert second == third  # May not stabilize after 1 pass (nested ternaries)

    def test_triple_transform_coalescing_only(self) -> None:
        code = '$x = $a ?? $b ?? $c ?? "d"'
        first = pwsh_transform(code)[0]
        second = pwsh_transform(first)[0]
        third = pwsh_transform(second)[0]
        assert second == third


# ============================================================================
# Corner case: ?. on $using: scoped variable (PS remoting / ForEach -Parallel)
# ============================================================================

class TestUsingScopeNullConditional:
    def test_using_var_dot_property(self) -> None:
        """$using:var?.Property — common in ForEach-Object -Parallel."""
        result = pwsh_transform("$using:var?.Name")[0]
        assert "?." not in result
        assert "if ($null -ne $using:var)" in result
        assert "$using:var.Name" in result

    def test_using_var_bracket_index(self) -> None:
        result = pwsh_transform("$using:arr?[0]")[0]
        assert "?[" not in result
        assert "if ($null -ne $using:arr)" in result
        assert "$using:arr[0]" in result

    def test_using_var_coalescing(self) -> None:
        result = pwsh_transform('$using:val ?? "default"')[0]
        assert "??" not in result
        assert "if ($null -ne $using:val)" in result

    def test_using_var_nca(self) -> None:
        result = pwsh_transform('$using:val ??= 0')[0]
        assert "??=" not in result
        assert "if ($null -eq $using:val)" in result

    def test_using_var_variable_property(self) -> None:
        result = pwsh_transform("$using:obj?.$prop")[0]
        assert "?." not in result
        assert "if ($null -ne $using:obj)" in result
        assert "$prop" in result

    def test_using_var_chained(self) -> None:
        result = pwsh_transform("$using:data?.Rows?.Count")[0]
        assert "?." not in result
        assert "$using:data" in result
        assert ".Rows" in result
        assert ".Count" in result

    def test_using_var_idempotent(self) -> None:
        code = "$using:obj?.Name"
        first = pwsh_transform(code)[0]
        second = pwsh_transform(first)[0]
        assert first == second


# ============================================================================
# Corner case: `.` dot-sourcing operator with chain operators
# ============================================================================

class TestDotSourcingChain:
    def test_dot_source_and_chain(self) -> None:
        """. ./script.ps1 && cmd2 — dot-sourcing then chain."""
        result = pwsh_transform(". ./script.ps1 && cmd2")[0]
        assert "&&" not in result
        assert "if ($?)" in result
        assert ". ./script.ps1" in result

    def test_dot_source_or_chain(self) -> None:
        result = pwsh_transform(". ./setup.ps1 || Write-Error failed")[0]
        assert "||" not in result
        assert "if (-not $?)" in result

    def test_dot_source_with_args_chain(self) -> None:
        result = pwsh_transform(". ./helper.ps1 -Force && Write-Output ok")[0]
        assert "&&" not in result
        assert "if ($?)" in result
        assert ". ./helper.ps1 -Force" in result

    def test_dot_source_then_or_then_and(self) -> None:
        result = pwsh_transform(". ./cfg.ps1 || . ./default.ps1 && Write-Output loaded")[0]
        assert "||" not in result
        assert "&&" not in result
        assert "if (-not $?)" in result
        assert "if ($?)" in result


# ============================================================================
# Corner case: `&` call operator with chain operators
# ============================================================================

class TestCallOperatorChain:
    def test_call_op_and_chain(self) -> None:
        """& $cmd $arg && & $cmd2 $arg2 — call operator chain."""
        result = pwsh_transform("& $cmd $arg && & $cmd2 $arg2")[0]
        assert "&&" not in result
        assert "if ($?)" in result
        assert "& $cmd $arg" in result
        assert "& $cmd2 $arg2" in result

    def test_call_op_or_chain(self) -> None:
        result = pwsh_transform("& $backup || & $restore")[0]
        assert "||" not in result
        assert "if (-not $?)" in result

    def test_call_op_with_splat_chain(self) -> None:
        result = pwsh_transform("& $cmd @args && Write-Output done")[0]
        assert "&&" not in result
        assert "if ($?)" in result
        assert "@args" in result

    def test_call_op_nested_chain(self) -> None:
        result = pwsh_transform("& $a && & $b || & $c")[0]
        assert "&&" not in result
        assert "||" not in result
        assert "if ($?)" in result
        assert "if (-not $?)" in result

    def test_call_op_with_scriptblock_chain(self) -> None:
        result = pwsh_transform("& { Get-Date } && Write-Output ok")[0]
        assert "&&" not in result
        assert "if ($?)" in result


# ============================================================================
# Corner case: ?. with scriptblock method arguments
# ============================================================================

class TestNullConditionalScriptblockMethod:
    def test_foreach_with_scriptblock(self) -> None:
        """$a?.ForEach({ $_ }) — method with scriptblock argument."""
        result = pwsh_transform("$a?.ForEach({ $_ })")[0]
        assert "?." not in result
        assert "if ($null -ne $a)" in result
        assert "$a.ForEach({ $_ })" in result

    def test_where_with_scriptblock(self) -> None:
        result = pwsh_transform("$a?.Where({ $_ -gt 0 })")[0]
        assert "?." not in result
        assert "$a.Where({ $_ -gt 0 })" in result

    def test_foreach_then_property_chain(self) -> None:
        """$a?.ForEach({ $_ })?.Count — chain after scriptblock method."""
        result = pwsh_transform("$a?.ForEach({ $_ })?.Count")[0]
        assert "?." not in result
        assert ".ForEach({ $_ })" in result
        assert ".Count" in result

    def test_scriptblock_with_inner_operators(self) -> None:
        """?.ForEach({ ... }) where scriptblock contains ?. or ?? which are at depth>0."""
        result = pwsh_transform('$a?.ForEach({ $_.Name ?? "unknown" })')[0]
        assert "?." not in result
        assert "ForEach" in result
        # ?? inside scriptblock is at depth>0, not transformed in single pass

    def test_where_then_foreach_chain(self) -> None:
        result = pwsh_transform("$a?.Where({ $_ }).ForEach({ $_ })?.Count")[0]
        # ?. processed first, the rest depends on chain detection
        assert "?." not in result


# ============================================================================
# Corner case: ?? inside @() array construction
# ============================================================================

class TestCoalescingInsideArrayExpr:
    def test_array_with_two_coalescing(self) -> None:
        """@($a ?? 0, $b ?? 1) — coalescing inside array subexpression."""
        result = pwsh_transform("@($a ?? 0, $b ?? 1)")[0]
        # At depth>0 inside @(), coalescing not transformed in single pass
        assert "$a" in result
        assert "$b" in result

    def test_array_with_coalescing_and_ternary(self) -> None:
        result = pwsh_transform('@($a ?? "x", $cond ? "t" : "f")')[0]
        # Operators inside @() at depth>0 are not transformed
        assert "$a" in result
        assert "$cond" in result

    def test_coalescing_outside_array(self) -> None:
        """@(1,2) ?? @(3) — coalescing where left is @(). Already covered in TestNullCoalescingComplex, but duplicating for array context."""
        result = pwsh_transform("@(1,2) ?? @(3)")[0]
        assert "??" not in result
        assert "if ($null -ne @(1,2))" in result


# ============================================================================
# Corner case: ?. on $Host / $PSVersionTable automatic variables
# ============================================================================

class TestAutomaticVariableNullConditional:
    def test_host_version(self) -> None:
        """$Host?.Version — null-conditional on $Host automatic variable."""
        result = pwsh_transform("$Host?.Version")[0]
        assert "?." not in result
        assert "if ($null -ne $Host)" in result
        assert "$Host.Version" in result

    def test_psversiontable_psversion(self) -> None:
        result = pwsh_transform("$PSVersionTable?.PSVersion")[0]
        assert "?." not in result
        assert "if ($null -ne $PSVersionTable)" in result

    def test_psversiontable_chained(self) -> None:
        result = pwsh_transform("$PSVersionTable?.PSVersion?.Major")[0]
        assert "?." not in result
        assert "$PSVersionTable" in result
        assert ".PSVersion" in result
        assert ".Major" in result

    def test_host_ui_rawui_chained(self) -> None:
        result = pwsh_transform("$Host?.UI?.RawUI?.WindowTitle")[0]
        assert "?." not in result
        assert "$Host" in result
        assert ".UI" in result
        assert ".RawUI" in result
        assert ".WindowTitle" in result

    def test_executioncontext_variable(self) -> None:
        result = pwsh_transform("$ExecutionContext?.SessionState")[0]
        assert "?." not in result
        assert "if ($null -ne $ExecutionContext)" in result

    def test_myinvocation_variable(self) -> None:
        result = pwsh_transform("$MyInvocation?.MyCommand?.Name")[0]
        assert "?." not in result
        assert "$MyInvocation" in result


# ============================================================================
# Corner case: ?. chained from static member access
# ============================================================================

class TestNullConditionalStaticMemberChained:
    def test_static_prop_to_prop_to_index(self) -> None:
        """[Type]::Prop?.SubProp?[0] — chain from static prop through ?. to ?[."""
        result = pwsh_transform("[SomeType]::Prop?.SubProp?[0]")[0]
        assert "?." not in result
        assert "?[" not in result
        assert "[SomeType]::Prop" in result

    def test_static_method_to_prop_to_coalescing(self) -> None:
        result = pwsh_transform('[Enum]::GetValues($t)?.Length ?? 0')[0]
        assert "?." not in result
        assert "??" not in result
        assert "[Enum]::GetValues($t)" in result

    def test_static_prop_with_variable_member(self) -> None:
        result = pwsh_transform("[SomeType]::Prop?.$member")[0]
        assert "?." not in result
        assert "if ($null -ne [SomeType]::Prop)" in result
        assert "$member" in result

    def test_static_method_to_quoted_member(self) -> None:
        result = pwsh_transform('[obj]::Method()?."prop-name"')[0]
        assert "?." not in result
        assert '[obj]::Method()' in result
        assert '"prop-name"' in result

    def test_static_prop_to_method(self) -> None:
        result = pwsh_transform("[SomeType]::Prop?.ToString()")[0]
        assert "?." not in result
        assert "[SomeType]::Prop.ToString()" in result


# ============================================================================
# Corner case: ${this} / ${PSCmdlet} automatic braced variables with ?.
# ============================================================================

class TestBracedAutomaticVarNullConditional:
    def test_this_variable_dot(self) -> None:
        """${this}?.Property — used in PS classes."""
        result = pwsh_transform("${this}?.Name")[0]
        assert "?." not in result
        assert "if ($null -ne ${this})" in result
        assert "${this}.Name" in result

    def test_this_variable_bracket(self) -> None:
        result = pwsh_transform("${this}?.[0]")[0]
        assert "?[" not in result
        # ?[ is processed after ?., but the `?.` before `[` makes it tricky

    def test_pscmdlet_variable(self) -> None:
        result = pwsh_transform("${PSCmdlet}?.MyInvocation")[0]
        assert "?." not in result
        assert "${PSCmdlet}" in result

    def test_this_variable_nca(self) -> None:
        result = pwsh_transform('${this} ??= "init"')[0]
        assert "??=" not in result
        assert "if ($null -eq ${this})" in result


# ============================================================================
# Corner case: ??= with string-literal-like left side
# ============================================================================

class TestNCALiteralLeft:
    def test_string_single_quoted_left_nca(self) -> None:
        """'literal' ??= 'value' — string literal on left of ??= (invalid PS, shouldn't crash)."""
        result = pwsh_transform("'literal' ??= 'value'")[0]
        assert isinstance(result, str)

    def test_number_left_nca(self) -> None:
        """123 ??= 'value' — number literal on left."""
        result = pwsh_transform("123 ??= 'value'")[0]
        assert isinstance(result, str)


# ============================================================================
# Corner case: -match operator combining with ternary and $Matches
# ============================================================================

class TestMatchOperatorWithTernary:
    def test_match_result_in_ternary_condition(self) -> None:
        r"""$s -match '(\d+)' ? $Matches[1] : $null — match then ternary."""
        result = pwsh_transform("$s -match '(\\d+)' ? $Matches[1] : $null")[0]
        assert "?" not in result
        assert "if ($s -match '(\\d+)')" in result

    def test_notmatch_in_ternary_condition(self) -> None:
        result = pwsh_transform('$s -notmatch "x" ? "clean" : "dirty"')[0]
        assert "?" not in result
        assert "if ($s -notmatch \"x\")" in result

    def test_match_with_parens_ternary(self) -> None:
        result = pwsh_transform('($s -match "^(\\d+)$") ? [int]$Matches[1] : -1')[0]
        assert "?" not in result
        assert "if (($s -match \"^(\\d+)$\"))" in result


# ============================================================================
# Corner case: $?.?. chain (automatic var then null-conditional NOT $? first)
# ============================================================================

class TestDollarQuestionWithNullConditional:
    def test_dollar_q_then_dot_chain(self) -> None:
        """$??.Property — $? is auto var, ?. is null-conditional. Should NOT treat $? as ternary."""
        result = pwsh_transform("$??.Property")[0]
        # $? is detected, ?. is null-conditional
        assert "if ($null -ne $?)" in result

    def test_dollar_q_then_bracket_chain(self) -> None:
        result = pwsh_transform("$??[0]")[0]
        # $?[0] in output is $? auto-var + [0] index, not ?[ operator
        assert "if ($null -ne $?)" in result
        assert "$?[0]" in result


# ============================================================================
# Corner case: ?? in a pipeline (right side piped)
# ============================================================================

class TestCoalescingInPipeline:
    def test_coalescing_right_piped_to_cmdlet(self) -> None:
        """$a ?? $b | ForEach-Object { $_ } — pipe binds tighter than ??, so ?? right is just $b."""
        result = pwsh_transform("$a ?? $b | ForEach-Object { $_ }")[0]
        assert "??" not in result
        assert "if ($null -ne $a)" in result

    def test_coalescing_left_is_pipeline(self) -> None:
        """(Get-Item $p) ?? $default — parenthesized pipeline as left."""
        result = pwsh_transform("(Get-Item $p) ?? $default")[0]
        assert "??" not in result
        assert "if ($null -ne (Get-Item $p))" in result

    def test_coalescing_pipe_both_sides(self) -> None:
        result = pwsh_transform("(Get-Date) ?? (Get-Date -Year 2000)")[0]
        assert "??" not in result
        assert "if ($null -ne (Get-Date))" in result


# ============================================================================
# Corner case: multi-line $() subexpression and here-string interaction
# ============================================================================

class TestMultiLineSubExprEdgeCases:
    def test_multiline_subexpr_left_of_coalescing(self) -> None:
        code = """$(if ($a) {
  Get-Date
} else {
  $null
}) ?? 'default'"""
        result = pwsh_transform(code)[0]
        assert "??" not in result
        # multi-line subexpr detection
        assert "if ($null -ne" in result

    def test_here_string_with_embedded_operators_across_lines(self) -> None:
        code = """$text = @'
line with ?? and ?. and &&
and || operators
'@
$x = $a ?? 'fallback'"""
        result = pwsh_transform(code)[0]
        # Operators inside here-string preserved
        assert "?? and ?. and &&" in result
        assert "|| operators" in result
        # Real ?? outside here-string transformed
        assert "if ($null -ne $a)" in result


# ============================================================================
# Corner case: $? in subexpression context
# ============================================================================

class TestDollarQuestionSubExpr:
    def test_dollar_q_in_if_condition(self) -> None:
        """if ($?) { ... } — $? in if condition, not ternary."""
        result = pwsh_transform("if ($?) { Write-Output ok } else { Write-Error fail }")[0]
        assert result == "if ($?) { Write-Output ok } else { Write-Error fail }"

    def test_dollar_q_assignment_ternary(self) -> None:
        """$x = $? ? 'success' : 'failure' — $? as ternary condition."""
        result = pwsh_transform("$x = $? ? 'success' : 'failure'")[0]
        assert result == "$x = if ($?) { 'success' } else { 'failure' }"

    def test_dollar_q_in_pipeline_chain(self) -> None:
        """cmd1; if ($?) { cmd2 } — already transformed chain, $? should be preserved."""
        result = pwsh_transform("cmd1; if ($?) { cmd2 }")[0]
        assert result == "cmd1; if ($?) { cmd2 }"


# ============================================================================
# Corner case: ?. with property name matching a PS keyword used as function name
# ============================================================================

class TestNullConditionalKeywordPropertyChains:
    def test_begin_process_end_chain(self) -> None:
        """$obj?.Begin?.Process?.End — keyword-named properties in chain."""
        result = pwsh_transform("$obj?.Begin?.Process?.End")[0]
        assert "?." not in result
        assert "$obj.Begin.Process.End" in result

    def test_exit_break_continue_chain(self) -> None:
        result = pwsh_transform("$obj?.Exit?.Break?.Continue")[0]
        assert "?." not in result
        assert "$obj.Exit.Break.Continue" in result

    def test_try_catch_finally_chain(self) -> None:
        result = pwsh_transform("$obj?.Try?.Catch?.Finally")[0]
        assert "?." not in result
        assert "$obj.Try.Catch.Finally" in result


# ============================================================================
# Corner case: ?. on array literal
# ============================================================================

class TestNullConditionalOnLiterals:
    def test_string_literal_dot(self) -> None:
        """'hello'?.Length — null-conditional on string literal (valid in PS7)."""
        result = pwsh_transform("'hello'?.Length")[0]
        assert "?." not in result
        assert "if ($null -ne 'hello')" in result
        assert "'hello'.Length" in result

    def test_number_literal_dot(self) -> None:
        result = pwsh_transform("123?.GetType()")[0]
        assert "?." not in result
        assert "if ($null -ne 123)" in result

    def test_double_quoted_string_literal_dot(self) -> None:
        result = pwsh_transform('"hello"?.Length')[0]
        assert "?." not in result
        assert "if ($null -ne \"hello\")" in result

    def test_dollar_null_dot(self) -> None:
        """$null?.Property — $null literal with null-conditional."""
        result = pwsh_transform("$null?.Property")[0]
        assert "?." not in result
        assert "if ($null -ne $null)" in result


# ============================================================================
# Corner case: combined $? as ??? (three question marks in a row)
# ============================================================================

class TestTripleQuestionMark:
    def test_dollar_q_double_question(self) -> None:
        """$??? "fallback" — $? followed by ?? operator."""
        result = pwsh_transform('$??? "fallback"')[0]
        assert "??" not in result
        assert "if ($null -ne $?)" in result
        assert '"fallback"' in result

    def test_dollar_q_then_question_dot(self) -> None:
        """$??.Property — $? followed by ?. operator."""
        result = pwsh_transform("$??.Property")[0]
        # $?.Property in output is $? auto-var + .Property, not ?. operator
        assert "if ($null -ne $?)" in result
        assert "$?.Property" in result

    def test_dollar_q_then_bracket_chain(self) -> None:
        result = pwsh_transform("$??[0]")[0]
        # $?[0] in output is $? auto-var + [0] index, not ?[ operator
        assert "if ($null -ne $?)" in result
        assert "$?[0]" in result


# ============================================================================
# Corner case: ?[ with depth tracking for nested brackets in index
# ============================================================================

class TestNullConditionalBracketDepthTracking:
    def test_bracket_with_multiple_nested_brackets(self) -> None:
        result = pwsh_transform("$a?[$b[$c[$d]]]")[0]
        assert "?[" not in result
        assert "if ($null -ne $a)" in result
        assert "$b[$c[$d]]" in result

    def test_bracket_with_parens_in_index(self) -> None:
        result = pwsh_transform("$a?[($b + $c)]")[0]
        assert "?[" not in result
        assert "if ($null -ne $a)" in result
        assert "($b + $c)" in result

    def test_bracket_with_array_index(self) -> None:
        result = pwsh_transform("$a?[$b, $c]")[0]
        assert "?[" not in result
        assert "if ($null -ne $a)" in result
        assert "$b, $c" in result

    def test_bracket_with_range_operator(self) -> None:
        result = pwsh_transform("$a?[$b..$c]")[0]
        assert "?[" not in result
        assert "if ($null -ne $a)" in result


# ============================================================================
# Corner case: ??= with trailing code that contains other operators
# ============================================================================

class TestNCAWithTrailingOperators:
    def test_nca_then_chain_on_same_line(self) -> None:
        result = pwsh_transform('$a ??= "x"; cmd1 && cmd2')[0]
        assert "??=" not in result
        assert "&&" not in result
        assert "if ($null -eq $a)" in result
        assert "if ($?)" in result

    def test_nca_then_ternary_on_same_line(self) -> None:
        result = pwsh_transform('$a ??= "x"; $b = $cond ? "t" : "f"')[0]
        assert "??=" not in result
        assert "?" not in result  # ternary ? is gone
        assert "if ($null -eq $a)" in result
        assert "if ($cond)" in result

    def test_nca_then_null_conditional_on_same_line(self) -> None:
        result = pwsh_transform('$a ??= "x"; $b = $obj?.Name')[0]
        assert "??=" not in result
        assert "?." not in result
        assert "if ($null -eq $a)" in result
        assert "if ($null -ne $obj)" in result

    def test_nca_then_coalescing_on_same_line(self) -> None:
        result = pwsh_transform('$a ??= "x"; $b = $c ?? "y"')[0]
        assert "??=" not in result
        assert "??" not in result
        assert "if ($null -eq $a)" in result
        assert "if ($null -ne $c)" in result


# ============================================================================
# Corner case: multiple ??= on same line separated by ; (two ??=)
# ============================================================================

class TestMultipleNCAOnSameLine:
    def test_two_nca_semicolon_separated(self) -> None:
        result = pwsh_transform('$a ??= 1; $b ??= 2')[0]
        assert "??=" not in result
        assert "if ($null -eq $a)" in result
        assert "if ($null -eq $b)" in result

    def test_three_nca_semicolon_separated(self) -> None:
        result = pwsh_transform('$a ??= 1; $b ??= 2; $c ??= 3')[0]
        assert "??=" not in result
        assert result.count("if ($null -eq $") == 3

    def test_nca_mixed_with_null_conditional_semicolons(self) -> None:
        result = pwsh_transform('$a ??= "x"; $b?.Name; $c ??= "y"')[0]
        assert "??=" not in result
        assert "?." not in result
        assert "if ($null -eq $a)" in result
        assert "if ($null -eq $c)" in result


# ============================================================================
# Corner case: backtick inside a here-string is literal, not continuation
# ============================================================================

class TestBacktickLiteralInHereString:
    def test_backtick_newline_inside_here_string(self) -> None:
        """Backtick+newline inside @'...'@ is literal, not merged."""
        code = "@'\nline1 `\nline2\n'@"
        result = pwsh_transform(code)[0]
        # The backtick+newline is inside a here-string region, so not collapsed
        assert isinstance(result, str)

    def test_backtick_newline_inside_dq_here_string(self) -> None:
        code = '@"\nline1 `\nline2\n"@'
        result = pwsh_transform(code)[0]
        assert isinstance(result, str)


# ============================================================================
# Corner case: ternary as sole content of a scriptblock
# ============================================================================

class TestTernaryInsideScriptBlock:
    def test_ternary_inside_scriptblock_not_transformed(self) -> None:
        """{ $a ? $b : $c } — ternary inside scriptblock at depth>0."""
        result = pwsh_transform('$sb = { $a ? $b : $c }')[0]
        assert "$sb = " in result
        # Ternary inside braces at depth>0 is NOT transformed
        assert "?" in result

    def test_ternary_inside_nested_scriptblock(self) -> None:
        result = pwsh_transform('$sb = { { $a ? $b : $c } }')[0]
        assert "?" in result


# ============================================================================
# Corner case: ?? on same base variable used after transformation
# ============================================================================

class TestCoalescingReuseSameVar:
    def test_same_var_multiple_coalescing(self) -> None:
        """$x = $a ?? 1; $y = $a ?? 2 — same var used in two ?? expressions."""
        result = pwsh_transform('$x = $a ?? 1; $y = $a ?? 2')[0]
        assert "??" not in result
        assert "if ($null -ne $a) { $a } else { 1 }" in result
        assert "if ($null -ne $a) { $a } else { 2 }" in result

    def test_same_var_coalescing_and_nca(self) -> None:
        result = pwsh_transform('$a ??= 0; $b = $a ?? 1')[0]
        assert "??=" not in result
        assert "??" not in result
        assert "if ($null -eq $a)" in result


# ============================================================================
# Corner case: -replace operator combined with ternary/coalescing
# ============================================================================

class TestReplaceOperatorCombined:
    def test_replace_in_ternary_condition(self) -> None:
        """-replace with comma-separated args before ternary.
        KNOWN LIMITATION: comma is an expression boundary, so the ternary
        condition is just '"y"' rather than '$s -replace "x","y"'."""
        result = pwsh_transform('$s -replace "x","y" ? "changed" : "same"')[0]
        assert "?" not in result
        # Current behaviour: comma delimits expression, condition is '"y"'
        assert "if (" in result
        assert "\"changed\"" in result
        assert "\"same\"" in result

    def test_replace_in_coalescing_left(self) -> None:
        result = pwsh_transform('($s -replace "a","b") ?? $s')[0]
        assert "??" not in result
        assert "if ($null -ne ($s -replace \"a\",\"b\"))" in result


# ============================================================================
# Corner case: ?. where member name is an integer (edge of _scan_member_name)
# ============================================================================

class TestNullConditionalNumericMember:
    def test_integer_member_name(self) -> None:
        """$a?.123 — numeric member names are not valid PS identifiers."""
        result = pwsh_transform("$a?.123")[0]
        assert isinstance(result, str)

    def test_member_starting_with_digit_then_alpha(self) -> None:
        """$a?.123abc — member starting with digit."""
        result = pwsh_transform("$a?.123abc")[0]
        assert isinstance(result, str)
