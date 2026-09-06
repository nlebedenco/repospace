"""Integration tests for repospace init, in place and from a clone."""

from __future__ import annotations

import os
import stat
import subprocess

import pytest

# -- init in place ----------------------------------------------------------


def test_init_here(topology, run_repospace):
    code, out, err = run_repospace(["init"], cwd=topology.app)
    assert code == 0, err
    assert (topology.app / ".repospace").is_dir()
    assert "Initialized" in out
    code, out, err = run_repospace(["topdir"], cwd=topology.app)
    assert out.strip() == str(topology.app)


def test_init_with_dir(topology, run_repospace, tmp_path):
    code, out, err = run_repospace(["init", str(topology.app)], cwd=tmp_path)
    assert code == 0, err
    assert (topology.app / ".repospace").is_dir()
    assert not (tmp_path / ".repospace").exists()


def test_init_found_from_nested_directory(topology, run_repospace):
    code, out, err = run_repospace(["init"], cwd=topology.app)
    assert code == 0, err
    nested = topology.app / "scripts"
    nested.mkdir()
    code, out, err = run_repospace(["topdir"], cwd=nested)
    assert out.strip() == str(topology.app)


def test_init_requires_manifest(tmp_path, run_repospace):
    empty = tmp_path / "empty"
    empty.mkdir()
    code, out, err = run_repospace(["init"], cwd=empty)
    assert code == 1
    assert "manifest file not found" in err
    assert not (empty / ".repospace").exists()


def test_init_nonexistent_location_attempts_clone(tmp_path, run_repospace):
    # A first argument that is not an existing directory is treated as a repository; here git clone
    # fails.
    code, out, err = run_repospace(["init", "ghost"], cwd=tmp_path)
    assert code != 0
    assert "does not exist" in err
    assert not (tmp_path / ".repospace").exists()


def test_init_in_place_warns_about_git_options(topology, run_repospace):
    code, out, err = run_repospace(
        ["init", str(topology.app), "--", "--branch", "x"],
        cwd=topology.app,
    )
    assert code == 0, err
    assert (topology.app / ".repospace").is_dir()
    assert "WARNING" in err
    assert "git clone was not invoked" in err


def test_init_in_place_warns_without_location(topology, run_repospace):
    # "init -- OPTS" initializes the current directory in place; the options are ignored with a
    # warning.
    code, out, err = run_repospace(["init", "--", "--branch", "x"], cwd=topology.app)
    assert code == 0, err
    assert (topology.app / ".repospace").is_dir()
    assert "git clone was not invoked" in err


def test_init_in_place_rejects_directory_argument(topology, run_repospace):
    code, out, err = run_repospace(["init", str(topology.app), "dest"], cwd=topology.app)
    assert code == 1
    assert "nothing to clone" in err
    assert not (topology.app / ".repospace").exists()


def test_init_refuses_repospace_file(tmp_path, run_repospace):
    where = tmp_path / "dir"
    where.mkdir()
    (where / "repospace.yaml").write_text("manifest:\n")
    (where / ".repospace").write_text("not a directory\n")
    code, out, err = run_repospace(["init"], cwd=where)
    assert code == 1
    assert "exists and is not a directory" in err


def test_init_refuses_existing_repospace(topology, run_repospace):
    code, out, err = run_repospace(["init"], cwd=topology.app)
    assert code == 0, err
    code, out, err = run_repospace(["init"], cwd=topology.app)
    assert code == 1
    assert "already in a repospace" in err


def test_init_writes_no_config(topology, run_repospace):
    code, out, err = run_repospace(["init"], cwd=topology.app)
    assert code == 0, err
    # .repospace/ presence alone defines the repospace; no bootstrap configuration is needed.
    assert list((topology.app / ".repospace").iterdir()) == []


