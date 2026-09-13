"""KiCad `.kicad_sch` -> the IR canvas — conversion-kicad.md #схема.

Sheets in the IR do not exist: there is one canvas, and KiCad's top-level
pages are laid out on it side by side, each bounded by a frame synthesized
from its own paper size. So every page carries an ORIGIN — where its own
top-left corner sits on the shared canvas — and page coordinates are
resolved against it.

That resolution is the one place the schematic side flips Y: a symbol's
own space is Y-up in both formats, but the placed canvas is Y-down in
KiCad. The law is the exact inverse of `schematic_export._page_x`/
`_page_y`, which was ground-truthed on the 47 parts tolmach shares with
KiCad's own import of it.
"""

from __future__ import annotations

from ir.attr import Attr
from ir.component_instance import ComponentInstance
from ir.graphics import Line, Text
from ir.naming import sanitize_attr_key
from ir.note import Note
from ir.part import Part

from . import geometry as geo
from . import sexpr

# KiCad's own page sizes, in mm, landscape — width first. `(paper "A4")`
# names one of these; `(paper "User" W H)` states its own.
PAPER_MM = {
    "A5": (210, 148), "A4": (297, 210), "A3": (420, 297), "A2": (594, 420),
    "A1": (841, 594), "A0": (1189, 841),
    "A": (279.4, 215.9), "B": (431.8, 279.4), "C": (558.8, 431.8),
    "D": (863.6, 558.8), "E": (1117.6, 863.6),
    "USLetter": (279.4, 215.9), "USLegal": (355.6, 215.9), "USLedger": (431.8, 279.4),
}

LAYER_SYMBOL = 94
LAYER_NETS = 91
LAYER_INFO = 97
LAYER_BOUNDS = 98
LAYER_GRAPHICS = 99

# frame.md: the stamp fields a page carries.
_STAMP_KEYS = ("title", "company", "rev", "date")


def page_size(tree: sexpr.Node, log, label: str) -> tuple[int, int]:
    """The page's width and height in µm. A named size may be turned on
    its side by a trailing `portrait`."""
    paper = sexpr.kid(tree, "paper")
    atoms = sexpr.atoms(paper) if paper else []
    if not atoms:
        log(f"{label}: no paper size — A4 assumed")
        return geo.um(297), geo.um(210)
    name = str(atoms[0])
    if name == "User" and len(atoms) >= 3:
        return geo.um(atoms[1]), geo.um(atoms[2])
    size = PAPER_MM.get(name)
    if size is None:
        log(f"{label}: unknown paper size {name!r} — A4 assumed")
        size = PAPER_MM["A4"]
    width, height = size
    if any(str(a) == "portrait" for a in atoms[1:]):
        width, height = height, width
    return geo.um(width), geo.um(height)


class Page:
    """One `.kicad_sch` being read onto the shared canvas.

    `origin` is (x0, y1): the canvas coordinates of this page's top-left
    corner — its frame's left edge and its TOP edge, the larger Y in a
    Y-up space."""

    def __init__(self, origin: tuple[int, int], size: tuple[int, int]):
        self.x0, self.y1 = origin
        self.width, self.height = size

    def x(self, value_mm) -> int:
        return geo.um(value_mm) + self.x0

    def y(self, value_mm) -> int:
        return self.y1 - geo.um(value_mm)

    def at(self, node: sexpr.Node) -> tuple[int, int, int]:
        """An `(at …)` resolved onto the canvas, with the angle in mdeg.

        The angle is NOT negated: KiCad's positive angle turns the same
        way the IR's does as displayed (library_export.kicad_rot)."""
        x_mm, y_mm, angle = geo.at(node)
        return self.x(x_mm), self.y(y_mm), round(angle * 1000) % 360000

    def point(self, node: sexpr.Node) -> tuple[int, int]:
        a = sexpr.atoms(node)
        return self.x(a[0]), self.y(a[1])


def stamp_fields(tree: sexpr.Node) -> dict[str, str]:
    """The page's title block — frame.md's stamp."""
    block = sexpr.kid(tree, "title_block")
    if block is None:
        return {}
    out = {}
    for key in _STAMP_KEYS:
        node = sexpr.kid(block, key)
        atoms = sexpr.atoms(node) if node else []
        if atoms and str(atoms[0]):
            out[key] = str(atoms[0])
    return out


