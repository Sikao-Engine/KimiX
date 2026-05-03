"""Tests for L6 ColdStorage."""

import tempfile
import time
from pathlib import Path

import pytest

from kimix.memory.cold_storage import ColdStorage
from kimix.memory.types import MemoryEntry, MemoryType


class TestColdStorage:
    def test_archive_and_restore(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cs = ColdStorage(archive_dir=tmpdir)
            entries = [
                MemoryEntry(content="old memory 1", memory_type=MemoryType.EPISODIC, timestamp=time.time() - 86400 * 365),
                MemoryEntry(content="old memory 2", memory_type=MemoryType.EPISODIC, timestamp=time.time() - 86400 * 300),
            ]
            path = cs.archive(entries)
            assert path.exists()

            # Restore current year range
            year = time.gmtime().tm_year
            restored = cs.restore_range(year - 1, year)
            assert len(restored) == 2
            contents = {e.content for e in restored}
            assert "old memory 1" in contents

    def test_archive_empty_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cs = ColdStorage(archive_dir=tmpdir)
            with pytest.raises(ValueError):
                cs.archive([])

    def test_list_archives(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cs = ColdStorage(archive_dir=tmpdir)
            entries = [MemoryEntry(content="x", memory_type=MemoryType.EPISODIC, timestamp=time.time())]
            cs.archive(entries, start_year=2020, end_year=2022)
            archives = cs.list_archives()
            assert len(archives) == 1
            assert archives[0][1] == 2020
            assert archives[0][2] == 2022

    def test_delete_archive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cs = ColdStorage(archive_dir=tmpdir)
            entries = [MemoryEntry(content="y", memory_type=MemoryType.EPISODIC, timestamp=time.time())]
            cs.archive(entries, start_year=2021, end_year=2021)
            assert cs.delete_archive(2021, 2021) is True
            assert cs.delete_archive(2021, 2021) is False

    def test_restore_range_no_overlap(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cs = ColdStorage(archive_dir=tmpdir)
            entries = [MemoryEntry(content="z", memory_type=MemoryType.EPISODIC, timestamp=time.time())]
            cs.archive(entries, start_year=2018, end_year=2019)
            restored = cs.restore_range(2020, 2022)
            assert restored == []

    def test_reflect(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cs = ColdStorage(archive_dir=tmpdir)
            entries = [MemoryEntry(content="a", memory_type=MemoryType.EPISODIC, timestamp=time.time())]
            cs.archive(entries)
            reflect = cs.reflect()
            assert "1 archives" in reflect
