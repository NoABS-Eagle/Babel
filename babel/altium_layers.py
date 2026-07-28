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

from babel.ir_util import is_paired_layer, parse_layer

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
        # A key names a SIDE when it is a "..._TOP" kind or a fixed layer whose
        # name starts with Top (Top, TopOverlay, TopSolder, TopPaste) — the
        # opposite side is derived, so the table lists the top row only.
        if key.endswith('_TOP'):
            bottom = key[:-4] + '_BOTTOM'
        elif key.startswith('Top'):
            bottom = 'Bottom' + key[3:]
        else:
            bottom = None
        # BOTH directions take the FIRST row naming a thing (setdefault):
        # later rows with the same IR number are collapsing aliases (several
        # Altium kinds landing on one IR layer), and the first one is the
        # canonical spelling to write back out.
        fwd.setdefault(ir, key)
        rev.setdefault(key, ir)
        if bottom:
            fwd.setdefault(-ir, bottom)
            rev.setdefault(bottom, -ir)
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


# ---------------------------------------------------------------------------
# WRITING side: IR layer -> a concrete Altium layer id
#
# Reading resolves a mechanical layer's meaning from the KIND the source file
# declares. Writing has to do the inverse: pick a mechanical SLOT for each
# kind we need and declare that kind in the file we emit — so the file says
# what its own mechanical layers mean, and our own importer reads it back
# through the very same table. Slots are handed out in a deterministic order
# (sorted IR number), and the PcbLib and the PcbDoc are planned from the SAME
# input, so a footprint's mechanical layer means the same thing on the board.
# ---------------------------------------------------------------------------

# Fixed layers: table key -> Altium layer id (PcbLayer values, kept as plain
# ints so this module stays free of altium_monkey imports).
FIXED_LAYER_ID = {
    'Top': 1, 'Bottom': 32,
    'TopOverlay': 33, 'BottomOverlay': 34,
    'TopPaste': 35, 'BottomPaste': 36,
    'TopSolder': 37, 'BottomSolder': 38,
}

_MECH_FIRST, _MECH_LAST = 57, 72          # Mechanical 1..16


def layers_used(ir_root):
    """Every IR layer number that any drawable in the project sits on —
    footprints and board alike. Both exporters plan from this same set.

    A footprint layer WITH A SIDE is counted on both sides: the file stores
    the top-relative number, and it is the placement of the footprint on the
    bottom that negates it (ir_util.place_layer). Altium flips a bottom
    component's mechanical geometry through the layer PAIRS the file
    declares, so both slots have to exist for that to work at all.
    """
    used = set()
    for el in ir_root.iter():
        raw = el.get('layer') if hasattr(el, 'get') else None
        if raw is None:
            continue
        try:
            anti, n = parse_layer(raw)
        except (TypeError, ValueError):
            continue
        if not anti:
            used.add(n)
    return used | {-n for n in used if is_paired_layer(n)}


def plan(ir_layers):
    """{IR layer -> (altium_layer_id, MechanicalLayerKind name or None,
    display name)} plus the top/bottom slot pairs, as (plan, pairs).

    An IR layer with no row in the table is NOT dropped and NOT guessed onto
    a semantic layer (this module's docstring): it gets a plain mechanical
    slot named after itself, and the caller logs it.
    """
    out, pairs = {}, []
    by_key = {}
    next_slot = _MECH_FIRST
    for n in sorted(ir_layers, key=lambda v: (abs(v), v)):
        key = _FWD.get(n)
        if key in FIXED_LAYER_ID:
            out[n] = (FIXED_LAYER_ID[key], None, key)
            continue
        name = key.replace('_', ' ').title() if key else f'IR layer {n}'
        if name in by_key:                # an alias of a kind already placed
            out[n] = by_key[name]
            continue
        if next_slot > _MECH_LAST:
            out[n] = None                 # out of mechanical layers
            continue
        out[n] = by_key[name] = (next_slot, key, name)
        next_slot += 1
    # Pairing is a MECHANICAL-layer notion (it is what makes Altium flip a
    # bottom-placed footprint's mechanical geometry); the fixed layers already
    # know their own opposite side.
    for n, slot in list(out.items()):
        other = out.get(-n)
        if (n > 0 and slot is not None and other is not None
                and other != slot and slot[0] >= _MECH_FIRST):
            pairs.append((slot[0], other[0]))
    return out, pairs


def declare(target, layer_plan, pairs):
    """Write the plan into the file being built (AltiumPcbLib or
    PcbDocBuilder — both carry the same three setters)."""
    for slot in {s for s in layer_plan.values() if s is not None}:
        layer_id, kind, name = slot
        if layer_id < _MECH_FIRST:
            continue                       # fixed layer: nothing to declare
        target.set_mechanical_layer(layer_id, name=name, enabled=True)
        if kind:
            target.set_mechanical_layer_kind(layer_id, kind)
    for top, bottom in pairs:
        target.set_mechanical_layer_pair(top, bottom)
