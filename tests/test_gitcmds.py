"""Integration tests for list, manifest, diff, status, forall, compare, grep."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

import pytest

import yaml

from repospace.manifest import QUAL_MANIFEST_REV, SCHEMA_VERSION


@pytest.fixture
def updated(repospace, run_repospace):
    code, out, err = run_repospace(["update"], cwd=repospace.ws)
    assert code == 0, err
    return repospace


def run(run_repospace, ws, *args, expect=0):
    code, out, err = run_repospace(list(args), cwd=ws.ws)
    assert code == expect, f"exit {code}: {err}"
    return out, err


# -- list -------------------------------------------------------------------


def test_list_default(updated, run_repospace):
    out, err = run(run_repospace, updated, "list")
    lines = out.splitlines()
    names = [line.split()[0] for line in lines]
    assert names == ["manifest", "liba", "libb", "libc"]
    assert "v1.0" in lines[2]
    assert "stale" not in out


def test_list_default_columns_align(updated, run_repospace):
    # A name wider than the column must push the whole row, not just its own line, to the right.
    updated.rewrite_app_yaml(
        extra_members=(
            "    - name: a-member-with-a-very-long-name\n"
            f"      url: {updated.url(updated.libc_src)}\n"
        )
    )
    out, _ = run(run_repospace, updated, "list")
    lines = out.splitlines()
    assert any(line.startswith("a-member-with-a-very-long-name ") for line in lines)
    # Only the first four fields are columns; a stale suffix follows the last one.
    starts = {tuple(m.start() for m in re.finditer(r"\S+", line))[:4] for line in lines}
    assert len(starts) == 1


def test_list_custom_format(updated, run_repospace):
    out, _ = run(run_repospace, updated, "list", "-f", "{name}|{declared_by}|{cloned}")
    assert "libc|liba|cloned" in out.splitlines()


def test_list_sha_format(updated, run_repospace):
    out, _ = run(run_repospace, updated, "list", "-f", "{name} {sha}")
    for line in out.splitlines()[1:]:
        assert re.fullmatch(r"\S+ [0-9a-f]{40}", line)


def test_list_unknown_key(updated, run_repospace):
    out, err = run(run_repospace, updated, "list", "-f", "{bogus}", expect=1)
    assert "unknown format key" in err


def test_list_inactive_filtering(updated, run_repospace):
    updated.rewrite_app_yaml(
        extra_members=(
            "    - name: libd\n"
            f"      url: {updated.url(updated.libc_src)}\n"
            "      groups: [opt]\n"
        ),
        group_filter="-opt",
    )
    out, _ = run(run_repospace, updated, "list")
    assert "libd" not in out
    out, _ = run(run_repospace, updated, "list", "-a")
    assert "libd" in out
    out, _ = run(run_repospace, updated, "list", "-i")
    assert [line.split()[0] for line in out.splitlines()] == ["libd"]


def test_list_stale_revision_changed(updated, run_repospace):
    yaml_file = updated.app / "repospace.yaml"
    yaml_file.write_text(yaml_file.read_text().replace("revision: v1.0", "revision: v9.9"))
    out, _ = run(run_repospace, updated, "list")
    libb_line = [ln for ln in out.splitlines() if ln.startswith("libb")][0]
    assert "revision-changed" in libb_line


def test_list_stale_added_member(updated, run_repospace):
    updated.rewrite_app_yaml(
        extra_members=("    - name: ghost\n" f"      url: {updated.url(updated.libc_src)}\n")
    )
    out, _ = run(run_repospace, updated, "list")
    ghost_line = [ln for ln in out.splitlines() if ln.startswith("ghost")][0]
    assert "not-cloned" in ghost_line
    assert "added" in ghost_line


def test_list_stale_diverged(updated, run_repospace):
    libb = updated.ws / "libb"
    subprocess.run(["git", "-C", str(libb), "checkout", "-q", "-b", "work"], check=True)
    updated.repos.commit(libb, {"local.txt": "x\n"}, "local")
    out, _ = run(run_repospace, updated, "list")
    libb_line = [ln for ln in out.splitlines() if ln.startswith("libb")][0]
    assert "diverged" in libb_line


def test_list_no_repospace_rev_without_git_noise(updated, run_repospace):
    # A missing repospace-rev is a normal staleness outcome; git's "fatal: ..." from the probing
    # rev-parse must not leak to stderr.
    subprocess.run(
        [
            "git",
            "-C",
            str(updated.ws / "libb"),
            "update-ref",
            "-d",
            QUAL_MANIFEST_REV,
        ],
        check=True,
    )
    out, err = run(run_repospace, updated, "list")
    libb_line = [ln for ln in out.splitlines() if ln.startswith("libb")][0]
    assert "no-repospace-rev" in libb_line
    assert "fatal" not in err


def test_list_no_snapshot_warning(updated, run_repospace):
    (updated.ws / ".repospace" / "members.json").unlink()
    out, err = run(run_repospace, updated, "list")
    assert "no usable members.json" in err


def test_list_orphan_warning(updated, run_repospace):
    yaml_file = updated.app / "repospace.yaml"
    text = "\n".join(
        line
        for line in yaml_file.read_text().splitlines()
        if "libb" not in line and "v1.0" not in line
    )
    yaml_file.write_text(text + "\n")
    out, err = run(run_repospace, updated, "list")
    assert 'member "libb" from the last update' in err


# -- manifest ---------------------------------------------------------------


def test_manifest_validate(updated, run_repospace):
    out, err = run(run_repospace, updated, "manifest", "--validate")
    assert out == ""


def test_manifest_validate_broken(updated, run_repospace):
    (updated.app / "repospace.yaml").write_text("manifest:\n  bogus: 1\n")
    out, err = run(run_repospace, updated, "manifest", "--validate", expect=1)
    assert "invalid manifest" in err


def test_manifest_path(updated, run_repospace):
    out, _ = run(run_repospace, updated, "manifest", "--path")
    assert out.strip() == str(updated.app / "repospace.yaml")


def test_manifest_file_config_escape_rejected(updated, run_repospace):
    # manifest.file has the same confinement as imports: it cannot point outside the manifest
    # repository.
    run(run_repospace, updated, "config", "manifest.file", "../evil.yaml")
    out, err = run(run_repospace, updated, "manifest", "--path", expect=1)
    assert "escapes" in err


def test_manifest_resolve(updated, run_repospace):
    out, _ = run(run_repospace, updated, "manifest", "--resolve")
    data = yaml.safe_load(out)
    mdata = data["manifest"]
    assert mdata["version"] == SCHEMA_VERSION
    members = {m["name"]: m for m in mdata["members"]}
    assert set(members) == {"liba", "libb", "libc"}
    assert "import" not in members["liba"]
    assert members["libc"]["url"].startswith("file://")
    assert mdata["self"]["name"] == "app"
    # liba's imported self section was attributed to liba.
    assert members["liba"]["cmake-packages"] == ["LibA"]


def test_manifest_freeze(updated, run_repospace):
    out, _ = run(run_repospace, updated, "manifest", "--freeze")
    data = yaml.safe_load(out)
    for member in data["manifest"]["members"]:
        assert re.fullmatch(r"[0-9a-f]{40}", member["revision"])


def test_manifest_freeze_uncloned_member_fails_cleanly(updated, run_repospace):
    updated.rewrite_app_yaml(
        extra_members=("    - name: ghost\n" f"      url: {updated.url(updated.libc_src)}\n")
    )
    out, err = run(run_repospace, updated, "manifest", "--freeze", expect=1)
    assert "cannot freeze" in err
    assert "ghost" in err
    assert "Traceback" not in err


def test_manifest_freeze_inactive_member_error_names_escapes(updated, run_repospace):
    # Plain "repospace update" skips inactive members, so for them the generic "run repospace
    # update" advice would be a dead end; the error must name the ways that actually work.
    updated.rewrite_app_yaml(
        extra_members=(
            "    - name: ghost\n"
            f"      url: {updated.url(updated.libc_src)}\n"
            "      groups: [optional]\n"
        ),
        group_filter="-optional",
    )
    out, err = run(run_repospace, updated, "manifest", "--freeze", expect=1)
    assert "cannot freeze" in err
    assert "inactive" in err
    assert "repospace update ghost" in err
    assert "--active-only" in err
    # And --active-only is indeed the escape: it freezes without ghost.
    out, _ = run(run_repospace, updated, "manifest", "--freeze", "--active-only")
    data = yaml.safe_load(out)
    assert "ghost" not in {m["name"] for m in data["manifest"]["members"]}


def test_manifest_resolve_before_update_fails(repospace, run_repospace):
    out, err = run(run_repospace, repospace, "manifest", "--resolve", expect=1)
    assert "cannot resolve" in err


def test_manifest_out_write_failure_fails_cleanly(updated, run_repospace):
    target = updated.ws / "no-such-dir" / "resolved.yaml"
    out, err = run(
        run_repospace,
        updated,
        "manifest",
        "--resolve",
        "-o",
        str(target),
        expect=1,
    )
    assert "cannot write" in err


# -- diff / status ----------------------------------------------------------


def test_diff_clean_and_dirty(updated, run_repospace):
    out, _ = run(run_repospace, updated, "diff")
    assert out.splitlines() == ["=== no differences"]
    (updated.ws / "liba" / "liba.txt").write_text("changed\n")
    out, _ = run(run_repospace, updated, "diff")
    assert "diff for liba (liba):" in out
    assert "liba/liba.txt" in out
    assert "libb" not in out


def test_diff_exit_code_passthrough(updated, run_repospace):
    (updated.ws / "liba" / "liba.txt").write_text("changed\n")
    out, err = run(run_repospace, updated, "diff", "--exit-code", expect=1)
    assert "diff for liba (liba):" in out
    assert "liba/liba.txt" in out
    assert "ERROR" not in err


def test_diff_all_states_the_verdict_per_member(updated, run_repospace):
    # As in "compare -a": a banner with nothing under it is the silence -a was meant to break, so
    # every member says where it stands, and the run-wide summary steps aside because they did.
    out, _ = run(run_repospace, updated, "diff", "-a")
    assert "=== diff for liba (liba): no differences" in out
    assert "=== diff for libb (libb): no differences" in out
    assert "=== no differences" not in out

    (updated.ws / "liba" / "liba.txt").write_text("changed\n")
    # Plain git diff exits 0 whether or not it printed a patch, as test_diff_clean_and_dirty shows.
    out, _ = run(run_repospace, updated, "diff", "-a")
    assert "=== diff for liba (liba):" in out
    assert "liba/liba.txt" in out
    assert "=== diff for libb (libb): no differences" in out


def test_diff_all_verdict_follows_git_not_the_output(updated, run_repospace):
    # --quiet reports differences without printing them, so an empty patch does not mean a clean
    # member; the exit status is what the per-member verdict follows.
    (updated.ws / "liba" / "liba.txt").write_text("changed\n")
    out, _ = run(run_repospace, updated, "diff", "-a", "--", "--quiet", expect=1)
    assert "=== diff for liba (liba): differences not shown" in out
    assert "=== diff for libb (libb): no differences" in out


def test_diff_summary_follows_git_not_the_output(updated, run_repospace):
    # A pass-through flag that reports differences without printing them must not be summarized as
    # "no differences", which would contradict the exit status.
    (updated.ws / "liba" / "liba.txt").write_text("changed\n")
    out, err = run(run_repospace, updated, "diff", "--", "--quiet", expect=1)
    assert out == ""
    assert "no differences" not in out


def test_diff_relays_repospace_qq(updated, run_repospace):
    # "repospace -qq diff" relays --quiet to git diff: exit status only.
    code, out, err = run_repospace(["-qq", "diff"], cwd=updated.ws)
    assert code == 0
    assert out == ""
    (updated.ws / "liba" / "liba.txt").write_text("changed\n")
    code, out, err = run_repospace(["-qq", "diff"], cwd=updated.ws)
    assert code == 1
    assert out == ""
    # A single -q only suppresses repospace banners; the patch shows.
    code, out, err = run_repospace(["-q", "diff"], cwd=updated.ws)
    assert code == 0
    assert "liba/liba.txt" in out
    assert "diff for liba (liba):" not in out


def test_diff_dashdash_passes_flags_through(updated, run_repospace):
    # The documented escape: after --, -a belongs to git diff (--text), not to this command (--all),
    # and is not a member name.
    out, err = run(run_repospace, updated, "diff", "--", "-a")
    assert out.splitlines() == ["=== no differences"]
    assert "unknown member" not in err
    (updated.ws / "liba" / "liba.txt").write_text("changed\n")
    out, _ = run(run_repospace, updated, "diff", "--", "--stat")
    assert "diff for liba (liba):" in out
    assert "liba.txt" in out
    assert "file changed" in out


def test_diff_members_precede_dashdash(updated, run_repospace):
    (updated.ws / "liba" / "liba.txt").write_text("changed\n")
    out, _ = run(run_repospace, updated, "diff", "libb", "--", "--stat")
    assert out.splitlines() == ["=== no differences"]


def test_diff_revision_positional_hints_dashdash(updated, run_repospace):
    # git diff's own positionals (revisions, paths) are read as member names; the error says where
    # they belong.
    code, out, err = run_repospace(["diff", "HEAD"], cwd=updated.ws)
    assert code == 1
    assert "unknown member(s): HEAD" in err
    assert 'go after "--"' in err
    out, _ = run(run_repospace, updated, "diff", "--", "HEAD")
    assert out.splitlines() == ["=== no differences"]


def test_diff_signal_death_maps_exit_code(updated, run_repospace, monkeypatch):
    # git diff killed by a signal exits with the conventional 128 + signal, not a negative code
    # truncated modulo 256.
    from repospace.manifest import Member

    real_git = Member.git

    def fake_git(self, args, **kwargs):
        if args and args[0] == "diff":
            return subprocess.CompletedProcess(args, -9, b"", b"")
        return real_git(self, args, **kwargs)

    monkeypatch.setattr(Member, "git", fake_git)
    code, out, err = run_repospace(["diff"], cwd=updated.ws)
    assert code == 137
    assert "died on signal 9" in err


def test_status(updated, run_repospace):
    out, _ = run(run_repospace, updated, "status")
    for name in ("liba", "libb", "libc"):
        assert f"status of {name} ({name}):" in out


def test_status_relays_repospace_verbose(updated, run_repospace):
    # "repospace -v status" relays --verbose to git status, which shows the diff of staged changes.
    libb = updated.ws / "libb"
    (libb / "libb.txt").write_text("staged change\n")
    subprocess.run(["git", "-C", str(libb), "add", "-A"], check=True)
    out, _ = run(run_repospace, updated, "status")
    assert "diff --git" not in out
    code, out, err = run_repospace(["-v", "status"], cwd=updated.ws)
    assert code == 0
    assert "diff --git" in out


def test_status_named_uncloned_member_fails(updated, run_repospace):
    updated.rewrite_app_yaml(
        extra_members=("    - name: ghost\n" f"      url: {updated.url(updated.libc_src)}\n")
    )
    out, err = run(run_repospace, updated, "status", "ghost", expect=1)
    assert "ghost" in err
    assert "not cloned" in err


def test_status_skips_uncloned_and_inactive(updated, run_repospace):
    updated.rewrite_app_yaml(
        extra_members=(
            "    - name: ghost\n"
            f"      url: {updated.url(updated.libc_src)}\n"
            "    - name: libd\n"
            f"      url: {updated.url(updated.libc_src)}\n"
            "      groups: [opt]\n"
        ),
        group_filter="-opt",
    )
    out, _ = run(run_repospace, updated, "status")
    assert "status of liba (liba):" in out
    assert "ghost" not in out
    assert "libd" not in out


# -- forall -----------------------------------------------------------------


def test_forall(updated, run_repospace):
    out, _ = run(
        run_repospace,
        updated,
        "forall",
        "-c",
        "echo member=$REPOSPACE_MEMBER_NAME rev=$REPOSPACE_MEMBER_REVISION",
    )
    assert "member=liba rev=main" in out
    assert "member=libb rev=v1.0" in out
    assert "member=libc rev=main" in out


def test_forall_failure(updated, run_repospace):
    out, err = run(run_repospace, updated, "forall", "-c", "false", expect=1)
    assert "command failed in" in err


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: signals")
def test_forall_signal_death_maps_exit_code(updated, run_repospace):
    # A command killed by a signal exits with the conventional 128 + signal, as in diff and grep,
    # and is reported as killed rather than as an ordinary non-zero status.
    out, err = run(run_repospace, updated, "forall", "-c", "kill -SEGV $$", expect=139)
    assert "killed by signal 11" in err


# -- compare ----------------------------------------------------------------


def test_compare_clean(updated, run_repospace):
    out, _ = run(run_repospace, updated, "compare")
    assert out.splitlines() == ["=== all members are up to date"]


def test_compare_clean_all_does_not_repeat_itself(updated, run_repospace):
    # With -a every member already says it is up to date; the summary exists to break silence.
    out, _ = run(run_repospace, updated, "compare", "-a")
    assert "all members are up to date" not in out
    assert out.splitlines() == [
        "=== liba (liba): up to date",
        "=== libb (libb): up to date",
        "=== libc (libc): up to date",
    ]


def test_compare_clean_quiet(updated, run_repospace):
    # Scripts reading the output get silence back, as before the summary line.
    out, _ = run(run_repospace, updated, "-q", "compare")
    assert out == ""


def test_compare_ahead(updated, run_repospace):
    libb = updated.ws / "libb"
    subprocess.run(["git", "-C", str(libb), "checkout", "-q", "-b", "work"], check=True)
    updated.repos.commit(libb, {"local.txt": "x\n"}, "local")
    out, _ = run(run_repospace, updated, "compare")
    assert "libb (libb):" in out
    assert 'branch "work"' in out
    assert "ahead 1, behind 0" in out
    run(run_repospace, updated, "compare", "--exit-code", expect=1)


def test_compare_dirty(updated, run_repospace):
    (updated.ws / "libc" / "libc.txt").write_text("dirty\n")
    out, _ = run(run_repospace, updated, "compare")
    assert "libc (libc):" in out
    assert "uncommitted changes" in out


def test_compare_unborn_head(updated, run_repospace):
    # repospace-rev exists but no commit is checked out: an update interrupted between update-ref
    # and checkout.
    libb = updated.ws / "libb"
    subprocess.run(
        ["git", "-C", str(libb), "symbolic-ref", "HEAD", "refs/heads/never-born"],
        check=True,
    )
    out, _ = run(run_repospace, updated, "compare")
    assert "libb (libb):" in out
    assert "no commit checked out" in out


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: non-UTF-8 ref names")
def test_compare_non_utf8_branch_name(updated, run_repospace):
    # A branch name that is not valid UTF-8 is reported with the undecodable byte escaped; it must
    # not cost the whole comparison.
    libb = updated.ws / "libb"
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
    out, err = run(run_repospace, updated, "compare", "--all")
    assert "Traceback" not in err
    assert r'checked out branch "br-\xff"' in out
    assert "liba (liba): up to date" in out


def test_compare_ref_named_head(updated, run_repospace):
    # A tag named HEAD makes "rev-parse --abbrev-ref HEAD" succeed with empty output; the member is
    # detached and up to date, so there is nothing to report and --exit-code must stay happy.
    libb = updated.ws / "libb"
    # "git tag HEAD" is refused by newer git; update-ref still creates the ref, as git's own
    # test suite does.
    subprocess.run(["git", "-C", str(libb), "update-ref", "refs/tags/HEAD", "HEAD"], check=True)
    out, _ = run(run_repospace, updated, "compare", "--exit-code")
    assert out.splitlines() == ["=== all members are up to date"]


def test_compare_manifest_member_rejected(updated, run_repospace):
    # Naming the manifest repository explicitly is an error, not a silent no-op; it has no
    # repospace-rev to compare against.
    code, out, err = run_repospace(["compare", "manifest"], cwd=updated.ws)
    assert code == 1
    assert "cannot be compared" in err


# -- grep -------------------------------------------------------------------


def test_grep_match(updated, run_repospace):
    out, _ = run(run_repospace, updated, "grep", "hello from liba")
    assert "liba (liba):" in out
    assert "hello from liba" in out


def test_grep_no_match(updated, run_repospace):
    run(run_repospace, updated, "grep", "NO_SUCH_TOKEN_ANYWHERE", expect=1)


def test_grep_passes_tool_flags_through(updated, run_repospace):
    # After the command name, -v belongs to the tool (git grep invert match), not to repospace
    # verbosity.
    out, _ = run(run_repospace, updated, "grep", "-v", "hello from liba")
    # Every file except the one containing the greeting still matches.
    assert "libb.txt" in out


def test_grep_tool_args_config_shell_quoting(updated, run_repospace):
    # grep.<tool>-args values are shell-split like aliases: a quoted argument with spaces reaches
    # the tool as one word, without literal quotes.
    run_repospace(
        ["config", "grep.git-grep-args", "-e 'hello from liba'"],
        cwd=updated.ws,
    )
    out, _ = run(run_repospace, updated, "grep", "--", "-e", "NO_SUCH_TOKEN")
    assert "hello from liba" in out


def test_grep_tool_args_config_malformed(updated, run_repospace):
    # An unbalanced quote must not break grep; warn and search without the configured extras.
    run_repospace(["config", "grep.git-grep-args", "don't"], cwd=updated.ws)
    code, out, err = run_repospace(["grep", "hello from liba"], cwd=updated.ws)
    assert code == 0
    assert "ignoring grep.git-grep-args" in err
    assert "hello from liba" in out


def test_grep_dashdash_passes_flags_through(updated, run_repospace):
    # The documented escape: after --, -m belongs to the tool (git grep max-count), not to this
    # command (--member).
    updated.repos.commit(updated.app, {"notes.txt": "GREP_TOKEN\nGREP_TOKEN\n"}, "notes")
    out, _ = run(run_repospace, updated, "grep", "--", "-m", "1", "GREP_TOKEN")
    assert out.count("GREP_TOKEN") == 1


def test_grep_user_quiet_flag_still_matches(updated, run_repospace):
    # A user-passed -q makes the tool match silently; the exit status must still reflect the match.
    code, out, err = run_repospace(["grep", "-q", "hello from liba"], cwd=updated.ws)
    assert code == 0
    assert out == ""


def test_grep_relays_repospace_qq(updated, run_repospace):
    # "repospace -qq grep" relays -q to the tool: exit status only.
    code, out, err = run_repospace(["-qq", "grep", "hello from liba"], cwd=updated.ws)
    assert code == 0
    assert out == ""
    code, out, err = run_repospace(["-qq", "grep", "NO_SUCH_TOKEN_ANYWHERE"], cwd=updated.ws)
    assert code == 1
    # A single -q only suppresses repospace banners; matches still show.
    code, out, err = run_repospace(["-q", "grep", "hello from liba"], cwd=updated.ws)
    assert code == 0
    assert "hello from liba" in out
    assert "liba (liba):" not in out


def test_grep_member_dir_exclusions_escape_glob_metachars():
    from repospace.app.gitcmds import Grep
    from repospace.manifest import Manifest

    manifest = Manifest.from_data(
        "manifest:\n"
        "  members:\n"
        "    - name: weird\n"
        "      url: u\n"
        '      path: "lib[x]/w*"\n'
    )
    command = Grep()
    command.manifest = manifest
    searched = manifest.members[0]
    # Metacharacters in member paths must match literally, not as glob syntax, in both tools'
    # exclusion patterns.
    assert command._member_dir_exclusions("ripgrep", searched) == ["--glob=!/lib\\[x\\]/w\\*"]
    assert command._member_dir_exclusions("grep", searched) == ["--exclude-dir=w\\*"]


def test_grep_member_dir_exclusions_only_nested_members():
    from repospace.app.gitcmds import Grep
    from repospace.manifest import Manifest

    manifest = Manifest.from_data(
        "manifest:\n"
        "  members:\n"
        "    - name: outer\n"
        "      url: u\n"
        "    - name: inner\n"
        "      url: u\n"
        "      path: outer/inner\n"
        "    - name: sibling\n"
        "      url: u\n"
    )
    command = Grep()
    command.manifest = manifest
    by_name = {m.name: m for m in manifest.members}
    # A member's own search excludes what is nested inside it, named relative to it, and nothing
    # else.
    assert command._member_dir_exclusions("ripgrep", by_name["outer"]) == ["--glob=!/inner"]
    assert command._member_dir_exclusions("ripgrep", by_name["inner"]) == []
    assert command._member_dir_exclusions("ripgrep", by_name["sibling"]) == []


def test_grep_searches_manifest_repo(updated, run_repospace):
    updated.repos.commit(updated.app, {"notes.txt": "MANIFEST_ONLY_TOKEN\n"}, "notes")
    out, _ = run(run_repospace, updated, "grep", "MANIFEST_ONLY_TOKEN")
    assert "manifest (.):" in out
    assert "notes.txt" in out


def test_grep_member_restriction(updated, run_repospace):
    out, _ = run(run_repospace, updated, "grep", "-m", "liba", "hello from liba")
    assert "liba (liba):" in out
    run(
        run_repospace,
        updated,
        "grep",
        "-m",
        "libb",
        "hello from liba",
        expect=1,
    )
    out, err = run(run_repospace, updated, "grep", "-m", "ghost", "x", expect=1)
    assert "unknown member" in err


def test_grep_plain_tool_skips_metadata(updated, run_repospace):
    # .repospace/packages.cmake contains this token; the plain grep tool must not descend into the
    # metadata directory.
    run(
        run_repospace,
        updated,
        "grep",
        "--tool",
        "grep",
        "REPOSPACE_UPDATE_HASH",
        expect=1,
    )


def test_grep_plain_tool_skips_member_git_dirs(updated, run_repospace):
    # Every repository's .git/config contains this key; the plain grep tool must not descend into
    # git metadata in members either.
    run(
        run_repospace,
        updated,
        "grep",
        "--tool",
        "grep",
        "repositoryformatversion",
        expect=1,
    )


def test_grep_plain_tool_no_duplicate_member_hits(updated, run_repospace):
    # The manifest-repository pass must not descend into member directories; they are searched
    # separately.
    (updated.ws / "liba" / "grep-dedup.txt").write_text("GREP_DEDUP_TOKEN\n")
    out, _ = run(run_repospace, updated, "grep", "--tool", "grep", "GREP_DEDUP_TOKEN")
    assert out.count("GREP_DEDUP_TOKEN") == 1
    assert "liba (liba):" in out


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_grep_ripgrep_no_duplicate_member_hits(updated, run_repospace):
    (updated.ws / "liba" / "grep-dedup.txt").write_text("GREP_DEDUP_TOKEN\n")
    out, _ = run(
        run_repospace,
        updated,
        "grep",
        "--tool",
        "ripgrep",
        "GREP_DEDUP_TOKEN",
    )
    assert out.count("GREP_DEDUP_TOKEN") == 1
    assert "liba (liba):" in out


def nested_member_repospace(repospace, run_repospace):
    """Update a topology with "inner" nested inside liba's directory."""
    repospace.rewrite_app_yaml(
        extra_members=(
            "    - name: inner\n"
            f"      url: {repospace.url(repospace.libc_src)}\n"
            "      path: liba/inner\n"
        )
    )
    code, out, err = run_repospace(["update"], cwd=repospace.ws)
    assert code == 0, err
    (repospace.ws / "liba" / "inner" / "nested.txt").write_text("GREP_NESTED_TOKEN\n")
    return repospace


