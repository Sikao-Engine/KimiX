#!/usr/bin/env python3
"""dist.py — build self-contained, offline-runnable kimix bundles.

Produces two bundles::

    dist/win/     Windows x64 bundle  (launcher: kimix.cmd)
    dist/ubuntu/  Linux x86_64 bundle (launcher: kimix)

Each bundle carries everything the target user needs:

* a private CPython interpreter (python-build-standalone, CPython 3.14 x64);
* every runtime dependency pinned by ``uv.lock`` (main deps + ``office`` and
  ``image_process`` extras) — no ``python``/``pip``/``uv`` on the user machine;
* the compiled kimix native runtime (``runtime_py.pyd`` / ``runtime_py.so``)
  plus the pure-Python ``kimix_native`` shim, staged into ``<bundle>/bin``;
* the ``rg`` (ripgrep) and ``rtk`` helper binaries under ``<bundle>/bundled/bin``;
  the launchers copy them into ``~/.kimi/bin`` on first run (offline copy, only
  when missing — that is the only directory the runtime looks them up in);
* launchers that wire up ``KIMIX_NATIVE_PATH`` and start ``python -m kimix``.

The target user is assumed to be *fully offline*: nothing is downloaded when
the bundle runs.  All downloading happens here, at package time (when the
packager still has network).  Downloads are cached under ``.dist_cache/`` so
re-runs are fast and a fully offline re-pack is possible as long as the cache
is warm.

Usage::

    python dist.py                 # build dist/win + dist/ubuntu (Windows host)
    python dist.py --platform win  # build only dist/win
    python dist.py --platform ubuntu
    python dist.py --clean         # wipe the target bundle dir(s) first
    python dist.py --with-playwright   # also bundle chromium (large; optional)

The ``ubuntu`` target can be built two ways:

* natively, when dist.py itself runs on Linux (used inside WSL);
* from a Windows host, by re-executing this same script inside WSL
  (``wsl -e python3 dist.py --platform ubuntu``) so dependency resolution and
  installation run against real Linux wheels instead of fragile cross-platform
  ``pip download`` tag emulation.

The script only uses the standard library so it can run under the WSL system
Python (3.10+) as well as the project venv (3.14).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST_DIR = ROOT / "dist"
CACHE_DIR = ROOT / ".dist_cache"

# CPython series required by pyproject.toml (requires-python = ">=3.14").
PYTHON_SERIES = "3.14"

# kimix native runtime release (mirrors install.py).
NATIVE_VERSION = (ROOT / "KIMIX_NATIVE_VERSION").read_text(encoding="utf-8").strip()
NATIVE_RELEASE_URL = "https://github.com/Sikao-Engine/KimiX-native/releases/download/Release"

# Helper binary versions (mirror kimi_cli/_ripgrep_common.py & _rtk_common.py).
RG_VERSION = "15.2.0"
RTK_VERSION = "0.49.0"

# python-build-standalone (astral) release assets.
PBS_API_URL = "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"

# Per-target constants.  ``family`` is the uv/python platform family; the
# bundle is produced on a host whose family matches (Windows host for win,
# Linux host — usually WSL — for ubuntu).
TARGETS = {
    "win": {
        "family": "windows",
        "pbs_target": "x86_64-pc-windows-msvc",
        "python_subdir": "python",
        "python_exe": "python.exe",
        "native_archive": "kimix_base-windows-x64-{version}.zip",
        "native_mod": "runtime_py.pyd",
        "bin_ext": ".exe",  # helper binary suffix (rg.exe / rtk.exe)
        "rg_target": "x86_64-pc-windows-msvc",
        "rtk_target": "x86_64-pc-windows-msvc",
        "launcher": "kimix.cmd",
    },
    "ubuntu": {
        "family": "linux",
        "pbs_target": "x86_64-unknown-linux-gnu",
        "python_subdir": "python",
        "python_exe": "python3",  # resolved from python/bin/python3
        "native_archive": "kimix_base-linux-x64-{version}.zip",
        "native_mod": "runtime_py.so",
        "bin_ext": "",  # rg / rtk
        "rg_target": "x86_64-unknown-linux-musl",
        "rtk_target": "x86_64-unknown-linux-musl",
        "launcher": "kimix",
    },
}


# ---------------------------------------------------------------------------
# small utilities
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    print(f"[dist] {msg}", flush=True)


def fail(msg: str) -> None:
    raise RuntimeError(msg)


def run(cmd: list[str], cwd: Path | None = None) -> None:
    """Run *cmd*, streaming output; raise on non-zero exit."""
    log(f"$ {' '.join(str(c) for c in cmd)}" + (f"  (cwd={cwd})" if cwd else ""))
    rc = subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None).returncode
    if rc != 0:
        fail(f"command failed with exit code {rc}: {cmd[0]}")


def _request(url: str, timeout: float = 60.0) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": "kimi-agent-dist/1.0"})


def download(url: str, dest: Path, retries: int = 3) -> Path:
    """Download *url* to *dest* (with retries); *dest* acts as a cache."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 0:
        log(f"cache hit: {dest.name} ({dest.stat().st_size // (1024 * 1024)} MiB)")
        return dest
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            log(f"downloading {url} (attempt {attempt}/{retries})")
            with urllib.request.urlopen(_request(url, timeout=300), timeout=300) as resp:
                tmp = dest.with_suffix(dest.suffix + ".part")
                with open(tmp, "wb") as fh:
                    shutil.copyfileobj(resp, fh, 64 * 1024)
                tmp.replace(dest)
            log(f"saved {dest.name} ({dest.stat().st_size // (1024 * 1024)} MiB)")
            return dest
        except Exception as exc:  # noqa: BLE001 - network errors vary wildly
            last = exc
            log(f"download failed: {exc}")
            if attempt < retries:
                time.sleep(2.0 * attempt)
    fail(f"could not download {url}: {last}")


