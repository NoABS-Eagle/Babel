from altium_monkey import AltiumSchLib
lib = AltiumSchLib("testData/out/altium/eagle_src.SchLib")
for sym in lib.symbols:
    impls = list(sym.implementations) if hasattr(sym, 'implementations') else []
    print("  %-20s  %d impl(s)" % (sym.name, len(impls)))
    for imp in impls:
        mn = getattr(imp, 'model_name', getattr(imp, 'name', '?'))
        ic = getattr(imp, 'is_current', '?')
        print("    fp=%-25r  is_current=%s" % (mn, ic))
