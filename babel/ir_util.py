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
# (46 -> 120 + a copy on 146, see eagle_parser).
LAYER_PLATING = 146       # standalone; marker overlay: cut walls are plated
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


def chain_loops(segments, tol_um=1.0):
    """[(x1, y1, x2, y2, curve), ...] -> ([closed loop, ...], [leftover, ...]).

    Walks endpoint to endpoint; a segment traversed backwards has its curve
    negated (the endpoint form's own inverse). Each loop comes back wound
    CCW so the output is a property of the geometry, not of the source's
    order. The one home of contour assembly: the board outline needs exactly
    one loop, a footprint's milling contour may bring several.
    """
    def near(ax, ay, bx, by):
        return math.hypot(ax - bx, ay - by) <= tol_um

    remaining = list(segments)
    loops, leftover = [], []
    while remaining:
        loop = [remaining.pop(0)]
        while True:
            cx, cy = loop[-1][2], loop[-1][3]
            if near(cx, cy, loop[0][0], loop[0][1]):
                break                                   # closed
            for i, s in enumerate(remaining):
                if near(cx, cy, s[0], s[1]):
                    loop.append(remaining.pop(i))
                    break
                if near(cx, cy, s[2], s[3]):
                    x1, y1, x2, y2, c = remaining.pop(i)
                    loop.append((x2, y2, x1, y1, -c))
                    break
            else:
                leftover.extend(loop)                   # never closed
                loop = None
                break
        if loop is None:
            continue
        if contour_area([(x1, y1, c) for x1, y1, _, _, c in loop]) < 0:
            loop = [(x2, y2, x1, y1, -c) for x1, y1, x2, y2, c in reversed(loop)]
        loops.append(loop)
    return loops, leftover


def flatten_loop(loop, sag_um=6.0):
    """Closed loop of (x1, y1, x2, y2, curve) -> [(x, y), ...] vertices, arcs
    approximated so the chord never departs from the true arc by more than
    sag_um. For consumers whose contour is polygonal only (an Altium region)."""
    pts = []
    for x1, y1, x2, y2, curve in loop:
        pts.append((x1, y1))
        p = arc_params(x1, y1, x2, y2, curve) if curve else None
        if p is None:
            continue
        cx, cy, r, start, sweep = p
        # sagitta of one step: r*(1 - cos(step/2)) <= sag  ->  step
        if r <= sag_um:
            continue
        step = 2 * math.degrees(math.acos(max(-1.0, 1 - sag_um / r)))
        n = max(1, int(math.ceil(abs(sweep) / max(step, 1e-6))))
        for i in range(1, n):
            a = math.radians(start + sweep * i / n)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def is_paired_layer(n):
    """Does this IR layer number have a top/bottom PAIR (so a bottom-side
    placement negates it)? The one home of that fact — place_layer below and
    the Altium layer planner both ask here."""
    return abs(int(n)) in _CANON_PAIRED


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
        if t == 'text' and bottom:
            # reading-direction flips with the side
            c.set('mirror', '' if c.get('mirror') == '1' else '1')
            if c.get('mirror') == '':
                del c.attrib['mirror']
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
        if el.tag in ('description', 'model3d', 'pin-mapping', 'attributes'):
            continue
        if el.tag == 'text' and (el.text or '').strip() in skip_texts:
            continue
        out.append(place_ir_element(el, ex_um, ey_um, rot_deg, bottom))
    return out


def instance_footprint(comp_el, inst_el):
    """The <footprint> variant an instance is placed as, or None.

    `footprint=` on the instance is recorded only for 2+-variant components
    (ir_schema.md "Component instance") and may hold either the footprint
    NAME or its VARIANT string (the unique-by-construction fallback when one
    package backs several devices) — both are accepted here.
    """
    fps = comp_el.findall('footprint')
    if not fps:
        return None
    if len(fps) == 1:
        return fps[0]
    key = inst_el.get('footprint') if inst_el is not None else None
    if key:
        for fp in fps:
            if fp.get('name') == key or fp.get('variant') == key:
                return fp
    return fps[0]


