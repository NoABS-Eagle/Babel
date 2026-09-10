"""IR `Layout` -> Eagle `<board>` (`.brd`) — conversion-eagle.md #плата (the
"В Eagle" pass). Board space reuses the same fixed layer table
schematic/footprint export already uses (geometry.py); the one thing
that's per-board is which of Eagle's 16 nominal copper positions this
stack occupies. This exporter always packs them contiguously from the top
— Eagle layer `k` for IR's own internal `k` (1..N-1), Eagle 16 for IR's
bottom (-1) — the simplest choice with nothing to invent: the IR stack
string only carries thicknesses, never which physical Eagle position they
originally sat at (ground truth shows real projects picking different
positions for the same layer count, e.g. 1+2+3+16 vs. 1+2+15+16 — a fact
lost on import, not recoverable here).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

from ir.graphics import Arc, Line, Polygon
from ir.module_instance import expand_designator
from ir.plating import Plating, PlatingArc
from ir.stack import parse_stack
from ir.via import Via

from . import geometry as geo
from . import library_export as lib_export
from .board import _CLEARANCE_KEYS

_TEMPLATE_DIR = Path(__file__).parent


def _designrules_template() -> ET.Element:
    return ET.parse(_TEMPLATE_DIR / "eagle_designrules_template.xml").getroot()


# --------------------------------------------------------------- designrules

def write_designrules(rules, mask_expansion: int, paste_expansion: int, coppers: list[int], dielectrics, log) -> ET.Element:
    dr_el = _designrules_template()

    def set_len(key: str, value_um: int) -> None:
        p = dr_el.find(f"param[@name='{key}']")
        if p is not None:
            p.set("value", f"{geo.mm(value_um)}mm")

    if rules is None:
        log("layout has no <rules> — design rules exported unchanged from Eagle's own template defaults")
    else:
        if rules.clearance is not None:
            for k in _CLEARANCE_KEYS:
                set_len(k, rules.clearance)
        if rules.edge_clearance is not None:
            set_len("mdCopperDimension", rules.edge_clearance)
        if rules.min_width is not None:
            set_len("msWidth", rules.min_width)
        if rules.min_drill is not None:
            set_len("msDrill", rules.min_drill)
        if rules.min_drill_web is not None:
            set_len("mdDrill", rules.min_drill_web)
        if rules.min_annular is not None:
            for p in dr_el.findall("param"):
                if p.get("name", "").startswith("rlMin"):
                    set_len(p.get("name"), rules.min_annular)

    # layout.md #маска-и-паста: one flat number per board — writing it into
    # BOTH min and max collapses Eagle's pad-size-dependent ratio down to
    # exactly that constant, and re-importing it back is then lossless.
    set_len("mlMinStopFrame", mask_expansion)
    set_len("mlMaxStopFrame", mask_expansion)
    set_len("mlMinCreamFrame", paste_expansion)
    set_len("mlMaxCreamFrame", paste_expansion)

    n = len(coppers)

    # **`layerSetup` is the board's own statement of its build**, and since
    # the importer now reads the stack from it (board.py's
    # `_declared_copper`), leaving the template's hard-coded `(1*16)` here
    # would quietly turn every exported four-layer board back into a
    # two-layer one. The layers are the same ones `mtCopper` below writes:
    # 1, 2, ... n-1, 16.
    #
    # `*` marks the core and `+` the prepreg. Ground truth `(1*16)` for two
    # layers and `(1+2*3+16)` / `(1+2*15+16)` for four: the core is the
    # MIDDLE gap, prepreg either side.
    layers = list(range(1, n)) + [16]
    gaps = ["+"] * (n - 1)
    if gaps:
        gaps[(n - 1) // 2 if (n - 1) % 2 else (n - 1) // 2 - 1] = "*"
    setup = "".join(f"{layers[i]}{gaps[i]}" for i in range(n - 1)) + str(layers[-1])
    setup_el = dr_el.find("param[@name='layerSetup']")
    if setup_el is not None:
        setup_el.set("value", f"({setup})")

    mt_copper_el = dr_el.find("param[@name='mtCopper']")
    if mt_copper_el is not None:
        values = mt_copper_el.get("value").split()
        for i in range(n - 1):
            values[i] = f"{geo.mm(coppers[i])}mm"
        values[15] = f"{geo.mm(coppers[n - 1])}mm"
        mt_copper_el.set("value", " ".join(values))

    # mtIsolate is read back by POSITION among active gaps, not raw Eagle
    # layer number (board.py's derive_stack) — so this board's own n-1
    # real gaps go into slots 0..n-2, sequentially, and every slot past
    # that stays the template's own unused boilerplate.
    mt_isolate_el = dr_el.find("param[@name='mtIsolate']")
    if mt_isolate_el is not None:
        iso = ["0mm"] * 15
        for i in range(n - 2):
            iso[i] = f"{geo.mm(dielectrics[i].thickness)}mm"
        if n >= 2:
            iso[n - 2] = f"{geo.mm(dielectrics[n - 2].thickness)}mm"
        mt_isolate_el.set("value", " ".join(iso))

    return dr_el


# -------------------------------------------------------------------- layers

def make_layer_reverse(copper_count: int, log):
    """IR board-space (number, side) -> Eagle layer number. Copper uses
    this exporter's own contiguous convention (see module docstring);
    everything else reuses the fixed footprint/board table."""
    def layer_reverse(layer: int) -> int:
        if layer == 1:
            return 1
        if layer == -1:
            return 16
        if 2 <= layer <= copper_count - 1:
            return layer
        return geo.footprint_layer_reverse(layer, False, log)
    return layer_reverse


# ------------------------------------------------------------------ elements

def _expanded_parts(project) -> dict[str, object]:
    """Eagle's board is always flat — no module concept at all — so an
    element from inside a module channel uses the already-expanded
    designator (module-instance.md). Replays the same offset-shift
    expansion the schematic side never needs to (Eagle's own schematic
    stays two-level; only the board flattens)."""
    modules_by_name = {m.name: m for m in project.modules}
    expanded: dict[str, object] = {}
    for mi in project.schematic.modinsts:
        module = modules_by_name.get(mi.module)
        if module is None:
            continue
        for part in module.schematic.parts:
            expanded[expand_designator(part.name, mi.name, mi.offset)] = part
    return expanded


_ATTR_DISPLAY_HEIGHT = 1778  # um — see write_element's own comment below.


def write_element(parent, el, parts_by_name, components_by_key, layer_reverse, log) -> None:
    part = parts_by_name.get(el.name)
    if part is None:
        raise ValueError(f"eagle export: element {el.name!r} has no matching schematic part, "
                          "top-level or inside a module channel")
    component = components_by_key[(part.library.lower(), part.component.lower())]
    device = next((d for d in component.devices if d.name.lower() == (part.device or "").lower()), None)
    if device is None:
        raise ValueError(f"eagle export: element {el.name!r}'s part has no device/footprint to place")

    value_attr = next((a for a in part.attrs if a.name.lower() == "value"), None)
    mirror = 1 if el.side.value == "bottom" else 0
    el_el = ET.SubElement(parent, "element", name=el.name, library=part.library, package=device.footprint,
                           value=value_attr.value if value_attr is not None else "",
                           x=geo.mm(el.x), y=geo.mm(el.y), rot=geo.eagle_rot(mirror, el.rot))

    # conversion-eagle.md #атрибуты: Eagle duplicates a part's own attrs
    # onto its board element too (real files carry MANF, MANF#, LCSC#...
    # there) — "value" is already the element's own `value=`, and "sheet"
    # is a schematic-only synthesized fact (frame.md), neither belongs
    # here a second time.
    extra_attrs = [a for a in part.attrs if a.name.lower() not in ("value", "sheet")]
    if not el.texts and not extra_attrs:
        return
    el_el.set("smashed", "yes")
    for t in el.texts:
        key = t.content[1:].split("@")[0]
        ET.SubElement(el_el, "attribute", name=key, x=geo.mm(t.x), y=geo.mm(t.y),
                      size=geo.mm(t.height), layer=str(layer_reverse(t.layer)),
                      align=geo.ALIGN_MAP_REVERSE.get(t.align, t.align),
                      rot=geo.eagle_rot(t.mirror, t.rot), font="vector")
    for a in extra_attrs:
        # No display position is stored for these in IR — but Eagle's
        # real parser (stricter than its own DTD, which marks all of
        # x/y/size/layer #IMPLIED) rejects an <attribute> missing any of
        # them even under display="off". Every real example
        # ground-truthed (MANF, MANF#, LCSC#...) sits at the element's
        # own origin/rotation on the Values layer of its own side —
        # cosmetic since it's hidden, but present.
        canon_layer = 127 if el.side.value == "top" else -127
        ET.SubElement(el_el, "attribute", name=a.name, value=a.value,
                      x=geo.mm(el.x), y=geo.mm(el.y), size=geo.mm(_ATTR_DISPLAY_HEIGHT),
                      layer=str(layer_reverse(canon_layer)), rot=geo.eagle_rot(mirror, el.rot),
                      display="off")


# ----------------------------------------------------------------------- via

def write_via(parent, v: Via, log) -> None:
    el = ET.SubElement(parent, "via", x=geo.mm(v.x), y=geo.mm(v.y), extent="1-16",
                        drill=geo.mm(v.drill), diameter=geo.mm(v.diameter))
    if v.attrs:
        log(f"via at ({v.x},{v.y}): {len(v.attrs)} tool-bookmark attr(s) have no Eagle "
            "equivalent (<via> carries no <attribute> there) — dropped")


# ------------------------------------------------------------------- plating

_PLATING_WIRE_WIDTH = 200  # um — Eagle structurally requires a wire width
# here, though plating.md deliberately keeps none: the real width lives on
# the layer-120 contour this path retraces, not on the plating itself.
# Arbitrary placeholder, and unverified against real ground truth — no
# test project uses plating at all.


def write_plating(parent, p: Plating, log) -> None:
    if p.land or (p.inner_land not in (0, None)):
        log(f"plating: land/inner_land ({p.land}/{p.inner_land}) has no Eagle equivalent "
            "(Eagle stores no strip width at all) — dropped")
    log(f"plating: exported to Eagle layer 46 (Milling) with a placeholder "
        f"{_PLATING_WIRE_WIDTH} um wire width — not stored anywhere in IR")
    for seg in p.path:
        attrs = dict(x1=geo.mm(seg.x1), y1=geo.mm(seg.y1), x2=geo.mm(seg.x2), y2=geo.mm(seg.y2),
                     width=geo.mm(_PLATING_WIRE_WIDTH), layer="46")
        if isinstance(seg, PlatingArc):
            attrs["curve"] = geo._degrees(seg.curve)
        ET.SubElement(parent, "wire", **attrs)


# -------------------------------------------------------------------- signal

def write_signal(parent, sig, layer_reverse, log) -> ET.Element:
    sig_el = ET.SubElement(parent, "signal", name=sig.name)
    for ref in sig.contactrefs:
        ET.SubElement(sig_el, "contactref", element=ref.element, pad=ref.pad)
    for item in sig.copper:
        if isinstance(item, Via):
            write_via(sig_el, item, log)
        elif isinstance(item, Plating):
            write_plating(sig_el, item, log)
        elif isinstance(item, Polygon):
            eagle_layer = layer_reverse(item.layer)
            poly_el = geo.write_polygon(item, eagle_layer, pour="cutout" if item.anti else "solid")
            if not item.anti:
                poly_el.set("rank", str(item.rank))
                poly_el.set("thermals", "yes" if item.thermals else "no")
                if item.clearance:
                    poly_el.set("isolate", geo.mm(item.clearance))
            sig_el.append(poly_el)
        elif isinstance(item, (Line, Arc)):
            sig_el.append(geo.write_wire(item, layer_reverse(item.layer)))
        else:
            raise TypeError(f"unexpected signal copper item: {item!r}")
    return sig_el


# --------------------------------------------------------------- top level

def write_classes(board_el, project, log) -> dict[str, int]:
    """The board's own <classes> numbering — entirely local to this file,
    independent of any numbering the schematic export chose for the same
    pool (class.md: class membership is a schematic net attribute; the
    board only needs to resolve width/drill/name for its own <signal>)."""
    classes_el = ET.SubElement(board_el, "classes")
    ET.SubElement(classes_el, "class", number="0", name="default", width="0", drill="0")
    class_by_name: dict[str, int] = {}
    for i, c in enumerate(project.classes, start=1):
        class_by_name[c.name] = i
        ET.SubElement(classes_el, "class", number=str(i), name=c.name,
                      width=geo.mm(c.width or 0), drill=geo.mm(c.drill or 0))
    return class_by_name


def write_board(layout, project, log) -> ET.Element:
    if any(e.exclude for e in layout.elements):
        raise ValueError(f"eagle export: layout {layout.name!r} has ghost element(s) — Eagle has no "
                          "multi-board/ghost concept (element.md); every device-bearing part must be "
                          "a real, present element on the one board Eagle has")

    coppers, dielectrics = parse_stack(layout.stack)
    layer_reverse = make_layer_reverse(len(coppers), log)

    board_el = ET.Element("board")

    # eagle.dtd order: plain, libraries, attributes, classes, designrules,
    # elements, signals — ground-truthed against all 5 test projects.
    plain_el = ET.SubElement(board_el, "plain")
    for g in layout.graphics:
        for el in lib_export.write_geometry(g, True, layer_reverse, log):
            plain_el.append(el)
    for h in layout.holes:
        ET.SubElement(plain_el, "hole", x=geo.mm(h.x), y=geo.mm(h.y), drill=geo.mm(h.drill))

    libs_el = ET.SubElement(board_el, "libraries")
    by_lib: dict[str, list] = defaultdict(list)
    for f in project.footprints:
        by_lib[f.library].append(f)
    for lib_name, fps in by_lib.items():
        lib_export.write_library(libs_el, lib_name, [], fps, [], log)

    attrs_el = ET.SubElement(board_el, "attributes")
    for a in layout.attrs:
        ET.SubElement(attrs_el, "attribute", name=a.name, value=a.value)

    class_by_name = write_classes(board_el, project, log)

    designrules_el = write_designrules(layout.rules, layout.mask_expansion, layout.paste_expansion,
                                        coppers, dielectrics, log)
    board_el.append(designrules_el)

    parts_by_name = {p.name: p for p in project.schematic.parts}
    for name, part in _expanded_parts(project).items():
        parts_by_name.setdefault(name, part)
    components_by_key = {(c.library.lower(), c.name.lower()): c for c in project.components}
    elements_el = ET.SubElement(board_el, "elements")
    for el in layout.elements:
        write_element(elements_el, el, parts_by_name, components_by_key, layer_reverse, log)

    nets_by_name = {n.name.lower(): n for n in project.schematic.nets}
    signals_el = ET.SubElement(board_el, "signals")
    for sig in layout.signals:
        sig_el = write_signal(signals_el, sig, layer_reverse, log)
        net = nets_by_name.get(sig.name.lower())
        class_attr = next((a.value for a in net.attrs if a.name.lower() == "class"), None) if net else None
        class_num = class_by_name.get(class_attr, 0) if class_attr else 0
        if class_num:
            sig_el.set("class", str(class_num))

    return board_el
