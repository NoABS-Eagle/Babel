import xml.etree.ElementTree as ET
tree = ET.parse("outputs/eagle_exp.swlib")
root = tree.getroot()
c = next(x for x in root.findall("component") if x.get("name") == "1825027")
print(repr(c.get("prefix")))
