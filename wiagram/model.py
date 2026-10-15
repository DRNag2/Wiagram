# data structures for the wiring diagram stuff
#
# connectivity lives in a networkx MultiGraph on Design.graph:
#   node  = component id (parts, experiments, line start/end nodes)
#   edge  = one Connection (port-to-port); cables ride along as edge data
# design.connections keeps the same Connection objects in file order for
# routing/rendering; the graph is the index for "what is this hooked to".

from dataclasses import dataclass, field
from typing import Optional

import networkx as nx


class ParseError(Exception):
    # just so we can say which file/line blew up
    def __init__(self, msg, file=None, line=None):
        self.msg = msg
        self.file = file
        self.line = line
        super().__init__(msg)

    def __str__(self):
        if self.file:
            if self.line is None:
                loc = self.file
            else:
                loc = "%s:%d" % (self.file, self.line)
            return "%s: %s" % (loc, self.msg)
        return self.msg


class ParseErrors(ParseError):
    # several ParseErrors at once (one per bad file / line)
    def __init__(self, errors):
        self.errors = list(errors)
        first = self.errors[0]
        super().__init__(first.msg, first.file, first.line)

    def __str__(self):
        return "\n".join(str(e) for e in self.errors)


@dataclass
class Src:
    file: str = ""
    line: int = 0


# map whatever people type to the real kind name
KIND_ALIASES = {
    "attenuator": "attenuator", "atten": "attenuator",
    "circulator": "circulator",
    "isolator": "isolator", "iso": "isolator",
    "amplifier": "amplifier", "amp": "amplifier", "hemt": "amplifier",
    "filter": "filter",
    "coupler": "coupler", "directional coupler": "coupler",
    "directional_coupler": "coupler",
    "term": "term", "termination": "term", "load": "term",
    "connector": "connector",
    "bulkhead": "bulkhead",
    "experiment": "experiment", "sample": "experiment",
    "device": "device", "box": "device",
    "biastee": "biastee", "bias tee": "biastee", "bias-tee": "biastee",
    "coil": "coil", "magnet": "coil",
    "switch": "switch",
    "node": "node",
    "cable": "cable",
}

# some kinds have fixed ports. None means we make them up as we go
FIXED_PORTS = {
    "circulator": ["1", "2", "3"],
    "coupler": ["in", "out", "cpl", "iso"],
    "biastee": ["rf", "dc", "out"],
    "term": ["p"],
    "node": ["a", "b"],
}

TWO_PORT_KINDS = {
    "attenuator", "isolator", "amplifier", "filter", "connector",
    "bulkhead", "cable", "coil", "switch",
}

DYNAMIC_PORT_KINDS = {"experiment", "device"}

# default in/out when someone puts a multiport thing in the middle of a chain
# without saying which ports
AUTO_THROUGH = {
    "circulator": ("1", "2"),
    "isolator": ("in", "out"),
    "coupler": ("in", "out"),
    "biastee": ("rf", "out"),
}


@dataclass
class Component:
    id: str
    kind: str
    name: str = ""
    label: str = ""
    serial: str = ""
    plate: str = ""
    owner: str = ""
    color: str = ""
    note: str = ""
    port_list: Optional[list] = None
    port_sides: dict = field(default_factory=dict)  # port -> W/E/N/S
    src: Src = field(default_factory=Src)
    # layout fills these in later
    x: float = 0.0
    y: float = 0.0
    orient: str = "h"   # h = left to right, v = going down
    flip: bool = False  # True if signal goes right to left (output lines)
    placed: bool = False
    zone: str = ""      # "line" or "band"

    def ports(self):
        if self.kind in FIXED_PORTS:
            return list(FIXED_PORTS[self.kind])
        if self.kind in DYNAMIC_PORT_KINDS:
            return list(self.port_list or [])
        return ["in", "out"]

    def display_label(self):
        # prefer the label they set, otherwise the name
        return self.label or self.name


@dataclass
class Connection:
    a: tuple   # (component_id, port)
    b: tuple
    cable_id: Optional[str] = None
    owner: str = ""
    src: Src = field(default_factory=Src)
    points: list = field(default_factory=list)  # layout puts the polyline here


