"""KiCad `.kicad_pcb` -> IR `Layout` — conversion-kicad.md #плата.

The board is read only together with its schematic: connectivity is the
schematic's to state (layout.md), and the board contributes where the
copper physically is.

Coordinates. The board space runs Y-down in KiCad and Y-up in the IR, and
the origin is the AUXILIARY AXIS where the board has one — that is what
the export side writes out as the IR's own (0, 0). A board without one
puts the origin at the page's top-left corner, so nothing moves relative
to the paper.
"""

from __future__ import annotations

from ir.attr import Attr
from ir.contactref import ContactRef
from ir.element import Element, Side
from ir.graphics import Arc, Line, Polygon, Shape, Text, Vertex
from ir.layout import Layout
from ir.signal import Signal
from ir.via import Via

from . import geometry as geo
from . import sexpr
from .layers import ir_layer

# Copper as KiCad numbers it: F.Cu is 0, B.Cu the last. The IR numbers
# copper from the stack instead (1 is the top, -1 the bottom), so the
# names are resolved against the board's own layer table.
_COPPER_SUFFIX = ".Cu"


class Space:
    """The board's own origin on the KiCad page."""

    def __init__(self, origin_mm: tuple[float, float]):
        self.ox, self.oy = origin_mm

    def x(self, value_mm) -> int:
        return geo.um(float(value_mm) - self.ox)

    def y(self, value_mm) -> int:
        return geo.um(self.oy - float(value_mm))

    def at(self, node: sexpr.Node) -> tuple[int, int, float]:
        x_mm, y_mm, angle = geo.at(node)
        return self.x(x_mm), self.y(y_mm), angle

    def point(self, node: sexpr.Node) -> tuple[int, int]:
        a = sexpr.atoms(node)
        return self.x(a[0]), self.y(a[1])


def board_space(board: sexpr.Node, log, label: str) -> Space:
    setup = sexpr.kid(board, "setup")
    aux = sexpr.kid(setup, "aux_axis_origin") if setup else None
    atoms = sexpr.atoms(aux) if aux else []
    if len(atoms) >= 2 and (atoms[0] or atoms[1]):
        return Space((float(atoms[0]), float(atoms[1])))
    log(f"{label}: no auxiliary axis — the origin is the page's top-left corner")
    return Space((0.0, 0.0))


def copper_layers(board: sexpr.Node) -> dict[str, int]:
    """KiCad copper layer name -> the IR's own number.

    layer-model.md numbers copper from the stack: 1 is the mount side, -1
    the opposite, and the inner layers count inward from 2. KiCad's own
    numbering is the file's business and does not travel."""
    layers = sexpr.kid(board, "layers")
    names = [str(sexpr.atoms(c)[0]) for c in (layers[1:] if layers else [])
             if isinstance(c, list) and str(sexpr.atoms(c)[0]).endswith(_COPPER_SUFFIX)]
    out: dict[str, int] = {}
    inner = 2
    for name in names:
        if name == "F.Cu":
            out[name] = 1
        elif name == "B.Cu":
            out[name] = -1
        else:
            out[name] = inner
            inner += 1
    return out


