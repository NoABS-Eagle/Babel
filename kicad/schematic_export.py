"""IR `Schematic` -> KiCad `.kicad_sch` content — conversion-kicad.md
#страницы (the "В KiCad" pass). Placed symbol instances first; wires,
labels, nets and page/frame tiling follow in later passes.
"""

from __future__ import annotations

import math
import uuid as _uuid

from eagle import geometry as geo
from ir.graphics import Shape
from ir.label import LabelStyle
from ir.pin import Direction

from . import library_export as lib_export
from .library_export import _placeholder_positions, field_rot, justify, kicad_rot, mm
from .sexpr import Sym

_KICAD_VERSION = 20260306  # matches the ground-truth .kicad_sch header

_PAGE_MARGIN = 19050
"""Blank border between the IR frame's own bbox and the paper edge. KiCad
draws its page border and title block itself (the IR frame isn't placed —
see `write_page`), and it draws them INSIDE the paper: a paper cut exactly
to the frame would have that border run straight through the drawing.
Ground truth, not a guess — it is the margin KiCad's own Eagle import
chose for this very project: 19050 µm above the frame, and a paper
260.35 + 2 x 19.05 = 298.45 mm wide, to the µm."""


def _uuid4() -> str:
    return str(_uuid.uuid4())


def _page_x(value_um: int, x0: int) -> float:
    """Placed (schematic-canvas) content shares one flip origin per page
    — its own frame's bbox — unlike a symbol's internal graphics, which
    are copied verbatim in their own Y-up space (library_export.mm): KiCad fixes
    a page's own (0,0) at its top-left corner, so canvas content has to
    land inside [0, width]x[0, height] of THIS page, not float off in
    whatever range the IR frame happened to occupy on the shared canvas
    (multiple pages tile side by side there, eagle.schematic_export's
    own build_sheets — only page 1 sits at x0=0)."""
    return (value_um - x0) / 1000


def _page_y(value_um: int, y1: int) -> float:
    """The Y half of `_page_x`'s own reasoning, plus **the one Y flip in
    this exporter**: the placed canvas is the only space where KiCad's Y
    runs down (a symbol's own space is Y-up on both sides — see
    library_export.mm). IR's frame TOP (`y1`, the larger Y-up value)
    lands at KiCad's y=0 and the frame BOTTOM at y=(y1-y0), the page's
    own height. Ground truth for the whole transform, offsets included:
    across all 47 parts the two tolmach files share (Eagle `.sch` and the
    same project imported by KiCad itself), `kicad_x = ir_x - frame_left`
    and `kicad_y = frame_top - ir_y` hold exactly, with zero outliers —
    and the same two formulas place all 75 of their smashed fields."""
    return (y1 - value_um) / 1000


def _instance_property(name: str, value: str, x_um: int, y_um: int, rot_mdeg: int,
                        origin: tuple[int, int], hidden: bool,
                        height_um: int = 1270, align: str = "center-center") -> list:
    """Same fields as library_export.write_property, but KiCad's own
    instance-placement serializer orders them differently (`hide` right
    after `at`, not right before `effects`) — kept as ground-truthed,
    even though sub-node order shouldn't matter to a real parser. The
    angle folds through `field_rot` and the align rides along untouched,
    for the reason documented there."""
    x0, y1 = origin
    node = ["property", name, value,
            ["at", _page_x(x_um, x0), _page_y(y_um, y1), field_rot(rot_mdeg)]]
    if hidden:
        node.append(["hide", Sym("yes")])
    effects = ["effects", ["font", ["size", mm(height_um), mm(height_um)]]]
    tokens = justify(align)
    if tokens:
        effects.append(["justify", *tokens])
    node += [["show_name", Sym("no")], ["do_not_autoplace", Sym("no")], effects]
    return node


def _resolve_unit(component, gate_name: str) -> int:
    for i, g in enumerate(component.gates, start=1):
        if g.name == gate_name:
            return i
    raise ValueError(f"component {component.name!r}: instance references unknown gate {gate_name!r}")


