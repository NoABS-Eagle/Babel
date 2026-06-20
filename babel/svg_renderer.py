"""SVG renderer for IR library symbols and footprints."""
import math
import xml.etree.ElementTree as ET
from pathlib import Path
from babel.ir_util import symbol_pool, component_gates

_MARGIN  = 3.0   # mm padding around content (renderer works in mm internally)
_MAX_DIM = 500   # max SVG dimension in pixels


def _v(um, default=0):
    """Read IR µm value → mm float for renderer (which works in mm space)."""
    try:
        return float(um) / 1000
    except (TypeError, ValueError):
        return float(default)

# Eagle standard color indices → hex RGB (dark board/library editor theme).
# Matches the colors defined in eagle_layers.xml.
_E = {
    1:  '#2266CC',  # blue   — Bottom
    2:  '#22AA44',  # green  — Pads, Vias, Pins
    3:  '#00AAAA',  # teal
    4:  '#CC2244',  # red    — Top, Symbols, tKeepout
    5:  '#AA22AA',  # purple
    6:  '#AAAA00',  # gold   — Guide
    7:  '#FFFF00',  # yellow — tPlace, tNames, tValues
    9:  '#2244FF',
    10: '#44FF44',
    11: '#00FFFF',
    12: '#FF4444',
    13: '#FF44FF',
    14: '#FFFF44',
    15: '#FFFFFF',  # white  — Dimension
}

_BG = '#141414'   # Eagle dark editor background

# IR layer → Eagle display color (from eagle_layers.xml color attributes)
_LAYER_COLORS = {
    'top':           _E[4],   # layer 1  color=4  red
    'bottom':        _E[1],   # layer 16 color=1  blue
    'cream_top':     _E[7],   # layer 31 color=7  yellow
    'cream_bottom':  _E[7],   # layer 32 color=7  yellow
    'silk_top':      _E[7],   # layer 21 color=7  yellow
    'silk_bottom':   _E[1],   # layer 22 color=1  blue
    'labels':        _E[7],   # layer 25 color=7  yellow
    'fab':           _E[7],   # layer 51 color=7  yellow (dimmed below)
    'courtyard':     _E[4],   # layer 39 color=4  red, dashed
}

# Layers rendered with dashed stroke (Eagle fill patterns 10/11 = hatch)
_LAYER_DASH = {'courtyard': '3,2'}

# Fab is same color as silkscreen but dimmer so it doesn't compete
_FAB_OPACITY = '0.45'

_LAYER_Z = ['courtyard', 'fab', 'silk_bottom', 'bottom', 'top', 'silk_top',
            'labels', 'cream_top', 'cream_bottom']

# Symbol layers
_SYM_BODY  = _E[4]   # layer 94 Symbols color=4 red
_SYM_NAMES = _E[7]   # layer 95 Names   color=7 yellow
_SYM_INFO  = '#888800'  # layer 97 Info  — dimmer yellow

# Pin direction colors (informational, not in Eagle — kept as a Babel feature)
_PIN_COLORS = {
    'in':  '#44AAFF',
    'out': '#FF6644',
    'io':  _E[2],
    'pwr': '#FF4444',
    'pas': '#888888',
}


# ---------------------------------------------------------------------------
# Coordinate helpers
# ---------------------------------------------------------------------------

def _tr(x, y, x_min, y_max, scale):
    """IR coords (mm, Y-up) → SVG pixel coords (Y-down)."""
    return (x - x_min + _MARGIN) * scale, (y_max - y + _MARGIN) * scale


