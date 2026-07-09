"""Altium .IntLib -> IR XML converter."""
import hashlib
import math
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
from xml.dom import minidom
from pathlib import Path

from altium_monkey import AltiumIntLib, AltiumSchLib, AltiumPcbLib
from babel.ir_util import sanitize_filename, clean_attr_name, arc_endpoints
from babel.altium_exporter import _um_lw

_MILS_TO_UM = 25.4   # 1 mil = 25.4 µm


def _pt_to_um(pt):
    """Altium font point size → IR text size, µm.

    Inverse of altium_exporter._font_id: Altium's point size does not follow
    the standard 72pt/inch typographic convention for rendered letter height —
    empirically, an 8pt font renders ~1.2mm tall, i.e. 1pt ~= 150 µm.
    """
    return round(float(pt) * 150)

_ELECTRICAL = {
    'Input':          'in',
    'Output':         'out',
    'Bidirectional':  'io',
    'Power':          'pwr',
    'Passive':        'pas',
    'Open Collector': 'out',
    'Open Emitter':   'out',
    'HiZ':            'out',
}
# Altium's own PinElectrical enum has no "Not Connected"/`nc` member at all
# (confirmed against altium_monkey's PIN_ELECTRICAL_NAMES — 8 values, none
# of them this) — unlike KiCad's `no_connect` and Eagle's `nc`, Altium
# genuinely has no electrical-type equivalent to import FROM. Falls through
# to the dict's own `.get(..., 'pas')` default below, same as any other
# unrecognized value — not a gap to plug, there's nothing on the Altium
# side to read.

_ORIENT_TO_ROT = {0: 0, 1: 90, 2: 180, 3: 270}  # for text

# Altium pin coord = body end; IR/Eagle pin coord = hot end (wire side).
# hot_end = body_end + length * direction(orient)
# IR rot = (altium_orient_deg + 180) % 360
_PIN_ORIENT_TO_ROT = {0: 180, 1: 270, 2: 0, 3: 90}

# altium_monkey TextJustification value → IR align string
_JUSTIFICATION = {
    0: 'bottom-left',
    1: 'bottom-center',
    2: 'bottom-right',
    3: 'center-left',
    4: 'center',
    5: 'center-right',
    6: 'top-left',
    7: 'top-center',
    8: 'top-right',
}

# Mirror vertical component of align (bottom↔top) for >VALUE placement
_FLIP_VERT = {
    'bottom-left':   'top-left',
    'bottom-center': 'top-center',
    'bottom-right':  'top-right',
    'center-left':   'center-left',
    'center':        'center',
    'center-right':  'center-right',
    'top-left':      'bottom-left',
    'top-center':    'bottom-center',
    'top-right':     'bottom-right',
}

# Altium PcbLayer ID → IR signed layer number as a string (ir_schema.md
# "Плата (Board IR)", canonical constants in ir_util.py).
# (numbering used by altium-monkey's PcbLayer enum)
# 1=Top, 32=Bottom, 33=TopOverlay, 34=BottomOverlay, 35=TopPaste, 36=BottomPaste,
# 57-72=Mechanical1-16, 74=Multi-Layer
_LAYER_MAP = {
    1:  '1',
    32: '-1',
    33: '121',
    34: '-121',
    35: '131',
    36: '-131',
    74: '1',             # Multi-Layer (TH pads through all layers)
}
for _i in range(57, 73):
    _LAYER_MAP[_i] = '151' if (_i % 2 == 1) else '139'   # fab / courtyard
_LAYER_MAP[67] = '139'   # Mech 11 — IPC courtyard convention
_LAYER_MAP[69] = '139'   # Mech 13
_LAYER_MAP[71] = '139'   # Mech 15


def _um(mils):
    """mils → integer µm string."""
    return str(round(float(mils) * _MILS_TO_UM))


def _f(v):
    """Format angle/ratio as compact float (degrees, percentages — not lengths)."""
    r = round(float(v), 4)
    return str(int(r)) if r == int(r) else str(r)


def _part_letter(n):
    """1-indexed Altium part id -> letter gate suffix (1->A, 2->B, ...).

    altium_monkey only exposes the numeric owner_part_id; the letter suffix
    (HL1A/HL1B) is purely an Altium UI convention applied when placing a
    multi-part symbol, not stored data — so this mapping is our own, chosen
    to match what an Altium user would actually see on the sheet.
    """
    return chr(ord('A') + n - 1)