def write_instance(inst, part, component, device, gate1_symbol, symbol_name: str,
                    footprint_pool: str, project_name: str, occurrences: list[tuple[str, str]],
                    origin: tuple[int, int], log, paths_out: dict | None = None) -> list:
    """One placed `(symbol ...)` — conversion-kicad.md #страницы and
    #библиотеки: `lib_id` names the per-device symbol
    (`write_component`'s own naming, `<component><device.name>`), `unit`
    is this instance's gate position among `component.gates` (KiCad's
    native per-unit mechanism, gate.md), and every property below either
    repeats this instance's own data (Reference/Value/Footprint/
    Datasheet) or a placeholder override — library default when
    `inst.texts` is empty, this instance's own exhaustive list otherwise
    (component-instance.md: present means "this is everything")."""
    unit = _resolve_unit(component, inst.gate)
    lib_id = f"{component.library}:{symbol_name}"
    x0, y1 = origin

    node = ["symbol", ["lib_id", lib_id],
            ["at", _page_x(inst.x, x0), _page_y(inst.y, y1), kicad_rot(inst.rot)]]
    if inst.mirror:
        # `mirror y` — the horizontal flip, the same axis Eagle's `M` and
        # the IR's own `mirror` mean, and KiCad composes it the same way
        # (mirror first, then rotate: units.md #зеркало-применяется-до-
        # поворота), so the angle beside it stays a literal copy.
        # Ground truth writes the mirrored-and-rotated case as the
        # equivalent `mirror x` with rot+180 (R(θ)·My == R(θ+180)·Mx);
        # this is the branch that keeps one rule instead of two.
        node.append(["mirror", Sym("y")])
    # The symbol's own KIID. It is the last segment of the path a placed
    # footprint carries on the board, which is the ONLY link "Update PCB
    # from Schematic" has when "Re-link footprints by reference" is off —
    # so it is handed back to the caller rather than thrown away.
    sym_uuid = _uuid4()
    node += [["unit", unit], ["body_style", 1],
             ["exclude_from_sim", Sym("no")], ["in_bom", Sym("yes")], ["on_board", Sym("yes")],
             ["in_pos_files", Sym("yes")], ["dnp", Sym("no")], ["uuid", sym_uuid]]

    attrs_by_key = {a.name.upper(): a.value for a in part.attrs}
    value = attrs_by_key.get("VALUE", "")
    footprint_ref = f"{footprint_pool}:{device.footprint}" if device is not None else ""
    datasheet = attrs_by_key.get("DATASHEET", "")

    # component-instance.md: `inst.texts`, when present, is already
    # absolute canvas coordinates (an explicit per-instance override —
    # same convention the Eagle path uses for a "smashed" attribute).
    # The library default isn't: a placeholder Text inside the symbol's
    # own graphics is local to the SYMBOL's own frame, and needs this
    # instance's own mirror/rot/translate applied to land in the same
    # canvas space — geo.transform_point is the exact same math the
    # Eagle path already uses for this composition (units.md #зеркало-
    # применяется-до-поворота), reused rather than re-derived.
    # **The angle below is the field's LOCAL one, never its absolute one.**
    # KiCad stores a property's angle relative to the symbol body and adds
    # the placement's own rotation back at render time — writing the
    # absolute angle counts the rotation twice, which is exactly why a
    # rotated symbol's fields came out turned the wrong way. So a smashed
    # override (whose IR angle IS absolute, like its position) has this
    # instance's rotation subtracted, while a library placeholder is
    # already local and travels as-is. Ground truth: the rule reproduces
    # every one of the 249 field angles KiCad's own import of this project
    # wrote, the 13 non-zero ones included.
    if inst.texts:
        placeholders_abs = {t.content[1:].split("@")[0].upper():
                            (t.x, t.y, (t.rot - inst.rot) % 360000, t.height, t.align)
                            for t in inst.texts}
    else:
        placeholders_abs = {
            key: (*geo.transform_point(x, y, inst.mirror, inst.rot, inst.x, inst.y),
                  rot, height, align)
            for key, (x, y, rot, height, align) in _placeholder_positions(gate1_symbol).items()
        }

    def emit(name: str, placeholder_key: str, value_: str, default_xy=(0, 0)) -> None:
        pos = placeholders_abs.get(placeholder_key)
        if pos is not None:
            x, y, rot, height, align = pos
            node.append(_instance_property(name, value_, x, y, rot, origin, hidden=False,
                                            height_um=height, align=align))
        else:
            dx, dy = default_xy
            ax, ay = geo.transform_point(dx, dy, inst.mirror, inst.rot, inst.x, inst.y)
            node.append(_instance_property(name, value_, ax, ay, 0, origin, hidden=True))

    emit("Reference", "NAME", occurrences[0][1], (0, -2540))
    emit("Value", "VALUE", value, (0, 2540))
    node.append(_instance_property("Footprint", footprint_ref, inst.x, inst.y, 0, origin, hidden=True))
    node.append(_instance_property("Datasheet", datasheet, inst.x, inst.y, 0, origin, hidden=True))
    node.append(_instance_property("Description", "", inst.x, inst.y, 0, origin, hidden=True))

    seen = {"VALUE", "DATASHEET"}
    for a in part.attrs:
        key = a.name.upper()
        if key in seen or key in ("VALUE", "DATASHEET"):
            continue
        seen.add(key)
        emit(a.name, key, a.value)

    # KiCad's own per-sheet-occurrence bookkeeping: which reference and unit
    # this symbol resolves to on each page it appears on. A symbol drawn
    # once inside a MODULE appears once per channel, so `occurrences` is a
    # list — one `(path ...)` per channel, each with that channel's own
    # expanded designator (module-instance.md). Ground truth: the ESC sheet
    # of `testData/kicad/OpenESC_20X20-main`, instantiated four times,
    # carries exactly four paths per symbol.
    project_node = ["project", project_name]
    for path, reference in occurrences:
        project_node.append(["path", path, ["reference", reference], ["unit", unit]])
        if paths_out is not None:
            # **The board's path and the schematic's are built differently,
            # and the difference is the ROOT.** An `(instances)` path names
            # the root sheet by the root FILE's uuid; a board `(path ...)`
            # gives the root no segment at all — it starts at "/" and lists
            # only sub-sheets, then the SYMBOL's own uuid.
            #
            # Ground truth, one symbol read in both files of
            # `testData/kicad/OpenESC_20X20-main`:
            #
            #   C86  instances /612d09d9(root)         board /0fb6bb4b(sym)
            #   R30  instances /612d09d9/f870c78f      board /f870c78f/0207287f
            #
            # KiCad's own netlist says the same: the root sheet's tstamps
            # are literally "/". Writing the root segment here made every
            # path resolve to nothing, so "Update PCB from Schematic" read
            # all 72 footprints as new parts and offered to place a second
            # copy of each.
            sheets = [s for s in path.split("/") if s][1:]
            board_path = "/" + "/".join([*sheets, sym_uuid])
            # **A multi-unit part is several symbols and ONE footprint**, so
            # the board has to pick which of them to name — and it picks the
            # first section. KiCad's own netlist lists every unit's uuid for
            # such a component (`(tstamps "u1" "u2" "u3" "u4")` for the
            # 4-section XS1 of VDS-32-R03), so unit order is the only thing
            # that makes the choice reproducible; keying by reference alone
            # let whichever section happened to be written last win.
            prev = paths_out.get(reference)
            if prev is None or unit < prev[0]:
                paths_out[reference] = (unit, board_path)
    node.append(["instances", project_node])

    return node


