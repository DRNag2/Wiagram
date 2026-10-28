"""Generate TOOLBOX.md and toolbox.png from the parser's own tables.

Everything listed here is read from the code (inline parts, device kinds,
default ports, symbol drawings), so the toolbox cannot drift from what the
parser actually accepts.  Run `python3 -m wiagram toolbox` to refresh.
"""

import os

from .model import AUTO_THROUGH, FIXED_PORTS, KIND_ALIASES, TWO_PORT_KINDS
from .parser import INLINE_PARTS
from .render import render_png
from .symbols import legend_symbol

MD_NAME = "TOOLBOX.md"
PNG_NAME = "toolbox.png"

# one-line meaning of every device kind; the aliases come from KIND_ALIASES
KIND_NOTES = {
    "attenuator": "fixed attenuator",
    "circulator": "3-port circulator (1 -> 2 -> 3 -> 1)",
    "isolator": "isolator (a circulator with port 3 terminated)",
    "amplifier": "HEMT, TWPA, SPA ... (symbol flips on `direction: out` lines)",
    "filter": "any filter (low-pass, Eccosorb, K&L ...)",
    "coupler": "directional coupler",
    "term": "50 Ohm termination; ends a chain",
    "connector": "DB-25, Fischer, SMA adapters ...",
    "bulkhead": "SMA bulkhead feedthrough on a plate",
    "experiment": "your sample / cavity / chip; ports are whatever you name",
    "device": "generic multi-port box; ports are whatever you name",
    "biastee": "bias tee",
    "coil": "magnet coil",
    "switch": "microwave switch",
}

# which kinds are shown on the symbol sheet, in this order
SHEET_KINDS = [
    "attenuator", "filter", "circulator", "isolator", "amplifier", "coupler",
    "biastee", "bulkhead", "connector", "term", "switch", "coil", "experiment",
]


def default_ports(kind):
    if kind in FIXED_PORTS:
        return ", ".join("`%s`" % p for p in FIXED_PORTS[kind])
    if kind in TWO_PORT_KINDS:
        return "`in`, `out`"
    if kind in ("experiment", "device"):
        return "from `ports:` (or whatever you use in a chain)"
    return "-"


def through_note(kind):
    if kind in AUTO_THROUGH:
        a, b = AUTO_THROUGH[kind]
        return "`%s` -> `%s`" % (a, b)
    if kind in TWO_PORT_KINDS:
        return "`in` -> `out`"
    if kind == "term":
        return "ends the chain"
    return "needs explicit `.port`"


