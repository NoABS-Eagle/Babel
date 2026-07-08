"""Shared helpers for reading the Babel IR symbol pool / gate structure."""
import math
import re
from pathlib import Path

_MODEL3D_EXTS = ('.step', '.stp', '.wrl')
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*]')

# Module-port edge angles, KiCad sheet-pin convention: right=0, top=90,
# left=180, bottom=270 (matches kicad_project_exporter._SIDE_ANGLE, kept
# here so the port-rotation transform below has one home shared by exporter
# AND importer — decisions.md "KiCad: поворот/зеркало ЛИСТА": одна функция
# трансформации side/coord, не две копии математики).
_SIDE_ANGLE = {'right': 0, 'top': 90, 'left': 180, 'bottom': 270}
_ANGLE_SIDE = {v: k for k, v in _SIDE_ANGLE.items()}


def rotate_port_side(side, coord_um, rot_deg, mirror):
    """A module port's (side, coord) after its instance's own rot/mirror is
    applied — the SINGLE source of truth for how KiCad relays out a rotated/
    mirrored sheet's pins (used by kicad_project_exporter._emit_sheet when
    baking, and by kicad_project_parser canonization when recovering the
    rot/mirror of a diverging occurrence).

    Ground truth: testData/modtest.sch (8 module instances — every rot(0/90/
    180/270) x mirror(0/1) combination), exported un-rotated, then the user
    rotated/mirrored each sheet BY HAND in real KiCad 10 and the resulting
    side/coord of every port was read back. A pure rigid-body coordinate
    rotation of the port's local offset (eagle_exporter._rotate_vec style)
    does NOT reproduce this — two ports on the SAME source side can land on
    DIFFERENT final sides once mirrored, because each side's own coord axis
    isn't a rotationally-consistent tangent direction. Fit against all 32
    ground-truth points, not derived algebra (three-revisions lesson,
    decisions.md "ir_rot — литеральное копирование"):
      new_side  = angle_to_side[ (side_angle[side] + rot) % 360 ]        (mirror=0)
                = angle_to_side[ (180 - side_angle[side] - rot) % 360 ]  (mirror=1)
      new_coord = coord * (cos(rot) - sin(rot))                          (mirror=0)
                = coord * (cos(rot) + sin(rot))                          (mirror=1)
    (rot is always a multiple of 90, so cos/sin are always exactly -1/0/1.)
    """
    theta = _SIDE_ANGLE[side]
    rot = round(rot_deg) % 360
    c = round(math.cos(math.radians(rot)))
    s = round(math.sin(math.radians(rot)))
    if mirror:
        new_theta = (180 - theta - rot) % 360
        sign = c + s
    else:
        new_theta = (theta + rot) % 360
        sign = c - s
    return _ANGLE_SIDE[new_theta], sign * coord_um


# ---------------------------------------------------------------------------
# Board/footprint layer numbers (ir_schema.md "Плата (Board IR)")
#
# A layer is a SIGNED int. |n| < 100 = copper (top=1, bottom=-1, inner 2..
# always standalone); |n| >= 100 = non-copper. Sign = side for PAIRED layers;
# a layer with no opposite-sign counterpart is standalone (side-less). In
# FOOTPRINT space the sign is side-RELATIVE (+ = mount side, - = far side);
# on the board it becomes absolute through <element side=...>: flipping to
# bottom negates paired layers, leaves standalone ones alone, and mirrors
# geometry. The numeric values follow "Eagle + 100" for recognizability
# (121 ~ tPlace 21) but are defined by the IR, not by Eagle.
#
# Footprint geometry carries layer="N" directly on each element — there are
# no per-layer container tags. <smd>/<pad>/<hole> carry no layer at all
# (copper-by-construction / side-less drill).
# ---------------------------------------------------------------------------

