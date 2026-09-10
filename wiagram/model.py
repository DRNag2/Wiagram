"""Data model for wiagram: components, ports, connections, lines, chains."""

from dataclasses import dataclass, field
from typing import Optional


class ParseError(Exception):
    """Error in a wiring text file, with source location."""

    def __init__(self, msg, file=None, line=None):
        self.msg = msg
        self.file = file
        self.line = line
        super().__init__(msg)

    def __str__(self):
        if self.file:
            loc = self.file if self.line is None else "%s:%d" % (self.file, self.line)
            return "%s: %s" % (loc, self.msg)
        return self.msg


@dataclass
class Src:
    file: str = ""
    line: int = 0


# Canonical component kinds and the aliases accepted in `type:` fields.
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

# Fixed port sets per kind. None => dynamic (ports created as referenced).
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

# Default through-path (entry, exit) for multi-port kinds used mid-chain
# without explicit ports.
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
    name: str = ""            # user-facing declared name, if any
    label: str = ""
    serial: str = ""
    plate: str = ""
    owner: str = ""
    color: str = ""
    note: str = ""
    port_list: Optional[list] = None   # explicit port order (dynamic kinds)
    port_sides: dict = field(default_factory=dict)  # port -> W/E/N/S override
    src: Src = field(default_factory=Src)
    # Filled by layout:
    x: float = 0.0
    y: float = 0.0
    orient: str = "h"          # 'h' = flow left->right, 'v' = flow downward
    flip: bool = False         # signal flows right->left (output lines)
    placed: bool = False
    zone: str = ""             # 'line' or 'band'

    def ports(self):
        if self.kind in FIXED_PORTS:
            return list(FIXED_PORTS[self.kind])
        if self.kind in DYNAMIC_PORT_KINDS:
            return list(self.port_list or [])
        return ["in", "out"]

    def display_label(self):
        return self.label or self.name


@dataclass
class Connection:
    a: tuple                  # (component_id, port)
    b: tuple
    cable_id: Optional[str] = None
    owner: str = ""
    src: Src = field(default_factory=Src)
    points: list = field(default_factory=list)   # filled by layout


@dataclass
class Chain:
    """One `->` chain as written; drives layout order."""
    owner: str
    comps: list = field(default_factory=list)     # ordered component ids (no cables)
    line: str = ""                                # set for line-template chains
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
        self.plates = []          # [(name, temp_label)]
        self.components = {}      # id -> Component (insertion ordered)
        self.connections = []
        self.chains = []          # user + line chains
        self.lines = {}           # name -> Line (insertion ordered)
        self.owners = []          # owner display names, file order
        self.warnings = []
        self._port_use = {}       # (comp_id, port) -> Src of first use

    def add_component(self, comp):
        if comp.id in self.components:
            raise ParseError("duplicate component id '%s'" % comp.id,
                             comp.src.file, comp.src.line)
        self.components[comp.id] = comp
        return comp

    def comp(self, cid):
        return self.components[cid]

    def use_port(self, ref, src):
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
        return [p[0] for p in self.plates]

    def validate(self):
        """Post-build checks; appends human-readable warnings."""
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
