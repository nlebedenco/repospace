"""Git convenience commands: diff, status, forall, compare, grep."""

from __future__ import annotations

import os
import posixpath
import shlex
import subprocess
import sys

from repospace import ansi, util
from repospace.app.common import MemberCommand
from repospace.commands import CommandError, Verbosity
from repospace.manifest import QUAL_MANIFEST_REV, ManifestMember


class Diff(MemberCommand):
    def __init__(self):
        super().__init__(
            "diff",
            "run git diff on members",
            "Run git diff on each cloned member; extra arguments are passed through to git diff. "
            "Everything after the first -- goes to git diff untouched; use it for git diff's own "
            "revisions and paths, which are otherwise read as member names, and for options that "
            "collide with this command's (e.g. -a). "
            "When no member has differences, a single line says so. "
            '"repospace -qq diff" relays --quiet to git diff (exit status only).',
            accepts_unknown_args=True,
            forward_dashdash=True,
        )

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, help=self.help, description=self.description)
        self.add_members_arg(parser)
        parser.add_argument(
            "-a",
            "--all",
            action="store_true",
            help="show a banner even for members with no diff",
        )
        return parser

    def do_run(self, args, unknown):
        self.die_if_no_git()
        # git always sees a pipe (output is captured to interleave banners), so it must be told
        # explicitly to color. Placed before the pass-through arguments so the user's flags win.
        color = ansi.use_color(sys.stdout, self.color_ui)
        differences = False
        printed = False
        members = self.selected_members(
            args,
            only_cloned=True,
            unknown_hint='; arguments meant for git diff go after "--"',
        )
        for member in members:
            command = [
                "diff",
                f"--src-prefix={member.path}/",
                f"--dst-prefix={member.path}/",
                "--expand-tabs",
            ]
            if color:
                command.append("--color=always")
            # A single -q only drops repospace's banners, keeping the patch output clean for piping;
            # -qq relays quiet to git diff itself (exit status only, implies --exit-code).
            if self.verbosity < Verbosity.WRN:
                command.append("--quiet")
            result = member.git(
                command + unknown,
                capture_stdout=True,
                capture_stderr=True,
                check=False,
            )
            # git diff exits 1 to signal differences under pass-through flags such as --exit-code;
            # only higher codes are errors. A negative returncode (git died on a signal) maps to the
            # conventional 128 + signal, as in Grep and main(); passed through, it would be
            # truncated modulo 256.
            if result.returncode not in (0, 1):
                detail = result.stderr.decode(errors="backslashreplace").strip() or (
                    f"git diff died on signal {-result.returncode}"
                    if result.returncode < 0
                    else f"git diff exited with status {result.returncode}"
                )
                self.err(f"{member.name_and_path}: {detail}")
                code = result.returncode
                raise CommandError(code if code > 0 else 128 - code)
            if result.returncode:
                differences = True
            output = result.stdout.decode(errors="backslashreplace")
            if output:
                printed = True
                self.banner(f"diff for {member.name_and_path}:")
                print(output, end="")
            elif args.all:
                # A banner with nothing under it is the silence -a was meant to break; give the
                # verdict instead, as "compare -a" does. Which verdict it is comes from the exit
                # status, not from the empty output: a pass-through flag such as --quiet reports
                # differences without printing them.
                verdict = "differences not shown" if result.returncode else "no differences"
                self.banner(f"diff for {member.name_and_path}: {verdict}")
        if not members:
            self.banner("no members to diff")
        elif not (printed or differences or args.all):
            # As in compare: silence would read as a command that did nothing. Banners already rule
            # out piping the output as a patch; "repospace -q diff" keeps it clean. Both signals are
            # needed: plain git diff exits 0 whether or not it printed a patch, while a pass-through
            # flag such as --quiet reports differences it does not print. With -a every member has
            # already said where it stands on a line of its own.
            self.banner("no differences")
        if differences:
            raise CommandError(1)


class Status(MemberCommand):
    def __init__(self):
        super().__init__(
            "status",
            "run git status on members",
            "Run git status on each cloned member; extra arguments are "
            'passed through to git status. "repospace -v status" relays '
            "--verbose to git status.",
            accepts_unknown_args=True,
        )

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, help=self.help, description=self.description)
        self.add_members_arg(parser)
        return parser

    def do_run(self, args, unknown):
        self.die_if_no_git()
        command = ["status"]
        if self.verbosity >= Verbosity.DBG:
            command.append("--verbose")
        for member in self.selected_members(args, only_cloned=True):
            self.banner(f"status of {member.name_and_path}:")
            member.git(command + unknown)


