"""Parser for the wiagram DSL.

File format (one file per owner, plus a shared common.txt):

    owner: Theo                      # top-level metadata

    [device kerrcat]                 # multi-port / named hardware
    type: experiment
    label: Kerr-Cat
    ports: in, out, pump:S

    [cable nb1]
    type: NbTi
    serial: ZS20211123-3

    [wiring]
    In 4 -> eccosorb -> kerrcat.in
    kerrcat.out -> nb1 -> c24.1
    c24.2 -> Out B
    c24.3 -> term

common.txt additionally declares [plates], [template NAME] and [lines].
"""

import os
import re

from .model import (
    AUTO_THROUGH, Chain, Component, Connection, Design, DYNAMIC_PORT_KINDS,
    FIXED_PORTS, KIND_ALIASES, Line, ParseError, Src, TWO_PORT_KINDS,
)

SECTION_RE = re.compile(r"^\[\s*(\w+)(?:\s+([^\]]+?))?\s*\]$")
KV_RE = re.compile(r"^([^:]+?)\s*:\s*(.*)$")
COMMENT_RE = re.compile(r"(?:^|\s)#.*$")
RANGE_RE = re.compile(r"^(.*?)(\d+|[A-Z])\s*\.\.\s*(?:(.*?))?(\d+|[A-Z])$")
CALL_RE = re.compile(r"^([\w-]+)\s*(?:\((.*)\))?$")

# ---------------------------------------------------------------------------
# Inline component patterns usable directly in chains, e.g. "-20dB", "term(50)"
# ---------------------------------------------------------------------------

def _inline_patterns():
    def atten(m, tok):
        return ("attenuator", tok.replace(" ", ""), {})

    def term(m, tok):
        return ("term", m.group(1) or "50Ω", {})

    pats = [
        (r"^[+-]?\d+(?:\.\d+)?\s*dB$", atten),
        (r"^term(?:\((.*)\))?$", term),
        (r"^eccosorb$", lambda m, t: ("filter", "Eccosorb", {})),
        (r"^(?:knl|k&l)(?:\((.*)\))?$",
         lambda m, t: ("filter", ("K&L " + m.group(1)).strip() if m.group(1) else "K&L", {})),
        (r"^(lp|hp|bp)\((.*)\)$",
         lambda m, t: ("filter", "%s %s" % (m.group(1).upper(), m.group(2)), {})),
        (r"^filter\((.*)\)$", lambda m, t: ("filter", m.group(1), {})),
        (r"^hemt(?:\((.*)\))?$",
         lambda m, t: ("amplifier", ("HEMT " + m.group(1)).strip() if m.group(1) else "HEMT", {})),
        (r"^amp(?:\((.*)\))?$",
         lambda m, t: ("amplifier", m.group(1) or "Amp", {})),
        (r"^bulkhead$", lambda m, t: ("bulkhead", "", {})),
        (r"^db-?25$", lambda m, t: ("connector", "DB-25", {})),
        (r"^fischer$", lambda m, t: ("connector", "Fischer", {})),
        (r"^sma$", lambda m, t: ("connector", "SMA", {})),
        (r"^cable\((.*)\)$", lambda m, t: ("cable", "", {"type": m.group(1)})),
        (r"^coupler(?:\((.*)\))?$",
         lambda m, t: ("coupler", m.group(1) or "", {})),
        (r"^biastee$|^bias-tee$", lambda m, t: ("biastee", "Bias-T", {})),
        (r"^circ(?:ulator)?\((.*)\)$",
         lambda m, t: ("circulator", m.group(1), {})),
        (r"^iso(?:lator)?(?:\((.*)\))?$",
         lambda m, t: ("isolator", m.group(1) or "", {})),
    ]
    return [(re.compile(p, re.I), fn) for p, fn in pats]

INLINE_PATTERNS = _inline_patterns()


# ---------------------------------------------------------------------------
# Phase 1: raw file parsing
# ---------------------------------------------------------------------------

