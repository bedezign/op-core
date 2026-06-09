"""Tests for :mod:`op_core.cli.discover` — upward ``.env`` collection."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from op_core.cli import discover
from op_core.cli.discover import _DirMeta, discover_env_files


def _touch(path: Path, content: str = "X=1\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _resolved(paths: list[Path]) -> list[Path]:
    return [p.resolve() for p in paths]


def _find(
    *,
    env_files: list[Path] | None = None,
    names: list[str] | None = None,
    ascend: bool = True,
    ascend_until: list[str] | None = None,
    cwd: Path,
    home: Path | None,
) -> list[Path]:
    return discover_env_files(
        env_files=[str(f) for f in (env_files or [])],
        names=names or [],
        ascend=ascend,
        ascend_until=ascend_until or [],
        cwd=cwd,
        home=home,
    )


# ---------------------------------------------------------------------------
# No ascent
# ---------------------------------------------------------------------------


class TestNoAscent:
    def test_explicit_files_only(self, tmp_path: Path) -> None:
        a = _touch(tmp_path / "a.env")
        b = _touch(tmp_path / "b.env")
        result = _find(env_files=[a, b], ascend=False, cwd=tmp_path, home=tmp_path)
        assert _resolved(result) == [a, b]


# ---------------------------------------------------------------------------
# Basic upward walk
# ---------------------------------------------------------------------------


class TestAscendFromCwd:
    def test_collects_dotenv_up_to_home_nearest_first(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        sub = home / "proj" / "sub"
        h = _touch(home / ".env")
        p = _touch(home / "proj" / ".env")
        s = _touch(sub / ".env")
        result = _find(cwd=sub, home=home)
        assert _resolved(result) == [s, p, h]

    def test_stops_at_home_inclusive(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        _touch(tmp_path / ".env")  # above home — must not be collected
        h = _touch(home / ".env")
        _touch(home / "sub" / ".env")
        result = _resolved(_find(cwd=home / "sub", home=home))
        assert h in result
        assert (tmp_path / ".env").resolve() not in result

    def test_anchor_only_when_cwd_not_under_home(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        work = tmp_path / "work" / "sub"
        s = _touch(work / ".env")
        _touch(tmp_path / "work" / ".env")  # parent — must not be collected
        result = _resolved(_find(cwd=work, home=home))
        assert result == [s]

    def test_home_none_means_anchor_only(self, tmp_path: Path) -> None:
        s = _touch(tmp_path / "a" / "b" / ".env")
        _touch(tmp_path / "a" / ".env")
        result = _resolved(_find(cwd=tmp_path / "a" / "b", home=None))
        assert result == [s]


# ---------------------------------------------------------------------------
# Anchoring on --env-file directories
# ---------------------------------------------------------------------------


class TestAnchoring:
    def test_anchor_is_env_file_dir_not_cwd(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        bookstack = home / "scripts" / "bookstack"
        unrelated = home / "elsewhere"
        unrelated.mkdir(parents=True)
        b = _touch(bookstack / ".env")
        scripts = _touch(home / "scripts" / ".env")
        h = _touch(home / ".env")
        result = _resolved(_find(env_files=[bookstack / ".env"], cwd=unrelated, home=home))
        assert result == [b, scripts, h]  # walked up from bookstack, not from cwd

    def test_multiple_env_files_ascend_each(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        a = _touch(home / "a" / ".env")
        b = _touch(home / "b" / ".env")
        h = _touch(home / ".env")
        result = _resolved(_find(env_files=[a, b], cwd=home, home=home))
        assert result == [a, b, h]  # explicit first, shared ancestor once

    def test_explicit_file_deduped_with_discovery(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        proj = _touch(home / "proj" / ".env")
        result = _resolved(_find(env_files=[proj], cwd=home, home=home))
        assert result.count(proj) == 1


# ---------------------------------------------------------------------------
# Name seeding
# ---------------------------------------------------------------------------


class TestNames:
    def test_basename_of_env_file_seeds_search(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        app = _touch(home / "proj" / "app.env")
        parent_app = _touch(home / "app.env")
        _touch(home / ".env")  # different name — must not be collected
        result = _resolved(_find(env_files=[app], cwd=home, home=home))
        assert parent_app in result
        assert (home / ".env").resolve() not in result

    def test_extra_env_file_name_added_on_top(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        app = _touch(home / "proj" / "app.env")
        dot = _touch(home / ".env")
        result = _resolved(_find(env_files=[app], names=[".env"], cwd=home, home=home))
        assert dot in result

    def test_default_dotenv_for_bare_ascend(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        s = _touch(home / ".env")
        result = _resolved(_find(cwd=home, home=home))
        assert s in result


# ---------------------------------------------------------------------------
# --ascend-until boundaries
# ---------------------------------------------------------------------------


class TestBoundaries:
    def test_path_boundary_stops_inclusive(self, tmp_path: Path) -> None:
        proj = tmp_path / "proj"
        sub = proj / "sub"
        s = _touch(sub / ".env")
        p = _touch(proj / ".env")
        _touch(tmp_path / ".env")  # above boundary
        result = _resolved(_find(cwd=sub, ascend_until=[str(proj)], home=None))
        assert result == [s, p]

    def test_basename_boundary_stops(self, tmp_path: Path) -> None:
        proj = tmp_path / "proj"
        sub = proj / "deep" / "sub"
        s = _touch(sub / ".env")
        d = _touch(proj / "deep" / ".env")
        p = _touch(proj / ".env")
        _touch(tmp_path / ".env")
        result = _resolved(_find(cwd=sub, ascend_until=["proj"], home=None))
        assert result == [s, d, p]

    def test_unmatched_boundary_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        anchor = tmp_path / "anchor"
        _touch(anchor / ".env")
        real = discover._dir_meta

        def fake(path: Path) -> _DirMeta:
            meta = real(path)
            if path == anchor.resolve().parent:  # make the parent untrusted to stop the climb
                return _DirMeta(dev=meta.dev, uid=meta.uid, mode=meta.mode | 0o002)
            return meta

        monkeypatch.setattr(discover, "_dir_meta", fake)
        with caplog.at_level(logging.WARNING, logger="op_core.cli.discover"):
            _find(cwd=anchor, ascend_until=["never-matches"], home=None)
        assert any("boundary not found" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Security ceiling
# ---------------------------------------------------------------------------


class TestSecurity:
    def test_world_writable_dir_stops_climb(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        proj = home / "proj"
        sub = proj / "sub"
        s = _touch(sub / ".env")
        _touch(proj / ".env")
        _touch(home / ".env")
        os.chmod(proj, 0o777)  # world-writable ancestor
        try:
            result = _resolved(_find(cwd=sub, home=home))
        finally:
            os.chmod(proj, 0o755)
        assert result == [s]  # stopped before entering the world-writable dir

    def test_world_writable_file_skipped(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        home = tmp_path / "home"
        good = _touch(home / "sub" / ".env")
        bad = _touch(home / ".env")
        os.chmod(bad, 0o666)  # world-writable file
        with caplog.at_level(logging.WARNING, logger="op_core.cli.discover"):
            result = _resolved(_find(cwd=home / "sub", home=home))
        assert good in result
        assert bad.resolve() not in result
        assert any("world-writable" in r.message for r in caplog.records)

    def test_symlinked_file_skipped(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        real = _touch(tmp_path / "secret.env")
        link = home / ".env"
        home.mkdir(parents=True, exist_ok=True)
        link.symlink_to(real)
        result = _resolved(_find(cwd=home, home=home))
        assert link.resolve() not in result

    def test_unowned_dir_stops_climb(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = tmp_path / "home"
        sub = home / "sub"
        s = _touch(sub / ".env")
        _touch(home / ".env")
        real = discover._dir_meta

        def fake(path: Path) -> _DirMeta:
            meta = real(path)
            if path == home.resolve():
                return _DirMeta(dev=meta.dev, uid=meta.uid + 1, mode=meta.mode)
            return meta

        monkeypatch.setattr(discover, "_dir_meta", fake)
        result = _resolved(_find(cwd=sub, home=home))
        assert result == [s]  # stopped before entering the not-owned directory

    def test_mount_crossing_stops_climb(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = tmp_path / "home"
        sub = home / "sub"
        s = _touch(sub / ".env")
        _touch(home / ".env")
        real = discover._dir_meta

        def fake(path: Path) -> _DirMeta:
            meta = real(path)
            if path == home.resolve():
                return _DirMeta(dev=meta.dev + 1, uid=meta.uid, mode=meta.mode)
            return meta

        monkeypatch.setattr(discover, "_dir_meta", fake)
        result = _resolved(_find(cwd=sub, home=home))
        assert result == [s]  # stopped at the filesystem boundary

    def test_symlinked_dir_component_stops_climb(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A symlinked ancestor directory halts the climb; that dir and above are not yielded."""
        import stat as _stat

        home = tmp_path / "home"
        sub = home / "sub"
        s = _touch(sub / ".env")
        _touch(home / ".env")  # above the symlink — must not be collected
        real = discover._dir_meta

        def fake(path: Path) -> _DirMeta:
            meta = real(path)
            if path == home.resolve():
                # Report home as a symlink by setting S_ISLNK bits in mode.
                symlink_mode = (meta.mode & ~0o170000) | _stat.S_IFLNK
                return _DirMeta(dev=meta.dev, uid=meta.uid, mode=symlink_mode)
            return meta

        monkeypatch.setattr(discover, "_dir_meta", fake)
        result = _resolved(_find(cwd=sub, home=home))
        assert result == [s]  # stopped before the symlinked directory
