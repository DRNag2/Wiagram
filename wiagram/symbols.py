# drawing the little component symbols

from xml.sax.saxutils import escape

# (width, height) when drawn horizontally
SIZES = {
    "attenuator": (30, 14),
    "filter": (36, 16),
    "bulkhead": (10, 16),
    "connector": (34, 14),
    "amplifier": (24, 20),
    "circulator": (28, 28),
    "isolator": (28, 28),
    "coupler": (42, 16),
    "term": (12, 12),
    "biastee": (34, 20),
    "coil": (26, 26),
    "switch": (34, 20),
    "node": (0, 0),
}

CABLE_COLORS = {
    "copper": "#d2691e", "cu": "#d2691e", "handmade copper": "#d2691e",
    "ss": "#7f7f7f", "stainless": "#7f7f7f", "steel": "#7f7f7f",
    "nbti": "#3465a4",
    "premade": "#111111",
    "flex": "#9932cc",
    "dc loom": "#2e8b57", "loom": "#2e8b57", "twisted pair": "#2e8b57",
}
DEFAULT_WIRE = "#444444"

FONT = "Helvetica, Arial, sans-serif"


def text_w(s, size):
    # rough guess of text width, good enough for layout
    return len(s) * size * 0.58


def cable_color(cable_comp):
    if cable_comp is None:
        return DEFAULT_WIRE
    if cable_comp.color:
        return cable_comp.color
    note = (cable_comp.note or "").lower()
    return CABLE_COLORS.get(note, DEFAULT_WIRE)