def resolved_attrs(comp_el, inst_el=None, fp_el=None):
    """{name: value} по канону разрешения ir_schema.md "ФОРМАЛЬНАЯ МОДЕЛЬ"
    (I9): инстанс > вариант > компонент. The component carries the family
    SCHEMA (every key, empty value = declaration, I10) plus family-wide
    facts; the variant carries this device's values; the instance overrides
    both. A variant-level empty value never shadows anything (values live
    on the variant, the schema lives on the component)."""
    attrs = {}
    attrs_el = comp_el.find('attributes')
    if attrs_el is not None:
        for a in attrs_el.findall('attr'):
            attrs[a.get('name')] = a.get('value', '')
    if fp_el is None and inst_el is not None:
        fp_el = instance_footprint(comp_el, inst_el)
    if fp_el is not None:
        fa = fp_el.find('attributes')
        if fa is not None:
            for a in fa.findall('attr'):
                if a.get('value', ''):
                    attrs[a.get('name')] = a.get('value')
    if inst_el is not None:
        for a in inst_el.findall('attr'):
            attrs[a.get('name')] = a.get('value', '')
    return attrs


def is_copper(layer_n):
    return abs(int(layer_n)) < 100


def parse_layer(s):
    """IR `layer` attribute string -> (anti: bool, n: int). Format: [!][-]N."""
    s = (s or '').strip()
    anti = s.startswith('!')
    return anti, int(s[1:] if anti else s)


# ---------------------------------------------------------------------------
# Arc-native contour offset (decisions.md "Заливка полигонов: МОДЕЛЬ ПЕРА").
# The ONE vector operation Babel needs: the stored pour contour is the pen's
# CENTERLINE; KiCad's zone outline is a hard copper boundary, so exporting a
# pour means offsetting the contour OUTWARD by width/2. Arcs stay arcs (the
# segment+arc class is closed under offsetting: edge offsets are edges, round
# pen joins are arcs). Degenerate cases hard-reject — no silent approximation.
# ---------------------------------------------------------------------------

_OFF_EPS = 1e-6


def _edge_tangents(e):
    """Unit tangents at the start and end of an edge ('line'/'arc' tuple)."""
    kind, x1, y1, x2, y2, curve = e
    if kind == 'line' or not curve:
        dx, dy = x2 - x1, y2 - y1
        l = math.hypot(dx, dy) or 1.0
        t = (dx / l, dy / l)
        return t, t
    cx, cy, r = arc_center(x1, y1, x2, y2, curve)
    s = 1.0 if curve > 0 else -1.0
    # tangent = radius vector rotated +90 (CCW arc) / -90 (CW arc)
    def tang(px, py):
        rx, ry = (px - cx) / r, (py - cy) / r
        return (-s * ry, s * rx)
    return tang(x1, y1), tang(x2, y2)


def _edge_len(e):
    """Arc length of an edge (line: chord; arc: r·sweep)."""
    kind, x1, y1, x2, y2, curve = e
    if kind == 'arc' and curve:
        c = arc_center(x1, y1, x2, y2, curve)
        if c is not None:
            return c[2] * math.radians(abs(curve))
    return math.hypot(x2 - x1, y2 - y1)


def _offset_edge(e, r):
    """One edge offset to the RIGHT of travel by r (CCW contour => outward).
    Raises ValueError when a concave arc's radius collapses."""
    kind, x1, y1, x2, y2, curve = e
    if kind == 'line' or not curve:
        dx, dy = x2 - x1, y2 - y1
        l = math.hypot(dx, dy)
        if l < _OFF_EPS:
            return None
        nx, ny = dy / l, -dx / l
        return ('line', x1 + nx * r, y1 + ny * r, x2 + nx * r, y2 + ny * r, 0.0)
    cx, cy, radius = arc_center(x1, y1, x2, y2, curve)
    # CCW arc (curve>0): center on the LEFT of travel -> right offset grows
    # the radius; CW arc: center on the right -> radius shrinks
    r2 = radius + r if curve > 0 else radius - r
    if r2 <= _OFF_EPS:
        raise ValueError(
            f'contour offset: concave arc radius {radius:.1f} <= pen '
            f'radius {r:.1f} — degenerate result')
    def scale(px, py):
        return cx + (px - cx) * r2 / radius, cy + (py - cy) * r2 / radius
    ox1, oy1 = scale(x1, y1)
    ox2, oy2 = scale(x2, y2)
    return ('arc', ox1, oy1, ox2, oy2, curve)


