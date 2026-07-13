"""SVG renderer for IR library symbols and footprints."""
import math
import xml.etree.ElementTree as ET
from pathlib import Path
from babel.ir_util import symbol_pool, component_gates, arc_center

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

# IR signed layer number (ir_schema.md "Плата (Board IR)") → Eagle display
# color (from eagle_layers.xml color attributes). Footprint geometry carries
# layer="N" directly; <smd>/<pad>/<hole> have no layer (copper by
# construction — far-side smd marked layer="-1").
_LAYER_COLORS = {
    1:    _E[4],   # copper mount side (Eagle 1  color=4  red)
    -1:   _E[1],   # copper far side   (Eagle 16 color=1  blue)
    121:  _E[7],   # silk    (Eagle 21 color=7 yellow)
    -121: _E[1],   # b silk  (Eagle 22 color=1 blue)
    125:  _E[7],   # names   (Eagle 25)
    -125: _E[1],
    127:  _E[7],   # values  (Eagle 27)
    -127: _E[1],
    129:  _E[7],   # mask
    -129: _E[1],
    131:  _E[7],   # paste   (Eagle 31)
    -131: _E[7],
    139:  _E[4],   # courtyard (Eagle 39 color=4 red, dashed)
    -139: _E[4],
    120:  _E[11],  # Dimension — cyan; ALL cuts (outline, slots). NOT white/
                   # yellow: must be tell-apart-able from silk 121 at 1px
    146:  _E[13],  # PLATING marker (Eagle projection: layer 156)
    148:  _E[7],   # Document notes (dimmed like fab)
    151:  _E[7],   # fab (Eagle 51, dimmed below)
    -151: _E[1],
}

# Layers rendered with dashed stroke (Eagle fill patterns 10/11 = hatch)
_LAYER_DASH = {139: '3,2', -139: '3,2'}

# Fab/Document are same color as silkscreen but dimmer so they don't compete;
# mask/paste are APERTURES, not ink — dimmed and painted UNDER copper, or a
# pad's mask rectangle floods the pad silk-yellow (caught on luminoso D1)
_DIM_LAYERS = {148, 151, -151, 129, -129, 131, -131}
_FAB_OPACITY = '0.45'

# Paint order, bottom-most first; pads (layer None) are painted with copper.
_LAYER_Z = [139, -139, 151, -151, 148, 120, 146, 129, -129, 131, -131,
            -121, -1, None, 1, 121, 125, -125, 127, -127]

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
            x1, y1 = _v(el.get('x1')), _v(el.get('y1'))
            x2, y2 = _v(el.get('x2')), _v(el.get('y2'))
            c = arc_center(x1, y1, x2, y2, float(el.get('curve', 0) or 0))
            if c is None:
                pts += [(x1, y1), (x2, y2)]
            else:
                cx, cy, r = c
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
        elif t == 'via':
            x, y = _v(el.get('x')), _v(el.get('y'))
            r = (_v(el.get('diameter', '0')) or _v(el.get('drill', '300')) * 1.5) / 2
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


def _arc(x1_ir, y1_ir, x2_ir, y2_ir, curve_deg, color, lw, x_min, y_max, scale, dash=''):
    """IR endpoint-form arc (ir_util's arc-math block) — SVG arcs are
    endpoint-parameterized too, so the endpoints transform like any point
    and only the radius is derived."""
    c = arc_center(x1_ir, y1_ir, x2_ir, y2_ir, curve_deg)
    x1s, y1s = _tr(x1_ir, y1_ir, x_min, y_max, scale)
    x2s, y2s = _tr(x2_ir, y2_ir, x_min, y_max, scale)
    if c is None:
        return _line(x1s, y1s, x2s, y2s, color, lw, dash)
    rp = c[2] * scale
    large = 1 if abs(curve_deg) > 180 else 0
    # IR positive curve = CCW (Y-up). After the Y flip the point moves in
    # the direction of DECREASING screen angle, and SVG sweep-flag=1 means
    # INCREASING screen angle (its Y is down) — so positive curve maps to
    # flag 0. (Was inverted; caught by eye on modtest.brd dxf art —
    # every arc bulged to the mirrored side.)
    cw = 0 if curve_deg > 0 else 1
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ''
    return (f'<path d="M{x1s:.1f},{y1s:.1f} A{rp:.1f},{rp:.1f} 0 {large} {cw} {x2s:.1f},{y2s:.1f}" '
            f'stroke="{color}" stroke-width="{lw:.1f}" fill="none" stroke-linecap="round"{dash_attr}/>')