def test_grep_plain_tool_no_duplicate_nested_member_hits(repospace, run_repospace):
    # Member paths may nest. The enclosing member's search must not descend into the nested one,
    # which is searched separately.
    ws = nested_member_repospace(repospace, run_repospace)
    out, _ = run(run_repospace, ws, "grep", "--tool", "grep", "GREP_NESTED_TOKEN")
    assert out.count("GREP_NESTED_TOKEN") == 1
    assert "inner (liba/inner):" in out


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_grep_ripgrep_no_duplicate_nested_member_hits(repospace, run_repospace):
    ws = nested_member_repospace(repospace, run_repospace)
    out, _ = run(run_repospace, ws, "grep", "--tool", "ripgrep", "GREP_NESTED_TOKEN")
    assert out.count("GREP_NESTED_TOKEN") == 1
    assert "inner (liba/inner):" in out


def test_grep_pattern_from_tool_args_config(updated, run_repospace):
    # Configured extras may carry the pattern themselves, so a bare "repospace grep" is not
    # necessarily patternless.
    run_repospace(
        ["config", "grep.git-grep-args", "-e 'hello from liba'"],
        cwd=updated.ws,
    )
    out, _ = run(run_repospace, updated, "grep")
    assert "hello from liba" in out
    # Without them, and without a pattern on the command line, the search cannot be run.
    run_repospace(["config", "-d", "grep.git-grep-args"], cwd=updated.ws)
    out, err = run(run_repospace, updated, "grep", expect=1)
    assert "missing search pattern" in err