def _iter_named_pins(sym, part_id=None):
    """Yield (ir_pin_name, pin) for sym's pins, filtered to one multi-part
    gate (part_id) or all of them (part_id=None), with the '@N' dedup-suffix
    numbering used everywhere an IR pin name is derived from a pin.

    Shared between symbol drawing and footprint pin-mapping so the two can't
    drift apart and disagree on a pin's name.
    """
    def _keep(pin):
        if part_id is None:
            return True
        opid = getattr(pin, 'owner_part_id', None)
        return opid is None or opid in (-1, 0) or opid == part_id

    seen: dict[str, int] = {}
    for pin in sym.pins:
        if not _keep(pin):
            continue
        pname = clean_attr_name(pin.name or str(pin.designator))
        seen[pname] = seen.get(pname, 0) + 1
        n = seen[pname]
        yield (pname if n == 1 else f'{pname}@{n}', pin)


def _convert_symbol(sym, sym_name, part_id=None):
    """Build a pool-ready <symbol name=...> from an altium-monkey symbol.

    part_id, if given, restricts graphics to one part of a multi-part Altium
    symbol: objects owned by that part (owner_part_id == part_id) plus objects
    shared across all parts (owner_part_id in (None, -1, 0) — e.g. the
    >NAME/>Value placeholders) are kept; another part's objects are dropped.
    With part_id=None (single-part symbols) everything is kept, matching the
    old behaviour.
    """
    def _keep(obj):
        if part_id is None:
            return True
        opid = getattr(obj, 'owner_part_id', None)
        return opid is None or opid in (-1, 0) or opid == part_id

    sym_el = ET.Element('symbol', name=sym_name)

    for rect in filter(_keep, sym.rectangles):
        loc = rect.location_mils
        cor = rect.corner_mils
        x1  = _um(loc.x_mils); y1 = _um(loc.y_mils)
        x2  = _um(cor.x_mils); y2 = _um(cor.y_mils)
        w   = str(_um_lw(rect.line_width))
        for ax1, ay1, ax2, ay2 in [(x1,y1,x2,y1),(x2,y1,x2,y2),(x2,y2,x1,y2),(x1,y2,x1,y1)]:
            ET.SubElement(sym_el, 'line',
                          x1=ax1, y1=ay1, x2=ax2, y2=ay2, width=w)

    for pl in filter(_keep, sym.polylines):
        pts = list(pl.points_mils)
        w   = str(_um_lw(pl.line_width))
        for a, b in zip(pts, pts[1:]):
            ET.SubElement(sym_el, 'line',
                          x1=_um(a.x_mils), y1=_um(a.y_mils),
                          x2=_um(b.x_mils), y2=_um(b.y_mils),
                          width=w)

    for ln in filter(_keep, sym.lines):
        loc = ln.location_mils
        cor = ln.corner_mils
        ET.SubElement(sym_el, 'line',
                      x1=_um(loc.x_mils), y1=_um(loc.y_mils),
                      x2=_um(cor.x_mils), y2=_um(cor.y_mils),
                      width=str(_um_lw(ln.line_width)))

    for arc in filter(_keep, sym.arcs):
        loc = arc.location_mils
        radius_mils = arc.radius_mils
        # AltiumSchEllipticalArc is a subclass of AltiumSchArc (so it's
        # already included here) and adds a second, minor-axis radius. IR has
        # no ellipse-arc primitive, so collapse to a circular arc using the
        # smaller of the two radii — stays inside the original ellipse.
        secondary_mils = getattr(arc, 'secondary_radius_mils', None)
        if secondary_mils is not None:
            radius_mils = min(radius_mils, secondary_mils)
        # Same '% 360 or 360' fallback as the PCB arc loop below: a full
        # circle drawn as start=0/end=360 must not collapse to a 0° sweep.
        # Altium's native arc form is the CENTER one — the endpoint-canon
        # conversion (ir_util's arc-math block) happens here, at the Altium
        # boundary; full circles are <shape roundness=100>, not arcs.
        sweep = (arc.end_angle - arc.start_angle) % 360 or 360
        if sweep >= 360:
            ET.SubElement(sym_el, 'shape',
                          x=_um(loc.x_mils), y=_um(loc.y_mils),
                          w=_um(radius_mils * 2), h=_um(radius_mils * 2),
                          roundness='100', rot='0',
                          outline=str(_um_lw(arc.line_width)))
        else:
            x1, y1, x2, y2, curve = arc_endpoints(
                float(loc.x_mils), float(loc.y_mils), float(radius_mils),
                float(arc.start_angle), sweep)
            ET.SubElement(sym_el, 'arc',
                          x1=_um(x1), y1=_um(y1), x2=_um(x2), y2=_um(y2),
                          curve=_f(curve),
                          width=str(_um_lw(arc.line_width)))

    for ell in filter(_keep, sym.ellipses):
        # Same min-radius collapse as elliptical arcs, as a full circle.
        loc = ell.location_mils
        radius_mils = min(ell.radius_mils, ell.secondary_radius_mils)
        ET.SubElement(sym_el, 'shape',
                      x=_um(loc.x_mils), y=_um(loc.y_mils),
                      w=_um(radius_mils * 2), h=_um(radius_mils * 2),
                      roundness='100', rot='0',
                      outline=str(_um_lw(ell.line_width)))

    for pol in filter(_keep, sym.polygons):
        pts = list(pol.points_mils)
        w   = str(_um_lw(pol.line_width))
        for a, b in zip(pts, pts[1:] + [pts[0]]):
            ET.SubElement(sym_el, 'line',
                          x1=_um(a.x_mils), y1=_um(a.y_mils),
                          x2=_um(b.x_mils), y2=_um(b.y_mils),
                          width=w)

    for ir_pin_name, pin in _iter_named_pins(sym, part_id):
        direction = _ELECTRICAL.get(pin.electrical_name, 'pas')
        orient    = pin.orientation  # 0-3
        rot       = _PIN_ORIENT_TO_ROT.get(orient, 180)
        # Altium x,y = body end; shift to hot end (wire connection point)
        orient_deg = orient * 90
        length_um  = float(pin.length_mils) * _MILS_TO_UM
        px_um = float(pin.x_mils) * _MILS_TO_UM + length_um * math.cos(math.radians(orient_deg))
        py_um = float(pin.y_mils) * _MILS_TO_UM + length_um * math.sin(math.radians(orient_deg))
        ET.SubElement(sym_el, 'pin',
                      name=ir_pin_name,
                      x=str(round(px_um)),
                      y=str(round(py_um)),
                      rot=str(rot),
                      length=str(round(length_um)),
                      direction=direction,
                      pinvis='1' if pin.show_name else '0',
                      padvis='1' if pin.show_designator else '0')

    for lbl in filter(_keep, sym.labels):
        if lbl.is_hidden or not lbl.text:
            continue
        lx    = _um(lbl.location.x_mils)
        ly    = _um(lbl.location.y_mils)
        lalign = _JUSTIFICATION.get(lbl.justification.value if hasattr(lbl.justification, 'value') else 0, 'bottom-left')
        lrot   = _ORIENT_TO_ROT.get(lbl.orientation.value if hasattr(lbl.orientation, 'value') else 0, 0)
        lsz    = str(_pt_to_um(lbl.font.size)) if lbl.font else '1270'
        ET.SubElement(sym_el, 'text',
                      x=lx, y=ly, size=lsz, rot=str(lrot),
                      align=lalign, layer='SYMBOLS').text = lbl.text

    # >NAME from designator
    desgns = list(filter(_keep, sym.designators))
    if desgns:
        d     = desgns[0]
        nx    = _um(d.location.x_mils)
        ny    = _um(d.location.y_mils)
        align = _JUSTIFICATION.get(d.justification.value, 'bottom-left')
        rot   = _ORIENT_TO_ROT.get(d.orientation.value if hasattr(d.orientation, 'value') else 0, 0)
        sz    = str(_pt_to_um(d.font.size)) if d.font else '1270'
    else:
        nx, ny, align, rot, sz = '2540', '2540', 'bottom-left', 0, '1270'
    ET.SubElement(sym_el, 'text',
                  x=nx, y=ny, size=sz, rot=str(rot),
                  align=align, layer='NAMES').text = '>NAME'

    # Visible parameters → >CleanedName placeholder texts
    for par in filter(_keep, sym.parameters):
        if par.is_hidden:
            continue
        pname = clean_attr_name(par.name)
        if not pname or pname.lower() == 'designator':
            continue
        px     = _um(par.location.x_mils)
        py     = _um(par.location.y_mils)
        palign = _JUSTIFICATION.get(par.justification.value if hasattr(par.justification, 'value') else 0, 'bottom-left')
        prot   = _ORIENT_TO_ROT.get(par.orientation.value if hasattr(par.orientation, 'value') else 0, 0)
        psz    = str(_pt_to_um(par.font.size)) if par.font else '1270'
        # Altium has two parameters that both mean "device value" — Comment
        # (the always-present schematic-visible field) and the ordinary,
        # optional Value parameter some libraries place directly instead.
        # Map either onto the canonical >VALUE placeholder, same field
        # Eagle/KiCad use (see altium_exporter.py's reverse mapping).
        placeholder = 'VALUE' if pname.lower() in ('value', 'comment') else pname
        ET.SubElement(sym_el, 'text',
                      x=px, y=py, size=psz, rot=str(prot),
                      align=palign, layer='VALUES').text = f'>{placeholder}'

    return sym_el


