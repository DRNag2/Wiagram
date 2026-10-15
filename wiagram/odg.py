# Convert a LibreOffice Draw .odg fridge diagram into wiagram text.
#
# Two halves:
#   * left of the MXC plate: per-line attenuators / HEMTs become templates
#     and a [lines] table in common.txt (plates, bulkheads, SS cables).
#   * MXC side: every drawn part becomes a node and every connector / abutting
#     box becomes an edge in a networkx graph. [wiring] chains are read off
#     that graph, so a circulator is only ever "connected to nothing" if the
#     drawing really leaves it floating.
#
# Anything the drawing says that the text format cannot hold is reported in
# the notes list that extract() returns (wiagram import prints them).

import math
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict

import networkx as nx

DRAW = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
SVG = "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"
STYLE = "urn:oasis:names:tc:opendocument:xmlns:style:1.0"
XMLID = "http://www.w3.org/XML/1998/namespace"

LINE_NAME = re.compile(
    r"^(In\s+\d+|Out\s+[A-Z]|DC\s+[A-Z0-9]+|Pair\s+\d+)$", re.I)
PLATE_RE = re.compile(r"^(.*?)\s*Plate$", re.I)
ATTEN_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?\s*dB$", re.I)
K_ATTEN_RE = re.compile(r"^K\s*-?\s*(\d+(?:\.\d+)?)\s*dB$", re.I)
HEMT_RE = re.compile(r"^HEMT\s*(.*)$", re.I)
SERIAL_RE = re.compile(r"^[A-Z]{1,3}\d{6,}(-\d+)?\??$")

# Conventional mixing-chamber temperatures when the ODG only says "4K Plate".
PLATE_TEMPS = {
    "Top": "300K",
    "300K": "300K",
    "77K": "77K",
    "50K": "50K",
    "4K": "4K",
    "Still": "800mK",
    "1K": "1K",
    "Cold": "100mK",
    "MXC": "20mK",
    "Mez.": "MXC mez",
    "Mez": "MXC mez",
}

SKIP_LABELS = {
    "legend", "experiment", "storage", "readout", "qubit",
    "cots (relatively) microwave device", "magnet coil attenuator",
    "magnet coil breakout", "breakout 1-24", "dc side", "big mu metal can",
    "mu-metal can", "wiring for pumping", "bulkhead", "clamps",
    "eccosorb filters", "filters", "for thermal", "ethan setup",
}

# wire colours from the drawing legend
STROKE_CABLE = {
    "#ea7500": "copper", "#ff972f": "copper", "#ff8000": "copper",
    "#ffdbb6": "copper",
    "#729fcf": "NbTi", "#3465a4": "NbTi",
    "#999999": "SS",
    "#666666": "premade",
}
THERMAL_STROKES = {"#ff0000"}
DC_STROKES = {"#81d41a", "#5eb91e"}

INPUT_PLATES = ("Top", "77K", "4K", "Still")
OUTPUT_PLATES = ("Top", "77K", "4K")

# small blue tags / text frames that name an experiment port
PORT_WORDS = {
    "q", "ro", "s", "r", "st", "in", "out", "cpl", "pump", "readout",
    "qubit", "storage", "gate", "rf", "dc", "iso", "pumpin", "sig",
}
DC_WORDS = re.compile(
    r"^(pair\s+\d+|db-?25|breakout.*|magnet coil.*|\d+\s*ma.*|weird series"
    r" resistors|vts heater|vts with.*|dc\s+\d+.*|dc lines|fischer)$", re.I)


class Shape:
    def __init__(self, page, sid, x, y, w, h, text, gtype):
        self.page = page
        self.id = sid
        self.x = x          # centre
        self.y = y
        self.w = w
        self.h = h
        self.text = text
        self.gtype = gtype  # rectangle / circular-arrow / frame / ...
        self.fill = None
        self.group = None   # id of the enclosing draw:g, if any

    @property
    def x0(self):
        return self.x - self.w / 2.0

    @property
    def x1(self):
        return self.x + self.w / 2.0

    @property
    def y0(self):
        return self.y - self.h / 2.0

    @property
    def y1(self):
        return self.y + self.h / 2.0

    def contains(self, x, y, margin=0.0):
        return (self.x0 - margin <= x <= self.x1 + margin and
                self.y0 - margin <= y <= self.y1 + margin)

    def __repr__(self):
        return "Shape(%s %r @%.1f,%.1f)" % (self.gtype, self.text[:20], self.x, self.y)


def _cm(v):
    if v is None:
        return None
    v = v.strip()
    for suffix, mul in (("cm", 1.0), ("mm", 0.1), ("in", 2.54), ("pt", 2.54 / 72)):
        if v.endswith(suffix):
            return float(v[: -len(suffix)]) * mul
    try:
        return float(v)
    except ValueError:
        return None


def _all_text(el):
    bits = []
    for t in el.itertext():
        t = t.strip()
        if t:
            bits.append(t)
    return " ".join(bits)


def _el_id(el):
    return el.get("{%s}id" % DRAW) or el.get("{%s}id" % XMLID)


def _center(el):
    """Shape center in cm, including draw:transform (rotated HEMTs etc.)."""
    w = _cm(el.get("{%s}width" % SVG)) or 0.0
    h = _cm(el.get("{%s}height" % SVG)) or 0.0
    x = _cm(el.get("{%s}x" % SVG))
    y = _cm(el.get("{%s}y" % SVG))
    tr = el.get("{%s}transform" % DRAW) or ""
    if x is not None and y is not None and "translate" not in tr:
        return x + w / 2.0, y + h / 2.0, w, h

    ang = 0.0
    rm = re.search(r"rotate\s*\(\s*([^)\s]+)", tr)
    if rm:
        ang = float(rm.group(1))
    tx = ty = 0.0
    tm = re.search(r"translate\s*\(\s*([^)]+)\)", tr)
    if tm:
        parts = tm.group(1).replace(",", " ").split()
        tx = _cm(parts[0]) or 0.0
        ty = _cm(parts[1]) if len(parts) > 1 else 0.0

    # ODF applies rotate then translate; a positive angle is a counter-
    # clockwise rotation on screen (y axis pointing down).  Local centre
    # is (w/2, h/2); the returned w/h are the axis-aligned bounding box.
    ca, sa = math.cos(ang), math.sin(ang)
    lx, ly = w / 2.0, h / 2.0
    rx = ca * lx + sa * ly
    ry = -sa * lx + ca * ly
    bw = abs(ca) * w + abs(sa) * h
    bh = abs(sa) * w + abs(ca) * h
    if x is not None and y is not None:
        return x + w / 2.0, y + h / 2.0, w, h
    return tx + rx, ty + ry, bw, bh


class _Styles:
    # resolve fill / stroke colours through the style parent chain
    def __init__(self, roots):
        self.styles = {}
        for root in roots:
            for st in root.iter("{%s}style" % STYLE):
                name = st.get("{%s}name" % STYLE)
                gp = st.find("{%s}graphic-properties" % STYLE)
                d = {"parent": st.get("{%s}parent-style-name" % STYLE)}
                if gp is not None:
                    d["fill"] = gp.get("{%s}fill-color" % DRAW)
                    d["stroke"] = gp.get("{%s}stroke-color" % SVG)
                self.styles[name] = d

    def get(self, name, key):
        seen = set()
        while name and name not in seen:
            seen.add(name)
            d = self.styles.get(name)
            if d is None:
                return None
            if d.get(key):
                return d[key].lower()
            name = d["parent"]
        return None


class Drawing:
    def __init__(self):
        self.shapes = []        # Shape (pages 0..n)
        self.connectors = []    # dict per connector on page 0
        self.by_id = {}         # id -> Shape, or ("group", [child ids]) or connector dict


def load_drawing(odg_path):
    z = zipfile.ZipFile(odg_path)
    root = ET.fromstring(z.read("content.xml"))
    try:
        sroot = ET.fromstring(z.read("styles.xml"))
    except KeyError:
        sroot = ET.Element("x")
    styles = _Styles([root, sroot])

    dr = Drawing()
    pages = list(root.iter("{%s}page" % DRAW))
    for pi, page in enumerate(pages):
        group_of = {}
        for g in page.iter("{%s}g" % DRAW):
            gid = _el_id(g)
            kids = []
            for el in g.iter():
                if el is g:
                    continue
                cid = _el_id(el)
                if cid:
                    kids.append(cid)
                    group_of.setdefault(cid, gid)
            if gid:
                dr.by_id[gid] = ("group", kids, _all_text(g))
        for el in page.iter():
            tag = el.tag.split("}")[-1]
            if tag in ("custom-shape", "frame", "rect"):
                text = _all_text(el)
                x, y, w, h = _center(el)
                geom = el.find("{%s}enhanced-geometry" % DRAW)
                gtype = geom.get("{%s}type" % DRAW) if geom is not None else tag
                s = Shape(pi, _el_id(el), x, y, w, h, text, gtype)
                s.fill = styles.get(el.get("{%s}style-name" % DRAW), "fill")
                s.group = group_of.get(s.id)
                dr.shapes.append(s)
                if s.id:
                    dr.by_id[s.id] = s
            elif tag == "connector" and pi == 0:
                cn = {
                    "id": _el_id(el),
                    "start": el.get("{%s}start-shape" % DRAW),
                    "end": el.get("{%s}end-shape" % DRAW),
                    "x1": _cm(el.get("{%s}x1" % SVG)),
                    "y1": _cm(el.get("{%s}y1" % SVG)),
                    "x2": _cm(el.get("{%s}x2" % SVG)),
                    "y2": _cm(el.get("{%s}y2" % SVG)),
                    "stroke": styles.get(el.get("{%s}style-name" % DRAW), "stroke"),
                }
                dr.connectors.append(cn)
                if cn["id"]:
                    dr.by_id[cn["id"]] = cn
    return dr


