# command line: wiagram build / check / import / toolbox
#
# a fridge folder always holds the same files:
#   common.txt, <owner>.txt ..., wiring.pdf, wiring.png, errors.log
# build rewrites errors.log every time. when the text has mistakes the log
# lists them and the PDF/PNG are left exactly as they were.

import argparse
import os
import sys

from .model import ParseError, ParseErrors
from .odg import extract as extract_odg
from .parser import load
from .render import render

PDF_NAME = "wiring.pdf"
PNG_NAME = "wiring.png"
LOG_NAME = "errors.log"


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="wiagram",
        description="Render dilution-fridge wiring diagrams from text files.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser(
        "build",
        help="parse a fridge folder and write wiring.pdf, wiring.png, errors.log")
    b.add_argument("dir", help="fridge folder holding common.txt and owner .txt files")

    c = sub.add_parser("check", help="validate wiring files without rendering")
    c.add_argument("paths", nargs="+",
                   help="fridge folder, or explicit .txt files")

    imp = sub.add_parser(
        "import",
        help="convert a LibreOffice .odg into a fridge folder and build it")
    imp.add_argument("odg", help="path to a .odg drawing")
    imp.add_argument("-o", "--out", required=True,
                     help="fridge folder to create (common.txt, extracted.txt, ...)")

    tb = sub.add_parser(
        "toolbox",
        help="write TOOLBOX.md / toolbox.png listing every component you can type")
    tb.add_argument("-o", "--out", default=".",
                    help="directory for TOOLBOX.md and toolbox.png (default: .)")

    args = ap.parse_args(argv)

    if args.cmd == "check":
        return _cmd_check(args.paths)
    if args.cmd == "build":
        return _cmd_build(args.dir)
    if args.cmd == "import":
        return _cmd_import(args.odg, args.out)
    if args.cmd == "toolbox":
        return _cmd_toolbox(args.out)
    return 2


def _error_lines(e):
    if isinstance(e, ParseErrors):
        return [str(x) for x in e.errors]
    return [str(e)]


def _print_errors(e):
    for line in _error_lines(e):
        print("error: %s" % line, file=sys.stderr)


def _summary(design):
    n_comps = 0
    for c in design.components.values():
        if c.kind != "node":
            n_comps += 1
    return "OK: %d lines, %d components, %d connections, %d warning(s)" % (
        len(design.lines), n_comps, len(design.connections),
        len(design.warnings))


def _cmd_check(paths):
    try:
        design = load(paths)
    except ParseError as e:
        _print_errors(e)
        return 1
    for w in design.warnings:
        print("warning: %s" % w, file=sys.stderr)
    print(_summary(design))
    return 0


def _cmd_build(folder):
    if not os.path.isdir(folder):
        print("error: %s is not a directory" % folder, file=sys.stderr)
        return 1
    log_path = os.path.join(folder, LOG_NAME)

    try:
        design = load([folder])
        pdf, png = render(design)
    except ParseError as e:
        _print_errors(e)
        with open(log_path, "w", encoding="utf-8") as f:
            for line in _error_lines(e):
                f.write(line + "\n")
        print("wrote %s (%s unchanged)" % (log_path, ", ".join((PDF_NAME, PNG_NAME))),
              file=sys.stderr)
        return 1

    for w in design.warnings:
        print("warning: %s" % w, file=sys.stderr)

    # all-or-nothing: both images were rendered before anything is written
    pdf_path = os.path.join(folder, PDF_NAME)
    png_path = os.path.join(folder, PNG_NAME)
    with open(pdf_path, "wb") as f:
        f.write(pdf)
    with open(png_path, "wb") as f:
        f.write(png)
    with open(log_path, "w", encoding="utf-8") as f:
        pass  # no errors
    print("wrote %s, %s, %s" % (pdf_path, png_path, log_path))
    print(_summary(design))
    return 0


def _cmd_import(odg, out):
    os.makedirs(out, exist_ok=True)
    try:
        common, wiring, notes = extract_odg(odg)
    except Exception as e:
        print("error: failed to read %s: %s" % (odg, e), file=sys.stderr)
        return 1
    cpath = os.path.join(out, "common.txt")
    with open(cpath, "w", encoding="utf-8") as f:
        f.write(common)
    print("wrote %s" % cpath)
    if wiring:
        wpath = os.path.join(out, "extracted.txt")
        with open(wpath, "w", encoding="utf-8") as f:
            f.write(wiring)
        print("wrote %s" % wpath)
    for n in notes:
        print("import: %s" % n, file=sys.stderr)
    return _cmd_build(out)


def _cmd_toolbox(out):
    from .toolbox import write_toolbox
    os.makedirs(out, exist_ok=True)
    for p in write_toolbox(out):
        print("wrote %s" % p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
