"""Cache-scoped write mode for read-only (researcher) child sessions.

The ``research`` tool spawns an anonymous read-only child session built from
``agent_readonly.json``.  That session now also owns the ``write``/``edit``
tools, but in a *special mode*: it may only ever produce **report artifacts**,
so every argument path must be a plain relative file name and is resolved under
``<work dir>/.kimix_cache/research/<session id>/`` — a per-session sandbox
inside the per-work-dir scratch directory.  Absolute paths, drive-qualified
paths, ``..`` escapes, URL/``conflict://`` schemes and symlinks that point
outside the sandbox are refused, so nothing outside it can be touched.

The mode is turned on per session through ``session.custom_data`` (see
:data:`CACHE_ONLY_WRITE_KEY`), so:

* the parent session (a normal read-write session) is completely unaffected;
* ``read_only=True`` runtimes (planner) stay blocked by the toolset guard —
  this module widens nothing there;
* the mode survives re-entrant tool calls because it lives on the session, not
  on a tool instance.

Every accepted write is recorded in :data:`CACHE_WRITTEN_FILES_KEY` as a
``.kimix_cache``-relative POSIX path, which is how the ``research`` tool reports
the artifacts the child produced back to the calling agent.  Recording is
best-effort and must never fail a write.

The per-session subdirectory matters: ``.kimix_cache`` also holds session state
(``<session id>/context.db``) and tool temp files (``tmp_<pid>/``).  A model
that writes ``report.md`` must not be able to clobber those, and two parallel
research runs must not overwrite each other's reports.
"""

from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath
from typing import Any

from kaos.path import KaosPath

#: Name of the per-work-dir scratch directory every cache-only write lands in.
CACHE_DIR_NAME = ".kimix_cache"
#: Subdirectory of :data:`CACHE_DIR_NAME` holding researcher report artifacts.
CACHE_SUBDIR_NAME = "research"
#: ``session.custom_data`` flag enabling the cache-only write mode.
CACHE_ONLY_WRITE_KEY = "cache_only_write"
#: ``session.custom_data`` key holding the cache-relative paths written so far.
CACHE_WRITTEN_FILES_KEY = "cache_written_files"

_SAFE_NAMESPACE_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)
_NAMESPACE_MAX_LEN = 64


def _real(path: KaosPath) -> KaosPath:
    """Return the physical path of *path* (symlinks and junctions resolved).

    ``KaosPath.canonical()`` only normalizes ``.``/``..``; a symlink inside the
    sandbox could otherwise point anywhere and still pass a prefix check.
    ``os.path.realpath`` resolves the existing prefix and leaves the missing
    tail untouched, so it also works for files that do not exist yet.
    """
    try:
        return KaosPath(os.path.realpath(str(path)))
    except Exception:  # pragma: no cover - defensive only
        return path.canonical()


def _safe_namespace(raw: str) -> str:
    """Reduce *raw* to a single path-safe directory name (never empty)."""
    cleaned = "".join(ch if ch in _SAFE_NAMESPACE_CHARS else "_" for ch in raw)
    cleaned = cleaned.strip("._-")[:_NAMESPACE_MAX_LEN]
    return cleaned or "default"


def cache_namespace(session: Any) -> str:
    """Return the sandbox namespace for *session* (its id, made path-safe)."""
    try:
        raw = str(getattr(session, "id", "") or "")
    except Exception:  # pragma: no cover - defensive only
        return "default"
    return _safe_namespace(raw)


def cache_base(work_dir: KaosPath) -> KaosPath:
    """Return ``<work dir>/.kimix_cache`` (physical path)."""
    return _real(work_dir / CACHE_DIR_NAME)


def cache_root(work_dir: KaosPath, namespace: str) -> KaosPath:
    """Return the per-session sandbox ``<work dir>/.kimix_cache/research/<ns>``."""
    return _real(cache_base(work_dir) / CACHE_SUBDIR_NAME / _safe_namespace(namespace))


def cache_only_write_enabled(session: Any) -> bool:
    """Return True when *session* may only write inside ``.kimix_cache/``.

    Tolerant by design: a session without ``custom_data`` (test doubles, legacy
    call sites) is a normal read-write session, never an error.
    """
    try:
        # ``is True`` (not truthiness): a Mock/legacy object must never be
        # mistaken for an opted-in session.
        return session.custom_data.get(CACHE_ONLY_WRITE_KEY) is True
    except Exception:
        return False


def enable_cache_only_write(session: Any, written_files: list[str] | None = None) -> list[str]:
    """Turn the cache-only write mode on for *session* and return its ledger.

    *written_files* lets the caller keep a reference to the ledger before the
    session exists (the ``research`` tool passes a list through
    ``custom_data=``), so it can report artifacts even after the child session
    has been closed and its ``custom_data`` dropped.
    """
    ledger = written_files if written_files is not None else []
    try:
        session.custom_data[CACHE_ONLY_WRITE_KEY] = True
        existing = session.custom_data.get(CACHE_WRITTEN_FILES_KEY)
        if isinstance(existing, list):
            ledger = existing
        session.custom_data[CACHE_WRITTEN_FILES_KEY] = ledger
    except Exception:
        pass
    return ledger


def _within_sandbox(path: KaosPath, root: KaosPath) -> bool:
    """True when *path* is inside *root* (both already physical paths)."""
    try:
        Path(str(path)).relative_to(Path(str(root)))
        return True
    except (ValueError, OSError):
        return False


