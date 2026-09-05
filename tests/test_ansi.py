"""Unit tests for ANSI color decisions."""

from __future__ import annotations

import sys

from repospace import ansi


class FakeTTY:
    def isatty(self):
        return True


def test_color_ui_false_disables():
    assert ansi.use_color(FakeTTY(), color_ui=False) is False


def test_no_color_env_disables(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert ansi.use_color(FakeTTY()) is False


def test_stream_defaults_to_stdout(monkeypatch):
    monkeypatch.setattr(sys, "stdout", FakeTTY())
    assert ansi.use_color() is True


def test_non_tty_disables():
    assert ansi.use_color(object()) is False


def test_closed_stream_disables(tmp_path):
    # isatty() raises ValueError on a closed stream; deciding on a
    # color must not fail an otherwise good command.
    stream = open(tmp_path / "closed", "w")
    stream.close()
    assert ansi.use_color(stream) is False
