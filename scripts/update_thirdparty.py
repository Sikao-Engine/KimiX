#!/usr/bin/env python3
"""Check and update third-party binary version strings in the project.

For each known third-party tool (ripgrep, rtk, git):
1. Fetches the latest release tag from GitHub.
2. Compares it to the version currently declared in the source code.
3. If a newer version exists, prints a diff and asks the user whether to
   update the version string in the relevant source file.

Usage:
    python scripts/update_thirdparty.py              # interactive
    python scripts/update_thirdparty.py --yes        # auto-approve all
    python scripts/update_thirdparty.py --check-only # just report, no edits
"""

from __future__ import annotations

import os
import re
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# ── Project root ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ── Data model ────────────────────────────────────────────────────────────────
@dataclass
class ThirdPartyTool:
    """Describes a third-party tool whose version is pinned in source."""

    name: str
    """Human-readable name (e.g. "ripgrep")."""

    github_repo: str
    """GitHub ``owner/repo`` string."""

    version_file: str
    """Path relative to PROJECT_ROOT of the source file that declares the
    version string."""

    version_var: str
    """Name of the version variable in that file (e.g. ``RG_VERSION``)."""

    version_pattern: str
    """Regex pattern to match the version assignment line. Must contain a
    ``{var}`` placeholder that will be replaced with *version_var*."""

    version_clean: Callable[[str], str] | None = None
    """Optional transform to strip a prefix (e.g. ``v``) from the GitHub tag
    before comparing / writing."""

    update_message: str = ""
    """Optional extra message to print when a newer version is found."""

    tag_pattern: str | None = None
    """Optional regex to filter GitHub tags. If set, the latest tag matching
    this pattern is used. The regex's first capture group is used as the
    version string."""

    extra_files: list[str] = field(default_factory=list)
    """Additional files (relative to PROJECT_ROOT) that contain the same
    version string and should also be updated."""


# ── Tool definitions ──────────────────────────────────────────────────────────
# Each entry knows which repo to query, which file+variable to update, and how
# to parse the version from a GitHub tag.

TOOLS: list[ThirdPartyTool] = [
    ThirdPartyTool(
        name="ripgrep",
        github_repo="BurntSushi/ripgrep",
        version_file="kimi-cli/src/kimi_cli/_ripgrep_common.py",
        version_var="RG_VERSION",
        version_pattern=r'''^{var}\s*=\s*"(?P<ver>[^"]+)"''',
        # Tag is e.g. "15.2.0" — already clean
    ),
    ThirdPartyTool(
        name="rtk",
        github_repo="rtk-ai/rtk",
        version_file="kimi-cli/src/kimi_cli/_rtk_common.py",
        version_var="RTK_VERSION",
        version_pattern=r'''^{var}\s*=\s*"(?P<ver>[^"]+)"''',
        # Tag is e.g. "v0.43.0" — strip leading "v"
        version_clean=lambda tag: tag.lstrip("v"),
    ),
    ThirdPartyTool(
        name="Git for Windows",
        github_repo="git-for-windows/git",
        version_file="scripts/install_git.py",
        version_var="GIT_VERSION",
        version_pattern=r'''^{var}\s*:\s*str\s*=\s*"(?P<ver>[^"]+)"''',
        # Tag is e.g. "v2.54.0.windows.1" — strip ".windows.N" suffix
        version_clean=lambda tag: re.sub(r"\.windows\.\d+$", "", tag.lstrip("v")),
        # Use the tag pattern to extract just the semver part
        tag_pattern=r"^v(\d+\.\d+\.\d+)\.windows\.\d+$",
    ),
]


# ── GitHub helpers ────────────────────────────────────────────────────────────

def _get_github_token() -> str | None:
    """Return a GitHub token from the environment, or ``None``."""
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


def _github_api(url: str, accept: str = "application/vnd.github+json") -> dict | list | None:
    """Call a GitHub API endpoint and return the parsed JSON response."""
    headers: dict[str, str] = {
        "Accept": accept,
        "User-Agent": "kimix-thirdparty-updater",
    }
    token = _get_github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read().decode("utf-8")
        import json

        return json.loads(data)
    except Exception as exc:
        print(f"  ⚠️  GitHub API request failed: {exc}")
        return None


def _get_latest_tag(repo: str, pattern: str | None = None) -> str | None:
    """Get the latest release tag name for *repo*.

    Uses the ``/releases/latest`` endpoint first; falls back to listing
    recent releases if the latest endpoint is not available.
    If *pattern* is given, the tag must match — the first capture group
    of the pattern is used as the version string.
    """
    # Try /releases/latest first
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    data = _github_api(url)
    if data and isinstance(data, dict):
        tag = data.get("tag_name", "")
        if tag:
            if pattern:
                m = re.match(pattern, tag)
                if m:
                    return m.group(1)
            return tag

    # Fallback: list recent releases
    url = f"https://api.github.com/repos/{repo}/releases?per_page=5"
    data = _github_api(url)
    if data and isinstance(data, list):
        for release in data:
            tag = release.get("tag_name", "")
            if tag and not release.get("prerelease", False) and not release.get("draft", False):
                if pattern:
                    m = re.match(pattern, tag)
                    if m:
                        return m.group(1)
                return tag

    return None


