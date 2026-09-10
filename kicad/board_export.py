"""IR `Layout` -> KiCad `.kicad_pcb` content.

Board space shares the footprint's conventions, not the schematic's:
**Y runs down and angles are negated** (`footprint_export.mm_y` /
`fp_rot`), which is where the whole file gets its coordinates from.

Nets are addressed BY NAME (`(net "VDD_3V3")`) — KiCad 10 keeps no
separate index table, so nothing here has to number them.
"""

from __future__ import annotations

import uuid as _uuid

from ir.element import Side
from ir import pen
from ir.graphics import Arc, Line, Polygon, Shape, Text
from ir.module_instance import expand_designator
from ir.stack import copper_layer_numbers, parse_stack
from ir.via import Via

from . import footprint_export as fp_export
from .footprint_export import fp_rot, mm, mm_y
from .layers import kicad_layer
from .sexpr import Sym

_KICAD_PCB_VERSION = 20260206  # ground truth: tolmach.kicad_pcb

# Non-copper layers KiCad wants declared even when nothing draws on them,
# with the numbering KiCad 10 itself uses (ground truth: tolmach.kicad_pcb
# and OpenESC's six-layer board — copper is interleaved with its masks,
# F.Cu 0 / F.Mask 1 / B.Cu 2 / B.Mask 3, and inner copper starts at 4).
_FIXED_LAYERS = [
    (1, "F.Mask", "user", None), (3, "B.Mask", "user", None),
    (5, "F.SilkS", "user", "F.Silkscreen"), (7, "B.SilkS", "user", "B.Silkscreen"),
    (9, "F.Adhes", "user", "F.Adhesive"), (11, "B.Adhes", "user", "B.Adhesive"),
    (13, "F.Paste", "user", None), (15, "B.Paste", "user", None),
    (17, "Dwgs.User", "user", "User.Drawings"), (19, "Cmts.User", "user", "User.Comments"),
    (21, "Eco1.User", "user", "User.Eco1"), (23, "Eco2.User", "user", "User.Eco2"),
    (25, "Edge.Cuts", "user", None), (27, "Margin", "user", None),
    (29, "B.CrtYd", "user", "B.Courtyard"), (31, "F.CrtYd", "user", "F.Courtyard"),
    (33, "B.Fab", "user", None), (35, "F.Fab", "user", None),
]


def _uuid4() -> str:
    return str(_uuid.uuid4())


BOARD_MARGIN = 10000
"""Clear space wanted between the board's own extent and the paper edge, µm."""

_PAPERS = [("A4", 297000, 210000), ("A3", 420000, 297000), ("A2", 594000, 420000),
           ("A1", 841000, 594000), ("A0", 1189000, 841000)]


class Space:
    """The board's placement on the KiCad page.

    **The IR origin lands at the CENTRE of the sheet**, and the board sits
    around it exactly as the source drew it — so the layout keeps the
    relationship to its own (0, 0) that its author chose, instead of being
    shoved into a corner. KiCad draws a page frame even in the board
    editor, and a board pushed against its top-left reads as a mistake.

    Ground truth, and a pleasing one: KiCad's own import of this project
    put Eagle's origin at 148.501, 105.004 — the centre of A4 to within
    its own rounding.

    Y still flips (board space runs down); sizes and widths do NOT pass
    through here — they are magnitudes."""

    def __init__(self, paper: str, width_um: int, height_um: int) -> None:
        self.paper = paper
        self.cx, self.cy = width_um / 2000, height_um / 2000

    def x(self, value_um: int) -> float:
        return self.cx + value_um / 1000

    def y(self, value_um: int) -> float:
        return self.cy - value_um / 1000


def board_space(layout) -> Space:
    """The smallest standard sheet the board fits on, centred. Fitting is
    measured from the IR origin outward, since that is what lands in the
    middle: a layout drawn far off its own origin needs the paper the
    REACH demands, not the one its bounding box would."""
    reach_x, reach_y = 0, 0

    def add(x, y):
        nonlocal reach_x, reach_y
        reach_x = max(reach_x, abs(x))
        reach_y = max(reach_y, abs(y))

    for el in layout.elements:
        add(el.x, el.y)
    for h in layout.holes:
        add(h.x, h.y)
    for source in [layout.graphics] + [s.copper for s in layout.signals]:
        for g in source:
            if isinstance(g, (Line, Arc)):
                add(g.x1, g.y1)
                add(g.x2, g.y2)
            elif isinstance(g, Polygon):
                for v in g.vertices:
                    add(v.x, v.y)
            else:
                add(g.x, g.y)

    for name, w, h in _PAPERS:
        if 2 * reach_x + 2 * BOARD_MARGIN <= w and 2 * reach_y + 2 * BOARD_MARGIN <= h:
            return Space(name, w, h)
    name, w, h = _PAPERS[-1]
    return Space(name, w, h)


