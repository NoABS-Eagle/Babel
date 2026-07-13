"""KiCad <-> IR layer projection — a USER-EDITABLE data table.

The mapping is a property of the KiCad importer/exporter (not of the IR),
so it lives in data, not code. Two files in babel/data/:
  kicad_layers.tsv            the WORKING table the code reads
  kicad_layers.reference.tsv  the pristine REFERENCE (example + fallback)

The code operates ONLY on the working table; the reference is consulted
only if the working table is missing or unparseable (so a badly-edited
working copy degrades to the shipped default instead of breaking).

Table semantics — see the header of the .tsv. Forward (IR->KiCad) uses
every row (paired "F." names auto-derive the "B." bottom side); reverse
(KiCad->IR) resolves each KiCad name to the FIRST row that names it, so
list the canonical layer first and any collapsing aliases after it — order
is the only thing that decides the reverse, no separate flag.
"""
from pathlib import Path

_DATA = Path(__file__).parent / 'data'
_WORKING = _DATA / 'kicad_layers.tsv'
_REFERENCE = _DATA / 'kicad_layers.reference.tsv'


def _parse(path):
    fwd, rev = {}, {}
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        ir = int(parts[0])
        name = parts[1]
        paired = name.startswith('F.')
        # forward: every row maps IR -> KiCad (paired names derive B. side)
        fwd[ir] = name
        if paired:
            fwd[-ir] = 'B.' + name[2:]
        # reverse: first row naming a KiCad layer wins (setdefault); later
        # rows with the same name are collapsing aliases, IR->KiCad only
        rev.setdefault(name, ir)
        if paired:
            rev.setdefault('B.' + name[2:], -ir)
    if not fwd:
        raise ValueError('empty layer table')
    return fwd, rev


def _load():
    for path in (_WORKING, _REFERENCE):
        if path.exists():
            try:
                return _parse(path)
            except (ValueError, IndexError, OSError):
                continue          # corrupt working copy -> try the reference
    raise RuntimeError('kicad_layers: no usable layer table in babel/data')


_FWD, _REV = _load()


def ir_to_kicad(n):
    """IR layer number (int) -> KiCad layer name, or None if not carried."""
    return _FWD.get(n)


def kicad_to_ir(name):
    """KiCad layer name -> IR layer number (int), or None if not carried."""
    return _REV.get(name)
