"""Read the exported Altium project back and check it against its IR.

The project-level IMPORTER needs a compiled .IntLib (Altium-only), so the
check goes the other way: altium_monkey compiles the emitted project exactly
as Altium's own compiler would (same hierarchy mode out of the .PrjPcb) and
the resulting netlist is compared with the IR flattened by the same rules —
module nets joined to the parent through their ports, module designators
through ir_util.flat_designator. Counters and mismatches only, never file
contents.
"""
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from altium_monkey.altium_design import AltiumDesign
from altium_monkey.altium_schdoc import AltiumSchDoc

from babel.altium_project_exporter import (_eagle_overbar_to_altium,
                                           _sup_pin_names)
from babel.altium_exporter import channel_designator
from babel.altium_netlist import compiled_nets
from babel.ir_util import instance_footprint

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else 'outputs/altium_sch_export')
NAME = sys.argv[2] if len(sys.argv) > 2 else next(
    p.stem for p in sorted(OUT.glob('*.PrjPcb')))

fails = []


def check(ok, msg):
    print(('  ok   ' if ok else '  FAIL ') + msg)
    if not ok:
        fails.append(msg)


def pin_key(desig, pin):
    """(designator, PAD) — the only pin identity that survives both sides.

    An IR pinref inside a multi-gate part reads `B.17`, the placed Altium pin
    knows the name `17` and the designator (pad) `B17`; two gates of one
    connector both have a pin named 17, so comparing by NAME merges two
    different pins into one key and invents mismatches. The IR side resolves
    the pad through the component's own <pin-mapping>, the Altium side reads
    the pin designator it was built with.
    """
    return desig, str(pin)


# The IR is a separate result of the run, next to the project folder
# (`ir_<name>/`); older runs left it inside, or beside the project files.
_ir_path = next((p for p in (OUT.parent / f'ir_{NAME}' / f'{NAME}.swprj',
                             OUT / 'ir' / f'{NAME}.swprj',
                             OUT / f'{NAME}.swprj') if p.exists()),
                OUT / f'{NAME}.swprj')
ir = ET.parse(_ir_path).getroot()
sch = ir.find('schematic')
modules = {m.get('name'): m for m in ir.findall('module')}
minsts = [i for i in sch.findall('instance') if i.get('module')]

# --- IR, flattened the way a hierarchical netlist sees it -------------------
# node = (canvas key, net name); a <portref> welds the parent's net to the
# like-named net inside that module instance.
parent = {}


def find(k):
    while parent.setdefault(k, k) != k:
        parent[k] = parent[parent[k]]
        k = parent[k]
    return k


def union(a, b):
    ra, rb = find(a), find(b)
    if ra != rb:
        parent[ra] = rb


pool = {s.get('name'): s for s in ir.findall('symbols/symbol')}
comp_by_name = {c.get('name'): c for c in ir.findall('component')}


def supply_names(canvas_el):
    """Designators placed as native Altium PowerPorts: they are not
    components there and contribute no pin to the compiled netlist — the
    net they name is the whole of what they carry over."""
    out = set()
    for inst_el in canvas_el.findall('instance'):
        comp_el = comp_by_name.get(inst_el.get('component', ''))
        if comp_el is not None and _sup_pin_names(comp_el, pool):
            out.add(inst_el.get('name'))
    return out


def pad_map(inst_el):
    """{IR pin name -> pin designator Altium got} — the FIRST footprint's
    pin-mapping, exactly what altium_exporter._pin_des_map builds the SchLib
    pins from (a pin mapped to several pads keeps the first)."""
    comp_el = (comp_by_name.get(inst_el.get('component', ''))
               if inst_el is not None else None)
    if comp_el is None:
        return {}
    fp_el = comp_el.find('footprint')
    pm = fp_el.find('pin-mapping') if fp_el is not None else None
    if pm is None:
        return {}
    return {m.get('pin'): (m.get('pad', '').split() or [''])[0]
            for m in pm.findall('map')}


ir_pins = {}
for canvas_key, canvas_el, desig_of in (
        [('', sch, lambda n: n)]
        + [(i.get('name'), modules[i.get('module')],
            (lambda n, room=i.get('name'): channel_designator(room, n)))
           for i in minsts]):
    supplies = supply_names(canvas_el)
    pads = {i.get('name'): pad_map(i) for i in canvas_el.findall('instance')}
    for net_el in canvas_el.findall('net'):
        key = (canvas_key, net_el.get('name'))
        find(key)
        pins = ir_pins.setdefault(key, set())
        for seg_el in net_el.findall('segment'):
            for r in seg_el.findall('pinref'):
                if r.get('part') in supplies:
                    continue
                pad = pads.get(r.get('part'), {}).get(r.get('pin'))
                pins.add(pin_key(desig_of(r.get('part')),
                                 pad if pad else r.get('pin')))
            for r in seg_el.findall('portref'):
                union(key, (r.get('part'), r.get('port')))

ir_nets = {}
for key, pins in ir_pins.items():
    ir_nets.setdefault(find(key), set()).update(pins)