def _norm(text):
    return re.sub(r"\s+", " ", (text or "").strip())


def find_plates(shapes):
    plates = []
    seen = set()
    for s in shapes:
        if s.page != 0:
            continue
        m = PLATE_RE.match(_norm(s.text))
        if not m or s.h < 8:
            continue
        name = m.group(1).strip() or "Top"
        if name in seen:
            continue
        seen.add(name)
        plates.append((s.x, name))
    plates.sort()
    return plates


def _plate_temp(name):
    if name in PLATE_TEMPS:
        return PLATE_TEMPS[name]
    key = name.rstrip(".")
    return PLATE_TEMPS.get(key, "")


def _nearest_plate(x, plates, max_dist=8.0):
    if not plates:
        return ""
    px, name = min(plates, key=lambda p: abs(p[0] - x))
    if abs(px - x) <= max_dist:
        return name
    return ""


def _is_line_name(text):
    return bool(LINE_NAME.match(_norm(text)))


def _canon_line(text):
    # "OUT B" -> "Out B", "in 4" -> "In 4"
    t = _norm(text)
    m = re.match(r"^(in|out|dc|pair)\s+(\S+)$", t, re.I)
    if not m:
        return t
    head = m.group(1).lower()
    head = {"in": "In", "out": "Out", "dc": "DC", "pair": "Pair"}[head]
    return "%s %s" % (head, m.group(2).upper())


def find_line_labels(shapes, plates):
    """Left-edge In/Out labels on page 1 (ignore the duplicate MXC copies)."""
    left = plates[0][0] if plates else 10.0
    labels = []
    seen = set()
    for s in shapes:
        if s.page != 0 or s.h > 4:
            continue
        t = _norm(s.text)
        if not _is_line_name(t):
            continue
        if s.x > left - 0.5:
            continue
        key = _canon_line(t)
        if key in seen:
            continue
        seen.add(key)
        labels.append((s.y, key))
    labels.sort()
    return labels


def _row_items(shapes, y, tol=0.40):
    out = []
    for s in shapes:
        if s.page != 0 or not s.text:
            continue
        if s.h > 5:
            continue
        if abs(s.y - y) <= tol:
            out.append(s)
    out.sort(key=lambda s: s.x)
    return out


def _atten_token(text):
    t = _norm(text).replace(" ", "")
    if t in ("-0dB", "+0dB"):
        t = "0dB"
    return t


def classify_inline(text):
    """Map an ODG label to a wiagram chain token, or None to skip."""
    t = _norm(text)
    if not t:
        return None
    low = t.lower()
    if low in SKIP_LABELS:
        return None
    if _is_line_name(t) or PLATE_RE.match(t):
        return None
    if ATTEN_RE.match(t):
        return _atten_token(t)
    m = K_ATTEN_RE.match(t)
    if m:
        return "-%sdB" % m.group(1)
    m = re.match(r"^[-]?(\d+)$", t)
    if m and low.startswith("-"):
        return "-%sdB" % m.group(1)     # "-10" box under a coupler
    if low in ("knl", "k&l"):
        return "knl"
    if "eccosorb" in low:
        return "eccosorb"
    m = HEMT_RE.match(t)
    if m:
        arg = m.group(1).strip()
        return "hemt(%s)" % arg if arg else "hemt"
    if low in ("db-25", "db25"):
        return "db25"
    if low == "fischer":
        return "fischer"
    if low in ("term.", "term", "50", "50ω", "50 ohm", "50 ω"):
        return "term"
    if "directional coupler" in low or low == "coupler":
        return "coupler"
    if low in ("iso", "isolator"):
        return "iso"
    if re.match(r"^ss\s*lp", low):
        return "lp(SS)"
    if re.match(r"^ss\s*bp", low):
        return "bp(SS)"
    if re.match(r"^lpf\s*", low):
        return "lp(%s)" % re.sub(r"^lpf\s*", "", t, flags=re.I)
    if re.match(r"^(lp|hp|bp)\b", low):
        return "%s(%s)" % (low[:2], t[2:].strip() or "?")
    if re.search(r"\bir\b", low) or re.search(r"\d{3}\s*ir", low):
        return "filter(%s)" % _sanitize_filter_label(t)
    if low in ("filter", "filters"):
        return "filter(Filter)"
    if low in ("biastee", "bias-tee", "bias tee"):
        return "biastee"
    if low in ("f-f adapter", "ff adapter"):
        return "sma"
    if re.match(r"^z[a-z]{2,4}-", low):
        return "filter(%s)" % _sanitize_filter_label(t)   # Mini-Circuits part numbers
    return None


def _is_amp_text(t):
    low = _norm(t).lower()
    return bool(re.search(r"\b(spa|twpa|wjpa|jpa|amp|jjfet|hemt)\b", low))


def _sanitize_filter_label(text):
    t = _norm(text).replace("(", "[").replace(")", "]")
    t = t.replace("->", "-")
    return t[:40] if t else "part"


def map_hemts_to_lines(dr, line_labels):
    """Attach HEMTs to output lines.

    Incoming connectors at ~20 cm are trustworthy. Remaining HEMTs (Oxford
    draws some of them off-row) are assigned top-to-bottom to leftover Out
    lines so none are dropped. Returns {line_name: hemt Shape}.
    """
    by_id = {}
    hemt_shapes = []
    for s in dr.shapes:
        if s.page != 0:
            continue
        if s.id and HEMT_RE.match(_norm(s.text)):
            by_id[s.id] = s
            hemt_shapes.append(s)
    hemt_shapes.sort(key=lambda s: s.y)

    out_labels = [(y, n) for y, n in line_labels if n.lower().startswith("out ")]
    if not out_labels:
        return {}

    best_left = {}
    for c in dr.connectors:
        x1, y1, x2, y2 = c["x1"], c["y1"], c["x2"], c["y2"]
        if x1 is None or x2 is None:
            continue
        for sid, hx, ox, oy in (
                (c["start"], x1, x2, y2),
                (c["end"], x2, x1, y1)):
            if sid not in by_id:
                continue
            if ox >= hx - 0.2:
                continue
            prev = best_left.get(sid)
            if prev is None or ox < prev[0]:
                best_left[sid] = (ox, oy)

    mapping = {}
    used_lines = set()
    used_hemts = set()
    for sid, (ox, oy) in best_left.items():
        if ox > 22.5 or oy is None:
            continue
        y, name = min(out_labels, key=lambda ln: abs(ln[0] - oy))
        if abs(y - oy) > 0.6 or name in used_lines:
            continue
        mapping[name] = by_id[sid]
        used_lines.add(name)
        used_hemts.add(sid)

    remain_lines = [n for _y, n in out_labels if n not in used_lines]
    remain_hemts = [s for s in hemt_shapes if s.id not in used_hemts]
    for s, name in zip(remain_hemts, remain_lines):
        mapping[name] = s
    return mapping


def _anchor(token, plate):
    if not plate or "@" in token:
        return token
    if token.startswith("cable(") or token == "term":
        return token
    return "%s@%s" % (token, plate)


def build_input_chain(atts_by_plate, plate_names):
    """example/-style backbone: bulkhead every plate Top→Still, SS in between."""
    parts = []
    att4 = atts_by_plate.get("4K") or "-20dB"
    still = atts_by_plate.get("Still") or "0dB"
    seq = [p for p in INPUT_PLATES if p in plate_names]
    if not seq:
        seq = [p for p in plate_names if p in INPUT_PLATES]
    for i, plate in enumerate(seq):
        if i:
            parts.append("cable(SS)")
        if plate == "4K":
            parts.append("bulkhead@%s" % plate)
            parts.append(_anchor(att4, "4K"))
        elif plate == "Still":
            parts.append("bulkhead@%s" % plate)
            parts.append(_anchor(still, "Still"))
        else:
            parts.append("bulkhead@%s" % plate)
    parts.append("cable(SS)")
    return parts, {"att4k": att4, "still": still}


