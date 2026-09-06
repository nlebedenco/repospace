"""Minimal ANSI terminal color support."""

from __future__ import annotations

import os
import sys

RED = "\033[91m"
YELLOW = "\033[93m"
GREEN = "\033[92m"
BOLD = "\033[1m"
RESET = "\033[0m"


def use_color(stream=None, color_ui: bool = True) -> bool:
    """Decide whether ANSI colors should be emitted on *stream*.

    Colors are disabled by the color.ui configuration option, by the NO_COLOR environment variable,
    or when the stream is not a terminal.
    """
    if not color_ui:
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if stream is None:
        stream = sys.stdout
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        # No isatty(): not a terminal. ValueError: the stream is closed, which is not a terminal
        # either — and deciding on a color is never worth failing an otherwise good command.
        return False
