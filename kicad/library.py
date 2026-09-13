"""KiCad `lib_symbols` -> IR `Symbol` + `Component` —
conversion-kicad.md #библиотеки.

The symbol side of a KiCad project needs no external library: every
`.kicad_sch` carries the cache `lib_symbols`, one entry per `lib_id`, and
that entry IS the reference copy. What KiCad has no notion of at all is a
library-level DEVICE — the symbol↔footprint pairing lives on each placed
instance — so devices are not read here; they are assembled from actual
use, by the schematic pass.
"""

from __future__ import annotations

import re

from ir.attr import Attr
from ir.component import Component, Device, Gate, Map
from ir.graphics import Text
from ir.naming import sanitize_attr_key
from ir.pin import Direction, Pin
from ir.symbol import Symbol

from . import geometry as geo
from . import sexpr

LAYER_SYMBOL = 94
LAYER_NAME = 95
LAYER_VALUE = 96
LAYER_INFO = 97

# gate.md: sections are laid out top-down for the component preview, this
# far apart. KiCad has no such layout of its own to read (there is no
# library-level component there at all), so it is computed.
GATE_GAP = 2540

_ELECTRICAL_TO_DIRECTION = {
    "input": Direction.IN,
    "output": Direction.OUT,
    "bidirectional": Direction.IO,
    "passive": Direction.PASSIVE,
    "power_in": Direction.POWER,
    "open_collector": Direction.OPEN_COLLECTOR,
    "tri_state": Direction.HIZ,
}

# KiCad draws distinctions the IR does not. Each keeps its pin and takes
# the nearest direction, with a line in the log — same policy the Eagle
# path uses for `nc` (conversion-eagle.md #библиотеки): a library that
# already drew one is not ours to judge.
_ELECTRICAL_APPROXIMATED = {
    "power_out": (Direction.POWER, "pwr"),
    "open_emitter": (Direction.OPEN_COLLECTOR, "oc"),
    "no_connect": (Direction.PASSIVE, "pas"),
    "unspecified": (Direction.PASSIVE, "pas"),
    "free": (Direction.PASSIVE, "pas"),
}

# conversion-kicad.md #атрибуты. `Reference` gives the prefix and is not an
# attribute; `Value` on a LIBRARY symbol is the default part value, which
# the IR keeps on the part, not on the family; `Footprint` names the device
# and is read from the placements, not from here.
_NOT_AN_ATTR = {"reference", "value", "footprint"}

_PLACEHOLDER_LAYER = {"Reference": LAYER_NAME, "Value": LAYER_VALUE}
_PLACEHOLDER_KEY = {"Reference": "NAME", "Value": "VALUE"}


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


def _text_is_visible(prop: sexpr.Node) -> bool:
    """A property is drawn unless hidden or given a zero font."""
    if geo.is_hidden(prop):
        return False
    height, _align, _mirror = geo.text_effects(prop)
    return height > 0


def convert_pin(node: sexpr.Node, log, label: str) -> tuple[Pin, str] | None:
    """The pin plus the pad number it addresses. KiCad's `name` is the
    displayed name and its `number` is the pad address — the two halves
    the IR keeps in `<pin>` and in `<map>` respectively."""
    atoms = sexpr.atoms(node)
    electrical = str(atoms[0]) if atoms else "passive"
    direction = _ELECTRICAL_TO_DIRECTION.get(electrical)
    if direction is None:
        approximated = _ELECTRICAL_APPROXIMATED.get(electrical)
        if approximated is None:
            log(f"{label}: pin of unknown electrical type {electrical!r} -> pas")
            direction = Direction.PASSIVE
        else:
            direction, as_ir = approximated
            log(f"{label}: pin type {electrical} has no IR equivalent -> {as_ir}")

    x_mm, y_mm, angle = geo.at(node)
    x, y = geo.um(x_mm), geo.um(y_mm)
    rot = round(angle * 1000) % 360000

    length_node = sexpr.kid(node, "length")
    length = geo.um(sexpr.atoms(length_node)[0]) if length_node else 2540

    name_node = sexpr.kid(node, "name")
    number_node = sexpr.kid(node, "number")
    name = str(sexpr.atoms(name_node)[0]) if name_node else ""
    number = str(sexpr.atoms(number_node)[0]) if number_node else ""
    # `~` alone is KiCad's way of writing "this pin has no name" — a
    # literal tilde, not an overbar (that is `~{…}`, converted below).
    if name == "~":
        name = ""
    name = geo.overbar(name)

    # pin.md forbids a nameless pin, and KiCad allows one — a resistor's
    # pins are drawn with no name at all. The pad number is the only other
    # identity the pin has, so it becomes the name, and `pinvis` records
    # that nothing was drawn.
    pinvis = 1 if name and geo.text_effects(name_node)[0] > 0 else 0
    padvis = 1 if number and geo.text_effects(number_node)[0] > 0 else 0
    if not name:
        name = number
        pinvis = 0
    if not name:
        log(f"{label}: pin at ({x}, {y}) has neither name nor number — dropped")
        return None

    return Pin(name=name, direction=direction, x=x, y=y, rot=rot,
               length=length, pinvis=pinvis, padvis=padvis), number