def _poly_pts(el):
    """IR <polygon> -> flat (x, y) mm point list, tessellating curved
    vertices (`curve` = arc to the NEXT vertex, degrees CCW+ — same
    construction as eagle_parser.eagle_arc)."""
    vs = el.findall('vertex')
    pts = []
    n = len(vs)
    for i, v in enumerate(vs):
        x1, y1 = _v(v.get('x')), _v(v.get('y'))
        pts.append((x1, y1))
        curve = float(v.get('curve') or 0)
        if not curve or n < 2:
            continue
        nxt = vs[(i + 1) % n]
        x2, y2 = _v(nxt.get('x')), _v(nxt.get('y'))
        chord = math.hypot(x2 - x1, y2 - y1)
        if chord < 1e-9:
            continue
        a = math.radians(abs(curve))
        r = chord / (2 * math.sin(a / 2))
        d = r * math.cos(a / 2)
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        sign = 1 if curve > 0 else -1
        cx = mx + sign * d * (-(y2 - y1) / chord)
        cy = my + sign * d * ((x2 - x1) / chord)
        start = math.atan2(y1 - cy, x1 - cx)
        steps = max(2, int(abs(curve) / 15))
        for k in range(1, steps):
            ang = start + math.radians(curve) * k / steps
            pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    return pts


def _shape(x_ir, y_ir, w_ir, h_ir, roundness, outline_ir, color, x_min, y_max, scale,
           rot_deg=0):
    xs, ys = _tr(x_ir, y_ir, x_min, y_max, scale)
    wp, hp = w_ir * scale, h_ir * scale
    filled = (outline_ir == 0)
    fc = color if filled else 'none'
    sw = max(outline_ir * scale, 0.5) if not filled else 0
    # IR CCW+ -> SVG rotate is CW-positive on screen (Y down)
    rot_attr = f' transform="rotate({-rot_deg:.3g},{xs:.1f},{ys:.1f})"' if rot_deg % 360 else ''
    if roundness == 100:
        return (f'<circle cx="{xs:.1f}" cy="{ys:.1f}" r="{wp/2:.1f}" '
                f'stroke="{color}" stroke-width="{sw:.1f}" fill="{fc}"/>')
    rx = (roundness / 100) * min(wp, hp) / 2
    return (f'<rect x="{xs-wp/2:.1f}" y="{ys-hp/2:.1f}" width="{wp:.1f}" height="{hp:.1f}" '
            f'rx="{rx:.1f}" stroke="{color}" stroke-width="{sw:.1f}" fill="{fc}"{rot_attr}/>')


def _align_to_svg(align):
    """Eagle align → (text-anchor, dominant-baseline)."""
    a = align.lower()
    if a == 'center':
        return 'middle', 'central'
    parts = a.split('-')
    ta = {'left': 'start', 'center': 'middle', 'right': 'end'}.get(parts[-1], 'start')
    db = {'bottom': 'auto', 'center': 'central', 'top': 'hanging'}.get(parts[0], 'auto')
    return ta, db


def _text(x_ir, y_ir, txt, size_mm, rot_deg, align, color, x_min, y_max, scale,
          mirror=False):
    xs, ys = _tr(x_ir, y_ir, x_min, y_max, scale)
    sp = max(size_mm * scale, 6)
    ta, db = _align_to_svg(align)
    # Eagle CCW rotation in Y-up → negate for SVG (Y-down, CW-positive);
    # mirror = reading-direction flip around the anchor (bottom-side texts)
    tf = []
    if rot_deg:
        tf.append(f'rotate({-rot_deg:.0f},{xs:.1f},{ys:.1f})')
    if mirror:
        tf.append(f'translate({2*xs:.1f},0) scale(-1,1)')
    r_attr = f' transform="{" ".join(tf)}"' if tf else ''
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
# Placeholder resolution + pin number lookup (shared by render_symbol and
# render_schematic)
# ---------------------------------------------------------------------------

def _component_attrs(comp_el):
    """{attr_name: value} from <component>/<attributes>."""
    attrs_el = comp_el.find('attributes')
    if attrs_el is None:
        return {}
    return {a.get('name'): a.get('value', '') for a in attrs_el.findall('attr')}


def _resolve_placeholder(txt, designator, attrs):
    """`>NAME`/`>VALUE`/`>whatever` -> the real value if one is set, else the
    literal placeholder text unchanged — per the user, a placeholder is only
    ever shown literally when there's genuinely nothing to substitute (see
    ir_schema.md "Component instance": resolution order is instance override
    -> component attribute -> literal placeholder).

    `designator` may be None (library-only preview, no placed instance to
    substitute `>NAME` with — left as the literal placeholder there, same
    "nothing to show" case).
    """
    if not txt.startswith('>'):
        return txt
    key = txt[1:]
    if key == 'NAME':
        return designator if designator else txt
    value = attrs.get(key.lower())
    return value if value else txt


