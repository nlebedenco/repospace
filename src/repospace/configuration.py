"""Configuration file handling.

repospace uses Git-style configuration at three levels:

  SYSTEM:  /etc/repospace-config (POSIX) or %PROGRAMDATA%\\repospace\\config
           (Windows); override with REPOSPACE_CONFIG_SYSTEM.
  GLOBAL:  ~/.repospace-config if it exists, otherwise
           $XDG_CONFIG_HOME/repospace/config (XDG_CONFIG_HOME defaults to
           ~/.config); override with REPOSPACE_CONFIG_GLOBAL.
  LOCAL:   <topdir>/.repospace/config; override with REPOSPACE_CONFIG_LOCAL.

Files are INI format. Options are named "section.key"; the name is split on
the first dot only. Sections may contain letters, digits, "-" and "_";
keys additionally allow ".". Like git config, section and key names are
case-insensitive and normalized to lowercase. The INI "DEFAULT" section
name is reserved (in any case): configparser would copy its keys into
every section. A key present without a value reads as the empty string.
Precedence is LOCAL > GLOBAL > SYSTEM.

Writes take effect immediately with no concurrency protection; callers are
responsible for mutual exclusion. Writes edit the file textually, touching
only the affected option's lines, so comments and layout are preserved.
"""

from __future__ import annotations

import configparser
import enum
import os
import pathlib
import platform
import re
import tempfile
from typing import Any, Iterable, Optional, Tuple

from repospace import util

_BOOLEAN_STATES = {
    "1": True,
    "yes": True,
    "true": True,
    "on": True,
    "0": False,
    "no": False,
    "false": False,
    "off": False,
}


class MalformedConfig(RuntimeError):
    """Raised on invalid configuration contents or state."""


class ConfigFile(enum.Enum):
    """Which configuration file(s) an operation applies to."""

    ALL = 1
    SYSTEM = 2
    GLOBAL = 3
    LOCAL = 4


# Section and key names are written verbatim into INI files; characters
# with INI syntax meaning ("=", ":", "#", ";", "[", whitespace, newlines)
# would be reinterpreted on the next read as a different option, a
# comment, or an injected line — possibly leaving the file unparseable,
# which breaks every later invocation. Restrict names to a safe set.
_SECTION_RE = re.compile(r"[A-Za-z0-9_-]+\Z")
_KEY_RE = re.compile(r"[A-Za-z0-9_.-]+\Z")


def _check_value(option: str, value: str) -> None:
    """Reject a value the file format cannot carry back unchanged.

    Values are written verbatim, so two constructs would be read back
    as something else entirely and are refused before anything is
    written, like the name restrictions above:

      - a carriage return, which universal-newline reading turns into a
        line break, truncating the value and injecting the rest of it
        as separate options;
      - a continuation line whose first non-blank character is "#" or
        ";", which configparser reads as a comment and drops.
    """
    if "\r" in value:
        raise MalformedConfig(
            f'cannot set "{option}": the value contains a carriage ' "return, which would be read back as a line break"
        )
    for line in value.split("\n")[1:]:
        stripped = line.strip()
        if stripped[:1] in ("#", ";"):
            raise MalformedConfig(
                f'cannot set "{option}": the value has a line starting '
                f'with "{stripped[0]}", which would be read back as a '
                "comment and dropped"
            )


def parse_key(option: str) -> Tuple[str, str]:
    """Split "section.key" on the first dot only.

    Like git config, section and key names are case-insensitive; both
    are returned lowercased. (configparser lowercases keys on its own,
    but leaves section names case-sensitive; without normalization,
    "Alias.up" would address a section no read ever consults.)
    """
    section, sep, key = option.partition(".")
    if not sep or not section or not key:
        raise ValueError(f'invalid configuration option "{option}"; expected "section.key" format')
    if not _SECTION_RE.match(section) or not _KEY_RE.match(key):
        raise ValueError(
            f'invalid configuration option "{option}"; the section may '
            'only contain letters, digits, "-" and "_", and the key '
            'additionally "."'
        )
    return section.lower(), key.lower()


def _system_path() -> str:
    env = os.environ.get("REPOSPACE_CONFIG_SYSTEM")
    if env:
        return env
    if platform.system() == "Windows":
        program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        return str(pathlib.PureWindowsPath(program_data) / "repospace" / "config")
    return "/etc/repospace-config"


def _global_path() -> str:
    env = os.environ.get("REPOSPACE_CONFIG_GLOBAL")
    if env:
        return env
    home_file = pathlib.Path.home() / ".repospace-config"
    if home_file.is_file():
        return str(home_file)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = pathlib.Path(xdg) if xdg else pathlib.Path.home() / ".config"
    return str(base / "repospace" / "config")