def copper_names(stack: str) -> dict[int, str]:
    """IR copper number -> KiCad copper layer name. layout.md numbers top 1,
    bottom -1 and internals 2..N-1 downward; KiCad names them F.Cu, B.Cu and
    In1..InN, which is the same order under different words."""
    copper, _ = parse_stack(stack)
    numbers = copper_layer_numbers(len(copper))
    out = {}
    inner = 0
    for n in numbers:
        if n == 1:
            out[n] = "F.Cu"
        elif n == -1:
            out[n] = "B.Cu"
        else:
            inner += 1
            out[n] = f"In{inner}.Cu"
    return out


def _layer_of(ir_layer: int, cu: dict[int, str], log, what: str) -> str | None:
    if ir_layer in cu:
        return cu[ir_layer]
    if abs(ir_layer) < 100:
        log(f"{what}: copper layer {ir_layer} is not in this board's stack — dropped")
        return None
    return kicad_layer(ir_layer, log, what)


def write_layers(stack: str) -> list:
    cu = copper_names(stack)
    node = ["layers"]
    node.append([0, "F.Cu", Sym("signal")])
    node.append([2, "B.Cu", Sym("signal")])
    index = 4
    for number, name in cu.items():
        if name.startswith("In"):
            node.append([index, name, Sym("signal")])
            index += 2
    for num, name, kind, alias in _FIXED_LAYERS:
        row = [num, name, Sym(kind)]
        if alias:
            row.append(alias)
        node.append(row)
    return node


def _thickness_mm(stack: str) -> float:
    copper, dielectrics = parse_stack(stack)
    return (sum(copper) + sum(d.thickness for d in dielectrics)) / 1000


def write_setup(layout) -> list:
    """Only what the IR actually models. `rules.md` gives six numbers and
    conversion-kicad.md is explicit that the rest is synthesized rather
    than passed through — KiCad fills its own defaults for everything
    absent here, and pretending otherwise would invent a DRC nobody
    specified."""
    node = ["setup", ["pad_to_mask_clearance", mm(layout.mask_expansion)],
            ["pad_to_paste_clearance", mm(layout.paste_expansion)],
            ["allow_soldermask_bridges_in_footprints", Sym("no")]]
    return node


def write_element(element, footprint, part, sp, pad_nets: dict,
                   footprint_pool: str, log, sym_path: str | None = None,
                   model_paths: dict | None = None) -> list:
    """One placed footprint. The library body is written afresh here rather
    than referenced: a `.kicad_pcb` carries its own full copy of every
    footprint it places, exactly as a `.kicad_sch` carries `lib_symbols`.

    `sym_path` is the KIID path of the schematic symbol this footprint
    stands for. It is not decoration: it is the ONLY thing tying the two
    together once "Re-link footprints to symbols based on their reference
    designators" is unticked, and without it KiCad reads every footprint
    as an orphan, tries to ADD each symbol afresh from the library, and
    strips the nets off the copper underneath. Ground truth: every real
    board in `testData/kicad` writes one per footprint."""
    back = element.side is Side.BOTTOM
    theta = element.rot % 360000 / 1000
    # **The footprint is generated already placed, not taken apart.** Side
    # and rotation are what the writer needs to emit the body correctly the
    # first time; picking a library body apart child by child afterwards is
    # how the local copy ended up differing from the library at all.
    body = fp_export.write_footprint(footprint, log, model_paths, back=back, theta=theta)
    # The placement angle is a literal copy on the front and `-θ - 180` on
    # the back — the other half of `fp_export.mm_x`'s bake, whose comment
    # carries the ground truth for the half turn.
    #
    # Four wrong answers preceded the underlying rule, all from guessing at
    # the same thing: the ORDER of Eagle's mirror and rotation. Every guess
    # could be made to place the pads correctly, because mirror-then-rotate
    # and rotate-then-mirror differ by a half turn that the placement angle
    # can absorb — so measurement kept agreeing while the parts sat visibly
    # turned around. The order is not derivable from the pads; it had to be
    # read off Eagle's own routed copper.
    rot = (-theta - 180) % 360 if back else theta % 360
    node = ["footprint", f"{footprint_pool}:{footprint.name}",
            ["layer", "B.Cu" if back else "F.Cu"],
            ["uuid", _uuid4()],
            ["at", sp.x(element.x), sp.y(element.y), rot]]
    if sym_path is not None:
        node.append(["path", sym_path])

    attrs_by_key = {a.name.upper(): a.value for a in (part.attrs if part else [])}
    for child in body[1:]:
        if not isinstance(child, list):
            continue
        tag = child[0]
        if tag in ("version", "generator", "generator_version", "layer"):
            continue
        if tag == "property" and child[1] == "Reference":
            child = list(child)
            child[2] = element.name
        elif tag == "property" and child[1] == "Value":
            child = list(child)
            child[2] = attrs_by_key.get("VALUE", footprint.name)
        elif tag == "pad":
            net = pad_nets.get(child[1])
            if net is not None:
                child = list(child) + [["net", net]]
        node.append(child)
    return node


