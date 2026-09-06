"""Command framework: the RepospaceCommand base class and extension loading.

Extension authors subclass RepospaceCommand and describe their commands in a YAML specification file
(conventionally ``repospace-commands.yaml``)::

    extension-commands:
      - file: scripts/my_extension.py
        commands:
          - name: my-command
            class: MyCommand      # optional, defaults to the name
            help: one-line help   # optional

The path in the manifest's ``extension-commands`` attribute and the ``file:`` keys inside the
specification are both relative to the member's root directory. Extension modules are imported
lazily, only when the command is actually invoked, and the command class must be constructible with
no arguments.
"""

from __future__ import annotations

import argparse
import enum
import importlib.util
import itertools
import os
import re
import subprocess
import sys
import textwrap
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

from repospace import ansi, util
from repospace.configuration import MalformedConfig

__all__ = [
    "CommandContextError",
    "CommandError",
    "HelpFormatter",
    "RepospaceCommand",
    "Verbosity",
]


class CommandError(RuntimeError):
    """An error occurred running a command; carries the exit code."""

    def __init__(self, returncode: int = 1):
        super().__init__()
        self.returncode = returncode


class CommandContextError(CommandError):
    """A command was run in an invalid context."""


class ExtensionCommandError(CommandError):
    """An extension command could not be loaded or created."""

    def __init__(self, hint: str = "", returncode: int = 1):
        super().__init__(returncode)
        self.hint = hint


class Verbosity(enum.IntEnum):
    QUIET = 0
    ERR = 1
    WRN = 2
    INF = 3
    DBG = 4
    DBG_MORE = 5
    DBG_EXTREME = 6


class HelpFormatter(argparse.HelpFormatter):
    """Fill hand-written help text to the terminal width.

    The default formatter collapses the whole description into a single paragraph and
    RawDescriptionHelpFormatter keeps the source's short lines; neither fits a multi-paragraph
    description. This one re-wraps to the terminal width while preserving paragraph breaks and a
    hand-written argument list (as in "repospace init").
    """

    #: An argument-list entry: a two-space indented name, two or more spaces, then the first line of
    #: its help text.
    _ENTRY = re.compile(r"^  (\S.*?)\s{2,}(\S.*)$")
    #: Column where an entry's help text starts.
    _HELP_COLUMN = 20

    def _format_usage(self, usage, actions, groups, prefix):
        if usage is None:
            return super()._format_usage(usage, actions, groups, prefix)
        # argparse emits a caller-supplied usage string verbatim; wrap it on option boundaries like
        # a generated one.
        if prefix is None:
            prefix = "usage: "
        width = max(self._width - self._current_indent, 11)
        indent = " " * (len(prefix) + len(self._prog) + 1)
        lines = [f"{prefix}{self._prog}"]
        for part in re.findall(r"\[[^]]*\]|\S+", usage % {"prog": ""}):
            if len(lines[-1]) + 1 + len(part) > width:
                lines.append(indent + part)
            else:
                lines[-1] += f" {part}"
        return "\n".join(lines) + "\n\n"

    def _fill_text(self, text, width, indent):
        out = []
        prose = []
        entry = None

        def close_prose():
            if prose:
                if out:
                    out.append("")
                out.extend(
                    textwrap.wrap(
                        " ".join(prose),
                        width,
                        initial_indent=indent,
                        subsequent_indent=indent,
                    )
                )
                del prose[:]

        def close_entry():
            nonlocal entry
            if entry is None:
                return
            name, words = entry
            entry = None
            lead = f"{indent}  {name}"
            column = len(indent) + self._HELP_COLUMN
            body = textwrap.wrap(" ".join(words), max(width - column, 11))
            if body and len(lead) + 2 <= column:
                out.append(lead.ljust(column) + body.pop(0))
            else:
                out.append(lead)
            out.extend(" " * column + line for line in body)

        for line in text.splitlines():
            match = self._ENTRY.match(line)
            if not line.strip():
                close_prose()
                close_entry()
            elif match:
                close_prose()
                close_entry()
                entry = (match.group(1), [match.group(2)])
            elif line.startswith(" ") and entry is not None:
                entry[1].append(line.strip())
            else:
                close_entry()
                prose.append(line.strip())
        close_prose()
        close_entry()
        return "\n".join(out)


