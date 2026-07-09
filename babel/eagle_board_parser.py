"""Eagle board (.brd) -> IR <layout> (ir_schema.md "Плата (Board IR)").

Vertical slice 1: free geometry only — the board's <plain> section (outline
on Dimension/120, silk texts, logos, holes) becomes direct children of
<layout>. Elements (<element>) and copper (<signal>/<via>/pours) are the
next slices; see decisions.md "Импорт платы Eagle — решения Этапа-0" and
the plan in progress.md.

Import is always PROJECT-scoped (.sch+.brd together, one <layout> per
import — Eagle can't have more than one board per schematic); this module
only builds the <layout> element, the pairing/validation against the
schematic lives in eagle_project_parser.
"""
import math
import re
import xml.etree.ElementTree as ET

from babel import import_log
from babel.eagle_parser import (convert_geometry, convert_geometry_mapped,
                                _pkg_layer, _um, fmt, parse_rot)


def _convert_element(e, layout, layout_name, pkg_placeholders=frozenset()):
    """Eagle <element> -> IR <element> (ir_schema.md "<element>"): placement
    (x/y/rot/side) + <text> placeholder overrides for smashed attributes.

    Deliberately NOT carried: library/package/value (identity is the REFDES;
    the shared schematic instance owns component binding and VALUE — pairing
    and validation live in eagle_project_parser), locked (editor UI state),
    smashed itself (an element with <text> children IS smashed).

    pkg_placeholders: placeholder names ('NAME'/'VALUE') whose >TEXT exists
    in this element's package — a smashed element that does NOT list such an
    attribute (deleted after smashing) or lists it display="off" has it
    HIDDEN in Eagle, which IR records as an explicit <text hidden="yes">
    suppression override (an absent override would un-hide the footprint's
    placeholder — caught twice on luminoso.brd: display="off", then CON2's
    deleted NAME rendering bottom-blue near R17).
    """
    el = ET.SubElement(layout, 'element')
    el.set('name', e.get('name'))
    el.set('x', _um(e.get('x'))); el.set('y', _um(e.get('y')))
    rot, mirror = parse_rot(e.get('rot'))
    if rot:
        el.set('rot', fmt(rot))
    if mirror:
        el.set('side', 'bottom')

    smashed = e.get('smashed') == 'yes'
    shown = set()

    ex, ey = float(e.get('x')), float(e.get('y'))
    for a in e.findall('attribute'):
        aname = a.get('name')
        if a.get('display') == 'off':
            # no placeholder is created (ir_schema.md); suppression of the
            # footprint's own >NAME/>VALUE is handled uniformly below
            continue
        shown.add(aname)
        t = ET.SubElement(el, 'text')
        t.text = '>' + aname
        # local coords in the element's unrotated system (Eagle stores
        # attribute x/y/rot as ABSOLUTE board values, like KiCad pad angles)
        dx, dy = float(a.get('x', ex)) - ex, float(a.get('y', ey)) - ey
        arot, amirror = parse_rot(a.get('rot'))
        r = math.radians(rot)
        lx = dx * math.cos(r) + dy * math.sin(r)
        ly = -dx * math.sin(r) + dy * math.cos(r)
        lrot = (arot - rot) % 360
        if mirror:      # undo placement mirror: local x sign + angle sense
            lx = -lx
            lrot = (-lrot) % 360
        t.set('x', str(round(lx * 1000))); t.set('y', str(round(ly * 1000)))
        t.set('size', _um(a.get('size', '1.778')))
        t.set('rot', fmt(lrot))
        t.set('align', a.get('align', 'bottom-left'))
        if a.get('font') == 'vector':
            t.set('font', 'vector')
        if a.get('ratio'):
            t.set('ratio', a.get('ratio'))
        ir_layer = _pkg_layer(int(a.get('layer')))
        if ir_layer:
            t.set('layer', ir_layer)

    if smashed:
        for ph in pkg_placeholders - shown:
            t = ET.SubElement(el, 'text')
            t.text = '>' + ph
            t.set('hidden', 'yes')


def _parse_layer_setup(board, brd_path):
    """Eagle designrules layerSetup ('(1+2*15+16)') -> ordered list of Eagle
    copper layer numbers, top first. THE source of the stack: Eagle numbers
    inner layers from BOTH ends (a 4-layer board uses 1,2,15,16), so inner
    IR numbers come from stack ORDER, not from the Eagle number. Blind/
    buried via spans ('[t:...:b]' brackets) -> hard reject (decisions.md
    "VIA — только сквозные")."""
    setup = '(1*16)'
    dr = board.find('designrules')
    if dr is not None:
        for p in dr.findall('param'):
            if p.get('name') == 'layerSetup':
                setup = p.get('value', setup)
    if '[' in setup or ':' in setup:
        raise ValueError(
            f'{brd_path}: layerSetup {setup!r} declares blind/buried via '
            f'spans — IR expresses only through vias (hard reject)')
    stack = [int(tok) for tok in re.findall(r'\d+', setup)]
    if not stack:
        raise ValueError(f'{brd_path}: unparseable layerSetup {setup!r}')
    return stack