def build_output_chain(hemt_text, plate_names):
    # the line stops right after the HEMT: circulator stacks, Eccosorb
    # filters and everything else at MXC come from the drawing's wires and
    # are written to extracted.txt, where they can be edited per experiment
    parts = []
    seq = [p for p in OUTPUT_PLATES if p in plate_names]
    if not seq:
        seq = plate_names[:3]
    for i, plate in enumerate(seq):
        if plate == "4K":
            break
        if i:
            parts.append("cable(SS)")
        parts.append("bulkhead@%s" % plate)
    parts.append("cable(SS)")
    params = {}
    if hemt_text:
        m = HEMT_RE.match(hemt_text)
        amp = m.group(1).strip() if m else hemt_text
        params["amp"] = amp
        parts.append("hemt(%s)@4K" % amp)
        # the wire out of the HEMT is drawn, so it is extracted with its colour
    else:
        parts.append("bulkhead@4K")
        parts.append("cable(NbTi)")
    return parts, params


def build_dc_chain():
    return ["fischer", "bulkhead@Top", "db25", "cable(loom)", "db25@4K"], {}


def _line_kind(name):
    n = name.lower()
    if n.startswith("in "):
        return "in"
    if n.startswith("out "):
        return "out"
    if n.startswith("dc ") or n.startswith("pair "):
        return "dc"
    return "other"


def _parse_line_index(name):
    m = re.match(r"^(.*?)(\d+)$", name)
    if m:
        return m.group(1), int(m.group(2)), "n"
    m = re.match(r"^(.*?)([A-Z])$", name)
    if m:
        return m.group(1), ord(m.group(2)), "a"
    return name, None, None


def compress_ranges(entries):
    """entries: (line_name, tmpl, params_dict) → grouped (spec, tmpl, params)."""
    grouped = []
    i = 0
    while i < len(entries):
        name, tmpl, params = entries[i]
        pre, idx, kind = _parse_line_index(name)
        j = i
        if idx is not None:
            while j + 1 < len(entries):
                n2, t2, p2 = entries[j + 1]
                pre2, idx2, kind2 = _parse_line_index(n2)
                if t2 != tmpl or p2 != params or kind2 != kind or pre2 != pre:
                    break
                if idx2 != idx + (j + 1 - i):
                    break
                j += 1
        if j > i:
            last = entries[j][0]
            spec = "%s..%s" % (name, last)
        else:
            spec = name
        grouped.append((spec, tmpl, params))
        i = j + 1
    return grouped


def _param_call(tmpl, params):
    if not params:
        return tmpl
    bits = []
    for k in params:
        bits.append("%s=%s" % (k, params[k]))
    return "%s(%s)" % (tmpl, ", ".join(bits))


def _wrap_chain(parts, indent=7):
    chunks = []
    cur = parts[0]
    for p in parts[1:]:
        piece = " -> " + p
        if len(cur) + len(piece) > 70:
            chunks.append(cur + " ->")
            cur = p
        else:
            cur += piece
    chunks.append(cur)
    pad = " " * indent
    return ("\n" + pad).join(chunks)


def cluster_templates(lines):
    """
    lines: list of (name, group, direction, parts, params_hint)
    params_hint already has the values we want to parameterize (still, amp).
    Returns templates dict name -> spec, and line entries.
    """
    def skeleton(parts, hint):
        sk = []
        for p in parts:
            if hint.get("still") and (p == hint["still"] + "@Still" or p == hint.get("still")):
                sk.append("{still}@Still")
            elif hint.get("att4k") and (p == hint["att4k"] + "@4K"):
                sk.append("{att4k}@4K")
            elif hint.get("amp") and p.startswith("hemt(") and p.endswith(")@4K"):
                sk.append("hemt({amp})@4K")
            else:
                sk.append(p)
        return tuple(sk)

    buckets = defaultdict(list)
    for name, group, direction, parts, hint in lines:
        sk = skeleton(parts, hint)
        buckets[(group, direction, sk)].append((name, parts, hint))

    templates = []  # (tmpl_name, group, direction, skeleton)
    assignments = []  # (line_name, tmpl_name, params)
    used_names = set()

    def uniq(base):
        n = base
        k = 2
        while n in used_names:
            n = "%s_%d" % (base, k)
            k += 1
        used_names.add(n)
        return n

    for (group, direction, sk), members in buckets.items():
        need_att4k = any("{att4k}" in p for p in sk)
        need_still = any("{still}" in p for p in sk)
        need_amp = any("{amp}" in p for p in sk)

        def values(key):
            return sorted(set(m[2][key] for m in members if key in m[2]))

        sk = list(sk)
        if need_att4k and len(values("att4k")) == 1:
            sk = [p.replace("{att4k}", values("att4k")[0]) for p in sk]
            need_att4k = False
        if need_still and len(values("still")) == 1:
            sk = [p.replace("{still}", values("still")[0]) for p in sk]
            need_still = False

        sk_t = tuple(sk)
        if group == "Inputs" and not direction:
            tname = uniq("input_line")
        elif group == "Outputs" and need_amp:
            tname = uniq("output_amp")
        elif group == "Outputs":
            tname = uniq("output_bare")
        elif group == "DC":
            tname = uniq("dc_loom")
        else:
            tname = uniq(group.lower() or "line")

        templates.append((tname, group, direction, sk_t))
        for name, _parts, hint in members:
            params = {}
            if need_att4k:
                params["att4k"] = hint["att4k"]
            if need_still:
                params["still"] = hint["still"]
            if need_amp:
                params["amp"] = hint["amp"]
            assignments.append((name, tname, params))

    order = {name: i for i, (name, *_) in enumerate(lines)}
    assignments.sort(key=lambda e: order.get(e[0], 9999))
    return templates, assignments


def fridge_title(odg_path):
    base = os.path.basename(odg_path).lower()
    if "bluefors" in base:
        return "Bluefors Dilution Fridge"
    if "oxford" in base:
        return "Oxford Dilution Fridge"
    return os.path.splitext(os.path.basename(odg_path))[0]


def _mxc_x(plates):
    for x, n in plates:
        if n.upper().startswith("MXC"):
            return x
    return plates[-1][0] if plates else None


def extract_lines(dr, plates, labels, hemt_map):
    """Left-of-MXC part of every fridge line -> template entries."""
    shapes = dr.shapes
    plate_names = [n for _x, n in plates]
    mxc_x = _mxc_x(plates)

    fridge_lines = []  # (name, group, direction, parts, hint)

    fischer_ys = []
    for s in shapes:
        if s.page == 0 and _norm(s.text).lower() == "fischer" and s.x < 15:
            fischer_ys.append(s.y)
    fischer_ys.sort()
    loom_ys = []
    for y in fischer_ys:
        if not loom_ys or abs(y - loom_ys[-1]) > 0.5:
            loom_ys.append(y)

    for y, name in labels:
        kind = _line_kind(name)
        if kind == "in":
            atts = {}
            for s in _row_items(shapes, y):
                if s.x >= (mxc_x or 1e9) - 2.5:
                    continue
                tok = classify_inline(s.text)
                if not tok or not ATTEN_RE.match(tok):
                    continue
                plate = _nearest_plate(s.x, plates)
                if plate:
                    atts[plate] = tok
            parts, hint = build_input_chain(atts, plate_names)
            fridge_lines.append((name, "Inputs", "", parts, hint))
        elif kind == "out":
            hemt = hemt_map.get(name)
            parts, hint = build_output_chain(_norm(hemt.text) if hemt else "",
                                             plate_names)
            fridge_lines.append((name, "Outputs", "out", parts, hint))

    for i, _y in enumerate(loom_ys):
        name = "DC %s" % chr(ord("A") + i)
        parts, hint = build_dc_chain()
        fridge_lines.append((name, "DC", "", parts, hint))

    fridge_lines.sort(
        key=lambda e: {"Outputs": 0, "DC": 1, "Inputs": 2}.get(e[1], 9))
    return fridge_lines


def emit_common(odg_path, plates, fridge_lines):
    templates, assignments = cluster_templates(fridge_lines)
    title = fridge_title(odg_path)
    src = os.path.basename(odg_path)
    lines = []
    lines.append("# Shared fridge infrastructure.")
    lines.append("# Extracted from %s by 'wiagram import'" % src)
    lines.append("")
    lines.append("owner: Common")
    lines.append("fridge: %s" % title)
    lines.append("")
    lines.append("[plates]")
    for _x, name in plates:
        temp = _plate_temp(name) or "?"
        lines.append("%s: %s" % (name, temp))
    lines.append("")

    order = {"Outputs": 0, "DC": 1, "Inputs": 2}
    templates.sort(key=lambda t: (order.get(t[1], 9), t[0]))
    comments = {
        "Inputs": "# ---- input lines ----------------------------------------------------------",
        "Outputs": "# ---- output lines ---------------------------------------------------------",
        "DC": "# ---- DC looms -------------------------------------------------------------",
    }
    seen_group = set()
    for tname, group, direction, sk in templates:
        if group not in seen_group:
            if seen_group:
                lines.append("")
            if group in comments:
                lines.append(comments[group])
            seen_group.add(group)
        lines.append("")
        lines.append("[template %s]" % tname)
        lines.append("group: %s" % group)
        if direction:
            lines.append("direction: %s" % direction)
        lines.append("chain: %s" % _wrap_chain(list(sk)))
    lines.append("")
    lines.append("[lines]")
    for spec, tmpl, params in compress_ranges(assignments):
        lines.append("%s: %s" % (spec, _param_call(tmpl, params)))
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MXC side: parts + wires -> graph -> [wiring]
# ---------------------------------------------------------------------------

