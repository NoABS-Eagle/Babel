"""IR → Eagle .lbr exporter."""
import math
import re
import xml.etree.ElementTree as ET
from xml.dom import minidom
from pathlib import Path
from babel.eagle_parser import fmt
from babel.ir_util import parse_layer, symbol_pool, component_gates
from babel import import_log


def _eagle_name(s):
    """Clean name for Eagle: letter-space-letter → _, space adjacent to punctuation → remove."""
    s = re.sub(r'([A-Za-z0-9]) ([A-Za-z0-9])', r'\1_\2', s or '')
    return s.replace(' ', '')


def _eagle_designator(s):
    """Pad/pin/signal designator, Eagle-cased — Eagle rejects lowercase
    letters in these outright (confirmed against real Eagle, testData/
    test.lbr, for pads; per the user, the same holds for pin names and, once
    we get there, net/signal names too — not yet individually re-confirmed
    for those, but treated as the same rule).

    IR itself carries no such restriction (e.g. kicad_parser's duplicate-pad
    disambiguation suffix is plain lowercase a/b/c) — this is purely an Eagle
    export-time concern, so it's applied here, not upstream. Must be used
    consistently on BOTH a designator's own declaration (<pad>/<smd>/<pin>)
    and every place that references it (<connect pad="..."/pin="...">), or
    the two stop matching.
    """
    return (s or '').upper()


_LAYERS_FILE = Path(__file__).parent / 'data' / 'eagle_layers.xml'

_WIRE_W = '0.1524'  # default wire width (6 mil), lost during import

def _pkg_eagle_layer(ln):
    """IR footprint/board layer attribute ('121', '-121', '!1', ...) ->
    Eagle layer number, or None (inexpressible -> drop). Inverse of
    eagle_parser._pkg_layer: pair bottoms are Eagle top-number + 1
    (-121 -> 22), standalone layers keep |n|-100, copper 1/-1 -> 1/16,
    inner copper keeps its number, anti-copper -> tRestrict(41)/
    bRestrict(42) (Eagle's native keepout mechanism for that side),
    other anti layers have no Eagle equivalent."""
    try:
        anti, n = parse_layer(ln)
    except (TypeError, ValueError):
        return None
    if anti:
        if abs(n) < 100:
            return 41 if n > 0 else 42
        return None
    if abs(n) < 100:
        if n == 1:
            return 1
        if n == -1:
            return 16
        return abs(n)          # inner copper: number preserved
    if n == 147:
        return 156             # PLATING: canonical Eagle projection (the
                               # |n|-100 formula would give 47 Measures — a
                               # system layer collision)
    base = abs(n) - 100
    return base if n > 0 else base + 1

_PIN_LEN = {0: 'point', 2.54: 'short', 5.08: 'middle', 7.62: 'long'}


def _tomm(um):
    """IR µm integer string → mm float string for Eagle output."""
    r = round(float(um) / 1000, 6)
    return f'{r:g}'

_SYM_TEXT_LAYER = {
    'NETS':    '91',
    'SYMBOLS': '94',
    'NAMES':   '95',
    'VALUES':  '96',
    'INFO':    '97',
    'GRAPHIC': '100',
}


def _pin_length_str(mm):
    closest = min(_PIN_LEN, key=lambda k: abs(k - float(mm)))
    return _PIN_LEN[closest]


def _emit_arc(parent, el, layer):
    """IR <arc> → Eagle <wire curve=...>. The IR arc canon IS Eagle's
    endpoint form (x1 y1 x2 y2 curve, same sign convention: positive = CCW
    from p1 to p2) — a pure attribute copy, µm→mm. No center math, no
    endpoint reconstruction: joints stay lattice-exact (the center-form
    canon produced ~1 µm endpoint wobble and Eagle drew ratsnest stubs at
    every arc↔wire joint — luminoso ground truth). Full circles are not
    arcs in IR (degenerate chord) — they are <shape roundness="100">."""
    w = ET.SubElement(parent, 'wire')
    w.set('x1', _tomm(el.get('x1'))); w.set('y1', _tomm(el.get('y1')))
    w.set('x2', _tomm(el.get('x2'))); w.set('y2', _tomm(el.get('y2')))
    w.set('width', _tomm(el.get('width', '152'))); w.set('layer', layer)
    w.set('curve', fmt(el.get('curve')))


def _rot_attr(deg):
    """Float angle → Eagle rot string, or None if 0."""
    d = round(deg)
    return f'R{d}' if d else None


def _text_layer(txt):
    t = txt.strip()
    if t == '>NAME':  return '95'
    if t == '>VALUE': return '96'
    return '94'


def _eagle_layer(el, fallback='94'):
    """IR <line>/<arc>/<shape>/<polygon> `layer` attribute -> Eagle numeric
    layer string. eagle_parser.convert_symbol now carries the real source
    layer through explicitly for every symbol object, not just text (ir_
    schema.md "Слои": no layer restricts what a symbol object can sit on,
    confirmed directly against real Eagle by the user) — this is the
    reverse direction. A known IR name (_SYM_TEXT_LAYER) maps back to its
    Eagle number; a raw Eagle number surviving round-trip (eagle_parser's
    `str(layer)` fallback for anything outside LAYER_MAP) is already the
    right string as-is; anything else (no layer attr at all — e.g. IR
    written by hand, or a non-Eagle source) falls back to plain Symbols/94.
    """
    ir_layer = el.get('layer', '')
    if ir_layer in _SYM_TEXT_LAYER:
        return _SYM_TEXT_LAYER[ir_layer]
    if ir_layer.isdigit():
        return ir_layer
    return fallback


# ---------------------------------------------------------------------------
# Symbol
# ---------------------------------------------------------------------------

def _packageless_symbols(components):
    """Symbol names used ONLY by footprint-less components. Eagle's rule
    (user decision, found on KiCad's PWR_FLAG): a part with no package may
    carry only `sup`-direction pins — a pin exists to bind to a pad, `sup`
    is the one exception. Such pins are coerced to `sup` at THIS boundary
    only; the IR keeps the honest KLC direction (`pwr` for a power_out ERC
    flag), because a real `sup` pin in IR would NAME the net after itself —
    resurrecting the exact PWR_FLAG-vs-real-rail name conflict in our own
    connectivity. A symbol shared with any footprint-bearing component is
    left alone (coercion would corrupt the packaged user's pin)."""
    with_fp, without_fp = set(), set()
    for comp_el in components:
        target = with_fp if comp_el.findall('footprint') else without_fp
        for _, sname in component_gates(comp_el):
            target.add(sname)
    return without_fp - with_fp


