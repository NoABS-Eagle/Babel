"""KiCad `(footprint …)` -> IR `Footprint` — conversion-kicad.md
#что-приготовить.

**The geometry is read off the BOARD, not out of a library.** Every placed
footprint in `.kicad_pcb` carries a full copy of it, and instances of one
footprint that agree with each other are the truth — a second copy to check
against is not needed. Instances that DISAGREE are an edit made on one
placement, and that stops the conversion (see `divergence`).

To compare instances at all they must first be un-placed: a placement bakes
its side and its angle into every child. The un-placing is the exact
inverse of what `footprint_export` writes, and those laws were measured
against KiCad itself, not derived — see `mm_x`/`fp_rot` there.
"""

from __future__ import annotations

from ir.footprint import Footprint
from ir.graphics import Arc, Line, Polygon, Shape, Text, Vertex
from ir.pad import Hole, Pad, Smd

from . import geometry as geo
from . import sexpr
from .layers import ir_layer

# footprint.md: the only copper a footprint knows is its mount side and the
# opposite one — it knows nothing of any board's stack.
_COPPER_LAYER = {"F.Cu": 1, "B.Cu": -1}

_ROUNDNESS = {"circle": 100, "oval": 100, "rect": 0}

# Shapes the IR has no form for. conversion-kicad.md: they become a plain
# rectangle of the pad's own size, and the log names the FOOTPRINT, since
# that is what has to be fixed.
_APPROXIMATED_SHAPES = ("trapezoid", "chamfered", "custom")


def un_xy(x_mm, y_mm, back: bool) -> tuple[int, int]:
    """Placed footprint child -> library coordinates. X passes through on
    both sides; Y carries the flip (negated on the front, left alone on
    the back). The inverse of `footprint_export.mm_x`/`mm_y`."""
    x = geo.um(x_mm)
    y = geo.um(y_mm)
    return x, (y if back else -y)


def un_rot(angle_deg: float, theta: float, back: bool) -> int:
    """A body child's angle back to the library's, in mdeg.

    Forward is `(theta + L) % 360` on the front and `(180 - theta - L) %
    360` on the back, where `L` is the IR angle negated. Both are inverted
    here."""
    if back:
        library = (180 - theta - angle_deg) % 360
    else:
        library = (angle_deg - theta) % 360
    return round((360 - library) % 360 * 1000) % 360000


def un_text_rot(angle_deg: float, theta: float, back: bool) -> int:
    """A TEXT's angle — the IR's own, never negated (lettering is not
    reflected; that is what `mirror` is for)."""
    if back:
        library = (180 - theta - angle_deg) % 360
    else:
        library = (angle_deg - theta) % 360
    return round(library * 1000) % 360000


def un_side_layer(name: str, back: bool) -> str:
    """A placed footprint stores its layers already flipped."""
    if back and len(name) > 2 and name[0] in "FB" and name[1] == ".":
        return ("B." if name[0] == "F" else "F.") + name[2:]
    return name


def _layer_numbers(node: sexpr.Node, back: bool) -> list[str]:
    layers = sexpr.kid(node, "layers")
    return [un_side_layer(str(a), back) for a in sexpr.atoms(layers)] if layers else []


def _graphic_layer(node: sexpr.Node, back: bool, log, label: str) -> int | None:
    layer = sexpr.kid(node, "layer")
    if layer is None:
        return None
    name = un_side_layer(str(sexpr.atoms(layer)[0]), back)
    if name in _COPPER_LAYER:
        return _COPPER_LAYER[name]
    number = ir_layer(name)
    if number is None:
        log(f"{label}: layer {name!r} has no row in kicad_layers.tsv — object dropped")
    return number


_at = geo.at


