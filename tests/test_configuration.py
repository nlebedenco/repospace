"""Tests for repospace.configuration."""

from __future__ import annotations

import os
import pathlib
import stat

import pytest

from repospace import configuration
from repospace.configuration import (
    ConfigFile,
    Configuration,
    MalformedConfig,
    parse_key,
)


def write_ini(path, text):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def repospace(tmp_path):
    (tmp_path / "ws" / ".repospace").mkdir(parents=True)
    return tmp_path / "ws"


def test_parse_key():
    assert parse_key("manifest.path") == ("manifest", "path")
    assert parse_key("grep.git-grep-args") == ("grep", "git-grep-args")
    assert parse_key("a.b.c") == ("a", "b.c")
    # Git-style case-insensitivity: names normalize to lowercase.
    assert parse_key("Alias.UP") == ("alias", "up")
    for bad in ("nodot", ".key", "section.", "."):
        with pytest.raises(ValueError):
            parse_key(bad)
    # INI-significant characters in a name would be reinterpreted on the next read of the file (as a
    # different option, a comment, or an injected line), so they are rejected before anything is
    # written.
    for bad in (
        "alias.x=y",
        "a.b:c",
        "a b.c",
        "a.b c",
        "#a.b",
        "a.;b",
        "[a.b",
        "a.b\nevil = injected",
    ):
        with pytest.raises(ValueError):
            parse_key(bad)


def test_precedence_local_over_global_over_system(tmp_path, monkeypatch, repospace):
    write_ini(tmp_path / "sys.ini", "[color]\nui = system\nsys = 1\n")
    write_ini(tmp_path / "glob.ini", "[color]\nui = global\nglob = 1\n")
    write_ini(repospace / ".repospace" / "config", "[color]\nui = local\n")
    monkeypatch.setenv("REPOSPACE_CONFIG_SYSTEM", str(tmp_path / "sys.ini"))
    monkeypatch.setenv("REPOSPACE_CONFIG_GLOBAL", str(tmp_path / "glob.ini"))
    config = Configuration(topdir=str(repospace))
    assert config.get("color.ui") == "local"
    assert config.get("color.glob") == "1"
    assert config.get("color.sys") == "1"
    assert config.get("color.ui", configfile=ConfigFile.GLOBAL) == "global"
    assert config.get("color.ui", configfile=ConfigFile.SYSTEM) == "system"


def test_malformed_file_raises(repospace):
    write_ini(repospace / ".repospace" / "config", "[a]\nb = 1\n[a]\nc = 2\n")
    with pytest.raises(MalformedConfig):
        Configuration(topdir=str(repospace))


def test_continuation_line_under_valueless_key_raises(repospace):
    # configparser fails on this with an AttributeError of its own rather than a parsing error; it
    # must still surface as MalformedConfig, not as a traceback out of every invocation.
    write_ini(repospace / ".repospace" / "config", "[a]\nb\n  c\n")
    with pytest.raises(MalformedConfig) as excinfo:
        Configuration(topdir=str(repospace))
    assert "continuation line" in str(excinfo.value)


def test_unreadable_file_raises(repospace):
    # configparser.read() would silently skip a file it cannot open; a configuration the user
    # believes is in effect must not be dropped without a diagnostic.
    path = repospace / ".repospace" / "config"
    write_ini(path, "[a]\nb = 1\n")
    path.chmod(0)
    try:
        if os.access(path, os.R_OK):
            pytest.skip("cannot make the file unreadable (root?)")
        with pytest.raises(MalformedConfig) as excinfo:
            Configuration(topdir=str(repospace))
        assert "cannot read" in str(excinfo.value)
    finally:
        path.chmod(0o644)


def test_get_default():
    config = Configuration(topdir=None)
    assert config.get("no.such", default="fallback") == "fallback"
    assert config.get("no.such") is None


def test_getboolean(repospace):
    write_ini(
        repospace / ".repospace" / "config",
        "[a]\nyes = true\nno = 0\nbad = maybe\n",
    )
    config = Configuration(topdir=str(repospace))
    assert config.getboolean("a.yes") is True
    assert config.getboolean("a.no") is False
    assert config.getboolean("a.missing", default=True) is True
    with pytest.raises(MalformedConfig):
        config.getboolean("a.bad")