class ForAll(MemberCommand):
    def __init__(self):
        super().__init__(
            "forall",
            "run a shell command in each member's directory",
            "Run a shell command in each cloned member's directory. The environment contains "
            "REPOSPACE_MEMBER_NAME, REPOSPACE_MEMBER_PATH, REPOSPACE_MEMBER_ABSPATH, "
            "REPOSPACE_MEMBER_REVISION, REPOSPACE_MEMBER_URL, and REPOSPACE_MEMBER_REMOTE. "
            "Every member runs, whatever the previous ones did; a failure in any of them exits 1, "
            "or, if a command was killed by a signal, with 128 + the first such signal.",
        )

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, help=self.help, description=self.description)
        self.add_members_arg(parser)
        parser.add_argument(
            "-c",
            dest="command",
            metavar="COMMAND",
            required=True,
            help="shell command to run in each member",
        )
        parser.add_argument(
            "-a",
            "--all",
            action="store_true",
            help="include inactive members",
        )
        return parser

    def do_run(self, args, unknown):
        """Run the command everywhere, then report every failure.

        A command killed by a signal reports that signal and makes the exit status 128 + it, as in
        Diff, Grep and main(); a negative status would otherwise be truncated modulo 256. With more
        than one, the first signal wins, since only one status can be returned and no member's
        failure is more important than another's.
        """
        failed = []
        signal = None
        for member in self.selected_members(args, only_active=not args.all, only_cloned=True):
            env = dict(os.environ)
            env.update(
                REPOSPACE_MEMBER_NAME=member.name,
                REPOSPACE_MEMBER_PATH=member.path or "",
                REPOSPACE_MEMBER_ABSPATH=member.abspath or "",
                REPOSPACE_MEMBER_REVISION=member.revision or "",
                REPOSPACE_MEMBER_URL=member.url or "",
                REPOSPACE_MEMBER_REMOTE=member.remote_name or "",
            )
            self.banner(f"running in {member.name_and_path}:")
            result = subprocess.run(args.command, shell=True, cwd=member.abspath, env=env)
            if result.returncode:
                if result.returncode < 0 and signal is None:
                    signal = -result.returncode
                failed.append((member, result.returncode))
        if failed:
            names = ", ".join(
                (
                    f"{member.name_and_path} (killed by signal {-code})"
                    if code < 0
                    else member.name_and_path
                )
                for member, code in failed
            )
            self.err(f"command failed in: {names}")
            raise CommandError(128 + signal if signal else 1)


