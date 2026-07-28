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
from altium_monkey.altium_pcb_enums import PadShape, PcbTextJustification
from altium_monkey.altium_record_types import PcbLayer, LineWidth
from altium_monkey.altium_sch_svg_renderer import LINE_WIDTH_MILS

from babel.ir_util import (component_gates, is_multi_gate, parse_layer,
                           resolve_model3d_file, arc_params)


def _mils(v):
    """IR µm → Altium mils (integer)."""
    return round(float(v) / 25.4)


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

# IR signed layer number (ir_schema.md "Плата (Board IR)") -> Altium PcbLayer.
_IR_TO_PCB_LAYER = {
    1:    PcbLayer.TOP,
    -1:   PcbLayer.BOTTOM,
    121:  PcbLayer.TOP_OVERLAY,
    -121: PcbLayer.BOTTOM_OVERLAY,
    125:  PcbLayer.TOP_OVERLAY,     # Eagle tNames/tValues text -> overlay
    -125: PcbLayer.BOTTOM_OVERLAY,
    127:  PcbLayer.TOP_OVERLAY,
    -127: PcbLayer.BOTTOM_OVERLAY,
    131:  PcbLayer.TOP_PASTE,
    -131: PcbLayer.BOTTOM_PASTE,
    139:  71,   # courtyard -> MECHANICAL_15
    -139: 71,
    151:  57,   # fab -> MECHANICAL_1
    -151: 57,
    148:  57,   # side-less Document notes -> MECHANICAL_1 too
}


def _pcb_layer(ln):
    """IR footprint layer attribute -> Altium PcbLayer, or None (drop)."""
    try:
        anti, n = parse_layer(ln)
    except (TypeError, ValueError):
        return None
    if anti:
        return None
    return _IR_TO_PCB_LAYER.get(n)


# ─── pin mapping helpers ─────────────────────────────────────────────────────

def _pin_des_map(comp_el, gate_name):
    """Build {pin_name: [pad_designators]} for one gate from the first footprint."""
    result = {}
    fp_el  = comp_el.find('footprint')
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


def _derive_footprints_for_sym(sym_name, ir_root):
    """Return list of footprint names for the first component that references sym_name."""
    for comp_el in ir_root.findall('component'):
        for _, sname in component_gates(comp_el):
            if sname == sym_name:
                return [fp.get('name', '') for fp in comp_el.findall('footprint')]
    return []


def _sym_used_standalone(sym_name, ir_root):
    """True if at least one single-gate component references this symbol directly."""
    for comp_el in ir_root.findall('component'):
        if comp_el.get('symbol') == sym_name:
            return True
    return False


