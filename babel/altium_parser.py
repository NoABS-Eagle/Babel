"""Altium .IntLib -> IR XML converter."""
import math
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
from xml.dom import minidom
from pathlib import Path

from altium_monkey import AltiumIntLib, AltiumSchLib, AltiumPcbLib

_MILS_TO_UM = 25.4   # 1 mil = 25.4 µm


def _clean_param_name(s):
    """letter-space-letter → underscore; space adjacent to punctuation → remove."""
    s = re.sub(r'([A-Za-z0-9]) ([A-Za-z0-9])', r'\1_\2', s or '')
    return s.replace(' ', '')

_ELECTRICAL = {
    'Input':          'in',
    'Output':         'out',
    'Bidirectional':  'io',
    'Power':          'pwr',
    'Passive':        'pas',
    'Open Collector': 'out',
    'Open Emitter':   'out',
    'HiZ':            'out',
    'Not Connected':  'pas',
}

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

# Altium PcbLayer ID → IR layer name
# (same numbering used by altium-monkey's PcbLayer enum)
# 1=Top, 32=Bottom, 33=TopOverlay, 34=BottomOverlay, 35=TopPaste, 36=BottomPaste,
# 57-72=Mechanical1-16, 74=Multi-Layer
_LAYER_MAP = {
    1:  'top',
    32: 'bottom',
    33: 'silk_top',
    34: 'silk_bottom',
    35: 'cream_top',
    36: 'cream_bottom',
    74: 'top',           # Multi-Layer (TH pads through all layers)
}
for _i in range(57, 73):
    _LAYER_MAP[_i] = 'fab' if (_i % 2 == 1) else 'courtyard'
_LAYER_MAP[67] = 'courtyard'   # Mech 11 — IPC courtyard convention
_LAYER_MAP[69] = 'courtyard'   # Mech 13
_LAYER_MAP[71] = 'courtyard'   # Mech 15


def _um(mils):
    """mils → integer µm string."""
    return str(round(float(mils) * _MILS_TO_UM))


def _f(v):
    """Format angle/ratio as compact float (degrees, percentages — not lengths)."""
    r = round(float(v), 4)
    return str(int(r)) if r == int(r) else str(r)


