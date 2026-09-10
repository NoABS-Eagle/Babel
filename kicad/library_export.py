"""IR `Symbol` / `Component` -> KiCad `.kicad_sym` content —
conversion-kicad.md #библиотеки (the "В KiCad" pass).

Device becomes a separate symbol (no library-level device in KiCad): a
component with N devices gives N symbols, `<component><device.name>`
(`R` + `-0603` -> `R-0603`), the pooled Symbol's graphics/pins copied into
each. A component with no devices (a power symbol, conversion-eagle.md
#символы-питания already left it device-less) exports as one symbol
unchanged. A multi-gate component maps 1:1 onto KiCad's own native
"unit" mechanism (`<symbol>_<unit>_1`), needing no synthesized layout —
unlike Eagle, KiCad has real per-unit sub-symbols.
"""

from __future__ import annotations

from ir.graphics import Arc, Line, Polygon, Shape, Text
from ir.pin import Direction

from .sexpr import Sym

_DIRECTION_TO_ELECTRICAL = {
    Direction.IN: Sym("input"),
    Direction.OUT: Sym("output"),
    Direction.IO: Sym("bidirectional"),
    Direction.PASSIVE: Sym("passive"),
    Direction.POWER: Sym("power_in"),
    Direction.SUPPLY: Sym("power_in"),
    Direction.OPEN_COLLECTOR: Sym("open_collector"),
    Direction.HIZ: Sym("tri_state"),
}

# conversion-kicad.md #атрибуты: Reference/Value/Footprint/Datasheet/
# Description are their own dedicated KiCad properties, never duplicated
# out of `component.attrs` even if an Eagle-imported project happened to
# carry a same-named attr.
_RESERVED_PROPERTY_NAMES = {"reference", "value", "footprint", "datasheet", "description"}


def mm(value_um: int) -> float:
    """µm -> mm, positions and magnitudes alike. **A symbol's own space
    is Y-up in KiCad too**, exactly like the IR's — so a Y position needs
    no flip here, only the placed-instance canvas (`.kicad_sch`) does
    (schematic_export._page_y). Ground truth, not derivation: the same
    tolmach imported by KiCad itself (`testData/Eagle/tolmach/hardware/
    kicad/tolmach-eagle-import.kicad_sym`) copies every asymmetric symbol
    coordinate verbatim — the 3.3V arrow tip stays at y=+2.54, the R
    Value placeholder at y=-2.032."""
    return value_um / 1000


def kicad_rot(rot_mdeg: int) -> int:
    """Angles are a literal copy in both spaces — KiCad's own positive
    angle is counterclockwise **as displayed**, the same sense Eagle and
    the IR use, so the canvas Y-flip does not negate it. Same ground
    truth as `mm`: across all 47 parts the two tolmach files share, an
    unmirrored R90/R180/R270 instance is 90/180/270 in KiCad, never the
    complement (and the 3.3V pin's R90 stays 90 in the library)."""
    return rot_mdeg % 360000 // 1000


def field_rot(rot_mdeg: int) -> int:
    """A property/field angle folds into [0, 180): KiCad never renders
    field text upside down, it only moves the anchor. The justify is NOT
    touched in the process — a mirrored or 180°-rotated instance keeps
    its library alignment verbatim (ground truth: `bottom-right` stays
    `right bottom` for every one of the 75 fields the two tolmach files
    share, under every rotation and both mirror states)."""
    return kicad_rot(rot_mdeg) % 180


def justify(align: str) -> list:
    """align.md's nine-cell grid ("bottom-left", "center-center", ...) ->
    KiCad's own justify tokens — horizontal first, and only the non-center
    half of each axis is named, center being the unwritten default on
    both (same omission rule conversion.md uses elsewhere for this grid)."""
    v, h = align.split("-")
    tokens = []
    if h != "center":
        tokens.append(Sym(h))
    if v != "center":
        tokens.append(Sym(v))
    return tokens


def is_power_symbol(symbol) -> bool:
    """power-symbol.md: a `sup`-direction pin is what makes a symbol a
    supply symbol."""
    return any(p.direction is Direction.SUPPLY for p in symbol.pins)


def power_designator(name: str, power: bool) -> str:
    """A KiCad reference beginning with `#` marks a VIRTUAL part: the
    symbol still drives its net, but it is not a component — it stays out
    of the BOM and out of the netlist's node list. Supply symbols are
    exactly that (they are drawn, not bought), and without the prefix
    every one of them counts as a real part: our GND net listed 72 nodes
    against the ground truth's 37, the extra 35 being the GND symbols
    themselves. Ground truth writes `#+P` in the library and `#+P1` on the
    placement, i.e. the IR designator with `#` in front — no renaming."""
    return f"#{name}" if power else name


