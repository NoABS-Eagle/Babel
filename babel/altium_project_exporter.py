"""IR .swprj -> Altium project: .PrjPcb + .SchDoc page(s) + .SchLib + .PcbDoc.

HIERARCHY (2026-07-29): an IR <module> becomes ONE child .SchDoc plus a sheet
symbol per instance on the parent, ports matched to sheet entries by name, the
whole set listed in the .PrjPcb. A document referenced by several sheet
symbols IS Altium's multi-channel form: the child sheet carries the module's
canonical designators (C1, R1, ...) and the physical name of a part in a
channel is computed from the project's ChannelDesignatorFormatString —
`$Component_$RoomName` -> `C1_DCDC1` (altium_exporter.channel_designator).
Eagle's own flattening (`offset`: C1 @ 100 -> C101) is arithmetic no Altium
template can express, so on this path the channel naming is Altium's; the
board is written with exactly the same names, or every part would read as
changed on the first ECO. Room name = the IR instance name (`DCDC1`), both
user decisions of 2026-07-29.

Mapping decisions (mirrors of altium_project_parser where one exists):
- rot/mirror: import derives `rot = orientation*90, mirror = is_mirrored`,
  so export is the exact inverse.
- FRAME instances -> sheet size only (custom size, Altium draws its own
  border+zones); the frame's graphics/placeholders are dropped and logged —
  inverse of import's size-only frame synthesis.
- Supply instances -> native Altium PowerPort (user decision, better than
  Altium's own Eagle import). Style: the ONE sanctioned heuristic — a net
  name containing "GND" or "0V" gets BAR, everything else ARROW.
- Net naming is geometric per island in Altium (same discipline as KiCad):
  every island of a named net must carry a NetLabel or a PowerPort. IR
  labels map 1:1; label-less islands of named nets get a synthesized
  NetLabel at a safe anchor (ported from kicad_project_exporter).
"""
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from altium_monkey import AltiumSchLib, AltiumPrjPcbBuilder
from altium_monkey.altium_prjpcb import NetIdentifierScope
from altium_monkey.altium_schdoc import AltiumSchDoc, CoordPoint
from altium_monkey.altium_record_sch__wire import AltiumSchWire
from altium_monkey.altium_record_sch__junction import AltiumSchJunction
from altium_monkey.altium_record_sch__net_label import AltiumSchNetLabel
from altium_monkey.altium_record_sch__power_port import AltiumSchPowerPort
from altium_monkey.altium_record_sch__label import AltiumSchLabel
from altium_monkey.altium_record_sch__parameter import AltiumSchParameter
from altium_monkey.altium_record_sch__polyline import AltiumSchPolyline
from altium_monkey.altium_record_sch__text_frame import AltiumSchTextFrame
from altium_monkey.altium_record_sch__port import AltiumSchPort
from altium_monkey.altium_record_sch__sheet_symbol import AltiumSchSheetSymbol
from altium_monkey.altium_record_sch__sheet_entry import AltiumSchSheetEntry
from altium_monkey.altium_record_sch__sheet_name import AltiumSchSheetName
from altium_monkey.altium_record_sch__file_name import AltiumSchFileName
from altium_monkey.altium_sch_enums import (PortIOType,
                                            PortStyle, PowerObjectStyle,
                                            SchHorizontalAlign,
                                            TextJustification, TextOrientation)
from altium_monkey.altium_symbol_transform import generate_unique_id

from babel import import_log
from babel import altium_exporter
from babel import altium_board_exporter
from babel.altium_exporter import (_mils, _lw, _justif, _orient, _font_pt,
                                   CHANNEL_DESIGNATOR_FORMAT,
                                   channel_designator, multiline_box)
from babel.ir_util import (component_gates, designator_resolver,
                           instance_footprint, is_multi_gate, resolved_attrs,
                           rotate_port_side, sanitize_filename)
from babel.kicad_project_exporter import _collinear_between


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _eagle_overbar_to_altium(text):
    """IR/Eagle `!TEXT!` overbar toggle -> Altium backslash-per-char notation
    (inverse of altium_parser._altium_overbar_to_eagle: a backslash AFTER a
    character overlines that ONE character)."""
    out = []
    overlined = False
    for ch in text:
        if ch == '!':
            overlined = not overlined
            continue
        out.append(ch)
        if overlined:
            out.append('\\')
    return ''.join(out)


def _power_style(net_name):
    """The ONE sanctioned heuristic (user decision 2026-07-27): GND-family
    names (AGND, GNDD, PGND, ... and the "0V" spelling seen in the wild)
    render as BAR, every other supply as ARROW."""
    u = (net_name or '').upper()
    if 'GND' in u or '0V' in u:
        return PowerObjectStyle.BAR
    return PowerObjectStyle.ARROW


def _sup_pin_names(comp_el, pool):
    """Bare names of `sup`-direction pins across the component's gates
    (same rule as kicad_project_exporter._sup_pin_names)."""
    names = set()
    for _, sym_name in component_gates(comp_el):
        sym_el = pool.get(sym_name)
        if sym_el is None:
            continue
        for p in sym_el.findall('pin'):
            if p.get('direction') == 'sup':
                names.add(p.get('name'))
    return names


def _inst_point(inst_el, lx_um, ly_um):
    """Symbol-local µm point -> canvas µm under the instance's rot/mirror
    (mirror local X, rotate, translate — the IR order, see
    altium_project_parser._altium_field_overrides)."""
    rot = float(inst_el.get('rot', '0'))
    mirror = inst_el.get('mirror') == '1'
    lx = -float(lx_um) if mirror else float(lx_um)
    ly = float(ly_um)
    r = math.radians(rot)
    x = lx * math.cos(r) - ly * math.sin(r)
    y = lx * math.sin(r) + ly * math.cos(r)
    return (float(inst_el.get('x', '0')) + x,
            float(inst_el.get('y', '0')) + y)


def _font(doc, size_um):
    """IR text size (µm) -> font id on THIS doc (same scale as the library
    exporter, so a symbol's text keeps its size once placed)."""
    return doc.font_manager.get_or_create_font(
        'Times New Roman', _font_pt(size_um or 1270))


# ---------------------------------------------------------------------------
# Page model: one FRAME instance -> one .SchDoc
# ---------------------------------------------------------------------------

def _is_frame(comp_el, pool):
    for _, sym_name in component_gates(comp_el):
        sym_el = pool.get(sym_name)
        if sym_el is not None and any(s.get('layer') == 'FRAME'
                                      for s in sym_el.findall('shape')):
            return True
    return False


def _frame_bbox(inst_el, comp_el, pool):
    """Absolute µm (x1, x2, y1, y2) of a frame instance's FRAME shape.
    Frame instances are synthesized rot=0/mirror=0 on every import path
    (ir_schema.md "Frame") — pure translation."""
    sym_name = component_gates(comp_el)[0][1]
    shape = next(s for s in pool[sym_name].findall('shape')
                 if s.get('layer') == 'FRAME')
    ix, iy = float(inst_el.get('x')), float(inst_el.get('y'))
    cx, cy = ix + float(shape.get('x')), iy + float(shape.get('y'))
    w, h = float(shape.get('w')), float(shape.get('h'))
    return cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2


class _Page:
    """One output .SchDoc: coordinate origin at the frame's bottom-left
    corner (Altium schematic coords are mils, Y-up, origin bottom-left —
    same handedness as the IR canvas, so the transform is pure offset)."""

    def __init__(self, name, fname, x1_um, y1_um, w_um, h_um):
        self.name = name
        self.fname = fname
        self.x1 = x1_um
        self.y1 = y1_um
        self.w = w_um
        self.h = h_um
        self.doc = AltiumSchDoc()
        sheet = self.doc.sheet
        sheet.use_custom_sheet = True
        # custom_x/custom_y are in internal 10-mil units
        sheet.custom_x = round(w_um / 25.4 / 10)
        sheet.custom_y = round(h_um / 25.4 / 10)
        # ~2.5in per reference zone, the A-series ballpark
        sheet.custom_x_zones = max(1, round(w_um / 25.4 / 2500))
        sheet.custom_y_zones = max(1, round(h_um / 25.4 / 2500))

    def pt(self, x_um, y_um):
        """IR canvas µm -> page mils (no Y flip — both are Y-up)."""
        return (_mils(float(x_um) - self.x1), _mils(float(y_um) - self.y1))

    def contains(self, x_um, y_um):
        return (self.x1 <= float(x_um) <= self.x1 + self.w
                and self.y1 <= float(y_um) <= self.y1 + self.h)


