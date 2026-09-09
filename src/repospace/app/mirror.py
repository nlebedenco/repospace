"""The mirror command."""

from __future__ import annotations

import concurrent.futures
import os
import subprocess
from dataclasses import dataclass, replace
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from repospace.app.common import MemberCommand, clean_scratch_refs, decode_ref, printable
from repospace.commands import CommandError, HelpFormatter, Verbosity
from repospace.manifest import (
    MANIFEST_MEMBER_NAME,
    QUAL_REFS,
    ManifestMember,
    Member,
    Upstream,
)
from repospace.util import ref_patterns_match

_DESCRIPTION = """\
Push selected upstream branches and tags to members' origin.

A member whose manifest entry has an "upstream" declares that its own repository -- origin, the one
its "url" names -- is a fork of that upstream. For each such member, the branches and tags selected
by "upstream: mirror" (default: all) and not named by "upstream: preserve" (default: none) are
fetched from the upstream URL into the member's checkout and pushed to origin, forced. A preserved
ref is never pushed to, even when upstream has a ref of the same name: "preserve" always wins over
"mirror".

Within one list, a pattern beginning with "!" excludes what it matches, and the last pattern
matching a ref is the one that decides, as in a gitignore file: "heads: ['**', '!wip/*']" selects
every branch except the ones below "wip/".

With --prune, every origin branch and tag that is neither selected nor preserved is deleted too, so
that origin ends up holding exactly the selected upstream refs plus the preserved ones (the work
that exists only on origin). Without it nothing on origin is ever deleted, and a "mirror" selection
narrower than what origin holds simply leaves the rest alone.

A member whose "mirror: heads" matches none of the branches upstream has is refused, with or
without --prune: a mirror is always for at least one branch, so selecting none of them mirrors
nothing, and with --prune deletes every branch origin holds that "preserve" does not match. Tags
are not checked that way: upstream may define none, and a fork may want none of the ones it does
define ("mirror: tags: []").

Without MEMBER arguments, every active member with an "upstream" that is declared by the manifest
repository itself (its manifest file and the files it self-imports) is mirrored; a member declared
by an imported manifest is mirrored only when named. A named member must have an "upstream" and be
cloned. A member with a "clone-depth", or whose checkout is shallow, cannot be mirrored.

The checkout's own state is never modified: not the working tree, HEAD, repospace-rev, or the
origin tracking refs. Run "repospace update" afterwards to pick up the new origin state. The
fetched refs are kept below refs/repospace/upstream/ only for the duration of the push. Their
objects are not: everything origin did not already have stays in the member's object store,
unreferenced once the run ends, so mirroring a large upstream grows the member's .git.

That costs disk and nothing else. A fetch asks only for what the repository does not already
have, reachable or not, so the objects an earlier run left behind spare the next one from
downloading them again; and "git gc" moves unreferenced objects into a cruft pack rather than
deleting them, so they are reclaimed only once they are older than gc.pruneExpire (two weeks by
default) or by an explicit "git gc --prune=now" -- which is why repospace runs no gc of its own.

Each changed ref is reported as new, updated, forced, or pruned; a dry run says "changed" where a
real run distinguishes updated from forced, which only the push itself can tell apart.

Every origin ref is updated under --force-with-lease, so a push that lands between the listing and
the push is not overwritten but reported as rejected. A ref reported as "forced" was not a
fast-forward: upstream rewrote it, and the commits origin held are dropped.
"""

#: Scratch namespace the upstream refs are fetched into; cleaned after every run, and by update.
#: A sibling of the namespace update fetches origin's branches into, never a parent of it: a ref
#: and a directory of the same name cannot both exist, so nesting the two would let a branch name
#: block the other command's fetch for good.
SCRATCH = f"{QUAL_REFS}upstream/"

#: What to call one ref of each namespace in a message; the namespaces themselves are named as the
#: manifest spells them, "heads" and "tags".
_NOUN = {"heads": "branch", "tags": "tag"}

#: What every listing asks the remote for. Patterns rather than "--heads --tags": those two suppress
#: the "--symref" line origin's HEAD is read from, while a pattern list does not, and a remote holds
#: refs outside both namespaces -- a fork on a code-hosting platform carries one refs/pull/ (or
#: refs/merge-requests/) ref per pull request its parent ever had, tens of thousands of refs that
#: every listing would otherwise download and parse_ls_remote would then discard. "refs/heads/*"
#: matches a hierarchical name such as "release/1.0" too: the pattern is matched as a whole, not
#: component by component.
_LISTED_REFS = ["HEAD", "refs/heads/*", "refs/tags/*"]