def export_symbol(sym_el, sym_name, coerce_sup=False):
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
            w.set('width', _tomm(el.get('width', '152'))); w.set('layer', _eagle_layer(el))

        elif t == 'arc':
            _emit_arc(sym, el, _eagle_layer(el))

        elif t == 'shape':
            rn = int(el.get('roundness', 0))
            x, y = float(el.get('x')) / 1000, float(el.get('y')) / 1000
            w2 = float(el.get('w', '0')) / 2000
            h2 = float(el.get('h', '0')) / 2000
            outline_mm = _tomm(el.get('outline', '0'))
            shape_layer = _eagle_layer(el)
            if rn == 100:
                c = ET.SubElement(sym, 'circle')
                c.set('x', fmt(x)); c.set('y', fmt(y))
                c.set('radius', fmt(w2))
                c.set('width', outline_mm); c.set('layer', shape_layer)
            elif float(el.get('outline', '0')) == 0:
                r_el = ET.SubElement(sym, 'rectangle')
                r_el.set('x1', fmt(x - w2)); r_el.set('y1', fmt(y - h2))
                r_el.set('x2', fmt(x + w2)); r_el.set('y2', fmt(y + h2))
                r_el.set('layer', shape_layer)
            else:
                corners = [(x-w2, y-h2), (x+w2, y-h2), (x+w2, y+h2), (x-w2, y+h2)]
                for (ax, ay), (bx, by) in zip(corners, corners[1:] + corners[:1]):
                    w_el = ET.SubElement(sym, 'wire')
                    w_el.set('x1', fmt(ax)); w_el.set('y1', fmt(ay))
                    w_el.set('x2', fmt(bx)); w_el.set('y2', fmt(by))
                    w_el.set('width', outline_mm); w_el.set('layer', shape_layer)

        elif t == 'text':
            txt = el.text or ''
            te = ET.SubElement(sym, 'text')
            te.set('x', _tomm(el.get('x'))); te.set('y', _tomm(el.get('y')))
            te.set('size', _tomm(el.get('size')))
            rot = _rot_attr(float(el.get('rot', 0)))
            if rot: te.set('rot', rot)
            align = el.get('align', 'bottom-left')
            if align != 'bottom-left': te.set('align', align)
            if el.get('ratio'): te.set('ratio', el.get('ratio'))
            # Stored IR layer takes priority; fall back to content-based guess
            te.set('layer', _eagle_layer(el, fallback=_text_layer(txt)))
            # Always vector, regardless of source font — per the user, this
            # is the default Babel should target for every Eagle export, not
            # just when IR happened to already say so (proportional/TrueType
            # rendering varies by what's installed, vector doesn't).
            te.set('font', 'vector')
            te.text = txt

        elif t == 'polygon':
            # Eagle <polygon> in a symbol/schematic is always solid-filled —
            # no percent concept there (that's a plated-zone-only knob). An
            # IR fill<=0 (unfilled closed contour) has no direct equivalent,
            # so fall back to a closed wire outline instead of lying about
            # the fill visually.
            verts = [(v.get('x', '0'), v.get('y', '0'), v.get('curve'))
                     for v in el.findall('vertex')]
            if float(el.get('fill', '100')) <= 0 and len(verts) >= 2:
                w = _tomm(el.get('width', '0'))
                lyr = _eagle_layer(el)
                for (ax, ay, _), (bx, by, _) in zip(verts, verts[1:] + verts[:1]):
                    w_el = ET.SubElement(sym, 'wire')
                    w_el.set('x1', _tomm(ax)); w_el.set('y1', _tomm(ay))
                    w_el.set('x2', _tomm(bx)); w_el.set('y2', _tomm(by))
                    w_el.set('width', w); w_el.set('layer', lyr)
            else:
                pg = ET.SubElement(sym, 'polygon')
                pg.set('width', _tomm(el.get('width', '0')))
                pg.set('layer', _eagle_layer(el))
                for x, y, curve in verts:
                    ve = ET.SubElement(pg, 'vertex')
                    ve.set('x', _tomm(x))
                    ve.set('y', _tomm(y))
                    if curve: ve.set('curve', curve)

        elif t == 'pin':
            p = ET.SubElement(sym, 'pin')
            raw = _eagle_designator(_eagle_name(el.get('name', '')))
            pin_name_count[raw] = pin_name_count.get(raw, 0) + 1
            n = pin_name_count[raw]
            p.set('name', raw if n == 1 else f'{raw}@{n}')
            p.set('x', _tomm(el.get('x'))); p.set('y', _tomm(el.get('y')))
            p.set('length', _pin_length_str(float(el.get('length', 2540)) / 1000))
            rot = _rot_attr(float(el.get('rot', 0)))
            if rot: p.set('rot', rot)
            direction = el.get('direction', 'pas')
            if coerce_sup and direction != 'sup':
                # Packageless part — see _packageless_symbols for the rule.
                import_log.log(sym_name, p.get('name'),
                                f'PIN_DIRECTION {direction} -> sup (part has no '
                                f'package; Eagle allows only supply pins without a pad)')
                direction = 'sup'
            p.set('direction', direction)
            # IR's two independent booleans (pinvis = name shown, padvis =
            # number shown) recombined into Eagle's one enum. Default '1'
            # (visible) if somehow missing — defaulting to "hidden" here was
            # the earlier bug (see git history): it silently hid every pin
            # label on every symbol that never explicitly set the attribute.
            pinvis = el.get('pinvis', '1') == '1'
            padvis = el.get('padvis', '1') == '1'
            visible = {(True, True): 'both', (True, False): 'pin',
                       (False, True): 'pad', (False, False): 'off'}[(pinvis, padvis)]
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


def _geom_sig(el):
    """Geometry identity of an element, layer excluded — the key for the
    cut/PLATING twin fold below."""
    return (el.tag,
            tuple(sorted((k, v) for k, v in el.attrib.items() if k != 'layer')),
            (el.text or '').strip())


def _emit_geometry(parent, el, eagle_num):
    """One IR drawing primitive (hole/line/arc/shape/polygon/text) ->
    Eagle element(s) appended to `parent` on layer eagle_num. The emission
    twin of eagle_parser.convert_geometry and its SINGLE home on the way
    out -- shared by export_package and the board exporter. Returns True
    if the tag was handled."""
    t = el.tag
    if t == 'hole':
        h = ET.SubElement(parent, 'hole')
        h.set('x', _tomm(el.get('x'))); h.set('y', _tomm(el.get('y')))
        h.set('drill', _tomm(el.get('drill')))

        return True
    if t == 'line':
        w = ET.SubElement(parent, 'wire')
        w.set('x1', _tomm(el.get('x1'))); w.set('y1', _tomm(el.get('y1')))
        w.set('x2', _tomm(el.get('x2'))); w.set('y2', _tomm(el.get('y2')))
        w.set('width', _tomm(el.get('width', '152'))); w.set('layer', str(eagle_num))

        return True
    if t == 'arc':
        _emit_arc(parent, el, str(eagle_num))

        return True
    if t == 'shape':
        rn = int(el.get('roundness', 0))
        x, y = float(el.get('x')) / 1000, float(el.get('y')) / 1000
        w2 = float(el.get('w', '0')) / 2000
        h2 = float(el.get('h', '0')) / 2000
        outline_mm = _tomm(el.get('outline', '0'))
        lyr = str(eagle_num)
        if rn == 100:
            c = ET.SubElement(parent, 'circle')
            c.set('x', fmt(x)); c.set('y', fmt(y))
            c.set('radius', fmt(w2))
            c.set('width', outline_mm); c.set('layer', lyr)
        elif float(el.get('outline', '0')) == 0:
            r_el = ET.SubElement(parent, 'rectangle')
            r_el.set('x1', fmt(x - w2)); r_el.set('y1', fmt(y - h2))
            r_el.set('x2', fmt(x + w2)); r_el.set('y2', fmt(y + h2))
            r_el.set('layer', lyr)
        else:
            corners = [(x-w2, y-h2), (x+w2, y-h2), (x+w2, y+h2), (x-w2, y+h2)]
            for (ax, ay), (bx, by) in zip(corners, corners[1:] + corners[:1]):
                w_el = ET.SubElement(parent, 'wire')
                w_el.set('x1', fmt(ax)); w_el.set('y1', fmt(ay))
                w_el.set('x2', fmt(bx)); w_el.set('y2', fmt(by))
                w_el.set('width', outline_mm); w_el.set('layer', lyr)

        return True
    if t == 'polygon':
        # Same fill<=0 fallback as the symbol-side branch above —
        # Eagle <polygon> has no percent-fill concept, so an unfilled
        # IR contour becomes a closed wire outline instead.
        verts = [(v.get('x', '0'), v.get('y', '0'), v.get('curve'))
                 for v in el.findall('vertex')]
        if float(el.get('fill', '100')) <= 0 and len(verts) >= 2:
            w = _tomm(el.get('width', '0'))
            lyr = str(eagle_num)
            for (ax, ay, _), (bx, by, _) in zip(verts, verts[1:] + verts[:1]):
                w_el = ET.SubElement(parent, 'wire')
                w_el.set('x1', _tomm(ax)); w_el.set('y1', _tomm(ay))
                w_el.set('x2', _tomm(bx)); w_el.set('y2', _tomm(by))
                w_el.set('width', w); w_el.set('layer', lyr)
        else:
            pg = ET.SubElement(parent, 'polygon')
            pg.set('width', _tomm(el.get('width', '0')))
            pg.set('layer', str(eagle_num))
            for x, y, curve in verts:
                ve = ET.SubElement(pg, 'vertex')
                ve.set('x', _tomm(x))
                ve.set('y', _tomm(y))
                if curve: ve.set('curve', curve)

        return True
    if t == 'text':
        te = ET.SubElement(parent, 'text')
        te.set('x', _tomm(el.get('x'))); te.set('y', _tomm(el.get('y')))
        te.set('size', _tomm(el.get('size')))
        rot = _rot_attr(float(el.get('rot', 0)))
        if el.get('mirror') == '1':
            te.set('rot', 'M' + (rot or 'R0'))   # MR0 stays explicit
        elif rot:
            te.set('rot', rot)
        align = el.get('align', 'bottom-left')
        if align != 'bottom-left': te.set('align', align)
        if el.get('ratio'): te.set('ratio', el.get('ratio'))
        # `>VALUE` always -> tValues (27), regardless of which IR
        # layer bucket it's actually sitting in (confirmed real:
        # always `fab` — a single, side-less documentation layer;
        # the pool footprint has no top/bottom of its own at all,
        # that only exists once an instance is placed+mirrored on a
        # board, same reason the SYMBOL-side `>VALUE` is always 96
        # unconditionally, never bValue-style). Per the user — only
        # `>VALUE`, NOT `>NAME` (stays on whatever layer it's on).
        eagle_layer = '27' if (el.text or '').strip() == '>VALUE' else str(eagle_num)
        te.set('layer', eagle_layer)
        te.set('font', 'vector')  # always — see export_symbol's text branch
        te.text = el.text or ''

        return True
    return False