def _effects(height_um: int = 1270, align: str = "center-center") -> list:
    node = ["effects", ["font", ["size", mm(height_um), mm(height_um)]]]
    tokens = justify(align)
    if tokens:
        node.append(["justify", *tokens])
    return node


def write_property(name: str, value: str, x_um: int = 0, y_um: int = 0,
                    rot_mdeg: int = 0, height_um: int = 1270,
                    align: str = "center-center", hidden: bool = True) -> list:
    node = ["property", name, value,
            ["at", mm(x_um), mm(y_um), field_rot(rot_mdeg)],
            ["show_name", Sym("no")], ["do_not_autoplace", Sym("no")]]
    if hidden:
        node.append(["hide", Sym("yes")])
    node.append(_effects(height_um, align))
    return node


def _placeholder_positions(symbol) -> dict[str, tuple[int, int, int, int, str]]:
    """conversion-kicad.md #плейсхолдеры (export direction) and the
    matching #атрибуты import rule this mirrors: a placeholder Text in
    the symbol's own graphics is where a field is actually shown — its
    key is the attr name (case-insensitive), same `>KEY` convention
    every importer in this project already produces. The placeholder's
    own height and align travel with it: they are how the field is
    actually drawn, and KiCad stores both per property (ground truth: the
    R Value field keeps size 0.762 and `right bottom`, not the 1.27
    default)."""
    out = {}
    for g in symbol.graphics:
        if isinstance(g, Text) and g.content.startswith(">"):
            key = g.content[1:].split("@")[0].upper()
            out[key] = (g.x, g.y, g.rot, g.height, g.align)
    return out


def _pin_text_effects(visible: int) -> list:
    """A hidden pin NAME or PAD number is drawn at font size zero, not with
    KiCad's `hide` flag — `hide` on a `(pin ...)` hides the whole pin,
    electrical connection included, which is not at all what pin.md's
    `pinvis`/`padvis` mean (they only silence the text). Ground truth: the
    resistor and GND pins KiCad's own import made are all present and
    connectable, each carrying `(size 0 0)` on exactly the text Eagle
    marked invisible — 75 of them across this project."""
    return ["effects", ["font", ["size", 1.27, 1.27] if visible else ["size", 0, 0]]]


def write_pin(p, number: str, log) -> list:
    electrical = _DIRECTION_TO_ELECTRICAL[p.direction]
    return ["pin", electrical, Sym("line"),
            ["at", mm(p.x), mm(p.y), kicad_rot(p.rot)],
            ["length", mm(p.length)],
            ["name", p.display_name(), _pin_text_effects(p.pinvis)],
            ["number", number, _pin_text_effects(p.padvis)]]


def _stroke(width_um: int) -> list:
    return ["stroke", ["width", mm(width_um)], ["type", Sym("default")]]


def write_graphic(g, log) -> list | None:
    if isinstance(g, Text):
        return None  # placeholder or literal label — handled by the caller
    if isinstance(g, Arc):
        # KiCad names an arc by three points on it; arc.md stores the chord
        # plus the swept angle. Endpoints go over verbatim, only the middle
        # one is derived (`Arc.midpoint`). Ground truth: reproduces all six
        # arcs of the inductor body in KiCad's own import of this project
        # to within the 0.1 µm that file is rounded to.
        mx, my = g.midpoint()
        return ["arc",
                ["start", mm(g.x1), mm(g.y1)], ["mid", mx / 1000, my / 1000],
                ["end", mm(g.x2), mm(g.y2)],
                _stroke(g.width), ["fill", ["type", Sym("none")]]]
    if isinstance(g, Line):
        return ["polyline",
                ["pts", ["xy", mm(g.x1), mm(g.y1)], ["xy", mm(g.x2), mm(g.y2)]],
                _stroke(g.width), ["fill", ["type", Sym("none")]]]
    if isinstance(g, Polygon):
        # polygon.md: an IR polygon is closed by definition. KiCad has no
        # closed flag — the first vertex is repeated at the end instead.
        # Ground truth: every closed body in KiCad's own import of this
        # project carries the repeat (IRLML9301's gate triangles), and
        # without it the closing edge's stroke is simply not drawn.
        verts = list(g.vertices) + list(g.vertices[:1])
        pts = ["pts"] + [["xy", mm(v.x), mm(v.y)] for v in verts]
        return ["polyline", pts, _stroke(g.width),
                ["fill", ["type", Sym("outline" if g.fill >= 50 else "none")]]]
    if isinstance(g, Shape):
        w, h = (g.h, g.w) if g.rot in (90000, 270000) else (g.w, g.h)
        x0, y0 = g.x - w // 2, g.y - h // 2
        x1, y1 = g.x + w // 2, g.y + h // 2
        fill = Sym("none" if g.outline else "outline")
        return ["rectangle", ["start", mm(x0), mm(y0)], ["end", mm(x1), mm(y1)],
                _stroke(g.outline), ["fill", ["type", fill]]]
    log(f"symbol graphic {type(g).__name__} has no KiCad symbol-space equivalent yet — dropped")
    return None