def gh_json(url: str) -> dict:
    with urllib.request.urlopen(_request(url), timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# URL / naming helpers (pure — unit tested)
# ---------------------------------------------------------------------------


def native_archive_name(key: str) -> str:
    return TARGETS[key]["native_archive"].format(version=NATIVE_VERSION)


def native_archive_url(key: str) -> str:
    return f"{NATIVE_RELEASE_URL}/{native_archive_name(key)}"


def rg_url(key: str) -> str:
    target = TARGETS[key]["rg_target"]
    ext = "zip" if "windows" in target else "tar.gz"
    return (
        f"https://github.com/BurntSushi/ripgrep/releases/download/"
        f"{RG_VERSION}/ripgrep-{RG_VERSION}-{target}.{ext}"
    )


def rtk_url(key: str) -> str:
    target = TARGETS[key]["rtk_target"]
    ext = "zip" if "windows" in target else "tar.gz"
    return f"https://github.com/rtk-ai/rtk/releases/download/v{RTK_VERSION}/rtk-{target}.{ext}"


def pbs_asset_regex(key: str) -> str:
    target = TARGETS[key]["pbs_target"]
    return rf"^cpython-{re.escape(PYTHON_SERIES)}\.\d+\+\d+-{re.escape(target)}-install_only\.tar\.gz$"


def pick_pbs_asset(assets: list[dict], key: str) -> str | None:
    """Pick the newest ``install_only`` CPython asset for *key* from a release's assets.

    Asset names look like ``cpython-3.14.1+20251215-x86_64-pc-windows-msvc-install_only.tar.gz``;
    sorting by name orders by CPython version then build date.
    """
    pattern = re.compile(pbs_asset_regex(key))
    names = sorted(
        a.get("name", "") for a in assets if pattern.match(a.get("name", ""))
    )
    return names[-1] if names else None


def select_native_artifacts(names: list[str], key: str) -> list[str]:
    """From the files inside an extracted kimix_base archive, select the ones to stage.

    The compiled extension (``runtime_py.pyd`` / ``runtime_py.so``) plus any
    ``*.dll`` runtime dependencies on Windows (mirrors install.py
    ``_stage_native_files``).
    """
    mod = TARGETS[key]["native_mod"]
    picked = [n for n in names if n == mod or (key == "win" and n.lower().endswith(".dll"))]
    return sorted(picked)


def win_path_to_wsl(path: Path) -> str:
    """Translate a Windows path to its WSL ``/mnt/<drive>`` form."""
    s = str(path.resolve())
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", s)
    if m:
        return "/mnt/" + m.group(1).lower() + "/" + m.group(2).replace("\\", "/")
    return s.replace("\\", "/")


# ---------------------------------------------------------------------------
# archive extraction
# ---------------------------------------------------------------------------


def _extract_archive(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
    else:
        with tarfile.open(archive, "r:gz") as tf:
            tf.extractall(dest)


def extract_binary_from_archive(archive: Path, bin_name: str, destination: Path) -> None:
    """Extract the single binary *bin_name* from a rg/rtk release archive."""
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            member = next(
                (n for n in zf.namelist() if Path(n).name == bin_name),
                None,
            )
            if member is None:
                fail(f"{bin_name} not found in {archive.name}")
            with zf.open(member) as src, open(destination, "wb") as dst:
                shutil.copyfileobj(src, dst)
    else:
        with tarfile.open(archive, "r:gz") as tf:
            member = next(
                (m for m in tf.getmembers() if Path(m.name).name == bin_name),
                None,
            )
            if member is None:
                fail(f"{bin_name} not found in {archive.name}")
            src = tf.extractfile(member)
            if src is None:
                fail(f"could not read {bin_name} from {archive.name}")
            with open(destination, "wb") as dst:
                shutil.copyfileobj(src, dst)
    if not _is_windows_family():  # keep exec bit on linux/mac hosts
        destination.chmod(destination.stat().st_mode | 0o755)


# ---------------------------------------------------------------------------
# platform detection
# ---------------------------------------------------------------------------


def _is_windows_family() -> bool:
    return sys.platform == "win32"


def host_family() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    fail(f"unsupported host platform: {sys.platform}")


def python_exe_path(out: Path, key: str) -> Path:
    t = TARGETS[key]
    if key == "win":
        return out / t["python_subdir"] / t["python_exe"]
    return out / t["python_subdir"] / "bin" / t["python_exe"]


# ---------------------------------------------------------------------------
# build steps
# ---------------------------------------------------------------------------


def fetch_python_archive(key: str) -> Path:
    """Download (or reuse the cached) python-build-standalone archive for *key*."""
    env_url = os.environ.get(f"KIMIX_PYTHON_BUILD_URL_{key.upper()}")
    if env_url:
        return download(env_url, CACHE_DIR / "python" / Path(env_url).name)
    release = gh_json(PBS_API_URL)
    asset = pick_pbs_asset(release.get("assets", []), key)
    if asset is None:
        fail(
            f"no CPython {PYTHON_SERIES} install_only asset for {TARGETS[key]['pbs_target']} "
            f"in python-build-standalone release {release.get('tag_name')!r}"
        )
    url = next(
        a["browser_download_url"]
        for a in release["assets"]
        if a.get("name") == asset
    )
    return download(url, CACHE_DIR / "python" / asset)


def install_python(key: str, out: Path) -> Path:
    """Install the private CPython into ``<out>/python``."""
    dest = out / TARGETS[key]["python_subdir"]
    exe = python_exe_path(out, key)
    if exe.is_file():
        log(f"python already installed at {dest}")
        return exe
    archive = fetch_python_archive(key)
    # The tarball ships a bundled terminfo database whose entries clash
    # case-insensitively (LFT-PC850 vs lft, x/ vs X/, self-referential links)
    # and cannot live on a Windows drive (drvfs). It is non-essential (the
    # curses module falls back to the system terminfo), so drop it before
    # copying to the bundle.
    log(f"extracting {archive.name} -> {dest}")
    if dest.exists():
        shutil.rmtree(dest)
    with tempfile.TemporaryDirectory(prefix="kimi-pbs-") as tmp:
        tmp_path = Path(tmp)
        _extract_archive(archive, tmp_path)
        entries = [p for p in tmp_path.iterdir()]
        src = entries[0] if len(entries) == 1 and entries[0].is_dir() else tmp_path
        terminfo = src / "share" / "terminfo"
        if terminfo.exists():
            shutil.rmtree(terminfo, ignore_errors=True)
        shutil.copytree(src, dest, symlinks=True, dirs_exist_ok=True)
    if not exe.is_file():
        fail(f"python extraction failed: {exe} missing")
    if key != "win":
        exe.chmod(exe.stat().st_mode | 0o755)
    log(f"python installed: {exe}")
    return exe


def build_project_wheels() -> Path:
    """Build the kimix and kimi-cli-x wheels into the shared wheelhouse."""
    wheelhouse = CACHE_DIR / "wheelhouse"
    wheelhouse.mkdir(parents=True, exist_ok=True)
    existing = sorted(wheelhouse.glob("*.whl"))
    if existing:
        log(f"wheelhouse cache hit: {[p.name for p in existing]}")
        return wheelhouse
    log("building project wheels (kimix + kimi-cli-x) ...")
    run(["uv", "build", "--wheel", "--out-dir", str(wheelhouse)], cwd=ROOT)
    run(["uv", "build", "--wheel", "--out-dir", str(wheelhouse)], cwd=ROOT / "kimi-cli")
    built = sorted(wheelhouse.glob("*.whl"))
    if len(built) < 2:
        fail(f"expected kimix + kimi-cli-x wheels in {wheelhouse}, found: {built}")
    return wheelhouse


def export_requirements(key: str) -> Path:
    """Export pinned runtime requirements (main + office + image_process) for *key*.

    Must run on a host whose platform family matches the target so environment
    markers (e.g. ``uvloop`` on Linux, ``pyobjc`` on macOS) are evaluated for
    the right platform.
    """
    reqs = CACHE_DIR / f"requirements-{key}.txt"
    if reqs.is_file():
        log(f"requirements cache hit: {reqs.name}")
        return reqs
    log("exporting pinned requirements from uv.lock ...")
    run(
        [
            "uv", "export", "--frozen", "--no-dev",
            "--extra", "office", "--extra", "image_process",
            "--no-emit-workspace",
            "--no-annotate", "--no-header", "--no-hashes",
            "-o", str(reqs),
        ],
        cwd=ROOT,
    )
    if not reqs.is_file() or not reqs.read_text(encoding="utf-8").strip():
        fail(f"uv export produced no requirements at {reqs}")
    return reqs


def install_requirements(py: Path, reqs: Path, wheelhouse: Path) -> None:
    """Install the pinned requirements + project wheels into the bundle python."""
    log("installing dependencies into bundle python (this is the slow step) ...")
    run(
        [
            "uv", "pip", "install", "--python", str(py),
            "-r", str(reqs),
            *[str(w) for w in sorted(wheelhouse.glob("*.whl"))],
        ],
        cwd=ROOT,
    )


def stage_native(key: str, out: Path) -> None:
    """Stage ``<out>/bin`` with the kimix_native shim + compiled runtime."""
    bin_dir = out / "bin"
    if (bin_dir / TARGETS[key]["native_mod"]).is_file():
        log("native runtime already staged")
        return
    archive = download(native_archive_url(key), CACHE_DIR / "native" / native_archive_name(key))
    with tempfile.TemporaryDirectory(prefix="kimi-native-") as tmp:
        tmp_path = Path(tmp)
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(tmp_path)
        # Locate the directory holding the compiled extension (archive may nest it).
        mod = TARGETS[key]["native_mod"]
        holders = sorted(
            d for d in [tmp_path, *[p for p in tmp_path.rglob("*") if p.is_dir()]]
            if (d / mod).is_file()
        )
        if not holders:
            fail(f"{mod} not found inside {archive.name}")
        src = holders[0]
        picked = select_native_artifacts(sorted(p.name for p in src.iterdir() if p.is_file()), key)
        if not picked:
            fail(f"no native artifacts selected from {archive.name}")
        bin_dir.mkdir(parents=True, exist_ok=True)
        for name in picked:
            shutil.copy2(src / name, bin_dir / name)
    # The pure-Python shim is tracked in the repo at bin/kimix_native; ship a copy
    # so <out>/bin is a complete standalone native home (KIMIX_NATIVE_PATH target).
    shim_src = ROOT / "bin" / "kimix_native"
    shim_dst = bin_dir / "kimix_native"
    if shim_dst.exists():
        shutil.rmtree(shim_dst)
    shutil.copytree(shim_src, shim_dst)
    # Version marker read by the shim's version gate (repo root = <out>).
    shutil.copy2(ROOT / "KIMIX_NATIVE_VERSION", out / "KIMIX_NATIVE_VERSION")
    log(f"native runtime staged: {sorted(p.name for p in bin_dir.iterdir())}")


def stage_tools(key: str, out: Path) -> None:
    """Stage ``rg`` and ``rtk`` binaries under ``<out>/bundled/bin``."""
    tools_dir = out / "bundled" / "bin"
    ext = TARGETS[key]["bin_ext"]
    names = [f"rg{ext}", f"rtk{ext}"]
    if all((tools_dir / n).is_file() for n in names):
        log("helper tools already staged")
        return
    tools_dir.mkdir(parents=True, exist_ok=True)
    rg_archive = download(rg_url(key), CACHE_DIR / "tools" / Path(rg_url(key)).name)
    extract_binary_from_archive(rg_archive, f"rg{ext}", tools_dir / f"rg{ext}")
    rtk_archive = download(rtk_url(key), CACHE_DIR / "tools" / Path(rtk_url(key)).name)
    extract_binary_from_archive(rtk_archive, f"rtk{ext}", tools_dir / f"rtk{ext}")
    log(f"helper tools staged: {names}")


# ---------------------------------------------------------------------------
# launchers (pure string builders — unit tested)
# ---------------------------------------------------------------------------


def windows_launcher() -> str:
    return r"""@echo off
rem Kimix self-contained launcher (offline bundle — no system Python needed).
setlocal
set "KIMIX_HOME=%~dp0"
if not defined KIMIX_NATIVE_PATH set "KIMIX_NATIVE_PATH=%KIMIX_HOME%bin"
set "PATH=%KIMIX_HOME%bundled\bin;%PATH%"

rem Bootstrap rg/rtk into the shared kimi bin dir (offline copy, only if missing).
if not exist "%USERPROFILE%\.kimi\bin" mkdir "%USERPROFILE%\.kimi\bin" >NUL 2>&1
for %%B in (rg.exe rtk.exe) do (
    if not exist "%USERPROFILE%\.kimi\bin\%%B" (
        if exist "%KIMIX_HOME%bundled\bin\%%B" copy /y "%KIMIX_HOME%bundled\bin\%%B" "%USERPROFILE%\.kimi\bin\%%B" >NUL
    )
)

"%KIMIX_HOME%python\python.exe" -m kimix %*
exit /b %ERRORLEVEL%
"""


def linux_launcher() -> str:
    return """#!/usr/bin/env bash
# Kimix self-contained launcher (offline bundle — no system Python needed).
set -e
KIMIX_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export KIMIX_NATIVE_PATH="${KIMIX_NATIVE_PATH:-$KIMIX_HOME/bin}"
export PATH="$KIMIX_HOME/bundled/bin:$PATH"

# Bootstrap rg/rtk into the shared kimi bin dir (offline copy, only if missing).
SHARE_BIN="${KIMI_SHARE_DIR:-$HOME/.kimi}/bin"
mkdir -p "$SHARE_BIN"
for b in rg rtk; do
    if [ ! -f "$SHARE_BIN/$b" ] && [ -f "$KIMIX_HOME/bundled/bin/$b" ]; then
        cp "$KIMIX_HOME/bundled/bin/$b" "$SHARE_BIN/$b"
        chmod +x "$SHARE_BIN/$b" 2>/dev/null || true
    fi
done

exec "$KIMIX_HOME/python/bin/python3" -m kimix "$@"
"""


def launcher_script(key: str) -> str:
    return windows_launcher() if key == "win" else linux_launcher()


BUNDLE_README = """\
Kimix offline bundle
====================

Run the CLI with the launcher in this directory:

    Windows:  kimix.cmd --help
    Linux:    ./kimix --help

The bundle is fully self-contained and offline:
  * python/          private CPython (no system Python required)
  * bin/             compiled kimix native runtime + shim (KIMIX_NATIVE_PATH);
                     when the compiled build does not match this CPython the
                     runtime automatically falls back to pure Python
  * bundled/bin/     rg (ripgrep) and rtk helper binaries; the launcher copies
                     them into ~/.kimi/bin on first run (offline, if missing)

Subcommands: serve / gui / ssecli / mcp (see `kimix --help`).

Optional: set KIMI_API_KEY / KIMIX_API_KEY before starting, or run `/init`.
"""


def write_launcher(key: str, out: Path) -> None:
    launcher = out / TARGETS[key]["launcher"]
    launcher.write_text(launcher_script(key), encoding="utf-8", newline="\n")
    if key != "win":
        launcher.chmod(launcher.stat().st_mode | 0o755)
    (out / "README.txt").write_text(BUNDLE_README, encoding="utf-8")


# ---------------------------------------------------------------------------
# smoke tests
# ---------------------------------------------------------------------------


def _check(desc: str, cmd: list[str], env: dict | None = None, timeout: int = 300) -> None:
    log(f"smoke: {desc}")
    result = subprocess.run(
        [str(c) for c in cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        print(result.stdout[-3000:])
        print(result.stderr[-3000:], file=sys.stderr)
        fail(f"smoke test failed: {desc} (exit {result.returncode})")


def _probe(cmd: list[str], env: dict | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(c) for c in cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        encoding="utf-8",
        errors="replace",
    )


def smoke(key: str, out: Path) -> None:
    """Verify the bundle imports and launches without any system Python."""
    py = python_exe_path(out, key)
    bin_dir = out / "bin"
    env = os.environ.copy()
    env["KIMIX_NATIVE_PATH"] = str(bin_dir)

    _check("python works", [py, "-c", "import sys; print(sys.version)"])
    _check(
        "import kimix + kimi_cli",
        [py, "-c", "import kimix, kimi_cli; print('imports OK')"],
    )

    # Native runtime: the compiled extension is an OPTIONAL acceleration with a
    # pure-Python fallback. Hard-require it only where the upstream release is
    # known to match the bundled CPython (win). Other platforms may ship an
    # extension compiled for a different CPython (e.g. the KimiX-native linux
    # build targets 3.10 while this bundle ships 3.14) — that is a graceful
    # fallback, not a packaging failure.
    native_probe = (
        "import sys; sys.path.insert(0, %r); import kimix_native as k; "
        "print('reason:', k.DISABLE_REASON); "
        "sys.exit(0 if k._native is not None else 3)" % str(bin_dir)
    )
    result = _probe([py, "-c", native_probe], env=dict(env, KIMIX_NATIVE="1"))
    if result.returncode == 0:
        log(f"smoke: native extension loads ({result.stdout.strip()})")
    elif key == "win":
        print(result.stdout[-2000:])
        print(result.stderr[-2000:], file=sys.stderr)
        fail("smoke test failed: native extension did not load on win bundle")
    else:
        reason = (result.stdout + result.stderr).strip().splitlines()
        log(f"smoke: native acceleration unavailable ({reason[-1] if reason else 'unknown'}); "
            "bundle will use the pure-Python fallback")

    loader_probe = (
        "from kimi_cli import native_loader as n; "
        "print('native_loader OK, NATIVE_AVAILABLE =', n.NATIVE_AVAILABLE)"
    )
    _check("native_loader resolves (native or fallback)", [py, "-c", loader_probe], env=env)

    launcher = out / TARGETS[key]["launcher"]
    if key == "win":
        _check("kimix.cmd --help", ["cmd", "/c", str(launcher), "--help"])
    else:
        _check("kimix --help", [str(launcher), "--help"])

    rg = out / "bundled" / "bin" / f"rg{TARGETS[key]['bin_ext']}"
    _check("rg --version", [rg, "--version"])
    log("all smoke tests passed")


# ---------------------------------------------------------------------------
# top-level build
# ---------------------------------------------------------------------------


def build(key: str, clean: bool, with_playwright: bool) -> Path:
    target = TARGETS[key]
    if host_family() != target["family"]:
        fail(
            f"target {key!r} must be built on a {target['family']} host "
            f"(current host family: {host_family()})"
        )
    out = DIST_DIR / key
    if clean and out.exists():
        log(f"cleaning {out}")
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    py = install_python(key, out)
    wheelhouse = build_project_wheels()
    reqs = export_requirements(key)
    install_requirements(py, reqs, wheelhouse)
    stage_native(key, out)
    stage_tools(key, out)
    write_launcher(key, out)

    if with_playwright:
        pw_dir = out / "pw-browsers"
        pw_env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(pw_dir))
        log("bundling chromium via playwright install (large download) ...")
        subprocess.run(
            [str(py), "-m", "playwright", "install", "chromium"],
            env=pw_env,
            check=True,
        )

    smoke(key, out)
    log(f"✅ {key} bundle ready at {out}")
    return out


def build_via_wsl(args: argparse.Namespace) -> None:
    """Re-execute this script inside WSL to build the ubuntu bundle natively."""
    wsl = shutil.which("wsl")
    if wsl is None:
        fail(
            "target 'ubuntu' from a Windows host requires WSL ('wsl' not found on PATH). "
            "Run this script inside WSL/Linux instead: python3 dist.py --platform ubuntu"
        )
    child_args = ["-e", "python3", win_path_to_wsl(Path(__file__)), "--platform", "ubuntu"]
    if args.clean:
        child_args.append("--clean")
    if args.with_playwright:
        child_args.append("--with-playwright")
    log(f"re-executing in WSL: wsl {' '.join(child_args)}")
    rc = subprocess.run([wsl, *child_args]).returncode
    if rc != 0:
        fail(f"WSL build failed with exit code {rc}")
    log("✅ ubuntu bundle ready (built in WSL)")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Package kimix into offline dist bundles.")
    parser.add_argument(
        "--platform",
        choices=["win", "ubuntu", "all"],
        default="all",
        help="bundle to build (default: all that this host can build)",
    )
    parser.add_argument("--clean", action="store_true", help="wipe the target bundle dir first")
    parser.add_argument(
        "--with-playwright",
        action="store_true",
        help="also bundle the chromium browser for the web-fetch playwright fallback (large)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.platform == "all":
        keys = ["win", "ubuntu"] if host_family() == "windows" else ["ubuntu"]
    else:
        keys = [args.platform]

    for key in keys:
        if key == "ubuntu" and host_family() == "windows":
            build_via_wsl(args)
        else:
            build(key, clean=args.clean, with_playwright=args.with_playwright)

    log("done.")


if __name__ == "__main__":
    main()
