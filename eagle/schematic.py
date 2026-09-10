"""Eagle project schematic (`<drawing><schematic>`) -> IR `Schematic` plus
the library pools it draws on — conversion-eagle.md #секции #атрибуты
#схема #символы-питания.

Pipeline, in order:
1. merge the embedded `<libraries>` (Eagle keeps more than one `<library>`
   element under the same `name` when parts pin to different historical
   URNs — ground-truthed in tolmach.sch: `supply_symbols` and `rc` both
   appear twice)
2. `<classes>` -> `Class` pool
3. `<parts>` -> raw part specs (component resolution, own attrs)
4. sheets -> tiled onto one canvas: frame-part discovery + `sheet` attr,
   stray-object containment, `<instances>`, `<nets>`, decorative `<plain>`
5. power-symbol normalization + geometry collapse (needs step 3's net
   connectivity to know each supply part's `value`)
"""

from __future__ import annotations

from ir.attr import Attr
from ir.class_ import Class
from ir.component import Component
from ir.component_instance import ComponentInstance
from ir.graphics import Line, Shape, Text
from ir.label import Label, LabelStyle
from ir.module import Module
from ir.module_instance import ModuleInstance
from ir.net import Net, Segment
from ir.part import Part
from ir.pin import Direction, Pin
from ir.pinref import PinRef
from ir.schematic import Schematic
from ir.symbol import Symbol

from . import geometry as geo
from . import library as lib_conv

_LABEL_ALIGN_DEFAULT = "bottom-left"  # Eagle's <label> carries no `align`
# attribute at all — DTD default, baked in once here rather than logged
# per occurrence (units.md #6: a named default, not a state to flag).


# ---------------------------------------------------------------- libraries

def _merge_pool(bucket: dict, items, key_fn, lib_name: str, kind: str, log) -> dict[str, str]:
    """Returns {original_name: renamed_name} for any item whose name
    collided with an existing, GENUINELY different entry (not the
    "several <library> blocks under one name" case merge_libraries
    already tolerates — this is a second definition of the same NAMED
    item inside that). Ground-truthed in tolmach.sch: a second
    `supply_symbols` snapshot (its own `urn`, distinct from the first's)
    redefines `GND` with different pin geometry — silently keeping only
    the first left every part pinned to the SECOND snapshot rendered
    with the FIRST snapshot's geometry, visibly displaced. Renamed
    Eagle's own way (`@1`, `@2`, ...) instead, so nothing is thrown away
    and both stay resolvable — merge_libraries repoints anything that
    actually came from the renamed snapshot."""
    renames: dict[str, str] = {}
    for item in items:
        k = key_fn(item).lower()
        existing = bucket.get(k)
        if existing is None:
            bucket[k] = item
            continue
        if existing == item:
            continue
        original_name = key_fn(item)
        n = 1
        while f"{k}@{n}" in bucket:
            n += 1
        new_name = f"{original_name}@{n}"
        item.name = new_name
        bucket[new_name.lower()] = item
        renames[original_name] = new_name
        log(f"library {lib_name!r}: two snapshots define {kind} {original_name!r} differently "
            f"— kept both, second renamed {new_name!r}")
    return renames


def merge_libraries(
    libraries_el, log, models_dir=None, restring=None
) -> tuple[list[Symbol], list, list[Component], dict[tuple[str, str], bool], dict[str, dict[str, str]]]:
    """Returns (symbols, footprints, components, uservalue_by_key,
    component_rename_by_urn) — the last one maps a `<library urn=...>`
    block's own urn to {original component name: renamed name}, for
    every component that collided and got renamed by `_merge_pool`
    because IT (not some earlier, still-canonical block) was the second,
    divergent definition. convert_raw_parts uses it to repoint a part
    whose own `library_urn` names that exact block."""
    by_name: dict[str, tuple[dict, dict, dict]] = {}
    uservalue_by_key: dict[tuple[str, str], bool] = {}
    component_rename_by_urn: dict[str, dict[str, str]] = {}
    for lib_el in libraries_el:
        name = lib_el.get("name")
        block_urn = lib_el.get("urn") or ""
        syms, fps, comps, uservalue = lib_conv.convert_library(lib_el, log, models_dir, restring)
        sd, fd, cd = by_name.setdefault(name, ({}, {}, {}))

        sym_renames = _merge_pool(sd, syms, lambda s: s.name, name, "symbol", log)
        fp_renames = _merge_pool(fd, fps, lambda f: f.name, name, "footprint", log)
        if sym_renames or fp_renames:
            # These `comps` were just parsed from THIS block, before we
            # knew any of its own symbols/footprints would need renaming
            # — their gates/devices still point at the pre-rename names.
            for c in comps:
                for g in c.gates:
                    if g.symbol in sym_renames:
                        g.symbol = sym_renames[g.symbol]
                for d in c.devices:
                    if d.footprint in fp_renames:
                        d.footprint = fp_renames[d.footprint]

        pre_rename_uservalue = {orig: uservalue.get((name.lower(), orig.lower()), False) for orig in
                                 {c.name for c in comps}}
        comp_renames = _merge_pool(cd, comps, lambda c: c.name, name, "component", log)
        if comp_renames:
            component_rename_by_urn.setdefault(block_urn, {}).update(comp_renames)

        for orig_name, uv in pre_rename_uservalue.items():
            final_name = comp_renames.get(orig_name, orig_name)
            uservalue_by_key.setdefault((name.lower(), final_name.lower()), uv)

    symbols, footprints, components = [], [], []
    for sd, fd, cd in by_name.values():
        symbols.extend(sd.values())
        footprints.extend(fd.values())
        components.extend(cd.values())
    return symbols, footprints, components, uservalue_by_key, component_rename_by_urn


