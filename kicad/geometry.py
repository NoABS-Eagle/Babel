"""KiCad geometry -> IR graphics.

**Symbol space is Y-up in KiCad, exactly like the IR's** — a symbol's own
coordinates carry over verbatim, and only the placed-instance canvas needs
a flip. That is not derived here: it is the same ground truth the export
side rests on (`library_export.mm`), where tolmach imported by KiCad
itself copies every asymmetric symbol coordinate unchanged.
"""

from __future__ import annotations

import math
import re

from ir.graphics import Arc, Line, Polygon, Shape, Text, Vertex

from . import sexpr

# A stroke width of 0 means "use the default", not "hairline" — the number
# lives in the project's own settings (`schematic.drawing.
# default_line_thickness`, in mils), and 6 mil is what KiCad ships with.
# Ground truth for the value: every rectangle KiCad wrote in these files
# with an explicit width carries 0.1524 mm, which is that same 6 mil.
DEFAULT_LINE_UM = 152


_OVERBAR = re.compile(r"~\{([^{}]*)\}")


def overbar(text: str) -> str:
    """KiCad's `~{RESET}` -> the IR's own `!RESET!` (text.md, pin.md: an
    active-low name is written, not flagged — a convention of the name
    itself, so that two editors draw one file the same way).

    Not cosmetic: braces are out of the pin-name domain outright
    (naming.md), so a `~{…}` name would be refused rather than drawn."""
    return _OVERBAR.sub(lambda m: f"!{m.group(1)}!" if m.group(1) else "", text)


# KiCad escapes characters it cannot put in a file name into a net or
# label name — `VPP/MCLR` is stored as `VPP{slash}MCLR`. This is the
# table its own UnescapeString uses.
_ESCAPES = {
    "slash": "/", "backslash": "\\", "lbrace": "{", "rbrace": "}",
    "colon": ":", "dblquote": '"', "lt": "<", "gt": ">", "bar": "|",
    "asterisk": "*", "question": "?", "space": " ", "tab": "\t",
    "return": "\r", "newline": "\n", "dollar": "$", "quote": "'",
}
_ESCAPE_RE = re.compile(r"\{(" + "|".join(_ESCAPES) + r")\}")


def unescape(name: str) -> str:
    """A KiCad name back to what it says. Braces are out of every IR name
    domain (naming.md), so leaving them would refuse the file over a
    character KiCad put there itself."""
    return _ESCAPE_RE.sub(lambda m: _ESCAPES[m.group(1)], name)


def um(value) -> int:
    """KiCad mm -> IR µm — units.md #1 (round once, on input)."""
    return round(float(value) * 1000)


def mils_to_um(value) -> int:
    return round(float(value) * 25.4)


def _xy(node: sexpr.Node, tag: str) -> tuple[int, int]:
    a = sexpr.atoms(sexpr.kid(node, tag))
    return um(a[0]), um(a[1])


def at(node: sexpr.Node) -> tuple[float, float, float]:
    """An `(at …)` as x, y, angle in KiCad's own units.

    Only the NUMBERS are read: KiCad mixes bare flags into this list —
    `(at 0 0 unlocked)` is ordinary, and taking the third slot positionally
    hands `unlocked` to `float()`."""
    a = [v for v in sexpr.atoms(sexpr.kid(node, "at")) if isinstance(v, (int, float))]
    if len(a) < 2:
        return 0.0, 0.0, 0.0
    return float(a[0]), float(a[1]), float(a[2]) if len(a) > 2 else 0.0


def stroke_width(node: sexpr.Node, default_um: int = DEFAULT_LINE_UM) -> int:
    s = sexpr.kid(node, "stroke")
    w = sexpr.kid(s, "width") if s else None
    if w is None:
        return default_um
    value = um(sexpr.atoms(w)[0])
    return value if value else default_um


def is_filled(node: sexpr.Node) -> bool:
    """`outline` and `background` both paint the body; `none` leaves the
    stroke alone. A filled IR `Shape` is the one with `outline=0`
    (shape.md), so this is the fact that decides that field."""
    f = sexpr.kid(node, "fill")
    t = sexpr.kid(f, "type") if f else None
    return bool(t) and str(sexpr.atoms(t)[0]) in ("outline", "background")


def convert_rectangle(node: sexpr.Node, layer: int, default_um: int = DEFAULT_LINE_UM) -> Shape:
    """shape.md: `x`/`y` is the CENTER; KiCad gives two opposite corners.
    KiCad writes them in either order (`(start -7.62 11.43) (end 7.62
    -11.43)` is real), so the extent is taken by absolute difference."""
    x1, y1 = _xy(node, "start")
    x2, y2 = _xy(node, "end")
    outline = 0 if is_filled(node) else stroke_width(node, default_um)
    return Shape((x1 + x2) // 2, (y1 + y2) // 2, abs(x2 - x1), abs(y2 - y1),
                 layer, outline=outline)


def convert_circle(node: sexpr.Node, layer: int, default_um: int = DEFAULT_LINE_UM) -> Shape | None:
    """A circle is a `Shape` with `roundness=100` — the IR has no circle of
    its own (shape.md), and a square shape rounded all the way IS one."""
    cx, cy = _xy(node, "center")
    radius = um(sexpr.atoms(sexpr.kid(node, "radius"))[0])
    if radius <= 0:
        return None
    outline = 0 if is_filled(node) else stroke_width(node, default_um)
    return Shape(cx, cy, radius * 2, radius * 2, layer, roundness=100, outline=outline)


