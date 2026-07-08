"""Round-trip linter test for the IR->KiCad project exporter.

source zip -> IR#1 -> KiCad project (our exporter) -> IR#2 (our importer
again, on our own output) -> structural compare IR#1 vs IR#2 + the
no-global_label guarantee grep. Usage:

    python roundtrip_kicad.py [testData/multichannel.zip ...]
"""
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from babel.kicad_project_parser import convert_project_full
from babel.kicad_project_exporter import export_project

FAIL = []


def _check(what, a, b):
    if a == b:
        print(f'  ok  {what}')
    else:
        FAIL.append(what)
        print(f'  FAIL {what}:\n    IR1: {a}\n    IR2: {b}')


def _net_summary(canvas_el):
    # Auto-names (N$k) are renumbered by the importer per run — identity of
    # an autoname is meaningless, so they compare as one bucket; structure
    # (class, segments, refs, widths) still distinguishes them.
    return sorted(('N$*' if re.fullmatch(r'N\$\d+', n.get('name') or '') else n.get('name'),
                   n.get('class') or '',
                   len(n.findall('segment')),
                   sum(len(s.findall('pinref')) + len(s.findall('portref'))
                       for s in n.findall('segment')),
                   sorted(l.get('width') for s in n.findall('segment')
                          for l in s.findall('line')))
                  for n in canvas_el.findall('net'))


def _inst_summary(canvas_el):
    out = []
    for i in canvas_el.findall('instance'):
        if 'Frame' in (i.get('component') or ''):
            continue   # frame designators are per-page counters, not identity
        out.append((i.get('name'), i.get('component') or i.get('module'),
                    i.get('gate'), i.get('footprint'),
                    i.get('rot'), i.get('mirror'),
                    i.get('populate')))
    return sorted(out)


def compare(ir1_path, ir2_path):
    a = ET.parse(ir1_path).getroot()
    b = ET.parse(ir2_path).getroot()

    _check('component names',
           sorted(c.get('name') for c in a.findall('component')
                  if not c.get('name', '').startswith('Frame_')),
           sorted(c.get('name') for c in b.findall('component')
                  if not c.get('name', '').startswith('Frame_')))

    ca, cb = a.find('classes'), b.find('classes')
    _check('net classes',
           sorted(dict(c.attrib).items() for c in ca) if ca is not None else [],
           sorted(dict(c.attrib).items() for c in cb) if cb is not None else [])

    _check('module names',
           sorted(m.get('name') for m in a.findall('module')),
           sorted(m.get('name') for m in b.findall('module')))
    for ma in a.findall('module'):
        mb = b.find(f'module[@name="{ma.get("name")}"]')
        if mb is None:
            continue
        stem = ma.get('name')

        def _ports(mod_el):
            # direction pwr -> pas: KiCad sheet pins have no power
            # connectionType at all — the exporter degrades pwr to passive
            # (logged), so the round-trip legitimately comes back as pas.
            return sorted(
                (p.get('name'),
                 'pas' if p.get('direction') == 'pwr' else p.get('direction'),
                 p.get('side'), p.get('coord'))
                for p in mod_el.findall('port'))

        _check(f'module {stem}: ports', _ports(ma), _ports(mb))
        _check(f'module {stem}: instances', _inst_summary(ma), _inst_summary(mb))
        _check(f'module {stem}: nets', _net_summary(ma), _net_summary(mb))

    sa, sb = a.find('schematic'), b.find('schematic')
    _check('top instances', _inst_summary(sa), _inst_summary(sb))
    _check('top nets', _net_summary(sa), _net_summary(sb))


def run(src_zip, out_root):
    name = Path(src_zip).stem
    print(f'== {name} ==')
    ir1 = out_root / f'{name}.swprj'
    kdir = out_root / f'{name}_kicad'
    ir2 = out_root / f'{name}_rt.swprj'
    convert_project_full(src_zip, ir1)
    export_project(ir1, kdir)

    # THE LABEL RULE (decisions.md, revised): no global_label IN MODULES,
    # no local label OUTSIDE modules. Module files are the ones referenced
    # by a Sheetfile property; everything else is a top-level page.
    texts = {sch: sch.read_text(encoding='utf-8')
             for sch in kdir.glob('*.kicad_sch')}
    module_files = set()
    for text in texts.values():
        module_files.update(re.findall(r'\(property "Sheetfile" "([^"]+)"', text))
    for sch, text in texts.items():
        if sch.name in module_files:
            if re.search(r'\(global_label', text):
                FAIL.append(f'{sch.name}: global_label inside a module')
                print(f'  FAIL {sch.name}: global_label inside a module')
        else:
            if re.search(r'^\t\(label ', text, re.M):
                FAIL.append(f'{sch.name}: local label on a top-level page')
                print(f'  FAIL {sch.name}: local label on a top-level page')
    print('  ok  label rule (no globals in modules, no locals outside)')

    convert_project_full(kdir, ir2)
    compare(ir1, ir2)


if __name__ == '__main__':
    out_root = Path('outputs')
    out_root.mkdir(exist_ok=True)
    srcs = sys.argv[1:] or ['testData/multichannel.zip', 'testData/vimdrones.zip']
    for src in srcs:
        run(src, out_root)
    print()
    if FAIL:
        print(f'ROUND-TRIP FAILED: {len(FAIL)} mismatch(es)')
        sys.exit(1)
    print('ROUND-TRIP OK')