# ------------------------------------------------------------------ classes

def convert_classes(classes_el, log) -> tuple[list[Class], dict[str, str | None]]:
    """Returns (classes, number_to_name) — net.md/class.md: the class
    literally named "default" doesn't get an IR entry at all; its role is
    played by a net simply not referencing any class (conversion-eagle.md
    #плата: "Класс по умолчанию не переносится"), so its number maps to
    None rather than a name."""
    if classes_el is None:
        return [], {}
    classes = []
    number_to_name: dict[str, str | None] = {}
    for c in classes_el:
        name = c.get("name")
        number = c.get("number")
        if name.lower() == "default":
            number_to_name[number] = None
            continue
        width = geo.um(c.get("width", "0"))
        drill = geo.um(c.get("drill", "0"))
        classes.append(Class(name=name, width=width or None, drill=drill or None))
        number_to_name[number] = name
    return classes, number_to_name


# -------------------------------------------------------------------- parts

class _RawPart:
    __slots__ = ("name", "library", "deviceset", "technology", "device_str",
                 "component_name", "own_attrs", "value")

    def __init__(self):
        self.own_attrs: list[Attr] = []
        self.value: str | None = None


def convert_raw_parts(
    parts_el,
    components_by_key: dict[tuple[str, str], Component],
    log,
    component_rename_by_urn: dict[str, dict[str, str]] | None = None,
) -> list[_RawPart]:
    component_rename_by_urn = component_rename_by_urn or {}
    raw_parts = []
    for p in parts_el:
        rp = _RawPart()
        rp.name = p.get("name")
        rp.library = p.get("library")
        rp.deviceset = p.get("deviceset")
        rp.technology = p.get("technology", "")
        rp.device_str = p.get("device", "")
        rp.component_name = rp.deviceset if not rp.technology else f"{rp.deviceset}{rp.technology}"
        rp.value = p.get("value")
        for a in p.findall("attribute"):
            rp.own_attrs.append(geo.safe_attr(a.get("name"), a.get("value", ""), log))

        # merge_libraries: this part's own <library urn=...> block may
        # have been the SECOND, divergent definition of its component —
        # repoint it to the renamed one, or it would silently resolve
        # against the first (unrelated) snapshot's geometry instead.
        library_urn = p.get("library_urn") or ""
        renamed = component_rename_by_urn.get(library_urn, {}).get(rp.component_name)
        if renamed is not None:
            rp.component_name = renamed

        key = (rp.library.lower(), rp.component_name.lower())
        if key not in components_by_key:
            raise ValueError(f"part {rp.name!r}: component ({rp.library!r}, {rp.component_name!r}) "
                              f"not found (deviceset {rp.deviceset!r}, technology {rp.technology!r})")
        raw_parts.append(rp)
    return raw_parts


# ------------------------------------------------------------- power symbols

def _is_power_symbol(sym: Symbol) -> bool:
    return any(p.direction is Direction.SUPPLY for p in sym.pins)


def _normalize_power_symbol_text(sym: Symbol, log) -> bool:
    """power-symbol.md: the bus name a non-normalized library bakes as
    literal text becomes `>VALUE` — establishes the invariant this
    converter enforces itself (value = net name) rather than trusting
    whatever the source symbol shows. Returns whether anything actually
    needed changing — a symbol that already used `>VALUE` natively was
    never a duplicate-per-rail copy in the first place (see caller)."""
    changed = False
    for g in sym.graphics:
        if isinstance(g, Text) and not g.content.startswith(">"):
            log(f"symbol {sym.library}:{sym.name}: power-symbol text {g.content!r} -> '>VALUE'")
            g.content = ">VALUE"
            changed = True
    return changed


def _graphic_key(g):
    if isinstance(g, Line):
        return ("line", g.x1, g.y1, g.x2, g.y2, g.width, g.layer, g.anti)
    from ir.graphics import Arc, Polygon, Shape
    if isinstance(g, Arc):
        return ("arc", g.x1, g.y1, g.x2, g.y2, g.curve, g.width, g.layer, g.anti)
    if isinstance(g, Shape):
        return ("shape", g.x, g.y, g.w, g.h, g.layer, g.rot, g.roundness, g.outline, g.anti)
    if isinstance(g, Polygon):
        verts = tuple((v.x, v.y, v.curve) for v in g.vertices)
        return ("polygon", g.layer, g.width, verts, g.fill, g.anti)
    if isinstance(g, Text):
        return ("text", g.x, g.y, g.height, g.layer, g.align, g.content, g.width, g.rot, g.mirror, g.ratio, g.anti)
    raise TypeError(f"unexpected graphic type: {g!r}")


