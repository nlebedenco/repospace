"""Shared helpers for built-in commands that operate on members."""

from __future__ import annotations

from typing import List

from repospace.commands import RepospaceCommand
from repospace.manifest import QUAL_REFS, ManifestMember, Member

#: How a ref name crosses between bytes and str; see decode_ref.
_ENCODING = "utf-8"
_ERRORS = "surrogateescape"


def decode_ref(data: bytes) -> str:
    """Decode a git ref name so the exact bytes reach git again.

    Ref names are byte strings; git accepts (and can be made to create) names that are not valid
    UTF-8. Undecodable bytes are kept as surrogates, which subprocess re-encodes unchanged when the
    name is passed back on a command line.

    Spelled out rather than left to os.fsdecode, whose error handler is the platform's: POSIX uses
    "surrogateescape", which is what this needs, but Windows uses "surrogatepass", which raises on
    a byte that is not valid UTF-8. A ref name only has to be valid UTF-8 where it was created, so
    a Windows client listing a POSIX-hosted remote would meet one and die with a traceback.
    """
    return data.decode(_ENCODING, _ERRORS).strip()


def printable(text: str) -> str:
    """Return *text* with undecodable bytes shown as escapes.

    A name from decode_ref may carry surrogates, which a strict UTF-8 stdout refuses to encode;
    messages must survive printing it.
    """
    raw = text.encode(_ENCODING, _ERRORS)
    return raw.decode(_ENCODING, "backslashreplace")


def clean_scratch_refs(member: Member, prefix: str = QUAL_REFS) -> None:
    """Delete every ref of *member* below *prefix*, best effort.

    This also runs while an exception unwinds, and a cleanup failure must not mask it; anything
    left over is removed by the next update of the same member, which cleans all of QUAL_REFS.
    """
    listing = member.git(
        ["for-each-ref", "--format", "%(refname)", prefix],
        check=False,
        capture_stdout=True,
        capture_stderr=True,
    )
    if listing.returncode != 0:
        return
    # Every deletion in one git invocation. A repository can have thousands of tags, and the scratch
    # namespace holds one ref per fetched ref, so a process per ref would be thousands of processes
    # on every run -- more time spent forking than deleting, on a machine whose process table is not
    # the caller's to fill. The -z command format is "delete SP <ref> NUL <oldvalue> NUL"; leaving
    # <oldvalue> empty asks for no verification, so a ref that moved or vanished underneath cannot
    # abort the transaction the other deletions share.
    #
    # The names go back as the bytes for-each-ref printed them: scratch refs mirror the remote's ref
    # names, which are not necessarily valid UTF-8, and a ref name holds no newline, so splitting on
    # lines names the very refs that were listed.
    commands = b"".join(b"delete " + line + b"\0\0" for line in listing.stdout.splitlines() if line)
    if not commands:
        return
    member.git(
        ["update-ref", "-z", "--stdin"],
        check=False,
        capture_stdout=True,
        capture_stderr=True,
        stdin_data=commands,
    )


class MemberCommand(RepospaceCommand):
    """Base class for commands taking an optional list of members."""

    def add_members_arg(self, parser, help_text="member names or paths"):
        parser.add_argument("members", metavar="MEMBER", nargs="*", help=help_text)

    def require_cloned(self, member: Member, explicit: bool) -> bool:
        """Say whether *member* is cloned; die when it was named explicitly and is not.

        A member the caller named must be worked on or not at all, while one that only came from
        the manifest is skipped: it has no content to work on yet, which "update" fixes.
        """
        if member.is_cloned():
            return True
        if explicit:
            self.die(f"member {member.name_and_path} is not cloned")
        return False

    def selected_members(
        self,
        args,
        include_manifest: bool = False,
        only_active: bool = True,
        only_cloned: bool = False,
        unknown_hint: str = "",
    ) -> List[Member]:
        """Resolve the command's member arguments.

        Naming members explicitly bypasses the group filter (and includes the manifest repository if
        named). *unknown_hint* is appended to the error for a name that is not a member.
        """
        manifest = self.manifest
        explicit = bool(getattr(args, "members", None))
        if explicit:
            try:
                members = manifest.get_members(args.members)
            except ValueError as err:
                unknown = ", ".join(err.args[0])
                self.die(f"unknown member(s): {unknown}{unknown_hint}")
        else:
            members = manifest.get_members([])
        result = []
        for member in members:
            if isinstance(member, ManifestMember):
                if not (include_manifest or explicit):
                    continue
            elif not explicit and only_active and not manifest.is_active(member):
                continue
            if only_cloned and not self.require_cloned(member, explicit):
                continue
            result.append(member)
        return result
