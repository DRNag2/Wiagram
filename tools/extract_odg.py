#!/usr/bin/env python3
"""Turn an old LibreOffice Draw .odg into wiagram common.txt.

The drawing is mined for plates, fridge lines, attenuators, HEMTs and DC
looms, then rewritten in the same style as example/common.txt (bulkhead /
cable backbone, templates, [lines] ranges).

Usage:
    python3 tools/extract_odg.py wiring_diagram_bluefors.odg
    python3 tools/extract_odg.py wiring_diagram_bluefors.odg --draft
    python3 tools/extract_odg.py wiring_diagram_bluefors.odg -o bluefors/
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from wiagram.odg import describe, extract  # noqa: E402


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or "-h" in argv or "--help" in argv:
        print(__doc__)
        return 0

    outdir = None
    if "-o" in argv:
        i = argv.index("-o")
        if i + 1 >= len(argv):
            print("error: -o needs a directory", file=sys.stderr)
            return 2
        outdir = argv[i + 1]
        del argv[i:i + 2]

    draft = "--draft" in argv
    if draft:
        argv.remove("--draft")

    odg = None
    for a in argv:
        if not a.startswith("-"):
            odg = a
            break
    if not odg:
        print(__doc__)
        return 2

    if not os.path.isfile(odg):
        print("error: file not found: %s" % odg, file=sys.stderr)
        return 1

    if not draft and not outdir:
        # readable report, same idea as the old default
        sys.stdout.write(describe(odg))
        print("\n# Write a real common.txt with --draft or -o DIR", file=sys.stderr)
        return 0

    common, wiring, notes = extract(odg)
    if outdir:
        os.makedirs(outdir, exist_ok=True)
        cpath = os.path.join(outdir, "common.txt")
        with open(cpath, "w", encoding="utf-8") as f:
            f.write(common)
        print("wrote %s" % cpath)
        if wiring:
            wpath = os.path.join(outdir, "extracted.txt")
            with open(wpath, "w", encoding="utf-8") as f:
                f.write(wiring)
            print("wrote %s" % wpath)
    else:
        sys.stdout.write(common)
    for n in notes:
        print("import: %s" % n, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