def test_init_with_alternate_manifest(topology, run_repospace):
    (topology.app / "manifests").mkdir()
    (topology.app / "manifests" / "alt.yaml").write_text(
        "manifest:\n"
        "  members:\n"
        "    - name: libb\n"
        f"      url: {topology.url(topology.libb_src)}\n"
        "      revision: v1.0\n"
    )
    code, out, err = run_repospace(["init", "--manifest", "manifests/alt.yaml"], cwd=topology.app)
    assert code == 0, err
    code, out, err = run_repospace(["manifest", "--path"], cwd=topology.app)
    assert out.strip() == str(topology.app / "manifests" / "alt.yaml")
    code, out, err = run_repospace(["update"], cwd=topology.app)
    assert code == 0, err
    assert (topology.app / "libb" / "libb.txt").is_file()
    code, out, err = run_repospace(["list"], cwd=topology.app)
    names = [line.split()[0] for line in out.splitlines()]
    assert names == ["manifest", "libb"]


def test_init_manifest_flag_validation(topology, run_repospace):
    code, out, err = run_repospace(["init", "--manifest", "/abs.yaml"], cwd=topology.app)
    assert code == 1
    assert "relative" in err
    code, out, err = run_repospace(["init", "--manifest", "../evil.yaml"], cwd=topology.app)
    assert code == 1
    assert "escapes" in err
    code, out, err = run_repospace(["init", "--manifest", "ghost.yaml"], cwd=topology.app)
    assert code == 1
    assert "manifest file not found" in err
    assert not (topology.app / ".repospace").exists()


# -- init from a cloned repository ------------------------------------------


def test_clone_uses_self_name(topology, run_repospace, tmp_path):
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    code, out, err = run_repospace(["init", topology.url(topology.app)], cwd=fresh)
    assert code == 0, err
    # self: name: is "app"; the repospace is inside the clone.
    assert (fresh / "app" / "repospace.yaml").is_file()
    assert (fresh / "app" / ".repospace").is_dir()
    assert not (fresh / ".repospace").exists()


def test_clone_relays_repospace_quiet(topology, run_repospace, tmp_path):
    # "repospace -q init" relays -q to git clone.
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    code, out, err = run_repospace(["-q", "init", topology.url(topology.app)], cwd=fresh)
    assert code == 0, err
    assert (fresh / "app" / ".repospace").is_dir()
    assert "Cloning into" not in err
    assert out == ""


def test_clone_with_directory(topology, run_repospace, tmp_path):
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    code, out, err = run_repospace(["init", topology.url(topology.app), "nested/dest"], cwd=fresh)
    assert code == 0, err
    target = fresh / "nested" / "dest"
    assert (target / "repospace.yaml").is_file()
    assert (target / ".repospace").is_dir()


def test_clone_url_basename_fallback(topology, run_repospace, repos, tmp_path):
    # A manifest without self:name clones to the URL basename, .git suffix stripped -- like git
    # clone.
    plain = repos.create("plain-src", {"repospace.yaml": "manifest:\n"})
    bare = repos.bare_clone(plain)
    assert bare.name == "plain-src.git"
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    code, out, err = run_repospace(["init", topology.url(bare)], cwd=fresh)
    assert code == 0, err
    assert (fresh / "plain-src" / ".repospace").is_dir()


def test_clone_with_alternate_manifest(topology, run_repospace, tmp_path):
    topology.repos.commit(
        topology.app,
        {"alt.yaml": "manifest:\n  self:\n    name: alt-app\n"},
        "add alt manifest",
    )
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    code, out, err = run_repospace(
        ["init", "--manifest", "alt.yaml", topology.url(topology.app)],
        cwd=fresh,
    )
    assert code == 0, err
    dest = fresh / "alt-app"
    assert (dest / ".repospace").is_dir()
    # The selection was recorded as the manifest.file option.
    code, out, err = run_repospace(["manifest", "--path"], cwd=dest)
    assert out.strip() == str(dest / "alt.yaml")


def test_clone_missing_alternate_manifest(topology, run_repospace, tmp_path):
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    code, out, err = run_repospace(
        ["init", "--manifest", "nope.yaml", topology.url(topology.app)],
        cwd=fresh,
    )
    assert code == 1
    assert "has no nope.yaml" in err
    assert list(fresh.glob(".repospace-clone-*")) == []