def _convert_graphics(sub: sexpr.Node, log, label: str, default_um: int) -> list:
    graphics = []
    for child in sub[1:]:
        if not isinstance(child, list):
            continue
        tag = child[0]
        if tag == "rectangle":
            graphics.append(geo.convert_rectangle(child, LAYER_SYMBOL, default_um))
        elif tag == "circle":
            shape = geo.convert_circle(child, LAYER_SYMBOL, default_um)
            if shape is not None:
                graphics.append(shape)
        elif tag == "arc":
            arc = geo.convert_arc(child, LAYER_SYMBOL, default_um)
            if arc is None:
                log(f"{label}: degenerate arc — dropped")
            else:
                graphics.append(arc)
        elif tag == "polyline":
            result = geo.convert_polyline(child, LAYER_SYMBOL, log, default_um)
            if result is not None:
                graphics.append(result)
        elif tag == "text":
            text = geo.convert_text(child, LAYER_SYMBOL)
            if text is not None:
                graphics.append(text)
        elif tag == "bezier":
            # A cubic curve has no IR equivalent, and approximating one by
            # segments would invent geometry the author never drew.
            log(f"{label}: bezier curve has no IR equivalent — dropped")
        elif tag == "text_box":
            log(f"{label}: text box inside a symbol is not carried — dropped")
    return graphics


def _sub_index(sub_name: str, base: str) -> tuple[int, int] | None:
    """`R_1_1` -> (unit 1, body style 1). The base name may itself hold
    underscores (`MountingHole_Pad_1_1`), so the split is from the right."""
    if not sub_name.startswith(base):
        return None
    parts = sub_name.rsplit("_", 2)
    if len(parts) != 3:
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


_TRAILING_DIGITS = re.compile(r"\d+$")


def _prefix_of(reference: str, log, label: str) -> str:
    """component.md: a component carries the PREFIX, and the designator's
    number is added when it is placed.

    Two things get stripped. A leading `#` is KiCad's mark for a virtual
    part — how supply symbols stay out of the netlist — and the IR reads
    that role off the `sup` pin instead. Trailing digits are the other
    half: a library `Reference` is meant to be the prefix, but authors who
    build a symbol from a placed part leave the number on it (`U3`, `J1`,
    `X1` in this very project), and KiCad itself re-annotates over it."""
    prefix = reference.lstrip("#").upper()
    trimmed = _TRAILING_DIGITS.sub("", prefix)
    if trimmed != prefix:
        log(f"{label}: library reference {reference!r} carries a number — prefix {trimmed!r}")
    return trimmed or "PART"


def _gate_letter(index: int) -> str:
    """A, B, … Z, AA — KiCad stores no name for a unit, so one is made."""
    name = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(ord("A") + rem) + name
    return name


