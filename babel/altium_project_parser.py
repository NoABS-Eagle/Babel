"""Altium PROJECT (.PrjPcb + .SchDoc(s) + .PcbDoc + sibling .IntLib) -> IR
XML converter.

First pass, FLAT topology only (single- or multi-page-but-not-hierarchical —
see [[project_altium_project_import]] memory / decisions.md): no sheet
symbols, no <module> nesting. Scope for this pass — connectivity + placement,
resolved against a real library. Deferred: DRC rules, dimensioning, embedded
3D models, explicit Altium pin<->pad remap (MAP_DEFINER — the library's own
<pin-mapping>, built by altium_parser.py, already carries the real mapping).

LIBRARY RESOLUTION (2026-07-21, course correction — see memory "ПЕРЕСМОТР
КУРСА"): components/symbols/footprints are NOT rebuilt empirically from
placed schematic/board instances. The precondition is a fresh
`Design -> Make Integrated Library` run in Altium (same operator step as
KiCad's "Archive Project"/"Export Footprints to New Library" preconditions),
producing a `<PrjPcb stem>.IntLib` sibling — the SAME already-proven
babel.altium_parser.convert_to_tree() that handles multi-gate parts,
placeholders and explicit pin-mapping builds the component pool from it.
The schematic resolves every placed component against that pool BY NAME
ONLY. A schematic reference to a name absent from the library is a HARD
REJECT — never a silent fallback to the geometry Altium happens to have
baked into that one placed instance, even though that geometry is technically
available (see the [[project_altium_project_import]] amendment to
precondition #1). This mirrors the KiCad project importer's own rule
(library is truth, a cached/edited instance copy is not) and
[[feedback_easy_unambiguous_no_heuristics]]: a schematic/library mismatch is
a signal to stop and have the user rebuild the IntLib, not a case for Babel
to guess which version is correct.

Net naming: POWER PORT always outranks net label (fixed IR rule, does NOT
follow the project's own PowerPortNamesTakePriority setting in .PrjPcb —
determinism over faithfully replicating Altium, decision confirmed with the
user 2026-07-21).
"""
import configparser
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.dom import minidom

from altium_monkey.altium_schdoc import AltiumSchDoc
from altium_monkey.altium_pcbdoc import AltiumPcbDoc
from altium_monkey.altium_netlist_multi_sheet import AltiumNetlistMultiSheetCompiler
from altium_monkey.altium_netlist_options import NetlistOptions

from babel.ir_util import clean_attr_name, format_stack, LAYER_DIMENSION, unoffset_contour
from babel import import_log
from babel.altium_parser import (
    convert_to_tree, _part_letter, _altium_overbar_to_eagle, _pt_to_um,
    _ORIENT_TO_ROT, _JUSTIFICATION, _f,
    emit_rect_geometry, emit_polyline_geometry, emit_line_geometry,
    emit_arc_geometry, emit_ellipse_geometry, emit_polygon_geometry,
    emit_label_geometry,
)
from babel.altium_exporter import DRC_RULES, _um_lw

_MILS_TO_UM = 25.4
_GAP_MILS = 500   # gap between tiled sheet frames, mils (KiCad path's _GAP_MM analog)


def _um(mils):
    return round(float(mils) * _MILS_TO_UM)


# ---------------------------------------------------------------------------
# Frame: size only, matching KiCad's own convention (ir_schema.md "Frame") —
# every placed object must fit entirely inside it (decisions.md
# "Stray-валидация"). The GOST/whatever stamp graphics the sheet's template
# draws are NOT reproduced (user's explicit call, 2026-07-21) — only the
# plain boundary rectangle, sized off Altium's own real structural data
# (doc.sheet.get_sheet_size_mils()), same certainty class as KiCad's own
# sch.paper.
# ---------------------------------------------------------------------------

def _frame_component_name(width_mils, height_mils):
    return f'Frame_{_um(width_mils)}x{_um(height_mils)}um'


