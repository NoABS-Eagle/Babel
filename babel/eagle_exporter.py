"""IR → Eagle .lbr exporter."""
import math
import re
import xml.etree.ElementTree as ET
from xml.dom import minidom
from pathlib import Path
from babel.eagle_parser import fmt
from babel.ir_util import symbol_pool, component_gates


def _eagle_name(s):
    """Clean name for Eagle: letter-space-letter → _, space adjacent to punctuation → remove."""
    s = re.sub(r'([A-Za-z0-9]) ([A-Za-z0-9])', r'\1_\2', s or '')
    return s.replace(' ', '')

_LAYERS_FILE = Path(__file__).parent / 'data' / 'eagle_layers.xml'

_WIRE_W = '0.1524'  # default wire width (6 mil), lost during import

_IR_LAYER_TO_EAGLE = {
    'top': 1, 'bottom': 16, 'silk_top': 21, 'silk_bottom': 22,
    'labels': 25, 'fab': 51, 'courtyard': 39,
    'cream_top': 31, 'cream_bottom': 32,
}

_PIN_LEN = {0: 'point', 2.54: 'short', 5.08: 'middle', 7.62: 'long'}


def _tomm(um):
    """IR µm integer string → mm float string for Eagle output."""
    r = round(float(um) / 1000, 6)
    return f'{r:g}'

_SYM_TEXT_LAYER = {
    'symbol':        '94',
    'symbol_names':  '95',
    'symbol_values': '96',
    'symbol_info':   '97',
}


def _pin_length_str(mm):
    closest = min(_PIN_LEN, key=lambda k: abs(k - float(mm)))
    return _PIN_LEN[closest]


def _arc_to_wire(cx, cy, r, start_deg, sweep_deg):
    """IR arc → Eagle wire (x1, y1, x2, y2, curve)."""
    sr = math.radians(start_deg)
    er = sr + math.radians(sweep_deg)
    return (cx + r * math.cos(sr), cy + r * math.sin(sr),
            cx + r * math.cos(er), cy + r * math.sin(er),
            -sweep_deg)


def _rot_attr(deg):
    """Float angle → Eagle rot string, or None if 0."""
    d = round(deg)
    return f'R{d}' if d else None


def _text_layer(txt):
    t = txt.strip()
    if t == '>NAME':  return '95'
    if t == '>VALUE': return '96'
    return '94'


# ---------------------------------------------------------------------------
# Symbol
# ---------------------------------------------------------------------------