def export_package(fp_el, pkg_name):
    pkg = ET.Element('package')
    pkg.set('name', _eagle_name(pkg_name))

    # Cut/PLATING twin fold (ir_schema.md "Резы и металлизация"): a cut on
    # 120 with an EXACT copy on 147 goes to Eagle as ONE object on 46
    # Milling (DRC-exempt there, so pours stay flush for the plating) and
    # the copy is not emitted — the deterministic inverse of the import
    # rule 46 -> 120 + 147 copy. Unmatched 120 -> 20, unmatched 147 -> 156.
    plating_pool = {}
    for el in fp_el:
        if el.get('layer') == '147':
            plating_pool.setdefault(_geom_sig(el), []).append(id(el))
    milled = set()      # ids of 120 elements to emit on 46
    folded = set()      # ids of 147 twins to skip
    for el in fp_el:
        if el.get('layer') == '120':
            pool = plating_pool.get(_geom_sig(el))
            if pool:
                folded.add(pool.pop())
                milled.add(id(el))

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

    for el in fp_el:
        t = el.tag
        if t in ('description', 'model3d', 'pin-mapping'):
            continue

        if t in ('smd', 'pad', 'hole'):
            # pads carry no layer attr (mount-side copper by construction);
            # an smd's rare far-side marker layer="-1" selects Bottom(16).
            eagle_num = 16 if el.get('layer') == '-1' else 1
        else:
            if id(el) in folded:
                continue
            eagle_num = 46 if id(el) in milled else _pkg_eagle_layer(el.get('layer'))
            if eagle_num is None:
                continue


        if t == 'smd':
            s = ET.SubElement(pkg, 'smd')
            s.set('name', _eagle_designator(el.get('name')))
            s.set('x', _tomm(el.get('x'))); s.set('y', _tomm(el.get('y')))
            s.set('dx', _tomm(el.get('width'))); s.set('dy', _tomm(el.get('height')))
            rn = int(float(el.get('roundness', 0)))
            if rn: s.set('roundness', str(rn))
            rot = float(el.get('rot', 0))
            if rot:
                r_str = f'R{int(rot)}' if rot == int(rot) else f'R{rot}'
                s.set('rot', r_str)
            s.set('layer', str(eagle_num))
            # IR thermals/stopmask/paste default "1" == Eagle thermals/
            # stop/cream default "yes" — only emit on the (rare)
            # explicit-off case (ir_schema.md "KiCad: технические слои
            # smd-пада"), same omit-the-default convention as roundness/rot.
            if el.get('thermals') == '0': s.set('thermals', 'no')
            if el.get('stopmask') == '0': s.set('stop', 'no')
            if el.get('paste') == '0': s.set('cream', 'no')

        elif t == 'pad':
            p = ET.SubElement(pkg, 'pad')
            p.set('name', _eagle_designator(el.get('name')))
            p.set('x', _tomm(el.get('x'))); p.set('y', _tomm(el.get('y')))
            p.set('drill', _tomm(el.get('drill')))
            if el.get('diameter'):
                p.set('diameter', _tomm(el.get('diameter')))
            shape = el.get('shape', 'round')
            if shape != 'round': p.set('shape', shape)
            if el.get('thermals') == '0': p.set('thermals', 'no')
            if el.get('stopmask') == '0': p.set('stop', 'no')

        else:
            _emit_geometry(pkg, el, eagle_num)

    return pkg


# ---------------------------------------------------------------------------
# Deviceset
# ---------------------------------------------------------------------------

def _device_names(comp_el):
    """Ordered list of `<device name=...>` values export_deviceset will
    create for this component's `<footprint>`s, in the same order — factored
    out so export_schematic's `<part device=...>` can reference a REAL
    device name instead of hardcoding `""`, which only actually exists as a
    device for 0- or 1-footprint components (Eagle convention: empty name
    only for a single, variant-less device). A 2+-footprint component has no
    device named `""` at all — `<part device="">` referencing it is invalid,
    confirmed by the user's real Eagle rejecting it
    ("attribute 'device' references undefined object ''").
    """
    fp_els = comp_el.findall('footprint')
    if not fp_els:
        return ['']
    single = len(fp_els) == 1
    seen = {}
    names = []
    for fp_el in fp_els:
        if single and not fp_el.get('variant'):
            dev_name = ''
        else:
            dev_name = _eagle_name(fp_el.get('variant', fp_el.get('name')))
        seen[dev_name] = seen.get(dev_name, 0) + 1
        if seen[dev_name] > 1:
            dev_name = f'{dev_name}_{seen[dev_name]}'
        names.append(dev_name)
    return names


def export_deviceset(comp_el):
    ds = ET.Element('deviceset')
    # renamed-from: flat-pool @N uniquification of same-named devicesets
    # from same-nicknamed libraries — the Eagle-facing name is the original
    cid = _eagle_name(comp_el.get('renamed-from') or comp_el.get('name', ''))
    ds.set('name', cid)
    if comp_el.get('prefix'):
        ds.set('prefix', comp_el.get('prefix'))
    if comp_el.get('uservalue') == 'yes':
        ds.set('uservalue', 'yes')

    # `description` is a plain IR attribute (same as KiCad/Altium — no
    # dedicated element), but Eagle has its own native deviceset
    # <description> slot, so it's special-cased back out here on export
    # only — excluded from the generic technology/attribute dump below.
    attrs_el = comp_el.find('attributes')
    desc_attr = attrs_el.find('attr[@name="description"]') if attrs_el is not None else None
    if desc_attr is not None and desc_attr.get('value'):
        d = ET.SubElement(ds, 'description')
        d.text = desc_attr.get('value')

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

    # Skip 'value' (built-in Eagle attribute), 'description' (already routed
    # to Eagle's native <description> above) and empty values.
    _seen_attrs = {}
    for a in (attrs_el.findall('attr') if attrs_el is not None else []):
        if a.get('name', '').lower() in ('value', 'description') or not a.get('value', ''):
            continue
        key = _eagle_name(a.get('name', '')).upper()
        _seen_attrs[key] = a.get('value', '')   # last value wins on collision
    attr_pairs = list(_seen_attrs.items())

    devices_el = ET.SubElement(ds, 'devices')

    fp_els = comp_el.findall('footprint')

    if not fp_els:
        # No footprint at all (power-flag/supply symbol, frame, or any other
        # footprint-less component — ir_schema.md "Frame"/"Supply symbol").
        # Eagle still requires at least one <device> per deviceset; the
        # standard Eagle convention for a packageless device (the same one
        # Eagle's own shipped supply*.lbr/frames.lbr use) is name="" and
        # package="", with no <connects> at all — there's nothing to map
        # pins to.
        # eagle.dtd: package is %String; #IMPLIED — optional, but if present
        # must name a real <package>; an empty string isn't a valid name and
        # real Eagle flags it ("invalid value '' for attribute 'package'"
        # confirmed by the user opening a generated file). Omit the
        # attribute entirely rather than supplying an empty one.
        dev = ET.SubElement(devices_el, 'device', name='')
        techs = ET.SubElement(dev, 'technologies')
        tech = ET.SubElement(techs, 'technology', name='')
        for name, val in attr_pairs:
            ET.SubElement(tech, 'attribute', name=name, value=val)

    dev_names = _device_names(comp_el)
    for fp_el, dev_name in zip(fp_els, dev_names):
        pkg_name = fp_el.get('name')
        dev = ET.SubElement(devices_el, 'device')
        dev.set('name', dev_name)
        dev.set('package', _eagle_name(pkg_name))

        pm = fp_el.find('pin-mapping')
        if pm is not None:
            connects = ET.SubElement(dev, 'connects')
            # Merge pads for the same pin — a <map> may already list several
            # space-separated (ir_schema.md's "один пин -> несколько падов"),
            # or the same pin may appear on several <map> elements; either way
            # they all end up joined on this one pin's single <connect>.
            #
            # Real Eagle DOES accept a space-separated pad list on one
            # <connect> regardless of declared version, pad type (SMD/thru-
            # hole/mixed) or count — confirmed against several real
            # Eagle-authored examples in testData/test.lbr, including one with
            # 5 mixed pads on one pin and one native-Eagle-created device with
            # 3 thru-hole-only pads. The real constraint, found the same way:
            # pad designators are case-sensitive, Eagle rejects lowercase
            # letters outright. Per the user this isn't pad-specific — pin
            # names too, and (once nets/signals are in scope) signal names as
            # well — _eagle_designator() below must be applied to BOTH a
            # designator's own declaration and every reference to it
            # (export_symbol's <pin>, here for <connect pin=.../pad=...>), or
            # they stop matching.
            pin_to_pads: dict[str, list[str]] = {}
            for m in pm.findall('map'):
                pin_to_pads.setdefault(m.get('pin'), []).extend(m.get('pad', '').split())
            for pin, pads in pin_to_pads.items():
                conn = ET.SubElement(connects, 'connect')
                if single_mode:
                    conn.set('gate', 'G$1')
                    conn.set('pin', _eagle_designator(_eagle_name(pin)))
                else:
                    gname, _, pname = pin.partition('.')
                    conn.set('gate', _eagle_name(gname))
                    conn.set('pin', _eagle_designator(_eagle_name(pname)))
                conn.set('pad', ' '.join(_eagle_designator(p) for p in pads))

        techs = ET.SubElement(dev, 'technologies')
        tech = ET.SubElement(techs, 'technology')
        tech.set('name', '')
        # Per-DEVICE attributes (fp_el's own <attributes>) take priority
        # over the component-wide attr_pairs. Safe now that
        # eagle_parser.convert_deviceset splits by technology NAME at
        # import time (each resulting IR <component> holds only footprints
        # that share the same real technology, e.g. "R-1%" vs "R-5%" are
        # separate components — ir_schema.md has no per-device/technology
        # level below component/footprint, so this is where that axis gets
        # materialized instead) — attr_pairs itself is no longer a lossy
        # cross-technology merge, just this one component's own deviceset-
        # level facts (real MANF/RU-type data genuinely shared by every
        # footprint of this specific technology). fp_el's per-footprint
        # attrs (e.g. PACKAGE="0402"/"0603"/"0805", genuinely different per
        # footprint even within one technology) still win when both exist —
        # found missing entirely before the technology split existed:
        # testData/maximus.sch's own round-trip previously stamped every
        # device with the same attr_pairs, discarding every device's own
        # PACKAGE value.
        fp_attrs_el = fp_el.find('attributes')
        fp_attrs = {}
        if fp_attrs_el is not None:
            for a in fp_attrs_el.findall('attr'):
                if not a.get('value', ''):
                    continue
                fp_attrs[_eagle_name(a.get('name', '')).upper()] = a.get('value', '')
        merged = dict(attr_pairs)
        merged.update(fp_attrs)
        for name, val in merged.items():
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
    eagle.set('version', '9.6.2')   # the dialect our ground truths came from; a 7.7.0 tag made Eagle 9 take its legacy text path and Cyrillic vector text stopped rendering (user ground truth)
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

    lib_el = ET.SubElement(drawing, 'library', name=lib_name)

    packages_el = ET.SubElement(lib_el, 'packages')
    for pkg_name, fp_el in packages_map.items():
        packages_el.append(export_package(fp_el, pkg_name))

    symbols_el = ET.SubElement(lib_el, 'symbols')
    sup_only = _packageless_symbols(ir_root.findall('component'))
    for sym_el in symbol_pool(ir_root).values():
        symbols_el.append(export_symbol(sym_el, sym_el.get('name'),
                                        coerce_sup=sym_el.get('name') in sup_only))

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


