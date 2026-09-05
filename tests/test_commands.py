"""Unit tests for the RepospaceCommand framework."""

from __future__ import annotations

import argparse
import subprocess
import sys

import pytest

from repospace import ansi
from repospace.commands import RepospaceCommand, Verbosity
from repospace.configuration import MalformedConfig


class Dummy(RepospaceCommand):
    def __init__(self, **kwargs):
        kwargs.setdefault("requires_repospace", False)
        super().__init__("dummy", "help", "description", **kwargs)
        self.ran = None

    def do_add_parser(self, parser_adder):
        return parser_adder.add_parser(self.name)

    def do_run(self, args, unknown):
        self.ran = (args, unknown)


class FakeTTY:
    def isatty(self):
        return True


class BadColorConfig:
    """A configuration whose color.ui value getboolean rejects."""

    def getboolean(self, option, default=None, configfile=None):
        raise MalformedConfig(f'"{option}" is not a boolean: "nonsense"')


def make_command(cls=Dummy, **kwargs):
    command = cls(**kwargs)
    parser = argparse.ArgumentParser(prog="repospace")
    command.add_parser(parser.add_subparsers())
    return command


def test_add_parser_must_return_parser():
    class NoParser(Dummy):
        def do_add_parser(self, parser_adder):
            parser_adder.add_parser(self.name)
            return None

    parser = argparse.ArgumentParser(prog="repospace")
    with pytest.raises(ValueError, match="did not return a parser"):
        NoParser().add_parser(parser.add_subparsers())


def test_unknown_args_rejected(capsys):
    command = make_command()
    args = command.parser.parse_args([])
    with pytest.raises(SystemExit) as excinfo:
        command.run(args, ["--bogus"], None, config=object())
    assert excinfo.value.code == 2
    assert "unexpected arguments" in capsys.readouterr().err


def test_pre_run_hook_runs_before_do_run():
    command = make_command()
    calls = []
    command.add_pre_run_hook(lambda cmd: calls.append((cmd, cmd.ran)))
    args = command.parser.parse_args([])
    command.run(args, [], None)
    assert calls == [(command, None)]
    assert command.ran == (args, [])


def test_manifest_missing_dies(capsys):
    command = make_command()
    assert not command.has_manifest
    with pytest.raises(SystemExit):
        command.manifest
    assert "requires the manifest" in capsys.readouterr().err


def test_config_missing_dies(capsys):
    command = make_command()
    assert not command.has_config
    with pytest.raises(SystemExit):
        command.config
    assert "requires configuration" in capsys.readouterr().err


def test_context_setters():
    command = make_command()
    command.manifest = "manifest"
    command.config = "config"
    assert command.manifest == "manifest"
    assert command.config == "config"
    assert command.has_manifest
    assert command.has_config


def test_color_ui_defaults_to_true_without_config():
    assert make_command().color_ui is True


def test_bad_color_ui_falls_back_and_warns_once(capsys):
    # A value getboolean rejects must not take down every invocation
    # (including the "config -d" that would remove it); every output
    # helper reads color_ui.
    command = make_command()
    command.config = BadColorConfig()
    assert command.color_ui is True
    assert command.color_ui is True
    err = capsys.readouterr().err
    assert "not a boolean" in err
    assert err.count("WARNING") == 1


def test_bad_color_ui_still_reports_the_real_error(capsys):
    # die() colorizes too; raising there would replace the fatal error
    # the user has to see with one about the color option.
    command = make_command()
    command.config = BadColorConfig()
    with pytest.raises(SystemExit):
        command.die("the real problem")
    assert "FATAL ERROR: the real problem" in capsys.readouterr().err


def test_colorize_on_tty():
    command = make_command()
    text = command._colorize("hi", ansi.RED, FakeTTY())
    assert text == f"{ansi.RED}hi{ansi.RESET}"


def test_output_below_verbosity_is_silent(capsys):
    command = make_command(verbosity=Verbosity.QUIET)
    command.inf("info")
    command.wrn("warning")
    command.err("error")
    command.banner("banner")
    command.small_banner("small banner")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_inf_colorize_without_tty_prints_plain(capsys):
    command = make_command()
    command.inf("a", "b", colorize=True)
    assert capsys.readouterr().out == "a b\n"


def test_inf_flushes(monkeypatch):
    # Unflushed, info output interleaves wrongly with child-process
    # output when stdout is a pipe.
    class Recorder:
        def __init__(self):
            self.flushed = False

        def write(self, text):
            return len(text)

        def flush(self):
            self.flushed = True

    stream = Recorder()
    monkeypatch.setattr(sys, "stdout", stream)
    make_command().inf("info")
    assert stream.flushed


def test_subprocess_helpers(tmp_path):
    command = make_command()
    command.check_call([sys.executable, "-c", "pass"], cwd=str(tmp_path))
    out = command.check_output([sys.executable, "-c", "print('hi')"])
    assert out == b"hi\n"


def test_run_subprocess_defaults_to_bytes():
    # No injected defaults: a default "errors" (or any text-mode
    # trigger) would silently hand every caller str instead of the
    # bytes subprocess.run returns.
    command = make_command()
    result = command.run_subprocess([sys.executable, "-c", "print('hi')"], stdout=subprocess.PIPE)
    assert result.stdout == b"hi\n"


def test_die_if_no_git(monkeypatch, capsys):
    import repospace.git as gitmod

    monkeypatch.setattr(gitmod, "_git_executable", None)
    monkeypatch.setattr(gitmod.shutil, "which", lambda name: None)
    command = make_command()
    with pytest.raises(SystemExit):
        command.die_if_no_git()
    assert "git is not installed" in capsys.readouterr().err
