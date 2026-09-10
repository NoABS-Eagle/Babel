"""IR `Footprint` -> KiCad `.kicad_mod` content — conversion-kicad.md
#библиотеки: "Корпуса... сваливаются в один пул `footprints.pretty` — по
файлу `.kicad_mod` на корпус."

**Footprint space is Y-DOWN** — the opposite of the symbol space next
door (`library_export.mm`), and not a guess: across the 116 pads the 17
footprints shared with KiCad's own import of tolmach, `kicad_x = ir_x`
and `kicad_y = -ir_y` hold with zero outliers.

The same writer produces the LIBRARY form and the form a board embeds:
pass `back`/`theta` and it emits the footprint already placed. That is
deliberate — a placed footprint is GENERATED for its side, never
assembled for the top and then taken apart and patched. See `mm_y` for
the flip-bake table.
"""

from __future__ import annotations

import uuid as _uuid

from ir import pen
from ir.graphics import Arc, Line, Polygon, Shape, Text
from ir.pad import Hole, Pad, Smd

from .layers import kicad_layer
from .sexpr import Sym

_KICAD_MOD_VERSION = 20260206  # ground truth: Library.pretty/*.kicad_mod

_COPPER_SIDE = {1: "F", -1: "B"}


def mm(value_um: int) -> float:
    return value_um / 1000


def mm_x(value_um: int, back: bool = False) -> float:
    """X passes through on BOTH sides — the flip is baked into Y.

        top     local ( x, -y)   at-rot =  θ         item angle = θ + L
        bottom  local ( x,  y)   at-rot = -θ - 180   item angle = θ + L + 180

    **The placement this produces is the same one as before**, to the
    micron: the old bake reflected the body about Y and the new one about
    X, and two reflections differ by exactly a half turn, which the
    placement angle absorbs. It is a respelling, not a new answer — the
    answer itself was read off Eagle's own routing and stands (element.md's
    mirror is ROTATE FIRST, MIRROR X AFTERWARDS, in board space: of the 20
    mirrored pads tolmach actually routes copper to, that composition puts
    16 exactly under a wire end against 8 and 10 for the alternatives).

    Why respell at all: KiCad compares a placed footprint against its
    library copy by UN-FLIPPING it, and it un-flips about X. Reflected
    about Y, every bottom footprint on every board came back as
    `lib_footprint_mismatch` — noise that would hide a real divergence.
    Ground truth, KiCad's own import of tolmach (which reports zero such
    mismatches): library pad `(at -3.75 -4.9 180)` is placed on `B.Cu` as
    `(at -3.75 4.9 90)` under `(at ... 90)` — X kept, Y negated, and the
    child's angle the library's MINUS the placement's."""
    return value_um / 1000


def mm_y(value_um: int, back: bool = False) -> float:
    """Y carries the flip: negated on the front (footprint space runs
    down where the IR runs up) and left alone on the back, which is the
    same negation applied twice. See `mm_x` for the whole table."""
    return value_um / 1000 if back else -value_um / 1000


def fp_rot(rot_mdeg: int, theta: float = 0.0, back: bool = False) -> float:
    """An item's angle.

    The LIBRARY angle is the IR one negated — the same conjugation as the
    Y flip beside it, ground-truthed on the 17 footprints shared with
    KiCad's own import (an Eagle R90 pad comes back as 270). A PLACED
    footprint then folds the placement's own rotation in, since a child's
    angle there is absolute; and the flip reverses which way that folds,
    so the back subtracts where the front adds.

    The two sign choices are indistinguishable on the pads themselves —
    rectangular copper at 90° and at 270° is the same rectangle — so no
    measurement of this board settles them; only looking at a placed part
    does.

    **On the BACK the angle is REFLECTED, not turned**: `180 - (θ + L)`.
    A mirror reverses the sense of every angle, so adding a half turn —
    which is what this did at first — agrees with the truth only where
    `θ + L` is already 0 or 180, and that is exactly why some footprints
    came back matching their library copy and others did not.

    Measured, not derived. A deliberately asymmetric probe footprint was
    placed eight times, twice at each of 0/90/180/270, and KiCad itself
    flipped four of them (`outputs/flipprobe`). Every child followed the
    same rule at every angle — pad at 30°: `30→150`, `120→60`, `210→330`,
    `300→240`; and the placement angle came out `180 - θ`, which is what
    `board_export` writes. Text alone looks different because KiCad folds
    it for readability afterwards."""
    library = (360000 - rot_mdeg) % 360000 / 1000
    if back:
        return (180 - theta - library) % 360
    return (theta + library) % 360


