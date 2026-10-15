"""Phase-7 IR-ownership orchestration tests (plan 3.5, Option A).

Covers:
  1. ``translator.translate`` is the live orchestration entry: empty input ->
     BashFix(command); the fix_bash_command thin wrapper delegates to it; the
     @-strip / cmd-if / cmd-for / fallback / wrapper / reparse / path / nul /
     heredoc outputs are byte-exact (the live path is the engine reader driving
     IR-owned decisions -- locked here for the shapes Option A owns).
  2. ``ir_command`` dispatcher slots 4-8 are genuine IR handlers: for a broad
     well-formed battery, rendering the IR decisions through ONE shared EditPlan
     (translate_ir) equals the live output byte-for-byte.
  3. ``register_default_rules`` fills slots 4-8 under the plan priorities.
  4. The @-strip re-dispatch (RuleResult.dispatch) and the outer match's
     consumed extent (TranslationContext.consumed_end) are honoured by the IR
     orchestrator.
"""
from __future__ import annotations

import pytest

from kimi_cli.native_loader import get_compat as _get_compat

import kimix_native._shell_compat as facade
from kimix_native.shell_compat import ir_command as irc
from kimix_native.shell_compat import translator as tr

assert _get_compat("_shell_compat") is facade  # the shim resolves in every mode


def _full(f):
    return (
        f.command,
        f.replacements,
        f.path_changes,
        f.shell_wrappers,
        f.nul_fixes,
        f.unsupported,
        f.cmd_conditional_fixes,
    )


# ── 1. translate is the live orchestration entry ─────────────────────────────


def test_empty_input_returns_byte_for_byte():
    assert tr.translate("").command == ""
    assert _full(tr.translate("")) == _full(facade.BashFix(""))
    # the live wrapper is fix_bash_command -> translate
    assert _full(facade.fix_bash_command("")) == _full(tr.translate(""))


@pytest.mark.parametrize(
    "cmd",
    [
        r"rev C:\a > nul",
        "tree",
        r"cd /d D:\x",
        "bash echo hi",
        "watch -n1 'tree'",
        "eval 'tree > nul'",
        "sudo tree",
        "timeout 5 rev C:\\a",
        "gtimeout 5 rev",
        "journalctl -u x",
        "goto :end",
        "exit /b",
        "@echo off",
        "@rev",
        r"if exist D:\a (dir D:\a 2>nul) else (echo no)",
        r"for %f in (a b) do echo %f",
        "bash -c 'cd C:\\x'",
        "trap 'tree > nul' EXIT",
        "xargs sh -c 'tree > nul'",
        r"C:\tools\rg.exe --flag",
        r"echo $(tree C:\a)",
        "cat <<E\nhi\nE\n&& echo y",
    ],
)
def test_fix_bash_command_delegates_to_translate(cmd):
    assert _full(facade.fix_bash_command(cmd)) == _full(tr.translate(cmd))


def test_heredoc_repair_applied_last():
    # trailing-operator repair moves '&& echo y' onto the redirection line
    result = tr.translate("cat <<EOF\nhi\nEOF\n&& echo y")
    assert result.command.startswith("cat <<EOF && echo y\nhi\nEOF\n")


# ── 2. IR slots 4-8 render through ONE shared EditPlan == live ───────────────

# controlled well-formed battery: one logical command per rule family the per-
# node dispatcher handlers own (fallback / path / nul / cd / single wrapper /
# unwrap / reparse / heredoc / @-strip / cmd-if / cmd-for / jump).  Chained /
# nested wrapper state and cmd constructs nested inside $()-substitution are
# engine-owned scan flow (Option A: the engine reader drives the flow) and are
# covered byte-for-byte by the live differential gate, not asserted here.
_CONTROLLED_BATTERY = [
    r"rev C:\a > nul",
    r"tree C:\dir > NUL",
    "cat /c/x/y",
    r"cd /d D:\x",
    "echo 2>nul",
    "journalctl -u x",
    r"C:\tools\rg.exe --flag",
    "python C:\\p\\q.py",
    "cat f > /tmp/x",
    "grep C:\\a f",
    "sed -e s C:\\a",
    r"awk '{print}' C:\a",
    "rg C:\\pat f",
    "bash echo hi",
    "bash cd /c/dev/x",
    r"bash rev C:\a > nul",
    "eval 'tree > nul'",
    "trap 'cd C:\\x' EXIT",
    "bash -c 'cd C:\\x'",
    "watch -n1 'tree'",
    r"bash -c 'rev C:\a > nul'",
    r"cat < /c/in > /c/out",
    r"find . -name C:\a",
    "xargs sh -c 'tree > nul'",
    "sh script.sh",
    "sudo tree",
    "gtimeout 5 rev",
    "timeout 5 rev C:\\a",
    "nohup tree",
    "nice -n 5 rev",
    "env PATH=/x tree",
    "@rev",
    "@echo off",
    r"@tree C:\a",
    "goto :end",
    "exit /b",
    r"if exist D:\a (dir D:\a 2>nul) else (echo no)",
    r"if defined V (dir D:\a 2>nul)",
    "for %f in (a b) do echo %f",
    r"for %f in (C:\a\*.txt) do type %f",
    "cat <<E\nhi\nE\n&& echo y",
    "cat <<'Q'\n$x\nQ\n; ls",
    "cat <<-E\n\tb\n\tE\n| wc -l",
    r"cd /d D:\x > nul",
    "echo $(tree C:\\a)",
    "echo $(rev) > nul",
    "cat <(tree)",
    "tree; rev",
    "tree && rev C:\\a",
    r"echo a | cat /c/b > nul",
]