#: The verb the report gives a ref that git push accepted, by the flag "--porcelain" printed for it.
#: git works out what the report needs -- a fast-forward against a rewrite -- while pushing, and
#: says so per ref, which is why nothing here asks merge-base about it. A space is a branch that
#: fast-forwarded; "+" is a branch that did not, and every tag, since a tag is replaced, never
#: advanced.
_PUSHED_VERB = {"*": "new", "+": "forced", "-": "pruned", " ": "updated"}

#: Bytes of refspecs (and lease options) per git invocation. A repository can have thousands of
#: tags, and Windows caps a command line at 32767 characters; the work is split into as many
#: fetches and pushes as needed.
ARGV_BUDGET = 16 * 1024


class MirrorError(RuntimeError):
    """One member cannot be mirrored; the message is complete and specific."""


@dataclass(frozen=True)
class RemoteRefs:
    """The branches and tags of a remote (short name to object id) and the target of its HEAD."""

    heads: Dict[str, str]
    tags: Dict[str, str]
    head_target: Optional[str] = None


@dataclass(frozen=True)
class RefChange:
    """One change to an origin ref: a creation (old None), an update, or a deletion (new None)."""

    kind: str  # "heads" or "tags"
    name: str
    old: Optional[str]
    new: Optional[str]

    @property
    def dst(self) -> str:
        return f"refs/{self.kind}/{self.name}"

    @property
    def scratch(self) -> str:
        return f"{SCRATCH}{self.kind}/{self.name}"


def parse_ls_remote(output: bytes) -> RemoteRefs:
    """Parse "git ls-remote [--symref]" output into the branches, tags, and HEAD target."""
    heads: Dict[str, str] = {}
    tags: Dict[str, str] = {}
    head_target = None
    for line in output.split(b"\n"):
        # Stripped before anything is tested, not only where decode_ref does it: a ref name holds
        # no whitespace, while a trailing line-ending byte would defeat the peeled-tag test below
        # and turn "v1^{}" into a tag of its own.
        left, _, right = line.strip().partition(b"\t")
        if not right:
            continue
        if left.startswith(b"ref: "):
            # "ref: refs/heads/main<TAB>HEAD", printed by --symref before the listing.
            if right == b"HEAD":
                head_target = decode_ref(left[len(b"ref: ") :])
            continue
        if right.endswith(b"^{}"):
            # The peeled object of an annotated tag; the tag object itself is what gets mirrored.
            continue
        sha = left.decode("ascii", "replace")
        if right.startswith(b"refs/heads/"):
            heads[decode_ref(right[len(b"refs/heads/") :])] = sha
        elif right.startswith(b"refs/tags/"):
            tags[decode_ref(right[len(b"refs/tags/") :])] = sha
    return RemoteRefs(heads, tags, head_target)


def compute_plan(
    upstream: RemoteRefs, origin: RemoteRefs, spec: Upstream, prune: bool = False
) -> List[RefChange]:
    """Return the changes that bring the selected upstream refs onto origin.

    Per namespace: an upstream ref matching a mirror pattern and no preserve pattern is selected,
    and origin gets every selected ref, created or updated where it differs. With *prune*, origin
    also loses every ref that is neither selected nor preserved; without it, nothing is deleted.
    """
    plan: List[RefChange] = []
    for kind, from_upstream, on_origin, mirror, preserve in (
        ("heads", upstream.heads, origin.heads, spec.mirror.heads, spec.preserve.heads),
        ("tags", upstream.tags, origin.tags, spec.mirror.tags, spec.preserve.tags),
    ):
        selected = {
            name: sha
            for name, sha in from_upstream.items()
            if ref_patterns_match(mirror, name) and not ref_patterns_match(preserve, name)
        }
        for name in sorted(selected.keys() | on_origin.keys()):
            old = on_origin.get(name)
            new = selected.get(name)
            if new is None:
                if prune and not ref_patterns_match(preserve, name):
                    plan.append(RefChange(kind, name, old, None))
            elif old != new:
                plan.append(RefChange(kind, name, old, new))
    return plan


