"""The list, manifest, and topdir commands."""

from __future__ import annotations

import json
import os
import subprocess

from repospace import util
from repospace.app.common import MemberCommand
from repospace.app.generate import MEMBERS_JSON
from repospace.commands import HelpFormatter, RepospaceCommand
from repospace.manifest import (
    QUAL_MANIFEST_REV,
    ImportFlag,
    MalformedManifest,
    Manifest,
    ManifestMember,
    manifest_file,
)

_DEFAULT_FORMAT = "{name:12} {path:28} {revision:40} {url}"

_LIST_DESCRIPTION = f"""\
Print information about members, one per line, using FORMAT.

FORMAT is a Python format string; the default is
"{_DEFAULT_FORMAT}".
Available keys: name, description, url, path, abspath, posixpath,
revision, sha, cloned, active, clone_depth, groups, declared_by, stale.

The "stale" key (also appended to default-format output) flags members
whose on-disk state may not match the manifest: not-cloned, diverged
(HEAD moved off repospace-rev), update-needed (declared tag/commit no
longer matches repospace-rev), and revision/url/path-changed or added
(manifest edited since the last update, detected via the
.repospace/members.json snapshot).
"""


class Topdir(RepospaceCommand):
    def __init__(self):
        super().__init__(
            "topdir",
            "print the repospace top-level directory",
            "Print the absolute path of the repospace top-level directory.",
        )

    def do_add_parser(self, parser_adder):
        return parser_adder.add_parser(self.name, help=self.help, description=self.description)

    def do_run(self, args, unknown):
        print(util.topdir(self.topdir))


class _LazyFormatMap:
    """Format-key lookup that computes expensive values on demand."""

    def __init__(self, command: "List", member, snapshot):
        self.command = command
        self.member = member
        self.snapshot = snapshot

    def __getitem__(self, key):
        member = self.member
        if key == "name":
            return member.name
        if key == "description":
            return member.description or "N/A"
        if key == "url":
            return member.url or "N/A"
        if key == "path":
            return member.path or "N/A"
        if key == "abspath":
            return member.abspath or "N/A"
        if key == "posixpath":
            return member.posixpath or "N/A"
        if key == "revision":
            return member.revision or "N/A"
        if key == "clone_depth":
            depth = member.clone_depth
            return "N/A" if depth is None else str(depth)
        if key == "groups":
            return ",".join(member.groups)
        if key == "declared_by":
            return member.declared_by or "N/A"
        if key == "cloned":
            return "cloned" if member.is_cloned() else "not-cloned"
        if key == "active":
            manifest = self.command.manifest
            return "active" if manifest.is_active(member) else "inactive"
        if key == "sha":
            if isinstance(member, ManifestMember) or not member.is_cloned():
                return "N/A"
            try:
                return member.sha(QUAL_MANIFEST_REV, capture_stderr=True)
            except subprocess.CalledProcessError:
                return "N/A"
        if key == "stale":
            return ",".join(self.command.stale_reasons(member, self.snapshot))
        raise KeyError(key)