def _edge_geo(e):
    """('line', p1, p2) or ('arc', center, R, a_start_deg, sweep_deg, p1, p2)."""
    kind, x1, y1, x2, y2, curve = e
    if kind == 'line' or not curve:
        return ('line', (x1, y1), (x2, y2))
    cx, cy, r = arc_center(x1, y1, x2, y2, curve)
    a1 = math.degrees(math.atan2(y1 - cy, x1 - cx))
    return ('arc', (cx, cy), r, a1, curve, (x1, y1), (x2, y2))


def _on_arc(geo, px, py):
    _, (cx, cy), r, a1, sweep, _, _ = geo
    a = math.degrees(math.atan2(py - cy, px - cx))
    d = (a - a1) % 360 if sweep > 0 else (a1 - a) % 360
    return d <= abs(sweep) + 1e-7


def _on_line(geo, px, py):
    _, (x1, y1), (x2, y2) = geo
    dx, dy = x2 - x1, y2 - y1
    l2 = dx * dx + dy * dy
    if l2 < _OFF_EPS:
        return False
    t = ((px - x1) * dx + (py - y1) * dy) / l2
    return -1e-9 <= t <= 1 + 1e-9 and \
        abs((px - x1) * dy - (py - y1) * dx) / math.sqrt(l2) < 1e-3


def _edge_intersections(ea, eb):
    """Intersection points of two edges (each in endpoint-tuple form),
    restricted to both segments/arc spans."""
    ga, gb = _edge_geo(ea), _edge_geo(eb)
    pts = []
    if ga[0] == 'line' and gb[0] == 'line':
        (x1, y1), (x2, y2) = ga[1], ga[2]
        (x3, y3), (x4, y4) = gb[1], gb[2]
        den = (x2 - x1) * (y4 - y3) - (y2 - y1) * (x4 - x3)
        if abs(den) > _OFF_EPS:
            t = ((x3 - x1) * (y4 - y3) - (y3 - y1) * (x4 - x3)) / den
            pts.append((x1 + t * (x2 - x1), y1 + t * (y2 - y1)))
    elif ga[0] == 'line' or gb[0] == 'line':
        line, arc = (ga, gb) if ga[0] == 'line' else (gb, ga)
        (x1, y1), (x2, y2) = line[1], line[2]
        (cx, cy), r = arc[1], arc[2]
        dx, dy = x2 - x1, y2 - y1
        fx, fy = x1 - cx, y1 - cy
        a = dx * dx + dy * dy
        b = 2 * (fx * dx + fy * dy)
        c = fx * fx + fy * fy - r * r
        disc = b * b - 4 * a * c
        if a > _OFF_EPS and disc >= 0:
            sq = math.sqrt(disc)
            for t in ((-b - sq) / (2 * a), (-b + sq) / (2 * a)):
                pts.append((x1 + t * dx, y1 + t * dy))
    else:
        (c1x, c1y), r1 = ga[1], ga[2]
        (c2x, c2y), r2 = gb[1], gb[2]
        d = math.hypot(c2x - c1x, c2y - c1y)
        if d > _OFF_EPS and abs(r1 - r2) - 1e-9 <= d <= r1 + r2 + 1e-9:
            a = (r1 * r1 - r2 * r2 + d * d) / (2 * d)
            h2 = r1 * r1 - a * a
            h = math.sqrt(max(h2, 0.0))
            mx = c1x + a * (c2x - c1x) / d
            my = c1y + a * (c2y - c1y) / d
            ux, uy = (c2y - c1y) / d, -(c2x - c1x) / d
            pts.append((mx + h * ux, my + h * uy))
            if h > _OFF_EPS:
                pts.append((mx - h * ux, my - h * uy))
    ok = []
    for px, py in pts:
        on_a = _on_line(ga, px, py) if ga[0] == 'line' else _on_arc(ga, px, py)
        on_b = _on_line(gb, px, py) if gb[0] == 'line' else _on_arc(gb, px, py)
        if on_a and on_b:
            ok.append((px, py))
    return ok