def _copper_map(stack):
    """Ordered Eagle copper stack -> {eagle_n: IR layer string}: top='1',
    bottom='-1', inner '2'..'N-1' top-down (ir_schema.md "Слои платы")."""
    m = {stack[0]: '1', stack[-1]: '-1'}
    for i, n in enumerate(stack[1:-1], start=2):
        m[n] = str(i)
    return m


def _convert_signal(s, layout, layout_name, copper_map, stack, brd_path):
    """Eagle <signal> -> IR <signal>: contactrefs, copper tracks (wire ->
    line/arc), through vias, pour polygons (contour only).

    Deliberately NOT carried: class (net classes are schematic truth, the
    project pairing validates .sch vs .brd nets), airwires (layer 19 —
    connectivity already lives in contactref, Eagle recomputes ratsnest).
    """
    sig = ET.SubElement(layout, 'signal')
    sig.set('name', s.get('name'))
    airwires = 0

    for c in s:
        tag = c.tag
        if tag == 'contactref':
            cr = ET.SubElement(sig, 'contactref')
            cr.set('element', c.get('element'))
            cr.set('pad', c.get('pad'))

        elif tag == 'wire':
            eagle_n = int(c.get('layer'))
            if eagle_n == 19:
                airwires += 1
                continue
            ir_layer = copper_map.get(eagle_n)
            if ir_layer is None:
                import_log.log(layout_name, s.get('name'),
                               'SIGNAL_WIRE dropped, non-copper layer',
                               str(eagle_n))
                continue
            convert_geometry(c, sig, ir_layer)

        elif tag == 'via':
            extent = c.get('extent', '')
            span = [int(t) for t in extent.split('-')] if extent else []
            if span != [stack[0], stack[-1]]:
                raise ValueError(
                    f'{brd_path}: via at ({c.get("x")},{c.get("y")}) has '
                    f'extent {extent!r} (blind/buried) — IR expresses only '
                    f'through vias (hard reject)')
            v = ET.SubElement(sig, 'via')
            v.set('x', _um(c.get('x'))); v.set('y', _um(c.get('y')))
            v.set('drill', _um(c.get('drill')))
            if c.get('diameter'):
                v.set('diameter', _um(c.get('diameter')))
            if c.get('shape') in ('square',):
                v.set('shape', c.get('shape'))

        elif tag == 'polygon':
            eagle_n = int(c.get('layer'))
            ir_layer = copper_map.get(eagle_n)
            if ir_layer is None:
                import_log.log(layout_name, s.get('name'),
                               'SIGNAL_POLYGON dropped, non-copper layer',
                               str(eagle_n))
                continue
            if c.get('pour') == 'cutout':
                ir_layer = '!' + ir_layer     # anti-copper of that layer
            convert_geometry(c, sig, ir_layer)
            pg = sig[-1]
            # pour parameters beyond the shared geometry conversion
            if c.get('pour') == 'hatch':
                # fill percent = stroke width / hatch pitch (ir_schema.md
                # "fill — единый процент")
                spacing = float(c.get('spacing', '1.27'))
                w = float(c.get('width', '0'))
                pg.set('fill', str(max(1, min(100, round(w / spacing * 100)))))
            if c.get('rank'):
                pg.set('rank', c.get('rank'))
            if c.get('thermals') == 'no':
                pg.set('thermals', '0')
            if c.get('isolate'):
                pg.set('clearance', _um(c.get('isolate')))

    if airwires:
        import_log.log(layout_name, s.get('name'), 'AIRWIRES dropped,',
                       f'{airwires} (connectivity lives in contactref)')


def convert_board(brd_path, layout_name='main'):
    """Parse one .brd file -> IR <layout> element (slices 1-2: <plain> +
    <element>)."""
    root = ET.parse(brd_path).getroot()
    board = root.find('.//board')
    if board is None:
        raise ValueError(f'{brd_path}: no <board> element')

    # which packages carry >NAME/>VALUE texts — needed to record smashed
    # elements' hidden placeholders as explicit suppression overrides
    pkg_placeholders = {}
    libraries = board.find('libraries')
    if libraries is not None:
        for lib in libraries:
            pkgs = lib.find('packages')
            for pkg in (pkgs if pkgs is not None else ()):
                phs = {t.text.strip().lstrip('>').upper()
                       for t in pkg.findall('text')
                       if (t.text or '').strip().startswith('>')}
                pkg_placeholders[(lib.get('name'), pkg.get('name'))] = \
                    phs & {'NAME', 'VALUE'}

    stack = _parse_layer_setup(board, brd_path)
    copper_map = _copper_map(stack)

    layout = ET.Element('layout', name=layout_name)
    layout.set('copper', str(len(stack)))

    plain = board.find('plain')
    if plain is not None:
        for child in plain:
            if not convert_geometry_mapped(child, layout):
                eagle_layer = child.get('layer')
                # <dimension> (measurement annotations) and friends — no IR
                # model yet, never silently.
                import_log.log(layout_name, child.tag, 'PLAIN_UNSUPPORTED dropped,',
                               f'layer={eagle_layer}')

    elements = board.find('elements')
    if elements is not None:
        for e in elements:
            _convert_element(e, layout, layout_name,
                             pkg_placeholders.get(
                                 (e.get('library'), e.get('package')),
                                 frozenset()))

    signals = board.find('signals')
    if signals is not None:
        for s in signals:
            _convert_signal(s, layout, layout_name, copper_map, stack, brd_path)

    return layout