class Compare(MemberCommand):
    def __init__(self):
        super().__init__(
            "compare",
            "compare member checkouts against the manifest",
            "Compare each cloned member's HEAD against repospace-rev (the manifest revision as of "
            "the last update). By default only members with differences are printed; when there "
            "are none, a single line says so.",
        )

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, help=self.help, description=self.description)
        self.add_members_arg(parser)
        parser.add_argument(
            "-a",
            "--all",
            action="store_true",
            help="print all members, even without differences",
        )
        parser.add_argument(
            "--exit-code",
            action="store_true",
            help="exit with status 1 if any member has differences",
        )
        return parser

    def do_run(self, args, unknown):
        self.die_if_no_git()
        differences = 0
        members = self.selected_members(args, only_cloned=True)
        for member in members:
            if isinstance(member, ManifestMember):
                # Only reachable by explicit naming; without an explicit member list,
                # selected_members already excludes it.
                self.die("the manifest repository has no repospace-rev and cannot be compared")
            report = self._compare_one(member)
            if report is None:
                if args.all:
                    self.banner(f"{member.name_and_path}: up to date")
                continue
            differences += 1
            self.banner(f"{member.name_and_path}:")
            for line in report:
                print(f"    {line}")
        if not members:
            self.banner("no members to compare")
        elif not differences and not args.all:
            # Silence would read as a command that did nothing; say the comparison ran and found
            # nothing. With -a every member already said so on its own line, and "repospace -q
            # compare" still prints nothing, for scripts.
            self.banner("all members are up to date")
        if args.exit_code and differences:
            raise CommandError(1)

    def _compare_one(self, member):
        try:
            rev_sha = member.sha(QUAL_MANIFEST_REV, capture_stderr=True)
        except subprocess.CalledProcessError:
            return [f'no {QUAL_MANIFEST_REV}; run "repospace update"']
        head = member.git(
            ["rev-parse", "HEAD"],
            check=False,
            capture_stdout=True,
            capture_stderr=True,
        )
        if head.returncode != 0:
            # repospace-rev exists but HEAD is unborn: an update was interrupted between update-ref
            # and checkout.
            return ['no commit checked out; run "repospace update"']
        head_sha = head.stdout.decode().strip()
        # A branch name is a byte string git does not require to be valid UTF-8, and it is only ever
        # printed here; undecodable bytes become escapes rather than an exception. Empty output
        # means a ref named HEAD (e.g. a tag) made the name ambiguous, which is a detached HEAD just
        # like the literal "HEAD".
        branch = (
            member.git(["rev-parse", "--abbrev-ref", "HEAD"], capture_stdout=True)
            .stdout.decode(errors="backslashreplace")
            .strip()
        )
        detached = branch in ("HEAD", "")
        dirty = bool(member.git(["status", "--porcelain"], capture_stdout=True).stdout.strip())
        report = []
        if head_sha != rev_sha:
            counts = (
                member.git(
                    [
                        "rev-list",
                        "--left-right",
                        "--count",
                        f"{QUAL_MANIFEST_REV}...HEAD",
                    ],
                    capture_stdout=True,
                )
                .stdout.decode()
                .split()
            )
            behind, ahead = counts[0], counts[1]
            where = "detached" if detached else f'branch "{branch}"'
            report.append(f"HEAD: {head_sha[:12]} ({where})")
            report.append(f"repospace-rev: {rev_sha[:12]}")
            report.append(f"ahead {ahead}, behind {behind}")
        elif not detached:
            report.append(f'checked out branch "{branch}" at repospace-rev')
        if dirty:
            report.append("working tree has uncommitted changes")
        return report or None


def _glob_escape(value: str) -> str:
    """Escape glob metacharacters so *value* matches literally.

    ripgrep --glob patterns and grep --exclude-dir patterns are both glob-matched, and both honor
    backslash escapes. Backslashes cannot occur in member paths (the manifest layer rejects them).
    """
    return "".join("\\" + c if c in "*?[]{}" else c for c in value)


def _nested_in(path: str, base: str):
    """Return *path* relative to *base*, or None if not strictly inside.

    Both are normalized posix member paths; the repospace root is ".".
    """
    if base == ".":
        return None if path == "." else path
    if path == base or not path.startswith(base + "/"):
        return None
    return path[len(base) + 1 :]