def _bbox(symbol: Symbol) -> tuple[int, int]:
    """Height and top edge of what this section occupies — enough to stack
    the sections without overlap."""
    ys = [p.y for p in symbol.pins]
    for g in symbol.graphics:
        for attr in ("y", "y1", "y2"):
            value = getattr(g, attr, None)
            if value is not None:
                ys.append(value)
        if hasattr(g, "vertices"):
            ys += [v.y for v in g.vertices]
        if hasattr(g, "h") and getattr(g, "h", None):
            ys += [g.y - g.h // 2, g.y + g.h // 2]
    if not ys:
        return 0, 0
    return max(ys) - min(ys), max(ys)


def convert_symbol_definition(node: sexpr.Node, log, default_um: int
                              ) -> tuple[list[Symbol], Component, dict] | None:
    """One `(symbol "lib:name" …)` out of `lib_symbols` -> the pooled
    symbols of its sections, the component that gates them, and the
    pin↔pad numbering keyed by gate — the raw material of `<map>`, which
    only becomes a device once the schematic says which footprint this
    symbol was paired with."""
    atoms = sexpr.atoms(node)
    if not atoms:
        return None
    lib_id = str(atoms[0])
    library, _, name = lib_id.rpartition(":")
    library = library or None
    label = f"symbol {lib_id}"

    # power-symbol.md: a supply symbol is one whose pin carries the bus
    # name onto its net. KiCad states the fact outright, on the symbol.
    is_supply = sexpr.kid(node, "power") is not None

    base = name
    by_unit: dict[int, list[sexpr.Node]] = {}
    for sub in sexpr.kids(node, "symbol"):
        sub_atoms = sexpr.atoms(sub)
        if not sub_atoms:
            continue
        index = _sub_index(str(sub_atoms[0]), base)
        if index is None:
            log(f"{label}: sub-symbol {sub_atoms[0]!r} does not name a unit — dropped")
            continue
        unit, style = index
        if style > 1:
            # DeMorgan alternate body. The IR has one drawing per gate.
            log(f"{label}: alternate (DeMorgan) body style {style} is not carried — dropped")
            continue
        by_unit.setdefault(unit, []).append(sub)

    # Unit 0 is the part drawn on EVERY section — a shared body, not a
    # section of its own.
    common = by_unit.pop(0, [])
    units = sorted(by_unit)
    if not units:
        units = [0]
        by_unit[0] = []

    symbols: list[Symbol] = []
    gates: list[Gate] = []
    pin_pads: dict[str, list[tuple[str, str]]] = {}
    multi = len(units) > 1
    common_ids = {id(sub) for sub in common}
    shared_pins = sum(len(sexpr.kids(sub, "pin")) for sub in common)
    if multi and shared_pins:
        log(f"{label}: {shared_pins} pin(s) shared by every section are carried "
            f"on section A alone — one pad answers one pin")
    for i, unit in enumerate(units):
        pairs: list[tuple[Pin, str]] = []
        graphics = []
        for sub in common + by_unit[unit]:
            # A pin drawn on unit 0 belongs to EVERY section in KiCad —
            # that is how a quad gate shares one VCC and one GND. The IR
            # cannot say that: map.md makes pad↔pin a single link, so the
            # same pad claimed by four sections would be four links. The
            # shared pins go on the first section only; the shared GRAPHIC
            # goes on all of them, being a drawing and not a connection.
            if id(sub) in common_ids and i > 0:
                graphics += _convert_graphics(sub, log, label, default_um)
                continue
            for pin_node in sexpr.kids(sub, "pin"):
                pair = convert_pin(pin_node, log, label)
                if pair is not None:
                    pairs.append(pair)
            graphics += _convert_graphics(sub, log, label, default_um)
        if is_supply:
            pairs = [(Pin(name=p.name, direction=Direction.SUPPLY, x=p.x, y=p.y,
                          rot=p.rot, length=p.length, pinvis=p.pinvis, padvis=p.padvis), n)
                     if p.direction is Direction.POWER else (p, n) for p, n in pairs]
        pairs = _disambiguate(pairs, log, label)
        gate_name = _gate_letter(i) if multi else ""
        symbol_name = f"{name}_{gate_name}" if multi else name
        if i == 0:
            # The placeholders belong to the component, and the export side
            # reads them off the FIRST section only — putting a copy in
            # every section would draw the designator once per gate.
            graphics += _placeholders(node, log)
        symbols.append(Symbol(name=symbol_name, library=library,
                              pins=[p for p, _ in pairs], graphics=graphics))
        gates.append(Gate(symbol=symbol_name, name=gate_name))
        pin_pads[gate_name] = [(p.name, n) for p, n in pairs]

    _stack_gates(gates, symbols)

    prefix = _prefix_of(_prop_value(node, "Reference"), log, label)

    attrs = []
    for prop in sexpr.kids(node, "property"):
        prop_atoms = sexpr.atoms(prop)
        if len(prop_atoms) < 2:
            continue
        key, value = str(prop_atoms[0]), str(prop_atoms[1])
        if key.lower() in _NOT_AN_ATTR or not value:
            continue
        fixed = sanitize_attr_key(key)
        if fixed is not None:
            log(f"{label}: attribute key {key!r} out of domain -> {fixed!r}")
            key = fixed
        attrs.append(Attr(name=key, value=value))

    component = Component(name=name, gates=gates, prefix=prefix,
                          library=library, attrs=attrs)
    return symbols, component, pin_pads


def _disambiguate(pairs: list[tuple[Pin, str]], log, label: str
                  ) -> list[tuple[Pin, str]]:
    """pin.md: a pin name is unique within its symbol, and KiCad's is not —
    three `GND` pins on different pads are ordinary there. The pad number
    is what tells them apart, so it becomes the `@` suffix; `display_name`
    strips it back off, and all three still draw as `GND`."""
    counts: dict[str, int] = {}
    for pin, _number in pairs:
        counts[pin.name.lower()] = counts.get(pin.name.lower(), 0) + 1
    if all(n == 1 for n in counts.values()):
        return pairs

    out: list[tuple[Pin, str]] = []
    taken: set[str] = set()
    for pin, number in pairs:
        name = pin.name
        if counts[pin.name.lower()] > 1:
            name = f"{pin.name}@{number}" if number else pin.name
            # Two pins on the SAME pad (KiCad's jumper pins) collide even
            # after the suffix; number them apart rather than lose one.
            candidate, n = name, 1
            while candidate.lower() in taken:
                n += 1
                candidate = f"{name}_{n}"
            name = candidate
            log(f"{label}: pin name {pin.name!r} repeats — carried as {name!r}")
        taken.add(name.lower())
        out.append((Pin(name=name, direction=pin.direction, x=pin.x, y=pin.y,
                        rot=pin.rot, length=pin.length,
                        pinvis=pin.pinvis, padvis=pin.padvis), number))
    return out


def _placeholders(node: sexpr.Node, log) -> list[Text]:
    """placeholders.md: a visible property of the symbol is drawn, and in
    the IR what is drawn is a `>KEY` text on its own layer."""
    texts = []
    for prop in sexpr.kids(node, "property"):
        atoms = sexpr.atoms(prop)
        if not atoms:
            continue
        key = str(atoms[0])
        if not _text_is_visible(prop):
            continue
        x_mm, y_mm, angle = geo.at(prop)
        height, align, mirror = geo.text_effects(prop)
        layer = _PLACEHOLDER_LAYER.get(key, LAYER_INFO)
        content = ">" + _PLACEHOLDER_KEY.get(key, key.upper())
        texts.append(Text(geo.um(x_mm), geo.um(y_mm), height, layer, align,
                          content=content, rot=round(angle * 1000) % 360000,
                          mirror=mirror))
    return texts


def _stack_gates(gates: list[Gate], symbols: list[Symbol]) -> None:
    """gate.md: the preview layout, computed because the source has none.
    Sections stack downward in order, each clear of the one above."""
    if len(gates) < 2:
        return
    y = 0
    for gate, symbol in zip(gates, symbols):
        height, top = _bbox(symbol)
        gate.x = 0
        gate.y = y - top
        y = gate.y + (top - height) - GATE_GAP


def convert_libraries(sheets, log, default_um: int = geo.DEFAULT_LINE_UM
                      ) -> tuple[list[Symbol], list[Component], dict]:
    """The symbol cache of EVERY sheet, merged into one pool.

    Each `.kicad_sch` carries its own copy of every symbol it places, so a
    two-sheet project holds `GND` twice. One `lib_id` is one component:
    the copies are the same library entry seen from two files. Where two
    copies disagree, the first is kept and the case is logged — the cache
    of one sheet was refreshed and the other was not."""
    symbols: list[Symbol] = []
    components: list[Component] = []
    pin_pads: dict[str, dict] = {}
    seen: dict[str, tuple] = {}
    for tree in sheets:
        cache = sexpr.kid(tree, "lib_symbols")
        if cache is None:
            continue
        for node in sexpr.kids(cache, "symbol"):
            lib_id = str(sexpr.atoms(node)[0])
            result = convert_symbol_definition(node, log, default_um)
            if result is None:
                continue
            syms, component, pads = result
            key = _symbol_key(syms)
            if lib_id in seen:
                if seen[lib_id] != key:
                    log(f"symbol {lib_id}: the sheets disagree on it — the first "
                        f"copy is used ('Update Symbols from Library' in KiCad)")
                continue
            seen[lib_id] = key
            symbols += syms
            components.append(component)
            pin_pads[lib_id] = pads
    return symbols, components, pin_pads


def _symbol_key(symbols: list[Symbol]) -> tuple:
    """What makes two cached copies of one symbol the same symbol: its
    pins and its body. Placeholder layout is left out — it is the
    component's, and an instance moves it freely."""
    from ir.graphics import Text as _Text
    return tuple(
        (s.name,
         tuple(sorted((p.name, p.direction.value, p.x, p.y, p.rot, p.length)
                      for p in s.pins)),
         tuple(sorted(str((type(g).__name__, getattr(g, "layer", None),
                           getattr(g, "x", None), getattr(g, "y", None),
                           getattr(g, "x1", None), getattr(g, "y1", None)))
                      for g in s.graphics if not isinstance(g, _Text))))
        for s in symbols)


def footprint_pairs(sheets, log) -> dict[str, list[str]]:
    """`lib_id` -> the footprints its placements name, in first-seen order.

    conversion-kicad.md #библиотеки: KiCad has no library-level device, so
    the family is read off actual USE. `Device:C` placed twelve times,
    three of them carrying `C_0805` and the rest `C_0603`, is one component
    with two devices — and the board is not needed to see it."""
    pairs: dict[str, list[str]] = {}
    for tree in sheets:
        for placement in sexpr.kids(tree, "symbol"):
            lib_node = sexpr.kid(placement, "lib_id")
            if lib_node is None:
                continue
            lib_id = str(sexpr.atoms(lib_node)[0])
            seen = pairs.setdefault(lib_id, [])
            value = _prop_value(placement, "Footprint")
            if value and value not in seen:
                seen.append(value)
    return pairs


def attach_devices(components: list[Component], pin_pads: dict,
                   pairs: dict[str, list[str]], footprints: dict, log) -> dict[str, set]:
    """Give every component the devices its placements actually used, and
    record which library each footprint has to be filed under.

    A footprint goes in the same library as the symbol that named it
    (library.md: a device addresses its footprint by name WITHIN its own
    library), so one named from two libraries is filed in both."""
    filed: dict[str, set] = {}
    by_lib_id = {f"{c.library}:{c.name}" if c.library else c.name: c for c in components}
    for lib_id, names in pairs.items():
        component = by_lib_id.get(lib_id)
        if component is None:
            continue
        gates = pin_pads.get(lib_id, {})
        devices = []
        for full in names:
            bare = full.rsplit(":", 1)[-1]
            reference = footprints.get(bare)
            if reference is None:
                log(f"{lib_id}: footprint {full!r} is not in the project library — "
                    f"device dropped")
                continue
            filed.setdefault(bare, set()).add(component.library)
            # device.md: the name is a SUFFIX to the family name, unique
            # within the component, and an empty one is legal — that is
            # the usual case, one device. The symbol is never renamed by
            # any of this: it stays `C` in the pool whatever footprints
            # its placements chose.
            #
            # KiCad states no suffix at all (it has no library-level
            # device to carry one), so where a symbol really did resolve
            # to several footprints, the devices are simply numbered.
            # Nothing is invented about them: which is which is stated by
            # the footprint each one names.
            suffix = f"-{len(devices) + 1}" if len(names) > 1 else ""
            maps = _build_maps(gates, reference, lib_id, full, log)
            devices.append(Device(footprint=bare, name=suffix, maps=maps))
        component.devices.extend(devices)
    return filed


def _build_maps(gates: dict, reference, lib_id: str, footprint_name: str, log) -> list[Map]:
    """map.md: pin -> pad, explicitly. KiCad states it by the pin's
    `number`, which IS the pad's name.

    A pad drawn as several copper islands answers ONE pin, and map.md
    gives the pad field a LIST for exactly that."""
    islands: dict[str, list[str]] = {}
    for pad in reference.pads:
        islands.setdefault(pad.name.split("@", 1)[0].lower(), []).append(pad.name)

    maps = []
    for gate_name, pins in gates.items():
        for pin_name, number in pins:
            if not number:
                continue
            group = islands.get(number.lower())
            if group is None:
                log(f"{lib_id}/{footprint_name}: pin {pin_name!r} addresses pad "
                    f"{number!r}, which the footprint does not have — mapping dropped")
                continue
            maps.append(Map(pin=pin_name, pad=" ".join(group), gate=gate_name))
    return maps


def convert_lib_symbols(tree: sexpr.Node, log, default_um: int = geo.DEFAULT_LINE_UM
                        ) -> tuple[list[Symbol], list[Component], dict]:
    """Every symbol cached in one `.kicad_sch`. The third result maps
    `lib_id` -> {gate: [(pin name, pad number)]}, kept for the device pass."""
    cache = sexpr.kid(tree, "lib_symbols")
    symbols: list[Symbol] = []
    components: list[Component] = []
    pin_pads: dict[str, dict] = {}
    if cache is None:
        return symbols, components, pin_pads
    for node in sexpr.kids(cache, "symbol"):
        result = convert_symbol_definition(node, log, default_um)
        if result is None:
            continue
        syms, component, pads = result
        symbols += syms
        components.append(component)
        pin_pads[str(sexpr.atoms(node)[0])] = pads
    return symbols, components, pin_pads