def export_symbol(sym_el, sym_name):
    sym = ET.Element('symbol')
    sym.set('name', _eagle_name(sym_name))

    desc = sym_el.find('description')
    if desc is not None and desc.text:
        d = ET.SubElement(sym, 'description')
        d.text = desc.text

    pin_name_count = {}   # track duplicates → add @N suffix

    for el in sym_el:
        t = el.tag

        if t == 'line':
            w = ET.SubElement(sym, 'wire')
            w.set('x1', _tomm(el.get('x1'))); w.set('y1', _tomm(el.get('y1')))
            w.set('x2', _tomm(el.get('x2'))); w.set('y2', _tomm(el.get('y2')))
            w.set('width', _tomm(el.get('width', '152'))); w.set('layer', '94')

        elif t == 'arc':
            x1, y1, x2, y2, curve = _arc_to_wire(
                float(el.get('cx')) / 1000, float(el.get('cy')) / 1000,
                float(el.get('r')) / 1000,
                float(el.get('start')), float(el.get('sweep')))
            w = ET.SubElement(sym, 'wire')
            w.set('x1', fmt(x1)); w.set('y1', fmt(y1))
            w.set('x2', fmt(x2)); w.set('y2', fmt(y2))
            w.set('width', _tomm(el.get('width', '152'))); w.set('layer', '94')
            w.set('curve', fmt(curve))

        elif t == 'shape':
            rn = int(el.get('roundness', 0))
            x, y = float(el.get('x')) / 1000, float(el.get('y')) / 1000
            w2 = float(el.get('w', '0')) / 2000
            h2 = float(el.get('h', '0')) / 2000
            outline_mm = _tomm(el.get('outline', '0'))
            if rn == 100:
                c = ET.SubElement(sym, 'circle')
                c.set('x', fmt(x)); c.set('y', fmt(y))
                c.set('radius', fmt(w2))
                c.set('width', outline_mm); c.set('layer', '94')
            elif float(el.get('outline', '0')) == 0:
                r_el = ET.SubElement(sym, 'rectangle')
                r_el.set('x1', fmt(x - w2)); r_el.set('y1', fmt(y - h2))
                r_el.set('x2', fmt(x + w2)); r_el.set('y2', fmt(y + h2))
                r_el.set('layer', '94')
            else:
                corners = [(x-w2, y-h2), (x+w2, y-h2), (x+w2, y+h2), (x-w2, y+h2)]
                for (ax, ay), (bx, by) in zip(corners, corners[1:] + corners[:1]):
                    w_el = ET.SubElement(sym, 'wire')
                    w_el.set('x1', fmt(ax)); w_el.set('y1', fmt(ay))
                    w_el.set('x2', fmt(bx)); w_el.set('y2', fmt(by))
                    w_el.set('width', outline_mm); w_el.set('layer', '94')

        elif t == 'text':
            txt = el.text or ''
            te = ET.SubElement(sym, 'text')
            te.set('x', _tomm(el.get('x'))); te.set('y', _tomm(el.get('y')))
            te.set('size', _tomm(el.get('size')))
            rot = _rot_attr(float(el.get('rot', 0)))
            if rot: te.set('rot', rot)
            align = el.get('align', 'bottom-left')
            if align != 'bottom-left': te.set('align', align)
            # Stored IR layer takes priority; fall back to content-based guess
            ir_layer = el.get('layer', '')
            te.set('layer', _SYM_TEXT_LAYER.get(ir_layer) or _text_layer(txt))
            if el.get('font') == 'vector':
                te.set('font', 'vector')
            te.text = txt

        elif t == 'polygon':
            pg = ET.SubElement(sym, 'polygon')
            pg.set('width', _tomm(el.get('width', '0')))
            pg.set('layer', '94')
            for v in el.findall('vertex'):
                ve = ET.SubElement(pg, 'vertex')
                ve.set('x', _tomm(v.get('x', '0')))
                ve.set('y', _tomm(v.get('y', '0')))

        elif t == 'pin':
            p = ET.SubElement(sym, 'pin')
            raw = _eagle_name(el.get('name', ''))
            pin_name_count[raw] = pin_name_count.get(raw, 0) + 1
            n = pin_name_count[raw]
            p.set('name', raw if n == 1 else f'{raw}@{n}')
            p.set('x', _tomm(el.get('x'))); p.set('y', _tomm(el.get('y')))
            p.set('length', _pin_length_str(float(el.get('length', 2540)) / 1000))
            rot = _rot_attr(float(el.get('rot', 0)))
            if rot: p.set('rot', rot)
            p.set('direction', el.get('direction', 'pas'))
            visible = el.get('visible', 'off')
            if visible != 'both': p.set('visible', visible)

    return sym


# ---------------------------------------------------------------------------
# Package (footprint)
# ---------------------------------------------------------------------------


def _mcad_to_eagle_rot(rx_m, ry_m, rz_m):
    """Convert MCAD intrinsic XYZ angles to Eagle extrinsic ZYX.

    R_mcad = Rx·Ry·Rz. Eagle applies Rx(90°) frame correction:
    R_eagle = Rx(90°)·R_mcad, decomposed as Rz·Ry·Rx.
    """
    from math import radians, degrees, cos, atan2, asin

    def _rx(a): c,s=cos(a),__import__('math').sin(a); return [[1,0,0],[0,c,-s],[0,s,c]]
    def _ry(a): c,s=cos(a),__import__('math').sin(a); return [[c,0,s],[0,1,0],[-s,0,c]]
    def _rz(a): c,s=cos(a),__import__('math').sin(a); return [[c,-s,0],[s,c,0],[0,0,1]]
    def _mul(A,B): return [[sum(A[i][k]*B[k][j] for k in range(3)) for j in range(3)] for i in range(3)]

    d = radians
    R_mcad  = _mul(_mul(_rx(d(rx_m)), _ry(d(ry_m))), _rz(d(rz_m)))
    R_eagle = _mul(_rx(d(90)), R_mcad)

    ry_e = asin(max(-1.0, min(1.0, -R_eagle[2][0])))
    if abs(cos(ry_e)) > 1e-6:
        rx_e = atan2(R_eagle[2][1], R_eagle[2][2])
        rz_e = atan2(R_eagle[1][0], R_eagle[0][0])
    else:
        rx_e = atan2(-R_eagle[0][1], R_eagle[1][1])
        rz_e = 0.0

    def _clean(rad):
        v = degrees(rad)
        v = v % 360
        if v > 180: v -= 360
        if abs(v) < 1e-9: v = 0.0
        return int(v) if v == int(v) else round(v, 4)

    return _clean(rx_e), _clean(ry_e), _clean(rz_e)


