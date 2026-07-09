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
# Arc math (ir_schema.md "<arc>"): the CANONICAL arc form is ENDPOINTS +
# curve angle (x1 y1 x2 y2 curve, degrees CCW+, positive = center to the
# LEFT of the chord) — Eagle's own form, chosen deliberately: copper
# connectivity is a contract of ENDPOINTS, and in endpoint form every
# segment lives on the same integer-µm lattice, so joints are exact BY
# CONSTRUCTION under any quantization. The center form (cx/cy/r/start/
# sweep) stores the construction process, not the result: re-deriving
# endpoints from a µm-rounded center gave ~1 µm wobble and Eagle drew
# ratsnest stubs at every arc<->wire joint (luminoso ground truth).
# Center math lives HERE, for the consumers that need it (Altium's native
# form, SVG bounds) — derived, never stored.
# ---------------------------------------------------------------------------

def arc_center(x1, y1, x2, y2, curve_deg):
    """(cx, cy, r) of the arc, or None for a degenerate chord."""
    dx, dy = x2 - x1, y2 - y1
    chord = math.hypot(dx, dy)
    if chord < 1e-10 or not curve_deg:
        return None
    a = math.radians(abs(curve_deg))
    r = chord / (2 * math.sin(a / 2))
    d = r * math.cos(a / 2)
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    sign = 1 if curve_deg > 0 else -1
    return mx + sign * d * (-dy / chord), my + sign * d * (dx / chord), r


def arc_params(x1, y1, x2, y2, curve_deg):
    """Endpoint form -> (cx, cy, r, start_deg, sweep_deg) center form for
    consumers that need it; None for a degenerate arc."""
    c = arc_center(x1, y1, x2, y2, curve_deg)
    if c is None:
        return None
    cx, cy, r = c
    start = math.degrees(math.atan2(y1 - cy, x1 - cx))
    return cx, cy, r, start, curve_deg


def arc_mid(x1, y1, x2, y2, curve_deg):
    """The arc's midpoint (KiCad's 3-point form wants one). Degenerate
    chord -> the chord middle."""
    p = arc_params(x1, y1, x2, y2, curve_deg)
    if p is None:
        return (x1 + x2) / 2, (y1 + y2) / 2
    cx, cy, r, start, sweep = p
    m = math.radians(start + sweep / 2)
    return cx + r * math.cos(m), cy + r * math.sin(m)


def arc_endpoints(cx, cy, r, start_deg, sweep_deg):
    """Center form -> (x1, y1, x2, y2, curve) endpoint form — for center-
    form SOURCES (Altium). Full circles (|sweep| >= 360) are NOT arcs in
    IR — represent them as a circle <shape>; callers must special-case."""
    sr, er = math.radians(start_deg), math.radians(start_deg + sweep_deg)
    return (cx + r * math.cos(sr), cy + r * math.sin(sr),
            cx + r * math.cos(er), cy + r * math.sin(er), sweep_deg)


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
# NOTE: there is no separate milling layer — EVERY through-cut (board
# outline, cutout, slot of any shape) lives on LAYER_DIMENSION 120; the
# geometry states the final material boundary (width>0 = removed stroke,
# width=0 = cut along the path). Eagle's 20/46 split dissolves at import
# (46 -> 120 + a copy on 147, see eagle_parser).
LAYER_PLATING = 147       # standalone; marker overlay: cut walls are plated
                          # where covered by this layer's filled stroke
                          # (ir_schema.md "Резы и металлизация"). Participates
                          # in DRC/pour recompute: covered cut stretches are
                          # clearance-exempt — plating must grow into copper;
                          # bare cuts on 120 repel copper by default.
LAYER_DOCUMENT = 148      # standalone, side-less notes (Dwgs.User/Cmts.User)
LAYER_FAB = 151           # tDocu 51 / F.Fab — assembly drawing