def _pin_pad_map(comp_el, gate_letter=None):
    """{ir_pin_name: pad_designator} from the first <footprint>'s
    <pin-mapping> — same "first/representative" convention already used for
    picking a gate to preview. A component without a footprint at all
    (power-flag/frame symbols) has no pad mapping, same as real Eagle: a
    device without a package can't show a pad number either.
    """
    fp_el = comp_el.find('footprint')
    if fp_el is None:
        return {}
    pm_el = fp_el.find('pin-mapping')
    if pm_el is None:
        return {}
    prefix = f'{gate_letter}.' if gate_letter else ''
    out = {}
    for m in pm_el.findall('map'):
        pin = m.get('pin', '')
        if gate_letter:
            if not pin.startswith(prefix):
                continue
            pin = pin[len(prefix):]
        elif '.' in pin:
            continue
        out[pin] = m.get('pad', '')
    return out


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
    gate_letter = None
    for gname, sname in gates:
        sym_el = pool.get(sname)
        if sym_el is not None:
            gate_letter = gname
            break
    if sym_el is None:
        return '<svg xmlns="http://www.w3.org/2000/svg"><text fill="red">no symbol</text></svg>'

    attrs = _component_attrs(comp_el)
    pad_map = _pin_pad_map(comp_el, gate_letter)

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
            out.append(_arc(_v(el.get('x1')), _v(el.get('y1')),
                            _v(el.get('x2')), _v(el.get('y2')),
                            float(el.get('curve', 0)),
                            _SYM_BODY, lw, **kw))

        elif t == 'shape':
            out.append(_shape(_v(el.get('x')), _v(el.get('y')),
                               _v(el.get('w')), _v(el.get('h')),
                               int(el.get('roundness', 0)), _v(el.get('outline', '0')),
                               _SYM_BODY, **kw, rot_deg=float(el.get('rot', 0))))

        elif t == 'polygon':
            coords = [_tr(_v(v.get('x')), _v(v.get('y')), **kw)
                      for v in el.findall('vertex')]
            pts_str = ' '.join(f'{xs:.1f},{ys:.1f}' for xs, ys in coords)
            out.append(f'<polygon points="{pts_str}" fill="{_SYM_BODY}" stroke="none"/>')

        elif t == 'text':
            txt = _resolve_placeholder(el.text or '', None, attrs)
            out.append(_text(_v(el.get('x')), _v(el.get('y')),
                              txt, _v(el.get('size', '1270')),
                              float(el.get('rot', 0)), el.get('align', 'bottom-left'),
                              _SYM_NAMES, **kw))

        elif t == 'pin':
            px, py = _v(el.get('x')), _v(el.get('y'))
            rot = float(el.get('rot', 0))
            length = _v(el.get('length', '2540'))
            direction = el.get('direction', 'pas')
            name = el.get('name', '')
            pinvis = el.get('pinvis', '1') == '1'
            padvis = el.get('padvis', '1') == '1'

            rad = math.radians(rot)
            ex = px + length * math.cos(rad)
            ey = py + length * math.sin(rad)
            pxs, pys = _tr(px, py, **kw)
            exs, eys = _tr(ex, ey, **kw)

            pc = _PIN_COLORS.get(direction, '#aaaaaa')
            out.append(_line(pxs, pys, exs, eys, pc, pin_lw))
            out.append(f'<circle cx="{pxs:.1f}" cy="{pys:.1f}" r="{pin_lw * 1.5:.1f}" fill="{pc}"/>')

            cos_r = math.cos(rad)
            ta = 'end' if cos_r > 0.01 else ('start' if cos_r < -0.01 else 'middle')
            ta_inner = 'start' if cos_r > 0.01 else ('end' if cos_r < -0.01 else 'middle')

            if pinvis and name:
                # place label outside body (opposite to stub direction), 0.8mm offset
                nx, ny = px - 0.8 * math.cos(rad), py - 0.8 * math.sin(rad)
                nxs, nys = _tr(nx, ny, **kw)
                sp = max(scale * 0.9, 7)
                out.append(f'<text x="{nxs:.1f}" y="{nys:.1f}" font-size="{sp:.0f}" '
                            f'font-family="monospace" fill="{pc}" text-anchor="{ta}" '
                            f'dominant-baseline="central">{name}</text>')

            if padvis:
                pad = pad_map.get(name, '')
                if pad:
                    # number sits toward the body end, opposite side from name
                    nx, ny = px + 0.6 * math.cos(rad), py + 0.6 * math.sin(rad)
                    nxs, nys = _tr(nx, ny, **kw)
                    sp = max(scale * 0.8, 6)
                    out.append(f'<text x="{nxs:.1f}" y="{nys:.1f}" font-size="{sp:.0f}" '
                                f'font-family="monospace" fill="{_SYM_INFO}" text-anchor="{ta_inner}" '
                                f'dominant-baseline="central">{pad}</text>')

    out.append('</svg>')
    return '\n'.join(out)


# ---------------------------------------------------------------------------
# Schematic canvas (.swprj <schematic>: placed <instance>s + <net>s)
# ---------------------------------------------------------------------------

_NET_WIRE = '#66DD66'
_NET_LABEL = '#FFAA33'
_DESIGNATOR_COLOR = '#88CCFF'