_SIDE_FLIP = {"F": "B", "B": "F"}


def _flip_layer(name: str) -> str:
    """`F.SilkS` <-> `B.SilkS`; a side-less layer (`*.Cu`, `Edge.Cuts`,
    `Dwgs.User`) is the same on both sides and stays put."""
    if len(name) > 2 and name[0] in _SIDE_FLIP and name[1] == ".":
        return _SIDE_FLIP[name[0]] + name[1:]
    return name


def _mirror_effects(effects: list) -> list:
    out = list(effects)
    for i, part in enumerate(out):
        if isinstance(part, list) and part and part[0] == "justify":
            out[i] = list(part) + [Sym("mirror")]
            return out
    return out + [["justify", Sym("mirror")]]


def write_track(line, net: str, sp, cu: dict[int, str], log) -> list | None:
    layer = _layer_of(line.layer, cu, log, f"signal {net!r} track")
    if layer is None:
        return None
    if isinstance(line, Arc):
        mx, my = line.midpoint()
        return ["arc", ["start", sp.x(line.x1), sp.y(line.y1)],
                ["mid", sp.x(mx), sp.y(my)], ["end", sp.x(line.x2), sp.y(line.y2)],
                ["width", mm(line.width)], ["layer", layer], ["net", net], ["uuid", _uuid4()]]
    return ["segment", ["start", sp.x(line.x1), sp.y(line.y1)],
            ["end", sp.x(line.x2), sp.y(line.y2)],
            ["width", mm(line.width)], ["layer", layer], ["net", net], ["uuid", _uuid4()]]


def write_via(via, net: str, sp, cu: dict[int, str], log) -> list:
    outer = [cu[n] for n in (1, -1) if n in cu]
    return ["via", ["at", sp.x(via.x), sp.y(via.y)],
            ["size", mm(via.diameter)], ["drill", mm(via.drill)],
            ["layers", *outer], ["net", net], ["uuid", _uuid4()]]


_RANK_TOP = 7
"""**The two scales run opposite ways.** polygon.md: `rank` is 1-6 and the
SMALLER one wins the disputed area; KiCad's `priority` is the other way
round, the LARGER winning. So the number is turned over rather than
copied, and polygon.md names the constant itself — "KiCad —
`priority = 7 - rank`". Writing `rank` straight through, as this did,
inverts every pour that overlaps another: the polygon meant to yield
takes the copper instead."""