def _prop(node: sexpr.Node, name: str) -> sexpr.Node | None:
    for p in sexpr.kids(node, "property"):
        atoms = sexpr.atoms(p)
        if atoms and str(atoms[0]) == name:
            return p
    return None


def _prop_value(node: sexpr.Node, name: str) -> str:
    p = _prop(node, name)
    atoms = sexpr.atoms(p) if p is not None else []
    return str(atoms[1]) if len(atoms) > 1 else ""


def placement_transform(node: sexpr.Node) -> tuple[int, int]:
    """A placement's rotation and mirror, in the IR's own terms.

    The IR mirrors about the vertical axis and then rotates
    (units.md #зеркало-применяется-до-поворота), which is KiCad's
    `(mirror y)` exactly. KiCad also writes the equivalent `(mirror x)`
    with the angle turned half round — `R(θ)·My == R(θ+180)·Mx` — and
    that is the same placement said differently, so it folds into the one
    rule rather than becoming a second one."""
    _x, _y, angle = geo.at(node)
    rot = round(angle * 1000) % 360000
    mirror_node = sexpr.kid(node, "mirror")
    axis = str(sexpr.atoms(mirror_node)[0]) if mirror_node else ""
    if axis == "y":
        return rot, 1
    if axis == "x":
        return (rot + 180000) % 360000, 1
    return rot, 0


def _gate_of(component, unit: int) -> str:
    """KiCad's unit number -> the gate's name. Units are 1-based and gates
    keep the order they were read in (library.convert_symbol_definition)."""
    if component is None or not component.gates:
        return ""
    index = max(1, unit) - 1
    if index >= len(component.gates):
        return component.gates[0].name
    return component.gates[index].name


def _device_of(component, footprint: str) -> str | None:
    """Which device this placement chose, by the footprint it names."""
    if component is None or not component.devices:
        return None
    bare = footprint.rsplit(":", 1)[-1]
    for d in component.devices:
        if d.footprint == bare:
            return d.name
    return None


def convert_placement(node: sexpr.Node, page: Page, components: dict, log,
                      label: str) -> tuple[Part, ComponentInstance] | None:
    """One placed `(symbol …)` -> the part it is and where it sits."""
    lib_node = sexpr.kid(node, "lib_id")
    if lib_node is None:
        return None
    lib_id = str(sexpr.atoms(lib_node)[0])
    component = components.get(lib_id)
    if component is None:
        log(f"{label}: placement of {lib_id!r}, which no sheet caches — dropped")
        return None

    designator = _prop_value(node, "Reference")
    if not designator:
        log(f"{label}: a placement of {lib_id!r} has no reference — dropped")
        return None
    # A leading `#` is KiCad's mark for a VIRTUAL part — how a supply
    # symbol stays out of the netlist and off the board. The IR reads that
    # role off the `sup` pin instead (power-symbol.md), and the export
    # side puts the `#` back on. Carried through, it would double.
    designator = designator.lstrip("#")
    # part.md: a designator is upper-case. KiCad does not enforce it.
    if designator != designator.upper():
        log(f"{label}: designator {designator!r} -> {designator.upper()!r}")
        designator = designator.upper()
    if not designator:
        log(f"{label}: a placement of {lib_id!r} has no reference — dropped")
        return None

    unit_node = sexpr.kid(node, "unit")
    unit = int(sexpr.atoms(unit_node)[0]) if unit_node else 1
    footprint = _prop_value(node, "Footprint")

    attrs = []
    value = _prop_value(node, "Value")
    if value:
        attrs.append(Attr("value", value))
    for prop in sexpr.kids(node, "property"):
        atoms = sexpr.atoms(prop)
        if len(atoms) < 2:
            continue
        key, text = str(atoms[0]), str(atoms[1])
        if key in ("Reference", "Value", "Footprint") or not text:
            continue
        # conversion-kicad.md #атрибуты: a field that differs from the
        # library's becomes an attribute of the PART; one that matches is
        # inherited and not repeated.
        if _library_field(component, key) == text:
            continue
        fixed = sanitize_attr_key(key)
        if fixed is not None:
            log(f"{label}: attribute key {key!r} out of domain -> {fixed!r}")
            key = fixed
        attrs.append(Attr(key, text))

    part = Part(name=designator, component=component.name,
                library=component.library or "", attrs=attrs,
                device=_device_of(component, footprint))

    x, y, _angle = page.at(node)
    rot, mirror = placement_transform(node)
    instance = ComponentInstance(part=designator, x=x, y=y,
                                 gate=_gate_of(component, unit),
                                 rot=rot, mirror=mirror,
                                 texts=_instance_texts(node, page))
    return part, instance