def _symbol_geometry_hash(sym_el):
    """Hash a symbol's children (line/arc/pin/text/...), name-independent.

    Same IntLib symbol record duplicated under different names (e.g. one per
    DesignItemId/Part Number) serializes its geometry in the same child order,
    since it comes from the same parser code path — so plain document-order
    serialization is enough to detect a duplicate without sorting.
    """
    parts = []
    for child in sym_el:
        attrs = ' '.join(f'{k}={v}' for k, v in child.attrib.items())
        parts.append(f'<{child.tag} {attrs}>{child.text or ""}')
    return hashlib.sha256('\n'.join(parts).encode('utf-8')).hexdigest()


# PcbTextJustification value → IR align
_PCB_JUST = {
    0: 'bottom-left',   # MANUAL
    1: 'top-left',      # LEFT_TOP
    2: 'center-left',   # LEFT_CENTER
    3: 'bottom-left',   # LEFT_BOTTOM
    4: 'top-center',    # CENTER_TOP
    5: 'center',        # CENTER_CENTER
    6: 'bottom-center', # CENTER_BOTTOM
    7: 'top-right',     # RIGHT_TOP
    8: 'center-right',  # RIGHT_CENTER
    9: 'bottom-right',  # RIGHT_BOTTOM
}

_INTERNAL_PER_MIL = 10000.0   # altium-monkey internal units per mil (for pad height)
_INTERNAL_TO_UM   = 25.4 / 10000.0   # internal units → µm (0.00254 µm/unit)


