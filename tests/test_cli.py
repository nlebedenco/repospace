"""CLI smoke tests: early args, help, config command, aliases, exit codes."""

from __future__ import annotations

import os

import pytest

from repospace import __version__
from repospace.app.main import parse_early_args


def make_repospace(tmp_path):
    ws = tmp_path / "ws"
    (ws / ".repospace").mkdir(parents=True)
    (ws / "repospace.yaml").write_text("manifest:\n  members:\n    - name: lib\n      url: u\n")
    return ws


def test_parse_early_args():
    early = parse_early_args(["-vv", "-q", "update", "-h", "lib"])
    assert early.verbosity_delta == 1
    assert early.command_name == "update"
    assert not early.help
    early = parse_early_args(["-hV"])
    assert early.help and early.version
    early = parse_early_args(["--bogus"])
    assert early.unexpected == "--bogus"
    assert parse_early_args([]).command_name is None


def test_version(run_repospace):
    code, out, err = run_repospace(["-V"])
    assert code == 0
    assert __version__ in out


def test_no_command_prints_help_to_stderr(run_repospace, tmp_path):
    code, out, err = run_repospace([], cwd=tmp_path)
    assert code == 2
    assert "built-in commands" in err


def test_help_flag(run_repospace, tmp_path):
    code, out, err = run_repospace(["-h"], cwd=tmp_path)
    assert code == 0
    assert "built-in commands for managing the repospace" in out
    assert "init:" in out
    assert "config:" in out


def test_help_command(run_repospace, tmp_path):
    code, out, err = run_repospace(["help"], cwd=tmp_path)
    assert code == 0
    assert "built-in commands" in out


def test_help_for_builtin(run_repospace, tmp_path):
    code, out, err = run_repospace(["help", "config"], cwd=tmp_path)
    assert code == 0
    assert "usage: repospace config" in out


def test_unknown_command(run_repospace, tmp_path):
    code, out, err = run_repospace(["frobnicate"], cwd=tmp_path)
    assert code == 2
    assert "unknown command" in err


def test_topdir_outside_repospace_fails(run_repospace, tmp_path):
    code, out, err = run_repospace(["topdir"], cwd=tmp_path)
    assert code == 1
    assert "must be run from a repospace" in err


def test_topdir_inside_repospace(run_repospace, tmp_path):
    ws = make_repospace(tmp_path)
    nested = ws / "sub"
    nested.mkdir()
    code, out, err = run_repospace(["topdir"], cwd=nested)
    assert code == 0
    assert out.strip() == str(ws)


def test_config_set_get_roundtrip(run_repospace, tmp_path):
    ws = make_repospace(tmp_path)
    code, out, err = run_repospace(["config", "color.ui", "false"], cwd=ws)
    assert code == 0
    code, out, err = run_repospace(["config", "color.ui"], cwd=ws)
    assert code == 0
    assert out.strip() == "false"


def test_config_unset_read(run_repospace, tmp_path):
    ws = make_repospace(tmp_path)
    code, out, err = run_repospace(["config", "no.such"], cwd=ws)
    assert code == 1
    assert "is unset" in err


def test_config_list(run_repospace, tmp_path):
    ws = make_repospace(tmp_path)
    run_repospace(["config", "update.fetch", "always"], cwd=ws)
    code, out, err = run_repospace(["config", "-l"], cwd=ws)
    assert code == 0
    assert "update.fetch=always" in out


def test_config_delete(run_repospace, tmp_path):
    ws = make_repospace(tmp_path)
    run_repospace(["config", "a.b", "1"], cwd=ws)
    code, out, err = run_repospace(["config", "-d", "a.b"], cwd=ws)
    assert code == 0
    code, out, err = run_repospace(["config", "a.b"], cwd=ws)
    assert code == 1


def test_config_invalid_name(run_repospace, tmp_path):
    ws = make_repospace(tmp_path)
    code, out, err = run_repospace(["config", "nodot", "x"], cwd=ws)
    assert code == 1
    assert "section.key" in err


