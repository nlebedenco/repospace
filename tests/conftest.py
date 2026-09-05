"""Shared fixtures: full host isolation and git repository factories."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest


@pytest.fixture(autouse=True)
def isolate_env(tmp_path, monkeypatch):
    """Isolate tests from the host's HOME, git and repospace configuration."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # pathlib.Path.home() ignores HOME on Windows: ntpath.expanduser
    # consults USERPROFILE, then HOMEDRIVE+HOMEPATH. Without these the
    # global-config tests would read and write the real profile.
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    # Redirected, not deleted: deleting it lets the system scope fall
    # back to the host's real /etc/repospace-config, whose contents
    # would then leak into every test.
    monkeypatch.setenv("REPOSPACE_CONFIG_SYSTEM", str(tmp_path / "no-system-config"))
    for var in (
        "REPOSPACE_CONFIG_GLOBAL",
        "REPOSPACE_CONFIG_LOCAL",
        "NO_COLOR",
    ):
        monkeypatch.delenv(var, raising=False)
    gitconfig = home / "gitconfig"
    gitconfig.write_text(textwrap.dedent("""\
            [user]
                name = Test User
                email = test@example.com
            [init]
                defaultBranch = main
            [protocol "file"]
                allow = always
            """))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    yield


@pytest.fixture(autouse=True)
def isolate_process_state():
    """Undo the process-global state extension loading leaves behind.

    Loading an extension command appends its directory to sys.path and
    caches the imported module by resolved path. Both outlive the test
    that caused them: sys.path grows with entries pointing at deleted
    temporary directories, and a later test reusing a path could be
    served a module imported from a previous test's file.
    """
    from repospace import commands

    saved_path = list(sys.path)
    commands._EXT_MODULES_CACHE.clear()
    try:
        yield
    finally:
        # In place, so a test that swapped sys.path for a copy of its
        # own (and whose monkeypatch undo runs after this) is unharmed.
        sys.path[:] = saved_path
        commands._EXT_MODULES_CACHE.clear()


def _git(repo, *args, capture=False):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE if capture else None,
        text=capture or None,
    )


class RepoFactory:
    """Create throwaway git repositories under a base directory."""

    def __init__(self, base):
        self.base = base

    def create(self, name, files=None, branch="main"):
        """Create a repo with an initial commit; return its path."""
        path = self.base / name
        path.mkdir(parents=True)
        _git(path, "init", "-q", "-b", branch)
        self.commit(path, files or {"README.md": f"# {name}\n"})
        return path

    def commit(self, repo, files, message="commit"):
        for relpath, content in files.items():
            dest = repo / relpath
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", message)
        return self.head(repo)

    def head(self, repo):
        return _git(repo, "rev-parse", "HEAD", capture=True).stdout.strip()

    def tag(self, repo, name):
        _git(repo, "tag", "-a", name, "-m", name)

    def branch(self, repo, name):
        _git(repo, "branch", name)

    def bare_clone(self, repo, name=None):
        """Clone *repo* into a bare repository; return its path."""
        bare = self.base / (name or (repo.name + ".git"))
        subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(bare)], check=True)
        return bare


@pytest.fixture
def repos(tmp_path):
    return RepoFactory(tmp_path / "repos")


LIBA_EXT_PY = """\
from repospace.commands import RepospaceCommand


class LibACommand(RepospaceCommand):
    def __init__(self):
        super().__init__(
            "liba-hello",
            "print a greeting from liba",
            "Print a greeting from the liba extension command.",
            requires_repospace=False,
        )

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, help=self.help)
        parser.add_argument("--shout", action="store_true")
        return parser

    def do_run(self, args, unknown):
        message = "hello from liba"
        print(message.upper() if args.shout else message)
"""

LIBA_EXT_YAML = """\
extension-commands:
  - file: scripts/liba_ext.py
    commands:
      - name: liba-hello
        class: LibACommand
        help: print a greeting from liba
"""


class Topology:
    """The standard test topology.

    app (manifest repo, self path "app", package App)
      liba  branch main, imports its own repospace.yaml -> libc,
            provides package LibA and the liba-hello extension command
      libb  tag v1.0
      libc  branch main, declared by liba's manifest
    """

    def __init__(self, repos, tmp_path):
        self.repos = repos
        self.libc_src = repos.create("libc-src", {"libc.txt": "c\n"})
        self.liba_src = repos.create(
            "liba-src",
            {
                "liba.txt": "a\n",
                "repospace.yaml": (
                    "manifest:\n"
                    "  members:\n"
                    "    - name: libc\n"
                    f"      url: {self.url(self.libc_src)}\n"
                    "  self:\n"
                    "    cmake-packages: [LibA]\n"
                    "    extension-commands: repospace-commands.yaml\n"
                ),
                "repospace-commands.yaml": LIBA_EXT_YAML,
                "scripts/liba_ext.py": LIBA_EXT_PY,
            },
        )
        self.libb_src = repos.create("libb-src", {"libb.txt": "b\n"})
        repos.tag(self.libb_src, "v1.0")
        self.ws = repos.base / "ws"
        self.app = repos.create("ws/app", {"repospace.yaml": self.app_yaml()})

    @staticmethod
    def url(path):
        return f"file://{path}"

    def app_yaml(self, extra_members="", group_filter=""):
        return (
            "manifest:\n" + (f"  group-filter: [{group_filter}]\n" if group_filter else "") + "  members:\n"
            "    - name: liba\n"
            f"      url: {self.url(self.liba_src)}\n"
            "      import: true\n"
            "    - name: libb\n"
            f"      url: {self.url(self.libb_src)}\n"
            "      revision: v1.0\n" + extra_members + "  self:\n"
            "    name: app\n"
            "    cmake-packages: [App]\n"
        )

    def rewrite_app_yaml(self, **kwargs):
        (self.app / "repospace.yaml").write_text(self.app_yaml(**kwargs))


@pytest.fixture
def topology(repos, tmp_path):
    return Topology(repos, tmp_path)


@pytest.fixture
def repospace(topology, run_repospace):
    """An initialized (not yet updated) repospace for the topology.

    Colocated: the app manifest repo is the repospace root; members
    clone into it. topology.ws is repointed at it so tests address
    members as <ws>/<member>.
    """
    code, out, err = run_repospace(["init"], cwd=topology.app)
    assert code == 0, err
    topology.ws = topology.app
    return topology


@pytest.fixture
def run_repospace(capfd):
    """Invoke the repospace CLI in-process; return (exit_code, out, err).

    Captures at the file-descriptor level so output written by child
    processes (git, forall shell commands) is captured too.
    """

    def run(args, cwd=None):
        from repospace.app.main import main

        prev = os.getcwd()
        if cwd is not None:
            os.chdir(cwd)
        try:
            code = 0
            try:
                main(list(args))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
            sys.stdout.flush()
            sys.stderr.flush()
            captured = capfd.readouterr()
            return code, captured.out, captured.err
        finally:
            os.chdir(prev)

    return run
