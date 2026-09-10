"""IR `Symbol` / `Footprint` / `Component` -> Eagle `<library>` content —
the reverse of library.py, conversion-eagle.md #библиотеки-1 (the "В
Eagle" pass). No technology reconstruction: every IR `Component` becomes
its own independent `<deviceset>`, even ones that came from a
technology-split family on import — the spec says so explicitly
("Технология остаётся единственной безымянной... заводить его при
выгрузке не из чего").
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from functools import partial

from ir.graphics import Arc, Line, Polygon, Shape, Text
from ir.pad import Hole, Pad, Smd

from . import geometry as geo


def write_pin(p, log) -> ET.Element:
    return ET.Element(
        "pin", name=p.name, x=geo.mm(p.x), y=geo.mm(p.y),
        visible=geo.pin_visible_name(p.pinvis, p.padvis),
        length=geo.pin_length_name(p.length, log),
        direction=p.direction.value, rot=geo.eagle_rot(0, p.rot),
    )


def write_geometry(g, is_footprint: bool, layer_reverse, log) -> list[ET.Element]:
    """One IR graphic -> zero or more Eagle elements. `layer_reverse` is
    `geo.footprint_layer_reverse` (bound with `log`) for footprint space,
    or identity for schematic space."""
    canon_layer, anti = g.layer, getattr(g, "anti", False)

    if anti:
        if is_footprint and abs(canon_layer) in (1,):
            eagle_num = 41 if canon_layer > 0 else 42
        elif isinstance(g, Polygon):
            eagle_num = layer_reverse(canon_layer)
            return [geo.write_polygon(g, eagle_num, pour="cutout")]
        else:
            log(f"anti-object on layer {canon_layer} outside !1/!-1 has no Eagle equivalent "
                f"(only <polygon> can be a cutout there) — dropped")
            return []
    else:
        eagle_num = layer_reverse(canon_layer)

    if isinstance(g, (Line, Arc)):
        return [geo.write_wire(g, eagle_num)]
    if isinstance(g, Text):
        if g.width is not None:
            log(f"text {g.content!r}: explicit width has no Eagle equivalent — dropped")
        return [geo.write_text(g, eagle_num)]
    if isinstance(g, Polygon):
        return [geo.write_polygon(g, eagle_num)]
    if isinstance(g, Shape):
        if g.roundness == 0 and g.outline != 0:
            # Eagle's <rectangle> is always solid — no contour-only
            # rectangle exists there. Written filled instead: on layer 98
            # this is exactly the frame recognition marker (symbol.md
            # #is_frame looks for a <shape>, not four <wire>s — decomposing
            # into wires here would silently un-frame the symbol on the
            # next import), and elsewhere it's the closest Eagle can do.
            log(f"shape at ({g.x},{g.y}): outline-only rectangle has no Eagle equivalent "
                f"(always solid there) — exported filled")
        elif g.roundness not in (0, 100) or (g.roundness == 100 and g.w != g.h and g.outline != 0):
            log(f"shape at ({g.x},{g.y}): roundness={g.roundness} w={g.w} h={g.h} has no exact "
                f"Eagle primitive (only rectangle or circle) — approximated")
        return [geo.write_shape(g, eagle_num)]
    raise TypeError(f"unexpected graphic type: {g!r}")


def write_geometry_children(parent: ET.Element, graphics: list, is_footprint: bool, layer_reverse, log) -> None:
    for g in graphics:
        for el in write_geometry(g, is_footprint, layer_reverse, log):
            parent.append(el)


_FRAME_CELL_UM = 45_000  # ~45 mm — no exact reconstruction from IR (see below)


def _write_native_frame(g: Shape, log) -> ET.Element:
    """frame.md: a `<shape layer=98>` inside a symbol IS the frame — but
    Eagle's own frame is a dedicated `<frame>` element (columns/rows grid),
    not a rectangle. `<shape layer=98>` only carries the IR-canonical
    recognition marker (x/y/w/h), not Eagle's columns/rows — those were
    never stored anywhere after import, so they're re-derived from a
    fixed cell size rather than an invented constant; the grid this draws
    won't necessarily match the source pixel-for-pixel, logged."""
    x1, y1 = g.x - g.w // 2, g.y - g.h // 2
    x2, y2 = x1 + g.w, y1 + g.h
    columns = max(1, round(g.w / _FRAME_CELL_UM))
    rows = max(1, round(g.h / _FRAME_CELL_UM))
    log(f"frame {g.w}x{g.h} um: columns/rows not preserved from import — "
        f"approximated as {columns}x{rows} from a {_FRAME_CELL_UM} um cell")
    return ET.Element("frame", x1=geo.mm(x1), y1=geo.mm(y1), x2=geo.mm(x2), y2=geo.mm(y2),
                       columns=str(columns), rows=str(rows), layer="94")