def _slug(text):
    s = re.sub(r"[^A-Za-z0-9]+", "", text or "")
    if not s:
        return ""
    if s[0].isdigit():
        s = "x" + s
    return s.lower()


def _circ_base(text):
    t = _norm(text)
    t = re.sub(r"(?i)^k[- ]?band circulator", "Kband", t)
    t = re.sub(r"(?i)circulator", "", t).strip(" ?")
    t = t.split("/")[0]  # "C3/4?" -> "C3"
    return _slug(t)


class Part:
    """One node of the drawing net."""

    def __init__(self, pid, kind, label, shape):
        self.id = pid
        self.kind = kind        # circulator / isolator / coupler / amplifier /
        #                         experiment / inline / term / line / dc / anchor
        self.label = label
        self.shape = shape
        self.token = ""         # inline token (-10dB, knl, filter(...))
        self.name = ""          # device name once declared
        self.ports = {}         # port -> side (experiments)
        self.tags = []          # (x, y, name) port tags around an experiment
        self.container = None   # circulator block this arrow sits in
        self.line = ""          # for kind == line / anchor
        self.plate = ""

    @property
    def x(self):
        return self.shape.x

    @property
    def y(self):
        return self.shape.y

    def __repr__(self):
        return "Part(%s %s %r)" % (self.id, self.kind, self.label)