def test_getint_getfloat(repospace):
    write_ini(repospace / ".repospace" / "config", "[n]\ni = 7\nf = 2.5\n")
    config = Configuration(topdir=str(repospace))
    assert config.getint("n.i") == 7
    assert config.getfloat("n.f") == 2.5
    with pytest.raises(MalformedConfig):
        config.getint("n.f")


def test_getint_getfloat_defaults_and_errors(repospace):
    write_ini(repospace / ".repospace" / "config", "[n]\ns = word\n")
    config = Configuration(topdir=str(repospace))
    assert config.getint("n.missing", default=7) == 7
    assert config.getfloat("n.missing", default=1.5) == 1.5
    with pytest.raises(MalformedConfig):
        config.getfloat("n.s")


def test_set_all_rejected(repospace):
    config = Configuration(topdir=str(repospace))
    with pytest.raises(ValueError):
        config.set("a.b", "1", configfile=ConfigFile.ALL)


def test_set_second_key_in_section(repospace):
    config = Configuration(topdir=str(repospace))
    config.set("a.b", "1")
    config.set("a.c", "2")
    fresh = Configuration(topdir=str(repospace))
    assert fresh.get("a.b") == "1"
    assert fresh.get("a.c") == "2"


def test_set_default_section_rejected(repospace):
    # DEFAULT is reserved by configparser; a clean error, no traceback. Reserved in any case, since
    # section names are lowercased.
    config = Configuration(topdir=str(repospace))
    for option in ("DEFAULT.key", "default.key", "Default.key"):
        with pytest.raises(MalformedConfig):
            config.set(option, "1")


def test_valueless_key_reads_as_empty(repospace):
    write_ini(repospace / ".repospace" / "config", "[a]\nbare\n")
    config = Configuration(topdir=str(repospace))
    assert config.get("a.bare") == ""
    assert config.get("a.bare", default="x") == ""
    assert dict(config.items())["a.bare"] == ""


def test_set_writes_local(repospace):
    config = Configuration(topdir=str(repospace))
    config.set("manifest.path", "app")
    text = (repospace / ".repospace" / "config").read_text()
    assert "path = app" in text
    fresh = Configuration(topdir=str(repospace))
    assert fresh.get("manifest.path") == "app"


def test_set_local_without_repospace_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = Configuration(topdir=None)
    with pytest.raises(MalformedConfig):
        config.set("a.b", "c")


def test_set_global_uses_xdg_when_no_home_file(tmp_path, repospace):
    config = Configuration(topdir=str(repospace))
    config.set("color.ui", "false", configfile=ConfigFile.GLOBAL)
    home = pathlib.Path(os.environ["HOME"])
    xdg_file = home / ".config" / "repospace" / "config"
    assert xdg_file.is_file()
    assert "ui = false" in xdg_file.read_text()


def test_global_home_file_wins_when_it_exists(repospace):
    home = pathlib.Path(os.environ["HOME"])
    write_ini(home / ".repospace-config", "[color]\nui = homefile\n")
    write_ini(home / ".config" / "repospace" / "config", "[color]\nui = xdg\n")
    config = Configuration(topdir=str(repospace))
    assert config.get("color.ui") == "homefile"


def test_delete_highest_precedence_only(tmp_path, monkeypatch, repospace):
    write_ini(tmp_path / "glob.ini", "[a]\nb = global\n")
    write_ini(repospace / ".repospace" / "config", "[a]\nb = local\n")
    monkeypatch.setenv("REPOSPACE_CONFIG_GLOBAL", str(tmp_path / "glob.ini"))
    config = Configuration(topdir=str(repospace))
    config.delete("a.b")
    fresh = Configuration(topdir=str(repospace))
    assert fresh.get("a.b") == "global"


def test_delete_all(tmp_path, monkeypatch, repospace):
    write_ini(tmp_path / "glob.ini", "[a]\nb = global\n")
    write_ini(repospace / ".repospace" / "config", "[a]\nb = local\n")
    monkeypatch.setenv("REPOSPACE_CONFIG_GLOBAL", str(tmp_path / "glob.ini"))
    config = Configuration(topdir=str(repospace))
    config.delete("a.b", configfile=ConfigFile.ALL)
    fresh = Configuration(topdir=str(repospace))
    assert fresh.get("a.b") is None