def write_symbol(el_parent: ET.Element, s, log) -> ET.Element:
    sym_el = ET.SubElement(el_parent, "symbol", name=s.name)
    plain_graphics = []
    for g in s.graphics:
        if isinstance(g, Shape) and g.layer == 98:
            sym_el.append(_write_native_frame(g, log))
        else:
            plain_graphics.append(g)
    write_geometry_children(sym_el, plain_graphics, is_footprint=False, layer_reverse=geo.schematic_layer, log=log)
    for p in s.pins:
        sym_el.append(write_pin(p, log))
    return sym_el


def write_pad(p: Pad, log) -> ET.Element:
    """Eagle's THT pad stores one `diameter` plus a shape keyword — it has
    no independent width/height at all, so a genuinely rectangular
    (width != height) IR pad has no exact Eagle shape. `long` is the
    closest primitive; its actual elongation is computed by Eagle's own
    design rules, not from our numbers, so this is logged as approximate."""
    if p.width != p.height:
        log(f"pad {p.name!r}: {p.width}x{p.height} um has no exact Eagle THT shape "
            f"(only one diameter, no independent width/height) -> shape 'long', approximate")
        shape, diameter = "long", min(p.width, p.height)
    else:
        shape, diameter = ("square" if p.roundness == 0 else "round"), p.width
    return ET.Element("pad", name=p.name, x=geo.mm(p.x), y=geo.mm(p.y), drill=geo.mm(p.drill),
                       diameter=geo.mm(diameter), shape=shape, rot=geo.eagle_rot(0, p.rot),
                       stop="no" if p.stopmask == 0 else "yes",
                       thermals="no" if p.thermals == 0 else "yes")


def write_smd(p: Smd, layer_reverse) -> ET.Element:
    eagle_layer = 1 if p.layer > 0 else 16
    return ET.Element(
        "smd", name=p.name, x=geo.mm(p.x), y=geo.mm(p.y), dx=geo.mm(p.width), dy=geo.mm(p.height),
        layer=str(eagle_layer), rot=geo.eagle_rot(0, p.rot), roundness=str(p.roundness),
        stop="no" if p.stopmask == 0 else "yes", cream="no" if p.paste == 0 else "yes",
        thermals="no" if p.thermals == 0 else "yes",
    )


def write_hole(h: Hole) -> ET.Element:
    return ET.Element("hole", x=geo.mm(h.x), y=geo.mm(h.y), drill=geo.mm(h.drill))


def _model3d_record(m) -> dict:
    """The inverse of library.py's `_convert_3d_models` — µm/mdeg back to
    the bare mm/degree numbers NoABS.Eagle3d's `<!--3d:{...}-->` comment
    carries. `geo.mm`/`geo._degrees` never round again (units.md: that
    happened once, on import), just re-render the same integers as
    Eagle's own decimal string, which `float()` turns into a JSON number
    with the shortest form that reads back exact."""
    rec: dict = {}
    if m.key is not None:
        rec["key"] = m.key
    rec["tx"], rec["ty"], rec["tz"] = float(geo.mm(m.tx)), float(geo.mm(m.ty)), float(geo.mm(m.tz))
    # The frame turns back with it — see geo.eagle_to_mcad_rot. Skipping
    # this here would still round-trip cleanly (both halves wrong the same
    # way), which is exactly why it went unnoticed the first time.
    rx, ry, rz = geo.mcad_to_eagle_rot(m.rx / 1000, m.ry / 1000, m.rz / 1000)
    rec["rx"], rec["ry"], rec["rz"] = float(rx), float(ry), float(rz)
    return rec


def write_footprint(el_parent: ET.Element, f, log) -> ET.Element:
    pkg_el = ET.SubElement(el_parent, "package", name=f.name)
    if f.models:
        # eagle.dtd: <package> is (description?, ...) — description, if
        # present, must be the first child. Only the NoABS.Eagle3d comment
        # is reconstructed; any other text the source description once
        # carried (a datasheet link, prose) was never kept in IR at all
        # (library.md #описание-не-переезжает-в-проект) and isn't restored.
        desc_el = ET.SubElement(pkg_el, "description")
        desc_el.text = " ".join(
            f"<!--3d:{json.dumps(_model3d_record(m), separators=(',', ':'))}-->" for m in f.models
        )
    layer_reverse = partial(geo.footprint_layer_reverse, anti=False, log=log)
    write_geometry_children(pkg_el, f.graphics, is_footprint=True, layer_reverse=layer_reverse, log=log)
    for pad in f.pads:
        pkg_el.append(write_smd(pad, layer_reverse) if isinstance(pad, Smd) else write_pad(pad, log))
    for hole in f.holes:
        pkg_el.append(write_hole(hole))
    return pkg_el


def write_gate(g) -> ET.Element:
    return ET.Element("gate", name=g.name, symbol=g.symbol, x=geo.mm(g.x), y=geo.mm(g.y))


def write_map(m) -> ET.Element:
    return ET.Element("connect", gate=m.gate, pin=m.pin, pad=m.pad)