def _page_for_point(pages, x_um, y_um, label):
    hits = [p for p in pages if p.contains(x_um, y_um)]
    if len(hits) != 1:
        raise ValueError(
            f'{label}: inside {len(hits)} page frame(s), need exactly one — '
            f'every schematic object must sit on exactly one page.')
    return hits[0]


# ---------------------------------------------------------------------------
# Placed components
# ---------------------------------------------------------------------------

def _part_number(comp_el, inst_el):
    """The DbLib key (xlsx "Part Number" column) for this instance — the row
    altium_exporter wrote for the footprint this instance is placed as.

    `<instance footprint=>` holds either the footprint NAME or its VARIANT
    string (ir_schema.md "Component instance"; the variant is the identity
    when one package backs several devices), so the lookup goes through
    ir_util.instance_footprint rather than matching names here.
    """
    fp_els = comp_el.findall('footprint')
    if len(fp_els) > 1:
        want = inst_el.get('footprint')
        keys = {f.get(k) for f in fp_els for k in ('name', 'variant')} - {None}
        if want not in keys:
            raise ValueError(
                f'instance {inst_el.get("name")}: component '
                f'"{comp_el.get("name")}" has {len(fp_els)} footprints, '
                f'instance footprint={want!r} matches neither a name nor a '
                f'variant of them — cannot pick a DbLib row')
    return altium_exporter.part_number(comp_el,
                                       instance_footprint(comp_el, inst_el))


def _link_to_database(comp, part_num, table_name, schlib_name):
    """Point a placed component at its DbLib row instead of baking a library.

    The parameter values still live in the sheet (Altium caches them there),
    but the DB link is what makes "Update from Libraries" authoritative — the
    xlsx row is the single home of the fact, per the DbLib decision.
    """
    comp.database_table_name = table_name
    comp.use_db_table_name = True
    comp.design_item_id = part_num
    comp.source_library_name = schlib_name
    # add_component_from_library() has to be handed a path it can READ, and
    # stores that path verbatim — which bakes OUR working directory into the
    # sheet ("outputs\altium_step4\step4.SchLib"). The library ships next to
    # the project, so the bare name is both correct and portable.
    comp.library_path = schlib_name


def _xlsx_columns(xlsx_path):
    """Header row of the DbLib table — the names the database actually owns."""
    import openpyxl
    ws = openpyxl.load_workbook(xlsx_path, read_only=True).active
    return {c.value for c in next(ws.iter_rows(max_row=1)) if c.value}


def _set_parameter(page, comp, name, text, db_owned):
    """Add or update one parameter on a placed component.

    The SchLib entry only carries placeholders, so an IR attribute with no
    matching placeholder used to be dropped silently — every attribute is
    written here instead, and marked DB-synchronizable.
    """
    # Case-insensitive: SchLib placeholders inherit Eagle's uppercase spelling
    # (PACKAGE) while the IR attribute is lowercase (package) — matching
    # exactly would leave both, i.e. one fact in two places.
    for obj in _component_children(page.doc, comp):
        if (type(obj).__name__ == 'AltiumSchParameter'
                and obj.name.lower() == name.lower()):
            # Adopt the IR spelling: the xlsx has exactly one column, so a
            # stray placeholder spelling would never match on DB sync.
            obj.name = name
            obj.text = text
            obj.allow_database_synchronize = db_owned
            return obj
    p = AltiumSchParameter()
    p.owner_index = page.doc.all_objects.index(comp)
    p.owner_part_id = comp.current_part_id
    p.name = name
    p.text = text
    p.location = comp.location
    p.font_id = _font(page.doc, 1778)
    p.is_hidden = True
    p.allow_database_synchronize = db_owned
    p.unique_id = generate_unique_id()
    page.doc.add_object(p)
    return p


