"""IR → KiCad .kicad_sym + .pretty exporter."""
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from babel.ir_util import symbol_pool, component_gates

_SYM_VERSION = 20251024   # KiCad 10
_FP_VERSION  = 20251024

_PIN_DIR = {
    'in':  'input',
    'out': 'output',
    'io':  'bidirectional',
    'pwr': 'power_in',
    'pas': 'passive',
}

_FP_LAYER = {
    'silk_top':     'F.SilkS',
    'silk_bottom':  'B.SilkS',
    'fab':          'F.Fab',
    'courtyard':    'F.CrtYd',
    'top':          'F.Cu',
    'bottom':       'B.Cu',
    'labels':       'F.Fab',
    'cream_top':    'F.Paste',
    'cream_bottom': 'B.Paste',
}


def _f(v):
    r = round(float(v), 6)
    return '0' if r == 0 else f'{r:g}'


def _mm(um):
    """IR µm → mm float for KiCad output."""
    return float(um) / 1000


def _ky(y):
    """IR Y (up, µm) → KiCad Y (down, mm)."""
    return -float(y) / 1000


def _q(s):
    return '"' + str(s).replace('\\', '\\\\').replace('"', '\\"') + '"'


def _build_pin_map(fp_el, gate_name=None):
    """pin_name → first_pad_number from <pin-mapping>.

    For multi-gate components the mapping pins are 'GATE.pin'; pass gate_name
    to strip the matching prefix and return bare pin names.
    """
    if fp_el is None:
        return {}
    pm = fp_el.find('pin-mapping')
    if pm is None:
        return {}
    result = {}
    prefix = f'{gate_name}.' if gate_name else None
    for m in pm.findall('map'):
        pin = m.get('pin', '')
        pad = m.get('pad', '').split()[0]
        if not pin or not pad:
            continue
        if prefix:
            if pin.startswith(prefix):
                result[pin[len(prefix):]] = pad
        else:
            result[pin] = pad
    return result


# ---------------------------------------------------------------------------
# Symbol geometry
# ---------------------------------------------------------------------------

