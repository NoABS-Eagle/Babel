import xml.etree.ElementTree as ET
from babel.ir_util import component_gates
from babel.altium_exporter import _pin_des_map

def _derive_pin_map(sym_name, ir_root):
    from babel.ir_util import component_gates
    for comp_el in ir_root.findall("component"):
        for gate_name, sname in component_gates(comp_el):
            if sname == sym_name:
                return _pin_des_map(comp_el, gate_name)
    return {}

tree = ET.parse("outputs/eagle_exp.swlib")
root = tree.getroot()

for cname in ["74AHC125", "AON7804", "BC847"]:
    comp = next(c for c in root.findall("component") if c.get("name") == cname)
    gs = component_gates(comp)
    print(f"\n{cname} gates:")
    for gate_name, sym_name in gs:
        dm = _pin_des_map(comp, gate_name)
        print(f"  gate={gate_name!r} sym={sym_name!r}  map={dm}")

print("\nDerived pin maps per symbol:")
for sym_name in ["3-STATE_BUFFER", "PWR+-", "NPN", "N-MOSFET"]:
    dm = _derive_pin_map(sym_name, root)
    print(f"  {sym_name!r}: {dm}")