# ---------------------------------------------------------------------------
# Schematic (IR <schematic> -> Eagle .sch, with embedded library)
# ---------------------------------------------------------------------------

# Standard Eagle schematic layer numbers (stable, well-known convention used
# throughout Eagle's own shipped libraries/frames — not project-specific).
_LYR_NETS = '91'
_LYR_SYMBOLS = '94'
_LYR_INFO = '97'
# Babel-defined custom layer, no native Eagle number — see babel/data/
# eagle_layers.xml and decisions.md "Eagle-экспорт: слой GRAPHIC для
# декоративной геометрии схемы".
_LYR_GRAPHIC = '100'


def _is_frame_component(comp_el, pool):
    """True if this component's symbol carries a `FRAME`-layer shape
    (ir_schema.md "Frame") — such a component is never placed as a Eagle
    `<part>`/`<instance>` (a deviceset would be meaningless for it); it
    becomes a native Eagle `<frame>` in `<plain>` instead, mirroring how
    decisions.md "Импорт рамок" already treats Eagle's OWN `<frame>` element
    as the structural ground truth on import — same idea, reversed for export.
    """
    for _, sym_name in component_gates(comp_el):
        sym_el = pool.get(sym_name)
        if sym_el is not None and any(s.get('layer') == 'FRAME'
                                       for s in sym_el.findall('shape')):
            return True
    return False


def _frame_bbox(inst_el, comp_el, pool):
    """Absolute (x1, x2, y1, y2) of a frame instance's boundary shape.

    Frame instances are always rot=0/mirror=0 (synthesized that way on every
    import path so far — ir_schema.md "Frame") so the instance transform is a
    pure translation; no rotation/mirror math needed here.
    """
    sym_name = component_gates(comp_el)[0][1]
    shape = next(s for s in pool[sym_name].findall('shape') if s.get('layer') == 'FRAME')
    ix, iy = float(inst_el.get('x')), float(inst_el.get('y'))
    cx, cy = ix + float(shape.get('x')), iy + float(shape.get('y'))
    w, h = float(shape.get('w')), float(shape.get('h'))
    return cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2


def _resolved_attrs(comp_el, inst_el):
    """Component attrs overlaid with this instance's own overrides — same
    precedence svg_renderer._resolve_placeholder already uses (instance wins).
    """
    attrs = {}
    attrs_el = comp_el.find('attributes')
    if attrs_el is not None:
        attrs = {a.get('name'): a.get('value', '') for a in attrs_el.findall('attr')}
    attrs.update({a.get('name'): a.get('value', '') for a in inst_el.findall('attr')})
    return attrs


def _instance_rot_attr(inst_el):
    """IR <instance> rot/mirror -> Eagle's combined 'MR90'-style rot string,
    or None if R0/no mirror (matches _rot_attr's "None means R0" convention).
    """
    deg = round(float(inst_el.get('rot', '0')))
    mirror = inst_el.get('mirror') == '1'
    if not deg and not mirror:
        return None
    return ('M' if mirror else '') + f'R{deg}'


def _emit_part(parts_el, inst_el, comp_el, lib_name, dev_name_by_fp):
    """One Eagle <part> from one IR <instance> — shared by the top-level
    <parts> and each <module>'s own <parts> (a module carries its own part
    list, mirroring the top-level structure — ground truth
    outputs/multigate.sch). One-part-per-designator dedup is the CALLER's
    job (see the top-level loop's comment on Eagle rejecting redefinitions).

    `lib_name` here is the FALLBACK used only when the component itself
    carries no `library=` (ir_schema.md "Компонент `<component>`") — an IR
    document with no per-component origin recorded (e.g. produced by the
    KiCad path, which has no equivalent concept worth preserving). A
    component that DOES carry `library=` (every component an Eagle source
    produces, per the user: "в игл же мы можем хранить имя библиотеки, из
    которой пришел компонент... давай не сливать все компоненты в одну
    библиотеку") uses its own real origin nickname instead — this must
    match whichever <library name=...> that component's deviceset/package/
    symbol actually got written into (see export_schematic's own library
    grouping), or Eagle's <part library="..."> would point at a library
    that doesn't contain the referenced deviceset.
    """
    # _eagle_name() must match EXACTLY what export_schematic's own library-
    # grouping wrote as the owning <library name=...> (found on
    # testData/maximus.sch's own round-trip: "linear regulators" (raw IR
    # value, with a space) written straight onto <part library=...> while
    # the actual <library> element got "linear_regulators" (sanitized) —
    # the two silently diverged, which would make real Eagle unable to
    # resolve the part's own deviceset at all).
    lib_name = _eagle_name(comp_el.get('library') or lib_name)
    fp_map = dev_name_by_fp[comp_el]
    inst_fp = inst_el.get('footprint')
    if inst_fp and inst_fp in fp_map:
        dev_name = fp_map[inst_fp]
    else:
        # ir_schema.md "Component instance": `footprint` is only
        # recorded when the component has 2+ — absent here means
        # either 0/1 footprint (device="" is the correct, real Eagle
        # device in that case, no loss) or an older/foreign IR that
        # never recorded it. Falling back to the first real device
        # only in the genuinely-ambiguous case; logged, since picking
        # the WRONG footprint silently is exactly the bug this
        # recording was added to fix (decisions.md "IR → Eagle:
        # экспорт схемы" — testData/vimdrones.zip "R6"/"R11", a generic
        # resistor instance actually placed as a fuse holder).
        dev_name = next(iter(fp_map.values()), '')
        if len(set(fp_map.values())) > 1:   # distinct DEVICES, not map keys
                                            # (each fp adds name+variant keys)
            import_log.log(inst_el.get('name'), comp_el.get('name'),
                            'DEVICE_VARIANT unknown, using ->', dev_name)
    part = ET.SubElement(parts_el, 'part', name=inst_el.get('name'), library=lib_name,
                          deviceset=_eagle_name(comp_el.get('renamed-from') or comp_el.get('name')), device=dev_name)
    value = _resolved_attrs(comp_el, inst_el).get('value')
    if value:
        part.set('value', value)
    # Per-instance attribute overrides (ir_schema.md "Component instance"):
    # a real per-designator fact (MANF#/LCSC#/ALLOCATED for one specific
    # placement, e.g. a generic "R" deviceset's actual resistor), distinct
    # from device/technology-level attributes already carried by the
    # <component> itself — eagle.dtd's own <part (attribute*, variant*)>
    # models exactly this (a real Eagle <part> commonly carries these
    # directly, confirmed testData/maximus.sch: "rc" library resistors).
    # `value` is excluded (already written as the <part> attribute above,
    # not a child <attribute>). Found missing on maximus.sch's own
    # round-trip (user caught it): _emit_part never wrote these at all,
    # only ever read `value` out of the resolved attrs.
    for a in inst_el.findall('attr'):
        name, val = a.get('name'), a.get('value', '')
        if name == 'value':
            continue
        ET.SubElement(part, 'attribute', name=name.upper(), value=val)
    # Base-configuration assembly flag (ir_schema.md "Варианты сборки"):
    # Eagle has no populate notion outside its variant system — honest
    # degradation, logged, never silent.
    if inst_el.get('populate') == 'no':
        import_log.log(inst_el.get('name'), '',
                        'ASSEMBLY_FLAG populate not representable in Eagle '
                        'base design, dropped')


def _emit_instance(instances_el, inst_el):
    """One Eagle <instance> from one IR component <instance> — shared by
    the top-level sheets and module sheets.

    ir_schema.md "Размещение многорежимного компонента": IR's own
    <instance gate="A"> says which gate THIS placement is (multiple
    <instance>s share one designator, one per gate) — Eagle's
    <instance gate="..."> means the same thing, just always required
    (no implicit single-gate default the way IR omits it for
    single-mode components), hence the 'G$1' fallback for those.
    """
    kwargs = {'part': inst_el.get('name'), 'gate': inst_el.get('gate') or 'G$1',
              'x': _tomm(inst_el.get('x')), 'y': _tomm(inst_el.get('y'))}
    rot = _instance_rot_attr(inst_el)
    if rot:
        kwargs['rot'] = rot
    ET.SubElement(instances_el, 'instance', **kwargs)