def text_rot(rot_mdeg: int, theta: float = 0.0, back: bool = False) -> float:
    """A TEXT's angle — the IR's own, **not negated**.

    `fp_rot` negates because a footprint's body is Y-flipped, and under a
    reflection a shape's angle reverses. Lettering is the exception: it is
    never reflected (that is what `mirror` in `justify` is for), so
    reversing its angle turns the words the wrong way. At 0/90/180/270 the
    error is invisible — the fold in `board_export._upright` hides it — and
    every text in four of the six test boards sits at one of those. It took
    `staya`, whose corner captions run at 30°, to show it: `HANDS OFF` came
    out at 150°.

    The back side still turns, by the same rule every other child follows
    (`fp_rot`): `180 - (θ + L)`."""
    library = rot_mdeg / 1000
    if back:
        return (180 - theta - library) % 360
    return (theta + library) % 360


def side_layer(name: str, back: bool) -> str:
    """`F.SilkS` -> `B.SilkS` on the back; a side-less layer stays put. A
    placed footprint stores its layers already flipped — KiCad does not
    derive them from the footprint sitting on `B.Cu`."""
    if back and len(name) > 2 and name[0] in "FB" and name[1] == ".":
        return ("B." if name[0] == "F" else "F.") + name[2:]
    return name


def _uuid4() -> str:
    return str(_uuid.uuid4())


def _justify(align: str) -> list:
    """The same nine-cell grid as everywhere else, carried over UNCHANGED
    — both halves.

    The vertical half used to be swapped here, on the reasoning that
    `bottom` in a Y-up world is `top` once the page runs the other way.
    That is wrong: an anchor is a statement about the TEXT, not about the
    axis. Measured in KiCad — a caption at `justify bottom` sits ABOVE its
    anchor point and one at `justify top` hangs BELOW it, exactly as Eagle
    places `align="bottom-…"` and `align="top-…"`. Swapping moved every
    such caption by its own height.

    It stayed hidden because Eagle libraries write `align="center"` almost
    always: across six real boards only 0-3 texts each have a vertical
    anchor at all."""
    v, h = align.split("-")
    tokens = []
    if h != "center":
        tokens.append(Sym(h))
    if v != "center":
        tokens.append(Sym(v))
    return tokens


def _effects(height_um: int, align: str, back: bool = False) -> list:
    node = ["effects", ["font", ["size", mm(height_um), mm(height_um)]]]
    tokens = _justify(align)
    if back:
        # Back-side lettering is mirrored, so it reads the right way
        # round from that side.
        tokens.append(Sym("mirror"))
    if tokens:
        node.append(["justify", *tokens])
    return node


def _stroke(width_um: int) -> list:
    return ["stroke", ["width", mm(width_um) or 0.01], ["type", Sym("solid")]]


def _pad_shape(width: int, height: int, roundness: int) -> tuple[Sym, list]:
    """shape.md's `roundness` scale -> KiCad's own pad shapes. 100 % is a
    full round end (a circle when square, an oval otherwise), 0 % a plain
    rectangle, and anything between is KiCad 10's native roundrect, whose
    ratio is measured against the SHORT side — the same quantity our
    percentage means."""
    if roundness >= 100:
        return (Sym("circle") if width == height else Sym("oval")), []
    if roundness <= 0:
        return Sym("rect"), []
    return Sym("roundrect"), [["roundrect_rratio", round(roundness / 200, 4)]]


def _graphic_layer(ir_layer: int, name: str, log, back: bool = False) -> str | None:
    """Copper never reaches the table: its numbers come from the board's
    stack. Inside a FOOTPRINT, though, the only copper there can be is the
    mount side and its opposite (footprint.md forbids internal copper —
    a footprint knows no stack), so those two resolve here directly."""
    side = _COPPER_SIDE.get(ir_layer)
    if side is not None:
        return side_layer(f"{side}.Cu", back)
    mapped = kicad_layer(ir_layer, log, f"footprint {name!r}")
    return None if mapped is None else side_layer(mapped, back)