def _symbol_signature(sym: Symbol):
    pins = tuple(sorted((p.x, p.y, p.rot, p.direction.value) for p in sym.pins))
    graphics = tuple(sorted(_graphic_key(g) for g in sym.graphics))
    return pins, graphics


def collapse_power_components(
    components: list[Component],
    symbols: list[Symbol],
    log,
) -> tuple[list[Component], dict[tuple[str, str], Component], dict[tuple, tuple[str, str]], dict[tuple, tuple[str, str]]]:
    """Phase 1 of conversion-eagle.md #символы-питания — global and
    pool-only, so it runs exactly once no matter how many schematic scopes
    a project has (the top-level canvas, plus one per module: the
    component/symbol pool is shared by all of them, but part/net names
    are not, which is why phase 2 — `apply_power_rename` — is scoped).

    Groups every power-symbol component by post-normalization geometry and
    decides the SUPPLY1/SUPPLY2/... merges. Returns (final component pool,
    power_components, rename, survivor_pin) — feed the last three into
    `apply_power_rename` once per scope."""
    symbols_by_key = {(s.library, s.name): s for s in symbols if s.name is not None}

    def gate_symbol(component: Component) -> Symbol | None:
        if len(component.gates) != 1:
            return None
        return symbols_by_key.get((component.library, component.gates[0].symbol))

    power_components: dict[tuple[str, str], Component] = {}
    mergeable: dict[tuple[str, str], Component] = {}
    for c in components:
        sym = gate_symbol(c)
        if sym is not None and _is_power_symbol(sym):
            power_components[(c.library, c.name)] = c
            # Only a symbol that ACTUALLY needed normalizing was a
            # duplicate-per-rail copy (a library author baking "+5V",
            # "+12V", ... as literal text into otherwise-identical
            # symbols) — merging is undoing that redundancy. A symbol
            # that already used ">VALUE" natively is a deliberately
            # generic, reusable one (ground-truthed: every merge case in
            # all 5 test projects turned out to be exactly this — already
            # ">VALUE", nothing to normalize), and merging it with its
            # equally-generic siblings would erase real information: each
            # kept its own pin name for a reason, and Eagle treats
            # same-named supply pins as implicitly the same net anywhere
            # on the sheet — collapsing them onto one shared pin name is
            # what actually produced wrong power rails on export.
            if _normalize_power_symbol_text(sym, log):
                mergeable[(c.library, c.name)] = c

    groups: dict[tuple, list[tuple[str, str]]] = {}
    for key, c in mergeable.items():
        sig = _symbol_signature(gate_symbol(c))
        groups.setdefault(sig, []).append(key)

    rename: dict[tuple[str, str], tuple[str, str]] = {}
    survivor_pin: dict[tuple[str, str], tuple[str, str]] = {}  # old key -> (gate, pin)
    merged_components: list[Component] = []
    n = 0
    for sig, keys in groups.items():
        if len(keys) == 1:
            continue  # nothing collided with it — keeps its own name
        n += 1
        merged_name = f"SUPPLY{n}"
        first = power_components[keys[0]]
        merged_lib = first.library
        first_gate = first.gates[0]
        first_pin = gate_symbol(first).pins[0].name
        merged_components.append(Component(
            name=merged_name, gates=first.gates, prefix=first.prefix, library=merged_lib, devices=[],
        ))
        for key in keys:
            rename[key] = (merged_lib, merged_name)
            survivor_pin[key] = (first_gate.name, first_pin)
        log(f"power symbols collapsed into {merged_lib}:{merged_name}: "
            + ", ".join(f"{lib}:{name}" for lib, name in keys))

    kept_keys = {(c.library, c.name) for c in components} - {k for ks in groups.values() if len(ks) > 1 for k in ks}
    final_components = [c for c in components if (c.library, c.name) in kept_keys] + merged_components

    return final_components, power_components, rename, survivor_pin


def apply_power_rename(
    raw_parts: list[_RawPart],
    net_of_part: dict[str, str],
    power_components: dict[tuple[str, str], Component],
    rename: dict[tuple, tuple[str, str]],
    survivor_pin: dict[tuple, tuple[str, str]],
) -> dict[str, tuple[str, str]]:
    """Phase 2 of conversion-eagle.md #символы-питания — scoped to one
    schematic (the top-level canvas, or one module definition): rewrites
    `rp.component_name`/`rp.library`/`rp.value` for this scope's own
    power-symbol parts in place against the renames phase 1 already
    decided, and returns the pin_rewrite this scope's own nets need.

    `pin_rewrite` maps a merged part's name to the (gate, pin) of the
    surviving symbol: power-symbol.md "у схлопнутого компонента вывод
    один, и ссылки на него переписываются" — every pinref naming that
    part must follow, since the merged component's single pin very
    likely isn't spelled the same as what that part originally pointed
    to (each pre-merge symbol names its own pin after its own bus)."""
    pin_rewrite: dict[str, tuple[str, str]] = {}
    for rp in raw_parts:
        old_key = (rp.library, rp.component_name)
        if old_key in power_components:
            if old_key in rename:
                pin_rewrite[rp.name] = survivor_pin[old_key]
            new_lib, new_name = rename.get(old_key, old_key)
            rp.library, rp.component_name = new_lib, new_name
            net_name = net_of_part.get(rp.name)
            if net_name is not None:
                rp.value = net_name

    return pin_rewrite