# Eagle draws a module port as a 0.2" pin starting ON the block perimeter
# and pointing OUTWARD — the wire attachment point is the pin's far end,
# 5.08mm outside the perimeter (user-observed in real Eagle; KiCad attaches
# wires directly at the sheet perimeter instead, and IR keeps the KiCad
# geometry — canonical, source-faithful). The export-side fix lives here:
# every wire that ends exactly at a port's perimeter position is either
# TRIMMED so its end lands on the pin's attachment point (clean — no wire
# overlapping the pin), or, when trimming isn't safe (wire shorter than the
# pin, approach not along the pin axis, several wires meeting at the
# perimeter point), a short bridge wire perimeter->attachment is added.
_PORT_PIN_LEN_UM = 5080

# side -> outward unit normal, IR canvas space (Y-up).
_SIDE_NORMAL = {'left': (-1, 0), 'right': (1, 0), 'top': (0, 1), 'bottom': (0, -1)}


def _rotate_vec(x, y, deg, mirror):
    """Rotate+optionally-X-mirror a LOCAL vector (offset or direction, no
    translation) by a module instance's own rot/mirror — same single-axis
    convention IR's <instance mirror="0|1"> and Eagle's own 'MR90'-style rot
    already use (mirror negates local X first, then rotate). Shared by both
    the port's local offset (a point relative to the module's own center)
    and its outward normal (a pure direction) — both need identical
    treatment, since the trim/bridge logic in _emit_segment compares the
    (rotated) normal against a (rotated-offset-derived) wire direction.
    """
    if mirror:
        x = -x
    theta = math.radians(deg)
    c, s = math.cos(theta), math.sin(theta)
    return x * c - y * s, x * s + y * c


def _port_geometry(module_els, module_insts):
    """{(instance_name, port_name): (perim_x_um, perim_y_um, nx, ny)} for
    every port of every placed module instance — perimeter position from
    the module definition's side/coord (same derivation the KiCad importer
    used to place the connection point, ir_schema.md "Модуль") + outward
    normal, both rotated/mirrored by the instance's own placement (rot/
    mirror on <instance module=...>). KiCad sheets never rotate a module
    instance (always rot=0/mirror=0), so this used to be a no-op in
    practice; Eagle's own <moduleinst rot="R90"/mirror> is a normal,
    expected case once Eagle schematics are imported (a real Eagle project
    can place a rotated/mirrored module instance) and must round-trip
    correctly — see babel/eagle_project_parser.py, which relies on this
    same function (shared, not duplicated) to resolve <portref> connection
    points on the PARENT canvas when importing.
    """
    mod_by_name = {m.get('name'): m for m in module_els}
    out = {}
    for inst_el in module_insts:
        mod_el = mod_by_name.get(inst_el.get('module'))
        if mod_el is None:
            continue
        cx, cy = float(inst_el.get('x')), float(inst_el.get('y'))
        deg = round(float(inst_el.get('rot', '0')))
        mirror = inst_el.get('mirror') == '1'
        half_w, half_h = float(mod_el.get('dx')) / 2, float(mod_el.get('dy')) / 2
        for p in mod_el.findall('port'):
            side, coord = p.get('side'), float(p.get('coord', '0'))
            n = _SIDE_NORMAL.get(side)
            if n is None:
                continue
            nx, ny = n
            if side in ('left', 'right'):
                lx, ly = nx * half_w, coord
            else:
                lx, ly = coord, ny * half_h
            rx, ry = _rotate_vec(lx, ly, deg, mirror)
            rnx, rny = _rotate_vec(nx, ny, deg, mirror)
            out[(inst_el.get('name'), p.get('name'))] = (cx + rx, cy + ry, rnx, rny)
    return out


def _emit_segment(seg_out, seg_el, port_geom=None):
    """One Eagle <segment>'s content from one IR <segment> — shared by the
    top-level nets and module nets (same content model both places,
    eagle.dtd: (pinref | portref | wire | junction | label)*).

    `port_geom` (top-level only — modules contain no moduleinsts): see
    _port_geometry/_PORT_PIN_LEN_UM above for the wire-end adjustment made
    for every <portref> in this segment.
    """
    line_els = seg_el.findall('line')
    trims = {}     # id(line_el) -> {'x1'/'x2': new_x_um, 'y1'/'y2': new_y_um}
    bridges = []   # ((from_x, from_y), (to_x, to_y)) µm

    def _ends(line_el):
        return ((float(line_el.get('x1')), float(line_el.get('y1'))),
                (float(line_el.get('x2')), float(line_el.get('y2'))))

    if port_geom:
        for p in seg_el.findall('portref'):
            g = port_geom.get((p.get('part'), p.get('port')))
            if g is None:
                continue
            px, py, nx, ny = g
            target = (px + nx * _PORT_PIN_LEN_UM, py + ny * _PORT_PIN_LEN_UM)
            # A wire already ending exactly at the attachment point (target)
            # needs no adjustment at all — this is the normal case for IR
            # imported straight from a real Eagle source (eagle_project_
            # parser copies the source wire's own endpoint verbatim, and a
            # real Eagle-drawn wire to a module port already stops at the
            # pin's attachment point, never at the bare perimeter — see
            # decisions.md/progress.md "Импорт целых схем Eagle"). Checked
            # BEFORE the perimeter-hit trim/bridge logic below, or a
            # redundant bridge from perimeter->target gets added on top of
            # an already-correct wire (found on multichannel.sch's own
            # round-trip: CH2's "+12V" port wire duplicated this way).
            already_at_target = any(
                (abs(a[0] - target[0]) < 0.5 and abs(a[1] - target[1]) < 0.5) or
                (abs(b[0] - target[0]) < 0.5 and abs(b[1] - target[1]) < 0.5)
                for line_el in line_els for a, b in [_ends(line_el)])
            if already_at_target:
                continue
            # wires of THIS segment ending exactly at the perimeter point
            # (exact µm match — same integer-grid coordinates the importer's
            # own connectivity relied on)
            hits = []
            for line_el in line_els:
                (a, b) = _ends(line_el)
                if abs(a[0] - px) < 0.5 and abs(a[1] - py) < 0.5:
                    hits.append((line_el, 'x1', 'y1', b))
                elif abs(b[0] - px) < 0.5 and abs(b[1] - py) < 0.5:
                    hits.append((line_el, 'x2', 'y2', a))
            trimmed = False
            if len(hits) == 1:
                # Trim only when it can't break anything: a SINGLE wire ends
                # here (no T-joint whose other branches would detach), it
                # leaves along the pin's own outward axis, and it's longer
                # than the pin — then moving its end onto the attachment
                # point removes exactly the stretch that would overlap the
                # pin, changing no connectivity.
                line_el, xk, yk, other = hits[0]
                vx, vy = other[0] - px, other[1] - py
                along = vx * nx + vy * ny
                if abs(vx * ny - vy * nx) < 0.5 and along > _PORT_PIN_LEN_UM:
                    trims[id(line_el)] = {xk: target[0], yk: target[1]}
                    trimmed = True
            if not trimmed:
                bridges.append(((px, py), target))

    for p in seg_el.findall('pinref'):
        # IR encodes gate in the pin name itself for multi-gate
        # components ("A.OUT", same GATE.pin form <pin-mapping>
        # already uses — ir_schema.md "Размещение
        # многорежимного компонента", same split already done
        # for <connect> above) — Eagle wants them split into
        # separate gate=/pin= attributes instead. Single-gate
        # components never have a '.' in the pin name (IR pin
        # names are cleaned identifiers, KiCad/Eagle pin
        # designators don't contain '.'), so this split is safe
        # without checking component mode explicitly.
        raw_pin = p.get('pin')
        pin_gate, sep, pin_name = raw_pin.partition('.')
        if not sep:
            pin_gate, pin_name = 'G$1', raw_pin
        ET.SubElement(seg_out, 'pinref', part=p.get('part'),
                      gate=_eagle_name(pin_gate),
                      pin=_eagle_designator(_eagle_name(pin_name)))
    for p in seg_el.findall('portref'):
        # IR <portref part= port=> (parent-side attachment of a module
        # instance's port, ir_schema.md "Модуль") -> Eagle's own attribute
        # naming, <portref moduleinst= port=> (eagle.dtd).
        ET.SubElement(seg_out, 'portref',
                      moduleinst=_eagle_designator(p.get('part')),
                      port=_eagle_designator(p.get('port')))
    for line_el in line_els:
        width_mm = _tomm(line_el.get('width', '0'))
        coords = {k: float(line_el.get(k)) for k in ('x1', 'y1', 'x2', 'y2')}
        coords.update(trims.get(id(line_el), {}))
        ET.SubElement(seg_out, 'wire',
                      x1=_tomm(coords['x1']), y1=_tomm(coords['y1']),
                      x2=_tomm(coords['x2']), y2=_tomm(coords['y2']),
                      width=(width_mm if float(width_mm) else _WIRE_W),
                      layer=_LYR_NETS)
    for (fx, fy), (tx, ty) in bridges:
        ET.SubElement(seg_out, 'wire',
                      x1=_tomm(fx), y1=_tomm(fy), x2=_tomm(tx), y2=_tomm(ty),
                      width=_WIRE_W, layer=_LYR_NETS)
    for j in seg_el.findall('junction'):
        ET.SubElement(seg_out, 'junction',
                      x=_tomm(j.get('x')), y=_tomm(j.get('y')))
    for l in seg_el.findall('label'):
        ET.SubElement(seg_out, 'label',
                      x=_tomm(l.get('x')), y=_tomm(l.get('y')),
                      size=_tomm(l.get('size', '1270')),
                      rot=(_rot_attr(float(l.get('rot', '0'))) or 'R0'),
                      layer=_LYR_INFO, xref='yes')