def write_graphic(g, name: str, log, back: bool = False, theta: float = 0.0) -> list | None:
    layer = _graphic_layer(g.layer, name, log, back)
    if layer is None:
        return None
    if getattr(g, "anti", False):
        # **An anti-object is the ABSENCE of copper**, and drawing it as
        # copper is worse than dropping it: the source subtracts here and
        # the output would add. Every shape with an area becomes a Rule
        # Area; only text has none (see `pen.subtracted_area`).
        try:
            edges = pen.subtracted_area(g)
        except ValueError as exc:
            log(f"footprint {name!r}: anti-object on layer {g.layer} — {exc}; "
                f"вычитание не переносится, в KiCad медь останется на месте")
            return None
        return write_rule_area(edges, layer,
                                lambda v: mm_x(v, back), lambda v: mm_y(v, back), _uuid4)
    if isinstance(g, Line):
        return ["fp_line", ["start", mm_x(g.x1, back), mm_y(g.y1, back)], ["end", mm_x(g.x2, back), mm_y(g.y2, back)],
                _stroke(g.width), ["layer", layer], ["uuid", _uuid4()]]
    if isinstance(g, Arc):
        mx, my = g.midpoint()
        return ["fp_arc", ["start", mm_x(g.x1, back), mm_y(g.y1, back)],
                ["mid", mm_x(mx, back), mm_y(my, back)], ["end", mm_x(g.x2, back), mm_y(g.y2, back)],
                _stroke(g.width), ["layer", layer], ["uuid", _uuid4()]]
    if isinstance(g, Polygon):
        pts = ["pts"] + [["xy", mm_x(v.x, back), mm_y(v.y, back)] for v in g.vertices]
        return ["fp_poly", pts, _stroke(g.width),
                ["fill", Sym("yes" if g.fill else "no")],
                ["layer", layer], ["uuid", _uuid4()]]
    if isinstance(g, Shape):
        w, h = (g.h, g.w) if g.rot in (90000, 270000) else (g.w, g.h)
        fill = Sym("no" if g.outline else "yes")
        if g.roundness >= 100:
            return ["fp_circle", ["center", mm_x(g.x, back), mm_y(g.y, back)],
                    ["end", mm_x(g.x + w // 2, back), mm_y(g.y, back)],
                    _stroke(g.outline), ["fill", fill], ["layer", layer], ["uuid", _uuid4()]]
        return ["fp_rect", ["start", mm_x(g.x - w // 2, back), mm_y(g.y - h // 2, back)],
                ["end", mm_x(g.x + w // 2, back), mm_y(g.y + h // 2, back)],
                _stroke(g.outline), ["fill", fill], ["layer", layer], ["uuid", _uuid4()]]
    if isinstance(g, Text):
        return ["fp_text", Sym("user"), g.content,
                ["at", mm_x(g.x, back), mm_y(g.y, back), text_rot(g.rot, theta, back)],
                ["layer", layer], ["uuid", _uuid4()], _effects(g.height, g.align, back)]
    log(f"footprint {name!r}: graphic {type(g).__name__} has no footprint equivalent — dropped")
    return None


def write_pad(p, name: str, log, back: bool = False, theta: float = 0.0) -> list | None:
    if isinstance(p, Hole):
        # hole.md: no copper, no net, no name — KiCad's own unplated pad.
        return ["pad", "", Sym("np_thru_hole"), Sym("circle"),
                ["at", mm_x(p.x, back), mm_y(p.y, back)], ["size", mm(p.drill), mm(p.drill)],
                ["drill", mm(p.drill)], ["layers", "F&B.Cu", "*.Mask"], ["uuid", _uuid4()]]

    shape, extra = _pad_shape(p.width, p.height, p.roundness)
    node = ["pad", p.name]
    if isinstance(p, Smd):
        side = _COPPER_SIDE.get(p.layer)
        if side is None:
            log(f"footprint {name!r}: pad {p.name!r} names copper layer {p.layer}, which a "
                f"footprint cannot have — dropped")
            return None
        layers = [side_layer(f"{side}.Cu", back)]
        if p.stopmask:
            layers.append(side_layer(f"{side}.Mask", back))
        if p.paste:
            layers.append(side_layer(f"{side}.Paste", back))
        node += [Sym("smd"), shape,
                 ["at", mm_x(p.x, back), mm_y(p.y, back), fp_rot(p.rot, theta, back)],
                 ["size", mm(p.width), mm(p.height)],
                 ["layers", *layers]]
        node += extra
    else:
        node += [Sym("thru_hole"), shape,
                 ["at", mm_x(p.x, back), mm_y(p.y, back), fp_rot(p.rot, theta, back)],
                 ["size", mm(p.width), mm(p.height)],
                 ["drill", mm(p.drill)],
                 ["layers", "*.Cu", "*.Mask" if p.stopmask else "F.Mask"]]
        node += extra
        if p.inner_dia == 0:
            # pad.md: no inner ring at all — KiCad's own "remove unused
            # layers" is the nearest thing it has.
            node.append(["remove_unused_layers", Sym("yes")])
        elif p.inner_dia is not None and p.inner_dia != p.width:
            # **A smaller ring on the inner layers is a real padstack**, and
            # KiCad 10 has one: `front_inner_back` gives the three groups
            # the IR's `diameter`/`inner_dia` pair already names. Ground
            # truth for the shape of the node:
            # `testData/kicad/OpenESC_20X20-main/hardware/4in1-mini.kicad_pcb`.
            #
            # It matters beyond looks: an inner ring drawn at the OUTER
            # size eats the clearance an Eagle board was designed with, and
            # KiCad then reports violations the source never had.
            inner = mm(p.inner_dia)
            node.append(["padstack", ["mode", Sym("front_inner_back")],
                         ["layer", "Inner", ["shape", shape], ["size", inner, inner]],
                         ["layer", "B.Cu", ["shape", shape],
                          ["size", mm(p.width), mm(p.height)]]])
    if not p.thermals:
        node.append(["zone_connect", 2])   # solid, no thermal spokes
    node.append(["uuid", _uuid4()])
    return node


def _file_angle(deg: float) -> float:
    """KiCad's FILE stores the dialog's angle NEGATED, folded to (-180,180]."""
    v = -deg % 360
    return round(v - 360 if v > 180 else v, 6)


def edges_to_pts(edges, xf, yf) -> list:
    """A boundary from `ir.pen` as a KiCad `(pts ...)`.

    A straight edge contributes its start point; an arc contributes the
    whole `(arc (start) (mid) (end))`, and the point after it is skipped —
    the arc has named its own end already. KiCad takes arcs inside `pts`,
    so nothing here is flattened into a polyline."""
    pts = ["pts"]
    after_arc = False
    for e in edges:
        kind, x1, y1, x2, y2, _curve = e
        if kind == "arc":
            mx, my = pen.edge_midpoint(e)
            pts.append(["arc",
                        ["start", round(xf(x1), 4), round(yf(y1), 4)],
                        ["mid", round(xf(mx), 4), round(yf(my), 4)],
                        ["end", round(xf(x2), 4), round(yf(y2), 4)]])
            after_arc = True
            continue
        if not after_arc:
            pts.append(["xy", round(xf(x1), 4), round(yf(y1), 4)])
        after_arc = False
    return pts


def write_rule_area(edges, layer: str, xf, yf, uuid_of) -> list:
    """An [anti-object](../spec/layer-model.md) as a KiCad Rule Area.

    **It forbids the POUR and nothing else.** layer-model.md is exact: an
    anti-object subtracts from `<polygon>`s of its layer and leaves lines,
    pads and text alone — "линии и текст не затрагиваются". KiCad's own
    Eagle import forbids tracks, vias and pads here as well; that is a
    stricter reading than the IR holds, and copying it would invent a
    restriction the source never stated."""
    return ["zone", ["layer", layer], ["uuid", uuid_of()],
            ["hatch", Sym("edge"), 0.5],
            ["keepout", ["tracks", Sym("allowed")], ["vias", Sym("allowed")],
             ["pads", Sym("allowed")], ["copperpour", Sym("not_allowed")],
             ["footprints", Sym("allowed")]],
            ["placement", ["enabled", Sym("no")], ["sheetname", ""]],
            ["fill", ["thermal_gap", 0.5], ["thermal_bridge_width", 0.5],
             ["island_removal_mode", 0]],
            ["polygon", edges_to_pts(edges, xf, yf)]]


def write_model(m, path: str) -> list:
    """One `(model ...)` — the 3D model as KiCad spells it.

    **The offset passes through unchanged, Y included.** It lives in the
    MODEL's own MCAD frame (Z-up, Y-up), not in the footprint's Y-down
    2D frame, so `mm_y`'s negation must NOT be applied here. Ground truth
    from the previous Babel: `maximus` DD1 `ty=+7` and XS1 `ty=+9.5`,
    confirmed by eye in KiCad.

    **The rotation is recomposed, not copied axis by axis** — see
    `Model3D.dialog_rotation` — and then negated, because KiCad's file
    stores the negation of what its own dialog shows."""
    a, b, g = m.dialog_rotation()
    return ["model", path,
            ["offset", ["xyz", mm(m.tx), mm(m.ty), mm(m.tz)]],
            ["scale", ["xyz", 1, 1, 1]],
            ["rotate", ["xyz", _file_angle(a), _file_angle(b), _file_angle(g)]]]


def _placeholder(fp, key: str):
    for g in fp.graphics:
        if isinstance(g, Text) and g.content.upper().startswith(">" + key):
            return g
    return None


def _property(prop: str, value: str, placeholder, default_layer: str, hidden: bool,
               back: bool = False, theta: float = 0.0) -> list:
    if placeholder is not None:
        at = ["at", mm_x(placeholder.x, back), mm_y(placeholder.y, back),
              fp_rot(placeholder.rot, theta, back)]
        layer = kicad_layer(placeholder.layer, lambda *_a: None, "") or default_layer
        effects = _effects(placeholder.height, placeholder.align, back)
        hidden = False
    else:
        # A field the footprint draws no placeholder for still has an
        # angle, and it goes through `fp_rot` like every other child —
        # with a zero library angle. Computing `theta` here directly
        # skipped the half turn the back side carries, and those three
        # fields alone came back as a `lib_footprint_mismatch` on every
        # bottom footprint.
        at = ["at", 0, 0, fp_rot(0, theta, back)]
        layer = default_layer
        effects = _effects(1270, "center-center", back)
    layer = side_layer(layer, back)
    node = ["property", prop, value, at, ["layer", layer]]
    if hidden:
        node.append(["hide", Sym("yes")])
    node += [["uuid", _uuid4()], effects]
    return node


def write_footprint(fp, log, model_paths: dict[str, str] | None = None, *,
                     back: bool = False, theta: float = 0.0) -> list:
    """One `.kicad_mod`. `Reference`/`Value` take their place from the
    `>NAME`/`>VALUE` placeholders the footprint already carries (the same
    convention the symbol side uses), and those two Texts are then not
    drawn again as ordinary graphics — in KiCad they ARE the properties.

    `model_paths` maps a footprint name to the path its 3D model got in
    the output — the caller resolves it, because only the caller knows
    which of `.step`/`.stp` the source actually shipped (model3d.md keeps
    the base name, never the extension)."""
    name_ph, value_ph = _placeholder(fp, "NAME"), _placeholder(fp, "VALUE")
    node = ["footprint", fp.name,
            ["version", _KICAD_MOD_VERSION], ["generator", "babel"],
            ["generator_version", "10.0"], ["layer", side_layer("F.Cu", back)],
            _property("Reference", "REF**", name_ph, "F.SilkS", False, back, theta),
            _property("Value", fp.name, value_ph, "F.Fab", True, back, theta),
            _property("Datasheet", "", None, "F.Fab", True, back, theta),
            _property("Description", "", None, "F.Fab", True, back, theta),
            ["duplicate_pad_numbers_are_jumpers", Sym("no")]]

    for g in fp.graphics:
        if g is name_ph or g is value_ph:
            continue
        written = write_graphic(g, fp.name, log, back, theta)
        if written is not None:
            node.append(written)
    for p in list(fp.pads) + list(fp.holes):
        written = write_pad(p, fp.name, log, back, theta)
        if written is not None:
            node.append(written)

    model_path = (model_paths or {}).get(fp.name)
    if fp.models and model_path is not None:
        fallback = next((m for m in fp.models if m.key is None), None)
        if fallback is not None:
            node.append(write_model(fallback, model_path))

    node.append(["embedded_fonts", Sym("no")])
    return node
