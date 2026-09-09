"""Manifest parsing, validation, and import resolution.

A repospace manifest is a YAML file (conventionally ``repospace.yaml``) with a single top-level
``manifest`` key containing these sections, all optional:

  version:       minimum schema version required to parse the file
  defaults:      default member remote and revision (this file only)
  remotes:       named URL prefixes
  members:       the repositories that make up the repospace
  self:          attributes of the manifest repository itself
  group-filter:  groups disabled or enabled by default

Manifests may import other manifests, either from the manifest repository itself (``self: import:``,
read from the filesystem) or from members (``members: - import:``, read from git at
``refs/heads/repospace-rev``). A ``self: import:`` inside a manifest that itself came from a member
is read from that member's git data too, never from its working tree. Resolution precedence:
self-imports first, then this file's members, then member imports in declaration order. The first
definition of a member name wins; later definitions are ignored and their imports are never
processed.
"""

from __future__ import annotations

import enum
import logging
import os
import posixpath
import re
import subprocess
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

import yaml

from repospace import util
from repospace.configuration import Configuration
from repospace.git import run_git

_logger = logging.getLogger(__name__)

#: The default manifest file name. The top-level entry point can be changed with the manifest.file
#: configuration option (written by "init --manifest"); imports name other files explicitly.
MANIFEST_FILE = "repospace.yaml"

#: Highest manifest schema version this repospace understands.
#:
#: MAINTAINERS: this is the major.minor of the repospace release that last changed the manifest
#: format, so it never runs ahead of repospace.__version__ and is left alone by a release that adds
#: no manifest feature. Bumping it means adding the new value to _VALID_SCHEMA_VERSIONS below.
SCHEMA_VERSION = "0.2"

#: Every schema version this repospace accepts, newest last. A version is valid only by being on
#: this list: releases that changed nothing in the manifest format have no schema version of their
#: own, so an unlisted value is a typo rather than an older schema.
_VALID_SCHEMA_VERSIONS = (SCHEMA_VERSION,)

#: Local branch owned by repospace, pointing at each member's manifest revision as of the last
#: update.
MANIFEST_REV = "repospace-rev"

#: Fully qualified name of MANIFEST_REV.
QUAL_MANIFEST_REV = f"refs/heads/{MANIFEST_REV}"

#: Scratch ref namespace used while fetching; always cleaned afterwards.
QUAL_REFS = "refs/repospace/"

#: Index of the ManifestMember in Manifest.members.
MANIFEST_MEMBER_INDEX = 0

#: Name of the manifest repository, reserved so that no member can take it. It is also what
#: Member.declared_by holds for a member the manifest repository declares itself (in its manifest
#: file and the files that file self-imports), as opposed to one an imported member declares.
MANIFEST_MEMBER_NAME = "manifest"

_DEFAULT_REVISION = "main"
_MAX_IMPORT_DEPTH = 50
_ENCODING = "utf-8"

#: git file mode of a symbolic link; git stores one as a blob holding the link target, so the object
#: type alone does not identify it.
_SYMLINK_MODE = "120000"


class MalformedManifest(RuntimeError):
    """Raised when manifest contents are invalid."""


class ManifestVersionError(RuntimeError):
    """Raised when a manifest requires a newer repospace."""

    def __init__(self, version: str, file: Optional[str] = None):
        super().__init__(
            f'manifest requires schema version "{version}", but this '
            f"repospace only supports up to {SCHEMA_VERSION}; "
            "please upgrade repospace"
        )
        self.version = version
        self.file = file


class ManifestImportFailed(RuntimeError):
    """Raised when imported manifest data cannot be obtained."""

    def __init__(self, member: Optional["Member"], path: Any, extra: str = ""):
        name = member.name if member is not None else "self"
        msg = f'failed to import manifest data "{path}" from {name}'
        if extra:
            msg += f": {extra}"
        super().__init__(msg)
        self.member = member
        self.path = path


class ImportFlag(enum.IntFlag):
    """Flags controlling import resolution."""

    #: Resolve all imports.
    DEFAULT = 0
    #: Ignore all imports.
    IGNORE = 1
    #: Always obtain member import data through the importer callback, never by reading a member's
    #: local git objects directly.
    FORCE_MEMBERS = 2
    #: Resolve self-imports only; ignore member imports.
    IGNORE_MEMBERS = 4


#: An importer callback receives the Member whose import is being resolved and the requested path
#: within it. It returns the manifest content as a string (or list of strings, for a directory), or
#: None to skip the import.
ImporterType = Callable[["Member", str], Optional[Union[str, List[str]]]]


@dataclass(frozen=True)
class Submodule:
    path: str
    name: Optional[str] = None


@dataclass(frozen=True)
class RefPatterns:
    """Branch and tag name patterns; see util.ref_patterns_match for how a list is read."""

    heads: Tuple[str, ...]
    tags: Tuple[str, ...]


#: Default "upstream: mirror:" selection: every branch and every tag. Spelled "**", not "*": a
#: single star stays within one "/"-separated component, so it would leave "release/1.0" unselected
#: (and therefore pruned from origin).
MIRROR_ALL = RefPatterns(("**",), ("**",))

#: Default "upstream: preserve:" selection: nothing.
PRESERVE_NONE = RefPatterns((), ())


@dataclass(frozen=True)
class Upstream:
    """A member's upstream repository and the ref selection the mirror command applies."""

    url: str
    mirror: RefPatterns = MIRROR_ALL
    preserve: RefPatterns = PRESERVE_NONE

    def __post_init__(self):
        """Refuse a "mirror: heads" that selects no branch however the refs turn out.

        Upstream always has a default branch, so a mirror is always for at least one branch, and
        "selected" is what the whole command is built on -- with --prune it means "delete every
        branch origin has". Two spellings select nothing whatever upstream holds, and both are
        refused here: an empty list, and a list of nothing but "!" exclusions, which only ever take
        away from what another pattern selected. No manifest can spell either -- _check_ref_patterns
        refuses them -- so the values are unloadable as well as meaningless, and the constructor is
        where that becomes impossible rather than merely unlikely.

        The other three lists may be empty. "mirror: tags: []" is a fork that wants none of
        upstream's tags, and each list defaulting to "**" on its own leaves no other way to say so;
        an empty "preserve" is the default, and it means what it says.
        """
        if not self.mirror.heads:
            raise MalformedManifest(
                f'upstream {self.url}: "mirror: heads" is empty; '
                "a mirror selects no branch without at least one pattern"
            )
        if not util.has_positive_ref_pattern(self.mirror.heads):
            raise MalformedManifest(
                f'upstream {self.url}: "mirror: heads" holds only exclusions; '
                'a mirror selects no branch without a pattern that does not begin with "!"'
            )

    def as_dict(self) -> dict:
        """Return this upstream as manifest data, omitting the lists left at their defaults."""
        data: Dict[str, Any] = {"url": self.url}
        for key, patterns, default in (
            ("mirror", self.mirror, MIRROR_ALL),
            ("preserve", self.preserve, PRESERVE_NONE),
        ):
            lists = {}
            for name, value, fallback in (
                ("heads", patterns.heads, default.heads),
                ("tags", patterns.tags, default.tags),
            ):
                if value == fallback:
                    continue
                lists[name] = list(value)
            if lists:
                data[key] = lists
        return data


def is_group(value: Any) -> bool:
    """Return True if *value* is a valid group name.

    Group names are strings, never numbers: YAML would otherwise mangle unquoted numeric names (1.10
    parses as the float 1.1).
    """
    if not isinstance(value, str) or not value:
        return False
    if value[0] in "+-":
        return False
    return not any(c.isspace() or c in ",:" for c in value)


def _merge_unique(first: List[str], second: List[str]) -> List[str]:
    """Concatenate keeping first-occurrence order, dropping duplicates."""
    result = list(first)
    for item in second:
        if item not in result:
            result.append(item)
    return result


def _str_list(value: Union[None, str, List[str]]) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


