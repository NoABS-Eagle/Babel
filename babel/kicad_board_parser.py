"""KiCad 10 .kicad_pcb -> IR <layout> (project import, path b).

Walks the RAW s-expression tree (kiutils' parse_sexp tokenizer only) — not
kiutils' Board dataclasses. That's the side-channel principle applied from
day one: kiutils has already been caught dropping tokens it doesn't know
(`power local`, `embedded_files`, property `hide`), so the board reader
never goes through its object model at all — what the tokenizer returns is
byte-complete by construction.

Board import happens ONLY as part of a project import (convert_project_full)
— nets/identity/attributes come from the schematic side; the board
contributes placement and copper. Format older than KiCad 10 is a HARD
REJECT upstream (decisions.md).

Milestone scope (flat tolmach, simple -> complex):
  coordinates (aux_axis_origin = IR (0,0) — the export rule read back),
  stack (stackup -> formula; absent -> synthesized from layer count +
  general thickness, logged), nets, tracks (segment/arc), through vias,
  free board graphics (gr_*), placed footprints -> <element> with un-baked
  side/rot, synthetic babel:HOLE_* -> <hole>.
Deferred, NEVER silently (import_log): zones, keepouts, rules,
dimensions, groups, element-level text overrides.
"""
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from kiutils.utils import sexpr as _kiutils_sexpr

from babel import import_log
from babel.eagle_board_exporter import _instance_footprint
from babel.ir_util import format_stack
from babel.kicad_layers import kicad_to_ir
from babel.kicad_parser import _arc_params

_MIN_VERSION = 20241229          # anything older than KiCad 9/10 era rejects


# ---------------------------------------------------------------------------
# Raw-tree helpers
# ---------------------------------------------------------------------------

def _get(node, key):
    """First child list whose head token is `key`, or None."""
    for it in node:
        if isinstance(it, list) and it and it[0] == key:
            return it
    return None


def _gets(node, key):
    return [it for it in node
            if isinstance(it, list) and it and it[0] == key]


def _val(node, key, i=1, default=None):
    it = _get(node, key)
    return it[i] if it is not None and len(it) > i else default


def _um(mm):
    return str(round(float(mm) * 1000))


def _f(v):
    s = f'{float(v):.6f}'.rstrip('0').rstrip('.')
    return s if s else '0'


class _Frame:
    """KiCad page mm (Y down) -> IR µm (Y up), origin at aux_axis_origin
    (export rule: aux_axis_origin, when present, IS IR (0,0))."""

    def __init__(self, ax_mm, ay_mm):
        self.ax, self.ay = float(ax_mm), float(ay_mm)

    def x(self, mm):
        return str(round((float(mm) - self.ax) * 1000))

    def y(self, mm):
        return str(round(-(float(mm) - self.ay) * 1000))

    def fx(self, mm):
        return (float(mm) - self.ax) * 1000

    def fy(self, mm):
        return -(float(mm) - self.ay) * 1000


def _copper_ir(name):
    """KiCad copper layer name -> IR copper layer string ('1' top, '-1'
    bottom, inner '2'..'N-1' top-down), or None for a non-copper name."""
    if name == 'F.Cu':
        return '1'
    if name == 'B.Cu':
        return '-1'
    m = re.fullmatch(r'In(\d+)\.Cu', name or '')
    return str(int(m.group(1)) + 1) if m else None


def _ir_layer(name, ctx, what):
    """KiCad non-copper layer name -> IR layer string via the editable
    layer table; None (+log) when the layer has no IR projection."""
    n = kicad_to_ir(name)
    if n is None:
        import_log.log('kicad_pcb', ctx, f'{what} dropped',
                       f'layer {name!r} not in kicad_layers.tsv')
        return None
    return str(n)


# ---------------------------------------------------------------------------
# Stack
# ---------------------------------------------------------------------------

def _copper_names(pcb):
    """Ordered copper layer names from the (layers ...) table, top-down."""
    names = []
    layers = _get(pcb, 'layers') or []
    for it in layers[1:]:
        if isinstance(it, list) and len(it) >= 3 and str(it[2]) == 'signal' \
                and str(it[1]).endswith('.Cu'):
            names.append(str(it[1]))
    # KiCad's table order is F.Cu, inners..., B.Cu already; keep it.
    return names