def test_config_list_with_name_rejected(run_repospace, tmp_path):
    ws = make_repospace(tmp_path)
    code, out, err = run_repospace(["config", "-l", "color.ui"], cwd=ws)
    assert code == 2
    assert "cannot be combined" in err


def test_config_missing_name(run_repospace, tmp_path):
    ws = make_repospace(tmp_path)
    code, out, err = run_repospace(["config"], cwd=ws)
    assert code == 2
    assert "missing option name" in err


def test_config_delete_with_value_rejected(run_repospace, tmp_path):
    ws = make_repospace(tmp_path)
    code, out, err = run_repospace(["config", "-d", "a.b", "1"], cwd=ws)
    assert code == 2
    assert "cannot combine a value" in err


def test_config_delete_with_append_rejected(run_repospace, tmp_path):
    ws = make_repospace(tmp_path)
    run_repospace(["config", "a.b", "1"], cwd=ws)
    code, out, err = run_repospace(["config", "-a", "-d", "a.b"], cwd=ws)
    assert code == 2
    assert "-a cannot be combined with -d/-D" in err
    # -a was not dropped in favour of the deletion.
    code, out, err = run_repospace(["config", "a.b"], cwd=ws)
    assert out.strip() == "1"


def test_config_list_with_delete_rejected(run_repospace, tmp_path):
    ws = make_repospace(tmp_path)
    run_repospace(["config", "a.b", "1"], cwd=ws)
    code, out, err = run_repospace(["config", "-l", "-d"], cwd=ws)
    assert code == 2
    assert "-l cannot be combined with -d/-D" in err
    assert "a.b=1" not in out


def test_config_list_with_append_rejected(run_repospace, tmp_path):
    ws = make_repospace(tmp_path)
    run_repospace(["config", "a.b", "1"], cwd=ws)
    code, out, err = run_repospace(["config", "-l", "-a"], cwd=ws)
    assert code == 2
    assert "-l cannot be combined with -a" in err
    assert "a.b=1" not in out


def test_config_delete_with_delete_all_rejected(run_repospace, tmp_path):
    ws = make_repospace(tmp_path)
    run_repospace(["config", "--global", "a.b", "g"], cwd=ws)
    run_repospace(["config", "--local", "a.b", "l"], cwd=ws)
    code, out, err = run_repospace(["config", "-d", "-D", "a.b"], cwd=ws)
    assert code == 2
    assert "-d cannot be combined with -D" in err
    # -d's scope was not silently widened to -D's; nothing was deleted.
    code, out, err = run_repospace(["config", "a.b"], cwd=ws)
    assert out.strip() == "l"


def test_config_delete_unset(run_repospace, tmp_path):
    ws = make_repospace(tmp_path)
    code, out, err = run_repospace(["config", "-d", "no.such"], cwd=ws)
    assert code == 1
    assert "is unset" in err


def test_config_append_requires_value(run_repospace, tmp_path):
    ws = make_repospace(tmp_path)
    code, out, err = run_repospace(["config", "-a", "a.b"], cwd=ws)
    assert code == 2
    assert "-a requires a value" in err


def test_config_append_to_unset_fails(run_repospace, tmp_path):
    ws = make_repospace(tmp_path)
    code, out, err = run_repospace(["config", "-a", "a.b", "x"], cwd=ws)
    assert code == 1
    assert "is not set" in err


def test_config_append(run_repospace, tmp_path):
    ws = make_repospace(tmp_path)
    run_repospace(["config", "a.b", "12"], cwd=ws)
    code, out, err = run_repospace(["config", "-a", "a.b", "3"], cwd=ws)
    assert code == 0
    code, out, err = run_repospace(["config", "a.b"], cwd=ws)
    assert out.strip() == "123"


def test_config_delete_precedence_and_delete_all(run_repospace, tmp_path):
    ws = make_repospace(tmp_path)
    run_repospace(["config", "--global", "a.b", "g"], cwd=ws)
    run_repospace(["config", "--local", "a.b", "l"], cwd=ws)
    # -d removes only the highest-precedence occurrence...
    code, out, err = run_repospace(["config", "-d", "a.b"], cwd=ws)
    assert code == 0
    code, out, err = run_repospace(["config", "a.b"], cwd=ws)
    assert out.strip() == "g"
    # ...while -D removes the option everywhere.
    run_repospace(["config", "--local", "a.b", "l"], cwd=ws)
    code, out, err = run_repospace(["config", "-D", "a.b"], cwd=ws)
    assert code == 0
    code, out, err = run_repospace(["config", "a.b"], cwd=ws)
    assert code == 1