def apply_pin_rewrite(nets: list[Net], pin_rewrite: dict[str, tuple[str, str]]) -> None:
    """power-symbol.md: "ссылки на него переписываются" — every pinref
    naming a merged part must follow it onto the surviving symbol's
    (gate, pin)."""
    if not pin_rewrite:
        return
    for net in nets:
        for seg in net.segments:
            for ref in seg.pinrefs:
                new_gp = pin_rewrite.get(ref.inst)
                if new_gp is not None:
                    ref.gate, ref.pin = new_gp


def finalize_parts(
    raw_parts: list[_RawPart],
    components_by_key: dict[tuple[str, str], Component],
    uservalue_by_key: dict[tuple[str, str], bool] | None = None,
) -> list[Part]:
    uservalue_by_key = uservalue_by_key or {}
    parts = []
    for rp in raw_parts:
        key = (rp.library.lower(), rp.component_name.lower())
        component = components_by_key[key]
        attrs = list(rp.own_attrs)
        has_value_attr = any(a.name.lower() == "value" for a in attrs)
        value = rp.value
        if value is None and not has_value_attr and not uservalue_by_key.get(key, False):
            # conversion-eagle.md #атрибуты: Eagle itself shows/stores
            # deviceset+device as this part's value when the library
            # doesn't expect a user-typed one (`uservalue="no"`, the
            # DTD's own default) — bake it in now so both exporters read
            # the same fact off the part, rather than leaving the
            # schematic correct (Eagle recomputes the display) and the
            # board wrong (its own <element value> has no such
            # recomputation — ground-truthed: real board elements for
            # these parts always carry this exact string).
            value = f"{rp.component_name}{rp.device_str}"
        if value is not None and not has_value_attr:
            attrs.insert(0, Attr("value", value))
        device = None if not component.devices else rp.device_str
        parts.append(Part(name=rp.name, component=rp.component_name, library=rp.library, device=device, attrs=attrs))
    return parts


# ------------------------------------------------------------------- sheets

_ALIGN_MAP = {
    "bottom-left": "bottom-left", "bottom-center": "bottom-center", "bottom-right": "bottom-right",
    "center-left": "center-left", "center": "center-center", "center-right": "center-right",
    "top-left": "top-left", "top-center": "top-center", "top-right": "top-right",
}


def _instance_gate_symbol(part_name, gate_name, parts_by_name, components_by_key, symbols_by_key):
    rp = parts_by_name[part_name]
    component = components_by_key[(rp.library.lower(), rp.component_name.lower())]
    gate = next((g for g in component.gates if g.name == gate_name), component.gates[0])
    return symbols_by_key.get((component.library, gate.symbol)), component, gate