def size(comp):
    # returns (w, h) before we flip for vertical orientation
    if comp.kind in ("experiment", "device"):
        label = comp.display_label()
        w = max(72.0, text_w(label, 10) + 46)  # need room for port labels
        n_side = max(1, len(comp.ports() or ["in"]))
        h = max(34.0, 6 + 13 * ((n_side + 1) // 2))
        return (w, h)
    w, h = SIZES.get(comp.kind, (30, 16))
    if comp.kind in ("attenuator", "filter", "connector", "coupler", "switch",
                     "biastee"):
        # label goes inside so make the box wide enough
        w = max(w, text_w(comp.display_label(), 7) + 8)
    return (w, h)


def _rot(dx, dy, orient):
    # rotate port offset when the component is drawn vertically
    if orient == "v":
        return (dy, dx)
    return (dx, dy)


def _rot_side(side, orient):
    if orient == "v":
        return {"W": "N", "E": "S", "N": "E", "S": "W"}[side]
    return side


def port_offsets(comp):
    # port name -> (dx, dy, side) assuming horizontal orientation
    w, h = size(comp)
    hw = w / 2.0
    hh = h / 2.0
    k = comp.kind

    if k == "circulator":
        return {"1": (-hw, 0, "W"), "2": (hw, 0, "E"), "3": (0, hh, "S")}
    if k == "coupler":
        return {"in": (-hw, 0, "W"), "out": (hw, 0, "E"),
                "cpl": (-hw * 0.45, hh, "S"), "iso": (hw * 0.45, hh, "S")}
    if k == "biastee":
        return {"rf": (-hw, 0, "W"), "out": (hw, 0, "E"), "dc": (0, -hh, "N")}
    if k == "term":
        return {"p": (-hw, 0, "W")}
    if k == "node":
        return {"a": (0, 0, "E"), "b": (0, 0, "W")}

    if k in ("experiment", "device"):
        ports = comp.ports() or []
        out = {}
        west = []
        east = []
        south = []
        north = []
        rest = []
        for p in ports:
            side = comp.port_sides.get(p, "")
            if side == "W":
                west.append(p)
            elif side == "E":
                east.append(p)
            elif side == "S":
                south.append(p)
            elif side == "N":
                north.append(p)
            else:
                rest.append(p)
        # default: first leftover port on the left, rest on the right
        if rest:
            west = west + rest[:1]
            east = east + rest[1:]

        def spread(plist, fixed, along_y):
            n = len(plist)
            for i, p in enumerate(plist):
                off = (i - (n - 1) / 2.0) * 14
                if along_y:
                    if fixed < 0:
                        out[p] = (fixed, off, "W")
                    else:
                        out[p] = (fixed, off, "E")
                else:
                    if fixed < 0:
                        out[p] = (off, fixed, "N")
                    else:
                        out[p] = (off, fixed, "S")

        spread(west, -hw, True)
        spread(east, hw, True)
        spread(south, hh, False)
        spread(north, -hh, False)
        return out

    # normal two-port thing
    return {"in": (-hw, 0, "W"), "out": (hw, 0, "E")}


def port_pos(comp, port):
    # absolute (x, y, side) after placement + orientation
    offs = port_offsets(comp)
    if port not in offs:
        # shouldn't really happen but just put it on the right
        offs[port] = (size(comp)[0] / 2.0, 0, "E")
    dx, dy, side = offs[port]
    rx, ry = _rot(dx, dy, comp.orient)
    return (comp.x + rx, comp.y + ry, _rot_side(side, comp.orient))


def _txt(x, y, s, sz=8, anchor="middle", color="#222", bold=False, rotate=None):
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
            'text-anchor="%s" fill="%s"%s%s>%s</text>'
            % (x, y, sz, FONT, anchor, color, style, rot, escape(s)))


def _label_below(comp, w, h, small=False):
    # label + serial next to / under the symbol
    out = []
    lab = comp.display_label()
    inside_kinds = ("attenuator", "filter", "connector", "coupler", "switch",
                    "biastee")
    if comp.kind in inside_kinds:
        lab = ""  # already drawn inside the box

    if comp.zone == "line":
        # lines are packed tight so put the label to the right of the symbol
        x = comp.x + w / 2.0 + 3
        if lab:
            out.append(_txt(x, comp.y - 4, lab, 6.5, anchor="start"))
        if comp.serial:
            out.append(_txt(x, comp.y + 10, comp.serial, 6, anchor="start",
                            color="#666"))
        return "".join(out)

    if comp.kind in ("circulator", "isolator") and comp.zone == "band":
        # put labels on the left so they don't hit the hang wire under port 3
        x = comp.x - w / 2.0 - 4
        if lab:
            out.append(_txt(x, comp.y + 4, lab, 7.5, anchor="end"))
        if comp.serial:
            out.append(_txt(x, comp.y + 13, comp.serial, 6.5, anchor="end",
                            color="#666"))
        return "".join(out)

    y = comp.y + h / 2.0 + 8
    if lab and comp.kind not in ("experiment", "device"):
        if small:
            out.append(_txt(comp.x, y, lab, 7.5))
        else:
            out.append(_txt(comp.x, y, lab, 8))
        y += 8
    if comp.serial:
        out.append(_txt(comp.x, y, comp.serial, 6.5, color="#666"))
        y += 7
    if comp.plate and comp.zone == "band":
        out.append(_txt(comp.x, y, "@" + comp.plate, 6.5, color="#996515"))
    return "".join(out)


def draw(comp):
    # svg for one placed component
    w, h = size(comp)
    if comp.orient == "v":
        w, h = h, w
    x0 = comp.x - w / 2.0
    y0 = comp.y - h / 2.0
    k = comp.kind
    s = []

    if k == "node":
        return ""

    if k == "attenuator":
        s.append('<rect x="%g" y="%g" width="%g" height="%g" rx="2" '
                 'fill="#ffd27f" stroke="#b8860b" stroke-width="1"/>' % (x0, y0, w, h))
        s.append(_txt(comp.x, comp.y + 2.8, comp.display_label(), 7.5))
        s.append(_label_below(comp, w, h, small=True))

    elif k == "filter":
        s.append('<rect x="%g" y="%g" width="%g" height="%g" rx="2" '
                 'fill="#c6e0b4" stroke="#4f7942" stroke-width="1"/>' % (x0, y0, w, h))
        s.append(_txt(comp.x, comp.y + 2.8, comp.display_label(), 7))
        s.append(_label_below(comp, w, h, small=True))

    elif k == "bulkhead":
        s.append('<rect x="%g" y="%g" width="%g" height="%g" '
                 'fill="#555" stroke="#222" stroke-width="0.8"/>' % (x0, y0, w, h))
        s.append('<line x1="%g" y1="%g" x2="%g" y2="%g" stroke="#fff" '
                 'stroke-width="1.4"/>' % (comp.x, y0 + 2, comp.x, y0 + h - 2))

    elif k == "connector":
        s.append('<rect x="%g" y="%g" width="%g" height="%g" rx="3" '
                 'fill="#d9d9d9" stroke="#555" stroke-width="1"/>' % (x0, y0, w, h))
        s.append(_txt(comp.x, comp.y + 2.6, comp.display_label(), 7))
        s.append(_label_below(comp, w, h, small=True))

    elif k == "amplifier":
        if comp.orient == "v":
            pts = "%g,%g %g,%g %g,%g" % (x0, y0, x0 + w, y0, comp.x, y0 + h)
        elif comp.flip:
            # output lines: signal goes right to left
            pts = "%g,%g %g,%g %g,%g" % (x0 + w, y0, x0 + w, y0 + h, x0, comp.y)
        else:
            pts = "%g,%g %g,%g %g,%g" % (x0, y0, x0, y0 + h, x0 + w, comp.y)
        s.append('<polygon points="%s" fill="#ffe066" stroke="#8a6d00" '
                 'stroke-width="1.2"/>' % pts)
        s.append(_label_below(comp, w, h))

    elif k in ("circulator", "isolator"):
        r = w / 2.0
        s.append('<circle cx="%g" cy="%g" r="%g" fill="#fff" stroke="#8a2be2" '
                 'stroke-width="1.4"/>' % (comp.x, comp.y, r))
        # little rotation arrow
        ar = r * 0.52
        s.append('<path d="M %g %g A %g %g 0 1 1 %g %g" fill="none" '
                 'stroke="#8a2be2" stroke-width="1.1"/>'
                 % (comp.x + ar, comp.y, ar, ar, comp.x, comp.y - ar))
        s.append('<path d="M %g %g l -3 -2.4 l 4.4 -1.4 z" fill="#8a2be2"/>'
                 % (comp.x, comp.y - ar))
        if k == "isolator":
            s.append('<line x1="%g" y1="%g" x2="%g" y2="%g" stroke="#8a2be2" '
                     'stroke-width="1.4"/>'
                     % (comp.x + r * 0.5, comp.y - r * 0.7,
                        comp.x + r * 0.9, comp.y + r * 0.2))
        s.append(_label_below(comp, w, h))

    elif k == "coupler":
        s.append('<rect x="%g" y="%g" width="%g" height="%g" rx="2" '
                 'fill="#bdd7ee" stroke="#2e75b6" stroke-width="1"/>' % (x0, y0, w, h))
        s.append('<line x1="%g" y1="%g" x2="%g" y2="%g" stroke="#2e75b6" '
                 'stroke-width="1"/>' % (x0 + 4, y0 + h - 3, x0 + w - 4, y0 + 3))
        s.append(_txt(comp.x, comp.y + 2.6, comp.display_label() or "DC", 6.5))
        s.append(_label_below(comp, w, h, small=True))

    elif k == "term":
        s.append('<rect x="%g" y="%g" width="%g" height="%g" '
                 'fill="#333" stroke="#000" stroke-width="1"/>' % (x0, y0, w, h))
        lx = comp.x
        ly = comp.y + h / 2.0 + 8
        s.append(_txt(lx, ly, comp.display_label(), 7, color="#333"))

    elif k == "biastee":
        s.append('<rect x="%g" y="%g" width="%g" height="%g" rx="2" '
                 'fill="#f4cccc" stroke="#a04040" stroke-width="1"/>' % (x0, y0, w, h))
        s.append(_txt(comp.x, comp.y + 2.6, comp.display_label() or "Bias-T", 6.5))
        s.append(_label_below(comp, w, h, small=True))

    elif k == "coil":
        r = w / 2.0
        s.append('<circle cx="%g" cy="%g" r="%g" fill="#fff2cc" stroke="#bf9000" '
                 'stroke-width="1.2"/>' % (comp.x, comp.y, r))
        for i in (-1, 0, 1):
            s.append('<circle cx="%g" cy="%g" r="2.2" fill="none" stroke="#bf9000" '
                     'stroke-width="1"/>' % (comp.x + i * 5.5, comp.y))
        s.append(_label_below(comp, w, h))

    elif k == "switch":
        s.append('<rect x="%g" y="%g" width="%g" height="%g" rx="2" '
                 'fill="#e2d5f0" stroke="#674ea7" stroke-width="1"/>' % (x0, y0, w, h))
        s.append('<line x1="%g" y1="%g" x2="%g" y2="%g" stroke="#674ea7" '
                 'stroke-width="1.2"/>' % (x0 + 6, comp.y + 4, x0 + w - 8, comp.y - 5))
        s.append(_txt(comp.x, y0 - 3, comp.display_label(), 7))
        s.append(_label_below(comp, w, h, small=True))

    elif k in ("experiment", "device"):
        if comp.color:
            fill = comp.color
        elif k == "device":
            fill = "#dae3f3"
        else:
            fill = "#fce5cd"
        if k == "device":
            stroke = "#2f5597"
        else:
            stroke = "#c55a11"
        s.append('<rect x="%g" y="%g" width="%g" height="%g" rx="4" '
                 'fill="%s" stroke="%s" stroke-width="1.4"/>'
                 % (x0, y0, w, h, fill, stroke))
        s.append(_txt(comp.x, comp.y + 3, comp.display_label(), 9, bold=True))
        if comp.serial:
            s.append(_txt(comp.x, y0 + h + 9, comp.serial, 6.5, color="#666"))
        # port names inside the box near each port
        for p, (dx, dy, side) in port_offsets(comp).items():
            px = comp.x + dx
            py = comp.y + dy
            if side == "W":
                s.append(_txt(px + 3, py + 2.5, p, 6, anchor="start", color="#555"))
            elif side == "E":
                s.append(_txt(px - 3, py + 2.5, p, 6, anchor="end", color="#555"))
            elif side == "S":
                s.append(_txt(px, py - 3, p, 6, color="#555"))
            else:
                s.append(_txt(px, py + 8, p, 6, color="#555"))
    else:
        # fallback for anything we forgot
        s.append('<rect x="%g" y="%g" width="%g" height="%g" fill="#eee" '
                 'stroke="#666"/>' % (x0, y0, w, h))
        s.append(_label_below(comp, w, h))

    return "".join(s)


def legend_symbol(kind, x, y):
    # tiny version of the symbol for the legend box
    s = []
    if kind == "attenuator":
        s.append('<rect x="%g" y="%g" width="28" height="13" rx="2" '
                 'fill="#ffd27f" stroke="#b8860b"/>' % (x, y - 6.5))
        s.append(_txt(x + 14, y + 2.5, "-XdB", 7))
    elif kind == "filter":
        s.append('<rect x="%g" y="%g" width="28" height="13" rx="2" '
                 'fill="#c6e0b4" stroke="#4f7942"/>' % (x, y - 6.5))
    elif kind in ("circulator", "isolator"):
        cx = x + 9
        s.append('<circle cx="%g" cy="%g" r="8" fill="#fff" stroke="#8a2be2" '
                 'stroke-width="1.2"/>' % (cx, y))
        s.append('<path d="M %g %g A 4 4 0 1 1 %g %g" fill="none" '
                 'stroke="#8a2be2"/>' % (cx + 4, y, cx, y - 4))
        if kind == "isolator":
            s.append('<line x1="%g" y1="%g" x2="%g" y2="%g" stroke="#8a2be2" '
                     'stroke-width="1.2"/>' % (cx + 4, y - 5, cx + 7, y + 2))
    elif kind == "amplifier":
        s.append('<polygon points="%g,%g %g,%g %g,%g" fill="#ffe066" '
                 'stroke="#8a6d00"/>' % (x, y - 8, x, y + 8, x + 18, y))
    elif kind == "coupler":
        s.append('<rect x="%g" y="%g" width="30" height="13" rx="2" '
                 'fill="#bdd7ee" stroke="#2e75b6"/>' % (x, y - 6.5))
        s.append('<line x1="%g" y1="%g" x2="%g" y2="%g" stroke="#2e75b6"/>'
                 % (x + 3, y + 4, x + 27, y - 4))
    elif kind == "biastee":
        s.append('<rect x="%g" y="%g" width="28" height="13" rx="2" '
                 'fill="#f4cccc" stroke="#a04040"/>' % (x, y - 6.5))
    elif kind == "bulkhead":
        s.append('<rect x="%g" y="%g" width="9" height="15" fill="#555" '
                 'stroke="#222"/>' % (x + 6, y - 7.5))
        s.append('<line x1="%g" y1="%g" x2="%g" y2="%g" stroke="#fff" '
                 'stroke-width="1.3"/>' % (x + 10.5, y - 5.5, x + 10.5, y + 5.5))
    elif kind == "connector":
        s.append('<rect x="%g" y="%g" width="28" height="12" rx="3" '
                 'fill="#d9d9d9" stroke="#555"/>' % (x, y - 6))
    elif kind == "term":
        s.append('<rect x="%g" y="%g" width="10" height="10" fill="#333"/>'
                 % (x + 5, y - 5))
    elif kind in ("experiment", "device"):
        s.append('<rect x="%g" y="%g" width="32" height="14" rx="3" '
                 'fill="#fce5cd" stroke="#c55a11" stroke-width="1.2"/>'
                 % (x, y - 7))
    elif kind == "switch":
        s.append('<rect x="%g" y="%g" width="28" height="13" rx="2" '
                 'fill="#e2d5f0" stroke="#674ea7"/>' % (x, y - 6.5))
    elif kind == "coil":
        s.append('<circle cx="%g" cy="%g" r="8" fill="#fff2cc" '
                 'stroke="#bf9000"/>' % (x + 9, y))
    return "".join(s)
