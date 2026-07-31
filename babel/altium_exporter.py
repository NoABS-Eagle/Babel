"""IR XML → Altium SchLib + PcbLib + xlsx + DbLib exporter.

DbLib approach: one SchLib entry per IR <symbol> (NPN, not BC847×25),
one PcbLib entry per unique footprint, one xlsx row per <component>,
one .DbLib config pointing Altium at the Excel table.
"""
import math
import xml.etree.ElementTree as ET
from pathlib import Path

from altium_monkey import (
    AltiumSchLib, AltiumPcbLib, AltiumSchPin,
    AltiumSchMapDefiner, AltiumSchMapDefinerList, AltiumSchImplParams,
)
from altium_monkey.altium_sch_enums import PinElectrical, TextJustification, TextOrientation
from altium_monkey.altium_pcb_enums import (PadShape, PcbRegionKind,
                                            PcbTextJustification)
from altium_monkey.altium_record_types import PcbLayer, LineWidth
from altium_monkey.altium_sch_svg_renderer import LINE_WIDTH_MILS

from babel import import_log
from babel import altium_layers
from babel.ir_util import (LAYER_DIMENSION, arc_params, chain_loops,
                           component_gates, flatten_loop, footprint_users,
                           is_multi_gate, model3d_dialog_rotation,
                           offset_contour, parse_layer, resolve_model3d_file,
                           sanitize_filename)


def _mils(v):
    """IR µm → Altium mils (integer).

    Whole mils are right for SCHEMATIC geometry (Altium's sheet grid is
    100 mil and symbol geometry lands on it), and wrong for a footprint —
    see _milf.
    """
    return round(float(v) / 25.4)


def _milf(v):
    """IR µm → Altium mils, EXACT.

    Footprint geometry does not sit on a mil grid: a 0.5 mm pad pitch is
    19.685 mil, and rounding it to 20 moved TQFP64 pads by up to 10 µm on
    the board (caught by verify_altium_board.py comparing every placed pad
    against the IR's own placement math). Altium's internal unit is 1/10000
    mil, so the fraction costs nothing.
    """
    return float(v) / 25.4


_ALIGN_JUSTIFICATION = {
    'bottom-left':   TextJustification.BOTTOM_LEFT,
    'bottom-center': TextJustification.BOTTOM_CENTER,
    'bottom-right':  TextJustification.BOTTOM_RIGHT,
    'center-left':   TextJustification.CENTER_LEFT,
    'center':        TextJustification.CENTER_CENTER,
    'center-center': TextJustification.CENTER_CENTER,
    'center-right':  TextJustification.CENTER_RIGHT,
    'top-left':      TextJustification.TOP_LEFT,
    'top-center':    TextJustification.TOP_CENTER,
    'top-right':     TextJustification.TOP_RIGHT,
}
_ORIENT_LIST = [
    TextOrientation.DEGREES_0,
    TextOrientation.DEGREES_90,
    TextOrientation.DEGREES_180,
    TextOrientation.DEGREES_270,
]


_ALIGN_PCB_JUSTIFICATION = {
    'bottom-left':   PcbTextJustification.LEFT_BOTTOM,
    'bottom-center': PcbTextJustification.CENTER_BOTTOM,
    'bottom-right':  PcbTextJustification.RIGHT_BOTTOM,
    'center-left':   PcbTextJustification.LEFT_CENTER,
    'center':        PcbTextJustification.CENTER_CENTER,
    'center-center': PcbTextJustification.CENTER_CENTER,
    'center-right':  PcbTextJustification.RIGHT_CENTER,
    'top-left':      PcbTextJustification.LEFT_TOP,
    'top-center':    PcbTextJustification.CENTER_TOP,
    'top-right':     PcbTextJustification.RIGHT_TOP,
}


def _justif(align_str):
    return _ALIGN_JUSTIFICATION.get((align_str or 'bottom-left').lower(),
                                    TextJustification.BOTTOM_LEFT)


def _pcb_justif(align_str):
    return _ALIGN_PCB_JUSTIFICATION.get((align_str or 'bottom-left').lower(),
                                        PcbTextJustification.LEFT_BOTTOM)