class RawFile:
    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        self.meta = {}
        self.devices = {}    # name -> (dict, Src)
        self.cables = {}
        self.templates = {}  # name -> (dict, Src)
        self.plates = []     # [(name, temp, Src)]
        self.line_decls = [] # [(name_spec, template, params, Src)]
        self.chains = []     # [(chain_text, Src)]


def _strip_comment(line):
    return COMMENT_RE.sub("", line).rstrip()


def parse_file(path):
    raw = RawFile(path)
    section = None       # None (meta) or (sect_kind, sect_name, dict, Src)
    pending_chain = None  # (text, Src) for chains continued across lines

    with open(path, encoding="utf-8") as f:
        raw_lines = f.readlines()

    def flush_chain():
        nonlocal pending_chain
        if pending_chain is not None:
            raw.chains.append(pending_chain)
            pending_chain = None

    for lineno, orig in enumerate(raw_lines, 1):
        line = _strip_comment(orig).strip()
        if not line:
            continue
        src = Src(raw.name, lineno)

        m = SECTION_RE.match(line)
        if m:
            flush_chain()
            kind, name = m.group(1).lower(), (m.group(2) or "").strip()
            if kind in ("device", "cable", "template"):
                if not name:
                    raise ParseError("[%s] needs a name, e.g. [%s c24]" % (kind, kind),
                                     raw.name, lineno)
                if "." in name or "->" in name or " " in name:
                    raise ParseError(
                        "%s name '%s' may not contain spaces, dots or arrows"
                        % (kind, name), raw.name, lineno)
                d = {}
                store = {"device": raw.devices, "cable": raw.cables,
                         "template": raw.templates}[kind]
                if name in store:
                    raise ParseError("duplicate [%s %s]" % (kind, name), raw.name, lineno)
                store[name] = (d, src)
                section = (kind, name, d, src)
            elif kind in ("plates", "lines", "wiring"):
                section = (kind, "", None, src)
            else:
                raise ParseError(
                    "unknown section [%s]; expected device, cable, template, "
                    "plates, lines or wiring" % kind, raw.name, lineno)
            continue

        if section is None:
            m = KV_RE.match(line)
            if not m:
                raise ParseError("expected 'key: value' metadata or a [section]",
                                 raw.name, lineno)
            raw.meta[m.group(1).strip().lower()] = m.group(2).strip()
            continue

        skind = section[0]
        if skind in ("device", "cable", "template"):
            m = KV_RE.match(line)
            if not m:
                # continuation of a multi-line value (previous line ends in ->)
                d = section[2]
                if d:
                    lastkey = list(d)[-1]
                    val, vsrc = d[lastkey]
                    if val.rstrip().endswith("->"):
                        d[lastkey] = (val + " " + line, vsrc)
                        continue
                raise ParseError("expected 'key: value' inside [%s %s]"
                                 % (skind, section[1]), raw.name, lineno)
            section[2][m.group(1).strip().lower()] = (m.group(2).strip(), src)
        elif skind == "plates":
            m = KV_RE.match(line)
            if not m:
                raise ParseError("expected 'PlateName: temperature'", raw.name, lineno)
            raw.plates.append((m.group(1).strip(), m.group(2).strip(), src))
        elif skind == "lines":
            m = KV_RE.match(line)
            if not m:
                raise ParseError(
                    "expected 'Line name: template' or 'In 1..In 24: template'",
                    raw.name, lineno)
            spec, rhs = m.group(1).strip(), m.group(2).strip()
            cm = CALL_RE.match(rhs)
            if not cm:
                raise ParseError("bad template reference '%s'" % rhs, raw.name, lineno)
            params = {}
            if cm.group(2):
                for part in cm.group(2).split(","):
                    if "=" not in part:
                        raise ParseError(
                            "template parameters must look like name=value: '%s'"
                            % part.strip(), raw.name, lineno)
                    k, v = part.split("=", 1)
                    params[k.strip()] = v.strip()
            raw.line_decls.append((spec, cm.group(1), params, src))
        elif skind == "wiring":
            if pending_chain is not None:
                text = pending_chain[0] + " " + line
                pending_chain = (text, pending_chain[1])
            else:
                pending_chain = (line, src)
            if not pending_chain[0].rstrip().endswith("->"):
                flush_chain()

    flush_chain()
    return raw