def test_delete_scoped(tmp_path, monkeypatch, repospace):
    write_ini(tmp_path / "glob.ini", "[a]\nb = global\n")
    write_ini(repospace / ".repospace" / "config", "[a]\nb = local\n")
    monkeypatch.setenv("REPOSPACE_CONFIG_GLOBAL", str(tmp_path / "glob.ini"))
    config = Configuration(topdir=str(repospace))
    config.delete("a.b", configfile=ConfigFile.GLOBAL)
    fresh = Configuration(topdir=str(repospace))
    assert fresh.get("a.b") == "local"
    assert fresh.get("a.b", configfile=ConfigFile.GLOBAL) is None


def test_delete_keeps_other_keys_in_section(repospace):
    write_ini(repospace / ".repospace" / "config", "[a]\nb = 1\nc = 2\n")
    config = Configuration(topdir=str(repospace))
    config.delete("a.b")
    fresh = Configuration(topdir=str(repospace))
    assert fresh.get("a.b") is None
    assert fresh.get("a.c") == "2"


def test_local_path_env_override(tmp_path, monkeypatch):
    override = tmp_path / "cfg" / "local.ini"
    monkeypatch.setenv("REPOSPACE_CONFIG_LOCAL", str(override))
    monkeypatch.chdir(tmp_path)
    config = Configuration(topdir=None)
    config.set("a.b", "1")
    assert "b = 1" in override.read_text()


def test_delete_missing_raises(repospace):
    config = Configuration(topdir=str(repospace))
    with pytest.raises(KeyError):
        config.delete("no.such")


def test_delete_last_key_removes_section(repospace):
    write_ini(repospace / ".repospace" / "config", "[solo]\nkey = 1\n")
    config = Configuration(topdir=str(repospace))
    config.delete("solo.key")
    text = (repospace / ".repospace" / "config").read_text()
    assert "solo" not in text


def test_percent_in_value_is_literal(repospace):
    # "%" must not trigger configparser interpolation (git format strings in aliases, URL-encoded
    # strings, ...).
    config = Configuration(topdir=str(repospace))
    config.set("alias.lg", "log --format=%h")
    fresh = Configuration(topdir=str(repospace))
    assert fresh.get("alias.lg") == "log --format=%h"
    assert dict(fresh.items())["alias.lg"] == "log --format=%h"


def test_items_merged(tmp_path, monkeypatch, repospace):
    write_ini(tmp_path / "glob.ini", "[a]\nb = global\nc = only-global\n")
    write_ini(repospace / ".repospace" / "config", "[a]\nb = local\n")
    monkeypatch.setenv("REPOSPACE_CONFIG_GLOBAL", str(tmp_path / "glob.ini"))
    config = Configuration(topdir=str(repospace))
    merged = dict(config.items())
    assert merged == {"a.b": "local", "a.c": "only-global"}


def test_option_names_case_insensitive(repospace):
    # Setting "Alias.UP" must address the same option every read consults, not a dead "[Alias]"
    # section.
    config = Configuration(topdir=str(repospace))
    config.set("Alias.UP", "update")
    fresh = Configuration(topdir=str(repospace))
    assert fresh.get("alias.up") == "update"
    assert fresh.get("ALIAS.Up") == "update"
    assert dict(fresh.items())["alias.up"] == "update"


def test_sections_differing_by_case_merge_on_read(repospace):
    # Hand-written files: like git config, "[Alias]" and "[alias]" are the same section, with the
    # later definition winning on conflict.
    write_ini(
        repospace / ".repospace" / "config",
        "[Alias]\nUP = one\nonly = here\n[alias]\nup = two\n",
    )
    config = Configuration(topdir=str(repospace))
    assert config.get("alias.up") == "two"
    assert config.get("alias.only") == "here"
    assert dict(config.items()) == {"alias.up": "two", "alias.only": "here"}


def test_write_failure_raises_malformed_config(tmp_path, monkeypatch):
    # A failing write (here: the parent "directory" is a file) must surface as MalformedConfig, like
    # a failing read, not as a raw OSError traceback.
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    monkeypatch.setenv("REPOSPACE_CONFIG_LOCAL", str(blocker / "config"))
    config = Configuration(topdir=str(tmp_path))
    with pytest.raises(MalformedConfig) as excinfo:
        config.set("a.b", "1")
    assert "cannot write configuration file" in str(excinfo.value)