def zone_outline(poly, sp, log, what: str) -> list:
    """The `pts` of a zone — the polygon's COPPER BOUNDARY, not its stored
    contour.

    polygon.md keeps the centre line of a round pen of width `width`, and
    says the copper edge stands `width/2` OUTSIDE it. A KiCad zone has no
    pen: its outline IS the edge of the copper. So the contour has to be
    offset outward by half the pen, or the pour comes out a full pen width
    narrower than the source drew it. (`gr_poly`/`fp_poly` need none of
    this — they carry a `stroke` of the same width, which reproduces the
    pen by itself. The zone is the one shape that cannot.)

    Round joins mean the boundary carries ARCS, and KiCad takes them
    inside `pts` as `(arc (start) (mid) (end))` — so nothing is flattened
    into a polyline on the way out. A point is written for a straight
    edge's start; after an arc it is skipped, the arc having named its own
    end already.

    A contour that genuinely has no offset boundary (a neck thinner than
    the pen) is reported and travels on its centre line: a pour missing
    half a pen is a smaller fault than a pour missing entirely."""
    try:
        edges = pen.copper_boundary(poly)
    except ValueError as exc:
        log(f"{what}: {exc} — зона написана по осевой линии, её медь окажется уже "
            f"исходной на {mm(poly.width)} мм по краю")
        return ["pts"] + [["xy", sp.x(v.x), sp.y(v.y)] for v in poly.vertices]

    pts = ["pts"]
    after_arc = False
    for e in edges:
        kind, x1, y1, x2, y2, _curve = e
        if kind == "arc":
            mx, my = pen.edge_midpoint(e)
            pts.append(["arc",
                        ["start", round(sp.x(x1), 4), round(sp.y(y1), 4)],
                        ["mid", round(sp.x(mx), 4), round(sp.y(my), 4)],
                        ["end", round(sp.x(x2), 4), round(sp.y(y2), 4)]])
            after_arc = True
            continue
        if not after_arc:
            pts.append(["xy", round(sp.x(x1), 4), round(sp.y(y1), 4)])
        after_arc = False
    return pts


def write_zone(poly, net: str, sp, cu: dict[int, str], rules, log) -> list | None:
    """A signal's polygon is a KiCad zone — the pour, not the drawn
    outline, and its outline is the copper's edge (see `zone_outline`)."""
    layer = _layer_of(poly.layer, cu, log, f"signal {net!r} polygon")
    if layer is None:
        return None
    pts = zone_outline(poly, sp, log, f"signal {net!r} polygon on layer {poly.layer}")
    # polygon.md is explicit that there is no separate thermal gap: **the
    # relief gap IS the polygon's resolved clearance**, and the spoke's
    # floor is the polygon's own pen width. Resolved, not raw — `0` there
    # means "the board's floor stands", so it is `max` with rules.md's
    # clearance. Emitting the raw 0 let KiCad fall back to its own 0.5 mm,
    # which is what drew every relief twice as wide as the source.
    gap = max(poly.clearance, rules.clearance if rules is not None else 0)
    fill = ["fill", Sym("yes"), ["thermal_gap", mm(gap)],
            ["thermal_bridge_width", mm(poly.width)]]
    if poly.fill < 100:
        # conversion-kicad.md #фигуры-и-заливки: a percentage becomes a
        # hatch, the pen width being the stroke and the gap coming out of
        # the percentage that collapsed into it.
        gap = poly.width * (100 - poly.fill) / max(poly.fill, 1)
        fill += [["mode", Sym("hatch")], ["hatch_thickness", mm(poly.width)],
                 ["hatch_gap", round(gap / 1000, 4) or 0.5], ["hatch_orientation", 0]]
    # `connect_pads` takes `yes` for a solid join and NOTHING at all for a
    # thermal relief — there is no token for the latter, its absence IS the
    # setting (ground truth writes the solid case as `(connect_pads yes ...)`).
    connect = ["connect_pads"]
    if not poly.thermals:
        connect.append(Sym("yes"))
    connect.append(["clearance", mm(gap)])
    return ["zone", ["net", net], ["layer", layer], ["uuid", _uuid4()],
            ["hatch", Sym("edge"), 0.5], ["priority", _RANK_TOP - poly.rank], connect,
            ["min_thickness", mm(poly.width)], fill, ["polygon", pts]]


def hole_footprint_name(drill_um: int) -> str:
    """One name per drill diameter — KiCad's own convention for holes
    (`MountingHole_3.2mm` and friends): a library footprint has ONE drill,
    so a single shared `NPTH` would disagree with every hole but the first
    and DRC would say so, footprint by footprint."""
    return f"NPTH-{mm(drill_um)}mm"