def _inst_point(x, y, inst):
    """Symbol-local mm (Y-up) -> absolute canvas mm (Y-up), via this
    instance's placement. Same mirror-then-rotate composition as
    kicad_schematic.py's KiCad-side transform, just without that module's
    extra Y-down/Y-up detour — IR's own canvas is already Y-up everywhere,
    symbol-local included, so no flip is needed here at all.
    """
    if inst['mirror']:
        x = -x
    rad = math.radians(inst['rot'])
    xr = x * math.cos(rad) - y * math.sin(rad)
    yr = x * math.sin(rad) + y * math.cos(rad)
    return inst['x'] + xr, inst['y'] + yr


def _inst_dir_angle(angle_deg, inst):
    """Direction (not position) transform for this instance — same
    mirror-then-rotate as _inst_point, just on a unit vector instead of a
    point (no translation to apply). Used for anything carrying its own
    rotation (pin stub via two transformed points instead, text/arc via
    this) so the math is derived once, not re-guessed per primitive.
    """
    rad = math.radians(angle_deg)
    dx, dy = math.cos(rad), math.sin(rad)
    if inst['mirror']:
        dx = -dx
    rrad = math.radians(inst['rot'])
    dxr = dx * math.cos(rrad) - dy * math.sin(rrad)
    dyr = dx * math.sin(rrad) + dy * math.cos(rrad)
    return math.degrees(math.atan2(dyr, dxr)) % 360


