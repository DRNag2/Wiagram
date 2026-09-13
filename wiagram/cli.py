# command line: wiagram build / check

import argparse
import os
import sys

from .model import ParseError
from .parser import load
from .render import render_pdf, render_svg


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="wiagram",
        description="Render dilution-fridge wiring diagrams from text files.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="parse wiring files and render a diagram")
    b.add_argument("paths", nargs="+",
                   help="directory of .txt wiring files, or explicit files")
    b.add_argument("-o", "--out", help="output SVG path "
                   "(default: <dir>/wiring.svg)")
    b.add_argument("--pdf", help="also write a PDF (requires cairosvg)")

    c = sub.add_parser("check", help="validate wiring files without rendering")
    c.add_argument("paths", nargs="+")

    args = ap.parse_args(argv)

    try:
        design = load(args.paths)
    except ParseError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1

    for w in design.warnings:
        print("warning: %s" % w, file=sys.stderr)

    if args.cmd == "check":
        n_lines = len(design.lines)
        n_comps = 0
        for c in design.components.values():
            if c.kind != "node":
                n_comps += 1
        print("OK: %d lines, %d components, %d connections, %d warning(s)"
              % (n_lines, n_comps, len(design.connections), len(design.warnings)))
        return 0

    # figure out where to write the svg
    out = args.out
    if not out:
        if os.path.isdir(args.paths[0]):
            base = args.paths[0]
        else:
            base = "."
        out = os.path.join(base, "wiring.svg")

    svg = render_svg(design)
    with open(out, "w", encoding="utf-8") as f:
        f.write(svg)
    print("wrote %s" % out)

    if args.pdf:
        try:
            render_pdf(svg, args.pdf)
            print("wrote %s" % args.pdf)
        except RuntimeError as e:
            print("error: %s" % e, file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