def _place_instance(page, inst_el, comp_el, pool, schlib_path, part_id, value,
                    attrs, table_name, db_cols, pin_maps, ir_root,
                    designator=None):
    """One IR <instance> -> placed AltiumSchComponent (geometry cloned from
    the SchLib entry by altium_monkey's own insert helper).

    `designator` overrides the instance's own name — a part inside a module
    is written under its flattened global spelling (ir_util.flat_designator),
    the same one the board uses."""
    x, y = page.pt(inst_el.get('x', '0'), inst_el.get('y', '0'))
    rot = round(float(inst_el.get('rot', '0'))) % 360
    entry = altium_exporter.lib_ref(
        comp_el.get('name') if is_multi_gate(comp_el)
        else component_gates(comp_el)[0][1])
    comp = page.doc.add_component_from_library(
        schlib_path, entry,
        designator=designator or inst_el.get('name', '?'),
        x=x, y=y,
        orientation=(rot // 90) % 4,
        is_mirrored=inst_el.get('mirror') == '1',
        part_id=part_id,
    )
    _link_to_database(comp, _part_number(comp_el, inst_el), table_name,
                      Path(schlib_path).name)
    # The footprint THIS instance is placed as — its own, not the component's
    # first: <instance footprint=> is what picks both the DbLib row and the
    # pad map. The MAP_DEFINERs of the model are written later, in one pass
    # per sheet (see altium_exporter.write_pin_maps).
    fp_el = instance_footprint(comp_el, inst_el)
    impl = altium_exporter.bake_footprint(
        comp, altium_exporter.fp_name(fp_el),
        Path(schlib_path).with_suffix('.PcbLib').name)
    pin_maps.append((impl, altium_exporter.pin_pad_pairs(comp_el, fp_el,
                                                         ir_root)))

    # Comment carries the instance's resolved value; every other resolved
    # attribute becomes a parameter whether or not the SchLib had a
    # placeholder for it. 'value'/'description' have their own homes.
    for obj in _component_children(page.doc, comp):
        if type(obj).__name__ == 'AltiumSchParameter' and obj.name == 'Comment':
            obj.text = value or ''
            # NOT database-synchronized, unlike every other column-backed
            # parameter: the value is the INSTANCE's (2.2u), while the row is
            # shared by every part with that number and its Comment column
            # carries the component's own name (`C`). One "Update From
            # Database" with the flag set and every capacitor on the sheet
            # reads `C`.
            obj.allow_database_synchronize = False
    # An attribute the xlsx has no column for is instance-level (e.g. board
    # attrs merged onto the instance): it must NOT be DB-synchronized, or
    # Altium wipes it on the next "Update from Database".
    for name, text in attrs.items():
        if name.lower() in ('value', 'description'):
            continue
        _set_parameter(page, comp, name, text, name in db_cols)

    # Last, so a parameter renamed to the IR spelling above is still found.
    _place_child_texts(page, comp, inst_el, pool[component_gates(comp_el)
                                                [part_id - 1][1]])
    return comp



_MIRROR_JUSTIFICATION = {
    TextJustification.BOTTOM_LEFT:  TextJustification.BOTTOM_RIGHT,
    TextJustification.BOTTOM_RIGHT: TextJustification.BOTTOM_LEFT,
    TextJustification.CENTER_LEFT:  TextJustification.CENTER_RIGHT,
    TextJustification.CENTER_RIGHT: TextJustification.CENTER_LEFT,
    TextJustification.TOP_LEFT:     TextJustification.TOP_RIGHT,
    TextJustification.TOP_RIGHT:    TextJustification.TOP_LEFT,
}


# Vertical half of a justification -> centre, horizontal half kept
_CENTER_VERT_JUSTIFICATION = {
    TextJustification.BOTTOM_LEFT:   TextJustification.CENTER_LEFT,
    TextJustification.BOTTOM_CENTER: TextJustification.CENTER_CENTER,
    TextJustification.BOTTOM_RIGHT:  TextJustification.CENTER_RIGHT,
    TextJustification.CENTER_LEFT:   TextJustification.CENTER_LEFT,
    TextJustification.CENTER_CENTER: TextJustification.CENTER_CENTER,
    TextJustification.CENTER_RIGHT:  TextJustification.CENTER_RIGHT,
    TextJustification.TOP_LEFT:      TextJustification.CENTER_LEFT,
    TextJustification.TOP_CENTER:    TextJustification.CENTER_CENTER,
    TextJustification.TOP_RIGHT:     TextJustification.CENTER_RIGHT,
}


# Both axes flipped — a half turn about the anchor
_OPPOSITE_JUSTIFICATION = {
    TextJustification.BOTTOM_LEFT:   TextJustification.TOP_RIGHT,
    TextJustification.BOTTOM_CENTER: TextJustification.TOP_CENTER,
    TextJustification.BOTTOM_RIGHT:  TextJustification.TOP_LEFT,
    TextJustification.CENTER_LEFT:   TextJustification.CENTER_RIGHT,
    TextJustification.CENTER_RIGHT:  TextJustification.CENTER_LEFT,
    TextJustification.TOP_LEFT:      TextJustification.BOTTOM_RIGHT,
    TextJustification.TOP_CENTER:    TextJustification.BOTTOM_CENTER,
    TextJustification.TOP_RIGHT:     TextJustification.BOTTOM_LEFT,
}


def _make_readable(obj):
    """Altium honours a text's angle literally, so 180 deg renders upside
    down. Turn such a text back to 0 deg and flip BOTH sides of its
    justification, which keeps the anchor — and thus the layout — put.
    A centred axis has no side to swap."""
    if getattr(obj, 'orientation', None) != TextOrientation.DEGREES_180:
        return
    obj.orientation = TextOrientation.DEGREES_0
    j = getattr(obj, 'justification', None)
    if j in _OPPOSITE_JUSTIFICATION:
        obj.justification = _OPPOSITE_JUSTIFICATION[j]


def _text_target(page, comp, content):
    """The record a symbol placeholder addresses: >NAME/>PART the designator,
    >VALUE the Comment parameter, >SOMETHING the like-named parameter."""
    kids = _component_children(page.doc, comp)
    if content in ('>NAME', '>PART'):
        return next((o for o in kids
                     if type(o).__name__ == 'AltiumSchDesignator'), None)
    name = 'Comment' if content == '>VALUE' else content[1:]
    return next((o for o in kids
                 if type(o).__name__ == 'AltiumSchParameter'
                 and o.name.lower() == name.lower()), None)


def _place_child_texts(page, comp, inst_el, sym_el):
    """Position every child text of a placed component from its IR
    placeholder: the symbol's by default, the instance's where it overrides.

    Both carry SYMBOL-LOCAL coordinates, so each is transformed by the
    instance's rot/mirror. Deriving the position here also sidesteps
    add_component_from_library, which resolves parameters to absolute canvas
    coordinates but leaves the designator in symbol-local space."""
    placeholders = {}
    for src in (sym_el, inst_el):
        for t in src.findall('text'):
            content = (t.text or '').strip()
            if content.startswith('>'):
                placeholders[content] = t

    for content, t in placeholders.items():
        target = _text_target(page, comp, content)
        if target is None:
            continue
        # A suppressed placeholder: Eagle's display="off", or — on a smashed
        # instance — a library placeholder with no <attribute> record at all,
        # which Eagle simply does not draw. Either way it carries no geometry
        # worth applying, only the fact that it must not show.
        if t.get('hidden') == 'yes':
            target.is_hidden = True
            continue
        cx, cy = _inst_point(inst_el, t.get('x', '0'), t.get('y', '0'))
        target.location = CoordPoint.from_mils(*page.pt(cx, cy))
        # The library's font_id is an index into the LIBRARY's font table;
        # cloning it into a SchDoc silently reinterprets it against this
        # document's table (which is why designators came out at its default
        # 10pt). Resolve the size from the IR text instead.
        target.font_id = _font(page.doc, t.get('size'))
        # The text's own angle from IR, turned by the instance's rotation.
        target.orientation = _orient(float(t.get('rot', '0'))
                                     + float(inst_el.get('rot', '0')))
        # Source justification, with the horizontal side swapped on a
        # mirrored instance (user decision 2026-07-28).
        just = _justif(t.get('align', 'bottom-left'))
        if inst_el.get('mirror') == '1':
            just = _MIRROR_JUSTIFICATION.get(just, just)
            # A mirrored part on its side needs both sides swapped as well
            if round(float(inst_el.get('rot', '0'))) % 360 in (90, 270):
                just = _OPPOSITE_JUSTIFICATION.get(just, just)
        target.justification = just
        if hasattr(target, 'auto_position'):
            target.auto_position = False
        _make_readable(target)


def _component_children(doc, comp):
    idx = doc.all_objects.index(comp)
    return [o for o in doc.all_objects
            if getattr(o, 'owner_index', None) == idx]


# ---------------------------------------------------------------------------
# Supply -> PowerPort
# ---------------------------------------------------------------------------

def _place_power_port(page, inst_el, comp_el, pool, net_name):
    """Supply instance -> native PowerPort at the sup pin's connect point."""
    sym_el = pool[component_gates(comp_el)[0][1]]
    pin = next((p for p in sym_el.findall('pin')
                if p.get('direction') == 'sup'), None)
    px, py = (0.0, 0.0)
    if pin is not None:
        px, py = float(pin.get('x', '0')), float(pin.get('y', '0'))
    cx, cy = _inst_point(inst_el, px, py)
    x, y = page.pt(cx, cy)
    pp = AltiumSchPowerPort()
    pp.location = CoordPoint.from_mils(x, y)
    pp.text = _eagle_overbar_to_altium(net_name)
    pp.style = _power_style(net_name)
    pp.show_net_name = True
    # Altium draws the port body pointing away from its connect point, while
    # IR's rot is the symbol's own rotation: GND-family bars sit 270 deg off,
    # every other supply 90 deg off (checked visually on tolmach).
    bump = 270 if pp.style is PowerObjectStyle.BAR else 90
    steps = round(float(inst_el.get('rot', '0'))) // 90 + bump // 90
    pp.orientation = TextOrientation(steps % 4)
    pp.font_id = _font(page.doc, 1778)
    pp.unique_id = generate_unique_id()
    page.doc.add_object(pp)
    return pp


# ---------------------------------------------------------------------------
# Nets: wires + junctions + net labels (per-island naming, KiCad discipline)
# ---------------------------------------------------------------------------

def _emit_wire(page, w_el):
    wire = AltiumSchWire()
    x1, y1 = page.pt(w_el.get('x1'), w_el.get('y1'))
    x2, y2 = page.pt(w_el.get('x2'), w_el.get('y2'))
    wire.add_point(x1, y1)
    wire.add_point(x2, y2)
    wire.line_width = _lw(float(w_el.get('width', '152')))
    wire._has_line_width = True
    page.doc.add_object(wire)


def _emit_junction(page, j_el):
    j = AltiumSchJunction()
    x, y = page.pt(j_el.get('x'), j_el.get('y'))
    j.location = CoordPoint.from_mils(x, y)
    page.doc.add_object(j)


def _emit_net_label(page, net_name, x_um, y_um, rot, size_um, style='crummy'):
    nl = AltiumSchNetLabel()
    x, y = page.pt(x_um, y_um)
    nl.location = CoordPoint.from_mils(x, y)
    nl.text = _eagle_overbar_to_altium(net_name)
    nl.orientation = TextOrientation((round(float(rot or 0)) // 90) % 4)
    nl.font_id = _font(page.doc, size_um)
    # A flag label (anything but `crummy`) sits ON the wire end, so centring
    # it vertically puts the text where the source shows it; a plain wire
    # caption keeps its baseline (user decision 2026-07-28).
    if style != 'crummy':
        nl.justification = _CENTER_VERT_JUSTIFICATION.get(
            getattr(nl, 'justification', TextJustification.BOTTOM_LEFT),
            TextJustification.CENTER_LEFT)
    nl.unique_id = generate_unique_id()
    _make_readable(nl)
    page.doc.add_object(nl)


def _on_own_wire(seg_el, label_el):
    """Does this label sit on a wire of its OWN island? (Endpoint or a point
    along a segment — Altium attaches a net label geometrically.)"""
    pt = (int(float(label_el.get('x', '0'))), int(float(label_el.get('y', '0'))))
    for a, b in _wires_um(seg_el):
        if pt == a or pt == b or _collinear_between(a, b, pt):
            return True
    return False


def _wires_um(seg_el):
    return [((int(float(w.get('x1'))), int(float(w.get('y1')))),
             (int(float(w.get('x2'))), int(float(w.get('y2')))))
            for w in seg_el.findall('line')]


def _segment_page(pages, seg_el, part_page, label):
    """The page of a segment: from its first wire endpoint, else from a
    placed part it references. None = empty segment (logged by caller)."""
    for w in seg_el.findall('line'):
        return _page_for_point(pages, w.get('x1'), w.get('y1'), label)
    for r in seg_el.findall('pinref'):
        p = part_page.get(r.get('part'))
        if p is not None:
            return p
    return None


def _label_anchor(net_name, segments, pages, part_page, other_wires):
    """A SAFE point on this net's own wires for a synthesized NetLabel —
    first endpoint/midpoint that does NOT lie on any OTHER net's wire
    (ported verbatim in spirit from kicad_project_exporter._label_anchor:
    Altium net labels attach geometrically exactly like KiCad's, an unsafe
    anchor silently merges nets)."""
    fallback = None
    for seg_el in segments:
        wires = _wires_um(seg_el)
        if not wires:
            continue
        page = _segment_page(pages, seg_el, part_page, net_name)
        if page is None:
            continue
        for a, b in wires:
            mid = ((a[0] + b[0]) // 2, (a[1] + b[1]) // 2)
            for pt in (a, b, mid):
                if fallback is None:
                    fallback = (page, pt, seg_el)
                if not any(_collinear_between(wa, wb, pt)
                           for wa, wb in other_wires):
                    return page, pt[0], pt[1], seg_el
    if fallback is not None:
        import_log.log(net_name, '', 'LABEL_ANCHOR every point of the net '
                        'touches another net\'s wire — label may merge nets, '
                        'check in Altium')
        return fallback[0], fallback[1][0], fallback[1][1], fallback[2]
    return None


def _emit_nets(pages, sch_el, part_page, supply_desigs, supply_net_by_desig,
               port_dirs=None):
    """All <net> content onto pages. supply_desigs: designators placed as
    PowerPorts (their pinrefs name their island); supply_net_by_desig is
    filled by the caller BEFORE this runs (power ports need net names).

    port_dirs (module canvases only): {net name -> IR port direction} for the
    nets that reach the module's boundary — their labels become Ports, which
    is what a net label IS on an Altium child sheet: the name AND the match
    to the parent's sheet entry."""
    port_dirs = port_dirs or {}
    wires_by_net = {n.get('name'): [w for s in n.findall('segment')
                                    for w in _wires_um(s)]
                    for n in sch_el.findall('net')}

    for net_el in sch_el.findall('net'):
        net_name = net_el.get('name')
        segments = net_el.findall('segment')
        named_segs = set()
        port_done = False

        seg_pages = {}
        for seg_el in segments:
            page = _segment_page(pages, seg_el, part_page, net_name)
            if page is None:
                import_log.log(net_name, '', 'NET_SEGMENT has no geometry '
                                'and no placed refs, skipped')
                continue
            seg_pages[id(seg_el)] = page
            for w in seg_el.findall('line'):
                _emit_wire(page, w)
            for j in seg_el.findall('junction'):
                _emit_junction(page, j)
            for l in seg_el.findall('label'):
                named_segs.add(id(seg_el))
                if not _on_own_wire(seg_el, l):
                    # Eagle binds a label to its net BY RECORD, Altium (like
                    # KiCad) only GEOMETRICALLY — a label drawn beside its
                    # wire names nothing there, and the island silently
                    # becomes its own net. Re-anchor onto the island's own
                    # wire, the same safe point an unlabeled island gets.
                    anchor = _label_anchor(net_name, [seg_el], pages,
                                           part_page, [])
                    if anchor is not None:
                        _, ax, ay, _s = anchor
                        l = ET.Element('label', {'x': str(ax), 'y': str(ay),
                                                 'rot': l.get('rot', '0'),
                                                 'size': l.get('size', '1270'),
                                                 'style': l.get('style',
                                                                'crummy')})
                        import_log.log(net_name, '', 'LABEL sat off its own '
                                        'wire (legal in Eagle, meaningless in '
                                        'Altium) — moved onto the wire')
                if net_name in port_dirs:
                    _emit_port(page, net_name, port_dirs[net_name],
                               l.get('x'), l.get('y'), l.get('rot', '0'),
                               l.get('size', '1270'))
                    port_done = True
                else:
                    _emit_net_label(page, net_name, l.get('x'), l.get('y'),
                                    l.get('rot', '0'), l.get('size', '1270'),
                                    l.get('style', 'crummy'))
            # a PowerPort names (and globally joins) ITS island
            if any(r.get('part') in supply_desigs
                   for r in seg_el.findall('pinref')):
                named_segs.add(id(seg_el))

        other = [w for nm, ws in wires_by_net.items()
                 if nm != net_name for w in ws]

        # A net that leaves the module needs its Port even when every island
        # is already named from the inside (a supply net is named by its own
        # power symbols): without one, the parent's sheet entry has nothing
        # to match and the connection the IR HAS disappears from Altium's
        # netlist. One Port is enough — the other islands keep their names.
        if net_name in port_dirs and not port_done:
            anchor = _label_anchor(net_name, segments, pages, part_page, other)
            if anchor is None:
                import_log.log(net_name, '', 'MODULE_PORT net has no wire to '
                                'anchor a Port on — the sheet entry stays '
                                'unmatched in Altium')
            else:
                page, ax, ay, _seg = anchor
                _emit_port(page, net_name, port_dirs[net_name], ax, ay, '0',
                           '1270')
                import_log.log(net_name, '', 'PORT synthesized on a net named '
                                'from the inside (power symbols), the module '
                                'declares it as a boundary port')

        # Island label synthesis for user-named nets (N$ autonames stay
        # anonymous — Altium will autoname disconnected pieces itself,
        # which is exactly what an autoname means).
        if not re.fullmatch(r'N\$\d+', net_name or ''):
            for seg_el in segments:
                if id(seg_el) in named_segs:
                    continue
                anchor = _label_anchor(net_name, [seg_el], pages, part_page,
                                       other)
                if anchor is None:
                    import_log.log(net_name, '', 'NET island has no wire to '
                                    'anchor a label, name will be lost in '
                                    'Altium')
                    continue
                page, ax, ay, _seg = anchor
                if net_name in port_dirs:
                    _emit_port(page, net_name, port_dirs[net_name],
                               ax, ay, '0', '1270')
                else:
                    _emit_net_label(page, net_name, ax, ay, '0', '1270')
                import_log.log(net_name, '', 'NET_LABEL synthesized on an '
                                'unlabeled island (Altium net naming is '
                                'geometric per island)')


# ---------------------------------------------------------------------------
# Hierarchy: sheet symbol + entries on the parent, ports on the child
#
# Ground truth for every number here: BC2087 (testData/altium) — sheet symbol
# `location` is the TOP-LEFT corner and `x_size`/`y_size` are in 10-mil units;
# an entry's own place is `distance_from_top` FROM THAT CORNER (the netlist
# compiler derives an entry point as `location.y - distance_from_top*10`);
# SheetName carries the `U_`-prefixed room name that the board's
# SOURCEHIERARCHICALPATH repeats.
# ---------------------------------------------------------------------------

_ENTRY_SIDE = {'left': 0, 'right': 1, 'top': 2, 'bottom': 3}

# IR <port direction> -> Altium IOType (same enum for sheet entry and port:
# 0=Unspecified 1=Output 2=Input 3=Bidirectional). `pwr`/`pas` have no
# Altium counterpart — Unspecified is the honest one, not a guess dressed up
# as a direction.
_PORT_IO = {'in': 2, 'out': 1, 'io': 3, 'pwr': 0, 'pas': 0, 'nc': 0}


def _room_name(minst_el):
    """Altium's room/channel name for a module instance — the IR instance
    name verbatim.

    Altium's own convention prefixes a sheet symbol's name with `U_` (26 of
    26 in the corpus), but that prefix carries no information the IR does not
    already have, and it turns every channel designator into `C1_U_DCDC1`.
    User decision 2026-07-29: the room is `DCDC1`. The board repeats it in
    SOURCEHIERARCHICALPATH."""
    return minst_el.get('name')


def _emit_sheet_symbol(page, minst_el, mod_el, child_fname):
    """IR <instance module=…> -> Altium sheet symbol with its entries."""
    rot = round(float(minst_el.get('rot', '0'))) % 360
    mirror = minst_el.get('mirror') == '1'
    dx, dy = float(mod_el.get('dx')), float(mod_el.get('dy'))
    if rot in (90, 270):
        dx, dy = dy, dx      # the box turns with the block, the frame stays
    cx, cy = float(minst_el.get('x', '0')), float(minst_el.get('y', '0'))
    left, top = page.pt(cx - dx / 2, cy + dy / 2)     # IR x/y = the CENTRE

    ss = AltiumSchSheetSymbol()
    ss.location = CoordPoint.from_mils(left, top)
    ss.x_size = round(_mils(dx) / 10)
    ss.y_size = round(_mils(dy) / 10)
    # Outline only — a filled block hides whatever it is drawn over and reads
    # as a solid slab on the sheet (user decision 2026-07-29).
    ss.is_solid = False
    ss._has_is_solid = True
    ss.unique_id = generate_unique_id()
    page.doc.add_object(ss)

    # Both labels go OUTSIDE the block, centred on it: the sheet name above
    # the top edge, the file name below the bottom one (user decision
    # 2026-07-29). The corpus default — both stacked at the bottom-left
    # corner — lands them inside the block, on top of the entry names.
    bottom = top - _mils(dy)
    centre = left + _mils(dx) / 2
    name = AltiumSchSheetName()
    name.text = _room_name(minst_el)
    name.location = CoordPoint.from_mils(centre, top)
    name.orientation = TextOrientation.DEGREES_0
    name.justification = TextJustification.BOTTOM_CENTER
    name.font_id = _font(page.doc, 1270)
    ss.set_sheet_name(name)
    fn = AltiumSchFileName()
    fn.text = child_fname
    fn.location = CoordPoint.from_mils(centre, bottom)
    fn.orientation = TextOrientation.DEGREES_0
    fn.justification = TextJustification.TOP_CENTER
    fn.font_id = _font(page.doc, 1270)
    ss.set_file_name(fn)

    for port_el in mod_el.findall('port'):
        # rot/mirror of the instance is BAKED into the entry's side+coord —
        # an Altium sheet symbol is always axis-aligned (same discipline as
        # the KiCad exporter, same shared transform).
        side, coord = rotate_port_side(port_el.get('side'),
                                       float(port_el.get('coord', '0')),
                                       rot, mirror)
        e = AltiumSchSheetEntry()
        e.name = _eagle_overbar_to_altium(port_el.get('name', ''))
        e.side = _ENTRY_SIDE[side]
        # coord is signed FROM THE CENTRE of that edge (+up on left/right,
        # +right on top/bottom); Altium counts from the top/left corner.
        along = (dy / 2 - coord) if side in ('left', 'right') else (dx / 2 + coord)
        e.distance_from_top_mils = _mils(along)
        e.io_type = _PORT_IO.get(port_el.get('direction', 'io'), 0)
        e.text_font_id = _font(page.doc, 1270)
        ss.add_entry(e)     # attaching a child registers it on the doc itself
    return ss


def _check_ports_present(page, port_dirs, minst_name):
    """Every sheet entry on the parent needs a like-named Port on the child,
    or Altium's compiler reports an unmatched entry and the connection the IR
    HAS is silently absent from the netlist."""
    got = {p.name for p in page.doc.ports}
    for name in port_dirs:
        if _eagle_overbar_to_altium(name) not in got:
            import_log.log(minst_name, name,
                           'MODULE_PORT has no net of that name on the '
                           'module canvas — the sheet entry stays unmatched '
                           'in Altium')


def _emit_port(page, name, direction, x_um, y_um, rot, size_um):
    """IR module-canvas net label -> Altium Port on the child sheet (the port
    IS the label there: it names the net and matches the parent's entry)."""
    p = AltiumSchPort()
    p.name = _eagle_overbar_to_altium(name)
    p.io_type = PortIOType(_PORT_IO.get(direction, 0))
    p.font_id = _font(page.doc, size_um)
    p.height_mils = 100
    # Altium's port body runs from `location` along +X (or +Y when vertical)
    # and BOTH ends connect; the label's own angle in the IR says which way
    # the flag pointed, so the body grows away from the wire, never over it.
    steps = (round(float(rot or 0)) // 90) % 4
    width = max(200, 80 + 60 * len(p.name))
    p.width_mils = int(round(width / 10.0) * 10)
    x, y = page.pt(x_um, y_um)
    if steps in (0, 2):
        p.style = PortStyle.NONE_HORIZONTAL
        # a horizontal port spans location.x .. location.x + width and BOTH
        # ends connect, so a leftward flag just starts a width earlier
        if steps == 2:
            x -= p.width_mils
    else:
        # A VERTICAL port keeps `location` on the wire: its far end is at
        # location.y + width, which no consumer derives from a horizontal
        # width field (altium_monkey's own connection points are the
        # horizontal pair) — a shifted-down port left C8.1 of every maximus
        # channel hanging off its own VSS net. The body may then overlap the
        # wire it names; connectivity comes first.
        p.style = PortStyle.NONE_VERTICAL
    p.location = CoordPoint.from_mils(x, y)
    p.alignment = SchHorizontalAlign.CENTER
    p.auto_size = False
    # A port record has no "solid" flag, so the fill is made WHITE — the body
    # still reads as an outline over a white sheet instead of a coloured slab
    # (user decision 2026-07-29).
    p.area_color = 0xFFFFFF
    p.unique_id = generate_unique_id()
    page.doc.add_object(p)
    return p


# ---------------------------------------------------------------------------
# Decorative schematic geometry
# ---------------------------------------------------------------------------

_FRAME_ALIGN = {'left': 0, 'center': 1, 'right': 2}


def _emit_text_frame(page, el):
    """A MULTI-LINE <text> -> Altium Text Frame, not a Label.

    A Label draws one line: a newline inside it comes out as a stray glyph.
    Altium's multi-line object is the Text Frame, a box the lines live in
    (the same split the board makes between a String and a String with
    `is_frame`). The box is sized from the text itself and NOT clipped —
    a metric that guesses a little short must not cut the text off.
    """
    lines = (el.text or '').strip().splitlines()
    size = float(el.get('size', '1270'))
    w, h = multiline_box(lines, _mils(size))
    align = (el.get('align') or 'bottom-left').lower()
    vert, _, horiz = align.partition('-')
    horiz = horiz or 'center'
    x, y = page.pt(el.get('x', '0'), el.get('y', '0'))
    # the IR anchor addresses the WHOLE block, exactly as it does one line
    x0 = x - w if horiz == 'right' else (x - w / 2 if horiz == 'center' else x)
    y0 = y - h if vert == 'top' else (y - h / 2 if vert == 'center' else y)

    tf = AltiumSchTextFrame()
    tf.text = '\n'.join(lines)          # the record escapes it as `~1` itself
    tf.location = CoordPoint.from_mils(x0, y0)
    tf.corner = CoordPoint.from_mils(x0 + w, y0 + h)
    tf.alignment = _FRAME_ALIGN.get(horiz, 0)
    tf.word_wrap = False                # the IR already says where lines break
    tf.clip_to_rect = False
    tf.show_border = False
    tf.is_solid = False
    tf.orientation = _orient(float(el.get('rot', 0) or 0))
    tf.font_id = _font(page.doc, size)
    tf.unique_id = generate_unique_id()
    page.doc.add_object(tf)


def _emit_decorations(pages, sch_el):
    for el in sch_el:
        if el.tag == 'line':
            page = _page_for_point(pages, el.get('x1'), el.get('y1'),
                                   'decorative line')
            pl = AltiumSchPolyline()
            pl.vertices = [
                CoordPoint.from_mils(*page.pt(el.get('x1'), el.get('y1'))),
                CoordPoint.from_mils(*page.pt(el.get('x2'), el.get('y2'))),
            ]
            pl.line_width = _lw(float(el.get('width', '152')))
            pl._has_line_width = True
            page.doc.add_object(pl)
        elif el.tag == 'text' and '\n' in (el.text or ''):
            page = _page_for_point(pages, el.get('x', '0'), el.get('y', '0'),
                                   'decorative text')
            _emit_text_frame(page, el)
        elif el.tag == 'text':
            page = _page_for_point(pages, el.get('x', '0'), el.get('y', '0'),
                                   'decorative text')
            lab = AltiumSchLabel()
            lab.location = CoordPoint.from_mils(
                *page.pt(el.get('x', '0'), el.get('y', '0')))
            lab.text = _eagle_overbar_to_altium((el.text or '').strip())
            lab.orientation = _orient(float(el.get('rot', '0')))
            lab.justification = _justif(el.get('align', 'bottom-left'))
            lab.font_id = _font(page.doc, el.get('size', '1270'))
            lab.unique_id = generate_unique_id()
            _make_readable(lab)
            page.doc.add_object(lab)
        elif el.tag in ('arc', 'shape', 'polygon'):
            import_log.log('schematic', el.tag,
                           'DECORATIVE geometry type not exported yet (v1)')


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _canvas_pages(canvas_el, comp_by_name, pool, name_of_page, what,
                  merge=False):
    """The FRAME instances of one canvas -> one _Page each (a module canvas
    carries its own synthesized frame, exactly like a top-level page).

    merge: all frames become ONE sheet spanning them. A canvas is ONE canvas
    in the IR — its pages are the source tool's pagination, and its nets are
    shared BY NAME across them. Altium has no name sharing between sheets
    under a hierarchical scope, so a paginated canvas that also carries
    modules is emitted as the single sheet it actually is.

    Returns (pages, ids of the frame instances)."""
    frames = []
    for inst_el in canvas_el.findall('instance'):
        comp_el = comp_by_name.get(inst_el.get('component', ''))
        if comp_el is not None and _is_frame(comp_el, pool):
            frames.append((inst_el, comp_el))
    if not frames:
        raise ValueError(f'{what}: no FRAME instance — every import path '
                         'synthesizes one, cannot derive sheet size')
    boxes = [_frame_bbox(i, c, pool) for i, c in frames]
    if merge and len(boxes) > 1:
        x1 = min(b[0] for b in boxes)
        x2 = max(b[1] for b in boxes)
        y1 = min(b[2] for b in boxes)
        y2 = max(b[3] for b in boxes)
        boxes = [(x1, x2, y1, y2)]
        import_log.log(what, '', f'{len(frames)} source pages -> ONE Altium '
                        'sheet: their nets are shared by NAME, and a project '
                        'with modules is compiled with a hierarchical scope, '
                        'where a net label stops at the sheet edge')
    pages = []
    for i, (x1, x2, y1, y2) in enumerate(boxes):
        inst_el = frames[i][0] if i < len(frames) else frames[0][0]
        pages.append(_Page(inst_el.get('name', f'FRAME{i + 1}'),
                           name_of_page(i), x1, y1, x2 - x1, y2 - y1))
        import_log.log(inst_el.get('name', ''), '',
                       'FRAME -> sheet size only, frame graphics dropped '
                       '(Altium draws its own border)')
    return pages, {id(i) for i, _ in frames}


def _pad_alias(ir_root, comp_by_name):
    """{element address: {pad: the pin designator the SCHEMATIC calls it by}}.

    A netlist speaks pins, a board speaks pads, and the two spellings differ
    for two independent reasons: a pin can own several pads (a crystal's
    shield), and the SchLib symbol takes its pin designators from the
    component's FIRST footprint while the instance may be placed as another
    one (CON5 of step4 is an XT30 whose `+` is pad `+`, but the symbol was
    built from a JST whose `+` is pad `2`). Folding the board's pads onto the
    schematic's pin designators is what lets the two halves be compared at
    all.
    """
    resolve = designator_resolver(ir_root)
    out = {}
    for el in ir_root.find('layout').findall('element'):
        addr = el.get('name')
        inst = resolve(addr)[0]
        comp_el = comp_by_name.get(inst.get('component', '')) if inst else None
        if comp_el is None:
            continue
        fp_el = instance_footprint(comp_el, inst)
        if fp_el is None:
            continue
        # Exactly the pairs the MAP_DEFINER of this instance is written with,
        # so the comparison cannot drift away from what was exported.
        alias = {}
        for desig, pads in altium_exporter.pin_pad_pairs(comp_el, fp_el,
                                                         ir_root):
            for pad in pads:
                if pad != desig:
                    alias[pad] = desig
        if alias:
            out[addr] = alias
    return out


def _compiled_net_names(prj_path, layout_el, desig_of, pad_alias):
    """{IR signal name -> the name Altium's compiler gives that same net}.

    Inside a channel Altium names nets its own way — `SW` in a room becomes
    `SW_DCDC1`, an unnamed one becomes `NetC1_DCDC1_2` — so a board written
    with the IR's `DCDC1:SW` disagrees with the schematic it came from, and
    the first "Update PCB" is a wall of net renames. We do NOT reproduce the
    formula: we compile the project we just wrote and ask. The match is by
    PIN SET, the one thing both sides state independently, so a change in
    Altium's naming can never quietly rename the wrong net.
    """
    from babel.altium_netlist import compiled_nets
    if layout_el is None:
        return {}
    nets = compiled_nets(prj_path)[0]
    # A name the compiler hands to SEVERAL nets is no name at all for our
    # purpose: maximus is eight ISOLATED channels whose grounds the compiler
    # calls plain `GND` eight times, and renaming the board's TM1:GND … TM8:GND
    # to it would weld the isolation shut — a short circuit, written by us,
    # that no verifier downstream could tell from an intended net. Such names
    # are refused and the IR spelling stands.
    seen = {}
    for net_name, _ in nets:
        seen[net_name] = seen.get(net_name, 0) + 1
    by_pins = {}
    for net_name, net_pins in nets:
        by_pins.setdefault(frozenset(net_pins), net_name)
    out, unmatched, ambiguous = {}, [], []
    for sig in layout_el.findall('signal'):
        name = sig.get('name')
        key = frozenset(
            (desig_of.get(r.get('element'), r.get('element')),
             pad_alias.get(r.get('element'), {}).get(r.get('pad'),
                                                     r.get('pad')))
            for r in sig.findall('contactref'))
        if not key:
            # Bare copper, no pad to name it by — nothing to match against
            # and nothing Altium would rename.
            continue
        got = by_pins.get(key)
        if got is None:
            # The board says these pads are one net and the compiled
            # schematic does not — either the two really disagree, or the
            # pads are spelled differently on the two sides. Never silent:
            # this is the only place the two halves are compared at all.
            unmatched.append(name)
            continue
        if got != name:
            if seen[got] > 1:
                ambiguous.append(name)
            else:
                out[name] = got
    # Belt and braces: whatever the matching decided, no two board nets may
    # end up sharing a name.
    written = [out.get(s.get('name'), s.get('name'))
               for s in layout_el.findall('signal')]
    if len(set(written)) != len(written):
        dupes = sorted({n for n in written if written.count(n) > 1})
        raise ValueError(f'net renaming would merge {len(dupes)} name(s) that '
                         f'are separate nets on the board: {dupes[:6]}')
    if ambiguous:
        import_log.log(', '.join(sorted(ambiguous)[:6]), '',
                        f'{len(ambiguous)} board net(s) keep their IR name: '
                        'the compiler gives that net a name it also gives to '
                        'others (isolated per-channel grounds and supplies), '
                        'and adopting it would weld them into one net')
    if unmatched:
        import_log.log(', '.join(sorted(unmatched)[:6]), '',
                        f'{len(unmatched)} board net(s) have no compiled net '
                        'with the same pads — written under the IR name, so '
                        '"Update PCB" will report them as changed')
    return out


def _export_canvas(canvas_el, pages, frame_ids, ctx, port_dirs=None,
                   desig_of=None):
    """Everything a canvas puts on its own sheets: components, power ports,
    nets, decorations. One body for the top level and for a module — the
    difference is only WHICH designator each part is written under
    (`desig_of`, the flattened module spelling) and whether a named net
    leaves through a Port (`port_dirs`).

    Returns {written designator: (unique id, lib ref, library)} for the ECO
    link the board needs, plus the (components, power ports) counts."""
    desig_of = desig_of or (lambda n: n)
    pool, comp_by_name = ctx['pool'], ctx['comp_by_name']

    # supply net lookup: designator -> net name (from pinrefs)
    supply_net_by_desig = {}
    for net_el in canvas_el.findall('net'):
        for seg_el in net_el.findall('segment'):
            for r in seg_el.findall('pinref'):
                supply_net_by_desig.setdefault(r.get('part'),
                                               net_el.get('name'))

    part_page = {}
    sch_link = {}
    supply_desigs = set()
    n_parts = n_power = 0
    for inst_el in canvas_el.findall('instance'):
        if id(inst_el) in frame_ids or inst_el.get('module'):
            continue
        comp_el = comp_by_name.get(inst_el.get('component', ''))
        if comp_el is None:
            raise ValueError(f'instance {inst_el.get("name")}: component '
                             f'"{inst_el.get("component")}" not in pool')
        desig = inst_el.get('name', '?')
        page = _page_for_point(pages, inst_el.get('x', '0'),
                               inst_el.get('y', '0'), f'instance {desig}')
        part_page[desig] = page

        sup_names = _sup_pin_names(comp_el, pool)
        if sup_names:
            net_name = supply_net_by_desig.get(desig)
            if net_name is None:
                # floating supply port: still name it by its own sup pin
                net_name = next(iter(sup_names))
                import_log.log(desig, '', 'SUPPLY instance not on any net, '
                                'PowerPort placed with its pin name')
            _place_power_port(page, inst_el, comp_el, pool, net_name)
            supply_desigs.add(desig)
            n_power += 1
            continue

        attrs = resolved_attrs(comp_el, inst_el)
        value = attrs.get('value', '')
        part_id = 1
        if is_multi_gate(comp_el):
            gate_names = [g for g, _ in component_gates(comp_el)]
            g = inst_el.get('gate')
            if g not in gate_names:
                raise ValueError(f'instance {desig}: gate "{g}" not in '
                                 f'component "{comp_el.get("name")}"')
            part_id = gate_names.index(g) + 1
        comp = _place_instance(page, inst_el, comp_el, pool,
                               ctx['schlib_path'], part_id, value, attrs,
                               ctx['table_name'], ctx['db_cols'],
                               ctx['pin_maps'].setdefault(id(page.doc), []),
                               ctx['ir_root'], designator=desig_of(desig))
        # What ties the board's components to THESE schematic ones. The
        # identity is the DESIGN ITEM ID (the DbLib part number, `C_C0603`),
        # not the SchLib entry the symbol was drawn from (`C`): Altium
        # compares Design Item IDs on ECO, and sending the entry name made it
        # offer to "Change Component Design Item ID" for every part on the
        # board — 70 of the 121 lines of the user's first real ECO.
        sch_link[desig_of(desig)] = (comp.unique_id, comp.design_item_id,
                                     comp.source_library_name)
        n_parts += 1

    _emit_nets(pages, canvas_el, part_page, supply_desigs,
               supply_net_by_desig, port_dirs)
    _emit_decorations(pages, canvas_el)
    return sch_link, n_parts, n_power


def export_project(swprj_path, output_dir):
    swprj_path = Path(swprj_path)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ir_root = ET.parse(swprj_path).getroot()
    proj_name = ir_root.get('name', swprj_path.stem)

    pool = {s.get('name'): s for s in ir_root.findall('symbols/symbol')}
    comp_by_name = {c.get('name'): c for c in ir_root.findall('component')}
    sch_el = ir_root.find('schematic')
    if sch_el is None:
        raise ValueError(f'{swprj_path}: no <schematic> — nothing to export')
    module_by_name = {m.get('name'): m for m in ir_root.findall('module')}
    minsts = [i for i in sch_el.findall('instance') if i.get('module')]

    # 1. Libraries: the DbLib set (SchLib + PcbLib + xlsx + DbLib), written by
    # the library exporter itself. The project links components to xlsx rows —
    # it never bakes a fat library of its own (user decision: DBLib target).
    altium_exporter.export(swprj_path, out_dir)
    ctx = {
        'pool': pool,
        'comp_by_name': comp_by_name,
        'schlib_path': out_dir / f'{proj_name}.SchLib',
        'table_name': 'Components',
        'db_cols': _xlsx_columns(out_dir / f'{proj_name}.xlsx'),
        # sheet doc -> [(footprint model, pin/pad pairs)], flushed before save
        'pin_maps': {},
        # a pool symbol's pin designators come from the FIRST component that
        # uses it, which may not be the one being placed
        'ir_root': ir_root,
    }

    # 2. Top-level pages from FRAME instances
    pages, frame_ids = _canvas_pages(
        sch_el, comp_by_name, pool,
        lambda i: f'{proj_name}.SchDoc' if i == 0
        else f'{proj_name}_{i + 1}.SchDoc', str(swprj_path),
        merge=bool(minsts))

    # 3. Top level
    sch_link, n_parts, n_power = _export_canvas(sch_el, pages, frame_ids, ctx)

    # 4. Hierarchy: ONE child .SchDoc per MODULE, referenced by a sheet
    # symbol per instance — Altium's native multi-channel form. The child
    # sheet carries the module's own canonical designators (C1, R1, …) and
    # Altium derives the physical ones per channel from the project's
    # ChannelDesignatorFormatString (altium_exporter.channel_designator); the
    # board must and does spell them the same way.
    child_pages = []                       # one page per MODULE
    child_link = {}                        # module -> {canonical: sch link}
    child_fname = {}                       # module -> its .SchDoc file name
    hier = {}                              # instance -> (room, ss uid, module)
    used_fnames = {p.fname.lower() for p in pages}
    for mod_name, mod_el in module_by_name.items():
        if not any(i.get('module') == mod_name for i in minsts):
            import_log.log(mod_name, '', 'MODULE defined but never placed, '
                            'no child sheet written')
            continue
        # A module may share its name with the project (NAMUR does) — the
        # child sheet would then overwrite the top-level one.
        stem = sanitize_filename(mod_name)
        fname = f'{stem}.SchDoc'
        n = 1
        while fname.lower() in used_fnames:
            n += 1
            fname = f'{stem}_{n}.SchDoc'
            import_log.log(mod_name, '', f'MODULE sheet name taken, written '
                            f'as {fname}')
        used_fnames.add(fname.lower())
        child_fname[mod_name] = fname
        cpages, cframe_ids = _canvas_pages(
            mod_el, comp_by_name, pool, lambda i: fname, f'module {mod_name}')
        if len(cpages) != 1:
            raise ValueError(f'module {mod_name}: {len(cpages)} FRAME '
                             'instances — a module is ONE sheet')
        port_dirs = {p.get('name'): p.get('direction', 'io')
                     for p in mod_el.findall('port')}
        link, np_, npw = _export_canvas(mod_el, cpages, cframe_ids, ctx,
                                        port_dirs=port_dirs)
        _check_ports_present(cpages[0], port_dirs, mod_name)
        child_link[mod_name] = link
        n_parts += np_
        n_power += npw
        child_pages.append(cpages[0])

    for minst_el in minsts:
        mod_el = module_by_name.get(minst_el.get('module'))
        if mod_el is None:
            raise ValueError(f'instance {minst_el.get("name")}: no <module> '
                             f'named "{minst_el.get("module")}"')
        mod_name = mod_el.get('name')
        room = _room_name(minst_el)
        if room in hier:
            raise ValueError(f'two module instances are both named "{room}" '
                             '— the room name IS the channel identity')
        page = _page_for_point(pages, minst_el.get('x', '0'),
                               minst_el.get('y', '0'),
                               f'module instance {room}')
        ss = _emit_sheet_symbol(page, minst_el, mod_el, child_fname[mod_name])
        hier[room] = (room, ss.unique_id, mod_name)

    # 5. What the board must call each part, and what it links to. A part on
    # a channel is addressed `<room>:<canonical>` in the IR layout; its name
    # in Altium is the channel format, and its identity is the PATH from the
    # top (the component's own uid is shared by every channel of the module —
    # the sheet symbol's uid is what separates them).
    desig_of = {}
    ss_uid_of = {}
    for room, (_room, ss_uid, mod_name) in hier.items():
        ss_uid_of[room] = ss_uid
        for canonical, link in child_link.get(mod_name, {}).items():
            address = f'{room}:{canonical}'
            desig_of[address] = channel_designator(room, canonical)
            sch_link[address] = link
    clash = sorted(set(desig_of.values()) & set(sch_link) - set(desig_of))
    if clash:
        raise ValueError(f'channel designator(s) {clash[:5]} collide with a '
                         'top-level part — the two would be one component in '
                         'Altium')

    # 6. Save SchDocs + PrjPcb
    all_pages = pages + child_pages
    for page in all_pages:
        altium_exporter.write_pin_maps(page.doc,
                                       ctx['pin_maps'].get(id(page.doc), ()))
        page.doc.save(out_dir / page.fname)
        print(f'Written: {out_dir / page.fname}')
    # 7. Project file — written BEFORE the board, because the board needs the
    # compiler's own net names and the compiler is driven by this file.
    builder = AltiumPrjPcbBuilder(proj_name)
    for page in all_pages:
        builder.add_schdoc(page.fname)
    if minsts:
        # STRICT hierarchical is the IR's own module model, one for one: what
        # crosses a module boundary is exactly what the module declares as a
        # <port>, and nothing else — net labels AND power ports stay local to
        # their sheet. Ground truth that this is the real requirement, not
        # tidiness: maximus is an 8-channel isolated thermometer, and Eagle's
        # own board calls the channel grounds TM1:GND … TM8:GND — eight nets,
        # separate from the top-level GND. A non-strict scope would weld all
        # nine into one and short the isolation. (29 of the 51 corpus
        # projects ship mode 4 as well.) Without modules nothing crosses a
        # sheet boundary by port, so the flat project keeps Altium's default.
        builder.set_net_identifier_scope(NetIdentifierScope.STRICT_HIERARCHICAL)
        # A room is GEOMETRY, and the IR has none to give: no source format
        # in the corpus has the concept. A bounding box drawn round each
        # channel's parts would be invented, so we invent nothing — user
        # decision 2026-07-30.
        import_log.log(proj_name, '', 'ROOMS, COMPONENT CLASSES and the '
                        'CHANNEL CLASS are created by Altium on the first '
                        'ECO. It places every room as a 2000x1000 mil '
                        'rectangle at (0,0), so EVERY component lands outside '
                        'its own room and the board lights up with Room '
                        'Confinement violations. There is nothing in the '
                        'source to draw a room from, so: either draw them '
                        'yourself around each channel, or delete them (Design '
                        '-> Rules -> Placement -> Room Definition)')
        # Not a defect of the export and not in the files at all: a shared
        # child sheet has several physical components behind one logical
        # designator, so Altium prints the whole list next to it in grey and
        # the sheet reads as a mess. It is a USER-level preference, so we
        # cannot set it from the project — we can only say where it lives.
        import_log.log(proj_name, '', 'MULTI-CHANNEL designators may show a '
                        'grey "(R4_DCDC1, ...)" list over the shared sheet. '
                        'That is Altium drawing the compiled names, nothing '
                        'is wrong with the schematic. To switch it off: '
                        'Preferences -> Schematic - Compiler -> Compiled '
                        'Names Expansion -> Designators -> Never display '
                        'superscript (same for Net Labels)')
    has_board = bool(ir_root.findall('layout'))
    if has_board:
        # A component's parameters live in ONE place — the DbLib row the
        # schematic points at — and Altium's own model treats the copy on the
        # PCB component as derived, refreshing it from the schematic on every
        # ECO. Writing that copy ourselves would be a second home for the same
        # fact, and one Altium overwrites with an identical value anyway. Same
        # for the Supply Nets rules, which it builds from the schematic's
        # power nets. So the first ECO legitimately has work to do; it is not
        # a disagreement between the two documents.
        import_log.log(proj_name, '', 'COMPONENT PARAMETERS and Supply Nets '
                        'rules are not copied onto the .PcbDoc — the '
                        'schematic (and behind it the DbLib row) is their one '
                        'home, and Altium pushes them on the first ECO')
    if has_board:
        builder.add_pcbdoc(f'{proj_name}.PcbDoc')
    # The library set is a SET: SchLib alone left the project with symbols
    # and no footprints listed, and with no route to the xlsx the components
    # actually resolve their parameters through.
    builder.add_schlib(ctx['schlib_path'].name)
    builder.add_pcblib(f'{proj_name}.PcbLib')
    builder.add_document(f'{proj_name}.DbLib')
    prj_path = out_dir / f'{proj_name}.PrjPcb'
    prj = builder.build()
    # The channel naming is a PROJECT setting: Altium's compiler reads it
    # from here, and so does altium_monkey's — which is what lets the
    # verifier judge the very designators Altium will produce.
    prj.config.set('Design', 'ChannelDesignatorFormatString',
                   CHANNEL_DESIGNATOR_FORMAT)
    prj.save(prj_path)
    print(f'Written: {prj_path}')

    # 8. Board, named the way the compiler names things
    pcb_path = None
    if has_board:
        pcb_path = altium_board_exporter.export_board(
            ir_root, out_dir, proj_name, out_dir / f'{proj_name}.PcbLib',
            sch_link, hier=ss_uid_of, desig_of=desig_of,
            net_names=_compiled_net_names(
                prj_path, ir_root.find('layout'), desig_of,
                _pad_alias(ir_root, comp_by_name)))

    print(f'Total: {len(all_pages)} sheet(s) ({len(child_pages)} module '
          f'sheet(s) for {len(minsts)} channel(s)), {n_parts} components, '
          f'{n_power} power ports')
    import_log.write(out_dir / f'{proj_name}.PrjPcb')


if __name__ == '__main__':
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else 'outputs/tolmach.swprj'
    dst = sys.argv[2] if len(sys.argv) > 2 else 'outputs/altium_out'
    export_project(src, dst)
