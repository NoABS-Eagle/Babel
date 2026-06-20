import xml.etree.ElementTree as ET
tree = ET.parse("outputs/eagle_exp.swlib")
root = tree.getroot()
for comp in root.findall("component"):
    fp = comp.find("footprint")
    if fp is None:
        continue
    print("=== footprint:", fp.get("name"))
    for layer in fp:
        for el in layer:
            if el.tag == "text":
                print(" ", layer.tag, el.attrib, repr(el.text))
    break
