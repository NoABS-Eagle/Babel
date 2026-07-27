"""IR .swprj -> Altium project: .PrjPcb + .SchDoc page(s) + .SchLib.

FLAT v1 (user decision 2026-07-27): <module> in the project is a hard
reject — hierarchy (sheet symbols + child SchDoc + ports) comes later.
The board (.PcbDoc) is a separate later stage as well.

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
from altium_monkey.altium_schdoc import AltiumSchDoc, CoordPoint
from altium_monkey.altium_record_sch__wire import AltiumSchWire
from altium_monkey.altium_record_sch__junction import AltiumSchJunction
from altium_monkey.altium_record_sch__net_label import AltiumSchNetLabel
from altium_monkey.altium_record_sch__power_port import AltiumSchPowerPort
from altium_monkey.altium_record_sch__label import AltiumSchLabel
from altium_monkey.altium_record_sch__polyline import AltiumSchPolyline
from altium_monkey.altium_sch_enums import PowerObjectStyle, TextOrientation
from altium_monkey.altium_symbol_transform import generate_unique_id

from babel import import_log
from babel.altium_exporter import (
    _export_schlib_symbol, _export_schlib_multipart, _mils, _lw, _justif,
    _orient,
)
from babel.ir_util import (component_gates, is_multi_gate, resolved_attrs)
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
    """IR text size (µm) -> font id on THIS doc (same 150 µm/pt empirical
    scale as altium_exporter._font_id)."""
    pt = max(1, round(float(size_um or 1270) / 150))
    return doc.font_manager.get_or_create_font('Times New Roman', pt)


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

def _place_instance(page, inst_el, comp_el, schlib_path, part_id, value,
                    attrs):
    """One IR <instance> -> placed AltiumSchComponent (geometry cloned from
    the SchLib entry by altium_monkey's own insert helper)."""
    x, y = page.pt(inst_el.get('x', '0'), inst_el.get('y', '0'))
    rot = round(float(inst_el.get('rot', '0'))) % 360
    entry = (comp_el.get('name') if is_multi_gate(comp_el)
             else component_gates(comp_el)[0][1])
    comp = page.doc.add_component_from_library(
        schlib_path, entry,
        designator=inst_el.get('name', '?'),
        x=x, y=y,
        orientation=(rot // 90) % 4,
        is_mirrored=inst_el.get('mirror') == '1',
        part_id=part_id,
    )
    # Fill parameter values: Comment carries the instance's resolved value;
    # any other cloned parameter whose name matches a resolved attr gets
    # that attr's value (clone leaves the SchLib placeholder literal).
    for obj in _component_children(page.doc, comp):
        if type(obj).__name__ == 'AltiumSchParameter':
            if obj.name == 'Comment':
                obj.text = value or ''
            elif obj.name in attrs:
                obj.text = attrs[obj.name]
    return comp


def _component_children(doc, comp):
    idx = doc.all_objects.index(comp)
    return [o for o in doc.all_objects
            if getattr(o, 'owner_index', None) == idx]


def _apply_text_overrides(page, inst_el, comp):
    """Per-instance IR <text> overrides (>NAME / >VALUE positions, absolute
    canvas coords) -> move the cloned Designator/Comment records."""
    for t in inst_el.findall('text'):
        content = (t.text or '').strip()
        x, y = page.pt(t.get('x', '0'), t.get('y', '0'))
        rot = round(float(t.get('rot', '0'))) % 360
        target = None
        if content in ('>NAME', '>PART'):
            target = next((o for o in _component_children(page.doc, comp)
                           if type(o).__name__ == 'AltiumSchDesignator'), None)
        elif content == '>VALUE':
            target = next((o for o in _component_children(page.doc, comp)
                           if type(o).__name__ == 'AltiumSchParameter'
                           and o.name == 'Comment'), None)
        if target is None:
            continue
        target.location = CoordPoint.from_mils(x, y)
        target.orientation = _orient(rot)
        target.justification = _justif(t.get('align', 'bottom-left'))
        if hasattr(target, 'auto_position'):
            target.auto_position = False


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
    pp.orientation = TextOrientation((round(float(inst_el.get('rot', '0')))
                                      // 90) % 4)
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


def _emit_net_label(page, net_name, x_um, y_um, rot, size_um):
    nl = AltiumSchNetLabel()
    x, y = page.pt(x_um, y_um)
    nl.location = CoordPoint.from_mils(x, y)
    nl.text = _eagle_overbar_to_altium(net_name)
    nl.orientation = TextOrientation((round(float(rot or 0)) // 90) % 4)
    nl.font_id = _font(page.doc, size_um)
    nl.unique_id = generate_unique_id()
    page.doc.add_object(nl)


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


def _emit_nets(pages, sch_el, part_page, supply_desigs, supply_net_by_desig):
    """All <net> content onto pages. supply_desigs: designators placed as
    PowerPorts (their pinrefs name their island); supply_net_by_desig is
    filled by the caller BEFORE this runs (power ports need net names)."""
    wires_by_net = {n.get('name'): [w for s in n.findall('segment')
                                    for w in _wires_um(s)]
                    for n in sch_el.findall('net')}

    for net_el in sch_el.findall('net'):
        net_name = net_el.get('name')
        segments = net_el.findall('segment')
        named_segs = set()

        for seg_el in segments:
            page = _segment_page(pages, seg_el, part_page, net_name)
            if page is None:
                import_log.log(net_name, '', 'NET_SEGMENT has no geometry '
                                'and no placed refs, skipped')
                continue
            for w in seg_el.findall('line'):
                _emit_wire(page, w)
            for j in seg_el.findall('junction'):
                _emit_junction(page, j)
            for l in seg_el.findall('label'):
                named_segs.add(id(seg_el))
                _emit_net_label(page, net_name, l.get('x'), l.get('y'),
                                l.get('rot', '0'), l.get('size', '1270'))
            # a PowerPort names (and globally joins) ITS island
            if any(r.get('part') in supply_desigs
                   for r in seg_el.findall('pinref')):
                named_segs.add(id(seg_el))

        # Island label synthesis for user-named nets (N$ autonames stay
        # anonymous — Altium will autoname disconnected pieces itself,
        # which is exactly what an autoname means).
        if not re.fullmatch(r'N\$\d+', net_name or ''):
            other = [w for nm, ws in wires_by_net.items()
                     if nm != net_name for w in ws]
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
                _emit_net_label(page, net_name, ax, ay, '0', '1270')
                import_log.log(net_name, '', 'NET_LABEL synthesized on an '
                                'unlabeled island (Altium net naming is '
                                'geometric per island)')


# ---------------------------------------------------------------------------
# Decorative schematic geometry
# ---------------------------------------------------------------------------

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
            page.doc.add_object(lab)
        elif el.tag in ('arc', 'shape', 'polygon'):
            import_log.log('schematic', el.tag,
                           'DECORATIVE geometry type not exported yet (v1)')


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def export_project(swprj_path, output_dir):
    swprj_path = Path(swprj_path)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ir_root = ET.parse(swprj_path).getroot()
    proj_name = ir_root.get('name', swprj_path.stem)

    if ir_root.find('module') is not None:
        raise ValueError('Altium project export v1 is FLAT: <module> '
                         'hierarchy not supported yet — hard reject.')

    pool = {s.get('name'): s for s in ir_root.findall('symbols/symbol')}
    comp_by_name = {c.get('name'): c for c in ir_root.findall('component')}
    sch_el = ir_root.find('schematic')
    if sch_el is None:
        raise ValueError(f'{swprj_path}: no <schematic> — nothing to export')

    # 1. SchLib — geometry source for placement (reuses the library exporter)
    schlib = AltiumSchLib()
    schlib._ensure_font_manager()
    from babel.altium_exporter import _sym_used_standalone
    for sym_el in pool.values():
        if _sym_used_standalone(sym_el.get('name', ''), ir_root):
            _export_schlib_symbol(sym_el, schlib, ir_root)
    for comp_el in ir_root.findall('component'):
        if is_multi_gate(comp_el):
            _export_schlib_multipart(comp_el, pool, schlib)
    schlib_path = out_dir / f'{proj_name}.SchLib'
    schlib.save(schlib_path)
    print(f'Written: {schlib_path}')

    # 2. Pages from FRAME instances
    pages = []
    frame_insts = []
    for inst_el in sch_el.findall('instance'):
        comp_el = comp_by_name.get(inst_el.get('component', ''))
        if comp_el is not None and _is_frame(comp_el, pool):
            frame_insts.append((inst_el, comp_el))
    if not frame_insts:
        raise ValueError(f'{swprj_path}: no FRAME instance — every import '
                         'path synthesizes one, cannot derive sheet size')
    for i, (inst_el, comp_el) in enumerate(frame_insts):
        x1, x2, y1, y2 = _frame_bbox(inst_el, comp_el, pool)
        fname = (f'{proj_name}.SchDoc' if i == 0
                 else f'{proj_name}_{i + 1}.SchDoc')
        pages.append(_Page(inst_el.get('name', f'FRAME{i + 1}'), fname,
                           x1, y1, x2 - x1, y2 - y1))
        import_log.log(inst_el.get('name', ''), '',
                       'FRAME -> sheet size only, frame graphics dropped '
                       '(Altium draws its own border)')

    # 3. Supply net lookup: designator -> net name (from pinrefs)
    supply_net_by_desig = {}
    for net_el in sch_el.findall('net'):
        for seg_el in net_el.findall('segment'):
            for r in seg_el.findall('pinref'):
                supply_net_by_desig.setdefault(r.get('part'),
                                               net_el.get('name'))

    # 4. Place instances
    part_page = {}
    supply_desigs = set()
    n_parts = n_power = 0
    frame_ids = {id(i) for i, _ in frame_insts}
    for inst_el in sch_el.findall('instance'):
        if id(inst_el) in frame_ids:
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
        comp = _place_instance(page, inst_el, comp_el, schlib_path, part_id,
                               value, attrs)
        _apply_text_overrides(page, inst_el, comp)
        n_parts += 1

    # 5. Nets
    _emit_nets(pages, sch_el, part_page, supply_desigs, supply_net_by_desig)

    # 6. Decorative geometry
    _emit_decorations(pages, sch_el)

    # 7. Save SchDocs + PrjPcb
    for page in pages:
        page.doc.save(out_dir / page.fname)
        print(f'Written: {out_dir / page.fname}')
    builder = AltiumPrjPcbBuilder(proj_name)
    for page in pages:
        builder.add_schdoc(page.fname)
    builder.add_schlib(schlib_path.name)
    prj_path = out_dir / f'{proj_name}.PrjPcb'
    builder.save(prj_path)
    print(f'Written: {prj_path}')

    print(f'Total: {len(pages)} sheet(s), {n_parts} components, '
          f'{n_power} power ports')
    import_log.write(out_dir / f'{proj_name}.PrjPcb')


if __name__ == '__main__':
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else 'outputs/tolmach.swprj'
    dst = sys.argv[2] if len(sys.argv) > 2 else 'outputs/altium_out'
    export_project(src, dst)
