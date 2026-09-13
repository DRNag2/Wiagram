# layout: where everything goes on the page
#
# left side = fridge lines (horizontal tracks + plate bars)
# middle = channels connecting line ends to people's stuff
# right side = one band per owner for their [wiring] chains

from . import symbols
from .model import Design

# all the magic numbers (in px)
TOP = 96
LEFT_LABEL = 84       # space for the "In 1" labels
PLATE_X0 = 170
PLATE_DX = 118
LINE_PITCH = 20       # how far apart the fridge line tracks are
GROUP_GAP = 30        # gap between input/output/DC groups
EXPT_PITCH = 52       # track spacing inside an owner band
BAND_PAD = 26
BAND_GAP = 26
CH_GAP = 10           # spacing between vertical routing channels
STUB = 9
COMP_GAP = 24         # space between components in a chain
HANG_GAP = 34         # space when hanging stuff off a south port

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
    # first connection between c0 and c1 (either way)
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

    # --- x positions for plates ---
    plate_x = {}
    for i, (pname, _temp) in enumerate(design.plates):
        plate_x[pname] = PLATE_X0 + i * PLATE_DX
    n_plates = len(design.plates)
    if n_plates:
        x_last_plate = PLATE_X0 + (n_plates - 1) * PLATE_DX
    else:
        x_last_plate = PLATE_X0
    ld.x_line_end = x_last_plate + 64
    ld.x_expt = ld.x_line_end + 130

    # --- assign a y to every fridge line ---
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

    # place the components that live on each line
    for line in design.lines.values():
        ty = line_y[line.name]
        start = comps[line.start_id]
        end = comps[line.end_id]
        start.x = LEFT_LABEL + 4
        start.y = ty
        end.x = ld.x_line_end
        end.y = ty
        for c in (start, end):
            c.placed = True
            c.zone = "line"
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
                # don't go backwards
                cx = max(cx, cur_x + 8 + w / 2.0)
            else:
                cx = cur_x + COMP_GAP + w / 2.0
            comp.x = cx
            comp.y = ty
            comp.placed = True
            comp.zone = "line"
            cur_x = cx + w / 2.0

    # --- owner bands, pass 1: relative placement ---
    band_info = {}  # owner -> {tracks, comps, max_y, max_x}

    def band(owner):
        if owner not in band_info:
            band_info[owner] = {
                "tracks": [],
                "comps": [],
                "max_y": 0.0,
                "max_x": ld.x_expt,
            }
        return band_info[owner]

    def new_track(b):
        b["tracks"].append({"cur_x": ld.x_expt, "right_id": None})
        return len(b["tracks"]) - 1

    def place_on_track(b, ti, comp, extra_gap=0.0):
        tr = b["tracks"][ti]
        w, h = symbols.size(comp)
        cx = tr["cur_x"] + COMP_GAP + extra_gap + w / 2.0
        comp.x = cx
        comp.y = ti * EXPT_PITCH  # relative for now, offset later
        comp.placed = True
        comp.zone = "band"
        tr["cur_x"] = cx + w / 2.0
        tr["right_id"] = comp.id
        b["comps"].append(comp)
        b["max_y"] = max(b["max_y"], comp.y + h / 2.0 + 24)
        b["max_x"] = max(b["max_x"], cx + w / 2.0)

    for chain in design.chains:
        if chain.line:
            continue  # already placed with the line
        new_ids = []
        for cid in chain.comps:
            if not comps[cid].placed:
                new_ids.append(cid)
        if not new_ids:
            continue  # just routing, nothing new to place
        b = band(chain.owner)
        first = comps[chain.comps[0]]

        mode = "track"
        ti = None
        hang_anchor = None

        if first.placed and first.zone == "band":
            if len(chain.comps) > 1:
                _conn, port = _find_connection(design, chain.comps[0], chain.comps[1])
            else:
                _conn, port = None, None
            if port:
                side = _side_of(first, port)
            else:
                side = "E"
            on_track = None
            for k, tr in enumerate(b["tracks"]):
                if tr["right_id"] == first.id:
                    on_track = k
            if side == "E" and on_track is not None:
                ti = on_track  # keep going on the same row
            elif side in ("S", "N"):
                mode = "hang"
                hang_anchor = symbols.port_pos(first, port)
            else:
                ti = new_track(b)
        elif first.placed and first.zone == "line":
            ti = new_track(b)  # coming off a fridge line
        else:
            ti = new_track(b)

        if mode == "hang":
            hx = hang_anchor[0]
            hy = hang_anchor[1]
            j = 0
            for cid in chain.comps:
                comp = comps[cid]
                if comp.placed:
                    continue
                w, h = symbols.size(comp)
                if j == 0:
                    hy = hy + HANG_GAP * 0.7 + h / 2.0
                else:
                    hy = hy + HANG_GAP + h / 2.0
                comp.x = hx
                comp.y = hy
                comp.orient = "v"
                comp.placed = True
                comp.zone = "band"
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
                            extra = 30.0  # leave room for the cable label
                place_on_track(b, ti, comp, extra_gap=extra)
                prev_cid = cid

    # --- owner bands, pass 2: absolute y ---
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

    # plate bars + canvas size
    bar_top = TOP - 16
    bar_bot = line_zone_bottom + 8
    for pname, temp in design.plates:
        ld.plate_bars.append((plate_x[pname], bar_top, bar_bot, pname, temp))

    max_x = ld.x_expt + 40
    for b in band_info.values():
        max_x = max(max_x, b["max_x"] + 40)
    ld.width = max_x + 30
    ld.height = max(line_zone_bottom, by) + 20

    # wire routing
    _route_all(design, ld, line_y)
    return ld


