import math
import re
import json
import shutil
import xml.etree.ElementTree as ET
from xml.dom import minidom
from pathlib import Path

# Eagle layer number → IR layer name (None = ignore)
LAYER_MAP = {
    1:  'top',
    16: 'bottom',
    21: 'silk_top',
    22: 'silk_bottom',
    25: 'labels',
    27: 'labels',
    31: 'cream_top',
    32: 'cream_bottom',
    39: 'courtyard',
    51: 'fab',
    94: 'symbol',
    95: 'symbol_names',
    96: 'symbol_values',
    97: 'symbol_info',
}

_SYM_GEOMETRY_LAYERS = {'symbol'}
_SYM_TEXT_LAYERS     = {'symbol', 'symbol_names', 'symbol_values', 'symbol_info'}

DIR_MAP = {
    'in':  'in',
    'out': 'out',
    'io':  'io',
    'oc':  'io',
    'pwr': 'pwr',
    'pas': 'pas',
    'hiz': 'io',
    'sup': 'pwr',
    'nc':  None,
}


def fmt(v):
    return f"{round(float(v), 6):g}"


def _um(mm):
    """mm string/float → integer µm string (rounds to nearest µm)."""
    return str(round(float(mm) * 1000))


def parse_rot(rot_str):
    """'MR90' → (90.0, True),  'R180' → (180.0, False),  None → (0.0, False)"""
    if not rot_str:
        return 0.0, False
    s = rot_str.strip()
    mirrored = 'M' in s
    angle = float(re.sub(r'[MRS]', '', s) or '0')
    return angle, mirrored


def eagle_arc(x1, y1, x2, y2, curve_deg):
    """Convert Eagle wire-with-curve to (cx, cy, r, start_deg, sweep_deg)."""
    dx, dy = x2 - x1, y2 - y1
    chord = math.hypot(dx, dy)
    if chord < 1e-10:
        return None
    a = math.radians(abs(curve_deg))
    r = chord / (2 * math.sin(a / 2))
    d = r * math.cos(a / 2)
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    sign = 1 if curve_deg > 0 else -1   # positive curve → center to the left of chord
    cx = mx + sign * d * (-dy / chord)
    cy = my + sign * d * (dx / chord)
    start = math.degrees(math.atan2(y1 - cy, x1 - cx))
    return cx, cy, r, start, curve_deg


def _eagle_to_mcad_rot(rx_e, ry_e, rz_e):
    """Convert Eagle intrinsic XYZ angles to MCAD intrinsic XYZ.

    Eagle stores R_eagle = Rx·Ry·Rz (same intrinsic order as MCAD/Altium),
    with an implicit Rx(90°) frame correction applied on the right:
    R_mcad = R_eagle·Rx(-90°), decomposed as Rx(rx)·Ry(ry)·Rz(rz).
    Reverse-engineered from ground-truth (rx_e,ry_e,rz_e) -> (rx_m,ry_m,rz_m)
    pairs read directly out of Altium's 3D Body properties panel.
    """
    from math import radians, degrees, cos, atan2, asin

    def _rx(a): c,s=cos(a),__import__('math').sin(a); return [[1,0,0],[0,c,-s],[0,s,c]]
    def _ry(a): c,s=cos(a),__import__('math').sin(a); return [[c,0,s],[0,1,0],[-s,0,c]]
    def _rz(a): c,s=cos(a),__import__('math').sin(a); return [[c,-s,0],[s,c,0],[0,0,1]]
    def _mul(A,B): return [[sum(A[i][k]*B[k][j] for k in range(3)) for j in range(3)] for i in range(3)]

    d = radians
    R_eagle = _mul(_mul(_rx(d(rx_e)), _ry(d(ry_e))), _rz(d(rz_e)))
    M = _mul(R_eagle, _rx(d(-90)))

    ry_m = asin(max(-1.0, min(1.0, M[0][2])))
    if abs(cos(ry_m)) > 1e-6:
        rz_m = atan2(-M[0][1], M[0][0])
        rx_m = atan2(-M[1][2], M[2][2])
    else:
        rz_m = atan2(M[1][0], M[1][1])
        rx_m = 0.0

    def _clean(rad):
        v = degrees(rad)
        v = v % 360
        if v > 180: v -= 360
        if abs(v) < 1e-9: v = 0.0
        return int(v) if v == int(v) else round(v, 4)

    return _clean(rx_m), _clean(ry_m), _clean(rz_m)


def parse_3d(desc):
    """Extract 3D placement dict from Eagle description comment <!--3d:{...}-->."""
    if not desc:
        return None
    m = re.search(r'<!--3d:(\{[^}]+\})-->', desc)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Symbol conversion
