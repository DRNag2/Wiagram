# parser for the wiring text format
#
# each person has their own file + there's a shared common.txt
#
# example:
#   owner: Bob
#
#   [device kerrcat]
#   type: experiment
#   label: Kerr-Cat
#   ports: in, out, pump:S
#
#   [cable nb1]
#   type: NbTi
#   serial: ZS20211123-3
#
#   [wiring]
#   In 4 -> eccosorb -> kerrcat.in
#   kerrcat.out -> nb1 -> c24.1
#   c24.2 -> Out B
#   c24.3 -> term
#
# common.txt also has [plates], [template ...] and [lines]

import os
import re

from .model import (
    AUTO_THROUGH, Chain, Component, Connection, Design, DYNAMIC_PORT_KINDS,
    FIXED_PORTS, KIND_ALIASES, Line, ParseError, ParseErrors, Src,
    TWO_PORT_KINDS,
)

SECTION_RE = re.compile(r"^\[\s*(\w+)(?:\s+([^\]]+?))?\s*\]$")
KV_RE = re.compile(r"^([^:]+?)\s*:\s*(.*)$")
COMMENT_RE = re.compile(r"(?:^|\s)#.*$")
RANGE_RE = re.compile(r"^(.*?)(\d+|[A-Z])\s*\.\.\s*(?:(.*?))?(\d+|[A-Z])$")
CALL_RE = re.compile(r"^([\w-]+)\s*(?:\((.*)\))?$")


# stuff you can type straight into a chain like "-20dB" or "term(50)".
# Each record is (regex, syntax, example, description, factory); the factory
# gets (match, token) and returns (kind, label, params).  TOOLBOX.md is
# generated from this table, so keep syntax/description readable.

class InlinePart:
    def __init__(self, regex, syntax, example, description, factory):
        self.regex = re.compile(regex, re.I)
        self.syntax = syntax
        self.example = example
        self.description = description
        self.factory = factory

    def match(self, tok):
        return self.regex.match(tok)

    def make(self, m, tok):
        return self.factory(m, tok)


def _opt(prefix, m):
    # "HEMT A1" / "HEMT" style labels from an optional (...) argument
    return (prefix + " " + m.group(1)).strip() if m.group(1) else prefix


INLINE_PARTS = [
    InlinePart(r"^[+-]?\d+(?:\.\d+)?\s*dB$", "<n>dB", "-20dB",
               "attenuator; the number is the label (0dB for a plain through)",
               lambda m, t: ("attenuator", t.replace(" ", ""), {})),
    InlinePart(r"^term(?:\((.*)\))?$", "term, term(<label>)", "term(50)",
               "termination / 50 Ohm load; ends the chain",
               lambda m, t: ("term", m.group(1) or "50Ω", {})),
    InlinePart(r"^eccosorb$", "eccosorb", "eccosorb",
               "Eccosorb infrared filter",
               lambda m, t: ("filter", "Eccosorb", {})),
    InlinePart(r"^(?:knl|k&l)(?:\((.*)\))?$", "knl, knl(<model>)", "knl(6L250)",
               "K&L low-pass filter",
               lambda m, t: ("filter", _opt("K&L", m), {})),
    InlinePart(r"^(lp|hp|bp)\((.*)\)$", "lp(<f>), hp(<f>), bp(<f>)", "lp(12GHz)",
               "low-/high-/band-pass filter with its corner frequency or model",
               lambda m, t: ("filter", "%s %s" % (m.group(1).upper(), m.group(2)), {})),
    InlinePart(r"^filter\((.*)\)$", "filter(<label>)", "filter(ZX75-...)",
               "any other filter; the label is printed under the symbol",
               lambda m, t: ("filter", m.group(1), {})),
    InlinePart(r"^hemt(?:\((.*)\))?$", "hemt, hemt(<name>)", "hemt(A1)",
               "HEMT amplifier (usually in the output line templates)",
               lambda m, t: ("amplifier", _opt("HEMT", m), {})),
    InlinePart(r"^amp(?:\((.*)\))?$", "amp, amp(<name>)", "amp(TWPA)",
               "any other amplifier (TWPA, SPA, JPA ...)",
               lambda m, t: ("amplifier", m.group(1) or "Amp", {})),
    InlinePart(r"^bulkhead$", "bulkhead", "bulkhead@4K",
               "SMA bulkhead feedthrough on a plate",
               lambda m, t: ("bulkhead", "", {})),
    InlinePart(r"^db-?25$", "db25", "db25@Top",
               "DB-25 connector (DC looms)",
               lambda m, t: ("connector", "DB-25", {})),
    InlinePart(r"^fischer$", "fischer", "fischer",
               "Fischer connector",
               lambda m, t: ("connector", "Fischer", {})),
    InlinePart(r"^sma$", "sma", "sma",
               "SMA adapter / connector",
               lambda m, t: ("connector", "SMA", {})),
    InlinePart(r"^cable\((.*)\)$", "cable(<type>)", "cable(NbTi)",
               "a cable between two parts (SS, NbTi, copper, premade, loom ...); "
               "sets the wire colour",
               lambda m, t: ("cable", "", {"type": m.group(1)})),
    InlinePart(r"^coupler(?:\((.*)\))?$", "coupler, coupler(<label>)", "coupler(-20dB)",
               "directional coupler, passes in->out; use a [device] to wire cpl/iso",
               lambda m, t: ("coupler", m.group(1) or "", {})),
    InlinePart(r"^biastee$|^bias-tee$", "biastee", "biastee",
               "bias tee, passes rf->out; use a [device] to wire the dc port",
               lambda m, t: ("biastee", "Bias-T", {})),
    InlinePart(r"^circ(?:ulator)?\((.*)\)$", "circ(<label>)", "circ(C7)",
               "circulator used in-line, passes 1->2; use a [device] to wire port 3",
               lambda m, t: ("circulator", m.group(1), {})),
    InlinePart(r"^iso(?:lator)?(?:\((.*)\))?$", "iso, iso(<label>)", "iso(LNF)",
               "isolator",
               lambda m, t: ("isolator", m.group(1) or "", {})),
]


