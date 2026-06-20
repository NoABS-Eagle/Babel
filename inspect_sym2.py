import xml.etree.ElementTree as ET
tree = ET.parse("outputs/eagle_exp.swlib")
root = tree.getroot()
syms = {}
for el in root.findall("symbols/symbol"):
    syms[el.get("name")] = el

for name, sym in syms.items():
    texts = [(c.attrib, c.text) for c in sym if c.tag == "text"]
    if texts:
        print(f"SYM: {name}")
        for a, t in texts:
            print(f"  [{a.get('layer')}] {repr(t)}")