def convert_pad(node: sexpr.Node, back: bool, theta: float, log, label: str):
    """One `(pad …)` -> `Pad`, `Smd`, `Hole`, or a list of `Shape`s when it
    carries no copper at all (a paste or mask aperture, smd.md)."""
    atoms = sexpr.atoms(node)
    if len(atoms) < 3:
        return None
    name = str(atoms[0])
    kind = str(atoms[1])
    shape = str(atoms[2])

    x_mm, y_mm, angle = _at(node)
    x, y = un_xy(x_mm, y_mm, back)
    rot = un_rot(angle, theta, back)

    size = sexpr.atoms(sexpr.kid(node, "size"))
    width = geo.um(size[0]) if size else 0
    height = geo.um(size[1]) if len(size) > 1 else width
    if width <= 0 or height <= 0:
        log(f"{label}: pad {name!r} has no size — dropped")
        return None

    roundness = _ROUNDNESS.get(shape)
    if roundness is None:
        if shape == "roundrect":
            ratio = sexpr.kid(node, "roundrect_rratio")
            roundness = round(float(sexpr.atoms(ratio)[0]) * 200) if ratio else 0
            roundness = max(0, min(100, roundness))
        else:
            roundness = 0
            if shape in _APPROXIMATED_SHAPES:
                log(f"{label}: pad {name!r} of shape {shape} -> plain rectangle "
                    f"{width}x{height} µm — check the land")

    layers = _layer_numbers(node, back)
    copper = [L for L in layers if L in _COPPER_LAYER or L == "*.Cu"]
    drill_node = sexpr.kid(node, "drill")
    drill = _drill(drill_node, log, label, name)

    if not copper:
        # smd.md: no copper means this is not a pad at all — it is paste or
        # mask geometry, and it arrives as one shape per technical layer.
        return _aperture(node, layers, x, y, width, height, rot, roundness, log, label, name)

    if kind == "np_thru_hole":
        # hole.md / conversion-kicad.md #плата: an unplated hole is read by
        # its copper. With none it is a `<hole>`; the copper case falls
        # through to the plated pad below.
        if drill:
            return Hole(x=x, y=y, drill=drill)
        log(f"{label}: unplated pad {name!r} has no drill — dropped")
        return None

    through = "*.Cu" in copper or len([L for L in copper if L in _COPPER_LAYER]) > 1
    if through or drill:
        if not drill:
            log(f"{label}: pad {name!r} spans copper layers but has no drill — dropped")
            return None
        if drill >= min(width, height):
            log(f"{label}: pad {name!r} drill {drill} leaves no ring in {width}x{height} — dropped")
            return None
        if not name:
            # A nameless plated pad addresses nothing; hole.md covers it.
            return Hole(x=x, y=y, drill=drill)
        return Pad(name=name, x=x, y=y, width=width, height=height, drill=drill,
                   rot=rot, roundness=roundness,
                   stopmask=1 if any(L.endswith(".Mask") for L in layers) else 0)

    side = _COPPER_LAYER[copper[0]]
    if not name:
        # Copper that addresses nothing — a fiducial's target, a shield
        # land. It is not a pad (nothing can map to it), but it IS copper,
        # so it stays as a shape on its own copper layer rather than being
        # dropped: footprint.md allows exactly `1`/`-1` there.
        log(f"{label}: nameless pad at ({x}, {y}) connects to nothing -> "
            f"copper shape on {copper[0]}")
        return [Shape(x, y, width, height, side, rot=rot, roundness=roundness)]
    return Smd(name=name, x=x, y=y, width=width, height=height, layer=side,
               rot=rot, roundness=roundness,
               stopmask=1 if any(L.endswith(".Mask") for L in layers) else 0,
               paste=1 if any(L.endswith(".Paste") for L in layers) else 0)


def _drill(node: sexpr.Node | None, log, label: str, name: str) -> int:
    if node is None:
        return 0
    atoms = sexpr.atoms(node)
    numbers = [a for a in atoms if isinstance(a, (int, float))]
    if not numbers:
        return 0
    if str(atoms[0]) == "oval" and len(numbers) > 1:
        # pad.md has one drill diameter. The short axis keeps the hole
        # drillable; the long one is the slot that is being lost.
        log(f"{label}: pad {name!r} has an oval drill -> round {min(numbers)} mm")
        return geo.um(min(numbers))
    return geo.um(numbers[0])


def _aperture(node, layers, x, y, width, height, rot, roundness, log, label, name) -> list:
    """A pad with no copper is paste/mask geometry — one `<shape>` per
    technical layer it asked for. Nothing asked for, nothing to draw."""
    shapes = []
    for layer_name in layers:
        number = ir_layer(layer_name)
        if number is None:
            continue
        shapes.append(Shape(x, y, width, height, number, rot=rot, roundness=roundness))
    if not shapes:
        log(f"{label}: pad {name!r} carries no copper and no technical layer — dropped")
    else:
        log(f"{label}: pad {name!r} carries no copper -> {len(shapes)} shape(s) "
            f"on {', '.join(layers)}")
    return shapes