class Member:
    """A repository declared in a manifest."""

    def __init__(
        self,
        name: str,
        url: Optional[str],
        revision: Optional[str] = None,
        path: Optional[str] = None,
        *,
        submodules: Union[bool, List[Submodule]] = False,
        clone_depth: Optional[int] = None,
        extension_commands: Optional[List[str]] = None,
        cmake_packages: Optional[List[str]] = None,
        topdir: Optional[str] = None,
        groups: Optional[List[str]] = None,
        userdata: Any = None,
        description: Optional[str] = None,
        declared_by: Optional[str] = None,
        upstream: Optional[Upstream] = None,
    ):
        self.name = name
        self.url = url
        self.revision = revision or _DEFAULT_REVISION
        self.path = path or name
        self.submodules = submodules
        self.clone_depth = clone_depth
        self.extension_commands = _str_list(extension_commands)
        self.cmake_packages = _str_list(cmake_packages)
        self.topdir = topdir
        # Always "origin", however the URL was declared: manifest remote names are URL-prefix
        # identifiers, not git remote names.
        self.remote_name = "origin"
        self.groups = list(groups or [])
        self.userdata = userdata
        self.description = description
        self.declared_by = declared_by
        self.upstream = upstream

    def __repr__(self):
        return f'Member("{self.name}", path="{self.path}")'

    @property
    def abspath(self) -> Optional[str]:
        if self.topdir is None or self.path is None:
            return None
        return os.path.realpath(os.path.join(self.topdir, self.path))

    @property
    def posixpath(self) -> Optional[str]:
        abspath = self.abspath
        return None if abspath is None else Path(abspath).as_posix()

    @property
    def name_and_path(self) -> str:
        return f"{self.name} ({self.path})"

    def git(
        self,
        args,
        check: bool = True,
        capture_stdout: bool = False,
        capture_stderr: bool = False,
        cwd=None,
        stdin_data: Optional[bytes] = None,
    ) -> subprocess.CompletedProcess:
        """Run git in this member's repository."""
        where = cwd if cwd is not None else self.abspath
        if where is None:
            raise RuntimeError(f"member {self.name} has no absolute path (no topdir)")
        return run_git(
            args,
            cwd=where,
            check=check,
            capture_stdout=capture_stdout,
            capture_stderr=capture_stderr,
            stdin_data=stdin_data,
        )

    def is_cloned(self) -> bool:
        """Return True if the member's directory is a git repository root."""
        abspath = self.abspath
        if abspath is None or not os.path.isdir(abspath):
            return False
        result = self.git(
            ["rev-parse", "--show-cdup"],
            check=False,
            capture_stdout=True,
            capture_stderr=True,
        )
        return result.returncode == 0 and not result.stdout.strip()

    def sha(self, rev: str = QUAL_MANIFEST_REV, capture_stderr: bool = False) -> str:
        """Return the commit SHA *rev* peels to.

        Callers that catch the CalledProcessError and treat a missing revision as a normal outcome
        should pass capture_stderr=True, or git's "fatal: ..." message leaks to the terminal.
        """
        result = self.git(
            ["rev-parse", f"{rev}^{{commit}}"],
            capture_stdout=True,
            capture_stderr=capture_stderr,
        )
        return result.stdout.decode().strip()

    def is_ancestor_of(self, rev1: str, rev2: str) -> bool:
        """Say whether *rev1* is an ancestor of *rev2*; a revision git cannot resolve is not."""
        result = self.git(["merge-base", "--is-ancestor", rev1, rev2], check=False)
        return result.returncode == 0

    def read_at(self, path: str, rev: str = QUAL_MANIFEST_REV) -> bytes:
        """Read a blob at *path* from *rev* in this member's repository."""
        result = self.git(["cat-file", "blob", f"{rev}:{path}"], capture_stdout=True)
        return result.stdout

    def as_dict(self) -> dict:
        """Return this member as manifest data (for resolved output)."""
        data: Dict[str, Any] = {"name": self.name}
        if self.description is not None:
            data["description"] = self.description
        data["url"] = self.url
        data["revision"] = self.revision
        if self.path != self.name:
            data["path"] = self.path
        if self.upstream is not None:
            data["upstream"] = self.upstream.as_dict()
        if self.clone_depth is not None:
            data["clone-depth"] = self.clone_depth
        if self.extension_commands:
            data["extension-commands"] = _shrink(self.extension_commands)
        if self.cmake_packages:
            data["cmake-packages"] = list(self.cmake_packages)
        if self.groups:
            data["groups"] = list(self.groups)
        if self.submodules:
            if isinstance(self.submodules, bool):
                data["submodules"] = True
            else:
                data["submodules"] = [
                    {"path": s.path, **({"name": s.name} if s.name else {})}
                    for s in self.submodules
                ]
        if self.userdata is not None:
            data["userdata"] = self.userdata
        return data


def _shrink(values: List[str]) -> Union[str, List[str]]:
    return values[0] if len(values) == 1 else list(values)


class ManifestMember(Member):
    """The manifest repository itself, always Manifest.members[0]."""

    def __init__(
        self,
        path: Optional[str] = None,
        topdir: Optional[str] = None,
        *,
        extension_commands: Optional[List[str]] = None,
        cmake_packages: Optional[List[str]] = None,
        userdata: Any = None,
    ):
        super().__init__(
            MANIFEST_MEMBER_NAME,
            url=None,
            revision=None,
            path=path,
            topdir=topdir,
            extension_commands=extension_commands,
            cmake_packages=cmake_packages,
            userdata=userdata,
        )
        # The manifest repository has no managed revision, and its path may legitimately be unknown
        # (no topdir).
        self.revision = None
        self.path = path

    def as_dict(self) -> dict:
        # The manifest repository is the repospace root itself; there is no location to record in
        # manifest data.
        data: Dict[str, Any] = {}
        if self.extension_commands:
            data["extension-commands"] = _shrink(self.extension_commands)
        if self.cmake_packages:
            data["cmake-packages"] = list(self.cmake_packages)
        if self.userdata is not None:
            data["userdata"] = self.userdata
        return data


# ---------------------------------------------------------------------------
# Validation

_TOP_KEYS = frozenset({"manifest"})
_MANIFEST_KEYS = frozenset({"version", "defaults", "remotes", "members", "self", "group-filter"})
_DEFAULTS_KEYS = frozenset({"remote", "revision"})
_REMOTE_KEYS = frozenset({"name", "url-base"})
_MEMBER_KEYS = frozenset(
    {
        "name",
        "description",
        "remote",
        "repo-path",
        "url",
        "revision",
        "path",
        "submodules",
        "clone-depth",
        "extension-commands",
        "cmake-packages",
        "import",
        "groups",
        "userdata",
        "upstream",
    }
)
_UPSTREAM_KEYS = frozenset({"remote", "repo-path", "url", "mirror", "preserve"})
#: The two ref namespaces "mirror" and "preserve" name. A tuple, not a set: _check_ref_patterns also
#: iterates it to check the values, and the order it reports two bad lists in must not depend on how
#: the strings happened to hash.
_REF_PATTERN_KEYS = ("heads", "tags")
#: Keys naming a repository, on a member and on its "upstream" alike. An explicit null and an empty
#: string are the same editing artifact here -- both would silently fall back to a default, or crash
#: path derivation -- so both get the same advice rather than a null being reported as a type error.
_REPO_KEYS = ("remote", "repo-path", "url")
_VALUE_HINT = "remove the key or set a value"
_SELF_KEYS = frozenset({"name", "extension-commands", "cmake-packages", "import", "userdata"})
_IMPORT_MAP_KEYS = frozenset(
    {
        "file",
        "name-allowlist",
        "name-blocklist",
        "path-allowlist",
        "path-blocklist",
        "path-prefix",
    }
)


#: CMake package names are interpolated into generated CMake code (packages.cmake), where arbitrary
#: characters could break the file or inject commands; restrict them to a safe set.
_CMAKE_PACKAGE_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*\Z")


def _version_tuple(version: str) -> Tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return ()


def _check_version(mdata: dict, where: str) -> None:
    if "version" not in mdata:
        return
    # Checked here rather than with the other keys: the version is checked before anything else, and
    # str(None) would otherwise be reported as the invalid version "None" with a quoting hint that
    # cannot help.
    _check_null_keys(mdata, ("version",), where)
    raw = mdata["version"]
    version = str(raw)
    parsed = _version_tuple(version)
    supported = _version_tuple(SCHEMA_VERSION)
    # Too new is reported before anything else, so that a file from a later repospace is answered
    # with "upgrade" rather than with a list of versions that cannot contain what it asks for. The
    # comparison is zero-padded to a common length, so "0.2.0" is not read as newer than "0.2".
    width = max(len(parsed), len(supported))
    padded = parsed + (0,) * (width - len(parsed))
    if parsed and padded > supported + (0,) * (width - len(supported)):
        raise ManifestVersionError(version, where)
    # Anything else has to name a version this repospace actually speaks. Spellings that merely
    # compare equal ("0.2.0") are rejected too: one version, one way to write it.
    if version in _VALID_SCHEMA_VERSIONS:
        return
    hint = "" if isinstance(raw, str) else "; do you need to quote the value?"
    valid = ", ".join(_VALID_SCHEMA_VERSIONS)
    raise MalformedManifest(
        f'{where}: invalid manifest version "{version}"; must be one of: {valid}{hint}'
    )


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise MalformedManifest(message)


def _key_list(keys) -> List[str]:
    """Render *keys* as a sorted list of strings.

    Manifest keys are not all strings: YAML resolves a bare "on:" or "no:" to a bool and a bare date
    to datetime.date, and such keys do not sort against strings. Sorting their string form keeps a
    key set reportable whatever the file contains.
    """
    return sorted(str(key) for key in keys)


def _check_keys(mapping: dict, allowed: Iterable[str], where: str) -> None:
    unknown = set(mapping).difference(allowed)
    _expect(not unknown, f"{where}: unknown key(s): {_key_list(unknown)}")


def _check_null_keys(mapping: dict, keys, where: str, hints: Optional[dict] = None) -> None:
    """Reject keys present with an explicit null value.

    Explicit null is invalid for every key except "manifest" itself and "userdata": the schema types
    everything else non-null, and a null key is an editing artifact — the key belongs removed
    instead.
    """
    for key in keys:
        if key in mapping and mapping[key] is None:
            hint = (hints or {}).get(key, "remove the key")
            raise MalformedManifest(f'{where}: "{key}" has no value; {hint}')


def _is_str_or_str_list(value: Any) -> bool:
    return isinstance(value, str) or (
        isinstance(value, list) and all(isinstance(v, str) for v in value)
    )


def _check_cmake_packages(value: Any, where: str) -> None:
    _expect(
        value is None or _is_str_or_str_list(value),
        f'{where}: "cmake-packages" is not a string or list of strings',
    )
    for name in _str_list(value):
        _expect(
            _CMAKE_PACKAGE_RE.match(name) is not None,
            f'{where}: invalid CMake package name "{name}"; package '
            "names may only contain letters, digits, and ._+- and must "
            "not start with a punctuation character",
        )


def _refname_defect(value: str, allow_glob: bool = False) -> Optional[str]:
    """Say what keeps *value* from being a git refname, or return None.

    These are git-check-ref-format's character rules. A revision reaches git as a fetch refspec and
    as a revision argument, and every refused character has syntax of its own there: ":" separates a
    refspec's source from its destination (a manifest could then move any local branch of the
    member), "*" makes it a pattern, "^", "~", ".." and "@{" are revision operators, "?", "[" and
    "\\" are glob syntax, and whitespace or control characters split or hide the argument.

    With *allow_glob*, "?", "*" and "[" pass: an upstream ref pattern is matched by repospace itself
    and never handed to git. "\\" stays refused, as no refname contains one and the patterns have
    no escape syntax.
    """
    refused = " ~^:\\" if allow_glob else " ~^:?*[\\"
    for char in value:
        if char in refused or ord(char) < 0x20 or ord(char) == 0x7F:
            return f"contains {char!r}"
    for sequence in ("..", "@{"):
        if sequence in value:
            return f'contains "{sequence}"'
    if value == "@":
        return 'is "@"'
    return None


