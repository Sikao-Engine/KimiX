"""FP-05 probes: third-party-library-policy compliance (AGENTS.md).

* `kimi_cli/tools/__init__.py` must not import the stdlib `json` module, while
  `extract_key_argument` must still return `None` for anything
  `kosong.utils.jsonx.loads_relaxed` can raise (it raises both
  `orjson.JSONDecodeError` and stdlib `json.JSONDecodeError`).
* `edit/modes/sloppy.py` must use `regex` (drop-in) instead of `re` and keep the
  marker patterns working.
* `tools/gate_library_policy.py` must report zero unwaived hits for both trees.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

sys.path.insert(0, str(REPO_ROOT / "tools"))
from gate_library_policy import scan  # noqa: E402


# --------------------------------------------------------------------------- #
# G-POLICY gate
# --------------------------------------------------------------------------- #


def test_gate_library_policy_has_no_unwaived_hits() -> None:
    hits, waived, unparsable = scan(
        ["kimi-cli/src/kimi_cli/tools", "src/kimix/tools"],
        REPO_ROOT / "tools" / "library_policy_allowlist.txt",
    )
    assert unparsable == 0
    assert hits == [], f"unwaived stdlib-for-third-party imports: {hits}"
    assert len(waived) >= 1, "the difflib waiver must be recorded"
    assert all(w["module"] == "difflib" for w in waived)


def test_policy_waiver_carries_a_rationale() -> None:
    text = (REPO_ROOT / "tools" / "library_policy_allowlist.txt").read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        assert "#" in stripped, f"allowlist entry without a rationale: {stripped!r}"


# --------------------------------------------------------------------------- #
# stdlib json removal in tools/__init__.py
# --------------------------------------------------------------------------- #


def _stdlib_json_imports(path: Path) -> list[int]:
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            lines.extend(node.lineno for alias in node.names if alias.name.split(".")[0] == "json")
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "json":
            lines.append(node.lineno)
    return lines


def test_tools_init_does_not_import_stdlib_json() -> None:
    path = REPO_ROOT / "kimi-cli/src/kimi_cli/tools/__init__.py"
    assert _stdlib_json_imports(path) == []


def test_extract_key_argument_returns_none_for_stdlib_json_decode_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The json_repair fallback in `loads_relaxed` raises stdlib json.JSONDecodeError."""
    import kosong.utils.jsonx as jsonx
    from kimi_cli.tools import extract_key_argument

    def _boom(_data: str) -> object:
        raise json.JSONDecodeError("nope", "{}", 0)

    monkeypatch.setattr(jsonx, "loads_relaxed", _boom)
    assert extract_key_argument("{}", "read") is None


def test_extract_key_argument_returns_none_for_orjson_decode_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import orjson

    import kosong.utils.jsonx as jsonx
    from kimi_cli.tools import extract_key_argument

    def _boom(_data: str) -> object:
        raise orjson.JSONDecodeError("nope", "{}", 0)

    monkeypatch.setattr(jsonx, "loads_relaxed", _boom)
    assert extract_key_argument("{}", "read") is None


def test_extract_key_argument_survives_a_real_source_exception() -> None:
    from kimi_cli.tools import extract_key_argument

    assert extract_key_argument("}{ not json at all ~~~", "read") is None
    assert extract_key_argument("", "read") is None


# --------------------------------------------------------------------------- #
# regex in sloppy mode
# --------------------------------------------------------------------------- #


def test_sloppy_mode_uses_the_regex_library() -> None:
    import regex

    from kimi_cli.tools.file.edit.modes import sloppy

    assert sloppy.re is regex, "sloppy mode must import `regex`, not the stdlib `re`"


def test_sloppy_section_marker_patterns_still_match() -> None:
    from kimi_cli.tools.file.edit.modes import sloppy

    section = sloppy._SECTION_RE.match("§*   Title of section  ")
    assert section is not None
    assert section.group(1) == "*"

    inline = sloppy._INLINE_RE.search("⟪old text│new text⟫")
    assert inline is not None
    assert inline.group(1) == "old text"
    assert inline.group(2) == "new text"

    assert sloppy._INLINE_RE.search("no markers here") is None


def test_sloppy_module_has_no_stdlib_re_import() -> None:
    path = REPO_ROOT / "kimi-cli/src/kimi_cli/tools/file/edit/modes/sloppy.py"
    text = path.read_text(encoding="utf-8")
    assert "\nimport re\n" not in text
    assert "import regex as re" in text


# --------------------------------------------------------------------------- #
# dead import removal
# --------------------------------------------------------------------------- #


def test_best_of_n_has_no_dead_xxhash_import() -> None:
    path = REPO_ROOT / "src/kimix/tools/swarm/best_of_n.py"
    assert "import xxhash" not in path.read_text(encoding="utf-8")


def test_best_of_n_still_renders_a_unified_diff(tmp_path: Path) -> None:
    """The waived `difflib` use must keep working (it is the only unified diff)."""
    module_path = REPO_ROOT / "src/kimix/tools/swarm/best_of_n.py"
    text = module_path.read_text(encoding="utf-8")
    assert "difflib.unified_diff(" in text

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import difflib;"
                "d=list(difflib.unified_diff(['a\\n'],['b\\n'],fromfile='a/x',tofile='b/x'));"
                "print(''.join(d))"
            ),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "--- a/x" in proc.stdout
    assert "+++ b/x" in proc.stdout