def render_schematic(root, scale=8, canvas_el=None):
    """Render a project's `<schematic>` (placed `<instance>`s + `<net>`s) to
    one SVG — sanity-check view for the KiCad project importer, not a
    polished schematic renderer (no de-overlap of text, no print frame).

    `canvas_el` overrides which canvas to draw: pass a `<module>` element
    to render that module's own inner canvas instead of the top-level
    `<schematic>` (same content model — ir_schema.md "Модуль": внутри — та
    же структура, что у <schematic>; the module's <port> children are
    simply not instances/nets and are skipped by the loops below).
    """
    schem_el = canvas_el if canvas_el is not None else root.find('schematic')
    if schem_el is None:
        return '<svg xmlns="http://www.w3.org/2000/svg"><text fill="red">no schematic</text></svg>'

    pool = symbol_pool(root)

    instances = []
    for inst_el in schem_el.findall('instance'):
        comp_el = root.find(f'.//component[@name="{inst_el.get("component")}"]')
        if comp_el is None:
            continue
        sym_el = None
        gate_letter = None
        for gname, sname in component_gates(comp_el):
            sym_el = pool.get(sname)
            if sym_el is not None:
                gate_letter = gname
                break
        if sym_el is None:
            continue

        designator = inst_el.get('name', '')
        # Instance overrides take priority over the component's own
        # (shared, device-level) attributes — see ir_schema.md "Component
        # instance" resolution order.
        attrs = dict(_component_attrs(comp_el))
        attrs.update({a.get('name'): a.get('value', '') for a in inst_el.findall('attr')})

        instances.append({
            'sym_el': sym_el,
            'x': _v(inst_el.get('x')), 'y': _v(inst_el.get('y')),
            'rot': float(inst_el.get('rot', 0)),
            'mirror': inst_el.get('mirror', '0') == '1',
            'name': designator,
            'attrs': attrs,
            'pad_map': _pin_pad_map(comp_el, gate_letter),
        })

    # Collect drawable primitives in absolute mm first (tracking bounds as
    # we go), then size/scale the SVG once and draw — same two-pass shape
    # render_symbol/render_footprint use, just across many instances+nets
    # instead of one local symbol frame.
    prims = []
    xs, ys = [], []

    def _track(x, y):
        xs.append(x)
        ys.append(y)

    for inst in instances:
        for el in inst['sym_el']:
            t = el.tag
            if t == 'line':
                x1, y1 = _inst_point(_v(el.get('x1')), _v(el.get('y1')), inst)
                x2, y2 = _inst_point(_v(el.get('x2')), _v(el.get('y2')), inst)
                _track(x1, y1); _track(x2, y2)
                prims.append(('line', x1, y1, x2, y2, _v(el.get('width', '152')), _SYM_BODY))
            elif t == 'arc':
                # endpoint canon: endpoints transform like any point, mirror
                # only flips the bulge side (curve sign)
                ax1, ay1 = _inst_point(_v(el.get('x1')), _v(el.get('y1')), inst)
                ax2, ay2 = _inst_point(_v(el.get('x2')), _v(el.get('y2')), inst)
                curve = float(el.get('curve', 0))
                if inst['mirror']:
                    curve = -curve
                _track(ax1, ay1); _track(ax2, ay2)
                prims.append(('arc', ax1, ay1, ax2, ay2, curve, _v(el.get('width', '152'))))
            elif t == 'shape':
                x, y = _inst_point(_v(el.get('x')), _v(el.get('y')), inst)
                w, h = _v(el.get('w')), _v(el.get('h'))
                rot = _inst_dir_angle(float(el.get('rot', 0)), inst)
                d = math.hypot(w, h) / 2
                _track(x - d, y - d); _track(x + d, y + d)
                prims.append(('shape', x, y, w, h, int(el.get('roundness', 0)),
                              _v(el.get('outline', '0')), rot))
            elif t == 'polygon':
                vs = [_inst_point(_v(v.get('x')), _v(v.get('y')), inst) for v in el.findall('vertex')]
                for vx, vy in vs:
                    _track(vx, vy)
                prims.append(('polygon', vs))
            elif t == 'text':
                x, y = _inst_point(_v(el.get('x')), _v(el.get('y')), inst)
                rot = _inst_dir_angle(float(el.get('rot', 0)), inst)
                txt = _resolve_placeholder(el.text or '', inst['name'], inst['attrs'])
                _track(x, y)
                prims.append(('text', x, y, txt, _v(el.get('size', '1270')),
                              rot, el.get('align', 'bottom-left'), _SYM_NAMES))
            elif t == 'pin':
                px, py = _v(el.get('x')), _v(el.get('y'))
                rad = math.radians(float(el.get('rot', 0)))
                length = _v(el.get('length', '2540'))
                ax, ay = _inst_point(px, py, inst)
                bx, by = _inst_point(px + length * math.cos(rad), py + length * math.sin(rad), inst)
                name = el.get('name', '')
                pinvis = el.get('pinvis', '1') == '1'
                padvis = el.get('padvis', '1') == '1'
                pad = inst['pad_map'].get(name, '') if padvis else ''
                # Name/pad offset points computed in LOCAL space (same 0.8/
                # 0.6mm convention as render_symbol), then transformed —
                # an isometry (rotate+optional reflect), so the offset
                # survives the instance transform unchanged in magnitude.
                nx, ny = _inst_point(px - 0.8 * math.cos(rad), py - 0.8 * math.sin(rad), inst) \
                    if pinvis and name else (None, None)
                qx, qy = _inst_point(px + 0.6 * math.cos(rad), py + 0.6 * math.sin(rad), inst) \
                    if pad else (None, None)
                _track(ax, ay); _track(bx, by)
                if nx is not None:
                    _track(nx, ny)
                if qx is not None:
                    _track(qx, qy)
                prims.append(('pin', ax, ay, bx, by, el.get('direction', 'pas'),
                              name if pinvis else '', nx, ny, pad, qx, qy))

        ox, oy = _inst_point(0, 0, inst)
        _track(ox, oy)
        prims.append(('designator', ox, oy, inst['name']))

    # Module instances (ir_schema.md "Модуль") — drawn as the block
    # rectangle the module declares (dx/dy, origin = center) with its ports
    # as dots+names on the edges; the module's INNER content is a separate
    # canvas, not rendered here (pass the <module> element as canvas_el to
    # see it).
    for inst_el in schem_el.findall('instance'):
        mod_name = inst_el.get('module')
        if not mod_name:
            continue
        mod_el = root.find(f'module[@name="{mod_name}"]')
        if mod_el is None:
            continue
        inst = {'x': _v(inst_el.get('x')), 'y': _v(inst_el.get('y')),
                'rot': float(inst_el.get('rot', 0)),
                'mirror': inst_el.get('mirror', '0') == '1'}
        dxm, dym = _v(mod_el.get('dx')), _v(mod_el.get('dy'))
        x, y = inst['x'], inst['y']
        half_diag = math.hypot(dxm, dym) / 2
        _track(x - half_diag, y - half_diag)
        _track(x + half_diag, y + half_diag)
        prims.append(('shape', x, y, dxm, dym, 0, _v('152'),
                      _inst_dir_angle(0, inst)))
        prims.append(('text', x, y, f"{inst_el.get('name', '')}: {mod_name}",
                      _v('1270'), 0, 'center', _SYM_NAMES))
        for p in mod_el.findall('port'):
            # side/coord -> block-local point (coord is measured from the
            # block CENTER along the edge, ir_schema.md "Модуль"), then the
            # same instance transform as any symbol-local geometry.
            side, coord = p.get('side'), _v(p.get('coord'))
            local = {'left': (-dxm / 2, coord), 'right': (dxm / 2, coord),
                     'top': (coord, dym / 2), 'bottom': (coord, -dym / 2)}.get(side)
            if local is None:
                continue
            ax, ay = _inst_point(local[0], local[1], inst)
            _track(ax, ay)
            prims.append(('junction', ax, ay))
            prims.append(('text', ax, ay, p.get('name', ''), _v('1270'),
                          0, 'bottom-left', _SYM_NAMES))

    for net_el in schem_el.findall('net'):
        for seg_el in net_el.findall('segment'):
            for line_el in seg_el.findall('line'):
                x1, y1 = _v(line_el.get('x1')), _v(line_el.get('y1'))
                x2, y2 = _v(line_el.get('x2')), _v(line_el.get('y2'))
                _track(x1, y1); _track(x2, y2)
                w = _v(line_el.get('width', '0')) or _v('152')
                prims.append(('line', x1, y1, x2, y2, w, _NET_WIRE))
            for j_el in seg_el.findall('junction'):
                jx, jy = _v(j_el.get('x')), _v(j_el.get('y'))
                _track(jx, jy)
                prims.append(('junction', jx, jy))
            for l_el in seg_el.findall('label'):
                lx, ly = _v(l_el.get('x')), _v(l_el.get('y'))
                _track(lx, ly)
                prims.append(('label', lx, ly, net_el.get('name', ''),
                              _v(l_el.get('size', '1270')), float(l_el.get('rot', 0))))

    # Decorative canvas geometry — direct <line>/<shape>/<arc> children of
    # <schematic> itself (ir_schema.md "Декоративная геометрия схемы"),
    # not inside any <instance>/<net> — drawn in the GRAPHIC color, same
    # primitive shapes as symbol-body geometry, just already in absolute
    # canvas mm (no instance transform to apply).
    for el in schem_el:
        t = el.tag
        if t == 'line':
            x1, y1 = _v(el.get('x1')), _v(el.get('y1'))
            x2, y2 = _v(el.get('x2')), _v(el.get('y2'))
            _track(x1, y1); _track(x2, y2)
            prims.append(('line', x1, y1, x2, y2, _v(el.get('width', '152')), _SYM_INFO))
        elif t == 'arc':
            ax1, ay1 = _v(el.get('x1')), _v(el.get('y1'))
            ax2, ay2 = _v(el.get('x2')), _v(el.get('y2'))
            _track(ax1, ay1); _track(ax2, ay2)
            prims.append(('arc', ax1, ay1, ax2, ay2,
                          float(el.get('curve', 0)), _v(el.get('width', '152'))))
        elif t == 'shape':
            x, y = _v(el.get('x')), _v(el.get('y'))
            w, h = _v(el.get('w')), _v(el.get('h'))
            d = math.hypot(w, h) / 2
            _track(x - d, y - d); _track(x + d, y + d)
            prims.append(('shape', x, y, w, h, int(el.get('roundness', 0)),
                          _v(el.get('outline', '0')), float(el.get('rot', 0))))
        elif t == 'text' and el.text:
            # Decorative canvas text — only elements WITH content:
            # placeholder <text> lives inside symbols, never here.
            x, y = _v(el.get('x')), _v(el.get('y'))
            _track(x, y)
            prims.append(('text', x, y, el.text, _v(el.get('size', '1270')),
                          float(el.get('rot', 0)), el.get('align', 'bottom-left'),
                          _SYM_INFO))
        elif t == 'note' and el.text:
            # <note> (markdown, ir_schema.md) — sanity-view rendering only:
            # dashed-ish bounding hint via the top edge + raw markdown text
            # (no markdown rendering here; that's the future editor's job).
            x, y = _v(el.get('x')), _v(el.get('y'))
            w = _v(el.get('w'))
            _track(x, y); _track(x + w, y)
            prims.append(('line', x, y, x + w, y, _v('76'), _SYM_INFO))
            prims.append(('text', x, y, el.text, _v('1270'),
                          float(el.get('rot', 0)), 'top-left', _SYM_INFO))

    if not xs:
        return '<svg xmlns="http://www.w3.org/2000/svg"><text fill="red">empty schematic</text></svg>'

    x_min, x_max, y_min, y_max = min(xs), max(xs), min(ys), max(ys)
    w_mm = max(x_max - x_min, 1.0)
    h_mm = max(y_max - y_min, 1.0)
    scale = min(scale, 2000 / (w_mm + 2 * _MARGIN), 2000 / (h_mm + 2 * _MARGIN))
    W = int((w_mm + 2 * _MARGIN) * scale)
    H = int((h_mm + 2 * _MARGIN) * scale)
    kw = dict(x_min=x_min, y_max=y_max, scale=scale)
    pin_lw = max(scale * 0.08, 0.8)
    junction_r = max(scale * 0.12, 1.5)

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}">',
           f'<rect width="{W}" height="{H}" fill="{_BG}"/>']

    for p in prims:
        kind = p[0]
        if kind == 'line':
            _, x1, y1, x2, y2, w, color = p
            x1s, y1s = _tr(x1, y1, **kw)
            x2s, y2s = _tr(x2, y2, **kw)
            out.append(_line(x1s, y1s, x2s, y2s, color, max(w * scale, 0.5)))
        elif kind == 'arc':
            _, ax1, ay1, ax2, ay2, curve, w = p
            out.append(_arc(ax1, ay1, ax2, ay2, curve, _SYM_BODY, max(w * scale, 0.5), **kw))
        elif kind == 'shape':
            _, x, y, w, h, roundness, outline, rot = p
            xs2, ys2 = _tr(x, y, **kw)
            wp, hp = w * scale, h * scale
            filled = (outline == 0)
            fc = _SYM_BODY if filled else 'none'
            sw = max(outline * scale, 0.5) if not filled else 0
            rot_attr = f' transform="rotate({-rot:.1f},{xs2:.1f},{ys2:.1f})"' if rot else ''
            if roundness == 100:
                out.append(f'<circle cx="{xs2:.1f}" cy="{ys2:.1f}" r="{wp/2:.1f}" '
                           f'stroke="{_SYM_BODY}" stroke-width="{sw:.1f}" fill="{fc}"{rot_attr}/>')
            else:
                rx = (roundness / 100) * min(wp, hp) / 2
                out.append(f'<rect x="{xs2-wp/2:.1f}" y="{ys2-hp/2:.1f}" width="{wp:.1f}" height="{hp:.1f}" '
                           f'rx="{rx:.1f}" stroke="{_SYM_BODY}" stroke-width="{sw:.1f}" fill="{fc}"{rot_attr}/>')
        elif kind == 'polygon':
            coords = [_tr(vx, vy, **kw) for vx, vy in p[1]]
            pts_str = ' '.join(f'{xs2:.1f},{ys2:.1f}' for xs2, ys2 in coords)
            out.append(f'<polygon points="{pts_str}" fill="{_SYM_BODY}" stroke="none"/>')
        elif kind == 'text':
            _, x, y, txt, size, rot, align, color = p
            out.append(_text(x, y, txt, size, rot, align, color, **kw))
        elif kind == 'pin':
            _, ax, ay, bx, by, direction, name, nx, ny, pad, qx, qy = p
            axs, ays = _tr(ax, ay, **kw)
            bxs, bys = _tr(bx, by, **kw)
            pc = _PIN_COLORS.get(direction, '#aaaaaa')
            out.append(_line(axs, ays, bxs, bys, pc, pin_lw))
            out.append(f'<circle cx="{axs:.1f}" cy="{ays:.1f}" r="{pin_lw*1.5:.1f}" fill="{pc}"/>')

            # Anchor from the TRANSFORMED stub direction (bx-ax), not the
            # symbol-local rot — after an arbitrary instance rotation/mirror
            # the local angle no longer says which screen-side the body is
            # on, but _tr never flips X, so the absolute-mm delta does.
            dx = bx - ax
            ta = 'end' if dx > 0.01 else ('start' if dx < -0.01 else 'middle')
            ta_inner = 'start' if dx > 0.01 else ('end' if dx < -0.01 else 'middle')

            if name and nx is not None:
                nxs, nys = _tr(nx, ny, **kw)
                sp = max(scale * 0.9, 7)
                out.append(f'<text x="{nxs:.1f}" y="{nys:.1f}" font-size="{sp:.0f}" '
                           f'font-family="monospace" fill="{pc}" text-anchor="{ta}" '
                           f'dominant-baseline="central">{name}</text>')
            if pad and qx is not None:
                qxs, qys = _tr(qx, qy, **kw)
                sp = max(scale * 0.8, 6)
                out.append(f'<text x="{qxs:.1f}" y="{qys:.1f}" font-size="{sp:.0f}" '
                           f'font-family="monospace" fill="{_SYM_INFO}" text-anchor="{ta_inner}" '
                           f'dominant-baseline="central">{pad}</text>')
        elif kind == 'designator':
            _, x, y, name = p
            xs2, ys2 = _tr(x, y, **kw)
            out.append(f'<text x="{xs2:.1f}" y="{ys2 - scale * 1.2:.1f}" '
                       f'font-size="{max(scale * 0.9, 7):.0f}" font-family="monospace" '
                       f'fill="{_DESIGNATOR_COLOR}" text-anchor="middle">{name}</text>')
        elif kind == 'junction':
            _, jx, jy = p
            jxs, jys = _tr(jx, jy, **kw)
            out.append(f'<circle cx="{jxs:.1f}" cy="{jys:.1f}" r="{junction_r:.1f}" fill="{_NET_WIRE}"/>')
        elif kind == 'label':
            _, lx, ly, name, size, rot = p
            out.append(_text(lx, ly, name, size, rot, 'bottom-left', _NET_LABEL, **kw))

    out.append('</svg>')
    return '\n'.join(out)