def write_gate_symbol(parent_base_name: str, gate_index: int, gate, symbol, device, log) -> list:
    """One KiCad `unit` sub-symbol — `<parent>_<unit>_1`, unit numbered
    from 1 (0 would mean "common to all units", which IR's own per-gate
    symbol pool doesn't produce: every pin/graphic here belongs to this
    gate alone). `parent_base_name` is always the BARE component/device
    name, never library-qualified — ground-truthed against a real
    embedded `lib_symbols` entry: the top symbol is named
    `"tolmach-eagle-import:3.3V"`, but its own unit sub-symbol is
    `"3.3V_1_0"`, not `"tolmach-eagle-import:3.3V_1_0"`. KiCad rejects
    the qualified form outright ("Invalid symbol unit name prefix").

    `device` gives this pin's KiCad `number` — map.md's own pad name,
    device-specific (two devices of one component can number the same
    symbol pin differently). `device=None` (a power symbol, a logo —
    conversion-eagle.md #символы-питания, #элемент-без-выводов) falls
    back to sequential numbering, matching a real KiCad power symbol's
    own single `number="1"`."""
    node = ["symbol", f"{parent_base_name}_{gate_index}_1"]
    for g in symbol.graphics:
        written = write_graphic(g, log)
        if written is not None:
            node.append(written)

    maps_by_pin = {}
    if device is not None:
        for m in device.maps:
            if m.gate == gate.name:
                maps_by_pin.setdefault(m.pin, []).append(m)

    for i, p in enumerate(symbol.pins, start=1):
        if device is None:
            node.append(write_pin(p, str(i), log))
            continue
        pin_maps = maps_by_pin.get(p.name, [])
        if not pin_maps:
            raise ValueError(f"symbol {parent_base_name!r}, gate {gate.name!r}: pin {p.name!r} "
                              "has no device map — library.md's own reference check should "
                              "have caught this already")
        for m in pin_maps:
            for pad in m.pads:
                node.append(write_pin(p, pad, log))
    return node


def write_symbol(name: str, base_name: str, component, gates_and_symbols: list, device,
                  footprint_ref: str, log, power_scope: str = "global") -> list:
    """`gates_and_symbols` is a list of (Gate, Symbol) pairs, one per
    section, in gate order — already resolved by the caller (device.md/
    gate.md pool lookups need the whole library, not just this
    component). `name` is what THIS symbol is addressed by here (bare
    for a standalone `.kicad_sym`, `library:`-qualified when embedded in
    a `.kicad_sch`'s own cache); `base_name` is always bare — unit
    sub-symbols are never library-qualified, see write_gate_symbol."""
    node = ["symbol", name]
    # power-symbol.md: a `sup` pin is what makes this a supply symbol. In
    # KiCad that fact lives on the SYMBOL, as `(power ...)`, and it is what
    # turns its `power_in` pin from something needing a driver into the
    # driver itself — without it every supply net raises
    # `power_pin_not_driven`.
    #
    # **The scope follows the page.** `global` at the top level; `local`
    # inside a module, where KiCad 10 scopes the net to the file — which is
    # exactly module.md's own scope. A global supply symbol there would
    # weld every channel's ground into one, and with 34 channels that is
    # most of the schematic quietly collapsing.
    power = any(is_power_symbol(symbol) for _, symbol in gates_and_symbols)
    if power:
        node.append(["power", Sym(power_scope)])
    node += [["exclude_from_sim", Sym("no")], ["in_bom", Sym("yes")],
             ["on_board", Sym("yes")], ["in_pos_files", Sym("yes")],
             ["duplicate_pin_numbers_are_jumpers", Sym("no")]]

    placeholders = _placeholder_positions(gates_and_symbols[0][1])

    def emit(prop_name: str, placeholder_key: str, value: str, default_xy: tuple[int, int]) -> None:
        pos = placeholders.get(placeholder_key)
        if pos is not None:
            x, y, rot, height, align = pos
            node.append(write_property(prop_name, value, x, y, rot, height, align, hidden=False))
        else:
            dx, dy = default_xy
            node.append(write_property(prop_name, value, dx, dy, hidden=True))

    emit("Reference", "NAME", power_designator(component.prefix, power), (0, -2540))
    emit("Value", "VALUE", "", (0, 2540))
    node.append(write_property("Footprint", footprint_ref, hidden=True))
    node.append(write_property("Datasheet", "", hidden=True))
    node.append(write_property("Description", "", hidden=True))

    attrs_by_key = {a.name.upper(): a for a in component.attrs
                     if a.name.lower() not in _RESERVED_PROPERTY_NAMES}
    seen: set[str] = set()
    for key, attr in attrs_by_key.items():
        seen.add(key)
        pos = placeholders.get(key)
        if pos is not None:
            x, y, rot, height, align = pos
            node.append(write_property(attr.name, attr.value, x, y, rot, height, align, hidden=False))
        else:
            node.append(write_property(attr.name, attr.value, hidden=True))
    for key, pos in placeholders.items():
        if key in ("NAME", "VALUE") or key in seen:
            continue
        x, y, rot, height, align = pos
        node.append(write_property(key, "", x, y, rot, height, align, hidden=False))

    for i, (gate, symbol) in enumerate(gates_and_symbols, start=1):
        node.append(write_gate_symbol(base_name, i, gate, symbol, device, log))

    node.append(["embedded_fonts", Sym("no")])
    return node


