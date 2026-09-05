"""The init command."""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import tempfile
from urllib.parse import urlparse

from repospace import util
from repospace.commands import HelpFormatter, RepospaceCommand, Verbosity
from repospace.configuration import Configuration
from repospace.git import run_git
from repospace.manifest import MANIFEST_FILE, ImportFlag, Manifest

_USAGE = "%(prog)s [-h] [--manifest FILE] [REPOSITORY] [DIRECTORY] [-- GIT_OPTIONS]"

_DESCRIPTION = f"""\
Create a repospace.

REPOSITORY is a git URI (local or remote) representing the repository
to be cloned. The repospace is created inside the clone, which must
contain the manifest file. Local repositories must be expressed as a
file:// URL rather than a plain path or they will be treated as
pre-existing local folders for in-place initialization — not a
repository to be cloned. The clone will be placed at DIRECTORY if one
is provided. Otherwise a destination directory will be computed by
appending the manifest's "self: name:" to the current working
directory. A missing self name defaults to the REPOSITORY basename
without any .git suffix. In all cases the clone folder must be a
non-existing or empty directory.

If REPOSITORY is omitted, DIRECTORY must instead point to a
pre-existing local folder containing the manifest file. The repospace
is created inside it and nothing is cloned. If both REPOSITORY and
DIRECTORY are omitted the current working directory is used as the
pre-existing local folder.

GIT_OPTIONS are passed to "git clone" directly. The clone's own
positional arguments (<repository> and <directory>) are supplied by
repospace and cannot be overridden. In addition "repospace -q init"
and "repospace -v init" relay -q/--verbose to git clone. If REPOSITORY
is omitted, GIT_OPTIONS are ignored with a warning.

The manifest file defaults to {MANIFEST_FILE}. --manifest FILE selects
another file, relative to the directory being initialized and recorded
as the manifest.file option.

positional arguments:
  REPOSITORY        git repository to clone, remote or local; a local one
                    must be a file:// URL, since a plain existing path is
                    taken as an existing directory for in-place
                    initialization
  DIRECTORY         where the manifest file is looked up and the repospace
                    is created: an existing directory (default: .) without
                    REPOSITORY, else the clone destination (default:
                    self:name, else the REPOSITORY basename without .git)
  -- GIT_OPTIONS    options passed to git clone; can only appear when
                    REPOSITORY is provided
"""


def _clone_dir_name(url: str) -> str:
    """Derive a clone directory name from a URL, like git clone does."""
    stripped = url.rstrip("/")
    path = urlparse(stripped).path or stripped
    path = path.rstrip("/")
    if path.endswith("/.git"):
        # "host/foo/.git" names the foo repository, as in git clone.
        path = path[: -len("/.git")]
    name = path.rpartition("/")[2]
    name = name.rpartition(":")[2]
    if name.endswith(".git") and len(name) > len(".git"):
        name = name[: -len(".git")]
    return name


def _set_umask_mode(path: pathlib.Path) -> None:
    """Give *path* the mode a directory created by the user would get."""
    umask = os.umask(0)
    os.umask(umask)
    try:
        os.chmod(path, 0o777 & ~umask)
    except OSError:
        # Being readable by other users (CI, build services) is a
        # convenience; a filesystem without POSIX modes is not an error.
        pass