def _check_revision(value: Any, where: str) -> None:
    # Revisions are strings, never numbers: YAML mangles unquoted numeric revisions (1.10 becomes
    # the float 1.1, 0700 an octal int), and a stray number is almost always a missing quote.
    if isinstance(value, str):
        if value.startswith("-"):
            # Git refnames cannot begin with "-", and such a value would be read as a command-line
            # option by the git commands that take a revision.
            raise MalformedManifest(
                f'{where}: revision "{value}" begins with "-"; this is not a valid git revision'
            )
        if value.startswith("+"):
            # A refname may begin with "+", but a refspec beginning with "+" is a forced one for the
            # name that follows it, so "update" would fetch a different branch than the one named.
            raise MalformedManifest(
                f'{where}: revision "{value}" begins with "+", which marks a forced refspec '
                f"when it is fetched; name the branch as refs/heads/{value} instead"
            )
        if not value:
            raise MalformedManifest(f"{where}: revision is empty; remove the key or set one")
        defect = _refname_defect(value)
        if defect is not None:
            raise MalformedManifest(
                f'{where}: revision "{value}" {defect}; this is not a valid git revision'
            )
        return
    if value is None:
        raise MalformedManifest(f"{where}: revision has no value; remove the key or set one")
    raise MalformedManifest(
        f'{where}: revision "{value}" is not a string; do you need to quote the value?'
    )


def _check_ref_patterns(value: Any, where: str, allow_empty: Sequence[str] = ()) -> None:
    """Check the shape of an "upstream: mirror" or "upstream: preserve" value.

    A key named by *allow_empty* may be spelled as an empty list; see Upstream.__post_init__ for
    which one is, and why the others are not.

    A pattern may begin with "!", which excludes what it matches from what the patterns before it
    selected (see util.ref_patterns_match). The marker is stripped before the name below it is
    checked, so an exclusion is held to the same rules as any other pattern -- and a list of
    nothing but exclusions is refused, since it selects nothing whatever refs exist.
    """
    _expect(isinstance(value, dict), f"{where} is not a mapping")
    _check_keys(value, _REF_PATTERN_KEYS, where)
    _check_null_keys(value, _REF_PATTERN_KEYS, where)
    for key in _REF_PATTERN_KEYS:
        if key not in value:
            continue
        patterns = value[key]
        empty_ok = key in allow_empty
        _expect(
            isinstance(patterns, list)
            and (empty_ok or len(patterns) > 0)
            and all(isinstance(p, str) for p in patterns),
            f'{where}: "{key}" is not a {"" if empty_ok else "non-empty "}list of strings',
        )
        for pattern in patterns:
            if not pattern:
                raise MalformedManifest(
                    f'{where}: "{key}" has an empty pattern; remove it or set a value'
                )
            _, body = util.split_ref_pattern(pattern)
            if not body:
                raise MalformedManifest(
                    f'{where}: "{key}" has a pattern that is only "{util.NEGATION}"; '
                    "an exclusion needs a pattern to exclude"
                )
            defect = _refname_defect(body, allow_glob=True)
            if defect is not None:
                raise MalformedManifest(
                    f'{where}: "{key}" pattern "{pattern}" {defect}; '
                    "this is not a valid ref pattern"
                )
        if patterns and not util.has_positive_ref_pattern(patterns):
            # Caught here rather than left to the run: an exclusion only takes away from what
            # another pattern selected, so a list of nothing else selects nothing at all -- for
            # "mirror" that is a member the run refuses, and for "preserve" it is the default
            # written out at length.
            hint = ", or write it as [] to select nothing" if empty_ok else ""
            raise MalformedManifest(
                f'{where}: "{key}" holds only exclusions, so it selects nothing; '
                f'add a pattern that does not begin with "{util.NEGATION}"{hint}'
            )


def _check_upstream(value: Any, where: str) -> None:
    """Check the shape of a member "upstream" value without resolving its URL."""
    _expect(isinstance(value, dict), f'{where}: "upstream" is not a mapping')
    here = f"{where}: upstream"
    _check_keys(value, _UPSTREAM_KEYS, here)
    _check_null_keys(
        value,
        _REPO_KEYS + ("mirror", "preserve"),
        here,
        hints=dict.fromkeys(_REPO_KEYS, _VALUE_HINT),
    )
    for key in _REPO_KEYS:
        if key in value:
            _expect(isinstance(value[key], str), f'{here}: "{key}" is not a string')
            if value[key] == "":
                raise MalformedManifest(f'{here}: "{key}" is empty; {_VALUE_HINT}')
    for key in ("mirror", "preserve"):
        if key in value:
            # Only "mirror: tags" may be an empty list; the other three have nothing to say.
            _check_ref_patterns(
                value[key],
                f"{here}: {key}",
                allow_empty=("tags",) if key == "mirror" else (),
            )


def _check_member_import(
    imp: Any, where: str, in_sequence: bool = False, _seen=frozenset()
) -> None:
    """Check the shape of a member "import" value without resolving it.

    Mirrors the cases _import_from_member handles, so that a load that does not follow imports
    (ImportFlag.IGNORE, as "manifest --validate" uses) still rejects what a real load would.
    """
    if isinstance(imp, bool):
        if in_sequence and not imp:
            raise MalformedManifest(f'{where}: falsy "import" inside a sequence')
    elif isinstance(imp, str):
        if not imp:
            raise MalformedManifest(f'{where}: "import" is empty; remove the key or set a value')
    elif isinstance(imp, list):
        if id(imp) in _seen:
            raise MalformedManifest(f'{where}: "import" contains a recursive YAML alias')
        for subimp in imp:
            _check_member_import(subimp, where, True, _seen | {id(imp)})
    elif isinstance(imp, dict):
        _load_import_map(imp, where)
    else:
        raise MalformedManifest(f"{where}: invalid import {imp} of type {type(imp).__name__}")


def _check_self_import(imp: Any, where: str, _seen=frozenset()) -> None:
    """Check the shape of a "self: import" value; see _check_member_import."""
    if isinstance(imp, bool):
        raise MalformedManifest(f'{where}: got "self: import: {imp}" of boolean')
    if isinstance(imp, str):
        if not imp:
            raise MalformedManifest(
                f'{where}: "self: import" is empty; remove the key or set a value'
            )
    elif isinstance(imp, list):
        if id(imp) in _seen:
            raise MalformedManifest(f'{where}: "self: import" contains a recursive YAML alias')
        for subimp in imp:
            _check_self_import(subimp, where, _seen | {id(imp)})
    elif isinstance(imp, dict):
        _load_import_map(imp, where)
    else:
        raise MalformedManifest(
            f'{where}: "self: import: {imp}" has invalid type {type(imp).__name__}'
        )