# ---------------------------------------------------------------------------

def convert_symbol(sym_el, sym_name):
    """Convert Eagle <symbol> to IR <symbol name="..."> for the library pool."""
    sym = ET.Element('symbol')
    sym.set('name', sym_name)

    for child in sym_el:
        tag = child.tag
        layer = int(child.get('layer', 0))
        ir_layer = LAYER_MAP.get(layer)

        if tag == 'wire':
            if ir_layer not in _SYM_GEOMETRY_LAYERS:
                continue
            curve = float(child.get('curve', 0))
            x1, y1 = float(child.get('x1')), float(child.get('y1'))
            x2, y2 = float(child.get('x2')), float(child.get('y2'))
            width = child.get('width', '0.1524')
            if curve != 0:
                arc = eagle_arc(x1, y1, x2, y2, curve)
                if arc:
                    cx, cy, r, start, sweep = arc
                    el = ET.SubElement(sym, 'arc')
                    el.set('cx', str(round(cx * 1000))); el.set('cy', str(round(cy * 1000)))
                    el.set('r', str(round(r * 1000)))
                    el.set('start', fmt(start)); el.set('sweep', fmt(sweep))
                    el.set('width', _um(width))
            else:
                el = ET.SubElement(sym, 'line')
                el.set('x1', str(round(x1 * 1000))); el.set('y1', str(round(y1 * 1000)))
                el.set('x2', str(round(x2 * 1000))); el.set('y2', str(round(y2 * 1000)))
                el.set('width', _um(width))

        elif tag == 'rectangle' and ir_layer in _SYM_GEOMETRY_LAYERS:
            x1, y1 = float(child.get('x1')), float(child.get('y1'))
            x2, y2 = float(child.get('x2')), float(child.get('y2'))
            rot, _ = parse_rot(child.get('rot'))
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            w, h = abs(x2 - x1), abs(y2 - y1)
            if round(rot) % 180 == 90:
                w, h = h, w
                rot = rot - 90
            el = ET.SubElement(sym, 'shape')
            el.set('x', str(round(cx * 1000))); el.set('y', str(round(cy * 1000)))
            el.set('w', str(round(w * 1000))); el.set('h', str(round(h * 1000)))
            el.set('roundness', '0')
            el.set('rot', str(round(rot)))
            el.set('outline', _um(child.get('width', '0')))

        elif tag == 'circle' and ir_layer in _SYM_GEOMETRY_LAYERS:
            r_um = round(float(child.get('radius')) * 1000)
            el = ET.SubElement(sym, 'shape')
            el.set('x', _um(child.get('x'))); el.set('y', _um(child.get('y')))
            el.set('w', str(r_um * 2)); el.set('h', str(r_um * 2))
            el.set('roundness', '100')
            el.set('rot', '0')
            el.set('outline', _um(child.get('width', '0')))

        elif tag == 'polygon' and ir_layer in _SYM_GEOMETRY_LAYERS:
            width_um = round(float(child.get('width', '0')) * 1000)
            if 0 < width_um < 50:
                width_um = 50
            pg = ET.SubElement(sym, 'polygon')
            pg.set('width', str(width_um))
            for v in child.findall('vertex'):
                ve = ET.SubElement(pg, 'vertex')
                ve.set('x', str(round(float(v.get('x', '0')) * 1000)))
                ve.set('y', str(round(float(v.get('y', '0')) * 1000)))

        elif tag == 'text':
            if ir_layer not in _SYM_TEXT_LAYERS:
                continue
            rot, _ = parse_rot(child.get('rot'))
            el = ET.SubElement(sym, 'text')
            el.set('x', _um(child.get('x'))); el.set('y', _um(child.get('y')))
            el.set('size', _um(child.get('size')))
            el.set('rot', fmt(rot))
            el.set('align', child.get('align', 'bottom-left'))
            el.set('layer', ir_layer)
            if child.get('font') == 'vector':
                el.set('font', 'vector')
            el.text = child.text or ''

        elif tag == 'pin':
            direction = child.get('direction', 'io')
            ir_dir = DIR_MAP.get(direction)
            if ir_dir is None:
                continue  # skip NC pins
            rot, _ = parse_rot(child.get('rot'))
            pin_len_um = {'point': 0, 'short': 2540, 'middle': 5080, 'long': 7620}.get(
                child.get('length', 'long'), 7620)
            el = ET.SubElement(sym, 'pin')
            el.set('name', child.get('name'))
            el.set('x', _um(child.get('x'))); el.set('y', _um(child.get('y')))
            el.set('rot', fmt(rot))
            el.set('length', str(pin_len_um))
            el.set('direction', ir_dir)
            el.set('function', child.get('function', 'none'))
            el.set('visible', child.get('visible', 'both'))

    return sym


