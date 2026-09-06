"""Tests for the generated members.json and packages.cmake."""

from __future__ import annotations

import json
import re
import subprocess
import sys

import pytest

from repospace.app.generate import update_hash, write_if_different


def guard_hash(packages_cmake):
    match = re.search(
        r'set\(REPOSPACE_UPDATE_HASH "([0-9a-f]{64})" CACHE STRING',
        packages_cmake.read_text(),
    )
    assert match, "no guard hash in packages.cmake"
    return match.group(1)


def git_out(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()


def update(run_repospace, repospace, *args, expect=0):
    code, out, err = run_repospace(["update", *args], cwd=repospace.ws)
    assert code == expect, err
    return out, err


def generated(repospace):
    rdir = repospace.ws / ".repospace"
    return rdir / "members.json", rdir / "packages.cmake"


def test_members_json_content(repospace, run_repospace):
    update(run_repospace, repospace)
    members_json, _ = generated(repospace)
    data = json.loads(members_json.read_text())
    assert data["version"] == 1
    assert data["topdir"] == str(repospace.ws)
    members = data["members"]
    assert [m["name"] for m in members] == ["manifest", "liba", "libb", "libc"]

    manifest_entry = members[0]
    assert manifest_entry["path"] == "."
    assert manifest_entry["url"] is None
    assert manifest_entry["revision"] is None
    assert manifest_entry["sha"] is None
    assert manifest_entry["declared-by"] is None
    assert manifest_entry["cmake-packages"] == ["App"]
    assert manifest_entry["abspath"] == str(repospace.ws)

    liba = members[1]
    assert liba["declared-by"] == "manifest"
    assert liba["cmake-packages"] == ["LibA"]
    assert liba["extension-commands"] == ["repospace-commands.yaml"]
    assert liba["sha"] == git_out(repospace.ws / "liba", "rev-parse", "refs/heads/repospace-rev")
    assert members[3]["declared-by"] == "liba"
    assert members[2]["revision"] == "v1.0"


def test_packages_cmake_content(repospace, run_repospace):
    update(run_repospace, repospace)
    members_json, packages_cmake = generated(repospace)
    text = packages_cmake.read_text()

    expected_hash = guard_hash(packages_cmake)
    assert f'if(NOT REPOSPACE_UPDATE_HASH STREQUAL "{expected_hash}")' in text
    assert "FATAL_ERROR" in text
    assert "cmake --fresh" in text

    app_block = (
        "if (NOT DEFINED ENV{App_ROOT})\n" f'  set(ENV{{App_ROOT}} "{repospace.ws}")\n' "endif()"
    )
    liba_block = (
        "if (NOT DEFINED ENV{LibA_ROOT})\n"
        f'  set(ENV{{LibA_ROOT}} "{repospace.ws / "liba"}")\n'
        "endif()"
    )
    assert app_block in text
    assert liba_block in text
    # Self comes first.
    assert text.index("App_ROOT") < text.index("LibA_ROOT")


def test_noop_update_preserves_mtimes(repospace, run_repospace):
    update(run_repospace, repospace)
    members_json, packages_cmake = generated(repospace)
    before = (
        members_json.stat().st_mtime_ns,
        packages_cmake.stat().st_mtime_ns,
    )
    content_before = members_json.read_bytes(), packages_cmake.read_bytes()
    update(run_repospace, repospace)
    after = (
        members_json.stat().st_mtime_ns,
        packages_cmake.stat().st_mtime_ns,
    )
    assert before == after
    assert content_before == (
        members_json.read_bytes(),
        packages_cmake.read_bytes(),
    )


def test_structural_change_changes_hash(repospace, run_repospace):
    update(run_repospace, repospace)
    members_json, packages_cmake = generated(repospace)
    hash_before = guard_hash(packages_cmake)
    repospace.repos.commit(repospace.liba_src, {"more.txt": "x\n"}, "advance")
    update(run_repospace, repospace)
    assert guard_hash(packages_cmake) != hash_before


def test_group_filter_change_changes_hash(repospace, run_repospace):
    # A member deactivated by a group filter stays cloned at the same commit, so members.json does
    # not move; packages.cmake loses its root, and a build tree configured against the old one must
    # still be told that it is stale.
    repospace.rewrite_app_yaml(
        extra_members=(
            "    - name: libd\n"
            f"      url: {repospace.url(repospace.libc_src)}\n"
            "      groups: [opt]\n"
            "      cmake-packages: [OptPkg]\n"
        )
    )
    update(run_repospace, repospace)
    members_json, packages_cmake = generated(repospace)
    members_before = members_json.read_bytes()
    hash_before = guard_hash(packages_cmake)
    assert "OptPkg" in packages_cmake.read_text()

    update(run_repospace, repospace, "--group-filter=-opt")
    assert members_json.read_bytes() == members_before
    assert "OptPkg" not in packages_cmake.read_text()
    assert guard_hash(packages_cmake) != hash_before


def test_update_hash_covers_both_inputs():
    # Neither input alone identifies the generated state.
    assert update_hash("members", "roots") != update_hash("members", "other")
    assert update_hash("members", "roots") != update_hash("other", "roots")
    assert update_hash("ab", "c") != update_hash("a", "bc")


def test_duplicate_package_first_wins(repospace, run_repospace):
    repospace.rewrite_app_yaml(
        extra_members=(
            "    - name: libd\n"
            f"      url: {repospace.url(repospace.libc_src)}\n"
            "      cmake-packages: [LibA]\n"
        )
    )
    out, err = update(run_repospace, repospace)
    assert "first declaration wins" in err
    _, packages_cmake = generated(repospace)
    text = packages_cmake.read_text()
    # A single block: one "if (NOT DEFINED ...)" and one "set(...)".
    assert text.count("ENV{LibA_ROOT}") == 2
    assert f'"{repospace.ws / "liba"}"' in text
    assert f'set(ENV{{LibA_ROOT}} "{repospace.ws / "libd"}")' not in text


def test_partial_failure_records_the_members_that_moved(repospace, run_repospace):
    # The members that did update are part of the on-disk state even when another member failed, so
    # the generated files must describe them; otherwise the guard hash keeps blessing CMake caches
    # that no longer match the repospace.
    update(run_repospace, repospace)
    members_json, packages_cmake = generated(repospace)
    hash_before = guard_hash(packages_cmake)
    new_sha = repospace.repos.commit(repospace.liba_src, {"more.txt": "x\n"}, "advance")
    repospace.rewrite_app_yaml(extra_members="    - name: ghost\n      url: file:///nowhere\n")
    _, err = update(run_repospace, repospace, expect=1)
    assert "update failed for: ghost (ghost)" in err
    data = json.loads(members_json.read_text())
    by_name = {m["name"]: m for m in data["members"]}
    assert by_name["liba"]["sha"] == new_sha
    assert by_name["ghost"]["sha"] is None
    assert guard_hash(packages_cmake) != hash_before


def test_packages_cmake_excludes_inactive(repospace, run_repospace):
    repospace.rewrite_app_yaml(
        extra_members=(
            "    - name: libd\n"
            f"      url: {repospace.url(repospace.libc_src)}\n"
            "      groups: [opt]\n"
            "      cmake-packages: [OptPkg]\n"
        ),
        group_filter="-opt",
    )
    update(run_repospace, repospace)
    _, packages_cmake = generated(repospace)
    assert "OptPkg" not in packages_cmake.read_text()
    # An extra +opt filter activates the member for this update run.
    update(run_repospace, repospace, "--group-filter", "+opt")
    assert "OptPkg" in packages_cmake.read_text()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: umask")
@pytest.mark.parametrize("umask, mode", [(0o022, 0o644), (0o077, 0o600)])
def test_generated_files_respect_umask(tmp_path, umask, mode):
    # Two masks, because a hardcoded 0o644 would satisfy the first one on its own.
    import os
    import stat

    target = tmp_path / "file.txt"
    old = os.umask(umask)
    try:
        write_if_different(str(target), "content\n")
    finally:
        os.umask(old)
    assert stat.S_IMODE(target.stat().st_mode) == mode


def test_write_if_different(tmp_path):
    target = tmp_path / "file.txt"
    assert write_if_different(str(target), "one\n") is True
    mtime = target.stat().st_mtime_ns
    assert write_if_different(str(target), "one\n") is False
    assert target.stat().st_mtime_ns == mtime
    assert write_if_different(str(target), "two\n") is True
    assert target.read_text() == "two\n"


def test_packages_cmake_escapes_paths(tmp_path):
    from repospace.app.generate import packages_cmake_text
    from repospace.manifest import Manifest

    # Characters in the topdir path that are special inside a quoted CMake string must be escaped in
    # the generated file.
    topdir = tmp_path / 'we"ird$dir'
    manifest = Manifest.from_data(
        {"manifest": {"members": [{"name": "m", "url": "u", "cmake-packages": ["Pkg"]}]}},
        topdir=str(topdir),
    )
    text = packages_cmake_text(manifest, "hash")
    (line,) = [l for l in text.splitlines() if "Pkg_ROOT}" in l and "set" in l]
    assert '\\"' in line
    assert "\\$" in line


def test_packages_cmake_from_data_with_topdir(tmp_path):
    import os

    from repospace.app.generate import package_roots_text
    from repospace.manifest import Manifest

    # In-memory manifest data with a topdir: the manifest repository is the topdir itself, so its
    # own packages have a root too.
    manifest = Manifest.from_data(
        {"manifest": {"self": {"cmake-packages": ["App"]}}},
        topdir=str(tmp_path),
    )
    assert manifest.members[0].path == "."
    root = os.path.realpath(tmp_path).replace("\\", "/")
    assert f'set(ENV{{App_ROOT}} "{root}")' in package_roots_text(manifest)


def test_packages_cmake_without_topdir_fails_cleanly():
    from repospace.app.generate import packages_cmake_text
    from repospace.manifest import Manifest

    # Every package root is a path under the topdir; without one there is nothing to generate, and
    # saying so beats an AttributeError.
    manifest = Manifest.from_data(
        {"manifest": {"members": [{"name": "m", "url": "u", "cmake-packages": ["Pkg"]}]}}
    )
    with pytest.raises(ValueError, match="topdir"):
        packages_cmake_text(manifest, "hash")