_LABEL_STYLE_TO_SHAPE = {
    LabelStyle.IN: Sym("input"),
    LabelStyle.OUT: Sym("output"),
    LabelStyle.IO: Sym("bidirectional"),
    LabelStyle.PASSIVE: Sym("passive"),
    LabelStyle.POWER: Sym("passive"),
    LabelStyle.CRUMMY: Sym("passive"),
}


def write_label(label, net_name: str, origin: tuple[int, int], log, *, local: bool = False) -> list:
    """A top-level page's label is a `global_label`, never KiCad's local
    `label` — even for a net that happens to live on one page today: a
    page is its own file in KiCad, and only a global (or hierarchical)
    label carries a name across files. A net.md segment can't itself span
    two pages, but nets (by shared name) very much can.

    **Inside a module the opposite is mandatory** (`local=True`): a
    module's nets are local by definition (module.md), and its file is
    drawn once but placed once PER CHANNEL. A global name there would
    weld every channel's copy of that net into one — the multichannel
    design silently collapsing into a single circuit. A KiCad local label
    is scoped to the sheet occurrence, which is exactly module.md's own
    scope.

    Shape comes from this label's own `LabelStyle`, already decided at
    import (net.md, label.md) — `crummy` (plain text, no inherent shape)
    and `power` fall back to `passive`, conversion-kicad.md's own named
    loss. A local label has no shape at all: KiCad draws it as bare text."""
    x0, y1 = origin
    shape = _LABEL_STYLE_TO_SHAPE[label.style]
    # A label has no mirror of its own in KiCad — a mirrored one reads as
    # the same text pointing the other way, i.e. rotated by 180°. Ground
    # truth: the mirrored labels of tolmach (rot 0, mirror) all come back
    # as plain rot 180 in KiCad's own import, unmirrored ones verbatim.
    rot = (kicad_rot(label.rot) + 180) % 360 if label.mirror else kicad_rot(label.rot)
    # A global label's justify is HORIZONTAL ONLY, and it follows the
    # angle, not the IR text align: the anchor is the point the label
    # touches the wire, and the flag has to extend away from it — left for
    # a label reading rightward/upward, right for one reading the other
    # way. Writing the align's vertical half here (`bottom`/`top`) is what
    # pushed every label off its wire. Ground truth: all 19 labels in
    # KiCad's own import carry exactly one token, `left` at 0/90 and
    # `right` at 180/270, and never a vertical one.
    node = ["label" if local else "global_label", net_name]
    if not local:
        node.append(["shape", shape])
    node += [["at", _page_x(label.x, x0), _page_y(label.y, y1), rot],
             ["fields_autoplaced", Sym("yes")],
             ["effects", ["font", ["size", mm(label.height), mm(label.height)]],
              ["justify", Sym("left" if rot in (0, 90) else "right")]]]
    node += [["uuid", _uuid4()],
             ["property", "Intersheetrefs", "${INTERSHEET_REFS}",
              ["at", _page_x(label.x, x0), _page_y(label.y, y1), 0], ["hide", Sym("yes")],
              ["show_name", Sym("no")], ["do_not_autoplace", Sym("no")],
              ["effects", ["font", ["size", 1.27, 1.27]]]]]
    return node


_DIRECTION_TO_SHAPE = {
    Direction.IN: Sym("input"),
    Direction.OUT: Sym("output"),
    Direction.IO: Sym("bidirectional"),
    Direction.HIZ: Sym("tri_state"),
}


def _port_shape(direction) -> Sym:
    """A module port's direction -> the shape KiCad draws on both ends of
    the connection (hierarchical label inside, sheet pin outside). KiCad
    has no power/passive/open-collector port shape, so everything else
    lands on `passive` — conversion-kicad.md's own named loss."""
    return _DIRECTION_TO_SHAPE.get(direction, Sym("passive"))


def _on_segment(ax: int, ay: int, bx: int, by: int, px: int, py: int) -> bool:
    """Whether (px, py) lies on the wire a-b (collinear and between)."""
    if (px - ax) * (by - ay) != (py - ay) * (bx - ax):
        return False
    return min(ax, bx) <= px <= max(ax, bx) and min(ay, by) <= py <= max(ay, by)


def _distance_to_segment(ax: int, ay: int, bx: int, by: int, px: int, py: int) -> float:
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


NEAR_WIRE = 635
"""0.025 inch, in µm. A label further than this from its own net's wire was
plainly not meant to touch one; a label CLOSER than this, and still not on
it, is a drafting slip worth shouting about — see `check_label_on_wire`."""