def _sym_geom(sym_el, pin_to_pad):
    """Yield indented S-expression strings for symbol body elements."""
    pin_seq = 0
    for el in sym_el:
        t = el.tag

        if t == 'line':
            x1 = _f(_mm(el.get('x1'))); y1 = _f(_mm(el.get('y1')))
            x2 = _f(_mm(el.get('x2'))); y2 = _f(_mm(el.get('y2')))
            w  = _f(_mm(el.get('width', '0')))
            yield (f'      (polyline\n'
                   f'        (pts (xy {x1} {y1}) (xy {x2} {y2}))\n'
                   f'        (stroke (width {w}) (type default))\n'
                   f'        (fill (type none))\n'
                   f'      )')

        elif t == 'arc':
            cx, cy = _mm(el.get('cx')), _mm(el.get('cy'))
            r      = _mm(el.get('r'))
            start  = float(el.get('start'))
            sweep  = float(el.get('sweep'))
            w      = _f(_mm(el.get('width', '0')))
            sr = math.radians(start)
            mr = math.radians(start + sweep / 2)
            er = math.radians(start + sweep)
            sx = _f(cx + r * math.cos(sr));  sy = _f(cy + r * math.sin(sr))
            mx = _f(cx + r * math.cos(mr));  my = _f(cy + r * math.sin(mr))
            ex = _f(cx + r * math.cos(er));  ey = _f(cy + r * math.sin(er))
            yield (f'      (arc (start {sx} {sy}) (mid {mx} {my}) (end {ex} {ey})\n'
                   f'        (stroke (width {w}) (type default))\n'
                   f'        (fill (type none))\n'
                   f'      )')

        elif t == 'shape':
            x, y = _mm(el.get('x')), _mm(el.get('y'))
            w, h = _mm(el.get('w', '0')), _mm(el.get('h', '0'))
            rnd  = int(el.get('roundness', 0))
            outline = _mm(el.get('outline', '0'))
            sw   = _f(outline)
            fill = 'none' if outline else 'background'
            if rnd == 100:
                yield (f'      (circle (center {_f(x)} {_f(y)}) (radius {_f(w/2)})\n'
                       f'        (stroke (width {sw}) (type default))\n'
                       f'        (fill (type {fill}))\n'
                       f'      )')
            else:
                x1 = _f(x - w/2); y1 = _f(y + h/2)
                x2 = _f(x + w/2); y2 = _f(y - h/2)
                yield (f'      (rectangle (start {x1} {y1}) (end {x2} {y2})\n'
                       f'        (stroke (width {sw}) (type default))\n'
                       f'        (fill (type {fill}))\n'
                       f'      )')

        elif t == 'polygon':
            w = _f(_mm(el.get('width', '0')))
            pts = ''.join(f' (xy {_f(_mm(v.get("x","0")))} {_f(_mm(v.get("y","0")))})'
                          for v in el.findall('vertex'))
            yield (f'      (polyline\n'
                   f'        (pts{pts})\n'
                   f'        (stroke (width {w}) (type default))\n'
                   f'        (fill (type outline))\n'
                   f'      )')

        elif t == 'text':
            text = el.text or ''
            if text.startswith('>'):
                continue
            kx     = _f(_mm(el.get('x')));  ky_ = _f(_mm(el.get('y')))
            sz_f   = _mm(el.get('size', '1270'))
            sz     = _f(sz_f)
            rot    = float(el.get('rot', 0))
            kr     = _f(rot % 360)
            ratio_ = int(el.get('ratio', '8'))
            t      = _f(sz_f * ratio_ / 100)
            b      = ' (bold yes)' if ratio_ >= 15 else ' (bold no)'
            yield (f'      (text {_q(text)} (at {kx} {ky_} {kr})\n'
                   f'        (effects (font (size {sz} {sz}) (thickness {t}){b}))\n'
                   f'      )')

        elif t == 'pin':
            name   = el.get('name', '')
            dir_   = _PIN_DIR.get(el.get('direction', 'pas'), 'passive')
            kx     = _f(_mm(el.get('x')))
            ky_    = _f(_mm(el.get('y')))
            rot    = float(el.get('rot', 0))
            ka     = _f(rot % 360)
            length = _f(_mm(el.get('length', '2540')))
            pin_seq += 1
            number = pin_to_pad.get(name, str(pin_seq))
            yield (f'      (pin {dir_} line (at {kx} {ky_} {ka}) (length {length})\n'
                   f'        (name {_q(name)} (effects (font (size 1.27 1.27))))\n'
                   f'        (number {_q(number)} (effects (font (size 1.27 1.27))))\n'
                   f'      )')


# ---------------------------------------------------------------------------
# Symbol export
# ---------------------------------------------------------------------------

