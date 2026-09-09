"""Repospace location and miscellaneous helpers."""

from __future__ import annotations

import functools
import os
import pathlib
import re
import shlex
from typing import Optional, Sequence, Tuple, Union

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


def _class_escape(char: str) -> str:
    """Escape one character for use as a literal inside a regular expression class.

    Everything the re module reads as syntax there is escaped: "\\", "[", "]", "^", "-", and the
    characters of the set operators "&&", "~~", "||", which re rejects or warns about.
    """
    return "\\" + char if char in "\\[]^-&~|" else char


def _translate_class(body: str) -> str:
    """Translate the inside of a "[...]" bracket expression to a regular expression class.

    A "-" between two characters is a range; one at either end is a literal. A reversed range
    ("[z-a]") holds nothing and is dropped, as in a shell glob. Every other character becomes a
    literal, so no pattern can produce a malformed regular expression.
    """
    negate = body[:1] == "!"
    if negate:
        body = body[1:]
    items = []
    i, n = 0, len(body)
    while i < n:
        if i + 2 < n and body[i + 1] == "-":
            start, end = body[i], body[i + 2]
            i += 3
            if start <= end:
                items.append(f"{_class_escape(start)}-{_class_escape(end)}")
            continue
        items.append(_class_escape(body[i]))
        i += 1
    if not items:
        # An empty class holds nothing, so it matches nothing; its negation matches any character
        # a negated class may match.
        return "[^/]" if negate else "(?!)"
    inner = "".join(items)
    if negate:
        # A negated class matches within one path component only.
        return f"[^/{inner}]"
    return f"[{inner}]"


def _translate_segment(segment: str) -> str:
    """Translate one "/"-free segment of a ref pattern to a regular expression."""
    out = []
    i, n = 0, len(segment)
    while i < n:
        char = segment[i]
        i += 1
        if char == "*":
            if not out or out[-1] != "[^/]*":
                out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            j = i
            if j < n and segment[j] == "!":
                j += 1
            if j < n and segment[j] == "]":
                j += 1
            while j < n and segment[j] != "]":
                j += 1
            if j >= n:
                out.append("\\[")
            else:
                out.append(_translate_class(segment[i:j]))
                i = j + 1
        else:
            out.append(re.escape(char))
    return "".join(out)


def translate_ref_pattern(pattern: str) -> str:
    """Return the regular expression matching the whole of a ref name against *pattern*.

    The pattern language is gitignore's, applied to the name below refs/heads/ or refs/tags/: "*"
    and "?" match within one "/"-separated component, "**" spans any number of components (also
    none), "[...]" is a character class and "[!...]" its negation, which also excludes "/", and the
    pattern must match the entire name. An unclosed "[" is a literal, as in a shell glob.

    The translation is written out here, and belongs to repospace rather than to a Python release:
    the standard library's glob.translate() only exists from 3.13 on (repospace supports 3.12), and
    it has no notion of a "/" a negated class must not match.
    """
    segments = pattern.split("/")
    last = len(segments) - 1
    out = []
    for index, segment in enumerate(segments):
        if segment == "**":
            if index < last:
                if segments[index + 1] != "**":
                    out.append("(?:.+/)?")
            else:
                out.append(".*")
        else:
            out.append(_translate_segment(segment))
            if index < last:
                out.append("/")
    return "(?s:" + "".join(out) + r")\Z"


# Bounded, and it memoizes the translation rather than the compilation: re.compile keeps a cache of
# its own, but keyed on the regular expression, so it would still leave translate_ref_pattern
# walking the pattern once per ref tested against it -- thousands of times over a repository's tags.
# The keys come from manifests, and a manifest holds a handful of patterns, so the limit is a bound
# on a cache that would otherwise never evict, not a size the normal case comes near.
@functools.lru_cache(maxsize=256)
def _compile_ref_pattern(pattern: str) -> "re.Pattern[str]":
    return re.compile(translate_ref_pattern(pattern))


def ref_pattern_match(pattern: str, name: str) -> bool:
    """Return True if the ref *name* matches *pattern*; see translate_ref_pattern.

    One pattern, matched as written: a leading "!" is a literal here, and only ref_patterns_match
    reads it as the exclusion marker.
    """
    return _compile_ref_pattern(pattern).match(name) is not None


#: What marks a pattern as an exclusion, as the same character does in a gitignore file.
NEGATION = "!"


def split_ref_pattern(pattern: str) -> Tuple[bool, str]:
    """Split a leading "!" off *pattern*: return whether it was there, and the pattern below it.

    Only the first character is the marker, and what follows it is an ordinary pattern in which "!"
    is a literal, so "!!x" excludes the ref named "!x". A pattern that selects has no equivalent
    spelling, since the language has no escape character (a backslash is refused: no refname holds
    one). A ref whose name begins with "!" is therefore selected only by a pattern that reaches it
    with a wildcard -- a name no repository is expected to carry, and the cost of keeping the
    marker readable at a glance.
    """
    if pattern.startswith(NEGATION):
        return True, pattern[len(NEGATION) :]
    return False, pattern


def ref_patterns_match(patterns: Sequence[str], name: str) -> bool:
    """Return True if *patterns* select the ref *name*; see translate_ref_pattern for one pattern.

    The last pattern matching *name* is the one that decides, as in a gitignore file: a plain
    pattern selects what it matches, a "!" pattern unselects it, and a later entry overrides an
    earlier one. So ["**", "!release/*"] is every name except those below "release/", and
    ["**", "!release/*", "release/1.0"] is that same selection with the one name put back.

    A name that no pattern matches is not selected, and neither is one whose last match is an
    exclusion: an exclusion only ever takes away, so a list holding nothing else selects nothing,
    as does an empty list.

    The scan runs back to front and stops at the first match it finds, which is the last one there
    is: the patterns before it cannot change what it says.
    """
    for pattern in reversed(patterns):
        negated, body = split_ref_pattern(pattern)
        if ref_pattern_match(body, name):
            return not negated
    return False


def has_positive_ref_pattern(patterns: Sequence[str]) -> bool:
    """Say whether *patterns* holds a pattern that selects, rather than only ones that exclude.

    A list of nothing but exclusions selects nothing whatever refs exist, since there is never
    anything for them to take away from; the manifest refuses one on those grounds.
    """
    return any(not split_ref_pattern(pattern)[0] for pattern in patterns)