def _hole_body(drill_um: int) -> list:
    """The parts a bare hole's footprint has in common wherever it is
    written — placed on the board, or on its own in the pool."""
    return [["attr", Sym("exclude_from_pos_files"), Sym("exclude_from_bom"),
             Sym("allow_missing_courtyard")],
            ["property", "Reference", "", ["at", 0, 0, 0], ["layer", "F.SilkS"],
             ["hide", Sym("yes")], ["uuid", _uuid4()],
             ["effects", ["font", ["size", 1.27, 1.27]]]],
            ["property", "Value", hole_footprint_name(drill_um), ["at", 0, 0, 0],
             ["layer", "F.Fab"], ["hide", Sym("yes")], ["uuid", _uuid4()],
             ["effects", ["font", ["size", 1.27, 1.27]]]],
            ["pad", "", Sym("np_thru_hole"), Sym("circle"), ["at", 0, 0],
             ["size", mm(drill_um), mm(drill_um)], ["drill", mm(drill_um)],
             ["layers", "F&B.Cu", "*.Mask"], ["uuid", _uuid4()]],
            ["embedded_fonts", Sym("no")]]


def write_hole_library_footprint(drill_um: int) -> list:
    """The same hole as a standalone `.kicad_mod`. Without it the board
    names a library that does not exist and KiCad says so once per hole
    ("The current configuration does not include the footprint library")
    — a placed footprint's `lib_id` has to resolve, even when the thing
    it stands for is nobody's component."""
    return ["footprint", hole_footprint_name(drill_um),
            ["version", fp_export._KICAD_MOD_VERSION], ["generator", "babel"],
            ["generator_version", "10.0"], ["layer", "F.Cu"],
            *_hole_body(drill_um)]


def write_board_hole(hole, sp, footprint_pool: str) -> list:
    """hole.md allows a bare unplated hole straight on the board; KiCad has
    no such object — a pad exists only inside a footprint. So each one is
    wrapped in a footprint of its own, marked as neither a real part nor
    something to appear in the BOM or the pick-and-place file.

    It carries no `(path ...)`: there is no symbol behind it, and that is
    the honest statement. It is also why "Delete footprints with no
    symbols" in KiCad's own F8 dialog must stay unticked on these boards."""
    return ["footprint", f"{footprint_pool}:{hole_footprint_name(hole.drill)}",
            ["layer", "F.Cu"], ["uuid", _uuid4()],
            ["at", sp.x(hole.x), sp.y(hole.y)],
            *_hole_body(hole.drill)]


def _upright(angle: float, align: str) -> tuple[float, str]:
    """KiCad never draws text upside down: an angle at or past 180° is the
    same reading turned round, so it folds into [0, 180) and the anchor
    swaps sides to keep the words where they were.

    Doing it ourselves rather than leaving it to KiCad matters for
    MIRRORED text: a mirrored caption written at 180° comes out as a
    mirror image, while the same caption at 0° reads correctly. Both
    appear in one board — text.md lets two labels on opposite edges face
    outward, half a turn apart — so without the fold exactly half of the
    bottom silkscreen is reversed, whichever way the rule is written."""
    if angle % 360 < 180:
        return angle % 360, align
    v, h = align.split("-")
    h = {"left": "right", "right": "left"}.get(h, h)
    return angle % 180, f"{v}-{h}"


