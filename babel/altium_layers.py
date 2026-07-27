"""Altium <-> IR layer projection — a USER-EDITABLE data table.

Same shape and same rationale as babel/kicad_layers.py (read its docstring
first): the mapping is a property of the Altium importer/exporter, not of
the IR, so it lives in data. Two files in babel/data/:
  altium_layers.tsv            the WORKING table the code reads
  altium_layers.reference.tsv  the pristine REFERENCE (example + fallback)

The ONE structural difference from the KiCad table is what a row is keyed
by. KiCad layer NAMES ("F.Fab") are stable across every project, so the
name is the key. Altium's mechanical layers are numbered 57..72 and their
meaning is assigned per project — Assembly sits on Mechanical 2 in one
design and Mechanical 13 in another — so the number carries no meaning and
must never be the key. Altium declares the meaning in the file itself
(`mechanical_layer_kinds`: layer id -> MechanicalLayerKind), so a row here
is keyed by that KIND, and the caller resolves id -> kind against the
library/board being read. Fixed layers (Top, TopOverlay, ...) have stable
ids and are keyed by name as usual.
"""
from pathlib import Path

_DATA = Path(__file__).parent / 'data'
_WORKING = _DATA / 'altium_layers.tsv'
_REFERENCE = _DATA / 'altium_layers.reference.tsv'


def _parse(path):
    fwd, rev = {}, {}
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        ir = int(parts[0])
        key = parts[1]
        paired = key.endswith('_TOP') or key == 'Top'
        fwd[ir] = key
        if paired:
            bottom = 'Bottom' if key == 'Top' else key[:-4] + '_BOTTOM'
            fwd[-ir] = bottom
        # reverse: first row naming a key wins (setdefault); later rows with
        # the same key are collapsing aliases, IR->Altium only
        rev.setdefault(key, ir)
        if paired:
            rev.setdefault('Bottom' if key == 'Top' else key[:-4] + '_BOTTOM', -ir)
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
    raise RuntimeError('altium_layers: no usable layer table in babel/data')


_FWD, _REV = _load()

# Fixed Altium PcbLayer ids -> the table key naming them. Mechanical layers
# (57..72) are deliberately absent: they resolve through the source's own
# declared MechanicalLayerKind, see the module docstring.
FIXED_LAYER_NAME = {
    1: 'Top', 32: 'Bottom',
    33: 'TopOverlay', 34: 'BottomOverlay',
    35: 'TopPaste', 36: 'BottomPaste',
    37: 'TopSolder', 38: 'BottomSolder',
    74: 'Top',            # Multi-Layer (TH pads span the whole stack)
}


def altium_to_ir(key):
    """Table key (fixed layer name or MechanicalLayerKind name) -> IR layer
    number, or None if the table does not carry it."""
    return _REV.get(key)


def ir_to_altium(n):
    """IR layer number -> table key, or None if not carried."""
    return _FWD.get(n)