def _strip_sandbox_prefix(raw: str, namespace: str) -> str:
    """Drop a sandbox prefix the model copied from an earlier tool result.

    The Reader prompt and every tool message spell paths as
    ``.kimix_cache/research/<id>/report.md``.  A model that passes that path
    back must land on the same file, not on a nested copy.
    """
    namespace = _safe_namespace(namespace)
    lowered = raw.lower()
    for prefix in (
        f"{CACHE_DIR_NAME}/{CACHE_SUBDIR_NAME}/{namespace}/",
        f"{CACHE_SUBDIR_NAME}/{namespace}/",
        f"{CACHE_DIR_NAME}/",
    ):
        if lowered.startswith(prefix.lower()) and len(raw) > len(prefix):
            return raw[len(prefix) :]
    return raw


def _is_plain_relative_name(raw: str) -> bool:
    """True when *raw* is a relative, drive-less path name (``a/b.md``).

    Windows path semantics are used on every platform on purpose: a manifest
    that grants this tool must refuse ``C:/x`` and ``\\\\server\\share\\x``
    no matter which OS the agent runs on.
    """
    if not raw or raw.startswith("~"):
        return False
    windows = PureWindowsPath(raw)
    return not windows.drive and not windows.root


def resolve_cache_only_path(
    raw_path: str, work_dir: KaosPath, namespace: str
) -> tuple[KaosPath | None, str | None]:
    """Resolve a cache-only argument path; return ``(path, error_message)``.

    Exactly one element is set.  The returned path is absolute, physical and
    guaranteed to be inside :func:`cache_root`.
    """
    # Backslashes are separators on Windows; treating them as separators on
    # every platform keeps the accepted grammar (and the refusal set) identical.
    raw = (raw_path or "").strip().replace("\\", "/")
    if not raw:
        return None, "File path cannot be empty."
    if "://" in raw:
        return None, (
            f"`{raw_path}` is not a plain file name. This session can only write "
            f"report files with a relative path inside `{CACHE_DIR_NAME}/`."
        )
    raw = _strip_sandbox_prefix(raw, namespace)
    if not _is_plain_relative_name(raw):
        return None, (
            f"`{raw_path}` is not a relative file name. This session can only "
            f"write relative paths, which are saved under `{CACHE_DIR_NAME}/`; "
            "absolute paths and paths outside the cache are refused."
        )
    root = cache_root(work_dir, namespace)
    try:
        resolved = _real((root / raw).canonical())
    except Exception as exc:  # never let a path oddity escape as a crash
        return None, f"Cannot resolve `{raw}` inside `{CACHE_DIR_NAME}/`: {exc}"
    if not _within_sandbox(resolved, root):
        return None, (
            f"`{raw}` escapes the `{CACHE_DIR_NAME}/` sandbox. Use a plain relative "
            "path such as `notes/findings.md`."
        )
    if Path(str(resolved)) == Path(str(root)):
        return None, (
            f"`{raw_path}` resolves to the `{CACHE_DIR_NAME}/` directory itself; "
            "pass a file name."
        )
    return resolved, None


def cache_relative_path(path: KaosPath, work_dir: KaosPath) -> str | None:
    """Return *path* relative to ``.kimix_cache`` (``research/<id>/a.md``).

    Returns ``None`` when *path* is outside the cache: a ledger entry the parent
    cannot resolve would be worse than no entry at all.
    """
    try:
        rel = Path(str(path)).relative_to(Path(str(cache_base(work_dir))))
    except (ValueError, OSError):
        return None
    if not rel.parts:
        return None
    return rel.as_posix()


def display_cache_path(rel_path: str | None) -> str:
    """Render a cache-relative path the way tool messages show it."""
    return f"{CACHE_DIR_NAME}/{rel_path}" if rel_path else CACHE_DIR_NAME


def record_cache_written(session: Any, rel_path: str | None) -> str | None:
    """Record a successful cache write on *session*; return its relative path.

    *rel_path* must already be relative to ``.kimix_cache`` (see
    :func:`cache_relative_path`).  Failure-isolated: a broken ledger must never
    turn a successful write into a tool error.
    """
    if not rel_path:
        return None
    recorded = False
    try:
        files = session.custom_data.setdefault(CACHE_WRITTEN_FILES_KEY, [])
        if isinstance(files, list):
            if rel_path not in files:
                files.append(rel_path)
            recorded = True
    except Exception:
        pass
    return rel_path if recorded else None


def cache_written_files(session: Any) -> list[str]:
    """Return the recorded cache-relative paths for *session* (never raises)."""
    try:
        files = session.custom_data.get(CACHE_WRITTEN_FILES_KEY)
    except Exception:
        return []
    if not isinstance(files, list):
        return []
    return [item for item in files if isinstance(item, str)]


def cache_scope_note(rel_path: str | None) -> str:
    """One-line note appended to a successful cache-only write message."""
    if not rel_path:
        return ""
    return (
        f" Saved in this read-only session's `{CACHE_DIR_NAME}/` sandbox: "
        f"`{display_cache_path(rel_path)}`."
    )


__all__ = [
    "CACHE_DIR_NAME",
    "CACHE_ONLY_WRITE_KEY",
    "CACHE_SUBDIR_NAME",
    "CACHE_WRITTEN_FILES_KEY",
    "cache_base",
    "cache_namespace",
    "cache_only_write_enabled",
    "cache_relative_path",
    "cache_root",
    "cache_scope_note",
    "cache_written_files",
    "display_cache_path",
    "enable_cache_only_write",
    "record_cache_written",
    "resolve_cache_only_path",
]