LAYER_COPPER_TOP = 1      # mount side copper (footprint) / board top
LAYER_COPPER_BOTTOM = -1
LAYER_DIMENSION = 120     # board outline (Eagle Dimension 20); standalone
LAYER_SILK = 121          # tPlace 21 / F.SilkS
LAYER_NAMES = 125         # tNames 25 (>NAME placeholder home in Eagle)
LAYER_VALUES = 127        # tValues 27
LAYER_MASK = 129          # tStop 29 / F.Mask
LAYER_PASTE = 131         # tCream 31 / F.Paste
LAYER_COURTYARD = 139     # tKeepout 39 / F.CrtYd
LAYER_DRILLS = 144        # standalone
LAYER_HOLES = 145         # standalone
LAYER_MILLING = 146       # standalone
LAYER_DOCUMENT = 148      # standalone, side-less notes (Dwgs.User/Cmts.User)
LAYER_FAB = 151           # tDocu 51 / F.Fab — assembly drawing


def is_copper(layer_n):
    return abs(int(layer_n)) < 100


def parse_layer(s):
    """IR `layer` attribute string -> (anti: bool, n: int). Format: [!][-]N."""
    s = (s or '').strip()
    anti = s.startswith('!')
    return anti, int(s[1:] if anti else s)


def sanitize_filename(name):
    """Replace characters illegal in filenames (Windows-illegal set, the
    strictest of the formats we touch) with '_'.

    Footprint names are free text (Eagle/Altium/KiCad all allow '/', ':', etc.)
    but every consumer that turns one into an actual filename — .kicad_mod,
    sidecar 3D models — needs the same substitution, or a name written by one
    step silently fails to be found by another.
    """
    return _UNSAFE_FILENAME_CHARS.sub('_', name)


def clean_attr_name(s):
    """letter-space-letter → underscore; space adjacent to punctuation → remove.

    Component attribute/parameter names are free text in both Altium
    ("Library Ref") and KiCad ("Manufacturer Part Number") — this normalizes
    them into something usable as an IR attribute name before lowercasing.
    """
    s = re.sub(r'([A-Za-z0-9]) ([A-Za-z0-9])', r'\1_\2', s or '')
    return s.replace(' ', '')


def symbol_pool(root):
    """Return {symbol_name: <symbol> element} from the library-level pool."""
    syms_el = root.find('symbols')
    if syms_el is None:
        return {}
    return {s.get('name'): s for s in syms_el.findall('symbol')}


def component_gates(comp_el):
    """Resolve a component's gates.

    Returns a list of (gate_name, symbol_name) tuples:
      - single-mode component (has `symbol` attr): [(None, symbol_name)]
      - multi-mode component (has <gate> children): [(gate_name, symbol_name), ...]
    """
    sym_attr = comp_el.get('symbol')
    if sym_attr is not None:
        return [(None, sym_attr)]
    return [(g.get('name'), g.get('symbol')) for g in comp_el.findall('gate')]


def is_multi_gate(comp_el):
    """True if the component routes symbols through explicit <gate> elements."""
    return comp_el.get('symbol') is None and comp_el.find('gate') is not None


def resolve_model3d_file(fp_el, search_dir):
    """Find a footprint's sidecar 3D model file inside search_dir, by name.

    Per ir_schema.md, the IR does not store the model filename — it's derived
    from the footprint name (`<footprint name="...">`), tried against the
    formats we support, in order. Returns a Path, or None if there's no
    <model3d> or no matching file.

    Tries the raw footprint name first (matches files written under it, e.g.
    by a hand-curated sidecar dir or eagle_parser), then the sanitized name
    (matches files written by a parser that already had to sanitize, e.g.
    altium_parser — see sanitize_filename).
    """
    if fp_el.find('model3d') is None:
        return None
    search_dir = Path(search_dir)
    fp_name = fp_el.get('name', '')
    names = [fp_name]
    safe = sanitize_filename(fp_name)
    if safe != fp_name:
        names.append(safe)
    for name in names:
        for ext in _MODEL3D_EXTS:
            p = search_dir / f'{name}{ext}'
            if p.exists():
                return p
    return None
