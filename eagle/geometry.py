"""Low-level Eagle XML -> IR conversion shared by every Eagle parser: unit
conversion (units.md), angle/mirror strings, and layer numbering.

Two independent layer spaces, matching layer-model.md:

- Schematic space (symbols, schematic canvas) takes Eagle's own numbers
  as-is — the canon's 91-99 were chosen to match Eagle's, so no formula
  applies (conversion-eagle.md #слои: "У Eagle нумерация фиксированная").
- Footprint/board space applies the "+100" formula, with three
  exceptions (20+46 -> 120, 41/42 -> anti-1/-1) baked in from the
  project's own <layers> table (ground truth, not guesswork) plus a
  logged fallback for anything not in that table.

`layer_of(eagle_number) -> int | Layer` is schematic_layer (plain int,
passthrough) or a `functools.partial(footprint_layer, log=...)` (a
`Layer`, carrying side + anti). `_split` below normalizes either into the
(signed_number, anti) pair every graphics converter needs.
"""

from __future__ import annotations

import math
import re

from ir.graphics import Arc, Line, Polygon, Shape, Text, Vertex
from ir.units import Layer

import import_log

_ROT_RE = re.compile(r"^(M)?(S)?R?([\d.]+)$")


def safe_attr(name: str, value: str, log):
    """Eagle attribute name -> IR `Attr`, sanitizing an out-of-domain key
    (naming.md #имя-вне-домена-чинится-подстановкой) instead of rejecting
    the whole file over one stray character (e.g. Eagle's own `AEC-Q`)."""
    from ir.attr import Attr
    from ir.naming import sanitize_attr_key

    fixed = sanitize_attr_key(name)
    if fixed is None:
        return Attr(name, value)
    log(f"attribute key {name!r} out of domain -> {fixed!r}")
    return Attr(fixed, value)


def um(value: str) -> int:
    """Eagle mm string -> IR µm int — units.md #1 (round once, on input)."""
    return round(float(value) * 1000)


_DRU_LEN_RE = re.compile(r"^(-?[0-9]*\.?[0-9]+)(mil|mm|in|inch)$")


def dru_length(s: str) -> int:
    """Eagle `<param>` length ("6mil", "0.35mm") -> IR µm int. Design-rule
    values always carry an explicit unit, unlike ordinary coordinates."""
    m = _DRU_LEN_RE.match(s.strip())
    if not m:
        raise ValueError(f"malformed design-rule length: {s!r}")
    value, unit = float(m.group(1)), m.group(2)
    if unit == "mil":
        return round(value * 25.4)
    if unit in ("in", "inch"):
        return round(value * 25400)
    return round(value * 1000)


# Eagle's own defaults, for a library read with no board beside it.
_RESTRING_DEFAULTS = {
    "via_outer": (0.25, 203, 508),
    "via_inner": (0.25, 203, 508),
    "pad_outer": (0.25, 254, 508),
    "pad_inner": (0.25, 254, 508),
}
_RESTRING_KEYS = {
    "via_outer": ("rvViaOuter", "rlMinViaOuter", "rlMaxViaOuter"),
    "via_inner": ("rvViaInner", "rlMinViaInner", "rlMaxViaInner"),
    "pad_outer": ("rvPadTop", "rlMinPadTop", "rlMaxPadTop"),
    "pad_inner": ("rvPadInner", "rlMinPadInner", "rlMaxPadInner"),
}