def _emit_deco(plain_el, el):
    """One IR decorative canvas element (<line>/<arc>/<shape>/<text>,
    ir_schema.md "Декоративная геометрия схемы") -> Eagle <plain> geometry
    — shared by the top-level sheets (which pick plain_el by frame first,
    see _graphic_sheet in export_schematic) and module sheets (single
    implicit sheet, no frames)."""
    t = el.tag
    if t == 'note':
        # <note> (markdown, ir_schema.md) — Eagle has no equivalent: honest
        # degradation to a plain vector text carrying the raw markdown
        # (logged). Embedded images are lost outright (logged separately).
        md = el.text or ''
        import_log.log('note', f'({el.get("x")},{el.get("y")})',
                        'NOTE degraded to plain text in Eagle export')
        if '![' in md:
            import_log.log('note', f'({el.get("x")},{el.get("y")})',
                            'NOTE_IMAGE lost in Eagle export (no raster support)')
        te = ET.SubElement(plain_el, 'text')
        te.set('x', _tomm(el.get('x'))); te.set('y', _tomm(el.get('y')))
        te.set('size', '1.27')
        te.set('align', 'top-left')
        rot = _rot_attr(float(el.get('rot', 0)))
        if rot: te.set('rot', rot)
        te.set('layer', _LYR_GRAPHIC)
        te.set('font', 'vector')
        te.text = md
        return
    if t == 'text':
        te = ET.SubElement(plain_el, 'text')
        te.set('x', _tomm(el.get('x'))); te.set('y', _tomm(el.get('y')))
        te.set('size', _tomm(el.get('size', '1270')))
        rot = _rot_attr(float(el.get('rot', 0)))
        if rot: te.set('rot', rot)
        align = el.get('align', 'bottom-left')
        if align != 'bottom-left': te.set('align', align)
        if el.get('ratio'): te.set('ratio', el.get('ratio'))
        te.set('layer', _eagle_layer(el, fallback=_LYR_GRAPHIC))
        te.set('font', 'vector')
        te.text = el.text or ''
        return
    if t == 'line':
        width_mm = _tomm(el.get('width', '0'))
        ET.SubElement(plain_el, 'wire',
                      x1=_tomm(el.get('x1')), y1=_tomm(el.get('y1')),
                      x2=_tomm(el.get('x2')), y2=_tomm(el.get('y2')),
                      width=(width_mm if float(width_mm) else _WIRE_W),
                      layer=_eagle_layer(el, fallback=_LYR_GRAPHIC))
    elif t == 'arc':
        _emit_arc(plain_el, el, _eagle_layer(el, fallback=_LYR_GRAPHIC))
    elif t == 'shape':
        rn = int(el.get('roundness', 0))
        sx, sy = _tomm(el.get('x')), _tomm(el.get('y'))
        w2 = float(el.get('w', '0')) / 2000
        h2 = float(el.get('h', '0')) / 2000
        outline_mm = _tomm(el.get('outline', '0'))
        shape_layer = _eagle_layer(el, fallback=_LYR_GRAPHIC)
        if rn == 100:
            c = ET.SubElement(plain_el, 'circle')
            c.set('x', sx); c.set('y', sy)
            c.set('radius', fmt(w2))
            c.set('width', outline_mm); c.set('layer', shape_layer)
        elif float(el.get('outline', '0')) == 0:
            r_el = ET.SubElement(plain_el, 'rectangle')
            r_el.set('x1', fmt(float(sx) - w2)); r_el.set('y1', fmt(float(sy) - h2))
            r_el.set('x2', fmt(float(sx) + w2)); r_el.set('y2', fmt(float(sy) + h2))
            r_el.set('layer', shape_layer)
        else:
            corners = [(-w2, -h2), (w2, -h2), (w2, h2), (-w2, h2)]
            for (ax, ay), (bx, by) in zip(corners, corners[1:] + corners[:1]):
                w_el = ET.SubElement(plain_el, 'wire')
                w_el.set('x1', fmt(float(sx) + ax)); w_el.set('y1', fmt(float(sy) + ay))
                w_el.set('x2', fmt(float(sx) + bx)); w_el.set('y2', fmt(float(sy) + by))
                w_el.set('width', outline_mm); w_el.set('layer', shape_layer)


def _emit_frame(plain_el, bbox, inst_el, comp_el, pool):
    """One frame-bearing IR instance -> Eagle <frame> in <plain> + resolved
    title-block texts — shared by the top-level sheets (which ALSO use the
    frame bboxes for sheet-splitting, see export_schematic) and module
    sheets (single sheet, frame is presentation only there)."""
    x1, x2, y1, y2 = bbox
    ET.SubElement(plain_el, 'frame',
                  x1=_tomm(x1), y1=_tomm(y1), x2=_tomm(x2), y2=_tomm(y2),
                  columns='4', rows='4', layer=_LYR_SYMBOLS)
    attrs = _resolved_attrs(comp_el, inst_el)
    sym_el = pool[component_gates(comp_el)[0][1]]
    ix, iy = float(inst_el.get('x')), float(inst_el.get('y'))
    for t in sym_el.findall('text'):
        placeholder = (t.text or '')
        if not placeholder.startswith('>'):
            continue
        val = attrs.get(placeholder[1:].lower())
        if not val:
            continue
        tx = ET.SubElement(plain_el, 'text',
                            x=_tomm(ix + float(t.get('x'))),
                            y=_tomm(iy + float(t.get('y'))),
                            size=_tomm(t.get('size')), layer=_LYR_INFO)
        tx.text = val


def _export_module(modules_el, mod_el, lib_name, comp_by_name, dev_name_by_fp, pool, class_num):
    """One IR <module> -> one Eagle <module> (ground truth
    outputs/multigate.sch + eagle.dtd): its own <ports>, its own <parts>,
    and a full single-<sheet> structure mirroring the top level. No frames
    inside a module (a design block is not a printable page), so the
    content is emitted unsplit, coordinates straight through.

    Port names go through _eagle_designator the same way net names do —
    the port <-> inner-net link is by NAME (ir_schema.md "Модуль"), so both
    sides must be transformed identically or the link silently breaks.
    """
    m = ET.SubElement(modules_el, 'module', name=_eagle_name(mod_el.get('name')),
                      prefix='', dx=_tomm(mod_el.get('dx')), dy=_tomm(mod_el.get('dy')))
    ports_el = ET.SubElement(m, 'ports')
    for p in mod_el.findall('port'):
        ET.SubElement(ports_el, 'port', name=_eagle_designator(p.get('name')),
                      side=p.get('side'), coord=_tomm(p.get('coord')),
                      direction=p.get('direction', 'io'))
    ET.SubElement(m, 'variantdefs')

    parts_el = ET.SubElement(m, 'parts')
    frame_insts, insts = [], []
    for i in mod_el.findall('instance'):
        if i.get('module'):
            continue
        comp_el = comp_by_name.get(i.get('component'))
        if comp_el is not None and _is_frame_component(comp_el, pool):
            # Same top-level treatment: a frame instance becomes <frame> in
            # <plain>, never a <part>/<instance> pair (a module has exactly
            # one implicit sheet, so no sheet-splitting role here — the
            # frame is presentation only).
            frame_insts.append(i)
        else:
            insts.append(i)
    seen_parts = set()
    for inst_el in insts:
        designator = inst_el.get('name')
        if designator in seen_parts:
            continue
        seen_parts.add(designator)
        comp_el = comp_by_name.get(inst_el.get('component'))
        if comp_el is None:
            continue
        _emit_part(parts_el, inst_el, comp_el, lib_name, dev_name_by_fp)

    sheets_el = ET.SubElement(m, 'sheets')
    sheet_el = ET.SubElement(sheets_el, 'sheet')
    plain_el = ET.SubElement(sheet_el, 'plain')
    instances_el = ET.SubElement(sheet_el, 'instances')
    ET.SubElement(sheet_el, 'busses')
    nets_el = ET.SubElement(sheet_el, 'nets')

    for inst_el in frame_insts:
        comp_el = comp_by_name[inst_el.get('component')]
        _emit_frame(plain_el, _frame_bbox(inst_el, comp_el, pool), inst_el, comp_el, pool)
    for inst_el in insts:
        _emit_instance(instances_el, inst_el)
    for el in mod_el:
        if el.tag in ('line', 'arc', 'shape'):
            _emit_deco(plain_el, el)
    for net_el in mod_el.findall('net'):
        net_out = ET.SubElement(nets_el, 'net',
                                 name=_eagle_designator(net_el.get('name')),
                                 **{'class': class_num.get(net_el.get('class'), '0')})
        for seg_el in net_el.findall('segment'):
            seg_out = ET.SubElement(net_out, 'segment')
            _emit_segment(seg_out, seg_el)


