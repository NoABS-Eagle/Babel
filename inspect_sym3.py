import xml.etree.ElementTree as ET
tree = ET.parse("outputs/eagle_exp.swlib")
root = tree.getroot()
syms = {}
for el in root.findall("symbols/symbol"):
    syms[el.get("name")] = el

for name in ["TS+2", "N-MOSFET", "3-STATE_BUFFER"]:
    sym = syms.get(name)
    if sym is None:
        continue
    print(f"SYM: {name}")
    for c in sym:
        if c.tag == "text":
            print(f"  [{c.get('layer')}] {repr(c.text)} @ x={c.get('x')} y={c.get('y')}")