def _read_stack(pcb, n_copper):
    """(setup (stackup ...)) -> IR stack formula. Absent stackup is a NORMAL
    KiCad state (written only after Board Setup was opened) — synthesize
    from layer count + (general (thickness)) with KiCad's default 35 µm
    copper, logged (decisions.md "Стек -> KiCad (stackup)")."""
    setup = _get(pcb, 'setup') or []
    stackup = _get(setup, 'stackup')
    if stackup is not None:
        coppers, dielectrics = [], []
        for lay in _gets(stackup, 'layer'):
            kind = _val(lay, 'type')
            th = _val(lay, 'thickness')
            if kind == 'copper':
                coppers.append(round(float(th) * 1000))
            elif str(lay[1]).startswith('dielectric'):
                eps = _val(lay, 'epsilon_r')
                tand = _val(lay, 'loss_tangent')
                dielectrics.append((round(float(th) * 1000),
                                    float(eps) if eps is not None else None,
                                    float(tand) if tand is not None else None))
        if len(coppers) == n_copper and len(dielectrics) == n_copper - 1:
            return format_stack(coppers, dielectrics)
        import_log.log('kicad_pcb', 'stackup', 'STACKUP malformed',
                       f'{len(coppers)} copper / {len(dielectrics)} dielectric '
                       f'cells for {n_copper} copper layers — synthesizing')
    total_um = round(float(_val(_get(pcb, 'general') or [], 'thickness',
                                default=1.6)) * 1000)
    cu = 35
    gaps = n_copper - 1
    d = max((total_um - n_copper * cu) // gaps, 1)
    coppers = [cu] * n_copper
    dielectrics = [(d, None, None)] * gaps
    formula = format_stack(coppers, dielectrics)
    import_log.log('kicad_pcb', 'stackup',
                   'STACKUP absent, synthesized',
                   f'{formula} from {n_copper} copper layers + total '
                   f'thickness {total_um}um (KiCad defaults)')
    return formula


# ---------------------------------------------------------------------------
# Copper: tracks, vias
# ---------------------------------------------------------------------------

def _net_name(item, code_to_name):
    """KiCad 10 canon is name-only `(net "NAME")` — the numbered net table
    died with v9 (a resave normalizes every reference to this form). The
    numbered legacy forms `(net N "NAME")` / `(net N)` still parse (our own
    fresh export writes them, v10 accepts them)."""
    it = _get(item, 'net')
    if it is None or len(it) < 2:
        return None
    if isinstance(it[1], str) and not it[1].lstrip('-').isdigit():
        return it[1] or None                       # (net "NAME")
    if len(it) > 2:
        return str(it[2]) or None                  # (net N "NAME")
    code = int(it[1])
    return code_to_name.get(code) if code else None   # (net N)


def _pt(node, key, frame):
    it = _get(node, key)
    return frame.fx(it[1]), frame.fy(it[2])


def _emit_track(sig, item, frame):
    if item[0] == 'segment':
        (x1, y1), (x2, y2) = _pt(item, 'start', frame), _pt(item, 'end', frame)
        el = ET.SubElement(sig, 'line',
                           x1=str(round(x1)), y1=str(round(y1)),
                           x2=str(round(x2)), y2=str(round(y2)))
    else:                                     # arc: start/mid/end -> curve
        p1, pm, p2 = (_pt(item, k, frame) for k in ('start', 'mid', 'end'))
        params = _arc_params(p1, pm, p2)
        if params is None:
            import_log.log('kicad_pcb', 'track', 'ARC degenerate, dropped',
                           f'start={p1} mid={pm} end={p2}')
            return
        el = ET.SubElement(sig, 'arc',
                           x1=str(round(p1[0])), y1=str(round(p1[1])),
                           x2=str(round(p2[0])), y2=str(round(p2[1])),
                           curve=_f(_snap_sweep(params[4])))
    el.set('width', _um(_val(item, 'width')))
    layer = _copper_ir(_val(item, 'layer'))
    if layer is None:
        import_log.log('kicad_pcb', 'track', 'TRACK dropped',
                       f'non-copper layer {_val(item, "layer")!r}')
        sig.remove(el)
        return
    el.set('layer', layer)


def _emit_via(sig, item, frame, n_copper):
    layers = _get(item, 'layers') or []
    span = {str(l) for l in layers[1:]}
    if span and span != {'F.Cu', 'B.Cu'}:
        # blind/buried: candidate hard-reject list (decisions.md) — reject
        # loudly rather than flattening the span
        raise ValueError(
            f'via span {sorted(span)}: only through vias are supported '
            f'(blind/buried vias are not modelled)')
    at = _get(item, 'at')
    ET.SubElement(sig, 'via',
                  x=frame.x(at[1]), y=frame.y(at[2]),
                  drill=_um(_val(item, 'drill')),
                  diameter=_um(_val(item, 'size')))


# ---------------------------------------------------------------------------
# Free board graphics
# ---------------------------------------------------------------------------

def _fill_none(item):
    """True when the graphic is NOT filled. Both spellings are legal and
    live: our own exporter writes (fill none)/(fill solid), a KiCad 10
    resave normalizes to (fill no)/(fill yes); older writers nest
    (fill (type none))."""
    f = _get(item, 'fill')
    if f is None or len(f) < 2:
        return False
    v = f[1]
    if isinstance(v, list):
        v = _val(f, 'type', default='')
    return str(v) in ('none', 'no')


def _snap_sweep(sweep):
    """Coordinate quantization in the file (µm / 6 decimals of mm) turns an
    exact 90° arc into 89.999955° after the 3-point re-fit — snap sweeps
    that are within fit noise of a whole degree."""
    r = round(sweep)
    return float(r) if abs(sweep - r) < 5e-3 and r else sweep


def _align_from_justify(effects):
    """Inverse of kicad_exporter._justify(align, flip_v=True) + the
    (justify ... mirror) merge: -> (align string, mirror bool)."""
    just = _get(effects or [], 'justify') or []
    kws = {str(k) for k in just[1:]}
    h = 'left' if 'left' in kws else 'right' if 'right' in kws else 'center'
    v = 'top' if 'top' in kws else 'bottom' if 'bottom' in kws else 'center'
    v = {'top': 'bottom', 'bottom': 'top'}.get(v, v)      # un-flip
    align = 'center' if v == h == 'center' else f'{v}-{h}'
    return align, 'mirror' in kws


def _text_attrs(el, item, frame):
    """(effects (font (size h w) (thickness t))) -> size/ratio/width/align."""
    effects = _get(item, 'effects')
    font = _get(effects or [], 'font')
    size = _get(font or [], 'size')
    h_mm = float(size[1]) if size else 1.27
    w_mm = float(size[2]) if size and len(size) > 2 else h_mm
    el.set('size', _um(h_mm))
    th = _val(font or [], 'thickness')
    if th is not None:
        el.set('ratio', str(round(float(th) / h_mm * 100)))
    # absent width = aspect 0.85*size (ir_schema canon) — write only a real
    # divergence, mirroring what the exporter does
    if abs(w_mm - 0.85 * h_mm) > 0.0005:
        el.set('width', _um(w_mm))
    align, mirror = _align_from_justify(effects)
    if align != 'bottom-left':
        el.set('align', align)
    if mirror:
        el.set('mirror', '1')


def _emit_graphic(layout, item, frame):
    tag = item[0]
    ctx = 'plain'
    if tag in ('gr_line', 'gr_arc'):
        ln = _ir_layer(_val(item, 'layer'), ctx, 'GEOMETRY')
        if ln is None:
            return
        w = _um(_val(_get(item, 'stroke') or [], 'width', default=0))
        if tag == 'gr_line':
            (x1, y1), (x2, y2) = _pt(item, 'start', frame), _pt(item, 'end', frame)
            ET.SubElement(layout, 'line',
                          x1=str(round(x1)), y1=str(round(y1)),
                          x2=str(round(x2)), y2=str(round(y2)),
                          width=w, layer=ln)
        else:
            p1, pm, p2 = (_pt(item, k, frame) for k in ('start', 'mid', 'end'))
            params = _arc_params(p1, pm, p2)
            if params is None:
                import_log.log('kicad_pcb', ctx, 'ARC degenerate, dropped', '')
                return
            ET.SubElement(layout, 'arc',
                          x1=str(round(p1[0])), y1=str(round(p1[1])),
                          x2=str(round(p2[0])), y2=str(round(p2[1])),
                          curve=_f(_snap_sweep(params[4])), width=w, layer=ln)
    elif tag == 'gr_circle':
        ln = _ir_layer(_val(item, 'layer'), ctx, 'GEOMETRY')
        if ln is None:
            return
        (cx, cy), (ex, ey) = _pt(item, 'center', frame), _pt(item, 'end', frame)
        r = math.hypot(ex - cx, ey - cy)
        outline = _um(_val(_get(item, 'stroke') or [], 'width', default=0))
        el = ET.SubElement(layout, 'shape',
                           x=str(round(cx)), y=str(round(cy)),
                           w=str(round(2 * r)), h=str(round(2 * r)),
                           roundness='100', layer=ln)
        if _fill_none(item):
            el.set('outline', outline)
    elif tag == 'gr_rect':
        ln = _ir_layer(_val(item, 'layer'), ctx, 'GEOMETRY')
        if ln is None:
            return
        (x1, y1), (x2, y2) = _pt(item, 'start', frame), _pt(item, 'end', frame)
        el = ET.SubElement(layout, 'shape',
                           x=str(round((x1 + x2) / 2)), y=str(round((y1 + y2) / 2)),
                           w=str(round(abs(x2 - x1))), h=str(round(abs(y2 - y1))),
                           layer=ln)
        if _fill_none(item):
            el.set('outline', _um(_val(_get(item, 'stroke') or [], 'width',
                                       default=0)))
    elif tag == 'gr_poly':
        ln = _ir_layer(_val(item, 'layer'), ctx, 'GEOMETRY')
        if ln is None:
            return
        el = ET.SubElement(layout, 'polygon',
                           width=_um(_val(_get(item, 'stroke') or [], 'width',
                                          default=0)),
                           layer=ln)
        if _fill_none(item):
            el.set('fill', '0')
        for xy in _gets(_get(item, 'pts') or [], 'xy'):
            ET.SubElement(el, 'vertex',
                          x=frame.x(xy[1]), y=frame.y(xy[2]))
    elif tag == 'gr_text':
        ln = _ir_layer(_val(item, 'layer'), ctx, 'GEOMETRY')
        if ln is None:
            return
        at = _get(item, 'at')
        el = ET.SubElement(layout, 'text',
                           x=frame.x(at[1]), y=frame.y(at[2]), layer=ln)
        rot = float(at[3]) if len(at) > 3 else 0.0
        if rot % 360:
            el.set('rot', _f(rot % 360))
        el.text = _kicad_overbar_to_eagle(str(item[1]))
        # KiCad board text is always the stroke font — that IS Eagle's
        # "vector", so the attribute survives the KiCad round-trip honestly
        el.set('font', 'vector')
        _text_attrs(el, item, frame)


def _kicad_overbar_to_eagle(s):
    """KiCad ~{SIG} -> IR/Eagle !SIG (inverse of _eagle_overbar_to_kicad)."""
    return re.sub(r'~\{([^}]*)\}', r'!\1', s)


# ---------------------------------------------------------------------------
# Footprints -> elements
# ---------------------------------------------------------------------------

def _fp_property(fp, key):
    for p in _gets(fp, 'property'):
        if len(p) > 2 and str(p[1]) == key:
            return str(p[2])
    return None


def _emit_element(layout, fp, frame, code_to_name, known_refdes):
    lib_id = str(fp[1])
    at = _get(fp, 'at')
    x, y = frame.x(at[1]), frame.y(at[2])
    at_rot = float(at[3]) if len(at) > 3 else 0.0
    bottom = _val(fp, 'layer') == 'B.Cu'
    refdes = _fp_property(fp, 'Reference') or ''

    # our own synthetic mounting-hole footprints -> layout <hole>
    if lib_id.startswith('babel:HOLE_'):
        pad = _get(fp, 'pad')
        ET.SubElement(layout, 'hole', x=x, y=y,
                      drill=_um(_val(pad, 'drill')))
        return

    if refdes not in known_refdes:
        # diagnostics over silence — same REFDES-identity rule as the Eagle
        # board path. Module-flattened refdes (TM1:C1 -> C101) will need the
        # reverse mapping when hierarchy lands; flat projects first.
        raise ValueError(
            f'board footprint {refdes!r} ({lib_id}) has no schematic '
            f'instance — REFDES identity broken (or a hierarchical project; '
            f'module boards are not supported yet)')

    # un-bake the export convention: top at-rot = θ, bottom at-rot = θ+180
    theta = (at_rot - 180) % 360 if bottom else at_rot % 360
    el = ET.SubElement(layout, 'element', name=refdes, x=x, y=y)
    if theta:
        el.set('rot', _f(theta))
    if bottom:
        el.set('side', 'bottom')

    # pad nets -> contactrefs on the matching <signal>
    refs = []
    for pad in _gets(fp, 'pad'):
        net = _net_name(pad, code_to_name)
        if net:
            refs.append((net, str(pad[1])))
    if _gets(fp, 'model'):
        pass                       # model binding lives on the library side
    return refs


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

_ANON_NET = re.compile(r'^N\$\d+$|^Net-\(.*\)$|^unconnected-\(.*\)$')


def _schem_net_by_pad(proj_el):
    """(designator, pad) -> schematic net name: <schematic> pinrefs pushed
    through each instance's footprint <pin-mapping>."""
    comp = {c.get('name'): c for c in proj_el.findall('component')}
    inst = {i.get('name'): i
            for i in proj_el.find('schematic').findall('instance')}
    pin2pad = {}                   # designator -> {pin: pad}
    out = {}
    for net in proj_el.find('schematic').findall('net'):
        for pr in net.iter('pinref'):
            d, pin = pr.get('part'), pr.get('pin')
            if d not in pin2pad:
                i = inst.get(d)
                c = comp.get(i.get('component')) if i is not None else None
                fp = _instance_footprint(c, i) if c is not None else None
                pm = fp.find('pin-mapping') if fp is not None else None
                pin2pad[d] = {} if pm is None else \
                    {m.get('pin'): m.get('pad') for m in pm.findall('map')}
            pad = pin2pad[d].get(pin)
            if pad is not None:
                out[(d, pad)] = net.get('name')
    return out


def _adopt_schematic_net_names(layout, signals, proj_el):
    """The schematic owns net identity (project import, path b): every board
    net that touches schematic pins must carry the SCHEMATIC's name.
    Anonymous board names (Eagle N$..., KiCad Net-(R1-Pad2) /
    unconnected-(...)) are adopted silently-with-log — they are generated
    labels, not facts. A NAMED board net disagreeing with the schematic is
    a real desync (stale board, F8 never run) — hard reject, the user fixes
    the source, we never guess which side is right."""
    if proj_el.find('schematic') is None:
        return
    pad2net = _schem_net_by_pad(proj_el)
    conflicts = []
    pending = []                     # (old name, sig element, target name)
    for name, sig in signals.items():
        votes = {pad2net.get((cr.get('element'), cr.get('pad')))
                 for cr in sig.findall('contactref')} - {None}
        if not votes:
            continue
        if len(votes) > 1:
            conflicts.append(f'{name!r} spans schematic nets {sorted(votes)}')
            continue
        want = votes.pop()
        if want == name:
            continue
        if not _ANON_NET.match(name):
            conflicts.append(f'{name!r} is named {want!r} on the schematic')
            continue
        pending.append((name, sig, want))
    # Two phases, because the schematic's regenerated anonymous numbering
    # can collide with the board's own (board N$1 and schematic N$1 are
    # unrelated nets) — renaming in a single pass would merge a renamed
    # signal into a NOT-yet-renamed namesake.
    for name, _, _ in pending:
        del signals[name]
    for name, sig, want in pending:
        if want in signals:            # two board fragments of one net
            for child in list(sig):
                signals[want].append(child)
            layout.remove(sig)
        else:
            sig.set('name', want)
            signals[want] = sig
        import_log.log('kicad_pcb', name,
                       f'NET renamed to schematic name "{want}"')
    if conflicts:
        raise ValueError(
            'board <-> schematic net desync (stale board? run "Update PCB '
            'from Schematic" in KiCad, fix the source, re-export):\n  ' +
            '\n  '.join(conflicts))


def convert_board(pcb_path, proj_el, known_refdes, layout_name='main'):
    """Parse pcb_path into a <layout> appended to proj_el.

    known_refdes: set of schematic instance designators — every placed
    footprint must resolve to one (REFDES identity), synthetic babel:HOLE_*
    excepted.
    """
    pcb_path = Path(pcb_path)
    text = pcb_path.read_text(encoding='utf-8')
    pcb = _kiutils_sexpr.parse_sexp(text)
    if pcb[0] != 'kicad_pcb':
        raise ValueError(f'{pcb_path}: not a .kicad_pcb')
    version = int(_val(pcb, 'version', default=0))
    if version < _MIN_VERSION:
        raise ValueError(
            f'{pcb_path}: format version {version} is older than KiCad 10 — '
            f'open the board in KiCad 10 and re-save it, then re-import')

    setup = _get(pcb, 'setup') or []
    aux = _get(setup, 'aux_axis_origin')
    if aux is None:
        import_log.log('kicad_pcb', 'setup', 'AUX_AXIS_ORIGIN absent',
                       'IR origin = KiCad page origin (top-left)')
        frame = _Frame(0, 0)
    else:
        frame = _Frame(aux[1], aux[2])

    copper_names = _copper_names(pcb)
    n_copper = len(copper_names)
    if n_copper < 2:
        raise ValueError(f'{pcb_path}: {n_copper} copper layer(s) — '
                         f'single-sided boards are rejected (stack canon)')

    layout = ET.SubElement(proj_el, 'layout', name=layout_name,
                           stack=_read_stack(pcb, n_copper))

    # legacy numbered net table (our own fresh export writes one; a v10
    # resave drops it entirely — nets live as name references on items)
    code_to_name = {}
    for it in _gets(pcb, 'net'):
        if len(it) > 2 and int(it[1]):
            code_to_name[int(it[1])] = str(it[2])

    signals = {}
    def _signal(name):
        if name.startswith('/'):
            raise ValueError(
                f'net {name!r}: hierarchical net names are not supported '
                f'yet (module boards; flat projects first)')
        if name not in signals:
            signals[name] = ET.SubElement(layout, 'signal', name=name)
        return signals[name]

    contactrefs = []               # (net, refdes, pad)
    n_zones = 0
    for item in pcb[1:]:
        if not isinstance(item, list) or not item:
            continue
        tag = item[0]
        if tag in ('segment', 'arc'):
            net = _net_name(item, code_to_name)
            if net is None:
                import_log.log('kicad_pcb', 'track', 'TRACK dropped',
                               'no net (code 0)')
                continue
            _emit_track(_signal(net), item, frame)
        elif tag == 'via':
            net = _net_name(item, code_to_name)
            if net is None:
                import_log.log('kicad_pcb', 'via', 'VIA dropped', 'no net')
                continue
            _emit_via(_signal(net), item, frame, n_copper)
        elif tag.startswith('gr_'):
            _emit_graphic(layout, item, frame)
        elif tag == 'footprint':
            refs = _emit_element(layout, item, frame, code_to_name,
                                 known_refdes)
            if refs:
                refdes = _fp_property(item, 'Reference')
                contactrefs += [(net, refdes, pad) for net, pad in refs]
        elif tag == 'zone':
            n_zones += 1
        elif tag in ('group', 'dimension', 'target'):
            import_log.log('kicad_pcb', tag, f'{tag.upper()} deferred',
                           'not carried yet')
    if n_zones:
        import_log.log('kicad_pcb', 'zone', 'ZONES deferred',
                       f'{n_zones} zone(s) not carried yet (pen-model '
                       f'inverse offset pending)')

    seen = set()
    for net, refdes, pad in contactrefs:
        if (refdes, pad) in seen:
            continue
        seen.add((refdes, pad))
        ET.SubElement(_signal(net), 'contactref', element=refdes, pad=pad)

    _adopt_schematic_net_names(layout, signals, proj_el)

    import_log.log('kicad_pcb', layout_name,
                   f'{len(layout.findall("element"))} element(s), '
                   f'{len(signals)} signal(s), {n_copper} copper layers')
    return layout
