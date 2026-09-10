"""IR `Project` (schematic-only) -> Eagle `<drawing><schematic>` — the
reverse of schematic.py, conversion-eagle.md #рамки #схема (the "В Eagle"
pass). Un-tiles the one flat IR canvas back into Eagle `<sheet>`s by the
same frame geometry the importer used to tile them in the first place.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import defaultdict

from ir.graphics import Arc, Line, Polygon, Shape, Text

from . import geometry as geo
from . import library_export as lib_export

_SHEET_LABEL_RE = re.compile(r"^(\d+)\.\s*(.*)$")


def _resolve_gate_symbol(part, gate_name, components_by_key, symbols_by_key):
    key = (part.library.lower(), part.component.lower())
    component = components_by_key[key]
    gate = next((g for g in component.gates if g.name == gate_name), component.gates[0])
    return symbols_by_key.get((component.library, gate.symbol)), component


def _find_frames(schematic, symbols_by_key, components_by_key):
    """Returns [(part, compinst, frame_shape, bbox)], one per frame
    instance — frame.md: a page is a placed component whose gate's symbol
    carries a `<shape layer=98>`."""
    parts_by_name = {p.name: p for p in schematic.parts}
    frames = []
    for ci in schematic.instances:
        part = parts_by_name[ci.part]
        symbol, _component = _resolve_gate_symbol(part, ci.gate, components_by_key, symbols_by_key)
        if symbol is None or not symbol.is_frame:
            continue
        shape = geo.find_frame_shape(symbol)
        bbox = geo.bbox_from_shape(shape, ci.mirror, ci.rot, ci.x, ci.y)
        frames.append((part, ci, shape, bbox))
    return frames


def _sheet_sort_key(part):
    sheet_attr = next((a.value for a in part.attrs if a.name.lower() == "sheet"), None)
    if sheet_attr is None:
        return (1, 0, "")
    m = _SHEET_LABEL_RE.match(sheet_attr)
    if m:
        return (0, int(m.group(1)), "")
    if sheet_attr.isdigit():
        return (0, int(sheet_attr), "")
    return (0, 0, sheet_attr)


def _inside(bbox, x, y) -> bool:
    x0, y0, x1, y1 = bbox
    return x0 <= x <= x1 and y0 <= y <= y1


def _which_sheet(bboxes, x, y):
    for i, bbox in enumerate(bboxes):
        if _inside(bbox, x, y):
            return i
    return None


def build_sheets(schematic, symbols_by_key, components_by_key, log):
    """Partitions every schematic object into a list of per-sheet
    payloads: `{instances, modinsts, graphics, nets, frame_part}`, in sheet
    order. Raises on anything conversion-eagle.md's export path must
    reject: no frames at all, a stray object outside every frame, a net
    segment that straddles two frames.

    Takes a flat IR `Schematic` rather than a `Project` — the same
    function serves the top-level product canvas and every module
    definition alike (a module's own schematic never carries a <modinst>,
    so that part of the partitioning is simply a no-op there)."""
    frames = _find_frames(schematic, symbols_by_key, components_by_key)
    if not frames:
        raise ValueError("eagle export: this schematic has no frame — "
                          "conversion-eagle.md #рамки: a frameless schematic cannot leave the IR")

    frames.sort(key=lambda f: _sheet_sort_key(f[0]))
    bboxes = [f[3] for f in frames]
    frame_parts = [f[0] for f in frames]

    sheets = [{"instances": [], "modinsts": [], "graphics": [], "nets": {},
               "frame_part": frame_parts[i], "bbox": bboxes[i]}
              for i in range(len(frames))]

    stray = []

    for ci in schematic.instances:
        idx = _which_sheet(bboxes, ci.x, ci.y)
        if idx is None:
            stray.append(f"part {ci.part!r} at ({ci.x}, {ci.y})")
            continue
        sheets[idx]["instances"].append(ci)

    for mi in schematic.modinsts:
        idx = _which_sheet(bboxes, mi.x, mi.y)
        if idx is None:
            stray.append(f"channel {mi.name!r} at ({mi.x}, {mi.y})")
            continue
        sheets[idx]["modinsts"].append(mi)

    for g in schematic.graphics:
        pts = [(g.x1, g.y1), (g.x2, g.y2)] if isinstance(g, (Line, Arc)) else [(g.x, g.y)]
        idxs = {_which_sheet(bboxes, x, y) for x, y in pts}
        if None in idxs:
            stray.append(f"decorative graphic at {pts}")
            continue
        if len(idxs) > 1:
            raise ValueError(f"eagle export: decorative graphic at {pts} spans two sheets")
        sheets[idxs.pop()]["graphics"].append(g)

    ci_by_part = {ci.part: ci for ci in schematic.instances}
    mi_by_name = {mi.name: mi for mi in schematic.modinsts}
    for net in schematic.nets:
        for seg in net.segments:
            pts = [(w.x1, w.y1) for w in seg.lines] + [(w.x2, w.y2) for w in seg.lines]
            for ref in seg.pinrefs:
                if ref.gate is None:
                    inst_mi = mi_by_name.get(ref.inst)
                    if inst_mi is not None:
                        pts.append((inst_mi.x, inst_mi.y))
                else:
                    inst_ci = ci_by_part.get(ref.inst)
                    if inst_ci is not None:
                        pts.append((inst_ci.x, inst_ci.y))
            idxs = {_which_sheet(bboxes, x, y) for x, y in pts}
            if None in idxs:
                stray.append(f"segment of net {net.name!r} falls outside every frame")
                continue
            if len(idxs) > 1:
                raise ValueError(f"eagle export: a segment of net {net.name!r} spans two sheets "
                                  "— Eagle can't lay a wire between two pages")
            # Eagle allows exactly one <net name=X> per sheet — several
            # segments of the same net on the same sheet are *children* of
            # it, not siblings; a second <net name="GND"> is a
            # "redefinition" error in Eagle's own reader.
            sheet_nets = sheets[idxs.pop()]["nets"]
            if net.name not in sheet_nets:
                sheet_nets[net.name] = (net, [])
            sheet_nets[net.name][1].append(seg)

    if stray:
        raise ValueError(f"eagle export: {len(stray)} object(s) lie outside every frame "
                          "(conversion-eagle.md #рамки — бесхозные объекты): " + "; ".join(stray[:10]))

    return sheets


# --------------------------------------------------------------- writers

def _sheet_description(part):
    sheet_attr = next((a.value for a in part.attrs if a.name.lower() == "sheet"), None)
    if sheet_attr is None:
        return None
    m = _SHEET_LABEL_RE.match(sheet_attr)
    return m.group(2) if m and m.group(2) else None


def write_instance(parent, ci, ox: int, oy: int, symbols_by_key, components_by_key, part, log) -> None:
    el = ET.SubElement(parent, "instance", part=ci.part, gate=ci.gate,
                        x=geo.mm(ci.x - ox), y=geo.mm(ci.y - oy), rot=geo.eagle_rot(ci.mirror, ci.rot))
    if not ci.texts:
        return
    el.set("smashed", "yes")
    symbol, _component = _resolve_gate_symbol(part, ci.gate, components_by_key, symbols_by_key)
    for t in ci.texts:
        key = t.content[1:].split("@")[0]
        ET.SubElement(el, "attribute", name=key, x=geo.mm(t.x - ox), y=geo.mm(t.y - oy),
                      size=geo.mm(t.height), layer=str(t.layer),
                      align=geo.ALIGN_MAP_REVERSE.get(t.align, t.align),
                      rot=geo.eagle_rot(t.mirror, t.rot), font="vector")


def write_modinst(parent, mi, ox: int, oy: int, log) -> None:
    if mi.offset is None:
        raise ValueError(f"eagle export: channel {mi.name!r} has no offset — Eagle has no colon-prefix "
                          "naming (module-instance.md), only a numeric offset; assign one before export")
    if mi.variant is not None:
        log(f"channel {mi.name!r}: variant selection {mi.variant!r} has no Eagle equivalent "
            "(module variants aren't transferred yet) — dropped")
    if mi.attrs:
        log(f"channel {mi.name!r}: {len(mi.attrs)} parametric attr(s) have no Eagle equivalent "
            "(module parameters aren't representable there) — dropped")
    el = ET.SubElement(parent, "moduleinst", name=mi.name, module=mi.module,
                        x=geo.mm(mi.x - ox), y=geo.mm(mi.y - oy), offset=str(mi.offset),
                        rot=geo.eagle_rot(mi.mirror, mi.rot))
    if not mi.texts:
        return
    el.set("smashed", "yes")
    for t in mi.texts:
        key = t.content[1:].split("@")[0]
        ET.SubElement(el, "attribute", name=key, x=geo.mm(t.x - ox), y=geo.mm(t.y - oy),
                      size=geo.mm(t.height), layer=str(t.layer),
                      align=geo.ALIGN_MAP_REVERSE.get(t.align, t.align),
                      rot=geo.eagle_rot(t.mirror, t.rot), font="vector")


def _segment_junctions(seg) -> list[tuple[int, int]]:
    """Eagle stores connection dots explicitly (eagle.dtd: <junction> is a
    child of <segment>) rather than deriving them from wire geometry the
    way our own connectivity model does — a point needs one wherever 3+
    wire ends meet (a T or star), not at a plain 2-wire pass-through or a
    lone wire-to-pin connection."""
    from collections import Counter
    counts = Counter()
    for w in seg.lines:
        counts[(w.x1, w.y1)] += 1
        counts[(w.x2, w.y2)] += 1
    return [pt for pt, n in counts.items() if n >= 3]


def write_net_fragment(nets_el, net, segments, ox, oy, class_by_name, log) -> None:
    """Eagle allows exactly one `<net name=X>` per sheet — every segment of
    that net on this sheet is a `<segment>` child of it, never a sibling
    `<net>` (a second one is a "redefinition" error in Eagle's reader)."""
    net_el = ET.SubElement(nets_el, "net", name=net.name)
    class_attr = next((a.value for a in net.attrs if a.name.lower() == "class"), None)
    if class_attr is not None:
        net_el.set("class", str(class_by_name.get(class_attr, 0)))
    else:
        net_el.set("class", "0")
    for seg in segments:
        seg_el = ET.SubElement(net_el, "segment")
        for w in seg.lines:
            ET.SubElement(seg_el, "wire", x1=geo.mm(w.x1 - ox), y1=geo.mm(w.y1 - oy),
                          x2=geo.mm(w.x2 - ox), y2=geo.mm(w.y2 - oy), width=geo.mm(w.width), layer="91")
        for ref in seg.pinrefs:
            if ref.gate is None:
                # pinref.md: one tag models both targets, but Eagle keeps
                # two: a channel's pin ref is <portref>, never <pinref>.
                ET.SubElement(seg_el, "portref", moduleinst=ref.inst, port=ref.pin)
            else:
                ET.SubElement(seg_el, "pinref", part=ref.inst, gate=ref.gate or "", pin=ref.pin)
        for lbl in seg.labels:
            # Eagle's <label> has no shape/style at all (eagle.dtd) — the
            # closest it has to the crummy/flag distinction is `xref`.
            # Import always produces "crummy" (conversion-eagle.md), so
            # this asymmetry is deliberate: a non-crummy style can only
            # come from a source richer than Eagle (e.g. a future
            # KiCad/Altium import), and round-tripping it back through
            # Eagle still can't draw a flag — xref is what survives.
            xref = "no" if lbl.style.value == "crummy" else "yes"
            ET.SubElement(seg_el, "label", x=geo.mm(lbl.x - ox), y=geo.mm(lbl.y - oy), size=geo.mm(lbl.height),
                          layer=str(lbl.layer), rot=geo.eagle_rot(lbl.mirror, lbl.rot),
                          ratio=str(lbl.ratio), xref=xref)
        for jx, jy in _segment_junctions(seg):
            ET.SubElement(seg_el, "junction", x=geo.mm(jx - ox), y=geo.mm(jy - oy))


def write_part(parts_el, part, components_by_key, symbols_by_key, log) -> None:
    value_attr = next((a for a in part.attrs if a.name.lower() == "value"), None)
    device = part.device or ""
    if not device and value_attr is not None:
        # power-symbol.md: this part's component has no IR `Device` at
        # all, but write_component just gave its deviceset one real,
        # named <device> per bus value — pick the one matching this
        # part's own value instead of an empty name nothing resolves to.
        component = components_by_key.get((part.library.lower(), part.component.lower()))
        if component is not None and lib_export._is_power_symbol_component(component, symbols_by_key):
            device = value_attr.value
    el = ET.SubElement(parts_el, "part", name=part.name, library=part.library,
                        deviceset=part.component, device=device, technology="")
    if value_attr is not None:
        el.set("value", value_attr.value)
    for a in part.attrs:
        if a is value_attr:
            continue
        if a.name.lower() == "sheet":
            # Synthesized on import from sheet order + <description>
            # (frame.md) — re-derived the same way on the next import, so
            # writing it back here too would be a second, driftable home
            # for the same fact (and duplicates it outright on a
            # roundtrip, since build_sheets() adds it again).
            continue
        ET.SubElement(el, "attribute", name=a.name, value=a.value)


def _write_sheets(parent_el, schematic, symbols_by_key, components_by_key, class_by_name, log) -> None:
    """Writes <parts> and <sheets> children onto `parent_el` — either the
    top-level <schematic> or a <module> — from a flat IR `schematic`.
    Shared by write_schematic and write_module: Eagle keeps the same
    two-layer shape IR does (conversion-eagle.md #схема), so a module
    definition's own body is laid out exactly like the product canvas."""
    sheets = build_sheets(schematic, symbols_by_key, components_by_key, log)

    parts_el = ET.SubElement(parent_el, "parts")
    for p in schematic.parts:
        write_part(parts_el, p, components_by_key, symbols_by_key, log)

    parts_by_name = {p.name: p for p in schematic.parts}
    sheets_el = ET.SubElement(parent_el, "sheets")
    for sheet in sheets:
        sheet_el = ET.SubElement(sheets_el, "sheet")
        desc = _sheet_description(sheet["frame_part"])
        if desc:
            desc_el = ET.SubElement(sheet_el, "description")
            desc_el.text = desc
        ox, oy, _x1, _y1 = sheet["bbox"]

        plain_el = ET.SubElement(sheet_el, "plain")
        for g in sheet["graphics"]:
            for el in lib_export.write_geometry(_offset_graphic(g, -ox, -oy), False, geo.schematic_layer, log):
                plain_el.append(el)

        if sheet["modinsts"]:
            # eagle.dtd: <moduleinsts> precedes <instances> in <sheet>.
            modinsts_el = ET.SubElement(sheet_el, "moduleinsts")
            for mi in sheet["modinsts"]:
                write_modinst(modinsts_el, mi, ox, oy, log)

        instances_el = ET.SubElement(sheet_el, "instances")
        for ci in sheet["instances"]:
            write_instance(instances_el, ci, ox, oy, symbols_by_key, components_by_key, parts_by_name[ci.part], log)

        ET.SubElement(sheet_el, "busses")
        nets_el = ET.SubElement(sheet_el, "nets")
        for net, segs in sheet["nets"].values():
            write_net_fragment(nets_el, net, segs, ox, oy, class_by_name, log)


_SIDE_FROM_ROT = {0: "right", 90000: "top", 180000: "left", 270000: "bottom"}


def write_port(p, mod_name: str, log) -> ET.Element:
    """pin.md #одна-геометрия-на-оба-случая: "сторона восстанавливается из
    rot" — the reverse translation needs no dx/dy at all, only the pin's
    own rot (always one of the four cardinal values, enforced already by
    Pin's own orthogonal-angle validation)."""
    side = _SIDE_FROM_ROT.get(p.rot)
    if side is None:
        raise ValueError(f"module {mod_name!r}: port {p.name!r} has rot={p.rot}, not one of the "
                          "four cardinal directions Eagle's side/coord model needs")
    coord = p.y if side in ("left", "right") else p.x
    return ET.Element("port", name=p.name, side=side, coord=geo.mm(coord), direction=p.direction.value)


def _module_box(symbol, mod_name: str) -> tuple[int, int]:
    """module.md: "Размер... выводится как габарит нарисованного" — dx/dy
    are never stored, only derived from the drawn extent. Only body-shaped
    ink counts (Line/Arc/Shape/Polygon): a ">NAME" placeholder is routinely
    drawn ABOVE the body (module.md's own worked example does this), and a
    pin's lead legitimately sticks out past the edge it sits on — neither
    should inflate the box, so Text and Pin are excluded on purpose."""
    pts = []
    for g in symbol.graphics:
        if isinstance(g, (Line, Arc)):
            pts += [(g.x1, g.y1), (g.x2, g.y2)]
        elif isinstance(g, Shape):
            x0, y0, x1, y1 = geo.bbox_from_shape(g, 0, 0, 0, 0)
            pts += [(x0, y0), (x1, y1)]
        elif isinstance(g, Polygon):
            pts += [(v.x, v.y) for v in g.vertices]
    if not pts:
        raise ValueError(f"module {mod_name!r}: outward symbol has no body geometry "
                          "(Line/Arc/Shape/Polygon) to derive dx/dy from")
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return max(xs) - min(xs), max(ys) - min(ys)


def write_module(parent, module, symbols_by_key, components_by_key, class_by_name, log) -> None:
    dx, dy = _module_box(module.symbol, module.name)
    if module.symbol.graphics:
        # Eagle's <module> has no drawing surface at all — no nested
        # <symbol>, just dx/dy plus <ports> — so every graphic on the
        # outward symbol (the synthesized body, a ">NAME" placeholder,
        # anything hand-drawn) is lost, only its bounding box survives.
        log(f"module {module.name!r}: outward body ({len(module.symbol.graphics)} graphic object(s)) has no "
            f"Eagle storage at module-definition level (only dx/dy survive, from their bounding box) — dropped")
    mod_el = ET.SubElement(parent, "module", name=module.name, prefix=module.prefix,
                            dx=geo.mm(dx), dy=geo.mm(dy))
    ports_el = ET.SubElement(mod_el, "ports")
    for p in module.symbol.pins:
        ports_el.append(write_port(p, module.name, log))
    if module.schematic.attrs:
        log(f"module {module.name!r}: {len(module.schematic.attrs)} global attr(s)/parameter declaration(s) "
            f"have no Eagle equivalent (<module> carries no attribute container there) — dropped")
    _write_sheets(mod_el, module.schematic, symbols_by_key, components_by_key, class_by_name, log)


def write_schematic(project, log) -> ET.Element:
    symbols_by_key = {(s.library, s.name): s for s in project.symbols if s.name is not None}
    components_by_key = {(c.library.lower(), c.name.lower()): c for c in project.components}

    sch_el = ET.Element("schematic")

    # libraries: group project pools by their `library` field.
    libs_el = ET.SubElement(sch_el, "libraries")
    by_lib = defaultdict(lambda: ([], [], []))
    for s in project.symbols:
        by_lib[s.library][0].append(s)
    for f in project.footprints:
        by_lib[f.library][1].append(f)
    for c in project.components:
        by_lib[c.library][2].append(c)
    all_parts = list(project.schematic.parts) + [p for m in project.modules for p in m.schematic.parts]
    values_by_component = lib_export.collect_power_symbol_values(all_parts)
    for lib_name, (syms, fps, comps) in by_lib.items():
        lib_export.write_library(libs_el, lib_name, syms, fps, comps, log, values_by_component)

    attrs_el = ET.SubElement(sch_el, "attributes")
    for a in project.schematic.attrs:
        ET.SubElement(attrs_el, "attribute", name=a.name, value=a.value)

    classes_el = ET.SubElement(sch_el, "classes")
    ET.SubElement(classes_el, "class", number="0", name="default", width="0", drill="0")
    class_by_name: dict[str, int] = {}
    for i, c in enumerate(project.classes, start=1):
        class_by_name[c.name] = i
        ET.SubElement(classes_el, "class", number=str(i), name=c.name,
                      width=geo.mm(c.width or 0), drill=geo.mm(c.drill or 0))
        if c.attrs:
            # A real Eagle <class> is a leaf element — no <attribute>
            # children anywhere in the 5 ground-truth projects.
            log(f"class {c.name!r}: {len(c.attrs)} attr(s) have no Eagle equivalent "
                f"(<class> carries no children there) — dropped")

    if project.modules:
        # eagle.dtd: <modules> precedes <parts>/<sheets> in <schematic>.
        modules_el = ET.SubElement(sch_el, "modules")
        for m in project.modules:
            write_module(modules_el, m, symbols_by_key, components_by_key, class_by_name, log)

    _write_sheets(sch_el, project.schematic, symbols_by_key, components_by_key, class_by_name, log)
    return sch_el


def _offset_graphic(g, dx: int, dy: int):
    from dataclasses import replace
    if isinstance(g, (Line, Arc)):
        return replace(g, x1=g.x1 + dx, y1=g.y1 + dy, x2=g.x2 + dx, y2=g.y2 + dy)
    if isinstance(g, Text):
        return replace(g, x=g.x + dx, y=g.y + dy)
    return g