def export_symbol(comp_el, root, lib_name):
    """
    Convert <component> to a KiCad symbol S-expression string.
    Returns None for components with no resolvable symbol.
    """
    gates = component_gates(comp_el)
    pool  = symbol_pool(root)
    gate_syms = [(gn, pool[sn]) for gn, sn in gates if sn in pool]
    if not gate_syms:
        return None

    comp_id = comp_el.get('name', '')
    prefix  = comp_el.get('prefix', 'U')
    fps     = comp_el.findall('footprint')
    multi   = len(fps) > 1

    if len(gate_syms) > 1:
        print(f'  ! {comp_id}: multi-gate KiCad units not implemented, '
              f'flattening {len(gate_syms)} gates')

    # First gate drives symbol-level text/style/visibility
    sym_el = gate_syms[0][1]

    fp_ref     = f'{lib_name}:{fps[0].get("name")}' if len(fps) == 1 else ''
    pin_to_pad = _build_pin_map(fps[0] if fps else None, gate_syms[0][0])

    attrs_el = comp_el.find('attributes')
    attrs = {}
    for a in (attrs_el.findall('attr') if attrs_el is not None else []):
        attrs[a.get('name', '')] = '' if multi else a.get('value', '')

    datasheet = attrs.pop('datasheet', '')
    value_val = attrs.pop('value', comp_id)

    # Eagle horizontal align → KiCad justify
    def _justify(align):
        h = align.lower().split('-')[-1]
        return {'left': ' (justify left)', 'right': ' (justify right)'}.get(h, '')

    # Collect style (position, size, align, ratio) from all >XXX placeholder texts
    _def_style = lambda y: {'at': (0, y, 0), 'size': 1.27, 'align': 'bottom-left', 'ratio': 8}
    name_style        = _def_style(2.54)
    value_style       = _def_style(0)
    placeholder_style = {}   # lowercase attr name → style dict
    for el in sym_el:
        if el.tag != 'text':
            continue
        txt = (el.text or '').strip()
        if not txt.startswith('>'):
            continue
        rot = float(el.get('rot', 0))
        info = {
            'at':    (_mm(el.get('x', 0)), _mm(el.get('y', 0)), rot % 360),
            'size':  _mm(el.get('size', '1270')),
            'align': el.get('align', 'bottom-left'),
            'ratio': int(el.get('ratio', '8')),
        }
        if txt == '>NAME':    name_style  = info
        elif txt == '>VALUE': value_style = info
        else:                 placeholder_style[txt[1:].lower()] = info

    def _effects(style):
        sz    = style['size']
        ratio = style.get('ratio', 8)
        s     = _f(sz)
        t     = _f(sz * ratio / 100)
        b     = ' (bold yes)' if ratio >= 15 else ' (bold no)'
        j     = _justify(style['align'])
        return f'(effects (font (size {s} {s}) (thickness {t}){b}){j})'

    def _at(info):
        a = info['at']
        return f'{_f(a[0])} {_f(a[1])} {_f(a[2])}'

    def _prop(name, val, at_str, eff, hide=False):
        result = [f'    (property {_q(name)} {_q(val)}',
                  f'      (at {at_str})',
                  f'      (show_name no)',
                  f'      (do_not_autoplace yes)']
        if hide:
            result.append(f'      (hide yes)')
        result.append(f'      {eff}')
        result.append(f'    )')
        return result

    # Majority vote on pin name / number visibility
    show_name = show_num = 0
    for el in sym_el:
        if el.tag != 'pin':
            continue
        v = el.get('visible', 'both')
        if v in ('both', 'pin'):  show_name += 1
        else:                     show_name -= 1
        if v in ('both', 'pad'):  show_num  += 1
        else:                     show_num  -= 1

    _hidden = {'at': (0, 0, 0), 'size': 1.27, 'align': 'center'}

    # pin_names block
    pn_lines = ['    (pin_names', '      (offset 1.016)']
    if show_name < 0:
        pn_lines.append('      (hide yes)')
    pn_lines.append('    )')
    # pin_numbers block
    pnum_lines = ['    (pin_numbers']
    if show_num < 0:
        pnum_lines.append('      (hide yes)')
    pnum_lines.append('    )')

    lines = [f'  (symbol {_q(comp_id)}',
             *pnum_lines,
             *pn_lines,
             f'    (exclude_from_sim no)',
             f'    (in_bom yes)',
             f'    (on_board yes)',
             f'    (in_pos_files yes)',
             f'    (duplicate_pin_numbers_are_jumpers no)']

    lines.extend(_prop('Reference', prefix,    _at(name_style),  _effects(name_style)))
    lines.extend(_prop('Value',     value_val, _at(value_style), _effects(value_style)))
    lines.extend(_prop('Footprint', fp_ref,    '0 -2.54 0',      _effects(_hidden), hide=True))
    lines.extend(_prop('Datasheet', datasheet, '0 -5.08 0',      _effects(_hidden), hide=True))
    # Attrs: visible if Eagle had a >ATTRNAME placeholder, hidden otherwise
    attrs_lower = {k.lower(): k for k in attrs}
    emitted = set()
    for ph_name, ph_style in placeholder_style.items():
        orig = attrs_lower.get(ph_name, ph_name)
        val  = attrs.get(orig, '')
        lines.extend(_prop(orig, val, _at(ph_style), _effects(ph_style), hide=False))
        emitted.add(orig)
    for name, val in attrs.items():
        if name not in emitted:
            lines.extend(_prop(name, val, '0 0 0', _effects(_hidden), hide=True))

    lines.append(f'    (symbol {_q(comp_id + "_1_1")}')
    for gname, gsym in gate_syms:
        lines.extend(_sym_geom(gsym, _build_pin_map(fps[0] if fps else None, gname)))
    lines.append(f'    )')
    lines.append(f'    (embedded_fonts no)')
    lines.append(f'  )')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Footprint export