@pytest.mark.parametrize("cmd", _CONTROLLED_BATTERY)
def test_ir_battery_render_equals_live(cmd):
    assert _full(irc.translate_ir(cmd)) == _full(tr.translate(cmd))


def test_ir_battery_is_disjoint_editplan():
    # every controlled command dispatches through the full 8-slot chain into ONE
    # shared context whose recorded splices are pairwise disjoint; render must
    # not raise OverlappingEdits (the Â§3.5 overlap-rejecting collector).
    for cmd in _CONTROLLED_BATTERY:
        ctx = tr.TranslationContext()
        dispatcher = irc.register_default_rules(
            tr.register_cmd_rules(tr.RuleDispatcher())
        )
        for node in irc.outer_commands(cmd):
            if node.span.start < ctx.consumed_end:
                continue
            try:
                dispatcher.dispatch(node, cmd, ctx)
            except tr.OverlappingEdits:
                pytest.fail(f"overlapping edits in {cmd!r}")
        _ = ctx.plan.render(cmd)  # must not raise


def _first_cmd(src):
    nodes = irc.outer_commands(src)
    return nodes[0]


# ── 3. register_default_rules fills slots 4-8 under plan priorities ──────────


def test_register_default_rules_fills_slots_4_to_8():
    d = irc.register_default_rules(tr.register_cmd_rules(tr.RuleDispatcher()))
    assert d.rules == (
        (tr.RuleDispatcher.PRIORITY_CMD_AT, "cmd_at"),
        (tr.RuleDispatcher.PRIORITY_CMD_CONTROL, "cmd_control"),
        (tr.RuleDispatcher.PRIORITY_UNSUPPORTED, "cmd_jump"),
        (tr.RuleDispatcher.PRIORITY_FALLBACK, "fallback"),
        (tr.RuleDispatcher.PRIORITY_WRAPPER, "wrapper"),
        (tr.RuleDispatcher.PRIORITY_PATH, "path"),
        (tr.RuleDispatcher.PRIORITY_NUL, "nul"),
        (tr.RuleDispatcher.PRIORITY_HEREDOC, "heredoc"),
    )


# ── 4. @-strip re-dispatch + consumed extent are genuine IR handlers ─────────


def test_at_strip_continuation_registers_fallback():
    # '@rev' -> slot1 strips '@' + slot4 (via decide_at_continuation) registers rev
    d = irc.decide_command(_first_cmd("@rev"), "@rev")
    assert not d.is_cmd_owned
    assert d.names4 == ["rev"]


def test_at_strip_echo_off_is_cmd_owned():
    # '@echo off' statement is owned by slot 1 (is_cmd_owned), not classified here
    d = irc.decide_command(_first_cmd("@echo off"), "@echo off")
    assert d.is_cmd_owned


def test_cmd_if_owned_skips_engine_buckets():
    # a plain 'if' (cmd keyword) is owned by slot 2 -> is_cmd_owned True
    d = irc.decide_command(_first_cmd(r"if exist D:\a (dir)"), r"if exist D:\a (dir)")
    assert d.is_cmd_owned


# ── the scanner-adapter / reader compatibility surface is intact ─────────────


def test_scanner_adapter_attributes_present():
    sc = facade._BashFixScanner("tree > nul")
    for attr in ("s", "n", "edits", "names", "path_notes", "shell_notes",
                 "heredoc_events", "nul_fixes", "unsupported",
                 "cmd_conditional_notes"):
        assert hasattr(sc, attr)
    sc._scan_range(0, sc.n)
    assert sc.nul_fixes == ["nul"]


def test_facade_al_untouched_195():
    assert len(facade.__all__) == 195


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