def _orient(rot_deg):
    return _ORIENT_LIST[(round(float(rot_deg or 0)) // 90) % 4]


def _altium_arc_angles(start, sweep):
    """IR (start, sweep) → Altium (start_angle, end_angle).
    IR positive sweep = CCW. Altium draws CCW from start to end.
    For negative sweep (CW arc), swap endpoints so path is unchanged.

    Full circle (|sweep| >= 360) is special-cased: '% 360' would otherwise
    fold end_angle back onto start_angle (e.g. 0,360 -> 0,0), the same
    degenerate-chord problem as Eagle's start==end wire — Altium needs an
    explicit start/end pair that's 360° apart, not equal, to render a circle."""
    if abs(sweep) >= 360:
        s = start % 360
        return s, s + 360
    if sweep >= 0:
        return start % 360, (start + sweep) % 360
    else:
        return (start + sweep) % 360, start % 360


# altium_monkey's own LINE_WIDTH_MILS (used internally for SVG rendering of
# every schematic primitive) is scaled for SVG preview, not true Altium mils —
# real native widths are 10x those values (confirmed against real Altium).
_LINE_WIDTH_UM = {lw: mils * 25.4 * 10 for lw, mils in LINE_WIDTH_MILS.items()}


def _lw(um):
    """IR µm line width → Altium LineWidth enum.

    SMALLEST and SMALL render identically (1 mil each); 0 maps to SMALLEST as
    the "no explicit width" default, anything up to the midpoint with MEDIUM
    still counts as SMALL.
    """
    w = float(um)
    if w <= 0: return LineWidth.SMALLEST
    mid_small_medium  = (_LINE_WIDTH_UM[LineWidth.SMALL]  + _LINE_WIDTH_UM[LineWidth.MEDIUM]) / 2
    mid_medium_large  = (_LINE_WIDTH_UM[LineWidth.MEDIUM] + _LINE_WIDTH_UM[LineWidth.LARGE]) / 2
    if w < mid_small_medium: return LineWidth.SMALL
    if w < mid_medium_large: return LineWidth.MEDIUM
    return LineWidth.LARGE


def _um_lw(line_width):
    """Altium LineWidth enum → IR µm. Inverse of _lw(), same source table."""
    return round(_LINE_WIDTH_UM.get(LineWidth(line_width), _LINE_WIDTH_UM[LineWidth.SMALL]))


def _sym_add_arc(sym, x, y, radius_mils, **kwargs):
    """sym.add_arc() wrapper working around an altium_monkey serialization bug:
    AltiumSchArc silently omits any field (radius, start_angle, end_angle) whose
    value happens to equal the class's hardcoded new-object default (radius=10
    i.e. 100 mils, start_angle=0, end_angle=90), because it only writes fields
    marked "explicitly set" and add_arc() never sets those markers. An omitted
    field reads back as 0 on reload (radius), or makes Altium treat the object
    as a full circle (missing start+end angle pair) instead of the intended arc.
    Re-asserting radius_mils/start_angle/end_angle together with their "_has_*"
    flags forces every field to actually be written regardless of value."""
    arc = sym.add_arc(x, y, radius_mils, **kwargs)
    arc.radius_mils = radius_mils
    arc._has_radius = True
    arc.start_angle = kwargs.get('start_angle', arc.start_angle)
    arc.end_angle = kwargs.get('end_angle', arc.end_angle)
    arc._has_start_angle = True
    arc._has_end_angle = True
    return arc


# IR <rules> attribute <-> Altium RULEKIND and the field carrying the number.
# ONE table for both directions: altium_project_parser reads through it,
# altium_board_exporter writes through it.
DRC_RULES = {
    'Clearance':           ('clearance',     'GAP'),
    'BoardOutlineClearance': ('edge_clearance', 'GAP'),
    'Width':               ('min_width',     'MINLIMIT'),
    'HoleSize':            ('min_drill',     'MINLIMIT'),
    'MinimumAnnularRing':  ('min_annular',   'MINIMUMRING'),
    'HoleToHoleClearance': ('min_drill_web', 'GAP'),
}


# Chord tolerance when a footprint's milling arc is flattened into the
# polygonal outline of a board-cutout region — 6 µm, well under any routing
# tolerance.
_CUTOUT_SAG_UM = 6.0


# Altium's point size doesn't follow the standard 72pt/inch typographic
# convention for rendered letter height: measured empirically, an 8pt font
# renders ~1.2mm tall, i.e. 1pt corresponds to ~150 µm of actual height.
_FONT_UM_PER_PT = 150
# Even at that scale Altium still renders text visibly too large against the
# source, so knock a flat 20% off the resulting point size. Hardcoded on
# purpose: Altium's own size handling is what is wrong, and no data in the IR
# can tell us by how much (user decision 2026-07-28, same issue as libraries).
_FONT_SIZE_FACTOR = 0.8


def _font_pt(size_um):
    """IR text size (µm) → Altium point size."""
    return max(1, round(float(size_um) / _FONT_UM_PER_PT * _FONT_SIZE_FACTOR))


def _font_id(schlib, size_um):
    """IR text size (µm) → Altium font_id via the library's font table."""
    fm = schlib.font_manager
    if fm is None:
        return 1
    return fm.get_or_create_font('Times New Roman', _font_pt(size_um))


_DIR_TO_ELEC = {
    'in':  PinElectrical.INPUT,
    'io':  PinElectrical.IO,
    'out': PinElectrical.OUTPUT,
    'oc':  PinElectrical.OPEN_COLLECTOR,
    'oe':  PinElectrical.OPEN_EMITTER,
    'pas': PinElectrical.PASSIVE,
    'hiz': PinElectrical.HIZ,
    'pwr': PinElectrical.POWER,
}
# No entry for IR's `nc` (KiCad/Eagle no-connect-ERC pin direction) on
# purpose — Altium's own PinElectrical enum has no equivalent member at all
# (decisions.md "KiCad: `no_connect` тип пина"). Falls through to this
# dict's call-site default (PinElectrical.PASSIVE) below — the pin's own
# NAME ("NC" or similar) still carries through untouched, only the
# special ERC-silencing behavior is lost, which Altium has no slot for
# regardless of what we do here.

# The IR->Altium layer projection lives in babel/data/altium_layers.tsv, the
# ONE table both directions read (user, 2026-07-28). What used to sit here was
# a second, hardcoded copy of the same fact, and the two had already drifted
# apart — the tsv carried tStop, the code did not, so drawn mask openings were
# silently dropped. Writing needs a concrete layer id, so a plan is computed
# once per project (altium_layers.plan) and declared into the file itself.


def _rot_about(pt, cx, cy, deg):
    """Point rotated CCW by deg about (cx, cy) — a <shape>'s own centre."""
    if not deg:
        return pt
    a = math.radians(deg)
    dx, dy = pt[0] - cx, pt[1] - cy
    return (cx + dx * math.cos(a) - dy * math.sin(a),
            cy + dx * math.sin(a) + dy * math.cos(a))


# Everything a keepout forbids: track, via, copper, SMD pad, TH pad (the five
# bits of Altium's keepout mask). An IR anti-layer means "no copper here" with
# no qualifier, so all five (user decision, 2026-07-28).
_KEEPOUT_ALL = 0b11111


def mark_keepout(rec):
    """Turn an emitted primitive into a keepout.

    `add_track`/`add_arc`/`add_fill` do not take the flag even though real
    Altium records carry it (ground truth: BC2087.PcbDoc has keepout tracks,
    arcs and fills on ordinary copper layers), so it goes on afterwards —
    the same shape of workaround as pour_over and the polygon index.
    """
    rec.is_keepout = True
    rec.keepout_restrictions = _KEEPOUT_ALL
    return rec


def target_layer(layer_plan, ln):
    """IR layer attribute -> (Altium layer, is_keepout).

    An ANTI-layer ('!1') is not a layer of its own: it is a keepout ON the
    copper layer it names. Altium can also put keepouts on the Keep-Out Layer
    (56), but that means "every layer" and would throw away the side the IR
    knows, so the flag goes on the copper layer itself.
    """
    try:
        anti, n = parse_layer(ln)
    except (TypeError, ValueError):
        return None, False
    slot = layer_plan.get(n)
    if slot is None:
        return None, False
    return slot[0], anti


def npth_pad_kwargs(hole_el):
    """IR <hole> -> the Altium object that IS a non-plated hole: a PAD with
    no copper around it.

    Altium has no separate "hole" primitive — ground truth from a real board
    (testData/altium/IND/RLT504_117C.PcbDoc): a free pad on Multi-Layer,
    round, unplated, empty designator, pad diameter EQUAL to the drill, so
    there is no annular ring. Shared by the footprint and the board sides,
    which differ only in where the position comes from.
    """
    d = _milf(hole_el.get('drill', 0))
    return dict(designator='', width_mils=d, height_mils=d,
                layer=PcbLayer.MULTI_LAYER, shape=PadShape.CIRCLE,
                hole_size_mils=d, plated=False)


def shape_primitives(el):
    """IR <shape> -> the Altium primitives that draw it, in IR µm.

    [('arc',   cx, cy, r, start_deg, end_deg, width),
     ('fill',  x1, y1, x2, y2, rotation_deg),
     ('track', x1, y1, x2, y2, width), ...]

    The DECISION (circle vs rectangle, filled vs outlined, where the rotated
    corners land) lives here once; the two callers — a footprint in a PcbLib
    and free board geometry in a PcbDoc — only translate it to their own
    add_* signatures, which differ in argument names for no good reason.
    """
    cx, cy = float(el.get('x', 0)), float(el.get('y', 0))
    outline = float(el.get('outline', 0) or 0)
    if int(el.get('roundness', 0)) == 100:
        r = float(el.get('w', 0)) / 2
        # A filled circle is drawn as a stroke of half the radius, running
        # along the mid-circle — no separate "filled arc" exists in Altium.
        return [('arc', cx, cy, r / 2 if not outline else r, 0, 360,
                 r if not outline else outline)]
    hw, hh = float(el.get('w', 0)) / 2, float(el.get('h', 0)) / 2
    # <shape rot> turns the shape about its OWN centre (ir_schema.md).
    rot = float(el.get('rot', 0) or 0) % 360
    if not outline:
        return [('fill', cx - hw, cy - hh, cx + hw, cy + hh, rot)]
    corners = [_rot_about((cx + sx * hw, cy + sy * hh), cx, cy, rot)
               for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
    return [('track', ax, ay, bx, by, outline or 100)
            for (ax, ay), (bx, by) in zip(corners, corners[1:] + corners[:1])]


def _pcb_layer(layer_plan, ln):
    """IR layer attribute -> the Altium layer id this project's plan gives
    it, or None when it has no home (anti-layer, or the mechanical slots ran
    out). The plan comes from babel/data/altium_layers.tsv."""
    try:
        anti, n = parse_layer(ln)
    except (TypeError, ValueError):
        return None
    if anti:
        return None
    slot = layer_plan.get(n)
    return slot[0] if slot else None


# ─── pin mapping helpers ─────────────────────────────────────────────────────

def _pin_des_map(comp_el, gate_name, fp_el=None):
    """Build {pin_name: [pad_designators]} for one gate of one footprint.

    Defaults to the first footprint — the map used to be taken from it for
    EVERY implementation, which silently mislabelled the pins of the second
    and later footprints whenever their pad names differ.
    """
    result = {}
    if fp_el is None:
        fp_el = comp_el.find('footprint')
    if fp_el is None:
        return result
    prefix = f'{gate_name}.' if gate_name else None
    for m in fp_el.findall('pin-mapping/map'):
        pin  = m.get('pin', '')
        pads = m.get('pad', '').split()
        if not pin or not pads:
            continue
        if prefix:
            if pin.startswith(prefix):
                result.setdefault(pin[len(prefix):], []).extend(pads)
        else:
            result.setdefault(pin, []).extend(pads)
    return result


def _derive_pin_map(sym_name, ir_root):
    """Pin→pad map for sym_name, taken from the first component that uses it."""
    for comp_el in ir_root.findall('component'):
        for gate_name, sname in component_gates(comp_el):
            if sname == sym_name:
                return _pin_des_map(comp_el, gate_name)
    return {}


def _owner_component_for_sym(sym_name, ir_root):
    """The first component that references sym_name — the one whose footprints
    and pin map a pool symbol borrows."""
    for comp_el in ir_root.findall('component'):
        for _, sname in component_gates(comp_el):
            if sname == sym_name:
                return comp_el
    return None


def _gate_of_sym(comp_el, sym_name):
    """The gate of comp_el drawn by sym_name ('' when single-gate)."""
    for gate_name, sname in component_gates(comp_el):
        if sname == sym_name:
            return gate_name
    return ''


def _sym_used_standalone(sym_name, ir_root):
    """True if at least one single-gate component references this symbol directly."""
    for comp_el in ir_root.findall('component'):
        if comp_el.get('symbol') == sym_name:
            return True
    return False


def pin_pad_pairs(comp_el, fp_el, ir_root=None):
    """[(schematic pin designator, pads of THIS footprint)], gates in order.

    A MAP_DEFINER answers "which pads does the pin CALLED X sit on", so its
    DesIntf must be the designator the SYMBOL gives the pin — and the symbol
    takes its designators from the component's FIRST footprint, whatever
    footprint the instance is placed with. Writing the current footprint's
    pad on both sides made the map say `+ -> +` for a symbol whose pin is
    called `2`, so Altium found no such pin and the first ECO offered to
    take CON5 off VDC and GND (ground truth: the user's own Update PCB on
    step4).
    """
    out = []
    for gate_name, sym_name in component_gates(comp_el):
        # WHOSE first footprint depends on what the SchLib entry is. A
        # multi-gate component gets an entry of its own, so its own first
        # footprint names the pins. A single-gate one is placed from the POOL
        # SYMBOL, which several components may share — and the symbol was
        # built from the FIRST of them (_derive_pin_map). Base's SB3 is a
        # TACT_SWITCH-KLS whose pin `2` is pad `2`, but the shared symbol was
        # named after TACT_SWITCH, where that pin is pad `3`: taking the
        # instance's own component here made the map name a pin the symbol
        # does not have.
        if ir_root is not None and not is_multi_gate(comp_el):
            named = _derive_pin_map(sym_name, ir_root)
        else:
            named = _pin_des_map(comp_el, gate_name, comp_el.find('footprint'))
        here = _pin_des_map(comp_el, gate_name, fp_el)
        for pin, pads in named.items():
            if pads:
                out.append((pads[0], here.get(pin) or pads))
    return out


def bake_footprint(comp, model_name, pcblib_name):
    """Give a PLACED component its own footprint model.

    A component that only points at a DbLib row carries no model, and Altium
    reports "Footprint of component ... cannot be found" even with the
    database connected and its driver installed — ground truth: the user's
    Altium on step4, where a component placed BY HAND from the same DbLib
    arrived with a footprint and ours did not. So the model is a property of
    the instance on the sheet; the DbLib only supplies it at placement time.
    The xlsx keeps its Footprint Ref column for "Update From Libraries".
    """
    return comp.add_footprint(model_name, library_name=pcblib_name,
                              is_current=True)


def write_pin_maps(doc, pending):
    """Append the MAP_DEFINERs of already baked models — the LAST thing done
    to a sheet, and by appending only.

    `pending`: iterable of (implementation record, pad lists).

    Two properties of the format force this. OwnerIndex is a POSITION in the
    document's object list, so inserting a record in the middle silently
    re-owns every child that follows. And add_object() on a component
    re-synchronizes its children, walking the implementation's own children
    but not theirs — a MAP_DEFINER, which sits one level deeper, is dropped
    on the next parameter written. Appending after everything else is
    immune to both.
    """
    from altium_monkey.altium_record_sch__implementation import (
        AltiumSchMapDefinerList)
    for impl, pairs in pending:
        impl_idx = doc.all_objects.index(impl)
        md_list = next(o for o in doc.all_objects
                       if isinstance(o, AltiumSchMapDefinerList)
                       and getattr(o, 'owner_index', None) == impl_idx)
        md_pos = doc.all_objects.index(md_list)
        for desig, pads in pairs:
            if not pads:
                continue
            md = AltiumSchMapDefiner()
            md.designator_interface       = desig
            md.implementation_designators = pads
            md._has_designator_interface       = True
            md._has_implementation_designators = True
            doc._bind_schematic_object(md)
            md.owner_index = md_pos
            doc.all_objects.append(md)
            doc._categorize_object(md)


def _footprint_impl_with_map(fp_name, pairs, is_current=True):
    """Build (impl_record, children) for a footprint implementation + MAP_DEFINERs.

    pairs: (schematic pin designator, pads of this footprint), from
    pin_pad_pairs — DesIntf names the PIN, the designators name the PADS.
    """
    impl = {
        'RECORD':               '45',
        'ModelName':            fp_name,
        'ModelType':            'PCBLIB',
        'IsCurrent':            'T' if is_current else 'F',
        'DatafileCount':        '1',
        'ModelDatafileEntity0': fp_name,
        'ModelDatafileKind0':   'PCBLib',
    }
    children = [AltiumSchMapDefinerList()]
    for desig, pads in pairs:
        if pads:
            md = AltiumSchMapDefiner()
            md.designator_interface       = desig
            md.implementation_designators = pads
            children.append(md)
    children.append(AltiumSchImplParams())
    return impl, children


# ─── schematic graphics ───────────────────────────────────────────────────────

def _add_gate_to_symbol(sym, sym_el, des_map, schlib, owner_part_id=None):
    """Emit all graphics and pins from an IR <symbol> element into an Altium symbol part.

    owner_part_id: None = single-gate (all objects on part 1); int = 1-indexed gate.
    """
    pid = -1 if owner_part_id is None else owner_part_id

    for pin_el in sym_el.findall('pin'):
        name      = pin_el.get('name', '')
        rot       = int(float(pin_el.get('rot', 0)))
        length_um = float(pin_el.get('length', '2540'))
        x_um      = float(pin_el.get('x', '0'))
        y_um      = float(pin_el.get('y', '0'))
        rad       = math.radians(rot)
        bx = _mils(str(x_um + length_um * math.cos(rad)))
        by = _mils(str(y_um + length_um * math.sin(rad)))
        pads = des_map.get(name) or []
        sym.add_pin(AltiumSchPin(
            designator         = pads[0] if pads else '',
            name               = name,
            x                  = bx,
            y                  = by,
            orientation        = ((rot + 180) // 90) % 4,
            length             = _mils(pin_el.get('length', '2540')),
            electrical_type    = _DIR_TO_ELEC.get(pin_el.get('direction', 'pas'),
                                                   PinElectrical.PASSIVE),
            name_visible       = pin_el.get('pinvis', '1') == '1',
            designator_visible = pin_el.get('padvis', '1') == '1',
            owner_part_id      = owner_part_id,
        ))

    for el in sym_el:
        if el.tag == 'line':
            sym.add_line(
                _mils(el.get('x1')), _mils(el.get('y1')),
                _mils(el.get('x2')), _mils(el.get('y2')),
                line_width=_lw(el.get('width', '0')),
                owner_part_id=pid,
            )
        elif el.tag == 'shape':
            rn         = int(el.get('roundness', 0))
            mx         = _mils(el.get('x', 0))
            my         = _mils(el.get('y', 0))
            outline_um = float(el.get('outline', '0'))
            lw         = _lw(el.get('outline', '0'))
            if rn == 100:
                _sym_add_arc(sym, mx, my,
                             round(float(el.get('w', '0')) / 50.8),
                             start_angle=0, end_angle=360,
                             line_width=lw,
                             owner_part_id=pid)
            else:
                hw = round(float(el.get('w', '0')) / 50.8)
                hh = round(float(el.get('h', '0')) / 50.8)
                if outline_um == 0:
                    sym.add_rectangle(mx - hw, my - hh, mx + hw, my + hh,
                                      area_color=0, is_solid=True, line_width=lw,
                                      owner_part_id=pid)
                else:
                    sym.add_rectangle(mx - hw, my - hh, mx + hw, my + hh,
                                      is_solid=False, line_width=lw,
                                      owner_part_id=pid)
        elif el.tag == 'arc':
            # endpoint canon -> Altium's native center form (derived here,
            # at the Altium boundary — ir_util.arc_params)
            p = arc_params(float(el.get('x1')), float(el.get('y1')),
                           float(el.get('x2')), float(el.get('y2')),
                           float(el.get('curve')))
            if p is None:
                continue
            cx_um, cy_um, r_um, start, sweep = p
            a1, a2 = _altium_arc_angles(start, sweep)
            _sym_add_arc(
                sym,
                _mils(cx_um), _mils(cy_um),
                _mils(r_um),
                start_angle=a1,
                end_angle=a2,
                line_width=_lw(el.get('width', '0')),
                owner_part_id=pid,
            )
        elif el.tag == 'polygon':
            verts = [(_mils(v.get('x', '0')), _mils(v.get('y', '0')))
                     for v in el.findall('vertex')]
            sym.add_polygon(verts, color=0, area_color=0,
                            line_width=_lw(el.get('width', '0')),
                            is_solid=True,
                            owner_part_id=pid)
        elif el.tag == 'text':
            content = (el.text or '').strip()
            if content and not content.startswith('>'):
                lbl = sym.add_label(
                    content,
                    _mils(el.get('x', '0')),
                    _mils(el.get('y', '0')),
                    font_id=_font_id(schlib, el.get('size', '1270')),
                    orientation=_orient(el.get('rot', '0')),
                    owner_part_id=pid,
                )
                lbl.owner_part_id = max(1, pid)  # add_label drops -1→None; force explicit OwnerPartId
                lbl.justification = _justif(el.get('align', 'bottom-left'))


# ─── SchLib: one entry per IR <symbol> ────────────────────────────────────────

def _export_schlib_symbol(sym_el, schlib, ir_root):
    """Add one IR <symbol> as an Altium SchLib component (DbLib-style)."""
    sname = sym_el.get('name', '')
    sym   = schlib.add_symbol(lib_ref(sname))

    # Locate >NAME/>PART, >VALUE and other > placeholders in symbol texts.
    # >SOMETHING (not NAME/PART/VALUE/GATE) → visible parameter at that position.
    _SKIP_PLACEHOLDERS = {'NAME', 'PART', 'VALUE', 'GATE'}
    des_x, des_y, des_align, des_rot, des_size = 0, 50, 'bottom-left', 0, '1270'
    val_x, val_y, val_align, val_rot, val_size = 0, 0,  'bottom-left', 0, '1270'
    val_found   = False
    placeholders: dict[str, tuple] = {}  # param_name → (x, y, align, rot, size)
    for el in sym_el:
        if el.tag != 'text':
            continue
        content = (el.text or '').strip()
        if content in ('>NAME', '>PART'):
            des_x, des_y = _mils(el.get('x', '0')), _mils(el.get('y', '0'))
            des_align    = el.get('align', 'bottom-left')
            des_rot      = float(el.get('rot', '0'))
            des_size     = el.get('size', '1270')
        elif content == '>VALUE':
            val_x, val_y = _mils(el.get('x', '0')), _mils(el.get('y', '0'))
            val_align    = el.get('align', 'bottom-left')
            val_rot      = float(el.get('rot', '0'))
            val_size     = el.get('size', '1270')
            val_found    = True
        elif content.startswith('>'):
            pname = content[1:]
            if pname not in _SKIP_PLACEHOLDERS:
                placeholders[pname] = (
                    _mils(el.get('x', '0')), _mils(el.get('y', '0')),
                    el.get('align', 'bottom-left'), float(el.get('rot', '0')),
                    el.get('size', '1270'),
                )
    if not val_found:
        val_x, val_y, val_align, val_rot, val_size = des_x, des_y - 50, des_align, des_rot, des_size

    # Use prefix from the first component that references this symbol
    des_prefix = 'U'
    for _c in ir_root.findall('component'):
        for _, _gs in component_gates(_c):
            if _gs == sname:
                des_prefix = _c.get('prefix') or 'U'
                break
        else:
            continue
        break

    d = sym.add_designator(f'{des_prefix}?', des_x, des_y, font_id=_font_id(schlib, des_size))
    d.owner_part_id = 1
    d.auto_position = False
    d.justification = _justif(des_align)
    d.orientation   = _orient(des_rot)

    # No >VALUE placeholder means the symbol never showed a value: Comment must
    # exist (BOM, DB sync) but stay hidden, or it lands next to the designator.
    p = sym.add_parameter('Comment', 'Comment', x=val_x, y=val_y,
                          is_hidden=not val_found,
                          font_id=_font_id(schlib, val_size))
    p.owner_part_id = 1
    p.auto_position = False
    p.justification = _justif(val_align)
    p.orientation   = _orient(val_rot)

    # Visible parameters for every > placeholder found in the symbol (e.g. >MANF#, >PACKAGE).
    # Value = param name so the symbol looks readable in the SchLib editor;
    # DbLib overwrites these values when the component is actually used.
    for pname, (px, py, palign, prot, psize) in placeholders.items():
        vp = sym.add_parameter(pname, pname, x=px, y=py, is_hidden=False,
                               font_id=_font_id(schlib, psize))
        vp.owner_part_id = 1
        vp.auto_position = False
        vp.justification = _justif(palign)
        vp.orientation   = _orient(prot)

    # Hidden parameters for component attrs without a symbol placeholder.
    # DbLib populates their values from xlsx.
    placeholder_upper = {k.upper() for k in placeholders}
    seen_attrs: set[str] = set()
    ordered_attrs: list[str] = []
    for comp_el in ir_root.findall('component'):
        for _, gate_sym in component_gates(comp_el):
            if gate_sym == sname:
                for k in _custom_attrs(comp_el):
                    if k not in seen_attrs:
                        seen_attrs.add(k)
                        ordered_attrs.append(k)
                break
    for attr_name in ordered_attrs:
        if attr_name.upper() not in placeholder_upper:
            hp = sym.add_parameter(attr_name, '', x=val_x, y=val_y, is_hidden=True,
                                   font_id=_font_id(schlib, val_size))
            hp.owner_part_id = 1

    des_map = _derive_pin_map(sname, ir_root)
    _add_gate_to_symbol(sym, sym_el, des_map, schlib)

    # Footprint implementations + MAP_DEFINERs (all footprints; first = IsCurrent)
    owner = _owner_component_for_sym(sname, ir_root)
    fp_els = owner.findall('footprint') if owner is not None else []
    for i, fp_el in enumerate(fp_els):
        impl, children = _footprint_impl_with_map(
            fp_name(fp_el), pin_pad_pairs(owner, fp_el, ir_root),
            is_current=(i == 0))
        sym.add_implementation(impl, children)


# ─── multi-part SchLib entry (multi-gate components) ─────────────────────────

def _export_schlib_multipart(comp_el, sym_pool, schlib):
    """Emit one multi-part SchLib symbol for a multi-gate IR component.

    Pool symbols (parts) are reused by reference but their geometry is copied into
    a single multi-part SchLib entry named after the component (e.g. 'LM358').
    DbLib Library Ref column will point here instead of the pool symbol.
    """
    cname = comp_el.get('name', '')
    gates = component_gates(comp_el)
    sym   = schlib.add_symbol(lib_ref(cname))
    sym.set_part_count(len(gates))

    # Anchor positions from first gate's pool symbol; collect > placeholders from all gates.
    _SKIP_PLACEHOLDERS = {'NAME', 'PART', 'VALUE', 'GATE'}
    des_x, des_y, des_align, des_rot, des_size = 0, 50, 'bottom-left', 0, '1270'
    val_x, val_y, val_align, val_rot, val_size = 0, 0,  'bottom-left', 0, '1270'
    des_found   = False
    val_found   = False
    placeholders: dict[str, tuple] = {}
    for _, gsym_name in gates:
        gate_sym_el = sym_pool.get(gsym_name)
        if gate_sym_el is None:
            continue
        for el in gate_sym_el:
            if el.tag != 'text':
                continue
            content = (el.text or '').strip()
            if content in ('>NAME', '>PART') and not des_found:
                des_x, des_y = _mils(el.get('x', '0')), _mils(el.get('y', '0'))
                des_align    = el.get('align', 'bottom-left')
                des_rot      = float(el.get('rot', '0'))
                des_size     = el.get('size', '1270')
                des_found    = True
            elif content == '>VALUE' and not val_found:
                val_x, val_y = _mils(el.get('x', '0')), _mils(el.get('y', '0'))
                val_align    = el.get('align', 'bottom-left')
                val_rot      = float(el.get('rot', '0'))
                val_size     = el.get('size', '1270')
                val_found    = True
            elif content.startswith('>'):
                pname = content[1:]
                if pname not in _SKIP_PLACEHOLDERS and pname not in placeholders:
                    placeholders[pname] = (
                        _mils(el.get('x', '0')), _mils(el.get('y', '0')),
                        el.get('align', 'bottom-left'), float(el.get('rot', '0')),
                        el.get('size', '1270'),
                    )
    if not val_found:
        val_x, val_y, val_align, val_rot, val_size = des_x, des_y - 50, des_align, des_rot, des_size

    # Designator and Comment are shared across all parts (OwnerPartId=-1)
    des_prefix = comp_el.get('prefix') or 'U'
    # No >VALUE placeholder means the symbol never showed a value: Comment must
    # exist (BOM, DB sync) but stay hidden, or it lands next to the designator.
    d = sym.add_designator(f'{des_prefix}?', des_x, des_y, font_id=_font_id(schlib, des_size))
    d.owner_part_id = -1
    d.auto_position = False
    d.justification = _justif(des_align)
    d.orientation   = _orient(des_rot)

    p = sym.add_parameter('Comment', 'Comment', x=val_x, y=val_y,
                          is_hidden=not val_found,
                          font_id=_font_id(schlib, val_size))
    p.owner_part_id = -1
    p.auto_position = False
    p.justification = _justif(val_align)
    p.orientation   = _orient(val_rot)

    for pname, (px, py, palign, prot, psize) in placeholders.items():
        vp = sym.add_parameter(pname, pname, x=px, y=py, is_hidden=False,
                               font_id=_font_id(schlib, psize))
        vp.owner_part_id = -1
        vp.auto_position = False
        vp.justification = _justif(palign)
        vp.orientation   = _orient(prot)

    placeholder_upper = {k.upper() for k in placeholders}
    for attr_name in _custom_attrs(comp_el):
        if attr_name.upper() not in placeholder_upper:
            hp = sym.add_parameter(attr_name, '', x=val_x, y=val_y, is_hidden=True,
                                   font_id=_font_id(schlib, val_size))
            hp.owner_part_id = -1

    # Each gate → one numbered part (1-indexed)
    for i, (gate_name, sym_name) in enumerate(gates, 1):
        gate_sym_el = sym_pool.get(sym_name)
        if gate_sym_el is None:
            print('  ! multi-gate %s: pool symbol %r not found' % (cname, sym_name))
            continue
        _add_gate_to_symbol(sym, gate_sym_el, _pin_des_map(comp_el, gate_name),
                            schlib, owner_part_id=i)

    # Footprint implementations with MAP_DEFINERs covering all gates in order (all footprints; first = IsCurrent)
    for i, fp_el in enumerate(comp_el.findall('footprint')):
        impl, children = _footprint_impl_with_map(
            fp_name(fp_el), pin_pad_pairs(comp_el, fp_el), is_current=(i == 0))
        sym.add_implementation(impl, children)


# ─── footprint names: PcbLib stores them as single-byte ──────────────────────

# Cyrillic letters that LOOK like Latin ones — the only non-ASCII characters
# real library names carry in practice ("2х2" is a 2x2 header whose x is a
# Cyrillic kha). Mapping them back to their Latin twin keeps the name
# readable; anything else non-ASCII becomes '_'.
_HOMOGLYPHS = str.maketrans({
    'А': 'A', 'В': 'B', 'Е': 'E', 'К': 'K', 'М': 'M', 'Н': 'H', 'О': 'O',
    'Р': 'P', 'С': 'C', 'Т': 'T', 'У': 'Y', 'Х': 'X',
    'а': 'a', 'в': 'b', 'е': 'e', 'к': 'k', 'м': 'm', 'н': 'h', 'о': 'o',
    'р': 'p', 'с': 'c', 'т': 't', 'у': 'y', 'х': 'x',
})


# ─── multi-line text ─────────────────────────────────────────────────────────

# The two multi-line objects we emit are drawn with DIFFERENT fonts, so they
# need different metrics — the schematic Text Frame with a real TrueType face,
# the PCB String with Altium's own stroke font. Sizing both off the TrueType
# metric made the PCB box a third too narrow and Altium cut the long lines
# off — caught by the user on step4's note.

# A line of text takes about 1.6 of its cap height together with the gap to
# the next one — the ratio Altium's own multi-line objects come out at.
_LINE_PITCH = 1.6

# The stroke font's own step, measured by altium_monkey off imported document
# text (_STROKE_MULTILINE_SPACING_FACTOR): noticeably airier than the
# TrueType one, which is why an Altium PCB note stands taller than its Eagle
# original. We follow Altium rather than pretend otherwise.
_STROKE_LINE_PITCH = 1.68


# A text's HEIGHT in the IR (and in Altium) is the cap height; a TrueType
# metric measures at the EM size, which is bigger. Arial's cap height is
# 0.716 em.
_CAP_PER_EM = 0.72


def multiline_box(lines, height_mils):
    """Size (w, h) in mils of a TrueType text block at that cap height.

    For the schematic Text Frame. The width is the LONGEST line.
    """
    from altium_monkey.altium_text_metrics import measure_text_width
    em = height_mils / _CAP_PER_EM
    width = max((measure_text_width(l, font_size_px=em)
                 for l in lines), default=height_mils)
    return width, len(lines) * height_mils * _LINE_PITCH


def stroke_box(lines, height_mils):
    """Size (w, h) in mils of a PCB stroke-font text block at that height.

    Altium's stroke font is a vector font whose glyphs are defined in units of
    the text height, so the width of a line is just the sum of its characters'
    advances times the height — exact, no font resolution involved. The table
    is altium_monkey's calibrated one for the Default stroke font, the only
    one we emit.
    """
    from altium_monkey.altium_stroke_font_data import STROKE_ADVANCES_DEFAULT
    fallback = STROKE_ADVANCES_DEFAULT[ord('X')]
    width = max((sum(STROKE_ADVANCES_DEFAULT.get(ord(c), fallback) for c in l)
                 for l in lines), default=1.0) * height_mils
    return width, len(lines) * height_mils * _STROKE_LINE_PITCH


# ─── multi-channel designators ───────────────────────────────────────────────

# Altium's own default, written into the project's [Design] section and read
# back by its compiler: the physical designator of a part in a channel is the
# canonical one plus the room it sits in. Eagle's `offset` arithmetic
# (C1 @ 100 -> C101) has no counterpart in this template language, so on the
# Altium path the channel naming is Altium's, not Eagle's — user decision
# 2026-07-29, taken with the alternative (padding the canonical designators to
# fake the arithmetic) on the table.
CHANNEL_DESIGNATOR_FORMAT = '$Component_$RoomName'


def channel_designator(room, canonical):
    """('DCDC1', 'C1') -> 'C1_DCDC1' — the ONE place this format is applied.

    Both the schematic side (which writes the format string into the project)
    and the board side (which must name the very same part identically, or
    every component shows up as changed on the first ECO) go through here.
    """
    return (CHANNEL_DESIGNATOR_FORMAT
            .replace('$Component', canonical)
            .replace('$RoomName', room))


def lib_ref(name):
    """The name a SchLib ENTRY is written and looked up under.

    Every entry is an OLE storage inside the .SchLib, and a storage name may
    not contain the characters a path may not contain — altium_monkey cut
    "CCDN2,5/18-G1P26THR" down to "CCDN2,5" and the placed component then
    found no symbol at all. Same substitution as any other name that becomes
    a file/storage name (ir_util.sanitize_filename), applied in ONE place so
    the library, the xlsx row and the placement agree.
    """
    out = sanitize_filename(name or '')
    if out != name:
        import_log.log(name, '', f'LIBRARY entry name written as "{out}" '
                                 '(a SchLib entry is an OLE storage, and its '
                                 'name may not contain path characters)')
    return out


def fp_name(fp_el_or_str):
    """The name a footprint is written under in EVERY Altium artifact.

    A PcbLib footprint header is a byte pascal string — Altium has no Unicode
    there — so a non-ASCII name cannot be stored at all. The single home of
    the substitution is here: the PcbLib entry, the SchLib implementation
    link, the xlsx row and the PcbDoc placement must all say the same word or
    the placed component loses its footprint.
    """
    name = (fp_el_or_str if isinstance(fp_el_or_str, str)
            else fp_el_or_str.get('name', ''))
    if name.isascii():
        return name
    out = name.translate(_HOMOGLYPHS)
    out = ''.join(c if c.isascii() else '_' for c in out)
    import_log.log(name, '', f'FOOTPRINT name is not ASCII, written as '
                             f'"{out}" (PcbLib stores names as single-byte)')
    return out


# ─── component table helpers ──────────────────────────────────────────────────

def part_number(comp_el, fp_el):
    """The DbLib row key ("Part Number" column) for one component/footprint.

    One row per component when it has 0/1 footprints, one row per footprint
    otherwise. The suffix is the footprint NAME — except when one package
    backs several devices (Eagle CON-2P: `-B2B-XH` and `-DS1069M` are both
    packaged B2B-XH-A), where the name is not an identity and the VARIANT
    string is (the same rule eagle_project_parser records on
    `<instance footprint=>`). Both the xlsx rows and the placed components
    resolve their key HERE — two spellings of it would silently unlink every
    placed part from its database row.
    """
    cname = comp_el.get('name', '')
    fps = comp_el.findall('footprint')
    if len(fps) <= 1 or fp_el is None:
        return cname
    name = fp_name(fp_el)
    if [f.get('name') for f in fps].count(fp_el.get('name', '')) > 1:
        return f'{cname}_{fp_el.get("variant") or name}'
    return f'{cname}_{name}'


def _custom_attrs(comp_el):
    """Return dict of custom attributes, skipping 'value' (mapped to Comment)
    and 'description' (mapped to the standard Description column — see
    _build_component_rows; same idea as eagle_exporter routing it to Eagle's
    native deviceset <description> instead of the generic attribute table)."""
    out = {}
    attrs_el = comp_el.find('attributes')
    if attrs_el is not None:
        for a in attrs_el.findall('attr'):
            n, v = a.get('name', ''), a.get('value', '')
            if n and n.lower() not in ('value', 'description'):
                out[n] = v
    return out


def _build_component_rows(ir_root, schlib_name, pcblib_name):
    """Return (rows, all_cols) for the component xlsx table.

    all_cols: standard columns first, then any custom attribute keys found across
    all components (insertion order preserved).
    """
    STD = ['Part Number', 'Library Ref', 'Library Path',
           'Footprint Ref', 'Footprint Path',
           'Designator', 'Comment', 'Description']

    rows     = []
    all_cols = list(STD)

    for comp_el in ir_root.findall('component'):
        cname  = comp_el.get('name', '')
        prefix = comp_el.get('prefix') or 'U'

        gates    = component_gates(comp_el)
        # Multi-gate: Library Ref points to the multi-part SchLib entry (named after
        # the component). Single-gate: Library Ref is the pool symbol name.
        sym_name = cname if is_multi_gate(comp_el) else (gates[0][1] if gates else '')

        fp_els = comp_el.findall('footprint')

        # 'value'/'description' are plain IR attributes (no dedicated
        # element — see ir_schema.md), routed to their own standard columns
        # here same as eagle_exporter routes 'description' to Eagle's native
        # deviceset <description>: 'value' → Altium Comment (BOM/schematic),
        # 'description' → the standard Description column.
        value = ''
        desc = ''
        attrs_el = comp_el.find('attributes')
        if attrs_el is not None:
            for a in attrs_el.findall('attr'):
                name = a.get('name', '').lower()
                if name == 'value':
                    value = a.get('value', '')
                elif name == 'description':
                    desc = a.get('value', '')
        comment = value or cname

        # Component-level custom attrs (base)
        comp_custom = _custom_attrs(comp_el)

        # For multi-footprint: find attrs defined per-device (vary by footprint)
        # so they don't bleed from one footprint's value into another's base.
        per_fp_keys: set[str] = set()
        if len(fp_els) > 1:
            for fp_el in fp_els:
                fa = fp_el.find('attributes')
                if fa is not None:
                    for a in fa.findall('attr'):
                        n = a.get('name', '')
                        if n and n.lower() != 'value':
                            per_fp_keys.add(n)
        comp_base = {k: v for k, v in comp_custom.items() if k not in per_fp_keys}

        def _fp_custom(fp_el_):
            """Per-footprint attrs merged onto (filtered) component base."""
            merged = dict(comp_base)
            fa = fp_el_.find('attributes') if fp_el_ is not None else None
            if fa is not None:
                for a in fa.findall('attr'):
                    n, v = a.get('name', ''), a.get('value', '')
                    if n and n.lower() != 'value':
                        merged[n] = v
            return merged

        # Gather all column keys across component + all footprints
        all_keys: list[str] = list(comp_custom.keys())
        seen_keys: set[str] = set(all_keys)
        for fp_el in fp_els:
            fa = fp_el.find('attributes')
            if fa is not None:
                for a in fa.findall('attr'):
                    n = a.get('name', '')
                    if n and n.lower() != 'value' and n not in seen_keys:
                        seen_keys.add(n)
                        all_keys.append(n)
        for k in all_keys:
            if k not in all_cols:
                all_cols.append(k)

        def _make_row(part_num, fp_name, fp_el_=None):
            custom = _fp_custom(fp_el_) if fp_el_ is not None else comp_custom
            r = {
                'Part Number':    part_num,
                'Library Ref':    lib_ref(sym_name),
                'Library Path':   schlib_name,
                'Footprint Ref':  fp_name,
                'Footprint Path': pcblib_name,
                'Designator':     f'{prefix}?',
                'Comment':        comment,
                'Description':    desc,
            }
            for k, v in custom.items():
                if k not in r:
                    r[k] = v
            return r

        if len(fp_els) <= 1:
            fp_el_one = fp_els[0] if fp_els else None
            fp_name = fp_el_one.get('name', '') if fp_el_one else ''
            rows.append(_make_row(cname, fp_name, fp_el_one))
        else:
            for fp_el_i in fp_els:
                rows.append(_make_row(part_number(comp_el, fp_el_i),
                                      fp_el_i.get('name', ''), fp_el_i))

    return rows, all_cols


# ─── xlsx writer ─────────────────────────────────────────────────────────────

def _write_xlsx(rows, all_cols, xlsx_path):
    import openpyxl
    from openpyxl.styles import Font

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Components'

    for ci, col in enumerate(all_cols, 1):
        cell = ws.cell(row=1, column=ci, value=col)
        cell.font = Font(bold=True)

    for ri, row in enumerate(rows, 2):
        for ci, col in enumerate(all_cols, 1):
            ws.cell(row=ri, column=ci, value=row.get(col, ''))

    for col_cells in ws.columns:
        max_len = max((len(str(c.value or '')) for c in col_cells), default=8)
        ws.column_dimensions[col_cells[0].column_letter].width = min(max_len + 2, 40)

    wb.save(xlsx_path)
    print(f'Written: {xlsx_path}')


# ─── DbLib config writer ──────────────────────────────────────────────────────

# xlsx column name → Altium reserved parameter name.
# Square brackets = Altium internal field (Library Ref, Footprint Ref, etc.).
_RESERVED_PARAM = {
    'Library Ref':    '[Library Ref]',
    'Library Path':   '[Library Path]',
    'Footprint Ref':  '[Footprint Ref 1]',
    'Footprint Path': '[Footprint Path 1]',
    'Designator':     '[Component Designator]',
    'Comment':        '[Component Comment]',
    'Description':    '[Description]',
}
# These internal-linkage fields are hidden from BOM dialogs (VisibleOnAdd=False)
_INTERNAL_RESERVED = {'[Library Ref]', '[Library Path]',
                      '[Footprint Ref 1]', '[Footprint Path 1]'}


def _write_dblib(dblib_path, xlsx_name, all_cols):
    """Write Altium .DbLib INI config file."""
    lines = [
        '[Design]',
        f'ConnectionString=Provider=Microsoft.ACE.OLEDB.12.0;Data Source={xlsx_name};'
        f'Extended Properties="Excel 12.0 Xml;HDR=YES;IMEX=1"',
        'AddMode=0',
        'RemoveMode=0',
        'SyncMode=1',
        'SelectMode=0',
        'ParamMode=0',
        'ReadonlyContent=F',
        'LibraryDatabaseType=Altium',
        '',
        '[Table1]',
        'TableName=Components$',
        'Enabled=True',
        'IconFilename=',
        'AutoEnable=True',
        'DatabaseType=Components',
        '',
    ]

    def _fm(idx, col, param_name, field_type, visible):
        v = 'True' if visible else 'False'
        return [
            f'[FieldMap{idx}]',
            (f'Options=FieldName=Components$.{col}'
             f'|TableNameOnly=Components$'
             f'|FieldNameOnly={col}'
             f'|FieldType={field_type}'
             f'|ParameterName={param_name}'
             f'|VisibleOnAdd={v}|AddMode=0|RemoveMode=0|UpdateMode=0|IsVisible={v}'),
            '',
        ]

    # Key field (Part Number) — FieldType=0
    lines += _fm(1, all_cols[0], '[Part Number]', 0, True)

    for i, col in enumerate(all_cols[1:], 2):
        param   = _RESERVED_PARAM.get(col, col)
        visible = param not in _INTERNAL_RESERVED
        lines  += _fm(i, col, param, 1, visible)

    Path(dblib_path).write_text('\n'.join(lines), encoding='utf-8')
    print(f'Written: {dblib_path}')


# ─── PCB footprint ────────────────────────────────────────────────────────────

def _board_cutouts(fp_el, fp):
    """Closed loops on the IR dimension layer inside a FOOTPRINT are the
    milling contour the package brings with it (a JST connector's slot).
    Altium's native home for that is a Board Cutout region, and putting it
    in the LIBRARY means the board inherits it through ordinary placement.

    Returns the child elements consumed, so they are not also drawn as plain
    mechanical tracks — the cutout region IS the contour.

    The region outline is polygonal (Altium's own board-cutout form), so arcs
    are flattened; _CUTOUT_SAG_UM is the chord tolerance.
    """
    segs, owner = [], {}
    for child in fp_el:
        if child.tag not in ('line', 'arc'):
            continue
        try:
            if int(child.get('layer', '0')) != LAYER_DIMENSION:
                continue
        except ValueError:
            continue
        seg = (float(child.get('x1')), float(child.get('y1')),
               float(child.get('x2')), float(child.get('y2')),
               float(child.get('curve', 0) or 0))
        segs.append(seg)
        owner[seg] = child
    if not segs:
        return set()

    loops, leftover = chain_loops(segs)
    consumed = {id(owner[s]) for loop in loops for s in loop
                if s in owner}          # reversed segments keep their own id
    for loop in loops:
        for s in loop:
            if s not in owner:          # traversed backwards -> find the twin
                x1, y1, x2, y2, c = s
                consumed |= {id(owner[t]) for t in owner
                             if t[:2] == (x2, y2) and t[2:4] == (x1, y1)}
        fp.add_region(
            outline_points_mils=[(_milf(x), _milf(y))
                                 for x, y in flatten_loop(loop, _CUTOUT_SAG_UM)],
            layer=PcbLayer.KEEPOUT,
            kind=PcbRegionKind.BOARD_CUTOUT,
            is_board_cutout=True,
        )
    return consumed


def _export_footprint(fp_el, pcblib, layer_plan, step_dir=None, user=None):
    """One IR <footprint> -> a PcbLib entry.

    `user`: a designator that carries this footprint on the board, or None
    when no element does (a library variant nothing selects). It rides along
    only so a log line names a PLACE and not just a library entry — a
    footprint name alone leaves you searching the board by eye.
    """
    where = '%s (used by %s)' % (fp_el.get('name', ''),
                                 user or 'no element on the board')
    fp = pcblib.add_footprint(fp_name(fp_el))
    cut = _board_cutouts(fp_el, fp)

    for child in fp_el:
        if id(child) in cut:
            continue
        tag = child.tag
        if tag in ('model3d', 'pin-mapping', 'description', 'attributes'):
            continue
        if tag in ('smd', 'pad', 'hole'):
            pcb_layer = None            # pads pick their own layer below
            smd_far = child.get('layer') == '-1'
        else:
            pcb_layer, keepout = target_layer(layer_plan, child.get('layer'))
            if pcb_layer is None:
                # Every branch below is guarded by `pcb_layer is not None`,
                # so an IR layer missing from the table used to vanish in
                # silence — the one thing this project does not do.
                import_log.log(where, tag,
                               'no Altium layer for IR layer',
                               str(child.get('layer')))


        if tag == 'smd':
            roundness = int(child.get('roundness', 0))
            if roundness == 100:
                shape = PadShape.CIRCLE
            elif roundness > 0:
                shape = PadShape.ROUNDED_RECTANGLE
            else:
                shape = PadShape.RECTANGLE
            fp.add_pad(
                designator            = child.get('name', ''),
                position_mils         = [_milf(child.get('x', 0)),
                                         _milf(child.get('y', 0))],
                width_mils            = _milf(child.get('width', 0)),
                height_mils           = _milf(child.get('height', 0)),
                layer                 = (PcbLayer.BOTTOM if smd_far
                                         else PcbLayer.TOP),
                shape                 = shape,
                rotation_degrees      = float(child.get('rot', 0)),
                corner_radius_percent = roundness if roundness > 0 else None,
            )

        elif tag == 'pad':
            drill    = _milf(child.get('drill', 0))
            shape    = (PadShape.RECTANGLE if child.get('shape', 'round') == 'square'
                        else PadShape.CIRCLE)
            pad_size = (_milf(child.get('diameter'))
                        if child.get('diameter') else drill * 1.8)
            fp.add_pad(
                designator    = child.get('name', ''),
                position_mils = [_milf(child.get('x', 0)),
                                 _milf(child.get('y', 0))],
                width_mils    = pad_size,
                height_mils   = pad_size,
                layer         = PcbLayer.MULTI_LAYER,
                shape         = shape,
                hole_size_mils= drill,
                # An IR <pad> IS the plated kind — the unplated one is <hole>,
                # and that distinction is the whole difference between them.
                # Altium's record defaults to unplated, so every through-hole
                # pad we ever wrote came out with no barrel.
                plated        = True,
            )

        elif tag == 'hole':
            # A mounting hole a footprint brings with it — dropped entirely
            # until now, because the branch simply did not exist.
            fp.add_pad(position_mils=[_milf(child.get('x', 0)),
                                      _milf(child.get('y', 0))],
                       **npth_pad_kwargs(child))

        elif tag == 'line' and pcb_layer is not None:
            rec = fp.add_track(
                [_milf(child.get('x1')), _milf(child.get('y1'))],
                [_milf(child.get('x2')), _milf(child.get('y2'))],
                width_mils=_milf(child.get('width', 100)),
                layer=pcb_layer,
            )
            if keepout:
                mark_keepout(rec)

        elif tag == 'text' and pcb_layer is not None:
            content = child.text or ''
            is_des  = (content == '>NAME')
            is_com  = (content == '>VALUE')
            if is_des or is_com or not content.startswith('>'):
                fp.add_text(
                    text               = ('.Designator' if is_des else
                                          '.Comment'    if is_com else content),
                    position_mils      = (_milf(child.get('x', '0')),
                                          _milf(child.get('y', '0'))),
                    height_mils        = max(_milf(child.get('size', '1000')), 20),
                    layer              = pcb_layer,
                    rotation_degrees   = float(child.get('rot', '0')),
                    stroke_width_mils  = 5.0,
                    is_designator      = is_des,
                    is_comment         = is_com,
                    text_justification = _pcb_justif(child.get('align', 'bottom-left')),
                )

        elif tag == 'shape' and pcb_layer is not None:
            for prim in shape_primitives(child):
                if prim[0] == 'arc':
                    _, cx, cy, r, a1, a2, w = prim
                    rec = fp.add_arc(center_mils=[_milf(cx), _milf(cy)],
                                     radius_mils=_milf(r),
                                     start_angle_degrees=a1, end_angle_degrees=a2,
                                     width_mils=_milf(w), layer=pcb_layer)
                elif prim[0] == 'fill':
                    _, x1, y1, x2, y2, rot = prim
                    rec = fp.add_fill(corner1_mils=(_milf(x1), _milf(y1)),
                                      corner2_mils=(_milf(x2), _milf(y2)),
                                      layer=pcb_layer, rotation_degrees=rot)
                else:
                    _, x1, y1, x2, y2, w = prim
                    rec = fp.add_track([_milf(x1), _milf(y1)],
                                       [_milf(x2), _milf(y2)],
                                       width_mils=_milf(w), layer=pcb_layer)
                if keepout:
                    mark_keepout(rec)

        elif tag == 'polygon' and pcb_layer is not None:
            # A filled area inside a footprint (an Eagle logo is 19 of them)
            # is an Altium REGION — the placement copies regions like any
            # other primitive. The contour is the IR pen centreline plus its
            # width, offset outward exactly as the board pours are, and then
            # flattened: a region outline is a plain point list.
            verts = [(float(v.get('x')), float(v.get('y')),
                      float(v.get('curve', 0) or 0))
                     for v in child.findall('vertex')]
            width = float(child.get('width', 0) or 0)
            # Two vertices enclose nothing only while BOTH edges are straight.
            # One curved edge and the closing chord make a circular segment —
            # a real area, and real libraries carry them (CAP_SMD_6.3X7.7 of
            # Base draws its polarity mark that way: a 4300 µm chord and an
            # 88.58° arc, 2.6 mm²). ir_util.offset_contour's pen model needs
            # three corners, so such a contour is not exported yet; it is
            # skipped as UNSUPPORTED, not as empty.
            pts = []
            if len(verts) >= 3:
                loop = [(x1, y1, x2, y2, c) for _kind, x1, y1, x2, y2, c
                        in offset_contour(verts, width / 2)]
                pts = [(_milf(x), _milf(y))
                       for x, y in flatten_loop(loop, _CUTOUT_SAG_UM)]
            if len(pts) >= 3:
                rec = fp.add_region(outline_points_mils=pts, layer=pcb_layer,
                                    kind=PcbRegionKind.COPPER)
                if keepout:
                    mark_keepout(rec)
            elif len(verts) == 2 and any(c for _x, _y, c in verts):
                import_log.log(where, tag,
                               'POLYGON is an arc and its chord (2 vertices, '
                               'one of them curved) — a real area the pen '
                               'model cannot offset yet, NOT exported')
            else:
                import_log.log(where, tag,
                               f'POLYGON has {len(verts)} vertices and no '
                               'curve — encloses nothing, dropped')

        elif tag == 'arc' and pcb_layer is not None:
            p = arc_params(float(child.get('x1', 0)), float(child.get('y1', 0)),
                           float(child.get('x2', 0)), float(child.get('y2', 0)),
                           float(child.get('curve', 0)))
            if p is None:
                continue
            cx_um, cy_um, r_um, start, sweep = p
            a1, a2 = _altium_arc_angles(start, sweep)
            fp.add_arc(
                center_mils         = [_milf(cx_um), _milf(cy_um)],
                radius_mils         = _milf(r_um),
                start_angle_degrees = a1,
                end_angle_degrees   = a2,
                width_mils          = _milf(child.get('width', 100)),
                layer               = pcb_layer,
            )

        else:
            # Nothing may leave the footprint in silence: <polygon> did for a
            # long time, and two Eagle logos arrived in Altium as empty
            # components because of it.
            import_log.log(where, tag,
                           'FOOTPRINT primitive not exported')

    # 3D model — embed STEP file if available
    m3d = fp_el.find('model3d')
    if m3d is not None and step_dir is not None:
        step_path = resolve_model3d_file(fp_el, step_dir)
        if step_path is not None:
            fname = step_path.name
            try:
                # NOT the IR angles per axis: Altium's model dialog states a
                # Rz*Ry*Rx composition, the IR an Rx*Ry*Rz one, and the two
                # coincide only for pure-Z rotations. DD1 of Base (LQFP100)
                # is IR (0, -90, -90) and stood wrong until the user entered
                # (90, 0, -90) by hand — exactly what the shared law gives,
                # and exactly what the KiCad path has used since 2026-07-15.
                rot_x, rot_y, rot_z = model3d_dialog_rotation(m3d)
                model = pcblib.add_embedded_model(
                    name               = fname,
                    model_data         = step_path.read_bytes(),
                    rotation_x_degrees = rot_x,
                    rotation_y_degrees = rot_y,
                    rotation_z_degrees = rot_z,
                    z_offset_mils      = round(float(m3d.get('tz', 0)) / 25.4),
                )
                fp.add_embedded_3d_model(
                    model,
                    location_mils        = (_mils(m3d.get('tx', '0')),
                                            _mils(m3d.get('ty', '0'))),
                    standoff_height_mils = round(float(m3d.get('tz', 0)) / 25.4),
                )
                print(f'  3D: {fname} -> {fp_el.get("name", "")}')
            except Exception as e:
                print(f'  ! 3D {fname}: {e}')


# ─── entry point ─────────────────────────────────────────────────────────────

def export(ir_path, output_dir=None):
    tree     = ET.parse(ir_path)
    ir_root  = tree.getroot()
    lib_name = ir_root.get('name', Path(ir_path).stem)

    out_dir = Path(output_dir) if output_dir else Path(ir_path).parent
    out_dir.mkdir(parents=True, exist_ok=True)

    sch_path    = out_dir / f'{lib_name}.SchLib'
    pcb_path    = out_dir / f'{lib_name}.PcbLib'
    xlsx_path   = out_dir / f'{lib_name}.xlsx'
    dblib_path  = out_dir / f'{lib_name}.DbLib'
    libpkg_path = out_dir / f'{lib_name}.LibPkg'

    # 1. SchLib — pool symbols (single-gate DbLib-style) + multi-part entries
    schlib   = AltiumSchLib()
    schlib._ensure_font_manager()
    sym_pool = {s.get('name'): s for s in ir_root.findall('symbols/symbol')}
    standalone_syms = [s for s in sym_pool.values()
                       if _sym_used_standalone(s.get('name', ''), ir_root)]
    for sym_el in standalone_syms:
        _export_schlib_symbol(sym_el, schlib, ir_root)
    multi_gate_comps = [c for c in ir_root.findall('component') if is_multi_gate(c)]
    for comp_el in multi_gate_comps:
        _export_schlib_multipart(comp_el, sym_pool, schlib)
    schlib.save(sch_path)
    n_skipped = len(sym_pool) - len(standalone_syms)
    n_multi   = len(multi_gate_comps)
    print('Written: %s  (%d standalone symbols%s%s)' % (
        sch_path,
        len(standalone_syms),
        (', %d multi-part' % n_multi) if n_multi else '',
        (', %d gate-only skipped' % n_skipped) if n_skipped else '',
    ))

    # 2. PcbLib — one entry per unique footprint name.
    # The mechanical-layer plan is declared IN the library, so the file says
    # what each of its mechanical layers means (altium_layers.tsv is the one
    # table; the board export plans from the same IR and gets the same slots).
    pcblib   = AltiumPcbLib()
    layer_plan, layer_pairs = altium_layers.plan(altium_layers.layers_used(ir_root))
    altium_layers.declare(pcblib, layer_plan, layer_pairs)
    seen_fp: set[str] = set()
    fp_users = footprint_users(ir_root)
    step_dir = Path(ir_path).parent / Path(ir_path).stem
    # A board-only padless element (Eagle artwork/logo) has no component:
    # its footprint hangs on the LAYOUT, and the PcbDoc still places it.
    for owner in (list(ir_root.findall('component'))
                  + list(ir_root.findall('layout'))):
        for fp_el in owner.findall('footprint'):
            fp_id = fp_el.get('name', '')
            if fp_id not in seen_fp:
                seen_fp.add(fp_id)
                _export_footprint(fp_el, pcblib, layer_plan,
                                  step_dir=step_dir,
                                  user=fp_users.get(fp_id))
    pcblib.save(pcb_path)
    print(f'Written: {pcb_path}  ({len(seen_fp)} footprints)')

    # 3. Component table → xlsx
    rows, all_cols = _build_component_rows(ir_root, sch_path.name, pcb_path.name)
    _write_xlsx(rows, all_cols, xlsx_path)

    # 4. DbLib config (INI pointing Altium at the Excel table via ACE OLEDB)
    _write_dblib(dblib_path, xlsx_path.name, all_cols)

    # 5. LibPkg (optional: compile SchLib+PcbLib → IntLib without DbLib)
    libpkg_path.write_text('\n'.join([
        '[Design]', 'Version=1.0', 'HierarchyMode=0',
        'OutputPath=Project Outputs', '',
        '[Document1]', f'DocumentPath={sch_path.name}', 'DocumentUniqueId=', '',
        '[Document2]', f'DocumentPath={pcb_path.name}', 'DocumentUniqueId=', '',
    ]), encoding='utf-8')
    print(f'Written: {libpkg_path}')

    n = len(ir_root.findall('component'))
    print('Total: %d components, %d pool symbols, %d footprints' % (
        n, len(sym_pool), len(seen_fp)))
