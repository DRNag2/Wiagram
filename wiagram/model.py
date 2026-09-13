# data structures for the wiring diagram stuff

from dataclasses import dataclass, field
from typing import Optional


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
        self._port_use = {}      # (comp_id, port) -> where it was first used

    def add_component(self, comp):
        if comp.id in self.components:
            raise ParseError("duplicate component id '%s'" % comp.id,
                             comp.src.file, comp.src.line)
        self.components[comp.id] = comp
        return comp

    def comp(self, cid):
        return self.components[cid]

    def use_port(self, ref, src):
        # make sure nobody double-connects the same port
        prev = self._port_use.get(ref)
        if prev is not None:
            comp = self.components[ref[0]]
            if comp.kind == "node":
                what = "line '%s'" % comp.name
            else:
                what = "port '%s.%s'" % (comp.name or comp.label or comp.id,
                                         ref[1])
            raise ParseError(
                "%s is already connected (first used at %s:%d)"
                % (what, prev.file, prev.line), src.file, src.line)
        self._port_use[ref] = src

    def port_used(self, ref):
        return ref in self._port_use

    def plate_names(self):
        names = []
        for p in self.plates:
            names.append(p[0])
        return names

    def validate(self):
        # check for declared stuff that never got used
        referenced = set()
        for c in self.connections:
            referenced.add(c.a[0])
            referenced.add(c.b[0])
            if c.cable_id:
                referenced.add(c.cable_id)
        for comp in self.components.values():
            if comp.kind == "node":
                continue
            if comp.name and comp.id not in referenced:
                self.warnings.append(
                    "%s: declared %s '%s' is never used in any [wiring] chain"
                    % (comp.src.file, comp.kind, comp.name))