# ---------------------------------------------------------------------------
# Footprint (package) conversion
# ---------------------------------------------------------------------------

def convert_package(pkg_el, pkg_name):
    fp = ET.Element('footprint')
    fp.set('name', pkg_name)

    desc_el = pkg_el.find('description')
    desc_text = (desc_el.text or '') if desc_el is not None else ''
    clean_desc = re.sub(r'\s*<!--3d:\{[^}]*\}-->', '', desc_text).strip()
    if clean_desc:
        d = ET.SubElement(fp, 'description')
        d.text = clean_desc

    layers = {
        'top':          ET.Element('top'),
        'bottom':       ET.Element('bottom'),
        'cream_top':    ET.Element('cream_top'),
        'cream_bottom': ET.Element('cream_bottom'),
        'silk_top':     ET.Element('silk_top'),
        'silk_bottom':  ET.Element('silk_bottom'),
        'fab':          ET.Element('fab'),
        'courtyard':    ET.Element('courtyard'),
        'labels':       ET.Element('labels'),
    }

    for child in pkg_el:
        tag = child.tag
        layer = int(child.get('layer', 0))
        ir_layer = LAYER_MAP.get(layer)

        if tag == 'smd':
            bucket = layers.get(ir_layer)
            if bucket is None:
                continue
            rot, _ = parse_rot(child.get('rot'))
            el = ET.SubElement(bucket, 'smd')
            el.set('name', child.get('name'))
            el.set('x', _um(child.get('x'))); el.set('y', _um(child.get('y')))
            el.set('width', str(round(float(child.get('dx')) * 1000)))
            el.set('height', str(round(float(child.get('dy')) * 1000)))
            el.set('roundness', child.get('roundness', '0'))
            if rot:
                el.set('rot', fmt(rot))

        elif tag == 'pad':
            bucket = layers['top']
            shape = child.get('shape', 'round')
            ir_shape = 'square' if shape == 'square' else 'round'
            el = ET.SubElement(bucket, 'pad')
            el.set('name', child.get('name'))
            el.set('x', _um(child.get('x'))); el.set('y', _um(child.get('y')))
            el.set('drill', _um(child.get('drill')))
            if child.get('diameter'):
                el.set('diameter', _um(child.get('diameter')))
            el.set('shape', ir_shape)

        elif tag == 'hole':
            bucket = layers['top']
            el = ET.SubElement(bucket, 'hole')
            el.set('x', _um(child.get('x'))); el.set('y', _um(child.get('y')))
            el.set('drill', _um(child.get('drill')))

        elif tag == 'wire':
            bucket = layers.get(ir_layer)
            if bucket is None:
                continue
            curve = float(child.get('curve', 0))
            x1, y1 = float(child.get('x1')), float(child.get('y1'))
            x2, y2 = float(child.get('x2')), float(child.get('y2'))
            width = child.get('width', '0.1524')
            if curve != 0:
                arc = eagle_arc(x1, y1, x2, y2, curve)
                if arc:
                    cx, cy, r, start, sweep = arc
                    el = ET.SubElement(bucket, 'arc')
                    el.set('cx', str(round(cx * 1000))); el.set('cy', str(round(cy * 1000)))
                    el.set('r', str(round(r * 1000)))
                    el.set('start', fmt(start)); el.set('sweep', fmt(sweep))
                    el.set('width', _um(width))
            else:
                el = ET.SubElement(bucket, 'line')
                el.set('x1', str(round(x1 * 1000))); el.set('y1', str(round(y1 * 1000)))
                el.set('x2', str(round(x2 * 1000))); el.set('y2', str(round(y2 * 1000)))
                el.set('width', _um(width))

        elif tag == 'circle':
            bucket = layers.get(ir_layer)
            if bucket is None:
                continue
            r_um = round(float(child.get('radius')) * 1000)
            el = ET.SubElement(bucket, 'shape')
            el.set('x', _um(child.get('x'))); el.set('y', _um(child.get('y')))
            el.set('w', str(r_um * 2)); el.set('h', str(r_um * 2))
            el.set('roundness', '100')
            el.set('rot', '0')
            el.set('outline', _um(child.get('width', '0')))

        elif tag == 'rectangle':
            bucket = layers.get(ir_layer)
            if bucket is None:
                continue
            x1, y1 = float(child.get('x1')), float(child.get('y1'))
            x2, y2 = float(child.get('x2')), float(child.get('y2'))
            rot, _ = parse_rot(child.get('rot'))
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            w, h = abs(x2 - x1), abs(y2 - y1)
            if round(rot) % 180 == 90:
                w, h = h, w
                rot = rot - 90
            el = ET.SubElement(bucket, 'shape')
            el.set('x', str(round(cx * 1000))); el.set('y', str(round(cy * 1000)))
            el.set('w', str(round(w * 1000))); el.set('h', str(round(h * 1000)))
            el.set('roundness', '0')
            el.set('rot', str(round(rot)))
            el.set('outline', _um(child.get('width', '0')))

        elif tag == 'polygon':
            bucket = layers.get(ir_layer)
            if bucket is None:
                continue
            width_um = round(float(child.get('width', '0')) * 1000)
            if 0 < width_um < 50:
                width_um = 50
            pg = ET.SubElement(bucket, 'polygon')
            pg.set('width', str(width_um))
            for v in child.findall('vertex'):
                ve = ET.SubElement(pg, 'vertex')
                ve.set('x', str(round(float(v.get('x', '0')) * 1000)))
                ve.set('y', str(round(float(v.get('y', '0')) * 1000)))

        elif tag == 'text':
            bucket = layers.get(ir_layer)
            if bucket is None:
                continue
            rot, _ = parse_rot(child.get('rot'))
            el = ET.SubElement(bucket, 'text')
            el.set('x', _um(child.get('x'))); el.set('y', _um(child.get('y')))
            el.set('size', _um(child.get('size')))
            el.set('rot', fmt(rot))
            el.set('align', child.get('align', 'bottom-left'))
            if child.get('font') == 'vector':
                el.set('font', 'vector')
            el.text = child.text or ''

    for name, bucket in layers.items():
        if len(bucket) > 0:
            fp.append(bucket)

    d3 = parse_3d(desc_text)
    if d3:
        m3d = ET.SubElement(fp, 'model3d')
        m3d.set('tx', str(round(float(d3.get('tx', 0)) * 1000)))
        m3d.set('ty', str(round(float(d3.get('ty', 0)) * 1000)))
        m3d.set('tz', str(round(float(d3.get('tz', 0)) * 1000)))
        rx_m, ry_m, rz_m = _eagle_to_mcad_rot(
            float(d3.get('rx', 0)), float(d3.get('ry', 0)), float(d3.get('rz', 0)))
        m3d.set('rx', fmt(rx_m))
        m3d.set('ry', fmt(ry_m))
        m3d.set('rz', fmt(rz_m))

    return fp


