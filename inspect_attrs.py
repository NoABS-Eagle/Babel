import xml.etree.ElementTree as ET
tree = ET.parse("outputs/eagle_exp.swlib")
root = tree.getroot()
for comp in root.findall("component"):
    print("COMP:", comp.get("name"), "prefix:", comp.get("prefix"))
    attrs = comp.find("attributes")
    if attrs is not None:
        for a in attrs:
            print("  attr:", a.attrib)
