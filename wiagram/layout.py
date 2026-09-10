"""Custom layout engine.

Zones (matching the group's existing hand-drawn diagrams):
- Line zone (left): temperature plates as vertical bars, every fridge line on
  its own horizontal track, components anchored to plates.
- Channel region: vertical routing lanes connecting line ends to user hardware.
- Experiment zone (right of the last plate): one horizontal band per owner,
  each [wiring] chain on its own track; linear runs stay in-line.
"""

from . import symbols
from .model import Design

# Geometry constants (px)
TOP = 96
LEFT_LABEL = 84            # room for line-name labels
PLATE_X0 = 170
PLATE_DX = 118
LINE_PITCH = 20            # vertical pitch of fridge-line tracks
GROUP_GAP = 30             # gap between line groups (inputs/outputs/DC)
EXPT_PITCH = 52            # vertical pitch of tracks inside an owner band
BAND_PAD = 26
BAND_GAP = 26
CH_GAP = 10                # spacing of vertical routing channels
STUB = 9
COMP_GAP = 24              # horizontal spacing between chain components
HANG_GAP = 34              # vertical spacing for hanging (S-port) chains

BAND_FILLS = ["#fdf6ec", "#eef4fb", "#eff8ef", "#fbeff4", "#f4effb", "#f7f7e8"]


class LayoutData:
    def __init__(self):
        self.width = 0
        self.height = 0
        self.plate_bars = []    # (x, y0, y1, name, temp)
        self.line_labels = []   # (x, y, text)
        self.bands = []         # (owner, x0, y0, x1, y1, fill)
        self.x_line_end = 0
        self.x_expt = 0


def _find_connection(design, c0, c1):
    """First connection joining components c0 and c1 (either direction)."""
    for conn in design.connections:
        if conn.a[0] == c0 and conn.b[0] == c1:
            return conn, conn.a[1]
        if conn.b[0] == c0 and conn.a[0] == c1:
            return conn, conn.b[1]
    return None, None


def _side_of(comp, port):
    return symbols.port_pos(comp, port)[2]


