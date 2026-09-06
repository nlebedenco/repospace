"""The update command."""

from __future__ import annotations

import os
import posixpath
import subprocess
import time

from repospace.app.common import MemberCommand
from repospace.commands import CommandError, HelpFormatter, Verbosity
from repospace.git import git_version
from repospace.manifest import (
    MANIFEST_REV,
    QUAL_MANIFEST_REV,
    QUAL_REFS,
    ImportFlag,
    MalformedManifest,
    Manifest,
    ManifestImportFailed,
    ManifestMember,
    ManifestVersionError,
    Member,
    is_group,
    member_manifest_content,
)

_DESCRIPTION = """\
Clone and update members to their manifest revisions.

For each active member: clone if necessary (git init + remote add, never git clone — repospace owns
only the repospace-rev branch and "origin" remote names); point "origin" at the manifest URL if it
changed; fetch if necessary ("smart" strategy skips the network when the revision is a tag or commit
already available locally); reset refs/heads/repospace-rev to the resolved commit; check out
detached HEAD unless -k or -r asks to keep or rebase a local branch; update submodules. The
repospace-rev branch itself is never kept or rebased: HEAD is detached from it before it moves.

Members whose manifests are imported are updated the moment their manifest data is needed during
resolution, so resolution always sees fresh data.
"""


#: Branch a fresh member repository starts on. It never gets a commit and is never used; it only
#: keeps HEAD off names that matter.
_INIT_PLACEHOLDER_BRANCH = "repospace-init-placeholder"


def _symlink_below_topdir(topdir: str, path: str):
    """Return the first symlinked component of <topdir>/<path>, or None.

    The path is walked lexically from the topdir down, so only symlinks below it are reported: a
    repospace reached through symlinked parents is the user's own doing, but inside it every member
    path must be a real path. The member directory itself counts.
    """
    prefix = topdir
    for part in posixpath.normpath(path).split("/"):
        if part in ("", "."):
            continue
        prefix = os.path.join(prefix, part)
        if os.path.islink(prefix):
            return prefix
    return None


def _decode_ref(data: bytes) -> str:
    """Decode a git ref name so the exact bytes reach git again.

    Ref names are byte strings; git accepts (and can be made to create) names that are not valid
    UTF-8. Undecodable bytes are kept as surrogates, which subprocess re-encodes unchanged when the
    name is passed back on a command line.
    """
    return os.fsdecode(data).strip()


def _printable(text: str) -> str:
    """Return *text* with undecodable bytes shown as escapes.

    A name from _decode_ref may carry surrogates, which a strict UTF-8 stdout refuses to encode;
    messages must survive printing it.
    """
    raw = text.encode("utf-8", "surrogateescape")
    return raw.decode("utf-8", "backslashreplace")


def _looks_like_sha(revision: str) -> bool:
    # Any hex string up to full SHA length may be a commit id; 64 covers SHA-256 repositories. This
    # deliberately mistakes short all-hex branch names for SHAs, the cost of which is only a wider
    # fetch refspec: _fetch() resolves such a name through its scratch ref.
    if not revision or len(revision) > 64:
        return False
    return all(c in "0123456789abcdefABCDEF" for c in revision)


