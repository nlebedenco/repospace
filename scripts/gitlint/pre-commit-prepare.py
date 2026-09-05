#!/usr/bin/env python

import argparse
import os
import shutil
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("msg_filename", metavar="MSG_FILENAME")
    args = parser.parse_args()

    # pre-commit's local-hook runner never forwards git's real "source"/"commit" arguments
    # positionally to the hook entry (it only appends MSG_FILENAME); it exposes them via the
    # PRE_COMMIT_COMMIT_MSG_SOURCE env var instead. git sets this to message/template/merge/
    # squash/commit, and leaves it unset entirely for a plain editor-based `git commit`.
    source = os.environ.get("PRE_COMMIT_COMMIT_MSG_SOURCE")

    src = args.msg_filename + ".orig"
    dst = args.msg_filename
    if os.path.exists(src):
        # Only restore when git prepared the message file itself with no real user-supplied
        # content (no source, or the configured commit.template). If the user gave an explicit
        # message (-m/-F), is reusing a specific commit (-c/-C/--amend), or this is a
        # merge/squash message, that content must take precedence over the stale backup.
        if source in (None, "template"):
            shutil.move(src, dst)
        else:
            # The backup doesn't apply to this attempt; discard it so it doesn't wrongly
            # resurface and overwrite an unrelated, later editor-based commit message.
            os.remove(src)


if __name__ == "__main__":
    sys.exit(main())