def test_config_delete_scoped(run_repospace, tmp_path):
    ws = make_repospace(tmp_path)
    run_repospace(["config", "--global", "a.b", "g"], cwd=ws)
    run_repospace(["config", "--local", "a.b", "l"], cwd=ws)
    code, out, err = run_repospace(["config", "--global", "-d", "a.b"], cwd=ws)
    assert code == 0
    code, out, err = run_repospace(["config", "a.b"], cwd=ws)
    assert out.strip() == "l"


def test_config_unwritable_directory_dies_cleanly(run_repospace, tmp_path):
    # A failed configuration write must die cleanly. The write is atomic (temporary file in the same
    # directory, then a rename), so it is the directory that has to be unwritable; a read-only
    # configuration file does not stop it, just as for git config.
    ws = make_repospace(tmp_path)
    run_repospace(["config", "a.b", "1"], cwd=ws)
    rdir = ws / ".repospace"
    rdir.chmod(0o500)
    try:
        if os.access(rdir, os.W_OK):
            pytest.skip("cannot make the directory unwritable (root?)")
        code, out, err = run_repospace(["config", "a.b", "2"], cwd=ws)
        assert code == 1
        assert "cannot write configuration file" in err
        assert "Traceback" not in err
        code, out, err = run_repospace(["config", "-d", "a.b"], cwd=ws)
        assert code == 1
        assert "cannot write configuration file" in err
        assert "Traceback" not in err
    finally:
        rdir.chmod(0o755)
    # The value that was there survived both failures intact.
    code, out, err = run_repospace(["config", "a.b"], cwd=ws)
    assert out.strip() == "1"


def test_config_works_outside_repospace_global(run_repospace, tmp_path):
    code, out, err = run_repospace(["config", "--global", "color.ui", "false"], cwd=tmp_path)
    assert code == 0
    code, out, err = run_repospace(["config", "color.ui"], cwd=tmp_path)
    assert code == 0
    assert out.strip() == "false"


def test_alias_expansion(run_repospace, tmp_path):
    ws = make_repospace(tmp_path)
    run_repospace(["config", "alias.where", "topdir"], cwd=ws)
    code, out, err = run_repospace(["where"], cwd=ws)
    assert code == 0
    assert out.strip() == str(ws)


def test_alias_listed_in_help(run_repospace, tmp_path):
    ws = make_repospace(tmp_path)
    run_repospace(["config", "alias.where", "topdir"], cwd=ws)
    code, out, err = run_repospace(["help"], cwd=ws)
    assert "aliases:" in out
    assert "where:" in out


def test_alias_with_global_flags(run_repospace, tmp_path):
    ws = tmp_path / "ws"
    (ws / ".repospace").mkdir(parents=True)
    (ws / "repospace.yaml").write_text(
        "manifest:\n"
        "  group-filter: [-opt]\n"
        "  members:\n"
        "    - name: lib\n"
        "      url: u\n"
        "      groups: [opt]\n"
    )
    run_repospace(["config", "alias.loud", "--", "-v update"], cwd=ws)
    code, out, err = run_repospace(["update"], cwd=ws)
    assert code == 0, err
    assert "skipping inactive" not in out
    # The -v inside the alias expansion counts.
    code, out, err = run_repospace(["loud"], cwd=ws)
    assert code == 0, err
    assert "skipping inactive member lib" in out


def test_empty_alias_fails(run_repospace, tmp_path):
    ws = make_repospace(tmp_path)
    run_repospace(["config", "alias.bad", ""], cwd=ws)
    code, out, err = run_repospace(["bad"], cwd=ws)
    assert code == 1
    assert "empty alias" in err