def toolbox_markdown():
    L = []
    w = L.append
    w("# Wiagram toolbox")
    w("")
    w("Everything you can write in a `.txt` wiring file.  This file is generated")
    w("by `python3 -m wiagram toolbox` from the parser tables; edit the code, not")
    w("this file.  `toolbox.png` next to it shows what each symbol looks like.")
    w("")
    w("![symbols](toolbox.png)")
    w("")
    w("## Chains")
    w("")
    w("```")
    w("In 1 -> -10dB@MXC -> knl -> cable(copper) -> cavity.ro_in")
    w("cavity.ro_out -> nb1 -> c24.1")
    w("c24.2 -> cable(NbTi) -> Out B")
    w("c24.3 -> term")
    w("```")
    w("")
    w("- Elements are joined by `->` and read left to right: from a fridge line")
    w("  towards your experiment, or from the experiment back to an output line.")
    w("- A fridge line (`In 1`, `Out B`, `DC A`) can only start or end a chain and")
    w("  can be used **once** across all files of a fridge.")
    w("- `name.port` picks a port of a declared device; a bare `name` on a two-port")
    w("  device goes in at `in` and out at `out`.")
    w("- `part@Plate` pins an inline part to a plate column (plates come from")
    w("  `[plates]` in `common.txt`).")
    w("- A cable (`cable(NbTi)` or a declared `[cable]`) sits *between* two parts:")
    w("  never two in a row, never first or last.")
    w("- `term` ends a chain; nothing can follow it.")
    w("- A chain may wrap onto the next line if the line ends with `->`.")
    w("- `#` starts a comment.")
    w("- Each port can be connected once; a second use is an error that names the")
    w("  first use.")
    w("- A device that is declared but never connected to a fridge line is reported")
    w("  as a warning.")
    w("")
    w("## Inline parts (no declaration needed)")
    w("")
    w("| Write | Example | Kind | Meaning |")
    w("|-------|---------|------|---------|")
    for part in INLINE_PARTS:
        kind = part.make(part.match(_probe(part)), _probe(part))[0]
        w("| `%s` | `%s` | %s | %s |" % (
            part.syntax.replace("|", "\\|"), part.example, kind, part.description))
    w("")
    w("## Declared devices")
    w("")
    w("```")
    w("[device c24]")
    w("type: circulator      # one of the kinds below (aliases allowed)")
    w("label: C24            # optional, printed under the symbol")
    w("serial: TS20231027-1  # optional")
    w("plate: MXC            # optional default plate")
    w("ports: in:W, out:E    # experiment/device: name your ports; :W/:E/:N/:S sets the side")
    w("color: #c55a11        # optional")
    w("note: anything        # optional")
    w("```")
    w("")
    w("| `type:` (aliases) | Default ports | Through a chain | Meaning |")
    w("|-------------------|---------------|-----------------|---------|")
    kinds = {}
    for alias, kind in KIND_ALIASES.items():
        if kind in ("cable", "node"):
            continue   # cables have their own section; nodes are internal
        kinds.setdefault(kind, []).append(alias)
    for kind in sorted(kinds):
        aliases = [a for a in kinds[kind] if a != kind]
        name = "`%s`" % kind
        if aliases:
            name += " (" + ", ".join("`%s`" % a for a in aliases) + ")"
        w("| %s | %s | %s | %s |" % (
            name, default_ports(kind), through_note(kind), KIND_NOTES.get(kind, "")))
    w("")
    w("## Declared cables")
    w("")
    w("```")
    w("[cable nb1]")
    w("type: NbTi            # copper, SS, NbTi, premade, flex, loom ... (sets the colour)")
    w("serial: ZS20211123-3  # optional")
    w("label: optional")
    w("```")
    w("")
    w("Use it between two parts: `cavity.ro_out -> nb1 -> c24.1`.")
    w("")
    w("## Metadata")
    w("")
    w("At the top of any file: `owner: Bob` names the band your wiring is drawn")
    w("in.  `common.txt` also has `fridge: <title>`.")
    w("")
    w("## `common.txt` sections")
    w("")
    w("| Section | Contents |")
    w("|---------|----------|")
    w("| `[plates]` | `Name: temperature`, one per line, warm to cold. Names are used by `@Name`. |")
    w("| `[template name]` | `chain:` with `{param}` placeholders; `group:` for vertical grouping; `direction: out` for output lines. |")
    w("| `[lines]` | `In 1..In 24: template(param=value)` creates the fridge lines; ranges over numbers or letters. |")
    w("| `[wiring]` | arrow chains (same as in owner files). |")
    w("")
    return "\n".join(L) + "\n"


def _probe(part):
    # a token that matches the pattern, to learn the kind it produces
    tok = part.example.split("@")[0]
    if part.match(tok):
        return tok
    return part.syntax.split(",")[0].strip()


def toolbox_svg():
    rows = []
    cell_w, cell_h = 150, 34
    cols = 3
    for i, kind in enumerate(SHEET_KINDS):
        cx = 12 + (i % cols) * cell_w
        cy = 40 + (i // cols) * cell_h
        rows.append(legend_symbol(kind, cx, cy))
        rows.append('<text x="%g" y="%g" font-family="Helvetica, Arial, sans-serif" '
                    'font-size="9" fill="#222">%s</text>' % (cx + 42, cy + 3, kind))
    n_rows = (len(SHEET_KINDS) + cols - 1) // cols
    w = 12 + cols * cell_w
    h = 40 + n_rows * cell_h
    head = ('<text x="12" y="20" font-family="Helvetica, Arial, sans-serif" '
            'font-size="12" font-weight="bold" fill="#222">Wiagram symbols</text>')
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
            'viewBox="0 0 %d %d"><rect width="%d" height="%d" fill="#fff"/>%s%s</svg>'
            % (w, h, w, h, w, h, head, "".join(rows)))


def write_toolbox(out_dir):
    """Write TOOLBOX.md and toolbox.png into out_dir; return the paths."""
    md_path = os.path.join(out_dir, MD_NAME)
    png_path = os.path.join(out_dir, PNG_NAME)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(toolbox_markdown())
    with open(png_path, "wb") as f:
        f.write(render_png(toolbox_svg()))
    return [md_path, png_path]
