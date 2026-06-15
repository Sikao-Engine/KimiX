from __future__ import annotations

from pathlib import Path

import pytest

from kimi_cli.vfs.core import VFS, merge


class TestVFSInit:
    def test_init_sets_paths(self, tmp_path: Path) -> None:
        vr = tmp_path / "virtual"
        wd = tmp_path / "work"
        vfs = VFS(vr, wd)
        assert vfs.virtual_root == vr.resolve()
        assert vfs.work_dir == wd.resolve()



class TestVFSRel:
    def test_rel_returns_relative_path(self, tmp_path: Path) -> None:
        wd = tmp_path / "work"
        wd.mkdir()
        vfs = VFS(tmp_path / "virtual", wd)
        file = wd / "subdir" / "file.txt"
        assert vfs._rel(file) == Path("subdir/file.txt")

    def test_rel_raises_for_outside_path(self, tmp_path: Path) -> None:
        wd = tmp_path / "work"
        wd.mkdir()
        vfs = VFS(tmp_path / "virtual", wd)
        with pytest.raises(ValueError):
            vfs._rel(tmp_path / "outside.txt")

    def test_rel_resolves_symlink_parent(self, tmp_path: Path) -> None:
        wd = tmp_path / "work"
        wd.mkdir()
        vfs = VFS(tmp_path / "virtual", wd)
        real_file = wd / "real.txt"
        real_file.write_text("hello")
        link = wd / "link.txt"
        link.symlink_to(real_file)
        # _rel resolves the parent directory but keeps the symlink name
        assert vfs._rel(link) == Path("link.txt")

    def test_rel_raises_for_symlink_outside_workdir(self, tmp_path: Path) -> None:
        wd = tmp_path / "work"
        wd.mkdir()
        vfs = VFS(tmp_path / "virtual", wd)
        real_file = wd / "real.txt"
        real_file.write_text("hello")
        link = tmp_path / "link.txt"
        link.symlink_to(real_file)
        with pytest.raises(ValueError):
            vfs._rel(link)


class TestVFSTranslatePath:
    def test_translate_path_clean(self, tmp_path: Path) -> None:
        wd = tmp_path / "work"
        wd.mkdir()
        vfs = VFS(tmp_path / "virtual", wd)
        file = wd / "file.txt"
        file.write_text("hello")
        assert vfs.translate_path(file) == file.resolve()

    def test_translate_path_dirty(self, tmp_path: Path) -> None:
        wd = tmp_path / "work"
        wd.mkdir()
        vr = tmp_path / "virtual"
        vfs = VFS(vr, wd)
        file = wd / "file.txt"
        file.write_text("hello")
        vfs.get(file)
        assert vfs.translate_path(file) == vr / "file.txt"


class TestVFSGet:
    def test_get_returns_virtual_when_already_dirty(self, tmp_path: Path) -> None:
        wd = tmp_path / "work"
        wd.mkdir()
        vr = tmp_path / "virtual"
        vfs = VFS(vr, wd)
        file = wd / "file.txt"
        file.write_text("hello")
        vfs.get(file)
        result = vfs.get(file)
        assert result == vr / "file.txt"

    def test_get_copies_file_to_virtual(self, tmp_path: Path) -> None:
        wd = tmp_path / "work"
        wd.mkdir()
        vr = tmp_path / "virtual"
        vfs = VFS(vr, wd)
        file = wd / "file.txt"
        file.write_text("hello")
        result = vfs.get(file)
        assert result == vr / "file.txt"
        assert (vr / "file.txt").read_text() == "hello"
        assert vfs.is_dirty(file)

    def test_get_without_mark_dirty_returns_original(self, tmp_path: Path) -> None:
        wd = tmp_path / "work"
        wd.mkdir()
        vr = tmp_path / "virtual"
        vfs = VFS(vr, wd)
        file = wd / "file.txt"
        file.write_text("hello")
        result = vfs.get(file, mark_dirty=False)
        assert result == file.resolve()
        assert not vfs.is_dirty(file)

    def test_get_missing_file_no_mark_dirty(self, tmp_path: Path) -> None:
        wd = tmp_path / "work"
        wd.mkdir()
        vfs = VFS(tmp_path / "virtual", wd)
        missing = wd / "missing.txt"
        result = vfs.get(missing, mark_dirty=False)
        assert result == missing.resolve()

    def test_get_creates_parent_dirs(self, tmp_path: Path) -> None:
        wd = tmp_path / "work"
        wd.mkdir()
        vr = tmp_path / "virtual"
        vfs = VFS(vr, wd)
        file = wd / "a" / "b" / "file.txt"
        file.parent.mkdir(parents=True)
        file.write_text("hello")
        result = vfs.get(file)
        assert result == vr / "a" / "b" / "file.txt"
        assert result.exists()


class TestVFSIsDirty:
    def test_is_dirty_true(self, tmp_path: Path) -> None:
        wd = tmp_path / "work"
        wd.mkdir()
        vfs = VFS(tmp_path / "virtual", wd)
        file = wd / "file.txt"
        file.write_text("hello")
        vfs.get(file)
        assert vfs.is_dirty(file)

    def test_is_dirty_false(self, tmp_path: Path) -> None:
        wd = tmp_path / "work"
        wd.mkdir()
        vfs = VFS(tmp_path / "virtual", wd)
        file = wd / "file.txt"
        file.write_text("hello")
        assert not vfs.is_dirty(file)