def test_set_preserves_comments_and_layout(repospace):
    path = repospace / ".repospace" / "config"
    write_ini(
        path,
        "# how to fetch\n"
        "[update]\n"
        "; a note\n"
        "fetch = always\n"
        "\n"
        "[color]\n"
        "ui = true\n",
    )
    config = Configuration(topdir=str(repospace))
    config.set("update.fetch", "smart")
    text = path.read_text()
    assert "# how to fetch" in text
    assert "; a note" in text
    assert "fetch = smart" in text
    assert "fetch = always" not in text
    # Untouched parts keep their layout.
    assert "[color]\nui = true\n" in text
    fresh = Configuration(topdir=str(repospace))
    assert fresh.get("update.fetch") == "smart"
    assert fresh.get("color.ui") == "true"


def test_set_new_key_lands_in_existing_section(repospace):
    path = repospace / ".repospace" / "config"
    write_ini(
        path,
        "[update]\n# a comment\nfetch = always\n\n[color]\nui = true\n",
    )
    config = Configuration(topdir=str(repospace))
    config.set("update.narrow", "true")
    text = path.read_text()
    assert "# a comment" in text
    # The new key lands inside its section, before the next header.
    assert text.index("narrow = true") < text.index("[color]")
    fresh = Configuration(topdir=str(repospace))
    assert fresh.get("update.fetch") == "always"
    assert fresh.get("update.narrow") == "true"


def test_delete_preserves_comments(repospace):
    path = repospace / ".repospace" / "config"
    write_ini(path, "# top comment\n[a]\n# about b\nb = 1\nc = 2\n")
    config = Configuration(topdir=str(repospace))
    config.delete("a.b")
    text = path.read_text()
    assert "# top comment" in text
    assert "# about b" in text
    assert "b = 1" not in text
    assert "c = 2" in text


def test_delete_last_key_keeps_section_with_comments(repospace):
    # Removing an all-blank section is tidy; removing one that still holds comments would delete the
    # user's notes with it.
    path = repospace / ".repospace" / "config"
    write_ini(path, "[solo]\n# keep me\nkey = 1\n")
    config = Configuration(topdir=str(repospace))
    config.delete("solo.key")
    text = path.read_text()
    assert "[solo]" in text
    assert "# keep me" in text
    fresh = Configuration(topdir=str(repospace))
    assert fresh.get("solo.key") is None


def test_set_replaces_multiline_value(repospace):
    path = repospace / ".repospace" / "config"
    write_ini(path, "[a]\nb = one\n\ttwo\nc = 3\n")
    config = Configuration(topdir=str(repospace))
    assert config.get("a.b") == "one\ntwo"
    config.set("a.b", "flat")
    fresh = Configuration(topdir=str(repospace))
    assert fresh.get("a.b") == "flat"
    assert fresh.get("a.c") == "3"


def test_set_multiline_value_round_trips(repospace):
    config = Configuration(topdir=str(repospace))
    config.set("a.b", "one\ntwo")
    config.set("a.c", "3")
    fresh = Configuration(topdir=str(repospace))
    assert fresh.get("a.b") == "one\ntwo"
    assert fresh.get("a.c") == "3"


def test_set_matches_section_case_insensitively(repospace):
    # The file's "[Alias]" and the option's "alias" are the same section; the edit must land there,
    # not append a duplicate.
    path = repospace / ".repospace" / "config"
    write_ini(path, "[Alias]\n# mine\nup = update\n")
    config = Configuration(topdir=str(repospace))
    config.set("alias.up", "update -k")
    text = path.read_text()
    assert "# mine" in text
    assert text.count("[") == 1
    fresh = Configuration(topdir=str(repospace))
    assert fresh.get("alias.up") == "update -k"


def test_set_replaces_valueless_key(repospace):
    path = repospace / ".repospace" / "config"
    write_ini(path, "[a]\nbare\n")
    config = Configuration(topdir=str(repospace))
    config.set("a.bare", "1")
    fresh = Configuration(topdir=str(repospace))
    assert fresh.get("a.bare") == "1"
    assert (repospace / ".repospace" / "config").read_text().count("bare") == 1


def test_default_section_rejected_on_read(repospace):
    # A [DEFAULT] section would leak its keys into every section on read, and the next write would
    # then duplicate them into each section. Reserved in any case, matching what set() enforces.
    for section in ("DEFAULT", "default", "Default"):
        write_ini(
            repospace / ".repospace" / "config",
            f"[{section}]\nx = 1\n[test]\ny = 2\n",
        )
        with pytest.raises(MalformedConfig) as excinfo:
            Configuration(topdir=str(repospace))
        assert "reserved" in str(excinfo.value)


