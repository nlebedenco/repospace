"""Integration tests for repospace update against local git remotes."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest
from conftest import git_bytes, git_out

from repospace.manifest import (
    MANIFEST_REV,
    QUAL_MANIFEST_REV,
    Manifest,
    ManifestImportFailed,
    Member,
    member_manifest_content,
)


def update(run_repospace, repospace, *args, expect=0):
    code, out, err = run_repospace(["update", *args], cwd=repospace.ws)
    assert code == expect, err
    return out, err


def test_full_update(repospace, run_repospace):
    update(run_repospace, repospace)
    ws = repospace.ws
    # Direct members and the import-provided member are all cloned.
    assert (ws / "liba" / "liba.txt").is_file()
    assert (ws / "libb" / "libb.txt").is_file()
    assert (ws / "libc" / "libc.txt").is_file()
    # Detached HEAD at the manifest revision, repospace-rev set.
    for name in ("liba", "libb", "libc"):
        assert git_out(ws / name, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
        assert git_out(ws / name, "rev-parse", QUAL_MANIFEST_REV)
    # libb is at its tag.
    tag_sha = git_out(repospace.libb_src, "rev-parse", "v1.0^{commit}")
    assert git_out(ws / "libb", "rev-parse", "HEAD") == tag_sha
    # No scratch refs left behind.
    assert git_out(ws / "liba", "for-each-ref", "refs/repospace") == ""


def test_refspec_in_revision_is_refused_before_any_fetch(repospace, run_repospace):
    # A manifest revision is fetched as a refspec; "main:refs/heads/work" would force-update the
    # member's local "work" branch to the remote's main. Validation refuses it before git is
    # involved.
    repospace.rewrite_app_yaml(
        extra_members=(
            "    - name: lw\n"
            f"      url: {repospace.url(repospace.libc_src)}\n"
            "      revision: 'main:refs/heads/work'\n"
        )
    )
    out, err = update(run_repospace, repospace, expect=1)
    assert "cannot resolve the manifest" in err
    assert "contains ':'" in err
    assert not (repospace.ws / "lw").exists()


def test_generate_write_failure_fails_cleanly(repospace, run_repospace):
    # A repospace-file write failure must surface as a clean error, not a traceback.
    rdir = repospace.ws / ".repospace"
    rdir.chmod(0o500)
    try:
        if os.access(rdir, os.W_OK):
            pytest.skip("cannot make the directory unwritable (root?)")
        code, out, err = run_repospace(["update"], cwd=repospace.ws)
    finally:
        rdir.chmod(0o755)
    assert code == 1
    assert "cannot write repospace files" in err
    assert "Traceback" not in err


def test_update_member_path_occupied_by_file(repospace, run_repospace):
    # A plain file where a member should be cloned makes makedirs raise OSError; that must fail the
    # member cleanly, not as a traceback.
    (repospace.ws / "libb").write_text("in the way\n")
    code, out, err = run_repospace(["update"], cwd=repospace.ws)
    assert code == 1
    assert "cannot update libb (libb)" in err
    assert "update failed for: libb (libb)" in err
    assert "Traceback" not in err
    # The healthy members were still updated.
    assert (repospace.ws / "liba" / "liba.txt").is_file()
    assert (repospace.ws / "libc" / "libc.txt").is_file()


def test_named_update_member_path_occupied_by_file(repospace, run_repospace):
    (repospace.ws / "libb").write_text("in the way\n")
    code, out, err = run_repospace(["update", "libb"], cwd=repospace.ws)
    assert code == 1
    assert "cannot update libb (libb)" in err
    assert "Traceback" not in err


def test_update_imported_member_path_occupied_by_file(repospace, run_repospace):
    # liba's manifest data is needed during resolution itself, so the failure cannot be aggregated;
    # it must still die cleanly.
    (repospace.ws / "liba").write_text("in the way\n")
    code, out, err = run_repospace(["update"], cwd=repospace.ws)
    assert code == 1
    assert "cannot resolve the manifest" in err
    assert "imported member liba (liba)" in err
    assert "Traceback" not in err


def test_attribution_after_update(repospace, run_repospace):
    update(run_repospace, repospace)
    manifest = Manifest.from_topdir(str(repospace.ws))
    by_name = {m.name: m for m in manifest.members}
    liba = by_name["liba"]
    # From liba's own manifest "self:" section.
    assert liba.cmake_packages == ["LibA"]
    assert liba.extension_commands == ["repospace-commands.yaml"]
    assert by_name["libc"].declared_by == "liba"
    assert by_name["manifest"].cmake_packages == ["App"]


def test_update_sha_revision(repospace, run_repospace):
    update(run_repospace, repospace)
    # A commit past v1.0, so the pinned SHA differs from the commit the first update already checked
    # out; the final assertion can only hold if the second update actually moved HEAD to the SHA.
    sha = repospace.repos.commit(repospace.libb_src, {"beyond.txt": "b\n"}, "beyond v1.0")
    assert sha != git_out(repospace.ws / "libb", "rev-parse", "HEAD")
    repospace.rewrite_app_yaml()
    yaml_file = repospace.app / "repospace.yaml"
    yaml_file.write_text(yaml_file.read_text().replace("revision: v1.0", f"revision: {sha}"))
    update(run_repospace, repospace)
    assert git_out(repospace.ws / "libb", "rev-parse", "HEAD") == sha


def test_update_all_hex_branch_name(repospace, run_repospace, repos):
    # "beef" is misdetected as a SHA, widening the fetch; the revision must still resolve through
    # the scratch ref.
    hex_src = repos.create("hex-src", {"hex.txt": "x\n"}, branch="beef")
    repospace.rewrite_app_yaml(
        extra_members=(
            "    - name: hexed\n" f"      url: {repospace.url(hex_src)}\n" "      revision: beef\n"
        )
    )
    update(run_repospace, repospace)
    hexed = repospace.ws / "hexed"
    assert (hexed / "hex.txt").is_file()
    assert git_out(hexed, "rev-parse", QUAL_MANIFEST_REV) == git_out(hex_src, "rev-parse", "beef")
    assert git_out(hexed, "for-each-ref", "refs/repospace") == ""


def test_quiet_update_silences_git(repospace, run_repospace):
    code, out, err = run_repospace(["-q", "update"], cwd=repospace.ws)
    assert code == 0, err
    assert out == ""
    assert err == ""


def test_reupdate_follows_branch(repospace, run_repospace):
    update(run_repospace, repospace)
    new_sha = repospace.repos.commit(repospace.liba_src, {"new.txt": "new\n"}, "advance")
    update(run_repospace, repospace)
    assert git_out(repospace.ws / "liba", "rev-parse", "HEAD") == new_sha
    assert (repospace.ws / "liba" / "new.txt").is_file()


def test_smart_fetch_skips_network_for_tag(repospace, run_repospace):
    update(run_repospace, repospace)
    # With the remote gone, updating the tag-pinned member still works because the tag peels locally
    # and no fetch happens.
    shutil.rmtree(repospace.libb_src)
    update(run_repospace, repospace, "libb")


def test_keep_descendants(repospace, run_repospace):
    update(run_repospace, repospace)
    libb = repospace.ws / "libb"
    subprocess.run(["git", "-C", str(libb), "checkout", "-q", "-b", "work"], check=True)
    repospace.repos.commit(libb, {"local.txt": "mine\n"}, "local work")
    out, err = update(run_repospace, repospace, "-k", "libb")
    assert git_out(libb, "rev-parse", "--abbrev-ref", "HEAD") == "work"
    assert (libb / "local.txt").is_file()


def test_rebase(repospace, run_repospace):
    update(run_repospace, repospace)
    liba = repospace.ws / "liba"
    subprocess.run(
        ["git", "-C", str(liba), "checkout", "-q", "-b", "feature"],
        check=True,
    )
    repospace.repos.commit(liba, {"feature.txt": "f\n"}, "feature work")
    remote_sha = repospace.repos.commit(repospace.liba_src, {"upstream.txt": "u\n"}, "upstream")
    update(run_repospace, repospace, "-r", "liba")
    assert git_out(liba, "rev-parse", "--abbrev-ref", "HEAD") == "feature"
    assert (liba / "feature.txt").is_file()
    assert (liba / "upstream.txt").is_file()
    merge_base = git_out(liba, "merge-base", "HEAD", remote_sha)
    assert merge_base == remote_sha


def test_group_filter(repospace, run_repospace):
    repospace.rewrite_app_yaml(
        extra_members=(
            "    - name: libd\n"
            f"      url: {repospace.url(repospace.libc_src)}\n"
            "      groups: [opt]\n"
        ),
        group_filter="-opt",
    )
    update(run_repospace, repospace)
    assert not (repospace.ws / "libd").exists()
    update(run_repospace, repospace, "--group-filter", "+opt")
    assert (repospace.ws / "libd" / "libc.txt").is_file()


def test_named_update_only_updates_named(repospace, run_repospace):
    update(run_repospace, repospace, "libb")
    assert (repospace.ws / "libb" / "libb.txt").is_file()
    assert not (repospace.ws / "liba").exists()


def test_named_update_of_imported_member_requires_full(repospace, run_repospace):
    code, out, err = run_repospace(["update", "libc"], cwd=repospace.ws)
    assert code == 1
    assert "plain" in err


def test_update_never_deletes(repospace, run_repospace):
    update(run_repospace, repospace)
    repospace.rewrite_app_yaml()
    yaml_file = repospace.app / "repospace.yaml"
    text = yaml_file.read_text()
    # Remove libb from the manifest entirely.
    lines = [line for line in text.splitlines() if "libb" not in line and "v1.0" not in line]
    yaml_file.write_text("\n".join(lines) + "\n")
    update(run_repospace, repospace)
    assert (repospace.ws / "libb" / "libb.txt").is_file()


def host_with_submodule(repos):
    """Create a repo containing a submodule "the-sub"; return its path."""
    sub_src = repos.create("sub-src", {"sub.txt": "s\n"})
    host_src = repos.create("host-src", {"host.txt": "h\n"})
    subprocess.run(
        [
            "git",
            "-C",
            str(host_src),
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            f"file://{sub_src}",
            "the-sub",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["git", "-C", str(host_src), "commit", "-q", "-m", "add submodule"],
        check=True,
    )
    return host_src


def repoint_submodule(repos, host_src):
    """Repoint host-src's submodule at a second, equivalent remote.

    Returns the new URL. It is a real clone of the original, so the with-sync path can adopt it and
    still update the submodule.
    """
    alt = repos.bare_clone(repos.base / "sub-src", "sub-alt.git")
    url = f"file://{alt}"
    gitmodules = host_src / ".gitmodules"
    gitmodules.write_text(gitmodules.read_text().replace(f"file://{repos.base / 'sub-src'}", url))
    subprocess.run(
        ["git", "-C", str(host_src), "commit", "-q", "-am", "repoint"],
        check=True,
    )
    return url


def submodule_url(clone):
    """The submodule URL configured in *clone*, as sync would set it."""
    return git_out(clone, "config", "--get", "submodule.the-sub.url")


def test_update_with_submodules(repospace, run_repospace, repos):
    host_src = host_with_submodule(repos)
    repospace.rewrite_app_yaml(
        extra_members=(
            "    - name: host\n"
            f"      url: {repospace.url(host_src)}\n"
            "      submodules: true\n"
        )
    )
    update(run_repospace, repospace)
    host = repospace.ws / "host"
    assert (host / "the-sub" / "sub.txt").is_file()
    # A URL change in the host's .gitmodules reaches the clone's own configuration, which only
    # "submodule sync" copies it into.
    new_url = repoint_submodule(repos, host_src)
    assert submodule_url(host) != new_url
    update(run_repospace, repospace)
    assert submodule_url(host) == new_url


def test_update_with_submodule_list(repospace, run_repospace, repos):
    host_src = host_with_submodule(repos)
    repospace.rewrite_app_yaml(
        extra_members=(
            "    - name: host\n"
            f"      url: {repospace.url(host_src)}\n"
            "      submodules:\n"
            "        - path: the-sub\n"
        )
    )
    update(run_repospace, repospace)
    assert (repospace.ws / "host" / "the-sub" / "sub.txt").is_file()


def test_update_submodules_without_sync(repospace, run_repospace, repos):
    host_src = host_with_submodule(repos)
    repospace.rewrite_app_yaml(
        extra_members=(
            "    - name: host\n"
            f"      url: {repospace.url(host_src)}\n"
            "      submodules: true\n"
        )
    )
    run_repospace(["config", "update.sync-submodules", "false"], cwd=repospace.ws)
    update(run_repospace, repospace)
    host = repospace.ws / "host"
    assert (host / "the-sub" / "sub.txt").is_file()
    old_url = submodule_url(host)
    # Without sync, a URL change in the host's .gitmodules must not reach the clone's own submodule
    # configuration.
    new_url = repoint_submodule(repos, host_src)
    assert new_url != old_url
    update(run_repospace, repospace)
    assert (host / "the-sub" / "sub.txt").is_file()
    assert submodule_url(host) == old_url


def test_looks_like_sha():
    from repospace.app.update import _looks_like_sha

    assert _looks_like_sha("beef")
    assert _looks_like_sha("a" * 40)
    assert _looks_like_sha("a" * 64)
    assert not _looks_like_sha("")
    assert not _looks_like_sha("a" * 65)
    assert not _looks_like_sha("v1.0")


def test_rev_type_non_commit_object(repos):
    from repospace.app.update import Update
    from repospace.manifest import Member

    repos.create("tree-src", {"f.txt": "x\n"})
    member = Member("tree-src", url="unused", topdir=str(repos.base))
    assert Update()._rev_type(member, "HEAD^{tree}") == "other"
    assert Update()._rev_type(member, "nonexistent") == "other"


def test_fetch_flag_always(repospace, run_repospace):
    update(run_repospace, repospace)
    # smart would skip the network for the tag; always must hit it.
    shutil.rmtree(repospace.libb_src)
    code, out, err = run_repospace(["update", "-f", "always", "libb"], cwd=repospace.ws)
    assert code == 1
    assert "update failed for: libb" in err


def test_fetch_config_always(repospace, run_repospace):
    update(run_repospace, repospace)
    run_repospace(["config", "update.fetch", "always"], cwd=repospace.ws)
    shutil.rmtree(repospace.libb_src)
    code, out, err = run_repospace(["update", "libb"], cwd=repospace.ws)
    assert code == 1
    assert "update failed for: libb" in err


def test_fetch_config_invalid_falls_back_to_smart(repospace, run_repospace):
    update(run_repospace, repospace)
    run_repospace(["config", "update.fetch", "bogus"], cwd=repospace.ws)
    shutil.rmtree(repospace.libb_src)
    out, err = update(run_repospace, repospace, "libb")
    assert 'ignoring invalid update.fetch value "bogus"' in err


def test_group_filter_invalid_item(repospace, run_repospace):
    code, out, err = run_repospace(["update", "--group-filter", "opt"], cwd=repospace.ws)
    assert code == 1
    assert 'begin with "+" or "-"' in err


def test_group_filter_invalid_group_name(repospace, run_repospace):
    code, out, err = run_repospace(["update", "--group-filter", "+bad name"], cwd=repospace.ws)
    assert code == 1
    assert "invalid group name" in err


def test_group_filter_blank_items_ignored(repospace, run_repospace):
    update(run_repospace, repospace, "--group-filter", " , ", "libb")
    assert (repospace.ws / "libb" / "libb.txt").is_file()


def test_named_update_of_imported_member_after_full(repospace, run_repospace):
    update(run_repospace, repospace)
    new_sha = repospace.repos.commit(repospace.libc_src, {"more.txt": "m\n"}, "advance")
    update(run_repospace, repospace, "libc")
    assert git_out(repospace.ws / "libc", "rev-parse", "HEAD") == new_sha


def test_named_update_uses_winning_definition(repospace, run_repospace):
    # A member import nested under a self-import registers its members before the top-level file's
    # own, so its definition of a name wins (first definition wins). A named update must resolve
    # that same winner, not the shadowed top-level definition.
    app = repospace.app
    (app / "extra.yaml").write_text(
        "manifest:\n"
        "  members:\n"
        "    - name: liba\n"
        f"      url: {repospace.url(repospace.liba_src)}\n"
        "      import: true\n"
    )
    old_sha = repospace.repos.head(repospace.libc_src)
    (app / "repospace.yaml").write_text(
        "manifest:\n"
        "  self:\n"
        "    import: extra.yaml\n"
        "  members:\n"
        "    - name: libc\n"
        f"      url: {repospace.url(repospace.libc_src)}\n"
        f"      revision: {old_sha}\n"
    )
    update(run_repospace, repospace)
    new_sha = repospace.repos.commit(repospace.libc_src, {"more.txt": "m\n"}, "advance")
    # liba's manifest declares libc at revision main; the shadowed top-level definition pins the old
    # commit.
    update(run_repospace, repospace, "libc")
    assert git_out(repospace.ws / "libc", "rev-parse", "HEAD") == new_sha


def test_named_update_unknown_member(repospace, run_repospace):
    update(run_repospace, repospace)
    code, out, err = run_repospace(["update", "nosuch"], cwd=repospace.ws)
    assert code == 1
    assert "unknown member(s): nosuch" in err


def test_manifest_repository_not_updatable(repospace, run_repospace):
    code, out, err = run_repospace(["update", "manifest"], cwd=repospace.ws)
    assert code == 1
    assert "manifest repository cannot be updated" in err


def test_manifest_repository_rejected_before_updating(repospace, run_repospace):
    # The rejection comes before any member moves: dying halfway through would leave the members
    # named earlier updated but unrecorded, since the generated files are never written.
    code, out, err = run_repospace(["update", "libb", "manifest"], cwd=repospace.ws)
    assert code == 1
    assert "manifest repository cannot be updated" in err
    assert not (repospace.ws / "libb").exists()


def test_named_update_malformed_manifest(repospace, run_repospace):
    (repospace.app / "repospace.yaml").write_text("manifest:\n  bogus: 1\n")
    code, out, err = run_repospace(["update", "libb"], cwd=repospace.ws)
    assert code == 1
    assert "cannot resolve the manifest" in err


def test_member_self_import_pinned_to_repospace_rev(repospace, run_repospace):
    # liba's manifest self-imports extra.yaml. After update, editing extra.yaml on a local branch in
    # the cloned member must not change what resolution sees: member data is read at repospace-rev.
    liba = repospace.liba_src
    (liba / "repospace.yaml").write_text("manifest:\n  self:\n    import: extra.yaml\n")
    repospace.repos.commit(
        liba,
        {
            "extra.yaml": (
                "manifest:\n"
                "  members:\n"
                "    - name: libc\n"
                f"      url: {repospace.url(repospace.libc_src)}\n"
            )
        },
        "self-import",
    )
    update(run_repospace, repospace)
    assert (repospace.ws / "libc" / "libc.txt").is_file()
    work = repospace.ws / "liba"
    subprocess.run(
        ["git", "-C", str(work), "checkout", "-q", "-b", "experiment"],
        check=True,
    )
    repospace.repos.commit(
        work,
        {"extra.yaml": ("manifest:\n  members:\n    - name: rogue\n      url: u\n")},
        "local edit",
    )
    manifest = Manifest.from_topdir(str(repospace.ws))
    by_name = {m.name: m for m in manifest.members}
    assert "libc" in by_name
    assert "rogue" not in by_name


def test_member_directory_import_skips_subdirectories(repospace, run_repospace):
    # A subdirectory whose name ends in .yaml inside an imported directory is not manifest data and
    # must be ignored.
    repospace.repos.commit(
        repospace.liba_src,
        {
            "manifests/good.yaml": (
                "manifest:\n"
                "  members:\n"
                "    - name: libc\n"
                f"      url: {repospace.url(repospace.libc_src)}\n"
            ),
            "manifests/sub.yaml/inner.txt": "not manifest data\n",
        },
        "directory import",
    )
    yaml_file = repospace.app / "repospace.yaml"
    yaml_file.write_text(yaml_file.read_text().replace("import: true", "import: manifests"))
    update(run_repospace, repospace)
    assert (repospace.ws / "libc" / "libc.txt").is_file()


def test_update_import_file_deleted(repospace, run_repospace):
    update(run_repospace, repospace)
    (repospace.liba_src / "repospace.yaml").unlink()
    repospace.repos.commit(repospace.liba_src, {}, "drop manifest")
    code, out, err = run_repospace(["update"], cwd=repospace.ws)
    assert code == 1
    assert "can't import from member liba" in err
    assert "not found" in err


def test_update_help_has_description(run_repospace, tmp_path):
    code, out, err = run_repospace(["update", "-h"], cwd=tmp_path)
    assert code == 0
    assert "repospace-rev" in out


def test_update_stats(repospace, run_repospace):
    out, err = update(run_repospace, repospace, "--stats", "libb")
    assert "libb: updated in" in out


def test_update_old_git_init(repospace, run_repospace, monkeypatch):
    # git learned "init --initial-branch" in 2.28; the clone must be initialized without it below
    # that version. The running git accepts both spellings, so the argv itself is what the test
    # looks at.
    import repospace.app.update as update_mod
    import repospace.manifest as manifest_mod

    calls = []
    real_run_git = manifest_mod.run_git

    def recording_run_git(args, **kwargs):
        calls.append(list(args))
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(manifest_mod, "run_git", recording_run_git)

    def init_argvs():
        return [c for c in calls if c[0] == "init"]

    monkeypatch.setattr(update_mod, "git_version", lambda: (2, 27, 0))
    update(run_repospace, repospace, "libb")
    assert (repospace.ws / "libb" / "libb.txt").is_file()
    old = init_argvs()
    assert old and all("--initial-branch" not in argv for argv in old)

    calls.clear()
    monkeypatch.setattr(update_mod, "git_version", lambda: (2, 28, 0))
    update(run_repospace, repospace)
    assert (repospace.ws / "liba" / "liba.txt").is_file()
    new = init_argvs()
    assert new and all("--initial-branch" in argv for argv in new)


def test_smart_fetch_lightweight_tag(repospace, run_repospace, repos):
    lw_src = repos.create("lw-src", {"lw.txt": "x\n"})
    subprocess.run(["git", "-C", str(lw_src), "tag", "lw"], check=True)
    repospace.rewrite_app_yaml(
        extra_members=(
            "    - name: lw\n" f"      url: {repospace.url(lw_src)}\n" "      revision: lw\n"
        )
    )
    update(run_repospace, repospace)
    # A lightweight tag is a commit object with a refs/tags symbolic name; it must still qualify for
    # the smart fetch skip.
    shutil.rmtree(lw_src)
    update(run_repospace, repospace, "lw")


def test_update_fetches_when_revision_is_local_branch(repospace, run_repospace):
    update(run_repospace, repospace)
    liba = repospace.ws / "liba"
    subprocess.run(["git", "-C", str(liba), "branch", "main"], check=True)
    new_sha = repospace.repos.commit(repospace.liba_src, {"new.txt": "n\n"}, "advance")
    # The stale local branch "main" must not satisfy the smart strategy.
    update(run_repospace, repospace, "liba")
    assert git_out(liba, "rev-parse", "HEAD") == new_sha


def test_narrow_update(repospace, run_repospace):
    # A narrow fetch asks for the manifest revision alone: the remote's tags must not come along, as
    # they do in a full update.
    repospace.repos.tag(repospace.liba_src, "v-narrow")
    update(run_repospace, repospace, "-n")
    for name in ("liba", "libb", "libc"):
        assert (repospace.ws / name).is_dir()
    liba = repospace.ws / "liba"
    assert git_out(liba, "tag", "-l") == ""
    # The same update without -n passes --tags, and the tag arrives.
    update(run_repospace, repospace)
    assert git_out(liba, "tag", "-l") == "v-narrow"


def test_clone_depth(repospace, run_repospace, repos):
    deep_src = repos.create("deep-src", {"a.txt": "1\n"})
    repos.commit(deep_src, {"a.txt": "2\n"}, "second")
    repospace.rewrite_app_yaml(
        extra_members=(
            "    - name: deep\n" f"      url: {repospace.url(deep_src)}\n" "      clone-depth: 1\n"
        )
    )
    update(run_repospace, repospace)
    deep = repospace.ws / "deep"
    assert (deep / "a.txt").read_text() == "2\n"
    assert git_out(deep, "rev-list", "--count", "HEAD") == "1"


def test_update_sha_only_member(repospace, run_repospace, repos):
    sha_src = repos.create("sha-src", {"s.txt": "s\n"})
    sha = repos.head(sha_src)
    repospace.rewrite_app_yaml(
        extra_members=(
            "    - name: pinned\n"
            f"      url: {repospace.url(sha_src)}\n"
            f"      revision: {sha}\n"
        )
    )
    update(run_repospace, repospace)
    pinned = repospace.ws / "pinned"
    assert git_out(pinned, "rev-parse", "HEAD") == sha
    assert git_out(pinned, "for-each-ref", "refs/repospace") == ""


def test_update_unreachable_sha_fails(repospace, run_repospace, repos):
    lost_src = repos.create("lost-src", {"l.txt": "l\n"})
    repospace.rewrite_app_yaml(
        extra_members=(
            "    - name: lost\n"
            f"      url: {repospace.url(lost_src)}\n"
            f"      revision: {'d' * 40}\n"
        )
    )
    code, out, err = run_repospace(["update", "lost"], cwd=repospace.ws)
    assert code == 1
    assert "was not fetched" in err
    assert "update failed for: lost" in err
    # The failed fetch must not leave scratch refs behind.
    assert git_out(repospace.ws / "lost", "for-each-ref", "refs/repospace") == ""


def test_update_reports_leaving_branch(repospace, run_repospace):
    update(run_repospace, repospace)
    libb = repospace.ws / "libb"
    subprocess.run(["git", "-C", str(libb), "checkout", "-q", "-b", "work"], check=True)
    out, err = update(run_repospace, repospace, "libb")
    assert 'left branch "work"' in out
    assert git_out(libb, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"


def _checkout_manifest_rev(repo):
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", MANIFEST_REV], check=True)


def test_update_detaches_from_repospace_rev(repospace, run_repospace):
    # repospace-rev is repospace's own branch. Left checked out, update-ref would move it underneath
    # HEAD and the checkout that follows would find HEAD already at the new commit, leaving the
    # index and working tree at the old one (files shown as deleted).
    update(run_repospace, repospace)
    libc = repospace.ws / "libc"
    _checkout_manifest_rev(libc)
    new_sha = repospace.repos.commit(repospace.libc_src, {"new.txt": "n\n"}, "advance")
    out, err = update(run_repospace, repospace, "libc")
    assert f'detached HEAD from "{MANIFEST_REV}"' in out
    assert git_out(libc, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert git_out(libc, "rev-parse", "HEAD") == new_sha
    assert git_out(libc, "status", "--porcelain") == ""
    assert (libc / "new.txt").is_file()


@pytest.mark.parametrize("flag", ["-k", "-r"])
def test_update_never_keeps_or_rebases_repospace_rev(repospace, run_repospace, flag):
    # -k and -r are for the user's branches; repospace-rev is not one, and keeping or rebasing it
    # would leave the working tree stale.
    update(run_repospace, repospace)
    libc = repospace.ws / "libc"
    _checkout_manifest_rev(libc)
    new_sha = repospace.repos.commit(repospace.libc_src, {"new.txt": "n\n"}, "advance")
    out, err = update(run_repospace, repospace, flag, "libc")
    assert git_out(libc, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert git_out(libc, "rev-parse", "HEAD") == new_sha
    assert git_out(libc, "status", "--porcelain") == ""
    assert (libc / "new.txt").is_file()


def test_update_detaches_unborn_repospace_rev(repospace, run_repospace):
    # An unborn HEAD on repospace-rev (git checkout --orphan) has no commit to detach at; HEAD is
    # parked elsewhere so that creating the branch does not silently attach HEAD to it.
    update(run_repospace, repospace)
    libc = repospace.ws / "libc"
    subprocess.run(["git", "-C", str(libc), "branch", "-q", "-D", MANIFEST_REV], check=True)
    subprocess.run(
        ["git", "-C", str(libc), "checkout", "-q", "--orphan", MANIFEST_REV],
        check=True,
    )
    subprocess.run(["git", "-C", str(libc), "rm", "-rfq", "."], check=True)
    sha = git_out(repospace.libc_src, "rev-parse", "HEAD")
    out, err = update(run_repospace, repospace, "libc")
    assert git_out(libc, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert git_out(libc, "rev-parse", "HEAD") == sha
    assert git_out(libc, "status", "--porcelain") == ""
    assert (libc / "libc.txt").is_file()


def test_update_syncs_origin_url(repospace, run_repospace):
    # The manifest says where a member comes from; "origin" (owned by repospace) follows a URL
    # change, so plain git commands in the member do not keep talking to the old location.
    update(run_repospace, repospace)
    libb = repospace.ws / "libb"
    moved = repospace.repos.bare_clone(repospace.libb_src, "libb-moved.git")
    yaml_file = repospace.app / "repospace.yaml"
    yaml_file.write_text(
        yaml_file.read_text().replace(repospace.url(repospace.libb_src), repospace.url(moved))
    )
    out, err = update(run_repospace, repospace, "libb")
    assert 'remote "origin" now points at' in out
    assert git_out(libb, "remote", "get-url", "origin") == repospace.url(moved)
    # Unchanged: left alone, silently.
    out, err = update(run_repospace, repospace, "libb")
    assert "now points at" not in out
    # A hand-made clone may lack the remote; it is added back.
    subprocess.run(["git", "-C", str(libb), "remote", "remove", "origin"], check=True)
    out, err = update(run_repospace, repospace, "libb")
    assert 'remote "origin" now points at' in out
    assert git_out(libb, "remote", "get-url", "origin") == repospace.url(moved)


def test_fetch_opt_beginning_with_dash(repospace, run_repospace):
    # argparse cannot take "-o --prune" (the value looks like an option); the documented attached
    # form reaches git fetch.
    update(run_repospace, repospace, "--fetch-opt=--prune", "libb")
    assert (repospace.ws / "libb" / "libb.txt").is_file()


def test_update_failure_of_imported_member(repospace, run_repospace):
    yaml_file = repospace.app / "repospace.yaml"
    yaml_file.write_text(
        yaml_file.read_text().replace(f"file://{repospace.liba_src}", "file:///nowhere/at/all")
    )
    code, out, err = run_repospace(["update"], cwd=repospace.ws)
    assert code == 1
    assert "cannot resolve the manifest" in err
    assert "liba" in err


def test_update_failure_reports_member(repospace, run_repospace):
    repospace.rewrite_app_yaml(
        extra_members=("    - name: ghost\n      url: file:///nowhere/at/all\n")
    )
    code, out, err = run_repospace(["update"], cwd=repospace.ws)
    assert code == 1
    assert "update failed for" in err
    assert "ghost" in err
    # The healthy members were still updated.
    assert (repospace.ws / "libb" / "libb.txt").is_file()


def test_update_version_error_dies_cleanly(repospace, run_repospace):
    (repospace.app / "repospace.yaml").write_text("manifest:\n  version: '2.0'\n")
    code, out, err = run_repospace(["update"], cwd=repospace.ws)
    assert code == 1
    assert "cannot resolve the manifest" in err
    assert "please upgrade repospace" in err
    assert "Traceback" not in err


def test_member_manifest_content_missing_ref_vs_path(repos):
    repo = repos.create("content-src", {"repospace.yaml": "manifest:\n"})
    member = Member("content-src", url="unused", topdir=str(repos.base))
    # No repospace-rev at all: not the same as a missing file, whatever wording the installed git
    # uses.
    with pytest.raises(subprocess.CalledProcessError):
        member_manifest_content(member, "repospace.yaml")
    repos.branch(repo, MANIFEST_REV)
    with pytest.raises(FileNotFoundError):
        member_manifest_content(member, "nonexistent.yaml")
    assert member_manifest_content(member, "repospace.yaml") == ["manifest:\n"]


def test_member_import_failure_reasons(repos):
    # Without an importer, the failure message names the actual cause (not cloned, no repospace-rev,
    # file missing at repospace-rev) instead of a catch-all diagnosis blaming the clone.
    repo = repos.create("reason-src", {"repospace.yaml": "manifest:\n"})

    def load(topdir=None, imp="true"):
        return Manifest.from_data(
            "manifest:\n"
            "  members:\n"
            "    - name: reason-src\n"
            "      url: u\n"
            f"      import: {imp}\n",
            topdir=topdir,
        )

    with pytest.raises(ManifestImportFailed) as excinfo:
        load()  # No topdir: the member has no clone to read from.
    assert "member is not cloned" in str(excinfo.value)

    with pytest.raises(ManifestImportFailed) as excinfo:
        load(topdir=str(repos.base))
    assert f"member has no {QUAL_MANIFEST_REV}" in str(excinfo.value)

    repos.branch(repo, MANIFEST_REV)
    with pytest.raises(ManifestImportFailed) as excinfo:
        load(topdir=str(repos.base), imp="missing.yaml")
    assert f"not found at {QUAL_MANIFEST_REV}" in str(excinfo.value)

    # With the ref and the file both present, the import resolves. A Manifest is always truthy, so
    # name the members it resolved to.
    manifest = load(topdir=str(repos.base))
    assert [m.name for m in manifest.members] == ["manifest", "reason-src"]


def test_member_manifest_content_ignores_bare_dotfile(repos):
    # A file named just ".yml" has no suffix; the filesystem side of directory imports skips it, and
    # the git side must match.
    repo = repos.create(
        "dotfile-src",
        {
            "conf/.yml": "manifest:\n",
            "conf/a.yml": "manifest: # a\n",
        },
    )
    repos.branch(repo, MANIFEST_REV)
    member = Member("dotfile-src", url="unused", topdir=str(repos.base))
    assert member_manifest_content(member, "conf") == ["manifest: # a\n"]


def test_symlink_escape_blocked(repospace, run_repospace, tmp_path):
    # liba commits a symlink pointing outside the repospace and its imported manifest declares a
    # member whose path traverses it.
    outside = tmp_path / "outside"
    outside.mkdir()
    liba = repospace.liba_src
    (liba / "link").symlink_to(outside)
    yaml_file = liba / "repospace.yaml"
    yaml_file.write_text(
        yaml_file.read_text().replace(
            "    - name: libc\n",
            "    - name: libc\n      path: liba/link/evil\n",
        )
    )
    repospace.repos.commit(liba, {}, "add symlink")
    code, out, err = run_repospace(["update"], cwd=repospace.ws)
    assert code == 1
    assert "is a symbolic link" in err
    assert not (outside / "evil").exists()


def test_symlinked_member_directory_rejected(repospace, run_repospace, tmp_path):
    # Symlinks are valid only above the topdir. A member directory symlinked elsewhere is not the
    # user's escape hatch: nothing tells it apart from the same symlink committed by a member, so
    # both are refused.
    real = tmp_path / "elsewhere" / "libb"
    real.mkdir(parents=True)
    (repospace.ws / "libb").symlink_to(real)
    code, out, err = run_repospace(["update"], cwd=repospace.ws)
    assert code == 1
    assert "is a symbolic link" in err
    assert "may not go through one" in err
    assert "Traceback" not in err
    assert not (real / "libb.txt").exists()


def test_symlink_below_topdir_detection(tmp_path):
    from repospace.app.update import _symlink_below_topdir

    top = tmp_path / "top"
    (top / "a").mkdir(parents=True)
    (top / "link").symlink_to(top / "a")
    # Every symlink below the topdir is reported, whether or not a nested repository encloses it and
    # wherever it points, including the member directory itself...
    assert _symlink_below_topdir(str(top), "link/m") == str(top / "link")
    assert _symlink_below_topdir(str(top), "link") == str(top / "link")
    # ...and a path of real directories is not.
    assert _symlink_below_topdir(str(top), "a/m") is None
    assert _symlink_below_topdir(str(top), ".") is None


def test_symlink_above_topdir_allowed(repospace, run_repospace, tmp_path):
    # The policy stops at the topdir: a repospace reached through a symlinked parent is nobody's
    # business but the user's.
    link = tmp_path / "link-to-repospace"
    link.symlink_to(repospace.ws)
    code, out, err = run_repospace(["update"], cwd=link)
    assert code == 0, err
    assert (repospace.ws / "libb" / "libb.txt").is_file()


def test_member_selectable_by_path(repospace, run_repospace):
    update(run_repospace, repospace)
    code, out, err = run_repospace(["list", "./libb"], cwd=repospace.ws)
    assert code == 0, err
    assert "libb" in out


def test_colocated_update(topology, run_repospace):
    # Bare init inside the manifest repo: members clone into it.
    code, out, err = run_repospace(["init"], cwd=topology.app)
    assert code == 0, err
    code, out, err = run_repospace(["update"], cwd=topology.app)
    assert code == 0, err
    app = topology.app
    for name in ("liba", "libb", "libc"):
        assert (app / name).is_dir()
        assert git_out(app / name, "rev-parse", QUAL_MANIFEST_REV)
    import json

    data = json.loads((app / ".repospace" / "members.json").read_text())
    assert data["topdir"] == str(app)
    manifest_entry = data["members"][0]
    assert manifest_entry["path"] == "."
    assert manifest_entry["abspath"] == str(app)
    # Members show as untracked in the colocated manifest repo (hygiene is the user's .gitignore
    # responsibility, documented).
    status = git_out(app, "status", "--porcelain")
    assert "liba" in status


def test_update_blocks_symlink_redirect(repospace, run_repospace, repos, tmp_path):
    # A symlink committed inside a member must not redirect another member outside the repospace,
    # even on the very first update when the symlink only comes into existence mid-update.
    outside = tmp_path / "outside"
    outside.mkdir()
    evil_src = repos.create("evil-src")
    (evil_src / "sub").symlink_to(outside)
    subprocess.run(["git", "-C", str(evil_src), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(evil_src), "commit", "-q", "-m", "symlink"],
        check=True,
    )
    repospace.rewrite_app_yaml(
        extra_members=(
            "    - name: evil\n"
            f"      url: {repospace.url(evil_src)}\n"
            "    - name: victim\n"
            f"      url: {repospace.url(repospace.libb_src)}\n"
            "      revision: v1.0\n"
            "      path: evil/sub/victim\n"
        )
    )
    _, err = update(run_repospace, repospace, expect=1)
    assert "is a symbolic link" in err
    assert not (outside / "victim").exists()


def test_update_rejects_toplevel_symlink(repospace, run_repospace, repos):
    # A symlink the user creates at the top level is still below the topdir, so member paths may not
    # traverse it either.
    real = repos.base / "elsewhere"
    real.mkdir()
    (repospace.ws / "external").symlink_to(real)
    repospace.rewrite_app_yaml(
        extra_members=(
            "    - name: libd\n"
            f"      url: {repospace.url(repospace.libb_src)}\n"
            "      revision: v1.0\n"
            "      path: external/libd\n"
        )
    )
    _, err = update(run_repospace, repospace, expect=1)
    assert "is a symbolic link" in err
    assert not (real / "libd").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: non-UTF-8 ref names")
def test_update_non_utf8_branch_name(repospace, run_repospace):
    # git ref names are byte strings that need not be valid UTF-8. One member checked out on such a
    # branch must not take down its own update, nor the other members'.
    update(run_repospace, repospace)
    libb = repospace.ws / "libb"
    subprocess.run(
        [
            b"git",
            b"-C",
            os.fsencode(str(libb)),
            b"checkout",
            b"-q",
            b"-b",
            b"br-\xff",
        ],
        check=True,
    )
    # -k asks git whether the branch descends from the manifest revision, so the name makes the
    # round trip back to git intact.
    out, err = update(run_repospace, repospace, "-k")
    assert "Traceback" not in err
    assert git_bytes(libb, "rev-parse", "--abbrev-ref", "HEAD") == b"br-\xff"
    assert (repospace.ws / "libc" / "libc.txt").is_file()
    # Without -k the member is detached and the name only printed, with the undecodable byte
    # escaped.
    out, err = update(run_repospace, repospace)
    assert git_bytes(libb, "rev-parse", "--abbrev-ref", "HEAD") == b"HEAD"
    assert r'left branch "br-\xff"' in out


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: non-UTF-8 ref names")
def test_update_non_utf8_remote_branch_name(repospace, run_repospace, repos):
    # A SHA revision fetches every remote branch tip into the scratch namespace, so the remote
    # decides those ref names; cleaning them up must survive one that is not valid UTF-8, and must
    # name it back to git byte for byte or the ref would survive.
    odd_src = repos.create("odd-src", {"odd.txt": "o\n"})
    subprocess.run(
        [b"git", b"-C", os.fsencode(str(odd_src)), b"branch", b"br-\xff"],
        check=True,
    )
    repospace.rewrite_app_yaml(
        extra_members=(
            "    - name: odd\n"
            f"      url: {repospace.url(odd_src)}\n"
            f"      revision: {repos.head(odd_src)}\n"
        )
    )
    out, err = update(run_repospace, repospace)
    assert "Traceback" not in err
    odd = repospace.ws / "odd"
    assert (odd / "odd.txt").is_file()
    assert git_bytes(odd, "for-each-ref", "refs/repospace") == b""


def test_update_with_ref_named_head(repospace, run_repospace):
    # A tag named HEAD makes "rev-parse --abbrev-ref HEAD" succeed with empty output. The member is
    # detached, so there is no branch left behind and no switch-back hint to give.
    update(run_repospace, repospace)
    libb = repospace.ws / "libb"
    # "git tag HEAD" is refused by newer git; update-ref still creates the ref, as git's own
    # test suite does.
    subprocess.run(["git", "-C", str(libb), "update-ref", "refs/tags/HEAD", "HEAD"], check=True)
    out, err = update(run_repospace, repospace, "libb")
    assert "left branch" not in out
    assert "checkout" not in out


def test_non_utf8_imported_manifest_fails_cleanly(repospace, run_repospace):
    (repospace.liba_src / "repospace.yaml").write_bytes(b"manifest: \xff\n")
    repospace.repos.commit(repospace.liba_src, {}, "bad encoding")
    code, out, err = run_repospace(["update"], cwd=repospace.ws)
    assert code == 1
    assert "not valid UTF-8" in err
    assert "Traceback" not in err
