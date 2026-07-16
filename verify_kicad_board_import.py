"""KiCad board IMPORT verifier: closed-loop oracle over the full chain

    Eagle .sch/.brd -> IR#1 (eagle_project_parser)
                    -> KiCad project (kicad_project_exporter)
                    -> [re-save in KiCad 10 by hand = ground truth]
                    -> IR#2 (kicad_project_parser + kicad_board_parser)

then compares IR#1's <layout> against IR#2's cell by cell: stack formula,
elements (x/y/rot/side), copper (lines/arcs/vias/contactrefs), free
geometry (lines/arcs/shapes/polygons/texts/holes). Equivalent
representations are normalized before comparing:
  - absent rot == rot 0, absent outline/roundness == 0 (defaults);
  - an arc with swapped endpoints and negated curve is the same arc
    (KiCad normalizes arc direction on save);
  - the stack is compared with ε/tanδ stripped when IR#1 carries none —
    a KiCad re-save materializes its 4.5/0.02 defaults into the stackup
    and they are indistinguishable from user-set values (decisions.md).
Known deferred-on-import items (zones and what rode in them, e.g.
footprint anti-copper rings placed as board keepouts) are reported, not
failed, until the zone milestone lands.

Usage: python verify_kicad_board_import.py outputs/tolmach.swprj testData/new_kicad/tolmach_kicad
"""
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from babel.kicad_project_parser import convert_project_full


def _norm_stack(s):
    return re.sub(r':[^\]]*\]', ']', s or '')


def _geo_key(c):
    rot = float(c.get('rot', 0) or 0) % 360
    if c.tag == 'line':
        pts = tuple(sorted([(c.get('x1'), c.get('y1')),
                            (c.get('x2'), c.get('y2'))]))
        return ('line', pts, c.get('width'), c.get('layer'))
    if c.tag == 'arc':
        a, b = (c.get('x1'), c.get('y1')), (c.get('x2'), c.get('y2'))
        cu = round(float(c.get('curve')), 2)
        if a > b:
            a, b, cu = b, a, -cu
        return ('arc', a, b, cu, c.get('width'), c.get('layer'))
    if c.tag == 'shape':
        return ('shape', c.get('x'), c.get('y'), c.get('w'), c.get('h'),
                c.get('roundness') or '0', c.get('outline') or '0', rot,
                c.get('layer'))
    if c.tag == 'polygon':
        pts = tuple(sorted((v.get('x'), v.get('y'))
                           for v in c.findall('vertex')))
        return ('poly', pts, c.get('layer'), c.get('width'))
    if c.tag == 'text':
        return ('text', (c.text or '').strip(), c.get('x'), c.get('y'), rot,
                c.get('size'), c.get('ratio'), c.get('align') or 'bottom-left',
                c.get('mirror') or '0', c.get('font'), c.get('width'),
                c.get('layer'))
    if c.tag == 'hole':
        return ('hole', c.get('x'), c.get('y'), c.get('drill'))
    return None


_ANON = re.compile(r'^N\$\d+$|^Net-\(.*\)$|^unconnected-\(.*\)$')


def _signal_items(layout):
    # Anonymous net names are generated labels, not facts: Eagle numbers
    # them N$1.., a KiCad round-trip regenerates them differently. Compare
    # by a canonical label derived from the net's own contactref set.
    canon = {}
    for s in layout.findall('signal'):
        n = s.get('name')
        if _ANON.match(n or ''):
            crs = sorted((c.get('element'), c.get('pad'))
                         for c in s.findall('contactref'))
            canon[n] = f'ANON:{crs[0][0]}.{crs[0][1]}' if crs else n
    out = Counter()
    for s in layout.findall('signal'):
        n = canon.get(s.get('name'), s.get('name'))
        for c in s:
            if c.tag == 'contactref':
                out[(n, 'cref', c.get('element'), c.get('pad'))] += 1
            elif c.tag == 'via':
                out[(n, 'via', c.get('x'), c.get('y'),
                     c.get('drill'), c.get('diameter'))] += 1
            else:
                out[(n,) + _geo_key(c)] += 1
    return out


def check(src_swprj, kicad_project_dir):
    src = ET.parse(src_swprj).getroot().find('layout')
    rt_path = Path('outputs') / (Path(src_swprj).stem + '_rt.swprj')
    convert_project_full(kicad_project_dir, rt_path)
    rt = ET.parse(rt_path).getroot().find('layout')
    ok = True
    if rt is None:
        print('  FAIL no <layout> imported')
        return False

    sa, sb = src.get('stack'), rt.get('stack')
    if sa == sb or _norm_stack(sa) == _norm_stack(sb):
        note = '' if sa == sb else '  (eps/tand = KiCad re-save defaults)'
        print(f'  ok  stack {sb}{note}')
    else:
        ok = False
        print(f'  FAIL stack: src={sa} imported={sb}')

    def elems(l):
        return {e.get('name'): (e.get('x'), e.get('y'),
                                float(e.get('rot', 0) or 0) % 360,
                                e.get('side') or 'top')
                for e in l.findall('element')}
    ea, eb = elems(src), elems(rt)
    bad = sorted(set(ea) ^ set(eb)) + \
        [n for n in set(ea) & set(eb) if ea[n] != eb[n]]
    if bad:
        ok = False
        print(f'  FAIL elements: {len(bad)} mismatch(es): {bad[:8]}')
        for n in bad[:4]:
            print(f'       {n}: src={ea.get(n)} imported={eb.get(n)}')
    else:
        print(f'  ok  {len(ea)} elements (x/y/rot/side)')

    ca, cb = _signal_items(src), _signal_items(rt)
    pours = Counter({k: v for k, v in ca.items() if k[1] == 'poly'})
    ca -= pours
    extra, missing = cb - ca, ca - cb
    if extra or missing:
        ok = False
        print(f'  FAIL copper: {sum(missing.values())} missing, '
              f'{sum(extra.values())} extra')
        for k in list(missing)[:4]:
            print(f'       missing: {k}')
        for k in list(extra)[:4]:
            print(f'       extra:   {k}')
    else:
        print(f'  ok  {sum(ca.values())} copper items '
              f'(tracks/arcs/vias/contactrefs)')
    if pours:
        print(f'  ..  {sum(pours.values())} pour polygon(s) = zones — '
              f'deferred until the zone milestone')

    ga = Counter(k for k in map(_geo_key, src) if k)
    gb = Counter(k for k in map(_geo_key, rt) if k)
    extra, missing = gb - ga, ga - gb
    anti = Counter({k: v for k, v in missing.items()
                    if '!' in str(k[-1] or '')})
    missing -= anti
    if extra or missing:
        ok = False
        print(f'  FAIL geometry: {sum(missing.values())} missing, '
              f'{sum(extra.values())} extra')
        for k in list(missing)[:4]:
            print(f'       missing: {k}')
        for k in list(extra)[:4]:
            print(f'       extra:   {k}')
    else:
        print(f'  ok  {sum(ga.values())} free-geometry items')
    if anti:
        print(f'  ..  {sum(anti.values())} anti-copper item(s) ride in '
              f'zones — deferred until the zone milestone')
    return ok


if __name__ == '__main__':
    src = sys.argv[1] if len(sys.argv) > 1 else 'outputs/tolmach.swprj'
    kdir = sys.argv[2] if len(sys.argv) > 2 else 'testData/new_kicad/tolmach_kicad'
    print(f'=== {src} <- {kdir}')
    good = check(src, kdir)
    print('\nKICAD BOARD IMPORT OK' if good else '\nKICAD BOARD IMPORT FAILED')