def _library_field(component, key: str) -> str | None:
    for a in component.attrs:
        if a.name.lower() == key.lower():
            return a.value
    return None


def _instance_texts(node: sexpr.Node, page: Page) -> list[Text]:
    """component-instance.md: a placement's own placeholder layout, in
    absolute canvas coordinates — and when present it is exhaustive, so
    every visible field goes in, not only the moved ones."""
    texts = []
    for prop in sexpr.kids(node, "property"):
        atoms = sexpr.atoms(prop)
        if not atoms or geo.is_hidden(prop):
            continue
        key = str(atoms[0])
        height, align, mirror = geo.text_effects(prop)
        if not height:
            continue
        x, y, angle = page.at(prop)
        content = ">" + {"Reference": "NAME", "Value": "VALUE"}.get(key, key.upper())
        layer = {"Reference": 95, "Value": 96}.get(key, LAYER_INFO)
        texts.append(Text(x, y, height, layer, align, content=content,
                          rot=angle, mirror=mirror))
    return texts


class Connectivity:
    """Connectivity read off coordinates — the thing KiCad has instead of
    a declared net (conversion-kicad.md: "Связность выводится из
    координат"). Union-find over points.

    KiCad's own rules, which this follows:
      - a wire joins its two ends;
      - a wire END lying anywhere on another wire joins them (that is the
        T, and KiCad draws a junction dot there by itself);
      - two wires CROSSING with neither end on the other are NOT joined
        unless a junction says so;
      - a junction joins everything passing through its point;
      - a pin joins whatever is at the point its tip lands on.
    """

    def __init__(self):
        self.parent: dict[tuple[int, int], tuple[int, int]] = {}
        self.wires: list[tuple[tuple[int, int], tuple[int, int], object]] = []

    def find(self, p):
        self.parent.setdefault(p, p)
        root = p
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[p] != root:
            self.parent[p], p = root, self.parent[p]
        return root

    def union(self, a, b) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb

    def add_wire(self, a, b, line) -> None:
        self.wires.append((a, b, line))
        self.union(a, b)

    def touch(self, p) -> None:
        """Register a point (a pin tip, a label anchor) and join it to any
        wire it lands on."""
        self.find(p)
        for a, b, _line in self.wires:
            if p != a and p != b and _on_segment(p, a, b):
                self.union(p, a)

    def junction(self, p) -> None:
        self.find(p)
        for a, b, _line in self.wires:
            if _on_segment(p, a, b):
                self.union(p, a)


def _on_segment(p, a, b) -> bool:
    """Is p on the segment a-b? Schematic wires are orthogonal in
    practice but not by rule, so this is the general test, in exact
    integer arithmetic."""
    (px, py), (ax, ay), (bx, by) = p, a, b
    if (px - ax) * (by - ay) != (py - ay) * (bx - ax):
        return False
    return min(ax, bx) <= px <= max(ax, bx) and min(ay, by) <= py <= max(ay, by)


def read_wires(tree: sexpr.Node, page: Page, conn: Connectivity) -> None:
    for node in sexpr.kids(tree, "wire"):
        pts = sexpr.kid(node, "pts")
        points = [page.point(p) for p in sexpr.kids(pts, "xy")] if pts else []
        width = geo.stroke_width(node, 0)
        for a, b in zip(points, points[1:]):
            if a == b:
                continue
            conn.add_wire(a, b, Line(a[0], a[1], b[0], b[1], width, LAYER_NETS))
    for node in sexpr.kids(tree, "junction"):
        conn.junction(page.point(sexpr.kid(node, "at") or node))


_LABEL_TAGS = ("label", "global_label", "hierarchical_label")

# label.md / feedback: a label that JOINS nets by name is always a flag,
# and its shape is a hint about the signal, not an ERC rule. KiCad states
# that shape outright on a global or hierarchical label.
_LABEL_SHAPE = {
    "input": "IN", "output": "OUT", "bidirectional": "IO",
    "passive": "PASSIVE", "tri_state": "IO",
}


