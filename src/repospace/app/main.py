"""The repospace application: argument dispatch and top-level help."""

from __future__ import annotations

import argparse
import logging
import shlex
import shutil
import subprocess
import sys
import textwrap
from typing import Dict, List, NamedTuple, Optional

from repospace import __version__, util
from repospace.commands import (
    CommandError,
    ExtCommandSpec,
    ExtensionCommandError,
    RepospaceCommand,
    Verbosity,
    extension_commands,
)
from repospace.configuration import ConfigFile, Configuration, MalformedConfig
from repospace.git import GitNotFound
from repospace.manifest import (
    MalformedManifest,
    Manifest,
    ManifestImportFailed,
    ManifestVersionError,
)

from repospace.app.config import Config
from repospace.app.gitcmds import Compare, Diff, ForAll, Grep, Status
from repospace.app.init import Init
from repospace.app.inspection import List as ListCommand
from repospace.app.inspection import ManifestCommand, Topdir
from repospace.app.update import Update

#: Commands allowed to run when the manifest cannot be loaded. "update" is
#: included because running it is the way to fix a broken repospace-rev,
#: and "manifest" because --validate/--path are how to debug the failure.
NO_MANIFEST_OK = frozenset({"help", "config", "topdir", "init", "update", "manifest"})


class EarlyArgs(NamedTuple):
    help: bool
    version: bool
    verbosity_delta: int
    command_name: Optional[str]
    unexpected: Optional[str]


def parse_early_args(argv: List[str]) -> EarlyArgs:
    """Scan global options up to the command name, by hand.

    This keeps everything after the command name untouched for the
    command's own parser (extensions included), while still letting global
    flags like -v adjust behavior before any parser exists.
    """
    help_flag = False
    version = False
    delta = 0
    command = None
    unexpected = None
    for arg in argv:
        if arg == "--help":
            help_flag = True
        elif arg == "--version":
            version = True
        elif arg == "--verbose":
            delta += 1
        elif arg == "--quiet":
            delta -= 1
        elif arg.startswith("--"):
            unexpected = arg
            break
        elif arg.startswith("-") and len(arg) > 1:
            for ch in arg[1:]:
                if ch == "h":
                    help_flag = True
                elif ch == "V":
                    version = True
                elif ch == "v":
                    delta += 1
                elif ch == "q":
                    delta -= 1
                else:
                    unexpected = arg
                    break
            if unexpected:
                break
        else:
            command = arg
            break
    return EarlyArgs(help_flag, version, delta, command, unexpected)


def _clamp_verbosity(value: int) -> Verbosity:
    return Verbosity(max(Verbosity.QUIET, min(Verbosity.DBG_EXTREME, value)))


class _LogHandler(logging.Handler):
    """Emit library log records with the CLI's warning/error format.

    sys.stderr is looked up per record, not at construction, so
    redirections (tests, callers embedding main) are honored.
    """

    def emit(self, record):
        message = self.format(record)
        if record.levelno >= logging.WARNING:
            message = f"{record.levelname}: {message}"
        print(message, file=sys.stderr, flush=True)


def _configure_logging(verbosity: Verbosity) -> None:
    """Route repospace library logging through the CLI conventions.

    Warnings match wrn() output and are silenced by -qq like every
    other warning; -v enables the libraries' debug messages. Without a
    handler, logging's last-resort fallback would print bare messages
    that ignore verbosity.
    """
    logger = logging.getLogger("repospace")
    logger.handlers[:] = [_LogHandler()]
    logger.propagate = False
    if verbosity >= Verbosity.DBG:
        logger.setLevel(logging.DEBUG)
    elif verbosity >= Verbosity.WRN:
        logger.setLevel(logging.WARNING)
    else:
        logger.setLevel(logging.ERROR)


class Help(RepospaceCommand):
    #: Back-pointer to the application, set after construction.
    app: "Optional[RepospaceApp]" = None

    def __init__(self):
        super().__init__(
            "help",
            "get help for repospace or a command",
            "Print top-level help, or help for a specific command.",
            requires_repospace=False,
        )

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, help=self.help, description=self.description)
        parser.add_argument("command", nargs="?", help="command to describe")
        return parser

    def do_run(self, args, unknown):
        app = self.app
        name = args.command
        if name is None:
            app.print_top_help()
        elif name == "help":
            self.parser.print_help()
        elif name in app.builtins:
            app.builtins[name].parser.print_help()
        elif app.extensions and name in app.extensions:
            app.run_extension(name, [name, "--help"])
        elif name in app.aliases:
            expansion = util.quote_sh_list(app.aliases[name])
            print(f'"{name}" is an alias for: {expansion}')
        else:
            self.wrn(f'unknown command "{name}"')
            app.print_top_help()
            if app.manifest_error is not None:
                self.wrn(
                    "your manifest could not be loaded, which may be why "
                    f'"{name}" is unknown; try "repospace update" or fix '
                    "the manifest"
                )


