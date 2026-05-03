"""L6 Cold Storage Archive: time-blocked, compressed long-term archives."""

from __future__ import annotations

import gzip
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from kimix.memory.types import MemoryEntry


class ColdStorage:
    """Archive memories into time-blocked, compressed files.

    Each block is named by a date range (e.g. ``2022-2024.jsonl.gz``).
    Memories are stored as JSON Lines inside gzip for efficient streaming.
    """

    def __init__(self, archive_dir: str | Path = ".kimix_cache/cold_storage") -> None:
        self.archive_dir = Path(archive_dir)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self._meta_path = self.archive_dir / "_meta.json"
        self._blocks_cache: list[tuple[str, int, int]] | None = None

    @staticmethod
    def _block_name(start_year: int, end_year: int) -> str:
        return f"{start_year}-{end_year}.jsonl.gz"

    @staticmethod
    def _parse_block_name(name: str) -> tuple[int, int] | None:
        """Parse ``YYYY-YYYY.jsonl.gz`` -> (start_year, end_year)."""
        if not name.endswith(".jsonl.gz"):
            return None
        stem = name[:-9]
        if "-" not in stem:
            return None
        try:
            a, b = stem.split("-", 1)
            return int(a), int(b)
        except ValueError:
            return None

    def _block_for_timestamp(self, ts: float) -> Path:
        year = time.gmtime(ts).tm_year
        block_name = self._block_name(year, year)
        return self.archive_dir / block_name

    @staticmethod
    def _entry_to_json(entry: MemoryEntry) -> str:
        """Fast serialization bypassing ``to_dict()`` (avoids ``get_effective_importance()``)."""
        embedding = entry.embedding
        if embedding is not None and hasattr(embedding, "tolist"):
            embedding = embedding.tolist()
        return json.dumps(
            {
                "content": entry.content,
                "memory_type": entry.memory_type.value,
                "timestamp": entry.timestamp,
                "importance": entry.importance,
                "access_count": entry.access_count,
                "last_accessed": entry.last_accessed,
                "embedding": embedding,
                "tags": entry.tags,
                "source": entry.source,
                "metadata": entry.metadata,
                "expires_at": entry.expires_at,
                "agent_id": entry.agent_id,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _read_meta(self) -> dict[str, int]:
        if self._meta_path.exists():
            try:
                with open(self._meta_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _write_meta(self, meta: dict[str, int]) -> None:
        tmp = self._meta_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        tmp.replace(self._meta_path)

    def _update_meta(self, block_name: str, delta: int) -> None:
        meta = self._read_meta()
        meta[block_name] = meta.get(block_name, 0) + delta
        if meta[block_name] <= 0:
            meta.pop(block_name, None)
        self._write_meta(meta)
        self._blocks_cache = None

    def archive(
        self,
        entries: Iterable[MemoryEntry],
        start_year: int | None = None,
        end_year: int | None = None,
    ) -> Path:
        """Archive a batch of memories into the appropriate time block.

        If *start_year* and *end_year* are provided they override auto-detection.
        """
        if start_year is not None and end_year is not None:
            block_path = self.archive_dir / self._block_name(start_year, end_year)
            count = 0
            mode = "at" if block_path.exists() else "wt"
            with gzip.open(block_path, mode, encoding="utf-8") as f:
                for entry in entries:
                    f.write(self._entry_to_json(entry) + "\n")
                    count += 1
            if count == 0:
                raise ValueError("No entries to archive")
            self._update_meta(block_path.name, count)
            return block_path

        groups: dict[int, list[MemoryEntry]] = defaultdict(list)
        total = 0
        for entry in entries:
            year = time.gmtime(entry.timestamp).tm_year
            groups[year].append(entry)
            total += 1

        if total == 0:
            raise ValueError("No entries to archive")

        first_path: Path | None = None
        for year in sorted(groups):
            block_path = self.archive_dir / self._block_name(year, year)
            mode = "at" if block_path.exists() else "wt"
            group = groups[year]
            with gzip.open(block_path, mode, encoding="utf-8") as f:
                for entry in group:
                    f.write(self._entry_to_json(entry) + "\n")
            self._update_meta(block_path.name, len(group))
            if first_path is None:
                first_path = block_path

        assert first_path is not None
        return first_path

    def restore_range(
        self,
        start_year: int,
        end_year: int,
    ) -> list[MemoryEntry]:
        """Restore all memories whose archive block overlaps the year range."""
        results: list[MemoryEntry] = []
        for path in self.archive_dir.glob("*.jsonl.gz"):
            parsed = self._parse_block_name(path.name)
            if parsed is None:
                continue
            block_start, block_end = parsed
            if block_end < start_year or block_start > end_year:
                continue
            with gzip.open(path, "rt", encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        entry = MemoryEntry.from_dict(data)
                        results.append(entry)
                    except Exception:
                        continue
        return results

    def list_archives(self) -> list[tuple[str, int, int]]:
        """List all archives as (filename, start_year, end_year)."""
        if self._blocks_cache is not None:
            return list(self._blocks_cache)
        archives: list[tuple[str, int, int]] = []
        for path in sorted(self.archive_dir.glob("*.jsonl.gz")):
            parsed = self._parse_block_name(path.name)
            if parsed:
                archives.append((path.name, parsed[0], parsed[1]))
        self._blocks_cache = archives
        return archives

    def delete_archive(self, start_year: int, end_year: int) -> bool:
        """Delete a specific archive block."""
        path = self.archive_dir / self._block_name(start_year, end_year)
        if path.exists():
            path.unlink()
            meta = self._read_meta()
            meta.pop(path.name, None)
            self._write_meta(meta)
            self._blocks_cache = None
            return True
        return False

    def reflect(self) -> str:
        meta = self._read_meta()
        total_entries = sum(meta.values())
        archives = self.list_archives()
        return (
            f"Cold Storage: {len(archives)} archives, ~{total_entries} entries"
        )