def _label_style(kind: str, node: sexpr.Node):
    """A local label is plain text on its wire; a global or hierarchical
    one is a flag, shaped by what KiCad says it carries."""
    from ir.label import LabelStyle

    if kind == "label":
        return LabelStyle.CRUMMY
    shape = sexpr.kid(node, "shape")
    name = str(sexpr.atoms(shape)[0]) if shape else "passive"
    return getattr(LabelStyle, _LABEL_SHAPE.get(name, "PASSIVE"))


def read_labels(tree: sexpr.Node, page: Page, conn: Connectivity,
                depth: int = 0) -> list:
    """Every label, anchored and joined to what it sits on. Returns
    (point, name, kind, node, depth).

    `depth` is how deep the page sits in the sheet tree — 0 at the top.
    A local label names its net only within its own sheet, so flattening
    can bring two different names onto one net; the shallower one wins
    (see `build_nets`)."""
    out = []
    for tag in _LABEL_TAGS:
        for node in sexpr.kids(tree, tag):
            atoms = sexpr.atoms(node)
            if not atoms:
                continue
            name = geo.overbar(geo.unescape(str(atoms[0])))
            x, y, _angle = page.at(node)
            conn.touch((x, y))
            out.append(((x, y), name, tag, node, depth))
    return out


def read_pins(instances, parts, components: dict, symbols: dict, page: Page,
              conn: Connectivity, log, label: str) -> list:
    """Where every pin of every placement lands on the canvas.

    A pin's own `(at …)` IS its connection point — the free end — and the
    body lies a `length` away along its angle. Ground truth in this very
    project: the resistor's pin sits at y=3.81 with length 1.27, and its
    body edge is at 2.54."""
    from eagle.geometry import transform_point

    out = []
    by_name = {p.name: p for p in parts}
    for inst in instances:
        part = by_name.get(inst.part)
        if part is None:
            continue
        component = components.get(f"{part.library}:{part.component}"
                                   if part.library else part.component)
        if component is None:
            continue
        gate = next((g for g in component.gates if g.name == inst.gate), None)
        if gate is None:
            continue
        symbol = symbols.get((component.library, gate.symbol))
        if symbol is None:
            log(f"{label}: {inst.part} names symbol {gate.symbol!r}, which is not "
                f"in the pool — its pins carry no connection")
            continue
        for pin in symbol.pins:
            x, y = transform_point(pin.x, pin.y, inst.mirror, inst.rot, inst.x, inst.y)
            conn.touch((x, y))
            out.append(((x, y), inst.part, pin, inst.gate))
    return out


def convert_graphics(tree: sexpr.Node, page: Page, log, label: str
                     ) -> tuple[list, list[Note]]:
    """Decorative text and lines. A text box becomes a `<note>`; a
    one-line box is ordinary text."""
    graphics, notes = [], []
    for node in sexpr.kids(tree, "text"):
        atoms = sexpr.atoms(node)
        content = str(atoms[0]) if atoms else ""
        if not content:
            continue
        x, y, angle = page.at(node)
        height, align, mirror = geo.text_effects(node)
        if angle % 90000:
            log(f"{label}: text {content[:20]!r} at {angle / 1000}° -> nearest 90°")
            angle = round(angle / 90000) * 90000 % 360000
        graphics.append(Text(x, y, height, LAYER_GRAPHICS, align,
                             content=geo.overbar(content), rot=angle, mirror=mirror))
    for node in sexpr.kids(tree, "text_box"):
        atoms = sexpr.atoms(node)
        content = str(atoms[0]) if atoms else ""
        if not content:
            continue
        x, y, angle = page.at(node)
        height, _align, mirror = geo.text_effects(node)
        size = sexpr.atoms(sexpr.kid(node, "size"))
        w = geo.um(size[0]) if size else 0
        h = geo.um(size[1]) if len(size) > 1 else height
        # note.md anchors a note by its box; KiCad gives the top-left
        # corner, and the Y flip has already turned that into the top.
        notes.append(Note(x=x, y=y, w=w, h=h, height=height, layer=LAYER_GRAPHICS,
                          content=geo.overbar(content), rot=angle, mirror=mirror))
    for node in sexpr.kids(tree, "polyline"):
        pts = sexpr.kid(node, "pts")
        points = [page.point(p) for p in sexpr.kids(pts, "xy")] if pts else []
        width = geo.stroke_width(node, 0)
        for a, b in zip(points, points[1:]):
            if a != b:
                graphics.append(Line(a[0], a[1], b[0], b[1], width, LAYER_GRAPHICS))
    return graphics, notes