def test_empty_alias_listed_in_help(run_repospace, tmp_path):
    # An empty expansion produces no help text; the alias must still appear in the listing rather
    # than silently vanish.
    ws = make_repospace(tmp_path)
    run_repospace(["config", "alias.bad", ""], cwd=ws)
    code, out, err = run_repospace(["help"], cwd=ws)
    assert code == 0
    assert "bad:" in out


def test_malformed_alias_warns_and_continues(run_repospace, tmp_path):
    # An alias value shlex cannot parse (unbalanced quote) must not take down every invocation;
    # other commands, including the config -d that removes it, keep working.
    ws = make_repospace(tmp_path)
    (ws / ".repospace" / "config").write_text('[alias]\nbad = "unclosed\n')
    code, out, err = run_repospace(["topdir"], cwd=ws)
    assert code == 0
    assert out.strip() == str(ws)
    assert 'ignoring alias "bad"' in err
    code, out, err = run_repospace(["config", "-d", "alias.bad"], cwd=ws)
    assert code == 0


def test_malformed_alias_warning_respects_quiet(run_repospace, tmp_path):
    # Startup warnings follow the same threshold as command-level warnings: -qq and lower silence
    # them.
    ws = make_repospace(tmp_path)
    (ws / ".repospace" / "config").write_text('[alias]\nbad = "unclosed\n')
    code, out, err = run_repospace(["-qq", "topdir"], cwd=ws)
    assert code == 0
    assert out.strip() == str(ws)
    assert "ignoring alias" not in err
    # A single -q keeps warnings, like wrn().
    code, out, err = run_repospace(["-q", "topdir"], cwd=ws)
    assert code == 0
    assert 'ignoring alias "bad"' in err


def test_invalid_config_group_filter_warns_once(run_repospace, tmp_path):
    # Library warnings follow the CLI conventions: the WARNING prefix, emitted once (not once per
    # grouped member or is_active call), and silenced by -qq like every other warning.
    ws = tmp_path / "ws"
    (ws / ".repospace").mkdir(parents=True)
    (ws / "repospace.yaml").write_text(
        "manifest:\n"
        "  members:\n"
        "    - name: lib1\n"
        "      url: u\n"
        "      groups: [optional]\n"
        "    - name: lib2\n"
        "      url: u\n"
        "      groups: [optional]\n"
    )
    code, out, err = run_repospace(["config", "manifest.group-filter", "bad"], cwd=ws)
    assert code == 0
    code, out, err = run_repospace(["list", "-a"], cwd=ws)
    assert code == 0
    assert err.count("invalid manifest.group-filter") == 1
    assert 'WARNING: invalid manifest.group-filter item "bad" ignored' in err
    code, out, err = run_repospace(["-qq", "list", "-a"], cwd=ws)
    assert code == 0
    assert "manifest.group-filter" not in err


def test_alias_expanding_to_version(run_repospace, tmp_path):
    ws = make_repospace(tmp_path)
    run_repospace(["config", "alias.ver", "--", "--version"], cwd=ws)
    code, out, err = run_repospace(["ver"], cwd=ws)
    assert code == 0
    assert __version__ in out


def test_alias_expanding_to_unexpected_flag(run_repospace, tmp_path):
    ws = make_repospace(tmp_path)
    run_repospace(["config", "alias.bad", "--", "--bogus update"], cwd=ws)
    code, out, err = run_repospace(["bad"], cwd=ws)
    assert code == 2
    assert "unexpected argument --bogus" in err


def test_corrupt_config_dies_cleanly(run_repospace, tmp_path):
    ws = make_repospace(tmp_path)
    (ws / ".repospace" / "config").write_text("[a]\nb = 1\n[a]\nc = 2\n")
    code, out, err = run_repospace(["topdir"], cwd=ws)
    assert code == 1
    assert "FATAL ERROR" in err
    assert "cannot parse configuration file" in err