class RepospaceCommand(ABC):
    """Abstract base class for repospace commands, built-in or extension."""

    def __init__(
        self,
        name: str,
        help: str,
        description: str,
        accepts_unknown_args: bool = False,
        requires_repospace: bool = True,
        verbosity: Verbosity = Verbosity.INF,
        forward_dashdash: bool = False,
    ):
        self.name = name
        self.help = help
        self.description = description
        self.accepts_unknown_args = accepts_unknown_args
        # With forward_dashdash, the dispatcher forwards everything after the first "--" as unknown
        # arguments instead of letting argparse assign it to positionals (such as MEMBER); a later
        # "--" is forwarded too, so the underlying tool's own separator stays reachable.
        self.forward_dashdash = forward_dashdash
        self.requires_repospace = requires_repospace
        self.verbosity = verbosity
        self.topdir: Optional[str] = None
        self.parser = None
        self._manifest = None
        self._config = None
        self._color_ui_broken = False
        self._pre_run_hooks: List[Callable[["RepospaceCommand"], None]] = []

    # -- framework entry points -------------------------------------------

    def add_parser(self, parser_adder):
        """Register this command's argument parser and return it."""
        parser = self.do_add_parser(parser_adder)
        if parser is None:
            raise ValueError(f"{self.name}: do_add_parser did not return a parser")
        self.parser = parser
        return parser

    def run(self, args, unknown, topdir, manifest=None, config=None) -> None:
        """Run the command; called by the application dispatcher."""
        if config is not None:
            self._config = config
        if unknown and not self.accepts_unknown_args:
            self.parser.error(f"unexpected arguments: {unknown}")
        if topdir is None and self.requires_repospace:
            self.die(
                f'"{self.name}" must be run from a repospace, but '
                "no .repospace directory was found in this or any parent "
                'directory; try "repospace init -h"'
            )
        self.topdir = os.fspath(topdir) if topdir is not None else None
        if manifest is not None:
            self._manifest = manifest
        for hook in self._pre_run_hooks:
            hook(self)
        self.do_run(args, unknown)

    def add_pre_run_hook(self, hook) -> None:
        """Register a callable invoked with the command just before do_run."""
        self._pre_run_hooks.append(hook)

    @abstractmethod
    def do_add_parser(self, parser_adder):
        """Add and return this command's argparse subparser."""

    @abstractmethod
    def do_run(self, args, unknown):
        """Run the command with parsed *args* and *unknown* leftovers."""

    # -- context ----------------------------------------------------------

    @property
    def manifest(self):
        if self._manifest is None:
            self.die(
                f"can't run repospace {self.name}; it requires the manifest, "
                "which was not available. "
                "Try 'repospace manifest --validate' to debug."
            )
        return self._manifest

    @manifest.setter
    def manifest(self, value):
        self._manifest = value

    @property
    def has_manifest(self) -> bool:
        return self._manifest is not None

    @property
    def config(self):
        if self._config is None:
            self.die(
                f"can't run repospace {self.name}; it requires configuration "
                "options, which were not available."
            )
        return self._config

    @config.setter
    def config(self, value):
        self._config = value

    @property
    def has_config(self) -> bool:
        return self._config is not None

    @property
    def color_ui(self) -> bool:
        if not self.has_config or self._color_ui_broken:
            return True
        try:
            return self._config.getboolean("color.ui", default=True)
        except MalformedConfig as err:
            # Like a broken commands.allow-extensions value: a value getboolean rejects must not
            # take down every invocation (including the "config -d" that would remove it). Every
            # output helper reads this property, so raising here would replace genuine errors with
            # this one.
            #
            # The flag is set before warning, and checked above: wrn() colorizes, which reads this
            # property again.
            self._color_ui_broken = True
            self.wrn(f"{err}; assuming the default")
            return True

    # -- output helpers ----------------------------------------------------

    def _colorize(self, text: str, color: str, stream) -> str:
        if ansi.use_color(stream, self.color_ui):
            return f"{color}{text}{ansi.RESET}"
        return text

    def dbg(self, *args, level: Verbosity = Verbosity.DBG, end="\n"):
        if self.verbosity >= level:
            # Flushed like the other helpers: _log_subprocess prints through here right before
            # spawning a child, and the line must precede the child's output when stdout is a pipe.
            print(*args, end=end, flush=True)

    def inf(self, *args, colorize: bool = False, end="\n"):
        if self.verbosity < Verbosity.INF:
            return
        text = " ".join(str(a) for a in args)
        if colorize:
            text = self._colorize(text, ansi.GREEN, sys.stdout)
        # Flushed like the other helpers, so output stays ordered with child-process output when
        # stdout is a pipe.
        print(text, end=end, flush=True)

    def wrn(self, *args, end="\n"):
        if self.verbosity < Verbosity.WRN:
            return
        text = "WARNING: " + " ".join(str(a) for a in args)
        print(
            self._colorize(text, ansi.YELLOW, sys.stderr),
            end=end,
            file=sys.stderr,
            flush=True,
        )

    def err(self, *args, fatal: bool = False, end="\n"):
        if self.verbosity < Verbosity.ERR:
            return
        prefix = "FATAL ERROR: " if fatal else "ERROR: "
        text = prefix + " ".join(str(a) for a in args)
        print(
            self._colorize(text, ansi.RED, sys.stderr),
            end=end,
            file=sys.stderr,
            flush=True,
        )

    def die(self, *args, exit_code: int = 1):
        self.err(*args, fatal=True)
        raise SystemExit(exit_code)

    def banner(self, *args):
        # Flushed so banners stay ordered with child-process output when stdout is a pipe.
        if self.verbosity < Verbosity.INF:
            return
        text = "=== " + " ".join(str(a) for a in args)
        print(
            self._colorize(text, ansi.BOLD + ansi.GREEN, sys.stdout),
            flush=True,
        )

    def small_banner(self, *args):
        if self.verbosity < Verbosity.INF:
            return
        print("--- " + " ".join(str(a) for a in args), flush=True)

    # -- subprocess helpers ------------------------------------------------

    def _log_subprocess(self, args, cwd):
        self.dbg(
            f"running: {util.quote_sh_list(args)}" + (f" (in {cwd})" if cwd else ""),
            level=Verbosity.DBG_MORE,
        )

    def check_call(self, args, **kwargs):
        self._log_subprocess(args, kwargs.get("cwd"))
        subprocess.check_call(args, **kwargs)

    def check_output(self, args, **kwargs):
        self._log_subprocess(args, kwargs.get("cwd"))
        return subprocess.check_output(args, **kwargs)

    def run_subprocess(self, args, **kwargs):
        # No defaults injected: setting e.g. "errors" here would silently switch subprocess.run into
        # text mode for every caller.
        self._log_subprocess(args, kwargs.get("cwd"))
        return subprocess.run(args, **kwargs)

    def die_if_no_git(self):
        from repospace.git import GitNotFound, git_executable

        try:
            git_executable()
        except GitNotFound as err:
            self.die(str(err))