# ---------------------------------------------------------------------------

def export_footprint(fp_el):
    """Convert <footprint> IR element to a KiCad .kicad_mod S-expression string."""
    fp_id = fp_el.get('name', 'unknown')

    desc_el = fp_el.find('description')
    desc_raw = (desc_el.text or '') if desc_el is not None else ''
    # Strip embedded HTML / 3D metadata for KiCad description
    desc = desc_raw.split('\n')[0].split('<')[0].strip()[:120]

    # Collect >NAME / >VALUE positions from any layer
    def _fp_text_style(placeholder, default_y):
        for layer_el in fp_el:
            for el in layer_el:
                if el.tag == 'text' and (el.text or '').strip() == placeholder:
                    ratio_ = int(el.get('ratio', '8') or '8')
                    sz_    = _mm(el.get('size', '1000') or '1000')
                    return True, {
                        'x':     _f(_mm(el.get('x', '0'))),
                        'y':     _f(_ky(el.get('y', '0'))),
                        'rot':   _f(float(el.get('rot', '0') or '0') % 360),
                        'size':  _f(sz_),
                        'thick': _f(sz_ * ratio_ / 100),
                        'bold':  '(bold yes)' if ratio_ >= 15 else '(bold no)',
                    }
        sz_ = 1.0
        return False, {'x': '0', 'y': _f(default_y), 'rot': '0',
                       'size': '1', 'thick': _f(sz_ * 8 / 100), 'bold': '(bold no)'}

    _,  rs = _fp_text_style('>NAME',  -1.5)
    val_found, vs = _fp_text_style('>VALUE',  1.5)

    lines = [f'(footprint {_q(fp_id)}',
             f'  (version {_FP_VERSION})',
             f'  (generator babel)',
             f'  (layer "F.Cu")']
    if desc:
        lines.append(f'  (descr {_q(desc)})')

    lines += [
        f'  (fp_text reference "REF**" (at {rs["x"]} {rs["y"]} {rs["rot"]}) (layer "F.SilkS")',
        f'    (effects (font (size {rs["size"]} {rs["size"]}) (thickness {rs["thick"]}) {rs["bold"]}))',
        f'  )',
        f'  (fp_text value {_q(fp_id)} (at {vs["x"]} {vs["y"]} {vs["rot"]}) (layer "F.Fab")',
        *([ f'    (hide yes)'] if not val_found else []),
        f'    (effects (font (size {vs["size"]} {vs["size"]}) (thickness {vs["thick"]}) {vs["bold"]}))',
        f'  )',
    ]

    for ir_layer, kicad_layer in _FP_LAYER.items():
        layer_el = fp_el.find(ir_layer)
        if layer_el is None:
            continue
        kl = _q(kicad_layer)

        for el in layer_el:
            t = el.tag

            if t == 'line':
                x1 = _f(_mm(el.get('x1'))); y1 = _f(_ky(el.get('y1')))
                x2 = _f(_mm(el.get('x2'))); y2 = _f(_ky(el.get('y2')))
                w  = _f(_mm(el.get('width', '120')))
                lines.append(f'  (fp_line (start {x1} {y1}) (end {x2} {y2}) (layer {kl}) (width {w}))')

            elif t == 'arc':
                cx, cy = _mm(el.get('cx')), _mm(el.get('cy'))
                r      = _mm(el.get('r'))
                start  = float(el.get('start'))
                sweep  = float(el.get('sweep'))
                w      = _f(_mm(el.get('width', '120')))
                sr = math.radians(start)
                mr = math.radians(start + sweep / 2)
                er = math.radians(start + sweep)
                sx = _f(cx + r * math.cos(sr));  sy = _f(-(cy + r * math.sin(sr)))
                mx = _f(cx + r * math.cos(mr));  my = _f(-(cy + r * math.sin(mr)))
                ex = _f(cx + r * math.cos(er));  ey = _f(-(cy + r * math.sin(er)))
                lines.append(f'  (fp_arc (start {sx} {sy}) (mid {mx} {my}) (end {ex} {ey}) (layer {kl}) (width {w}))')

            elif t == 'shape':
                x, y = _mm(el.get('x')), _mm(el.get('y'))
                w, h = _mm(el.get('w', '0')), _mm(el.get('h', '0'))
                rnd  = int(el.get('roundness', 0))
                outline = _mm(el.get('outline', '0'))
                lw   = _f(outline)
                fill = 'none' if outline else 'solid'
                if rnd == 100:
                    r = w / 2
                    lines.append(f'  (fp_circle (center {_f(x)} {_f(-y)}) (end {_f(x+r)} {_f(-y)}) (layer {kl}) (width {lw}) (fill {fill}))')
                else:
                    x1k = _f(x - w/2); y1k = _f(-(y - h/2))
                    x2k = _f(x + w/2); y2k = _f(-(y + h/2))
                    lines.append(f'  (fp_rect (start {x1k} {y1k}) (end {x2k} {y2k}) (layer {kl}) (width {lw}) (fill {fill}))')

            elif t == 'polygon':
                lw = _f(_mm(el.get('width', '0')))
                pts = ' '.join(f'(xy {_f(_mm(v.get("x","0")))} {_f(-_mm(v.get("y","0")))})'
                               for v in el.findall('vertex'))
                lines.append(f'  (fp_poly (pts {pts}) (layer {kl}) (width {lw}) (fill solid))')

            elif t == 'text':
                text = el.text or ''
                if text.startswith('>'):
                    continue
                kx  = _f(_mm(el.get('x')));  ky_ = _f(_ky(el.get('y')))
                sz_f   = _mm(el.get('size', '1000'))
                sz     = _f(sz_f)
                rot = float(el.get('rot', 0))
                kr  = _f((-rot) % 360)
                ratio_ = int(el.get('ratio', '8'))
                t      = _f(sz_f * ratio_ / 100)
                b      = ' (bold yes)' if ratio_ >= 15 else ' (bold no)'
                lines += [f'  (fp_text user {_q(text)} (at {kx} {ky_} {kr}) (layer {kl})',
                          f'    (effects (font (size {sz} {sz}) (thickness {t}){b}))',
                          f'  )']

            elif t == 'smd':
                x, y = _mm(el.get('x')), _mm(el.get('y'))
                w, h = _mm(el.get('width')), _mm(el.get('height'))
                name = el.get('name', '')
                rnd  = float(el.get('roundness', 0)) / 100
                rot  = float(el.get('rot', 0))
                kx = _f(x); ky_ = _f(-y); kw = _f(w); kh = _f(h)
                at = f'{kx} {ky_} {_f((-rot) % 360)}' if rot else f'{kx} {ky_}'
                if rnd > 0:
                    lines += [f'  (pad {_q(name)} smd roundrect (at {at}) (size {kw} {kh})',
                              f'    (layers "F.Cu" "F.Paste" "F.Mask")',
                              f'    (roundrect_rratio {_f(rnd)})',
                              f'  )']
                else:
                    lines += [f'  (pad {_q(name)} smd rect (at {at}) (size {kw} {kh})',
                              f'    (layers "F.Cu" "F.Paste" "F.Mask")',
                              f'  )']

            elif t == 'pad':
                x, y   = _mm(el.get('x')), _mm(el.get('y'))
                drill  = _mm(el.get('drill', '1000'))
                shape  = el.get('shape', 'round')
                name   = el.get('name', '')
                od     = drill * 1.7
                kshape = 'rect' if shape == 'square' else 'circle'
                lines += [f'  (pad {_q(name)} thru_hole {kshape} (at {_f(x)} {_f(-y)}) (size {_f(od)} {_f(od)})',
                          f'    (drill {_f(drill)})',
                          f'    (layers "*.Cu" "*.Mask")',
                          f'  )']

            elif t == 'hole':
                x, y = _mm(el.get('x')), _mm(el.get('y'))
                d    = _mm(el.get('drill'))
                lines += [f'  (pad "" np_thru_hole circle (at {_f(x)} {_f(-y)}) (size {_f(d)} {_f(d)})',
                          f'    (drill {_f(d)})',
                          f'    (layers "*.Cu" "*.Mask")',
                          f'  )']

    lines.append(')')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Top-level export