def _local_path(topdir: Optional[str]) -> Optional[str]:
    env = os.environ.get("REPOSPACE_CONFIG_LOCAL")
    if env:
        return env
    if topdir is None:
        return None
    return os.path.join(topdir, util.REPOSPACE_DIR, "config")


# Matches a section header on an already-stripped line. Like
# configparser's SECTCRE, the name is everything up to the *last* "]",
# so a hand-written "[a]x]" names the section "a]x" here as well; a
# non-greedy match would edit options the reader attributes to another
# section, writing values no read can find.
_HEADER_RE = re.compile(r"\[(.+)\]")


def _option_key(line: str) -> Optional[str]:
    """Return the lowercased key if *line* starts an option, else None."""
    if line[:1] in (" ", "\t") or not line.strip():
        return None
    stripped = line.strip()
    if stripped[0] in "#;[":
        return None
    for index, char in enumerate(line):
        if char in "=:":
            return line[:index].strip().lower()
    # allow_no_value: a key may appear without a delimiter.
    return stripped.lower()


def _value_end(lines, start: int) -> int:
    """Index one past the value block of the option starting at *start*.

    Continuation lines are indented; a blank line belongs to the value
    only when further indented content follows (configparser's
    empty_lines_in_values default). Leaving such lines behind would
    attach them to whatever replaces the option.
    """
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line[:1] in (" ", "\t") and line.strip():
            end += 1
            continue
        if not line.strip():
            look = end + 1
            while look < len(lines) and not lines[look].strip():
                look += 1
            if look < len(lines) and lines[look][:1] in (" ", "\t") and lines[look].strip():
                end = look + 1
                continue
        break
    return end


def _edited(text: str, section: str, key: str, value: Optional[str]) -> str:
    """Return *text* with section.key set to *value* (deleted if None).

    The edit is textual and touches only the option's own lines, so
    comments, blank lines, and the rest of the layout survive —
    ConfigParser.write() would re-serialize from parsed data and drop
    every comment in the file. Section and key matching is
    case-insensitive, mirroring how reads merge the file.
    """
    # Split on "\n" only: str.splitlines() also breaks on "\x0b",
    # "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028" and "\u2029",
    # and rejoining with "\n" would promote every one of them to a real
    # line break — corrupting values this edit never touched. Reading
    # translates "\r\n" and "\r" to "\n" already, so "\n" is the only
    # boundary left in *text*.
    lines = text.split("\n")
    if lines and lines[-1] == "":
        # A trailing newline ends the last line rather than starting an
        # empty one; joined() puts it back.
        lines.pop()

    def matching_spans():
        """(header index, end index) of sections named *section*."""
        headers = []
        for index, line in enumerate(lines):
            if line[:1] in (" ", "\t"):
                # An indented "[...]" line is a value continuation.
                continue
            stripped = line.strip()
            if stripped.startswith("["):
                match = _HEADER_RE.match(stripped)
                if match:
                    headers.append((index, match.group(1).lower()))
        spans = []
        for pos, (index, name) in enumerate(headers):
            if name == section:
                stop = headers[pos + 1][0] if pos + 1 < len(headers) else len(lines)
                spans.append((index, stop))
        return spans

    def joined():
        return "\n".join(lines) + "\n" if lines else ""

    spans = matching_spans()
    # The option's occurrences, as (start, end) line ranges. Sections
    # differing only in case merge on read with the later definition
    # winning, so the same key may occur more than once.
    found = []
    for start, stop in spans:
        index = start + 1
        while index < stop:
            if _option_key(lines[index]) == key:
                found.append((index, min(_value_end(lines, index), stop)))
                index = found[-1][1]
            else:
                index += 1

    if value is None:
        # Delete every occurrence: removing only the last would
        # resurrect an earlier, shadowed one.
        for start, end in reversed(found):
            del lines[start:end]
        # Drop matching sections whose bodies are now all blank; a body
        # that still holds comments keeps its header, so the comments
        # keep their context.
        for start, stop in reversed(matching_spans()):
            if all(not lines[i].strip() for i in range(start + 1, stop)):
                del lines[start:stop]
        return joined()

    # Same formatting as ConfigParser.write(), so the value reads back
    # identically.
    new = f"{key} = {value}".replace("\n", "\n\t").split("\n")
    if found:
        # Replace the last occurrence: on read, later definitions win.
        start, end = found[-1]
        lines[start:end] = new
    elif spans:
        # Append inside the section, after its last non-blank line, so
        # a blank separator before the next section stays where it is.
        start, stop = spans[-1]
        insert = start + 1
        for index in range(start + 1, stop):
            if lines[index].strip():
                insert = index + 1
        lines[insert:insert] = new
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"[{section}]")
        lines.extend(new)
    return joined()