def _pick_name(names: set, log, label: str) -> str | None:
    """One name for one net, out of what the pages called it.

    A local label names its net only inside its own sheet, so flattening
    can bring two different names onto one net — this project does exactly
    that, calling one net `VPP/MCLR` outside and `VPP-MCLR` inside. **The
    shallower page wins**: it is the one that sees the whole net, while
    the name inside was local to a sheet that no longer exists. The loss
    goes in the log, named.

    Two different names at the SAME depth is a genuine contradiction — no
    page is above the other — and that is refused."""
    if not names:
        return None
    best = min(depth for depth, _n in names)
    top = sorted({n for depth, n in names if depth == best})
    if len(top) > 1:
        raise SystemExit(
            f"{label}: one net carries several names on the same page — "
            f"{', '.join(top)}. A net has one name; rename all but one in "
            f"KiCad, then convert.")
    for depth, n in sorted(names):
        if depth != best and n != top[0]:
            log(f"{label}: net {top[0]!r} is also labelled {n!r} on a flattened "
                f"sheet — that name is lost, the parent's is kept")
    return top[0]


def read_sheet_pins(tree: sexpr.Node, page: Page) -> list:
    """A sheet symbol's pins, as points on the PARENT page, with the file
    each sheet names. Returns (point, pin name, sheet file, sheet node)."""
    out = []
    for sheet in sexpr.kids(tree, "sheet"):
        filename = ""
        for prop in sexpr.kids(sheet, "property"):
            atoms = sexpr.atoms(prop)
            if len(atoms) > 1 and str(atoms[0]) == "Sheetfile":
                filename = str(atoms[1])
        for pin in sexpr.kids(sheet, "pin"):
            atoms = sexpr.atoms(pin)
            if not atoms:
                continue
            x, y, _angle = page.at(pin)
            out.append(((x, y), geo.unescape(str(atoms[0])), filename, sheet))
    return out


def read_hierarchical_ports(tree: sexpr.Node, page: Page) -> dict:
    """The inside half of a sheet's interface: a hierarchical label
    answers the sheet pin of the same name (conversion-kicad.md #схема —
    "листовой вывод отвечает иерархической метке того же имени")."""
    ports = {}
    for node in sexpr.kids(tree, "hierarchical_label"):
        atoms = sexpr.atoms(node)
        if not atoms:
            continue
        x, y, _angle = page.at(node)
        ports[geo.unescape(str(atoms[0]))] = (x, y)
    return ports