def _trim_edge(e, px, py, at_end):
    """Move an edge's end (at_end) or start to (px, py); arcs re-derive
    their sweep toward the kept endpoint."""
    kind, x1, y1, x2, y2, curve = e
    if kind == 'line' or not curve:
        return ('line', x1, y1, px, py, 0.0) if at_end \
            else ('line', px, py, x2, y2, 0.0)
    cx, cy, r = arc_center(x1, y1, x2, y2, curve)
    a1 = math.degrees(math.atan2(y1 - cy, x1 - cx))
    a2 = math.degrees(math.atan2(y2 - cy, x2 - cx))
    ap = math.degrees(math.atan2(py - cy, px - cx))
    if at_end:
        sweep = (ap - a1) % 360 if curve > 0 else -((a1 - ap) % 360)
        return ('arc', x1, y1, px, py, sweep)
    sweep = (a2 - ap) % 360 if curve > 0 else -((ap - a2) % 360)
    return ('arc', px, py, x2, y2, sweep)


def contour_area(vertices):
    """Signed area (CCW positive) of a closed contour [(x, y, curve), ...] —
    shoelace over chords plus circular-segment corrections for arc edges."""
    area = 0.0
    n = len(vertices)
    for i, (x1, y1, curve) in enumerate(vertices):
        x2, y2, _ = vertices[(i + 1) % n]
        area += (x1 * y2 - x2 * y1) / 2
        if curve:
            c = arc_center(x1, y1, x2, y2, curve)
            if c is not None:
                r = c[2]
                a = math.radians(abs(curve))
                seg = r * r / 2 * (a - math.sin(a))
                area += seg if curve > 0 else -seg
    return area