# ---------------------------------------------------------------------------
# Extension command discovery and lazy loading

_EXT_SCHEMA_ERROR = "invalid extension-commands specification"

_module_names = (f"repospace.commands.ext.cmd_{i}" for i in itertools.count(1))

# Cache of already-imported extension modules, keyed by resolved path so a file imported through
# different spellings (case, slashes, symlinks) is imported only once even if it keeps module-level
# state.
_EXT_MODULES_CACHE: Dict[Path, Any] = {}


@dataclass
class _ExtFactory:
    py_file: str
    name: str
    attr: str

    def __call__(self):
        module = self._import()
        try:
            cls = getattr(module, self.attr)
        except AttributeError:
            raise ExtensionCommandError(hint=f"no attribute {self.attr} in {self.py_file}")
        try:
            command = cls()
        except KeyboardInterrupt:
            raise
        except (Exception, SystemExit) as err:
            # SystemExit too: a constructor calling sys.exit() would otherwise end repospace with
            # the extension's own code and no diagnostic at all.
            raise ExtensionCommandError(hint=f"command constructor threw an exception: {err}")
        if not isinstance(command, RepospaceCommand):
            raise ExtensionCommandError(
                hint=f"{self.attr} in {self.py_file} is not a RepospaceCommand"
            )
        if command.name != self.name:
            # Dispatch found the command under the specification's name; a class registering another
            # name would only fail later with a bare argparse "invalid choice" error.
            raise ExtensionCommandError(
                hint=f"{self.attr} in {self.py_file} names itself "
                f'"{command.name}", but the specification declares '
                f'"{self.name}"'
            )
        return command

    def _import(self):
        """Import the extension module, or raise ExtensionCommandError.

        The module's own directory joins sys.path so it can import sibling helper modules naturally.
        It is appended rather than prepended, so an extension directory cannot shadow the standard
        library, and it stays only if the import succeeded. Sibling modules do share one global
        namespace: when two extensions each ship a "helper.py", both see whichever was imported
        first, which no sys.path ordering can change.
        """
        parent = os.path.dirname(self.py_file)
        added = parent not in sys.path
        if added:
            sys.path.append(parent)
        loaded = False
        try:
            module = _module_from_file(self.py_file)
            loaded = True
            return module
        except KeyboardInterrupt:
            # The user's interrupt, not the extension's failure.
            raise
        except (Exception, SystemExit) as err:
            # Extension modules are arbitrary user code; any failure at import time becomes a clean
            # error, not a traceback. _module_from_file re-raises BaseException, so SystemExit
            # arrives here too: without this, an extension calling sys.exit() at import time would
            # end repospace with the extension's own code and nothing said about why.
            detail = (
                f"it called sys.exit({err.code!r}) at import time"
                if isinstance(err, SystemExit)
                else str(err)
            )
            raise ExtensionCommandError(hint=f"could not import {self.py_file}: {detail}")
        finally:
            # A failed import leaves nothing behind on sys.path. (The module's own code may have
            # edited it; only remove what is still there.)
            if added and not loaded and parent in sys.path:
                sys.path.remove(parent)