# ---- phase 1: just read the file into a RawFile ----

class RawFile:
    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        self.meta = {}
        self.devices = {}     # name -> (dict, Src)
        self.cables = {}
        self.templates = {}   # name -> (dict, Src)
        self.plates = []      # (name, temp, Src)
        self.line_decls = []  # (name_spec, template, params, Src)
        self.chains = []      # (chain_text, Src)


def _strip_comment(line):
    return COMMENT_RE.sub("", line).rstrip()


def parse_file(path):
    # every bad line in the file is collected and raised together
    raw = RawFile(path)
    section = None        # None = still in metadata, else (kind, name, dict, Src)
    pending_chain = None  # for chains that wrap across lines with ->
    errors = []

    with open(path, encoding="utf-8") as f:
        raw_lines = f.readlines()

    for lineno, orig in enumerate(raw_lines, 1):
        line = _strip_comment(orig).strip()
        if not line:
            continue
        src = Src(raw.name, lineno)
        try:
            section, pending_chain = _parse_line(
                raw, line, lineno, src, section, pending_chain)
        except ParseError as e:
            errors.append(e)

    if pending_chain is not None:
        raw.chains.append(pending_chain)
    if errors:
        raise ParseErrors(errors)
    return raw


def _parse_line(raw, line, lineno, src, section, pending_chain):
    # one non-blank line; returns the updated (section, pending_chain)

    def flush(pending):
        if pending is not None:
            raw.chains.append(pending)
        return None

    m = SECTION_RE.match(line)
    if m:
        pending_chain = flush(pending_chain)
        kind = m.group(1).lower()
        name = (m.group(2) or "").strip()
        if kind in ("device", "cable", "template"):
            if not name:
                raise ParseError("[%s] needs a name, e.g. [%s c24]" % (kind, kind),
                                 raw.name, lineno)
            if "." in name or "->" in name or " " in name:
                raise ParseError(
                    "%s name '%s' may not contain spaces, dots or arrows"
                    % (kind, name), raw.name, lineno)
            d = {}
            if kind == "device":
                store = raw.devices
            elif kind == "cable":
                store = raw.cables
            else:
                store = raw.templates
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
        return section, pending_chain

    # still at the top of the file (metadata)
    if section is None:
        m = KV_RE.match(line)
        if not m:
            raise ParseError("expected 'key: value' metadata or a [section]",
                             raw.name, lineno)
        raw.meta[m.group(1).strip().lower()] = m.group(2).strip()
        return section, pending_chain

    skind = section[0]
    if skind in ("device", "cable", "template"):
        m = KV_RE.match(line)
        if not m:
            # maybe continuing a value that ended with ->
            d = section[2]
            if d:
                lastkey = list(d)[-1]
                val, vsrc = d[lastkey]
                if val.rstrip().endswith("->"):
                    d[lastkey] = (val + " " + line, vsrc)
                    return section, pending_chain
            raise ParseError("expected 'key: value' inside [%s %s]"
                             % (skind, section[1]), raw.name, lineno)
        key = m.group(1).strip().lower()
        section[2][key] = (m.group(2).strip(), src)
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
        spec = m.group(1).strip()
        rhs = m.group(2).strip()
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
        # glue onto previous line if they left a trailing ->
        if pending_chain is not None:
            text = pending_chain[0] + " " + line
            pending_chain = (text, pending_chain[1])
        else:
            pending_chain = (line, src)
        if not pending_chain[0].rstrip().endswith("->"):
            pending_chain = flush(pending_chain)
    return section, pending_chain


