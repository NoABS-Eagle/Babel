from altium_monkey import AltiumSchLib
lib = AltiumSchLib("testData/finalTest/altium_out/eagle_src.SchLib")
for sym in lib.symbols:
    n_pins  = len(list(sym.pins))
    n_des   = len(list(sym.designators))
    n_param = len(list(sym.parameters))
    n_label = len(list(sym.labels))
    pc      = getattr(sym, "part_count", 1)
    print("  %-20s pins=%-3d designators=%-2d params=%-3d labels=%-3d parts=%d" % (
        sym.name, n_pins, n_des, n_param, n_label, pc))
    for d in sym.designators:
        print("    DES: text=%r  x=%s y=%s  auto=%s" % (
            d.text, d.location.x if hasattr(d,'location') else '?',
            d.location.y if hasattr(d,'location') else '?',
            getattr(d, 'auto_position', '?')))
    for p in sym.parameters:
        print("    PAR: name=%r  hidden=%s  x=%s y=%s" % (
            p.name, p.is_hidden,
            p.location.x if hasattr(p,'location') else '?',
            p.location.y if hasattr(p,'location') else '?'))
    for lbl in sym.labels:
        print("    LBL: text=%r  font_id=%s" % (
            lbl.text, getattr(lbl, 'font_id', '?')))
