#!/usr/bin/env python

import argparse
import sys

# The blanks sed's [[:space:]] would strip, minus the line terminators, which are put back after the
# trim.
BLANKS = b" \t\v\f"
EOLS = (b"\r\n", b"\n", b"\r")


def trim(data):
    """Strip trailing whitespace from every line, preserving each line's own terminator."""
    lines = []
    for line in data.splitlines(True):
        eol = b""
        for candidate in EOLS:
            if line.endswith(candidate):
                eol = candidate
                line = line[: -len(candidate)]
                break
        lines.append(line.rstrip(BLANKS) + eol)
    return b"".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("msg_filename", metavar="MSG_FILENAME")
    args = parser.parse_args()

    # Bytes in, bytes out: the commit message encoding is git's business (i18n.commitEncoding) and
    # trailing whitespace is ASCII either way, so nothing here has to decode the message and risk
    # failing on it.
    with open(args.msg_filename, "rb") as f:
        data = f.read()
    trimmed = trim(data)
    if trimmed != data:
        with open(args.msg_filename, "wb") as f:
            f.write(trimmed)

    return 0


if __name__ == "__main__":
    sys.exit(main())