def layout(design: Design) -> LayoutData:
    ld = LayoutData()
    comps = design.components

    # ---------------- x axis ----------------
    plate_x = {}
    for i, (pname, _temp) in enumerate(design.plates):
        plate_x[pname] = PLATE_X0 + i * PLATE_DX
    n_plates = len(design.plates)
    x_last_plate = PLATE_X0 + (n_plates - 1) * PLATE_DX if n_plates else PLATE_X0
    ld.x_line_end = x_last_plate + 64
    ld.x_expt = ld.x_line_end + 130

    # ---------------- line tracks ----------------
    y = TOP
    prev_group = None
    line_y = {}
    for line in design.lines.values():
        if prev_group is not None and line.group != prev_group:
            y += GROUP_GAP
        prev_group = line.group
        line_y[line.name] = y
        ld.line_labels.append((LEFT_LABEL - 6, y + 3, line.name))
        y += LINE_PITCH
    line_zone_bottom = y

    # place components of each line
    for line in design.lines.values():
        ty = line_y[line.name]
        start = comps[line.start_id]
        end = comps[line.end_id]
        start.x, start.y = LEFT_LABEL + 4, ty
        end.x, end.y = ld.x_line_end, ty
        for c in (start, end):
            c.placed, c.zone = True, "line"
        cur_x = start.x
        for cid in line.chain.comps:
            comp = comps[cid]
            if comp.kind == "node" or comp.placed:
                continue
            w, h = symbols.size(comp)
            if comp.plate and comp.plate in plate_x:
                if comp.kind == "bulkhead":
                    cx = plate_x[comp.plate]
                else:
                    cx = plate_x[comp.plate] + 14 + w / 2.0
                cx = max(cx, cur_x + 8 + w / 2.0)
            else:
                cx = cur_x + COMP_GAP + w / 2.0
            comp.x, comp.y = cx, ty
            comp.placed, comp.zone = True, "line"
            cur_x = cx + w / 2.0

    # ---------------- owner bands (pass A: relative placement) ----------------
    band_info = {}    # owner -> dict(tracks=[{cur_x, right_id}], comps=[], max_y, max_x)

    def band(owner):
        if owner not in band_info:
            band_info[owner] = {"tracks": [], "comps": [], "max_y": 0.0,
                                "max_x": ld.x_expt}
        return band_info[owner]

    def new_track(b):
        b["tracks"].append({"cur_x": ld.x_expt, "right_id": None})
        return len(b["tracks"]) - 1

    def place_on_track(b, ti, comp, extra_gap=0.0):
        tr = b["tracks"][ti]
        w, h = symbols.size(comp)
        cx = tr["cur_x"] + COMP_GAP + extra_gap + w / 2.0
        comp.x = cx
        comp.y = ti * EXPT_PITCH          # relative; band offset added later
        comp.placed, comp.zone = True, "band"
        tr["cur_x"] = cx + w / 2.0
        tr["right_id"] = comp.id
        b["comps"].append(comp)
        b["max_y"] = max(b["max_y"], comp.y + h / 2.0 + 24)
        b["max_x"] = max(b["max_x"], cx + w / 2.0)

    for chain in design.chains:
        if chain.line:
            continue
        new_ids = [cid for cid in chain.comps if not comps[cid].placed]
        if not new_ids:
            continue                       # pure routing chain
        b = band(chain.owner)
        first = comps[chain.comps[0]]

        mode, ti, hang_anchor = "track", None, None
        if first.placed and first.zone == "band":
            _conn, port = _find_connection(design, chain.comps[0], chain.comps[1]) \
                if len(chain.comps) > 1 else (None, None)
            side = _side_of(first, port) if port else "E"
            on_track = None
            for k, tr in enumerate(b["tracks"]):
                if tr["right_id"] == first.id:
                    on_track = k
            if side == "E" and on_track is not None:
                ti = on_track                       # continue in-line
            elif side in ("S", "N"):
                mode, hang_anchor = "hang", symbols.port_pos(first, port)
            else:
                ti = new_track(b)
        elif first.placed and first.zone == "line":
            ti = new_track(b)                       # chain starts at a fridge line
        else:
            ti = new_track(b)

        if mode == "hang":
            hx, hy = hang_anchor[0], hang_anchor[1]
            j = 0
            for cid in chain.comps:
                comp = comps[cid]
                if comp.placed:
                    continue
                w, h = symbols.size(comp)
                hy = hy + HANG_GAP * (0.7 if j == 0 else 1.0) + h / 2.0
                comp.x, comp.y = hx, hy
                comp.orient = "v"
                comp.placed, comp.zone = True, "band"
                hy += h / 2.0
                b["comps"].append(comp)
                b["max_y"] = max(b["max_y"], comp.y + h + 18)
                b["max_x"] = max(b["max_x"], hx + w / 2.0 + 10)
                j += 1
        else:
            prev_cid = None
            for cid in chain.comps:
                comp = comps[cid]
                if comp.placed:
                    prev_cid = cid
                    continue
                extra = 0.0
                if prev_cid is not None:
                    conn, _p = _find_connection(design, prev_cid, cid)
                    if conn and conn.cable_id:
                        cab = comps[conn.cable_id]
                        if cab.serial or cab.label or cab.note:
                            extra = 30.0   # room for the cable label
                place_on_track(b, ti, comp, extra_gap=extra)
                prev_cid = cid

    # ---------------- owner bands (pass B: absolute y) ----------------
    by = TOP
    for owner in design.owners:
        if owner not in band_info or not band_info[owner]["comps"]:
            continue
        b = band_info[owner]
        y0 = by
        for comp in b["comps"]:
            comp.y += y0 + BAND_PAD
        height = b["max_y"] + 2 * BAND_PAD
        fill = BAND_FILLS[len(ld.bands) % len(BAND_FILLS)]
        ld.bands.append((owner, ld.x_expt - 16, y0, b["max_x"] + 18,
                         y0 + height, fill))
        by = y0 + height + BAND_GAP

    # ---------------- plate bars & canvas ----------------
    bar_top, bar_bot = TOP - 16, line_zone_bottom + 8
    for pname, temp in design.plates:
        ld.plate_bars.append((plate_x[pname], bar_top, bar_bot, pname, temp))

    max_x = ld.x_expt + 40
    for b in band_info.values():
        max_x = max(max_x, b["max_x"] + 40)
    ld.width = max_x + 30
    ld.height = max(line_zone_bottom, by) + 20

    # ---------------- routing ----------------
    _route_all(design, ld, line_y)
    return ld


# ---------------------------------------------------------------------------
# Orthogonal routing
# ---------------------------------------------------------------------------

_DIR = {"W": (-1, 0), "E": (1, 0), "N": (0, -1), "S": (0, 1)}


def _stub(p):
    x, y, side = p
    dx, dy = _DIR[side]
    return (x + dx * STUB, y + dy * STUB)


