from __future__ import annotations

import hashlib
import shutil
import threading
from functools import lru_cache
from pathlib import Path


def _file_digest(path: Path) -> str:
    """Return blake2b hex digest of file contents using chunked reads."""
    h = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class VFS:
    """Virtual file system that overlays a virtual_root on top of a work_dir."""

    def __init__(self, virtual_root: Path, work_dir: Path) -> None:
        self.virtual_root = Path(virtual_root).resolve()
        self.work_dir = Path(work_dir).resolve()
        self._lock = threading.Lock()

    @staticmethod
    @lru_cache(maxsize=4096)
    def _resolve_rel(work_dir_str: str, path_str: str) -> str:
        p = Path(path_str)
        p = p.parent.resolve() / p.name if p.is_symlink() else p.resolve()
        rel = p.relative_to(Path(work_dir_str))
        return str(rel)

    def _rel(self, path: Path) -> Path:
        """Return relative path from work_dir, raising if outside."""
        try:
            rel_str = self._resolve_rel(str(self.work_dir), str(path))
        except ValueError:
            p = Path(path).resolve()
            raise ValueError(f"Path {p} is not under work_dir {self.work_dir}") from None
        return Path(rel_str)

    def translate_path(self, path: Path) -> Path:
        """Return the current effective path for *path* (virtual if dirty, else original)."""
        rel = self._rel(path)
        with self._lock:
            if (self.virtual_root / rel).exists():
                return self.virtual_root / rel
        return self.work_dir / rel

    def get(self, path: Path, mark_dirty: bool = True) -> Path:
        """Retrieve *path* and optionally copy it into the virtual layer."""
        original = Path(path)
        rel = self._rel(original)
        resolved = self.work_dir / rel

        with self._lock:
            if (self.virtual_root / rel).exists():
                return self.virtual_root / rel

            if not mark_dirty or not resolved.is_file():
                return original

            dest = self.virtual_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            try:
                shutil.copyfile(resolved, tmp)
                tmp.replace(dest)
            finally:
                if tmp.exists():
                    tmp.unlink()

            return dest

    def is_dirty(self, path: Path) -> bool:
        """Check whether *path* is currently tracked as dirty."""
        rel = self._rel(path)
        with self._lock:
            return (self.virtual_root / rel).exists()


def merge(
    *vfs_instances: VFS, apply: bool = False
) -> tuple[dict[Path, list[tuple[int, bytes]]], dict[Path, bytes]]:
    """Detect conflicts across multiple VFS instances.

    If *apply* is True, non-conflicting changes are written to the shared
    work_dir and removed from each VFS's virtual layer.

    Returns a tuple of (conflicts, applied_changes).
    """
    if not vfs_instances:
        return {}, {}

    conflicts: dict[Path, list[tuple[int, bytes]]] = {}
    applied_changes: dict[Path, bytes] = {}

    all_paths: set[Path] = set()
    for vfs in vfs_instances:
        if vfs.virtual_root.exists():
            for p in vfs.virtual_root.rglob("*"):
                if p.is_file():
                    all_paths.add(p.relative_to(vfs.virtual_root))

    for rel in all_paths:
        holders: list[tuple[int, VFS]] = []
        for idx, vfs in enumerate(vfs_instances):
            if (vfs.virtual_root / rel).is_file():
                holders.append((idx, vfs))

        if not holders:
            continue

        # Fast path: single holder with no apply -> can't conflict, skip I/O.
        if len(holders) == 1 and not apply:
            continue

        # Hash every holder's file via streaming reads.
        idx_hashes: list[tuple[int, str, VFS]] = []
        for idx, vfs in holders:
            h = _file_digest(vfs.virtual_root / rel)
            idx_hashes.append((idx, h, vfs))

        unique_hashes = {h for _, h, _ in idx_hashes}
        is_conflict = len(unique_hashes) > 1

        if is_conflict:
            conflicts[rel] = [
                (idx, (vfs.virtual_root / rel).read_bytes())
                for idx, _, vfs in idx_hashes
            ]
            continue

        if apply and idx_hashes:
            idx, _, vfs = idx_hashes[0]
            data = (vfs.virtual_root / rel).read_bytes()
            dest = vfs.work_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            try:
                tmp.write_bytes(data)
                tmp.replace(dest)
                applied_changes[rel] = data
            finally:
                if tmp.exists():
                    tmp.unlink()
            for _, _, vfs2 in idx_hashes:
                vfile = vfs2.virtual_root / rel
                if vfile.exists():
                    vfile.unlink()

    return conflicts, applied_changes