# ---- phase 2: turn RawFiles into a Design ----

def expand_range(spec, src):
    # "In 1..In 24" -> ["In 1", ..., "In 24"]
    # single names just come back as a one-element list
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
        lo = int(s1)
        hi = int(s2)
        if hi < lo:
            raise ParseError("range is backwards: '%s'" % spec, src.file, src.line)
        out = []
        for i in range(lo, hi + 1):
            out.append("%s%d" % (pre1, i))
        return out
    lo = ord(s1)
    hi = ord(s2)
    if hi < lo:
        raise ParseError("range is backwards: '%s'" % spec, src.file, src.line)
    out = []
    for i in range(lo, hi + 1):
        out.append("%s%s" % (pre1, chr(i)))
    return out


def _get(d, key, default=""):
    v = d.get(key)
    if v:
        return v[0]
    return default


class Builder:
    def __init__(self):
        self.design = Design()
        self.anon_counter = 0

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
            plist = []
            sides = {}
            for p in ports_val.split(","):
                p = p.strip()
                if ":" in p:
                    pname, side = p.split(":", 1)
                    pname = pname.strip()
                    side = side.strip().upper()
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
        # stash the cable type/material in note (a bit hacky but works)
        comp.note = _get(d, "type")
        return self.design.add_component(comp)

    def check_plate(self, plate, src):
        if plate not in self.design.plate_names():
            raise ParseError(
                "unknown plate '%s' (plates: %s)"
                % (plate, ", ".join(self.design.plate_names())),
                src.file, src.line)

    def resolve_token(self, tok, ctx, src):
        # returns ('comp', Component, port_or_None)
        #      or ('cable', Component, None)
        #      or ('line', Line, None)
        tok = tok.strip()
        if not tok:
            raise ParseError("empty chain element (double arrow '-> ->'?)",
                             src.file, src.line)

        plate = ""
        if "@" in tok:
            base, plate = tok.rsplit("@", 1)
            tok = base.strip()
            plate = plate.strip()
            self.check_plate(plate, src)

        # is it a fridge line?
        if tok in self.design.lines:
            if plate:
                raise ParseError("line '%s' cannot take a plate anchor" % tok,
                                 src.file, src.line)
            return ("line", self.design.lines[tok], None)

        # declared device/cable, maybe with .port
        name = tok
        port = None
        if "." in tok:
            name, port = tok.split(".", 1)
            name = name.strip()
            port = port.strip()
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
        # try the inline patterns (-20dB, hemt, etc).  Done before the
        # "unknown device" check so labels with a dot, e.g. lp(12.5GHz),
        # are not mistaken for name.port
        for part in INLINE_PARTS:
            m = part.match(tok)
            if m:
                kind, label, params = part.make(m, tok)
                self.anon_counter += 1
                comp = Component(
                    id="%s:~%d" % (ctx["owner"], self.anon_counter),
                    kind=kind, label=label, plate=plate,
                    owner=ctx["owner"], src=src)
                if kind == "cable":
                    comp.note = params.get("type", "")
                self.design.add_component(comp)
                if kind == "cable":
                    return ("cable", comp, None)
                return ("comp", comp, None)

        if "." in tok:
            raise ParseError(
                "unknown device '%s' (declare it with [device %s])" % (name, name),
                src.file, src.line)

        known = sorted(ctx["devices"])
        if known:
            hint = "; declared here: %s" % ", ".join(known)
        else:
            hint = ""
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
        # which port do we leave on to keep going down the chain
        if comp.kind in TWO_PORT_KINDS:
            if entered != "out":
                return "out"
            return "in"
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
        # parse one arrow chain and make the connections
        # returns (Chain, open_out_portref_or_None, trailing_cable_or_None)
        tokens = [t for t in chain_text.split("->")]
        if len(tokens) < 2 and not line_name:
            raise ParseError("a chain needs at least two elements joined by ->",
                             src.file, src.line)
        chain = Chain(owner=ctx["owner"], line=line_name, src=src)
        prev_out = ctx.get("start_port")  # line templates start at the start node
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
                if prev_out is None:
                    # chain starts at a line
                    prev_out = (end_comp.id, "a")
                    chain.comps.append(end_comp.id)
                    continue
                if not last:
                    raise ParseError(
                        "line '%s' can only start or end a chain" % ln.name,
                        src.file, src.line)
                ref = (end_comp.id, "a")
                self.connect(prev_out, ref, pending_cable, ctx["owner"], src)
                chain.comps.append(end_comp.id)
                pending_cable = None
                prev_out = None
                continue

            comp = res[1]
            explicit = res[2]

            if prev_out is None:
                # first thing in the chain = the source
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
            self.connect(prev_out, (comp.id, entry), pending_cable, ctx["owner"], src)
            pending_cable = None
            chain.comps.append(comp.id)
            if last:
                prev_out = None
                # open end on a two-port so we can still connect to a node later
                if comp.kind in TWO_PORT_KINDS and not explicit:
                    prev_out = (comp.id, "out")
            else:
                prev_out = (comp.id, self.exit_port(comp, entry, explicit, src))

        if pending_cable is not None and not line_name:
            raise ParseError("chain ends with a cable; add the component it "
                             "connects to", src.file, src.line)
        self.design.chains.append(chain)
        return chain, prev_out, pending_cable

    def connect(self, a, b, cable, owner, src):
        # add_connection raises if either port is already taken
        cid = None
        if cable:
            cid = cable.id
        self.design.add_connection(Connection(
            a=a, b=b, cable_id=cid, owner=owner, src=src))

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

        # replace {param} with whatever they passed in
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
            self.connect(open_out, (end.id, "b"), trail_cable, "", tsrc)
            chain.comps.append(end.id)
        if _get(tdict, "direction").lower() == "out":
            for cid in chain.comps:
                self.design.components[cid].flip = True
        line.chain = chain
        return line


