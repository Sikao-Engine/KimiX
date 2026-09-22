from __future__ import annotations

import asyncio
import os
import regex as re
from collections.abc import Sequence
from pathlib import Path, PurePath
from stat import S_ISDIR

import aiofiles.os
from kaos.path import KaosPath

from kimi_cli.utils.environment import is_windows
from kimi_cli.utils.logging import logger
from kimi_cli.utils.windows_paths import posix_path_to_windows

_ROTATION_OPEN_FLAGS = os.O_CREAT | os.O_EXCL | os.O_WRONLY
_ROTATION_FILE_MODE = 0o600


async def _reserve_rotation_path(path: Path) -> bool:
    """Atomically create an empty file as a reservation for *path*."""

    def _create() -> None:
        fd = os.open(str(path), _ROTATION_OPEN_FLAGS, _ROTATION_FILE_MODE)
        os.close(fd)

    try:
        await asyncio.to_thread(_create)
    except FileExistsError:
        return False
    return True


async def next_available_rotation(path: Path) -> Path | None:
    """Return a reserved rotation path for *path* or ``None`` if parent is missing.

    The caller must overwrite/reuse the returned path immediately because this helper
    commits an empty placeholder file to guarantee uniqueness. It is therefore suited
    for rotating *files* (like history logs) but **not** directory creation.
    """

    if not path.parent.exists():
        return None

    base_name = path.stem
    suffix = path.suffix
    pattern = re.compile(rf"^{re.escape(base_name)}_(\d+){re.escape(suffix)}$")
    max_num = 0
    for entry in await aiofiles.os.listdir(path.parent):
        if match := pattern.match(entry):
            max_num = max(max_num, int(match.group(1)))

    next_num = max_num + 1
    while True:
        next_path = path.parent / f"{base_name}_{next_num}{suffix}"
        if await _reserve_rotation_path(next_path):
            return next_path
        next_num += 1


_LIST_DIR_ROOT_WIDTH = 30  # worst-case ~330 lines ≈ 2.5k tokens
_LIST_DIR_CHILD_WIDTH = 10


async def _collect_entries(
    dir_path: KaosPath, max_width: int
) -> tuple[list[tuple[str, bool]], int]:
    """Collect up to *max_width* entries from *dir_path*.

    Returns ``(entries, total_count)`` where each entry is ``(name, is_dir)``.
    All entries are stat-ed, sorted directories-first then alphabetically,
    and truncated to *max_width* so the returned subset is deterministic
    regardless of filesystem enumeration order.
    """
    all_entries: list[tuple[str, bool]] = []
    async for entry in dir_path.iterdir():
        try:
            st = await entry.stat()
            is_dir = S_ISDIR(st.st_mode)
        except OSError:
            is_dir = False
        all_entries.append((entry.name, is_dir))
    all_entries.sort(key=lambda e: (not e[1], e[0]))
    return all_entries[:max_width], len(all_entries)


async def list_directory(work_dir: KaosPath) -> str:
    """Return a compact tree listing of *work_dir* (up to 2 levels).

    This helper is used mainly to provide context to the LLM (for example
    the additional-directories section of the system prompt) and to show
    top-level directory contents in tools.

    Both depth and width are capped to keep the system-prompt token budget
    bounded (see GH-1809):

    * **Depth 0** (root): up to :data:`_LIST_DIR_ROOT_WIDTH` entries.
    * **Depth 1** (children of root dirs): up to :data:`_LIST_DIR_CHILD_WIDTH`
      entries per directory.
    * Truncated levels show ``... and N more`` so the LLM knows more exists.
    """
    lines: list[str] = []
    entries, total = await _collect_entries(work_dir, _LIST_DIR_ROOT_WIDTH)
    remaining = total - len(entries)

    for i, (name, is_dir) in enumerate(entries):
        is_last = (i == len(entries) - 1) and remaining == 0
        connector = "└── " if is_last else "├── "

        if is_dir:
            lines.append(f"{connector}{name}/")
            child_prefix = "    " if is_last else "│   "
            try:
                child_entries, child_total = await _collect_entries(
                    work_dir / name, _LIST_DIR_CHILD_WIDTH
                )
            except OSError:
                lines.append(f"{child_prefix}└── [not readable]")
                continue
            child_remaining = child_total - len(child_entries)
            for j, (child_name, child_is_dir) in enumerate(child_entries):
                child_is_last = (j == len(child_entries) - 1) and child_remaining == 0
                child_connector = "└── " if child_is_last else "├── "
                suffix = "/" if child_is_dir else ""
                lines.append(f"{child_prefix}{child_connector}{child_name}{suffix}")
            if child_remaining > 0:
                lines.append(f"{child_prefix}└── ... and {child_remaining} more")
        else:
            lines.append(f"{connector}{name}")

    if remaining > 0:
        lines.append(f"└── ... and {remaining} more entries")

    return "\n".join(lines) if lines else "(empty directory)"