def write_device(d, log) -> ET.Element:
    """A real Eagle `<device>` never carries `<attribute>` directly —
    ground-truthed against all 5 test projects, every device attribute
    lives inside `<technologies><technology name=...>`, even when that
    name is empty (the "no technology" placeholder, since we don't
    reconstruct named technologies on export)."""
    dev_el = ET.Element("device", name=d.name, package=d.footprint)
    connects_el = ET.SubElement(dev_el, "connects")
    for m in d.maps:
        connects_el.append(write_map(m))
    if d.attrs:
        techs_el = ET.SubElement(dev_el, "technologies")
        tech_el = ET.SubElement(techs_el, "technology", name="")
        for a in d.attrs:
            ET.SubElement(tech_el, "attribute", name=a.name, value=a.value)
    return dev_el


def collect_power_symbol_values(parts) -> dict[tuple[str, str], list[str]]:
    """(library, component) -> sorted distinct `value` attr strings seen
    across every part using it. power-symbol.md components carry no
    `Device` at all (a power symbol has no footprint) — but Eagle still
    wants ONE real `<device>` per distinct bus (component.md's own
    request: "для каждой шины питания — свой power-symbol device"), so
    the exporter needs to know, ahead of writing any one deviceset, every
    value its own parts actually use. Harmless to compute for non-power
    components too — `write_component` only consults this for a
    device-less deviceset whose gate symbol carries a `sup` pin."""
    values: dict[tuple[str, str], set[str]] = {}
    for p in parts:
        value_attr = next((a.value for a in p.attrs if a.name.lower() == "value"), None)
        if value_attr is None:
            continue
        values.setdefault((p.library, p.component), set()).add(value_attr)
    return {k: sorted(v) for k, v in values.items()}


def _is_power_symbol_component(c, symbols_by_key) -> bool:
    if len(c.gates) != 1:
        return False
    symbol = symbols_by_key.get((c.library, c.gates[0].symbol))
    if symbol is None:
        return False
    from ir.pin import Direction
    return any(p.direction is Direction.SUPPLY for p in symbol.pins)


def write_component(el_parent: ET.Element, c, symbols_by_key, values_by_component, log) -> ET.Element:
    # `uservalue` isn't modeled in IR (schematic.py's finalize_parts only
    # consults it transiently, at import, to decide whether a part with
    # no literal value= should get the deviceset+device fallback baked in
    # — conversion-eagle.md #атрибуты). Always "yes" on export: a part
    # IR already gave a real `value` writes it explicitly regardless of
    # this flag (uservalue only governs Eagle's OWN fallback display,
    # never overrides a stored one), so the only case this flag actually
    # controls is the one where IR correctly left `value` unset — and for
    # that case "yes" is exactly the right answer (it's what let the
    # original stay unset in the first place).
    ds_el = ET.SubElement(el_parent, "deviceset", name=c.name, prefix=c.prefix, uservalue="yes")
    gates_el = ET.SubElement(ds_el, "gates")
    for g in c.gates:
        gates_el.append(write_gate(g))
    devices_el = ET.SubElement(ds_el, "devices")
    if not c.devices:
        values = values_by_component.get((c.library, c.name), []) if _is_power_symbol_component(c, symbols_by_key) else []
        if values:
            # power-symbol.md: one real, named <device> per distinct bus
            # this deviceset actually represents in this project — not
            # one generic device relying on a dynamic ">VALUE" for
            # everything, which erases the fact that these are different
            # rails at the device level (only the instance still knew).
            for value in values:
                dev_el = ET.SubElement(devices_el, "device", name=value)
                ET.SubElement(dev_el, "connects")
                techs_el = ET.SubElement(dev_el, "technologies")
                tech_el = ET.SubElement(techs_el, "technology", name="")
                ET.SubElement(tech_el, "attribute", name="VALUE", value=value, constant="no")
        else:
            ET.SubElement(devices_el, "device", name="")
    for d in c.devices:
        devices_el.append(write_device(d, log))
    if c.attrs:
        # Ground-truthed against all 5 test projects: no real <deviceset>
        # ever carries a direct <attribute> — Eagle has no family-level
        # attribute slot at all, only per-device/per-technology ones.
        log(f"deviceset {c.name!r}: {len(c.attrs)} component-level attr(s) have no Eagle "
            f"equivalent (only device/technology attributes exist there) — dropped")
    return ds_el


def write_library(el_parent: ET.Element, name: str, symbols, footprints, components, log,
                   values_by_component: dict | None = None) -> ET.Element:
    """eagle.dtd: `<library>` is (description?, packages?, packages3d?,
    symbols?, devicesets?) — a strict sequence, packages before symbols."""
    symbols_by_key = {(s.library, s.name): s for s in symbols if s.name is not None}
    lib_el = ET.SubElement(el_parent, "library", name=name)
    packages_el = ET.SubElement(lib_el, "packages")
    for f in footprints:
        write_footprint(packages_el, f, log)
    symbols_el = ET.SubElement(lib_el, "symbols")
    for s in symbols:
        write_symbol(symbols_el, s, log)
    devicesets_el = ET.SubElement(lib_el, "devicesets")
    for c in components:
        write_component(devicesets_el, c, symbols_by_key, values_by_component or {}, log)
    return lib_el
