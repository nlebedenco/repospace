"""Unit tests for the git subprocess plumbing."""

from __future__ import annotations

import subprocess

import pytest

import repospace.git as gitmod


def test_git_version_parses_and_caches(monkeypatch):
    monkeypatch.setattr(gitmod, "_git_version", None)
    version = gitmod.git_version()
    assert version >= (2,)
    # Cached: a second call must not rerun git.
    monkeypatch.setattr(gitmod, "run_git", None)
    assert gitmod.git_version() == version


def test_git_version_unparseable(monkeypatch):
    monkeypatch.setattr(gitmod, "_git_version", None)
    monkeypatch.setattr(
        gitmod,
        "run_git",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=b"git version garbage\n"),
    )
    with pytest.raises(RuntimeError, match="cannot parse git version"):
        gitmod.git_version()


def test_run_git_env_extra(tmp_path):
    # env_extra must reach git itself: point GIT_CONFIG_GLOBAL at a
    # config only this call can see, and read a value back out of it.
    # The isolated environment's own global config has no such key, so
    # dropping env_extra makes git exit non-zero and run_git raise.
    gitconfig = tmp_path / "extra-gitconfig"
    gitconfig.write_text("[repospace]\n    envextra = seen\n")
    result = gitmod.run_git(
        ["config", "--global", "--get", "repospace.envextra"],
        capture_stdout=True,
        env_extra={"GIT_CONFIG_GLOBAL": str(gitconfig)},
    )
    assert result.returncode == 0
    assert result.stdout.decode().strip() == "seen"
    # Without it, the same call finds nothing.
    with pytest.raises(subprocess.CalledProcessError):
        gitmod.run_git(
            ["config", "--global", "--get", "repospace.envextra"],
            capture_stdout=True,
        )