def _model3d_comment(m3):
    """Build <!--3d:{...}--> comment for Eagle package description."""
    import json
    rx_e, ry_e, rz_e = _mcad_to_eagle_rot(
        float(m3.get('rx', 0)), float(m3.get('ry', 0)), float(m3.get('rz', 0)))
    data = {
        'tx': float(m3.get('tx', 0)) / 1000,
        'ty': float(m3.get('ty', 0)) / 1000,
        'tz': float(m3.get('tz', 0)) / 1000,
        'rx': rx_e, 'ry': ry_e, 'rz': rz_e,
    }
    return '<!--3d:' + json.dumps(data, separators=(',', ':')) + '-->'


def export_package(fp_el, pkg_name):
    pkg = ET.Element('package')
    pkg.set('name', _eagle_name(pkg_name))

    desc_text = ''
    desc = fp_el.find('description')
    if desc is not None and desc.text:
        desc_text = desc.text

    m3 = fp_el.find('model3d')
    if m3 is not None:
        comment = _model3d_comment(m3)
        desc_text = (desc_text + '\n' + comment).strip()

    if desc_text:
        d = ET.SubElement(pkg, 'description')
        d.text = desc_text

    for ir_layer, eagle_num in _IR_LAYER_TO_EAGLE.items():
        layer_el = fp_el.find(ir_layer)
        if layer_el is None:
            continue

        for el in layer_el:
            t = el.tag

            if t == 'smd':
                s = ET.SubElement(pkg, 'smd')
                s.set('name', el.get('name'))
                s.set('x', _tomm(el.get('x'))); s.set('y', _tomm(el.get('y')))
                s.set('dx', _tomm(el.get('width'))); s.set('dy', _tomm(el.get('height')))
                rn = int(float(el.get('roundness', 0)))
                if rn: s.set('roundness', str(rn))
                rot = float(el.get('rot', 0))
                if rot:
                    r_str = f'R{int(rot)}' if rot == int(rot) else f'R{rot}'
                    s.set('rot', r_str)
                s.set('layer', str(eagle_num))

            elif t == 'pad':
                p = ET.SubElement(pkg, 'pad')
                p.set('name', el.get('name'))
                p.set('x', _tomm(el.get('x'))); p.set('y', _tomm(el.get('y')))
                p.set('drill', _tomm(el.get('drill')))
                if el.get('diameter'):
                    p.set('diameter', _tomm(el.get('diameter')))
                shape = el.get('shape', 'round')
                if shape != 'round': p.set('shape', shape)

            elif t == 'hole':
                h = ET.SubElement(pkg, 'hole')
                h.set('x', _tomm(el.get('x'))); h.set('y', _tomm(el.get('y')))
                h.set('drill', _tomm(el.get('drill')))

            elif t == 'line':
                w = ET.SubElement(pkg, 'wire')
                w.set('x1', _tomm(el.get('x1'))); w.set('y1', _tomm(el.get('y1')))
                w.set('x2', _tomm(el.get('x2'))); w.set('y2', _tomm(el.get('y2')))
                w.set('width', _tomm(el.get('width', '152'))); w.set('layer', str(eagle_num))

            elif t == 'arc':
                x1, y1, x2, y2, curve = _arc_to_wire(
                    float(el.get('cx')) / 1000, float(el.get('cy')) / 1000,
                    float(el.get('r')) / 1000,
                    float(el.get('start')), float(el.get('sweep')))
                w = ET.SubElement(pkg, 'wire')
                w.set('x1', fmt(x1)); w.set('y1', fmt(y1))
                w.set('x2', fmt(x2)); w.set('y2', fmt(y2))
                w.set('width', _tomm(el.get('width', '152'))); w.set('layer', str(eagle_num))
                w.set('curve', fmt(curve))

            elif t == 'shape':
                rn = int(el.get('roundness', 0))
                x, y = float(el.get('x')) / 1000, float(el.get('y')) / 1000
                w2 = float(el.get('w', '0')) / 2000
                h2 = float(el.get('h', '0')) / 2000
                outline_mm = _tomm(el.get('outline', '0'))
                lyr = str(eagle_num)
                if rn == 100:
                    c = ET.SubElement(pkg, 'circle')
                    c.set('x', fmt(x)); c.set('y', fmt(y))
                    c.set('radius', fmt(w2))
                    c.set('width', outline_mm); c.set('layer', lyr)
                elif float(el.get('outline', '0')) == 0:
                    r_el = ET.SubElement(pkg, 'rectangle')
                    r_el.set('x1', fmt(x - w2)); r_el.set('y1', fmt(y - h2))
                    r_el.set('x2', fmt(x + w2)); r_el.set('y2', fmt(y + h2))
                    r_el.set('layer', lyr)
                else:
                    corners = [(x-w2, y-h2), (x+w2, y-h2), (x+w2, y+h2), (x-w2, y+h2)]
                    for (ax, ay), (bx, by) in zip(corners, corners[1:] + corners[:1]):
                        w_el = ET.SubElement(pkg, 'wire')
                        w_el.set('x1', fmt(ax)); w_el.set('y1', fmt(ay))
                        w_el.set('x2', fmt(bx)); w_el.set('y2', fmt(by))
                        w_el.set('width', outline_mm); w_el.set('layer', lyr)

            elif t == 'polygon':
                pg = ET.SubElement(pkg, 'polygon')
                pg.set('width', _tomm(el.get('width', '0')))
                pg.set('layer', str(eagle_num))
                for v in el.findall('vertex'):
                    ve = ET.SubElement(pg, 'vertex')
                    ve.set('x', _tomm(v.get('x', '0')))
                    ve.set('y', _tomm(v.get('y', '0')))

            elif t == 'text':
                te = ET.SubElement(pkg, 'text')
                te.set('x', _tomm(el.get('x'))); te.set('y', _tomm(el.get('y')))
                te.set('size', _tomm(el.get('size')))
                rot = _rot_attr(float(el.get('rot', 0)))
                if rot: te.set('rot', rot)
                align = el.get('align', 'bottom-left')
                if align != 'bottom-left': te.set('align', align)
                te.set('layer', str(eagle_num))
                if el.get('font') == 'vector':
                    te.set('font', 'vector')
                te.text = el.text or ''

    return pkg