def _module_from_file(file: str):
    key = Path(file).resolve()
    cached = _EXT_MODULES_CACHE.get(key)
    if cached is not None:
        return cached
    name = next(_module_names)
    spec = importlib.util.spec_from_file_location(name, file)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {file}")
    module = importlib.util.module_from_spec(spec)
    # Registered before exec_module, as importlib documents: the module must be findable by name
    # while its own top-level code runs (dataclasses with deferred annotations, get_type_hints, and
    # pickle all look it up in sys.modules).
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    _EXT_MODULES_CACHE[key] = module
    return module


@dataclass
class ExtCommandSpec:
    """An extension command declared by a member, not yet loaded."""

    name: str
    member: Any
    help: str
    factory: _ExtFactory


def extension_commands(config, manifest) -> "Dict[str, List[ExtCommandSpec]]":
    """Discover extension commands from all members, without loading them.

    Returns an ordered mapping from member path to that member's specs, in manifest resolution
    order. Nothing is imported here; each spec's factory imports and instantiates its command class
    on first call.
    """
    result: Dict[str, List[ExtCommandSpec]] = {}
    if config is not None and not config.getboolean("commands.allow-extensions", default=True):
        return result
    for member in manifest.members:
        if not member.extension_commands:
            continue
        specs = _member_specs(member)
        if specs:
            result[member.path or member.name] = specs
    return result


def _member_specs(member) -> List[ExtCommandSpec]:
    root = member.abspath
    if root is None:
        return []
    specs: List[ExtCommandSpec] = []
    for spec_rel in member.extension_commands:
        spec_file = os.path.join(root, spec_rel)
        if util.escapes_directory(spec_file, root):
            raise ExtensionCommandError(
                hint=f"{member.name}: extension-commands path {spec_rel} "
                "escapes the member directory"
            )
        if not os.path.isfile(spec_file):
            # The member may not be cloned yet; ignore silently.
            continue
        specs.extend(_specs_from_file(member, root, spec_file))
    return specs


def _specs_from_file(member, root: str, spec_file: str):
    try:
        with open(spec_file, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except OSError as err:
        raise ExtensionCommandError(hint=f"{spec_file}: cannot read: {err}")
    except UnicodeDecodeError as err:
        # One member's undecodable file must not become a traceback out of every invocation, down to
        # "repospace topdir".
        raise ExtensionCommandError(hint=f"{spec_file}: not valid UTF-8: {err}")
    except yaml.YAMLError as err:
        raise ExtensionCommandError(hint=f"{spec_file}: cannot parse YAML: {err}")
    if not isinstance(data, dict) or not isinstance(data.get("extension-commands"), list):
        raise ExtensionCommandError(
            hint=f'{spec_file}: {_EXT_SCHEMA_ERROR}: expected an "extension-commands" list'
        )
    specs = []
    for entry in data["extension-commands"]:
        ok = (
            isinstance(entry, dict)
            and isinstance(entry.get("file"), str)
            and isinstance(entry.get("commands"), list)
        )
        if not ok:
            raise ExtensionCommandError(
                hint=f'{spec_file}: {_EXT_SCHEMA_ERROR}: each entry needs "file" and "commands"'
            )
        py_file = os.path.join(root, entry["file"])
        if util.escapes_directory(py_file, root):
            raise ExtensionCommandError(
                hint=f'{spec_file}: file {entry["file"]} escapes the member directory'
            )
        for command in entry["commands"]:
            if (
                not isinstance(command, dict)
                or not isinstance(command.get("name"), str)
                or not command["name"]
            ):
                raise ExtensionCommandError(
                    hint=f"{spec_file}: {_EXT_SCHEMA_ERROR}: each command "
                    'needs a non-empty string "name"'
                )
            name = command["name"]
            attr = command.get("class", name)
            if not isinstance(attr, str) or not attr:
                raise ExtensionCommandError(
                    hint=f"{spec_file}: {_EXT_SCHEMA_ERROR}: command "
                    f'"{name}": "class" is not a non-empty string'
                )
            help_text = command.get(
                "help",
                f'(no help provided; try "repospace {name} -h")',
            )
            if not isinstance(help_text, str):
                raise ExtensionCommandError(
                    hint=f"{spec_file}: {_EXT_SCHEMA_ERROR}: command "
                    f'"{name}": "help" is not a string'
                )
            specs.append(
                ExtCommandSpec(
                    name=name,
                    member=member,
                    help=help_text,
                    factory=_ExtFactory(py_file, name, attr),
                )
            )
    return specs