class Net:
    def __init__(self, dr, plates, labels, hemt_map, notes):
        self.dr = dr
        self.plates = plates
        self.labels = labels
        self.hemt_map = hemt_map
        self.notes = notes
        self.mxc_x = _mxc_x(plates)
        self.parts = {}            # id -> Part
        self.graph = nx.MultiGraph()
        self.shape_part = {}       # Shape id -> Part id (direct)
        self.containers = []       # (Shape, [arrow Parts])
        self.port_taken = defaultdict(set)   # part id -> ports in use
        self.line_parts = {}       # line name -> the one Part standing for it
        self.mxc_labels = set()    # line names that have a label box at MXC
        self.clamps = []           # unlabelled bulkhead boxes on the MXC plate
        self.hit_extra = []        # (Shape, Part): port stubs / tags that count as hits
        self.shields = []          # mu-metal cans etc. (wires glued to their edge)
        self.legend = None
        self.seq = 0

    def line_part(self, name, shape=None, kind="line"):
        # exactly one node per fridge line, whatever the drawing calls it
        p = self.line_parts.get(name)
        if p is not None:
            if shape is not None:
                self.shape_part[id(shape)] = p.id
                if shape.id:
                    self.shape_part[shape.id] = p.id
                self.hit_extra.append((shape, p))
            return p
        if shape is None:
            y = 0.0
            for ly, n in self.labels:
                if n == name:
                    y = ly
            shape = Shape(0, None, (self.mxc_x or 0) - 1.0, y, 2.0, 0.6, name, "virtual")
        p = Part(self.new_id("line"), kind, name, shape)
        p.line = name
        self.add_part(p, shape)
        self.line_parts[name] = p
        return p

    # ----- helpers -------------------------------------------------------

    def note(self, msg):
        self.notes.append(msg)

    def new_id(self, base):
        self.seq += 1
        return "%s%d" % (base, self.seq)

    def region_x0(self):
        # halfway between MXC and the plate before it: circulator stacks
        # drawn between Cold and MXC count as MXC-side
        xs = sorted(x for x, _n in self.plates)
        if self.mxc_x is None or len(xs) < 2:
            return (self.mxc_x or 0) - 3.0
        prev = [x for x in xs if x < self.mxc_x - 0.1]
        if not prev:
            return self.mxc_x - 3.0
        return (prev[-1] + self.mxc_x) / 2.0

    def in_legend(self, s):
        return self.legend is not None and self.legend.contains(s.x, s.y, 0.2)

    # ----- 1. classify shapes into parts ---------------------------------

    def build_parts(self):
        shapes = [s for s in self.dr.shapes if s.page == 0]
        for s in shapes:
            if _norm(s.text).lower() == "legend" and s.w > 8:
                self.legend = s
                break
        x0 = self.region_x0()
        plate_bars = [s for s in shapes if PLATE_RE.match(_norm(s.text)) and s.h > 8]

        region = []
        for s in shapes:
            if s.gtype == "frame" or self.in_legend(s) or s in plate_bars:
                continue
            if s.x < x0 and s.gtype != "circular-arrow":
                continue
            region.append(s)
        region = _dedupe_shapes(region)   # copy-pasted twins sit exactly on top

        # circulator containers (labelled rectangles holding the arrow symbols)
        circ_rects = [s for s in region if s.gtype != "circular-arrow"
                      and "circulat" in _norm(s.text).lower()]
        circ_rects = _dedupe_shapes(circ_rects)
        arrows = _dedupe_shapes([s for s in region if s.gtype == "circular-arrow"])
        taken = set()
        for rect in sorted(circ_rects, key=lambda r: (r.y, r.x)):
            inside = [a for a in arrows if id(a) not in taken
                      and rect.contains(a.x, a.y, 0.3)]
            if not inside and rect.group:
                grp = self.dr.by_id.get(rect.group)
                if grp and grp[0] == "group":
                    inside = [a for a in arrows if a.id in grp[1] and id(a) not in taken]
            inside.sort(key=lambda a: a.x)
            for a in inside:
                taken.add(id(a))
            self.containers.append((rect, inside))
        for a in arrows:
            if id(a) not in taken:
                # bare arrow with no labelled box
                self.containers.append((None, [a]))

        for rect, inside in self.containers:
            label = _norm(rect.text) if rect is not None else "Circulator"
            if inside:
                for a in inside:
                    p = Part(self.new_id("circ"), "circulator", label, a)
                    p.container = rect if rect is not None else a
                    self.add_part(p, a)
            else:
                p = Part(self.new_id("circ"), "circulator", label, rect)
                p.container = rect
                self.add_part(p, rect)

        # everything else
        tags = []
        stubs = []
        experiments = []
        ecc_boxes = [s for s in region if "eccosorb" in _norm(s.text).lower() and s.h > 3]
        for s in region:
            if s.gtype == "circular-arrow" or any(s is r for r, _i in self.containers):
                continue
            if any(r is not None and r is s for r, _i in self.containers):
                continue
            t = _norm(s.text)
            low = t.lower()
            if not t:
                if (self.mxc_x is not None and abs(s.x - self.mxc_x) < 2.5
                        and s.w < 3.0 and s.h < 1.5):
                    self.clamps.append(s)
                else:
                    stubs.append(s)
                continue
            if DC_WORDS.match(t) or s.fill in ("#5eb91e",) and not _is_amp_text(t):
                p = Part(self.new_id("dc"), "dc", t, s)
                self.add_part(p, s)
                continue
            if _is_line_name(t):
                self.mxc_labels.add(_canon_line(t))
                self.line_part(_canon_line(t), s)
                continue
            if self._is_container(s):
                self.shields.append(s)
                continue
            if self._is_tag(s):
                tags.append(s)
                continue
            if low in SKIP_LABELS or PLATE_RE.match(t):
                continue
            if "eccosorb" in low and s.h > 3:
                continue   # the column holding the numbered filters
            if "circulat" in low:
                continue
            if "isolator" in low:
                self.add_part(Part(self.new_id("iso"), "isolator", t, s), s)
                continue
            if "coupler" in low:
                self.add_part(Part(self.new_id("dc"), "coupler", t, s), s)
                continue
            if HEMT_RE.match(t):
                continue  # HEMTs are handled as line anchors
            if _is_amp_text(t) and not classify_inline(t):
                self.add_part(Part(self.new_id("amp"), "amplifier", t, s), s)
                continue
            tok = classify_inline(t)
            if tok == "term":
                self.add_part(Part(self.new_id("term"), "term", t, s), s)
                continue
            if tok in ("db25", "fischer"):
                self.add_part(Part(self.new_id("dc"), "dc", t, s), s)
                continue
            if tok and tok not in ("coupler", "iso", "hemt") and not tok.startswith("hemt"):
                p = Part(self.new_id("p"), "inline", t, s)
                p.token = tok
                if self.mxc_x is not None and abs(s.x - self.mxc_x) < 4.0:
                    p.plate = "MXC"   # first box on the row, bolted to the plate
                self.add_part(p, s)
                continue
            if t.isdigit():
                # Eccosorb index column at MXC, or a port number tag
                near_mxc = self.mxc_x is not None and abs(s.x - self.mxc_x) < 2.5
                in_ecc = any(b.contains(s.x, s.y, 0.5) for b in ecc_boxes)
                if near_mxc or in_ecc:
                    p = Part(self.new_id("p"), "inline", "Ecc %s" % t, s)
                    p.token = "filter(Ecc %s)" % t
                    p.plate = "MXC"
                    self.add_part(p, s)
                else:
                    tags.append(s)
                continue
            if s.w < 2.0 and s.h < 1.0:
                tags.append(s)
                continue
            # a labelled box we cannot name is somebody's experiment / sample
            p = Part(self.new_id("expt"), "experiment", t, s)
            self.add_part(p, s)
            experiments.append(p)

        # HEMT anchors (where the Out lines leave the drawing's left half)
        for lname, hs in self.hemt_map.items():
            self.line_part(lname, hs, kind="anchor")

        # port stubs and tags on experiments
        frames = [s for s in self.dr.shapes if s.page == 0 and s.gtype == "frame"
                  and not self.in_legend(s)]
        for stub in stubs:
            host = self._host_of(stub, experiments, margin=0.3, overlap=True)
            if host is None:
                continue
            name = None
            best = None
            for f in frames:
                t = _norm(f.text)
                if not t or SERIAL_RE.match(t) or len(t) > 12:
                    continue
                # distance from the label to the stub's box
                dx = max(stub.x0 - f.x, 0.0, f.x - stub.x1)
                dy = max(stub.y0 - f.y, 0.0, f.y - stub.y1)
                d = math.hypot(dx, dy)
                if d < 2.2 and (best is None or d < best):
                    best, name = d, t
            if not name:
                name = self._side_name(host, stub.x, stub.y)
            host.tags.append((stub.x, stub.y, _port_name(name)))
            self.hit_extra.append((stub, host))
        for tag in tags:
            host = self._host_of(tag, experiments, margin=0.9)
            if host is None:
                # a port tag on the wall of a shield can is a feedthrough:
                # wires on both sides of it are one connection through it
                if tag.fill == "#729fcf" and any(
                        can.contains(tag.x, tag.y, 0.6) for can in self.shields):
                    p = Part(self.new_id("p"), "inline", _norm(tag.text), tag)
                    p.token = "bulkhead"
                    self.add_part(p, tag)
                # tags also sit on couplers (In / Out / Cpl) -> ignore, geometry decides
                continue
            host.tags.append((tag.x, tag.y, _port_name(_norm(tag.text))))
            self.hit_extra.append((tag, host))
        for f in frames:
            t = _norm(f.text)
            if not t or SERIAL_RE.match(t) or len(t) > 12:
                continue
            if t.lower() not in PORT_WORDS and len(t) > 4:
                continue
            host = self._host_of(f, experiments, margin=2.0)
            if host is None:
                continue
            if any(abs(tx - f.x) < 1.5 and abs(ty - f.y) < 1.5 for tx, ty, _n in host.tags):
                continue
            host.tags.append((f.x, f.y, _port_name(t)))

    def _is_container(self, s):
        low = _norm(s.text).lower()
        if s.fill == "#ffd8ce":
            return True
        if re.search(r"(mu[- ]?metal|cryoperm|can\b|shield|legend)", low):
            return True
        return False

    def _is_tag(self, s):
        t = _norm(s.text)
        if s.w > 2.2 or s.h > 1.8:
            return False
        if t.lower() in PORT_WORDS:
            return True
        return False

    def _host_of(self, s, experiments, margin=0.5, overlap=False):
        best = None
        for e in experiments:
            if overlap:
                hit = (s.x0 < e.shape.x1 + margin and s.x1 > e.shape.x0 - margin
                       and s.y0 < e.shape.y1 + margin and s.y1 > e.shape.y0 - margin)
            else:
                hit = e.shape.contains(s.x, s.y, margin)
            if hit:
                area = e.shape.w * e.shape.h
                if best is None or area < best[0]:
                    best = (area, e)
        return best[1] if best else None

    def _side_name(self, host, x, y):
        return {"W": "in", "E": "out", "N": "top", "S": "bottom"}[
            _side_of(host.shape, x, y)]

    def add_part(self, p, shape):
        self.parts[p.id] = p
        self.graph.add_node(p.id, part=p)
        self.shape_part[id(shape)] = p.id
        if shape.id:
            self.shape_part[shape.id] = p.id

    # ----- 2. resolve connector ends -------------------------------------

    def part_at(self, x, y, margin=0.35):
        best = None
        for p in self.parts.values():
            if p.kind == "anchor":
                continue
            s = p.shape
            if s.contains(x, y, margin):
                area = s.w * s.h
                if best is None or area < best[0]:
                    best = (area, p)
        if best:
            return best[1]
        # a port stub / tag on an experiment, or a bulkhead box standing for
        # a fridge line (pick the closest when small boxes are stacked)
        best = None
        for s, host in self.hit_extra:
            if s.contains(x, y, margin):
                d = _outside_dist(s, x, y)
                if best is None or d < best[0]:
                    best = (d, host)
        if best:
            return best[1]
        # inside a circulator block but between the arrows
        for rect, inside in self.containers:
            if rect is not None and inside and rect.contains(x, y, margin):
                return self._nearest_arrow(inside, x, y)
        # big boxes are drawn loosely: allow a wider miss on experiments
        for p in self.parts.values():
            if p.kind == "experiment" and p.shape.contains(x, y, 0.75):
                return p
        # a wire glued to the edge of a shield can means "the device on that
        # row inside the can"
        for can in self.shields:
            if not can.contains(x, y, 0.5):
                continue
            best = None
            for p in self.parts.values():
                if p.kind != "experiment" or not can.contains(p.x, p.y):
                    continue
                dy = abs(p.y - y)
                if dy < 1.6 and (best is None or dy < best[0]):
                    best = (dy, p)
            if best:
                return best[1]
        return None

    def _nearest_arrow(self, arrows, x, y):
        a = min(arrows, key=lambda s: abs(s.x - x) + 0.2 * abs(s.y - y))
        return self.parts[self.shape_part[id(a)]]

    def resolve_ref(self, ref, x, y):
        """Glued shape id -> Part (group -> nearest arrow / child)."""
        obj = self.dr.by_id.get(ref)
        if obj is None:
            return None
        if isinstance(obj, tuple) and obj[0] == "group":
            kids = []
            for cid in obj[1]:
                pid = self.shape_part.get(cid)
                if pid:
                    kids.append(self.parts[pid])
            arrows = [k for k in kids if k.kind == "circulator"]
            if arrows:
                return min(arrows, key=lambda k: abs(k.x - x) + 0.2 * abs(k.y - y))
            if kids:
                return min(kids, key=lambda k: math.hypot(k.x - x, k.y - y))
            return None
        if isinstance(obj, dict):
            return None  # connector: handled by the net union
        pid = self.shape_part.get(id(obj))
        if pid:
            return self.parts[pid]
        # a circulator label rectangle -> nearest arrow in it
        for rect, inside in self.containers:
            if rect is obj and inside:
                return self._nearest_arrow(inside, x, y)
        return None

    def line_at(self, x, y):
        # the MXC-side end of a fridge line is a label box; its left-side
        # twin is at the same y, so a free end on the row also counts
        if self.mxc_x is None or x > self.mxc_x + 4.0:
            return None
        best = None
        for ly, name in self.labels:
            if name not in self.mxc_labels:
                continue   # this line has no label row at MXC (e.g. Oxford Outs)
            d = abs(ly - y)
            if d < 0.38 and (best is None or d < best[0]):
                best = (d, name)
        return best[1] if best else None

    def build_wires(self):
        conns = [c for c in self.dr.connectors if c["x1"] is not None]
        # union connectors glued to each other or sharing an end point
        uf = {i: i for i in range(len(conns))}

        def find(i):
            while uf[i] != i:
                uf[i] = uf[uf[i]]
                i = uf[i]
            return i

        def union(a, b):
            uf[find(a)] = find(b)

        idx_of = {c["id"]: i for i, c in enumerate(conns) if c["id"]}
        ends = []
        for i, c in enumerate(conns):
            for ref, x, y in ((c["start"], c["x1"], c["y1"]), (c["end"], c["x2"], c["y2"])):
                if ref in idx_of:
                    union(i, idx_of[ref])
                ends.append((x, y, i))
        ends.sort()
        for k in range(len(ends) - 1):
            x, y, i = ends[k]
            for m in range(k + 1, min(k + 6, len(ends))):
                x2, y2, j = ends[m]
                if abs(x2 - x) > 0.15:
                    break
                if abs(y2 - y) < 0.15 and i != j:
                    union(i, j)

        nets = defaultdict(list)
        for i, c in enumerate(conns):
            nets[find(i)].append(c)

        x0 = self.region_x0()
        ordered = sorted(nets.values(), key=lambda m: (m[0]["y1"], m[0]["x1"]))

        # pass 1: a fridge line that reaches the MXC plate through an
        # unlabelled bulkhead/clamp box (Oxford) -- the box becomes the line
        if self.clamps:
            for members in ordered:
                line = None
                hits = []
                for c in members:
                    for ref, x, y in ((c["start"], c["x1"], c["y1"]),
                                      (c["end"], c["x2"], c["y2"])):
                        if ref in idx_of:
                            continue
                        p = self.resolve_ref(ref, x, y) if ref else None
                        cl = min((c for c in self.clamps if c.contains(x, y, 0.3)),
                                 key=lambda c: _outside_dist(c, x, y), default=None)
                        if p is None and cl is not None:
                            hits.append(cl)
                            continue
                        if p is None:
                            p = self.part_at(x, y)
                        if p is not None and p.kind in ("line", "anchor"):
                            line = p
                if line is not None and line.line:
                    for cl in hits:
                        self.line_part(line.line, cl)

        for members in ordered:
            strokes = {c["stroke"] for c in members}
            if strokes & THERMAL_STROKES and not (strokes - THERMAL_STROKES):
                continue  # thermalisation straps, not signal
            xs = [c["x1"] for c in members] + [c["x2"] for c in members]
            if max(xs) < x0:
                continue  # entirely in the template half of the drawing
            cable = ""
            for st in strokes:
                if st in STROKE_CABLE:
                    cable = STROKE_CABLE[st]
                    break
            # green is the DC colour, but Oxford also uses it for RF cabling
            # inside sample cans: only treat it as DC if it reaches a DC part
            is_dc_wire = False
            terminals = []   # (Part, x, y)
            loose = []
            for c in members:
                for ref, x, y in ((c["start"], c["x1"], c["y1"]), (c["end"], c["x2"], c["y2"])):
                    if ref in idx_of:
                        continue
                    p = self.resolve_ref(ref, x, y) if ref else None
                    if p is None:
                        p = self.part_at(x, y)
                    if p is None:
                        ln = self.line_at(x, y)
                        if ln:
                            p = self.line_part(ln)
                        else:
                            loose.append((x, y))
                            continue
                    terminals.append((p, x, y))
            # drop duplicate terminals (same part twice from a shared end)
            # (a wire touching both the body and a port stub of the same
            # experiment is one connection)
            uniq = []
            for t in terminals:
                if any(u[0] is t[0] for u in uniq):
                    continue
                uniq.append(t)
            terminals = uniq

            in_scope = any(t[0].kind not in ("line", "anchor") for t in terminals)
            if not in_scope:
                continue   # fridge-line backbones etc.
            if is_dc_wire or any(t[0].kind == "dc" for t in terminals):
                self._note_dc(terminals)
                continue
            if len(terminals) < 2:
                if terminals:
                    t = terminals[0]
                    where = loose[0] if loose else (t[1], t[2])
                    self.note("wire from '%s' ends in empty space at (%.1f, %.1f) cm"
                              % (t[0].label, where[0], where[1]))
                continue
            if len(terminals) > 2:
                names = ", ".join(t[0].label for t in terminals)
                self.note("wire joins %d parts (%s); connected in sequence"
                          % (len(terminals), names))
            for a, b in zip(terminals, terminals[1:]):
                self.add_edge(a, b, cable, "wire")

    def _note_dc(self, terminals):
        rf = [t for t in terminals if t[0].kind != "dc"]
        dc = [t for t in terminals if t[0].kind == "dc"]
        if rf and dc:
            self.note("DC wire %s -> %s skipped (no DC ports in the text format)"
                      % (", ".join(t[0].label for t in dc), ", ".join(t[0].label for t in rf)))

    # ----- 3. abutting boxes and terminations ----------------------------

    def build_abutments(self):
        # arrows inside one circulator block are in series
        for rect, inside in self.containers:
            ps = [self.parts[self.shape_part[id(a)]] for a in inside]
            for a, b in zip(ps, ps[1:]):
                self._edge_ports(a, "E", b, "W", "", "series")

        boxes = []
        for p in self.parts.values():
            if p.kind in ("inline", "amplifier", "coupler", "isolator", "line", "term"):
                boxes.append((p, p.shape))
        for rect, inside in self.containers:
            if rect is not None and inside:
                boxes.append((("block", inside), rect))
            elif rect is None:
                a = inside[0]
                boxes.append((self.parts[self.shape_part[id(a)]], a))
        boxes.sort(key=lambda b: (b[1].x0, b[1].y))
        for i, (pa, sa) in enumerate(boxes):
            for pb, sb in boxes[i + 1:]:
                gap = sb.x0 - sa.x1
                if gap > 0.4:
                    if sb.x0 - sa.x1 > 6:
                        break
                    continue
                if gap < -0.6:
                    continue
                overlap = min(sa.y1, sb.y1) - max(sa.y0, sb.y0)
                if overlap < 0.5 * min(sa.h, sb.h):
                    continue
                a = pa
                b = pb
                if isinstance(a, tuple):
                    a = self.parts[self.shape_part[id(a[1][-1])]]
                if isinstance(b, tuple):
                    b = self.parts[self.shape_part[id(b[1][0])]]
                if a is b or a.kind == "term" and b.kind == "term":
                    continue
                self._edge_ports(a, "E", b, "W", "", "abut")

    def build_hangs(self):
        """Boxes drawn touching the underside of a circulator / coupler.

        "Term." under a circulator, "-10" under a coupler: no connector is
        drawn, the box simply sits on the port. Runs after the wires so only
        boxes that still have a free port are considered.
        """
        hosts = [p for p in self.parts.values() if p.kind in ("circulator", "coupler")]
        for t in sorted(self.parts.values(), key=lambda p: (p.y, p.x)):
            if t.kind not in ("term", "inline"):
                continue
            if self.graph.degree(t.id) >= (1 if t.kind == "term" else 2):
                continue
            best = None
            for c in hosts:
                cs = c.shape
                dx = abs(c.x - t.x)
                gap = t.shape.y0 - cs.y1
                x_over = dx < max(cs.w, 1.2) / 2.0 + 0.4
                if x_over and -0.3 < gap < 1.2:
                    d = dx + max(gap, 0)
                    if best is None or d < best[0]:
                        best = (d, c, "S")
            if best is None and t.kind == "term":
                # "50" beside a circulator
                for c in hosts:
                    if c.kind != "circulator":
                        continue
                    d = math.hypot(c.x - t.x, c.y - t.y)
                    if d < 2.4 and (best is None or d < best[0]):
                        best = (d, c, _side_of(c.shape, t.x, t.y))
            if best is None:
                if t.kind == "term":
                    self.note("termination '%s' at (%.1f, %.1f) cm is not under any "
                              "circulator or coupler" % (t.label, t.x, t.y))
                continue
            _d, host, side = best
            self._edge_ports(host, side, t, "N", "", "hang")

    def orient_circulators(self):
        """Number circulator ports by signal flow: 1 in, 2 out, 3 isolated.

        The drawing never labels circulator ports, so the geometric guess is
        replaced by one that follows the path from the input lines towards
        the HEMTs. Blocks that touch neither keep the geometric numbering.
        """
        g = self.graph
        sources = [p.id for p in self.parts.values()
                   if p.kind in ("line", "anchor") and _line_kind(p.line) == "in"]
        sinks = [p.id for p in self.parts.values()
                 if p.kind in ("line", "anchor") and _line_kind(p.line) == "out"]
        d_in = _multi_source_dist(g, sources)
        d_out = _multi_source_dist(g, sinks)
        INF = 10 ** 6
        for c in self.parts.values():
            if c.kind != "circulator":
                continue
            edges = list(g.edges(c.id, keys=True, data=True))
            if not edges:
                continue
            if len(edges) == 1:
                # a dangling block: its one wire is the output if it leads
                # towards an Out line, the input if it comes from an In line
                u, v, k, d = edges[0]
                nbr = v if u == c.id else u
                if d_out.get(nbr, INF) < INF and d_in.get(nbr, INF) == INF:
                    d["ports"][c.id] = "2"
                elif d_in.get(nbr, INF) < INF:
                    d["ports"][c.id] = "1"
                continue
            scored = []
            for u, v, k, d in edges:
                nbr = v if u == c.id else u
                p = self.parts[nbr]
                di = d_in.get(nbr, INF)
                do = d_out.get(nbr, INF)
                if p.kind == "term":
                    score = (2, 0)          # isolated port
                elif di < INF or do < INF:
                    # upstream neighbours are nearer the inputs / farther from outputs
                    score = (0, di - do)
                else:
                    score = (1, 0)
                scored.append((score, nbr, (u, v, k, d)))
            if all(s[0][0] != 0 for s in scored):
                continue  # nothing to orient by
            scored.sort(key=lambda s: (s[0], s[1]))
            assign = {}
            # most upstream -> 1, most downstream -> 2, the rest -> 3
            flow = [s for s in scored if s[0][0] == 0]
            assign[id(flow[0][2][3])] = "1"
            if len(flow) > 1:
                assign[id(flow[-1][2][3])] = "2"
            for s in scored:
                if id(s[2][3]) not in assign:
                    assign[id(s[2][3])] = "3" if "3" not in assign.values() else \
                        ("2" if "2" not in assign.values() else "1")
            for _u, _v, _k, d in edges:
                d["ports"][c.id] = assign[id(d)]

    # ----- edges with port bookkeeping -----------------------------------

    def add_edge(self, a, b, cable, how):
        pa, xa, ya = a
        pb, xb, yb = b
        if pa is pb:
            return
        side_a = _side_of(pa.shape, xa, ya)
        side_b = _side_of(pb.shape, xb, yb)
        if how == "wire":
            pa, side_a = self._slide(pa, side_a)
            pb, side_b = self._slide(pb, side_b)
            if pa is pb:
                return
        # a wire landing on a tagged port of an experiment
        hint_a = self._tag_near(pa, xa, ya)
        hint_b = self._tag_near(pb, xb, yb)
        self._edge_ports(pa, side_a, pb, side_b, cable, how, hint_a, hint_b)

    def _slide(self, p, side):
        """A wire glued to a box whose side is covered by abutting boxes
        (e.g. '-10dB | KNL | -20dB' drawn over the connector) really leaves
        from the far end of that row of boxes."""
        if p.kind not in ("inline", "amplifier", "isolator") or side not in ("E", "W"):
            return p, side
        seen = {p.id}
        while True:
            nxt = None
            for u, v, d in self.graph.edges(p.id, data=True):
                if d["how"] != "abut":
                    continue
                o = self.parts[v if u == p.id else u]
                if o.id in seen or o.kind not in ("inline", "amplifier", "isolator"):
                    continue
                if (side == "E" and o.x > p.x) or (side == "W" and o.x < p.x):
                    nxt = o
                    break
            if nxt is None:
                return p, side
            seen.add(nxt.id)
            p = nxt

    def _tag_near(self, p, x, y):
        if p.kind != "experiment" or not p.tags:
            return None
        best = None
        for tx, ty, name in p.tags:
            d = math.hypot(tx - x, ty - y)
            if d < 4.5 and (best is None or d < best[0]):
                best = (d, name)
        return best[1] if best else None

    def _edge_ports(self, a, side_a, b, side_b, cable, how, hint_a=None, hint_b=None):
        if a is b:
            return
        if a.kind in ("line", "anchor") and b.kind in ("line", "anchor"):
            return
        pa = self._pick_port(a, side_a, hint_a)
        pb = self._pick_port(b, side_b, hint_b)
        if pa is False or pb is False:
            full = a if pa is False else b
            self.note("'%s' has no free port for another wire (to '%s'); wire dropped"
                      % (full.label, (b if full is a else a).label))
            return
        self.port_taken[a.id].add(pa)
        self.port_taken[b.id].add(pb)
        # ports keyed by node id: MultiGraph.edges() does not keep (u, v) order
        self.graph.add_edge(a.id, b.id, ports={a.id: pa, b.id: pb}, cable=cable, how=how)

    def _pick_port(self, p, side, hint=None):
        taken = self.port_taken[p.id]
        if p.kind == "circulator":
            want = {"W": "1", "E": "2", "S": "3", "N": "3"}.get(side, "2")
            for cand in [want] + [q for q in ("1", "2", "3") if q != want]:
                if cand not in taken:
                    return cand
            return False
        if p.kind == "coupler":
            if side == "S":
                want = "cpl"
            else:
                want = {"W": "in", "E": "out", "N": "cpl"}.get(side, "out")
            order = [want] + [q for q in ("in", "out", "cpl", "iso") if q != want]
            for cand in order:
                if cand not in taken:
                    return cand
            return False
        if p.kind in ("inline", "isolator", "amplifier", "term"):
            # two-port things: direction is sorted out by the chain walk.
            # amplifiers may collect pump wires on extra ports.
            n = len(taken)
            if p.kind == "term":
                return "p" if n == 0 else False
            if n == 0:
                return "in"
            if n == 1:
                return "out"
            if p.kind == "amplifier":
                return "pump" if n == 2 else "pump%d" % (n - 1)
            return False
        if p.kind == "experiment":
            if hint and hint not in taken:
                return hint
            base = {"W": "in", "E": "out", "N": "top", "S": "bottom"}.get(side, "port")
            if hint:
                base = hint
            cand = base
            k = 2
            while cand in taken:
                cand = "%s%d" % (base, k)
                k += 1
            return cand
        if p.kind in ("line", "anchor"):
            return "line"
        return False

    # ----- 4. naming -----------------------------------------------------

    def name_devices(self):
        used = set()

        def uniq(base):
            n = base
            k = 2
            while n in used:
                n = "%s_%d" % (base, k)
                k += 1
            used.add(n)
            return n

        # circulator blocks: label from the box, or from the Out line they serve
        for rect, inside in self.containers:
            arrows = [self.parts[self.shape_part[id(a)]] for a in inside] if inside else []
            if not arrows and rect is not None:
                arrows = [self.parts[self.shape_part[id(rect)]]]
            base = _circ_base(rect.text) if rect is not None else ""
            if not base:
                line = self._served_line(arrows)
                if line:
                    base = "out" + line.split()[-1].lower()
                else:
                    base = "circ"
            if len(arrows) == 1:
                name = uniq(base)
                arrows[0].name = name
                arrows[0].label = (_circ_base(rect.text).upper() if rect is not None and _circ_base(rect.text)
                                   else name.upper())
            else:
                for i, a in enumerate(arrows):
                    letter = chr(ord("a") + i)
                    a.name = uniq("%s_%s" % (base, letter))
                    if rect is not None and _circ_base(rect.text):
                        a.label = "%s%s" % (_circ_base(rect.text).upper(), letter)
                    else:
                        a.label = "%s %s" % (base.upper(), letter)

        for p in self.parts.values():
            if self.graph.degree(p.id) == 0:
                continue  # reported as orphan, not declared
            if p.kind == "experiment":
                short = re.sub(r"\(.*", "", p.label)
                p.name = uniq(_slug(short)[:18] or "expt")
            elif p.kind == "coupler":
                p.name = uniq("dc")
            elif p.kind == "amplifier" and self.graph.degree(p.id) > 2:
                p.name = uniq(_slug(p.label) or "amp")
            elif p.kind == "isolator" and self.graph.degree(p.id) > 2:
                p.name = uniq(_slug(p.label) or "iso")

    def _served_line(self, arrows):
        for a in arrows:
            for _u, v, d in self.graph.edges(a.id, data=True):
                other = self.parts.get(v)
                if other is not None and other.kind in ("anchor", "line"):
                    return other.line
        return ""

    # ----- 5. walk the graph into chains ---------------------------------

    def chains(self):
        g = self.graph
        emitted = set()
        chains = []

        def edge_key(u, v, k):
            return (u, v, k) if str(u) <= str(v) else (v, u, k)

        def unused_edges(n):
            out = []
            for u, v, k, d in g.edges(n, keys=True, data=True):
                if edge_key(u, v, k) not in emitted:
                    out.append((u, v, k, d))
            return out

        def port_tok(p, port):
            if p.kind in ("inline",):
                return self._inline_tok(p)
            if p.kind == "term":
                return "term"
            if p.name:
                return "%s.%s" % (p.name, port)
            return self._inline_tok(p)

        def is_multi(p):
            return p.kind in ("circulator", "coupler", "experiment") or bool(p.name)

        def follow(start_tok, n, u, v, k, d):
            toks = [start_tok]
            while True:
                emitted.add(edge_key(u, v, k))
                other = v if u == n else u
                if d.get("cable"):
                    toks.append("cable(%s)" % d["cable"])
                p = self.parts[other]
                port = d["ports"][other]
                if p.kind in ("line", "anchor"):
                    toks.append(p.line)
                    return toks, p
                toks.append(port_tok(p, port))
                if is_multi(p) or p.kind == "term":
                    return toks, p
                nxt = unused_edges(other)
                if not nxt:
                    return toks, p
                if len(nxt) > 1:
                    self.note("'%s' has %d wires but is a two-port part; only one path kept"
                              % (p.label, len(nxt) + 1))
                n = other
                u, v, k, d = nxt[0]

        def run_from(node):
            p0 = self.parts[node]
            for u, v, k, d in unused_edges(node):
                if edge_key(u, v, k) in emitted:
                    continue
                if p0.kind in ("line", "anchor"):
                    first = p0.line
                    line_kind = _line_kind(first)
                else:
                    first = port_tok(p0, d["ports"][node])
                    line_kind = ""
                toks, end = follow(first, node, u, v, k, d)
                # fridge lines read left to right: In first, Out last
                if line_kind == "out":
                    toks.reverse()
                elif not line_kind and end.kind in ("line", "anchor") \
                        and _line_kind(end.line) == "in":
                    toks.reverse()
                chains.append(toks)
                if end.kind not in ("line", "anchor", "term"):
                    pending.append(end.id)

        pending = []

        def drain():
            while pending:
                run_from(pending.pop(0))

        # input lines first, in fridge order, then the outputs, then leftovers
        ordered = [p.id for p in self.parts.values() if p.kind in ("line", "anchor")]
        ordered.sort(key=lambda pid: (_line_kind(self.parts[pid].line) != "in",
                                      _line_order(self.parts[pid].line)))
        for pid in ordered:
            run_from(pid)
            drain()
        for pid in sorted(self.parts, key=lambda i: (self.parts[i].y, self.parts[i].x)):
            if is_multi(self.parts[pid]):
                run_from(pid)
                drain()
        # anything left (two-port islands)
        for pid in sorted(self.parts, key=lambda i: (self.parts[i].y, self.parts[i].x)):
            if self.parts[pid].kind != "term" and unused_edges(pid):
                run_from(pid)
                drain()
        return chains

    def _inline_tok(self, p):
        tok = p.token
        if p.kind == "amplifier":
            tok = "amp(%s)" % _sanitize_filter_label(p.label)
        elif p.kind == "isolator":
            tok = "iso"
        elif p.kind == "coupler":
            tok = "coupler"
        if p.plate and "@" not in tok:
            tok = "%s@%s" % (tok, p.plate)
        return tok

    # ----- 6. report -----------------------------------------------------

    def report_orphans(self):
        for p in sorted(self.parts.values(), key=lambda p: (p.y, p.x)):
            if p.kind in ("line", "anchor", "dc", "term"):
                continue
            if self.graph.degree(p.id) == 0:
                self.note("'%s' at (%.1f, %.1f) cm has no wires in the drawing"
                          % (p.label, p.x, p.y))
            elif p.kind == "circulator" and self.graph.degree(p.id) == 1:
                self.note("circulator '%s' has only one wire in the drawing" % p.label)