# ---------------------------------------------------------------------------
# Phase 2: build the Design
# ---------------------------------------------------------------------------

def expand_range(spec, src):
    """'In 1..In 24' -> ['In 1', ..., 'In 24']; single names pass through."""
    if ".." not in spec:
        return [spec]
    m = RANGE_RE.match(spec)
    if not m:
        raise ParseError("cannot parse line range '%s'" % spec, src.file, src.line)
    pre1, s1, pre2, s2 = m.group(1), m.group(2), m.group(3), m.group(4)
    if pre2 and pre2.strip() and pre2.strip() != pre1.strip():
        raise ParseError("range prefixes differ: '%s' vs '%s'" % (pre1, pre2),
                         src.file, src.line)
    if s1.isdigit() != s2.isdigit():
        raise ParseError("range endpoints must both be numbers or both letters: '%s'"
                         % spec, src.file, src.line)
    if s1.isdigit():
        lo, hi = int(s1), int(s2)
        if hi < lo:
            raise ParseError("range is backwards: '%s'" % spec, src.file, src.line)
        return ["%s%d" % (pre1, i) for i in range(lo, hi + 1)]
    lo, hi = ord(s1), ord(s2)
    if hi < lo:
        raise ParseError("range is backwards: '%s'" % spec, src.file, src.line)
    return ["%s%s" % (pre1, chr(i)) for i in range(lo, hi + 1)]


def _get(d, key, default=""):
    v = d.get(key)
    return v[0] if v else default