def validate(source: Union[str, dict], where: str = "manifest data") -> dict:
    """Validate manifest data; return the value of the ``manifest`` key.

    *source* is YAML text or already-parsed data. Raises MalformedManifest or ManifestVersionError.
    The version is checked before anything else, since future schemas may be structurally
    incompatible. Import values are checked for shape here, not only when they are resolved, so a
    load with imports disabled rejects the same manifests a full load does.
    """
    if isinstance(source, str):
        try:
            data = yaml.safe_load(source)
        except yaml.YAMLError as err:
            raise MalformedManifest(f"{where}: cannot parse YAML: {err}")
    else:
        data = source
    _expect(isinstance(data, dict), f"{where}: manifest data is not a mapping")
    _check_keys(data, _TOP_KEYS, where)
    _expect("manifest" in data, f'{where}: no "manifest" key')
    mdata = data["manifest"]
    if mdata is None:
        mdata = {}
    _expect(isinstance(mdata, dict), f'{where}: "manifest" value is not a mapping')
    _check_version(mdata, where)
    _check_keys(mdata, _MANIFEST_KEYS, where)
    _check_null_keys(
        mdata,
        ("defaults", "remotes", "members", "self", "group-filter"),
        where,
    )

    defaults = mdata.get("defaults")
    if defaults is not None:
        _expect(isinstance(defaults, dict), f"{where}: defaults is not a mapping")
        _check_keys(defaults, _DEFAULTS_KEYS, f"{where}: defaults")
        # Presence checks, not .get(): an explicit null would otherwise be coerced downstream
        # (str(None) is the revision "None").
        if "remote" in defaults:
            _expect(
                isinstance(defaults["remote"], str) and defaults["remote"] != "",
                f"{where}: defaults: remote is not a non-empty string",
            )
        if "revision" in defaults:
            _check_revision(defaults["revision"], f"{where}: defaults")

    remotes = mdata.get("remotes")
    if remotes is not None:
        _expect(isinstance(remotes, list), f"{where}: remotes is not a sequence")
        remote_names = set()
        for remote in remotes:
            _expect(
                isinstance(remote, dict),
                f"{where}: remotes entry is not a mapping",
            )
            _check_keys(remote, _REMOTE_KEYS, f"{where}: remotes entry")
            for key in ("name", "url-base"):
                _expect(
                    isinstance(remote.get(key), str) and remote[key] != "",
                    f'{where}: remotes entry needs a non-empty string "{key}"',
                )
            # Like duplicate member names: the later entry would win silently, and which URL a
            # member gets must not depend on declaration order.
            _expect(
                remote["name"] not in remote_names,
                f'{where}: remote name {remote["name"]} used twice',
            )
            remote_names.add(remote["name"])

    members = mdata.get("members")
    if members is not None:
        _expect(isinstance(members, list), f"{where}: members is not a sequence")
        for md in members:
            _expect(
                isinstance(md, dict),
                f"{where}: members entry is not a mapping",
            )
            _check_keys(md, _MEMBER_KEYS, f"{where}: member entry")
            _expect(
                isinstance(md.get("name"), str) and md["name"] != "",
                f'{where}: member entry needs a non-empty string "name"',
            )
            name = md["name"]
            here = f"{where}: member {name}"
            # String keys use presence checks so an explicit null is an error: it would otherwise
            # crash path joining or coerce to the literal revision "None".
            string_keys = ("description",) + _REPO_KEYS + ("path",)
            _check_null_keys(md, string_keys, here, hints=dict.fromkeys(string_keys, _VALUE_HINT))
            for key in string_keys:
                if key in md:
                    _expect(
                        isinstance(md[key], str),
                        f'{here}: "{key}" is not a string',
                    )
            # An empty string would silently fall back to a default (or crash path derivation);
            # reject it like an explicit null.
            for key in _REPO_KEYS + ("path",):
                if key in md and md[key] == "":
                    raise MalformedManifest(f'{here}: "{key}" is empty; {_VALUE_HINT}')
            _check_null_keys(
                md,
                (
                    "clone-depth",
                    "submodules",
                    "extension-commands",
                    "cmake-packages",
                    "import",
                    "groups",
                    "upstream",
                ),
                here,
                hints={
                    "submodules": 'remove the key or use "submodules: false"',
                    "import": 'remove the key or use "import: false"',
                },
            )
            if "import" in md:
                _check_member_import(md["import"], here)
            if "upstream" in md:
                _check_upstream(md["upstream"], here)
            depth = md.get("clone-depth")
            _expect(
                depth is None
                or (isinstance(depth, int) and not isinstance(depth, bool) and depth >= 1),
                f'{here}: "clone-depth" is not a positive integer',
            )
            if "revision" in md:
                _check_revision(md["revision"], here)
            ce = md.get("extension-commands")
            _expect(
                ce is None or _is_str_or_str_list(ce),
                f'{here}: "extension-commands" is not a string or list of strings',
            )
            _check_cmake_packages(md.get("cmake-packages"), here)
            groups = md.get("groups")
            _expect(
                groups is None or isinstance(groups, list),
                f'{here}: "groups" is not a sequence',
            )

    slf = mdata.get("self")
    if slf is not None:
        _expect(isinstance(slf, dict), f"{where}: self is not a mapping")
        _check_keys(slf, _SELF_KEYS, f"{where}: self")
        _check_null_keys(
            slf,
            ("extension-commands", "cmake-packages", "import"),
            f"{where}: self",
        )
        if "import" in slf:
            _check_self_import(slf["import"], where)
        name = slf.get("name")
        if "name" in slf:
            _expect(
                isinstance(name, str),
                f'{where}: self: "name" is not a string',
            )
        if isinstance(name, str):
            # A suggested clone-directory name only, never a path: the manifest author must not
            # steer placement on the caller's filesystem. Leading dots are refused entirely: they
            # cover ".", "..", and hidden or git-confusing names (".git", ".repospace") the author
            # must not create either.
            _expect(
                name != "" and not name.startswith(".") and "/" not in name and "\\" not in name,
                f'{where}: self: "name" must be a single plain path '
                f'component that does not start with ".", not "{name}"',
            )
        ce = slf.get("extension-commands")
        _expect(
            ce is None or _is_str_or_str_list(ce),
            f'{where}: self: "extension-commands" is not a string or list of strings',
        )
        _check_cmake_packages(slf.get("cmake-packages"), f"{where}: self")

    gf = mdata.get("group-filter")
    if gf is not None:
        _expect(isinstance(gf, list), f"{where}: group-filter is not a sequence")

    return mdata


# ---------------------------------------------------------------------------
# Import machinery


@dataclass(frozen=True)
class _ImportMap:
    file: str
    name_allowlist: List[str]
    name_blocklist: List[str]
    path_allowlist: List[str]
    path_blocklist: List[str]
    path_prefix: str


_FilterFn = Optional[Callable[[Member], bool]]


def _import_requested(imp: Any) -> bool:
    """True if a member "import" value requests an import.

    An absent key requests nothing, and "import: false" is the explicit way to say so (validation
    rejects an explicit null). Any other value is a request — including an empty mapping, since
    every import-map key is optional.
    """
    return imp is not None and imp is not False


def _load_import_map(imp: dict, where: str) -> _ImportMap:
    _expect(
        not (set(imp) - _IMPORT_MAP_KEYS),
        f"{where}: invalid import contents: {_key_list(set(imp) - _IMPORT_MAP_KEYS)}",
    )
    _check_null_keys(imp, sorted(_IMPORT_MAP_KEYS), where)
    lists = {}
    for key in (
        "name-allowlist",
        "name-blocklist",
        "path-allowlist",
        "path-blocklist",
    ):
        value = imp.get(key, [])
        _expect(
            _is_str_or_str_list(value),
            f'{where}: bad import "{key}": {value}',
        )
        lists[key] = _str_list(value)
    for key in ("path-allowlist", "path-blocklist"):
        for pattern in lists[key]:
            # A pattern with no path components ("" or ".") matches nothing and makes PurePath.match
            # raise; like the other empty strings in a manifest it is an editing artifact.
            _expect(
                bool(PurePosixPath(pattern).parts),
                f'{where}: import "{key}" entry "{pattern}" is not a '
                "path pattern; remove it or set a value",
            )
    file = imp.get("file", MANIFEST_FILE)
    _expect(isinstance(file, str), f'{where}: import "file" is not a string')
    if file == "":
        # An empty path would import the repository root directory, not fall back to the default;
        # almost certainly an editing artifact.
        raise MalformedManifest(f'{where}: import "file" is empty; remove the key or set a value')
    prefix = imp.get("path-prefix", "")
    _expect(
        isinstance(prefix, str),
        f'{where}: import "path-prefix" is not a string',
    )
    return _ImportMap(
        file,
        lists["name-allowlist"],
        lists["name-blocklist"],
        lists["path-allowlist"],
        lists["path-blocklist"],
        prefix,
    )


def _import_map_filter(imap: _ImportMap) -> _FilterFn:
    if not (
        imap.name_allowlist or imap.name_blocklist or imap.path_allowlist or imap.path_blocklist
    ):
        return None

    def check(member: Member) -> bool:
        name = member.name
        path = PurePosixPath(member.path)
        blocked = name in imap.name_blocklist or any(path.match(p) for p in imap.path_blocklist)
        allowed = name in imap.name_allowlist or any(path.match(p) for p in imap.path_allowlist)
        if blocked:
            # An explicit allowlist entry overrides a blocklist match.
            return allowed
        no_allowlists = not (imap.name_allowlist or imap.path_allowlist)
        return allowed or no_allowlists

    return check


def _compose_filters(first: _FilterFn, second: _FilterFn) -> _FilterFn:
    if first is None:
        return second
    if second is None:
        return first
    return lambda member: first(member) and second(member)


def _filter_allows(filter_fn: _FilterFn, member: Member) -> bool:
    return filter_fn is None or filter_fn(member)


@dataclass
class _Shared:
    """State shared across the whole resolution."""

    members: "Dict[str, Member]" = field(default_factory=dict)
    group_filter_q: deque = field(default_factory=deque)
    topdir: Optional[str] = None
    import_flags: ImportFlag = ImportFlag.DEFAULT
    importer: Optional[ImporterType] = None
    has_imports: bool = False


@dataclass(frozen=True)
class _Branch:
    """Per-recursion-branch import context."""

    origin: str
    #: The member whose git data this document came from, or None when it came from the manifest
    #: repository's filesystem.
    origin_member: Optional[Member]
    repo_abspath: Optional[str]
    imap_filter: _FilterFn
    path_prefix: PurePosixPath
    extension_commands_sink: List[str]
    cmake_packages_sink: List[str]
    depth: int
    where: str
    #: Ancestor chain of the documents being resolved on this branch, as (key, label) pairs, for
    #: cycle detection. Keys are (None, realpath) for filesystem documents and (member name,
    #: normalized path) for documents read from a member's git data.
    chain: Tuple[Tuple[Any, str], ...] = ()


@dataclass(frozen=True)
class _Defaults:
    remote: Optional[str]
    revision: str


def _resolve_url(
    md: dict, name: str, url_bases: dict, default_remote: Optional[str], where: str
) -> str:
    """Return the URL *md* declares as "url", or as "remote" plus "repo-path" (default *name*).

    Without either, the default remote applies. Shared by members and their "upstream" so that
    both spell a repository the same way.
    """
    url = md.get("url")
    remote = md.get("remote")
    repo_path = md.get("repo-path")
    if remote and url:
        raise MalformedManifest(f'{where} has both "remote: {remote}" and "url: {url}"')
    if default_remote and not (remote or url):
        remote = default_remote
    if url:
        if repo_path:
            raise MalformedManifest(f'{where} has both "repo-path: {repo_path}" and "url: {url}"')
        return url
    if remote:
        if remote not in url_bases:
            raise MalformedManifest(f"{where}: remote {remote} is not defined")
        return url_bases[remote].rstrip("/") + "/" + (repo_path or name)
    raise MalformedManifest(f"{where} has no remote or url and no default remote is set")


def _load_ref_patterns(raw: Optional[dict], default: RefPatterns) -> RefPatterns:
    if raw is None:
        return default
    return RefPatterns(
        tuple(raw.get("heads", default.heads)),
        tuple(raw.get("tags", default.tags)),
    )


def _same_repository(one: str, other: str) -> bool:
    """Say whether two git URLs are two spellings of the same repository.

    Only the spellings git itself treats as interchangeable are normalized away: a trailing "/" and
    the conventional ".git" suffix. Two URLs reaching one repository over different transports
    (scp-like "git@host:org/a" against "ssh://git@host/org/a") still read as different, so a True
    here is proof of sameness while a False is not proof of distinctness.
    """

    def normalized(url: str) -> str:
        trimmed = url.rstrip("/")
        return trimmed[: -len(".git")] if trimmed.endswith(".git") else trimmed

    return normalized(one) == normalized(other)


