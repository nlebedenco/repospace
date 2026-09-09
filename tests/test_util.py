"""Tests for repospace.util."""

from __future__ import annotations

import warnings

import pytest

from repospace import util
from repospace.app.common import decode_ref


def test_topdir_found_from_nested_directory(tmp_path):
    (tmp_path / ".repospace").mkdir()
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    assert util.topdir(nested) == str(tmp_path)


def test_topdir_at_topdir_itself(tmp_path):
    (tmp_path / ".repospace").mkdir()
    assert util.topdir(tmp_path) == str(tmp_path)


def test_topdir_not_found(tmp_path):
    with pytest.raises(util.RepospaceNotFound):
        util.topdir(tmp_path)


def test_topdir_marker_file_does_not_count(tmp_path):
    (tmp_path / ".repospace").write_text("not a directory")
    with pytest.raises(util.RepospaceNotFound):
        util.topdir(tmp_path)


def test_repospace_dir(tmp_path):
    (tmp_path / ".repospace").mkdir()
    assert util.repospace_dir(tmp_path) == str(tmp_path / ".repospace")


def test_escapes_directory(tmp_path):
    inside = tmp_path / "sub" / "file"
    assert not util.escapes_directory(inside, tmp_path)
    assert util.escapes_directory(tmp_path.parent, tmp_path)
    sneaky = tmp_path / "sub" / ".." / ".." / "other"
    assert util.escapes_directory(sneaky, tmp_path)


def test_quote_sh_list():
    assert util.quote_sh_list(["git", "log", "--format=%h %s"]) == ("git log '--format=%h %s'")


@pytest.mark.parametrize(
    "pattern,name,expected",
    [
        # "*" and "?" stay within one "/"-separated component; "**" spans components.
        ("*", "main", True),
        ("*", "release/1.0", False),
        ("**", "main", True),
        ("**", "release/1.0", True),
        ("release/*", "release/1.0", True),
        ("release/*", "release/1.0/x", False),
        ("release/**", "release/1.0/x", True),
        ("release/**", "release", False),
        ("**/hotfix", "hotfix", True),
        ("**/hotfix", "a/b/hotfix", True),
        ("a?b", "axb", True),
        ("a?b", "a/b", False),
        # Character classes, and their negation, which also excludes "/".
        ("v[0-9]*", "v1.2", True),
        ("v[0-9]*", "vx", False),
        ("v[!0-9]*", "vx", True),
        ("v[!0-9]*", "v1", False),
        ("[a]", "a", True),
        # A reversed range holds nothing, so the class matches nothing and its negation everything;
        # neither may raise, however the pattern was written.
        ("v[z-a]", "va", False),
        ("v[z-a]", "vz", False),
        ("v[!z-a]", "va", True),
        # "a--z" is the reversed range "a" to "-", dropped as any other; neither end survives as a
        # literal, and the "--" never reaches the regular expression as its set-difference operator.
        ("v[a--z]", "vz", True),
        ("v[a--z]", "va", False),
        # "^" has no special meaning: only "[!...]" negates.
        ("v[^ab]", "v^", True),
        ("v[^ab]", "va", True),
        ("v[^ab]", "vc", False),
        # An unclosed "[" is a literal, as in a shell glob.
        ("a[b", "a[b", True),
        ("a[b", "ab", False),
        # The whole name must match.
        ("ma", "main", False),
        ("main", "main", True),
        ("main", "topic/main", False),
        # A "." is literal, not the regular expression wildcard.
        ("v1.0", "v1x0", False),
    ],
)
def test_ref_pattern_match(pattern, name, expected):
    assert util.ref_pattern_match(pattern, name) is expected


def test_ref_pattern_match_surrogate_name():
    # Ref names do not have to be valid UTF-8; decode_ref leaves undecodable bytes as surrogates.
    assert util.ref_pattern_match("br-*", decode_ref(b"br-\xff"))


def test_translate_ref_pattern_is_anchored():
    assert util.translate_ref_pattern("release/*") == r"(?s:release/[^/]*)\Z"


@pytest.mark.parametrize(
    "pattern",
    ["[z-a]", "[a--z]", "[&&]", "[~~]", "[||]", "[]]", "[-]", "[\\]", "[^]", "[a-]", "[!]-]"],
)
def test_translate_ref_pattern_never_raises(pattern):
    # A pattern only has to pass the manifest's refname rules; nothing it can say may reach the re
    # module as syntax, whether by raising or by warning about a set operator.
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        util.ref_pattern_match(pattern, "x")


@pytest.mark.parametrize(
    "patterns,name,expected",
    [
        # A name no pattern matches is not selected, and an empty list matches no name.
        ([], "main", False),
        (["main"], "main", True),
        (["main"], "other", False),
        # A "!" pattern excludes what it matches from what the patterns before it selected.
        (["**", "!wip/*"], "main", True),
        (["**", "!wip/*"], "wip/x", False),
        # The last match decides, either way: a later pattern puts back what an earlier one
        # excluded, and excludes what an earlier one selected.
        (["**", "!wip/*", "wip/keep"], "wip/keep", True),
        (["**", "!wip/*", "wip/keep"], "wip/x", False),
        (["wip/keep", "!wip/*"], "wip/keep", False),
        # An exclusion never selects on its own, whether it matches or not.
        (["!wip/*"], "wip/x", False),
        (["!wip/*"], "main", False),
        # Only the first "!" is the marker; the pattern below it reads one literally.
        (["**", "!!x"], "!x", False),
        (["**", "!!x"], "x", True),
        (["a!b"], "a!b", True),
        # A pattern that selects has no escape, so a wildcard is what reaches such a name.
        (["?x"], "!x", True),
    ],
)
def test_ref_patterns_match(patterns, name, expected):
    assert util.ref_patterns_match(patterns, name) is expected


def test_split_ref_pattern():
    assert util.split_ref_pattern("!wip/*") == (True, "wip/*")
    assert util.split_ref_pattern("wip/*") == (False, "wip/*")
    assert util.split_ref_pattern("!!x") == (True, "!x")
    assert util.split_ref_pattern("a!b") == (False, "a!b")


def test_has_positive_ref_pattern():
    assert util.has_positive_ref_pattern(["**", "!wip/*"]) is True
    # Nothing for the exclusions to take away from: such a list selects nothing at all.
    assert util.has_positive_ref_pattern(["!wip/*", "!x"]) is False
    assert util.has_positive_ref_pattern([]) is False