def _map_signature(device) -> frozenset:
    return frozenset((m.gate, m.pin, tuple(sorted(m.pads))) for m in device.maps)


def device_groups(component) -> list[tuple[str, list]]:
    """`[(bare symbol name, [devices it serves])]` — conversion-kicad.md
    #библиотеки.

    KiCad has no library-level device, but it does not need one to choose
    a footprint: `Footprint` is a per-instance property, set when the
    symbol is placed. So a family stays ONE symbol and each placement
    carries its own footprint, instead of `R-0402`/`R-0603`/`R-0805`/...
    filling the library with copies that differ in nothing a symbol
    expresses.

    The one thing a symbol does own per device is the pin `number`, which
    map.md lets two devices assign differently (real case: `R-1%`'s
    `-1206` names its pads `P$1`/`P$2` where the rest of the family uses
    `1`/`2`). A single shared number set would wire the odd device's
    placements to the wrong pads — so devices are grouped BY MAP, and only
    a genuinely divergent one splits off. Grouping, not an all-or-nothing
    test: one odd footprint must not shatter the other six.

    The largest group keeps the family name; every other group is named
    after its own first device."""
    groups: dict[frozenset, list] = {}
    for d in component.devices:
        groups.setdefault(_map_signature(d), []).append(d)
    ordered = sorted(groups.values(), key=lambda ds: (-len(ds), ds[0].name))
    return [(component.name if i == 0 else f"{component.name}{ds[0].name}", ds)
            for i, ds in enumerate(ordered)]


def symbol_name_for(component, device) -> str:
    """The bare (library-unqualified) KiCad symbol name a placement of this
    component+device resolves to. Single source of truth for both the
    library writer below and `schematic_export`'s `lib_id`."""
    if device is None or not component.devices:
        return component.name
    for name, devices in device_groups(component):
        if any(d.name == device.name for d in devices):
            return name
    raise ValueError(f"component {component.name!r}: device {device.name!r} is not its own")


def write_component(component, symbols_by_key, log, name_prefix: str = "",
                     power_scope: str = "global") -> list[list]:
    """Returns the KiCad `(symbol ...)` nodes for one component — one per
    distinct pin map, normally just one for the whole device family (see
    `device_groups`). `name_prefix` is `"<library>:"` when embedding into
    a `.kicad_sch`'s own `lib_symbols` cache (whose entries are addressed
    by full `lib_id`, library included) and empty for a standalone
    `.kicad_sym`, where library membership is the file itself."""
    gates_and_symbols = []
    for gate in component.gates:
        symbol = symbols_by_key.get((component.library, gate.symbol))
        if symbol is None:
            raise ValueError(f"component {component.name!r}: gate {gate.name!r} names "
                              f"unknown symbol {gate.symbol!r}")
        gates_and_symbols.append((gate, symbol))

    if not component.devices:
        base_name = component.name
        return [write_symbol(f"{name_prefix}{base_name}", base_name, component,
                              gates_and_symbols, None, "", log, power_scope)]

    groups = device_groups(component)
    if len(groups) > 1:
        log(f"component {component.library}:{component.name}: {len(component.devices)} devices "
            f"carry {len(groups)} different pin maps — split into symbols "
            f"{', '.join(name for name, _ in groups)}")
    out = []
    for base_name, devices in groups:
        # Pin numbers come from the map, identical inside a group by
        # construction. The footprint deliberately does NOT ride along
        # when a group serves several devices — it is the one fact that
        # differs between them, and it belongs on the placement.
        footprint_ref = f":{devices[0].footprint}" if len(devices) == 1 else ""
        out.append(write_symbol(f"{name_prefix}{base_name}", base_name, component,
                                 gates_and_symbols, devices[0], footprint_ref, log, power_scope))
    return out