def _load_upstream(
    data: dict, name: str, member_url: str, url_bases: dict, defaults: _Defaults, where: str
) -> Upstream:
    here = f"{where}: upstream"
    url = _resolve_url(data, name, url_bases, defaults.remote, here)
    # Compared by repository, not by spelling: a member mirroring itself force-overwrites its own
    # refs, and with --prune deletes the ones a narrowed "mirror" leaves unselected. A trailing "/"
    # or a ".git" the two urls do not share must not be all it takes to get past this.
    if _same_repository(url, member_url):
        raise MalformedManifest(
            f"{here}: url {url} is the member's own url; "
            "an upstream must be a different repository"
        )
    return Upstream(
        url,
        mirror=_load_ref_patterns(data.get("mirror"), MIRROR_ALL),
        preserve=_load_ref_patterns(data.get("preserve"), PRESERVE_NONE),
    )


def _check_import_cycle(key, label: str, branch: _Branch) -> None:
    """Reject an import whose target is already being resolved.

    Only the ancestor chain counts: the same file imported on two sibling branches (a diamond) is
    legal, and duplicate members are handled by first-definition-wins.
    """
    for seen_key, _ in branch.chain:
        if seen_key == key:
            labels = [lbl for _, lbl in branch.chain] + [label]
            raise MalformedManifest("import cycle: " + " -> ".join(labels))


def _decode_import_data(member: Member, path: str, data: bytes) -> str:
    try:
        return data.decode(_ENCODING)
    except UnicodeDecodeError as err:
        raise MalformedManifest(
            f"{member.name_and_path}: import data {path} is not valid UTF-8: {err}"
        )


def _git_mode_at(member: Member, path: str, rev: str) -> str:
    """Return the git file mode of *path* at *rev*, or "" if unknown."""
    result = member.git(
        ["ls-tree", "-z", rev, "--", path],
        check=False,
        capture_stdout=True,
        capture_stderr=True,
    )
    if result.returncode != 0:
        return ""
    # Records are "<mode> <type> <object>\t<name>\0"; the name may be any byte sequence, the
    # metadata before the tab is always ASCII.
    meta = result.stdout.split(b"\0")[0].partition(b"\t")[0].split()
    return meta[0].decode("ascii", "replace") if meta else ""


def _symlinked_member_import(member: Member, path: str) -> MalformedManifest:
    return MalformedManifest(
        f"{member.name_and_path}: import path {path} is a symbolic link; "
        "symlinked manifest data is not allowed"
    )


def _symlinked_import(where: str, imp: str, file: Any) -> MalformedManifest:
    return MalformedManifest(
        f'{where}: "self: import: {imp}": {file} is a symbolic link; '
        "symlinked manifest data is not allowed"
    )


def member_manifest_content(member: Member, path: str) -> List[str]:
    """Read manifest content at *path* from a member's repospace-rev.

    A blob yields a single document; a tree yields every .yml/.yaml file in it, sorted by name.
    Raises FileNotFoundError if the path does not exist at repospace-rev, and CalledProcessError on
    other git failures (e.g. no repospace-rev ref yet).
    """
    spec = f"{QUAL_MANIFEST_REV}:{path}"
    result = member.git(
        ["cat-file", "-t", spec],
        check=False,
        capture_stdout=True,
        capture_stderr=True,
    )
    if result.returncode != 0:
        # Error message wording varies across git versions; ask git whether the ref itself exists
        # instead of parsing stderr.
        ref = member.git(
            ["rev-parse", "--verify", "--quiet", QUAL_MANIFEST_REV],
            check=False,
            capture_stdout=True,
            capture_stderr=True,
        )
        if ref.returncode != 0:
            # Always raises here: the status is known to be non-zero.
            result.check_returncode()
        raise FileNotFoundError(path)
    objtype = result.stdout.decode().strip()
    if objtype == "blob":
        # A committed symlink is a blob too, and its link target would be parsed as manifest data;
        # the filesystem side refuses one as well, so an import behaves the same by either route.
        if _git_mode_at(member, path, QUAL_MANIFEST_REV) == _SYMLINK_MODE:
            raise _symlinked_member_import(member, path)
        return [_decode_import_data(member, path, member.read_at(path))]
    if objtype == "tree":
        listing = _decode_import_data(
            member,
            path,
            member.git(["ls-tree", "-z", spec], capture_stdout=True).stdout,
        )
        names = []
        for entry in listing.split("\0"):
            meta, _, name = entry.partition("\t")
            # Only blobs: a subdirectory or submodule whose name ends in .yml/.yaml is not manifest
            # data (mirrors the is_file check on the filesystem side). Matched by suffix, not
            # endswith, also mirroring the filesystem side: a dotfile named just ".yml" has no
            # suffix.
            if PurePosixPath(name).suffix not in (".yml", ".yaml"):
                continue
            mode, kind = meta.split()[:2]
            if mode == _SYMLINK_MODE:
                raise _symlinked_member_import(member, f"{path}/{name}")
            if kind == "blob":
                names.append(name)
        names.sort()
        return [
            _decode_import_data(member, f"{path}/{name}", member.read_at(f"{path}/{name}"))
            for name in names
        ]
    raise MalformedManifest(
        f"{member.name_and_path}: import path {path} is a git object of "
        f'type "{objtype}"; expected a file or directory'
    )


def manifest_file(topdir: str, config: Optional[Configuration]) -> str:
    """Return the absolute path of the selected top-level manifest file.

    The manifest.file configuration option selects the file (default: repospace.yaml), as a relative
    path inside the manifest repository — the same confinement imports have.
    """
    name = MANIFEST_FILE
    if config is not None:
        name = config.get("manifest.file", MANIFEST_FILE) or MANIFEST_FILE
    if os.path.isabs(name):
        raise MalformedManifest(
            f'manifest.file "{name}" is an absolute path; it must be '
            "relative to the repospace topdir"
        )
    path = os.path.join(topdir, name)
    if util.escapes_directory(path, topdir):
        raise MalformedManifest(f'manifest.file "{name}" escapes the repospace topdir')
    return path