def _replace_file(path: str, text: str) -> None:
    """Give *path* the contents *text* in a single step.

    Written to a temporary file in the same directory and renamed over
    the original, so an interrupted or failing write cannot leave a
    truncated (or empty) configuration file behind.
    """
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(path) or ".",
        prefix=os.path.basename(path) + ".",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        try:
            mode = os.stat(path).st_mode & 0o777
        except FileNotFoundError:
            # mkstemp creates the file 0600; a configuration file
            # created here gets ordinary umask-based permissions
            # instead, as an ordinary write would.
            umask = os.umask(0)
            os.umask(umask)
            mode = 0o666 & ~umask
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


class Configuration:
    """Read and write repospace configuration options.

    File locations are resolved at construction time; environment variable
    changes after that are not observed by this instance.
    """

    def __init__(self, topdir: Optional[str] = None):
        if topdir is None:
            try:
                topdir = util.topdir()
            except util.RepospaceNotFound:
                topdir = None
        self.topdir = topdir
        self._paths = {
            ConfigFile.SYSTEM: _system_path(),
            ConfigFile.GLOBAL: _global_path(),
            ConfigFile.LOCAL: _local_path(topdir),
        }
        self._parsers = {}
        for level, path in self._paths.items():
            # No interpolation: "%" is an ordinary character in values
            # (git format strings in aliases, URL-encoded strings, ...).
            parser = configparser.ConfigParser(allow_no_value=True, interpolation=None)
            if path is not None and os.path.isfile(path):
                # Not parser.read(), which silently skips files it
                # cannot open: a configuration the user believes is in
                # effect (e.g. unreadable due to permissions) must not
                # be dropped without a diagnostic.
                raw = configparser.ConfigParser(allow_no_value=True, interpolation=None)
                try:
                    with open(path, encoding="utf-8") as f:
                        raw.read_file(f, source=path)
                except OSError as err:
                    raise MalformedConfig(f"cannot read configuration file: {err}")
                except UnicodeDecodeError as err:
                    raise MalformedConfig(f"configuration file is not valid UTF-8: " f"{path}: {err}")
                except configparser.Error as err:
                    raise MalformedConfig(f"cannot parse configuration file: {err}")
                except AttributeError:
                    # configparser's own failure mode for an indented
                    # (continuation) line under an option that has no
                    # value: with allow_no_value the option holds None,
                    # and the reader appends to it. A parse error like
                    # the others, not a traceback out of every command.
                    raise MalformedConfig(
                        f"cannot parse configuration file: {path}: a "
                        "continuation line follows an option without a value"
                    )
                # The DEFAULT section name is reserved, in any case:
                # configparser copies its keys into every section on
                # read, which is why set() refuses to write it. Reject
                # it on read too — a leak-then-materialize would
                # otherwise duplicate the keys into every section the
                # next time the file is written.
                if raw.defaults() or any(
                    section.lower() == configparser.DEFAULTSECT.lower() for section in raw.sections()
                ):
                    raise MalformedConfig(
                        f"cannot use configuration file: {path}: the "
                        f'section name "{configparser.DEFAULTSECT}" is '
                        "reserved (in any case); rename the section"
                    )
                # Git-style case-insensitivity: section names are
                # lowercased (configparser already lowercases keys),
                # and sections differing only by case merge, later
                # definitions winning — as git config reads them.
                merged: dict = {}
                for section in raw.sections():
                    merged.setdefault(section.lower(), {}).update(raw.items(section))
                parser.read_dict(merged, source=path)
            self._parsers[level] = parser

    # Highest precedence first.
    _PRECEDENCE = (ConfigFile.LOCAL, ConfigFile.GLOBAL, ConfigFile.SYSTEM)

    def _raw_get(self, section: str, key: str, configfile: ConfigFile):
        if configfile == ConfigFile.ALL:
            levels: Iterable[ConfigFile] = self._PRECEDENCE
        else:
            levels = (configfile,)
        for level in levels:
            parser = self._parsers[level]
            if parser.has_option(section, key):
                value = parser.get(section, key)
                # allow_no_value: a key without "=" reads as None;
                # report it as set-but-empty, not as unset.
                return "" if value is None else value
        return None

    def get(self, option: str, default: Any = None, configfile: ConfigFile = ConfigFile.ALL) -> Any:
        section, key = parse_key(option)
        value = self._raw_get(section, key, configfile)
        return default if value is None else value

    def getboolean(self, option: str, default: Any = None, configfile: ConfigFile = ConfigFile.ALL) -> Any:
        value = self.get(option, None, configfile)
        if value is None:
            return default
        state = _BOOLEAN_STATES.get(str(value).lower())
        if state is None:
            raise MalformedConfig(f'"{option}" is not a boolean: "{value}"')
        return state

    def getint(self, option: str, default: Any = None, configfile: ConfigFile = ConfigFile.ALL) -> Any:
        value = self.get(option, None, configfile)
        if value is None:
            return default
        try:
            return int(value)
        except ValueError:
            raise MalformedConfig(f'"{option}" is not an integer: "{value}"')

    def getfloat(self, option: str, default: Any = None, configfile: ConfigFile = ConfigFile.ALL) -> Any:
        value = self.get(option, None, configfile)
        if value is None:
            return default
        try:
            return float(value)
        except ValueError:
            raise MalformedConfig(f'"{option}" is not a float: "{value}"')

    def set(self, option: str, value: Any, configfile: ConfigFile = ConfigFile.LOCAL) -> None:
        if configfile == ConfigFile.ALL:
            raise ValueError("set() requires a specific configuration file")
        section, key = parse_key(option)
        if section == configparser.DEFAULTSECT.lower():
            # Reserved by configparser in any case (names are
            # lowercased): "[DEFAULT]" keys would leak into every
            # section on the next read.
            raise MalformedConfig(
                f'cannot set "{option}": the section name ' f'"{configparser.DEFAULTSECT}" is reserved'
            )
        _check_value(option, str(value))
        # Stored as configparser reads it back: surrounding whitespace
        # on each line and trailing blank lines do not survive the
        # file, so keeping them in memory would make get() answer
        # differently in this process than in the next one.
        value = "\n".join(line.strip() for line in str(value).split("\n")).rstrip()
        path = self._paths[configfile]
        if path is None:
            raise MalformedConfig(
                "local configuration file location unknown; run inside a " "repospace or set REPOSPACE_CONFIG_LOCAL"
            )
        parser = self._parsers[configfile]
        try:
            if not parser.has_section(section):
                parser.add_section(section)
            parser.set(section, key, value)
        except ValueError as err:
            raise MalformedConfig(f'cannot set "{option}": {err}')
        self._edit_file(configfile, section, key, value)

    def delete(self, option: str, configfile: Optional[ConfigFile] = None) -> None:
        """Delete *option*.

        With configfile=None, delete from the highest-precedence file where
        it is set. With ConfigFile.ALL, delete from every file where it is
        set. Raise KeyError if the option is not set anywhere applicable.
        """
        section, key = parse_key(option)
        if configfile is None:
            levels: Iterable[ConfigFile] = self._PRECEDENCE
            delete_all = False
        elif configfile == ConfigFile.ALL:
            levels = self._PRECEDENCE
            delete_all = True
        else:
            levels = (configfile,)
            delete_all = True
        deleted = False
        for level in levels:
            parser = self._parsers[level]
            if not parser.has_option(section, key):
                continue
            parser.remove_option(section, key)
            if not parser.options(section):
                parser.remove_section(section)
            self._edit_file(level, section, key, None)
            deleted = True
            if not delete_all:
                return
        if not deleted:
            raise KeyError(option)

    def items(self, configfile: ConfigFile = ConfigFile.ALL):
        """Return (option, value) pairs, precedence applied for ALL."""
        if configfile == ConfigFile.ALL:
            merged = {}
            for level in reversed(self._PRECEDENCE):
                for option, value in self.items(level):
                    merged[option] = value
            return list(merged.items())
        parser = self._parsers[configfile]
        result = []
        for section in parser.sections():
            for key, value in parser.items(section):
                result.append((f"{section}.{key}", "" if value is None else value))
        return result

    def _edit_file(self, configfile: ConfigFile, section: str, key: str, value: Optional[str]) -> None:
        """Set or delete one option in the file, preserving comments.

        The file is re-read and edited textually rather than rewritten
        from the in-memory parser, which would drop every comment.
        """
        path = self._paths[configfile]
        # Wrapped like the read in __init__: a permission problem or
        # full disk must not become a traceback.
        try:
            pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
            except FileNotFoundError:
                text = ""
            # The replacement is computed before anything is written,
            # then swapped in whole: truncating the file first would
            # leave the user with an empty configuration if the edit or
            # the write failed part-way.
            _replace_file(path, _edited(text, section, key, value))
        except OSError as err:
            raise MalformedConfig(f"cannot write configuration file: {err}")
        except UnicodeDecodeError as err:
            raise MalformedConfig(f"configuration file is not valid UTF-8: {path}: {err}")