def _convert_footprint(fp, fp_el):
    """Append footprint geometry to fp_el from altium-monkey AltiumPcbFootprint.

    Geometry goes in as DIRECT children of <footprint> with a layer="N"
    attribute (unified board/footprint layer model, ir_schema.md "Плата
    (Board IR)"); <smd>/<pad> carry no layer (copper by construction,
    far-side smd gets an explicit layer="-1").
    """
    def _layer_n(lyr_id):
        return _LAYER_MAP.get(int(lyr_id))

    # Non-electrical pads (fiducials, mounting holes) carry an empty Altium
    # designator. Eagle requires a non-empty smd/pad name, so number them,
    # skipping any value already used by a real designator.
    used_names = {str(p.designator).strip() for p in fp.pads if str(p.designator).strip()}
    anon_n = 0

    for pad in fp.pads:
        ln = _layer_n(pad.layer)
        if ln not in ('1', '-1'):
            continue
        x    = _um(pad.x_mils)
        y    = _um(pad.y_mils)
        w    = _um(pad.width_mils)
        h    = str(round(float(pad.height) * _INTERNAL_TO_UM))
        name = str(pad.designator).strip()
        if not name:
            anon_n += 1
            while str(anon_n) in used_names:
                anon_n += 1
            name = str(anon_n)
            used_names.add(name)
        shape_id = int(pad.effective_top_shape)

        if not pad.is_smt:
            el = ET.SubElement(fp_el, 'pad')
            el.set('name', name)
            el.set('x', x); el.set('y', y)
            el.set('drill', _um(pad.hole_size_mils))
            diam_mils = min(float(pad.width_mils), float(pad.height) / _INTERNAL_PER_MIL)
            el.set('diameter', _um(diam_mils))
            el.set('shape', 'square' if shape_id == 2 else 'round')
        else:
            if shape_id == 1:    # CIRCLE
                roundness = '100'
            elif shape_id == 4:  # ROUNDED_RECTANGLE
                crp = pad.corner_radius_percentage
                roundness = str(int(crp)) if crp else '50'
            elif shape_id == 3:  # OCTAGONAL
                roundness = '50'
            else:                # RECTANGLE, CUSTOM, unknown
                roundness = '0'
            el = ET.SubElement(fp_el, 'smd')
            if ln == '-1':
                el.set('layer', '-1')
            el.set('name', name)
            el.set('x', x); el.set('y', y)
            el.set('width', w); el.set('height', h)
            el.set('roundness', roundness)
            rot = float(pad.rotation or 0)
            if rot:
                el.set('rot', _f(rot % 360))

    for track in fp.tracks:
        ln = _layer_n(track.layer)
        if ln is None:
            continue
        el = ET.SubElement(fp_el, 'line')
        el.set('x1', _um(track.start_x_mils)); el.set('y1', _um(track.start_y_mils))
        el.set('x2', _um(track.end_x_mils));   el.set('y2', _um(track.end_y_mils))
        el.set('width', _um(track.width_mils))
        el.set('layer', ln)

    for arc in fp.arcs:
        ln = _layer_n(arc.layer)
        if ln is None:
            continue
        start = float(arc.start_angle)
        end   = float(arc.end_angle)
        sweep = (end - start) % 360 or 360
        if sweep >= 360:
            el = ET.SubElement(fp_el, 'shape')
            el.set('layer', ln)
            el.set('x', _um(arc.center_x_mils)); el.set('y', _um(arc.center_y_mils))
            el.set('w', _um(float(arc.radius_mils) * 2))
            el.set('h', _um(float(arc.radius_mils) * 2))
            el.set('roundness', '100'); el.set('rot', '0')
            el.set('outline', _um(arc.width_mils))
        else:
            x1, y1, x2, y2, curve = arc_endpoints(
                float(arc.center_x_mils), float(arc.center_y_mils),
                float(arc.radius_mils), start, sweep)
            el = ET.SubElement(fp_el, 'arc')
            el.set('layer', ln)
            el.set('x1', _um(x1)); el.set('y1', _um(y1))
            el.set('x2', _um(x2)); el.set('y2', _um(y2))
            el.set('curve', _f(curve))
            el.set('width', _um(arc.width_mils))

    for txt in fp.texts:
        ln = _layer_n(txt.layer)
        if ln is None:
            continue
        content = txt.text_content or ''
        if content == '.Designator':
            content = '>NAME'
        elif content == '.Comment':
            content = '>VALUE'
        j     = txt.effective_justification
        jval  = j.value if hasattr(j, 'value') else int(j)
        align = _PCB_JUST.get(jval, 'bottom-left')
        el = ET.SubElement(fp_el, 'text')
        el.set('layer', ln)
        el.set('x', _um(txt.x_mils)); el.set('y', _um(txt.y_mils))
        el.set('size', _um(txt.height_mils) if txt.height_mils else '1000')
        el.set('rot', _f(float(txt.rotation or 0) % 360))
        el.set('align', align)
        el.text = content