class Manifest:
    """A resolved manifest.

    Attributes:
      members:       resolved members; members[0] is the ManifestMember
      topdir:        repospace top-level directory, or None
      repo_abspath:  absolute path of the manifest repository, or None
      abspath:       absolute path of the top-level manifest file, or None
      yaml_name:     the top-level file's ``self: name:`` value, or None
      userdata:      the top-level file's ``self: userdata:`` value
      group_filter:  resolved group filter (sorted "-group" entries)
      has_imports:   True if any import was resolved, even one the
                     importer callback skipped; imports disabled by
                     ImportFlag do not count
    """

    @staticmethod
    def from_topdir(
        topdir: Optional[str] = None,
        config: Optional[Configuration] = None,
        import_flags: ImportFlag = ImportFlag.DEFAULT,
        importer: Optional[ImporterType] = None,
    ) -> "Manifest":
        """Load the repospace's top-level manifest.

        A repospace is always colocated with its manifest repository: the topdir is the manifest
        repository, marked by the .repospace/ directory. The manifest.file configuration option
        selects the manifest file (default: repospace.yaml).
        """
        if topdir is None:
            topdir = util.topdir()
        if config is None:
            config = Configuration(topdir)
        source_file = manifest_file(topdir, config)
        try:
            with open(source_file, encoding=_ENCODING) as f:
                source_data = f.read()
        except FileNotFoundError:
            raise MalformedManifest(f"manifest file not found: {source_file}")
        except OSError as err:
            raise MalformedManifest(f"cannot read manifest file: {source_file}: {err}")
        except UnicodeDecodeError as err:
            raise MalformedManifest(f"manifest file is not valid UTF-8: {source_file}: {err}")
        return Manifest(
            source_data,
            topdir=topdir,
            config=config,
            repo_abspath=topdir,
            abspath=os.path.abspath(source_file),
            import_flags=import_flags,
            importer=importer,
        )

    @staticmethod
    def from_file(
        source_file: util.PathType,
        topdir: Optional[str] = None,
        import_flags: ImportFlag = ImportFlag.DEFAULT,
        importer: Optional[ImporterType] = None,
    ) -> "Manifest":
        """Load a manifest from an explicit file path."""
        source_file = os.path.abspath(os.fspath(source_file))
        if topdir is None:
            try:
                topdir = util.topdir(os.path.dirname(source_file))
            except util.RepospaceNotFound:
                topdir = None
        # A repospace is always colocated with its manifest repository, so inside a repospace the
        # manifest repository is the topdir even when the file lives in a subdirectory — the
        # anchoring from_topdir uses. Outside any repospace, the file's directory is the best
        # available anchor.
        repo_abspath = topdir if topdir is not None else os.path.dirname(source_file)
        try:
            with open(source_file, encoding=_ENCODING) as f:
                source_data = f.read()
        except FileNotFoundError:
            raise MalformedManifest(f"manifest file not found: {source_file}")
        except OSError as err:
            raise MalformedManifest(f"cannot read manifest file: {source_file}: {err}")
        except UnicodeDecodeError as err:
            raise MalformedManifest(f"manifest file is not valid UTF-8: {source_file}: {err}")
        return Manifest(
            source_data,
            topdir=topdir,
            repo_abspath=repo_abspath,
            abspath=source_file,
            import_flags=import_flags,
            importer=importer,
        )

    @staticmethod
    def from_data(
        source_data: Union[str, dict],
        topdir: Optional[str] = None,
        import_flags: ImportFlag = ImportFlag.DEFAULT,
        importer: Optional[ImporterType] = None,
    ) -> "Manifest":
        """Load a manifest from in-memory data (no manifest repository)."""
        return Manifest(
            source_data,
            topdir=topdir,
            import_flags=import_flags,
            importer=importer,
        )

    def __init__(
        self,
        source_data: Union[str, dict],
        *,
        topdir: Optional[str] = None,
        config: Optional[Configuration] = None,
        repo_abspath: Optional[str] = None,
        abspath: Optional[str] = None,
        import_flags: ImportFlag = ImportFlag.DEFAULT,
        importer: Optional[ImporterType] = None,
    ):
        self.topdir = topdir
        self.repo_abspath = repo_abspath
        self.abspath = abspath
        self.yaml_name: Optional[str] = None
        self.userdata: Any = None
        self._config = config
        self._config_filter: Optional[List[str]] = None

        where = abspath or "manifest data"
        mdata = validate(source_data, where)

        shared = _Shared(topdir=topdir, import_flags=import_flags, importer=importer)
        chain: Tuple[Tuple[Any, str], ...] = ()
        if abspath is not None:
            chain = (((None, os.path.realpath(abspath)), abspath),)
        top_branch = _Branch(
            origin=MANIFEST_MEMBER_NAME,
            origin_member=None,
            repo_abspath=repo_abspath,
            imap_filter=None,
            path_prefix=PurePosixPath("."),
            extension_commands_sink=[],
            cmake_packages_sink=[],
            depth=0,
            where=where,
            chain=chain,
        )
        self._shared = shared
        self._load_file(mdata, shared, top_branch, top_level=True)

        self.has_imports = shared.has_imports
        self.group_filter = self._final_group_filter(shared.group_filter_q)

        # A repospace is always colocated with its manifest repository, so with a topdir the
        # manifest repository is the topdir itself, whichever route the data came by.
        manifest_member = ManifestMember(
            path="." if topdir else None,
            topdir=topdir,
            extension_commands=top_branch.extension_commands_sink,
            cmake_packages=top_branch.cmake_packages_sink,
            userdata=self.userdata,
        )
        self.members: List[Member] = list(shared.members.values())
        self.members.insert(MANIFEST_MEMBER_INDEX, manifest_member)
        self._members_by_name: Dict[str, Member] = {MANIFEST_MEMBER_NAME: manifest_member}
        self._members_by_name.update(shared.members)

        self._check_paths_are_unique()
        self._check_paths_are_confined()

    # -- public API --------------------------------------------------------

    def get_members(
        self,
        member_ids,
        allow_paths: bool = True,
    ) -> List[Member]:
        """Return members for a list of names (or paths); all if empty."""
        if not member_ids:
            return list(self.members)
        members = []
        unknown = []
        for mid in member_ids:
            member = self._members_by_name.get(mid)
            if member is None and allow_paths:
                member = self._member_by_path(mid)
            if member is None:
                unknown.append(mid)
            else:
                members.append(member)
        if unknown:
            raise ValueError(unknown)
        return members

    def _member_by_path(self, value: str) -> Optional[Member]:
        if self.topdir is None:
            return None
        # realpath, like Member.abspath, so a repospace reached through a symlink above its topdir
        # still matches.
        target = Path(os.path.realpath(value))
        for member in self.members:
            if member.abspath and Path(member.abspath) == target:
                return member
        return None

    def is_active(self, member: Member, extra_filter=None) -> bool:
        """Return True unless all of the member's groups are disabled.

        *extra_filter* is an optional list of "+group"/"-group" entries applied after the manifest's
        own filter and the configured one. An entry that is empty or does not begin with "+" or "-"
        raises ValueError.
        """
        for entry in extra_filter or []:
            if not entry or entry[0] not in "+-":
                raise ValueError(
                    f'invalid group filter entry "{entry}"; entries must begin with "+" or "-"'
                )
        if not member.groups:
            return True
        disabled = set()
        for entry in self.group_filter:
            _apply_filter_entry(disabled, entry)
        for entry in self._config_group_filter():
            _apply_filter_entry(disabled, entry)
        for entry in extra_filter or []:
            _apply_filter_entry(disabled, entry)
        return any(group not in disabled for group in member.groups)

    def _config_group_filter(self) -> List[str]:
        """Parse the manifest.group-filter configuration option.

        Parsed once per Manifest: is_active runs once per grouped member per command, and an invalid
        item must be warned about once, not once per call.
        """
        if self._config_filter is None:
            entries = []
            raw = self._config.get("manifest.group-filter") if self._config is not None else None
            for item in str(raw).split(",") if raw else []:
                item = item.strip()
                if not item:
                    continue
                if item[0] not in "+-" or not is_group(item[1:]):
                    _logger.warning(f'invalid manifest.group-filter item "{item}" ignored')
                    continue
                entries.append(item)
            self._config_filter = entries
        return self._config_filter

    def as_dict(self, active_only: bool = False) -> dict:
        """Return resolved manifest data, free of imports."""
        members = [
            member.as_dict()
            for member in self.members[MANIFEST_MEMBER_INDEX + 1 :]
            if not active_only or self.is_active(member)
        ]
        mdata: Dict[str, Any] = {"version": SCHEMA_VERSION}
        if self.group_filter:
            mdata["group-filter"] = list(self.group_filter)
        mdata["members"] = members
        self_dict: Dict[str, Any] = {}
        if self.yaml_name:
            self_dict["name"] = self.yaml_name
        self_dict.update(self.members[MANIFEST_MEMBER_INDEX].as_dict())
        if self_dict:
            mdata["self"] = self_dict
        return {"manifest": mdata}

    def as_frozen_dict(self, active_only: bool = False) -> dict:
        """as_dict() with every revision pinned to its repospace-rev SHA."""
        data = self.as_dict(active_only=active_only)
        by_name = self._members_by_name
        for md in data["manifest"]["members"]:
            member = by_name[md["name"]]
            try:
                md["revision"] = member.sha(QUAL_MANIFEST_REV, capture_stderr=True)
            except (subprocess.CalledProcessError, RuntimeError, OSError):
                if not self.is_active(member):
                    # Plain "repospace update" skips inactive members, so the generic advice below
                    # would be a dead end.
                    raise RuntimeError(
                        f"cannot freeze: inactive member "
                        f"{member.name_and_path} is not cloned or has no "
                        f'repospace-rev, and plain "repospace update" '
                        f'skips inactive members; run "repospace update '
                        f'{member.name}" to clone it, or freeze active '
                        "members only (--active-only)"
                    )
                raise RuntimeError(
                    f"cannot freeze: member {member.name_and_path} is not "
                    "cloned or has no repospace-rev; run "
                    '"repospace update" first'
                )
        return data

    def as_yaml(self, **kwargs) -> str:
        return yaml.safe_dump(self.as_dict(**kwargs), sort_keys=False, default_flow_style=False)

    def as_frozen_yaml(self, **kwargs) -> str:
        return yaml.safe_dump(
            self.as_frozen_dict(**kwargs),
            sort_keys=False,
            default_flow_style=False,
        )

    # -- loading -----------------------------------------------------------

    def _load_file(
        self,
        mdata: dict,
        shared: _Shared,
        branch: _Branch,
        top_level: bool = False,
    ) -> None:
        if branch.depth > _MAX_IMPORT_DEPTH:
            # Attribute the failure to the member whose git data this document came from; None
            # (rendered "self") only for documents read from the manifest repository itself.
            raise ManifestImportFailed(branch.origin_member, branch.where, "import level too deep")

        self._load_self(mdata, shared, branch, top_level)
        self._load_group_filter(mdata, shared, branch)

        url_bases = {}
        for remote in mdata.get("remotes") or []:
            url_bases[remote["name"]] = remote["url-base"]
        defaults = self._load_defaults(mdata.get("defaults") or {}, url_bases, branch)
        self._load_members(mdata, shared, branch, url_bases, defaults)

    def _load_self(self, mdata: dict, shared: _Shared, branch: _Branch, top_level: bool) -> None:
        slf = mdata.get("self")
        if not slf:
            return

        if top_level:
            self.yaml_name = slf.get("name")
            self.userdata = slf.get("userdata")

        imp = slf.get("import")
        if imp is not None:
            self._import_from_self(imp, shared, branch)

        # Self-imported values were merged into the sinks above; the current file's own values come
        # after them, so self-imports take precedence.
        branch.extension_commands_sink[:] = _merge_unique(
            branch.extension_commands_sink,
            _str_list(slf.get("extension-commands")),
        )
        branch.cmake_packages_sink[:] = _merge_unique(
            branch.cmake_packages_sink, _str_list(slf.get("cmake-packages"))
        )

    def _import_from_self(self, imp, shared: _Shared, branch: _Branch, _seen=frozenset()):
        if isinstance(imp, bool):
            raise MalformedManifest(f'{branch.where}: got "self: import: {imp}" of boolean')
        if shared.import_flags & ImportFlag.IGNORE:
            return
        if branch.origin_member is None and branch.repo_abspath is None:
            raise ManifestImportFailed(None, imp, "no manifest repository to import from")
        shared.has_imports = True
        if isinstance(imp, str):
            if not imp:
                raise MalformedManifest(
                    f'{branch.where}: "self: import" is empty; remove the key or set a value'
                )
            self._import_path_from_self(imp, shared, branch)
        elif isinstance(imp, list):
            # A YAML alias can make a sequence contain itself; like _check_import_cycle, only the
            # ancestor chain counts, so a diamond (the same anchored list aliased twice as siblings)
            # stays legal.
            if id(imp) in _seen:
                raise MalformedManifest(
                    f'{branch.where}: "self: import" contains a recursive YAML alias'
                )
            for subimp in imp:
                self._import_from_self(subimp, shared, branch, _seen | {id(imp)})
        elif isinstance(imp, dict):
            imap = _load_import_map(imp, branch.where)
            child = replace(
                branch,
                imap_filter=_compose_filters(branch.imap_filter, _import_map_filter(imap)),
                path_prefix=branch.path_prefix / imap.path_prefix,
            )
            self._import_path_from_self(imap.file, shared, child)
        else:
            raise MalformedManifest(
                f'{branch.where}: "self: import: {imp}" has invalid type {type(imp).__name__}'
            )

    def _import_path_from_self(self, imp: str, shared: _Shared, branch: _Branch) -> None:
        pathobj = Path(imp)
        if pathobj.is_absolute():
            raise MalformedManifest(f"{branch.where}: self import {imp} is an absolute path")
        if branch.origin_member is not None:
            self._import_member_self_path(imp, shared, branch)
            return
        target = Path(branch.repo_abspath) / pathobj
        # Manifest data is never read through a symlink, whichever route it comes by: git stores a
        # symlink as a blob, so on the git side a linked file's target text would be parsed as YAML
        # and a path through a linked directory would not resolve at all, and the two routes must
        # not disagree about the same repository. Every component below the repository root is
        # checked, as _check_paths_are_confined does for member paths; the root itself and anything
        # above it is the user's placement.
        current = Path(branch.repo_abspath)
        for part in pathobj.parts:
            current = current / part
            if current.is_symlink():
                raise _symlinked_import(branch.where, imp, current)
        if util.escapes_directory(target, branch.repo_abspath):
            raise MalformedManifest(
                f'{branch.where}: "self: import: {imp}": path escapes the manifest repository'
            )
        # An unreadable path raises OSError, not the "does it exist" False that the query methods
        # give for a missing one; wrap it like the top-level manifest read, so callers catching
        # MalformedManifest cover it (a permission problem must not become a traceback).
        try:
            if target.is_file():
                files = [target]
            elif target.is_dir():
                # Symlinks are collected too, though they are refused below: skipping them here
                # would silently ignore a link the git route reports as an error.
                files = sorted(
                    child
                    for child in target.iterdir()
                    if child.suffix in (".yml", ".yaml") and (child.is_symlink() or child.is_file())
                )
            else:
                hint = (
                    "this is neither a file nor a directory"
                    if target.exists()
                    else "file not found"
                )
                raise MalformedManifest(f'{branch.where}: "self: import: {imp}": {hint}')
        except OSError as err:
            raise MalformedManifest(f'{branch.where}: "self: import: {imp}": cannot read: {err}')
        for file in files:
            if file.is_symlink():
                raise _symlinked_import(branch.where, imp, file)
            # A file inside an imported directory could still lie outside the repository (through a
            # mount, say); check each file, not just the directory.
            if util.escapes_directory(file, branch.repo_abspath):
                raise MalformedManifest(
                    f'{branch.where}: "self: import: {imp}": {file} '
                    "escapes the manifest repository"
                )
            key = (None, os.path.realpath(file))
            _check_import_cycle(key, str(file), branch)
            child = replace(
                branch,
                depth=branch.depth + 1,
                where=str(file),
                chain=branch.chain + ((key, str(file)),),
            )
            try:
                text = file.read_text(encoding=_ENCODING)
            except (OSError, UnicodeDecodeError) as err:
                raise MalformedManifest(
                    f'{branch.where}: "self: import: {imp}": cannot read {file}: {err}'
                )
            child_mdata = validate(text, str(file))
            self._load_file(child_mdata, shared, child)

    def _import_member_self_path(self, imp: str, shared: _Shared, branch: _Branch) -> None:
        """Resolve a self-import declared by a member-imported manifest.

        The importing document came from the member's git data at repospace-rev, so the imported
        files are read from there too — never from the member's working tree, which may be checked
        out somewhere else entirely.
        """
        member = branch.origin_member
        norm = posixpath.normpath(imp)
        if PurePosixPath(norm).parts[:1] == ("..",):
            raise MalformedManifest(
                f'{branch.where}: "self: import: {imp}": path escapes the member repository'
            )
        key = (member.name, norm)
        where = f"{norm} (self-imported from member {member.name})"
        _check_import_cycle(key, where, branch)
        content = self._member_import_content(member, norm, shared)
        if content is None:
            return
        for document in content:
            child = replace(
                branch,
                depth=branch.depth + 1,
                where=where,
                chain=branch.chain + ((key, where),),
            )
            child_mdata = validate(document, where)
            self._load_file(child_mdata, shared, child)

    def _load_group_filter(self, mdata: dict, shared: _Shared, branch: _Branch) -> None:
        raw = mdata.get("group-filter")
        if not raw:
            return
        entries = []
        for item in raw:
            if isinstance(item, (int, float)) and not isinstance(item, bool):
                # YAML parses signed numbers: a filter written as -1 arrives here as the integer -1,
                # sign already consumed.
                raise MalformedManifest(
                    f"{branch.where}: group filter contains numeric item "
                    f'"{item}"; this must begin with "+" or "-"; '
                    "do you need to quote the value?"
                )
            item = str(item)
            if not item or item[0] not in "+-":
                raise MalformedManifest(
                    f"{branch.where}: group filter contains invalid item "
                    f'"{item}"; this must begin with "+" or "-"'
                )
            if not is_group(item[1:]):
                raise MalformedManifest(
                    f"{branch.where}: group filter contains invalid item "
                    f'"{item}"; "{item[1:]}" is an invalid group name'
                )
            entries.append(item)
        # Filters from files loaded later end up earlier in the queue; final application iterates
        # left to right with later entries winning, so the load hierarchy's precedence is preserved
        # (self-imports beat the top level, which beats member imports).
        shared.group_filter_q.appendleft(entries)

    @staticmethod
    def _final_group_filter(queue: deque) -> List[str]:
        disabled: set = set()
        for entries in queue:
            for entry in entries:
                _apply_filter_entry(disabled, entry)
        return [f"-{g}" for g in sorted(disabled)]

    def _load_defaults(self, defaults: dict, url_bases: dict, branch: _Branch) -> _Defaults:
        remote = defaults.get("remote")
        if remote and remote not in url_bases:
            raise MalformedManifest(f"{branch.where}: default remote {remote} is not defined")
        revision = defaults.get("revision", _DEFAULT_REVISION)
        return _Defaults(remote, revision)

    def _load_members(
        self,
        mdata: dict,
        shared: _Shared,
        branch: _Branch,
        url_bases: dict,
        defaults: _Defaults,
    ) -> None:
        pending_imports = []
        names = set()
        for md in mdata.get("members") or []:
            member = self._load_member(md, shared, branch, url_bases, defaults)
            name = member.name

            # Duplicates are checked before the import filter: a file declaring the same name twice
            # is malformed regardless of which occurrences an importing manifest happens to keep.
            if name in names:
                raise MalformedManifest(f"{branch.where}: member name {name} used twice")
            names.add(name)

            if not _filter_allows(branch.imap_filter, member):
                _logger.debug(
                    f"member {name} in {branch.where} ignored: an importing "
                    "manifest blocked or did not allow it"
                )
                continue

            if name in shared.members:
                # First definition wins; this one is ignored, and its import (if any) is never
                # processed.
                _logger.debug(f"member {name} in {branch.where} ignored: already defined")
                continue
            shared.members[name] = member

            imp = md.get("import")
            if _import_requested(imp):
                if shared.import_flags & (ImportFlag.IGNORE | ImportFlag.IGNORE_MEMBERS):
                    continue
                pending_imports.append((member, imp))

        for member, imp in pending_imports:
            self._import_from_member(member, imp, shared, branch)

    def _load_member(
        self,
        md: dict,
        shared: _Shared,
        branch: _Branch,
        url_bases: dict,
        defaults: _Defaults,
    ) -> Member:
        name = md["name"]
        where = f"{branch.where}: member {name}"

        if name == MANIFEST_MEMBER_NAME:
            raise MalformedManifest(
                f'{branch.where}: no member can be named "{MANIFEST_MEMBER_NAME}"; '
                "the name is reserved for the manifest repository"
            )
        if "/" in name or "\\" in name:
            raise MalformedManifest(
                f'{branch.where}: member name "{name}" contains a path separator'
            )
        if any(c.isspace() or c == "," for c in name):
            _logger.warning(
                f'{branch.where}: member name "{name}" contains whitespace '
                "or a comma; this is discouraged"
            )

        url = _resolve_url(md, name, url_bases, defaults.remote, where)
        upstream = None
        if "upstream" in md:
            upstream = _load_upstream(md["upstream"], name, url, url_bases, defaults, where)

        # A path-prefix in the member's own "import" scopes only to the imported content; the
        # member's placement is its declaring file's prefix plus "path". The result is normalized
        # here so that the checks below, the placement, and as_dict() all speak of the same
        # directory: "ext/a/.." is the directory "ext".
        imp = md.get("import")
        declared_path = md.get("path", name)
        path = posixpath.normpath((branch.path_prefix / declared_path).as_posix())

        raw_groups = md.get("groups") or []
        for group in raw_groups:
            if not is_group(group):
                hint = (
                    "; do you need to quote the value?"
                    if isinstance(group, (int, float)) and not isinstance(group, bool)
                    else ""
                )
                raise MalformedManifest(f'{where}: invalid group "{group}"{hint}')
        groups = list(raw_groups)

        if _import_requested(imp) and groups:
            raise MalformedManifest(f'{where}: "groups" cannot be combined with "import"')

        revision = md.get("revision", defaults.revision)

        member = Member(
            name,
            url,
            revision=revision,
            path=path,
            submodules=self._load_submodules(md.get("submodules"), where),
            clone_depth=md.get("clone-depth"),
            extension_commands=md.get("extension-commands"),
            cmake_packages=md.get("cmake-packages"),
            topdir=shared.topdir,
            groups=groups,
            userdata=md.get("userdata"),
            description=md.get("description"),
            declared_by=branch.origin,
            upstream=upstream,
        )

        # Purely lexical path checks; the filesystem is consulted later, by
        # _check_paths_are_confined. Member paths are POSIX, so normalize with posixpath:
        # os.path.normpath would switch to backslashes on Windows and defeat the component checks
        # below.
        if "\\" in member.path:
            raise MalformedManifest(
                f"{where} path {member.path} contains a backslash; "
                "member paths use forward slashes"
            )
        norm = posixpath.normpath(member.path)
        if norm.startswith("/") or os.path.isabs(norm):
            raise MalformedManifest(
                f"{where} has absolute path {member.path}; this must be "
                "relative to the repospace topdir"
            )
        if norm[1:2] == ":":
            # On Windows a drive-relative path like "C:evil" is not "absolute", yet joining it
            # discards the topdir entirely.
            raise MalformedManifest(
                f"{where} path {member.path} begins with a drive letter; "
                "this must be relative to the repospace topdir"
            )
        if norm == ".":
            raise MalformedManifest(f"{where} path {declared_path} is the repospace topdir itself")
        prefix = posixpath.normpath(branch.path_prefix.as_posix())
        if norm == prefix:
            # Same mistake one level down: under an import path-prefix, "path: ." names the prefix
            # directory, not a member of its own.
            raise MalformedManifest(
                f"{where} path {declared_path} is the import path-prefix "
                f"directory {prefix} itself"
            )
        # The first component, not a string prefix: "..foo" is a valid directory name.
        if PurePosixPath(norm).parts[0] == "..":
            raise MalformedManifest(
                f"{where} path {declared_path} normalizes to {norm}, "
                "which escapes the repospace topdir"
            )
        if PurePosixPath(norm).parts[0] == util.REPOSPACE_DIR:
            raise MalformedManifest(
                f"{where} path {member.path} is inside the {util.REPOSPACE_DIR} directory"
            )
        # Any component named ".git" (in any case: some filesystems are case-insensitive) could
        # place member content where git treats it as a git directory — hooks included, which git
        # executes on the next command. Git itself refuses to track such paths.
        if any(p.lower() == ".git" for p in PurePosixPath(norm).parts):
            raise MalformedManifest(f'{where} path {member.path} contains a ".git" component')
        return member

    def _load_submodules(self, value, where: str):
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, list):
            result = []
            for index, item in enumerate(value):
                ok = (
                    isinstance(item, dict)
                    and isinstance(item.get("path"), str)
                    and (item.get("name") is None or isinstance(item["name"], str))
                    and not (set(item) - {"path", "name"})
                )
                if not ok:
                    raise MalformedManifest(
                        f"{where}: invalid submodule element {item} at index {index}"
                    )
                result.append(Submodule(item["path"], item.get("name")))
            return result
        raise MalformedManifest(f"{where}: invalid submodules: {value}; expected a list or boolean")

    def _import_from_member(
        self,
        member: Member,
        imp,
        shared: _Shared,
        branch: _Branch,
        _seen=frozenset(),
    ) -> None:
        shared.has_imports = True
        if isinstance(imp, bool):
            if not imp:
                raise MalformedManifest(
                    f'{branch.where}: member {member.name}: falsy "import" inside a sequence'
                )
            self._import_files_from_member(member, MANIFEST_FILE, shared, branch, None)
        elif isinstance(imp, str):
            if not imp:
                raise MalformedManifest(
                    f'{branch.where}: member {member.name}: "import" is '
                    "empty; remove the key or set a value"
                )
            self._import_files_from_member(member, imp, shared, branch, None)
        elif isinstance(imp, list):
            # See _import_from_self: reject a sequence that contains itself through a YAML alias;
            # sibling aliases stay legal.
            if id(imp) in _seen:
                raise MalformedManifest(
                    f'{branch.where}: member {member.name}: "import" '
                    "contains a recursive YAML alias"
                )
            for subimp in imp:
                self._import_from_member(member, subimp, shared, branch, _seen | {id(imp)})
        elif isinstance(imp, dict):
            imap = _load_import_map(imp, f"{branch.where}: member {member.name}")
            self._import_files_from_member(member, imap.file, shared, branch, imap)
        else:
            raise MalformedManifest(
                f"{branch.where}: member {member.name}: invalid import "
                f"{imp} of type {type(imp).__name__}"
            )

    def _import_files_from_member(
        self,
        member: Member,
        path: str,
        shared: _Shared,
        branch: _Branch,
        imap: Optional[_ImportMap],
    ) -> None:
        # Confined like a self-import path: git would refuse to read an absolute or escaping path
        # anyway, and the read failure would then be reported as a stale repospace-rev, advising an
        # update that cannot help.
        if posixpath.isabs(path) or os.path.isabs(path):
            raise MalformedManifest(
                f'{branch.where}: member {member.name}: "import: {path}" is an absolute path'
            )
        norm = posixpath.normpath(path)
        if PurePosixPath(norm).parts[:1] == ("..",):
            raise MalformedManifest(
                f'{branch.where}: member {member.name}: "import: {path}": '
                "path escapes the member repository"
            )
        key = (member.name, norm)
        where = f"{path} (imported from member {member.name})"
        _check_import_cycle(key, where, branch)
        # Read at the normalized path, as _import_member_self_path does: git does not normalize an
        # inner ".." in a <rev>:<path> spec, and the failure would be reported as a stale
        # repospace-rev.
        content = self._member_import_content(member, norm, shared)
        if content is None:
            return

        if imap is not None:
            imap_filter = _compose_filters(branch.imap_filter, _import_map_filter(imap))
            prefix = branch.path_prefix / imap.path_prefix
        else:
            imap_filter = branch.imap_filter
            prefix = branch.path_prefix

        for document in content:
            child = _Branch(
                origin=member.name,
                origin_member=member,
                repo_abspath=member.abspath,
                imap_filter=imap_filter,
                path_prefix=prefix,
                # Extension commands and CMake packages declared under the imported manifest's
                # "self:" logically belong to the member; collect them separately and attach below.
                extension_commands_sink=[],
                cmake_packages_sink=[],
                depth=branch.depth + 1,
                where=where,
                chain=branch.chain + ((key, where),),
            )
            child_mdata = validate(document, where)
            self._load_file(child_mdata, shared, child)
            member.extension_commands = _merge_unique(
                member.extension_commands, child.extension_commands_sink
            )
            member.cmake_packages = _merge_unique(member.cmake_packages, child.cmake_packages_sink)

    def _member_import_content(
        self, member: Member, path: str, shared: _Shared
    ) -> Optional[List[str]]:
        content: Union[None, str, List[str]]
        cloned = member.is_cloned()
        use_git = not (shared.import_flags & ImportFlag.FORCE_MEMBERS) and cloned
        # The local repospace-rev may be missing or stale; on failure, give the importer a chance to
        # fix that. The reason strings only surface when there is no importer, so each failure mode
        # keeps its own diagnosis instead of one catch-all message.
        if use_git:
            try:
                content = member_manifest_content(member, path)
            except FileNotFoundError:
                content = self._call_importer(
                    member,
                    path,
                    shared,
                    f'not found at {QUAL_MANIFEST_REV}; run "repospace update"',
                )
            except subprocess.CalledProcessError:
                content = self._call_importer(
                    member,
                    path,
                    shared,
                    f'member has no {QUAL_MANIFEST_REV}; run "repospace update"',
                )
        elif not cloned:
            content = self._call_importer(
                member,
                path,
                shared,
                'member is not cloned; run "repospace update"',
            )
        else:
            # Cloned, but FORCE_MEMBERS routes everything through the importer; reachable without
            # one only through API misuse.
            content = self._call_importer(member, path, shared, "no importer callback was provided")
        if content is None:
            return None
        if isinstance(content, str):
            return [content]
        return content

    @staticmethod
    def _call_importer(member: Member, path: str, shared: _Shared, reason: str):
        if shared.importer is None:
            raise ManifestImportFailed(member, path, reason)
        return shared.importer(member, path)

    def _check_paths_are_unique(self) -> None:
        seen: Dict[str, Member] = {}
        for member in self.members:
            if member.path is None:
                continue
            norm = posixpath.normpath(member.path)
            other = seen.get(norm)
            if other is not None:
                raise MalformedManifest(
                    f'member {member.name} path "{member.path}" is taken by {other.name}'
                )
            seen[norm] = member

    def _check_paths_are_confined(self) -> None:
        """Reject member paths that leave the repospace on disk.

        The lexical checks in _load_member stop textual escapes; this pass consults the filesystem.
        Member checkouts live in real directories inside the repospace: no existing component of a
        member path below the topdir may be a symlink — the member directory itself included — so
        that neither a link committed in a member nor one committed in the manifest repository can
        redirect a checkout elsewhere. Components that do not exist yet are fine; "update" creates
        them as directories. The topdir and anything above it may be symlinks: that placement is the
        user's own doing, not something manifest data can steer.

        A second pass keeps the resolved check for paths nested inside another member, which also
        catches an escape a symlink did not cause (a mount point, say).
        """
        if self.topdir is None:
            return
        for member in self.members:
            if member.path is None or isinstance(member, ManifestMember):
                continue
            current = self.topdir
            for part in PurePosixPath(posixpath.normpath(member.path)).parts:
                current = os.path.join(current, part)
                if os.path.islink(current):
                    raise MalformedManifest(
                        f'member {member.name} path "{member.path}": '
                        f"{current} is a symbolic link; a member path may "
                        "not go through one inside the repospace"
                    )
        owners: Dict[str, Member] = {}
        for member in self.members:
            if member.path is None or isinstance(member, ManifestMember):
                continue
            owners[posixpath.normpath(member.path)] = member
        rdir = os.path.join(self.topdir, util.REPOSPACE_DIR)
        for member in self.members:
            if member.path is None or isinstance(member, ManifestMember):
                continue
            norm = PurePosixPath(posixpath.normpath(member.path))
            owner = None
            for parent in norm.parents:
                owner = owners.get(parent.as_posix())
                if owner is not None:
                    break
            if owner is None:
                continue
            if util.escapes_directory(member.abspath, self.topdir) or not util.escapes_directory(
                member.abspath, rdir
            ):
                raise MalformedManifest(
                    f'member {member.name} path "{member.path}" is inside '
                    f'member {owner.name} path "{owner.path}" and resolves '
                    f"to {member.abspath}, outside the repospace or inside "
                    f"{util.REPOSPACE_DIR}; a symlink committed inside a "
                    "member cannot redirect other members"
                )


def _apply_filter_entry(disabled: set, entry: str) -> None:
    group = entry[1:]
    if entry[0] == "-":
        disabled.add(group)
    else:
        disabled.discard(group)