def check_label_on_wire(label, net_name: str, own_lines, log, where: str) -> bool:
    """Does this label actually sit on a wire of its own net?

    It matters because the two formats disagree on what a label IS. Eagle
    binds a label to a net as a FACT — the name holds wherever the label is
    drawn. KiCad reads it GEOMETRICALLY: a label that misses the wire by a
    hair names nothing at all, and the net silently goes out auto-named.
    So every label is checked here, and a near miss is reported loudly:
    that one is almost certainly a slip in the source drawing, and it is
    the case a person can actually go and fix."""
    if not own_lines:
        return False
    if any(_on_segment(ln.x1, ln.y1, ln.x2, ln.y2, label.x, label.y) for ln in own_lines):
        return True
    nearest = min(_distance_to_segment(ln.x1, ln.y1, ln.x2, ln.y2, label.x, label.y)
                  for ln in own_lines)
    if nearest <= NEAR_WIRE:
        log(f"ВОЗМОЖНО, МЕТКА С ИМЕНЕМ {net_name!r} {where} ДОЛЖНА ЛЕЖАТЬ НА ПРОВОДЕ — "
            f"она в {nearest / 1000:.3f} мм от провода своей цепи, но не на нём; "
            f"KiCad читает метку геометрически, и так она цепь не назовёт")
    else:
        log(f"label {net_name!r} {where}: not on a wire of its own net "
            f"({nearest / 1000:.2f} mm away) — KiCad will not take the name from it")
    return False


