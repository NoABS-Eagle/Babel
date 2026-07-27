"""Structural check of the exported .SchDoc, read back with altium_monkey.

The project-level importer needs a compiled .IntLib (Altium-only), so this
verifies the emitted document directly instead. Compact counters only.
"""
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from altium_monkey.altium_schdoc import AltiumSchDoc

OUT = Path('outputs/altium_sch_export')

doc = AltiumSchDoc(str(OUT / 'tolmach.SchDoc'))
kinds = Counter(type(r).__name__ for r in doc.all_objects)
for name, n in sorted(kinds.items(), key=lambda kv: -kv[1]):
    print(f'  {n:5d}  {name}')

root = ET.parse(OUT / 'tolmach.swprj').getroot()
sch = root.find('schematic')
insts = sch.findall('instance')
print(f'\nIR: {len(insts)} instances, {len(sch.findall("net"))} nets')

des_ir = {i.get('name') for i in insts}
des_doc = {d.text for d in doc.designators}
pp_names = {p.text for p in doc.power_ports}
print(f'designators: IR={len(des_ir)} SchDoc={len(des_doc)}')
lost, extra = sorted(des_ir - des_doc), sorted(des_doc - des_ir)
if lost:
    print('   only in IR   :', ', '.join(lost[:20]))
if extra:
    print('   only in SchDoc:', ', '.join(extra[:20]))

# --- connectivity: IR nets vs the netlist Altium will compile from the doc
ir_net = {}
for n in sch.findall('net'):
    pins = set()
    for seg in n.findall('segment'):
        for r in seg.findall('pinref'):
            pins.add((r.get('part'), r.get('pin')))
    ir_net[n.get('name')] = pins

# Protel netlist "Wire List": "[00001] NETNAME" then "DESIG PIN# PINNAME ..."
doc_net = {}
cur = None
for line in doc.to_netlist().splitlines():
    m = re.match(r'\[\d+\]\s+(\S+)', line)
    if m:
        cur = m.group(1)
        doc_net[cur] = set()
        continue
    if cur and line.startswith(' '):
        f = line.split()
        if len(f) >= 2:
            doc_net[cur].add((f[0], f[1]))

print(f'\nnets: IR={len(ir_net)} SchDoc={len(doc_net)}')
lost, extra = sorted(set(ir_net) - set(doc_net)), sorted(set(doc_net) - set(ir_net))
if lost:
    print('   only in IR    :', ', '.join(lost[:20]))
if extra:
    print('   only in SchDoc:', ', '.join(extra[:20]))
bad = [n for n in set(ir_net) & set(doc_net) if ir_net[n] != doc_net[n]]
print(f'   same name, different pin set: {len(bad)}')
for n in sorted(bad)[:5]:
    print(f'     {n}: IR-only={sorted(ir_net[n] - doc_net[n])[:4]} '
          f'doc-only={sorted(doc_net[n] - ir_net[n])[:4]}')
