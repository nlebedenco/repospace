"""The config command."""

from __future__ import annotations

import sys

from repospace.commands import CommandError, RepospaceCommand
from repospace.configuration import ConfigFile, MalformedConfig, parse_key

_DESCRIPTION = """\
Get and set repospace configuration options, named "section.key".

Reads consult all three configuration files (system, global, local) with local winning; writes
default to the local file. Use --system, --global, or --local to address one file explicitly.
"""


class Config(RepospaceCommand):
    def __init__(self):
        super().__init__(
            "config",
            "get or set configuration options",
            _DESCRIPTION,
            requires_repospace=False,
        )

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, help=self.help, description=self.description)
        parser.add_argument("-l", "--list", action="store_true", help="list all options")
        parser.add_argument(
            "-d",
            "--delete",
            action="store_true",
            help="delete an option from the highest-precedence file where it is set",
        )
        parser.add_argument(
            "-D",
            "--delete-all",
            action="store_true",
            help="delete an option everywhere it is set",
        )
        parser.add_argument(
            "-a",
            "--append",
            action="store_true",
            help="append to an existing option value",
        )
        group = parser.add_mutually_exclusive_group()
        group.add_argument(
            "--system",
            dest="configfile",
            action="store_const",
            const=ConfigFile.SYSTEM,
            help="use the system configuration file",
        )
        group.add_argument(
            "--global",
            dest="configfile",
            action="store_const",
            const=ConfigFile.GLOBAL,
            help="use the global configuration file",
        )
        group.add_argument(
            "--local",
            dest="configfile",
            action="store_const",
            const=ConfigFile.LOCAL,
            help="use the repospace-local configuration file",
        )
        parser.add_argument("name", nargs="?", help="option name (section.key)")
        parser.add_argument("value", nargs="?", help="value to set")
        return parser

    def do_run(self, args, unknown):
        config = self.config
        if args.list:
            if args.delete or args.delete_all:
                self.parser.error("-l cannot be combined with -d/-D")
            if args.append:
                self.parser.error("-l cannot be combined with -a")
            if args.name is not None:
                self.parser.error("-l cannot be combined with a name")
            for option, value in config.items(args.configfile or ConfigFile.ALL):
                print(f"{option}={value}")
            return

        if args.name is None:
            self.parser.error("missing option name; see repospace config -h")
        try:
            parse_key(args.name)
        except ValueError as err:
            self.die(str(err))

        if args.delete or args.delete_all:
            if args.delete and args.delete_all:
                # The two name different scopes; -D must not quietly widen the deletion the -d asked
                # for.
                self.parser.error("-d cannot be combined with -D")
            if args.value is not None:
                self.parser.error("cannot combine a value with -d/-D")
            if args.append:
                self.parser.error("-a cannot be combined with -d/-D")
            configfile = args.configfile or (ConfigFile.ALL if args.delete_all else None)
            try:
                config.delete(args.name, configfile=configfile)
            except KeyError:
                print(f"{args.name} is unset", file=sys.stderr)
                raise CommandError(1)
            except MalformedConfig as err:
                self.die(str(err))
            return

        if args.value is None:
            if args.append:
                self.parser.error("-a requires a value")
            value = config.get(args.name, configfile=args.configfile or ConfigFile.ALL)
            if value is None:
                print(f"{args.name} is unset", file=sys.stderr)
                raise CommandError(1)
            print(value)
            return

        configfile = args.configfile or ConfigFile.LOCAL
        value = args.value
        if args.append:
            existing = config.get(args.name, configfile=configfile)
            if existing is None:
                self.die(f"-a: {args.name} is not set in the selected configuration file")
            value = existing + value
        try:
            config.set(args.name, value, configfile=configfile)
        except MalformedConfig as err:
            self.die(str(err))