def read_stack(board: sexpr.Node, copper_count: int, log, label: str) -> str:
    """layout.md's stack formula, out of KiCad's `stackup`.

    Bare numbers are copper, bracketed ones the dielectric between them,
    top to bottom. Where the stackup is missing or does not answer the
    number of copper layers, the formula is synthesized from the board's
    total thickness — the real numbers then have to be put in by hand, and
    the log says so."""
    setup = sexpr.kid(board, "setup")
    stackup = sexpr.kid(setup, "stackup") if setup else None
    pieces: list[str] = []
    coppers = 0
    if stackup is not None:
        pending = 0
        for layer in sexpr.kids(stackup, "layer"):
            kind = sexpr.kid(layer, "type")
            kind_name = str(sexpr.atoms(kind)[0]) if kind else ""
            thickness = sexpr.kid(layer, "thickness")
            value = geo.um(sexpr.atoms(thickness)[0]) if thickness else 0
            if kind_name == "copper":
                if coppers:
                    pieces.append(f"[{pending}]")
                pieces.append(str(value))
                coppers += 1
                pending = 0
            elif kind_name in ("core", "prepreg", "dielectric") or "dielectric" in str(
                    sexpr.atoms(layer)[0] if sexpr.atoms(layer) else ""):
                pending += value
    if coppers == copper_count and coppers >= 2:
        return "".join(pieces)

    general = sexpr.kid(board, "general")
    total = sexpr.kid(general, "thickness") if general else None
    total_um = geo.um(sexpr.atoms(total)[0]) if total else 1600
    copper_um = 35
    gaps = max(1, copper_count - 1)
    dielectric = max(1, (total_um - copper_um * copper_count) // gaps)
    log(f"{label}: the stackup does not answer {copper_count} copper layer(s) — "
        f"a formula is synthesized from the board's total thickness "
        f"({total_um} µm); put the real thicknesses in yourself")
    parts = [str(copper_um)]
    for _ in range(gaps):
        parts += [f"[{dielectric}]", str(copper_um)]
    return "".join(parts)


def _layer_of(node: sexpr.Node, copper: dict[str, int], log, label: str) -> int | None:
    layer = sexpr.kid(node, "layer")
    if layer is None:
        return None
    name = str(sexpr.atoms(layer)[0])
    if name in copper:
        return copper[name]
    number = ir_layer(name)
    if number is None:
        log(f"{label}: layer {name!r} has no row in kicad_layers.tsv — object dropped")
    return number


def convert_graphic(node: sexpr.Node, space: Space, copper: dict, log, label: str):
    """One `gr_*` — the board's own graphics, outside any footprint."""
    tag = node[0]
    layer = _layer_of(node, copper, log, label)
    if layer is None:
        return None
    width = geo.stroke_width(node, 0)

    if tag == "gr_line":
        x1, y1 = space.point(sexpr.kid(node, "start"))
        x2, y2 = space.point(sexpr.kid(node, "end"))
        return None if (x1, y1) == (x2, y2) else Line(x1, y1, x2, y2, width, layer)
    if tag == "gr_arc":
        x1, y1 = space.point(sexpr.kid(node, "start"))
        mx, my = space.point(sexpr.kid(node, "mid"))
        x2, y2 = space.point(sexpr.kid(node, "end"))
        if (x1, y1) == (x2, y2):
            return None
        curve = geo._sweep(x1, y1, mx, my, x2, y2)
        if curve is None:
            log(f"{label}: degenerate arc — dropped")
            return None
        return Arc(x1, y1, x2, y2, curve, width, layer)
    if tag == "gr_circle":
        cx, cy = space.point(sexpr.kid(node, "center"))
        ex, ey = space.point(sexpr.kid(node, "end"))
        radius = round(((ex - cx) ** 2 + (ey - cy) ** 2) ** 0.5)
        if radius <= 0:
            return None
        filled = _filled(node)
        return Shape(cx, cy, radius * 2, radius * 2, layer, roundness=100,
                     outline=0 if filled else width)
    if tag == "gr_rect":
        x1, y1 = space.point(sexpr.kid(node, "start"))
        x2, y2 = space.point(sexpr.kid(node, "end"))
        return Shape((x1 + x2) // 2, (y1 + y2) // 2, abs(x2 - x1), abs(y2 - y1),
                     layer, outline=0 if _filled(node) else width)
    if tag == "gr_poly":
        pts = sexpr.kid(node, "pts")
        verts = [Vertex(*space.point(p)) for p in sexpr.kids(pts, "xy")] if pts else []
        deduped = [v for i, v in enumerate(verts)
                   if i == 0 or (v.x, v.y) != (verts[i - 1].x, verts[i - 1].y)]
        if len(deduped) > 2 and (deduped[0].x, deduped[0].y) == (deduped[-1].x, deduped[-1].y):
            del deduped[-1]
        if len(deduped) < 3:
            return None
        return Polygon(layer, width, deduped, fill=100 if _filled(node) else 0)
    if tag == "gr_text":
        atoms = sexpr.atoms(node)
        content = str(atoms[0]) if atoms else ""
        if not content:
            return None
        x, y, angle = space.at(node)
        height, align, mirror = geo.text_effects(node)
        return Text(x, y, height, layer, align, content=geo.overbar(content),
                    rot=round(angle * 1000) % 360000, mirror=mirror)
    return None


def _filled(node: sexpr.Node) -> bool:
    f = sexpr.kid(node, "fill")
    if f is None:
        return False
    atoms = sexpr.atoms(f)
    return bool(atoms) and str(atoms[0]) in ("yes", "solid")


def convert_element(node: sexpr.Node, space: Space, log, label: str
                    ) -> tuple[Element, dict] | None:
    """A placed footprint -> where the part sits, plus which of its pads
    the board says are on which net."""
    layer = sexpr.kid(node, "layer")
    back = bool(layer) and str(sexpr.atoms(layer)[0]).startswith("B.")
    x, y, angle = space.at(node)

    designator = ""
    for prop in sexpr.kids(node, "property"):
        atoms = sexpr.atoms(prop)
        if len(atoms) > 1 and str(atoms[0]) == "Reference":
            designator = str(atoms[1]).lstrip("#").upper()
    if not designator:
        log(f"{label}: a placed footprint has no reference — dropped")
        return None

    # footprint_export.fp_rot: the placement angle of a back-side footprint
    # is written `180 - θ`, a REFLECTION rather than a half turn. The front
    # keeps its angle as it is.
    rot = round(((180 - angle) if back else angle) * 1000) % 360000

    nets = {}
    for pad in sexpr.kids(node, "pad"):
        atoms = sexpr.atoms(pad)
        net = sexpr.kid(pad, "net")
        net_atoms = sexpr.atoms(net) if net else []
        if not atoms or not net_atoms:
            continue
        name = [a for a in net_atoms if isinstance(a, str)]
        if name and str(atoms[0]):
            nets[str(atoms[0])] = name[0]

    element = Element(name=designator, x=x, y=y, rot=rot,
                      side=Side.BOTTOM if back else Side.TOP)
    return element, nets


def convert_track(node: sexpr.Node, space: Space, copper: dict, log, label: str):
    layer = _layer_of(node, copper, log, label)
    if layer is None:
        return None, ""
    net = sexpr.kid(node, "net")
    net_atoms = [a for a in (sexpr.atoms(net) if net else []) if isinstance(a, str)]
    name = geo.unescape(net_atoms[0]) if net_atoms else ""
    width = geo.um(sexpr.atoms(sexpr.kid(node, "width"))[0]) if sexpr.kid(node, "width") else 0

    if node[0] == "arc":
        x1, y1 = space.point(sexpr.kid(node, "start"))
        mx, my = space.point(sexpr.kid(node, "mid"))
        x2, y2 = space.point(sexpr.kid(node, "end"))
        if (x1, y1) == (x2, y2):
            return None, name
        curve = geo._sweep(x1, y1, mx, my, x2, y2)
        if curve is None:
            return None, name
        return Arc(x1, y1, x2, y2, curve, width, layer), name

    x1, y1 = space.point(sexpr.kid(node, "start"))
    x2, y2 = space.point(sexpr.kid(node, "end"))
    if (x1, y1) == (x2, y2):
        return None, name
    return Line(x1, y1, x2, y2, width, layer), name


def convert_via(node: sexpr.Node, space: Space, log, label: str):
    """via.md: only a through via travels. A blind or buried one is
    refused — the IR cannot say which layers it spans."""
    kinds = [str(a) for a in sexpr.atoms(node)]
    if "blind" in kinds or "micro" in kinds:
        raise SystemExit(
            f"{label}: the board carries blind/buried vias, which the IR "
            f"expresses no way at all (via.md — only through vias). Convert "
            f"them to through vias in KiCad, or leave this board behind.")
    x, y, _angle = space.at(node)
    size = sexpr.kid(node, "size")
    drill = sexpr.kid(node, "drill")
    if size is None or drill is None:
        log(f"{label}: a via has no size or drill — dropped")
        return None, ""
    net = sexpr.kid(node, "net")
    net_atoms = [a for a in (sexpr.atoms(net) if net else []) if isinstance(a, str)]
    name = geo.unescape(net_atoms[0]) if net_atoms else ""
    return Via(x=x, y=y, drill=geo.um(sexpr.atoms(drill)[0]),
               diameter=geo.um(sexpr.atoms(size)[0])), name


def net_translation(board: sexpr.Node, net_by_pad: dict, log, label: str) -> dict:
    """KiCad's net name -> the schematic's own, learned from the PADS.

    The board names nets its own way: it prefixes the sheet path (`/LEDRK`
    for what the schematic calls `LEDRK`) and invents names outright where
    the author gave none (`Net-(R1-Pad2)`, and our own `N$…` differs from
    it). None of that is a fact (conversion-kicad.md #плата) — the fact is
    which pads the copper reaches, and every pad on the board says both
    names at once: its own net here, and the net the schematic wired that
    same pad to. That makes the table exact, with nothing to guess.

    A pad whose two names disagree with the rest of its net is an
    out-of-date board, and that is refused."""
    table: dict[str, str] = {}
    disagreed: list[str] = []
    for node in sexpr.kids(board, "footprint"):
        designator = ""
        for prop in sexpr.kids(node, "property"):
            atoms = sexpr.atoms(prop)
            if len(atoms) > 1 and str(atoms[0]) == "Reference":
                designator = str(atoms[1]).lstrip("#").upper()
        for pad in sexpr.kids(node, "pad"):
            atoms = sexpr.atoms(pad)
            net = sexpr.kid(pad, "net")
            names = [a for a in (sexpr.atoms(net) if net else []) if isinstance(a, str)]
            if not atoms or not names or not str(atoms[0]):
                continue
            here = geo.unescape(names[0])
            there = net_by_pad.get((designator, str(atoms[0])))
            if there is None:
                continue
            if table.setdefault(here, there) != there:
                disagreed.append(f"  {designator} pad {atoms[0]}: board says "
                                 f"{here!r}, schematic says {there!r}")
    if disagreed:
        raise SystemExit(
            f"{label}: the board's nets no longer answer the schematic:\n"
            + "\n".join(sorted(set(disagreed))[:15]) +
            "\nRun 'Update PCB from Schematic' in KiCad, then convert.")
    return table


def convert_board(board: sexpr.Node, name: str, schematic_nets: set, log,
                  net_by_pad: dict | None = None) -> Layout:
    """The whole board. Net names come from the SCHEMATIC: KiCad's own
    auto-names (`Net-(R1-Pad2)`, `unconnected-…`) are generated afresh on
    each side and state nothing (conversion-kicad.md #плата)."""
    label = f"board {name}"
    space = board_space(board, log, label)
    copper = copper_layers(board)
    if len(copper) < 2:
        raise SystemExit(
            f"{label}: a single-sided board — the stack formula does not "
            f"express one (layout.md).")
    stack = read_stack(board, len(copper), log, label)
    translate = net_translation(board, net_by_pad or {}, log, label)

    elements, graphics, holes = [], [], []
    signals: dict[str, Signal] = {}

    def signal_of(net_name: str) -> Signal:
        if net_name not in signals:
            signals[net_name] = Signal(name=net_name)
        return signals[net_name]

    auto = 0

    seen_auto: dict[str, str] = {}

    def resolve(net_name: str) -> str:
        """The schematic's name for what the board calls `net_name`."""
        nonlocal auto
        if net_name in translate:
            return translate[net_name]
        if net_name and net_name in schematic_nets:
            return net_name
        # A net the board names but no pad ties to the schematic — copper
        # that reaches nothing. It keeps its geometry and gets a name of
        # its own, since copper belongs to a signal (signal.md).
        if net_name not in seen_auto:
            auto += 1
            seen_auto[net_name] = f"N${auto}" if not net_name else net_name.lstrip("/")
        return seen_auto[net_name]

    for node in sexpr.kids(board, "footprint"):
        got = convert_element(node, space, log, label)
        if got is None:
            continue
        element, pad_nets = got
        elements.append(element)
        for pad, net_name in pad_nets.items():
            # `unconnected-(U1-BP-Pad4)` is KiCad's way of saying this pad
            # is on no net at all — it invents one such name per loose pad.
            # A <contactref> asserts a connection (contactref.md), so there
            # is nothing here to assert.
            if geo.unescape(net_name).startswith("unconnected-"):
                continue
            signal_of(resolve(geo.unescape(net_name))).contactrefs.append(
                ContactRef(element=element.name, pad=pad))

    for child in board[1:]:
        if not isinstance(child, list):
            continue
        tag = child[0]
        if tag.startswith("gr_"):
            result = convert_graphic(child, space, copper, log, label)
            if result is not None:
                graphics.append(result)
        elif tag in ("segment", "arc"):
            copper_item, net_name = convert_track(child, space, copper, log, label)
            if copper_item is not None:
                signal_of(resolve(net_name)).copper.append(copper_item)
        elif tag == "via":
            via, net_name = convert_via(child, space, log, label)
            if via is not None:
                signal_of(resolve(net_name)).copper.append(via)
        elif tag == "zone":
            # The zone outline is the edge of the copper, and the IR keeps
            # the pen's centre line (polygon.md) — the inverse offset is
            # its own piece of work, not yet done here.
            log(f"{label}: a zone is not carried yet — its copper is missing "
                f"from the result")

    return Layout(name=name, stack=stack, elements=elements,
                  signals=list(signals.values()), graphics=graphics, holes=holes)