def shorten_home(path: KaosPath) -> KaosPath:
    """
    Convert absolute path to use `~` for home directory.
    """
    try:
        home = KaosPath.home()
        p = path.relative_to(home)
        return KaosPath("~") / p
    except Exception:
        return path


def normalize_user_path(raw: str) -> str:
    """Normalize a user-provided path string to a native form.

    On Windows, recognize MSYS/git-bash POSIX-style paths and convert them to
    native Windows form. The model running through git-bash sometimes emits
    ``/c/Users/foo`` when the file tool needs ``C:\\Users\\foo`` for Python's
    ``os``/``pathlib`` APIs.

    On non-Windows hosts this is a passthrough — POSIX-style paths are already
    native, and we don't want to corrupt names like ``/cygdrive/`` if the user
    has such a path on Linux.
    """
    if not is_windows():
        return raw

    # Match POSIX MSYS forms: /c/..., /C/..., /cygdrive/c/..., //server/share
    # Avoid touching pure relative paths or already-Windows paths.
    if raw.startswith("//"):
        return posix_path_to_windows(raw)
    if raw.startswith("/cygdrive/"):
        return posix_path_to_windows(raw)
    if len(raw) >= 2 and raw[0] == "/" and raw[1].isalpha() and (len(raw) == 2 or raw[2] == "/"):
        return posix_path_to_windows(raw)

    return raw


def kaos_path_from_user_input(raw: str) -> KaosPath:
    """Convert a model-supplied path string into a usable :class:`KaosPath`.

    Performs the two normalizations every file tool needs:

    1. :func:`normalize_user_path` — convert MSYS/Cygwin POSIX paths to native
       Windows form when running on Windows; passthrough elsewhere.
    2. ``KaosPath.expanduser()`` — expand a leading ``~`` to the user's home.

    Centralizing this in one place ensures every file-tool entry point is
    consistent and means future path-shape conversions only need to be added
    once.
    """
    return KaosPath(normalize_user_path(raw)).expanduser()


def kaos_path_from_tool_input(raw: str, work_dir: KaosPath) -> KaosPath:
    """Convert a tool/user path string into a :class:`KaosPath` resolved against *work_dir*.

    Resolution order:

    1. Apply :func:`normalize_user_path` and ``KaosPath.expanduser()``.
    2. If the result is absolute, canonicalize it (resolve ``.``/``..`` but not symlinks).
    3. If the result is relative, prepend *work_dir* and then canonicalize.

    This ensures that relative paths supplied by the model are always interpreted
    relative to the session work directory instead of the process current directory.
    """
    p = kaos_path_from_user_input(raw)
    if p.is_absolute():
        return p.canonical()
    return (work_dir / str(p)).canonical()


def local_path_for_cwd(work_dir: KaosPath) -> Path:
    """Return a local :class:`pathlib.Path` for use as a subprocess ``cwd``.

    :class:`KaosPath` cannot be passed directly to APIs such as
    ``asyncio.create_subprocess_exec(..., cwd=...)`` that expect a
    :class:`pathlib.Path` or ``str``. This helper performs an explicit cast.
    """
    return Path(str(work_dir))


def sanitize_cli_path(raw: str) -> str:
    """Strip surrounding quotes from a CLI path argument.

    On macOS, dragging a file into the terminal wraps the path in single
    quotes (e.g. ``'/path/to/file'``).  This helper strips matching outer
    quotes (single or double) so downstream path handling works correctly.
    """
    raw = raw.strip()
    if len(raw) >= 2 and ((raw[0] == "'" and raw[-1] == "'") or (raw[0] == '"' and raw[-1] == '"')):
        raw = raw[1:-1]
    return raw


def is_within_directory(path: KaosPath, directory: KaosPath) -> bool:
    """
    Check whether *path* is contained within *directory* using pure path semantics.
    Both arguments should already be canonicalized (e.g. via KaosPath.canonical()).
    """
    candidate = PurePath(str(path))
    base = PurePath(str(directory))
    try:
        candidate.relative_to(base)
        return True
    except ValueError:
        return False


def is_within_workspace(
    path: KaosPath,
    work_dir: KaosPath,
    additional_dirs: Sequence[KaosPath] = (),
) -> bool:
    """
    Check whether *path* is within the workspace (work_dir or any additional directory).
    """
    if is_within_directory(path, work_dir):
        return True
    return any(is_within_directory(path, d) for d in additional_dirs)