def mirrors_no_branch(upstream: RemoteRefs, spec: Upstream) -> bool:
    """Say whether "mirror: heads" matches none of the branches upstream has.

    A mirror is always for at least one branch, since upstream always has a default one, so a
    "mirror: heads" matching none of them is an error however the run was invoked: it copies no
    branch at all, and with --prune it deletes every branch origin has that "preserve" does not
    match as well -- what tells a ref upstream deleted from a ref that only ever existed on origin
    is exclusion, and anything "preserve" does not name is taken to be upstream's. The pattern list
    being empty is the same error, caught earlier still: Upstream.__post_init__ refuses it, so it
    never reaches a run.

    Tags are not checked the same way, and neither is "preserve". Upstream may define no tag at
    all, a fork may want none of the tags upstream does define ("mirror: tags: []" says so), and a
    "preserve" pattern matching nothing is what a fork looks like before it has created the ref it
    means to keep. Each is a use of its own that cannot be told from a mistyped pattern, and none
    of them leaves the mirror with nothing to mirror; --prune and --dry-run are what guard them.

    False where upstream has no branches at all: no pattern, however wide, could have matched one,
    so the emptiness is not what the patterns say.
    """
    return bool(upstream.heads) and not any(
        ref_patterns_match(spec.mirror.heads, name) for name in upstream.heads
    )


def head_deletion(plan: Sequence[RefChange], origin: RemoteRefs) -> Optional[RefChange]:
    """Return the change deleting the ref origin's HEAD points at, if *plan* holds one."""
    if origin.head_target is None:
        return None
    for change in plan:
        if change.new is None and change.dst == origin.head_target:
            return change
    return None


def with_fetched_shas(plan: Sequence[RefChange], shas: Dict[str, str]) -> List[RefChange]:
    """Return *plan* with each new value replaced by what the scratch ref actually holds.

    Upstream can move between the listing the plan was computed from and the fetch. What the push
    writes is the scratch ref, so that is what the report must name; a change whose ref was not
    fetched (there is none once the fetch succeeded) keeps the value it was planned with.
    """
    return [
        change if change.new is None else replace(change, new=shas.get(change.scratch, change.new))
        for change in plan
    ]


def chunked_by_size(groups: Sequence[Sequence[str]], budget: int) -> Iterator[List[Sequence[str]]]:
    """Split *groups* of arguments into chunks of at most *budget* bytes; a group is never split.

    A chunk always holds at least one group, so a single oversized group still goes through.
    """
    chunk: List[Sequence[str]] = []
    size = 0
    for group in groups:
        length = sum(len(os.fsencode(arg)) + 1 for arg in group)
        if chunk and size + length > budget:
            yield chunk
            chunk, size = [], 0
        chunk.append(group)
        size += length
    if chunk:
        yield chunk


def parse_push_porcelain(output: bytes) -> Dict[str, Tuple[str, str]]:
    """Map each destination ref in "git push --porcelain" output to its (flag, summary)."""
    status: Dict[str, Tuple[str, str]] = {}
    for line in output.split(b"\n"):
        # "<flag>\t<from>:<to>\t<summary>"; the "To <url>" and "Done" lines have no tab there.
        if line[1:2] != b"\t":
            continue
        flag = line[:1].decode("ascii", "replace")
        refspec, _, summary = line[2:].partition(b"\t")
        _, _, dst = refspec.partition(b":")
        status[decode_ref(dst)] = (flag, summary.decode("utf-8", "backslashreplace").strip())
    return status