def render_all_symbols(root, out_dir, scale=10):
    """render_symbol() for every <component> directly under root (works for
    both a `.swlib` <library> and a `.swprj` <project> — components sit at
    the same depth in both), written one file per component into out_dir.

    A quick browseable gallery — looking at one symbol in isolation (e.g.
    "is this 48-pin MCU's layout actually sane?") beats squinting at it
    buried inside a whole rendered schematic.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for comp in root.findall('component'):
        if not component_gates(comp):
            continue   # no symbol at all (shouldn't happen, but be defensive)
        svg = render_symbol(comp, root, scale=scale)
        p = out_dir / f'{comp.get("name")}.svg'
        p.write_text(svg, encoding='utf-8')
        written.append(p)
    return written


def render_footprint(fp_el, scale=20, fixed_size=None):
    """Render <footprint> element to an SVG string.

    fixed_size: if set, all SVGs are exactly fixed_size×fixed_size px,
                content is scaled to fit and centered.
    """
    def _el_layer(el):
        """Element -> signed layer number for styling; None for pads
        (copper-by-construction; the far-side smd is tinted separately
        below). No/unparseable layer and anti (!) objects -> 'drop'
        (subtractive geometry is not rendered yet)."""
        if el.tag in ('smd', 'pad', 'hole', 'via'):
            return None
        try:
            return int((el.get('layer') or '').strip())
        except ValueError:
            return 'drop'

    all_els = [el for el in fp_el
               if el.tag not in ('description', 'model3d', 'pin-mapping')
               and _el_layer(el) != 'drop']

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

    by_layer = {}
    for el in all_els:
        by_layer.setdefault(_el_layer(el), []).append(el)

    # _LAYER_Z first (correct paint order for the known set), then any layer
    # numbers outside it (user layers etc.) on top, in numeric order.
    extra = sorted((k for k in by_layer if k not in _LAYER_Z),
                   key=lambda v: abs(v))
    for layer_n in list(_LAYER_Z) + extra:
        group = by_layer.get(layer_n, [])
        if not group:
            continue
        # unknown (user) layers: neutral gray — anything from the known
        # palette would masquerade as a semantic layer (150 dxf art used to
        # render silk-yellow)
        color = _LAYER_COLORS.get(layer_n, '#9A9A9A') if layer_n is not None else _E[4]
        dash  = _LAYER_DASH.get(layer_n, '')
        opacity = _FAB_OPACITY if layer_n in _DIM_LAYERS else ''
        if opacity:
            out.append(f'<g opacity="{opacity}">')

        for el in group:
            t = el.tag
            if t == 'line':
                lw = max(_v(el.get('width', '152')) * scale, 0.5)
                x1s, y1s = _tr(_v(el.get('x1')), _v(el.get('y1')), **kw)
                x2s, y2s = _tr(_v(el.get('x2')), _v(el.get('y2')), **kw)
                out.append(_line(x1s, y1s, x2s, y2s, color, lw, dash))

            elif t == 'arc':
                lw = max(_v(el.get('width', '152')) * scale, 0.5)
                out.append(_arc(_v(el.get('x1')), _v(el.get('y1')),
                                _v(el.get('x2')), _v(el.get('y2')),
                                float(el.get('curve', 0)),
                                color, lw, **kw, dash=dash))

            elif t == 'shape':
                out.append(_shape(_v(el.get('x')), _v(el.get('y')),
                                   _v(el.get('w')), _v(el.get('h')),
                                   int(el.get('roundness', 0)), _v(el.get('outline', '0')),
                                   color, **kw, rot_deg=float(el.get('rot', 0))))

            elif t == 'polygon':
                coords = [_tr(px, py, **kw) for px, py in _poly_pts(el)]
                pts_str = ' '.join(f'{xs:.1f},{ys:.1f}' for xs, ys in coords)
                # copper polygons (pours) translucent: the recomputed fill
                # isn't stored, an opaque contour fill would bury the board
                op = ' fill-opacity="0.35"' if layer_n is not None and abs(layer_n) < 100 else ''
                out.append(f'<polygon points="{pts_str}" fill="{color}" stroke="none"{op}/>')

            elif t == 'via':
                xs, ys = _tr(_v(el.get('x')), _v(el.get('y')), **kw)
                dia = _v(el.get('diameter', '0')) or _v(el.get('drill')) * 1.5
                ro = dia / 2 * kw['scale']
                ri = _v(el.get('drill')) / 2 * kw['scale']
                out.append(f'<circle cx="{xs:.1f}" cy="{ys:.1f}" r="{ro:.1f}" '
                           f'fill="{_E[2]}"/>')      # Eagle Vias color 2 green
                out.append(f'<circle cx="{xs:.1f}" cy="{ys:.1f}" r="{ri:.1f}" '
                           f'fill="{_BG}"/>')

            elif t == 'smd':
                x, y = _v(el.get('x')), _v(el.get('y'))
                rot = float(el.get('rot', 0))
                pad_color = _E[1] if el.get('layer') == '-1' else color
                out.append(_smd(x, y, _v(el.get('width')), _v(el.get('height')),
                                el.get('roundness', '0'), rot, pad_color, **kw))
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
                if el.get('hidden') == 'yes':
                    continue    # suppression override (ir_schema.md <element>)
                out.append(_text(_v(el.get('x')), _v(el.get('y')),
                                  el.text or '', _v(el.get('size', '1270')),
                                  float(el.get('rot', 0)), el.get('align', 'bottom-left'),
                                  color, **kw, mirror=el.get('mirror') == '1'))

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

    symbols_dir = Path('outputs') / f'{Path(ir_path).stem}_symbols'
    written = render_all_symbols(root, symbols_dir)
    print(f'Written: {len(written)} symbol SVG(s) in {symbols_dir}')

    if root.find('schematic') is not None:
        svg = render_schematic(root)
        out_p = Path('outputs') / f'{Path(ir_path).stem}_schematic.svg'
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(svg, encoding='utf-8')
        print(f'Written: {out_p}')
        sys.exit(0)

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
