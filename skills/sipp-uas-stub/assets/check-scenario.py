#!/usr/bin/env python3
"""Validate sipp 3.6 scenario XML files before feeding them to sipp.

Why: sipp's own error is a bare `Unable to load or parse '<file>' xml scenario file`
with NO line/column -- you end up guessing. Python's XML parser pinpoints the position,
and the three "silent killers" below are all cheap to check up front.

Checks per file:
  1. non-ASCII bytes      -> sipp fails when the declared encoding != actual bytes
  2. UTF-8 BOM            -> can break the XML declaration
  3. '--' inside comments -> XML forbids it; sipp reports the same generic parse error
  4. well-formed XML      -> reports exact line/column on failure

Usage:
    python3 check-scenario.py <file.xml> [more.xml ...]
    python3 check-scenario.py *.xml        # exit 1 if any check fails
"""
import glob
import re
import sys
import xml.etree.ElementTree as ET


def check(path):
    raw = open(path, "rb").read()
    problems = []

    bad = [i for i, b in enumerate(raw) if b > 127][:5]
    if bad:
        problems.append("non-ASCII bytes at offsets %s (keep scenarios pure ASCII)" % bad)

    if raw[:3] == b"\xef\xbb\xbf":
        problems.append("UTF-8 BOM present (strip it)")

    hits = [m.start() for m in re.finditer(rb"<!--(.*?)-->", raw, re.S) if b"--" in m.group(1)]
    if hits:
        problems.append("'--' inside %d XML comment(s) at byte offsets %s "
                        "(XML forbids it; use ';' or a single '-')" % (len(hits), hits[:5]))

    try:
        ET.parse(path)
    except ET.ParseError as e:
        problems.append("not well-formed XML -> %s" % e)

    return problems


def main(argv):
    files = []
    for a in argv:
        files.extend(sorted(glob.glob(a)) if any(c in a for c in "*?[") else [a])
    if not files:
        print(__doc__)
        return 2

    failed = 0
    for f in files:
        try:
            problems = check(f)
        except OSError as e:
            print("  %-24s FAIL  %s" % (f, e))
            failed += 1
            continue
        if problems:
            failed += 1
            print("  %-24s FAIL" % f)
            for p in problems:
                print("      - %s" % p)
        else:
            print("  %-24s OK" % f)

    print("\n%d/%d file(s) passed" % (len(files) - failed, len(files)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