def offset_contour(vertices, r):
    """Closed contour [(x_um, y_um, curve_deg_to_next), ...] offset OUTWARD
    by r (µm), round joins — the pen model's copper boundary. Returns edges
    [('line', x1, y1, x2, y2, 0) | ('arc', x1, y1, x2, y2, curve)], CCW.
    Raises ValueError on degenerate results (collapsed concave arc, failed
    concave trim, self-intersecting outcome) — hard reject, never a guess."""
    if len(vertices) < 3:
        raise ValueError('contour offset: fewer than 3 vertices')
    if contour_area(vertices) < 0:
        n = len(vertices)
        vertices = [(vertices[(i + 1) % n][0], vertices[(i + 1) % n][1],
                     -vertices[i][2] if vertices[i][2] else 0.0)
                    for i in range(n - 1, -1, -1)]
    edges = []
    n = len(vertices)
    for i, (x1, y1, curve) in enumerate(vertices):
        x2, y2, _ = vertices[(i + 1) % n]
        if math.hypot(x2 - x1, y2 - y1) < _OFF_EPS:
            continue
        edges.append(('arc' if curve else 'line', x1, y1, x2, y2,
                      float(curve or 0.0)))
    # Corner resolution with EDGE CONSUMPTION: a centerline edge shorter
    # than the local concave overlap is swallowed whole by the pen (Komar
    # PHASE_B_GND: a 53 µm closing sliver under a 100 µm pen radius) — the
    # true boundary simply doesn't contain its image, exactly how Eagle
    # renders it. When a concave trim finds no intersection, the shorter
    # of the two offset edges is dropped and the pass restarts against
    # pristine offsets; each restart removes one edge, so this terminates.
    # Deterministic geometry of the Minkowski boundary, not a guess — a
    # genuinely degenerate contour still fails below (< 3 edges left, or
    # the global self-intersection check).
    while True:
        off = []
        for e in edges:
            oe = _offset_edge(e, r)
            if oe is not None:
                off.append((e, oe))
        if len(off) < 3:
            raise ValueError(
                'contour offset: contour degenerates under this pen '
                '(fewer than 3 edges survive)')
        consumed = None
        out = []
        m = len(off)
        for i in range(m):
            (e1, o1), (e2, o2) = off[i], off[(i + 1) % m]
            out.append(i)
            p_end = (o1[3], o1[4])
            p_start = (o2[1], o2[2])
            gap = math.hypot(p_start[0] - p_end[0], p_start[1] - p_end[1])
            if gap < 1e-3:
                continue
            t1 = _edge_tangents(e1)[1]
            t2 = _edge_tangents(e2)[0]
            cross = t1[0] * t2[1] - t1[1] * t2[0]
            vx, vy = e1[3], e1[4]           # the original corner
            if cross > 1e-9:
                # convex (left turn on a CCW contour): round pen join around V
                a1 = math.degrees(math.atan2(p_end[1] - vy, p_end[0] - vx))
                a2 = math.degrees(math.atan2(p_start[1] - vy, p_start[0] - vx))
                sweep = (a2 - a1) % 360
                out.append(('join', p_end, p_start, sweep))
            else:
                # concave: offset edges overlap — trim both to intersection
                xs = _edge_intersections(o1, o2)
                if not xs:
                    consumed = e1 if _edge_len(o1) < _edge_len(o2) else e2
                    break
                px, py = min(xs, key=lambda p: math.hypot(p[0] - vx, p[1] - vy))
                off[i] = (e1, _trim_edge(o1, px, py, at_end=True))
                off[(i + 1) % m] = (e2, _trim_edge(o2, px, py, at_end=False))
        if consumed is None:
            break
        edges = [e for e in edges if e is not consumed]
    result = []
    for item in out:
        if isinstance(item, tuple):
            _, (px1, py1), (px2, py2), sweep = item
            if sweep > 1e-6:
                result.append(('arc', px1, py1, px2, py2, sweep))
        else:
            kind, x1, y1, x2, y2, curve = off[item][1]
            if math.hypot(x2 - x1, y2 - y1) > 1e-3 or abs(curve) > 1e-6:
                result.append((kind, x1, y1, x2, y2, curve))
    # global self-intersection: any two non-adjacent edges crossing = reject
    k = len(result)
    for i in range(k):
        for j in range(i + 2, k):
            if i == 0 and j == k - 1:
                continue
            if _edge_intersections(result[i], result[j]):
                raise ValueError(
                    'contour offset: result self-intersects (narrow neck '
                    'thinner than the pen) — hard reject')
    return result


def _edge_circle(e):
    """(cx, cy, r) of an arc edge's full circle, or None for a line."""
    kind, x1, y1, x2, y2, curve = e
    if kind == 'arc' and curve:
        return arc_center(x1, y1, x2, y2, curve)
    return None