def _do_model_extraction(orig_to_compel, intlib, pcblib_cache, models_dir):
    """Extract embedded STEP models, copy to models_dir/{fp_name}.step, add <model3d> to IR."""
    models_dir = Path(models_dir)
    models_dir.mkdir(parents=True, exist_ok=True)

    # Extract all models from every parsed PCBLib into a temp dir
    pcblib_model_map = {}   # mvp → {original_name_lower: temp Path}
    with tempfile.TemporaryDirectory() as tmp_extract:
        for mvp, pcblib in pcblib_cache['parsed'].items():
            sub = Path(tmp_extract) / re.sub(r'[^\w]', '_', mvp)
            try:
                entries   = pcblib.get_embedded_model_entries()
                extracted = pcblib.extract_embedded_models(sub)
            except Exception:
                continue
            # Map by GUID so duplicate model names (step_temp.STEP etc.) resolve correctly
            id_map = {}
            for (model, _), path in zip(entries, extracted):
                id_map[model.id] = path
            pcblib_model_map[mvp] = id_map

        for comp in intlib.components:
            comp_el = orig_to_compel.get(comp.name)
            if comp_el is None:
                continue

            for model in comp.models:
                if model.model_type != 'PCBLIB':
                    continue
                mvp = model.virtual_path.lstrip(':\\').replace('\\', '/')
                pcblib = pcblib_cache['parsed'].get(mvp)
                if pcblib is None:
                    continue

                fp = pcblib.find_footprint(model.name)
                if fp is None:
                    continue

                fp_el = comp_el.find(f'footprint[@name="{model.name}"]')
                if fp_el is None:
                    continue

                # A merged generic component (e.g. "R") can hold one footprint
                # shared by several original IntLib rows (R0603/R0603_1) — only
                # extract/attach the model once.
                if fp_el.find('model3d') is not None:
                    continue

                body = next((b for b in fp.component_bodies if b.model_is_embedded), None)
                if body is None:
                    continue

                src_path = pcblib_model_map.get(mvp, {}).get(body.model_id)
                if src_path is None or not src_path.exists():
                    continue

                step_file = f'{sanitize_filename(model.name)}.step'
                shutil.copy2(str(src_path), str(models_dir / step_file))

                m3 = ET.Element('model3d')
                m3.set('tx', str(round(body.model_2d_x  * _INTERNAL_TO_UM)))
                m3.set('ty', str(round(body.model_2d_y  * _INTERNAL_TO_UM)))
                m3.set('tz', str(round(body.model_3d_dz * _INTERNAL_TO_UM)))
                m3.set('rx', _f(body.model_3d_rotx % 360))
                m3.set('ry', _f(body.model_3d_roty % 360))
                m3.set('rz', _f(body.model_3d_rotz % 360))
                fp_el.insert(0, m3)