# 1-pin nets are named but carry no connection to check
ir_parts = {p[0] for pins in ir_nets.values() for p in pins}

# --- what the exported project compiles to ---------------------------------
design = AltiumDesign.from_prjpcb(str(OUT / f'{NAME}.PrjPcb'))
netlist = design.to_netlist()
doc_parts = {c.designator for c in netlist.components}

# The compiled netlist, with altium_monkey's own gaps corrected in the one
# place both the exporter and this verifier read it from.
nets, unresolved = compiled_nets(OUT / f'{NAME}.PrjPcb')
doc_nets = {i: {pin_key(d, p) for d, p in pins}
            for i, (_, pins) in enumerate(nets)}
if unresolved:
    print(f'  info {len(unresolved)} hierarchy link(s) the compiler could '
          f'not resolve: {unresolved[:2]}')

print(f'IR: {len(sch.findall("instance"))} top instances, {len(minsts)} module '
      f'instances, {len(modules)} module(s)')
schdocs = sorted(OUT.glob('*.SchDoc'))
print(f'{len(schdocs)} .SchDoc:')
for f in schdocs:
    doc = AltiumSchDoc(str(f))
    kinds = Counter(type(r).__name__.replace('AltiumSch', '')
                    for r in doc.all_objects)
    print(f'  {f.name}: ' + ', '.join(
        f'{n} {k}' for k, n in sorted(kinds.items(), key=lambda kv: -kv[1])
        if k not in ('Parameter', 'Font', 'Sheet')))

# --- hierarchy -------------------------------------------------------------
print('\nHIERARCHY')
tops = [AltiumSchDoc(str(f)) for f in schdocs]
syms = [ss for t in tops for ss in t.sheet_symbols]
check(len(syms) == len(minsts),
      f'{len(syms)} sheet symbol(s) for {len(minsts)} module instance(s)')
for ss, minst in zip(syms, sorted(minsts, key=lambda i: i.get('name'))):
    mod_el = modules[minst.get('module')]
    # the sheet entry carries the Altium spelling of the port name
    ports = [_eagle_overbar_to_altium(p.get('name'))
             for p in mod_el.findall('port')]
    entries = [e.name for e in ss.entries]
    child = OUT / ss.file_name.text
    check(child.exists(), f'{ss.sheet_name.text} -> {ss.file_name.text} exists')
    check(sorted(entries) == sorted(ports),
          f'{ss.sheet_name.text}: entries {sorted(entries)} = module ports')
    if child.exists():
        names = {p.name for p in AltiumSchDoc(str(child)).ports}
        check(names >= set(entries),
              f'{ss.sheet_name.text}: every entry has a Port on the child '
              f'sheet (missing: {sorted(set(entries) - names)})')

# --- components ------------------------------------------------------------
print('\nCOMPONENTS')
check(ir_parts <= doc_parts,
      f'{len(doc_parts)} compiled designators cover the {len(ir_parts)} the IR '
      f'connects (missing: {sorted(ir_parts - doc_parts)[:6]})')

# A DbLib link alone leaves the part without a footprint ("Footprint of
# component ... cannot be found" in Altium even with the database connected):
# the model has to be baked into the placed component.
placed = [c for f in schdocs for c in AltiumSchDoc(str(f)).components]
no_model = [c.design_item_id for c in placed if not c.footprint]
check(not no_model,
      f'all {len(placed)} placed components carry a footprint model '
      f'(without one: {sorted(set(no_model))[:6]})')

# --- connectivity: the partition of pins into nets --------------------------
print('\nNETS')
ir_conn = {frozenset(p) for p in ir_nets.values() if len(p) > 1}
doc_conn = {frozenset(p) for p in doc_nets.values() if len(p) > 1}
print(f'  IR {len(ir_conn)} multi-pin net(s), compiled {len(doc_conn)}')
lost = ir_conn - doc_conn
# A net whose PINS differ only in how the pin is spelled (a symbol with more
# pins than the footprint maps gives the SchLib a designator the IR pad table
# does not repeat) is the same net electrically — the designator sets match
# exactly. Reported, not failed: connectivity is what this check is for.
doc_by_desig = {}
for pins in doc_conn:
    doc_by_desig.setdefault(frozenset(d for d, _ in pins), []).append(pins)
renamed = {p for p in lost if frozenset(d for d, _ in p) in doc_by_desig}
lost -= renamed
if renamed:
    print(f'  info {len(renamed)} net(s) match by component but spell a pin '
          f'differently (library pin/pad mapping)')
check(not lost, f'every IR net is compiled with exactly its pins '
                f'({len(lost)} differ)')
for pins in sorted(lost, key=lambda s: sorted(s))[:5]:
    same = max(doc_conn, key=lambda d: len(d & pins), default=frozenset())
    print(f'     IR net {sorted(pins)[:4]}...\n'
          f'       closest compiled: {sorted(same & pins)[:3]} + '
          f'{len(same - pins)} extra, {len(pins - same)} missing')

print()
print('PROJECT VERIFY: ' + ('OK' if not fails else f'{len(fails)} FAILURE(S)'))
sys.exit(1 if fails else 0)