def export_schematic(ir_path, output_path=None):
    """IR <project> (pool + <schematic> instances/nets) -> a full Eagle .sch
    (<libraries> embedded fresh from the pool, <parts>, <sheets>).

    Frame-bearing components (ir_schema.md "Frame") split the canvas into
    one Eagle <sheet> per frame — decisions.md "Рамка — маркер для экспорта
    в печать": every other instance must fall inside EXACTLY one frame's
    bbox, or the whole export hard-fails naming the stray part(s), same
    discipline as every other "can't safely guess" case in this project. A
    project with zero frames exports as a single sheet, unsplit.
    """
    tree = ET.parse(ir_path)
    ir_root = tree.getroot()
    lib_name = ir_root.get('name', Path(ir_path).stem)
    schem_el = ir_root.find('schematic')
    if schem_el is None:
        raise ValueError(f'{ir_path}: no <schematic> section — nothing to export as a schematic '
                          f'(use export() for a library-only .lbr instead).')

    pool = symbol_pool(ir_root)
    comp_by_name = {c.get('name'): c for c in ir_root.findall('component')}

    module_els = ir_root.findall('module')

    frame_insts, part_insts, module_insts = [], [], []
    for inst_el in schem_el.findall('instance'):
        if inst_el.get('module'):
            # Module instance (ir_schema.md "Модуль") -> Eagle <moduleinst>,
            # not a <part>/<instance> pair — separate flow below.
            module_insts.append(inst_el)
            continue
        comp_el = comp_by_name.get(inst_el.get('component'))
        if comp_el is None:
            continue
        (frame_insts if _is_frame_component(comp_el, pool) else part_insts).append(inst_el)

    frames = sorted(
        ((_frame_bbox(inst_el, comp_by_name[inst_el.get('component')], pool), inst_el)
         for inst_el in frame_insts),
        key=lambda f: -f[0][3])   # top-to-bottom, matches the vertical-column import tiling

    if frames:
        part_sheet = {}
        modinst_sheet = {}
        strays = []
        # Module instances take part in the frame split like any other
        # placed object (their representative point = the block's center,
        # which IS the instance's own x/y — ir_schema.md "Модуль").
        for inst_el, target in ([(i, part_sheet) for i in part_insts]
                                 + [(i, modinst_sheet) for i in module_insts]):
            x, y = float(inst_el.get('x')), float(inst_el.get('y'))
            hits = [i for i, (bbox, _) in enumerate(frames)
                    if bbox[0] <= x <= bbox[1] and bbox[2] <= y <= bbox[3]]
            if len(hits) != 1:
                strays.append(inst_el.get('name'))
            else:
                target[inst_el.get('name')] = hits[0]
        if strays:
            raise ValueError(
                f'{", ".join(sorted(strays))}: not inside exactly one frame — Eagle needs every '
                f'component placed on a printable sheet/frame to know which page it belongs to. '
                f'Move it inside a frame (or remove overlapping frames) and re-export.')
        n_sheets = len(frames)
    else:
        part_sheet = {inst_el.get('name'): 0 for inst_el in part_insts}
        modinst_sheet = {inst_el.get('name'): 0 for inst_el in module_insts}
        n_sheets = 1

    # Coordinates are written straight from IR, unshifted — per the user,
    # not this exporter's job to hunt for a "nice" quadrant (each Eagle
    # <sheet> is already its own self-contained unit in the file, doesn't
    # need to avoid overlapping any other sheet's geometry the way tiled
    # pages on one shared IR <schematic> canvas do). Negative coordinates
    # are valid Eagle geometry — the user repositions the visible area in
    # Eagle itself if they want a specific quadrant, cheaper and less
    # error-prone than us re-deriving a shift here (see decisions.md
    # "Eagle-экспорт: без sheet_shift").
    def _sx(sheet_idx, x_um):
        return _tomm(x_um)

    def _sy(sheet_idx, y_um):
        return _tomm(y_um)

    eagle = ET.Element('eagle', version='9.6.2')
    drawing = ET.SubElement(eagle, 'drawing')
    settings = ET.SubElement(drawing, 'settings')
    ET.SubElement(settings, 'setting').set('alwaysvectorfont', 'no')
    ET.SubElement(settings, 'setting').set('verticaltext', 'up')
    grid = ET.SubElement(drawing, 'grid')
    for k, v in [('distance', '0.1'), ('unitdist', 'inch'), ('unit', 'inch'),
                 ('style', 'lines'), ('multiple', '1'), ('display', 'no'),
                 ('altdistance', '0.01'), ('altunitdist', 'inch'), ('altunit', 'inch')]:
        grid.set(k, v)
    drawing.append(ET.parse(_LAYERS_FILE).getroot())

    schematic_el = ET.SubElement(drawing, 'schematic')
    libraries_el = ET.SubElement(schematic_el, 'libraries')

    # Components placed only inside a module still need their deviceset/
    # package/symbol in <libraries> — a module's <parts> references them by
    # library/deviceset name exactly like top-level parts do. Frame-bearing
    # components are excluded the same way top-level frame_insts are (they
    # become <frame> in <plain>, never a library part).
    module_part_insts = [i for m in module_els for i in m.findall('instance')
                          if not i.get('module') and i.get('component') in comp_by_name
                          and not _is_frame_component(comp_by_name[i.get('component')], pool)]
    used_components = {comp_by_name[i.get('component')]
                        for i in part_insts + module_part_insts}

    # Group components by their own ORIGIN library nickname (ir_schema.md
    # "Компонент `<component>`" — `library=`), one real Eagle <library> per
    # nickname, instead of collapsing every component into a single library
    # named after the output file. Per the user: unlike the KiCad path
    # (which has no meaningful per-component library concept to preserve,
    # so falls back to `lib_name`), Eagle always carries a real, actually-
    # used nickname on every component it produces — losing that grouping
    # was pure convenience, not a format necessity, and it's cheap to keep
    # since IR already tracks it per-component.
    components_by_lib = {}
    for comp_el in used_components:
        components_by_lib.setdefault(comp_el.get('library') or lib_name, []).append(comp_el)

    sup_only = _packageless_symbols(used_components)
    for this_lib_name, lib_components in components_by_lib.items():
        library_el = ET.SubElement(libraries_el, 'library', name=_eagle_name(this_lib_name))
        packages_el = ET.SubElement(library_el, 'packages')
        symbols_el = ET.SubElement(library_el, 'symbols')
        devicesets_el = ET.SubElement(library_el, 'devicesets')

        packages_seen = set()
        for comp_el in lib_components:
            devicesets_el.append(export_deviceset(comp_el))
            for fp_el in comp_el.findall('footprint'):
                pkg_name = fp_el.get('name')
                if pkg_name not in packages_seen:
                    packages_seen.add(pkg_name)
                    packages_el.append(export_package(fp_el, pkg_name))
        # A symbol shared by components from DIFFERENT libraries (none seen
        # in any real fixture so far, but not forbidden by the format) must
        # be written into EACH such library — Eagle resolves a deviceset's
        # <gate symbol=...> only within its OWN library, a cross-library
        # reference isn't valid Eagle. Scoping used_symbol_names to just
        # this library's own components (not the global set) gives exactly
        # that duplication when/if it's ever needed.
        used_symbol_names = {sname for comp_el in lib_components
                              for _, sname in component_gates(comp_el)}
        for sname in used_symbol_names:
            symbols_el.append(export_symbol(pool[sname], sname,
                                            coerce_sup=sname in sup_only))

    ET.SubElement(schematic_el, 'attributes')
    ET.SubElement(schematic_el, 'variantdefs')
    classes_el = ET.SubElement(schematic_el, 'classes')
    ET.SubElement(classes_el, 'class', number='0', name='default', width='0', drill='0')
    # IR <classes> (ir_schema.md "Net class") -> numbered Eagle classes.
    # Eagle keys nets to classes by NUMBER; IR by name — the mapping is
    # assigned here (enumeration order) and consumed by every net emission
    # below (top-level and module nets alike).
    class_num = {}
    ir_classes = ir_root.find('classes')
    if ir_classes is not None:
        for i, cl in enumerate(ir_classes.findall('class'), start=1):
            num = str(i)
            cl_el = ET.SubElement(classes_el, 'class', number=num,
                                   name=_eagle_name(cl.get('name')),
                                   width=_tomm(cl.get('width', '0')),
                                   drill=_tomm(cl.get('drill', '0')))
            if cl.get('clearance'):
                ET.SubElement(cl_el, 'clearance', **{'class': num,
                              'value': _tomm(cl.get('clearance'))})
            class_num[cl.get('name')] = num

    # <modules> sits between <classes> and <parts> (ground truth
    # outputs/multigate.sch) — the element is created here to hold that
    # position, filled below once dev_name_by_fp exists (module <parts>
    # need it). Omitted entirely for module-less projects so their output
    # stays byte-identical to before.
    modules_el = ET.SubElement(schematic_el, 'modules') if module_els else None

    parts_el = ET.SubElement(schematic_el, 'parts')
    # {component_el: {footprint_name: device_name}} — _device_names()
    # returns names in the same order as comp_el.findall('footprint'), zip
    # them for an O(1) lookup by the instance's own recorded footprint.
    # Keyed by BOTH the footprint name and its variant: one package can
    # back several devices (name key collides and silently keeps the last —
    # luminoso CON-2P, ERC caught the wrong device), so instances of such
    # components record the VARIANT, which is unique by construction.
    dev_name_by_fp = {}
    for comp_el in used_components:
        m = {}
        for fp, dev in zip(comp_el.findall('footprint'), _device_names(comp_el)):
            m[fp.get('name')] = dev
            if fp.get('variant'):
                m[fp.get('variant')] = dev
        dev_name_by_fp[comp_el] = m
    # One Eagle <part> per PHYSICAL component (designator), not per IR
    # <instance> — a multi-gate component has several <instance>s sharing
    # one designator (ir_schema.md "Размещение многорежимного компонента"),
    # but Eagle's <part> is the deviceset-level placement, referenced by
    # NAME alone from every <instance gate="..."> pointing at it; a second
    # <part> with the same name is a redefinition, real Eagle rejects it
    # outright ("redefinition of name 'U1' in tag <part>"). First instance
    # encountered per designator wins for device/value resolution — same
    # value on every gate's instance in practice (see decisions.md "KiCad:
    # multi-gate компоненты на канвасе").
    seen_parts = set()
    for inst_el in part_insts:
        designator = inst_el.get('name')
        if designator in seen_parts:
            continue
        seen_parts.add(designator)
        _emit_part(parts_el, inst_el, comp_by_name[inst_el.get('component')],
                   lib_name, dev_name_by_fp)

    # Fill the <modules> element reserved above (needs dev_name_by_fp).
    for mod_el in module_els:
        _export_module(modules_el, mod_el, lib_name, comp_by_name, dev_name_by_fp, pool, class_num)

    sheets_el = ET.SubElement(schematic_el, 'sheets')
    sheet_els = [ET.SubElement(sheets_el, 'sheet') for _ in range(n_sheets)]
    plain_els = [ET.SubElement(s, 'plain') for s in sheet_els]
    # <moduleinsts> sits between <plain> and <instances> (ground truth
    # outputs/multigate.sch); omitted for module-less projects, same
    # byte-identity rationale as <modules> above.
    moduleinsts_els = ([ET.SubElement(s, 'moduleinsts') for s in sheet_els]
                        if module_insts else None)
    instances_els = [ET.SubElement(s, 'instances') for s in sheet_els]
    for s in sheet_els:
        ET.SubElement(s, 'busses')
    nets_els = [ET.SubElement(s, 'nets') for s in sheet_els]

    for inst_el in module_insts:
        # eagle.dtd <moduleinst>: rot only 0/90/180/270 (KiCad sheets can't
        # rotate at all, so IR gives 0 today — the attribute is still
        # emitted via the shared helper for when another source can).
        kwargs = {'name': _eagle_designator(inst_el.get('name')),
                  'module': _eagle_name(inst_el.get('module')),
                  'x': _tomm(inst_el.get('x')), 'y': _tomm(inst_el.get('y'))}
        rot = _instance_rot_attr(inst_el)
        if rot:
            kwargs['rot'] = rot
        # `offset` (eagle.dtd: designator-disambiguation between different
        # module instances, ir_schema.md "Модуль") — was silently dropped
        # here, never read off the IR <instance> at all. Found on
        # testData/maximus.sch's own round-trip: all 8 "TMETER" module
        # instances carry a real offset (100/200/.../800) in the source,
        # lost entirely on re-export (would have caused real Eagle to
        # collide every module's internal designators across all 8
        # instances instead of disambiguating them).
        offset = inst_el.get('offset')
        if offset and offset != '0':
            kwargs['offset'] = offset
        sheet_idx = modinst_sheet[inst_el.get('name')]
        ET.SubElement(moduleinsts_els[sheet_idx], 'moduleinst', **kwargs)

    for sheet_idx, (bbox, inst_el) in enumerate(frames):
        _emit_frame(plain_els[sheet_idx], bbox,
                    inst_el, comp_by_name[inst_el.get('component')], pool)

    for inst_el in part_insts:
        _emit_instance(instances_els[part_sheet[inst_el.get('name')]], inst_el)

    # Decorative canvas geometry (ir_schema.md "Декоративная геометрия
    # схемы") — direct <line>/<shape>/<arc> children of <schematic>, not
    # tied to any instance/net, so which sheet each one belongs to is
    # resolved the same way part_insts above are: a representative point
    # (bbox center, since unlike a part's single (x,y) this geometry has
    # real extent) tested against each frame's bbox. Same strict "exactly
    # one frame" discipline as part placement (decisions.md "Stray-
    # валидация" applied identically on the KiCad import side already
    # guarantees every piece fits inside its source page, so a genuine
    # cross-frame straddle here would mean the geometry legitimately spans
    # two pages — not supported, hard-fails naming it, same as a stray part).
    def _graphic_sheet(cx, cy, label):
        if not frames:
            return 0
        hits = [i for i, (bbox, _) in enumerate(frames)
                if bbox[0] <= cx <= bbox[1] and bbox[2] <= cy <= bbox[3]]
        if len(hits) != 1:
            raise ValueError(
                f'{label}: not inside exactly one frame — Eagle needs every decorative '
                f'canvas object placed on a printable sheet/frame to know which page it '
                f'belongs to. Move it inside a frame (or remove overlapping frames) and '
                f're-export.')
        return hits[0]

    for el in schem_el:
        t = el.tag
        if t == 'line':
            x1, y1 = float(el.get('x1')), float(el.get('y1'))
            x2, y2 = float(el.get('x2')), float(el.get('y2'))
            sheet_idx = _graphic_sheet((x1 + x2) / 2, (y1 + y2) / 2, f'line ({x1},{y1})-({x2},{y2})')
        elif t == 'arc':
            cx = (float(el.get('x1')) + float(el.get('x2'))) / 2
            cy = (float(el.get('y1')) + float(el.get('y2'))) / 2
            sheet_idx = _graphic_sheet(cx, cy, f'arc ({cx},{cy})')
        elif t == 'shape':
            x, y = float(el.get('x')), float(el.get('y'))
            sheet_idx = _graphic_sheet(x, y, f'shape ({x},{y})')
        elif t in ('text', 'note'):
            x, y = float(el.get('x')), float(el.get('y'))
            sheet_idx = _graphic_sheet(x, y, f'{t} ({x},{y})')
        else:
            continue
        _emit_deco(plain_els[sheet_idx], el)

    port_geom = _port_geometry(module_els, module_insts)

    for net_el in schem_el.findall('net'):
        segs_by_sheet = {}
        for seg_el in net_el.findall('segment'):
            sheet_candidates = {part_sheet[p.get('part')] for p in seg_el.findall('pinref')
                                 if p.get('part') in part_sheet}
            # A segment attached to a module instance's port lives on that
            # moduleinst's sheet — same rule as pinrefs.
            sheet_candidates |= {modinst_sheet[p.get('part')] for p in seg_el.findall('portref')
                                  if p.get('part') in modinst_sheet}
            if len(sheet_candidates) > 1:
                raise ValueError(
                    f'net "{net_el.get("name")}": one segment touches parts on different '
                    f'sheets — not supported, a net segment must stay on one printable page.')
            sheet_idx = next(iter(sheet_candidates), 0)
            segs_by_sheet.setdefault(sheet_idx, []).append(seg_el)

        for sheet_idx, segs in segs_by_sheet.items():
            net_out = ET.SubElement(nets_els[sheet_idx], 'net',
                                     name=_eagle_designator(net_el.get('name')),
                                     **{'class': class_num.get(net_el.get('class'), '0')})
            for seg_el in segs:
                seg_out = ET.SubElement(net_out, 'segment')
                _emit_segment(seg_out, seg_el, port_geom)

    xml_str = minidom.parseString(ET.tostring(eagle, encoding='unicode')) \
                     .toprettyxml(indent='  ')
    lines = xml_str.splitlines()
    clean = '\n'.join(l for l in lines if l.strip())
    result = ('<?xml version="1.0" encoding="utf-8"?>\n'
              '<!DOCTYPE eagle SYSTEM "eagle.dtd">\n' +
              '\n'.join(clean.splitlines()[1:]))

    if output_path:
        Path(output_path).write_text(result, encoding='utf-8')
        print(f'Written: {output_path}  ({n_sheets} sheet(s), {len(part_insts)} part(s), '
              f'{len(module_els)} module(s), {len(module_insts)} moduleinst(s))')
        import_log.write(output_path)
    return result


if __name__ == '__main__':
    import sys
    ir  = sys.argv[1] if len(sys.argv) > 1 else 'testData/rc.swlib'
    out = sys.argv[2] if len(sys.argv) > 2 else 'testData/rc.roundtrip.lbr'
    # Dispatch on the OUTPUT extension, not the IR's own content — an IR with
    # a <schematic> section can still legitimately be asked for a pool-only
    # .lbr (e.g. just the parts library, no sheets/nets at all).
    if Path(out).suffix.lower() == '.sch':
        export_schematic(ir, out)
    else:
        export(ir, out)
