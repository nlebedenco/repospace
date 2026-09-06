#!/usr/bin/env python

import argparse
import subprocess
import sys

# git's default core.commentChar; a custom one is not supported here (the template in .gitmessage
# relies on '#' too).
COMMENT = b"#"
# With `git commit -v` (or commit.verbose) and commit.status on, git appends the staged diff to the
# message file below this line WITHOUT commenting it out, and only truncates the message there
# itself when the commit is verbose. A backup taken past this line would therefore be restored, pass
# gitlint (which also stops at this line) and be committed as message text by a later non-verbose
# attempt.
CUTLINE = b"# ------------------------ >8 ------------------------"


def message_lines(data):
    """
    Return the lines above git's cut line (all of them if there is none), keeping each line's own
    terminator.
    """
    lines = []
    for line in data.splitlines(True):
        if line.rstrip(b"\r\n") == CUTLINE:
            break
        lines.append(line)
    return lines


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("msg_filename", metavar="MSG_FILENAME")
    args = parser.parse_args()

    src = args.msg_filename
    dst = args.msg_filename + ".orig"

    command = ["gitlint", "--staged", "--msg-filename", src]
    try:
        r = subprocess.run(command, check=False)
    except FileNotFoundError:
        print(
            f"{parser.prog}: error: {command[0]} not found on PATH",
            file=sys.stderr,
        )
        return 1

    if r.returncode != 0:
        # Bytes in, bytes out: the commit message encoding is git's business (i18n.commitEncoding)
        # and the comment char, the cut line and blanks are ASCII either way, so nothing here has to
        # decode the message and risk failing on it - which would lose the backup this script exists
        # to make.
        with open(src, "rb") as f:
            lines = message_lines(f.read())
        # Back up the message for it to be re-used in the next attempt to commit BUT ONLY if it is
        # not empty; otherwise the user has just tried to abort this commit and backing up an empty
        # message would only replace the fresh template with stale comments in the next attempt.
        if any(x.strip() and not x.startswith(COMMENT) for x in lines):
            with open(dst, "wb") as f:
                f.write(b"".join(lines))

    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