def convert_sheet(
    sheet_el,
    sheet_index: int,
    tile_dx: int,
    tile_dy: int,
    parts_by_name: dict[str, _RawPart],
    components_by_key: dict[tuple[str, str], Component],
    symbols_by_key: dict[tuple, Symbol],
    class_by_number: dict[str, str | None],
    log,
    modules_by_name: dict[str, Module] | None = None,
) -> tuple[list[ComponentInstance], list[ModuleInstance], list, list[Net], dict[str, str],
           tuple[int, int, int, int] | None, str | None]:
    """Returns (compinsts, modinsts, graphics, nets, net_of_part, frame_bbox, frame_part_name).
    `modules_by_name` is only meaningful at the top level — a module's own
    sheets never legally carry a <moduleinst> (conversion-eagle.md: hierarchy
    is exactly one level deep), so callers converting a module's own sheets
    pass nothing and just check the returned list came back empty."""
    modules_by_name = modules_by_name or {}
    instances_el = sheet_el.find("instances")
    compinsts: list[ComponentInstance] = []
    frame_bbox = None
    frame_part_name = None

    for inst_el in instances_el if instances_el is not None else []:
        part_name = inst_el.get("part")
        gate_name = inst_el.get("gate", "")
        x, y = geo.um(inst_el.get("x")) + tile_dx, geo.um(inst_el.get("y")) + tile_dy
        mirror, rot = geo.angle(inst_el.get("rot"))

        symbol, component, gate = _instance_gate_symbol(
            part_name, gate_name, parts_by_name, components_by_key, symbols_by_key)

        if symbol is not None and symbol.is_frame:
            if frame_part_name is not None and frame_part_name != part_name:
                raise ValueError(f"sheet {sheet_index + 1}: more than one frame instance found")
            frame_part_name = part_name
            shape = geo.find_frame_shape(symbol)
            frame_bbox = geo.bbox_from_shape(shape, mirror, rot, x, y)

        texts = []
        if inst_el.get("smashed") == "yes" and symbol is not None:
            declared = {
                g.content[1:].split("@")[0].upper()
                for g in symbol.graphics if isinstance(g, Text) and g.content.startswith(">")
            }
            for a in inst_el.findall("attribute"):
                key = a.get("name", "").upper()
                if key not in declared:
                    log(f"instance {part_name}/{gate_name}: dropped placeholder-position override "
                        f"for {a.get('name')!r} — symbol declares no such placeholder")
                    continue
                amirror, arot = geo.angle(a.get("rot"))
                align = _ALIGN_MAP.get(a.get("align", "bottom-left"), "bottom-left")
                texts.append(Text(
                    x=geo.um(a.get("x")) + tile_dx, y=geo.um(a.get("y")) + tile_dy,
                    height=geo.um(a.get("size", "1.778")), layer=geo.schematic_layer(int(a.get("layer", "95"))),
                    align=align, content=f">{key}", rot=arot, mirror=amirror,
                    ratio=int(a.get("ratio", "8")),
                ))

        compinsts.append(ComponentInstance(part=part_name, x=x, y=y, gate=gate_name, rot=rot, mirror=mirror, texts=texts))

    modinsts: list[ModuleInstance] = []
    modinsts_el = sheet_el.find("moduleinsts")
    for mi_el in (modinsts_el if modinsts_el is not None else []):
        mod_name = mi_el.get("module")
        mi_name = mi_el.get("name")
        x, y = geo.um(mi_el.get("x")) + tile_dx, geo.um(mi_el.get("y")) + tile_dy
        mirror, rot = geo.angle(mi_el.get("rot"))
        # DTD default is a literal "0", not a sentinel for "omitted" —
        # Eagle has no colon-prefix naming mode at all (module-instance.md),
        # only this numeric offset, so a bare 0 always means "as declared".
        offset = int(mi_el.get("offset", "0"))
        # modulevariant is dropped silently: module variants aren't
        # transferred at all yet (conversion-eagle.md #что-не-переносится),
        # the same as a <part>'s own <variant> children today.

        texts = []
        module = modules_by_name.get(mod_name)
        if mi_el.get("smashed") == "yes" and module is not None:
            declared = {
                g.content[1:].split("@")[0].upper()
                for g in module.symbol.graphics if isinstance(g, Text) and g.content.startswith(">")
            }
            for a in mi_el.findall("attribute"):
                key = a.get("name", "").upper()
                if key not in declared:
                    log(f"channel {mi_name}: dropped placeholder-position override for {a.get('name')!r} "
                        f"— module {mod_name!r} declares no such placeholder")
                    continue
                amirror, arot = geo.angle(a.get("rot"))
                align = _ALIGN_MAP.get(a.get("align", "bottom-left"), "bottom-left")
                texts.append(Text(
                    x=geo.um(a.get("x")) + tile_dx, y=geo.um(a.get("y")) + tile_dy,
                    height=geo.um(a.get("size", "1.778")), layer=geo.schematic_layer(int(a.get("layer", "95"))),
                    align=align, content=f">{key}", rot=arot, mirror=amirror,
                    ratio=int(a.get("ratio", "8")),
                ))

        modinsts.append(ModuleInstance(module=mod_name, name=mi_name, x=x, y=y, rot=rot, mirror=mirror,
                                        offset=offset, texts=texts))

    graphics = []
    plain_el = sheet_el.find("plain")
    for child in (plain_el if plain_el is not None else []):
        if child.tag == "wire":
            g = geo.convert_wire(child, geo.schematic_layer)
            if g is None:            # короче микрона — см. geo.convert_wire
                continue
        elif child.tag == "text":
            g = geo.convert_text(child, geo.schematic_layer)
        elif child.tag in ("circle", "rectangle", "polygon"):
            log(f"sheet {sheet_index + 1}: decorative <{child.tag}> on the canvas not yet "
                f"converted (conversion-eagle.md #схема) — dropped")
            continue
        else:
            continue
        graphics.append(_offset_graphic(g, tile_dx, tile_dy))

    nets: list[Net] = []
    net_of_part: dict[str, str] = {}
    nets_el = sheet_el.find("nets")
    for net_el in (nets_el if nets_el is not None else []):
        net_name = net_el.get("name")
        segments = []
        for seg_el in net_el.findall("segment"):
            lines, labels, pinrefs = [], [], []
            for child in seg_el:
                if child.tag == "wire":
                    lines.append(Line(
                        x1=geo.um(child.get("x1")) + tile_dx, y1=geo.um(child.get("y1")) + tile_dy,
                        x2=geo.um(child.get("x2")) + tile_dx, y2=geo.um(child.get("y2")) + tile_dy,
                        width=geo.um(child.get("width")), layer=None,
                    ))
                elif child.tag == "pinref":
                    pinrefs.append(PinRef(inst=child.get("part"), pin=child.get("pin"), gate=child.get("gate", "")))
                    net_of_part[child.get("part")] = net_name
                elif child.tag == "portref":
                    # pinref.md: one tag for both targets; a channel's pin
                    # ref is written with gate=None (never ""), since a
                    # channel has no sections at all to leave blank.
                    pinrefs.append(PinRef(inst=child.get("moduleinst"), pin=child.get("port"), gate=None))
                elif child.tag == "label":
                    mirror, rot = geo.angle(child.get("rot"))
                    # xref="yes" says "this label carries pin-type
                    # semantics" (the closest Eagle gets to our flag
                    # styles, since it can't draw a flag shape at all —
                    # see write_net_fragment on the export side). Which
                    # exact direction it was isn't recoverable from xref
                    # alone, so PASSIVE stands in as the generic, safe
                    # flag style rather than guessing a specific one.
                    style = LabelStyle.PASSIVE if child.get("xref") == "yes" else LabelStyle.CRUMMY
                    labels.append(Label(
                        x=geo.um(child.get("x")) + tile_dx, y=geo.um(child.get("y")) + tile_dy,
                        height=geo.um(child.get("size")), align=_LABEL_ALIGN_DEFAULT,
                        layer=geo.schematic_layer(int(child.get("layer"))), style=style,
                        rot=rot, mirror=mirror, ratio=int(child.get("ratio", "8")),
                    ))
                # <junction> carries no data of its own — dropped, not logged
                # (purely a rendering cue for an already-implied connection).
            segments.append(Segment(lines=lines, labels=labels, pinrefs=pinrefs))
        class_name = class_by_number.get(net_el.get("class"))
        attrs = [Attr("class", class_name)] if class_name else []
        nets.append(Net(name=net_name, segments=segments, attrs=attrs))

    return compinsts, modinsts, graphics, nets, net_of_part, frame_bbox, frame_part_name