# ---------------------------------------------------------------------------
# Component (deviceset) conversion
# ---------------------------------------------------------------------------

def collect_attributes(ds_el):
    attrs = {}
    for tech in ds_el.iter('technology'):
        for attr in tech.findall('attribute'):
            name = attr.get('name')
            val  = attr.get('value', '')
            if name not in attrs or (not attrs[name] and val):
                attrs[name] = val
    return attrs


def convert_deviceset(ds_el, packages):
    comp = ET.Element('component')
    comp.set('name', ds_el.get('name'))
    if ds_el.get('prefix'):
        comp.set('prefix', ds_el.get('prefix'))
    if ds_el.get('uservalue') == 'yes':
        comp.set('uservalue', 'yes')

    desc_el = ds_el.find('description')
    if desc_el is not None and desc_el.text:
        d = ET.SubElement(comp, 'description')
        d.text = desc_el.text

    attrs_el = ET.SubElement(comp, 'attributes')
    ET.SubElement(attrs_el, 'attr').attrib.update({'name': 'value', 'value': ''})
    for name, val in collect_attributes(ds_el).items():
        ET.SubElement(attrs_el, 'attr').attrib.update({'name': name.lower(), 'value': val})

    gates_el = ds_el.find('gates')
    gate_list = gates_el.findall('gate') if gates_el is not None else []
    multi = len(gate_list) > 1

    if not multi and gate_list:
        comp.set('symbol', gate_list[0].get('symbol', ''))
    else:
        for gate in gate_list:
            g = ET.SubElement(comp, 'gate')
            g.set('name', gate.get('name'))
            g.set('symbol', gate.get('symbol', ''))
            g.set('x', str(round(float(gate.get('x', '0')) * 1000)))
            g.set('y', str(round(float(gate.get('y', '0')) * 1000)))

    for device in ds_el.find('devices').findall('device'):
        pkg_name = device.get('package')
        if not pkg_name or pkg_name not in packages:
            continue

        fp = convert_package(packages[pkg_name], pkg_name)
        dev_variant = device.get('name', '')
        if dev_variant:
            fp.set('variant', dev_variant)

        # Per-device attributes (from all technologies merged; first non-empty wins).
        # NOTE: Eagle technologies (e.g. -1%, -5% tolerance variants) are NOT supported —
        # multiple technologies per device collapse into one attribute set.
        dev_attrs: dict[str, str] = {}
        for tech in device.findall('technologies/technology'):
            for attr in tech.findall('attribute'):
                n = attr.get('name', '').lower()
                v = attr.get('value', '')
                if n and (n not in dev_attrs or (not dev_attrs[n] and v)):
                    dev_attrs[n] = v
        if dev_attrs:
            fa = ET.SubElement(fp, 'attributes')
            for n, v in dev_attrs.items():
                ET.SubElement(fa, 'attr').attrib.update({'name': n, 'value': v})

        connects = device.find('connects')
        if connects is not None:
            pm = ET.SubElement(fp, 'pin-mapping')
            for conn in connects.findall('connect'):
                m = ET.SubElement(pm, 'map')
                m.set('pad', conn.get('pad'))
                if multi:
                    m.set('pin', f"{conn.get('gate')}.{conn.get('pin')}")
                else:
                    m.set('pin', conn.get('pin'))

        comp.append(fp)

    return comp