# Derived DISPLAY CHANNELS (ir_schema.md "Производные каналы отображения",
# decisions.md 2026-07-09): renderer/editor-only layer numbers whose
# membership is decided by object TYPE, never by a stored attribute — these
# numbers are ILLEGAL in .swprj files (|n| >= 900 reserved). <via>/<pad>
# span the whole stack, airwires aren't stored at all; the channel exists so
# the editor's layer machinery (visibility/color) can still address them.
CHANNEL_VIAS = 901        # rings/barrels of all <via>
CHANNEL_PADS = 902        # TH pad copper (<pad>)
CHANNEL_ORIGINS = 903     # <element> grab points; paired: -903 = bottom side
CHANNEL_AIRWIRES = 904    # ratsnest (contactref minus routed copper)
CHANNEL_DRILLS = 905      # ALL drill renderings (pad/via/hole `drill` attrs).
                          # ONE channel, not Eagle's 44/45 pair: the real
                          # PTH/NPTH boundary lives in the object TYPE
                          # (pad/via = plated, hole = not) and resurfaces at
                          # CAM export; a view channel carries no semantics.
# 906+ reserved (DRC/ERC markers etc. — when they exist)


# |n| values that form canonical top/bottom PAIRS (ir_schema.md "Слои
# футпринта <-> слои платы": bottom placement negates paired layers, leaves
# standalone ones alone). The real rule consults the layout's OWN layer
# table (a pair = both signs declared); this set covers the canonical
# numbers until layouts grow explicit <layer> declarations.
_CANON_PAIRED = {1, 121, 125, 127, 129, 131, 139, 151}


def place_layer(ln, bottom):
    """Footprint-space layer attr (side-relative sign) -> board-space layer
    attr for an element on the given side. Sign arithmetic per ir_schema.md:
    top passes through; bottom negates paired layers, standalone unchanged."""
    if not bottom or not ln:
        return ln
    anti, n = parse_layer(ln)
    if abs(n) in _CANON_PAIRED:
        n = -n
    return ('!' if anti else '') + str(n)


def place_ir_element(el, ex_um, ey_um, rot_deg, bottom):
    """One footprint-space IR element -> a transformed deep copy in board
    space for a placement at (ex_um, ey_um µm), rot_deg CCW (top view),
    side bottom = mirror about the Y axis THEN rotate (ir_schema.md
    <element>). The single home of placement math — renderer today, board
    exporters that must bake placements tomorrow."""
    import copy
    c = copy.deepcopy(el)
    a = math.radians(rot_deg)
    cos_a, sin_a = math.cos(a), math.sin(a)

    def pt(x, y):
        if bottom:
            x = -x
        return (ex_um + x * cos_a - y * sin_a, ey_um + x * sin_a + y * cos_a)

    def set_pt(el_, kx, ky):
        if el_.get(kx) is None:
            return
        x, y = pt(float(el_.get(kx)), float(el_.get(ky)))
        el_.set(kx, str(round(x))); el_.set(ky, str(round(y)))

    def obj_rot(theta):
        # M . R(theta) = R(-theta) . M  =>  net object angle rot -/+ theta
        return (rot_deg - theta) % 360 if bottom else (rot_deg + theta) % 360

    t = c.tag
    if t == 'line':
        set_pt(c, 'x1', 'y1'); set_pt(c, 'x2', 'y2')
    elif t == 'arc':
        # endpoint form: endpoints move like line ends, mirror flips the
        # bulge side (curve sign) — no angle bookkeeping at all
        set_pt(c, 'x1', 'y1'); set_pt(c, 'x2', 'y2')
        if bottom:
            c.set('curve', f'{-float(c.get("curve", 0)):g}')
    elif t == 'polygon':
        for v in c.findall('vertex'):
            set_pt(v, 'x', 'y')
    else:   # shape / text / smd / pad / hole — point + own rotation
        set_pt(c, 'x', 'y')
        if c.get('rot') is not None or t in ('shape', 'text', 'smd'):
            c.set('rot', f'{obj_rot(float(c.get("rot", 0))):g}')
    if c.get('layer') is not None:
        c.set('layer', place_layer(c.get('layer'), bottom))
    elif t == 'smd':
        c.set('layer', place_layer('1', bottom))
    return c


def place_footprint(fp_el, ex_um, ey_um, rot_deg, bottom, skip_texts=()):
    """All drawable children of a footprint, placed. skip_texts: placeholder
    strings ('>NAME', ...) overridden or hidden by the element's own <text>
    children — the footprint's copy is not emitted."""
    out = []
    for el in fp_el:
        if el.tag in ('description', 'model3d', 'pin-mapping'):
            continue
        if el.tag == 'text' and (el.text or '').strip() in skip_texts:
            continue
        out.append(place_ir_element(el, ex_um, ey_um, rot_deg, bottom))
    return out


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