def _unbounded_intersections(e1, e2):
    """Intersection points of two edges' UNBOUNDED carriers (infinite lines /
    full circles). The inverse-offset miter needs this: the boundary edges
    were TRIMMED by the forward offset, so their inward images are shorter
    than the centerline edges and the true vertex lies beyond the segment
    ends — segment-bounded intersection can't see it."""
    c1, c2 = _edge_circle(e1), _edge_circle(e2)
    if c1 is None and c2 is None:
        x1, y1, x2, y2 = e1[1:5]
        x3, y3, x4, y4 = e2[1:5]
        d1x, d1y = x2 - x1, y2 - y1
        d2x, d2y = x4 - x3, y4 - y3
        den = d1x * d2y - d1y * d2x
        if abs(den) < 1e-12:
            return []
        t = ((x3 - x1) * d2y - (y3 - y1) * d2x) / den
        return [(x1 + d1x * t, y1 + d1y * t)]
    if c1 is not None and c2 is not None:
        (ax, ay, ar), (bx, by, br) = c1, c2
        d = math.hypot(bx - ax, by - ay)
        if d < 1e-9:
            return []
        a = (ar * ar - br * br + d * d) / (2 * d)
        h2 = ar * ar - a * a
        if h2 < -1e-6:
            return []
        h = math.sqrt(max(h2, 0.0))
        mx, my = ax + a * (bx - ax) / d, ay + a * (by - ay) / d
        ux, uy = -(by - ay) / d, (bx - ax) / d
        return [(mx + h * ux, my + h * uy), (mx - h * ux, my - h * uy)]
    if c1 is None:
        line, (cx, cy, r) = e1, c2
    else:
        line, (cx, cy, r) = e2, c1
    x1, y1, x2, y2 = line[1:5]
    dx, dy = x2 - x1, y2 - y1
    l = math.hypot(dx, dy)
    if l < 1e-9:
        return []
    dx, dy = dx / l, dy / l
    t0 = (cx - x1) * dx + (cy - y1) * dy
    px, py = x1 + dx * t0, y1 + dy * t0
    h2 = r * r - ((px - cx) ** 2 + (py - cy) ** 2)
    if h2 < -1e-6:
        return []
    h = math.sqrt(max(h2, 0.0))
    return [(px + dx * h, py + dy * h), (px - dx * h, py - dy * h)]


def _edges_match(ea, eb, tol):
    """One boundary edge ≈ one rebuilt edge, all numerics within tol (arcs
    also compare sweep, in degrees against a fixed 0.1° allowance — angle
    is not a length, µm tolerance doesn't apply to it)."""
    if ea[0] != eb[0]:
        return False
    if any(abs(a - b) > tol for a, b in zip(ea[1:5], eb[1:5])):
        return False
    return abs((ea[5] or 0.0) - (eb[5] or 0.0)) <= 0.1