def build_nets(conn: Connectivity, labels: list, pins: list, parts: list,
               log, label: str, bridges: list | None = None) -> list:
    """Connected groups -> `<net>`s.

    Two names on one connected group is a refusal
    (conversion-kicad.md #метки): the IR gives a net one name, and
    KiCad's own priority resolution is not something to reproduce —
    it would pick silently where the source is ambiguous."""
    from ir.label import Label
    from ir.net import Net, Segment
    from ir.pin import Direction
    from ir.pinref import PinRef

    groups: dict = {}
    for a, b, line in conn.wires:
        groups.setdefault(conn.find(a), {"lines": [], "labels": [], "pinrefs": [],
                                          "names": set(), "supply": []})["lines"].append(line)

    def group_of(point):
        return groups.get(conn.find(point))

    value_by_part = {p.name: (p.attr("value") if hasattr(p, "attr") else None) for p in parts}
    for p in parts:
        for a in p.attrs:
            if a.name.lower() == "value":
                value_by_part[p.name] = a.value

    for point, name, kind, node, depth in labels:
        group = group_of(point)
        if group is None:
            log(f"{label}: label {name!r} touches no wire — its net has no geometry, dropped")
            continue
        # A hierarchical label is the INSIDE half of a sheet port, and a
        # flattened sheet has no port left — so, like the sheet pin facing
        # it, the name is only a candidate. What the author wrote on the
        # wire itself is the name (`VPP/MCLR` here), and the interface
        # name (`VPP-MCLR`) is what the two runs were joined by.
        if kind == "hierarchical_label":
            group.setdefault("port", name)
        else:
            group["names"].add((depth, name))
        height, align, mirror = geo.text_effects(node)
        _x_mm, _y_mm, angle = geo.at(node)
        group["labels"].append(Label(x=point[0], y=point[1], height=height or 1270,
                                     align=align, layer=LAYER_NETS,
                                     style=_label_style(kind, node),
                                     rot=round(angle * 1000) % 360000, mirror=mirror))

    # line.md #провод-нулевой-длины: KiCad joins pins by ABUTMENT — a
    # supply symbol set straight against a capacitor's lead, with no wire
    # between them. The IR has no "connected by nothing": a connection is
    # always a wire, and this one's length is zero. Without it the whole
    # net would vanish, since a segment must carry a wire (segment.md).
    at_point: dict = {}
    for point, _designator, _pin, _gate in pins:
        at_point.setdefault(point, 0)
        at_point[point] += 1
    for point, count in at_point.items():
        if count < 2 or conn.find(point) in groups:
            continue
        groups[conn.find(point)] = {
            "lines": [Line(point[0], point[1], point[0], point[1], 0, LAYER_NETS)],
            "labels": [], "pinrefs": [], "names": set(), "supply": []}

    for point, designator, pin, gate in pins:
        group = group_of(point)
        if group is None:
            continue
        # pinref.md: `gate` is written IFF the target is a part — a
        # module channel has no sections. None here would say "channel",
        # and the schematic would look for a <modinst> by this name.
        group["pinrefs"].append(PinRef(inst=designator, pin=pin.name, gate=gate))
        if pin.direction is Direction.SUPPLY:
            # power-symbol.md: the bus name is the part's own value, and
            # it names the net this pin touches.
            supply = value_by_part.get(designator)
            if supply:
                group["supply"].append(supply)

    # A flattened sheet's own wires are a separate connected piece — the
    # sheet boundary is not geometry. What joins them is the interface:
    # a sheet pin on the parent and the hierarchical label of the same
    # name inside. Those two pieces are ONE net with two segments, so the
    # join is made between GROUPS, never between points: merging points
    # would put wires from two pages into one segment, and a segment is a
    # connected run (segment.md).
    logical: dict = {}

    def lfind(key):
        logical.setdefault(key, key)
        while logical[key] != key:
            key = logical[key]
        return key

    for point_a, point_b, port in bridges or ():
        ga, gb = conn.find(point_a), conn.find(point_b)
        if ga not in groups or gb not in groups:
            log(f"{label}: sheet port {port!r} has no wire on one side — not joined")
            continue
        ra, rb = lfind(ga), lfind(gb)
        if ra != rb:
            logical[ra] = rb

    # The port's own name is WEAK. A flattened sheet has no boundary left,
    # so its pin is not a module pin any more — it is just where two runs
    # of one net met. A real name on either side (a label, a supply
    # symbol) is what the author wrote, and it wins; the port name is used
    # only when the net would otherwise have none.
    weak: dict = {}
    for point_a, _point_b, port in bridges or ():
        weak.setdefault(lfind(conn.find(point_a)), port)

    merged: dict = {}
    for root, group in groups.items():
        merged.setdefault(lfind(root), []).append(group)

    nets: dict[str, list] = {}
    auto = 0
    for root, members in merged.items():
        group = {"lines": [], "labels": [], "pinrefs": [], "names": set(), "supply": []}
        for g in members:
            group["names"] |= g["names"]
            group["supply"] += g["supply"]
            if "port" in g:
                weak.setdefault(root, g["port"])
        # A supply symbol names its net wherever it sits, so it counts as
        # a top-level name. Local labels carry the depth of their page.
        names = {(0, n) for n in group["supply"]} | group["names"]
        name = _pick_name(names, log, label)
        if name is None:
            if root in weak:
                name = weak[root]
            else:
                auto += 1
                name = f"N${auto}"
        nets.setdefault(name, []).extend(members)

    out = []
    for name, members in nets.items():
        segments = [Segment(lines=g["lines"], labels=g["labels"], pinrefs=g["pinrefs"])
                    for g in members if g["lines"]]
        if segments:
            out.append(Net(name=name, segments=segments))
    return out


