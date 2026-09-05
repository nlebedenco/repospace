"""Shared helpers for built-in commands that operate on members."""

from __future__ import annotations

from typing import List

from repospace.commands import RepospaceCommand
from repospace.manifest import Member, ManifestMember


class MemberCommand(RepospaceCommand):
    """Base class for commands taking an optional list of members."""

    def add_members_arg(self, parser, help_text="member names or paths"):
        parser.add_argument("members", metavar="MEMBER", nargs="*", help=help_text)

    def selected_members(
        self,
        args,
        include_manifest: bool = False,
        only_active: bool = True,
        only_cloned: bool = False,
        unknown_hint: str = "",
    ) -> List[Member]:
        """Resolve the command's member arguments.

        Naming members explicitly bypasses the group filter (and includes
        the manifest repository if named). *unknown_hint* is appended to
        the error for a name that is not a member.
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
            if only_cloned and not member.is_cloned():
                if explicit:
                    self.die(f"member {member.name_and_path} is not cloned")
                continue
            result.append(member)
        return result
