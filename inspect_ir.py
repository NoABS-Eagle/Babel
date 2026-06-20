import xml.etree.ElementTree as ET
from babel.ir_util import component_gates

tree = ET.parse("outputs/eagle_exp.swlib")
root = tree.getroot()

syms = root.findall("symbols/symbol")
print(f"Symbols ({len(syms)}):")
for s in syms:
    print(f"  {s.get('name')}")

comps = root.findall("component")
print(f"\nComponents ({len(comps)}):")
for c in comps[:10]:
    gs = component_gates(c)
    fp = c.find("footprint")
    fp_name = fp.get("name", "-") if fp is not None else "-"
    attrs = c.find("attributes")
    attr_keys = [a.get("name") for a in (attrs.findall("attr") if attrs is not None else [])]
    prefix = c.get('prefix') or '?'
    print(f"  {c.get('name'):20s} prefix={prefix:<3s} gates={[s for _,s in gs]}  fp={fp_name}  attrs={attr_keys}")
