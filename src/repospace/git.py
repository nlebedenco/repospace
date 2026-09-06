"""Git subprocess plumbing shared by all repospace code."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Optional, Sequence, Tuple

from repospace.util import PathType

_git_executable: Optional[str] = None
_git_version: Optional[Tuple[int, ...]] = None


class GitNotFound(RuntimeError):
    """Raised when no git executable can be found."""


def git_executable() -> str:
    """Return the path of the git executable, caching the lookup."""
    global _git_executable
    if _git_executable is None:
        exe = shutil.which("git")
        if exe is None:
            raise GitNotFound("git is not installed or not on PATH; repospace requires git")
        _git_executable = exe
    return _git_executable


def git_version() -> Tuple[int, ...]:
    """Return git's version as a tuple of ints, usually 3 elements.

    Tolerates vendor suffixes such as "2.28.0.windows.1", "2.24.3 (Apple Git-128)", and truncated
    forms like "2.29.GIT".
    """
    global _git_version
    if _git_version is None:
        out = run_git(["--version"], capture_stdout=True).stdout.decode(errors="backslashreplace")
        match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", out)
        if not match:
            raise RuntimeError(f'cannot parse git version from "{out.strip()}"')
        parts = [match.group(1), match.group(2), match.group(3)]
        _git_version = tuple(int(p) for p in parts if p is not None)
    return _git_version


def run_git(
    args: Sequence[str],
    cwd: Optional[PathType] = None,
    check: bool = True,
    capture_stdout: bool = False,
    capture_stderr: bool = False,
    env_extra: Optional[dict] = None,
) -> subprocess.CompletedProcess:
    """Run git with *args*, the single choke point for all git invocations."""
    cmd = [git_executable()] + [os.fspath(a) for a in args]
    env = None
    if env_extra:
        env = dict(os.environ)
        env.update(env_extra)
    return subprocess.run(
        cmd,
        cwd=None if cwd is None else os.fspath(cwd),
        check=check,
        stdout=subprocess.PIPE if capture_stdout else None,
        stderr=subprocess.PIPE if capture_stderr else None,
        env=env,
    )