def _offset_graphic(g, dx: int, dy: int):
    from dataclasses import replace
    from ir.graphics import Arc, Line, Text
    if isinstance(g, Line):
        return replace(g, x1=g.x1 + dx, y1=g.y1 + dy, x2=g.x2 + dx, y2=g.y2 + dy)
    if isinstance(g, Arc):
        return replace(g, x1=g.x1 + dx, y1=g.y1 + dy, x2=g.x2 + dx, y2=g.y2 + dy)
    if isinstance(g, Text):
        return replace(g, x=g.x + dx, y=g.y + dy)
    return g


def check_stray(sheet_index, frame_bbox, compinsts, modinsts, graphics, nets, log):
    from ir.graphics import Arc

    if frame_bbox is None:
        raise ValueError(f"sheet {sheet_index + 1}: no frame found — every sheet needs exactly one")
    x0, y0, x1, y1 = frame_bbox

    def inside(x, y) -> bool:
        return x0 <= x <= x1 and y0 <= y <= y1

    stray = []
    for ci in compinsts:
        if not inside(ci.x, ci.y):
            stray.append(f"part {ci.part!r} at ({ci.x}, {ci.y})")
    for mi in modinsts:
        if not inside(mi.x, mi.y):
            stray.append(f"channel {mi.name!r} at ({mi.x}, {mi.y})")
    for g in graphics:
        if isinstance(g, (Line, Arc)):
            pts = [(g.x1, g.y1), (g.x2, g.y2)]
        else:
            pts = [(g.x, g.y)]
        if not all(inside(x, y) for x, y in pts):
            stray.append(f"decorative graphic at {pts}")
    for net in nets:
        for seg in net.segments:
            for line in seg.lines:
                if not (inside(line.x1, line.y1) and inside(line.x2, line.y2)):
                    stray.append(f"net {net.name!r} wire at ({line.x1},{line.y1})-({line.x2},{line.y2})")
            for lbl in seg.labels:
                if not inside(lbl.x, lbl.y):
                    stray.append(f"net {net.name!r} label at ({lbl.x},{lbl.y})")
    if stray:
        raise ValueError(f"sheet {sheet_index + 1}: {len(stray)} object(s) fall outside its frame: "
                          + "; ".join(stray[:10]))


# ------------------------------------------------------------------ modules

_MODULE_BODY_BORDER = 1000  # µm — module.md gives no width for the outward
# body's drawn outline, and Eagle stores no geometry for it at all (only
# dx/dy) — same defaulting convention as library.py's synthesized frame
# shape, _FRAME_DEFAULT_BORDER.
_MODULE_NAME_MARGIN = 1000  # µm above the box's top edge for the
# synthesized ">NAME" placeholder — Eagle stores no base position for it
# either: every real <moduleinst> we've seen is smashed with an explicit
# override, because Eagle has nowhere else to keep a default one.
_MODULE_NAME_HEIGHT = 1270  # µm — matches the library importer's own
# default text height for a symbol's ">NAME".

