import xml.etree.ElementTree as ET

tree = ET.parse("outputs/eagle_exp.swlib")
root = tree.getroot()

for comp in root.findall("component"):
    for fp in comp.findall("footprint"):
        m = fp.find("model3d")
        if m is not None:
            print(f"{comp.get('name')} / {fp.get('name')}:")
            print(f"  attrs: {dict(m.attrib)}")
            print(f"  text:  {repr(m.text)}")