class Mirror(MemberCommand):
    def __init__(self):
        super().__init__(
            "mirror",
            "push selected upstream branches and tags to members' origin",
            _DESCRIPTION,
        )
        self.dry_run = False
        self.prune = False

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(
            self.name,
            help=self.help,
            description=self.description,
            formatter_class=HelpFormatter,
        )
        self.add_members_arg(parser)
        parser.add_argument(
            "-n",
            "--dry-run",
            action="store_true",
            help="print what would be pushed and deleted; fetch and push nothing",
        )
        parser.add_argument(
            "-p",
            "--prune",
            action="store_true",
            help="also delete the origin refs that are neither mirrored nor preserved",
        )
        return parser

    def do_run(self, args, unknown):
        self.die_if_no_git()
        self.dry_run = args.dry_run
        self.prune = args.prune
        members, failed = self._eligible_members(args)
        if not members and not failed:
            self.banner("no members to mirror")
        for member in members:
            try:
                self._mirror_one(member)
            except MirrorError as err:
                self.err(f"{member.name}: {err}")
                failed.append(member)
            except subprocess.CalledProcessError:
                failed.append(member)
            except OSError as err:
                # E.g. the member directory vanished underneath; this must not become a traceback.
                self.err(f"cannot mirror {member.name_and_path}: {err}")
                failed.append(member)
        if failed:
            names = ", ".join(m.name_and_path for m in failed)
            self.err(f"mirror failed for: {names}")
            raise CommandError(1)

    def _eligible_members(self, args) -> Tuple[List[Member], List[Member]]:
        """Return the members to mirror and the ones already refused.

        Every reason to refuse is local, found before the first network access: an explicit name
        that cannot be mirrored stops the run before anything is pushed, and a member selected by
        the manifest is refused without being pushed to at all. A refused member still fails the
        run, so it is returned rather than dropped.
        """
        explicit = bool(args.members)
        result = []
        refused = []
        for member in self.selected_members(args):
            if isinstance(member, ManifestMember):
                self.die("the manifest repository has no upstream and cannot be mirrored")
            if member.upstream is None:
                if explicit:
                    self.die(f'member {member.name_and_path} has no "upstream"')
                self.dbg(f"skipping member {member.name}: no upstream")
                continue
            if not explicit and member.declared_by != MANIFEST_MEMBER_NAME:
                self.dbg(
                    f"skipping member {member.name}: declared by {member.declared_by}, "
                    "not by the manifest repository"
                )
                continue
            if not self.require_cloned(member, explicit):
                self.inf(f"{member.name}: not cloned; skipping")
                continue
            refusal = self._shallow_refusal(member)
            if refusal is not None:
                message = f"cannot mirror {member.name_and_path}: {refusal}"
                if explicit:
                    self.die(message)
                self.err(message)
                refused.append(member)
                continue
            result.append(member)
        return result, refused

    def _shallow_refusal(self, member: Member) -> Optional[str]:
        """Say why *member* cannot mirror upstream history, or None; a local check, no network."""
        if member.clone_depth is not None:
            return (
                f'has "clone-depth: {member.clone_depth}"; '
                "a shallow checkout cannot mirror upstream history"
            )
        result = member.git(
            ["rev-parse", "--is-shallow-repository"],
            check=False,
            capture_stdout=True,
            capture_stderr=True,
        )
        if result.returncode == 0 and result.stdout.strip() == b"true":
            return (
                "the checkout is shallow; a shallow checkout cannot mirror upstream history "
                '(see "git fetch --unshallow")'
            )
        return None

    # -- per-member mirror ---------------------------------------------------

    def _mirror_one(self, member: Member):
        spec = member.upstream
        # Both URLs are named: the mirror reads one repository and force-pushes onto another, and a
        # mistyped "upstream: url" is invisible in a report that only names the member's own.
        self.banner(f"mirroring {member.name_and_path} from {spec.url} to {member.url}:")
        upstream, origin = self._list_remotes(member, spec)
        self._refuse_a_mirror_without_a_branch(upstream, spec)
        plan = compute_plan(upstream, origin, spec, prune=self.prune)
        # A plan is "doomed" when the change would delete the ref origin's HEAD points at. A git
        # server refuses such push, so the member is refused whole -- before the fetch, and without
        # pushing the rest of the plan, which would leave origin half mirrored behind an error the
        # user has to undo by hand.
        doomed = head_deletion(plan, origin)
        if doomed is not None:
            # Which advice to give depends on whether upstream has a ref of the offending name, not
            # on "preserve", which compute_plan already ruled out. The plan may contain a deletion
            # only for a name no preserve pattern matches, so a preserved ref never reaches here.
            # That is what makes "preserve" the answer that always works.
            #
            # Being here therefore means the name is not preserved and was not selected for the
            # mirror, and "selected" is built from upstream's refs alone, so exactly one of two
            # things is true:
            #
            #   - upstream has the name, but no "mirror" pattern matches it. Adding it there
            #     selects it, and the deletion becomes a push. Either key resolves this.
            #
            #   - upstream does not have the name at all: it is origin's own. No "mirror" pattern
            #     can ever select it, however wide, so naming it there leaves the next run failing
            #     the same way. Only "preserve" (or moving HEAD) resolves this.
            #
            # The first case is the common one. A remote's HEAD is a symref, held once in the
            # repository itself and read here through "ls-remote --symref"; on a code-hosting
            # platform (GitHub, GitLab, Gitea) it tracks the repository's default-branch setting,
            # and a fork inherits that setting from its parent when it is created. So origin's HEAD
            # usually names a branch upstream has too ("main", "master"), and the run fails only
            # because the "mirror" patterns were narrowed without it in mind. The second case is
            # what origin gets once its default branch is pointed at a branch of its own.
            #
            # Offering both keys in the second case would send the user down the dead end, so say
            # which one applies.
            kind = doomed.kind
            from_upstream = upstream.heads if kind == "heads" else upstream.tags
            keep = (
                f'"upstream: preserve: {kind}" or "upstream: mirror: {kind}"'
                if doomed.name in from_upstream
                else f'"upstream: preserve: {kind}" (upstream has no {_NOUN[kind]} of that name, '
                'so "mirror" cannot select it)'
            )
            raise MirrorError(
                f"origin's HEAD points at {printable(doomed.dst)}, which the mirror would delete, "
                "and a git server refuses to delete the current branch; list it under "
                f"{keep}, or move HEAD on origin"
            )
        if not plan:
            self.inf(f"{member.name}: origin is up to date")
            return
        if self.dry_run:
            self.inf(f"{member.name}: dry run; changes that would be made on {member.url}:")
            self._report(plan, None)
            return
        try:
            # Cleared before the fetch as well as after it: a run killed mid-flight leaves scratch
            # refs behind, and a name that has since become a directory upstream ("a" renamed to
            # "a/b") would make this fetch die on the leftover rather than on anything it did.
            clean_scratch_refs(member, SCRATCH)
            self._fetch(member, plan)
            plan = with_fetched_shas(plan, self._fetched_shas(member))
            status, aborted = self._push(member, plan)
        finally:
            clean_scratch_refs(member, SCRATCH)
        rejected, unreported = self._report(plan, status)
        problems = []
        if aborted:
            problems.append(aborted)
        if rejected:
            problems.append(f"{rejected} ref(s) rejected by origin")
        if unreported:
            problems.append(f"{unreported} ref(s) left unreported by git push")
        if problems:
            raise MirrorError("; ".join(problems))

    def _list_remotes(self, member: Member, spec: Upstream) -> Tuple[RemoteRefs, RemoteRefs]:
        """Return upstream's refs and origin's, listed at the same time.

        Both listings are narrowed to _LISTED_REFS; only origin's asks for "--symref", because only
        origin's HEAD is consulted -- what upstream calls its default branch is upstream's affair.
        Nothing downstream needs one before the other, and each is a round-trip to a different
        server, so a member pays for one rather than for two in a row. Two processes per member and
        never more: leaving the pool waits for both, including where the first result raises, so no
        git is left running with nobody to collect it. The results are read in a fixed order, so
        which failure is reported does not depend on which listing lost the race.
        """
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            upstream = pool.submit(self._ls_remote, member, spec.url, [])
            origin = pool.submit(self._ls_remote, member, member.url, ["--symref"])
            return upstream.result(), origin.result()

    def _refuse_a_mirror_without_a_branch(self, upstream: RemoteRefs, spec: Upstream):
        """Refuse a member whose "mirror: heads" selects none of upstream's branches."""
        if not mirrors_no_branch(upstream, spec):
            return
        raise MirrorError(
            f'"upstream: mirror: heads" matches none of the {len(upstream.heads)} branch(es) '
            "upstream has, so this mirrors no branch at all (and with --prune deletes every "
            "branch on origin that is not matched by preserve). Note that a pattern is matched "
            'against the ref name below "refs/heads/" (e.g. "main", "release/1.0"), so a fully '
            'qualified pattern such as "refs/heads/main" never matches anything, and that a "!" '
            "pattern only ever excludes what the patterns before it selected."
        )

    def _ls_remote(self, member: Member, url: str, options: List[str]) -> RemoteRefs:
        result = member.git(
            ["ls-remote"] + options + ["--", url] + _LISTED_REFS,
            check=False,
            capture_stdout=True,
            capture_stderr=True,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "backslashreplace").strip()
            raise MirrorError(f"cannot list {url}: {detail}")
        return parse_ls_remote(result.stdout)

    @property
    def _git_quiet(self):
        # Below normal verbosity, silence the git subcommands that print progress on their own.
        return ["-q"] if self.verbosity < Verbosity.INF else []

    def _fetch(self, member: Member, plan: Sequence[RefChange]):
        refspecs = [[f"+{c.dst}:{c.scratch}"] for c in plan if c.new is not None]
        if not refspecs:
            return
        # By URL, with explicit refspecs, as update does: no remote is added to the checkout, and
        # --no-tags keeps auto-followed tags out of refs/tags/ (an explicit tag refspec still
        # fetches the tag). --no-recurse-submodules keeps the user's fetch.recurseSubmodules out of
        # a mirror, which reads none of the fetched trees; see _push on the same config inheritance.
        base = (
            ["fetch", "-f", "--no-tags", "--no-recurse-submodules"]
            + self._git_quiet
            + ["--", member.upstream.url]
        )
        for chunk in chunked_by_size(refspecs, ARGV_BUDGET):
            member.git(base + [arg for group in chunk for arg in group])

    def _fetched_shas(self, member: Member) -> Dict[str, str]:
        """Map each scratch ref to the object the fetch actually brought into it.

        A failure here is raised rather than reported as an empty mapping: falling back to the
        planned values would name pre-fetch object ids for refs the push then set to something
        else, which is the very answer with_fetched_shas exists to keep out of the report.
        """
        result = member.git(
            ["for-each-ref", "--format", "%(objectname) %(refname)", SCRATCH],
            check=False,
            capture_stdout=True,
            capture_stderr=True,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "backslashreplace").strip()
            raise MirrorError(f"cannot read the fetched refs below {SCRATCH}: {detail}")
        shas = {}
        for line in result.stdout.splitlines():
            sha, _, ref = line.partition(b" ")
            shas[decode_ref(ref)] = sha.decode("ascii", "replace")
        return shas

    def _push(
        self, member: Member, plan: Sequence[RefChange]
    ) -> Tuple[Dict[str, Tuple[str, str]], Optional[str]]:
        """Push the plan; return what git reported per ref, and why the push stopped, if it did.

        A chunk that fails ends the push, whether it failed outright or origin refused a deletion in
        it, but whatever the chunks before it pushed is on origin already: their status is returned
        rather than discarded, so the report can name the refs that landed and the caller can count
        the ones that did not.
        """
        deletions = {change.dst for change in plan if change.new is None}
        groups = []
        # Deletions first, and not only inside a chunk: renaming a branch from "a/b" to "a"
        # upstream needs "a/b" gone before "a" can be created, and a single git push already
        # orders its commands that way. Chunking would otherwise split the pair across two
        # invocations in plan order -- "a" sorts before "a/b" -- and the first would be refused
        # with "'refs/heads/a/b' exists; cannot create 'refs/heads/a'". The sort is stable, so
        # each group keeps the plan's order within itself, and _report still prints by name.
        for change in sorted(plan, key=lambda c: c.new is not None):
            # The lease names the value ls-remote saw (empty: the ref must not exist yet), so a
            # concurrent push is rejected instead of overwritten; it also allows the forced update
            # a non-fast-forward or a tag needs.
            lease = f"--force-with-lease={change.dst}:{change.old or ''}"
            refspec = (
                f"{change.scratch}:{change.dst}" if change.new is not None else f":{change.dst}"
            )
            groups.append((lease, refspec))
        status: Dict[str, Tuple[str, str]] = {}
        # Never -q: it would silence the porcelain status lines too. Below normal verbosity the
        # progress on stderr is captured and shown only on a transport failure.
        quiet = bool(self._git_quiet)
        for chunk in chunked_by_size(groups, ARGV_BUDGET):
            leases = [group[0] for group in chunk]
            refspecs = [group[1] for group in chunk]
            result = member.git(
                # The plan is the whole of what a mirror pushes, so the options that would add to
                # it are turned off rather than left to the user's git config: push.followTags
                # would carry the checkout's own annotated tags along -- unplanned, unleased, and
                # unreported -- and push.recurseSubmodules would push to other repositories still.
                # "push.gpgSign = true" is turned off for a different reason: it asks for a signed
                # push, which a server that does not implement one refuses outright ("the receiving
                # end does not support --signed push"), so every member would fail on a setting
                # that has nothing to do with the mirror.
                [
                    "push",
                    "--porcelain",
                    "--no-follow-tags",
                    "--no-recurse-submodules",
                    "--signed=false",
                ]
                + leases
                + ["--", member.url]
                + refspecs,
                check=False,
                capture_stdout=True,
                capture_stderr=quiet,
            )
            reported = parse_push_porcelain(result.stdout)
            status.update(reported)
            if result.returncode != 0:
                # Whatever git said about the failure, say it too. Without -q it went straight to
                # the terminal; with -q it was captured, and the per-ref report that would carry
                # the reason is itself suppressed -- a rejection would otherwise be counted and
                # never explained. git writes no progress to a pipe, so this is the reason alone
                # ("remote: error: denying non-fast-forward ...") and not a transcript.
                if quiet and result.stderr:
                    self.err(result.stderr.decode("utf-8", "backslashreplace").strip())
                # A push that fails without saying which ref it refused did not get that far: the
                # transport or the server is at fault, and the per-ref report says nothing about it.
                if not any(flag == "!" for flag, _ in reported.values()):
                    return status, f"git push failed (exit {result.returncode})"
                # A refused deletion is the one rejection the chunks after it depend on. Deletions
                # are pushed first so that renaming "a/b" to "a" upstream can create "a" at all; if
                # the deletion did not land, the creation is refused with "'refs/heads/a/b' exists"
                # -- a second failure that reads as unrelated and is not. Stop here instead, and
                # let the report name the rest as unpushed. Any other rejection is local to its own
                # ref, so the remaining chunks still go.
                refused = sorted(
                    dst for dst, (flag, _) in reported.items() if flag == "!" and dst in deletions
                )
                if refused:
                    return status, (
                        f"origin refused to delete {printable(refused[0])}; the refs after it were "
                        "not pushed, as creating a ref can need a deletion to have landed first"
                    )
        return status, None

    def _report(
        self,
        plan: Sequence[RefChange],
        status: Optional[Dict[str, Tuple[str, str]]],
    ) -> Tuple[int, int]:
        """Print one line per change; return how many refs origin rejected and left unreported.

        *status* is None for a dry run, where nothing was pushed and every change is named as it
        was planned. Otherwise it is what git push reported, and only what it reported is taken as
        done: a ref git said nothing about was not pushed, whatever the plan expected of it.
        """
        width = max(len(printable(change.dst)) for change in plan)
        rejected = 0
        unreported = 0
        for change in plan:
            summary = ""
            entry = None if status is None else status.get(change.dst)
            if status is None:
                verb = _planned_verb(change)
            elif entry is None:
                # git push --porcelain prints a line per ref it processed, so a missing one means
                # the push stopped before this ref; a chunk that had a rejection gets this far.
                verb = "unpushed"
                summary = "no status reported by git push"
                unreported += 1
            elif entry[0] == "!":
                verb, summary = "rejected", entry[1]
                rejected += 1
            elif entry[0] == "=":
                # Origin already held what the push would have set; nothing was written.
                verb, summary = "current", entry[1]
            else:
                # An unknown flag would be a git that reports something this does not know about;
                # naming the change as planned says more than an empty column would.
                verb = _PUSHED_VERB.get(entry[0], _planned_verb(change))
            old = (change.old or "")[:10]
            new = (change.new or "")[:10]
            if change.new is None:
                shas = old
            elif change.old is None:
                shas = f"-> {new}"
            else:
                shas = f"{old}{'...' if verb == 'forced' else '..'}{new}"
            line = f"  {verb:<9}{printable(change.dst):<{width}}  {shas}"
            self.inf(f"{line}  {summary}" if summary else line)
        return rejected, unreported


def _planned_verb(change: RefChange) -> str:
    """Name a change before it is made, as a dry run reports it.

    An update is "changed", never "updated": whether upstream fast-forwarded the ref or rewrote it
    is what the push reports as it makes the change, and a dry run pushes nothing.
    """
    if change.new is None:
        return "pruned"
    if change.old is None:
        return "new"
    return "changed"