def convert_arc(node: sexpr.Node, layer: int, default_um: int = DEFAULT_LINE_UM) -> Arc | None:
    """KiCad names an arc by three points on it; arc.md stores the chord
    plus the swept angle, signed. The exact inverse of what the exporter
    writes (`library_export.write_graphic`), so a round trip is closed by
    construction."""
    x1, y1 = _xy(node, "start")
    mx, my = _xy(node, "mid")
    x2, y2 = _xy(node, "end")
    if (x1, y1) == (x2, y2):
        return None  # a full circle written as an arc has no chord
    curve = _sweep(x1, y1, mx, my, x2, y2)
    if curve is None:
        return None  # the three points are collinear: a line, not an arc
    return Arc(x1, y1, x2, y2, curve, stroke_width(node, default_um), layer)


def _sweep(x1: int, y1: int, mx: int, my: int, x2: int, y2: int) -> int | None:
    """Swept angle in mdeg, signed the way arc.md wants it.

    Taken from the SAGITTA — how far the middle point stands off the chord
    — and not from the circumcenter of the three points. Both are exact in
    real arithmetic, but the circumcenter divides by a cross product that
    goes to zero exactly when the arc goes flat, and a shallow arc is the
    common case: it cost 0.03° at 10°, while this form is exact there.

    `mid` is the point halfway ALONG the arc (`Arc.midpoint`), so it sits
    on the chord's perpendicular bisector and the half-angle is a plain
    `atan2` — including past 180°, where the sagitta exceeds the half
    chord and the angle simply passes a right angle."""
    dx, dy = x2 - x1, y2 - y1
    half = math.hypot(dx, dy) / 2
    if half == 0:
        return None
    # Signed offset of the middle point along the chord's LEFT normal —
    # the same normal `Arc.center` measures its own offset along, hence
    # the sign flip here: the arc bulges away from its center.
    nx, ny = -dy / (2 * half), dx / (2 * half)
    sagitta = (mx - (x1 + x2) / 2) * nx + (my - (y1 + y2) / 2) * ny
    if abs(sagitta) < 1e-9:
        return None  # the three points are collinear: a line, not an arc
    return round(math.degrees(4 * math.atan2(-sagitta, half)) * 1000)


def convert_polyline(node: sexpr.Node, layer: int, log,
                     default_um: int = DEFAULT_LINE_UM) -> Polygon | Line | None:
    """A KiCad polyline is one shape for three IR things, and the vertices
    decide which — the same reading `eagle.geometry.convert_polygon` does.
    A closed run (last vertex repeating the first) is a `Polygon`; two
    points are a `Line`; anything shorter carries nothing."""
    pts = sexpr.kid(node, "pts")
    verts = [(um(sexpr.atoms(p)[0]), um(sexpr.atoms(p)[1]))
             for p in sexpr.kids(pts, "xy")] if pts else []
    # Consecutive duplicates say nothing and break the closed-ness test.
    deduped = [v for i, v in enumerate(verts) if i == 0 or v != verts[i - 1]]
    width = stroke_width(node, default_um)
    closed = len(deduped) > 2 and deduped[0] == deduped[-1]
    if closed:
        del deduped[-1]  # polygon.md: an IR polygon closes by definition
    if len(deduped) < 2:
        log(f"polyline with {len(deduped)} distinct point(s) carries no shape — dropped")
        return None
    if closed and len(deduped) < 3:
        # A closed run of two distinct points (`A B A`) encloses no area:
        # it is a stroke drawn out and back, and that is a line.
        closed = False
    if closed:
        return Polygon(layer, width, [Vertex(x, y) for x, y in deduped],
                       fill=100 if is_filled(node) else 0)
    if len(deduped) == 2:
        (x1, y1), (x2, y2) = deduped
        return Line(x1, y1, x2, y2, width, layer)
    # An open run of three or more: a polygon whose fill would be a lie,
    # so it keeps its outline and nothing else.
    return Polygon(layer, width, [Vertex(x, y) for x, y in deduped], fill=0)


_JUSTIFY_H = {"left": "left", "right": "right"}
_JUSTIFY_V = {"top": "top", "bottom": "bottom"}


def text_effects(node: sexpr.Node) -> tuple[int, str, int]:
    """Height in µm, align, and mirror — off a KiCad `(effects …)`.

    **`mirror` lives in the justify list but is not a justification.** It
    is text.md's own `mirror`, and reading it as an alignment token is the
    trap conversion-kicad.md warns about by name."""
    effects = sexpr.kid(node, "effects")
    height = 1270
    align_h = align_v = "center"
    mirror = 0
    if effects is not None:
        font = sexpr.kid(effects, "font")
        size = sexpr.kid(font, "size") if font else None
        if size is not None:
            height = um(sexpr.atoms(size)[0])
        j = sexpr.kid(effects, "justify")
        for token in (str(a) for a in sexpr.atoms(j)) if j else ():
            if token == "mirror":
                mirror = 1
            elif token in _JUSTIFY_H:
                align_h = _JUSTIFY_H[token]
            elif token in _JUSTIFY_V:
                align_v = _JUSTIFY_V[token]
    return height, f"{align_v}-{align_h}", mirror


def is_hidden(node: sexpr.Node) -> bool:
    """`(hide yes)` as a nested list — the shape KiCad 10 writes. Older
    files put a bare `hide` atom in the same place, and both are read."""
    h = sexpr.kid(node, "hide")
    if h is not None:
        return str(sexpr.atoms(h)[0]) == "yes"
    return any(isinstance(c, sexpr.Sym) and c.value == "hide" for c in node[1:])


def convert_text(node: sexpr.Node, layer: int) -> Text | None:
    atoms = sexpr.atoms(node)
    content = str(atoms[0]) if atoms else ""
    if not content:
        return None
    x_mm, y_mm, angle = at(node)
    height, align, mirror = text_effects(node)
    return Text(um(x_mm), um(y_mm), height, layer, align, content=content,
                rot=round(angle * 1000) % 360000, mirror=mirror)