_PARAM_REF = re.compile(r'^=([A-Za-z_][A-Za-z0-9_ ]*)$')


def _designator_prefix(sym):
    """Strip the placeholder '?' from the symbol's designator pattern (R? -> R)."""
    desgns = list(sym.designators)
    if not desgns or not desgns[0].text:
        return ''
    return desgns[0].text.rstrip('?') or desgns[0].text


def _is_param_alias(par, all_params):
    """True if par's text is a '=OtherParamName' reference to a real sibling
    parameter — Altium's way of mirroring one field onto another (e.g. a
    Comment field that just displays Value). The referenced parameter already
    carries the data under its own name, so the alias would only duplicate it.
    """
    m = _PARAM_REF.match((par.text or '').strip())
    if not m:
        return False
    ref_name = m.group(1).strip()
    return any(p.name == ref_name for p in all_params)


def _register_symbol(pool, symbols_el, geom_pool, sym, name, part_id=None):
    """Build (if needed) and register one symbol — or one part of a
    multi-part symbol — in the library pool. Dedups by exact name first
    (cheap, no rebuild needed); then by geometry hash, since IntLib gives
    one symbol record per DesignItemId even when the Library Ref (and thus
    the geometry) is shared — e.g. R0603/R0603_1 differing only in Value.
    Returns the registered <symbol> Element (the canonical one on a hash hit).
    """
    if name not in pool:
        sym_el = _convert_symbol(sym, name, part_id=part_id)
        ghash = _symbol_geometry_hash(sym_el)
        if ghash in geom_pool:
            pool[name] = pool[geom_pool[ghash]]
        else:
            geom_pool[ghash] = name
            pool[name] = sym_el
            symbols_el.append(sym_el)
    return pool[name]