def _convert_symbol(sym, sym_name):
    """Build a pool-ready <symbol name=...> from an altium-monkey symbol."""
    sym_el = ET.Element('symbol', name=sym_name)

    _RECT_W = '254'   # 0.254mm = 254µm — Eagle body wire aesthetic
    _LINE_W = '152'   # 0.1524mm = 152µm (≈6mil)
    for rect in sym.rectangles:
        loc = rect.location_mils
        cor = rect.corner_mils
        x1  = _um(loc.x_mils); y1 = _um(loc.y_mils)
        x2  = _um(cor.x_mils); y2 = _um(cor.y_mils)
        for ax1, ay1, ax2, ay2 in [(x1,y1,x2,y1),(x2,y1,x2,y2),(x2,y2,x1,y2),(x1,y2,x1,y1)]:
            ET.SubElement(sym_el, 'line',
                          x1=ax1, y1=ay1, x2=ax2, y2=ay2, width=_RECT_W)

    for pl in sym.polylines:
        pts = list(pl.points_mils)
        for a, b in zip(pts, pts[1:]):
            ET.SubElement(sym_el, 'line',
                          x1=_um(a.x_mils), y1=_um(a.y_mils),
                          x2=_um(b.x_mils), y2=_um(b.y_mils),
                          width=_LINE_W)

    for ln in sym.lines:
        loc = ln.location_mils
        cor = ln.corner_mils
        ET.SubElement(sym_el, 'line',
                      x1=_um(loc.x_mils), y1=_um(loc.y_mils),
                      x2=_um(cor.x_mils), y2=_um(cor.y_mils),
                      width=_LINE_W)

    for arc in sym.arcs:
        loc = arc.location_mils
        ET.SubElement(sym_el, 'arc',
                      cx=_um(loc.x_mils), cy=_um(loc.y_mils),
                      r=_um(arc.radius),
                      start=_f(arc.start_angle),
                      sweep=_f((arc.end_angle - arc.start_angle) % 360),
                      width=_LINE_W)

    for pol in sym.polygons:
        pts = list(pol.points_mils)
        for a, b in zip(pts, pts[1:] + [pts[0]]):
            ET.SubElement(sym_el, 'line',
                          x1=_um(a.x_mils), y1=_um(a.y_mils),
                          x2=_um(b.x_mils), y2=_um(b.y_mils),
                          width=_RECT_W)

    seen_pin_names: dict[str, int] = {}
    for pin in sym.pins:
        direction = _ELECTRICAL.get(pin.electrical_name, 'pas')
        orient    = pin.orientation  # 0-3
        rot       = _PIN_ORIENT_TO_ROT.get(orient, 180)
        # Altium x,y = body end; shift to hot end (wire connection point)
        orient_deg = orient * 90
        length_um  = float(pin.length_mils) * _MILS_TO_UM
        px_um = float(pin.x_mils) * _MILS_TO_UM + length_um * math.cos(math.radians(orient_deg))
        py_um = float(pin.y_mils) * _MILS_TO_UM + length_um * math.sin(math.radians(orient_deg))
        visible = 'both'
        if not pin.show_name and not pin.show_designator:
            visible = 'off'
        elif not pin.show_name:
            visible = 'pad'
        elif not pin.show_designator:
            visible = 'pin'
        pname = _clean_param_name(pin.name or str(pin.designator))
        seen_pin_names[pname] = seen_pin_names.get(pname, 0) + 1
        n = seen_pin_names[pname]
        ir_pin_name = pname if n == 1 else f'{pname}@{n}'
        ET.SubElement(sym_el, 'pin',
                      name=ir_pin_name,
                      x=str(round(px_um)),
                      y=str(round(py_um)),
                      rot=str(rot),
                      length=str(round(length_um)),
                      direction=direction,
                      visible=visible)

    for lbl in sym.labels:
        if lbl.is_hidden or not lbl.text:
            continue
        lx    = _um(lbl.location.x_mils)
        ly    = _um(lbl.location.y_mils)
        lalign = _JUSTIFICATION.get(lbl.justification.value if hasattr(lbl.justification, 'value') else 0, 'bottom-left')
        lrot   = _ORIENT_TO_ROT.get(lbl.orientation.value if hasattr(lbl.orientation, 'value') else 0, 0)
        ET.SubElement(sym_el, 'text',
                      x=lx, y=ly, size='1270', rot=str(lrot),
                      align=lalign, layer='symbol').text = lbl.text

    # >NAME from designator
    desgns = list(sym.designators)
    if desgns:
        d     = desgns[0]
        nx    = _um(d.location.x_mils)
        ny    = _um(d.location.y_mils)
        align = _JUSTIFICATION.get(d.justification.value, 'bottom-left')
        rot   = _ORIENT_TO_ROT.get(d.orientation.value if hasattr(d.orientation, 'value') else 0, 0)
        sz    = str(round(float(d.font.size) * 10 * _MILS_TO_UM)) if d.font else '1270'
    else:
        nx, ny, align, rot, sz = '2540', '2540', 'bottom-left', 0, '1270'
    ET.SubElement(sym_el, 'text',
                  x=nx, y=ny, size=sz, rot=str(rot),
                  align=align, layer='symbol_names').text = '>NAME'

    # Visible parameters → >CleanedName placeholder texts
    for par in sym.parameters:
        if par.is_hidden:
            continue
        pname = _clean_param_name(par.name)
        if not pname or pname.lower() == 'designator':
            continue
        px     = _um(par.location.x_mils)
        py     = _um(par.location.y_mils)
        palign = _JUSTIFICATION.get(par.justification.value if hasattr(par.justification, 'value') else 0, 'bottom-left')
        prot   = _ORIENT_TO_ROT.get(par.orientation.value if hasattr(par.orientation, 'value') else 0, 0)
        psz    = str(round(float(par.font.size) * 10 * _MILS_TO_UM)) if par.font else '1270'
        ET.SubElement(sym_el, 'text',
                      x=px, y=py, size=psz, rot=str(prot),
                      align=palign, layer='symbol_values').text = f'>{pname}'

    return sym_el


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
    """Append footprint geometry to fp_el from altium-monkey AltiumPcbFootprint."""
    layers = {}

    def _bucket(lyr_id):
        name = _LAYER_MAP.get(int(lyr_id))
        if name is None:
            return None
        if name not in layers:
            layers[name] = ET.Element(name)
        return layers[name]

    for pad in fp.pads:
        bkt = _bucket(pad.layer)
        if bkt is None:
            continue
        x    = _um(pad.x_mils)
        y    = _um(pad.y_mils)
        w    = _um(pad.width_mils)
        h    = str(round(float(pad.height) * _INTERNAL_TO_UM))
        name = str(pad.designator)
        shape_id = int(pad.effective_top_shape)

        if not pad.is_smt:
            el = ET.SubElement(bkt, 'pad')
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
            el = ET.SubElement(bkt, 'smd')
            el.set('name', name)
            el.set('x', x); el.set('y', y)
            el.set('width', w); el.set('height', h)
            el.set('roundness', roundness)
            rot = float(pad.rotation or 0)
            if rot:
                el.set('rot', _f(rot % 360))

    for track in fp.tracks:
        bkt = _bucket(track.layer)
        if bkt is None:
            continue
        el = ET.SubElement(bkt, 'line')
        el.set('x1', _um(track.start_x_mils)); el.set('y1', _um(track.start_y_mils))
        el.set('x2', _um(track.end_x_mils));   el.set('y2', _um(track.end_y_mils))
        el.set('width', _um(track.width_mils))

    for arc in fp.arcs:
        bkt = _bucket(arc.layer)
        if bkt is None:
            continue
        start = float(arc.start_angle)
        end   = float(arc.end_angle)
        sweep = (end - start) % 360 or 360
        el = ET.SubElement(bkt, 'arc')
        el.set('cx', _um(arc.center_x_mils)); el.set('cy', _um(arc.center_y_mils))
        el.set('r',  _um(arc.radius_mils))
        el.set('start', _f(start)); el.set('sweep', _f(sweep))
        el.set('width', _um(arc.width_mils))

    for txt in fp.texts:
        bkt = _bucket(txt.layer)
        if bkt is None:
            continue
        content = txt.text_content or ''
        if content == '.Designator':
            content = '>NAME'
        elif content == '.Comment':
            content = '>VALUE'
        j     = txt.effective_justification
        jval  = j.value if hasattr(j, 'value') else int(j)
        align = _PCB_JUST.get(jval, 'bottom-left')
        el = ET.SubElement(bkt, 'text')
        el.set('x', _um(txt.x_mils)); el.set('y', _um(txt.y_mils))
        el.set('size', _um(txt.height_mils) if txt.height_mils else '1000')
        el.set('rot', _f(float(txt.rotation or 0) % 360))
        el.set('align', align)
        el.text = content

    for bkt in layers.values():
        if len(bkt):
            fp_el.append(bkt)