def test_missing_git_dies_cleanly(run_repospace, tmp_path, monkeypatch):
    import repospace.git as gitmod

    ws = make_repospace(tmp_path)
    # A member directory makes commands probe it with git.
    (ws / "lib").mkdir()
    monkeypatch.setattr(gitmod, "_git_executable", None)
    monkeypatch.setattr(gitmod.shutil, "which", lambda name: None)
    code, out, err = run_repospace(["list"], cwd=ws)
    assert code == 1
    assert "git is not installed" in err
    assert "Traceback" not in err


def test_manifest_error_deferred_for_config(run_repospace, tmp_path):
    ws = make_repospace(tmp_path)
    (ws / "repospace.yaml").write_text("manifest:\n  bogus: 1\n")
    # config still works with a broken manifest...
    run_repospace(["config", "color.ui", "false"], cwd=ws)
    code, out, err = run_repospace(["config", "color.ui"], cwd=ws)
    assert code == 0
    assert out.strip() == "false"
    # ...but a manifest-requiring command dies cleanly.
    code, out, err = run_repospace(["list"], cwd=ws)
    assert code == 1
    assert "can't run repospace list" in err


def test_command_help_available_with_broken_manifest(run_repospace, tmp_path):
    # Help is how a user finds the way out of a broken repospace, so "-h" is answered before the
    # manifest is required -- unlike an actual run of the same command.
    ws = make_repospace(tmp_path)
    (ws / "repospace.yaml").write_text("manifest:\n  bogus: 1\n")
    for name in ("list", "diff", "status", "grep", "forall", "compare", "mirror"):
        code, out, err = run_repospace([name, "-h"], cwd=ws)
        assert code == 0, err
        assert f"usage: repospace {name}" in out
    code, out, err = run_repospace(["diff"], cwd=ws)
    assert code == 1
    assert "can't run repospace diff" in err


def test_non_utf8_manifest_error_deferred_for_config(run_repospace, tmp_path):
    ws = make_repospace(tmp_path)
    (ws / "repospace.yaml").write_bytes(b"manifest: \xff\n")
    # config still works with an undecodable manifest...
    code, out, err = run_repospace(["config", "color.ui", "false"], cwd=ws)
    assert code == 0, err
    # ...but a manifest-requiring command dies cleanly.
    code, out, err = run_repospace(["list"], cwd=ws)
    assert code == 1
    assert "not valid UTF-8" in err
    assert "Traceback" not in err


def test_non_utf8_config_dies_cleanly(run_repospace, tmp_path):
    ws = make_repospace(tmp_path)
    (ws / ".repospace" / "config").write_bytes(b"[a]\nb = 1\n\xff\n")
    code, out, err = run_repospace(["topdir"], cwd=ws)
    assert code == 1
    assert "not valid UTF-8" in err
    assert "Traceback" not in err


def test_manifest_path_honors_out_file(run_repospace, tmp_path):
    ws = make_repospace(tmp_path)
    out_file = tmp_path / "path.txt"
    code, out, err = run_repospace(["manifest", "--path", "-o", str(out_file)], cwd=ws)
    assert code == 0, err
    assert out == ""
    assert out_file.read_text().strip() == str(ws / "repospace.yaml")


def test_manifest_validate_rejects_out_file(run_repospace, tmp_path):
    # --validate prints nothing, so -o has nothing to write; it is refused instead of ignored.
    ws = make_repospace(tmp_path)
    out_file = tmp_path / "nothing.txt"
    code, out, err = run_repospace(["manifest", "--validate", "-o", str(out_file)], cwd=ws)
    assert code == 2
    assert "cannot be combined" in err
    assert not out_file.exists()


def test_manifest_path_rejects_active_only(run_repospace, tmp_path):
    ws = make_repospace(tmp_path)
    code, out, err = run_repospace(["manifest", "--path", "--active-only"], cwd=ws)
    assert code == 2
    assert "cannot be combined" in err


def test_list_sha_placeholder_is_not_padded(run_repospace, tmp_path):
    # The placeholder must not carry the default layout's padding into a user format string.
    ws = make_repospace(tmp_path)
    code, out, err = run_repospace(["list", "-a", "-f", "[{sha}]"], cwd=ws)
    assert code == 0, err
    lines = out.splitlines()
    assert lines
    assert all(line == "[N/A]" for line in lines)
