"""Repospace location and miscellaneous helpers."""

from __future__ import annotations

import os
import pathlib
import shlex
from typing import Optional, Union

PathType = Union[str, "os.PathLike[str]"]

# Name of the directory that marks the repospace top-level directory and holds the local
# configuration file and generated files.
REPOSPACE_DIR = ".repospace"


class RepospaceNotFound(RuntimeError):
    """Raised when no repospace can be located."""


def topdir(start: Optional[PathType] = None) -> str:
    """Return the absolute path of the repospace top-level directory.

    Starting at *start* (default: the current working directory), walk up the directory tree looking
    for a ".repospace" directory. Raise RepospaceNotFound if the filesystem root is reached without
    finding one.
    """
    origin = os.fspath(start) if start is not None else os.getcwd()
    cur = pathlib.Path(origin).resolve()
    while True:
        if (cur / REPOSPACE_DIR).is_dir():
            return os.fspath(cur)
        if cur.parent == cur:
            raise RepospaceNotFound(
                f'could not find a repospace in "{origin}" or any parent directory'
            )
        cur = cur.parent


def repospace_dir(start: Optional[PathType] = None) -> str:
    """Return the absolute path of the repospace's .repospace directory."""
    return os.path.join(topdir(start), REPOSPACE_DIR)


def escapes_directory(path: PathType, directory: PathType) -> bool:
    """Return True if *path* does not lie inside *directory*.

    Both are resolved before comparison, so symlinks and ".." components cannot be used to slip
    outside.
    """
    resolved = pathlib.Path(path).resolve()
    base = pathlib.Path(directory).resolve()
    try:
        resolved.relative_to(base)
        return False
    except ValueError:
        return True


def quote_sh_list(cmd) -> str:
    """Return a shell-quoted rendering of a command argument list."""
    return " ".join(shlex.quote(os.fspath(arg)) for arg in cmd)