def _do_model_extraction(lib_el, intlib, pcblib_cache, models_dir):
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
            comp_el = lib_el.find(f'component[@name="{comp.name}"]')
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

                body = next((b for b in fp.component_bodies if b.model_is_embedded), None)
                if body is None:
                    continue

                src_path = pcblib_model_map.get(mvp, {}).get(body.model_id)
                if src_path is None or not src_path.exists():
                    continue

                # Sanitize footprint name for filesystem
                safe_fp = re.sub(r'[<>:"/\\|?*]', '_', model.name)
                step_file = f'{safe_fp}.step'
                shutil.copy2(str(src_path), str(models_dir / step_file))

                m3 = ET.Element('model3d')
                m3.set('tx', str(round(body.model_2d_x  * _INTERNAL_TO_UM)))
                m3.set('ty', str(round(body.model_2d_y  * _INTERNAL_TO_UM)))
                m3.set('tz', str(round(body.model_3d_dz * _INTERNAL_TO_UM)))
                m3.set('rx', _f(body.model_3d_rotx % 360))
                m3.set('ry', _f(body.model_3d_roty % 360))
                m3.set('rz', _f(body.model_3d_rotz % 360))
                fp_el.insert(0, m3)


def _convert_component(comp, schlib_cache, pcblib_cache, pool, symbols_el):
    """Convert one IntLibComponent → <component> XML element, or None on error."""
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

    comp_el = ET.Element('component', name=comp.name)
    if comp.description:
        comp_el.set('description', comp.description)

    # Register the symbol in the library pool (dedup by symbol name) and
    # reference it from the component (single-mode).
    sym_name = sym.name
    if sym_name not in pool:
        pool[sym_name] = _convert_symbol(sym, sym_name)
        symbols_el.append(pool[sym_name])
    comp_el.set('symbol', sym_name)

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

        fp_el = ET.SubElement(comp_el, 'footprint', name=model.name)
        _convert_footprint(fp, fp_el)

        # Pin → pad mapping by matching designators
        pad_des = {str(p.designator) for p in fp.pads}
        pm_el = ET.SubElement(fp_el, 'pin-mapping')
        seen_names: dict[str, int] = {}
        for pin in sym.pins:
            pname = _clean_param_name(pin.name or str(pin.designator))
            seen_names[pname] = seen_names.get(pname, 0) + 1
            n = seen_names[pname]
            ir_name = pname if n == 1 else f'{pname}@{n}'
            des = str(pin.designator)
            if des in pad_des:
                ET.SubElement(pm_el, 'map', pin=ir_name, pad=des)

    attrs_el = ET.SubElement(comp_el, 'attributes')
    for par in sym.parameters:
        if par.name == 'Designator':
            continue
        pname = _clean_param_name(par.name)
        if pname:
            ET.SubElement(attrs_el, 'attr', name=pname, value=par.text or '')

    return comp_el


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

    for comp in intlib.components:
        comp_el = _convert_component(comp, schlib_cache, pcblib_cache,
                                     pool, symbols_el)
        if comp_el is not None:
            lib_el.append(comp_el)

    models_dir = Path(output_path).parent / lib_path.stem
    _do_model_extraction(lib_el, intlib, pcblib_cache, models_dir)

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
