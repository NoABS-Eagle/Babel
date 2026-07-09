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
import xml.etree.ElementTree as ET

from babel import import_log
from babel.eagle_parser import convert_geometry_mapped, _pkg_layer, _um, fmt, parse_rot


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
        ir_layer = _pkg_layer(int(a.get('layer')))
        if ir_layer:
            t.set('layer', ir_layer)

    if smashed:
        for ph in pkg_placeholders - shown:
            t = ET.SubElement(el, 'text')
            t.text = '>' + ph
            t.set('hidden', 'yes')


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

    layout = ET.Element('layout', name=layout_name)
    # copper stack size: fixed 2 until the <signal> slice lands (then it is
    # derived from the copper layers actually used, ir_schema.md `copper`).
    layout.set('copper', '2')

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

    return layout