def convert_graphic(node: sexpr.Node, back: bool, theta: float, log, label: str):
    tag = node[0]
    layer = _graphic_layer(node, back, log, label)
    if layer is None:
        return None
    width = geo.stroke_width(node, 0)

    if tag == "fp_line":
        x1, y1 = un_xy(*sexpr.atoms(sexpr.kid(node, "start"))[:2], back)
        x2, y2 = un_xy(*sexpr.atoms(sexpr.kid(node, "end"))[:2], back)
        if (x1, y1) == (x2, y2):
            return None
        return Line(x1, y1, x2, y2, width, layer)

    if tag == "fp_arc":
        x1, y1 = un_xy(*sexpr.atoms(sexpr.kid(node, "start"))[:2], back)
        mx, my = un_xy(*sexpr.atoms(sexpr.kid(node, "mid"))[:2], back)
        x2, y2 = un_xy(*sexpr.atoms(sexpr.kid(node, "end"))[:2], back)
        if (x1, y1) == (x2, y2):
            return None
        curve = geo._sweep(x1, y1, mx, my, x2, y2)
        if curve is None:
            log(f"{label}: degenerate arc — dropped")
            return None
        return Arc(x1, y1, x2, y2, curve, width, layer)

    if tag == "fp_circle":
        cx, cy = un_xy(*sexpr.atoms(sexpr.kid(node, "center"))[:2], back)
        ex, ey = un_xy(*sexpr.atoms(sexpr.kid(node, "end"))[:2], back)
        radius = round(((ex - cx) ** 2 + (ey - cy) ** 2) ** 0.5)
        if radius <= 0:
            return None
        filled = _is_filled(node)
        return Shape(cx, cy, radius * 2, radius * 2, layer, roundness=100,
                     outline=0 if filled else width)

    if tag == "fp_rect":
        x1, y1 = un_xy(*sexpr.atoms(sexpr.kid(node, "start"))[:2], back)
        x2, y2 = un_xy(*sexpr.atoms(sexpr.kid(node, "end"))[:2], back)
        return Shape((x1 + x2) // 2, (y1 + y2) // 2, abs(x2 - x1), abs(y2 - y1),
                     layer, outline=0 if _is_filled(node) else width)

    if tag == "fp_poly":
        pts = sexpr.kid(node, "pts")
        verts = [Vertex(*un_xy(*sexpr.atoms(p)[:2], back))
                 for p in sexpr.kids(pts, "xy")] if pts else []
        deduped = [v for i, v in enumerate(verts)
                   if i == 0 or (v.x, v.y) != (verts[i - 1].x, verts[i - 1].y)]
        if len(deduped) > 2 and (deduped[0].x, deduped[0].y) == (deduped[-1].x, deduped[-1].y):
            del deduped[-1]
        if len(deduped) < 3:
            log(f"{label}: polygon with {len(deduped)} point(s) — dropped")
            return None
        return Polygon(layer, width, deduped, fill=100 if _is_filled(node) else 0)

    if tag == "fp_text":
        atoms = sexpr.atoms(node)
        kind = str(atoms[0]) if atoms else "user"
        content = str(atoms[1]) if len(atoms) > 1 else ""
        if kind == "reference" or content in ("${REFERENCE}", "REF**"):
            return None  # the designator placeholder, handled by the caller
        if not content:
            return None
        x_mm, y_mm, angle = _at(node)
        x, y = un_xy(x_mm, y_mm, back)
        height, align, mirror = geo.text_effects(node)
        return Text(x, y, height, layer, align, content=geo.overbar(content),
                    rot=un_text_rot(angle, theta, back), mirror=mirror)

    return None


def _is_filled(node: sexpr.Node) -> bool:
    """A footprint shape writes `(fill yes|no)`, not the symbol's
    `(fill (type …))`."""
    f = sexpr.kid(node, "fill")
    if f is None:
        return False
    atoms = sexpr.atoms(f)
    if atoms:
        return str(atoms[0]) in ("yes", "solid")
    return geo.is_filled(node)


def convert_footprint(node: sexpr.Node, log) -> Footprint | None:
    """One placed footprint, un-placed back into its library form."""
    atoms = sexpr.atoms(node)
    if not atoms:
        return None
    lib_id = str(atoms[0])
    library, _, name = lib_id.rpartition(":")
    label = f"footprint {lib_id}"

    layer_node = sexpr.kid(node, "layer")
    back = bool(layer_node) and str(sexpr.atoms(layer_node)[0]).startswith("B.")
    _x, _y, theta = _at(node)

    pads, holes, graphics = [], [], []
    for child in node[1:]:
        if not isinstance(child, list):
            continue
        if child[0] == "pad":
            result = convert_pad(child, back, theta, log, label)
            if result is None:
                continue
            if isinstance(result, list):
                graphics += result
            elif isinstance(result, Hole):
                holes.append(result)
            else:
                pads.append(result)
        elif child[0].startswith("fp_"):
            result = convert_graphic(child, back, theta, log, label)
            if result is not None:
                graphics.append(result)

    pads, aliases = _rename_repeats(pads, log, label)
    fp = Footprint(name=name, library=library or None,
                   graphics=graphics, pads=pads, holes=holes)
    return fp, aliases