@dataclass
class Chain:
    # one -> chain from the text file. layout uses the order of comps
    owner: str
    comps: list = field(default_factory=list)  # component ids in order (no cables)
    line: str = ""   # set if this came from a line template
    src: Src = field(default_factory=Src)


@dataclass
class Line:
    name: str
    template: str
    group: str = ""
    chain: Optional[Chain] = None
    start_id: str = ""
    end_id: str = ""
    src: Src = field(default_factory=Src)


class Design:
    def __init__(self):
        self.meta = {}
        self.plates = []         # list of (name, temp_label)
        self.components = {}     # id -> Component, insertion order
        self.connections = []
        self.chains = []
        self.lines = {}          # name -> Line
        self.owners = []         # owner names in file order
        self.warnings = []
        self.graph = nx.MultiGraph()

    def add_component(self, comp):
        if comp.id in self.components:
            raise ParseError("duplicate component id '%s'" % comp.id,
                             comp.src.file, comp.src.line)
        self.components[comp.id] = comp
        if comp.kind != "cable":
            # cables are edge data, not nodes
            self.graph.add_node(comp.id, comp=comp)
        return comp

    def comp(self, cid):
        return self.components[cid]

    def port_connection(self, ref):
        # the Connection already using (comp_id, port), or None
        cid, port = ref
        if cid not in self.graph:
            return None
        for _u, _v, conn in self.graph.edges(cid, data="conn"):
            if (conn.a == ref) or (conn.b == ref):
                return conn
        return None

    def port_used(self, ref):
        return self.port_connection(ref) is not None

    def _describe_port(self, ref):
        comp = self.components[ref[0]]
        if comp.kind == "node":
            return "line '%s'" % comp.name
        return "port '%s.%s'" % (comp.name or comp.label or comp.id, ref[1])

    def add_connection(self, conn):
        # make sure nobody double-connects the same port, then record it in
        # both the ordered list and the graph
        for ref in (conn.a, conn.b):
            prev = self.port_connection(ref)
            if prev is not None:
                raise ParseError(
                    "%s is already connected (first used at %s:%d)"
                    % (self._describe_port(ref), prev.src.file, prev.src.line),
                    conn.src.file, conn.src.line)
        self.connections.append(conn)
        self.graph.add_edge(conn.a[0], conn.b[0], conn=conn)
        return conn

    def connections_between(self, c0, c1):
        # every Connection joining c0 and c1 (either direction), file order
        data = self.graph.get_edge_data(c0, c1)
        if not data:
            return []
        conns = [d["conn"] for d in data.values()]
        conns.sort(key=lambda c: self.connections.index(c))
        return conns

    def connections_of(self, cid):
        # every Connection touching cid, file order
        if cid not in self.graph:
            return []
        conns = [conn for _u, _v, conn in self.graph.edges(cid, data="conn")]
        conns.sort(key=lambda c: self.connections.index(c))
        return conns

    def plate_names(self):
        names = []
        for p in self.plates:
            names.append(p[0])
        return names

    def validate(self):
        # declared stuff that never got used
        cables_used = set()
        for c in self.connections:
            if c.cable_id:
                cables_used.add(c.cable_id)
        for comp in self.components.values():
            if comp.kind == "node" or not comp.name:
                continue
            if comp.kind == "cable":
                unused = comp.id not in cables_used
            else:
                unused = self.graph.degree(comp.id) == 0
            if unused:
                self.warnings.append(
                    "%s: declared %s '%s' is never used in any [wiring] chain"
                    % (comp.src.file, comp.kind, comp.name))

        # declared devices that are wired up, but only to each other and
        # never back to a fridge line (probably a typo in a line/device name)
        line_nodes = set()
        for ln in self.lines.values():
            line_nodes.add(ln.start_id)
            line_nodes.add(ln.end_id)
        for part in nx.connected_components(self.graph):
            if len(part) == 1 or part & line_nodes:
                continue  # singletons are already reported as unused
            named = []
            for cid in part:
                comp = self.components[cid]
                if comp.name and comp.kind != "node":
                    named.append(comp)
            if not named:
                continue
            named.sort(key=lambda c: (c.src.file, c.src.line))
            first = named[0]
            self.warnings.append(
                "%s:%d: %s '%s' is not connected to any fridge line"
                % (first.src.file, first.src.line, first.kind, first.name))