# orthogonal routing (manhattan paths between ports)

_DIR = {"W": (-1, 0), "E": (1, 0), "N": (0, -1), "S": (0, 1)}


def _stub(p):
    x, y, side = p
    dx, dy = _DIR[side]
    return (x + dx * STUB, y + dy * STUB)


def _dedupe(pts):
    # drop duplicates and points that sit on a straight line
    out = []
    for p in pts:
        if out and abs(out[-1][0] - p[0]) < 0.01 and abs(out[-1][1] - p[1]) < 0.01:
            continue
        out.append(p)
    if len(out) < 3:
        return out
    clean = [out[0]]
    for i in range(1, len(out) - 1):
        a = clean[-1]
        b_ = out[i]
        c = out[i + 1]
        colinear_x = abs(a[0] - b_[0]) < 0.01 and abs(b_[0] - c[0]) < 0.01
        colinear_y = abs(a[1] - b_[1]) < 0.01 and abs(b_[1] - c[1]) < 0.01
        if colinear_x or colinear_y:
            continue
        clean.append(b_)
    clean.append(out[-1])
    return clean


JOG = 24  # how far to jog when the wire has to double back


def _route(pa, pb, via_x=None):
    # draw an orthogonal path from port pa to port pb
    # each port is (x, y, side)
    a = (pa[0], pa[1])
    b = (pb[0], pb[1])

    # facing each other on the same row/col -> just a straight line
    # (stubs would overshoot when things are close)
    if via_x is None and abs(a[1] - b[1]) < 0.01:
        if (pa[2] == "E" and pb[2] == "W" and b[0] >= a[0] - 0.01) or \
           (pa[2] == "W" and pb[2] == "E" and a[0] >= b[0] - 0.01):
            return [a, b]
    if via_x is None and abs(a[0] - b[0]) < 0.01:
        if (pa[2] == "S" and pb[2] == "N" and b[1] >= a[1] - 0.01) or \
           (pa[2] == "N" and pb[2] == "S" and a[1] >= b[1] - 0.01):
            return [a, b]

    sa = _stub(pa)
    sb = _stub(pb)
    pts_a = [a, sa]
    pts_b = [b, sb]
    ea = sa
    eb = sb

    # if the stub points the wrong way, jog vertically first so we don't
    # run the wire back through the component
    if via_x is not None:
        tx_a = via_x
    else:
        tx_a = sb[0]
    if (pa[2] == "E" and tx_a < sa[0] - 0.01) or \
       (pa[2] == "W" and tx_a > sa[0] + 0.01):
        if sb[1] > sa[1]:
            lane = sa[1] + JOG
        else:
            lane = sa[1] - JOG
        ea = (sa[0], lane)
        pts_a.append(ea)

    if via_x is not None:
        tx_b = via_x
    else:
        tx_b = sa[0]
    if (pb[2] == "E" and tx_b < sb[0] - 0.01) or \
       (pb[2] == "W" and tx_b > sb[0] + 0.01):
        if sa[1] > sb[1]:
            lane = sb[1] + JOG
        else:
            lane = sb[1] - JOG
        eb = (sb[0], lane)
        pts_b.append(eb)

    if via_x is not None:
        mid = [(via_x, ea[1]), (via_x, eb[1])]
    elif abs(ea[1] - eb[1]) < 0.01 or abs(ea[0] - eb[0]) < 0.01:
        mid = []
    elif pb[2] in ("N", "S") or len(pts_b) > 2:
        mid = [(eb[0], ea[1])]  # go horizontal then drop into b
    elif pa[2] in ("N", "S") or len(pts_a) > 2:
        mid = [(ea[0], eb[1])]  # go vertical then across
    else:
        vx = (ea[0] + eb[0]) / 2.0
        mid = [(vx, ea[1]), (vx, eb[1])]
    return _dedupe(pts_a + mid + list(reversed(pts_b)))


def _route_all(design, ld, line_y):
    comps = design.components

    def pp(ref):
        comp = comps[ref[0]]
        return comp, symbols.port_pos(comp, ref[1])

    # connections that cross from line zone to band zone get a channel
    crossers = []
    for conn in design.connections:
        ca, pa = pp(conn.a)
        cb, pb = pp(conn.b)
        a_line = ca.zone == "line"
        b_line = cb.zone == "line"
        if a_line != b_line and (ca.kind == "node" or cb.kind == "node"):
            if a_line:
                node_y = pa[1]
            else:
                node_y = pb[1]
            crossers.append((node_y, conn))
    crossers.sort(key=lambda t: (t[0], t[1].src.line))
    channel_x = {}
    for k, (_yy, conn) in enumerate(crossers):
        channel_x[id(conn)] = ld.x_line_end + 16 + k * CH_GAP

    for conn in design.connections:
        ca, pa = pp(conn.a)
        cb, pb = pp(conn.b)
        via = channel_x.get(id(conn))
        # line-end nodes are zero-size so aim the stub toward the channel
        if ca.kind == "node" and ca.zone == "line":
            pa = (pa[0], pa[1], "E")
        if cb.kind == "node" and cb.zone == "line":
            pb = (pb[0], pb[1], "E")
        conn.points = _route(pa, pb, via_x=via)