def _bounds(elems_iter):
    """Return (x_min, x_max, y_min, y_max) in mm for an iterable of IR elements."""
    pts = []
    for el in elems_iter:
        t = el.tag
        if t in ('line',):
            pts += [(_v(el.get('x1')), _v(el.get('y1'))),
                    (_v(el.get('x2')), _v(el.get('y2')))]
        elif t == 'arc':
            cx, cy, r = _v(el.get('cx')), _v(el.get('cy')), _v(el.get('r'))
            pts += [(cx - r, cy - r), (cx + r, cy + r)]
        elif t == 'shape':
            x, y = _v(el.get('x')), _v(el.get('y'))
            w2, h2 = _v(el.get('w')) / 2, _v(el.get('h')) / 2
            pts += [(x - w2, y - h2), (x + w2, y + h2)]
        elif t == 'smd':
            x, y = _v(el.get('x')), _v(el.get('y'))
            w2, h2 = _v(el.get('width')) / 2, _v(el.get('height')) / 2
            d = math.hypot(w2, h2)
            pts += [(x - d, y - d), (x + d, y + d)]
        elif t == 'pad':
            x, y = _v(el.get('x')), _v(el.get('y'))
            r = _v(el.get('drill', '1000')) * 1.2
            pts += [(x - r, y - r), (x + r, y + r)]
        elif t == 'hole':
            x, y = _v(el.get('x')), _v(el.get('y'))
            r = _v(el.get('drill')) / 2
            pts += [(x - r, y - r), (x + r, y + r)]
        elif t == 'pin':
            px, py = _v(el.get('x')), _v(el.get('y'))
            rot = float(el.get('rot', 0))
            length = _v(el.get('length', '2540'))
            rad = math.radians(rot)
            pts += [(px, py), (px + length * math.cos(rad), py + length * math.sin(rad))]
        elif t == 'polygon':
            for v in el.findall('vertex'):
                pts.append((_v(v.get('x')), _v(v.get('y'))))
        elif t == 'text':
            pts.append((_v(el.get('x')), _v(el.get('y'))))
    if not pts:
        return 0, 1, 0, 1
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), max(xs), min(ys), max(ys)


# ---------------------------------------------------------------------------
# SVG element builders
# ---------------------------------------------------------------------------

def _line(x1s, y1s, x2s, y2s, color, lw, dash=''):
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ''
    return (f'<line x1="{x1s:.1f}" y1="{y1s:.1f}" x2="{x2s:.1f}" y2="{y2s:.1f}" '
            f'stroke="{color}" stroke-width="{lw:.1f}" stroke-linecap="round"{dash_attr}/>')


def _arc(cx_ir, cy_ir, r_ir, start_deg, sweep_deg, color, lw, x_min, y_max, scale, dash=''):
    sr = math.radians(start_deg)
    er = sr + math.radians(sweep_deg)
    x1s, y1s = _tr(cx_ir + r_ir * math.cos(sr), cy_ir + r_ir * math.sin(sr),
                   x_min, y_max, scale)
    x2s, y2s = _tr(cx_ir + r_ir * math.cos(er), cy_ir + r_ir * math.sin(er),
                   x_min, y_max, scale)
    rp = r_ir * scale
    large = 1 if abs(sweep_deg) > 180 else 0
    # CCW in IR (positive sweep) → CW in SVG (Y-flipped) → sweep-flag=1
    cw = 1 if sweep_deg > 0 else 0
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ''
    return (f'<path d="M{x1s:.1f},{y1s:.1f} A{rp:.1f},{rp:.1f} 0 {large} {cw} {x2s:.1f},{y2s:.1f}" '
            f'stroke="{color}" stroke-width="{lw:.1f}" fill="none" stroke-linecap="round"{dash_attr}/>')


def _shape(x_ir, y_ir, w_ir, h_ir, roundness, outline_ir, color, x_min, y_max, scale):
    xs, ys = _tr(x_ir, y_ir, x_min, y_max, scale)
    wp, hp = w_ir * scale, h_ir * scale
    filled = (outline_ir == 0)
    fc = color if filled else 'none'
    sw = max(outline_ir * scale, 0.5) if not filled else 0
    if roundness == 100:
        return (f'<circle cx="{xs:.1f}" cy="{ys:.1f}" r="{wp/2:.1f}" '
                f'stroke="{color}" stroke-width="{sw:.1f}" fill="{fc}"/>')
    rx = (roundness / 100) * min(wp, hp) / 2
    return (f'<rect x="{xs-wp/2:.1f}" y="{ys-hp/2:.1f}" width="{wp:.1f}" height="{hp:.1f}" '
            f'rx="{rx:.1f}" stroke="{color}" stroke-width="{sw:.1f}" fill="{fc}"/>')


def _align_to_svg(align):
    """Eagle align → (text-anchor, dominant-baseline)."""
    a = align.lower()
    if a == 'center':
        return 'middle', 'central'
    parts = a.split('-')
    ta = {'left': 'start', 'center': 'middle', 'right': 'end'}.get(parts[-1], 'start')
    db = {'bottom': 'auto', 'center': 'central', 'top': 'hanging'}.get(parts[0], 'auto')
    return ta, db


