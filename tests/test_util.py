"""Tests for repospace.util."""

from __future__ import annotations

import pytest

from repospace import util


def test_topdir_found_from_nested_directory(tmp_path):
    (tmp_path / ".repospace").mkdir()
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    assert util.topdir(nested) == str(tmp_path)


def test_topdir_at_topdir_itself(tmp_path):
    (tmp_path / ".repospace").mkdir()
    assert util.topdir(tmp_path) == str(tmp_path)


def test_topdir_not_found(tmp_path):
    with pytest.raises(util.RepospaceNotFound):
        util.topdir(tmp_path)


def test_topdir_marker_file_does_not_count(tmp_path):
    (tmp_path / ".repospace").write_text("not a directory")
    with pytest.raises(util.RepospaceNotFound):
        util.topdir(tmp_path)


def test_repospace_dir(tmp_path):
    (tmp_path / ".repospace").mkdir()
    assert util.repospace_dir(tmp_path) == str(tmp_path / ".repospace")


def test_escapes_directory(tmp_path):
    inside = tmp_path / "sub" / "file"
    assert not util.escapes_directory(inside, tmp_path)
    assert util.escapes_directory(tmp_path.parent, tmp_path)
    sneaky = tmp_path / "sub" / ".." / ".." / "other"
    assert util.escapes_directory(sneaky, tmp_path)


def test_quote_sh_list():
    assert util.quote_sh_list(["git", "log", "--format=%h %s"]) == ("git log '--format=%h %s'")