class Grep(MemberCommand):
    _TOOLS = ("git-grep", "ripgrep", "grep")

    def __init__(self):
        super().__init__(
            "grep",
            "search members for a pattern",
            "Search the manifest repository and cloned members for a pattern, using git grep, "
            "ripgrep, or grep. Extra arguments are passed to the tool. Everything after the first "
            "-- goes to the tool untouched; use it for arguments that collide with this command's "
            "own options (e.g. -m). "
            '"repospace -qq grep" relays -q to the tool (exit status only).',
            accepts_unknown_args=True,
            forward_dashdash=True,
        )

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, help=self.help, description=self.description)
        parser.add_argument(
            "-m",
            "--member",
            dest="members",
            action="append",
            default=[],
            metavar="MEMBER",
            help="restrict the search to this member (name or path); may be repeated",
        )
        parser.add_argument(
            "--tool",
            choices=self._TOOLS,
            help="search tool (default: grep.tool config or git-grep)",
        )
        parser.add_argument("--tool-path", help="path to the search tool executable")
        return parser

    def _member_dir_exclusions(self, tool, searched):
        """Options keeping the search of *searched* out of the directories of the members nested
        inside it, which are searched separately (git grep never sees them; they are separate
        repositories, not tracked files). Member paths may nest, so this is not the manifest
        repository's privilege."""
        base = posixpath.normpath(searched.path or ".")
        options = []
        for member in self.manifest.members:
            if member.path is None:
                continue
            path = _nested_in(posixpath.normpath(member.path), base)
            if path is None:
                continue
            if tool == "ripgrep":
                # The leading "/" anchors the gitignore-style glob to the search root, excluding
                # exactly the member path.
                options.append(f"--glob=!/{_glob_escape(path)}")
            else:
                # grep can only exclude by directory name, so a directory that merely shares a
                # nested member directory's basename is excluded too; the price of plain grep having
                # no path-anchored filters.
                options.append(f"--exclude-dir={_glob_escape(posixpath.basename(path))}")
        return list(dict.fromkeys(options))

    def do_run(self, args, unknown):
        self.die_if_no_git()
        tool = args.tool or self.config.get("grep.tool", default="git-grep")
        if tool not in self._TOOLS:
            self.die(f'invalid grep.tool "{tool}"; choose from {self._TOOLS}')
        tool_path = args.tool_path or self.config.get(f"grep.{tool}-path")
        extra = self.config.get(f"grep.{tool}-args")
        extra_args = []
        if extra:
            try:
                # Shell-split, like aliases: quoted arguments (globs, patterns with spaces) reach
                # the tool as one word.
                extra_args = shlex.split(extra)
            except ValueError as err:
                self.wrn(f"ignoring grep.{tool}-args ({err}): {extra}")
        if not unknown and not extra_args:
            # With configured extras the tool is the judge: they may carry the pattern themselves
            # (e.g. "-e foo").
            self.die("missing search pattern")

        # The tools always see a pipe (output is captured to interleave banners), so they must be
        # told explicitly to color.
        color = ansi.use_color(sys.stdout, self.color_ui)

        matched = False
        explicit = bool(args.members)
        for member in self.selected_members(args, include_manifest=True):
            # The manifest repository's files are on disk whether or not it is a git repository
            # (repospace init accepts a plain directory); only git grep requires one. Regular
            # members have no content at all until cloned.
            if isinstance(member, ManifestMember):
                if tool == "git-grep" and not member.is_cloned():
                    message = (
                        "the manifest repository is not a git repository, "
                        "so git grep cannot search it; use --tool ripgrep "
                        "or --tool grep"
                    )
                    if explicit:
                        self.die(message)
                    self.wrn(message)
                    continue
            elif not self.require_cloned(member, explicit):
                continue
            if tool == "git-grep":
                command = [tool_path or "git", "grep"]
            elif tool == "ripgrep":
                command = [tool_path or "rg"]
                command += self._member_dir_exclusions(tool, member)
            else:
                # Plain grep has no gitignore support; at least keep it out of the git object and
                # metadata directories, in every repository searched.
                command = [
                    tool_path or "grep",
                    "-r",
                    "--exclude-dir=.git",
                    f"--exclude-dir={util.REPOSPACE_DIR}",
                ]
                command += self._member_dir_exclusions(tool, member)
            if color:
                command.append("--color=always")
            # A single -q only drops repospace's banners, keeping the match output clean for piping;
            # -qq relays quiet to the tool itself (exit status only). All three tools take -q.
            if self.verbosity < Verbosity.WRN:
                command.append("-q")
            command += extra_args + unknown
            if tool == "grep":
                command.append(".")
            try:
                result = self.run_subprocess(
                    command,
                    cwd=member.abspath,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    errors="backslashreplace",
                )
            except FileNotFoundError:
                self.die(
                    f'search tool not found: "{command[0]}"; use '
                    f"--tool-path or set the grep.{tool}-path option"
                )
            except OSError as err:
                # E.g. a --tool-path that exists but is not executable; same clean exit as a missing
                # tool, not a traceback.
                self.die(
                    f'cannot run the search tool "{command[0]}": {err}; '
                    f"use --tool-path or set the grep.{tool}-path option"
                )
            if result.returncode == 0:
                # Exit status, not output, decides: with a quiet flag (relayed or passed by the
                # user) the tools match silently.
                matched = True
                if result.stdout:
                    self.banner(f"{member.name_and_path}:")
                    print(result.stdout, end="")
            elif result.returncode != 1:
                # 1 means "no matches"; anything else, including a negative returncode (tool killed
                # by a signal), is an error.
                detail = result.stderr.strip() or (
                    f"search tool died on signal {-result.returncode}"
                    if result.returncode < 0
                    else f"search tool exited with status {result.returncode}"
                )
                self.err(f"{member.name_and_path}: {detail}")
                code = result.returncode
                raise CommandError(code if code > 0 else 128 - code)
        if not matched:
            raise CommandError(1)