def _build_frame_component(width_mils, height_mils, symbols_el, proj_el):
    """Get-or-create the shared Frame <component>+<symbol> for one sheet
    size — a component without a footprint, exactly one rectangle on the
    `FRAME` layer, plus a single `>SHEET` placeholder naming the page.

    Still no title block (unlike the KiCad path's _build_frame_component,
    which stamps Title/Company/Rev/Date): Altium's own title-block fields on
    these documents are formula references (`Title='=project_title'`), not
    text, so there is nothing honest to put there. The SHEET NAME is
    different — it is real per-page data that was being dropped outright,
    and with pages tiled side by side an unlabelled frame is unidentifiable.
    Same placeholder mechanism as `>VALUE`: the text lives on the shared
    per-size component, the value on each frame INSTANCE.
    """
    comp_name = _frame_component_name(width_mils, height_mils)
    if proj_el.find(f'component[@name="{comp_name}"]') is not None:
        return comp_name

    w, h = _um(width_mils), _um(height_mils)
    sym_el = ET.SubElement(symbols_el, 'symbol', name=comp_name)
    ET.SubElement(sym_el, 'shape',
                  x='0', y='0', w=str(w), h=str(h),
                  roundness='0', outline=str(_um(6)), rot='0',
                  layer='FRAME')
    # Just inside the frame's bottom-left corner (symbol-local origin is the
    # frame's own CENTER — see the instance loop), on the same `INFO` layer
    # the KiCad path uses for its title-block placeholders.
    margin = _um(100)
    t_el = ET.SubElement(sym_el, 'text',
                         x=str(-w // 2 + margin), y=str(-h // 2 + margin),
                         size=str(_um(120)), rot='0', align='bottom-left',
                         layer='INFO')
    t_el.text = '>SHEET'
    ET.SubElement(proj_el, 'component', name=comp_name, prefix='FRAME',
                  symbol=comp_name, synth='frame')
    return comp_name


# ---------------------------------------------------------------------------
# Power port: Altium's PowerObjectStyle is NOT a library component at all —
# no designator, no library_ref, just a location + one of 11 built-in shape
# styles (confirmed against altium_monkey's PowerObjectStyle enum: CIRCLE,
# ARROW, BAR, WAVE, GND_POWER, GND_SIGNAL, GND_EARTH, GOST_ARROW,
# GOST_GND_POWER, GOST_GND_EARTH, GOST_BAR — three of them explicitly ГОСТ
# variants, matching this project's own A3_GOST.SchDot sheet template). IR's
# canon power symbol IS an ordinary pool <component> (a single `sup`-
# direction pin, synthesized "#PWR" designator) — same reason ir_schema
# never added a dedicated "power symbol" entity: a library component lets
# the user redraw its graphics if they want, and avoids inventing a new
# object class for something composition already expresses.
#
# Classification (user's call, 2026-07-21): the 11 real styles collapse to
# exactly TWO synthesized symbols — CIRCLE/ARROW/WAVE (rail-style pictograms)
# count as the POSITIVE rail; everything else (BAR + every GND_*/GOST_GND_*
# variant, i.e. ground-style pictograms) counts as the NEGATIVE/ground
# symbol. Not a faithful per-style redraw — same "rough now, unify visuals
# across every CAD path later" spirit just agreed for labels.
# ---------------------------------------------------------------------------

_POWER_POSITIVE_STYLES = {0, 1, 3}   # CIRCLE, ARROW, WAVE

_PWR_POS_NAME = 'PWR_POS'
_PWR_NEG_NAME = 'PWR_NEG'


def _build_power_symbols(symbols_el, proj_el):
    """Get-or-create both synthesized power symbols at once (there are only
    ever two, unlike Frame's one-per-sheet-size). Geometry is NOT our own
    drawing — copied from testData/supref.lbr's real GND/VDC symbols (user's
    call: our first hand-drawn attempt "неважные" — mediocre), converted
    mm -> µm (×1000, these are literal Eagle-native coordinates, not
    Altium mils). Includes the reference symbols' own `>VALUE` placeholder —
    resolved per INSTANCE from the real net name the power port carried in
    the source schematic (see convert_project's instance-building loop),
    not the fixed pin name. Pin names themselves stay fixed generic labels
    ('VCC'/'GND') — this project's own AltiumNetlistMultiSheetCompiler
    already establishes full connectivity from wire geometry directly, so
    IR's `sup`-pin by-NAME collapse mechanism (what Eagle/KiCad rely on)
    isn't what's gluing anything together here; the net's real name is
    carried by the containing <net name=...> already, `>VALUE` is purely
    the on-schematic label."""
    for name in (_PWR_POS_NAME, _PWR_NEG_NAME):
        if proj_el.find(f'component[@name="{name}"]') is not None:
            continue
        sym_el = ET.SubElement(symbols_el, 'symbol', name=name)
        if name == _PWR_POS_NAME:
            # supref.lbr VDC: pin (0,0) rot=90 -> body/arrow apex at
            # (0,2540); chevron down to (±762,1270).
            ET.SubElement(sym_el, 'pin', name='VCC', x='0', y='0', rot='90',
                          length='2540', direction='sup', pinvis='0', padvis='0',
                          function='none')
            ET.SubElement(sym_el, 'line', x1='762', y1='1270', x2='0', y2='2540', width='254')
            ET.SubElement(sym_el, 'line', x1='0', y1='2540', x2='-762', y2='1270', width='254')
            ET.SubElement(sym_el, 'text', x='0', y='3556', size='1270', rot='0',
                          align='center', layer='VALUES').text = '>VALUE'
        else:
            # supref.lbr GND: pin (0,0) rot=270 -> body/bar at (0,-2540).
            ET.SubElement(sym_el, 'pin', name='GND', x='0', y='0', rot='270',
                          length='2540', direction='sup', pinvis='0', padvis='0',
                          function='none')
            ET.SubElement(sym_el, 'line', x1='-1905', y1='-2540', x2='1905', y2='-2540', width='254')
            ET.SubElement(sym_el, 'text', x='0', y='-3302', size='889', rot='0',
                          align='center', layer='VALUES').text = '>VALUE'
        ET.SubElement(proj_el, 'component', name=name, prefix='#PWR',
                      symbol=name, synth='power')


# ---------------------------------------------------------------------------
# Board (<layout>) — first pass, scope agreed 2026-07-21: outline + element
# placement only. Copper (tracks/vias/polygons/split-planes) and the real
# per-layer stack (thickness/dielectric) are BOTH deferred — see
# [[project_altium_project_parser_decisions]] "Плата". `<element>` on the
# IR board carries ONLY placement (name/x/y/rot/side) — the footprint
# itself is already fully defined on the shared pool <component> (built
# from the IntLib), never re-embedded per board element (confirmed against
# eagle_board_parser.py's own _convert_element: identity is the REFDES,
# component binding lives on the schematic instance).
# ---------------------------------------------------------------------------

def _convert_board_outline(pcb, layout_el):
    """AltiumBoardOutline -> IR <line>/<arc> on LAYER_DIMENSION (ir_schema.md
    board outline convention, same layer eagle_board_parser uses). Each
    vertex's `is_arc` flags whether the edge FROM it TO the next vertex
    (wrapping around, the outline is a closed loop) is an arc, with
    center/radius/start_angle_deg/end_angle_deg describing it.

    start_angle_deg/end_angle_deg do NOT reliably correspond to (this
    vertex, next vertex) in a fixed order — confirmed empirically both
    ways: vertex0's start_angle lands on ITS OWN (x,y) and end_angle on the
    NEXT vertex (forward), but vertex6's end_angle lands on ITS OWN (x,y)
    and start_angle on the NEXT vertex (backward) — same board, same file
    (user caught this: "некоторые арки инвертированы, но не все"). Fixed by
    checking which raw angle actually matches THIS vertex's own coordinate
    (small tolerance) instead of assuming a fixed order, then signing
    `curve` for the forward-from-a-to-b sweep either way."""
    ox, oy = pcb.board.origin_x, pcb.board.origin_y
    verts = pcb.board.outline.vertices
    n = len(verts)

    def pt(cx, cy, r, ang_deg):
        a = math.radians(ang_deg)
        return cx + r * math.cos(a), cy + r * math.sin(a)

    for i in range(n):
        a, b = verts[i], verts[(i + 1) % n]
        x1, y1 = _um(a.x_mils - ox), _um(a.y_mils - oy)
        x2, y2 = _um(b.x_mils - ox), _um(b.y_mils - oy)
        if x1 == x2 and y1 == y2:
            continue   # degenerate zero-length edge (arc/line vertex coincidence)
        if a.is_arc:
            # RELATIVE comparison, not an absolute-distance threshold — a
            # pair of adjacent arc vertices can carry slightly DIFFERENT
            # radius_mils for what's geometrically the same arc (confirmed:
            # 47.2441 vs 47.2447 on this very board), which trig amplifies
            # into several mil of positional error — enough to blow past a
            # fixed 0.5 mil cutoff and misclassify direction. Whichever
            # hypothesis (start->self, end->self) is closer wins, always.
            sx, sy = pt(a.center_x_mils, a.center_y_mils, a.radius_mils, a.start_angle_deg)
            ex, ey = pt(a.center_x_mils, a.center_y_mils, a.radius_mils, a.end_angle_deg)
            d_start = math.hypot(sx - a.x_mils, sy - a.y_mils)
            d_end = math.hypot(ex - a.x_mils, ey - a.y_mils)
            forward = d_start < d_end
            # Normalize into (-180, 180] — NOT a plain `% 360` (which forces
            # a positive result and corrupts a genuinely-negative/CW minor
            # sweep, e.g. -70.742° for a real corner, into its 289.258°
            # long-way-round complement; confirmed on this exact board's
            # notch arcs). This form fixes the 0-vs-360 wraparound (Altium
            # reports the same angle as 0.0 on one vertex, 360.0 on its
            # neighbor) while still always picking the SHORT sweep.
            raw = (a.end_angle_deg - a.start_angle_deg) if forward \
                else (a.start_angle_deg - a.end_angle_deg)
            curve = ((raw + 180) % 360) - 180
            ET.SubElement(layout_el, 'arc', x1=str(x1), y1=str(y1), x2=str(x2), y2=str(y2),
                          curve=f'{curve:g}', width='0', layer=str(LAYER_DIMENSION))
        else:
            ET.SubElement(layout_el, 'line', x1=str(x1), y1=str(y1), x2=str(x2), y2=str(y2),
                          width='0', layer=str(LAYER_DIMENSION))


# Fixed Altium format convention (not board-specific, never renamed by a
# user): the legacy PCB LayerID space is exactly Top=1, Mid1..Mid30=2..31,
# Bottom=32 — confirmed via `pcb.board.display_name_for_legacy_layer()`
# returning THIS board's own (possibly user-renamed) copper-layer names in
# that exact order (ground truth 8AO-VI: id 1/2/3/32 -> 'Top Layer'/'Signal
# Layer 1'/'Signal Layer 2'/'Bottom Layer', matching `v9_stack` order 1:1).
# `PcbPolygon.layer` is a separate string enum over the SAME fixed space
# ('TOP'/'MID1'..'MID30'/'BOTTOM') rather than the legacy int id track/arc
# use — this table bridges the two before the real per-board copper map
# (built in _build_layout from v9_stack, see `_altium_copper_layer_map`)
# takes it the rest of the way to an IR layer number.
_POLYGON_LAYER_TO_LEGACY_ID = {'TOP': 1, 'BOTTOM': 32}
_POLYGON_LAYER_TO_LEGACY_ID.update({f'MID{n}': n + 1 for n in range(1, 31)})


def _altium_copper_layer_map(pcb, copper_span):
    """legacy Altium PCB LayerID (int, track/arc/via's own `.layer`) -> IR
    layer number string (top-down 1/2../-1, ir_util.py), for exactly the
    copper layers actually present in this board's OWN stack (`copper_span`,
    the `<layer copper=True>` v9_stack entries _build_layout already sliced
    out for the stack formula — same source of truth, not a second read).
    Matches by NAME against `display_name_for_legacy_layer()` rather than
    assuming legacy id N always means "the Nth copper layer of THIS board"
    — a board that skips some Mid-Layer slots (uses Mid1 and Mid5 but not
    2-4) would otherwise misnumber everything after the gap."""
    name_to_ir = {}
    n = len(copper_span)
    for idx, layer in enumerate(copper_span):
        ir = '1' if idx == 0 else ('-1' if idx == n - 1 else str(idx + 1))
        name_to_ir[layer.name] = ir
    out = {}
    for legacy_id in range(1, 33):
        name = pcb.board.display_name_for_legacy_layer(legacy_id)
        if name in name_to_ir:
            out[legacy_id] = name_to_ir[name]
    return out


def _polygon_outline_edges(vertices, ox, oy):
    """Altium PcbPolygon.outline -> [('line'/'arc', x1, y1, x2, y2, curve),
    ...] IR-µm edge list, ir_util.unoffset_contour's input shape. Same
    robust per-edge arc-direction check as _convert_board_outline (start/
    end angle order is NOT fixed here either — same file, same lesson)."""
    def pt(cx, cy, r, ang_deg):
        a = math.radians(ang_deg)
        return cx + r * math.cos(a), cy + r * math.sin(a)

    n = len(vertices)
    edges = []
    for i in range(n):
        a, b = vertices[i], vertices[(i + 1) % n]
        x1, y1 = _um(a.x_mils - ox), _um(a.y_mils - oy)
        x2, y2 = _um(b.x_mils - ox), _um(b.y_mils - oy)
        if x1 == x2 and y1 == y2:
            continue
        if a.kind == 1:   # arc
            # Relative comparison — see _convert_board_outline's identical
            # fix for why (radius_mils imprecision between paired vertices
            # can exceed a fixed distance threshold).
            sx, sy = pt(a.center_x_mils, a.center_y_mils, a.radius_mils, a.start_angle)
            ex, ey = pt(a.center_x_mils, a.center_y_mils, a.radius_mils, a.end_angle)
            d_start = math.hypot(sx - a.x_mils, sy - a.y_mils)
            d_end = math.hypot(ex - a.x_mils, ey - a.y_mils)
            forward = d_start < d_end
            # (-180,180] normalization, not plain `% 360` — see
            # _convert_board_outline's identical fix for why.
            raw = (a.end_angle - a.start_angle) if forward else (a.start_angle - a.end_angle)
            curve = ((raw + 180) % 360) - 180
            edges.append(('arc', x1, y1, x2, y2, curve))
        else:
            edges.append(('line', x1, y1, x2, y2, 0.0))
    return edges


def _polygon_thermal_relief(pcb, polygon_name):
    """Effective Direct/Relief connect-style for a named polygon pour, from
    Altium's `PolygonConnect` design rules — a genuine little rule engine
    (SCOPE1EXPRESSION/SCOPE2EXPRESSION + PRIORITY, lower number wins), not a
    single flat value: ground truth on RLT504_117C.PcbDoc has 9 enabled
    PolygonConnect rules, most scoped to one specific named polygon
    (`SCOPE2EXPRESSION="IsNamedPolygon('X')"`), one scoped to vias only
    (`SCOPE1EXPRESSION='isVia'`, applies regardless of WHICH polygon), and
    one true global fallback (`SCOPE1EXPRESSION=SCOPE2EXPRESSION='All'`).
    IR's `<polygon thermals>` is a single flag for the WHOLE polygon
    regardless of what it connects to (pad vs via) — a rule scoped by
    connecting-object type rather than by polygon name (like the isVia one)
    can't be mapped onto that per-object distinction, so it's deliberately
    NOT treated as a candidate default here (only a rule that's either
    genuinely global — SCOPE1 and SCOPE2 both 'All' — or specific to THIS
    polygon by name is considered, keeping the spoke width/count/angle data
    Altium also carries per rule is still lost — IR/Eagle's polygon model
    has no field for it, only the Direct/Relief bit).
    Returns True (Relief/thermals on), False (Direct/thermals off), or None
    if no applicable rule was found (caller leaves Eagle's own default,
    which is thermals on, alone)."""
    target_scope2 = f"IsNamedPolygon('{polygon_name}')"
    best = None   # (priority, is_relief)
    for r in pcb.rules:
        rr = r.raw_record
        if rr.get('RULEKIND') != 'PolygonConnect' or rr.get('ENABLED') != 'TRUE':
            continue
        scope1 = rr.get('SCOPE1EXPRESSION')
        scope2 = rr.get('SCOPE2EXPRESSION')
        if not (scope2 == target_scope2 or (scope1 == 'All' and scope2 == 'All')):
            continue
        try:
            priority = int(rr.get('PRIORITY'))
        except (TypeError, ValueError):
            continue
        if best is None or priority < best[0]:
            best = (priority, rr.get('CONNECTSTYLE') == 'Relief')
    return best[1] if best else None


def _build_polygons(pcb, layout_el, ox, oy, signal_by_net, copper_map):
    """Copper pour polygons -> IR <polygon> under their net's <signal>
    (pen model, ir_schema.md "МОДЕЛЬ ПЕРА" — same un-offset technique as
    kicad_board_parser._import_zone, see memory "Плата"): the polygon's
    OWN outline is the filled boundary (Altium's pour result), un-offset
    inward by track_width/2 recovers the pen centerline. `hatch_style`
    (string, "Solid" seen on this project) drives `fill` — non-solid
    deferred (logged, not guessed) since no ground truth for its percent
    formula yet."""
    for p in pcb.polygons:
        legacy_id = _POLYGON_LAYER_TO_LEGACY_ID.get(p.layer)
        ir_layer = copper_map.get(legacy_id) if legacy_id is not None else None
        if ir_layer is None:
            import_log.log(f'layout: polygon "{p.name}" on non-copper side '
                            f'{p.layer!r} dropped')
            continue
        sig_el = signal_by_net.get(p.net)
        if sig_el is None:
            import_log.log(f'layout: polygon "{p.name}" net index {p.net} '
                            f'has no matching <signal> — dropped')
            continue
        w_um = _um(p.track_width_mils)
        edges = _polygon_outline_edges(p.outline, ox, oy)
        try:
            verts, exact = unoffset_contour(edges, w_um / 2)
        except ValueError as e:
            import_log.log(f'layout: polygon "{p.name}": {e} — dropped')
            continue
        if not exact:
            import_log.log(f'layout: polygon "{p.name}" outline not a pen '
                            f'equidistant — centerline recovered, copper = '
                            f'actual Altium fill (track_width pen)')
        if p.hatch_style != 'Solid':
            import_log.log(f'layout: polygon "{p.name}" hatch_style={p.hatch_style!r} '
                            f'— fill% formula not ground-truthed yet, using 100 (solid)')
        pg_el = ET.SubElement(sig_el, 'polygon', width=str(w_um), fill='100',
                              layer=ir_layer)
        if _polygon_thermal_relief(pcb, p.name) is False:
            pg_el.set('thermals', '0')
        for vx, vy, vcurve in verts:
            v_el = ET.SubElement(pg_el, 'vertex', x=str(round(vx)), y=str(round(vy)))
            if vcurve:
                v_el.set('curve', f'{vcurve:g}')


def _build_signals(pcb, layout_el, ox, oy, multi_pad_groups, copper_map):
    """<signal name=NetName> per PCB net — contactref (pad membership) +
    routed copper (tracks/arcs with NO owning component — footprint-owned
    copper is already carried on the pool footprint, never duplicated
    here) + through vias + copper pour polygons (_build_polygons, called
    from here so it can add to a signal BEFORE the empty-signal prune
    below runs — a net with ONLY a pour and no discrete tracks must not
    be pruned as if it had no copper at all).

    Returns {net_index: <signal> element} for _build_polygons."""
    comp_designator = {i: c.designator for i, c in enumerate(pcb.components)}
    pad_net = {}   # (designator, pad_name) set per net_index, for contactref
    # A raw Altium pad designator shared by 2+ physical pads (multi_pad_groups,
    # see convert_project) must produce a <contactref> for EACH of them, not
    # just one — pcb.pads still reports the same raw designator for every
    # occurrence, so pair them up by encounter order against the resolved
    # footprint's own disambiguated name list (same order both sides: neither
    # altium_monkey's raw pad list nor _convert_footprint's pad_name_groups
    # reorders pads, both walk the same underlying record stream).
    pad_occurrence = {}   # (ref, raw designator) -> next index into its group
    for p in pcb.pads:
        if p.net_index is None or p.net_index < 0:
            continue
        ref = comp_designator.get(p.component_index)
        if ref is None:
            continue
        key = (ref, p.designator)
        group = multi_pad_groups.get(key)
        if group:
            idx = pad_occurrence.get(key, 0)
            pad_occurrence[key] = idx + 1
            name = group[idx] if idx < len(group) else p.designator
        else:
            name = p.designator
        pad_net.setdefault(p.net_index, set()).add((ref, name))

    skipped_layers = set()
    signal_by_net = {}
    for i, net in enumerate(pcb.nets):
        name = _altium_overbar_to_eagle(net.name)
        sig_el = ET.SubElement(layout_el, 'signal', name=clean_attr_name(name))
        signal_by_net[i] = sig_el
        for ref, pad in sorted(pad_net.get(i, ())):
            ET.SubElement(sig_el, 'contactref', element=ref, pad=pad)
        for t in pcb.tracks:
            if t.net_index != i or t.component_index is not None:
                continue
            ir_layer = copper_map.get(t.layer)
            if ir_layer is None:
                skipped_layers.add(t.layer)
                continue
            ET.SubElement(sig_el, 'line',
                          x1=str(_um(t.start_x_mils - ox)), y1=str(_um(t.start_y_mils - oy)),
                          x2=str(_um(t.end_x_mils - ox)), y2=str(_um(t.end_y_mils - oy)),
                          width=str(_um(t.width_mils)), layer=ir_layer)
        for a in pcb.arcs:
            if a.net_index != i or a.component_index is not None:
                continue
            ir_layer = copper_map.get(a.layer)
            if ir_layer is None:
                skipped_layers.add(a.layer)
                continue
            sx, sy = _pt_on_circle(a.center_x_mils, a.center_y_mils, a.radius_mils, a.start_angle)
            ex, ey = _pt_on_circle(a.center_x_mils, a.center_y_mils, a.radius_mils, a.end_angle)
            sweep = (a.end_angle - a.start_angle) % 360 or 360
            ET.SubElement(sig_el, 'arc',
                          x1=str(_um(sx - ox)), y1=str(_um(sy - oy)),
                          x2=str(_um(ex - ox)), y2=str(_um(ey - oy)),
                          curve=f'{sweep:g}', width=str(_um(a.width_mils)), layer=ir_layer)
        for v in pcb.vias:
            if v.net_index != i:
                continue
            # IR only models THROUGH vias (see [[project_ir_via_only_through]])
            # — a blind/buried via (span not the full Top..Bottom stack) can't
            # be represented, so hard-reject the whole import rather than
            # silently drawing it as if it went all the way through (same
            # policy already enforced on the KiCad/Eagle board paths).
            if v.layer_start != 1 or v.layer_end != 32:
                raise ValueError(
                    f'net "{net.name}": via at ({v.x_mils:g}, {v.y_mils:g}) mils '
                    f'spans Altium layer {v.layer_start}..{v.layer_end}, not the '
                    f'full Top..Bottom stack — blind/buried vias are not '
                    f'modelled in IR yet, cannot import this board.')
            ET.SubElement(sig_el, 'via',
                          x=str(_um(v.x_mils - ox)), y=str(_um(v.y_mils - oy)),
                          drill=str(_um(v.hole_size_mils)), diameter=str(_um(v.diameter_mils)))
    if skipped_layers:
        import_log.log(f'layout: track/arc(s) on non-copper Altium layer '
                        f'ID(s) {sorted(skipped_layers)} dropped (mechanical/'
                        f'dimension records mixed into tracks, not real copper)')

    _build_polygons(pcb, layout_el, ox, oy, signal_by_net, copper_map)

    for sig_el in list(layout_el.findall('signal')):
        if not list(sig_el):
            layout_el.remove(sig_el)


def _pt_on_circle(cx, cy, r, ang_deg):
    a = math.radians(ang_deg)
    return cx + r * math.cos(a), cy + r * math.sin(a)


# ir_schema.md "DRC-ядро" — 6 numbers, same set eagle_board_parser reads
# from Eagle designrules (_RULE_PARAMS). Altium RULEKIND -> (IR attr, field
# key) confirmed against this project's real Rules6 stream (user added
# BoardOutlineClearance/MinimumAnnularRing after the first pass didn't have
# them — Altium DOES have both, just as separate rule KINDS, not a scoped
# variant of Clearance the way edge_clearance first looked from Eagle's
# side). No Eagle-style multi-pair clearance merge needed — Altium's own
# generic Clearance rule (scope1=All, scope2=All) is already the single
# value.
# The RULEKIND table lives in altium_exporter — one table, both directions
# (the board exporter writes these very rules back).
_DRC_RULES = DRC_RULES


def _parse_dr_dim(s):
    """Altium dimension string ('7.4803mil', '10mil', occasionally a bare
    mm number) -> IR µm int. Same shape as eagle_board_parser._dr_um for
    Eagle's own designrules strings — this is the Altium-side equivalent,
    not a reused function (different unit vocabulary / no shared source)."""
    m = re.match(r'^([\d.]+)\s*(mil|mm)?$', s.strip())
    if not m:
        raise ValueError(f'designrule dimension {s!r} unparseable')
    val, unit = float(m.group(1)), m.group(2) or 'mil'
    return round(val * (_MILS_TO_UM if unit == 'mil' else 1000.0))


def _build_rules(pcb, layout_el):
    """<rules> — first child of <layout>, same position convention as
    eagle_board_parser._rules_el. Only ENABLED rules of a known kind are
    read. The flat 6-number core wants the BOARD DEFAULT, which in Altium's
    rule engine is the generic-scope rule (both scope expressions 'All');
    among several generics the one with the numerically largest PRIORITY is
    the weakest fallback — exactly the default semantics (ground truth
    BC2087: Clearance had scoped PoE rules serialized BEFORE the generic
    'general' 4mil one, so the earlier first-in-stream pick baked a
    39.37mil net-class gap in as the board clearance). Scoped rules are
    never read — dropped with a log line, not guessed at."""
    generic, scoped_kinds = {}, set()
    for r in pcb.rules:
        rr = r.raw_record
        kind = rr.get('RULEKIND')
        mapping = _DRC_RULES.get(kind)
        if mapping is None or rr.get('ENABLED') != 'TRUE':
            continue
        attr, field = mapping
        raw = rr.get(field)
        if not raw:
            continue
        if rr.get('SCOPE1EXPRESSION') != 'All' or rr.get('SCOPE2EXPRESSION') != 'All':
            scoped_kinds.add(attr)
            continue
        try:
            prio = int(rr.get('PRIORITY') or 0)
        except ValueError:
            prio = 0
        if attr not in generic or prio > generic[attr][0]:
            generic[attr] = (prio, _parse_dr_dim(raw))
    if scoped_kinds:
        import_log.log(f'layout: scoped (non-All/All) Altium rules for '
                        f'{sorted(scoped_kinds)} dropped — flat 6-number DRC '
                        f'core keeps only the generic board default')
    missing = {attr for attr in scoped_kinds if attr not in generic}
    if missing:
        import_log.log(f'layout: rules {sorted(missing)} exist ONLY with a '
                        f'specific scope, no generic All/All default — attribute '
                        f'left unset')
    if generic:
        rules_el = ET.Element('rules', **{k: str(v) for k, (_, v) in generic.items()})
        layout_el.insert(0, rules_el)


def _build_layout(pcb, proj_el, placements_designators, multi_pad_groups):
    """<layout name="main"> — outline + stack + element placement.

    `stack=` uses `pcb.board.v9_stack` — the board's REAL, user-authored
    Layer Stack Manager data (copper thickness, core/prepreg dielectric
    height+material+eps) — NOT `pcb.board.layer_stackup`, which is a
    separate, legacy 32-layer array left at Altium's unmodified template
    defaults on this test board and was wrongly treated as authoritative in
    an earlier pass (caught by the user: "я открыл проект и вижу что стек
    описан правильно"). Ground-truthed on RLT504_117C.PcbDoc (2-layer):
    Top/Bottom copper 1.378mil (=35µm, standard 1oz) and a real FR-4 core,
    59.0551mil (=1500µm exactly) at eps=4.2. Works for any copper-layer
    count, not just 2 — `v9_stack` lists the whole physical stack top-down
    (Top Layer, dielectric, inner copper, dielectric, ..., Bottom Layer),
    already the exact order `format_stack`/IR's own top-down layer
    convention (1=top, 2..N-1=inner, -1=bottom; see ir_util.py) wants;
    ground-truthed on 8AO-VI's 4-layer RLT504_132.PcbDoc (Top/Signal 1/
    Signal 2/Bottom copper + 3 real dielectrics in between).
    Inner-layer copper (tracks/vias/polygons) is now read too — see
    `_altium_copper_layer_map` — for any copper-layer count, all through the
    same real per-board layer identity `v9_stack` already gives us. IR still
    only models THROUGH vias (see [[project_ir_via_only_through]]) — a via
    whose Altium span isn't the full Top-to-Bottom stack hard-rejects the
    whole import in `_build_signals`, same policy as the KiCad/Eagle board
    paths.
    """
    layout_el = ET.SubElement(proj_el, 'layout', name='main')
    v9 = pcb.board.v9_stack
    cu_idxs = [i for i, l in enumerate(v9) if l.is_copper]
    span = v9[cu_idxs[0]:cu_idxs[-1] + 1]
    copper_span = [l for l in span if l.is_copper]
    coppers = [round(l.copper_thickness * _MILS_TO_UM) for l in copper_span]
    dielectrics = []
    for l in span:
        if l.is_dielectric:
            d_um = round(l.diel_height * _MILS_TO_UM)
            eps = l.diel_constant if l.diel_constant else None
            tand = l.diel_loss_tangent if (eps is not None and l.diel_loss_tangent) else None
            dielectrics.append((d_um, eps, tand))
    layout_el.set('stack', format_stack(coppers, dielectrics))
    copper_map = _altium_copper_layer_map(pcb, copper_span)

    _build_rules(pcb, layout_el)
    _convert_board_outline(pcb, layout_el)

    ox, oy = pcb.board.origin_x, pcb.board.origin_y
    missing = set()
    for c in pcb.components:
        if c.designator not in placements_designators:
            missing.add(c.designator)
            continue
        el = ET.SubElement(layout_el, 'element', name=c.designator,
                            x=str(_um(c.get_x_mils(ox))), y=str(_um(c.get_y_mils(oy))))
        rot = c.get_rotation_degrees()
        bottom = c.layer == 'BOTTOM'
        if bottom:
            # Confirmed by the user against real Eagle: a bottom-side
            # component needs an extra +180° on top of Altium's own
            # rotation value to land correctly — Altium's `rotation` for a
            # bottom part isn't the same angle convention our mirror-then-
            # rotate placement (ir_util.place_ir_element) expects as-is.
            rot = (rot + 180) % 360
        if rot:
            el.set('rot', f'{rot:g}')
        if bottom:
            el.set('side', 'bottom')
    if missing:
        import_log.log(f'layout: {len(missing)} board component(s) not found '
                        f'among schematic designators (board-only, no schematic '
                        f'placement): {sorted(missing)}')

    _build_signals(pcb, layout_el, ox, oy, multi_pad_groups, copper_map)
    _warn_standalone_copper(pcb)
    return layout_el


def _warn_standalone_copper(pcb):
    """Altium stores a polygon pour's COMPUTED fill as Fill/Region records
    tagged with the owning polygon_index — those are rebuilt from the pour
    outline (pen model) and rightly ignored. A Fill/Region WITHOUT an owner
    is different: hand-placed raw copper (RF stubs, power slugs, thermal
    slabs), which this importer does not read yet — that would be a SILENT
    copper hole in the output, so it is loudly logged instead. Import
    deferred until a real board exercises it (ground-truth method), user
    decision 2026-07-27."""
    def _unowned(prim):
        return all(getattr(prim, f, None) in (None, -1)
                   for f in ('polygon_index', 'component_index'))
    fills   = [f for f in pcb.fills if _unowned(f)]
    regions = [r for r in pcb.regions if _unowned(r) and not r.is_keepout]
    for kind, lost in (('fill', fills), ('region', regions)):
        if lost:
            layers = sorted({str(p.layer) for p in lost})
            import_log.log(f'layout: {len(lost)} standalone (hand-placed, no '
                            f'owning polygon/component) copper {kind}(s) on '
                            f'Altium layer(s) {layers} NOT imported — copper '
                            f'missing from output')


# ---------------------------------------------------------------------------
# .PrjPcb -> resolved document list + sibling .IntLib
# ---------------------------------------------------------------------------

def _find_intlib(prjpcb_path):
    """Precondition #4 ([[project_altium_project_import]]): the project must
    have been run through Altium's own `Design -> Make Integrated Library`,
    producing a same-stem sibling .IntLib next to the .PrjPcb. Not found ->
    HARD REJECT with instructions, not a search across the filesystem for
    "some" IntLib — an ambiguous pick would silently resolve components
    against the wrong library."""
    candidate = prjpcb_path.with_suffix('.IntLib')
    if candidate.exists():
        return candidate
    raise ValueError(
        f'{prjpcb_path}: no sibling "{candidate.name}" found. Run '
        f'Design -> Make Integrated Library in Altium first (project '
        f'import resolves every schematic component against that compiled '
        f'library by name — see [[project_altium_project_import]] '
        f'precondition #4).')


def _read_prjpcb(path):
    """-> (.SchDoc paths in DocumentN order, single .PcbDoc path, project
    parameters).

    Project parameters are the `[ParameterN] Name=/Value=` sections — the
    global variables an Altium schematic references as `=project_title`,
    `=revision` and so on. Read from the same already-open INI rather than
    through a second parser over the same file."""
    proj_dir = Path(path).resolve().parent
    cfg = configparser.ConfigParser(strict=False)
    with open(path, encoding='utf-8-sig') as f:
        cfg.read_file(f)

    sch_docs = []
    pcb_doc = None
    params = {}
    for section in cfg.sections():
        if re.fullmatch(r'Parameter\d+', section):
            name = clean_attr_name(cfg[section].get('Name', '')).lower()
            if name:
                params[name] = cfg[section].get('Value', '')
            continue
        if not section.startswith('Document'):
            continue
        doc_path = cfg[section].get('DocumentPath')
        if not doc_path:
            continue
        p = (proj_dir / doc_path).resolve()
        ext = p.suffix.lower()
        if ext == '.schdoc':
            sch_docs.append(p)
        elif ext == '.pcbdoc':
            if pcb_doc is not None:
                raise ValueError(
                    f'{path}: multiple .PcbDoc documents referenced '
                    f'({pcb_doc.name}, {p.name}) — one-board projects only')
            pcb_doc = p
    if pcb_doc is None:
        raise ValueError(f'{path}: no .PcbDoc document referenced')
    if not sch_docs:
        raise ValueError(f'{path}: no .SchDoc document referenced')
    return sch_docs, pcb_doc, params


# ---------------------------------------------------------------------------
# Net naming: power port > net label > netlist-compiler auto name
# ---------------------------------------------------------------------------

def _net_name_lookup(sch_docs):
    """{unique_id: text} merged across every sheet, for power ports and net
    labels separately — Altium's uids are short random tokens, effectively
    globally unique across a project's documents. Overbar-converted (Altium
    backslash-per-character -> IR/Eagle `!TEXT!`) — a net's chosen NAME goes
    straight into IR/Eagle net-name text same as the label itself does, and
    must use the same notation, not Altium's raw backslashes."""
    power = {}
    label = {}
    for doc in sch_docs:
        for pp in doc.get_power_ports():
            power[pp.unique_id] = _altium_overbar_to_eagle(pp.text)
        for nl in doc.get_net_labels():
            label[nl.unique_id] = _altium_overbar_to_eagle(nl.text)
    return power, label


def _resolve_net_name(net, power_lookup, label_lookup):
    for uid in net.graphical.power_ports:
        if uid in power_lookup:
            return power_lookup[uid]
    for uid in net.graphical.labels:
        if uid in label_lookup:
            return label_lookup[uid]
    return net.name


def _altium_field_overrides(c, sym_el, inst_x_um, inst_y_um, ir_rot, ir_mirror, dx):
    """Altium per-instance Designator/Parameter placements -> IR
    <instance><text> overrides — same convention as
    kicad_project_parser._field_overrides (ir_schema.md, Eagle's "smashed"
    model): a placed schematic instance carries its OWN Designator/
    Parameter records (`c.record.children`), independent of the library
    symbol's own default placeholder position — ground-truthed on
    IND80S28 (four FID components sharing one library symbol show four
    DIFFERENT designator-label x positions: 90/120/150/180 mils). Most of
    this divergence turns out to be `auto_position=True` on the instance's
    own Designator record — Altium's OWN engine recomputing a "nice" label
    position per component ROTATION rather than rigidly rotating the
    library template's position the way Eagle/KiCad do (confirmed: nearly
    every rotated instance on this project gets an override, virtually none
    are a genuine hand-drag) — not a bug in this function, a real behavior
    mismatch between the two tools this override mechanism exists to paper
    over regardless of WHY the position differs. Emits an override only
    when the instance's record differs from the library default (position/
    rotation/size/align) or hidden-state; matching fields emit nothing (one
    fact, one place). `sym_el` is the resolved <symbol> this instance
    actually uses (gate-specific for a multi-gate
    component) — its own <text>&gt;KEY&lt;/text> children are the library
    defaults being compared against."""
    styles = {}
    for t in sym_el.findall('text'):
        s = (t.text or '').strip()
        if s.startswith('>'):
            styles[s[1:].upper()] = (
                float(t.get('x', 0)), float(t.get('y', 0)),
                float(t.get('rot', 0) or 0),
                int(t.get('size', 1270)), t.get('align', 'bottom-left'))
    out = []
    for ch in c.record.children:
        tname = type(ch).__name__
        if tname == 'AltiumSchDesignator':
            key = 'NAME'
        elif tname == 'AltiumSchParameter':
            pname = clean_attr_name(ch.name or '')
            if not pname:
                continue
            key = 'VALUE' if pname.lower() in ('value', 'comment') else pname.upper()
        else:
            continue
        lib = styles.get(key)
        if lib is None:
            continue   # not a real visible placeholder on this symbol
        if ch.is_hidden:
            out.append({'_text': f'>{key}', 'hidden': 'yes'})
            continue
        ax = (ch.location.x_mils + dx) * _MILS_TO_UM
        ay = ch.location.y_mils * _MILS_TO_UM
        arot = _ORIENT_TO_ROT.get(getattr(ch.orientation, 'value', ch.orientation), 0)
        aalign = _JUSTIFICATION.get(getattr(ch.justification, 'value', ch.justification), 'bottom-left')
        asize = _pt_to_um(ch.font.size) if ch.font else 1270

        # Inverse of eagle_exporter._emit_instance's own forward transform
        # (mirror local X, rotate by inst rot, translate) — same IR
        # placement convention used board-wide, applied backwards to
        # recover the library-relative (symbol-local) position/rotation
        # this instance's absolute record implies.
        r = math.radians(ir_rot)
        ddx, ddy = ax - inst_x_um, ay - inst_y_um
        lxm = ddx * math.cos(r) + ddy * math.sin(r)
        lym = -ddx * math.sin(r) + ddy * math.cos(r)
        lx = -lxm if ir_mirror else lxm
        ly = lym
        lrot = (ir_rot - arot) % 360 if ir_mirror else (arot - ir_rot) % 360

        lib_x, lib_y, lib_rot, lib_size, lib_align = lib
        position_matches = (abs(lx - lib_x) < 10 and abs(ly - lib_y) < 10
                             and lrot % 360 == lib_rot % 360
                             and abs(asize - lib_size) <= 1 and aalign == lib_align)
        if position_matches and not ir_mirror:
            continue
        rec = {'_text': f'>{key}'}
        if not position_matches:
            rec.update(x=str(round(lx)), y=str(round(ly)), size=str(asize),
                       align=aalign, font='vector')
            if lrot:
                rec['rot'] = _f(lrot)
        if ir_mirror:
            # Altium never letter-mirrors Designator/Parameter text, even on
            # a mirrored part — only the anchor position moves (ground-
            # truthed: X1's own AltiumSchDesignator carries is_mirrored=True
            # right alongside the component's, yet the user confirmed real
            # Altium shows it upright, not backward) — unlike Eagle's own
            # "smash" convention, where a mirrored part's attribute text
            # mirrors right along with it by default. Explicit per-field
            # `mirror='0'` tells eagle_exporter._emit_instance to keep the
            # plain "R" rot prefix instead of "MR" for this placeholder,
            # independent of whether its position also needed an override.
            rec['mirror'] = '0'
        out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Decorative sheet graphics
# ---------------------------------------------------------------------------
#
# Free-standing geometry drawn straight ON THE SHEET (not part of any placed
# symbol): the blue grouping boxes and their captions every real schematic
# uses to say "this block is the power supply". Purely decorative — no
# electrical meaning, never fed into the netlist — but NOT optional: dropping
# it silently discards real drawn content the author put there on purpose
# (12 grouping boxes + 11 captions on RoXY_Motherboard alone).
#
# IR home is already defined and already exercised by the KiCad importer:
# direct <line>/<shape>/<arc>/<text> children of <schematic>, layer GRAPHIC
# (ir_schema.md "Декоративная геометрия схемы"). The per-primitive geometry
# math is the SAME as for a symbol body, so it reuses altium_parser's own
# emit_*_geometry functions verbatim rather than growing a second, silently
# diverging implementation of the same conversions (module docstring rule).
#
# Ownership: an Altium graphic record belongs to the SHEET itself when its
# owner_index points at the header record — 0, or -1 in the older encoding —
# and to a placed component otherwise. A component's own body graphics are
# NOT re-imported here: they come from the IntLib, through the resolved
# <symbol> (library is truth, an instance's baked copy is not).
_SHEET_OWNER_INDEX = (0, -1)

_DECO_GEOMETRY = (
    ('rectangles',      emit_rect_geometry),
    ('polylines',       emit_polyline_geometry),
    ('lines',           emit_line_geometry),
    ('arcs',            emit_arc_geometry),
    ('elliptical_arcs', emit_arc_geometry),   # AltiumSchArc subclass; separate
                                              # collection on a SchDoc
    ('ellipses',        emit_ellipse_geometry),
    ('polygons',        emit_polygon_geometry),
)

# Graphic record kinds with no IR primitive to land on. Dropped with a log
# entry rather than hard-rejected: unlike a missing library component (which
# would silently corrupt the netlist), a lost decorative curve is visible,
# harmless and reported. The list mirrors exactly what _convert_symbol
# already ignores on the symbol side — same gap, one place to close later.
_DECO_UNSUPPORTED = ('rounded_rectangles', 'beziers', 'images', 'ieee_symbols')

# Altium `Note` (a sticky comment with an author) and `TextFrame` (a wrapped
# text block) are the same object shape as KiCad's `text_box`, and IR already
# has the primitive for it: <note> (decisions.md "<note> — markdown-заметки").
# Both carry exactly what a note needs — raw text plus a WIDTH — so the line
# wrapping is redone by our own renderer with our own metrics and Altium's
# own line breaking never has to be guessed at, same reasoning as the KiCad
# importer's text_box branch. Their border, when shown, is a separate
# decorative rectangle, also same as KiCad's.
_DECO_NOTES = ('notes', 'text_frames')


# Altium writes a variable reference as `=name` (a project or document
# parameter). Only a BARE IDENTIFIER is a reference we can carry: Altium also
# accepts full expressions (`= 'Sheet: ' + SheetNumber + '/' + SheetTotal`,
# real and common) and IR has no expression language to hold one. Confirmed
# against the whole Altium corpus: every expression form lives INSIDE a
# library title-block component, never on the free canvas — so the canvas
# path can stay strict without losing anything real.
_PARAM_REF_RE = re.compile(r'^=\s*([A-Za-z_][A-Za-z0-9_ ]*?)\s*$')


def _placeholder_text(raw, project_params, doc_params, sheet_label, errors):
    """Altium free text -> IR text, converting a `=name` reference into IR's
    own `>NAME` placeholder form; returns None for ordinary text.

    Resolution collapses ONE level of indirection, which is not a guess but
    Altium's own data: a document parameter routinely holds nothing but a
    reference to a project parameter (`Title` = `'=project_title'` on every
    sheet of BC2087), i.e. the document is saying "my title IS the project
    title". So `=Title` and `=project_title` name the same global and both
    become `>PROJECT_TITLE`.

    Anything that resolves to neither — an expression, an unknown name, or a
    document parameter with a genuinely per-sheet value like SheetNumber
    (which cannot live in the project-wide `<schematic><attributes>` at all)
    — is collected as a HARD REJECT rather than being silently flattened to
    a literal or left as dangling text, per the user's call on scope."""
    raw = (raw or '').strip()
    if not raw.startswith('='):
        return None
    m = _PARAM_REF_RE.match(raw)
    if not m:
        errors.append(f'{sheet_label}: "{raw}" — Altium expression, not a plain '
                       f'parameter reference; IR has no expression language')
        return None
    key = clean_attr_name(m.group(1)).lower()
    if key in project_params:
        return f'>{key.upper()}'
    indirect = _PARAM_REF_RE.match((doc_params.get(key) or '').strip())
    if indirect:
        target = clean_attr_name(indirect.group(1)).lower()
        if target in project_params:
            return f'>{target.upper()}'
    if key in doc_params:
        errors.append(f'{sheet_label}: "{raw}" refers to the DOCUMENT parameter '
                       f'"{key}" (value {doc_params[key]!r}), which is per-sheet — '
                       f'IR schematic attributes are project-wide. Promote it to a '
                       f'project parameter in Altium (Project Options -> Parameters)')
    else:
        errors.append(f'{sheet_label}: "{raw}" refers to "{key}", which is neither a '
                       f'project parameter nor a parameter of this document')
    return None


def _doc_parameters(doc):
    """Parameters owned by the SHEET itself, keyed lowercase. Filtering by
    owner matters: altium_monkey's own get_parameter_dict() merges in every
    placed component's parameters too (150+ entries of Supplier/Manufacturer
    noise on a real sheet), which are emphatically not document variables."""
    return {clean_attr_name(p.name).lower(): (p.text or '')
            for p in doc.parameters
            if getattr(p, 'owner_index', 0) in _SHEET_OWNER_INDEX and p.name}


def _deco_bounds(rec, dx):
    """Bounding box (x0, x1, y0, y1) in tiled mils, read off the record's own
    geometry fields — used only for the stray check. Circles/arcs go by
    center±radius, i.e. the full circle, which is conservative for an arc."""
    pts = getattr(rec, 'points_mils', None)
    if pts:
        xs = [p.x_mils + dx for p in pts]
        ys = [p.y_mils for p in pts]
    elif getattr(rec, 'corner_mils', None) is not None:
        xs = [rec.location_mils.x_mils + dx, rec.corner_mils.x_mils + dx]
        ys = [rec.location_mils.y_mils, rec.corner_mils.y_mils]
    elif getattr(rec, 'radius_mils', None) is not None:
        r = max(rec.radius_mils, getattr(rec, 'secondary_radius_mils', None) or 0)
        cx, cy = rec.location_mils.x_mils + dx, rec.location_mils.y_mils
        xs, ys = [cx - r, cx + r], [cy - r, cy + r]
    else:
        loc = rec.location_mils
        xs, ys = [loc.x_mils + dx], [loc.y_mils]
    return min(xs), max(xs), min(ys), max(ys)


def _sheet_graphics(doc, dx, sheet_label, project_params, ph_errors):
    """Decorative sheet geometry of one SchDoc -> [(label, bbox, [elements])].

    Built early (before anything is emitted) so the same stray-validation
    pass that covers wires/components covers this geometry too, then appended
    to <schematic> once every check has passed."""
    out = []
    doc_params = _doc_parameters(doc)

    def _text_or_placeholder(raw):
        """Free text as it should land in IR: a `=name` variable reference
        becomes IR's own `>NAME` placeholder, everything else keeps Altium's
        overbar notation converted the same way a net label's does."""
        ph = _placeholder_text(raw, project_params, doc_params, sheet_label, ph_errors)
        return ph if ph is not None else _altium_overbar_to_eagle(raw)

    def _xform(x, y):
        return x + dx, y

    for field, emit in _DECO_GEOMETRY:
        for rec in getattr(doc, field, []):
            if getattr(rec, 'owner_index', 0) not in _SHEET_OWNER_INDEX:
                continue
            holder = ET.Element('graphics')
            emit(holder, rec, xform=_xform)
            for el in holder:
                el.set('layer', 'GRAPHIC')
            if len(holder):
                out.append((f'{sheet_label} {field[:-1]}', _deco_bounds(rec, dx),
                            list(holder)))

    for lbl in getattr(doc, 'labels', []):
        if getattr(lbl, 'owner_index', 0) not in _SHEET_OWNER_INDEX:
            continue
        holder = ET.Element('graphics')
        el = emit_label_geometry(holder, lbl, xform=_xform, layer='GRAPHIC')
        if el is None:
            continue
        el.text = _text_or_placeholder(el.text)
        out.append((f'{sheet_label} text "{(lbl.text or "")[:20]}"',
                    _deco_bounds(lbl, dx), [el]))

    # Sheet symbols — under the FLATTEN policy (see convert_project's
    # hierarchy guard) the hierarchy they encode is dissolved into the flat
    # netlist, but the DRAWING is real authored content: the top-level page
    # IS the block diagram, and dropping the blocks left it an empty frame.
    # Rectangle + per-entry connection names become plain GRAPHIC geometry;
    # the sheet-name/file-name captions ride along via their own label
    # records below (they are ordinary label subclasses with their own
    # locations). x_size/y_size are in 10-mil sheet units (ground truth
    # BC2087: CONNECTOR_B y_size=380 vs its entries spanning 3700 mils);
    # location is the TOP-left corner (Y-up).
    for ss in getattr(doc, 'sheet_symbols', []):
        holder = ET.Element('graphics')
        loc = ss.location_mils
        w, h = ss.x_size * 10.0, ss.y_size * 10.0
        x0, x1, top = loc.x_mils + dx, loc.x_mils + dx + w, loc.y_mils
        ET.SubElement(holder, 'shape', x=str(_um((x0 + x1) / 2)),
                      y=str(_um(top - h / 2)), w=str(_um(w)), h=str(_um(h)),
                      roundness='0', rot='0',
                      outline=str(_um_lw(ss.line_width)), layer='GRAPHIC')
        for e in getattr(doc, 'sheet_entries', []):
            if e.parent is not ss:
                continue
            d_mils = e.distance_from_top_mils
            # side: 0=left 1=right 2=top 3=bottom (only 0/1 seen in the
            # corpus; 2/3 mapped by the same edge logic, unverified).
            margin = 40
            if e.side == 0:
                ex, ey, al = x0 + margin, top - d_mils, 'center-left'
            elif e.side == 1:
                ex, ey, al = x1 - margin, top - d_mils, 'center-right'
            elif e.side == 2:
                ex, ey, al = x0 + d_mils, top - margin, 'top-left'
            else:
                ex, ey, al = x0 + d_mils, top - h + margin, 'bottom-left'
            t_el = ET.SubElement(holder, 'text', x=str(_um(ex)), y=str(_um(ey)),
                                  size=str(_pt_to_um(e.font.size) if e.font else 1270),
                                  rot='0', align=al, layer='GRAPHIC')
            t_el.text = _altium_overbar_to_eagle(e.name or '')
        nm = ss.sheet_name.text if ss.sheet_name is not None else '?'
        out.append((f'{sheet_label} sheet symbol "{nm}"',
                    (x0, x1, top - h, top), list(holder)))
    for coll in ('sheet_names', 'file_names'):
        for rec in getattr(doc, coll, []):
            holder = ET.Element('graphics')
            el = emit_label_geometry(holder, rec, xform=_xform, layer='GRAPHIC')
            if el is None:
                continue
            out.append((f'{sheet_label} {coll[:-1]} "{(rec.text or "")[:20]}"',
                        _deco_bounds(rec, dx), [el]))

    for field in _DECO_NOTES:
        for rec in getattr(doc, field, []):
            if getattr(rec, 'owner_index', 0) not in _SHEET_OWNER_INDEX:
                continue
            if getattr(rec, 'is_hidden', False):
                continue
            x0, x1, y0, y1 = _deco_bounds(rec, dx)
            holder = ET.Element('graphics')
            # An EMPTY text frame is not an empty object: its BORDER is real
            # drawn content. Ground truth — BC2087's Project_Information
            # sheet builds its whole revision-history TABLE out of 20 frames
            # of which only 6 carry text; the other 14 are the blank cells,
            # and skipping them on "no text" erased most of the table's grid
            # (found by the user: "до орла доходит только несколько из них").
            # So the note and the border are decided independently.
            if rec.text:
                # IR anchors a note at its TOP-left corner (kicad_project_parser's
                # own note branch, and kicad_project_exporter reads it back the
                # same way); Altium's location/corner pair is a plain rectangle
                # with no guaranteed corner order, hence min/max rather than
                # assuming which of the two is which.
                n_el = ET.SubElement(holder, 'note', x=str(_um(x0)), y=str(_um(y1)),
                                     w=str(_um(x1 - x0)), rot='0')
                n_el.text = _text_or_placeholder(rec.text)
            if getattr(rec, 'show_border', False):
                ET.SubElement(holder, 'shape', x=str(_um((x0 + x1) / 2)),
                              y=str(_um((y0 + y1) / 2)), w=str(_um(x1 - x0)),
                              h=str(_um(y1 - y0)), roundness='0', rot='0',
                              outline=str(_um_lw(rec.line_width)),
                              layer='GRAPHIC')
            if len(holder):
                out.append((f'{sheet_label} note "{(rec.text or "")[:20]}"',
                            (x0, x1, y0, y1), list(holder)))

    for field in _DECO_UNSUPPORTED:
        n = sum(1 for r in getattr(doc, field, [])
                if getattr(r, 'owner_index', 0) in _SHEET_OWNER_INDEX)
        if n:
            import_log.log(f'{sheet_label}: {n} decorative {field} dropped — '
                            f'no IR primitive for this Altium graphic kind')
    return out


def _consistency_check(pcb, netlist, prj_name):
    """Source-side schematic<->board consistency preflight (user decision
    2026-07-26: LOUD WARNING, not a reject — the schematic stays the truth
    and the import proceeds, but every divergence is named in full).

    Comparison is by PAD SET — each net reduced to its {(designator, pad)}
    and the two partitions compared as sets of sets. This is deliberately
    NOT a name comparison: names are blind to a net being split or fused,
    which are exactly the real defects (ground truth BC2087: a net-label
    typo SPI_BNO085_/SPI_BNO08x_ leaving two one-pin nets where the board
    has one — invisible to name matching, since each half is individually
    legal).

    Both sides keep every pad group INCLUDING single-pad ones, mirroring
    exactly what the export carries — the check's job is to predict Eagle's
    pair-consistency verdict line for line (found the hard way: the first
    version filtered <2-pad nets and missed 3 of Eagle's 13 errors on
    BC2087, `None / NAME` mismatches on one-pad board nets). Single-pin
    nets are imported into the schematic since 2026-07-26 (see the net
    loop), so the two sides are symmetric again."""
    comp = {i: c.designator for i, c in enumerate(pcb.components)}
    board_pads = {}
    for p in pcb.pads:
        ni = getattr(p, 'net_index', None)
        ci = getattr(p, 'component_index', None)
        if ni is None or ni < 0 or ci not in comp:
            continue
        board_pads.setdefault(ni, set()).add((comp[ci].upper(), str(p.designator).upper()))
    board_name = {i: n.name for i, n in enumerate(pcb.nets)}
    board = {frozenset(v): board_name.get(k, '?')
             for k, v in board_pads.items()}
    sch = {}
    for n in netlist.nets:
        s = frozenset((t.designator.upper(), str(t.pin).upper()) for t in n.terminals)
        if s:
            sch[s] = n.name
    if board.keys() == sch.keys():
        return
    def _fmt(pads):
        return ', '.join(f'{d}.{p}' for d, p in sorted(pads))
    for s in sorted(board.keys() - sch.keys(), key=lambda s: board[s]):
        import_log.log(f'CONSISTENCY: board net "{board[s]}" has no schematic '
                        f'equivalent — its pads [{_fmt(s)}] group differently in '
                        f'the compiled schematic')
    for s in sorted(sch.keys() - board.keys(), key=lambda s: sch[s]):
        import_log.log(f'CONSISTENCY: schematic net "{sch[s]}" has no board '
                        f'equivalent — its pads [{_fmt(s)}] group differently on '
                        f'the board')
    n_diff = len(board.keys() ^ sch.keys())
    import_log.log(f'CONSISTENCY: {prj_name}: schematic and board disagree on '
                    f'{n_diff} net grouping(s) (see lines above) — the SOURCE '
                    f'project is out of sync; the schematic was imported as '
                    f'truth. Re-sync the PCB in Altium (Design -> Import '
                    f'Changes) to clear this.')


# ---------------------------------------------------------------------------
# Top-level conversion
# ---------------------------------------------------------------------------

def convert_project(prjpcb_path, output_path):
    prjpcb_path = Path(prjpcb_path)
    output_path = Path(output_path)

    intlib_path = _find_intlib(prjpcb_path)
    models_dir = output_path.parent / output_path.stem

    sch_paths, pcb_path, project_params = _read_prjpcb(prjpcb_path)
    sch_docs = [AltiumSchDoc(str(p)) for p in sch_paths]

    # ------------------------------------------------------------------
    # Hierarchy guard (user decision, 2026-07-26). Policy: hierarchy is
    # FLATTENED — every sheet joins one flat canvas and connectivity comes
    # from the netlist compiler under the everything-is-global assumption
    # (empirically the BEST scope against Altium's own compiled boards:
    # 97.2% of 6128 nets across the 50-project corpus, better than the
    # "correct" hierarchical modes on 30 projects, worse on none). The two
    # cases flattening cannot express are hard rejects, not degradations:
    #  - a sheet FILE referenced by 2+ sheet symbols is a real multichannel
    #    module — flattening would collide every designator; the module
    #    import path is deferred until a real multichannel project exists
    #    to test against (none in the 344-sheet-symbol corpus);
    #  - REPEAT(...) stacked sheet symbols are that same multichannel in
    #    Altium's compressed drawing form.
    # ------------------------------------------------------------------
    seen_child = {}
    hier_errors = []
    for doc, path in zip(sch_docs, sch_paths):
        for ss in doc.sheet_symbols:
            nm = ss.sheet_name.text if ss.sheet_name is not None else '?'
            if ss.is_multichannel():
                hier_errors.append(
                    f'{Path(path).name}: sheet symbol "{nm}" uses REPEAT(...) '
                    f'multichannel stacking — not expressible in IR')
            fn = (ss.file_name.text if ss.file_name is not None else '').lower()
            if not fn:
                continue
            if fn in seen_child:
                hier_errors.append(
                    f'sheet "{fn}" is instanced more than once '
                    f'({seen_child[fn]} and {Path(path).name}:"{nm}") — a repeated '
                    f'sheet is a multichannel module, which this importer does '
                    f'not support yet; flatten it in Altium or drop a channel')
            else:
                seen_child[fn] = f'{Path(path).name}:"{nm}"'
    if hier_errors:
        raise ValueError(f'{prjpcb_path.name}: unsupported hierarchy:\n  '
                          + '\n  '.join(sorted(set(hier_errors))))

    # Symbol<->footprint association comes from the SCHEMATIC, not the
    # library: Altium has no real library-level "device" (same as KiCad —
    # the pairing lives on the placed instance), so the IntLib supplies the
    # symbols and the footprints while the schematic says which go together.
    # Keyed by BOTH names an instance can carry, since which one the IntLib
    # stores the component under varies ([[project_altium_design_item_id]]);
    # harmless when they're equal, and a key that matches no component is
    # simply never looked up.
    footprint_usage = {}
    for doc in sch_docs:
        for c in doc.get_components():
            if not c.footprint:
                continue
            for key in (c.library_ref, getattr(c.record, 'design_item_id', None)):
                if key:
                    footprint_usage.setdefault(key, set()).add(c.footprint)

    lib_el, orig_to_compel, pin_des_by_comp = convert_to_tree(
        str(intlib_path), models_dir, footprint_usage=footprint_usage)

    # One printable Frame per sheet, tiled left-to-right (same convention as
    # kicad_project_parser.py's own page tiling, _GAP_MILS = its _GAP_MM).
    # Size is real structural data (doc.sheet.get_sheet_size_mils()), same
    # certainty class as KiCad's sch.paper — see [[project_altium_project_parser_decisions]]
    # "Рамка". No decorative stamp/title-block graphics (explicit user call).
    doc_dx, doc_size_mils = [], []
    x_shift = 0.0
    for doc in sch_docs:
        doc_dx.append(x_shift)
        wh = doc.sheet.get_sheet_size_mils()
        doc_size_mils.append(wh)
        x_shift += wh[0] + _GAP_MILS

    # Stray-validation (decisions.md "Stray-валидация", same rule as the
    # KiCad importer): every object must fit ENTIRELY inside its own
    # sheet's frame, not just touch it — collected as one atomic hard
    # reject, alongside the by-name resolution checks below.
    strays = []

    def _stray_check(dx, wh, label, x0, x1, y0, y1):
        fx0, fx1, fy0, fy1 = dx, dx + wh[0], 0.0, wh[1]
        if x0 < fx0 or x1 > fx1 or y0 < fy0 or y1 > fy1:
            strays.append(f'{label}: bbox x[{x0:g},{x1:g}] y[{y0:g},{y1:g}] mils '
                           f'vs frame x[{fx0:g},{fx1:g}] y[{fy0:g},{fy1:g}] mils')

    # Decorative sheet geometry, built up-front so it goes through the same
    # stray check as everything else (see _sheet_graphics).
    deco_els = []
    ph_errors = []
    for doc, path, dx, wh in zip(sch_docs, sch_paths, doc_dx, doc_size_mils):
        for label, (x0, x1, y0, y1), els in _sheet_graphics(
                doc, dx, Path(path).stem, project_params, ph_errors):
            _stray_check(dx, wh, label, x0, x1, y0, y1)
            deco_els.extend(els)
    if ph_errors:
        raise ValueError(
            f'{prjpcb_path.name}: {len(ph_errors)} text placeholder(s) on the '
            f'schematic canvas cannot be resolved to a project parameter:\n  '
            + '\n  '.join(ph_errors))

    for doc, dx, wh in zip(sch_docs, doc_dx, doc_size_mils):
        for w in doc.wires:
            for p in w.points_mils:
                _stray_check(dx, wh, f'wire {w.unique_id}', p.x_mils + dx, p.x_mils + dx,
                             p.y_mils, p.y_mils)
        for j in doc.junctions:
            loc = j.location_mils
            _stray_check(dx, wh, 'junction', loc.x_mils + dx, loc.x_mils + dx, loc.y_mils, loc.y_mils)
        for nl in doc.get_net_labels():
            loc = nl.record.location_mils
            _stray_check(dx, wh, f'net label "{nl.text}"', loc.x_mils + dx, loc.x_mils + dx,
                         loc.y_mils, loc.y_mils)
        for pp in doc.get_power_ports():
            loc = pp.record.location_mils
            _stray_check(dx, wh, f'power port "{pp.text}"', loc.x_mils + dx, loc.x_mils + dx,
                         loc.y_mils, loc.y_mils)

    # Resolve EVERY placed component against the library BY NAME before
    # building anything — atomic hard reject (module docstring), not a
    # partial project with some components silently missing.
    # A placed instance carries TWO names for its device, and which one the
    # compiled IntLib stores the component under depends on where the part
    # came from: `library_ref` is the human-readable source-library name
    # ("TI-TPS54331D8"), `design_item_id` is the catalogue/Vault identity
    # ("CMP-0323-00209-3"). A plain hand-drawn SchLib part has them equal, so
    # library_ref alone worked on IND80S28/8AO-VI — but an Altium Content
    # Vault part is compiled into the IntLib under its design_item_id, and
    # matching only library_ref reported 58 of RoXY_Motherboard's components
    # as "missing" while they were physically right there in the library
    # (caught by the user: "я открыл интлиб и вижу тут кучу компонентов...
    # они физически попали в интлиб"). Try the readable name first (keeps
    # every existing project resolving byte-identically), then the catalogue
    # id — still a strict by-name resolve against the library, no guessing.
    def _resolve(c):
        comp_el = orig_to_compel.get(c.library_ref)
        if comp_el is None:
            comp_el = orig_to_compel.get(getattr(c.record, 'design_item_id', None))
        return comp_el

    placements = []   # (designator, SchComponentInfo, dx) — one per PLACED PART
    missing = set()
    for doc, dx, wh in zip(sch_docs, doc_dx, doc_size_mils):
        for c in doc.get_components():
            placements.append((c.designator, c, dx))
            if _resolve(c) is None:
                missing.add((c.designator, c.library_ref))
            b = c.full_bounds_mils()
            _stray_check(dx, wh, c.designator, min(b.x1_mils, b.x2_mils) + dx,
                         max(b.x1_mils, b.x2_mils) + dx, min(b.y1_mils, b.y2_mils),
                         max(b.y1_mils, b.y2_mils))
    if strays:
        raise ValueError(
            f'{prjpcb_path.name}: {len(strays)} object(s) extend outside their '
            f'sheet\'s printable frame — every object must fit entirely inside '
            f'it, not just touch it. Move it inside the frame (or enlarge the '
            f'sheet) in Altium and re-export:\n  ' + '\n  '.join(sorted(strays)))
    if missing:
        names = ', '.join(f'{d} ({r})' for d, r in sorted(missing))
        raise ValueError(
            f'{prjpcb_path.name}: {len(missing)} schematic component(s) not '
            f'found in {intlib_path.name} by name: {names}. Rebuild the '
            f'Integrated Library (Design -> Make Integrated Library) so it '
            f'covers every component actually placed on the schematic.')

    # designator -> its sheet's tile offset, needed below to bucket a net's
    # <segment>s one-per-sheet (a multi-sheet global net's pinrefs must land
    # in the segment for the sheet that part is actually on).
    designator_dx = {}
    for designator, _c, dx in placements:
        designator_dx.setdefault(designator, dx)

    # A component with 2+ <footprint> variants (typically a generic-merged
    # one like "C" — several catalog capacitor packages sharing one symbol)
    # needs the SAME by-name resolve + hard-reject discipline as the
    # component lookup above: which variant a given instance actually uses
    # is real data (the schematic's own per-instance footprint string),
    # never a silent "pick the first one" — that exact failure mode is a
    # documented past incident in eagle_exporter.py (a resistor silently
    # exported as a fuse holder, testData/vimdrones.zip "R6"/"R11").
    bad_footprint = set()
    for designator, c, _dx in placements:
        comp_el = _resolve(c)
        fp_names = {fp.get('name') for fp in comp_el.findall('footprint')}
        if len(fp_names) > 1 and c.footprint not in fp_names:
            bad_footprint.add((designator, c.footprint, comp_el.get('name')))
    if bad_footprint:
        names = ', '.join(f'{d}: "{fp}" not in {cn}' for d, fp, cn in sorted(bad_footprint))
        raise ValueError(
            f'{prjpcb_path.name}: {len(bad_footprint)} instance(s) reference a '
            f'footprint variant not found on their resolved library component: '
            f'{names}. Rebuild the Integrated Library so its footprint variants '
            f'match what the schematic actually places.')

    # The netlist compiler's Terminal only carries the bare Altium pin NAME
    # ("GND" on all three ground pins of a connector alike) — not usable
    # directly as an IR pinref: it collides exactly where the library's own
    # <pin-mapping> already disambiguates (GND/GND@2/GND@3) and, for a
    # multi-gate component, already gate-qualifies (GATE.PIN, ir_schema
    # <pin-mapping> format — see altium_parser.py's own pin-mapping
    # emission). Terminal.pin (the pin NUMBER/designator, e.g. "B1") is
    # exactly the pin-mapping's `pad=` key — build (designator, pad) ->
    # disambiguated ir_pin_name straight from each instance's OWN resolved
    # footprint variant's <pin-mapping>, instead of re-deriving
    # disambiguation/gate-qualification a second, possibly-diverging way.
    pad_to_pin = {}
    # (designator, raw Altium pad designator) -> every disambiguated IR pad
    # name sharing it — usually just [raw], but a raw designator shared by
    # two physical pads on different layers (see altium_parser.py's
    # _convert_footprint, "ME BUS FE CONTACT" ground truth) needs a
    # <contactref> for EACH of them, not only the first. A <map>'s space-
    # joined pad list already IS this group — its first entry is always the
    # raw designator unchanged (_convert_footprint's own convention: first
    # occurrence keeps the raw name, later ones get raw+letter).
    multi_pad_groups = {}
    for designator, c, _dx in placements:
        comp_el = _resolve(c)
        fps = comp_el.findall('footprint')
        fp_el = next((fp for fp in fps if fp.get('name') == c.footprint), fps[0] if fps else None)
        if fp_el is None:
            continue
        pm_el = fp_el.find('pin-mapping')
        if pm_el is None:
            continue
        for m in pm_el.findall('map'):
            pads = m.get('pad', '').split()
            if len(pads) > 1:
                multi_pad_groups[(designator, pads[0])] = pads
        # Netlist terminals identify a pin by its DESIGNATOR, so resolve
        # through the library's own designator->IR-name table rather than
        # via the pad name: the two are only incidentally equal and an
        # explicit MAP_DEFINER breaks that outright (altium_parser.
        # _explicit_pin_map, AMS1117 pin '4' -> pad '2').
        for key in (c.library_ref, getattr(c.record, 'design_item_id', None)):
            for pin_des, ir_name in (pin_des_by_comp.get(key) or {}).items():
                pad_to_pin[(designator, pin_des)] = ir_name

    compiler = AltiumNetlistMultiSheetCompiler(sch_docs, None, NetlistOptions())
    netlist = compiler.build()
    power_lookup, label_lookup = _net_name_lookup(sch_docs)

    proj_el = ET.Element('project', name=prjpcb_path.stem)
    symbols_el = lib_el.find('symbols')
    proj_el.append(symbols_el)
    for comp_el in lib_el.findall('component'):
        proj_el.append(comp_el)
    schematic_el = ET.SubElement(proj_el, 'schematic')

    # Project-wide variables (.PrjPcb `[ParameterN]`) -> schematic-level
    # <attributes>, the same key->value shape <component>/<footprint>
    # already use. This is what the canvas `>NAME` placeholders resolve
    # against (_placeholder_text), and it maps 1:1 onto Eagle's own
    # <schematic><attributes> (eagle.dtd) — no new concept on either side.
    if project_params:
        attrs_el = ET.SubElement(schematic_el, 'attributes')
        for key in sorted(project_params):
            ET.SubElement(attrs_el, 'attr', name=key, value=project_params[key])

    # One Frame instance per sheet, at its own tile's center — origin
    # convention matches the shape's own local center in _build_frame_component.
    for i, (dx, wh, path) in enumerate(zip(doc_dx, doc_size_mils, sch_paths), 1):
        frame_name = _build_frame_component(*wh, symbols_el, proj_el)
        frame_el = ET.SubElement(schematic_el, 'instance', component=frame_name,
                                  library=prjpcb_path.stem, name=f'FRAME{i}',
                                  x=str(_um(dx + wh[0] / 2)), y=str(_um(wh[1] / 2)),
                                  rot='0', mirror='0')
        # Sheet name = the document's own FILE NAME (user's call). The other
        # two candidates are worse: the document's `Title` parameter is a
        # formula reference on real projects, and the parent sheet symbol's
        # SheetName only exists in a hierarchical project — the file name is
        # present and unique in both, so one rule covers every project.
        ET.SubElement(frame_el, 'attr', name='sheet', value=Path(path).stem)

    # Decorative sheet geometry, straight after the frames — behind the
    # placed parts, same drawing order as on the Altium sheet.
    for el in deco_els:
        schematic_el.append(el)

    _build_power_symbols(symbols_el, proj_el)

    for designator, c, dx in placements:
        comp_el = _resolve(c)
        is_multi_gate = comp_el.find('gate') is not None
        loc = c.record.location_mils
        inst_el = ET.SubElement(schematic_el, 'instance',
                                 component=comp_el.get('name'), library=intlib_path.stem,
                                 name=designator,
                                 x=str(_um(loc.x_mils + dx)), y=str(_um(loc.y_mils)),
                                 rot=str(int(c.record.orientation * 90)),
                                 mirror='1' if c.record.is_mirrored else '0')
        if is_multi_gate:
            gate_letter = _part_letter(c.record.current_part_id)
            inst_el.set('gate', gate_letter)
            gate_el = comp_el.find(f'gate[@name="{gate_letter}"]')
            sym_name = gate_el.get('symbol') if gate_el is not None else None
        else:
            sym_name = comp_el.get('symbol')
        sym_el = symbols_el.find(f'symbol[@name="{sym_name}"]') if sym_name else None
        if sym_el is not None:
            overrides = _altium_field_overrides(
                c, sym_el, float(inst_el.get('x')), float(inst_el.get('y')),
                float(inst_el.get('rot')), inst_el.get('mirror') == '1', dx)
            for tkw in overrides:
                t_el = ET.SubElement(inst_el, 'text')
                t_el.text = tkw.pop('_text')
                for k, v in tkw.items():
                    t_el.set(k, v)
        if len({fp.get('name') for fp in comp_el.findall('footprint')}) > 1:
            inst_el.set('footprint', c.footprint)
        # Same normalization as altium_parser.py's own instance-attr writer
        # (line ~742): lowercase, and Value/Comment (Altium has two
        # parameters that both mean "device value") collapse onto the
        # single canonical 'value' key eagle_exporter/ir_util's
        # resolved_attrs() looks up verbatim for the part's Value field and
        # >VALUE placeholder resolution. Unlike the library side (which
        # never sees both at once — generic components skip both, non-
        # generic ones only ever define one), a PLACED instance routinely
        # carries both, and Comment is very often literally the formula
        # string "=Value" (Altium's own "mirror whatever Value says"
        # convention, confirmed on R3: Value='4.7k', Comment='=Value') —
        # not real text to display. Collect into a dict first so the two
        # never collide as duplicate <attr name="value"> siblings; prefer
        # whichever isn't a bare formula reference.
        inst_attrs = {}
        for p in c.parameters or []:
            if not p.text:
                continue
            pname = clean_attr_name(p.name)
            if not pname:
                # Source data defect, not an import gap — ground-truth on
                # 8AO-VI: 10 instances (C25/C26/C59/C60/C61/C95/C96/C97/
                # C101/C102) carry a parameter with a genuinely blank NAME
                # in the raw schematic (value "C1206C102KGRACTU", looks like
                # an intended manufacturer-part-number field whose Name got
                # emptied). IR requires a non-empty <attr name> — dropped
                # per user decision rather than inventing a name for it.
                import_log.log(f'{designator}: parameter with blank name '
                                f'(value {p.text!r}) dropped — source data defect')
                continue
            key = 'value' if pname.lower() in ('value', 'comment') else pname.lower()
            if key not in inst_attrs or inst_attrs[key].startswith('='):
                inst_attrs[key] = p.text
        for key, value in inst_attrs.items():
            ET.SubElement(inst_el, 'attr', name=key, value=str(value))

    # Wire/junction geometry is NOT decorative (unlike symbol-body graphics,
    # decision #4) — it's the actual electrical connection in Eagle. A
    # <segment> with only <pinref>, no <wire> touching those pin positions,
    # left every part visually disconnected (confirmed opening in real
    # Eagle: components placed correctly, "никак не соединены"). The real,
    # already-drawn wire geometry is right there in the source schematic —
    # draw it instead of only recording which pins are electrically equal.
    # `net.graphical.junctions` is ALWAYS empty in altium_monkey's compiler
    # (confirmed: 0 across every net vs 82 real AltiumSchJunction objects on
    # this schematic, while `.wires` resolves fully — an uncovered field in
    # the compiler, not a gap in Altium's own file format: junction
    # coordinates ARE stored, `doc.junctions[i].location_mils` reads them
    # straight from the source). Recovered by EXACT geometric match instead
    # of guessing: a junction belongs to whichever net's just-emitted wire
    # endpoints land on its coordinate — unambiguous, not a heuristic (every
    # real junction on this schematic sits exactly at a wire endpoint, see
    # memory; a junction matching no net's wire endpoints is simply not
    # emitted, same as it wouldn't have been before).
    # (wire/junction, dx) — dx tags which sheet's tile the RAW mils
    # coordinate needs shifting into (matching is done in the doc's own raw
    # coordinate space below, dx only applied when finally written to XML).
    wire_by_uid = {}
    all_junctions = []
    net_label_by_uid = {}
    power_port_by_uid = {}
    for doc, dx in zip(sch_docs, doc_dx):
        for w in doc.wires:
            wire_by_uid[w.unique_id] = (w, dx)
        all_junctions.extend((j, dx) for j in doc.junctions)
        for nl in doc.get_net_labels():
            net_label_by_uid[nl.unique_id] = (nl, dx)
        for pp in doc.get_power_ports():
            power_port_by_uid[pp.unique_id] = (pp, dx)
    port_by_uid = {}
    for doc, dx in zip(sch_docs, doc_dx):
        for p in doc.ports:
            port_by_uid[p.unique_id] = (p, dx)

    # Every power port gets a REAL placed <instance>, unconditionally — it's
    # a real object in the source schematic (per user: dropping a floating
    # one silently would make it vanish from the model entirely, not just
    # from connectivity — the object stays, only its <pinref> is
    # conditional on actually belonging to a net).
    power_designator = {}
    pwr_counter = 0
    for uid, (pp, pdx) in power_port_by_uid.items():
        positive = pp.record.style in _POWER_POSITIVE_STYLES
        sym_name = _PWR_POS_NAME if positive else _PWR_NEG_NAME
        pwr_counter += 1
        desig = f'#PWR{pwr_counter}'
        power_designator[uid] = desig
        loc = pp.record.location_mils
        # Quarter-turn correction confirmed by the user against real Altium
        # — the power port's own `orientation` field is offset from what
        # _ORIENT_TO_ROT's usual mapping (already correct elsewhere, e.g.
        # net labels) gives, and the SIGN of the offset differs by polarity:
        # sup/rail-style ports (VCC) need -90°, ground-style ports need +90°.
        base_rot = _ORIENT_TO_ROT.get(getattr(pp.record.orientation, 'value', pp.record.orientation), 0)
        rot = (base_rot + (-90 if positive else 90)) % 360
        inst_el = ET.SubElement(schematic_el, 'instance', component=sym_name,
                                 library=prjpcb_path.stem, name=desig,
                                 x=str(_um(loc.x_mils + pdx)), y=str(_um(loc.y_mils)),
                                 rot=str(rot), mirror='0')
        # >VALUE on the synthesized symbol resolves from this — the REAL net
        # name the power port carried in the source, not the fixed generic
        # pin name ('VCC'/'GND').
        ET.SubElement(inst_el, 'attr', name='value', value=_altium_overbar_to_eagle(pp.text))

    used_power_uids = set()
    used_port_uids = set()
    for net in netlist.nets:
        # SINGLE-PIN NETS ARE IMPORTED (user decision 2026-07-26, reversing
        # the earlier NetlistSinglePinNets=0-mirroring drop): a one-pin net
        # in the source is usually an authoring mistake, but flagging
        # mistakes is ERC's job, not the converter's — the converter's job
        # is to carry the source faithfully, absurdity included. Concretely
        # this also closes the whole `None / NAME` class of Eagle pair-
        # consistency errors (BC2087 J2.65/66/68, RoXY ENC1_*/CONFIG: the
        # board names these one-pad nets, and the schematic used to arrive
        # with the pin bare). Only a net with literally nothing to
        # reference — no component pin AND no power port (power ports
        # aren't Terminal objects in the compiler's model, counted
        # separately) — is skipped, logged: it has no <pinref> to emit.
        if len(net.terminals) + len(net.graphical.power_ports) == 0:
            import_log.log(f'net "{net.name}" has no pins and no power ports '
                            f'— nothing to reference, dropped')
            continue
        name = _resolve_net_name(net, power_lookup, label_lookup)
        net_el = ET.SubElement(schematic_el, 'net', name=clean_attr_name(name))
        # A global net's wires/labels/pinrefs can span several sheets (tied
        # together only by matching label text, per Eagle's own model — see
        # [[feedback_labels_on_wire_ends]]/net-label-as-glue). Eagle requires
        # one <segment> per printable page (eagle_exporter._segment_sheet
        # already hard-rejects a segment whose pinrefs land on different
        # sheets) — bucket everything below by its sheet's tile offset (dx)
        # instead of dumping the whole net into one <segment>. Ground-truth
        # bug found on 8AO-VI (4-sheet project): a single-segment net whose
        # wires lived on two different sheets tripped exactly that reject.
        segs_by_dx = {}

        def _seg(dx):
            seg_el = segs_by_dx.get(dx)
            if seg_el is None:
                seg_el = ET.SubElement(net_el, 'segment')
                segs_by_dx[dx] = seg_el
            return seg_el

        wire_endpoints_by_dx = {}
        for uid in net.graphical.wires:
            entry = wire_by_uid.get(uid)
            if entry is None:
                continue
            w, dx = entry
            seg_el = _seg(dx)
            pts = list(w.points_mils)
            for a, b in zip(pts, pts[1:]):
                ET.SubElement(seg_el, 'line',
                              x1=str(_um(a.x_mils + dx)), y1=str(_um(a.y_mils)),
                              x2=str(_um(b.x_mils + dx)), y2=str(_um(b.y_mils)),
                              width=str(_um_lw(w.line_width)))
            endpoints = wire_endpoints_by_dx.setdefault(dx, set())
            for p in pts:
                endpoints.add((round(p.x_mils, 3), round(p.y_mils, 3)))
        for j, jdx in all_junctions:
            loc = j.location_mils
            if (round(loc.x_mils, 3), round(loc.y_mils, 3)) in wire_endpoints_by_dx.get(jdx, ()):
                seg_el = _seg(jdx)
                ET.SubElement(seg_el, 'junction', x=str(_um(loc.x_mils + jdx)), y=str(_um(loc.y_mils)))
        # The instance itself was already placed unconditionally above —
        # only the <pinref> (this net actually owns this power port) is
        # conditional here.
        for uid in net.graphical.power_ports:
            entry = power_port_by_uid.get(uid)
            if entry is None:
                continue
            used_power_uids.add(uid)
            pp, pdx = entry
            seg_el = _seg(pdx)
            pin_name = 'VCC' if pp.record.style in _POWER_POSITIVE_STYLES else 'GND'
            ET.SubElement(seg_el, 'pinref', part=power_designator[uid], pin=pin_name)
        # Net labels — ALWAYS flag-style (`style="passive"`) regardless of
        # role, per the user (2026-07-21): Altium itself has no shape/style
        # concept at all for a net label (always bare text — the exact
        # anti-pattern [[feedback_labels_need_flags]] warns against
        # replicating, "net label рядом с проводом, но не прицеплен"), and
        # computing the real role (connectivity glue vs a caption on an
        # already-continuous wire) is deferred as shared work across every
        # CAD import path, not an Altium-specific problem to solve now.
        for uid in net.graphical.labels:
            entry = net_label_by_uid.get(uid)
            if entry is None:
                continue
            nl, ldx = entry
            seg_el = _seg(ldx)
            loc = nl.record.location_mils
            rot = _ORIENT_TO_ROT.get(getattr(nl.record.orientation, 'value', nl.record.orientation), 0)
            align = _JUSTIFICATION.get(getattr(nl.record.justification, 'value', nl.record.justification), 'bottom-left')
            size = _pt_to_um(nl.record.font.size) if nl.record.font else 1270
            lbl_el = ET.SubElement(seg_el, 'label',
                                    x=str(_um(loc.x_mils + ldx)), y=str(_um(loc.y_mils)),
                                    size=str(size), rot=str(rot), align=align,
                                    layer='NETS', style='passive')
            lbl_el.text = _altium_overbar_to_eagle(nl.text)
        # Ports — under the flatten policy a port is just one more global
        # net identifier (exactly what NetIdentifierScope.GLOBAL treats it
        # as), so it lands as the same flag-style <label> a net label does:
        # without it the wire that ended at the port ends at bare nothing.
        # A port uid is never referenced by two different nets (verified on
        # BC2087: 0 of 120), only duplicated WITHIN one net's list — hence
        # the set(). Harness-typed ports never appear here at all (their
        # member signals resolve individually; 0 of 45 on BC2087) — they
        # are counted and logged as dropped drawing after this loop.
        for uid in set(net.graphical.ports):
            entry = port_by_uid.get(uid)
            if entry is None:
                continue
            used_port_uids.add(uid)
            p, pdx = entry
            seg_el = _seg(pdx)
            loc = p.location_mils
            lbl_el = ET.SubElement(seg_el, 'label',
                                    x=str(_um(loc.x_mils + pdx)), y=str(_um(loc.y_mils)),
                                    size=str(_pt_to_um(p.font.size) if p.font else 1270),
                                    rot='0', align='bottom-left',
                                    layer='NETS', style='passive')
            lbl_el.text = _altium_overbar_to_eagle(p.name or '')
        for t in net.terminals:
            dx = designator_dx.get(t.designator, 0)
            seg_el = _seg(dx)
            pin_name = pad_to_pin.get((t.designator, t.pin))
            if pin_name is None:
                # No footprint pad for this pin (rare — a symbol-only pin
                # with nothing to mount) — fall back to the raw Altium pin
                # name/number; logged since it means the pin's real IR name
                # (disambiguated/gate-qualified) couldn't be recovered.
                pin_name = t.pin_name or t.pin
                import_log.log(f'{t.designator}: pin "{t.pin}" not in its '
                                f'footprint pin-mapping, using raw name "{pin_name}"')
            ET.SubElement(seg_el, 'pinref', part=t.designator, pin=clean_attr_name(pin_name))

    n_harness = sum(1 for p, _ in port_by_uid.values() if p.harness_type)
    if n_harness:
        import_log.log(f'{n_harness} harness-typed port(s) dropped as drawing — '
                        f'their member signals are named individually and their '
                        f'connectivity is already in the flat netlist')
    for uid, (p, pdx) in port_by_uid.items():
        if uid not in used_port_uids and not p.harness_type:
            import_log.log(f'port "{p.name}" at ({p.location_mils.x_mils + pdx:g}, '
                            f'{p.location_mils.y_mils:g}) mils belongs to no net — dropped')

    for uid, (pp, pdx) in power_port_by_uid.items():
        if uid not in used_power_uids:
            import_log.log(f'power port "{pp.text}" ({power_designator[uid]}) at '
                            f'({pp.record.location_mils.x_mils + pdx:g}, '
                            f'{pp.record.location_mils.y_mils:g}) mils touches no wire — placed but '
                            f'left unconnected (floating in the source schematic, not an import defect)')

    pcb = AltiumPcbDoc.from_file(str(pcb_path))
    _consistency_check(pcb, netlist, prjpcb_path.name)
    placements_designators = {designator for designator, c, dx in placements}
    _build_layout(pcb, proj_el, placements_designators, multi_pad_groups)
    _apply_net_classes(pcb, proj_el, schematic_el)

    xml_str = minidom.parseString(ET.tostring(proj_el, encoding='unicode')).toprettyxml(indent='  ')
    lines = [l for l in xml_str.splitlines() if l.strip()]
    result = '<?xml version="1.0" encoding="utf-8"?>\n' + '\n'.join(lines[1:])
    output_path.write_text(result, encoding='utf-8')
    print(f'Written: {output_path}')
    import_log.write(output_path)
    return result


def _apply_net_classes(pcb, proj_el, schematic_el):
    """Altium net classes (Classes6, kind==0 = net class, non-empty — the
    empty system 'All Nets' and every other kind fall out on those two
    checks alone) -> IR <classes> pool + class= on the schematic <net>,
    the same two homes eagle_project_parser._collect_classes fills.
    MEMBERSHIP ONLY, no per-class rules — same decision as _build_rules:
    the small flat IR DRC is easier to re-author by hand than Altium's
    scope-expression engine is to translate. Class lives on the schematic
    net only, never duplicated onto the layout <signal> (one fact, one
    place). A net in SEVERAL classes — legal in Altium, where a class is
    just a rule-scope set and the axes are orthogonal (clearance group vs
    width group) — keeps the alphabetically FIRST name: deterministic
    loss, loudly logged (user decision 2026-07-27)."""
    member = {}
    class_names = set()
    for nc in pcb.net_classes:
        if getattr(nc, 'kind', None) != 0 or not nc.members:
            continue
        cname = clean_attr_name(nc.name)
        class_names.add(cname)
        for m in nc.members:
            member.setdefault(clean_attr_name(_altium_overbar_to_eagle(m)), []).append(cname)
    if not class_names:
        return
    classes_el = ET.SubElement(proj_el, 'classes')
    for cname in sorted(class_names):
        ET.SubElement(classes_el, 'class', name=cname)
    unmatched = set(member)
    for net_el in schematic_el.findall('net'):
        classes = member.get(net_el.get('name'))
        if not classes:
            continue
        unmatched.discard(net_el.get('name'))
        classes = sorted(classes)
        net_el.set('class', classes[0])
        if len(classes) > 1:
            import_log.log(f'net "{net_el.get("name")}" is in {len(classes)} '
                            f'Altium net classes {classes} — kept '
                            f'"{classes[0]}" (alphabetically first), rest '
                            f'dropped (IR holds one class per net)')
    if unmatched:
        import_log.log(f'{len(unmatched)} net-class member(s) matched no '
                        f'schematic net (board-only or renamed) — class '
                        f'membership dropped: {sorted(unmatched)}')


if __name__ == '__main__':
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else 'testData/altium/IND/IND80S28.PrjPcb'
    out = sys.argv[2] if len(sys.argv) > 2 else 'outputs/IND80S28.swprj'
    convert_project(src, out)