class Restring:
    """Eagle's annular ring: **a fraction of the drill, clamped** — not a
    plain multiple of it.

    `diameter = drill + 2 * clamp(drill * ratio, min, max)`, and the clamp
    is what matters: at the usual `ratio = 0.25` it binds for every drill
    below four times the minimum, which is most of them. Reading the rule
    as `drill * 1.5` (ratio only) is wrong there — on `step4`, whose pad
    minimum is 254 µm, an 813 µm drill gives 1321 µm and the ratio-only
    form says 1220, a full 100 µm of missing copper per pad.

    **The inner ring is its own number** (via.md `inner_dia`, per-object —
    there is deliberately no board-wide setting), and the reason it
    usually differs is not the rules, which are identical inner and outer
    on all six test boards: it is that an explicit `diameter` on a
    `<via>`/`<pad>` sets the OUTER ring only, while the inner one is
    always computed. That is how a via drawn 0.5 mm wide sits 0.45 mm wide
    on the inner layers — 25 µm per side that a DRC will notice."""

    def __init__(self, params: dict[str, str] | None = None) -> None:
        self._rules = {}
        for kind, (rv, lo, hi) in _RESTRING_KEYS.items():
            d_ratio, d_lo, d_hi = _RESTRING_DEFAULTS[kind]
            p = params or {}
            self._rules[kind] = (
                float(p[rv]) if rv in p else d_ratio,
                dru_length(p[lo]) if lo in p else d_lo,
                dru_length(p[hi]) if hi in p else d_hi,
            )
        self.from_rules = params is not None

    def diameter(self, kind: str, drill: int) -> int:
        """units.md #1: round ONCE, and on the result. Rounding the ring
        first and doubling it afterwards moves the answer by a micron on
        every half-micron ring — the arithmetic has to end where the IR's
        integer does."""
        ratio, lo, hi = self._rules[kind]
        return round(drill + 2 * max(lo, min(hi, drill * ratio)))


def _unsigned_mdeg(value: str) -> int:
    return round(float(value) * 1000) % 360000


def _signed_mdeg(value: str) -> int:
    v = round(float(value) * 1000)
    return v % 360000 if v >= 0 else -((-v) % 360000)


# ------------------------------------------------- 3D model orientation
#
# **NoABS.Eagle3d's angles are NOT the IR's**, and the difference is a
# quarter turn of the FRAME: Eagle's 3D frame stands Y-up where MCAD (and
# so [model3d.md](../spec/model3d.md), and so Altium and KiCad) stands
# Z-up. Both spell a rotation as intrinsic `Rx·Ry·Rz`; only the frame
# differs, and the whole conversion is therefore one right-multiplication:
#
#     R_mcad = R_eagle · Rx(-90°)
#
# Ground truth is the previous Babel's `_eagle_to_mcad_rot`, reverse-
# engineered there from `(rx, ry, rz)` pairs read straight out of Altium's
# own 3D Body panel. Carrying the numbers over untouched — which is what
# the first version of this importer did — turns Eagle's usual `rx=90`
# ("model already upright") into a real 90° tilt, so EVERY model comes out
# lying on its side. It is invisible to a round-trip through Eagle alone,
# since both halves would be wrong the same way.
#
# The way back is the exact inverse, `R_eagle = R_mcad · Rx(+90°)`, and is
# written as such rather than derived a second time: the previous Babel
# derived it separately and got a function that is NOT the inverse of its
# own forward (3 of 8 sample triples fail to round-trip). One composition,
# one decomposition, two thin wrappers.