def _dedupe(pts):
    """Drop repeated and collinear points."""
    out = []
    for p in pts:
        if out and abs(out[-1][0] - p[0]) < 0.01 and abs(out[-1][1] - p[1]) < 0.01:
            continue
        out.append(p)
    if len(out) < 3:
        return out
    clean = [out[0]]
    for i in range(1, len(out) - 1):
        a, b_, c = clean[-1], out[i], out[i + 1]
        if (abs(a[0] - b_[0]) < 0.01 and abs(b_[0] - c[0]) < 0.01) or \
           (abs(a[1] - b_[1]) < 0.01 and abs(b_[1] - c[1]) < 0.01):
            continue
        clean.append(b_)
    clean.append(out[-1])
    return clean


JOG = 24   # vertical jog when a wire must double back past its own component


def _route(pa, pb, via_x=None):
    """Orthogonal polyline from port pa to port pb ((x, y, side) each)."""
    a, b = (pa[0], pa[1]), (pb[0], pb[1])

    # Directly facing ports: straight segment, no stubs (avoids stub overshoot
    # when components sit close together).
    if via_x is None and abs(a[1] - b[1]) < 0.01:
        if (pa[2] == "E" and pb[2] == "W" and b[0] >= a[0] - 0.01) or \
           (pa[2] == "W" and pb[2] == "E" and a[0] >= b[0] - 0.01):
            return [a, b]
    if via_x is None and abs(a[0] - b[0]) < 0.01:
        if (pa[2] == "S" and pb[2] == "N" and b[1] >= a[1] - 0.01) or \
           (pa[2] == "N" and pb[2] == "S" and a[1] >= b[1] - 0.01):
            return [a, b]

    sa, sb = _stub(pa), _stub(pb)
    pts_a, pts_b = [a, sa], [b, sb]
    ea, eb = sa, sb

    # If a stub points away from where the wire must travel, jog vertically
    # first so the wire doesn't run back across its own component.
    tx_a = via_x if via_x is not None else sb[0]
    if (pa[2] == "E" and tx_a < sa[0] - 0.01) or \
       (pa[2] == "W" and tx_a > sa[0] + 0.01):
        lane = sa[1] + (JOG if sb[1] > sa[1] else -JOG)
        ea = (sa[0], lane)
        pts_a.append(ea)
    tx_b = via_x if via_x is not None else sa[0]
    if (pb[2] == "E" and tx_b < sb[0] - 0.01) or \
       (pb[2] == "W" and tx_b > sb[0] + 0.01):
        lane = sb[1] + (JOG if sa[1] > sb[1] else -JOG)
        eb = (sb[0], lane)
        pts_b.append(eb)

    if via_x is not None:
        mid = [(via_x, ea[1]), (via_x, eb[1])]
    elif abs(ea[1] - eb[1]) < 0.01 or abs(ea[0] - eb[0]) < 0.01:
        mid = []
    elif pb[2] in ("N", "S") or len(pts_b) > 2:
        mid = [(eb[0], ea[1])]        # horizontal first, drop into b
    elif pa[2] in ("N", "S") or len(pts_a) > 2:
        mid = [(ea[0], eb[1])]        # vertical first, run into b
    else:
        vx = (ea[0] + eb[0]) / 2.0
        mid = [(vx, ea[1]), (vx, eb[1])]
    return _dedupe(pts_a + mid + list(reversed(pts_b)))


def _route_all(design, ld, line_y):
    comps = design.components

    def pp(ref):
        comp = comps[ref[0]]
        return comp, symbols.port_pos(comp, ref[1])

    # channel assignment for connections crossing line zone <-> band zone
    crossers = []
    for conn in design.connections:
        ca, pa = pp(conn.a)
        cb, pb = pp(conn.b)
        a_line = ca.zone == "line"
        b_line = cb.zone == "line"
        if a_line != b_line and (ca.kind == "node" or cb.kind == "node"):
            node_y = pa[1] if a_line else pb[1]
            crossers.append((node_y, conn))
    crossers.sort(key=lambda t: (t[0], t[1].src.line))
    channel_x = {}
    for k, (_yy, conn) in enumerate(crossers):
        channel_x[id(conn)] = ld.x_line_end + 16 + k * CH_GAP

    for conn in design.connections:
        ca, pa = pp(conn.a)
        cb, pb = pp(conn.b)
        via = channel_x.get(id(conn))
        # line-end nodes have zero size; aim the stub toward the channel
        if ca.kind == "node" and ca.zone == "line":
            pa = (pa[0], pa[1], "E")
        if cb.kind == "node" and cb.zone == "line":
            pb = (pb[0], pb[1], "E")
        conn.points = _route(pa, pb, via_x=via)