def _rename_repeats(pads: list, log, label: str) -> tuple[list, dict[str, list[str]]]:
    """footprint.md makes a pad name unique; KiCad does not, and a repeated
    number is how one terminal is drawn as SEVERAL copper islands (a
    thermal pad split into a grid, a connector shell with four tabs).

    Nothing is dropped: the copies are renamed apart and reported as a
    group, because [map.md gives one pin a LIST of pads](../spec/map.md) —
    this is exactly what that list is for."""
    counts: dict[str, int] = {}
    for p in pads:
        counts[p.name.lower()] = counts.get(p.name.lower(), 0) + 1

    aliases: dict[str, list[str]] = {}
    seen: dict[str, int] = {}
    out = []
    for p in pads:
        key = p.name.lower()
        if counts[key] > 1:
            seen[key] = seen.get(key, 0) + 1
            name = p.name if seen[key] == 1 else f"{p.name}@{seen[key]}"
            aliases.setdefault(p.name, []).append(name)
            p = _renamed(p, name)
        out.append(p)
    for original, group in aliases.items():
        log(f"{label}: pad {original!r} is drawn as {len(group)} copper islands "
            f"-> {' '.join(group)}, one pin mapped to all of them")
    return out, aliases


def _renamed(pad, name: str):
    from dataclasses import replace
    return replace(pad, name=name)


def signature(fp: Footprint) -> tuple:
    """What must be IDENTICAL across every instance of one footprint.

    conversion-kicad.md #правка-на-размещении-отвергает-проект: geometry
    belongs to the library and the instance carries only where it sits.
    The comparison is exact, with no tolerance — "how many microns is not
    an edit" has no non-arbitrary answer.

    Compared after un-placing, so side and angle are already divided out.
    Ordering is not a difference: KiCad is free to write the same children
    in another order, and two files that draw the same copper are the same
    footprint."""
    pads = sorted((type(p).__name__, p.name.lower(), p.x, p.y,
                   getattr(p, "width", 0), getattr(p, "height", 0),
                   getattr(p, "drill", 0), getattr(p, "layer", 0),
                   # A pad's outline is centrally symmetric — rectangle,
                   # oval, circle, roundrect alike — so θ and θ+180 are the
                   # SAME copper, and KiCad writes whichever the flip
                   # happened to produce. Comparing the raw angle reports
                   # an edit on every bottom-side instance of a part whose
                   # top-side twin sits at 0.
                   p.rot % 180000, getattr(p, "roundness", 0))
                  for p in fp.pads)
    holes = sorted((h.x, h.y, h.drill) for h in fp.holes)
    graphics = sorted(_graphic_key(g) for g in fp.graphics)
    return tuple(pads), tuple(holes), tuple(graphics)


def _graphic_key(g) -> tuple:
    """A graphic reduced to what it draws. Text is compared by its
    content and box, not by position alone — a moved label is NOT an edit
    (that is the placeholder layout, which every instance overrides), and
    the caller keeps text out of the signature for that very reason."""
    return (type(g).__name__, getattr(g, "layer", None),
            getattr(g, "x", None), getattr(g, "y", None),
            getattr(g, "x1", None), getattr(g, "y1", None),
            getattr(g, "x2", None), getattr(g, "y2", None),
            getattr(g, "w", None), getattr(g, "h", None),
            getattr(g, "curve", None), getattr(g, "width", None),
            getattr(g, "outline", None), getattr(g, "roundness", None),
            getattr(g, "fill", None),
            tuple((v.x, v.y) for v in getattr(g, "vertices", ())))


def without_text(fp: Footprint) -> Footprint:
    """The same footprint with its lettering removed.

    Lettering is not geometry: conversion-kicad.md lists "раскладка
    надписей" among the things an instance carries itself, and in these
    files every instance really has moved its own `Reference`. Comparing
    it would report an edit on every board."""
    from dataclasses import replace
    return replace(fp, graphics=[g for g in fp.graphics if not isinstance(g, Text)])


def first_difference(a: Footprint, b: Footprint) -> str | None:
    """The first thing that differs, named for the refusal message."""
    sa, sb = signature(without_text(a)), signature(without_text(b))
    for what, xs, ys in zip(("pad", "hole", "graphic"), sa, sb):
        if xs == ys:
            continue
        if len(xs) != len(ys):
            return f"{what} count {len(xs)} vs {len(ys)}"
        for x, y in zip(xs, ys):
            if x != y:
                return f"{what} {x} vs {y}"
    return None
