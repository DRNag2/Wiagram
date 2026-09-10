# Wiagram

Dilution-fridge wiring diagrams from human-readable text files.

Everyone keeps their own wiring file (`theo.txt`, `haley.txt`, ...) next to a
shared `common.txt`; the engine merges them and renders one clean SVG diagram
of the whole fridge. Because the inputs are plain text, wiring changes are
tracked in git like code, and nobody fights over a LibreOffice file lock.

## Quick start

```
python3 -m wiagram build example/ -o example/wiring.svg
python3 -m wiagram check example/            # validate only
python3 -m wiagram build example/ --pdf example/wiring.pdf   # needs: pip install cairosvg
```

No required dependencies (pure standard library). `cairosvg` is optional, for
PDF output.

## Writing a wiring file

A file has optional metadata at the top, declarations, and a `[wiring]`
section of chains. Chains read left to right, from the fridge line toward
your experiment:

```
owner: Theo

[device cavity]
type: experiment
label: Transmon + Cavity
serial: TS20231027-1
plate: MXC
ports: ro_in:W, qubit:W, ro_out:E

[device c24]
type: circulator
label: C24

[cable nb1]
type: NbTi
serial: ZS20211123-3

[wiring]
In 1 -> -10dB@MXC -> knl -> cavity.ro_in
In 2 -> -3dB@MXC -> knl -> cavity.qubit
cavity.ro_out -> nb1 -> c24.1
c24.2 -> Out B
c24.3 -> term
```

Rules of thumb:

- **Fridge lines** (`In 1`...`In 24`, `Out A`...`Out H`, `DC A`...) come from
  `common.txt` and can start or end a chain. A line can only be claimed once
  across all files -- the build fails with a clear error if two people use
  `In 4`.
- **Simple two-port parts** are written inline, no declaration needed:
  `-20dB`, `0dB`, `eccosorb`, `knl`, `lp(12GHz)`, `filter(anything)`,
  `hemt(A1)`, `amp(SPA)`, `iso`, `bulkhead`, `db25`, `fischer`, `sma`,
  `term`, `term(50)`, `cable(NbTi)`, `biastee`.
- `@Plate` anchors a part to a plate label (e.g. `-10dB@MXC`).
- **Multi-port devices** (circulators, couplers, experiments) are declared
  once as `[device name]` and referenced by port from as many chains as
  needed: `c24.1`, `c24.2`, `c24.3`. Circulator ports are `1/2/3`; coupler
  ports are `in/out/cpl/iso`; bias tees are `rf/dc/out`; experiments declare
  their own `ports:` (with optional `:W/:E/:N/:S` side hints).
- **Cables** with serial numbers are declared as `[cable name]` with a
  `type:` (`copper`, `SS`, `NbTi`, `premade`, ...) that sets the wire color,
  and are placed between two parts in a chain: `kc.out -> nb1 -> c24.1`.
- A chain may span several text lines if each continued line ends with `->`.
- `#` starts a comment.

## common.txt

Declares what rarely changes: `[plates]`, line `[template]`s and `[lines]`.
Templates keep 24 identical input lines to a few text lines, with `{param}`
substitution for per-line differences:

```
[template input_line]
group: Inputs
chain: bulkhead@Top -> cable(SS) -> bulkhead@77K -> cable(SS) -> bulkhead@4K ->
       -20dB@4K -> cable(SS) -> bulkhead@Still -> {still}@Still -> cable(SS)

[lines]
In 1..In 15: input_line(still=-10dB)
In 16..In 24: input_line(still=0dB)
```

`direction: out` in a template flips amplifier symbols for lines whose signal
flows out of the fridge.

## Layout

The renderer draws the familiar layout of our hand-made diagrams: plates as
vertical bars with signal flowing left to right, output lines on top, DC
looms in the middle, input lines at the bottom, and one tinted band per owner
to the right of the MXC plate. Within a band, every `[wiring]` chain gets its
own row; a chain that continues from the previous chain's rightmost component
stays on the same row, and a chain hanging off a downward port (for example a
circulator's port 3 to a termination) drops below it. Layout is
deterministic: the same text always produces the same diagram.

## Regenerating common.txt from an old ODG diagram

`tools/extract_odg.py` mines a LibreOffice Draw diagram for its rows of
shapes and prints a draft:

```
python3 tools/extract_odg.py wiring_diagram.odg          # readable report
python3 tools/extract_odg.py wiring_diagram.odg --draft  # draft common.txt
```

The draft needs proofreading -- it is a starting point, not gospel.