def test_grep_missing_tool(updated, run_repospace):
    out, err = run(
        run_repospace,
        updated,
        "grep",
        "--tool",
        "ripgrep",
        "--tool-path",
        "/nonexistent/rg",
        "pattern",
        expect=1,
    )
    assert "search tool not found" in err


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: file modes")
def test_grep_tool_not_executable(updated, run_repospace, tmp_path):
    # A tool path that exists but cannot be executed is as much a configuration error as a missing
    # one, and dies the same way.
    if os.geteuid() == 0:
        pytest.skip("root is not stopped by the execute bit")
    tool = tmp_path / "unrunnable"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o644)
    out, err = run(
        run_repospace,
        updated,
        "grep",
        "--tool",
        "grep",
        "--tool-path",
        str(tool),
        "pattern",
        expect=1,
    )
    assert "cannot run the search tool" in err
    assert "Traceback" not in err


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: /bin/sh and signals")
def test_grep_tool_killed_by_signal(updated, run_repospace, tmp_path):
    # A tool that dies on a signal is an error, not "no matches".
    tool = tmp_path / "crashgrep"
    tool.write_text("#!/bin/sh\nkill -SEGV $$\n")
    tool.chmod(0o755)
    out, err = run(
        run_repospace,
        updated,
        "grep",
        "--tool",
        "grep",
        "--tool-path",
        str(tool),
        "pattern",
        expect=139,
    )
    assert "died on signal" in err


def test_grep_non_git_manifest_repository(run_repospace, tmp_path):
    # repospace init accepts a plain (non-git) directory; grep must still search it with the non-git
    # tools, and say why git grep cannot instead of silently skipping it.
    ws = tmp_path / "plainws"
    (ws / ".repospace").mkdir(parents=True)
    (ws / "repospace.yaml").write_text("manifest:\n")
    (ws / "notes.txt").write_text("plain needle\n")

    code, out, err = run_repospace(["grep", "--tool", "grep", "plain needle"], cwd=ws)
    assert code == 0, err
    assert "plain needle" in out

    code, out, err = run_repospace(["grep", "plain needle"], cwd=ws)
    assert code == 1
    assert "not a git repository" in err

    code, out, err = run_repospace(["grep", "-m", "manifest", "plain needle"], cwd=ws)
    assert code == 1
    assert "cannot search it" in err