def build(rawfiles):
    # rawfiles should have common.txt first
    b = Builder()
    design = b.design

    # plates - only one file should declare them
    plate_files = []
    for rf in rawfiles:
        if rf.plates:
            plate_files.append(rf)
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
        owner = rf.meta.get("owner")
        if not owner:
            owner = os.path.splitext(rf.name)[0].capitalize()
        rf.owner = owner
        design.owners.append(owner)
        for k, v in rf.meta.items():
            if k not in design.meta:
                design.meta[k] = v

    # from here on, errors in one declaration or chain are collected and the
    # rest is still checked, so a file with several mistakes reports them all
    errors = []

    # devices & cables (names are per-file)
    contexts = {}
    for rf in rawfiles:
        devices = {}
        broken = set()   # declarations that failed: chains using them are
        for name, (d, src) in rf.devices.items():   # not reported again
            try:
                devices[name] = b.make_device(rf, rf.owner, name, d, src).id
            except ParseError as e:
                errors.append(e)
                broken.add(name)
        for name, (d, src) in rf.cables.items():
            if name in devices:
                errors.append(ParseError("'%s' is both a device and a cable" % name,
                                         src.file, src.line))
                continue
            devices[name] = b.make_cable(rf, rf.owner, name, d, src).id
        contexts[rf.name] = {"owner": rf.owner, "devices": devices, "broken": broken}

    # templates are global
    templates = {}
    for rf in rawfiles:
        for name, (d, src) in rf.templates.items():
            if name in templates:
                raise ParseError("template '%s' defined in more than one file"
                                 % name, src.file, src.line)
            templates[name] = (d, src)

    # instantiate the lines from [lines]
    for rf in rawfiles:
        ctx = contexts[rf.name]
        for spec, tmpl, params, src in rf.line_decls:
            for lname in expand_range(spec, src):
                b.instantiate_line(lname, tmpl, params, templates, ctx, src)

    # then the [wiring] chains
    for rf in rawfiles:
        ctx = contexts[rf.name]
        for text, src in rf.chains:
            try:
                b.walk_chain(text, ctx, src)
            except ParseError as e:
                names = {t.strip().split("@")[0].split(".")[0] for t in text.split("->")}
                if not (names & ctx["broken"]):
                    errors.append(e)
    if errors:
        raise ParseErrors(errors)

    design.validate()
    return design


def load(paths, common_name="common.txt"):
    # paths can be a dir or a list of files
    if isinstance(paths, str):
        paths = [paths]
    files = []
    for p in paths:
        if os.path.isdir(p):
            for f in sorted(os.listdir(p)):
                if f.endswith(".txt") and not f.startswith("."):
                    files.append(os.path.join(p, f))
        else:
            files.append(p)
    if not files:
        raise ParseError("no .txt wiring files found in %s" % ", ".join(paths))
    # put common.txt first so plates/templates exist before other files
    files.sort(key=lambda f: (os.path.basename(f) != common_name,
                              os.path.basename(f)))
    # phase 1 is per-file, so read every file and report all the syntax
    # errors together instead of stopping at the first one
    rawfiles = []
    errors = []
    for f in files:
        try:
            rawfiles.append(parse_file(f))
        except ParseError as e:
            errors.append(e)
    if errors:
        raise ParseErrors(errors)
    return build(rawfiles)