# ---------------------------------------------------------------------------
# Deviceset
# ---------------------------------------------------------------------------

def export_deviceset(comp_el):
    ds = ET.Element('deviceset')
    cid = _eagle_name(comp_el.get('name', ''))
    ds.set('name', cid)
    if comp_el.get('prefix'):
        ds.set('prefix', comp_el.get('prefix'))
    if comp_el.get('uservalue') == 'yes':
        ds.set('uservalue', 'yes')

    desc = comp_el.find('description')
    if desc is not None and desc.text:
        d = ET.SubElement(ds, 'description')
        d.text = desc.text

    # Gates: single-mode → one gate G$1; multi-mode → one gate per IR <gate>
    gate_list = component_gates(comp_el)
    single_mode = len(gate_list) == 1 and gate_list[0][0] is None

    gates = ET.SubElement(ds, 'gates')
    if single_mode:
        gate = ET.SubElement(gates, 'gate')
        gate.set('name', 'G$1')
        gate.set('symbol', _eagle_name(gate_list[0][1]))
        gate.set('x', '0'); gate.set('y', '0')
    else:
        for gname, sname in gate_list:
            gate_ir = comp_el.find(f'gate[@name="{gname}"]')
            gate = ET.SubElement(gates, 'gate')
            gate.set('name', _eagle_name(gname))
            gate.set('symbol', _eagle_name(sname))
            if gate_ir is not None:
                gate.set('x', _tomm(gate_ir.get('x', '0')))
                gate.set('y', _tomm(gate_ir.get('y', '0')))
            else:
                gate.set('x', '0'); gate.set('y', '0')

    attrs_el = comp_el.find('attributes')
    # Skip 'value' (built-in Eagle attribute) and skip empty values
    _seen_attrs = {}
    for a in (attrs_el.findall('attr') if attrs_el is not None else []):
        if a.get('name', '').lower() == 'value' or not a.get('value', ''):
            continue
        key = _eagle_name(a.get('name', '')).upper()
        _seen_attrs[key] = a.get('value', '')   # last value wins on collision
    attr_pairs = list(_seen_attrs.items())

    devices_el = ET.SubElement(ds, 'devices')

    fp_els = comp_el.findall('footprint')
    single = len(fp_els) == 1
    seen_dev_names = {}

    for fp_el in fp_els:
        pkg_name = fp_el.get('name')
        dev = ET.SubElement(devices_el, 'device')
        if single and not fp_el.get('variant'):
            dev_name = ''           # Eagle convention: single variant → empty name
        else:
            dev_name = _eagle_name(fp_el.get('variant', pkg_name))
        # Deduplicate device names within this deviceset
        seen_dev_names[dev_name] = seen_dev_names.get(dev_name, 0) + 1
        if seen_dev_names[dev_name] > 1:
            dev_name = f'{dev_name}_{seen_dev_names[dev_name]}'
        dev.set('name', dev_name)
        dev.set('package', _eagle_name(pkg_name))

        pm = fp_el.find('pin-mapping')
        if pm is not None:
            connects = ET.SubElement(dev, 'connects')
            # Merge pads for the same pin (Eagle forbids duplicate pin in <connects>)
            pin_to_pads: dict[str, list[str]] = {}
            for m in pm.findall('map'):
                pin_to_pads.setdefault(m.get('pin'), []).append(m.get('pad'))
            for pin, pads in pin_to_pads.items():
                conn = ET.SubElement(connects, 'connect')
                if single_mode:
                    conn.set('gate', 'G$1')
                    conn.set('pin', pin)
                else:
                    gname, _, pname = pin.partition('.')
                    conn.set('gate', _eagle_name(gname))
                    conn.set('pin', pname)
                conn.set('pad', ' '.join(pads))

        techs = ET.SubElement(dev, 'technologies')
        tech = ET.SubElement(techs, 'technology')
        tech.set('name', '')
        for name, val in attr_pairs:
            a = ET.SubElement(tech, 'attribute')
            a.set('name', name); a.set('value', val)

    return ds


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------