# pin.md #одна-геометрия-на-оба-случая gives this conversion explicitly:
# side="left" coord=c -> x=-dx/2, y=c, rot=180000; side="right" coord=c ->
# x=dx/2, y=c, rot=0. top/bottom follow the same pattern on the other axis.
_PORT_SIDE_GEOMETRY = {
    "left": lambda dx, dy, coord: (-(dx // 2), coord, 180000),
    "right": lambda dx, dy, coord: (dx // 2, coord, 0),
    "top": lambda dx, dy, coord: (coord, dy // 2, 90000),
    "bottom": lambda dx, dy, coord: (coord, -(dy // 2), 270000),
}


def convert_port(port_el, dx: int, dy: int, mod_name: str, log) -> Pin:
    side = port_el.get("side")
    geometry = _PORT_SIDE_GEOMETRY.get(side)
    if geometry is None:
        raise ValueError(f"module {mod_name!r}: port {port_el.get('name')!r} has unknown side {side!r}")
    coord = geo.um(port_el.get("coord"))
    x, y, rot = geometry(dx, dy, coord)
    direction_s = port_el.get("direction", "io")
    direction = lib_conv._DIRECTION_MAP[direction_s]
    if direction_s == "nc":
        log(f"module {mod_name!r}: port {port_el.get('name')!r} declared direction=nc — "
            f"pin.md has no nc, using the most neutral direction 'pas'")
    return Pin(name=port_el.get("name"), direction=direction, x=x, y=y, rot=rot)


def convert_module_symbol(module_el, mod_name: str, dx: int, dy: int, log) -> Symbol:
    body = Shape(x=0, y=0, w=dx, h=dy, layer=94, outline=_MODULE_BODY_BORDER, roundness=0)
    log(f"module {mod_name!r}: outward body has no stored geometry in Eagle (dx x dy only) — "
        f"synthesized a {dx}x{dy} um outline on layer 94, border width defaulted")
    name_text = Text(x=0, y=dy // 2 + _MODULE_NAME_MARGIN, height=_MODULE_NAME_HEIGHT, layer=95,
                      align="bottom-center", content=">NAME")
    ports_el = module_el.find("ports")
    pins = [convert_port(p, dx, dy, mod_name, log) for p in (ports_el if ports_el is not None else [])]
    return Symbol(name=None, library=None, pins=pins, graphics=[body, name_text])


def _fallback_module_prefix(name: str, log) -> str:
    """module.md: prefix is required, no default — but Eagle's own DTD
    default for an omitted attribute is "" (ground-truthed: real projects
    do leave it empty). naming.md's usual repair — substitute `_` for a
    domain violation — doubles as the fallback here, applied to the
    module's own name rather than inventing something out of nothing."""
    bad = set(" \t\n@{}:")
    fixed = "".join("_" if ch in bad else ch for ch in name).upper()
    log(f"module {name!r}: prefix is empty in Eagle — derived {fixed!r} from the module name")
    return fixed


def convert_module(
    module_el,
    components_by_key: dict[tuple[str, str], Component],
    symbols_by_key: dict[tuple, Symbol],
    final_components_by_key: dict[tuple[str, str], Component],
    class_by_number: dict[str, str | None],
    power_components: dict[tuple[str, str], Component],
    rename: dict[tuple, tuple[str, str]],
    survivor_pin: dict[tuple, tuple[str, str]],
    uservalue_by_key: dict[tuple[str, str], bool],
    component_rename_by_urn: dict[str, dict[str, str]],
    log,
) -> Module:
    """<module> -> IR `Module` — conversion-eagle.md #схема: "иерархия
    переносится почти буквально". Eagle already keeps the same two-layer
    shape IR does (a module definition's own part/net names stay local,
    only ports bridge outward), so this is a near-literal structural copy
    of the same sheet-tiling pipeline `convert()` runs at the top level —
    just scoped to this module's own <parts>/<sheets>, on its own canvas."""
    name = module_el.get("name")
    dx, dy = geo.um(module_el.get("dx")), geo.um(module_el.get("dy"))
    prefix = module_el.get("prefix") or _fallback_module_prefix(name, log)
    symbol = convert_module_symbol(module_el, name, dx, dy, log)

    parts_el = module_el.find("parts")
    raw_parts = convert_raw_parts(
        parts_el if parts_el is not None else [], components_by_key, log, component_rename_by_urn)
    parts_by_name = {rp.name: rp for rp in raw_parts}

    sheets_el = module_el.find("sheets")
    all_compinsts, all_graphics = [], []
    nets_by_name: dict[str, Net] = {}
    net_of_part: dict[str, str] = {}
    tile_dx = 0
    tile_margin = 10_000

    for i, sheet_el in enumerate(sheets_el if sheets_el is not None else []):
        compinsts, modinsts, graphics, nets, sheet_net_of_part, frame_bbox, frame_part_name = convert_sheet(
            sheet_el, i, tile_dx, 0, parts_by_name, components_by_key, symbols_by_key, class_by_number, log)
        if modinsts:
            raise ValueError(f"module {name!r}: contains a nested module instance — "
                              "hierarchy is exactly one level deep (conversion-eagle.md)")
        check_stray(i, frame_bbox, compinsts, modinsts, graphics, nets, log)

        if frame_part_name is not None:
            desc_el = sheet_el.find("description")
            label = f"{i + 1}. {desc_el.text}" if desc_el is not None and desc_el.text else str(i + 1)
            frame_attrs = parts_by_name[frame_part_name].own_attrs
            frame_attrs[:] = [a for a in frame_attrs if a.name.lower() != "sheet"]
            frame_attrs.append(Attr("sheet", label))

        all_compinsts.extend(compinsts)
        all_graphics.extend(graphics)
        net_of_part.update(sheet_net_of_part)

        for n in nets:
            key = n.name.lower()
            existing = nets_by_name.get(key)
            if existing is None:
                nets_by_name[key] = n
                continue
            if existing.name != n.name:
                log(f"module {name!r}: net {existing.name!r} reappears as {n.name!r} on sheet {i + 1} "
                    f"(case differs) — keeping the first spelling")
            existing.segments.extend(n.segments)
            for a in n.attrs:
                if not any(ea.name.lower() == a.name.lower() for ea in existing.attrs):
                    existing.attrs.append(a)

        if frame_bbox is not None:
            tile_dx = frame_bbox[2] + tile_margin

    pin_rewrite = apply_power_rename(raw_parts, net_of_part, power_components, rename, survivor_pin)
    parts = finalize_parts(raw_parts, final_components_by_key, uservalue_by_key)

    all_nets = list(nets_by_name.values())
    apply_pin_rewrite(all_nets, pin_rewrite)

    # module.md: no <modinst> and no global attrs — Eagle's own <module>
    # has nowhere to store the latter at all (no <attributes> child per
    # eagle.dtd), so the parametric interface simply doesn't exist here.
    inner_schematic = Schematic(parts=parts, instances=all_compinsts, nets=all_nets, graphics=all_graphics)
    return Module(name=name, prefix=prefix, symbol=symbol, schematic=inner_schematic)


# --------------------------------------------------------------- top level

def convert(sch_el, log, models_dir=None, restring=None) -> tuple[Schematic, list[Symbol], list, list[Component], list[Class], list[Module]]:
    libraries_el = sch_el.find("libraries")
    symbols, footprints, components, uservalue_by_key, component_rename_by_urn = \
        merge_libraries(libraries_el, log, models_dir, restring)
    symbols_by_key = {(s.library, s.name): s for s in symbols if s.name is not None}
    components_by_key = {(c.library.lower(), c.name.lower()): c for c in components}

    classes, class_by_number = convert_classes(sch_el.find("classes"), log)

    # Phase 1 of power-symbol collapsing runs once, globally: the component
    # pool is shared by the top-level canvas and every module definition
    # alike, so SUPPLY1/SUPPLY2/... names must come out of one place, not
    # be renumbered independently per scope (which would collide).
    final_components, power_components, rename, survivor_pin = collapse_power_components(components, symbols, log)
    final_components_by_key = {(c.library.lower(), c.name.lower()): c for c in final_components}

    modules_el = sch_el.find("modules")
    modules: list[Module] = []
    modules_by_name: dict[str, Module] = {}
    for module_el in (modules_el if modules_el is not None else []):
        module = convert_module(
            module_el, components_by_key, symbols_by_key, final_components_by_key,
            class_by_number, power_components, rename, survivor_pin, uservalue_by_key,
            component_rename_by_urn, log,
        )
        modules.append(module)
        modules_by_name[module.name] = module

    parts_el = sch_el.find("parts")
    raw_parts = convert_raw_parts(parts_el, components_by_key, log, component_rename_by_urn)
    parts_by_name = {rp.name: rp for rp in raw_parts}

    sheets_el = sch_el.find("sheets")
    all_compinsts, all_modinsts, all_graphics = [], [], []
    nets_by_name: dict[str, Net] = {}
    net_of_part: dict[str, str] = {}
    tile_dx = 0
    tile_margin = 10_000  # 10 mm between tiled sheets

    for i, sheet_el in enumerate(sheets_el):
        compinsts, modinsts, graphics, nets, sheet_net_of_part, frame_bbox, frame_part_name = convert_sheet(
            sheet_el, i, tile_dx, 0, parts_by_name, components_by_key, symbols_by_key, class_by_number, log,
            modules_by_name=modules_by_name)
        check_stray(i, frame_bbox, compinsts, modinsts, graphics, nets, log)

        if frame_part_name is not None:
            desc_el = sheet_el.find("description")
            label = f"{i + 1}. {desc_el.text}" if desc_el is not None and desc_el.text else str(i + 1)
            frame_attrs = parts_by_name[frame_part_name].own_attrs
            frame_attrs[:] = [a for a in frame_attrs if a.name.lower() != "sheet"]
            frame_attrs.append(Attr("sheet", label))

        all_compinsts.extend(compinsts)
        all_modinsts.extend(modinsts)
        all_graphics.extend(graphics)
        net_of_part.update(sheet_net_of_part)

        for n in nets:
            # net.md: pieces on different sheets are SEGMENTS of one net,
            # never two nets — but Eagle stores each sheet's <nets> block
            # separately, so a multi-sheet net arrives here as several
            # same-named <net> elements that need merging back into one.
            key = n.name.lower()
            existing = nets_by_name.get(key)
            if existing is None:
                nets_by_name[key] = n
                continue
            if existing.name != n.name:
                log(f"net {existing.name!r} reappears as {n.name!r} on sheet {i + 1} "
                    f"(case differs) — keeping the first spelling")
            existing.segments.extend(n.segments)
            for a in n.attrs:
                if not any(ea.name.lower() == a.name.lower() for ea in existing.attrs):
                    existing.attrs.append(a)

        if frame_bbox is not None:
            tile_dx = frame_bbox[2] + tile_margin

    pin_rewrite = apply_power_rename(raw_parts, net_of_part, power_components, rename, survivor_pin)
    parts = finalize_parts(raw_parts, final_components_by_key, uservalue_by_key)

    attrs_el = sch_el.find("attributes")
    global_attrs = [geo.safe_attr(a.get("name"), a.get("value", ""), log)
                    for a in (attrs_el if attrs_el is not None else [])]

    all_nets = list(nets_by_name.values())
    apply_pin_rewrite(all_nets, pin_rewrite)
    schematic = Schematic(parts=parts, instances=all_compinsts, modinsts=all_modinsts, nets=all_nets,
                           graphics=all_graphics, attrs=global_attrs)
    return schematic, symbols, footprints, final_components, classes, modules