class Builder:
    def __init__(self):
        self.design = Design()
        self.anon_counter = 0

    # -- declared components -------------------------------------------------

    def make_device(self, rawfile, owner, name, d, src):
        typ = _get(d, "type").lower()
        if not typ:
            raise ParseError("[device %s] is missing 'type:'" % name, src.file, src.line)
        kind = KIND_ALIASES.get(typ)
        if kind is None or kind == "cable":
            raise ParseError(
                "unknown device type '%s' (known: %s)"
                % (typ, ", ".join(sorted(set(KIND_ALIASES) - {"cable"}))),
                src.file, src.line)
        comp = Component(
            id="%s:%s" % (owner, name), kind=kind, name=name,
            label=_get(d, "label", name), serial=_get(d, "serial"),
            plate=_get(d, "plate"), owner=owner, color=_get(d, "color"),
            note=_get(d, "note"), src=src)
        ports_val = _get(d, "ports")
        if ports_val:
            plist, sides = [], {}
            for p in ports_val.split(","):
                p = p.strip()
                if ":" in p:
                    pname, side = p.split(":", 1)
                    pname, side = pname.strip(), side.strip().upper()
                    if side not in ("W", "E", "N", "S"):
                        raise ParseError("port side must be W, E, N or S: '%s'" % p,
                                         src.file, src.line)
                    sides[pname] = side
                    plist.append(pname)
                else:
                    plist.append(p)
            comp.port_list = plist
            comp.port_sides = sides
        if comp.plate:
            self.check_plate(comp.plate, src)
        return self.design.add_component(comp)

    def make_cable(self, rawfile, owner, name, d, src):
        comp = Component(
            id="%s:%s" % (owner, name), kind="cable", name=name,
            label=_get(d, "label"), serial=_get(d, "serial"),
            owner=owner, color=_get(d, "color"), src=src)
        comp.note = _get(d, "type")   # cable material/type lives in note
        return self.design.add_component(comp)

    def check_plate(self, plate, src):
        if plate not in self.design.plate_names():
            raise ParseError(
                "unknown plate '%s' (plates: %s)"
                % (plate, ", ".join(self.design.plate_names())),
                src.file, src.line)

    # -- chain walking ---------------------------------------------------------

    def resolve_token(self, tok, ctx, src):
        """Return ('comp', Component, explicit_port|None) or ('cable', Component)
        or ('line', Line)."""
        tok = tok.strip()
        if not tok:
            raise ParseError("empty chain element (double arrow '-> ->'?)",
                             src.file, src.line)
        plate = ""
        if "@" in tok:
            base, plate = tok.rsplit("@", 1)
            tok, plate = base.strip(), plate.strip()
            self.check_plate(plate, src)

        # line reference
        if tok in self.design.lines:
            if plate:
                raise ParseError("line '%s' cannot take a plate anchor" % tok,
                                 src.file, src.line)
            return ("line", self.design.lines[tok], None)

        # declared name (with optional .port)
        name, port = tok, None
        if "." in tok:
            name, port = tok.split(".", 1)
            name, port = name.strip(), port.strip()
        dev = ctx["devices"].get(name)
        if dev is not None:
            comp = self.design.components[dev]
            if plate and not comp.plate:
                comp.plate = plate
            if comp.kind == "cable":
                if port:
                    raise ParseError("cables have no ports: '%s'" % tok,
                                     src.file, src.line)
                return ("cable", comp, None)
            if port:
                self.check_port(comp, port, src)
            return ("comp", comp, port)
        if "." in tok:
            raise ParseError(
                "unknown device '%s' (declare it with [device %s])" % (name, name),
                src.file, src.line)

        # inline patterns
        for pat, fn in INLINE_PATTERNS:
            m = pat.match(tok)
            if m:
                kind, label, params = fn(m, tok)
                self.anon_counter += 1
                comp = Component(
                    id="%s:~%d" % (ctx["owner"], self.anon_counter),
                    kind=kind, label=label, plate=plate,
                    owner=ctx["owner"], src=src)
                if kind == "cable":
                    comp.note = params.get("type", "")
                self.design.add_component(comp)
                return ("cable" if kind == "cable" else "comp", comp, None)

        known = sorted(ctx["devices"])
        hint = ("; declared here: %s" % ", ".join(known)) if known else ""
        raise ParseError(
            "cannot understand '%s': not a line, declared device, or inline "
            "component like -20dB, eccosorb, term, hemt(A1), bulkhead, "
            "cable(NbTi)%s" % (tok, hint), src.file, src.line)

    def check_port(self, comp, port, src):
        ports = comp.ports()
        if comp.kind in DYNAMIC_PORT_KINDS:
            if comp.port_list is None:
                comp.port_list = []
            if port not in comp.port_list:
                comp.port_list.append(port)
            return
        if port not in ports:
            raise ParseError(
                "%s '%s' has ports %s, not '%s'"
                % (comp.kind, comp.name or comp.label, "/".join(ports), port),
                src.file, src.line)

    def entry_port(self, comp, explicit, src):
        if explicit:
            return explicit
        if comp.kind in TWO_PORT_KINDS:
            return "in"
        if comp.kind in AUTO_THROUGH:
            return AUTO_THROUGH[comp.kind][0]
        if comp.kind == "term":
            return "p"
        if comp.kind == "node":
            return "b"
        raise ParseError(
            "%s '%s' needs an explicit port here, e.g. %s.in"
            % (comp.kind, comp.name or comp.label, comp.name or "name"),
            src.file, src.line)

    def exit_port(self, comp, entered, explicit, src):
        """Port used to continue the chain after this component."""
        if comp.kind in TWO_PORT_KINDS:
            return "out" if entered != "out" else "in"
        if comp.kind == "node":
            return "b"
        if comp.kind in AUTO_THROUGH and explicit is None:
            return AUTO_THROUGH[comp.kind][1]
        if comp.kind == "term":
            raise ParseError("a termination ends the chain; nothing can follow it",
                             src.file, src.line)
        raise ParseError(
            "cannot continue a chain through '%s.%s'; start a new chain from "
            "another port instead" % (comp.name or comp.label, entered),
            src.file, src.line)

    def walk_chain(self, chain_text, ctx, src, line_name=""):
        """Parse one arrow chain, creating components and connections.

        Returns (Chain, open_out_portref_or_None).
        """
        tokens = [t for t in chain_text.split("->")]
        if len(tokens) < 2 and not line_name:
            raise ParseError("a chain needs at least two elements joined by ->",
                             src.file, src.line)
        chain = Chain(owner=ctx["owner"], line=line_name, src=src)
        prev_out = ctx.get("start_port")   # line templates start at the start node
        if prev_out is not None:
            chain.comps.append(prev_out[0])
        pending_cable = None

        for i, tok in enumerate(tokens):
            res = self.resolve_token(tok, ctx, src)
            if res[0] == "cable":
                if pending_cable is not None:
                    raise ParseError(
                        "two cables in a row ('%s'); put a component between them"
                        % tok.strip(), src.file, src.line)
                if prev_out is None:
                    raise ParseError(
                        "a chain cannot start with a cable ('%s')" % tok.strip(),
                        src.file, src.line)
                pending_cable = res[1]
                continue

            last = (i == len(tokens) - 1)
            if res[0] == "line":
                ln = res[1]
                end_comp = self.design.components[ln.end_id]
                if prev_out is None:            # chain starts at a line
                    prev_out = (end_comp.id, "a")
                    chain.comps.append(end_comp.id)
                    continue
                if not last:
                    raise ParseError(
                        "line '%s' can only start or end a chain" % ln.name,
                        src.file, src.line)
                ref = (end_comp.id, "a")
                self.design.use_port(prev_out, src)
                self.design.use_port(ref, src)
                self.connect(prev_out, ref, pending_cable, ctx["owner"], src)
                chain.comps.append(end_comp.id)
                pending_cable = None
                prev_out = None
                continue

            comp, explicit = res[1], res[2]
            if prev_out is None:
                # first element: acts as a source
                if explicit:
                    out = explicit
                elif comp.kind in TWO_PORT_KINDS:
                    out = "out"
                elif comp.kind in AUTO_THROUGH:
                    out = AUTO_THROUGH[comp.kind][1]
                elif comp.kind == "node":
                    out = "b"
                else:
                    raise ParseError(
                        "%s '%s' needs an explicit port to start a chain"
                        % (comp.kind, comp.name or comp.label), src.file, src.line)
                prev_out = (comp.id, out)
                chain.comps.append(comp.id)
                continue

            entry = self.entry_port(comp, explicit, src)
            self.design.use_port(prev_out, src)
            self.design.use_port((comp.id, entry), src)
            self.connect(prev_out, (comp.id, entry), pending_cable, ctx["owner"], src)
            pending_cable = None
            chain.comps.append(comp.id)
            if last:
                prev_out = None
                if comp.kind in TWO_PORT_KINDS and not explicit:
                    prev_out = (comp.id, "out")   # open end, may continue to node
            else:
                prev_out = (comp.id, self.exit_port(comp, entry, explicit, src))

        if pending_cable is not None and not line_name:
            raise ParseError("chain ends with a cable; add the component it "
                             "connects to", src.file, src.line)
        self.design.chains.append(chain)
        return chain, prev_out, pending_cable

    def connect(self, a, b, cable, owner, src):
        self.design.connections.append(Connection(
            a=a, b=b, cable_id=cable.id if cable else None, owner=owner, src=src))

    # -- lines -----------------------------------------------------------------

    def instantiate_line(self, name, tmpl_name, params, templates, ctx_common, src):
        if name in self.design.lines:
            raise ParseError("line '%s' declared twice" % name, src.file, src.line)
        if tmpl_name not in templates:
            raise ParseError(
                "unknown template '%s' (known: %s)"
                % (tmpl_name, ", ".join(sorted(templates)) or "none"),
                src.file, src.line)
        tdict, tsrc = templates[tmpl_name]
        chain_text = _get(tdict, "chain")
        if not chain_text:
            raise ParseError("[template %s] has no 'chain:'" % tmpl_name,
                             tsrc.file, tsrc.line)
        # parameter substitution
        def sub(m):
            k = m.group(1)
            if k not in params:
                raise ParseError(
                    "line '%s': template '%s' needs parameter '%s='"
                    % (name, tmpl_name, k), src.file, src.line)
            return params[k]
        chain_text = re.sub(r"\{(\w+)\}", sub, chain_text)

        start = self.design.add_component(Component(
            id="line:%s:start" % name, kind="node", name=name, owner="", src=src))
        end = self.design.add_component(Component(
            id="line:%s:end" % name, kind="node", name=name, owner="", src=src))
        line = Line(name=name, template=tmpl_name,
                    group=_get(tdict, "group", tmpl_name),
                    start_id=start.id, end_id=end.id, src=src)
        self.design.lines[name] = line

        ctx = dict(ctx_common)
        ctx["start_port"] = (start.id, "b")
        chain, open_out, trail_cable = self.walk_chain(
            chain_text, ctx, tsrc, line_name=name)
        if open_out is not None:
            self.design.use_port(open_out, tsrc)
            self.design.use_port((end.id, "b"), tsrc)
            self.connect(open_out, (end.id, "b"), trail_cable, "", tsrc)
            chain.comps.append(end.id)
        if _get(tdict, "direction").lower() == "out":
            for cid in chain.comps:
                self.design.components[cid].flip = True
        line.chain = chain
        return line