def _convert_component(comp, schlib_cache, pcblib_cache, pool, symbols_el, geom_pool):
    """Convert one IntLibComponent → component info dict, or None on error.

    Returns {'orig_name', 'prefix', 'is_generic', 'is_multi_gate', 'symbol_name',
    'symbol_el', 'gates', 'description', 'footprints': [(name, <footprint> Element), ...],
    'attrs': [(name, value), ...]}. ('symbol_name'/'symbol_el' are None for a
    multi-gate component; 'gates' is None for a single-symbol one.)
    Building plain Elements here (instead of attaching them to the tree) lets
    the caller merge generic value-parametrized parts (R/C/...) that IntLib
    splits into one row per catalog value, before deciding the final tree shape.
    """
    vp = comp.virtual_path.lstrip(':\\').replace('\\', '/')
    sch_file = schlib_cache['paths'].get(vp)
    if sch_file is None:
        return None

    if vp not in schlib_cache['parsed']:
        try:
            schlib_cache['parsed'][vp] = AltiumSchLib(str(sch_file))
        except Exception:
            return None
    schlib = schlib_cache['parsed'][vp]

    sym = next((s for s in schlib.symbols if s.name == comp.name), None)
    if sym is None:
        return None

    # A multi-part Altium symbol (Part Count > 1, e.g. a 2-diode LED package)
    # draws every part's geometry into the SAME symbol record, distinguished
    # only by owner_part_id — converting it as one IR symbol would overlay
    # all parts' geometry on top of each other. Split it into one IR symbol
    # per part instead, wired up as an IR multi-gate component.
    is_multi_gate = sym.part_count > 1
    if is_multi_gate:
        gates = []
        for part_idx in range(1, sym.part_count + 1):
            gate_name = _part_letter(part_idx)
            part_sym_el = _register_symbol(pool, symbols_el, geom_pool, sym,
                                            f'{sym.name}_{gate_name}', part_id=part_idx)
            gates.append((gate_name, part_sym_el.get('name')))
        canonical_sym_name, sym_el_for_info = None, None
    else:
        sym_el = _register_symbol(pool, symbols_el, geom_pool, sym, sym.name)
        canonical_sym_name, sym_el_for_info, gates = sym_el.get('name'), sym_el, None

    all_params = list(sym.parameters)
    # Generic value-parametrized merging (R/C/...) doesn't make sense for a
    # multi-gate part (an LED pair isn't "the same device at a different
    # catalog value"), so it's never treated as a merge candidate.
    is_generic = (not is_multi_gate) and any(p.name == 'Value' for p in all_params)

    footprints = []
    for model in comp.models:
        if model.model_type != 'PCBLIB':
            continue
        mvp = model.virtual_path.lstrip(':\\').replace('\\', '/')
        pcb_file = pcblib_cache['paths'].get(mvp)
        if pcb_file is None:
            continue

        if mvp not in pcblib_cache['parsed']:
            try:
                pcblib_cache['parsed'][mvp] = AltiumPcbLib.from_file(str(pcb_file))
            except Exception:
                continue
        pcblib = pcblib_cache['parsed'][mvp]

        fp = pcblib.find_footprint(model.name)
        if fp is None:
            continue

        fp_el = ET.Element('footprint', name=model.name)
        _convert_footprint(fp, fp_el)

        # Pin → pad mapping by matching designators
        pad_des = {str(p.designator) for p in fp.pads}
        pm_el = ET.SubElement(fp_el, 'pin-mapping')
        if is_multi_gate:
            for part_idx in range(1, sym.part_count + 1):
                gate_name = _part_letter(part_idx)
                for ir_name, pin in _iter_named_pins(sym, part_idx):
                    des = str(pin.designator)
                    if des in pad_des:
                        ET.SubElement(pm_el, 'map', pin=f'{gate_name}.{ir_name}', pad=des)
        else:
            for ir_name, pin in _iter_named_pins(sym):
                des = str(pin.designator)
                if des in pad_des:
                    ET.SubElement(pm_el, 'map', pin=ir_name, pad=des)

        footprints.append((model.name, fp_el))

    attrs = []
    for par in all_params:
        if par.name == 'Designator':
            continue
        # Value/Comment are per-instance catalog data for generic parts
        # (R/C/...) — they live on the schematic placement, not on the device
        # record (Comment is Altium's schematic-visible "value" field, see
        # the >VALUE mapping above).
        if is_generic and par.name in ('Value', 'Comment'):
            continue
        if _is_param_alias(par, all_params):
            continue
        pname = clean_attr_name(par.name)
        if not pname:
            continue
        # IR attribute names are canonically lowercase (see
        # eagle_parser.convert_deviceset); Value/Comment both map onto the
        # same canonical 'value' key (see the >VALUE mapping above).
        key = 'value' if pname.lower() in ('value', 'comment') else pname.lower()
        attrs.append((key, par.text or ''))

    return {
        'orig_name': comp.name,
        'prefix': _designator_prefix(sym),
        'is_generic': is_generic,
        'is_multi_gate': is_multi_gate,
        'symbol_name': canonical_sym_name,
        'symbol_el': sym_el_for_info,
        'gates': gates,
        'description': comp.description,
        'footprints': footprints,
        'attrs': attrs,
    }