def test_clone_dir_name_git_dir_suffix():
    # "host/foo/.git" names the foo repository, like git clone; a bare ".git" never becomes the
    # destination directory.
    from repospace.app.init import _clone_dir_name

    assert _clone_dir_name("https://host/foo/.git") == "foo"
    assert _clone_dir_name("https://host/foo/bar.git") == "bar"
    assert _clone_dir_name("git@host:foo/.git") == "foo"


def test_clone_url_dotgit_directory_fallback(topology, run_repospace, repos, tmp_path):
    # A URL whose basename is ".git" clones to the parent component, never to a ".git" directory in
    # the cwd.
    plain = repos.create("dotgit-src", {"repospace.yaml": "manifest:\n"})
    bare = repos.bare_clone(plain, name="proj/.git")
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    code, out, err = run_repospace(["init", topology.url(bare)], cwd=fresh)
    assert code == 0, err
    assert (fresh / "proj" / ".repospace").is_dir()
    assert not (fresh / ".git").exists()


def test_clone_git_options_passthrough(topology, run_repospace, tmp_path):
    # The marker is committed on the release branch only, so the clone can contain it only if
    # --branch release actually reached git clone.
    subprocess.run(["git", "-C", str(topology.app), "switch", "-q", "-c", "release"], check=True)
    topology.repos.commit(topology.app, {"marker.txt": "on-branch\n"}, "branch commit")
    subprocess.run(["git", "-C", str(topology.app), "switch", "-q", "main"], check=True)
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    code, out, err = run_repospace(
        [
            "init",
            topology.url(topology.app),
            "--",
            "--branch",
            "release",
        ],
        cwd=fresh,
    )
    assert code == 0, err
    assert (fresh / "app" / "marker.txt").is_file()


def test_clone_refuses_nonempty_target(topology, run_repospace, tmp_path):
    fresh = tmp_path / "fresh"
    (fresh / "app").mkdir(parents=True)
    (fresh / "app" / "keep.txt").write_text("keep\n")
    code, out, err = run_repospace(["init", topology.url(topology.app)], cwd=fresh)
    assert code == 1
    assert "refusing to overwrite" in err
    assert (fresh / "app" / "keep.txt").is_file()
    # The temporary clone was cleaned up.
    assert list(fresh.glob(".repospace-clone-*")) == []


def test_clone_accepts_empty_target(topology, run_repospace, tmp_path):
    # An existing empty directory is a valid destination, like git clone.
    fresh = tmp_path / "fresh"
    (fresh / "app").mkdir(parents=True)
    code, out, err = run_repospace(["init", topology.url(topology.app)], cwd=fresh)
    assert code == 0, err
    assert (fresh / "app" / ".repospace").is_dir()


def test_clone_with_directory_accepts_empty_target(topology, run_repospace, tmp_path):
    fresh = tmp_path / "fresh"
    (fresh / "dest").mkdir(parents=True)
    code, out, err = run_repospace(["init", topology.url(topology.app), "dest"], cwd=fresh)
    assert code == 0, err
    assert (fresh / "dest" / ".repospace").is_dir()


def test_clone_failure_restores_preexisting_empty_target(topology, run_repospace, repos, tmp_path):
    src = repos.create("no-manifest-keep", {"README.md": "hi\n"})
    fresh = tmp_path / "fresh"
    (fresh / "dest").mkdir(parents=True)
    code, out, err = run_repospace(["init", topology.url(src), "dest"], cwd=fresh)
    assert code == 1
    assert "has no repospace.yaml" in err
    # The pre-existing (empty) destination was left as it was.
    assert (fresh / "dest").is_dir()
    assert list((fresh / "dest").iterdir()) == []


def test_clone_requires_manifest(topology, run_repospace, repos, tmp_path):
    bare = repos.create("no-manifest", {"README.md": "hi\n"})
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    code, out, err = run_repospace(["init", topology.url(bare)], cwd=fresh)
    assert code == 1
    assert "has no repospace.yaml" in err
    assert list(fresh.glob(".repospace-clone-*")) == []