def write_graphic(g, sp, cu: dict[int, str], log) -> list | None:
    layer = _layer_of(g.layer, cu, log, "board graphic")
    if layer is None:
        return None
    if getattr(g, "anti", False):
        # See footprint_export.write_graphic — the same rule, the same
        # reason: an anti-object drawn as copper is the opposite of what
        # the source says.
        try:
            edges = pen.subtracted_area(g)
        except ValueError as exc:
            log(f"board anti-object on layer {g.layer}: {exc}; вычитание не переносится, "
                f"в KiCad медь останется на месте")
            return None
        return fp_export.write_rule_area(edges, layer, sp.x, sp.y, _uuid4)
    stroke = ["stroke", ["width", mm(getattr(g, "width", 0) or getattr(g, "outline", 0)) or 0.05],
              ["type", Sym("solid")]]
    if isinstance(g, Line):
        return ["gr_line", ["start", sp.x(g.x1), sp.y(g.y1)], ["end", sp.x(g.x2), sp.y(g.y2)],
                stroke, ["layer", layer], ["uuid", _uuid4()]]
    if isinstance(g, Arc):
        mx, my = g.midpoint()
        return ["gr_arc", ["start", sp.x(g.x1), sp.y(g.y1)], ["mid", sp.x(mx), sp.y(my)],
                ["end", sp.x(g.x2), sp.y(g.y2)], stroke, ["layer", layer], ["uuid", _uuid4()]]
    if isinstance(g, Polygon):
        pts = ["pts"] + [["xy", sp.x(v.x), sp.y(v.y)] for v in g.vertices]
        return ["gr_poly", pts, stroke, ["fill", Sym("yes" if g.fill else "no")],
                ["layer", layer], ["uuid", _uuid4()]]
    if isinstance(g, Shape):
        w, h = (g.h, g.w) if g.rot in (90000, 270000) else (g.w, g.h)
        if g.roundness >= 100:
            return ["gr_circle", ["center", sp.x(g.x), sp.y(g.y)],
                    ["end", sp.x(g.x + w // 2), sp.y(g.y)], stroke,
                    ["fill", Sym("no" if g.outline else "yes")],
                    ["layer", layer], ["uuid", _uuid4()]]
        return ["gr_rect", ["start", sp.x(g.x - w // 2), sp.y(g.y - h // 2)],
                ["end", sp.x(g.x + w // 2), sp.y(g.y + h // 2)], stroke,
                ["fill", Sym("no" if g.outline else "yes")],
                ["layer", layer], ["uuid", _uuid4()]]
    if isinstance(g, Text):
        # Mirrored text — text.md's own `mirror`, which is what bottom-side
        # lettering carries so it reads correctly from that side. KiCad
        # spells it `mirror` in the justify, the same token a back-side
        # footprint's text takes, **and the angle mirrors with it**: a flip
        # about the vertical axis sends a direction `φ` to `180 - φ`. The
        # token alone leaves every caption a half-turn out (the same
        # "extra 180°" the placed footprints had). Driven by the IR flag,
        # not by the layer: a back layer does not oblige text to mirror,
        # and the source says which it is.
        # **A caption's angle is the source's own** — see
        # `fp_export.text_rot`. Only a MIRRORED one reverses, and that is
        # the mirror doing it, not the change of Y direction: ground truth
        # from KiCad's own Eagle import, where `MR90` arrives as `-90` and
        # `MR0` as `-0`.
        angle = -g.rot / 1000 if g.mirror else g.rot / 1000
        angle, align = _upright(angle, g.align)
        effects = fp_export._effects(g.height, align)
        if g.mirror:
            effects = _mirror_effects(effects)
        return ["gr_text", g.content, ["at", sp.x(g.x), sp.y(g.y), angle],
                ["layer", layer], ["uuid", _uuid4()], effects]
    log(f"board graphic {type(g).__name__} has no equivalent — dropped")
    return None


def kicad_net_name(signal, channels) -> str:
    """What KiCad will call this signal — the ONE place that answers it, so
    the board's copper and the schematic's labels cannot drift apart.

    Two rules, both mechanical:

    - **A channel's own net: `CHANNEL:NAME` -> `/CHANNEL/NAME`.** The colon
      form is Eagle's own on the flat board (`step4.brd` writes
      `DCDC1:SW`); the slash form is KiCad's own for the same thing — the
      net of a local label inside sheet `DCDC1`. Same name, each tool's
      spelling.
    - **Copper nobody claims travels with no net at all.** signal.md draws
      the line at `<contactref>`, not at the name: a signal with none
      asserts nothing about connectivity, and its name is Eagle's private
      bookkeeping that no schematic can carry. conversion-kicad.md
      #имена-цепей spells out the consequence — such copper goes out whole
      but unnamed, because a net absent from the netlist is one the first
      "Update PCB from Schematic" erases. Ground truth for the spelling:
      KiCad 10 reads `(net "")` and REFUSES the old `(net 0)`.
    """
    if not signal.contactrefs:
        return ""
    channel, sep, rest = signal.name.partition(":")
    if sep and channel in channels:
        return f"/{channel}/{rest}"
    return signal.name


def write_board(layout, project, footprints_by_name, footprint_pool: str, log,
                 symbol_paths: dict | None = None, model_paths: dict | None = None) -> list:
    cu = copper_names(layout.stack)
    sp = board_space(layout)
    # Channels included — a board calls them by their expanded designator.
    parts_by_name = {p.name: p for p in project.schematic.parts}
    modules_by_name = {m.name: m for m in project.modules}
    for mi in project.schematic.modinsts:
        module = modules_by_name.get(mi.module)
        if module is not None:
            for p in module.schematic.parts:
                parts_by_name[expand_designator(p.name, mi.name, mi.offset)] = p

    channels = {mi.name for mi in project.schematic.modinsts}
    net_names = {signal.name: kicad_net_name(signal, channels) for signal in layout.signals}
    # Only copper is worth a line: a signal that is empty AND unclaimed
    # writes nothing at all, and saying so would be noise.
    unnamed = sorted(s.name for s in layout.signals if not net_names[s.name] and s.copper)
    if unnamed:
        log(f"{len(unnamed)} signal(s) carry copper but no <contactref> — that copper travels "
            f"whole but UNNAMED, as conversion-kicad.md #имена-цепей requires; only the Eagle "
            f"name is lost ({', '.join(unnamed[:6])}{', …' if len(unnamed) > 6 else ''})")

    # contactref -> which net each element's pad belongs to. The board says
    # it once per signal; a placed pad needs it the other way round.
    pad_nets: dict[str, dict[str, str]] = {}
    for signal in layout.signals:
        for ref in signal.contactrefs:
            pad_nets.setdefault(ref.element, {})[ref.pad] = net_names[signal.name]

    node = ["kicad_pcb", ["version", _KICAD_PCB_VERSION], ["generator", "babel"],
            ["generator_version", "10.0"],
            ["general", ["thickness", _thickness_mm(layout.stack)],
             ["legacy_teardrops", Sym("no")]],
            ["paper", sp.paper],
            write_layers(layout.stack),
            write_setup(layout)]

    for element in layout.elements:
        if element.exclude:
            log(f"element {element.name!r}: excluded from this board (a ghost) — not placed")
            continue
        part = parts_by_name.get(element.name)
        footprint = footprints_by_name.get(element.name)
        if footprint is None:
            log(f"element {element.name!r}: no footprint resolved — not placed")
            continue
        sym_path = (symbol_paths or {}).get(element.name)
        if sym_path is None and part is not None:
            log(f"element {element.name!r}: no schematic symbol to link to — "
                f"KiCad will read it as an orphan on the first «Update PCB from Schematic»")
        node.append(write_element(element, footprint, part, sp,
                                   pad_nets.get(element.name, {}), footprint_pool, log,
                                   sym_path, model_paths))

    for g in layout.graphics:
        written = write_graphic(g, sp, cu, log)
        if written is not None:
            node.append(written)
    for hole in layout.holes:
        node.append(write_board_hole(hole, sp, footprint_pool))

    # via.md keeps an inner-layer ring of its own; KiCad's `(via ...)` has
    # one size for the whole stack and no padstack (tested — the token is
    # rejected there, though a PAD accepts it). So the narrower inner ring
    # is a real loss, and a loud one: the copper KiCad draws on inner
    # layers is wider than the source's, which eats the clearance the
    # board was designed with and shows up as violations it never had.
    narrow = [s.name for s in layout.signals
              for v in s.copper
              if isinstance(v, Via) and v.inner_dia is not None and v.inner_dia != v.diameter]
    if narrow:
        log(f"{len(narrow)} переход(ов) имеют на внутренних слоях кольцо уже, чем на внешних "
            f"(via.md `inner_dia`) — у KiCad переход одного размера на весь стек, и это "
            f"НЕ ВЫРАЗИМО: на внутренних слоях медь выйдет шире исходной, а DRC покажет "
            f"зазоры, которых в источнике не было")

    for signal in layout.signals:
        net = net_names[signal.name]
        for item in signal.copper:
            if isinstance(item, Via):
                node.append(write_via(item, net, sp, cu, log))
            elif isinstance(item, Polygon):
                written = write_zone(item, net, sp, cu, layout.rules, log)
                if written is not None:
                    node.append(written)
            elif isinstance(item, (Line, Arc)):
                written = write_track(item, net, sp, cu, log)
                if written is not None:
                    node.append(written)
            else:
                log(f"signal {signal.name!r}: {type(item).__name__} has no board equivalent yet "
                    f"— dropped")

    node.append(["embedded_fonts", Sym("no")])
    return node