@pytest.mark.parametrize(
    "raw,stored",
    [
        ("  lead", "lead"),
        ("trail  ", "trail"),
        ("a  \n   b  \n\n", "a\nb"),
        ("\nfirst line blank", "\nfirst line blank"),
    ],
)
def test_set_stores_the_value_the_reader_gives_back(repospace, raw, stored):
    # configparser strips each line and drops trailing blank lines on read; get() must answer the
    # same in this process and the next.
    config = Configuration(topdir=str(repospace))
    config.set("alias.x", raw)
    assert config.get("alias.x") == stored
    assert Configuration(topdir=str(repospace)).get("alias.x") == stored


def test_set_rejects_carriage_return_in_value(repospace):
    # Universal-newline reading turns a written "\r" into a line break, which would truncate the
    # value and inject the rest as options of its own; the value is refused instead.
    path = repospace / ".repospace" / "config"
    write_ini(path, "[sec]\nopt = safe\n")
    config = Configuration(topdir=str(repospace))
    with pytest.raises(MalformedConfig) as excinfo:
        config.set("sec.opt", "safe\rui = injected")
    assert "carriage return" in str(excinfo.value)
    fresh = Configuration(topdir=str(repospace))
    assert fresh.get("sec.opt") == "safe"
    assert fresh.get("sec.ui") is None


def test_set_rejects_comment_continuation_line(repospace):
    # An indented "#" or ";" line is a comment to configparser, so such a value would silently read
    # back short.
    config = Configuration(topdir=str(repospace))
    for value in ("one\n#two", "one\n;two", "one\n  # two"):
        with pytest.raises(MalformedConfig) as excinfo:
            config.set("alias.x", value)
        assert "comment" in str(excinfo.value)
    assert Configuration(topdir=str(repospace)).get("alias.x") is None


def test_unrelated_set_preserves_line_boundary_characters(repospace):
    # str.splitlines() breaks on boundaries the file format does not have; rewriting them as
    # newlines would corrupt values the edit never touched.
    path = repospace / ".repospace" / "config"
    exotic = "one\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029two"
    path.write_text(f"[a]\nb = {exotic}\nc = 3\n", encoding="utf-8")
    config = Configuration(topdir=str(repospace))
    assert config.get("a.b") == exotic
    config.set("a.c", "4")
    fresh = Configuration(topdir=str(repospace))
    assert fresh.get("a.b") == exotic
    assert fresh.get("a.c") == "4"


def test_failed_write_leaves_the_file_intact(repospace, monkeypatch):
    # The replacement is computed first and swapped in whole: a failure part-way must not leave an
    # empty configuration file behind.
    path = repospace / ".repospace" / "config"
    write_ini(path, "[a]\nb = 1\n")
    config = Configuration(topdir=str(repospace))

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(configuration, "_edited", boom)
    with pytest.raises(RuntimeError):
        config.set("a.b", "2")
    assert path.read_text() == "[a]\nb = 1\n"


def test_set_preserves_file_mode_and_leaves_no_temp_file(repospace):
    path = repospace / ".repospace" / "config"
    write_ini(path, "[a]\nb = 1\n")
    # Not 0600: that is what mkstemp() creates the temporary file with, so it would not show a mode
    # the swap failed to carry over.
    path.chmod(0o640)
    config = Configuration(topdir=str(repospace))
    config.set("a.b", "2")
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert list(path.parent.iterdir()) == [path]


def test_set_agrees_with_the_reader_on_bracketed_section_names(repospace):
    # configparser reads "[a]x]" as the section "a]x". A writer that stopped at the first "]" would
    # overwrite that section's option and write a value no read can find.
    path = repospace / ".repospace" / "config"
    write_ini(path, "[a]x]\nkey = other\n")
    config = Configuration(topdir=str(repospace))
    config.set("a.key", "mine")
    assert "key = other" in path.read_text()
    fresh = Configuration(topdir=str(repospace))
    assert fresh.get("a.key") == "mine"


def test_non_utf8_file_raises_malformed_config(repospace):
    (repospace / ".repospace" / "config").write_bytes(b"[a]\nb = 1\n\xff\xfe\n")
    with pytest.raises(MalformedConfig) as excinfo:
        Configuration(topdir=str(repospace))
    assert "not valid UTF-8" in str(excinfo.value)