class RepospaceApp:
    """One repospace invocation."""

    def __init__(self):
        self.topdir: Optional[str] = None
        self.config: Optional[Configuration] = None
        self.manifest: Optional[Manifest] = None
        self.manifest_error: Optional[Exception] = None
        self.builtins: Dict[str, RepospaceCommand] = {}
        self.builtin_groups = {}
        # None means "could not be determined" (manifest unloadable),
        # unlike {} which means there are genuinely none.
        self.extensions: Optional[Dict[str, ExtCommandSpec]] = None
        self.extension_groups: Dict[str, List[ExtCommandSpec]] = {}
        self.aliases: Dict[str, List[str]] = {}
        self.queued_warnings: List[str] = []
        self.verbosity_delta = 0

    # -- setup -------------------------------------------------------------

    def _setup(self):
        try:
            self.topdir = util.topdir()
        except util.RepospaceNotFound:
            self.topdir = None
        self.config = Configuration(self.topdir)
        if self.topdir is not None:
            try:
                self.manifest = Manifest.from_topdir(self.topdir, config=self.config)
            except (
                MalformedManifest,
                MalformedConfig,
                ManifestVersionError,
                ManifestImportFailed,
                GitNotFound,
                OSError,
            ) as err:
                self.manifest_error = err

        self._make_builtins()
        self._load_extension_specs()
        self._load_aliases()

    def _make_builtins(self):
        self.builtin_groups = {
            "built-in commands for managing the repospace": [
                Init(),
                Update(),
                ListCommand(),
                ManifestCommand(),
                Compare(),
                Diff(),
                Status(),
                ForAll(),
                Grep(),
            ],
            "other built-in commands": [Config(), Topdir(), Help()],
        }
        for commands in self.builtin_groups.values():
            for command in commands:
                self.builtins[command.name] = command
        self.builtins["help"].app = self

    def _load_extension_specs(self):
        if self.manifest is None:
            self.extensions = None
            return
        self.extensions = {}
        try:
            groups = extension_commands(self.config, self.manifest)
        except ExtensionCommandError as err:
            self.queued_warnings.append(f"cannot load extension commands: {err.hint}")
            self.extensions = None
            return
        except MalformedConfig as err:
            # Like a broken alias: a bad commands.allow-extensions
            # value must not take down every invocation (including the
            # "config -d" that would remove it).
            self.queued_warnings.append(f"cannot load extension commands: {err}")
            self.extensions = None
            return
        for path, specs in groups.items():
            kept = []
            for spec in specs:
                if spec.name in self.builtins:
                    self.queued_warnings.append(
                        f"ignoring member {spec.member.name} extension "
                        f'command "{spec.name}"; this is a built-in command'
                    )
                elif spec.name in self.extensions:
                    other = self.extensions[spec.name]
                    self.queued_warnings.append(
                        f"ignoring member {spec.member.name} extension "
                        f'command "{spec.name}"; it is already defined by '
                        f"member {other.member.name}"
                    )
                else:
                    self.extensions[spec.name] = spec
                    kept.append(spec)
            if kept:
                self.extension_groups[path] = kept

    def _load_aliases(self):
        for option, value in self.config.items(ConfigFile.ALL):
            section, _, key = option.partition(".")
            if section != "alias":
                continue
            try:
                self.aliases[key] = shlex.split(value or "")
            except ValueError as err:
                # E.g. unbalanced quotes. A broken alias must not take
                # down every invocation (including the "config -d" that
                # would remove it).
                self.queued_warnings.append(f'ignoring alias "{key}" ({err}): {value}')

    # -- parsers -----------------------------------------------------------

    def _make_parser(self):
        parser = argparse.ArgumentParser(
            prog="repospace",
            description="The repospace multi-repository tool.",
            add_help=False,
        )
        parser.add_argument("-h", "--help", action="store_true", help="show help and exit")
        parser.add_argument(
            "-V",
            "--version",
            action="store_true",
            help="print the program version and exit",
        )
        parser.add_argument(
            "-v",
            "--verbose",
            action="count",
            default=0,
            help="print more output; may be given more than once",
        )
        parser.add_argument(
            "-q",
            "--quiet",
            action="count",
            default=0,
            help="print less output; may be given more than once",
        )
        # Global flags (including -v/-q) are recognized before the
        # command name only; everything after it belongs to the command
        # itself, so a pass-through command can hand an untouched -v to
        # its underlying tool.
        subparser_gen = parser.add_subparsers(metavar="<command>", dest="command")
        return parser, subparser_gen

    # -- output ------------------------------------------------------------

    def _flush_warnings(self):
        # Same threshold as RepospaceCommand.wrn: -qq and lower
        # silence warnings.
        verbosity = _clamp_verbosity(Verbosity.INF + self.verbosity_delta)
        if verbosity >= Verbosity.WRN:
            for warning in self.queued_warnings:
                print(f"WARNING: {warning}", file=sys.stderr)
        self.queued_warnings = []

    def print_top_help(self, file=None):
        file = file or sys.stdout
        columns = shutil.get_terminal_size().columns
        width = min(75, max(columns - 2, 30))
        indent = "    "

        def emit_command(name, help_text):
            lead = f"{indent}{name}:"
            if len(lead) < 22:
                # wrap() yields no lines for empty help text (e.g. an
                # alias set to the empty string); the entry must still
                # be listed.
                lines = textwrap.wrap(
                    help_text or "",
                    width=width,
                    initial_indent=lead.ljust(22),
                    subsequent_indent=" " * 22,
                ) or [lead]
            else:
                lines = [lead] + textwrap.wrap(
                    help_text or "",
                    width=width,
                    initial_indent=" " * 22,
                    subsequent_indent=" " * 22,
                )
            for line in lines:
                print(line, file=file)

        print(
            "usage: repospace [-h] [-V] [-v] [-q] <command> ...",
            file=file,
        )
        print("", file=file)
        print("The repospace multi-repository tool.", file=file)
        print("", file=file)
        print("optional arguments:", file=file)
        emit_command("-h, --help", "show this help message and exit")
        emit_command("-V, --version", "print the program version and exit")
        emit_command("-v, --verbose", "print more output (before <command>)")
        emit_command("-q, --quiet", "print less output (before <command>)")

        for group, commands in self.builtin_groups.items():
            print("", file=file)
            print(f"{group}:", file=file)
            for command in commands:
                emit_command(command.name, command.help)

        if self.extensions is None and self.topdir is not None:
            print("", file=file)
            print(
                "Cannot load extension commands; help for them is not " "available.",
                file=file,
            )
            print(
                '(To debug, try: "repospace manifest --validate".)',
                file=file,
            )
        else:
            for path, specs in self.extension_groups.items():
                member = specs[0].member
                print("", file=file)
                print(
                    f"extension commands from member {member.name} " f"(path: {path}):",
                    file=file,
                )
                for spec in specs:
                    emit_command(spec.name, spec.help)

        if self.aliases:
            print("", file=file)
            print("aliases:", file=file)
            for name, words in self.aliases.items():
                emit_command(name, util.quote_sh_list(words))

        print("", file=file)
        print(
            'Run "repospace help <command>" for help on each <command>.',
            file=file,
        )

    # -- dispatch ----------------------------------------------------------

    def run(self, argv: List[str]) -> None:
        early = parse_early_args(argv)
        if early.version:
            print(f"repospace version {__version__}")
            return
        if early.unexpected:
            print(
                f"repospace: unexpected argument {early.unexpected}",
                file=sys.stderr,
            )
            raise SystemExit(2)

        # Before _setup(): manifest loading already logs.
        _configure_logging(_clamp_verbosity(Verbosity.INF + early.verbosity_delta))
        self._setup()
        # Global flags from the original command line only; alias
        # expansion below may add more, but the queued warnings (a
        # broken alias among them) come out before expansion.
        self.verbosity_delta = early.verbosity_delta
        self._flush_warnings()

        command = early.command_name

        # Alias expansion. Aliases never shadow real commands.
        expanded = set()
        while (
            command is not None
            and command in self.aliases
            and command not in self.builtins
            and not (self.extensions and command in self.extensions)
        ):
            if command in expanded:
                self._die(f'circular alias "{command}"')
            expanded.add(command)
            words = self.aliases[command]
            if not words:
                self._die(f'empty alias "{command}"')
            index = argv.index(command)
            argv = argv[:index] + words + argv[index + 1 :]
            early = parse_early_args(argv)
            command = early.command_name

        # Alias expansion may have introduced global flags; the checks
        # at the top of this method ran before expansion.
        if early.version:
            print(f"repospace version {__version__}")
            return
        if early.unexpected:
            print(
                f"repospace: unexpected argument {early.unexpected}",
                file=sys.stderr,
            )
            raise SystemExit(2)

        # After alias expansion so global flags inside an alias count.
        self.verbosity_delta = early.verbosity_delta
        _configure_logging(_clamp_verbosity(Verbosity.INF + self.verbosity_delta))

        if command is None:
            self.print_top_help(file=sys.stdout if early.help else sys.stderr)
            raise SystemExit(0 if early.help else 2)

        if early.help and command:
            # "repospace -h <command>" behaves like "repospace help".
            self.print_top_help()
            return

        if command in self.builtins:
            self.run_builtin(command, argv)
        elif self.extensions and command in self.extensions:
            self.run_extension(command, argv)
        else:
            self._unknown_command(command)

    def _die(self, message: str):
        print(f"repospace: {message}", file=sys.stderr)
        raise SystemExit(1)

    def _unknown_command(self, command: str):
        if self.topdir is not None:
            if self.manifest_error is not None:
                hint = (
                    "the manifest could not be loaded, so extension "
                    "commands are unavailable; try "
                    '"repospace manifest --validate"'
                )
            else:
                hint = f"repospace {self.topdir} does not define this " 'extension command; try "repospace help"'
        else:
            hint = "do you need to run this inside a repospace?"
        print(
            f'repospace: unknown command "{command}"; {hint}',
            file=sys.stderr,
        )
        raise SystemExit(2)

    def _adjust_verbosity(self, command: RepospaceCommand):
        # Global -v/-q flags apply before the command name only; after
        # it, arguments belong to the command (or, for pass-through
        # commands, to the underlying tool).
        command.verbosity = _clamp_verbosity(command.verbosity + self.verbosity_delta)

    def _check_manifest_available(self, command: RepospaceCommand):
        if command.name not in NO_MANIFEST_OK and self.manifest is None and self.topdir is not None:
            reason = str(self.manifest_error) if self.manifest_error is not None else "the manifest could not be loaded"
            print(
                f"FATAL ERROR: can't run repospace {command.name}: {reason}",
                file=sys.stderr,
            )
            raise SystemExit(1)

    @staticmethod
    def _split_forwarded(command: RepospaceCommand, argv: List[str]):
        """Split argv at the first "--" for forward_dashdash commands.

        Everything after the first "--" is forwarded untouched; argparse
        would otherwise assign it to positionals (such as MEMBER). A
        later "--" is forwarded too, so the underlying tool's own
        separator stays reachable. parse_early_args rejects a "--"
        before the command name, so the split never touches global
        arguments.
        """
        if not command.forward_dashdash or "--" not in argv:
            return argv, []
        index = argv.index("--")
        return argv[:index], argv[index + 1 :]

    def run_builtin(self, name: str, argv: List[str]):
        parser, subparser_gen = self._make_parser()
        for group in self.builtin_groups.values():
            for command in group:
                command.add_parser(subparser_gen)
        command = self.builtins[name]
        argv, forwarded = self._split_forwarded(command, argv)
        # Parsed before the manifest is required: argparse services
        # "<command> -h" while parsing, and help must stay available in
        # a repospace whose manifest is broken -- reading it is how the
        # user finds the way out.
        args, unknown = parser.parse_known_args(argv)
        self._check_manifest_available(command)
        self._adjust_verbosity(command)
        command.run(
            args,
            unknown + forwarded,
            self.topdir,
            manifest=self.manifest,
            config=self.config,
        )

    def run_extension(self, name: str, argv: List[str]):
        spec = self.extensions[name]
        try:
            command = spec.factory()
        except ExtensionCommandError as err:
            print(
                f"FATAL ERROR: extension command {name} could not be " f"created: {err.hint}",
                file=sys.stderr,
            )
            raise SystemExit(err.returncode)
        parser, subparser_gen = self._make_parser()
        command.add_parser(subparser_gen)
        argv, forwarded = self._split_forwarded(command, argv)
        args, unknown = parser.parse_known_args(argv)
        self._adjust_verbosity(command)
        command.run(
            args,
            unknown + forwarded,
            self.topdir,
            manifest=self.manifest,
            config=self.config,
        )


def main(argv: Optional[List[str]] = None) -> None:
    app = RepospaceApp()
    try:
        app.run(argv if argv is not None else sys.argv[1:])
    except KeyboardInterrupt:
        raise SystemExit(130)
    except BrokenPipeError:
        raise SystemExit(0)
    except CommandError as err:
        raise SystemExit(err.returncode)
    except (GitNotFound, MalformedConfig) as err:
        print(f"FATAL ERROR: {err}", file=sys.stderr)
        raise SystemExit(1)
    except subprocess.CalledProcessError as err:
        # A negative returncode means the child died on a signal; exit
        # with the conventional 128 + signal instead, since a negative
        # status would be truncated modulo 256 (-9 would become 247,
        # not SIGKILL's 137).
        if err.returncode < 0:
            detail = f"command died on signal {-err.returncode}"
        else:
            detail = f"command exited with status {err.returncode}"
        print(
            f"FATAL ERROR: {detail}: {util.quote_sh_list(err.cmd)}",
            file=sys.stderr,
        )
        raise SystemExit(err.returncode if err.returncode > 0 else 128 - err.returncode)


if __name__ == "__main__":
    main()
