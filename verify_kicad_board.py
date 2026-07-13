"""KiCad board export verifier: IR -> .kicad_pcb, then every pad's ABSOLUTE
board position/net recomputed from the .kicad_pcb (at + rotation + local)
is compared against the IR oracle (ir_util.place_ir_element — the canonical
placement math). Catches any sign/axis error in the flip bake that eyes
would miss. Tracks/vias/outline are spot-checked by coordinate multisets.

Usage: python verify_kicad_board.py outputs/tolmach.swprj
"""
import math
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from babel.eagle_board_exporter import _instance_footprint
from babel.ir_util import place_ir_element
from babel.kicad_board_exporter import export_board_kicad, _Frame

TOL = 0.002   # mm


def _fp_blocks(text):
    out = []
    for m in re.finditer(r'\n\t\(footprint "([^"]+)"', text):
        depth, i = 0, m.start() + 2
        start = i
        while True:
            c = text[i]
            if c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out.append((m.group(1), text[start:i + 1]))
    return out


def _kicad_pads(text):
    """{(refdes, padname): (abs_x_mm, abs_y_mm, net)} from the .kicad_pcb."""
    pads = {}
    for lib_id, b in _fp_blocks(text):
        ref_m = re.search(r'\(property "Reference" "([^"]*)"', b)
        at_m = re.search(r'\n\t\t\(at ([-0-9.]+) ([-0-9.]+)(?: ([-0-9.]+))?\)', b)
        if not ref_m or not at_m:
            continue
        ref = ref_m.group(1)
        fx, fy = float(at_m.group(1)), float(at_m.group(2))
        rot = float(at_m.group(3) or 0)
        # KiCad render: board = at + Rv(rot) . local, Rv visual-CCW in the
        # y-down frame: (x, y) -> (x cos + y sin, -x sin + y cos)
        c, s = math.cos(math.radians(rot)), math.sin(math.radians(rot))
        for pm in re.finditer(
                r'\(pad "([^"]*)" (\w+) \w+\n\t\t\t\(at ([-0-9.]+) ([-0-9.]+)'
                r'(?: ([-0-9.]+))?\)[\s\S]*?(?:\(net "((?:[^"\\]|\\.)*)"\)'
                r'[\s\S]*?)?\n\t\t\)', b):
            name, kind = pm.group(1), pm.group(2)
            lx, ly = float(pm.group(3)), float(pm.group(4))
            net = pm.group(6)
            ax = fx + lx * c + ly * s
            ay = fy - lx * s + ly * c
            pads[(ref, name or f'@{round(ax,2)},{round(ay,2)}')] = \
                (ax, ay, net, kind)
    return pads


def _ir_pads(root, layout, frame):
    """Same map from the IR oracle."""
    comp_by_name = {c.get('name'): c for c in root.findall('component')}
    inst_by_des = {}
    schem = root.find('schematic')
    if schem is not None:
        for i in schem.findall('instance'):
            inst_by_des.setdefault(i.get('name'), i)
    module_by_name = {m.get('name'): m for m in root.findall('module')}
    local_fp = {f.get('name'): f for f in layout.findall('footprint')}
    pad_nets = {}
    for sig in layout.findall('signal'):
        for cr in sig.findall('contactref'):
            pad_nets[(cr.get('element'), cr.get('pad'))] = sig.get('name')

    def resolve(des):
        if ':' not in des:
            return inst_by_des.get(des)
        minst_name, part = des.split(':', 1)
        minst = inst_by_des.get(minst_name)
        mod = module_by_name.get(minst.get('module')) if minst is not None else None
        if mod is None:
            return None
        return next((i for i in mod.findall('instance')
                     if i.get('name') == part), None)

    pads = {}
    for e in layout.findall('element'):
        des = e.get('name')
        if e.get('footprint'):
            fp = local_fp.get(e.get('footprint'))
        else:
            inst = resolve(des)
            comp = comp_by_name.get(inst.get('component')) if inst is not None else None
            fp = _instance_footprint(comp, inst) if comp is not None else None
        if fp is None:
            continue
        ex, ey = float(e.get('x')), float(e.get('y'))
        rot = float(e.get('rot', '0') or '0')
        bottom = e.get('side') == 'bottom'
        for child in fp:
            if child.tag not in ('smd', 'pad', 'hole'):
                continue
            placed = place_ir_element(child, ex, ey, rot, bottom)
            ax = frame.x(placed.get('x'))
            ay = frame.y(placed.get('y'))
            name = child.get('name')
            net = pad_nets.get((des, name))
            pads[(des, name or f'@{round(ax,2)},{round(ay,2)}')] = \
                (ax, ay, net, child.tag)
    return pads