def _multi_source_dist(g, sources):
    """Distance counted in devices: passing through an attenuator, filter or
    other inline part is free, every circulator / amplifier / experiment
    costs one.  (Plain hop counts would rank a line with a long attenuator
    chain as 'farther upstream' than one with a short chain.)"""
    from collections import deque

    def cost(n):
        p = g.nodes[n].get("part")
        return 0 if p is not None and p.kind == "inline" else 1

    dist = {}
    dq = deque()
    for s in sources:
        if s in g:
            dist[s] = 0
            dq.append(s)
    while dq:
        n = dq.popleft()
        for nbr in g.neighbors(n):
            d = dist[n] + cost(nbr)
            if d < dist.get(nbr, 10 ** 9):
                dist[nbr] = d
                if cost(nbr) == 0:
                    dq.appendleft(nbr)
                else:
                    dq.append(nbr)
    return dist


def _port_name(text):
    # port names must survive "name.port": no spaces or dots
    t = re.sub(r"[^A-Za-z0-9_+-]+", "_", _norm(text)).strip("_")
    return t or "port"


def _outside_dist(shape, x, y):
    """0 inside the box, else distance to its edge."""
    dx = max(shape.x0 - x, 0.0, x - shape.x1)
    dy = max(shape.y0 - y, 0.0, y - shape.y1)
    return math.hypot(dx, dy)