def test_clone_with_directory_requires_manifest(topology, run_repospace, repos, tmp_path):
    src = repos.create("no-manifest-dir", {"README.md": "hi\n"})
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    code, out, err = run_repospace(["init", topology.url(src), "dest"], cwd=fresh)
    assert code == 1
    assert "has no repospace.yaml" in err
    # The failed clone was not left behind.
    assert not (fresh / "dest").exists()


def test_clone_refuses_committed_repospace_dir(topology, run_repospace, repos, tmp_path):
    src = repos.create(
        "has-metadata",
        {"repospace.yaml": "manifest:\n", ".repospace/config": ""},
    )
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    code, out, err = run_repospace(["init", topology.url(src)], cwd=fresh)
    assert code == 1
    assert "already contains .repospace" in err
    assert list(fresh.glob(".repospace-clone-*")) == []
    code, out, err = run_repospace(["init", topology.url(src), "dest"], cwd=fresh)
    assert code == 1
    assert "already contains .repospace" in err
    assert not (fresh / "dest").exists()


def test_clone_inside_repospace_fails(topology, run_repospace):
    code, out, err = run_repospace(["init"], cwd=topology.app)
    assert code == 0, err
    code, out, err = run_repospace(["init", topology.url(topology.liba_src)], cwd=topology.app)
    assert code == 1
    assert "already in a repospace" in err


def test_clone_into_directory_inside_repospace_fails(topology, run_repospace, tmp_path):
    # The check covers DIRECTORY too, not only the current directory: nothing may plant a repospace
    # inside another one.
    outer = tmp_path / "outer"
    (outer / ".repospace").mkdir(parents=True)
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    code, out, err = run_repospace(
        ["init", topology.url(topology.app), str(outer / "sub" / "dest")],
        cwd=fresh,
    )
    assert code == 1
    assert "already in a repospace" in err
    assert str(outer) in err
    # Nothing was cloned.
    assert not (outer / "sub").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_clone_root_gets_umask_mode(topology, run_repospace, tmp_path):
    # Without DIRECTORY the clone goes through a temporary directory that mkdtemp creates 0700; the
    # repospace root must not keep that.
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    old = os.umask(0o022)
    try:
        code, out, err = run_repospace(["init", topology.url(topology.app)], cwd=fresh)
    finally:
        os.umask(old)
    assert code == 0, err
    dest = fresh / "app"
    assert (dest / ".repospace").is_dir()
    assert stat.S_IMODE(dest.stat().st_mode) == 0o755


def test_clone_rename_failure_restores_empty_target(topology, run_repospace, tmp_path, monkeypatch):
    # The pre-existing empty destination is removed just before the clone is renamed onto it; a
    # failed rename must put it back.
    fresh = tmp_path / "fresh"
    (fresh / "app").mkdir(parents=True)

    def no_rename(*args, **kwargs):
        raise OSError("rename refused")

    monkeypatch.setattr(os, "rename", no_rename)
    code, out, err = run_repospace(["init", topology.url(topology.app)], cwd=fresh)
    assert code == 1
    assert "cannot move the clone" in err
    assert (fresh / "app").is_dir()
    assert list((fresh / "app").iterdir()) == []
    assert list(fresh.glob(".repospace-clone-*")) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_clone_failure_keeps_destination_directory(topology, run_repospace, repos, tmp_path):
    # "init URL ." undoing itself must empty the current directory, not delete and recreate it: the
    # inode other processes are sitting in, and its mode and ownership, have to survive.
    src = repos.create("no-manifest-here", {"README.md": "hi\n"})
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    fresh.chmod(0o750)
    before = fresh.stat()
    code, out, err = run_repospace(["init", topology.url(src), "."], cwd=fresh)
    assert code == 1
    assert "has no repospace.yaml" in err
    after = fresh.stat()
    assert after.st_ino == before.st_ino
    assert stat.S_IMODE(after.st_mode) == 0o750
    assert list(fresh.iterdir()) == []