# ------------------------------------------------------------------ frames

def frame_symbol(size: tuple[int, int], library: str | None):
    """The page border, as the IR states one: a `<shape>` on layer 98 is
    what MAKES a symbol a frame (frame.md, component.md #особые-роли).

    KiCad has no frame object at all — it draws the border and the title
    block itself, from the paper setting — so the frame is synthesized
    here, at the size of the page it bounds. Its grid and stamp are not
    recreated: they are not in the file (conversion-kicad.md #схема)."""
    from ir.graphics import Shape
    from ir.symbol import Symbol

    width, height = size
    name = f"FRAME_{round(width / 1000)}x{round(height / 1000)}"
    shape = Shape(width // 2, height // 2, width, height, LAYER_BOUNDS, outline=0)
    return Symbol(name=name, library=library, graphics=[shape])


def frame_part(symbol_name: str, library: str | None, designator: str,
               page: Page, number: int, sheetname: str, stamp: dict):
    """The frame's part and its placement — one per page.

    `sheet` is frame.md's own convention `номер. имя`: KiCad states both,
    the page number keeps the order and the name is what a human reads."""
    from ir.component import Component, Gate

    component = Component(name=symbol_name, gates=[Gate(symbol=symbol_name)],
                          prefix="FRAME", library=library)
    attrs = [Attr("sheet", f"{number}. {sheetname}" if sheetname else str(number))]
    for key, value in stamp.items():
        attrs.append(Attr(key, value))
    part = Part(name=designator, component=symbol_name,
                library=library or "", attrs=attrs)
    # shape.md puts a shape's own origin at its CENTRE, and the frame's
    # shape was built around the page's own (0,0) corner — so the
    # placement sits at that corner, not at the middle of the page.
    instance = ComponentInstance(part=designator, x=page.x0,
                                 y=page.y1 - page.height, gate="")
    return component, part, instance


def split_stamp(stamps: list[dict]) -> tuple[dict, list[dict]]:
    """frame.md: a stamp field that reads the same on EVERY page belongs to
    the schematic as a whole; one that differs belongs to its own page's
    frame. No field gets a second home."""
    if not stamps:
        return {}, []
    keys = set().union(*(s.keys() for s in stamps))
    shared = {k: stamps[0].get(k) for k in keys
              if all(s.get(k) == stamps[0].get(k) for s in stamps)
              and stamps[0].get(k)}
    per_page = [{k: v for k, v in s.items() if k not in shared} for s in stamps]
    return shared, per_page


def sheet_graphics(tree: sexpr.Node, page: Page, log, label: str) -> list:
    """A sheet symbol, redrawn as plain graphics.

    conversion-kicad.md #схема: the top page of such a project IS the block
    diagram, and without the blocks nothing would be left of it but an
    empty frame. The drawing carries no connectivity — that already lives
    in the nets."""
    from ir.graphics import Shape

    out = []
    for sheet in sexpr.kids(tree, "sheet"):
        x, y, _angle = page.at(sheet)
        size = sexpr.atoms(sexpr.kid(sheet, "size"))
        if len(size) < 2:
            continue
        width, height = geo.um(size[0]), geo.um(size[1])
        stroke = geo.stroke_width(sheet, 0)
        out.append(Shape(x + width // 2, y - height // 2, width, height,
                         LAYER_GRAPHICS, outline=stroke or 152))
        for prop in sexpr.kids(sheet, "property"):
            atoms = sexpr.atoms(prop)
            if len(atoms) < 2 or not str(atoms[1]):
                continue
            px, py, angle = page.at(prop)
            text_height, align, mirror = geo.text_effects(prop)
            out.append(Text(px, py, text_height, LAYER_GRAPHICS, align,
                            content=geo.unescape(str(atoms[1])), rot=angle,
                            mirror=mirror))
        for pin in sexpr.kids(sheet, "pin"):
            atoms = sexpr.atoms(pin)
            if not atoms:
                continue
            px, py, angle = page.at(pin)
            text_height, align, mirror = geo.text_effects(pin)
            out.append(Text(px, py, text_height or 1270, LAYER_GRAPHICS, align,
                            content=geo.unescape(str(atoms[0])), rot=angle,
                            mirror=mirror))
    return out