def _remove_contents(path: pathlib.Path) -> None:
    """Delete everything inside *path*, keeping the directory itself.

    Used to undo a clone into a directory that was already there: the
    directory's inode, mode, ownership and ACLs must survive, and other
    processes may have it as their working directory.
    """
    try:
        entries = list(path.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            try:
                entry.unlink()
            except OSError:
                pass


class Init(RepospaceCommand):
    def __init__(self):
        super().__init__(
            "init",
            "create a repospace in place or by cloning a manifest repository",
            _DESCRIPTION,
            requires_repospace=False,
        )

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(
            self.name,
            help=self.help,
            usage=_USAGE,
            description=self.description,
            formatter_class=HelpFormatter,
        )
        parser.add_argument(
            "--manifest",
            metavar="FILE",
            help="manifest file to use, relative to the directory being "
            f"initialized (default: {MANIFEST_FILE}); must be given "
            "before the positional arguments",
        )
        # The general syntax is [REPOSITORY] [DIRECTORY] [-- GIT_OPTIONS],
        # but the parser cannot mirror it: only the existing-directory
        # test tells REPOSITORY and DIRECTORY apart, so one argument
        # takes the first positional and a REMAINDER takes the rest.
        # Their help entries are hand-written in _DESCRIPTION, one per
        # syntax element, and the argparse entries are suppressed.
        parser.add_argument(
            "location",
            nargs="?",
            metavar="REPOSITORY | DIRECTORY",
            help=argparse.SUPPRESS,
        )
        parser.add_argument(
            "rest",
            nargs=argparse.REMAINDER,
            metavar="[DIRECTORY] [-- GIT_OPTIONS]",
            help=argparse.SUPPRESS,
        )
        return parser

    @staticmethod
    def _is_empty_dir(path: pathlib.Path) -> bool:
        return path.is_dir() and not any(path.iterdir())

    @staticmethod
    def _split_rest(rest):
        """Split REMAINDER args into (directory, git clone options)."""
        directory = None
        if rest and rest[0] != "--" and not rest[0].startswith("-"):
            directory = rest[0]
            rest = rest[1:]
        if rest and rest[0] == "--":
            rest = rest[1:]
        return directory, rest

    def do_run(self, args, unknown):
        location = args.location
        rest = list(args.rest)
        if location and location.startswith("-") and location != "-":
            # argparse dropped the "--" of "init -- GIT_OPTIONS..." and
            # made the first option the DIRECTORY|REPOSITORY positional;
            # everything was options.
            rest = [location] + rest
            location = None
        location = location or "."
        mfile = args.manifest or MANIFEST_FILE
        if os.path.isabs(mfile):
            self.die(f"--manifest {mfile} must be a relative path")
        directory, git_options = self._split_rest(rest)
        if pathlib.Path(location).is_dir():
            self._init_in_place(location, mfile, args.manifest, directory, git_options)
        else:
            self._init_from_clone(location, mfile, args.manifest, directory, git_options)

    def _init_in_place(self, location, mfile, manifest_arg, directory, git_options):
        manifest_dir = pathlib.Path(location).resolve()
        if directory is not None:
            self.die(
                f"unexpected argument {directory}: {manifest_dir} is an "
                "existing directory, so there is nothing to clone"
            )
        if git_options:
            self.wrn(
                f"ignoring {util.quote_sh_list(git_options)}: git clone "
                f"was not invoked because {manifest_dir} is an existing "
                "directory"
            )
        target = manifest_dir / mfile
        if util.escapes_directory(target, manifest_dir):
            self.die(f"--manifest {mfile} escapes {manifest_dir}")
        if not target.is_file():
            self.die(f"manifest file not found: {target}")
        self._die_if_in_repospace(manifest_dir)
        rdir = manifest_dir / util.REPOSPACE_DIR
        try:
            rdir.mkdir()
        except FileExistsError:
            # topdir() only recognizes a directory, so what exists here
            # is something else (e.g. a file named .repospace).
            self.die(f"{rdir} exists and is not a directory")
        except OSError as err:
            self.die(f"cannot create {rdir}: {err}")
        if manifest_arg:
            Configuration(str(manifest_dir)).set("manifest.file", mfile)
        self.banner(f'Initialized. Now run "repospace update" inside {manifest_dir}.')

    def _die_if_in_repospace(self, where: pathlib.Path):
        try:
            existing = util.topdir(where)
        except util.RepospaceNotFound:
            return
        self.die(f"already in a repospace: {existing}")

    def _init_from_clone(self, repository, mfile, manifest_arg, directory, git_options):
        self.die_if_no_git()
        cwd = pathlib.Path(os.getcwd()).resolve()
        self._die_if_in_repospace(cwd)

        if directory:
            dest = pathlib.Path(directory)
            preexisting = dest.exists()
            # DIRECTORY can name a place outside the current directory,
            # which was checked above; a repospace must not be nested in
            # another one wherever the clone lands.
            self._die_if_in_repospace(dest.resolve())
            # Like git clone, an existing destination is acceptable only
            # when it is an empty directory.
            if preexisting and not self._is_empty_dir(dest):
                self.die(f"refusing to overwrite existing path: {dest}")
            self._clone(repository, git_options, str(dest))
            dest = dest.resolve()
            try:
                self._check_clone(dest, mfile)
            except SystemExit:
                # Do not leave a clone behind that cannot become a
                # repospace; parent directories created by git are kept.
                if preexisting:
                    # The destination existed (empty) before this run:
                    # take the clone back out of it instead of deleting
                    # the directory itself, which would substitute a new
                    # one for the user's (DIRECTORY can be ".").
                    _remove_contents(dest)
                else:
                    shutil.rmtree(dest, ignore_errors=True)
                raise
        else:
            try:
                tmp = pathlib.Path(tempfile.mkdtemp(prefix=".repospace-clone-", dir=cwd))
            except OSError as err:
                self.die(f"cannot create a temporary directory in {cwd}: {err}")
            try:
                # git clone accepts an existing empty directory.
                self._clone(repository, git_options, str(tmp))
                self._check_clone(tmp, mfile)
                dest = cwd / self._dest_name(repository, tmp, mfile)
                preexisting = dest.exists()
                if preexisting:
                    # Like git clone, an existing destination is
                    # acceptable only when it is an empty directory; the
                    # rename below replaces it.
                    if not self._is_empty_dir(dest):
                        self.die(f"refusing to overwrite existing path: {dest}")
                    try:
                        dest.rmdir()
                    except OSError as err:
                        self.die(f"cannot move the clone to {dest}: {err}")
                try:
                    os.rename(tmp, dest)
                except OSError as err:
                    if preexisting:
                        # Put back the empty directory removed just
                        # above, so a failed run leaves the destination
                        # as it was found.
                        try:
                            dest.mkdir(exist_ok=True)
                        except OSError:
                            # The rename error below is the one worth
                            # reporting.
                            pass
                    self.die(f"cannot move the clone to {dest}: {err}")
                # mkdtemp made the directory 0700 for its own sake; the
                # repospace root gets ordinary permissions instead, so
                # that other users (CI, build services) can reach it.
                _set_umask_mode(dest)
            finally:
                # Gone already if the rename above succeeded.
                if tmp.exists():
                    shutil.rmtree(tmp, ignore_errors=True)

        rdir = dest / util.REPOSPACE_DIR
        try:
            rdir.mkdir()
        except OSError as err:
            self.die(f"cannot create {rdir}: {err}")
        if manifest_arg:
            Configuration(str(dest)).set("manifest.file", mfile)
        self.banner(f'Initialized. Now run "repospace update" inside {dest}.')

    def _clone(self, repository: str, git_options, dest: str):
        self.small_banner(f"Cloning manifest repository from {repository}")
        # Relay repospace verbosity; the user's options come after and
        # win on conflict.
        options = []
        if self.verbosity < Verbosity.INF:
            options.append("-q")
        elif self.verbosity >= Verbosity.DBG:
            options.append("--verbose")
        run_git(["clone"] + options + git_options + ["--", repository, dest])

    def _check_clone(self, where: pathlib.Path, mfile: str):
        target = where / mfile
        if util.escapes_directory(target, where):
            self.die(f"--manifest {mfile} escapes the cloned repository")
        if not target.is_file():
            self.die(f"cannot initialize: cloned repository has no {mfile}")
        if (where / util.REPOSPACE_DIR).exists():
            self.die(
                f"cannot initialize: cloned repository already contains "
                f"{util.REPOSPACE_DIR}; it must not be committed"
            )

    def _dest_name(self, repository: str, clone_dir: pathlib.Path, mfile: str) -> str:
        try:
            parsed = Manifest.from_file(clone_dir / mfile, import_flags=ImportFlag.IGNORE)
        except Exception as err:
            self.die(f"cannot parse cloned manifest: {err}")
        if parsed.yaml_name:
            return parsed.yaml_name
        name = _clone_dir_name(repository)
        if not name or name in (".", "..", ".git", util.REPOSPACE_DIR):
            self.die(f"cannot derive a directory name from {repository}; " "pass DIRECTORY explicitly")
        return name
