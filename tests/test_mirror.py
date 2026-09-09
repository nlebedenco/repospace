"""Integration tests for repospace mirror against local git remotes."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from conftest import git_bytes, git_out

from repospace.app import mirror as mirror_mod
from repospace.app import update as update_mod
from repospace.app.mirror import (
    RefChange,
    RemoteRefs,
    chunked_by_size,
    compute_plan,
    head_deletion,
    mirrors_no_branch,
    parse_ls_remote,
    parse_push_porcelain,
    with_fetched_shas,
)
from repospace.manifest import Member, RefPatterns, Upstream

# ---------------------------------------------------------------------------
# Pure functions


def test_parse_ls_remote():
    output = (
        b"ref: refs/heads/main\tHEAD\n"
        b"1111111111111111111111111111111111111111\tHEAD\n"
        b"1111111111111111111111111111111111111111\trefs/heads/main\n"
        b"2222222222222222222222222222222222222222\trefs/heads/release/1.0\n"
        b"3333333333333333333333333333333333333333\trefs/tags/v1\n"
        b"4444444444444444444444444444444444444444\trefs/tags/v1^{}\n"
        b"5555555555555555555555555555555555555555\trefs/pull/7/head\n"
    )
    refs = parse_ls_remote(output)
    assert refs.head_target == "refs/heads/main"
    assert refs.heads == {"main": "1" * 40, "release/1.0": "2" * 40}
    # The peeled line of an annotated tag is dropped: the tag object is what gets mirrored, and
    # refs outside heads/tags are none of the mirror's business.
    assert refs.tags == {"v1": "3" * 40}


def test_parse_ls_remote_crlf_line_endings():
    # Every field is normalized before it is tested, not only the ones decode_ref sees: a trailing
    # carriage return must not make the peeled line of an annotated tag a tag of its own.
    refs = parse_ls_remote(
        b"3333333333333333333333333333333333333333\trefs/tags/v1\r\n"
        b"4444444444444444444444444444444444444444\trefs/tags/v1^{}\r\n"
    )
    assert refs.tags == {"v1": "3" * 40}


def test_parse_ls_remote_without_symref():
    refs = parse_ls_remote(b"1111111111111111111111111111111111111111\trefs/heads/main\n")
    assert refs.head_target is None


def test_parse_ls_remote_non_utf8_name():
    # Not skipped off POSIX: a ref name only has to be valid UTF-8 where it was created, so this is
    # what a Windows client listing a POSIX-hosted remote gets back. decode_ref must not raise on
    # it, whatever the platform's filesystem codec would do.
    refs = parse_ls_remote(b"1111111111111111111111111111111111111111\trefs/heads/br-\xff\n")
    (name,) = refs.heads
    assert name.encode("utf-8", "surrogateescape") == b"br-\xff"


def _plan_tuples(plan):
    return [(c.kind, c.name, c.old, c.new) for c in plan]


def test_compute_plan_create_update_delete_unchanged():
    upstream = RemoteRefs({"main": "a", "new": "b", "same": "s"}, {"v1": "t"})
    origin = RemoteRefs({"main": "old", "same": "s", "gone": "g"}, {})
    plan = compute_plan(upstream, origin, Upstream("u"), prune=True)
    assert _plan_tuples(plan) == [
        ("heads", "gone", "g", None),
        ("heads", "main", "old", "a"),
        ("heads", "new", None, "b"),
        ("tags", "v1", None, "t"),
    ]


def test_compute_plan_without_prune_deletes_nothing():
    # Deleting is opt-in: without it an origin ref the patterns do not select is left alone.
    upstream = RemoteRefs({"main": "a"}, {})
    origin = RemoteRefs({"main": "a", "gone": "g"}, {"old": "t"})
    assert compute_plan(upstream, origin, Upstream("u")) == []


def test_compute_plan_default_selects_hierarchical_names():
    # The default is "**", not "*": a single star stays within one component, so "release/1.0"
    # would be left unselected and pruned from origin.
    upstream = RemoteRefs({"release/1.0": "a"}, {})
    origin = RemoteRefs({"release/1.0": "a"}, {})
    assert compute_plan(upstream, origin, Upstream("u")) == []


def test_compute_plan_patterns_and_preserve():
    upstream = RemoteRefs(
        {"main": "a", "release/1.0": "b", "topic/z": "c", "forked/x": "u"},
        {"v1": "t1", "beta": "t2"},
    )
    origin = RemoteRefs(
        {"main": "a", "forked/x": "o", "junk": "j"},
        {"v1": "t1", "old": "t3"},
    )
    spec = Upstream(
        "u",
        mirror=RefPatterns(("main", "release/*"), ("v[0-9]*",)),
        preserve=RefPatterns(("forked/*",), ()),
    )
    assert _plan_tuples(compute_plan(upstream, origin, spec, prune=True)) == [
        # forked/x is preserved: neither pushed to (though upstream has one) nor pruned.
        ("heads", "junk", "j", None),
        ("heads", "release/1.0", None, "b"),
        ("tags", "old", "t3", None),
    ]


def test_compute_plan_prunes_refs_a_wider_run_mirrored():
    upstream = RemoteRefs({"main": "a", "topic/z": "c"}, {})
    origin = RemoteRefs({"main": "a", "topic/z": "c"}, {})
    spec = Upstream("u", mirror=RefPatterns(("main",), ("**",)))
    assert _plan_tuples(compute_plan(upstream, origin, spec, prune=True)) == [
        ("heads", "topic/z", "c", None)
    ]


def test_compute_plan_exclusions_narrow_the_mirror():
    upstream = RemoteRefs(
        {"main": "a", "wip/x": "b", "wip/keep": "c"},
        {"v1": "t1", "nightly": "t2"},
    )
    origin = RemoteRefs({}, {})
    spec = Upstream("u", mirror=RefPatterns(("**", "!wip/*", "wip/keep"), ("**", "!nightly")))
    # The last pattern matching a ref decides: "wip/keep" is selected again after "!wip/*" took it.
    assert _plan_tuples(compute_plan(upstream, origin, spec, prune=True)) == [
        ("heads", "main", None, "a"),
        ("heads", "wip/keep", None, "c"),
        ("tags", "v1", None, "t1"),
    ]


def test_compute_plan_an_exclusion_narrows_preserve():
    # What "preserve" excludes is not origin's to keep: --prune deletes it as it does any other ref
    # neither selected nor preserved.
    upstream = RemoteRefs({"main": "a"}, {})
    origin = RemoteRefs({"main": "a", "forked/x": "o", "forked/tmp": "t"}, {})
    spec = Upstream("u", preserve=RefPatterns(("forked/*", "!forked/tmp"), ()))
    assert _plan_tuples(compute_plan(upstream, origin, spec, prune=True)) == [
        ("heads", "forked/tmp", "t", None),
    ]


def test_compute_plan_namespaces_are_independent():
    upstream = RemoteRefs({"v1": "h"}, {"v1": "t"})
    origin = RemoteRefs({}, {})
    assert _plan_tuples(compute_plan(upstream, origin, Upstream("u"))) == [
        ("heads", "v1", None, "h"),
        ("tags", "v1", None, "t"),
    ]


def test_mirrors_no_branch():
    upstream = RemoteRefs({"main": "a"}, {"v1": "t"})
    assert mirrors_no_branch(upstream, Upstream("u")) is False
    # A qualified pattern is a well formed one that matches nothing: the name it is tested against
    # is "main", not "refs/heads/main".
    qualified = Upstream("u", mirror=RefPatterns(("refs/heads/main",), ("**",)))
    assert mirrors_no_branch(upstream, qualified) is True
    # Tags are not part of the question: upstream may define none, and a fork may want none of the
    # ones it does define.
    no_tags = Upstream("u", mirror=RefPatterns(("main",), ()))
    assert mirrors_no_branch(upstream, no_tags) is False
    assert mirrors_no_branch(upstream, Upstream("u", mirror=RefPatterns(("main",), ("nope",)))) is (
        False
    )
    # Upstream holding no branches at all says nothing about the branch patterns: no pattern,
    # however wide, could have matched one.
    assert mirrors_no_branch(RemoteRefs({}, {"v1": "t"}), qualified) is False
    # An exclusion can leave a wide pattern selecting nothing, and the refusal is the same: what
    # matters is what the list selects, not how it was spelled.
    excluded = Upstream("u", mirror=RefPatterns(("**", "!main"), ("**",)))
    assert mirrors_no_branch(upstream, excluded) is True
    # "preserve" is not part of it either. A mirror pattern that matches is enough, even where
    # preserve then keeps every branch it selected.
    preserved = Upstream(
        "u", mirror=RefPatterns(("main",), ("**",)), preserve=RefPatterns(("main",), ())
    )
    assert mirrors_no_branch(upstream, preserved) is False


def test_head_deletion():
    origin = RemoteRefs({"main": "a"}, {}, "refs/heads/main")
    deletion = RefChange("heads", "main", "a", None)
    # The change itself, not just its name: the caller reports on its kind and name.
    assert head_deletion([deletion], origin) is deletion
    assert head_deletion([RefChange("heads", "main", "a", "b")], origin) is None
    assert head_deletion([RefChange("heads", "other", "a", None)], origin) is None
    # A HEAD pointing at a branch that does not exist on origin is not a deletion.
    assert head_deletion([deletion], RemoteRefs({}, {})) is None


def test_with_fetched_shas():
    plan = [
        RefChange("heads", "main", "old", "listed"),
        RefChange("heads", "gone", "g", None),
    ]
    shas = {f"{mirror_mod.SCRATCH}heads/main": "fetched"}
    # Upstream moved between the listing and the fetch: the push writes what was fetched, so that
    # is the value the report has to name. A deletion has nothing to fetch.
    assert _plan_tuples(with_fetched_shas(plan, shas)) == [
        ("heads", "main", "old", "fetched"),
        ("heads", "gone", "g", None),
    ]
    # A change whose scratch ref is missing keeps the value it was planned with.
    assert with_fetched_shas(plan, {})[0].new == "listed"


def test_chunked_by_size():
    groups = [("aaaa",), ("bbbb",), ("cccc",)]
    assert list(chunked_by_size(groups, 100)) == [groups]
    assert list(chunked_by_size(groups, 11)) == [[("aaaa",), ("bbbb",)], [("cccc",)]]
    # A group larger than the budget still goes out, alone: splitting it would break the refspec.
    assert list(chunked_by_size([("x" * 50,), ("y",)], 10)) == [[("x" * 50,)], [("y",)]]
    assert list(chunked_by_size([], 10)) == []


def test_parse_push_porcelain():
    output = (
        b"To /somewhere\n"
        b" \trefs/repospace/upstream/heads/a:refs/heads/a\ta1b2..c3d4\n"
        b"+\trefs/repospace/upstream/heads/b:refs/heads/b\tb1..b2 (forced update)\n"
        b"-\t:refs/heads/c\t[deleted]\n"
        b"!\t:refs/heads/d\t[remote rejected] (stale info)\n"
        b"=\trefs/repospace/upstream/tags/v1:refs/tags/v1\t[up to date]\n"
        b"Done\n"
    )
    assert parse_push_porcelain(output) == {
        "refs/heads/a": (" ", "a1b2..c3d4"),
        "refs/heads/b": ("+", "b1..b2 (forced update)"),
        "refs/heads/c": ("-", "[deleted]"),
        "refs/heads/d": ("!", "[remote rejected] (stale info)"),
        "refs/tags/v1": ("=", "[up to date]"),
    }


# ---------------------------------------------------------------------------
# Integration


def refs_of(repo, *namespaces):
    """Return {refname: object id} for *repo*, restricted to *namespaces* when given."""
    out = git_out(repo, "for-each-ref", "--format=%(refname) %(objectname)", *namespaces)
    return dict(line.split(" ", 1) for line in out.splitlines() if line)


def member_yaml(url, upstream_url=None, mirror=None, preserve=None, extra="", name="fork"):
    """Render one member entry for Topology.app_yaml(extra_members=...)."""
    text = f"    - name: {name}\n      url: {url}\n{extra}"
    if upstream_url is None:
        return text
    text += f"      upstream:\n        url: {upstream_url}\n"
    for key, patterns in (("mirror", mirror), ("preserve", preserve)):
        if not patterns:
            continue
        text += f"        {key}:\n"
        for kind, values in patterns.items():
            rendered = ", ".join(f'"{v}"' for v in values)
            text += f"          {kind}: [{rendered}]\n"
    return text


class Forked:
    """A repospace whose member "fork" has a bare origin and an upstream repository.

    "fork" names the fixture and the member; the two repositories are origin and upstream.
    """

    def __init__(self, repospace, run_repospace, upstream, origin):
        self.topology = repospace
        self.run_repospace = run_repospace
        self.repos = repospace.repos
        self.ws = repospace.ws
        self.upstream = upstream
        self.origin = origin
        self.member = repospace.ws / "fork"

    @property
    def origin_url(self):
        return self.topology.url(self.origin)

    @property
    def upstream_url(self):
        return self.topology.url(self.upstream)

    def declare(self, upstream=True, **kwargs):
        """Rewrite the manifest with the member; pass upstream=False to drop the section."""
        self.topology.rewrite_app_yaml(
            extra_members=member_yaml(
                self.origin_url,
                self.upstream_url if upstream else None,
                **kwargs,
            )
        )

    def mirror(self, *args, expect=0):
        code, out, err = self.run_repospace(["mirror", *args], cwd=self.ws)
        assert code == expect, err
        return out, err

    def update(self, *args, expect=0):
        code, out, err = self.run_repospace(["update", *args], cwd=self.ws)
        assert code == expect, err
        return out, err

    def push_to_origin(self, refspec):
        """Push straight to origin, as its owner or another clone would."""
        subprocess.run(
            ["git", "-C", str(self.upstream), "push", "-q", str(self.origin), refspec],
            check=True,
        )


@pytest.fixture
def forked(repospace, run_repospace, repos):
    upstream = repos.create("fork-upstream", {"f.txt": "1\n"})
    repos.tag(upstream, "v1")
    # Origin must be bare: a push to the checked-out branch of a non-bare repository is refused.
    origin = repos.bare_clone(upstream, "fork-origin.git")
    fixture = Forked(repospace, run_repospace, upstream, origin)
    fixture.declare()
    fixture.update()
    return fixture


def test_mirror_help_available_without_repospace(run_repospace, tmp_path):
    code, out, err = run_repospace(["mirror", "-h"], cwd=tmp_path)
    assert code == 0, err
    assert "usage: repospace mirror" in out
    assert "upstream" in out


def test_mirror_banner_names_both_repositories(forked):
    # The mirror reads one repository and force-pushes onto another; a report naming only the
    # member's own url leaves a mistyped "upstream: url" invisible until the wrong history lands.
    out, err = forked.mirror()

    assert f"from {forked.upstream_url} to {forked.origin_url}" in out


def test_mirror_creates_updates_and_prunes(forked):
    forked.push_to_origin("main:refs/heads/stray")
    forked.repos.commit(forked.upstream, {"f.txt": "2\n"})
    forked.repos.branch(forked.upstream, "feature")
    forked.repos.tag(forked.upstream, "v2")

    out, err = forked.mirror("--prune")

    assert refs_of(forked.origin) == refs_of(forked.upstream)
    assert "new      refs/heads/feature" in out
    assert "updated  refs/heads/main" in out
    assert "new      refs/tags/v2" in out
    assert "pruned   refs/heads/stray" in out
    # The scratch refs live only for the duration of the push.
    assert git_bytes(forked.member, "for-each-ref", "refs/repospace") == b""


def test_mirror_without_prune_keeps_unselected_origin_refs(forked):
    # Deleting is opt-in, so a stray origin ref survives a plain run.
    forked.push_to_origin("main:refs/heads/stray")
    forked.repos.branch(forked.upstream, "feature")

    out, err = forked.mirror()

    assert "new      refs/heads/feature" in out
    assert "pruned" not in out
    assert "refs/heads/stray" in refs_of(forked.origin)


def test_mirror_is_idempotent(forked):
    forked.repos.commit(forked.upstream, {"f.txt": "2\n"})
    forked.mirror()
    before = refs_of(forked.origin)

    out, err = forked.mirror()

    assert "origin is up to date" in out
    assert refs_of(forked.origin) == before


def test_mirror_patterns_and_preserve(forked):
    forked.repos.branch(forked.upstream, "release/1.0")
    forked.repos.branch(forked.upstream, "topic/z")
    forked.repos.tag(forked.upstream, "beta")
    forked.push_to_origin("main:refs/heads/forked/patch")
    forked.push_to_origin("main:refs/heads/junk")
    forked.declare(
        mirror={"heads": ["main", "release/*"], "tags": ["v[0-9]*"]},
        preserve={"heads": ["forked/*"]},
    )

    forked.mirror("--prune")

    assert set(refs_of(forked.origin)) == {
        "refs/heads/main",
        "refs/heads/release/1.0",
        "refs/heads/forked/patch",
        "refs/tags/v1",
    }


def test_mirror_exclusion_patterns_narrow_the_selection(forked):
    forked.repos.branch(forked.upstream, "wip/x")
    forked.repos.branch(forked.upstream, "wip/keep")
    forked.push_to_origin("main:refs/heads/forked/patch")
    forked.push_to_origin("main:refs/heads/forked/tmp")
    forked.declare(
        mirror={"heads": ["**", "!wip/*", "wip/keep"]},
        preserve={"heads": ["forked/*", "!forked/tmp"]},
    )

    forked.mirror("--prune")

    assert set(refs_of(forked.origin, "refs/heads")) == {
        "refs/heads/main",
        "refs/heads/wip/keep",
        "refs/heads/forked/patch",
    }


def test_mirror_preserve_wins_over_an_upstream_ref_of_the_same_name(forked):
    # A preserved ref is origin's own, even where upstream happens to use the name.
    forked.repos.branch(forked.upstream, "forked/x")
    forked.push_to_origin("main:refs/heads/forked/x")
    forked.repos.commit(forked.upstream, {"f.txt": "moved\n"})
    origin_sha = refs_of(forked.origin)["refs/heads/forked/x"]
    forked.declare(preserve={"heads": ["forked/*"]})

    out, err = forked.mirror()

    assert refs_of(forked.origin)["refs/heads/forked/x"] == origin_sha
    assert "forked/x" not in out


def test_mirror_narrower_patterns_prune_an_earlier_run(forked):
    forked.repos.branch(forked.upstream, "topic/z")
    forked.mirror()
    assert "refs/heads/topic/z" in refs_of(forked.origin)

    forked.declare(mirror={"heads": ["main"], "tags": ["**"]})
    out, err = forked.mirror("--prune")

    assert "pruned   refs/heads/topic/z" in out
    assert "refs/heads/topic/z" not in refs_of(forked.origin)


def test_mirror_prunes_all_origin_tags_when_upstream_has_none(forked):
    subprocess.run(["git", "-C", str(forked.upstream), "tag", "-d", "v1"], check=True)

    forked.mirror("--prune")

    assert refs_of(forked.origin, "refs/tags") == {}


@pytest.mark.parametrize("args", [(), ("--prune",), ("--dry-run",)])
def test_mirror_refuses_a_branch_pattern_that_matches_nothing(forked, args):
    # A mirror is always for at least one branch, since upstream always has a default one, so a
    # "mirror: heads" selecting none of them mirrors nothing whatever the run was asked to do.
    # With --prune it deletes every origin branch on top of that: what tells a ref upstream
    # deleted from a ref that only ever existed on origin is exclusion, and whatever "preserve"
    # does not name is taken to be upstream's.
    forked.declare(mirror={"heads": ["refs/heads/main"], "tags": ["**"]})
    forked.repos.tag(forked.upstream, "v2")
    before = refs_of(forked.origin)

    out, err = forked.mirror(*args, expect=1)

    assert '"upstream: mirror: heads" matches none of the 1 branch(es)' in err
    assert 'a fully qualified pattern such as "refs/heads/main" never matches anything' in err
    # Refused before the fetch, so not even the tags the patterns did select are pushed.
    assert refs_of(forked.origin) == before


def test_mirror_allows_a_tag_pattern_that_matches_nothing(forked):
    # Upstream may define no tag at all, and a fork may want none of the ones it does define, so a
    # tag list matching nothing cannot be told from a mistyped one -- and it deletes no branch.
    forked.push_to_origin("main:refs/heads/stray")
    forked.declare(mirror={"heads": ["**"], "tags": ["not-created-yet"]})

    out, err = forked.mirror("--prune")

    assert "pruned   refs/heads/stray" in out
    assert "pruned   refs/tags/v1" in out


def test_mirror_prune_allows_a_preserve_pattern_that_matches_nothing(forked):
    # A "preserve" list matching nothing is also what a fork looks like before it has created the
    # branch it means to keep, so nothing can be inferred from it.
    forked.push_to_origin("main:refs/heads/stray")
    forked.declare(preserve={"heads": ["not-created-yet"]})

    out, err = forked.mirror("--prune")

    assert "pruned   refs/heads/stray" in out


def test_mirror_an_empty_tag_list_mirrors_no_tag(forked):
    # Each list defaults to "**" on its own, so "mirror: tags: []" is the only way to say that a
    # fork wants none of upstream's tags. Without --prune the tags origin already holds stay.
    forked.declare(mirror={"heads": ["**"], "tags": []})
    forked.repos.tag(forked.upstream, "v2")

    out, err = forked.mirror()

    assert "refs/tags/v2" not in out
    assert "refs/tags/v2" not in refs_of(forked.origin)
    assert "refs/tags/v1" in refs_of(forked.origin)


def test_mirror_prune_with_an_empty_tag_list_deletes_origins_tags(forked):
    # "origin ends up holding exactly the selected upstream refs plus the preserved ones" holds
    # here too: selecting no tag and preserving none deletes the tags origin has. "preserve" is
    # what keeps a fork's own tags out of it.
    forked.declare(mirror={"heads": ["**"], "tags": []})

    out, err = forked.mirror("--prune")

    assert "pruned   refs/tags/v1" in out
    assert refs_of(forked.origin, "refs/tags") == {}


def test_mirror_leaves_origins_own_tags_alone_without_prune(forked):
    # A "mirror" block that names only branches still defaults its tag list to "**", so a tag
    # of origin's own would be pruned by it; nothing is deleted until --prune asks for it.
    forked.push_to_origin("v1:refs/tags/origin-release")
    forked.declare(mirror={"heads": ["main"]})

    forked.mirror()

    assert "refs/tags/origin-release" in refs_of(forked.origin)


def test_mirror_forced_update_is_reported(forked):
    subprocess.run(
        ["git", "-C", str(forked.upstream), "commit", "-q", "--amend", "-m", "amended"],
        check=True,
    )

    out, err = forked.mirror()

    assert "forced   refs/heads/main" in out
    assert refs_of(forked.origin)["refs/heads/main"] == forked.repos.head(forked.upstream)


def test_mirror_detects_a_fast_forward_past_a_direct_push_to_origin(forked):
    # Origin moved since the last update, so its old tip is not in the member's checkout yet. The
    # fetch of the new upstream tip brings its whole history along, that tip included, so the
    # update is still classified exactly.
    forked.repos.commit(forked.upstream, {"f.txt": "2\n"})
    forked.push_to_origin("main:refs/heads/main")
    forked.repos.commit(forked.upstream, {"f.txt": "3\n"})

    out, err = forked.mirror()

    assert "updated  refs/heads/main" in out
    assert refs_of(forked.origin)["refs/heads/main"] == forked.repos.head(forked.upstream)


def test_mirror_reports_the_sha_the_fetch_brought(forked, monkeypatch):
    # Upstream can move between the listing and the fetch. What the push writes is the fetched ref,
    # so the report must name that object and not the one the plan was computed from.
    real = mirror_mod.Mirror._ls_remote
    upstream_url = forked.upstream_url

    def fake_ls_remote(self, member, url, options):
        refs = real(self, member, url, options)
        if url == upstream_url:
            forked.repos.commit(forked.upstream, {"f.txt": "moved after the listing\n"})
        return refs

    monkeypatch.setattr(mirror_mod.Mirror, "_ls_remote", fake_ls_remote)
    forked.repos.commit(forked.upstream, {"f.txt": "2\n"})

    out, err = forked.mirror()

    head = forked.repos.head(forked.upstream)
    assert refs_of(forked.origin)["refs/heads/main"] == head
    assert head[:10] in out


def test_mirror_fails_when_the_fetched_refs_cannot_be_read(forked, monkeypatch):
    # Reporting an empty mapping instead would name pre-fetch object ids for refs the push then set
    # to something else, which is the very answer with_fetched_shas exists to keep out of the
    # report -- and it would say nothing about why.
    forked.repos.branch(forked.upstream, "feature")
    real_git = Member.git

    def fake_git(self, args, *rest, **kwargs):
        if args and args[0] == "for-each-ref" and "%(objectname) %(refname)" in args:
            return subprocess.CompletedProcess(args, 129, b"", b"fatal: broken ref store")
        return real_git(self, args, *rest, **kwargs)

    monkeypatch.setattr(Member, "git", fake_git)

    out, err = forked.mirror(expect=1)

    assert "cannot read the fetched refs" in err
    assert "broken ref store" in err
    assert "refs/heads/feature" not in refs_of(forked.origin)


def test_mirror_ignores_the_checkouts_push_gpgsign(forked):
    # "push.gpgSign = true" asks git for a signed push, which a receiving end that does not
    # implement one refuses outright ("the receiving end does not support --signed push"). Left to
    # the user's config it would fail every member for a reason that has nothing to do with the
    # mirror, and say only "git push failed (exit 128)".
    subprocess.run(
        ["git", "-C", str(forked.member), "config", "push.gpgSign", "true"],
        check=True,
    )
    forked.repos.branch(forked.upstream, "feature")

    out, err = forked.mirror()

    assert "new      refs/heads/feature" in out
    assert "refs/heads/feature" in refs_of(forked.origin)


def test_mirror_does_not_push_the_checkouts_own_tags(forked):
    # A mirror pushes its plan and nothing else: push.followTags would carry an annotated tag of
    # the checkout's own along with the branch it is reachable from -- unleased and unreported.
    subprocess.run(
        ["git", "-C", str(forked.member), "config", "push.followTags", "true"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(forked.member), "tag", "-a", "local-only", "-m", "local"],
        check=True,
    )
    forked.repos.commit(forked.upstream, {"f.txt": "2\n"})

    out, err = forked.mirror()

    assert "updated  refs/heads/main" in out
    assert "refs/tags/local-only" not in refs_of(forked.origin)


def test_mirror_refuses_to_delete_origins_head(forked):
    forked.repos.branch(forked.upstream, "dev")
    forked.declare(mirror={"heads": ["dev"]})
    before = refs_of(forked.origin)

    out, err = forked.mirror("--prune", expect=1)

    assert "origin's HEAD points at refs/heads/main" in err
    # Upstream has "main", so widening "mirror" would select it; both ways out are offered.
    assert '"upstream: preserve: heads" or "upstream: mirror: heads"' in err
    assert "mirror failed for: fork (fork)" in err
    assert refs_of(forked.origin) == before
    assert git_bytes(forked.member, "for-each-ref", "refs/repospace") == b""


def test_mirror_head_on_a_branch_of_origins_own_names_only_preserve(forked):
    # A mirror run pushes what it selects from upstream's refs, so a branch upstream does not have
    # cannot be selected however wide "mirror" is: naming it there would leave the run failing the
    # same way, and only "preserve" (or moving HEAD) resolves it.
    forked.push_to_origin("main:refs/heads/own")
    subprocess.run(
        ["git", "-C", str(forked.origin), "symbolic-ref", "HEAD", "refs/heads/own"],
        check=True,
    )
    before = refs_of(forked.origin)

    out, err = forked.mirror("--prune", expect=1)

    assert "origin's HEAD points at refs/heads/own" in err
    assert '"upstream: preserve: heads" (upstream has no branch of that name' in err
    assert '"upstream: mirror: heads"' not in err
    assert refs_of(forked.origin) == before


def test_mirror_dry_run_changes_nothing(forked):
    forked.repos.commit(forked.upstream, {"f.txt": "2\n"})
    forked.repos.branch(forked.upstream, "feature")
    before = refs_of(forked.origin)

    out, err = forked.mirror("-n")

    assert "dry run" in out
    # "changed", not "updated": a fast-forward and a rewrite are told apart by the push that makes
    # the change, and a dry run pushes nothing.
    assert "changed  refs/heads/main" in out
    assert "new      refs/heads/feature" in out
    assert refs_of(forked.origin) == before
    assert git_bytes(forked.member, "for-each-ref", "refs/repospace") == b""


def test_mirror_leaves_the_checkout_alone(forked):
    def state():
        return (
            git_out(forked.member, "rev-parse", "HEAD"),
            git_out(forked.member, "rev-parse", "repospace-rev"),
            git_out(forked.member, "status", "--porcelain"),
            refs_of(forked.member, "refs/remotes"),
            git_out(forked.member, "remote", "get-url", "origin"),
        )

    forked.repos.commit(forked.upstream, {"f.txt": "2\n"})
    before = state()

    forked.mirror()

    assert state() == before
    # The new origin state is picked up by the next update, not by the mirror.
    forked.update()
    assert git_out(forked.member, "rev-parse", "HEAD") == forked.repos.head(forked.upstream)


def test_mirror_push_rejection_fails_the_member(forked):
    subprocess.run(
        ["git", "-C", str(forked.origin), "config", "receive.denyNonFastForwards", "true"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(forked.upstream), "commit", "-q", "--amend", "-m", "amended"],
        check=True,
    )

    out, err = forked.mirror(expect=1)

    assert "rejected refs/heads/main" in out
    assert "1 ref(s) rejected by origin" in err
    assert "mirror failed for: fork (fork)" in err
    assert git_bytes(forked.member, "for-each-ref", "refs/repospace") == b""


def test_mirror_stale_lease_is_rejected(forked, monkeypatch):
    # A push landing on origin between the listing and the push must not be overwritten.
    real = mirror_mod.Mirror._ls_remote
    origin_url = forked.origin_url

    def fake_ls_remote(self, member, url, options):
        refs = real(self, member, url, options)
        if url != origin_url:
            return refs
        heads = dict(refs.heads)
        heads["main"] = "0" * 40
        return mirror_mod.RemoteRefs(heads, refs.tags, refs.head_target)

    monkeypatch.setattr(mirror_mod.Mirror, "_ls_remote", fake_ls_remote)
    forked.repos.commit(forked.upstream, {"f.txt": "2\n"})
    before = refs_of(forked.origin)

    out, err = forked.mirror(expect=1)

    assert "rejected refs/heads/main" in out
    assert "stale info" in out
    assert refs_of(forked.origin) == before


def test_mirror_survives_a_leftover_update_scratch_ref(forked):
    # update fetches origin's branch tips into a namespace of its own, one that mirror's own
    # scratch refs are never below: a ref and a directory of the same name cannot both exist, so a
    # branch named "upstream" left behind by a killed update would otherwise block every mirror.
    subprocess.run(
        [
            "git",
            "-C",
            str(forked.member),
            "update-ref",
            f"{update_mod.SCRATCH}upstream",
            forked.repos.head(forked.upstream),
        ],
        check=True,
    )
    forked.repos.branch(forked.upstream, "feature")

    out, err = forked.mirror()

    assert "new      refs/heads/feature" in out


def test_mirror_clears_leftover_scratch_refs_before_the_fetch(forked):
    # A run killed between the fetch and the cleanup leaves its scratch refs behind. If upstream has
    # since turned one of those names into a directory ("a" renamed to "a/b"), the fetch cannot
    # create the new scratch ref while the old one is in the way: a ref and a directory of the same
    # name cannot both exist. Clearing the namespace only afterwards would cost a failed run first.
    subprocess.run(
        [
            "git",
            "-C",
            str(forked.member),
            "update-ref",
            f"{mirror_mod.SCRATCH}heads/a",
            forked.repos.head(forked.upstream),
        ],
        check=True,
    )
    forked.repos.branch(forked.upstream, "a/b")

    out, err = forked.mirror()

    assert "new      refs/heads/a/b" in out
    assert git_bytes(forked.member, "for-each-ref", "refs/repospace") == b""


def test_mirror_deletes_every_scratch_ref_in_one_process(forked, monkeypatch):
    # A repository can have thousands of tags, hence thousands of scratch refs. Deleting them one
    # git process at a time would spend more time forking than deleting, on a machine whose process
    # table is not this command's to fill.
    for name in ("a", "b", "c", "d"):
        forked.repos.branch(forked.upstream, name)
    real_git = Member.git
    deletions = []

    def fake_git(self, args, *rest, **kwargs):
        if args and args[0] == "update-ref":
            deletions.append(list(args))
        return real_git(self, args, *rest, **kwargs)

    monkeypatch.setattr(Member, "git", fake_git)

    forked.mirror()

    # One invocation, after the push; the clear before the fetch finds nothing and runs no git.
    assert deletions == [["update-ref", "-z", "--stdin"]]
    assert git_bytes(forked.member, "for-each-ref", "refs/repospace") == b""


def test_mirror_lists_only_the_namespaces_it_mirrors(forked, monkeypatch):
    # A fork on a code-hosting platform carries one refs/pull/ (or refs/merge-requests/) ref per
    # pull request its parent ever had. Listing them costs a download on every run and buys nothing:
    # parse_ls_remote drops every ref outside refs/heads/ and refs/tags/ anyway.
    forked.push_to_origin("main:refs/pull/1/head")
    real_git = Member.git
    listings = []

    def fake_git(self, args, *rest, **kwargs):
        result = real_git(self, args, *rest, **kwargs)
        if args and args[0] == "ls-remote":
            listings.append(result.stdout)
        return result

    monkeypatch.setattr(Member, "git", fake_git)
    forked.repos.branch(forked.upstream, "feature")

    out, err = forked.mirror()

    assert listings
    assert not any(b"refs/pull/" in listing for listing in listings)
    # The filter must not cost the "--symref" line, which is where origin's HEAD is read from.
    assert any(b"ref: refs/heads/main\tHEAD" in listing for listing in listings)
    assert "new      refs/heads/feature" in out
    assert "refs/pull/1/head" in refs_of(forked.origin)


def test_mirror_quiet_still_says_why_origin_rejected_a_push(forked, run_repospace):
    # -q suppresses the per-ref report, which is where the reason would otherwise be, and git's own
    # message is captured rather than shown. Left at that, the run would count a rejection and
    # explain nothing.
    subprocess.run(
        ["git", "-C", str(forked.origin), "config", "receive.denyNonFastForwards", "true"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(forked.upstream), "commit", "-q", "--amend", "-m", "amended"],
        check=True,
    )

    code, out, err = run_repospace(["-q", "mirror"], cwd=forked.ws)

    assert code == 1
    assert "rejected refs/heads/main" not in out
    assert "denying non-fast-forward" in err
    assert "1 ref(s) rejected by origin" in err


def test_mirror_reports_what_an_aborted_push_landed(forked, monkeypatch):
    # A push that fails without refusing a ref of its own (the transport or the server is at fault)
    # ends the run, but what the pushes before it landed is on origin already: the report has to
    # name it, or the user is left not knowing which half of the mirror went through.
    monkeypatch.setattr(mirror_mod, "ARGV_BUDGET", 1)
    forked.repos.branch(forked.upstream, "a")
    forked.repos.branch(forked.upstream, "b")
    real_git = Member.git
    pushes = []

    def fake_git(self, args, *rest, **kwargs):
        if args and args[0] == "push":
            pushes.append(args)
            if len(pushes) > 1:
                return subprocess.CompletedProcess(args, 128, b"", b"fatal: remote end hung up")
        return real_git(self, args, *rest, **kwargs)

    monkeypatch.setattr(Member, "git", fake_git)

    out, err = forked.mirror(expect=1)

    assert "new      refs/heads/a" in out
    assert "unpushed refs/heads/b" in out
    assert "git push failed (exit 128)" in err
    assert "refs/heads/a" in refs_of(forked.origin)
    assert "refs/heads/b" not in refs_of(forked.origin)


def test_mirror_chunks_long_argument_lists(forked, monkeypatch):
    monkeypatch.setattr(mirror_mod, "ARGV_BUDGET", 40)
    for name in ("a", "b", "c", "d"):
        forked.repos.branch(forked.upstream, name)

    forked.mirror()

    assert refs_of(forked.origin) == refs_of(forked.upstream)


def test_mirror_deletes_before_it_creates(forked, monkeypatch):
    # Upstream renames "a/b" to "a". The creation must not reach origin while "a/b" is still there,
    # or git refuses it: "'refs/heads/a/b' exists; cannot create 'refs/heads/a'". A budget this
    # small puts every refspec in a push of its own, so only the order across chunks protects it --
    # and the plan is sorted by name, where "a" comes first.
    forked.repos.branch(forked.upstream, "a/b")
    forked.mirror()
    assert "refs/heads/a/b" in refs_of(forked.origin)

    subprocess.run(
        ["git", "-C", str(forked.upstream), "branch", "-D", "a/b"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    forked.repos.branch(forked.upstream, "a")
    monkeypatch.setattr(mirror_mod, "ARGV_BUDGET", 1)

    out, err = forked.mirror("--prune")

    assert "pruned   refs/heads/a/b" in out
    assert "new      refs/heads/a" in out
    assert refs_of(forked.origin) == refs_of(forked.upstream)


def test_mirror_stops_when_origin_refuses_a_deletion(forked, monkeypatch):
    # The same rename, with the deletion refused on a stale lease. Every chunk after it depends on
    # that deletion having landed, so pushing on would only earn "'refs/heads/a/b' exists; cannot
    # create 'refs/heads/a'" -- a second failure that reads as unrelated to the first and is not.
    forked.repos.branch(forked.upstream, "a/b")
    forked.mirror()
    subprocess.run(
        ["git", "-C", str(forked.upstream), "branch", "-D", "a/b"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    forked.repos.branch(forked.upstream, "a")
    monkeypatch.setattr(mirror_mod, "ARGV_BUDGET", 1)
    real = mirror_mod.Mirror._ls_remote
    origin_url = forked.origin_url

    def fake_ls_remote(self, member, url, options):
        refs = real(self, member, url, options)
        if url != origin_url:
            return refs
        heads = dict(refs.heads)
        heads["a/b"] = "0" * 40
        return mirror_mod.RemoteRefs(heads, refs.tags, refs.head_target)

    monkeypatch.setattr(mirror_mod.Mirror, "_ls_remote", fake_ls_remote)

    out, err = forked.mirror("--prune", expect=1)

    assert "rejected refs/heads/a/b" in out
    assert "unpushed refs/heads/a" in out
    assert "refused to delete refs/heads/a/b" in err
    assert "refs/heads/a" not in refs_of(forked.origin)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: non-UTF-8 ref names")
def test_mirror_non_utf8_branch_name(forked):
    subprocess.run(
        [b"git", b"-C", os.fsencode(str(forked.upstream)), b"branch", b"br-\xff"],
        check=True,
    )

    out, err = forked.mirror()

    assert "Traceback" not in err
    assert b"refs/heads/br-\xff" in git_bytes(forked.origin, "for-each-ref", "--format=%(refname)")
    assert r"br-\xff" in out
    assert git_bytes(forked.member, "for-each-ref", "refs/repospace") == b""


def test_mirror_skips_members_without_upstream(forked):
    # libb has no upstream: nothing is pushed for it and it is not an error.
    out, err = forked.mirror()
    assert "libb" not in out
    assert "libb" not in err


def test_mirror_named_member_without_upstream_dies(forked, run_repospace):
    code, out, err = run_repospace(["mirror", "libb"], cwd=forked.ws)
    assert code == 1
    assert 'has no "upstream"' in err
    assert "FATAL ERROR" in err


def test_mirror_skips_members_declared_by_an_imported_manifest(forked, run_repospace):
    # liba's manifest declares the member; mirroring it would push to a repository the manifest
    # author named, so it takes an explicit request.
    liba = forked.topology.liba_src
    forked.repos.commit(
        liba,
        {
            "repospace.yaml": (
                "manifest:\n"
                "  members:\n"
                "    - name: libc\n"
                f"      url: {forked.topology.url(forked.topology.libc_src)}\n"
                "    - name: imported-fork\n"
                f"      url: {forked.origin_url}\n"
                "      upstream:\n"
                f"        url: {forked.upstream_url}\n"
            )
        },
    )
    forked.topology.rewrite_app_yaml()
    forked.update()
    forked.repos.branch(forked.upstream, "feature")

    out, err = forked.mirror()
    assert "imported-fork" not in out

    code, out, err = run_repospace(["mirror", "imported-fork"], cwd=forked.ws)
    assert code == 0, err
    assert "refs/heads/feature" in out


def test_mirror_skips_inactive_members(forked, run_repospace):
    forked.declare(extra="      groups: [opt]\n")
    forked.topology.rewrite_app_yaml(
        extra_members=member_yaml(
            forked.origin_url,
            forked.upstream_url,
            extra="      groups: [opt]\n",
        ),
        group_filter="-opt",
    )
    forked.repos.branch(forked.upstream, "feature")

    out, err = forked.mirror()
    assert "fork" not in out

    # Naming the member bypasses the group filter, as it does for every member command.
    code, out, err = run_repospace(["mirror", "fork"], cwd=forked.ws)
    assert code == 0, err
    assert "new      refs/heads/feature" in out


def test_mirror_uncloned_member(forked, run_repospace):
    forked.repos.branch(forked.upstream, "feature")
    forked.topology.rewrite_app_yaml(
        extra_members=(
            member_yaml(forked.origin_url, forked.upstream_url)
            + member_yaml(forked.origin_url, forked.upstream_url, name="never-cloned")
        )
    )

    out, err = forked.mirror()
    assert "never-cloned: not cloned; skipping" in out
    assert "new      refs/heads/feature" in out

    code, out, err = run_repospace(["mirror", "never-cloned"], cwd=forked.ws)
    assert code == 1
    assert "is not cloned" in err


def test_mirror_manifest_repository_dies(forked, run_repospace):
    code, out, err = run_repospace(["mirror", "manifest"], cwd=forked.ws)
    assert code == 1
    assert "manifest repository has no upstream" in err


def test_mirror_rejects_a_shallow_member(forked, run_repospace):
    forked.declare(extra="      clone-depth: 1\n")
    forked.update()

    out, err = forked.mirror(expect=1)
    assert 'has "clone-depth: 1"' in err

    # The checkout stays shallow after the attribute is gone, and is still refused.
    forked.declare()
    out, err = forked.mirror(expect=1)
    assert "checkout is shallow" in err


def test_mirror_refuses_a_shallow_member_before_pushing_another(forked, run_repospace):
    # Every reason to refuse is local, so it is found before the first push: a run that cannot
    # mirror one of the members it was given must not leave another one mirrored behind the error.
    forked.topology.rewrite_app_yaml(
        extra_members=(
            member_yaml(forked.origin_url, forked.upstream_url)
            + member_yaml(
                forked.origin_url,
                forked.upstream_url,
                name="shallow",
                extra="      clone-depth: 1\n",
            )
        )
    )
    forked.update()
    forked.repos.branch(forked.upstream, "feature")
    before = refs_of(forked.origin)

    code, out, err = run_repospace(["mirror", "fork", "shallow"], cwd=forked.ws)

    assert code == 1
    assert 'has "clone-depth: 1"' in err
    assert refs_of(forked.origin) == before


def test_mirror_says_when_no_member_has_an_upstream(forked):
    # Silence would read as a command that did nothing, as it would for diff and compare.
    forked.declare(upstream=False)

    out, err = forked.mirror()

    assert "no members to mirror" in out


def test_mirror_aggregates_failures(forked, run_repospace, tmp_path):
    forked.repos.branch(forked.upstream, "feature")
    missing = tmp_path / "nowhere.git"
    forked.topology.rewrite_app_yaml(
        extra_members=(
            member_yaml(forked.origin_url, f"file://{missing}", name="bad")
            + member_yaml(forked.origin_url, forked.upstream_url)
        )
    )
    forked.update()

    out, err = forked.mirror(expect=1)

    # The healthy member is mirrored even though the other one failed.
    assert "new      refs/heads/feature" in out
    assert "mirror failed for: bad (bad)" in err
    assert "cannot list" in err


def test_mirror_upstream_in_resolved_manifest(forked, run_repospace):
    forked.declare(
        mirror={"heads": ["main"]},
        preserve={"tags": ["forked/*"]},
    )
    code, out, err = run_repospace(["manifest", "--resolve"], cwd=forked.ws)
    assert code == 0, err
    assert "upstream:" in out
    assert "mirror:" in out
    assert "preserve:" in out