# ---------------------------------------------------------------------------
# Top-level library conversion
# ---------------------------------------------------------------------------

def convert(lbr_path, output_path=None):
    tree = ET.parse(lbr_path)
    root = tree.getroot()
    lib_el = root.find('.//library')

    packages = {p.get('name'): p for p in lib_el.find('packages').findall('package')}
    eagle_syms = {s.get('name'): s for s in lib_el.find('symbols').findall('symbol')}

    lib = ET.Element('library')
    lib.set('name', Path(lbr_path).stem)

    # Symbol pool — all symbols defined in the library
    symbols_el = ET.SubElement(lib, 'symbols')
    for sym_name, sym_el in eagle_syms.items():
        symbols_el.append(convert_symbol(sym_el, sym_name))

    # Components from devicesets
    used_pkg_names = set()
    for ds in lib_el.find('devicesets').findall('deviceset'):
        comp = convert_deviceset(ds, packages)
        lib.append(comp)
        for device in ds.find('devices').findall('device'):
            pkg = device.get('package', '')
            if pkg:
                used_pkg_names.add(pkg)

    # Orphaned packages (defined but not referenced by any deviceset)
    for pkg_name, pkg_el in packages.items():
        if pkg_name not in used_pkg_names:
            lib.append(convert_package(pkg_el, pkg_name))

    # 3D model files: attach file= attrs to <model3d> BEFORE serializing to XML
    if output_path:
        _copy_step_files(lib, Path(lbr_path), Path(output_path))

    xml_str = minidom.parseString(ET.tostring(lib, encoding='unicode')) \
                     .toprettyxml(indent='  ')
    lines = xml_str.splitlines()
    clean = '\n'.join(l for l in lines if l.strip())
    result = '<?xml version="1.0" encoding="utf-8"?>\n' + \
             '\n'.join(clean.splitlines()[1:])

    if output_path:
        Path(output_path).write_text(result, encoding='utf-8')
        print(f"Written: {output_path}")
    return result


def _copy_step_files(lib_el, lbr_path, out_path):
    """Find STEP/WRL files in <lbr_stem>/ next to lbr, copy to <out_stem>/ next to IR.

    Also sets file="<pkg>.step" attribute on each matching <model3d> element.
    """
    step_src = lbr_path.parent / lbr_path.stem
    if not step_src.is_dir():
        return
    step_dst = out_path.parent / out_path.stem

    def _attach(fp_el):
        m3d = fp_el.find('model3d')
        if m3d is None:
            return
        pkg_name = fp_el.get('name', '')
        for ext in ('.step', '.stp', '.wrl'):
            src = step_src / f'{pkg_name}{ext}'
            if src.exists():
                m3d.set('file', src.name)
                step_dst.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, step_dst / src.name)
                print(f'  3D: {src.name}')
                break

    for comp in lib_el.findall('component'):
        for fp_el in comp.findall('footprint'):
            _attach(fp_el)
    for fp_el in lib_el.findall('footprint'):
        _attach(fp_el)


if __name__ == '__main__':
    import sys
    lbr = sys.argv[1] if len(sys.argv) > 1 else 'testData/rc.lbr'
    out = sys.argv[2] if len(sys.argv) > 2 else 'testData/rc.swlib'
    convert(lbr, out)