class List(MemberCommand):
    def __init__(self):
        super().__init__(
            "list",
            "print information about members",
            _LIST_DESCRIPTION,
        )

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(
            self.name,
            help=self.help,
            description=self.description,
            formatter_class=HelpFormatter,
        )
        self.add_members_arg(parser)
        parser.add_argument(
            "-a",
            "--all",
            action="store_true",
            help="include inactive members",
        )
        parser.add_argument(
            "-i",
            "--inactive",
            action="store_true",
            help="list only inactive members",
        )
        parser.add_argument(
            "-f",
            "--format",
            dest="fmt",
            metavar="FORMAT",
            help="Python format string; see the command description",
        )
        return parser

    def do_run(self, args, unknown):
        if args.inactive and args.members:
            self.parser.error("-i cannot be combined with an explicit member list")
        manifest = self.manifest
        snapshot = self._load_snapshot()
        fmt = args.fmt or _DEFAULT_FORMAT
        default_fmt = args.fmt is None

        members = self.selected_members(args, include_manifest=True, only_active=False)
        for member in members:
            active = isinstance(member, ManifestMember) or manifest.is_active(member)
            if not args.members:
                if args.inactive:
                    if active:
                        continue
                elif not (args.all or active):
                    continue
            try:
                line = fmt.format_map(_LazyFormatMap(self, member, snapshot))
            except KeyError as err:
                self.die(f"unknown format key {err}")
            except (ValueError, IndexError) as err:
                self.die(f"malformed format string: {err}")
            if default_fmt:
                reasons = self.stale_reasons(member, snapshot)
                if reasons:
                    line += f' (stale: {",".join(reasons)})'
            print(line)

        self._warn_snapshot_issues(manifest, snapshot)

    # -- staleness ---------------------------------------------------------

    def _load_snapshot(self):
        path = os.path.join(self.topdir, util.REPOSPACE_DIR, MEMBERS_JSON)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return {m["name"]: m for m in data["members"]}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _warn_snapshot_issues(self, manifest, snapshot):
        if snapshot is None:
            self.wrn(
                'no usable members.json snapshot; run "repospace update" '
                "(staleness detection is limited to git state)"
            )
            return
        live = {m.name for m in manifest.members}
        for name in snapshot:
            if name != "manifest" and name not in live:
                self.wrn(
                    f'member "{name}" from the last update is no longer '
                    "in the manifest (its directory, if any, was not "
                    "deleted)"
                )

    def stale_reasons(self, member, snapshot):
        if isinstance(member, ManifestMember):
            return []
        reasons = []
        cloned = member.is_cloned()
        if self.manifest.is_active(member) and not cloned:
            reasons.append("not-cloned")
        if snapshot is not None:
            entry = snapshot.get(member.name)
            if entry is None:
                reasons.append("added")
            else:
                for key, value in (
                    ("revision", member.revision),
                    ("url", member.url),
                    ("path", member.path),
                ):
                    if entry.get(key) != value:
                        reasons.append(f"{key}-changed")
        if cloned:
            reasons.extend(self._git_reasons(member))
        return reasons

    def _git_reasons(self, member):
        reasons = []
        try:
            rev_sha = member.sha(QUAL_MANIFEST_REV, capture_stderr=True)
        except subprocess.CalledProcessError:
            return ["no-repospace-rev"]
        head = member.git(
            ["rev-parse", "HEAD"],
            check=False,
            capture_stdout=True,
            capture_stderr=True,
        )
        if head.returncode == 0 and head.stdout.decode().strip() != rev_sha:
            reasons.append("diverged")
        # A declared tag or commit that peels locally but no longer
        # matches repospace-rev means the manifest was edited (or the
        # tag moved) without an update.
        peeled = member.git(
            ["rev-parse", f"{member.revision}^{{commit}}"],
            check=False,
            capture_stdout=True,
            capture_stderr=True,
        )
        if peeled.returncode == 0:
            symbolic = member.git(
                ["rev-parse", "--verify", "--quiet", "--symbolic-full-name", member.revision],
                check=False,
                capture_stdout=True,
                capture_stderr=True,
            )
            is_branch = symbolic.returncode == 0 and symbolic.stdout.decode().strip().startswith("refs/heads/")
            if not is_branch and (peeled.stdout.decode().strip() != rev_sha):
                reasons.append("update-needed")
        return reasons


class ManifestCommand(RepospaceCommand):
    def __init__(self):
        super().__init__(
            "manifest",
            "inspect or validate the manifest",
            "Resolve, freeze, or validate the repospace manifest.",
        )

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, help=self.help, description=self.description)
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument(
            "--resolve",
            action="store_true",
            help="print the fully resolved manifest, imports expanded",
        )
        group.add_argument(
            "--freeze",
            action="store_true",
            help="like --resolve, with revisions pinned to exact commits",
        )
        group.add_argument(
            "--validate",
            action="store_true",
            help="validate the manifest and exit (imports not followed)",
        )
        group.add_argument(
            "--path",
            action="store_true",
            help="print the path of the top-level manifest file",
        )
        parser.add_argument("-o", "--out", metavar="FILE", help="write output to FILE")
        parser.add_argument(
            "--active-only",
            action="store_true",
            help="restrict --resolve/--freeze to active members",
        )
        return parser

    def _manifest_file_path(self) -> str:
        # Honors the manifest.file configuration option.
        return manifest_file(self.topdir, self._config if self.has_config else None)

    def _full_manifest(self) -> Manifest:
        if self.has_manifest:
            return self.manifest
        try:
            return Manifest.from_topdir(self.topdir)
        except Exception as err:
            self.die(f"cannot resolve the manifest: {err}")

    def do_run(self, args, unknown):
        # Every accepted option must reach the selected mode: the ones
        # it cannot honor are rejected rather than dropped.
        if args.active_only and (args.validate or args.path):
            self.parser.error("--active-only cannot be combined with --validate/--path")
        if args.validate:
            if args.out:
                self.parser.error("-o/--out cannot be combined with --validate, which prints nothing")
            try:
                Manifest.from_file(
                    self._manifest_file_path(),
                    topdir=self.topdir,
                    import_flags=ImportFlag.IGNORE,
                )
            except Exception as err:
                self.die(f"invalid manifest: {err}")
            return

        if args.path:
            try:
                output = self._manifest_file_path() + "\n"
            except MalformedManifest as err:
                self.die(str(err))
        else:
            manifest = self._full_manifest()
            try:
                if args.freeze:
                    output = manifest.as_frozen_yaml(active_only=args.active_only)
                else:
                    output = manifest.as_yaml(active_only=args.active_only)
            except RuntimeError as err:
                self.die(str(err))
        self._emit(output, args.out)

    def _emit(self, output: str, out):
        if not out:
            print(output, end="")
            return
        try:
            with open(out, "w", encoding="utf-8") as f:
                f.write(output)
        except OSError as err:
            self.die(f"cannot write {out}: {err}")