def test_set_replaces_an_indented_option(repospace):
    # Directly after a section header nothing is in progress for an indented line to continue, so
    # configparser reads "    up = update" as an option. The editor must find that line too, or it
    # appends a second, unindented copy that the strict reader rejects on every later invocation.
    path = repospace / ".repospace" / "config"
    write_ini(path, "[alias]\n    up = update\n    st = status\n")
    config = Configuration(topdir=str(repospace))
    assert config.get("alias.up") == "update"
    config.set("alias.up", "update -v")
    assert path.read_text() == "[alias]\n    up = update -v\n    st = status\n"
    fresh = Configuration(topdir=str(repospace))
    assert fresh.get("alias.up") == "update -v"
    assert fresh.get("alias.st") == "status"


def test_delete_removes_an_indented_option(repospace):
    path = repospace / ".repospace" / "config"
    write_ini(path, "[alias]\n    up = update\n    st = status\n")
    config = Configuration(topdir=str(repospace))
    config.delete("alias.up")
    assert path.read_text() == "[alias]\n    st = status\n"
    fresh = Configuration(topdir=str(repospace))
    assert fresh.get("alias.up") is None
    assert fresh.get("alias.st") == "status"


def test_set_appends_with_the_indentation_of_the_section(repospace):
    path = repospace / ".repospace" / "config"
    write_ini(path, "[alias]\n    up = update\n\n[color]\nui = auto\n")
    config = Configuration(topdir=str(repospace))
    config.set("alias.st", "status")
    assert path.read_text() == "[alias]\n    up = update\n    st = status\n\n[color]\nui = auto\n"
    fresh = Configuration(topdir=str(repospace))
    assert fresh.get("alias.st") == "status"
    assert fresh.get("color.ui") == "auto"


def test_set_agrees_with_the_reader_on_indented_section_headers(repospace):
    # To configparser an indented "[...]" line is a section header directly after another header,
    # and a continuation of the value after an option. The editor must tell the two apart, or a
    # write addressed to one section lands in another.
    path = repospace / ".repospace" / "config"
    write_ini(path, "[s1]\n  [s2]\nk = 1\n[s3]\na = 1\n  [s4]\n")
    config = Configuration(topdir=str(repospace))
    assert config.get("s1.k") is None
    assert config.get("s2.k") == "1"
    assert config.get("s3.a") == "1\n[s4]"
    config.set("s1.k", "9")
    config.set("s3.a", "flat")
    assert path.read_text() == "[s1]\n  k = 9\n  [s2]\nk = 1\n[s3]\na = flat\n"
    fresh = Configuration(topdir=str(repospace))
    assert fresh.get("s1.k") == "9"
    assert fresh.get("s2.k") == "1"
    assert fresh.get("s3.a") == "flat"


def test_edits_leave_an_indented_comment_after_the_option_alone(repospace):
    # A comment is a comment to configparser however it is indented, so it is no part of the value
    # block and must survive the option being replaced or deleted.
    path = repospace / ".repospace" / "config"
    write_ini(path, "[s]\nk = 1\n  # keep me\nj = 2\n")
    config = Configuration(topdir=str(repospace))
    assert config.get("s.k") == "1"
    config.set("s.k", "5")
    assert path.read_text() == "[s]\nk = 5\n  # keep me\nj = 2\n"
    config.delete("s.k")
    assert path.read_text() == "[s]\n  # keep me\nj = 2\n"
    fresh = Configuration(topdir=str(repospace))
    assert fresh.get("s.k") is None
    assert fresh.get("s.j") == "2"


def test_failed_write_leaves_the_instance_unchanged(repospace, monkeypatch):
    # The in-memory view follows the file: a caller that survives the error must not keep reading a
    # value the file never received.
    path = repospace / ".repospace" / "config"
    write_ini(path, "[a]\nb = 1\n")
    config = Configuration(topdir=str(repospace))

    def boom(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(configuration, "_replace_file", boom)
    with pytest.raises(MalformedConfig):
        config.set("a.b", "2")
    assert config.get("a.b") == "1"
    with pytest.raises(MalformedConfig):
        config.set("c.d", "3")
    assert config.get("c.d") is None
    with pytest.raises(MalformedConfig):
        config.delete("a.b")
    assert config.get("a.b") == "1"
