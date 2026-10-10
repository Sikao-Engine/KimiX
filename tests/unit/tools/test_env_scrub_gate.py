"""FP-11 probes: credential scrubbing for every shell child process.

`ProcessTask.scrub_env` used to default to `False`, and none of the eight shell
spawn sites (bash x3, pwsh x3, Run, python) passed it, so `security.scrub_child_env`
never ran for bash/pwsh/Run. Worse, even with `scrub_env=True` the caller's
`env=` snapshot - a full `os.environ.copy()` from `_env_with_rg_bin_path()` - is
merged in AFTER the base is scrubbed, which restored every credential.

Fix: `scrub_env` now defaults to `True`, and `_env_with_rg_bin_path()` scrubs the
snapshot at its source, so no spawn site can re-introduce a secret.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from kimi_cli.tools.common import ProcessTask, _env_with_rg_bin_path  # noqa: E402
from kimi_cli.tools.security import scrub_child_env  # noqa: E402

# NOTE: `KIMIX_*` is an explicit SAFE prefix in `security.scrub_child_env` (it is
# the project's own config namespace), so probes must NOT use a KIMIX_-prefixed
# name to test scrub-bing.
_ENV_PROBE = (
    "import os,sys;"
    "print('|'.join(sorted(k for k in os.environ if k in "
    "('GATE_API_KEY','GATE_TOKEN','GATE_PLAIN'))))"
)


def test_process_task_scrubs_by_default() -> None:
    task = ProcessTask(sys.executable, ["-c", "print(1)"])
    assert task.scrub_env is True, "defaults must scrub; the python tool already did"


def test_rg_bin_path_helper_scrubs_the_snapshot() -> None:
    base = {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "GATE_API_KEY": "secret-value",
        "AWS_SECRET_ACCESS_KEY": "secret-value",
        "GITHUB_TOKEN": "secret-value",
        "GATE_PLAIN": "kept",
        "KIMIX_CONFIG_PATH": "kept-by-design",
    }
    out = _env_with_rg_bin_path(base)
    assert "GATE_API_KEY" not in out
    assert "AWS_SECRET_ACCESS_KEY" not in out
    assert "GITHUB_TOKEN" not in out
    assert out["GATE_PLAIN"] == "kept"
    assert out["HOME"] == "/home/x"
    assert out["KIMIX_CONFIG_PATH"] == "kept-by-design", "KIMIX_* is a safe prefix"
    assert out["PATH"].endswith("/usr/bin")
    assert base["GATE_API_KEY"] == "secret-value", "input must not be mutated"


def test_rg_bin_path_still_puts_the_share_bin_first() -> None:
    out = _env_with_rg_bin_path({"PATH": "/usr/bin"})
    assert out["PATH"].startswith(str(_share_bin()))
    assert "/usr/bin" in out["PATH"]


def _share_bin() -> Path:
    from kimi_cli.share import get_share_dir

    return get_share_dir() / "bin"


def test_scrub_child_env_is_idempotent() -> None:
    env = {"PATH": "/x", "MY_TOKEN": "t", "PLAIN": "p"}
    once = scrub_child_env(env)
    twice = scrub_child_env(dict(once))
    assert once == twice


@pytest.mark.asyncio
async def test_child_process_cannot_see_a_secret_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: the real child env must not contain a credential-looking var."""
    import asyncio

    monkeypatch.setenv("GATE_API_KEY", "gate-secret")
    monkeypatch.setenv("GATE_PLAIN", "plain-value")

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _ENV_PROBE,
        env=_env_with_rg_bin_path(dict(os.environ)),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await process.communicate()
    text = out.decode("utf-8", errors="replace")
    assert "GATE_API_KEY" not in text
    assert "GATE_PLAIN" in text