class TestMerge:
    def test_no_conflict_single_vfs(self, tmp_path: Path) -> None:
        wd = tmp_path / "work"
        wd.mkdir()
        vr = tmp_path / "virtual"
        vfs = VFS(vr, wd)
        file = wd / "file.txt"
        file.write_text("hello")
        vfs.get(file)
        (vr / "file.txt").write_text("world")

        conflicts, applied = merge(vfs)
        assert conflicts == {}
        assert applied == {}

    def test_conflict_different_content(self, tmp_path: Path) -> None:
        wd = tmp_path / "work"
        wd.mkdir()
        vr1 = tmp_path / "virtual1"
        vr2 = tmp_path / "virtual2"
        vfs1 = VFS(vr1, wd)
        vfs2 = VFS(vr2, wd)
        file = wd / "file.txt"
        file.write_text("base")
        vfs1.get(file)
        vfs2.get(file)
        (vr1 / "file.txt").write_text("A")
        (vr2 / "file.txt").write_text("B")

        conflicts, applied = merge(vfs1, vfs2)
        assert len(conflicts) == 1
        assert Path("file.txt") in conflicts
        assert applied == {}

    def test_no_conflict_same_content(self, tmp_path: Path) -> None:
        wd = tmp_path / "work"
        wd.mkdir()
        vr1 = tmp_path / "virtual1"
        vr2 = tmp_path / "virtual2"
        vfs1 = VFS(vr1, wd)
        vfs2 = VFS(vr2, wd)
        file = wd / "file.txt"
        file.write_text("base")
        vfs1.get(file)
        vfs2.get(file)
        (vr1 / "file.txt").write_text("same")
        (vr2 / "file.txt").write_text("same")

        conflicts, applied = merge(vfs1, vfs2)
        assert conflicts == {}
        assert applied == {}

    def test_apply_non_conflict_single_vfs(self, tmp_path: Path) -> None:
        wd = tmp_path / "work"
        wd.mkdir()
        vr = tmp_path / "virtual"
        vfs = VFS(vr, wd)
        file = wd / "file.txt"
        file.write_text("hello")
        vfs.get(file)
        (vr / "file.txt").write_text("world")

        conflicts, applied = merge(vfs, apply=True)
        assert conflicts == {}
        assert Path("file.txt") in applied
        assert applied[Path("file.txt")] == b"world"
        assert (wd / "file.txt").read_text() == "world"
        assert not vfs.is_dirty(file)
        assert not (vr / "file.txt").exists()

    def test_apply_non_conflict_same_content_multi(self, tmp_path: Path) -> None:
        wd = tmp_path / "work"
        wd.mkdir()
        vr1 = tmp_path / "virtual1"
        vr2 = tmp_path / "virtual2"
        vfs1 = VFS(vr1, wd)
        vfs2 = VFS(vr2, wd)
        file = wd / "file.txt"
        file.write_text("base")
        vfs1.get(file)
        vfs2.get(file)
        (vr1 / "file.txt").write_text("same")
        (vr2 / "file.txt").write_text("same")

        conflicts, applied = merge(vfs1, vfs2, apply=True)
        assert conflicts == {}
        assert Path("file.txt") in applied
        assert (wd / "file.txt").read_text() == "same"
        assert not vfs1.is_dirty(file)
        assert not vfs2.is_dirty(file)
        assert not (vr1 / "file.txt").exists()
        assert not (vr2 / "file.txt").exists()

    def test_apply_leaves_conflicts(self, tmp_path: Path) -> None:
        wd = tmp_path / "work"
        wd.mkdir()
        vr1 = tmp_path / "virtual1"
        vr2 = tmp_path / "virtual2"
        vfs1 = VFS(vr1, wd)
        vfs2 = VFS(vr2, wd)
        file = wd / "file.txt"
        file.write_text("base")
        vfs1.get(file)
        vfs2.get(file)
        (vr1 / "file.txt").write_text("A")
        (vr2 / "file.txt").write_text("B")

        conflicts, applied = merge(vfs1, vfs2, apply=True)
        assert len(conflicts) == 1
        assert applied == {}
        assert vfs1.is_dirty(file)
        assert vfs2.is_dirty(file)
        assert (vr1 / "file.txt").exists()
        assert (vr2 / "file.txt").exists()

    def test_apply_mixed_scenario(self, tmp_path: Path) -> None:
        wd = tmp_path / "work"
        wd.mkdir()
        vr1 = tmp_path / "virtual1"
        vr2 = tmp_path / "virtual2"
        vfs1 = VFS(vr1, wd)
        vfs2 = VFS(vr2, wd)

        f1 = wd / "only1.txt"
        f1.write_text("base1")
        vfs1.get(f1)
        (vr1 / "only1.txt").write_text("only1")

        f2 = wd / "same.txt"
        f2.write_text("base")
        vfs1.get(f2)
        vfs2.get(f2)
        (vr1 / "same.txt").write_text("shared")
        (vr2 / "same.txt").write_text("shared")

        f3 = wd / "conflict.txt"
        f3.write_text("base")
        vfs1.get(f3)
        vfs2.get(f3)
        (vr1 / "conflict.txt").write_text("X")
        (vr2 / "conflict.txt").write_text("Y")

        conflicts, applied = merge(vfs1, vfs2, apply=True)
        assert set(conflicts.keys()) == {Path("conflict.txt")}
        assert set(applied.keys()) == {Path("only1.txt"), Path("same.txt")}
        assert (wd / "only1.txt").read_text() == "only1"
        assert (wd / "same.txt").read_text() == "shared"
        assert (wd / "conflict.txt").read_text() == "base"
        assert not vfs1.is_dirty(f1)
        assert not vfs1.is_dirty(f2)
        assert vfs1.is_dirty(f3)