def _side_of(shape, x, y):
    # which edge of the box a point is nearest (outside or inside)
    dx = (x - shape.x) / max(shape.w / 2.0, 0.3)
    dy = (y - shape.y) / max(shape.h / 2.0, 0.3)
    if abs(dx) >= abs(dy):
        return "E" if dx > 0 else "W"
    return "S" if dy > 0 else "N"


def _dedupe_shapes(shapes):
    kept = []
    for s in sorted(shapes, key=lambda s: (s.y, s.x)):
        if any(abs(s.x - k.x) < 0.35 and abs(s.y - k.y) < 0.35
               and _norm(s.text) == _norm(k.text) for k in kept):
            continue
        kept.append(s)
    return kept


def _line_order(name):
    pre, idx, _k = _parse_line_index(name)
    return (pre, idx or 0)


def emit_wiring(net, chains):
    devices = []
    for p in sorted(net.parts.values(), key=lambda p: (p.kind != "circulator", p.y, p.x)):
        if not p.name:
            continue
        if p.kind == "circulator":
            devices.append((p.name, "circulator", p.label, {}))
        elif p.kind == "coupler":
            devices.append((p.name, "coupler", "DC", {}))
        elif p.kind == "experiment":
            ports = _experiment_ports(net, p)
            devices.append((p.name, "experiment", p.label, ports))
        elif p.kind == "amplifier":
            devices.append((p.name, "device", p.label, _generic_ports(net, p)))
        elif p.kind == "isolator":
            devices.append((p.name, "device", p.label, _generic_ports(net, p)))
    if not devices and not chains:
        return ""
    lines = []
    lines.append("# MXC-side wiring read from the drawing: every box is a part and")
    lines.append("# every connector / touching box is a wire. Circulator stacks between")
    lines.append("# the HEMTs and the Eccosorb filters are here too, so edit or split")
    lines.append("# this file into per-experiment owner files.")
    lines.append("")
    lines.append("owner: Extracted")
    lines.append("")
    for name, kind, label, ports in devices:
        lines.append("[device %s]" % name)
        lines.append("type: %s" % kind)
        if label and label != name:
            lines.append("label: %s" % label.replace("\n", " ")[:40])
        if ports:
            lines.append("ports: %s" % ", ".join(ports))
        lines.append("")
    lines.append("[wiring]")
    terms = [c for c in chains if c[-1] == "term"]
    rest = [c for c in chains if c[-1] != "term"]
    for c in rest + terms:
        if len(c) >= 2:
            lines.append(_wrap_chain(c, indent=0))
    lines.append("")
    return "\n".join(lines)