# ---------------------------------------------------------------------------

def export(ir_path, output_dir=None):
    """
    Export IR library to KiCad format.
    Creates {lib_name}.kicad_sym and {lib_name}.pretty/ in output_dir.
    Returns (sym_path, pretty_dir).
    """
    root     = ET.parse(ir_path).getroot()
    lib_name = root.get('name', Path(ir_path).stem)

    out_dir = Path(output_dir) if output_dir else Path(ir_path).parent
    out_dir.mkdir(parents=True, exist_ok=True)

    sym_path   = out_dir / f'{lib_name}.kicad_sym'
    pretty_dir = out_dir / f'{lib_name}.pretty'
    pretty_dir.mkdir(exist_ok=True)

    # ── Symbol library ────────────────────────────────────────────────────────
    sym_parts = [
        f'(kicad_symbol_lib',
        f'  (version {_SYM_VERSION})',
        f'  (generator "babel")',
        f'  (generator_version "1.0")',
    ]
    for comp in root.findall('component'):
        sexp = export_symbol(comp, root, lib_name)
        if sexp:
            sym_parts.append(sexp)
    sym_parts.append(')')
    sym_path.write_text('\n'.join(sym_parts), encoding='utf-8')

    # ── Footprints ────────────────────────────────────────────────────────────
    seen = set()

    def _write_fp(fp_el):
        fp_id = fp_el.get('name')
        if fp_id in seen:
            return
        seen.add(fp_id)
        safe = re.sub(r'[\\/:*?"<>|]', '_', fp_id)
        (pretty_dir / f'{safe}.kicad_mod').write_text(
            export_footprint(fp_el), encoding='utf-8')

    for comp in root.findall('component'):
        for fp in comp.findall('footprint'):
            _write_fp(fp)

    for fp in root.findall('footprint'):   # orphan packages
        _write_fp(fp)

    print(f'Written: {sym_path}')
    print(f'Written: {pretty_dir}/ ({len(seen)} footprints)')
    return str(sym_path), str(pretty_dir)


if __name__ == '__main__':
    import sys
    ir   = sys.argv[1] if len(sys.argv) > 1 else 'testData/r.swlib'
    out  = sys.argv[2] if len(sys.argv) > 2 else 'testData/kicad_out'
    export(ir, out)
