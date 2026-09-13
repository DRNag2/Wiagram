# turn a laid-out Design into an SVG string

from xml.sax.saxutils import escape

from . import symbols
from .layout import LayoutData, layout
from .model import Design

WIRE_W = 1.4

LEGEND_KINDS = [
    ("attenuator", "Attenuator"),
    ("filter", "Filter"),
    ("circulator", "Circulator"),
    ("isolator", "Isolator"),
    ("amplifier", "Amplifier / HEMT"),
    ("coupler", "Directional coupler"),
    ("biastee", "Bias tee"),
    ("bulkhead", "Bulkhead"),
    ("connector", "Connector"),
    ("term", "50Ω termination"),
    ("experiment", "Experiment"),
]


def _txt(x, y, s, sz=9, anchor="start", color="#222", bold=False, rotate=None):
    if not s:
        return ""
    if bold:
        style = ' font-weight="bold"'
    else:
        style = ""
    if rotate:
        rot = ' transform="rotate(%g %g %g)"' % (rotate, x, y)
    else:
        rot = ""
    return ('<text x="%g" y="%g" font-size="%g" font-family="%s" '
            'text-anchor="%s" fill="%s"%s%s>%s</text>\n'
            % (x, y, sz, symbols.FONT, anchor, color, style, rot, escape(s)))


def _wire(conn, design):
    if conn.cable_id:
        cable = design.components.get(conn.cable_id)
    else:
        cable = None
    color = symbols.cable_color(cable)
    pts = " ".join("%g,%g" % (p[0], p[1]) for p in conn.points)
    out = ['<polyline points="%s" fill="none" stroke="%s" stroke-width="%g" '
           'stroke-linejoin="round"/>\n' % (pts, color, WIRE_W)]

    # put the cable label on the longest segment
    # in the line zone the color already tells you the type so we only
    # print serials/labels there
    in_line_zone = (design.components[conn.a[0]].zone == "line" and
                    design.components[conn.b[0]].zone == "line")
    if cable:
        show = cable.serial or cable.label
    else:
        show = None
    if in_line_zone and cable and not show:
        return "".join(out)

    if cable and (cable.serial or cable.label or cable.note):
        best = None
        blen = -1
        for i in range(len(conn.points) - 1):
            x1, y1 = conn.points[i]
            x2, y2 = conn.points[i + 1]
            ln = abs(x2 - x1) + abs(y2 - y1)
            if ln > blen:
                blen = ln
                horiz = abs(x2 - x1) >= abs(y2 - y1)
                best = ((x1 + x2) / 2.0, (y1 + y2) / 2.0, horiz)
        if best:
            mx, my, horiz = best
            label = cable.serial or cable.label or cable.note
            if horiz:
                out.append(_txt(mx, my - 3, label, 6.5, anchor="middle",
                                color=color))
            else:
                out.append(_txt(mx - 3, my, label, 6.5, anchor="middle",
                                color=color, rotate=-90))
    return "".join(out)


def _legend(design, ld):
    # returns (svg_string, height_used)
    used_kinds = set()
    for c in design.components.values():
        used_kinds.add(c.kind)
    kinds = []
    for k, lab in LEGEND_KINDS:
        if k in used_kinds:
            kinds.append((k, lab))

    cable_types = []
    for c in design.components.values():
        if c.kind == "cable" and c.note:
            key = c.note.lower()
            already = False
            for t in cable_types:
                if t[0] == key:
                    already = True
                    break
            if not already:
                cable_types.append((key, c.note))

    x0 = 20
    y0 = ld.height + 14
    rows = max(len(kinds), len(cable_types) + 1)
    h = 26 + rows * 22
    w = 420
    s = ['<rect x="%g" y="%g" width="%g" height="%g" fill="#fafafa" '
         'stroke="#999" rx="4"/>\n' % (x0, y0, w, h)]
    s.append(_txt(x0 + 10, y0 + 16, "Legend", 10, bold=True))

    yy = y0 + 38
    for kind, lab in kinds:
        s.append(symbols.legend_symbol(kind, x0 + 14, yy))
        s.append(_txt(x0 + 64, yy + 3, lab, 8.5))
        yy += 22

    yy = y0 + 38
    cx = x0 + 210
    s.append(_txt(cx, yy - 12, "Cables", 8.5, bold=True))
    for key, name in cable_types:
        color = symbols.CABLE_COLORS.get(key, symbols.DEFAULT_WIRE)
        s.append('<line x1="%g" y1="%g" x2="%g" y2="%g" stroke="%s" '
                 'stroke-width="2"/>\n' % (cx, yy + 1, cx + 36, yy + 1, color))
        s.append(_txt(cx + 44, yy + 4, name, 8.5))
        yy += 22
    s.append('<line x1="%g" y1="%g" x2="%g" y2="%g" stroke="%s" '
             'stroke-width="2"/>\n'
             % (cx, yy + 1, cx + 36, yy + 1, symbols.DEFAULT_WIRE))
    s.append(_txt(cx + 44, yy + 4, "unspecified wire", 8.5, color="#555"))
    return "".join(s), h + 28


def render_svg(design: Design, ld: LayoutData = None) -> str:
    if ld is None:
        ld = layout(design)

    body = []

    # owner bands underneath everything
    for owner, x0, y0, x1, y1, fill in ld.bands:
        body.append('<rect x="%g" y="%g" width="%g" height="%g" rx="6" '
                    'fill="%s" stroke="#c9b99a" stroke-width="0.8"/>\n'
                    % (x0, y0, x1 - x0, y1 - y0, fill))
        body.append(_txt(x0 + 8, y0 + 14, owner, 11, bold=True, color="#8a6d3b"))

    # plate bars
    for x, y0, y1, name, temp in ld.plate_bars:
        body.append('<rect x="%g" y="%g" width="14" height="%g" '
                    'fill="#f2b632" stroke="#a87900" stroke-width="1"/>\n'
                    % (x - 7, y0, y1 - y0))
        body.append(_txt(x, y0 - 22, name, 10, anchor="middle", bold=True))
        body.append(_txt(x, y0 - 10, temp, 8, anchor="middle", color="#666"))

    # wires
    for conn in design.connections:
        if conn.points:
            body.append(_wire(conn, design))

    # components (skip the invisible nodes)
    for comp in design.components.values():
        if comp.placed and comp.kind != "node":
            body.append(symbols.draw(comp))

    # line name labels on the left
    for x, y, name in ld.line_labels:
        body.append(_txt(x, y, name, 8.5, anchor="end", bold=True, color="#333"))

    # title
    title = design.meta.get("fridge", "") or design.meta.get("title", "")
    body.append(_txt(20, 26, title or "Fridge wiring", 16, bold=True))
    subtitle = ", ".join(o for o in design.owners)
    body.append(_txt(20, 42, subtitle, 9, color="#777"))

    legend_svg, legend_h = _legend(design, ld)
    body.append(legend_svg)

    W = ld.width
    H = ld.height + legend_h
    head = ('<svg xmlns="http://www.w3.org/2000/svg" width="%g" height="%g" '
            'viewBox="0 0 %g %g">\n<rect width="%g" height="%g" fill="white"/>\n'
            % (W, H, W, H, W, H))
    return head + "".join(body) + "</svg>\n"


def render_pdf(svg_text, pdf_path):
    try:
        import cairosvg
    except ImportError:
        raise RuntimeError(
            "PDF output needs the optional 'cairosvg' package: pip install cairosvg")
    cairosvg.svg2pdf(bytestring=svg_text.encode("utf-8"), write_to=pdf_path)
