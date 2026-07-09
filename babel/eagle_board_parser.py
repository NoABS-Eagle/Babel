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
import xml.etree.ElementTree as ET

from babel import import_log
from babel.eagle_parser import convert_geometry_mapped


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

    return layout