async def find_project_root(work_dir: KaosPath) -> KaosPath:
    """Walk up from *work_dir* to find the nearest directory containing ``.git``.

    Returns *work_dir* itself if no ``.git`` marker is found before reaching the
    filesystem root. Used by AGENTS.md discovery and by resolving relative
    ``extra_skill_dirs`` entries to the project root (not the CWD).
    """
    current = work_dir
    while True:
        if await (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:  # filesystem root
            return work_dir
        current = parent


_AGENTS_MD_MAX_BYTES = 32 * 1024  # 32 KiB


async def _dirs_root_to_leaf(work_dir: KaosPath, project_root: KaosPath) -> list[KaosPath]:
    """Return the list of directories from *project_root* down to *work_dir* (inclusive)."""
    dirs: list[KaosPath] = []
    current = work_dir
    while True:
        dirs.append(current)
        if current == project_root:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    dirs.reverse()  # root → leaf
    return dirs


async def load_agents_md(work_dir: KaosPath) -> str | None:
    """Discover and merge ``AGENTS.md`` files from the project root down to *work_dir*.

    For each directory on the path, the following candidates are checked in order:

    1. ``.kimi/AGENTS.md``  — project-local kimi config (highest priority)
    2. ``AGENTS.md``        — standard location
    3. ``agents.md``        — lowercase variant (mutually exclusive with 2)

    Within a single directory, ``.kimi/AGENTS.md`` and ``AGENTS.md``/``agents.md``
    are **both** loaded (with ``.kimi/`` first), but ``AGENTS.md`` and ``agents.md``
    are mutually exclusive (uppercase wins).

    All discovered files are concatenated root→leaf, separated by ``\\n\\n``, with
    source annotations.  Total size is capped at :data:`_AGENTS_MD_MAX_BYTES`.
    Budget is allocated leaf-first so deeper (more specific) files are never
    truncated in favour of shallower ones.
    """
    project_root = await find_project_root(work_dir)
    dirs = await _dirs_root_to_leaf(work_dir, project_root)

    # Phase 1: collect all candidate files (root → leaf order)
    discovered: list[tuple[KaosPath, str]] = []  # (path, content)
    for d in dirs:
        # .kimi/AGENTS.md is always checked independently (can coexist with root-level file)
        kimi_path = d / ".kimi" / "AGENTS.md"
        # AGENTS.md and agents.md are mutually exclusive (uppercase wins)
        root_candidates = [d / "AGENTS.md", d / "agents.md"]

        candidates: list[KaosPath] = []
        if await kimi_path.is_file():
            candidates.append(kimi_path)
        for rc in root_candidates:
            if await rc.is_file():
                candidates.append(rc)
                break

        for path in candidates:
            content = (await path.read_text()).strip()
            if content:
                discovered.append((path, content))
                logger.info("Loaded agents.md: {path}", path=path)

    if not discovered:
        logger.info(
            "No AGENTS.md found from {root} to {cwd}",
            root=project_root,
            cwd=work_dir,
        )
        return None

    # Phase 2: allocate budget leaf-first so deeper (more specific) files
    # are never truncated in favour of shallower ones.
    # The annotation overhead (<!-- From: ... -->\n and \n\n separators)
    # is included in the budget so the final output never exceeds the limit.
    remaining = _AGENTS_MD_MAX_BYTES
    budgeted: list[tuple[KaosPath, str]] = [None] * len(discovered)  # type: ignore[list-item]
    for i in reversed(range(len(discovered))):
        path, content = discovered[i]
        annotation = f"<!-- From: {path} -->\n"
        # Reserve space for the annotation and the \n\n separator between parts
        separator_cost = len(b"\n\n") if i < len(discovered) - 1 else 0
        overhead = len(annotation.encode()) + separator_cost
        remaining -= overhead
        if remaining <= 0:
            budgeted[i] = (path, "")
            remaining = 0
            continue
        encoded = content.encode()
        if len(encoded) > remaining:
            content = encoded[:remaining].decode(errors="ignore").strip()
            logger.warning("AGENTS.md truncated due to size limit: {path}", path=path)
        remaining -= len(content.encode())
        budgeted[i] = (path, content)

    # Phase 3: assemble in root → leaf order, skipping entries emptied by truncation
    parts: list[str] = []
    for path, content in budgeted:
        if content:
            parts.append(f"<!-- From: {path} -->\n{content}")

    return "\n\n".join(parts) if parts else None