def _text(x_ir, y_ir, txt, size_mm, rot_deg, align, color, x_min, y_max, scale):
    xs, ys = _tr(x_ir, y_ir, x_min, y_max, scale)
    sp = max(size_mm * scale, 6)
    ta, db = _align_to_svg(align)
    # Eagle CCW rotation in Y-up → negate for SVG (Y-down, CW-positive)
    r_attr = f' transform="rotate({-rot_deg:.0f},{xs:.1f},{ys:.1f})"' if rot_deg else ''
    return (f'<text x="{xs:.1f}" y="{ys:.1f}" font-size="{sp:.0f}" font-family="monospace" '
            f'fill="{color}" text-anchor="{ta}" dominant-baseline="{db}"{r_attr}>'
            f'{txt}</text>')


def _smd(x_ir, y_ir, w_ir, h_ir, roundness, rot_deg, color, x_min, y_max, scale):
    xs, ys = _tr(x_ir, y_ir, x_min, y_max, scale)
    wp, hp = w_ir * scale, h_ir * scale
    rx = (int(roundness) / 100) * min(wp, hp) / 2
    rot_str = f' transform="rotate({-rot_deg:.3g},{xs:.1f},{ys:.1f})"' if rot_deg else ''
    return (f'<rect x="{xs-wp/2:.1f}" y="{ys-hp/2:.1f}" width="{wp:.1f}" height="{hp:.1f}" '
            f'rx="{rx:.1f}" fill="{color}" stroke="none" opacity="0.85"{rot_str}/>')


def _pad(x_ir, y_ir, drill, shape, color, x_min, y_max, scale):
    xs, ys = _tr(x_ir, y_ir, x_min, y_max, scale)
    dp = float(drill) * scale / 2  # drill radius in px
    outer = dp * 1.7               # annular ring outer radius
    if shape == 'square':
        copper = (f'<rect x="{xs-outer:.1f}" y="{ys-outer:.1f}" '
                  f'width="{outer*2:.1f}" height="{outer*2:.1f}" fill="{color}" stroke="none"/>')
    else:
        copper = f'<circle cx="{xs:.1f}" cy="{ys:.1f}" r="{outer:.1f}" fill="{color}" stroke="none"/>'
    hole = f'<circle cx="{xs:.1f}" cy="{ys:.1f}" r="{dp:.1f}" fill="#000000"/>'
    return copper + '\n' + hole


def _hole(x_ir, y_ir, drill, x_min, y_max, scale):
    xs, ys = _tr(x_ir, y_ir, x_min, y_max, scale)
    rp = float(drill) * scale / 2
    return f'<circle cx="{xs:.1f}" cy="{ys:.1f}" r="{rp:.1f}" fill="#2a2a2a" stroke="#444" stroke-width="0.5"/>'