def port_anchor(net_lines, foreign_lines, log, port_name: str):
    """A FALLBACK point on this net's own wires to hang a synthesized label
    on — used only when the source drew no usable label of its own, since
    the author's chosen spot is always the better one (see `write_page`).

    Endpoints first, then midpoints, and the first candidate that does
    NOT also lie on some OTHER net's wire wins. That check is not
    fussiness: a KiCad label attaches to EVERY wire passing through its
    point, so anchoring on a crossing would silently weld two nets
    together — a connectivity change made by the exporter, which is the
    one thing conversion.md forbids doing quietly. Returns None when the
    net has no wire at all (an island with nothing to attach to; the
    caller logs it and the port stays unconnected)."""
    fallback = None
    for ln in net_lines:
        mid = ((ln.x1 + ln.x2) // 2, (ln.y1 + ln.y2) // 2)
        for pt in ((ln.x1, ln.y1), (ln.x2, ln.y2), mid):
            if fallback is None:
                fallback = pt
            if not any(_on_segment(f.x1, f.y1, f.x2, f.y2, *pt) for f in foreign_lines):
                return pt
    if fallback is not None:
        log(f"port {port_name!r}: every point of its net touches another net's wire — "
            f"anchored anyway, VERIFY IN KICAD that no two nets got welded")
    return fallback


def write_hierarchical_label(name: str, direction, x_um: int, y_um: int, rot_mdeg: int,
                              height_um: int, origin: tuple[int, int], log) -> list:
    """The inside end of a module port. A module's inner net reaches its
    port BY NAME in Eagle (module.md: the module declares ports, the inner
    schematic simply has nets of those names) — KiCad instead wants a real
    object sitting on the wire, so one is synthesized here and anchored by
    `port_anchor`."""
    x0, y1 = origin
    rot = kicad_rot(rot_mdeg)
    return ["hierarchical_label", name, ["shape", _port_shape(direction)],
            ["at", _page_x(x_um, x0), _page_y(y_um, y1), rot],
            ["effects", ["font", ["size", mm(height_um), mm(height_um)]],
             ["justify", Sym("left" if rot in (0, 90) else "right")]],
            ["uuid", _uuid4()]]


def write_sheet(minst, module, sheet_uuid: str, sheet_file: str, page_number: int,
                 project_name: str, parent_path: str, origin: tuple[int, int], log) -> list:
    """One channel -> one `(sheet ...)` block on the parent page.

    **A KiCad sheet carries no angle or mirror of its own**, so the
    channel's placement is BAKED into this occurrence's layout: the body
    is reduced to the axis-aligned box its transformed corners span (a
    90/270 channel simply comes out with width and height swapped), and
    every port lands wherever its own transformed pin does. The module's
    outward symbol is where both come from — `<shape>` body plus one pin
    per port (pin.md #одна-геометрия-на-оба-случая), the same geometry the
    Eagle path writes back out."""
    x0, y1 = origin
    body = next((g for g in module.symbol.graphics if isinstance(g, Shape)), None)
    if body is None:
        raise ValueError(f"module {module.name!r}: its outward symbol has no body <shape> to "
                          f"become a KiCad sheet outline")
    bx0, by0, bx1, by1 = geo.bbox_from_shape(body, minst.mirror, minst.rot, minst.x, minst.y)
    node = ["sheet",
            ["at", _page_x(bx0, x0), _page_y(by1, y1)],
            ["size", mm(bx1 - bx0), mm(by1 - by0)],
            ["exclude_from_sim", Sym("no")], ["in_bom", Sym("yes")],
            ["on_board", Sym("yes")], ["dnp", Sym("no")],
            ["fields_autoplaced", Sym("yes")],
            ["stroke", ["width", 0.1524], ["type", Sym("solid")]],
            ["fill", ["color", 0, 0, 0, 0]],
            ["uuid", sheet_uuid],
            ["property", "Sheetname", minst.name,
             ["at", _page_x(bx0, x0), _page_y(by1, y1) - 0.7112, 0],
             ["show_name", Sym("no")], ["do_not_autoplace", Sym("no")],
             ["effects", ["font", ["size", 1.27, 1.27]], ["justify", Sym("left"), Sym("bottom")]]],
            ["property", "Sheetfile", sheet_file,
             ["at", _page_x(bx0, x0), _page_y(by0, y1) + 0.5842, 0],
             ["show_name", Sym("no")], ["do_not_autoplace", Sym("no")],
             ["effects", ["font", ["size", 1.27, 1.27]], ["justify", Sym("left"), Sym("top")]]]]

    for pin in module.symbol.pins:
        px, py = geo.transform_point(pin.x, pin.y, minst.mirror, minst.rot, minst.x, minst.y)
        # **A mirrored channel mirrors the pin's ANGLE too, not just its
        # position.** The angle is how KiCad knows which edge a sheet pin
        # belongs to; leaving it unmirrored puts a left-pointing pin on the
        # right edge, and KiCad resolves that contradiction by connecting
        # it to whatever is on the left instead — silently welding two
        # ports of the channel together. A flip about the vertical axis
        # sends a direction `φ` to `180 - φ`.
        rot = kicad_rot((pin.rot + minst.rot) % 360000)
        if minst.mirror:
            rot = (180 - rot) % 360
        # The pin's text is drawn INSIDE the box, so it hangs off the edge
        # the opposite way a free-standing label does: ground truth writes
        # `right` on a right-edge (angle 0) pin and `left` on every other.
        node.append(["pin", pin.name, _port_shape(pin.direction),
                      ["at", _page_x(px, x0), _page_y(py, y1), rot],
                      ["uuid", _uuid4()],
                      ["effects", ["font", ["size", 1.27, 1.27]],
                       ["justify", Sym("right" if rot == 0 else "left")]]])

    node.append(["instances", ["project", project_name,
                                ["path", parent_path, ["page", str(page_number)]]]])
    return node


def write_port_stubs(minst, module, sheet_nets, origin: tuple[int, int], log) -> list[list]:
    """The wires that actually attach a channel to the nets around it.

    Eagle declares this connection LOGICALLY — `<portref moduleinst port>`
    on the net — and draws the port with a lead, so the parent's wire
    deliberately stops short of the module body (5.08 mm in step4). KiCad
    knows no such thing: a sheet pin sits ON the border and connects only
    to a wire that reaches it. So the lead Eagle merely drew is
    materialized here as a real wire, from the sheet pin to the nearest
    endpoint of the net that names this port. Without it every channel
    exports as an island — its ports listed in the netlist with nothing on
    the other side."""
    x0, y1 = origin
    out = []
    for pin in module.symbol.pins:
        px, py = geo.transform_point(pin.x, pin.y, minst.mirror, minst.rot, minst.x, minst.y)
        target, width = None, 152
        for net, segs in sheet_nets.values():
            for seg in segs:
                if not any(r.inst == minst.name and r.pin == pin.name for r in seg.pinrefs):
                    continue
                for ln in seg.lines:
                    width = ln.width
                    for pt in ((ln.x1, ln.y1), (ln.x2, ln.y2)):
                        d = abs(pt[0] - px) + abs(pt[1] - py)
                        if target is None or d < target[0]:
                            target = (d, pt)
        if target is None:
            log(f"channel {minst.name!r}: port {pin.name!r} is on no net with wires — "
                f"it stays unconnected in KiCad")
            continue
        if target[0] == 0:
            continue
        tx, ty = target[1]
        if tx != px and ty != py:
            log(f"channel {minst.name!r}: port {pin.name!r} and its net's nearest wire end are "
                f"not aligned — connected by a diagonal wire, check it in KiCad")
        out.append(["wire",
                     ["pts", ["xy", _page_x(px, x0), _page_y(py, y1)],
                      ["xy", _page_x(tx, x0), _page_y(ty, y1)]],
                     ["stroke", ["width", mm(width)], ["type", Sym("solid")]],
                     ["uuid", _uuid4()]])
    return out


# A synthesized label is DRAWN, like any other. Zero height would hide it
# and was tried: it works (the net still netlists as `/DCDC1/N$1`), and it
# is still wrong — an object that changes the schematic while being
# invisible on it is the one kind of mistake a person cannot catch by
# looking. Whatever affects connectivity is on the drawing.
#
# So synthesis is now rare on purpose: it happens only where NOT naming
# the net would break connectivity outright (see `write_page` — a net
# spanning pages), never merely to carry a name across. A name the author
# never drew is a name the author never showed, and KiCad is welcome to
# invent its own.
#
# (Ground truth kept because it costs an hour to rediscover: `(hide yes)`
# on a `label` is NOT a valid token, and KiCad answers it by **silently
# dropping the entire sheet** — 72 references in the netlist fell to 16
# with no error printed anywhere. Never reach for it.)
_SYNTHESIZED_LABEL_HEIGHT = 1.27


def write_synthesized_label(net_name: str, x_um: int, y_um: int,
                             origin: tuple[int, int], local: bool = False) -> list:
    """A name the source states but never draws — see `write_page`. Shape
    `passive`, the same neutral fallback label.md's own `crummy` takes:
    nothing here claims to know a direction the source did not give.

    **`local` follows the page, exactly as a drawn label's does.** A
    synthesized name inside a module has the same power to weld every
    channel into one that an author-drawn one has: written global, all
    four `N$1`s become a single net and the multichannel schematic
    silently collapses. It also has to be local for the name to come out
    RIGHT — KiCad scopes a local label as `/DCDC1/N$1`, which is what the
    board writes for Eagle's `DCDC1:N$1`."""
    x0, y1 = origin
    h = _SYNTHESIZED_LABEL_HEIGHT
    if local:
        return ["label", net_name,
                ["at", _page_x(x_um, x0), _page_y(y_um, y1), 0],
                ["fields_autoplaced", Sym("yes")],
                ["effects", ["font", ["size", h, h]], ["justify", Sym("left")]],
                ["uuid", _uuid4()]]
    return ["global_label", net_name, ["shape", Sym("passive")],
            ["at", _page_x(x_um, x0), _page_y(y_um, y1), 0],
            ["fields_autoplaced", Sym("yes")],
            ["effects", ["font", ["size", h, h]], ["justify", Sym("left")]],
            ["uuid", _uuid4()],
            ["property", "Intersheetrefs", "${INTERSHEET_REFS}",
             ["at", _page_x(x_um, x0), _page_y(y_um, y1), 0], ["hide", Sym("yes")],
             ["show_name", Sym("no")], ["do_not_autoplace", Sym("no")],
             ["effects", ["font", ["size", h, h]]]]]


def write_netclass_flag(class_name: str, x_um: int, y_um: int,
                         origin: tuple[int, int]) -> list:
    """A net-class directive — the one way to say "this net is in class X"
    **without knowing what the net is called**.

    KiCad assigns classes by NAME everywhere else (`.kicad_pro`'s
    `netclass_patterns`, ground truth in every real project here), and a
    net the source never labelled has no name we can predict — KiCad
    invents `Net-(IC101-BOOT)` for it. class.md, though, lets a net belong
    to a class whether or not it is named, so name-based assignment cannot
    carry the fact at all.

    The directive attaches GEOMETRICALLY, exactly like a label: it names
    the class of whatever net its point touches. Verified on step4 —
    placed on an unnamed wire inside the DCDC module, `kicad-cli sch
    export netlist` reports `Net-(IC101-BOOT)` in class `HV,Default`, and
    all FOUR channels at once, the module file being drawn once."""
    x0, y1 = origin
    px, py = _page_x(x_um, x0), _page_y(y_um, y1)
    return ["netclass_flag", class_name,
            ["length", 2.54], ["shape", Sym("round")],
            ["at", px, py, 0],
            ["fields_autoplaced", Sym("yes")],
            ["effects", ["font", ["size", 1.27, 1.27]], ["justify", Sym("left")]],
            ["uuid", _uuid4()],
            ["property", "Netclass", class_name, ["at", px, py, 0],
             ["effects", ["font", ["size", 1.27, 1.27]],
              ["justify", Sym("left"), Sym("bottom")]]]]


def write_wire(line, origin: tuple[int, int], log) -> list:
    """segment.md: a net segment's own `Line`s are its wire geometry —
    conversion-kicad.md doesn't single wires out as lossy, they're the
    plain default case."""
    x0, y1 = origin
    return ["wire",
            ["pts", ["xy", _page_x(line.x1, x0), _page_y(line.y1, y1)],
             ["xy", _page_x(line.x2, x0), _page_y(line.y2, y1)]],
            ["stroke", ["width", mm(line.width)], ["type", Sym("solid")]],
            ["uuid", _uuid4()]]


def _endpoints(lines) -> dict[tuple[int, int], int]:
    counts: dict[tuple[int, int], int] = {}
    for ln in lines:
        for pt in ((ln.x1, ln.y1), (ln.x2, ln.y2)):
            counts[pt] = counts.get(pt, 0) + 1
    return counts


_JUNCTION_DOT_RATIO = 6
"""Junction dot diameter, in wire widths.

**Written out, never left to `(diameter 0)`.** That zero means "take the
project default", and the default resolved to nothing at all here: the
dots were in the file, KiCad kept them when asked to rewrite it, and drew
them at zero size — invisible on screen and in a plot alike, appearing
only while a symbol was dragged (the user found it that way, twice). An
explicit diameter renders regardless of how a project resolves its
defaults, which is the same lesson `.kicad_pro` taught with design rules
and drawing settings.

The ratio is measured, not guessed: the dot KiCad's own import of tolmach
draws is 0.84 mm across on a 0.1524 mm wire — about 5.5x, and 6x is what
reproduces it (an anti-aliased disc measures slightly under)."""


def write_junctions(lines, origin: tuple[int, int], log) -> list[list]:
    """A junction dot is needed wherever 3+ wire endpoints coincide — a
    real T/star connection, not just two segments' shared endpoint
    (which reads as one continuous wire without a dot). Endpoints landing
    mid-wire on ANOTHER segment (a T without a shared vertex) aren't
    caught by this endpoint-coincidence count; segment.md's own model
    doesn't distinguish that case from a shared vertex, so this is the
    same connectivity the segment already asserts, not a guess."""
    x0, y1 = origin
    out = []
    for (x, y), n in _endpoints(lines).items():
        if n < 3:
            continue
        width = max((ln.width for ln in lines
                     if (ln.x1, ln.y1) == (x, y) or (ln.x2, ln.y2) == (x, y)), default=152)
        out.append(["junction", ["at", _page_x(x, x0), _page_y(y, y1)],
                    ["diameter", round(mm(width) * _JUNCTION_DOT_RATIO, 4)],
                    ["color", 0, 0, 0, 0], ["uuid", _uuid4()]])
    return out


def build_lib_symbols_cache(used_lib_ids: set[str], components, symbols_by_key, log,
                             power_scope: str = "global") -> list:
    """conversion-kicad.md #библиотеки: `.kicad_sch` carries its own full
    copy of every symbol it places (`lib_symbols`), addressed by the
    complete `lib_id` (library included) rather than the bare name a
    standalone `.kicad_sym` uses. Built from every component's full
    device expansion, then pruned to what THIS page actually placed —
    KiCad itself prunes unused cache entries on save, and shipping the
    whole project's symbol set in every page would just be dead weight."""
    index: dict[str, list] = {}
    for component in components:
        for node in lib_export.write_component(component, symbols_by_key, log,
                                                 name_prefix=f"{component.library}:",
                                                 power_scope=power_scope):
            index[node[1]] = node
    cache = ["lib_symbols"]
    for lib_id in sorted(used_lib_ids):
        node = index.get(lib_id)
        if node is None:
            raise ValueError(f"placed instance names unknown symbol {lib_id!r}")
        cache.append(node)
    return cache


def _named_by_power_symbol(net_name: str, segs, parts_by_name,
                            components_by_key, symbols_by_key) -> bool:
    """Does a power symbol on this net already state its name?

    KiCad reads a power net's name off the symbol's `Value`, and for a
    supply pin that Value IS the net name (power-symbol.md; Eagle names
    the net after the supply pin in the first place). The equality is
    checked rather than assumed — an author who renamed the net out from
    under its symbol gets the label, which is the safe way round."""
    for seg in segs:
        for ref in seg.pinrefs:
            part = parts_by_name.get(ref.inst)
            if part is None:
                continue
            component = components_by_key.get((part.library.lower(), part.component.lower()))
            if component is None or not component.gates:
                continue
            gate1 = symbols_by_key.get((component.library, component.gates[0].symbol))
            if gate1 is None or not lib_export.is_power_symbol(gate1):
                continue
            value = next((a.value for a in part.attrs if a.name.upper() == "VALUE"), "")
            if value == net_name:
                return True
    return False


def write_page(sheet, project, symbols_by_key, components_by_key, footprint_pool: str, log,
                *, parts_by_name, page_uuid: str, occurrences_for, local_labels: bool = False,
                ports=None, extra_nodes=(), root_page: bool = True, page_number: int = 1,
                must_name=frozenset(), where: str = "", paths_out: dict | None = None) -> list:
    """One `.kicad_sch` file — the project's own top page, or a module's.

    `sheet` is one of `eagle.schematic_export.build_sheets`'s own per-frame
    buckets, reused as-is: the same rule ("every object lies inside exactly
    one frame, a net segment can't straddle two") is conversion-kicad.md's
    own #страницы requirement, word for word — except for a module page,
    whose schematic carries no frame at all and arrives as one bucket built
    by the caller.

    `occurrences_for(part)` gives the `(path, reference)` pairs this part
    resolves to — one for a top-level part, one PER CHANNEL for a part
    inside a module. `ports` (module pages only) is the module's own
    outward pins, each of which gets a `hierarchical_label` anchored on the
    inner net that carries its name.

    `paths_out`, when given, collects `reference -> KIID path` for every
    occurrence written — what the board needs to link its footprints back
    to these symbols."""
    project_name = project.name
    x0, y0, x1, y1 = sheet["bbox"]
    origin = (x0 - _PAGE_MARGIN, y1 + _PAGE_MARGIN)

    instance_nodes = []
    used_lib_ids: set[str] = set()
    for inst in sheet["instances"]:
        part = parts_by_name[inst.part]
        component = components_by_key[(part.library.lower(), part.component.lower())]
        gate1 = symbols_by_key[(component.library, component.gates[0].symbol)]
        if gate1.is_frame:
            # frame.md / conversion-kicad.md #страницы: KiCad has no
            # placed-symbol equivalent of a frame at all — the page
            # border and title block are native, not a component. This
            # is the same part `build_sheets` used to find the page's
            # own bbox in the first place; it doesn't get placed again.
            continue
        device = None
        if component.devices:
            device = next((d for d in component.devices if d.name == (part.device or "")), None)
            if device is None:
                raise ValueError(f"part {part.name!r} names unknown device {part.device!r} "
                                  f"of component {component.name!r}")
        symbol_name = lib_export.symbol_name_for(component, device)
        instance_nodes.append(write_instance(
            inst, part, component, device, gate1, symbol_name,
            footprint_pool, project_name, occurrences_for(part), origin, log, paths_out))
        used_lib_ids.add(f"{component.library}:{symbol_name}")

    port_names = {p.name for p in ports} if ports else set()
    lines_by_net = {name: [ln for seg in segs for ln in seg.lines]
                    for name, (net, segs) in sheet["nets"].items()}
    labels_by_net = {name: [lbl for seg in segs for lbl in seg.labels]
                     for name, (net, segs) in sheet["nets"].items()}

    # **Which of the author's own labels each port will take over.** The
    # port's hierarchical label reuses that label's spot, so that ONE label
    # must not also be drawn — but every OTHER label of the same net must,
    # and this is exactly where a whole class of broken nets came from.
    #
    # A module net can have SEVERAL segments, joined in Eagle by name;
    # `staya`'s ESC module has `PHA` in two of them, each carrying its own
    # label. Suppressing every label of a port net left the segment without
    # the hierarchical label unnamed, so it became an island: 13 of 179
    # groups lost a pin, and `R101` pin 2 came out `unconnected-(R101-Pad2)`
    # while Eagle has it on the same net as `CON1` pin 3.
    port_label: dict[str, object] = {}
    for pin in ports or ():
        own = lines_by_net.get(pin.name)
        if own:
            port_label[pin.name] = next(
                (lbl for lbl in labels_by_net.get(pin.name, ())
                 if check_label_on_wire(lbl, pin.name, own, log, where)), None)

    wire_nodes, junction_nodes, label_nodes = [], [], []
    for net, segs in sheet["nets"].values():
        # **Class membership rides on a directive, and on EVERY segment.**
        # class.md lets an unnamed net belong to a class, so the name-based
        # assignment KiCad uses elsewhere cannot express it; and two
        # segments of an unnamed net are two separate nets to KiCad, so one
        # directive for the whole net would leave the others out. Named or
        # not, the rule is the same one — no special case to get wrong.
        net_class = net.attr("class")
        for seg in (segs if net_class else ()):
            foreign = [ln for other, lns in lines_by_net.items()
                        if other != net.name for ln in lns]
            anchor = port_anchor(seg.lines, foreign, log, f"{net.name} (класс {net_class})")
            if anchor is None:
                log(f"net {net.name!r} {where}: belongs to class {net_class!r} but this segment "
                    f"has no wire to hang the directive on — that part keeps the default class")
                continue
            label_nodes.append(write_netclass_flag(net_class, anchor[0], anchor[1], origin))
        for seg in segs:
            for ln in seg.lines:
                wire_nodes.append(write_wire(ln, origin, log))
            junction_nodes += write_junctions(seg.lines, origin, log)
            for label in seg.labels:
                if label is port_label.get(net.name):
                    # This exact label is the spot the port's own
                    # hierarchical label takes over — see `port_label`.
                    # Only this one; its siblings on other segments stay.
                    continue
                check_label_on_wire(label, net.name, lines_by_net.get(net.name), log, where)
                label_nodes.append(write_label(label, net.name, origin, log, local=local_labels))

    # **A net that would otherwise COME APART gets its name written here,
    # even where the author drew no label.** Eagle holds a net's name as a
    # FACT and joins by it; KiCad joins pages only through labels, so a
    # net spanning pages whose name is written on none of them becomes
    # several separate auto-named islands — a ground on three sheets
    # quietly becomes three grounds.
    #
    # **Only that.** A name is not carried across for its own sake: where
    # the author drew no label, the name is one the source itself never
    # shows (Eagle prints `N$5` nowhere), so it is not important enough to
    # add a drawing element for — KiCad names such a net itself, and a
    # person who disagrees puts a label on it, in the source, where it
    # belongs. What the board does about it is its own business (see
    # `board_export.kicad_net_name` and export.py's own report).
    #
    # A net whose name some OTHER thing already states is skipped: its
    # own label, or a power symbol sitting on it (KiCad names a power net
    # from that symbol's `Value`, which is the net name — power-symbol.md).
    for name in sorted(must_name):
        entry = sheet["nets"].get(name)
        if entry is None or any(seg.labels for seg in entry[1]):
            continue
        if _named_by_power_symbol(name, entry[1], parts_by_name,
                                   components_by_key, symbols_by_key):
            continue
        own = lines_by_net.get(name)
        if not own:
            log(f"net {name!r} {where}: needs its name written here but has no wire to hang it "
                f"on — this part of it travels unnamed (conversion-kicad.md #имена-цепей)")
            continue
        foreign = [ln for other, lns in lines_by_net.items() if other != name for ln in lns]
        anchor = port_anchor(own, foreign, log, name)
        if anchor is None:
            continue
        log(f"net {name!r} {where}: label synthesized — the source names this net without "
            f"drawing one here")
        label_nodes.append(write_synthesized_label(name, anchor[0], anchor[1], origin,
                                                    local=local_labels))

    for pin in ports or ():
        own = lines_by_net.get(pin.name)
        if not own:
            log(f"module port {pin.name!r}: its inner net has no wire on this page — "
                f"the port is left unconnected (conversion-kicad.md #имена-цепей)")
            continue
        # **Where the author put the name is where the name belongs.** The
        # source already drew a label for this net, at a spot chosen to
        # read well — a wire end, clear of the symbols. Reusing it keeps
        # the drawing recognisable; synthesizing a position instead parks
        # the port label on the first wire endpoint found, which is
        # usually a pin, and the page stops looking like the original.
        # Fidelity, though, never outranks connectivity: a label that
        # misses the wire cannot carry the name in KiCad at all, so such a
        # one is reported and the search below takes over.
        placed = port_label.get(pin.name)
        if placed is not None:
            label_nodes.append(write_hierarchical_label(
                pin.name, pin.direction, placed.x, placed.y, placed.rot,
                placed.height, origin, log))
            continue
        foreign = [ln for name, lns in lines_by_net.items() if name != pin.name for ln in lns]
        anchor = port_anchor(own, foreign, log, pin.name)
        if anchor is None:
            continue
        label_nodes.append(write_hierarchical_label(
            pin.name, pin.direction, anchor[0], anchor[1], pin.rot, 1270, origin, log))

    node = ["kicad_sch", ["version", _KICAD_VERSION], ["generator", "babel"],
            ["generator_version", "10.0"], ["uuid", page_uuid],
            ["paper", "User", mm(x1 - x0 + 2 * _PAGE_MARGIN), mm(y1 - y0 + 2 * _PAGE_MARGIN)],
            build_lib_symbols_cache(used_lib_ids, list(components_by_key.values()), symbols_by_key,
                                     log, "local" if local_labels else "global")]
    node += junction_nodes + wire_nodes + label_nodes + instance_nodes + list(extra_nodes)
    if root_page:
        # Only a ROOT file carries the page-numbering table; a module's file
        # is placed BY its sheet nodes and numbers nothing itself (ground
        # truth: `ESC.kicad_sch` has no `sheet_instances` at all). With
        # several frames there are several roots, each numbering itself.
        node.append(["sheet_instances", ["path", "/", ["page", str(page_number)]]])
    node.append(["embedded_fonts", Sym("no")])
    return node