def unoffset_contour(edges, r, tol=2.5):
    """Inverse of offset_contour: a copper-boundary contour (KiCad zone
    outline) -> the pen CENTERLINE vertices [(x_um, y_um, curve_deg), ...],
    pen radius r (µm). Returns (vertices, exact).

    Structure mirrors the forward construction exactly (никаких эвристик —
    каждый шаг обратим по построению):
      - a CONVEX arc of radius == r (µm tolerance) is a round pen JOIN —
        it collapses back into a plain centerline vertex AT ITS CENTER
        (the forward pass put the join circle around that very vertex);
      - every other edge is a real centerline edge offset outward:
        _offset_edge(e, -r) shifts a line back along its own normal and
        restores an arc's radius around the SAME center (curve preserved);
      - vertices are re-established: through a join — the join center;
        between directly-adjacent edges — the intersection of the two
        inward carriers (un-trimming what the forward pass trimmed; косые
        углы обрабатываются пересечением, наклон ребра роли не играет).

    `exact` reports whether re-running offset_contour on the result
    reproduces the input contour within tol — True for our own exporter's
    output (self-check oracle), False for a hand-drawn zone whose sharp
    convex corners the pen necessarily rounds (physically equal to KiCad's
    own min_thickness-stroked fill, so the COPPER still matches; caller
    logs it). Degenerate input (necks thinner than the pen, collapsed
    arcs) raises ValueError — hard reject, never a guess."""
    if len(edges) < 1:
        raise ValueError('zone outline: empty contour')
    area_verts = [(e[1], e[2], e[5] or 0.0) for e in edges]
    if contour_area(area_verts) < 0:
        edges = [(e[0], e[3], e[4], e[1], e[2], -(e[5] or 0.0))
                 for e in reversed(edges)]
    n = len(edges)
    join_center = [None] * n
    for i, e in enumerate(edges):
        if e[0] == 'arc' and (e[5] or 0) > 0:
            c = arc_center(e[1], e[2], e[3], e[4], e[5])
            if c is not None and abs(c[2] - r) <= tol:
                join_center[i] = (c[0], c[1])
    segs = [i for i in range(n) if join_center[i] is None]
    if not segs:
        raise ValueError('zone outline: only pen-join arcs, no centerline '
                         'edges (bare pen dot/ring)')
    ins = {}
    for i in segs:
        o = _offset_edge(edges[i], -r)
        if o is not None:
            ins[i] = o
    segs = [i for i in segs if i in ins]
    if not segs:
        raise ValueError('zone outline: every centerline edge degenerated '
                         'under the inverse offset')

    m = len(segs)
    conn = []          # conn[k]: vertices between segs[k] and segs[(k+1)%m]
    for k in range(m):
        a, b = segs[k], segs[(k + 1) % m]
        between = []
        i = (a + 1) % n
        while i != b:
            if join_center[i] is not None:
                between.append(join_center[i])
            i = (i + 1) % n
        if between:
            conn.append(between)
            continue
        # direct miter: un-trim to the carriers' intersection nearest the
        # boundary corner (the shared endpoint the forward trim produced)
        corner = (edges[a][3], edges[a][4])
        xs = _unbounded_intersections(ins[a], ins[b])
        if xs:
            conn.append([min(xs, key=lambda p: math.hypot(p[0] - corner[0],
                                                          p[1] - corner[1]))])
            continue
        # collinear continuation: the two inward endpoints coincide
        pa, pb = (ins[a][3], ins[a][4]), (ins[b][1], ins[b][2])
        if math.hypot(pb[0] - pa[0], pb[1] - pa[1]) <= tol * 2:
            conn.append([((pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2)])
            continue
        raise ValueError('zone outline: adjacent edges have no carrier '
                         'intersection — not an offset image')

    out = []           # (x, y, curve-of-edge-leaving-this-vertex)
    for k in range(m):
        sv = conn[k - 1][-1]
        ev = conn[k][0]
        e = edges[segs[k]]
        curve = 0.0
        if e[0] == 'arc' and e[5]:
            cx, cy, _ = _edge_circle(ins[segs[k]])
            a1 = math.degrees(math.atan2(sv[1] - cy, sv[0] - cx))
            a2 = math.degrees(math.atan2(ev[1] - cy, ev[0] - cx))
            curve = ((a2 - a1) % 360) if e[5] > 0 else -((a1 - a2) % 360)
            # atan2 re-fit noise: snap to a whole degree, same convention
            # as kicad_board_parser._snap_sweep
            snapped = round(curve)
            if snapped and abs(curve - snapped) < 5e-3:
                curve = float(snapped)
        out.append((sv[0], sv[1], curve))
        for p in conn[k][:-1]:
            out.append((p[0], p[1], 0.0))

    verts = []
    for x, y, curve in out:
        xi, yi = round(x), round(y)
        if verts and (xi, yi) == (verts[-1][0], verts[-1][1]):
            continue
        verts.append((xi, yi, curve))
    if len(verts) > 1 and (verts[0][0], verts[0][1]) == (verts[-1][0], verts[-1][1]):
        verts.pop()
    if len(verts) < 3:
        raise ValueError('zone outline: centerline collapsed to fewer than '
                         '3 vertices')

    rebuilt = offset_contour(verts, r)     # ValueError propagates = reject
    rest = list(rebuilt)
    exact = len(rest) == len(edges)
    if exact:
        for ea in edges:
            for j, eb in enumerate(rest):
                if _edges_match(ea, eb, tol):
                    rest.pop(j)
                    break
            else:
                exact = False
                break
    return verts, exact


DEFAULT_STACK = '35[1530]35'


def parse_stack(s):
    """IR <layout stack=...> formula (ir_schema.md "Формула стека") ->
    (coppers, dielectrics). coppers = [µm, ...] top-down (index 0 = layer 1,
    last = layer -1, inner = 2..N-1); dielectrics = [(µm, ε|None, tanδ|None),
    ...] between them, len(coppers)-1 items. ε/tanδ are dimensionless
    decimals — the sanctioned exception to the integer-µm canon (not
    lengths). Grammar violations hard-reject: the sandwich must alternate
    copper[dielectric]copper..., ≥2 coppers (single-sided boards rejected
    until a real need — same door as via spans)."""
    s = (s or DEFAULT_STACK).strip()
    parts = re.split(r'\[([^\[\]]*)\]', s)
    coppers, dielectrics = [], []
    if len(parts) < 3 or len(parts) % 2 == 0:
        raise ValueError(f'stack {s!r}: expected copper[dielectric]copper...,'
                         f' >= 2 copper layers')
    for i, tok in enumerate(parts):
        tok = tok.strip()
        if i % 2 == 0:
            if not re.fullmatch(r'[1-9]\d*', tok):
                raise ValueError(
                    f'stack {s!r}: copper thickness {tok!r} must be a '
                    f'positive integer (µm)')
            coppers.append(int(tok))
        else:
            fields = [f.strip() for f in tok.split(':')]
            if not 1 <= len(fields) <= 3 or \
                    not re.fullmatch(r'[1-9]\d*', fields[0]):
                raise ValueError(
                    f'stack {s!r}: dielectric {tok!r} must be '
                    f'thickness_um[:eps[:tand]], thickness a positive '
                    f'integer (µm)')
            try:
                eps = float(fields[1]) if len(fields) > 1 else None
                tand = float(fields[2]) if len(fields) > 2 else None
            except ValueError:
                raise ValueError(f'stack {s!r}: dielectric {tok!r}: '
                                 f'eps/tand must be decimal numbers')
            dielectrics.append((int(fields[0]), eps, tand))
    return coppers, dielectrics


def format_stack(coppers, dielectrics):
    """Inverse of parse_stack."""
    if len(coppers) != len(dielectrics) + 1:
        raise ValueError(f'stack: {len(coppers)} coppers need '
                         f'{len(coppers) - 1} dielectrics, '
                         f'got {len(dielectrics)}')
    out = [str(coppers[0])]
    for (d_um, eps, tand), cu in zip(dielectrics, coppers[1:]):
        fields = [str(d_um)]
        if tand is not None and eps is None:
            raise ValueError('stack: tand without eps')
        if eps is not None:
            fields.append(f'{eps:g}')
        if tand is not None:
            fields.append(f'{tand:g}')
        out.append('[' + ':'.join(fields) + ']' + str(cu))
    return ''.join(out)


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


def instance_designator(canonical, minst_name, offset):
    """Real per-instance designator of a module part, given the module's
    CANONICAL designator (`R4`, `#GND1`), the module INSTANCE name (`TM1`)
    and its Eagle `offset=` (or None/'' /'0' when absent).

    Two standard Eagle namespacing schemes, one function so every exporter
    derives it identically (single source of truth — the board path's
    `eagle_board_exporter._resolve_instance` documents the same two spellings):

    - offset present → NUMERIC flatten: split the trailing number off the
      canonical designator and add the offset (`R4` @ 100 → `R104`,
      `#GND1` @ 200 → `#GND201`).
    - offset absent → COLON composite `INST:REFDES` (`TM1:R4`), the exact IR
      element-address canon (ir_schema.md "<element>").
    """
    if offset and offset != '0':
        m = re.match(r'^(.*?)(\d+)$', canonical)
        if m:
            return f'{m.group(1)}{int(m.group(2)) + int(offset)}'
    return f'{minst_name}:{canonical}'


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
    return model3d_file_for_name(fp_el.get('name', ''), search_dir)


def model3d_file_for_name(fp_name, search_dir):
    """The sidecar model file for a footprint NAME, without requiring the
    footprint to already declare <model3d> — which is how a model the user
    dropped in for a package Eagle never annotated gets noticed at all."""
    search_dir = Path(search_dir)
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
