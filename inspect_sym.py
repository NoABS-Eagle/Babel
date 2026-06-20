import xml.etree.ElementTree as ET
tree = ET.parse("outputs/eagle_exp.swlib")
root = tree.getroot()
syms = {}
for el in root.findall("symbols/symbol"):
    syms[el.get("name")] = el

for name, sym in list(syms.items())[:2]:
    print("SYM:", name)
    for child in sym:
        if child.tag == "text":
            print("  text:", child.attrib, repr(child.text))

# Also check component prefix
for comp in root.findall("component")[:3]:
    print("COMP:", comp.get("name"), "prefix:", comp.get("prefix", "?"))