def build(rawfiles):
    """rawfiles: list of RawFile, common first."""
    b = Builder()
    design = b.design

    # plates (exactly one file may declare them)
    plate_files = [rf for rf in rawfiles if rf.plates]
    if not plate_files:
        raise ParseError("no [plates] section found in any file; common.txt "
                         "must declare the temperature plates")
    if len(plate_files) > 1:
        raise ParseError("[plates] declared in multiple files: %s"
                         % ", ".join(rf.name for rf in plate_files))
    for pname, temp, psrc in plate_files[0].plates:
        if pname in design.plate_names():
            raise ParseError("duplicate plate '%s'" % pname, psrc.file, psrc.line)
        design.plates.append((pname, temp))

    # metadata / owners
    for rf in rawfiles:
        owner = rf.meta.get("owner") or os.path.splitext(rf.name)[0].capitalize()
        rf.owner = owner
        design.owners.append(owner)
        for k, v in rf.meta.items():
            design.meta.setdefault(k, v)

    # devices & cables (file-scoped names)
    contexts = {}
    for rf in rawfiles:
        devices = {}
        for name, (d, src) in rf.devices.items():
            devices[name] = b.make_device(rf, rf.owner, name, d, src).id
        for name, (d, src) in rf.cables.items():
            if name in devices:
                raise ParseError("'%s' is both a device and a cable" % name,
                                 src.file, src.line)
            devices[name] = b.make_cable(rf, rf.owner, name, d, src).id
        contexts[rf.name] = {"owner": rf.owner, "devices": devices}

    # templates (global registry)
    templates = {}
    for rf in rawfiles:
        for name, (d, src) in rf.templates.items():
            if name in templates:
                raise ParseError("template '%s' defined in more than one file"
                                 % name, src.file, src.line)
            templates[name] = (d, src)

    # lines
    for rf in rawfiles:
        ctx = contexts[rf.name]
        for spec, tmpl, params, src in rf.line_decls:
            for lname in expand_range(spec, src):
                b.instantiate_line(lname, tmpl, params, templates, ctx, src)

    # wiring chains
    for rf in rawfiles:
        ctx = contexts[rf.name]
        for text, src in rf.chains:
            b.walk_chain(text, ctx, src)

    design.validate()
    return design


def load(paths, common_name="common.txt"):
    """Load a directory or explicit list of .txt files into a Design."""
    if isinstance(paths, str):
        paths = [paths]
    files = []
    for p in paths:
        if os.path.isdir(p):
            files.extend(sorted(
                os.path.join(p, f) for f in os.listdir(p)
                if f.endswith(".txt") and not f.startswith(".")))
        else:
            files.append(p)
    if not files:
        raise ParseError("no .txt wiring files found in %s" % ", ".join(paths))
    # common file first
    files.sort(key=lambda f: (os.path.basename(f) != common_name,
                              os.path.basename(f)))
    rawfiles = [parse_file(f) for f in files]
    return build(rawfiles)
