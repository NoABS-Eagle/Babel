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


def _convert_element(e, layout, layout_name):
    """Eagle <element> -> IR <element> (ir_schema.md "<element>"): placement
    (x/y/rot/side) + <text> placeholder overrides for smashed attributes.

    Deliberately NOT carried: library/package/value (identity is the REFDES;
    the shared schematic instance owns component binding and VALUE — pairing
    and validation live in eagle_project_parser), locked (editor UI state),
    smashed itself (an element with <text> children IS smashed).
    """
    el = ET.SubElement(layout, 'element')
    el.set('name', e.get('name'))
    el.set('x', _um(e.get('x'))); el.set('y', _um(e.get('y')))
    rot, mirror = parse_rot(e.get('rot'))
    if rot:
        el.set('rot', fmt(rot))
    if mirror:
        el.set('side', 'bottom')

    ex, ey = float(e.get('x')), float(e.get('y'))
    for a in e.findall('attribute'):
        aname = a.get('name')
        hidden = a.get('display') == 'off'
        if hidden and aname not in ('NAME', 'VALUE'):
            # no footprint placeholder to suppress -> nothing to record
            # (ir_schema.md: display="off" creates no placeholder)
            continue
        t = ET.SubElement(el, 'text')
        t.text = '>' + aname
        if hidden:
            # the footprint DOES have >NAME/>VALUE — an absent override
            # would un-hide it; explicit suppression override
            t.set('hidden', 'yes')
            continue
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


def convert_board(brd_path, layout_name='main'):
    """Parse one .brd file -> IR <layout> element (slice 1: <plain> only)."""
    root = ET.parse(brd_path).getroot()
    board = root.find('.//board')
    if board is None:
        raise ValueError(f'{brd_path}: no <board> element')

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
            _convert_element(e, layout, layout_name)

    return layout