def _mul3(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _compose_xyz(rx: float, ry: float, rz: float) -> list[list[float]]:
    """Intrinsic `Rx·Ry·Rz`, degrees — model3d.md's own composition."""
    x, y, z = math.radians(rx), math.radians(ry), math.radians(rz)
    cx, sx = math.cos(x), math.sin(x)
    cy, sy = math.cos(y), math.sin(y)
    cz, sz = math.cos(z), math.sin(z)
    return _mul3(_mul3([[1, 0, 0], [0, cx, -sx], [0, sx, cx]],
                        [[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]]),
                  [[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])


def _decompose_xyz(m) -> tuple[float, float, float]:
    """The inverse of `_compose_xyz`: `m[0][2] = sin(ry)`."""
    ry = math.asin(max(-1.0, min(1.0, m[0][2])))
    if abs(math.cos(ry)) > 1e-9:
        rz = math.atan2(-m[0][1], m[0][0])
        rx = math.atan2(-m[1][2], m[2][2])
    else:                                   # gimbal lock: ry = ±90, rx taken as 0
        rz = math.atan2(m[1][0], m[1][1])
        rx = 0.0

    def clean(rad: float) -> float:
        v = math.degrees(rad) % 360
        if v > 180:
            v -= 360
        return 0.0 if abs(v) < 1e-9 else round(v, 6)

    return clean(rx), clean(ry), clean(rz)


_FRAME = [[1, 0, 0], [0, 0, 1], [0, -1, 0]]        # Rx(-90°)
_FRAME_BACK = [[1, 0, 0], [0, 0, -1], [0, 1, 0]]   # Rx(+90°)


def eagle_to_mcad_rot(rx: float, ry: float, rz: float) -> tuple[float, float, float]:
    return _decompose_xyz(_mul3(_compose_xyz(rx, ry, rz), _FRAME))


def mcad_to_eagle_rot(rx: float, ry: float, rz: float) -> tuple[float, float, float]:
    return _decompose_xyz(_mul3(_compose_xyz(rx, ry, rz), _FRAME_BACK))


def angle(rot: str | None) -> tuple[int, int]:
    """Eagle `rot="MR90"`-style string -> (mirror, rot_mdeg).

    `M` (mirror) and `S` (spin, free-angle text) are prefixes on an
    optional `R` + degrees. Absent altogether means Eagle's own DTD
    default `rot="R0"` — baked in explicitly per conversion.md
    #что-импортёр-дописывает-сам.
    """
    if not rot:
        return 0, 0
    m = _ROT_RE.match(rot)
    if not m:
        raise ValueError(f"malformed Eagle rot string: {rot!r}")
    mirror_flag, _spin_flag, degrees = m.groups()
    return (1 if mirror_flag else 0), _unsigned_mdeg(degrees)


# Explicit Eagle layer number -> IR Layer, board/footprint space.
# Ground-truth from testData/Eagle/tolmach/hardware/tolmach.brd <layers>.
_FOOTPRINT_LAYER_TABLE: dict[int, Layer] = {
    1: Layer(1, 1),
    16: Layer(1, -1),
    17: Layer(117, 1),
    18: Layer(118, 1),
    19: Layer(119, 1),
    20: Layer(120, 1),
    46: Layer(120, 1),
    41: Layer(1, 1, anti=True),
    42: Layer(1, -1, anti=True),
    43: Layer(143, 1),
}
# eagle_top -> (canon_number, eagle_bottom_partner) — every pair here is
# ground-truthed against testData/Eagle/tolmach/hardware/tolmach.brd's own
# <layers> table, not guessed.
_FOOTPRINT_PAIRS: dict[int, tuple[int, int]] = {
    21: (121, 22),   # tPlace / bPlace -> Top/Bottom Silk
    23: (123, 24),   # tOrigins / bOrigins -> Top/Bottom Origins
    25: (125, 26),   # tNames / bNames
    27: (127, 28),   # tValues / bValues
    29: (129, 30),   # tStop / bStop -> Top/Bottom Mask
    31: (131, 32),   # tCream / bCream -> Top/Bottom Paste
    33: (133, 34),   # tFinish / bFinish
    35: (135, 36),   # tGlue / bGlue
    37: (137, 38),   # tTest / bTest
    39: (139, 40),   # tKeepout / bKeepout -> Top/Bottom Courtyard
    51: (151, 52),   # tDocu / bDocu -> Top/Bottom Fab
    53: (153, 54),   # tGND_GNDA / bGND_GNDA
    57: (157, 58),   # tCAD / bCAD
}
_FOOTPRINT_STANDALONE_NAMED: dict[int, int] = {
    44: 144,  # Drills
    45: 145,  # Holes
    47: 147,  # Measures
    48: 148,  # Document
    49: 149,  # Reference
    50: 150,  # dxf
    56: 156,  # wert
    59: 159,  # Invisible
    60: 160,  # bCarbon — named "b" but has no top counterpart in Eagle's own set
    61: 161,  # stand
}


def footprint_layer(eagle_number: int, log) -> Layer:
    """Eagle board-space layer number -> IR `Layer` — conversion-eagle.md
    #слои. Falls back to `+100`, standalone (no side), for anything
    outside the verified table.

    Ground-truthed reversal: a real board can use `50 dxf` heavily (143
    and 214 occurrences in two of the five test projects) — that's the
    "+100" formula's own canon `150`. A DIFFERENT real board also has a
    literal custom Eagle layer numbered `150` ("Notes") — passing THAT
    through unchanged would collide with `50`'s already-claimed `150`,
    two unrelated Eagle numbers landing on one canon number. `+100`
    applied uniformly, regardless of whether the Eagle number is already
    >= 100, never collides — it's just N -> N+100, injective by
    construction — so a custom `150` becomes canon `250`, clear of
    everything the fixed tables claim (they top out at Eagle 61 -> canon
    161). No side is assigned (`side=1`, i.e. standalone): guessing a
    top/bottom pairing for a number with no evidence of one invents a
    fact this converter doesn't have — every genuine legacy PAIRED layer
    (tGlue/tTest/tCAD...) is already in `_FOOTPRINT_PAIRS`, so this
    fallback never needs to guess a pairing in practice."""
    if eagle_number in _FOOTPRINT_LAYER_TABLE:
        return _FOOTPRINT_LAYER_TABLE[eagle_number]
    for top, (canon, bottom) in _FOOTPRINT_PAIRS.items():
        if eagle_number == top:
            return Layer(canon, 1)
        if eagle_number == bottom:
            return Layer(canon, -1)
    if eagle_number in _FOOTPRINT_STANDALONE_NAMED:
        return Layer(_FOOTPRINT_STANDALONE_NAMED[eagle_number], 1)
    log(f"eagle layer {eagle_number}: not in the verified table, "
        f"using it standalone -> {eagle_number + 100}")
    return Layer(eagle_number + 100, 1)


def schematic_layer(eagle_number: int) -> int:
    """Schematic space: Eagle's own number, unchanged — layer-model.md
    #часть-1-слои-схемы."""
    return eagle_number


def _split(layer_result: int | Layer) -> tuple[int, bool]:
    if isinstance(layer_result, Layer):
        return layer_result.signed, layer_result.anti
    return layer_result, False


def convert_wire(el, layer_of) -> Line | Arc | None:
    """`None` when the wire is shorter than the unit it is measured in.

    units.md rounds once, on input, and µm is the unit — so a wire whose
    two ends land on the SAME micron has no length in the IR, no chord and
    (for an arc) no centre. Such a thing is not geometry the converter
    should try to keep: it is a leftover of the source editor's own
    arithmetic, and at this size it cannot connect, cover or clear
    anything. Real case that forced the rule: `staya.brd` carries two
    copper arcs of **0.108 µm** chord in signal GND, and 6 zero-length
    segments besides — enough to abort a 600 KB board on import."""
    x1, y1, x2, y2 = um(el.get("x1")), um(el.get("y1")), um(el.get("x2")), um(el.get("y2"))
    width = um(el.get("width"))
    signed, anti = _split(layer_of(int(el.get("layer"))))
    curve = el.get("curve")
    if (x1, y1) == (x2, y2):
        kind = "arc" if curve is not None and float(curve) != 0.0 else "segment"
        import_log.log(f"{kind} on layer {el.get('layer')} at ({el.get('x1')}, {el.get('y1')}) "
                        f"is shorter than one micron — it does not survive the IR's own unit "
                        f"(units.md) and is dropped")
        return None
    if curve is not None and float(curve) != 0.0:
        return Arc(x1, y1, x2, y2, _signed_mdeg(curve), width, layer=signed, anti=anti)
    return Line(x1, y1, x2, y2, width, layer=signed, anti=anti)


def convert_circle(el, layer_of) -> Shape:
    """shape.md: `x`/`y` is the CENTER — Eagle's own `<circle>` already
    uses center coordinates, so this is a direct passthrough, not a
    corner computation."""
    x, y, radius, width = um(el.get("x")), um(el.get("y")), um(el.get("radius")), um(el.get("width"))
    signed, anti = _split(layer_of(int(el.get("layer"))))
    d = radius * 2
    outline = 0 if width == 0 else width
    return Shape(x, y, d, d, signed, roundness=100, outline=outline, anti=anti)


def convert_rectangle(el, layer_of) -> Shape:
    """shape.md: `x`/`y` is the CENTER, rotated around it — Eagle's
    `<rectangle>` gives two opposite corners instead, so the center has
    to be computed, not read off directly."""
    x1, y1, x2, y2 = um(el.get("x1")), um(el.get("y1")), um(el.get("x2")), um(el.get("y2"))
    signed, anti = _split(layer_of(int(el.get("layer"))))
    _mirror, rot = angle(el.get("rot"))
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    return Shape(cx, cy, abs(x2 - x1), abs(y2 - y1), signed, rot=rot, anti=anti)


def convert_polygon(el, layer_of, log) -> Polygon | Line | None:
    """Eagle `<polygon>` -> IR `Polygon` — or a degenerate fallback,
    conversion.md #что-импортёр-дописывает-сам ("округление умеет и
    схлопывать"): consecutive coincident vertices collapse to one, and
    what's left dictates the shape — 2 points with real width is exactly
    what a `<line>` is (a genuine Eagle idiom: a 2-vertex filled polygon
    draws a round-capped thick stroke, same as a wire of that width),
    fewer than that carries no area at all and is dropped."""
    width = um(el.get("width"))
    signed, anti = _split(layer_of(int(el.get("layer"))))
    is_cutout = el.get("pour", "solid") == "cutout"

    raw = []
    for v in el.findall("vertex"):
        curve = v.get("curve")
        raw.append(Vertex(um(v.get("x")), um(v.get("y")), _signed_mdeg(curve) if curve else None))

    vertices = []
    for v in raw:
        if vertices and vertices[-1].x == v.x and vertices[-1].y == v.y:
            log(f"polygon on layer {el.get('layer')}: collapsed a coincident vertex at ({v.x}, {v.y})")
            continue
        vertices.append(v)

    if len(vertices) >= 3:
        return Polygon(layer=signed, width=width, vertices=vertices, anti=anti or is_cutout)
    if len(vertices) == 2:
        log(f"polygon on layer {el.get('layer')}: only 2 vertices left -> degenerate <line>")
        a, b = vertices
        return Line(a.x, a.y, b.x, b.y, width, layer=signed, anti=anti or is_cutout)
    log(f"polygon on layer {el.get('layer')}: fewer than 2 vertices left after collapsing -> dropped")
    return None


_ALIGN_MAP = {
    "bottom-left": "bottom-left", "bottom-center": "bottom-center", "bottom-right": "bottom-right",
    "center-left": "center-left", "center": "center-center", "center-right": "center-right",
    "top-left": "top-left", "top-center": "top-center", "top-right": "top-right",
}


# =====================================================================
# IR -> Eagle (export). Same conversions, same tables, run backwards.
# =====================================================================

def mm(value: int) -> str:
    """IR µm int -> Eagle mm string, exact decimal (no float rounding —
    units.md #where-rounding-happens: it happened once, on import; the
    exporter only translates, never rounds again)."""
    sign = "-" if value < 0 else ""
    value = abs(value)
    whole, frac = divmod(value, 1000)
    if frac == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{f'{frac:03d}'.rstrip('0')}"


def _degrees(mdeg: int) -> str:
    sign = "-" if mdeg < 0 else ""
    mdeg = abs(mdeg)
    whole, frac = divmod(mdeg, 1000)
    if frac == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{f'{frac:03d}'.rstrip('0')}"


def eagle_rot(mirror: int, rot_mdeg: int) -> str:
    """(mirror, rot_mdeg) -> Eagle `rot="[M]R<degrees>"` string."""
    return f"{'M' if mirror else ''}R{_degrees(rot_mdeg)}"


_PIN_LENGTH_REVERSE = [(0, "point"), (2540, "short"), (5080, "middle"), (7620, "long")]


def pin_length_name(length_um: int, log) -> str:
    for value, name in _PIN_LENGTH_REVERSE:
        if value == length_um:
            return name
    nearest = min(_PIN_LENGTH_REVERSE, key=lambda p: abs(p[0] - length_um))
    log(f"pin length {length_um} um has no exact Eagle enum match, using nearest {nearest[1]!r}")
    return nearest[1]


_PIN_VISIBLE_REVERSE = {(1, 1): "both", (1, 0): "pin", (0, 1): "pad", (0, 0): "off"}


def pin_visible_name(pinvis: int, padvis: int) -> str:
    return _PIN_VISIBLE_REVERSE[(pinvis, padvis)]


# canon (number, side, anti) -> eagle number, built once from the forward
# tables above so the two directions cannot drift apart.
def _build_footprint_layer_reverse() -> dict[tuple[int, int, bool], int]:
    reverse: dict[tuple[int, int, bool], int] = {
        (1, 1, False): 1, (1, -1, False): 16,
        (1, 1, True): 41, (1, -1, True): 42,
    }
    for eagle_num, layer in _FOOTPRINT_LAYER_TABLE.items():
        if eagle_num in (1, 16, 41, 42):
            continue  # copper/anti-copper handled above, exactly
        reverse.setdefault((layer.number, layer.side, layer.anti), eagle_num)
    for eagle_top, (canon, eagle_bottom) in _FOOTPRINT_PAIRS.items():
        reverse[(canon, 1, False)] = eagle_top
        reverse[(canon, -1, False)] = eagle_bottom
    for eagle_num, canon in _FOOTPRINT_STANDALONE_NAMED.items():
        reverse[(canon, 1, False)] = eagle_num
    return reverse


_FOOTPRINT_LAYER_REVERSE = _build_footprint_layer_reverse()


def footprint_layer_reverse(layer: int, anti: bool, log) -> int:
    """IR footprint-space (number, anti) -> Eagle layer number —
    conversion-eagle.md #слои run backwards. `120` was folded from two
    Eagle layers on import (`20 Dimension` + `46 Milling`); on export it
    goes back to `20`, logged, since which one it originally was isn't
    kept anywhere."""
    number, side = abs(layer), (1 if layer > 0 else -1)
    key = (number, side, anti)
    if key in _FOOTPRINT_LAYER_REVERSE:
        if number == 120:
            log("layer 120 (board outline/milling) exported to Eagle 20 Dimension "
                "— could equally have been 46 Milling, that distinction wasn't kept")
        return _FOOTPRINT_LAYER_REVERSE[key]
    # Mirror of footprint_layer's own fallback: unrecognized canon numbers
    # only ever come out of THAT as `Layer(eagle_number + 100, 1)` —
    # standalone, no side — so reversing is just subtracting the 100 back
    # off. A side=-1 canon number reaching here has no known Eagle
    # counterpart at all (the forward direction never produces one); the
    # same subtraction is still the least-wrong guess, logged either way.
    guess = number - 100
    log(f"canon layer {layer}{' (anti)' if anti else ''}: no verified Eagle counterpart, "
        f"guessing {guess} (inverse of footprint_layer's own standalone +100 fallback)")
    return guess


ALIGN_MAP_REVERSE = {v: ("center" if v == "center-center" else v) for v in _ALIGN_MAP.values()}


def transform_point(x: int, y: int, mirror: int, rot: int, tx: int, ty: int) -> tuple[int, int]:
    """units.md #зеркало-применяется-до-поворота: mirror around own
    origin, then rot, then translate. Used both ways — resolving an
    instance's absolute position on import, and placing it back in a
    sheet's local space on export. Exact integer arithmetic, valid only
    for the orthogonal angles the schematic side is restricted to — for
    a board element's free rotation (element.md: "Поворот. Свободный"),
    use `transform_point_free` instead."""
    if mirror:
        x = -x
    if rot == 90000:
        x, y = -y, x
    elif rot == 180000:
        x, y = -x, -y
    elif rot == 270000:
        x, y = y, -x
    return x + tx, y + ty


def transform_point_free(x: int, y: int, mirror: int, rot: int, tx: int, ty: int) -> tuple[int, int]:
    """Same mirror-then-rotate-then-translate order as `transform_point`,
    but for an arbitrary angle (element.md: a board element's rotation
    isn't limited to 90° steps, unlike the schematic side) — trig instead
    of the exact integer cases, rounded back to the nearest µm."""
    if mirror:
        x = -x
    if rot % 360000:
        rad = math.radians(rot / 1000)
        cos_r, sin_r = math.cos(rad), math.sin(rad)
        x, y = round(x * cos_r - y * sin_r), round(x * sin_r + y * cos_r)
    return x + tx, y + ty


def bbox_from_shape(shape, mirror: int, rot: int, tx: int, ty: int) -> tuple[int, int, int, int]:
    """A symbol's frame `<shape>` (center-based, shape.md) transformed
    through its placing instance -> the frame's absolute (x0, y0, x1, y1)
    on the canvas."""
    x0, y0 = shape.x - shape.w // 2, shape.y - shape.h // 2
    corners = [(x0, y0), (x0 + shape.w, y0), (x0, y0 + shape.h), (x0 + shape.w, y0 + shape.h)]
    pts = [transform_point(x, y, mirror, rot, tx, ty) for x, y in corners]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def find_frame_shape(symbol):
    for g in symbol.graphics:
        if isinstance(g, Shape) and g.layer == 98:
            return g
    return None


def write_wire(g, eagle_layer: int):
    """`Line`/`Arc` -> Eagle `<wire>`. Caller has already resolved
    `eagle_layer` (identity for schematic space, `footprint_layer_reverse`
    for footprint space) and decided whether an anti-flagged object even
    belongs here at all."""
    import xml.etree.ElementTree as ET
    el = ET.Element("wire", x1=mm(g.x1), y1=mm(g.y1), x2=mm(g.x2), y2=mm(g.y2),
                     width=mm(g.width), layer=str(eagle_layer))
    if isinstance(g, Arc):
        el.set("curve", _degrees(g.curve))
    return el


def write_shape(g: Shape, eagle_layer: int):
    """`Shape` -> Eagle `<circle>` (round==100, w==h) or `<rectangle>`
    (round==0). `g.x`/`g.y` is the CENTER (shape.md) in both IR and
    Eagle's own `<circle>`; Eagle's `<rectangle>` instead wants two
    opposite corners of the unrotated box, so those are derived from the
    center here. A partial roundness or an oval (w != h, round==100) has
    no Eagle primitive — approximated and logged by the caller before
    this is reached; this function only draws what Eagle can hold
    exactly."""
    import xml.etree.ElementTree as ET
    if g.roundness == 100 and g.w == g.h:
        radius = g.w // 2
        width = 0 if g.outline == 0 else g.outline
        return ET.Element("circle", x=mm(g.x), y=mm(g.y), radius=mm(radius), width=mm(width), layer=str(eagle_layer))
    x1, y1 = g.x - g.w // 2, g.y - g.h // 2
    x2, y2 = x1 + g.w, y1 + g.h
    el = ET.Element("rectangle", x1=mm(x1), y1=mm(y1), x2=mm(x2), y2=mm(y2), layer=str(eagle_layer))
    if g.rot:
        el.set("rot", eagle_rot(0, g.rot))
    return el


def write_polygon(g: Polygon, eagle_layer: int, pour: str = "solid"):
    import xml.etree.ElementTree as ET
    el = ET.Element("polygon", width=mm(g.width), layer=str(eagle_layer))
    if pour != "solid":
        el.set("pour", pour)
    for v in g.vertices:
        vel = ET.SubElement(el, "vertex", x=mm(v.x), y=mm(v.y))
        if v.curve is not None:
            vel.set("curve", _degrees(v.curve))
    return el


def write_text(g: Text, eagle_layer: int):
    """Eagle's own DTD default font is "proportional", but every real text
    in all 5 ground-truth projects (992/1042) is "vector" — baking that in
    explicitly rather than silently switching typeface via the DTD default
    (units.md #что-импортёр-дописывает-сам applies to the exporter too)."""
    import xml.etree.ElementTree as ET
    align = ALIGN_MAP_REVERSE.get(g.align, g.align)
    el = ET.Element("text", x=mm(g.x), y=mm(g.y), size=mm(g.height), layer=str(eagle_layer),
                     font="vector", align=align, rot=eagle_rot(g.mirror, g.rot), ratio=str(g.ratio))
    el.text = g.content
    return el


def convert_text(el, layer_of) -> Text:
    """`el.text` is the raw content — a caller that means to hand this a
    library placeholder (`>NAME`) must have already resolved the Eagle
    `>` escaping (ElementTree already unescapes `&gt;` for us)."""
    x, y = um(el.get("x")), um(el.get("y"))
    height = um(el.get("size"))
    signed, anti = _split(layer_of(int(el.get("layer"))))
    mirror, rot = angle(el.get("rot"))
    align = _ALIGN_MAP.get(el.get("align", "bottom-left"), "bottom-left")
    ratio = int(el.get("ratio", "8"))
    return Text(x, y, height, signed, align, content=el.text or "", rot=rot, mirror=mirror, ratio=ratio, anti=anti)