def _footprint_impl_with_map(fp_name, pad_lists, is_current=True):
    """Build (impl_record, children) for a footprint implementation + MAP_DEFINERs.

    pad_lists: iterable of pad-name lists, one list per schematic pin.
    Each list's first element becomes DesIntf (pin designator on the schematic).
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
    for pads in pad_lists:
        if pads:
            md = AltiumSchMapDefiner()
            md.designator_interface       = pads[0]
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
    sym   = schlib.add_symbol(sname)

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
    fp_names = _derive_footprints_for_sym(sname, ir_root)
    for i, fp_name in enumerate(fp_names):
        impl, children = _footprint_impl_with_map(fp_name, des_map.values(), is_current=(i == 0))
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
    sym   = schlib.add_symbol(cname)
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

    # Each gate → one numbered part (1-indexed); collect pad lists in gate order
    all_pad_lists = []
    for i, (gate_name, sym_name) in enumerate(gates, 1):
        gate_sym_el = sym_pool.get(sym_name)
        if gate_sym_el is None:
            print('  ! multi-gate %s: pool symbol %r not found' % (cname, sym_name))
            continue
        gate_des_map = _pin_des_map(comp_el, gate_name)
        all_pad_lists.extend(gate_des_map.values())
        _add_gate_to_symbol(sym, gate_sym_el, gate_des_map, schlib, owner_part_id=i)

    # Footprint implementations with MAP_DEFINERs covering all gates in order (all footprints; first = IsCurrent)
    for i, fp_el in enumerate(comp_el.findall('footprint')):
        impl, children = _footprint_impl_with_map(fp_el.get('name', ''), all_pad_lists, is_current=(i == 0))
        sym.add_implementation(impl, children)


# ─── component table helpers ──────────────────────────────────────────────────

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
                'Library Ref':    sym_name,
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
                fp_name = fp_el_i.get('name', '')
                rows.append(_make_row(f'{cname}_{fp_name}', fp_name, fp_el_i))

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

def _export_footprint(fp_el, pcblib, step_dir=None):
    fp = pcblib.add_footprint(fp_el.get('name', ''))

    for child in fp_el:
        tag = child.tag
        if tag in ('model3d', 'pin-mapping', 'description', 'attributes'):
            continue
        if tag in ('smd', 'pad', 'hole'):
            pcb_layer = None            # pads pick their own layer below
            smd_far = child.get('layer') == '-1'
        else:
            pcb_layer = _pcb_layer(child.get('layer'))


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
                position_mils         = [_mils(child.get('x', 0)),
                                         _mils(child.get('y', 0))],
                width_mils            = _mils(child.get('width', 0)),
                height_mils           = _mils(child.get('height', 0)),
                layer                 = (PcbLayer.BOTTOM if smd_far
                                         else PcbLayer.TOP),
                shape                 = shape,
                rotation_degrees      = float(child.get('rot', 0)),
                corner_radius_percent = roundness if roundness > 0 else None,
            )

        elif tag == 'pad':
            drill    = _mils(child.get('drill', 0))
            shape    = (PadShape.RECTANGLE if child.get('shape', 'round') == 'square'
                        else PadShape.CIRCLE)
            pad_size = (_mils(child.get('diameter'))
                        if child.get('diameter') else round(drill * 1.8))
            fp.add_pad(
                designator    = child.get('name', ''),
                position_mils = [_mils(child.get('x', 0)),
                                 _mils(child.get('y', 0))],
                width_mils    = pad_size,
                height_mils   = pad_size,
                layer         = PcbLayer.MULTI_LAYER,
                shape         = shape,
                hole_size_mils= drill,
            )

        elif tag == 'line' and pcb_layer is not None:
            fp.add_track(
                [_mils(child.get('x1')), _mils(child.get('y1'))],
                [_mils(child.get('x2')), _mils(child.get('y2'))],
                width_mils=_mils(child.get('width', 100)),
                layer=pcb_layer,
            )

        elif tag == 'text' and pcb_layer is not None:
            content = child.text or ''
            is_des  = (content == '>NAME')
            is_com  = (content == '>VALUE')
            if is_des or is_com or not content.startswith('>'):
                fp.add_text(
                    text               = ('.Designator' if is_des else
                                          '.Comment'    if is_com else content),
                    position_mils      = (_mils(child.get('x', '0')),
                                          _mils(child.get('y', '0'))),
                    height_mils        = max(_mils(child.get('size', '1000')), 20),
                    layer              = pcb_layer,
                    rotation_degrees   = float(child.get('rot', '0')),
                    stroke_width_mils  = 5.0,
                    is_designator      = is_des,
                    is_comment         = is_com,
                    text_justification = _pcb_justif(child.get('align', 'bottom-left')),
                )

        elif tag == 'shape' and pcb_layer is not None:
            rn         = int(child.get('roundness', 0))
            cx         = _mils(child.get('x', 0))
            cy         = _mils(child.get('y', 0))
            outline_um = float(child.get('outline', '0'))
            if rn == 100:
                r_um = float(child.get('w', '0')) / 2
                if outline_um == 0:
                    fp.add_arc(
                        center_mils         = [cx, cy],
                        radius_mils         = round(r_um / 50.8),
                        start_angle_degrees = 0,
                        end_angle_degrees   = 360,
                        width_mils          = round(r_um / 25.4),
                        layer               = pcb_layer,
                    )
                else:
                    fp.add_arc(
                        center_mils         = [cx, cy],
                        radius_mils         = round(r_um / 25.4),
                        start_angle_degrees = 0,
                        end_angle_degrees   = 360,
                        width_mils          = _mils(child.get('outline', '0')),
                        layer               = pcb_layer,
                    )
            else:
                hw = round(float(child.get('w', '0')) / 50.8)
                hh = round(float(child.get('h', '0')) / 50.8)
                if outline_um == 0:
                    fp.add_fill(
                        corner1_mils = (cx - hw, cy - hh),
                        corner2_mils = (cx + hw, cy + hh),
                        layer        = pcb_layer,
                    )
                else:
                    lw = _mils(str(outline_um)) or 4
                    corners = [(cx-hw, cy-hh), (cx+hw, cy-hh),
                               (cx+hw, cy+hh), (cx-hw, cy+hh)]
                    for (ax, ay), (bx, by) in zip(corners, corners[1:] + corners[:1]):
                        fp.add_track([ax, ay], [bx, by],
                                     width_mils=lw, layer=pcb_layer)

        elif tag == 'arc' and pcb_layer is not None:
            p = arc_params(float(child.get('x1', 0)), float(child.get('y1', 0)),
                           float(child.get('x2', 0)), float(child.get('y2', 0)),
                           float(child.get('curve', 0)))
            if p is None:
                continue
            cx_um, cy_um, r_um, start, sweep = p
            a1, a2 = _altium_arc_angles(start, sweep)
            fp.add_arc(
                center_mils         = [_mils(cx_um), _mils(cy_um)],
                radius_mils         = _mils(r_um),
                start_angle_degrees = a1,
                end_angle_degrees   = a2,
                width_mils          = _mils(child.get('width', 100)),
                layer               = pcb_layer,
            )

    # 3D model — embed STEP file if available
    m3d = fp_el.find('model3d')
    if m3d is not None and step_dir is not None:
        step_path = resolve_model3d_file(fp_el, step_dir)
        if step_path is not None:
            fname = step_path.name
            try:
                model = pcblib.add_embedded_model(
                    name               = fname,
                    model_data         = step_path.read_bytes(),
                    rotation_x_degrees = float(m3d.get('rx', 0)),
                    rotation_y_degrees = float(m3d.get('ry', 0)),
                    rotation_z_degrees = float(m3d.get('rz', 0)),
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

    # 2. PcbLib — one entry per unique footprint name
    pcblib   = AltiumPcbLib()
    seen_fp: set[str] = set()
    step_dir = Path(ir_path).parent / Path(ir_path).stem
    for comp_el in ir_root.findall('component'):
        for fp_el in comp_el.findall('footprint'):
            fp_id = fp_el.get('name', '')
            if fp_id not in seen_fp:
                seen_fp.add(fp_id)
                _export_footprint(fp_el, pcblib, step_dir=step_dir)
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