def _get_fallback_tag_via_redirect(repo: str) -> str | None:
    """Resolve the ``/releases/latest`` redirect URL to get the latest tag.

    This is a fallback when the GitHub API is rate-limited.
    """
    url = f"https://github.com/{repo}/releases/latest"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "kimix-thirdparty-updater"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            final_url = resp.url
        tag = final_url.rstrip("/").rsplit("/", 1)[-1]
        return tag
    except Exception as exc:
        print(f"  ⚠️  Redirect fallback failed: {exc}")
        return None


# ── File helpers ──────────────────────────────────────────────────────────────

def _read_current_version(tool: ThirdPartyTool) -> str | None:
    """Read the current version string from the source file."""
    filepath = PROJECT_ROOT / tool.version_file
    if not filepath.is_file():
        print(f"  ❌ File not found: {tool.version_file}")
        return None

    pattern = tool.version_pattern.format(var=tool.version_var)
    content = filepath.read_text(encoding="utf-8")
    for line in content.splitlines():
        m = re.match(pattern, line)
        if m:
            return m.group("ver")
    return None


def _update_version_in_file(filepath: Path, tool: ThirdPartyTool, new_version: str) -> bool:
    """Replace the version string in *filepath* with *new_version*.

    Returns ``True`` if the file was modified.
    """
    pattern = tool.version_pattern.format(var=tool.version_var)
    content = filepath.read_text(encoding="utf-8")

    def _replace(m: re.Match) -> str:
        return m.group(0).replace(m.group("ver"), new_version)

    new_content, count = re.subn(pattern, _replace, content, count=1, flags=re.MULTILINE)
    if count == 0:
        print(f"  ❌ Could not find version pattern in {filepath.name}")
        return False

    filepath.write_text(new_content, encoding="utf-8")
    return True


# ── User interaction ──────────────────────────────────────────────────────────

def _ask_yes_no(prompt: str, default: bool = True) -> bool:
    """Ask the user a yes/no question.

    In non-interactive environments the *default* value is returned.
    """
    if not sys.stdin.isatty():
        return default

    suffix = " [Y/n]: " if default else " [y/N]: "
    try:
        answer = input(prompt + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return default

    if not answer:
        return default
    return answer in ("y", "yes")


# ── Main logic ────────────────────────────────────────────────────────────────

def check_and_update(tool: ThirdPartyTool, auto_yes: bool, check_only: bool) -> int:
    """Check a single tool and optionally update its version string.

    Returns 0 if up-to-date or successfully updated, 1 if an update is
    available but was declined or failed.
    """
    print(f"\n── {tool.name} ──")

    # 1. Read current version
    current = _read_current_version(tool)
    if current is None:
        print(f"  ⚠️  Could not read current version from {tool.version_file}.")
        return 1

    print(f"  Current version: {current}")

    # 2. Fetch latest version from GitHub
    latest_tag = _get_latest_tag(tool.github_repo, tool.tag_pattern)
    if latest_tag is None:
        latest_tag = _get_fallback_tag_via_redirect(tool.github_repo)
        if latest_tag is None:
            print(f"  ❌ Could not fetch latest version from GitHub.")
            return 1

    # Clean the tag if needed
    latest = tool.version_clean(latest_tag) if tool.version_clean else latest_tag
    if not latest:
        latest = latest_tag

    print(f"  Latest version:  {latest}")

    # 3. Compare
    if current == latest:
        print(f"  ✅ Up-to-date.")
        return 0

    print(f"  ⬆️  Update available: {current} → {latest}")
    if tool.update_message:
        print(f"     {tool.update_message}")

    if check_only:
        print(f"  ⏭️  Check-only mode; skipping update.")
        return 1

    # 4. Ask user
    if not auto_yes and not _ask_yes_no(f"  Update {tool.name} to {latest}?", default=True):
        print(f"  ⏭️  Skipped.")
        return 1

    # 5. Update primary file
    primary = PROJECT_ROOT / tool.version_file
    if not _update_version_in_file(primary, tool, latest):
        return 1
    print(f"  ✅ Updated {tool.version_file}")

    # 6. Update extra files (same variable pattern)
    for extra_rel in tool.extra_files:
        extra_path = PROJECT_ROOT / extra_rel
        if extra_path.is_file():
            if _update_version_in_file(extra_path, tool, latest):
                print(f"  ✅ Updated {extra_rel}")

    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Check and update third-party binary version strings.",
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        dest="auto_yes",
        help="Auto-approve all updates without prompting.",
    )
    parser.add_argument(
        "--check-only", "-c",
        action="store_true",
        help="Only check for newer versions; do not update.",
    )
    parser.add_argument(
        "tools",
        nargs="*",
        choices=[t.name for t in TOOLS],
        help="Specific tools to check (default: all).",
    )
    args = parser.parse_args()

    # Filter tools if specific ones requested
    if args.tools:
        tools_to_check = [t for t in TOOLS if t.name in args.tools]
    else:
        tools_to_check = list(TOOLS)

    if not tools_to_check:
        print("No matching tools found.")
        return 1

    exit_code = 0
    for tool in tools_to_check:
        rc = check_and_update(tool, args.auto_yes, args.check_only)
        if rc != 0:
            exit_code = rc

    print()
    if exit_code == 0:
        print("✅ All tools are up-to-date or successfully updated.")
    else:
        print("⚠️  Some tools have updates available but were not applied.")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