def _pad_label(x_ir, y_ir, name, x_min, y_max, scale):
    xs, ys = _tr(x_ir, y_ir, x_min, y_max, scale)
    sp = max(scale * 0.5, 5)
    return (f'<text x="{xs:.1f}" y="{ys:.1f}" font-size="{sp:.0f}" font-family="monospace" '
            f'fill="white" text-anchor="middle" dominant-baseline="central">{name}</text>')


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render_symbol(comp_el, root, scale=10):
    """Render a component's symbol to an SVG string.

    For multi-gate components, renders the first gate only (representative preview).
    """
    pool = symbol_pool(root)
    gates = component_gates(comp_el)
    sym_el = None
    for _gname, sname in gates:
        sym_el = pool.get(sname)
        if sym_el is not None:
            break
    if sym_el is None:
        return '<svg xmlns="http://www.w3.org/2000/svg"><text fill="red">no symbol</text></svg>'

    all_els = list(sym_el)
    x_min, x_max, y_min, y_max = _bounds(all_els)
    w_mm = max(x_max - x_min, 1.0)
    h_mm = max(y_max - y_min, 1.0)
    scale = min(scale, _MAX_DIM / (w_mm + 2 * _MARGIN), _MAX_DIM / (h_mm + 2 * _MARGIN))

    W = int((w_mm + 2 * _MARGIN) * scale)
    H = int((h_mm + 2 * _MARGIN) * scale)
    pin_lw = max(scale * 0.08, 0.8)  # pin stubs — fixed thin line

    kw = dict(x_min=x_min, y_max=y_max, scale=scale)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}">',
           f'<rect width="{W}" height="{H}" fill="{_BG}"/>']

    for el in all_els:
        t = el.tag
        if t == 'line':
            lw = max(_v(el.get('width', '152')) * scale, 0.5)
            x1s, y1s = _tr(_v(el.get('x1')), _v(el.get('y1')), **kw)
            x2s, y2s = _tr(_v(el.get('x2')), _v(el.get('y2')), **kw)
            out.append(_line(x1s, y1s, x2s, y2s, _SYM_BODY, lw))

        elif t == 'arc':
            lw = max(_v(el.get('width', '152')) * scale, 0.5)
            out.append(_arc(_v(el.get('cx')), _v(el.get('cy')), _v(el.get('r')),
                            float(el.get('start')), float(el.get('sweep')),
                            _SYM_BODY, lw, **kw))

        elif t == 'shape':
            out.append(_shape(_v(el.get('x')), _v(el.get('y')),
                               _v(el.get('w')), _v(el.get('h')),
                               int(el.get('roundness', 0)), _v(el.get('outline', '0')),
                               _SYM_BODY, **kw))

        elif t == 'polygon':
            coords = [_tr(_v(v.get('x')), _v(v.get('y')), **kw)
                      for v in el.findall('vertex')]
            pts_str = ' '.join(f'{xs:.1f},{ys:.1f}' for xs, ys in coords)
            out.append(f'<polygon points="{pts_str}" fill="{_SYM_BODY}" stroke="none"/>')

        elif t == 'text':
            out.append(_text(_v(el.get('x')), _v(el.get('y')),
                              el.text or '', _v(el.get('size', '1270')),
                              float(el.get('rot', 0)), el.get('align', 'bottom-left'),
                              _SYM_NAMES, **kw))

        elif t == 'pin':
            px, py = _v(el.get('x')), _v(el.get('y'))
            rot = float(el.get('rot', 0))
            length = _v(el.get('length', '2540'))
            direction = el.get('direction', 'pas')
            name = el.get('name', '')

            rad = math.radians(rot)
            ex = px + length * math.cos(rad)
            ey = py + length * math.sin(rad)
            pxs, pys = _tr(px, py, **kw)
            exs, eys = _tr(ex, ey, **kw)

            pc = _PIN_COLORS.get(direction, '#aaaaaa')
            out.append(_line(pxs, pys, exs, eys, pc, pin_lw))
            out.append(f'<circle cx="{pxs:.1f}" cy="{pys:.1f}" r="{pin_lw * 1.5:.1f}" fill="{pc}"/>')

            if name:
                cos_r = math.cos(rad)
                ta = 'end' if cos_r > 0.01 else ('start' if cos_r < -0.01 else 'middle')
                # place label outside body (opposite to stub direction), 0.8mm offset
                nx, ny = px - 0.8 * math.cos(rad), py - 0.8 * math.sin(rad)
                nxs, nys = _tr(nx, ny, **kw)
                sp = max(scale * 0.9, 7)
                out.append(f'<text x="{nxs:.1f}" y="{nys:.1f}" font-size="{sp:.0f}" '
                            f'font-family="monospace" fill="{pc}" text-anchor="{ta}" '
                            f'dominant-baseline="central">{name}</text>')

    out.append('</svg>')
    return '\n'.join(out)