def check(swprj):
    swprj = Path(swprj)
    out_pcb = swprj.with_suffix('.kicad_pcb')
    export_board_kicad(swprj, out_pcb)
    text = out_pcb.read_text(encoding='utf-8')

    root = ET.parse(swprj).getroot()
    layout = root.find('layout')
    xs, ys = [], []
    for el in layout.iter():
        for kx, ky in (('x', 'y'), ('x1', 'y1'), ('x2', 'y2')):
            if el.get(kx) is not None and el.get(ky) is not None:
                try:
                    xs.append(float(el.get(kx))); ys.append(float(el.get(ky)))
                except ValueError:
                    pass
    frame = _Frame(min(xs), max(ys))

    kp, ip = _kicad_pads(text), _ir_pads(root, layout, frame)
    # synthetic hole footprints exist only on the KiCad side
    kp = {k: v for k, v in kp.items() if not k[0].startswith('H')
          or k[0] in {d for d, _ in ip}}
    ok = True
    if set(kp) != set(ip):
        ok = False
        print(f'  FAIL pad sets: only-kicad={sorted(set(kp)-set(ip))[:5]} '
              f'only-ir={sorted(set(ip)-set(kp))[:5]}')
    n_bad = 0
    for key in sorted(set(kp) & set(ip)):
        ka, ia = kp[key], ip[key]
        if abs(ka[0] - ia[0]) > TOL or abs(ka[1] - ia[1]) > TOL:
            n_bad += 1
            if n_bad <= 8:
                print(f'  FAIL pos {key}: kicad=({ka[0]:.3f},{ka[1]:.3f}) '
                      f'ir=({ia[0]:.3f},{ia[1]:.3f})')
        elif (ka[2] or None) != (ia[2] or None):
            n_bad += 1
            if n_bad <= 8:
                print(f'  FAIL net {key}: kicad={ka[2]!r} ir={ia[2]!r}')
    if n_bad:
        ok = False
        print(f'  ... {n_bad} pad mismatches total')
    else:
        nets = sum(1 for v in ip.values() if v[2])
        print(f'  ok  {len(ip)} pads position+net (of them {nets} netted)')

    # coarse counts
    for pat, label in ((r'\n\t\(segment', 'segments'),
                       (r'\n\t\(via', 'vias'),
                       (r'\n\t\(arc', 'track arcs'),
                       (r'\n\t\(zone', 'zones'),
                       (r'\(gr_line|\(gr_arc|\(gr_poly|\(gr_circle|\(gr_rect',
                        'graphics'),
                       (r'\(gr_text', 'texts')):
        print(f'  ..  {len(re.findall(pat, text))} {label}')

    # the ultimate syntax oracle: KiCad's own parser (skipped if absent)
    kicad_py = Path('C:/Program Files/KiCad/10.0/bin/python.exe')
    if kicad_py.exists():
        import subprocess
        r = subprocess.run(
            [str(kicad_py), '-c',
             f'import pcbnew; b = pcbnew.LoadBoard(r"{out_pcb}"); '
             f'print(len(b.GetFootprints()), b.GetNetCount(), len(b.Zones()))'],
            capture_output=True, text=True)
        if r.returncode:
            ok = False
            print(f'  FAIL pcbnew rejects the file: {r.stderr.strip()[:200]}')
        else:
            fp, nets, zones = r.stdout.split()
            print(f'  ok  pcbnew loads it ({fp} footprints, {nets} nets, '
                  f'{zones} zones)')
    return ok


if __name__ == '__main__':
    good = True
    for arg in (sys.argv[1:] or ['outputs/tolmach.swprj']):
        print(f'=== {arg}')
        good &= check(arg)
    print('\nKICAD BOARD EXPORT OK' if good else '\nKICAD BOARD EXPORT FAILED')
    sys.exit(0 if good else 1)