def convert(intlib_path: str, output_path: str):
    """Convert .IntLib to IR XML. Returns output_path."""
    lib_path = Path(intlib_path)
    intlib   = AltiumIntLib(str(lib_path))

    tmpdir  = tempfile.mkdtemp()
    sources = intlib.extract_sources(tmpdir)

    schlib_cache = {'paths': {}, 'parsed': {}}
    pcblib_cache = {'paths': {}, 'parsed': {}}
    for src in sources.sources:
        if src.kind == 'SchLib':
            schlib_cache['paths'][src.stream_path] = src.output_path
        elif src.kind == 'PCBLib':
            pcblib_cache['paths'][src.stream_path] = src.output_path

    lib_el     = ET.Element('library', name=lib_path.stem, source='altium')
    symbols_el = ET.SubElement(lib_el, 'symbols')
    pool: dict = {}
    geom_pool: dict = {}   # geometry_hash -> canonical symbol name

    # Generic value-parametrized parts (R/C/...) get one IR <component> per
    # (prefix, symbol) — IntLib otherwise gives one row per catalog Value,
    # which is schematic-instance data, not a distinct device.
    # Keyed by (prefix, id(symbol_el)) rather than the symbol's name string:
    # the symbol gets renamed to the prefix below, and a name-based key would
    # go stale between the first group member (pre-rename) and the next one
    # (post-rename) sharing the same aliased symbol Element.
    generic_groups: dict = {}      # (prefix, id(symbol_el)) -> (comp_el, {fp_name: fp_el})
    orig_to_compel: dict = {}      # original IntLib component name -> its <component> Element

    for comp in intlib.components:
        info = _convert_component(comp, schlib_cache, pcblib_cache,
                                   pool, symbols_el, geom_pool)
        if info is None:
            continue

        if info['is_multi_gate']:
            comp_el = ET.Element('component', name=info['orig_name'], prefix=info['prefix'])
            # Altium has no on-sheet gate position to carry over (parts share
            # one symbol record, distinguished only by owner_part_id) — stack
            # the gates vertically, 0.5" apart, so identical-looking gates
            # (e.g. two diodes in one LED package) don't render on top of
            # each other at (0,0).
            for i, (gate_name, gate_sym_name) in enumerate(info['gates']):
                ET.SubElement(comp_el, 'gate', name=gate_name, symbol=gate_sym_name,
                              x='0', y=str(i * 12700))
            for fp_name, fp_el in info['footprints']:
                comp_el.append(fp_el)
            attrs_el = ET.SubElement(comp_el, 'attributes')
            # `description` is a plain attribute in IR (same as KiCad/Eagle),
            # not a dedicated element/component-attribute — kept first for
            # readability, no semantic significance to the order.
            if info['description']:
                ET.SubElement(attrs_el, 'attr', name='description', value=info['description'])
            for aname, avalue in info['attrs']:
                ET.SubElement(attrs_el, 'attr', name=aname, value=avalue)
            lib_el.append(comp_el)
            orig_to_compel[info['orig_name']] = comp_el
        elif info['is_generic']:
            sym_el = info['symbol_el']
            prefix = info['prefix']
            key = (prefix, id(sym_el))
            if key not in generic_groups:
                # Rename the pooled symbol from its catalog-row name (R0603)
                # to the generic device name (R) — unless that name is
                # already taken by some unrelated symbol.
                clash = symbols_el.find(f'symbol[@name="{prefix}"]')
                if clash is None or clash is sym_el:
                    sym_el.set('name', prefix)
                comp_el = ET.Element('component', name=prefix,
                                      prefix=prefix, symbol=sym_el.get('name'))
                attrs_el = ET.SubElement(comp_el, 'attributes')
                if info['description']:
                    ET.SubElement(attrs_el, 'attr', name='description', value=info['description'])
                for aname, avalue in info['attrs']:
                    ET.SubElement(attrs_el, 'attr', name=aname, value=avalue)
                lib_el.append(comp_el)
                generic_groups[key] = (comp_el, {})
            comp_el, fp_map = generic_groups[key]
            for fp_name, fp_el in info['footprints']:
                if fp_name not in fp_map:
                    comp_el.append(fp_el)
                    fp_map[fp_name] = fp_el
            orig_to_compel[info['orig_name']] = comp_el
        else:
            comp_el = ET.Element('component', name=info['orig_name'],
                                  prefix=info['prefix'], symbol=info['symbol_name'])
            for fp_name, fp_el in info['footprints']:
                comp_el.append(fp_el)
            attrs_el = ET.SubElement(comp_el, 'attributes')
            if info['description']:
                ET.SubElement(attrs_el, 'attr', name='description', value=info['description'])
            for aname, avalue in info['attrs']:
                ET.SubElement(attrs_el, 'attr', name=aname, value=avalue)
            lib_el.append(comp_el)
            orig_to_compel[info['orig_name']] = comp_el

    models_dir = Path(output_path).parent / lib_path.stem
    _do_model_extraction(orig_to_compel, intlib, pcblib_cache, models_dir)

    raw   = minidom.parseString(ET.tostring(lib_el, encoding='unicode')).toprettyxml(indent='  ')
    clean = '\n'.join(l for l in raw.splitlines() if l.strip())
    xml   = '<?xml version="1.0" encoding="utf-8"?>\n' + '\n'.join(clean.splitlines()[1:])

    Path(output_path).write_text(xml, encoding='utf-8')
    n_comp = len(lib_el.findall('component'))
    print(f'Written: {output_path}  ({n_comp} components)')
    return output_path


if __name__ == '__main__':
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else \
        r'testData/GessorLib/Project Outputs for gessor_lib/gessor_lib.IntLib'
    dst = sys.argv[2] if len(sys.argv) > 2 else 'testData/gessor.swlib'
    convert(src, dst)