def render_footprint(fp_el, scale=20, fixed_size=None):
    """Render <footprint> element to an SVG string.

    fixed_size: if set, all SVGs are exactly fixed_size×fixed_size px,
                content is scaled to fit and centered.
    """
    all_els = []
    for layer_el in fp_el:
        if layer_el.tag in _LAYER_COLORS:
            all_els.extend(list(layer_el))

    x_min, x_max, y_min, y_max = _bounds(all_els)
    w_mm = max(x_max - x_min, 1.0)
    h_mm = max(y_max - y_min, 1.0)

    if fixed_size:
        W = H = fixed_size
        scale = min(fixed_size / (w_mm + 2 * _MARGIN),
                    fixed_size / (h_mm + 2 * _MARGIN))
        ox = (fixed_size - (w_mm + 2 * _MARGIN) * scale) / 2
        oy = (fixed_size - (h_mm + 2 * _MARGIN) * scale) / 2
    else:
        scale = min(scale, _MAX_DIM / (w_mm + 2 * _MARGIN),
                    _MAX_DIM / (h_mm + 2 * _MARGIN))
        W = int((w_mm + 2 * _MARGIN) * scale)
        H = int((h_mm + 2 * _MARGIN) * scale)
        ox = oy = 0.0

    kw = dict(x_min=x_min, y_max=y_max, scale=scale)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}">',
           f'<rect width="{W}" height="{H}" fill="{_BG}"/>',
           f'<g transform="translate({ox:.1f},{oy:.1f})">']

    for layer_name in _LAYER_Z:
        layer_el = fp_el.find(layer_name)
        if layer_el is None:
            continue
        color = _LAYER_COLORS[layer_name]
        dash  = _LAYER_DASH.get(layer_name, '')
        opacity = _FAB_OPACITY if layer_name == 'fab' else ''
        if opacity:
            out.append(f'<g opacity="{opacity}">')

        for el in layer_el:
            t = el.tag
            if t == 'line':
                lw = max(_v(el.get('width', '152')) * scale, 0.5)
                x1s, y1s = _tr(_v(el.get('x1')), _v(el.get('y1')), **kw)
                x2s, y2s = _tr(_v(el.get('x2')), _v(el.get('y2')), **kw)
                out.append(_line(x1s, y1s, x2s, y2s, color, lw, dash))

            elif t == 'arc':
                lw = max(_v(el.get('width', '152')) * scale, 0.5)
                out.append(_arc(_v(el.get('cx')), _v(el.get('cy')), _v(el.get('r')),
                                float(el.get('start')), float(el.get('sweep')),
                                color, lw, **kw, dash=dash))

            elif t == 'shape':
                out.append(_shape(_v(el.get('x')), _v(el.get('y')),
                                   _v(el.get('w')), _v(el.get('h')),
                                   int(el.get('roundness', 0)), _v(el.get('outline', '0')),
                                   color, **kw))

            elif t == 'polygon':
                coords = [_tr(_v(v.get('x')), _v(v.get('y')), **kw)
                          for v in el.findall('vertex')]
                pts_str = ' '.join(f'{xs:.1f},{ys:.1f}' for xs, ys in coords)
                out.append(f'<polygon points="{pts_str}" fill="{color}" stroke="none"/>')

            elif t == 'smd':
                x, y = _v(el.get('x')), _v(el.get('y'))
                rot = float(el.get('rot', 0))
                out.append(_smd(x, y, _v(el.get('width')), _v(el.get('height')),
                                el.get('roundness', '0'), rot, color, **kw))
                out.append(_pad_label(x, y, el.get('name', ''), **kw))

            elif t == 'pad':
                x, y = _v(el.get('x')), _v(el.get('y'))
                out.append(_pad(x, y, _v(el.get('drill', '1000')), el.get('shape', 'round'),
                                color, **kw))
                out.append(_pad_label(x, y, el.get('name', ''), **kw))

            elif t == 'hole':
                out.append(_hole(_v(el.get('x')), _v(el.get('y')),
                                  _v(el.get('drill')), **kw))

            elif t == 'text':
                out.append(_text(_v(el.get('x')), _v(el.get('y')),
                                  el.text or '', _v(el.get('size', '1270')),
                                  float(el.get('rot', 0)), el.get('align', 'bottom-left'),
                                  color, **kw))

        if opacity:
            out.append('</g>')

    out.append('</g>')   # translate group
    out.append('</svg>')
    return '\n'.join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import sys

    ir_path = sys.argv[1] if len(sys.argv) > 1 else 'testData/rc.swlib'
    comp_name = sys.argv[2] if len(sys.argv) > 2 else None

    tree = ET.parse(ir_path)
    root = tree.getroot()
    comp = (root.find(f'.//component[@name="{comp_name}"]') if comp_name
            else root.find('.//component'))

    if comp is None:
        print(f'Component "{comp_name}" not found')
        sys.exit(1)

    cid = comp.get('name')

    svg = render_symbol(comp, root)
    p = Path(f'testData/{cid}_sym.svg')
    p.write_text(svg, encoding='utf-8')
    print(f'Written: {p}')

    for fp in comp.findall('footprint'):
        fid = fp.get('name')
        svg = render_footprint(fp)
        p = Path(f'testData/{cid}_{fid}_fp.svg')
        p.write_text(svg, encoding='utf-8')
        print(f'Written: {p}')