class Update(MemberCommand):
    def __init__(self):
        super().__init__(
            "update",
            "update members to their manifest revisions",
            _DESCRIPTION,
        )
        self.updated = set()
        self.args = None
        self.fetch_strategy = "smart"
        self.narrow = False
        self.extra_group_filter = []

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(
            self.name,
            help=self.help,
            description=self.description,
            formatter_class=HelpFormatter,
        )
        self.add_members_arg(parser)
        parser.add_argument(
            "-f",
            "--fetch",
            choices=("smart", "always"),
            help="fetch strategy (default: update.fetch config or smart)",
        )
        parser.add_argument(
            "-o",
            "--fetch-opt",
            dest="fetch_opts",
            action="append",
            default=[],
            metavar="OPTION",
            help="additional option passed to git fetch; may be repeated; "
            "an OPTION that begins with - must be attached: "
            "--fetch-opt=OPTION",
        )
        parser.add_argument(
            "-n",
            "--narrow",
            action="store_true",
            help="fetch just the manifest revision, not all branches/tags",
        )
        parser.add_argument(
            "-k",
            "--keep-descendants",
            action="store_true",
            help="keep a checked-out branch if it descends from the manifest revision",
        )
        parser.add_argument(
            "-r",
            "--rebase",
            action="store_true",
            help="rebase a checked-out branch onto the manifest revision",
        )
        parser.add_argument(
            "--group-filter",
            "--gf",
            dest="group_filter",
            action="append",
            default=[],
            metavar="FILTER",
            help='additional group filter, e.g. "+optional"; may be repeated',
        )
        parser.add_argument(
            "--stats",
            action="store_true",
            help="print per-member update timing",
        )
        return parser

    def do_run(self, args, unknown):
        self.die_if_no_git()
        self.args = args
        self.updated = set()
        self.fetch_strategy = self._fetch_strategy(args)
        self.narrow = args.narrow or self.config.getboolean("update.narrow", default=False)
        self.extra_group_filter = self._parse_group_filter(args)

        if args.members:
            self._update_some(args)
        else:
            self._update_all(args)

    # -- strategies --------------------------------------------------------

    def _fetch_strategy(self, args) -> str:
        if args.fetch:
            return args.fetch
        configured = self.config.get("update.fetch")
        if configured is None:
            return "smart"
        if configured not in ("smart", "always"):
            self.wrn(f'ignoring invalid update.fetch value "{configured}"; using "smart"')
            return "smart"
        return configured

    def _parse_group_filter(self, args):
        entries = []
        for raw in args.group_filter:
            for item in raw.split(","):
                item = item.strip()
                if not item:
                    continue
                if item[0] not in "+-":
                    self.die(f'invalid --group-filter item "{item}"; it must begin with "+" or "-"')
                if not is_group(item[1:]):
                    self.die(
                        f'invalid --group-filter item "{item}"; '
                        f'"{item[1:]}" is an invalid group name'
                    )
                entries.append(item)
        return entries

    # -- whole-repospace update -------------------------------------------

    def _update_all(self, args):
        try:
            manifest = Manifest.from_topdir(
                self.topdir,
                import_flags=ImportFlag.FORCE_MEMBERS,
                importer=self.update_importer,
            )
        except (ManifestImportFailed, MalformedManifest, ManifestVersionError) as err:
            self.die(f"cannot resolve the manifest: {err}")
        self.manifest = manifest

        failed = []
        for member in manifest.members:
            if isinstance(member, ManifestMember):
                continue
            if member.name in self.updated:
                continue
            if not manifest.is_active(member, extra_filter=self.extra_group_filter):
                self.dbg(f"skipping inactive member {member.name}")
                continue
            try:
                self.update_one(member)
            except subprocess.CalledProcessError:
                failed.append(member)
            except OSError as err:
                # E.g. the member's path is occupied by a plain file, or a permission problem; this
                # must not become a traceback.
                self.err(f"cannot update {member.name_and_path}: {err}")
                failed.append(member)
        self._finish(manifest, failed)

    def _finish(self, manifest, failed):
        if failed:
            names = ", ".join(m.name_and_path for m in failed)
            self.err(f"update failed for: {names}")
        # Also on partial failure: the members that did move are part of the on-disk state, and the
        # generated files must describe it or the CMake guard hash keeps blessing caches that no
        # longer match the repospace.
        self._generate(manifest)
        if failed:
            raise CommandError(1)

    def _generate(self, manifest):
        from repospace.app import generate

        try:
            generate.write_repospace_files(self, manifest, extra_filter=self.extra_group_filter)
        except OSError as err:
            self.die(f"cannot write repospace files: {err}")

    # -- restricted update -------------------------------------------------

    def _update_some(self, args):
        # Requested names must resolve against the full manifest when possible: a name may be
        # defined more than once across the import graph with only the first definition winning, and
        # a partial resolution can pick a shadowed definition (and so a different revision). Before
        # the members providing import data are updated, full resolution may be impossible; fall
        # back to resolving without member imports, so members declared in the manifest repository
        # itself can still be updated first.
        full_error = None
        try:
            manifest = Manifest.from_topdir(self.topdir)
        except (ManifestImportFailed, MalformedManifest, ManifestVersionError) as err:
            full_error = err
            try:
                manifest = Manifest.from_topdir(self.topdir, import_flags=ImportFlag.IGNORE_MEMBERS)
            except (ManifestImportFailed, MalformedManifest, ManifestVersionError) as narrow_err:
                self.die(f"cannot resolve the manifest: {narrow_err}")
        try:
            members = manifest.get_members(args.members)
        except ValueError as err:
            unknown = ", ".join(err.args[0])
            if full_error is not None:
                self.die(
                    f"unknown member(s): {unknown}; the manifest's "
                    f"imports could not be resolved ({full_error}), so "
                    "members declared there are unavailable; please "
                    'run plain "repospace update" first'
                )
            self.die(f"unknown member(s): {unknown}")
        self.manifest = manifest
        # Before anything is updated: rejecting this from inside the loop would leave the earlier
        # names already fetched and moved, with no chance to record them.
        if any(isinstance(member, ManifestMember) for member in members):
            self.die("the manifest repository cannot be updated")

        failed = []
        for member in members:
            try:
                self.update_one(member)
            except subprocess.CalledProcessError:
                failed.append(member)
            except OSError as err:
                self.err(f"cannot update {member.name_and_path}: {err}")
                failed.append(member)
        if failed:
            names = ", ".join(m.name_and_path for m in failed)
            self.err(f"update failed for: {names}")
        # Regenerate repospace files from a full resolution if possible, on partial failure too: see
        # _finish().
        try:
            full = Manifest.from_topdir(self.topdir)
        except Exception:
            self.wrn(
                "repospace files not regenerated: the full manifest could "
                'not be resolved; run plain "repospace update"'
            )
        else:
            self._generate(full)
        if failed:
            raise CommandError(1)

    # -- the importer callback --------------------------------------------

    def update_importer(self, member, path):
        # Only regular members reach here: self-imports from the manifest repository are read from
        # its filesystem, never through the importer.
        if member.name not in self.updated:
            # The manifest layer guarantees groups+import never combine, so there is no activity
            # question to answer here. A member already updated this run is not updated again:
            # resolving its manifest plus its self-imports takes several reads.
            try:
                self.update_one(member)
            except subprocess.CalledProcessError:
                # Resolution cannot continue without this member's manifest data, so there is
                # nothing to aggregate.
                self.die(
                    "cannot resolve the manifest: update failed for "
                    f"imported member {member.name_and_path}"
                )
            except OSError as err:
                # Unlike the git failure above, nothing was printed for this yet; include the cause.
                self.die(
                    "cannot resolve the manifest: update failed for "
                    f"imported member {member.name_and_path}: {err}"
                )
        self.updated.add(member.name)
        try:
            return member_manifest_content(member, path)
        except FileNotFoundError:
            self.die(
                f"can't import from member {member.name}: {path} not "
                f"found at {QUAL_MANIFEST_REV}"
            )
        except subprocess.CalledProcessError:
            self.die(
                f"can't import from member {member.name}: no "
                f"{QUAL_MANIFEST_REV}; the fetched revision "
                f'"{member.revision}" may be wrong'
            )

    # -- per-member update -------------------------------------------------

    def update_one(self, member: Member):
        self.banner(f"updating {member.name_and_path}:")
        started = time.perf_counter()

        self._check_confined(member)
        if not member.is_cloned():
            self._initialize(member)
        else:
            self._sync_remote(member)
        self._detach_from_manifest_rev(member)

        revision = member.revision
        try:
            if self.fetch_strategy == "smart" and self._rev_type(member, revision) in (
                "tag",
                "commit",
            ):
                self.dbg(f"{member.name}: {revision} is available locally; skipping fetch")
                sha = member.sha(revision)
            else:
                sha = self._fetch(member)
            member.git(
                [
                    "update-ref",
                    "-m",
                    f"repospace update: moving to {revision}",
                    QUAL_MANIFEST_REV,
                    sha,
                ]
            )
        finally:
            # Cleaned even when the fetch or update-ref fails: a stale scratch ref would shadow a
            # same-named revision in a later update (_fetch prefers the scratch ref).
            self._clean_scratch_refs(member)
        self._checkout(member, sha)
        if member.submodules:
            self._update_submodules(member)

        if self.args.stats:
            elapsed = time.perf_counter() - started
            self.inf(f"{member.name}: updated in {elapsed:.2f} s")

    def _check_confined(self, member: Member):
        # Member paths may not cross a symlink below the topdir, so no member (nor a symlink
        # committed inside one) can redirect another member's directory. The manifest layer rejects
        # this too, but it only sees the symlinks that exist when the manifest is loaded. Re-check
        # just before touching the member's directory: an earlier clone in this very update may have
        # created the symlink.
        link = _symlink_below_topdir(self.topdir, member.path)
        if link is not None:
            self.die(
                f"cannot update member {member.name_and_path}: {link} is "
                "a symbolic link; a member path may not go through one "
                "inside the repospace"
            )

    def _initialize(self, member: Member):
        self.small_banner(f"{member.name}: initializing")
        os.makedirs(member.abspath, exist_ok=True)
        init = ["init", "-q"]
        if git_version() >= (2, 28):
            # A placeholder branch name silences init.defaultBranch advice; it never gets a commit
            # and is never used.
            init += ["--initial-branch", _INIT_PLACEHOLDER_BRANCH]
        member.git(init)
        member.git(["remote", "add", "--", member.remote_name, member.url])

    def _sync_remote(self, member: Member):
        # The manifest says where a member comes from, and fetches use its URL directly; the
        # "origin" remote (repospace's own, see _initialize) must follow a URL change too, or "git
        # fetch origin" inside the member keeps using the old location. The raw configured value is
        # compared, not "remote get-url", which expands insteadOf rewrites and would mismatch on
        # every update.
        result = member.git(
            ["config", "--get", f"remote.{member.remote_name}.url"],
            check=False,
            capture_stdout=True,
            capture_stderr=True,
        )
        if result.returncode == 0:
            if result.stdout.decode(errors="replace").strip() == member.url:
                return
            member.git(["remote", "set-url", "--", member.remote_name, member.url])
        else:
            # A clone made by hand may lack the remote altogether.
            member.git(["remote", "add", "--", member.remote_name, member.url])
        self.small_banner(
            f'{member.name}: remote "{member.remote_name}" now points at {member.url}'
        )

    def _detach_from_manifest_rev(self, member: Member):
        # update-ref moves repospace-rev underneath an attached HEAD: the checkout that follows then
        # finds HEAD already at the new commit and leaves the index and working tree at the old one.
        # The branch is repospace's own, so unlike a user branch it is never kept (-k) or rebased
        # (-r): HEAD is detached from it where it stands, and the usual checkout takes over from
        # there.
        result = member.git(
            ["symbolic-ref", "-q", "HEAD"],
            check=False,
            capture_stdout=True,
            capture_stderr=True,
        )
        if result.returncode != 0 or _decode_ref(result.stdout) != QUAL_MANIFEST_REV:
            return
        if self._head_ok(member):
            member.git(["checkout", "-q", "--detach"])
        else:
            # Unborn (e.g. "git checkout --orphan repospace-rev"): there is no commit to detach at;
            # park HEAD on the placeholder branch instead, as a fresh repository starts out.
            member.git(["symbolic-ref", "HEAD", f"refs/heads/{_INIT_PLACEHOLDER_BRANCH}"])
        self.inf(f'{member.name}: detached HEAD from "{MANIFEST_REV}", which is owned by repospace')

    def _rev_type(self, member: Member, revision: str) -> str:
        result = member.git(
            ["cat-file", "-t", revision],
            check=False,
            capture_stdout=True,
            capture_stderr=True,
        )
        if result.returncode != 0:
            return "other"
        objtype = result.stdout.decode().strip()
        if objtype == "tag":
            return "tag"
        if objtype != "commit":
            return "other"
        result = member.git(
            ["rev-parse", "--verify", "--quiet", "--symbolic-full-name", revision],
            check=False,
            capture_stdout=True,
            capture_stderr=True,
        )
        if result.returncode != 0:
            return "other"
        symbolic = result.stdout.decode().strip()
        if not symbolic:
            # A raw commit id has no symbolic name.
            return "commit"
        if symbolic.startswith("refs/heads/"):
            return "branch"
        if symbolic.startswith("refs/tags/"):
            return "tag"
        return "other"

    @property
    def _git_quiet(self):
        # Below normal verbosity, silence the git subcommands that print progress on their own.
        return ["-q"] if self.verbosity < Verbosity.INF else []

    def _fetch(self, member: Member) -> str:
        revision = member.revision
        sha_like = _looks_like_sha(revision) and not self.narrow
        if sha_like:
            # Many servers refuse to serve a bare SHA; fetch all branch tips into a scratch
            # namespace and hope it is reachable.
            refspec = f"refs/heads/*:{QUAL_REFS}*"
        else:
            refspec = revision
        fetch = ["fetch", "-f"] + self._git_quiet
        if not self.narrow:
            fetch.append("--tags")
        if member.clone_depth:
            fetch += ["--depth", str(member.clone_depth)]
        fetch += self.args.fetch_opts
        fetch += ["--", member.url, refspec]
        self.small_banner(f"{member.name}: fetching, need revision {revision}")
        member.git(fetch)
        if sha_like:
            # An all-hex branch name misdetected as a SHA is not an object id, but its tip was just
            # fetched into the scratch namespace; prefer the ref, as git does for ambiguous names.
            for candidate in (f"{QUAL_REFS}{revision}", revision):
                result = member.git(
                    ["rev-parse", f"{candidate}^{{commit}}"],
                    check=False,
                    capture_stdout=True,
                    capture_stderr=True,
                )
                if result.returncode == 0:
                    return result.stdout.decode().strip()
            self.err(
                f"{member.name}: revision {revision} was not fetched; it "
                "is neither a branch name nor a commit reachable from a "
                "branch on the remote"
            )
            raise subprocess.CalledProcessError(result.returncode, result.args)
        # The two-step peel avoids "not a valid object name" for annotated tags, which cannot be
        # peeled inside a refspec.
        return member.sha("FETCH_HEAD")

    def _clean_scratch_refs(self, member: Member):
        # Best-effort: this also runs while an exception unwinds, and a cleanup failure must not
        # mask it; anything left over is removed by the next update of the same member.
        listing = member.git(
            ["for-each-ref", "--format", "%(refname)", QUAL_REFS],
            check=False,
            capture_stdout=True,
            capture_stderr=True,
        )
        if listing.returncode != 0:
            return
        # Scratch refs mirror the remote's branch names, which are not necessarily valid UTF-8;
        # decoding as os.fsdecode does keeps the bytes intact, so the deletion below names the very
        # ref that was listed.
        for ref in os.fsdecode(listing.stdout).splitlines():
            if ref:
                member.git(
                    ["update-ref", "-d", ref],
                    check=False,
                    capture_stdout=True,
                    capture_stderr=True,
                )

    def _head_ok(self, member: Member) -> bool:
        return member.git(["show-ref", "--quiet", "--head", "/"], check=False).returncode == 0

    def _checkout(self, member: Member, sha: str):
        if not self._head_ok(member):
            # Unborn HEAD, fresh repository: detach at the new revision.
            member.git(["checkout", "-q", "--detach", QUAL_MANIFEST_REV])
            return

        branch = _decode_ref(
            member.git(
                ["rev-parse", "--abbrev-ref", "HEAD"],
                capture_stdout=True,
            ).stdout
        )
        # "HEAD" is what git prints for a detached HEAD; it prints nothing at all when a ref named
        # HEAD (e.g. a tag) makes the name ambiguous, which is a detached HEAD just the same.
        detached = branch in ("HEAD", "")
        shown = _printable(branch)

        if not detached and self.args.keep_descendants and (member.is_ancestor_of(sha, branch)):
            self.small_banner(f'{member.name}: left descendant branch "{shown}" checked out')
            return
        if not detached and self.args.rebase:
            member.git(["rebase"] + self._git_quiet + [QUAL_MANIFEST_REV])
            return

        head = member.git(["rev-parse", "HEAD"], capture_stdout=True).stdout.decode().strip()
        if head == sha and detached:
            return
        member.git(["checkout", "-q", "--detach", sha])
        if not detached:
            self.inf(
                f'{member.name}: left branch "{shown}"; to switch back: '
                f"git -C {member.path} checkout {shown}"
            )

    def _update_submodules(self, member: Member):
        strategy = "--rebase" if self.args.rebase else "--checkout"
        quiet = ["--quiet"] if self._git_quiet else []
        if self.config.getboolean("update.sync-submodules", default=True):
            sync = ["submodule"] + quiet + ["sync", "--recursive"]
            if isinstance(member.submodules, list):
                for submodule in member.submodules:
                    member.git(sync + ["--", submodule.path])
            else:
                member.git(sync)
        base = ["submodule"] + quiet + ["update", "--init", strategy, "--recursive"]
        if isinstance(member.submodules, list):
            for submodule in member.submodules:
                member.git(base + ["--", submodule.path])
        else:
            member.git(base)