def _experiment_ports(net, p):
    out = []
    seen = set()
    for _u, v, d in net.graph.edges(p.id, data=True):
        port = d["ports"][p.id]
        if port in seen:
            continue
        seen.add(port)
        side = None
        for tx, ty, name in p.tags:
            if name == port:
                side = _side_of(p.shape, tx, ty)
                break
        if side is None:
            side = {"in": "W", "out": "E", "top": "N", "bottom": "S"}.get(
                re.sub(r"\d+$", "", port))
        out.append("%s:%s" % (port, side) if side else port)
    return out


def _generic_ports(net, p):
    out = []
    for _u, v, d in net.graph.edges(p.id, data=True):
        port = d["ports"][p.id]
        if port not in out:
            out.append(port)
    return out


def extract(odg_path):
    """Returns (common_txt, extracted_txt, notes)."""
    dr = load_drawing(odg_path)
    plates = find_plates(dr.shapes)
    labels = find_line_labels(dr.shapes, plates)
    hemt_map = map_hemts_to_lines(dr, labels)
    fridge_lines = extract_lines(dr, plates, labels, hemt_map)
    notes = []
    net = Net(dr, plates, labels, hemt_map, notes)
    net.build_parts()
    net.build_abutments()
    net.build_wires()
    net.build_hangs()
    net.orient_circulators()
    net.name_devices()
    chains = net.chains()
    net.report_orphans()
    common = emit_common(odg_path, plates, fridge_lines)
    wiring = emit_wiring(net, chains)
    return common, wiring, notes


def describe(odg_path):
    """Human-readable dump of what the drawing contains."""
    dr = load_drawing(odg_path)
    plates = find_plates(dr.shapes)
    labels = find_line_labels(dr.shapes, plates)
    hemt_map = map_hemts_to_lines(dr, labels)
    fridge_lines = extract_lines(dr, plates, labels, hemt_map)
    out = []
    out.append("Plates (x_cm, name):")
    for x, name in plates:
        out.append("  %7.2f  %s" % (x, name))
    out.append("")
    out.append("Fridge lines:")
    for name, group, _d, parts, hint in fridge_lines:
        out.append("  %s [%s] %s" % (name, group, " -> ".join(parts)))
    common, wiring, notes = extract(odg_path)
    if wiring:
        out.append("")
        out.append("MXC-side wiring:")
        for ln in wiring.splitlines():
            out.append("  " + ln)
    if notes:
        out.append("")
        out.append("Notes:")
        for n in notes:
            out.append("  - " + n)
    return "\n".join(out) + "\n"