def export(ir_path, output_path=None):
    tree = ET.parse(ir_path)
    ir_root = tree.getroot()
    lib_name = ir_root.get('name', Path(ir_path).stem)

    # Collect packages: from component footprints AND orphaned standalone footprints
    packages_map = {}
    for comp in ir_root.findall('component'):
        for fp_el in comp.findall('footprint'):
            pid = fp_el.get('name')
            if pid not in packages_map:
                packages_map[pid] = fp_el
    for fp_el in ir_root.findall('footprint'):
        pid = fp_el.get('name')
        if pid not in packages_map:
            packages_map[pid] = fp_el

    eagle = ET.Element('eagle')
    eagle.set('version', '7.7.0')
    drawing = ET.SubElement(eagle, 'drawing')

    settings = ET.SubElement(drawing, 'settings')
    ET.SubElement(settings, 'setting').set('alwaysvectorfont', 'no')
    ET.SubElement(settings, 'setting').set('verticaltext', 'up')

    grid = ET.SubElement(drawing, 'grid')
    for k, v in [('distance','0.1'),('unitdist','inch'),('unit','inch'),
                 ('style','lines'),('multiple','1'),('display','no'),
                 ('altdistance','0.01'),('altunitdist','inch'),('altunit','inch')]:
        grid.set(k, v)

    drawing.append(ET.parse(_LAYERS_FILE).getroot())

    lib_el = ET.SubElement(drawing, 'library')

    packages_el = ET.SubElement(lib_el, 'packages')
    for pkg_name, fp_el in packages_map.items():
        packages_el.append(export_package(fp_el, pkg_name))

    symbols_el = ET.SubElement(lib_el, 'symbols')
    for sym_el in symbol_pool(ir_root).values():
        symbols_el.append(export_symbol(sym_el, sym_el.get('name')))

    devicesets_el = ET.SubElement(lib_el, 'devicesets')
    for comp in ir_root.findall('component'):
        if component_gates(comp):
            devicesets_el.append(export_deviceset(comp))

    xml_str = minidom.parseString(ET.tostring(eagle, encoding='unicode')) \
                     .toprettyxml(indent='  ')
    lines = xml_str.splitlines()
    clean = '\n'.join(l for l in lines if l.strip())
    result = ('<?xml version="1.0" encoding="utf-8"?>\n'
              '<!DOCTYPE eagle SYSTEM "eagle.dtd">\n' +
              '\n'.join(clean.splitlines()[1:]))

    if output_path:
        Path(output_path).write_text(result, encoding='utf-8')
        print(f'Written: {output_path}')
    return result


if __name__ == '__main__':
    import sys
    ir  = sys.argv[1] if len(sys.argv) > 1 else 'testData/rc.swlib'
    out = sys.argv[2] if len(sys.argv) > 2 else 'testData/rc.roundtrip.lbr'
    export(ir, out)
